"""
Cog: Challonge

Provides Discord slash commands to manage Challonge tournament brackets:

Marshal / Admin commands:
  /challonge_link      -- link a bracket to this channel
  /challonge_unlink    -- remove the bracket link
  /challonge_report    -- report a match result

Anyone:
  /challonge_matches   -- list open / pending / completed matches
  /challonge_bracket   -- show bracket info and progress
"""
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone
from typing import Optional
import re

from db.database import Database
from utils.challonge_client import (
    ChallongeClient,
    ChallongeAPIError,
    parse_challonge_url,
    build_participant_cache,
    find_participant_by_name,
    format_match_display,
)


# ────────────────────────────────────────────────────────────────
# Permission helper  (reuses marshal role from guild_config)
# ────────────────────────────────────────────────────────────────

async def _is_marshal_or_admin(interaction: discord.Interaction) -> bool:
    """Check if the user has the marshal role or is admin."""
    from utils.constants import ROLE_MARSHAL

    user = interaction.user
    if user.guild_permissions.administrator:
        return True

    marshal_role_id = ROLE_MARSHAL
    if not marshal_role_id:
        cfg = await Database.get_config(interaction.guild_id, "marshal_role_id")
        if cfg:
            marshal_role_id = int(cfg)

    if marshal_role_id and discord.utils.get(user.roles, id=marshal_role_id):
        return True

    return False


# ────────────────────────────────────────────────────────────────
# DB helpers for bracket-channel links
# ────────────────────────────────────────────────────────────────

async def get_channel_bracket(guild_id: int, channel_id: int) -> Optional[dict]:
    """Get the bracket linked to a channel."""
    return await Database.fetchone(
        "SELECT * FROM challonge_brackets WHERE guild_id = %s AND channel_id = %s",
        (guild_id, channel_id),
    )


async def set_channel_bracket(
    guild_id: int,
    channel_id: int,
    tournament_slug: str,
    tournament_name: str,
    tournament_url: str,
    state: str,
    linked_by: int,
) -> None:
    """Link a bracket to a channel (insert or update)."""
    await Database.execute(
        "INSERT INTO challonge_brackets "
        "(guild_id, channel_id, tournament_slug, tournament_name, tournament_url, state, linked_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE tournament_slug=VALUES(tournament_slug), "
        "tournament_name=VALUES(tournament_name), tournament_url=VALUES(tournament_url), "
        "state=VALUES(state), linked_by=VALUES(linked_by)",
        (guild_id, channel_id, tournament_slug, tournament_name, tournament_url, state, linked_by),
    )


async def remove_channel_bracket(guild_id: int, channel_id: int) -> bool:
    """Remove bracket link. Returns True if a row was deleted."""
    rows = await Database.execute(
        "DELETE FROM challonge_brackets WHERE guild_id = %s AND channel_id = %s",
        (guild_id, channel_id),
    )
    return rows > 0


# ────────────────────────────────────────────────────────────────
# Cog
# ────────────────────────────────────────────────────────────────

class Challonge(commands.Cog):
    """Challonge bracket integration commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._client: Optional[ChallongeClient] = None

    def _get_client(self) -> ChallongeClient:
        """Lazy-init the API client."""
        if self._client is None:
            self._client = ChallongeClient()
        return self._client

    # -- /challonge_link -------------------------------------------------------

    @app_commands.command(
        name="challonge_link",
        description="Link a Challonge bracket to this channel.",
    )
    @app_commands.describe(url="Full Challonge URL (e.g. https://challonge.com/my_tourney)")
    async def challonge_link(self, interaction: discord.Interaction, url: str):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin to link brackets.", ephemeral=True
            )
            return

        # Check if already linked
        existing = await get_channel_bracket(interaction.guild_id, interaction.channel_id)
        if existing:
            await interaction.response.send_message(
                f"❌ This channel is already linked to **{existing['tournament_name']}**.\n"
                "Use `/challonge_unlink` first to remove the existing link.",
                ephemeral=True,
            )
            return

        # Parse URL
        slug = parse_challonge_url(url)
        if not slug:
            await interaction.response.send_message(
                "❌ Invalid Challonge URL.\n"
                "Expected: `https://challonge.com/your_tournament` or just a slug like `my_tourney`",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            client = self._get_client()
            success, tournament, error = await client.validate_tournament(slug)
            if not success:
                await interaction.followup.send(f"❌ {error}")
                return

            # Fetch participants to verify connectivity
            participants = await client.get_participants(slug)

            # Save to DB
            await set_channel_bracket(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                tournament_slug=slug,
                tournament_name=tournament.get("name", "Unknown"),
                tournament_url=tournament.get("full_challonge_url", url),
                state=tournament.get("state", "unknown"),
                linked_by=interaction.user.id,
            )

            embed = discord.Embed(
                title="✅ Bracket Linked",
                color=0x00CC66,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="Tournament",
                value=f"[{tournament.get('name', 'Unknown')}]({tournament.get('full_challonge_url', url)})",
                inline=False,
            )
            embed.add_field(name="State", value=tournament.get("state", "unknown").title(), inline=True)
            embed.add_field(name="Participants", value=str(len(participants)), inline=True)
            embed.set_footer(text=f"Linked by {interaction.user.display_name}")

            await interaction.followup.send(embed=embed)

        except ValueError as e:
            await interaction.followup.send(f"❌ Configuration error: {e}")
        except ChallongeAPIError as e:
            await interaction.followup.send(f"❌ Challonge API error: {e.message}")
        except Exception as e:
            await interaction.followup.send(f"❌ Unexpected error: {e}")

    # -- /challonge_unlink -----------------------------------------------------

    @app_commands.command(
        name="challonge_unlink",
        description="Remove the Challonge bracket link from this channel.",
    )
    async def challonge_unlink(self, interaction: discord.Interaction):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin to unlink brackets.", ephemeral=True
            )
            return

        bracket = await get_channel_bracket(interaction.guild_id, interaction.channel_id)
        if not bracket:
            await interaction.response.send_message("❌ No bracket linked to this channel.", ephemeral=True)
            return

        name = bracket["tournament_name"]
        await remove_channel_bracket(interaction.guild_id, interaction.channel_id)
        await interaction.response.send_message(f"✅ Unlinked **{name}** from this channel.")

    # -- /challonge_matches ----------------------------------------------------

    @app_commands.command(
        name="challonge_matches",
        description="List matches from the linked Challonge bracket.",
    )
    @app_commands.describe(show_completed="Include completed matches (default: False)")
    async def challonge_matches(
        self, interaction: discord.Interaction, show_completed: bool = False
    ):
        bracket = await get_channel_bracket(interaction.guild_id, interaction.channel_id)
        if not bracket:
            await interaction.response.send_message(
                "❌ No bracket linked. Use `/challonge_link` first.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            client = self._get_client()
            slug = bracket["tournament_slug"]

            state = "all" if show_completed else "open"
            matches = await client.get_matches(slug, state=state)

            # Also fetch pending if not showing all
            if not show_completed:
                pending = await client.get_matches(slug, state="pending")
                matches.extend(pending)

            # Build participant cache
            participants = await client.get_participants(slug)
            participant_cache = build_participant_cache(participants)

            if not matches:
                desc = "open or pending" if not show_completed else ""
                await interaction.followup.send(
                    f"📋 No {desc} matches found in **{bracket['tournament_name']}**."
                )
                return

            # Sort by play order
            matches.sort(key=lambda m: m.get("suggested_play_order") or m.get("id") or 0)

            embed = discord.Embed(
                title=f"📋 Matches: {bracket['tournament_name']}",
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc),
            )

            # Group by state
            open_matches = [m for m in matches if m.get("state") == "open"]
            pending_matches = [m for m in matches if m.get("state") == "pending"]
            complete_matches = [m for m in matches if m.get("state") == "complete"]

            if open_matches:
                lines = [format_match_display(m, participant_cache) for m in open_matches[:10]]
                value = "\n".join(lines)
                if len(open_matches) > 10:
                    value += f"\n*… and {len(open_matches) - 10} more*"
                embed.add_field(
                    name=f"🔵 Open ({len(open_matches)})",
                    value=value or "None",
                    inline=False,
                )

            if pending_matches:
                lines = [format_match_display(m, participant_cache) for m in pending_matches[:5]]
                value = "\n".join(lines)
                if len(pending_matches) > 5:
                    value += f"\n*… and {len(pending_matches) - 5} more*"
                embed.add_field(
                    name=f"⏳ Pending ({len(pending_matches)})",
                    value=value or "None",
                    inline=False,
                )

            if show_completed and complete_matches:
                lines = [
                    format_match_display(m, participant_cache, include_state=True)
                    for m in complete_matches[-5:]
                ]
                embed.add_field(
                    name=f"✅ Completed ({len(complete_matches)})",
                    value="\n".join(lines) or "None",
                    inline=False,
                )

            embed.set_footer(text="Use /challonge_report to submit results")
            await interaction.followup.send(embed=embed)

        except ChallongeAPIError as e:
            await interaction.followup.send(f"❌ Challonge API error: {e.message}")
        except Exception as e:
            await interaction.followup.send(f"❌ Error fetching matches: {e}")

    # -- /challonge_report -----------------------------------------------------

    @app_commands.command(
        name="challonge_report",
        description="Report a match result to the linked Challonge bracket.",
    )
    @app_commands.describe(
        match_number="Match number from /challonge_matches",
        winner="Name of the winning team/player",
        score="Score in X-Y format (e.g. 2-1)",
    )
    async def challonge_report(
        self,
        interaction: discord.Interaction,
        match_number: int,
        winner: str,
        score: str,
    ):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin to report results.", ephemeral=True
            )
            return

        bracket = await get_channel_bracket(interaction.guild_id, interaction.channel_id)
        if not bracket:
            await interaction.response.send_message(
                "❌ No bracket linked. Use `/challonge_link` first.", ephemeral=True
            )
            return

        # Validate score format
        if not re.match(r"^\d+-\d+$", score):
            await interaction.response.send_message(
                "❌ Invalid score format. Use **X-Y** (e.g. `2-1`, `3-0`).", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            client = self._get_client()
            slug = bracket["tournament_slug"]

            # Fetch all matches to find the target
            matches = await client.get_matches(slug, state="all")
            target = None
            for m in matches:
                if (m.get("suggested_play_order") or m.get("id")) == match_number:
                    target = m
                    break

            if not target:
                await interaction.followup.send(f"❌ Match #{match_number} not found.")
                return

            if target.get("state") == "complete":
                await interaction.followup.send(
                    f"❌ Match #{match_number} already has a result.\n"
                    f"Score: {target.get('scores_csv', 'N/A')}"
                )
                return

            if not target.get("player1_id") or not target.get("player2_id"):
                await interaction.followup.send(
                    f"❌ Match #{match_number} is pending — waiting for previous matches."
                )
                return

            # Resolve winner
            participants = await client.get_participants(slug)
            participant_cache = build_participant_cache(participants)

            found = find_participant_by_name(participant_cache, winner)
            if not found:
                p1 = participant_cache.get(target["player1_id"], "Unknown")
                p2 = participant_cache.get(target["player2_id"], "Unknown")
                await interaction.followup.send(
                    f"❌ Participant '{winner}' not found.\n"
                    f"This match is between: **{p1}** vs **{p2}**"
                )
                return

            winner_id, winner_name = found

            # Verify winner is actually in this match
            if winner_id not in (target.get("player1_id"), target.get("player2_id")):
                p1 = participant_cache.get(target["player1_id"], "Unknown")
                p2 = participant_cache.get(target["player2_id"], "Unknown")
                await interaction.followup.send(
                    f"❌ **{winner_name}** is not in match #{match_number}.\n"
                    f"This match is between: **{p1}** vs **{p2}**"
                )
                return

            # Report
            await client.update_match(slug, target["id"], winner_id, score)

            loser_id = (
                target["player1_id"]
                if winner_id == target["player2_id"]
                else target["player2_id"]
            )
            loser_name = participant_cache.get(loser_id, "Unknown")

            embed = discord.Embed(
                title="✅ Match Result Reported",
                color=0x00CC66,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Match", value=f"#{match_number}", inline=True)
            embed.add_field(name="Score", value=score, inline=True)
            embed.add_field(name="Winner", value=f"🏆 {winner_name}", inline=False)
            embed.add_field(name="Loser", value=loser_name, inline=False)
            embed.set_footer(text=f"Reported by {interaction.user.display_name}")

            await interaction.followup.send(embed=embed)

        except ChallongeAPIError as e:
            await interaction.followup.send(f"❌ Challonge API error: {e.message}")
        except Exception as e:
            await interaction.followup.send(f"❌ Error reporting result: {e}")

    # -- /challonge_bracket ----------------------------------------------------

    @app_commands.command(
        name="challonge_bracket",
        description="Show info about the linked Challonge bracket.",
    )
    async def challonge_bracket(self, interaction: discord.Interaction):
        bracket = await get_channel_bracket(interaction.guild_id, interaction.channel_id)
        if not bracket:
            await interaction.response.send_message(
                "❌ No bracket linked. Use `/challonge_link` first.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            client = self._get_client()
            slug = bracket["tournament_slug"]

            tournament = await client.get_tournament(slug)
            matches = await client.get_matches(slug, state="all")

            total = len(matches)
            complete = len([m for m in matches if m.get("state") == "complete"])
            open_count = len([m for m in matches if m.get("state") == "open"])

            embed = discord.Embed(
                title=f"🏆 {tournament.get('name', bracket['tournament_name'])}",
                url=tournament.get("full_challonge_url", bracket.get("tournament_url")),
                color=0xF2C21A,
                timestamp=datetime.now(timezone.utc),
            )

            state = tournament.get("state", "unknown")
            state_emoji = {"pending": "⏳", "underway": "🔵", "complete": "✅"}.get(state, "❓")

            embed.add_field(name="State", value=f"{state_emoji} {state.title()}", inline=True)
            embed.add_field(
                name="Participants",
                value=str(tournament.get("participants_count", "?")),
                inline=True,
            )
            embed.add_field(
                name="Game",
                value=tournament.get("game_name") or "N/A",
                inline=True,
            )
            embed.add_field(
                name="Progress",
                value=(
                    f"{complete}/{total} matches completed\n"
                    f"{open_count} matches ready to play"
                ),
                inline=False,
            )

            linked_at = bracket.get("linked_at") or bracket.get("created_at")
            if linked_at:
                try:
                    if isinstance(linked_at, str):
                        dt = datetime.fromisoformat(linked_at)
                    else:
                        dt = linked_at
                    embed.set_footer(text=f"Linked {discord.utils.format_dt(dt, style='R')}")
                except Exception:
                    pass

            await interaction.followup.send(embed=embed)

        except ChallongeAPIError as e:
            # Fallback to cached data
            embed = discord.Embed(
                title=f"🏆 {bracket['tournament_name']}",
                url=bracket.get("tournament_url"),
                color=0xFF9900,
                description=f"⚠️ Could not fetch live data: {e.message}\nShowing cached info.",
            )
            embed.add_field(name="State", value=bracket.get("state", "unknown").title(), inline=True)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ Error fetching bracket info: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Challonge(bot))
