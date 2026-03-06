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

    # -- /challonge_report (multi-step interactive) ----------------------------

    @app_commands.command(
        name="challonge_report",
        description="Report a match result to the linked Challonge bracket (guided).",
    )
    async def challonge_report(self, interaction: discord.Interaction):
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

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            client = self._get_client()
            slug = bracket["tournament_slug"]

            # Fetch open matches
            matches = await client.get_matches(slug, state="open")
            if not matches:
                await interaction.followup.send(
                    "📋 No open matches to report. All matches may be completed or pending.",
                    ephemeral=True,
                )
                return

            # Build participant cache
            participants = await client.get_participants(slug)
            participant_cache = build_participant_cache(participants)

            # Filter to matches with both players assigned (not TBD)
            reportable = [
                m for m in matches
                if m.get("player1_id") and m.get("player2_id")
            ]
            if not reportable:
                await interaction.followup.send(
                    "⏳ All open matches are waiting for previous results. "
                    "No matches can be reported yet.",
                    ephemeral=True,
                )
                return

            # Sort by play order
            reportable.sort(
                key=lambda m: m.get("suggested_play_order") or m.get("id") or 0
            )

            # Cap at 25 (Select Menu limit)
            reportable = reportable[:25]

            # Build select options
            options = []
            for m in reportable:
                order = m.get("suggested_play_order") or m.get("id", "?")
                p1 = participant_cache.get(m["player1_id"], "TBD")
                p2 = participant_cache.get(m["player2_id"], "TBD")
                label = f"#{order}: {p1} vs {p2}"
                # Discord labels max 100 chars
                if len(label) > 100:
                    label = label[:97] + "…"
                options.append(
                    discord.SelectOption(
                        label=label,
                        value=str(m["id"]),
                        description=f"Match #{order}",
                    )
                )

            view = _MatchSelectView(
                options=options,
                matches={m["id"]: m for m in reportable},
                participant_cache=participant_cache,
                client=client,
                slug=slug,
                reporter=interaction.user,
            )

            await interaction.followup.send(
                "**Step 1/3:** Select the match to report:",
                view=view,
                ephemeral=True,
            )

        except ChallongeAPIError as e:
            await interaction.followup.send(
                f"❌ Challonge API error: {e.message}", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error fetching matches: {e}", ephemeral=True
            )

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


# ────────────────────────────────────────────────────────────────
# Interactive multi-step Views for challonge_report
# ────────────────────────────────────────────────────────────────

class _MatchSelectView(discord.ui.View):
    """Step 1: Select the match to report."""

    def __init__(
        self,
        *,
        options: list[discord.SelectOption],
        matches: dict,
        participant_cache: dict,
        client,
        slug: str,
        reporter,
    ):
        super().__init__(timeout=120)
        self.matches = matches
        self.participant_cache = participant_cache
        self.client = client
        self.slug = slug
        self.reporter = reporter

        select = discord.ui.Select(
            placeholder="Choose a match…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.reporter.id:
            await interaction.response.send_message(
                "❌ Only the person who started this report can use this.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        match_id = int(interaction.data["values"][0])
        match = self.matches.get(match_id)

        if not match:
            await interaction.response.edit_message(
                content="❌ Match not found. It may have been completed already.",
                view=None,
            )
            return

        p1_id = match.get("player1_id")
        p2_id = match.get("player2_id")
        p1_name = self.participant_cache.get(p1_id, "Unknown")
        p2_name = self.participant_cache.get(p2_id, "Unknown")
        order = match.get("suggested_play_order") or match.get("id", "?")

        # Build winner select with exactly 2 options
        options = [
            discord.SelectOption(
                label=p1_name[:100],
                value=str(p1_id),
                description=f"Select {p1_name[:90]} as the winner",
                emoji="🏆",
            ),
            discord.SelectOption(
                label=p2_name[:100],
                value=str(p2_id),
                description=f"Select {p2_name[:90]} as the winner",
                emoji="🏆",
            ),
        ]

        view = _WinnerSelectView(
            options=options,
            match=match,
            participant_cache=self.participant_cache,
            client=self.client,
            slug=self.slug,
            reporter=self.reporter,
        )

        await interaction.response.edit_message(
            content=(
                f"**Step 2/3:** Match **#{order}** — "
                f"**{p1_name}** vs **{p2_name}**\n"
                "Select the winner:"
            ),
            view=view,
        )
        self.stop()

    async def on_timeout(self):
        pass  # Ephemeral message, auto-cleans


class _WinnerSelectView(discord.ui.View):
    """Step 2: Select the winner."""

    def __init__(
        self,
        *,
        options: list[discord.SelectOption],
        match: dict,
        participant_cache: dict,
        client,
        slug: str,
        reporter,
    ):
        super().__init__(timeout=120)
        self.match = match
        self.participant_cache = participant_cache
        self.client = client
        self.slug = slug
        self.reporter = reporter

        select = discord.ui.Select(
            placeholder="Choose the winner…",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.reporter.id:
            await interaction.response.send_message(
                "❌ Only the person who started this report can use this.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        winner_id = int(interaction.data["values"][0])
        winner_name = self.participant_cache.get(winner_id, "Unknown")

        modal = _ScoreModal(
            match=self.match,
            winner_id=winner_id,
            winner_name=winner_name,
            participant_cache=self.participant_cache,
            client=self.client,
            slug=self.slug,
            reporter=self.reporter,
        )
        await interaction.response.send_modal(modal)
        self.stop()

    async def on_timeout(self):
        pass


class _ScoreModal(discord.ui.Modal, title="Enter Match Score"):
    """Step 3: Enter the score."""

    score_input = discord.ui.TextInput(
        label="Score (X-Y format, e.g. 2-1 or 3-0)",
        placeholder="2-1",
        min_length=3,
        max_length=10,
        required=True,
    )

    def __init__(
        self,
        *,
        match: dict,
        winner_id: int,
        winner_name: str,
        participant_cache: dict,
        client,
        slug: str,
        reporter,
    ):
        super().__init__(timeout=120)
        self.match = match
        self.winner_id = winner_id
        self.winner_name = winner_name
        self.participant_cache = participant_cache
        self.client = client
        self.slug = slug
        self.reporter = reporter

    async def on_submit(self, interaction: discord.Interaction):
        score = self.score_input.value.strip()

        # Validate format
        if not re.match(r"^\d+-\d+$", score):
            await interaction.response.send_message(
                "❌ Invalid score format. Use **X-Y** (e.g. `2-1`, `3-0`).\n"
                "Please run `/challonge_report` again.",
                ephemeral=True,
            )
            return

        # Auto-orient score: Challonge expects scores from player1's perspective
        parts = score.split("-")
        s1, s2 = int(parts[0]), int(parts[1])
        p1_id = self.match.get("player1_id")

        if self.winner_id == p1_id:
            # Winner is player1 — their score (higher) goes first
            oriented_score = f"{max(s1, s2)}-{min(s1, s2)}"
        else:
            # Winner is player2 — their score (higher) goes second
            oriented_score = f"{min(s1, s2)}-{max(s1, s2)}"

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            # Re-check match state (race condition guard)
            matches = await self.client.get_matches(self.slug, state="all")
            current = None
            for m in matches:
                if m.get("id") == self.match["id"]:
                    current = m
                    break

            if not current:
                await interaction.followup.send(
                    "❌ Match not found. It may have been removed.",
                    ephemeral=True,
                )
                return

            if current.get("state") == "complete":
                await interaction.followup.send(
                    "❌ This match was already reported while you were filling in the score.\n"
                    f"Existing score: {current.get('scores_csv', 'N/A')}",
                    ephemeral=True,
                )
                return

            # Submit to Challonge
            await self.client.update_match(
                self.slug, self.match["id"], self.winner_id, oriented_score
            )

            order = self.match.get("suggested_play_order") or self.match.get("id", "?")
            loser_id = (
                self.match["player1_id"]
                if self.winner_id == self.match["player2_id"]
                else self.match["player2_id"]
            )
            loser_name = self.participant_cache.get(loser_id, "Unknown")

            embed = discord.Embed(
                title="✅ Match Result Reported",
                color=0x00CC66,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Match", value=f"#{order}", inline=True)
            embed.add_field(name="Score", value=oriented_score, inline=True)
            embed.add_field(
                name="Winner", value=f"🏆 {self.winner_name}", inline=False
            )
            embed.add_field(name="Loser", value=loser_name, inline=False)
            embed.set_footer(
                text=f"Reported by {self.reporter.display_name}"
            )

            # Post result publicly so everyone sees it
            channel = interaction.channel
            await channel.send(embed=embed)
            await interaction.followup.send(
                "✅ Result submitted!", ephemeral=True
            )

        except ChallongeAPIError as e:
            await interaction.followup.send(
                f"❌ Challonge API error: {e.message}", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Error reporting result: {e}", ephemeral=True
            )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ):
        await interaction.response.send_message(
            f"❌ An error occurred: {error}. Please try `/challonge_report` again.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Challonge(bot))
