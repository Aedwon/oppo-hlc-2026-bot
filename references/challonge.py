"""
Challonge Integration Cog

Provides Discord slash commands to:
- Link Challonge brackets to channels
- View open matches
- Report match results
- Manage bracket connections
"""

import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone
from typing import Optional, List
import json
import os

from utils.challonge_client import (
    ChallongeClient,
    ChallongeAPIError,
    parse_challonge_url,
    build_participant_cache,
    find_participant_by_name,
    format_match_display
)

# Data persistence
BRACKETS_FILE = "data/challonge_brackets.json"

# Permission: Marshal role ID (from matches.py)
MARSHAL_ROLE_ID = 1176872289501974529


def load_brackets() -> dict:
    """Load bracket mappings from disk."""
    if not os.path.exists(BRACKETS_FILE):
        return {}
    try:
        with open(BRACKETS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_brackets(data: dict):
    """Save bracket mappings to disk."""
    os.makedirs(os.path.dirname(BRACKETS_FILE), exist_ok=True)
    with open(BRACKETS_FILE, 'w') as f:
        json.dump(data, f, indent=4)


def get_channel_bracket(channel_id: int) -> Optional[dict]:
    """Get bracket info for a specific channel."""
    brackets = load_brackets()
    return brackets.get(str(channel_id))


def set_channel_bracket(channel_id: int, bracket_data: dict):
    """Set bracket info for a channel."""
    brackets = load_brackets()
    brackets[str(channel_id)] = bracket_data
    save_brackets(brackets)


def remove_channel_bracket(channel_id: int) -> bool:
    """Remove bracket link from a channel. Returns True if existed."""
    brackets = load_brackets()
    if str(channel_id) in brackets:
        del brackets[str(channel_id)]
        save_brackets(brackets)
        return True
    return False


def has_permission(member: discord.Member) -> bool:
    """Check if member has permission to use Challonge commands."""
    # Admins always have permission
    if member.guild_permissions.administrator:
        return True
    # Check for Marshal role
    if discord.utils.get(member.roles, id=MARSHAL_ROLE_ID):
        return True
    return False


class WinnerSelect(discord.ui.Select):
    """Dropdown to select match winner from participants."""
    
    def __init__(self, participants: dict, match_num: int):
        self.participants = participants
        self.match_num = match_num
        
        options = [
            discord.SelectOption(label=name[:100], value=str(pid))
            for pid, name in list(participants.items())[:25]  # Discord limit
        ]
        
        super().__init__(
            placeholder="Select the winner...",
            options=options,
            min_values=1,
            max_values=1
        )
    
    async def callback(self, interaction: discord.Interaction):
        # This will be handled by the parent view
        self.view.selected_winner_id = int(self.values[0])
        self.view.selected_winner_name = self.participants.get(int(self.values[0]), "Unknown")
        await interaction.response.defer()
        self.view.stop()


class WinnerSelectView(discord.ui.View):
    """View containing winner selection dropdown."""
    
    def __init__(self, participants: dict, match_num: int, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.selected_winner_id = None
        self.selected_winner_name = None
        self.add_item(WinnerSelect(participants, match_num))


class Challonge(commands.Cog):
    """Challonge bracket integration commands."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = None  # Lazy init to handle missing API key gracefully
    
    def _get_client(self) -> ChallongeClient:
        """Get or create Challonge client."""
        if self.client is None:
            self.client = ChallongeClient()
        return self.client
    
    @app_commands.command(
        name="challonge_link",
        description="Link a Challonge bracket to this channel"
    )
    @app_commands.describe(url="Full Challonge bracket URL (e.g., https://challonge.com/msl_week1)")
    async def challonge_link(self, interaction: discord.Interaction, url: str):
        """Link a Challonge tournament to the current channel."""
        
        # Permission check
        if not has_permission(interaction.user):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin permissions to link brackets.",
                ephemeral=True
            )
            return
        
        # Check if already linked
        existing = get_channel_bracket(interaction.channel_id)
        if existing:
            await interaction.response.send_message(
                f"❌ This channel is already linked to **{existing.get('tournament_name', 'a bracket')}**.\n"
                "Use `/challonge_unlink` first to remove the existing link.",
                ephemeral=True
            )
            return
        
        # Parse URL
        slug = parse_challonge_url(url)
        if not slug:
            await interaction.response.send_message(
                "❌ Invalid Challonge URL format.\n"
                "Expected: `https://challonge.com/your_tournament` or `https://subdomain.challonge.com/your_tournament`",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(thinking=True)
        
        try:
            client = self._get_client()
            
            # Validate tournament exists
            success, tournament, error = await client.validate_tournament(slug)
            if not success:
                await interaction.followup.send(f"❌ {error}")
                return
            
            # Fetch participants for caching
            participants = await client.get_participants(slug)
            participant_cache = build_participant_cache(participants)
            
            # Save bracket link
            bracket_data = {
                "tournament_slug": slug,
                "tournament_name": tournament.get("name", "Unknown Tournament"),
                "tournament_id": tournament.get("id"),
                "url": tournament.get("full_challonge_url", url),
                "state": tournament.get("state", "unknown"),
                "linked_by": interaction.user.id,
                "linked_at": datetime.now(timezone.utc).isoformat(),
                "participants_cache": {str(k): v for k, v in participant_cache.items()}
            }
            set_channel_bracket(interaction.channel_id, bracket_data)
            
            # Success embed
            embed = discord.Embed(
                title="✅ Bracket Linked",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(
                name="Tournament",
                value=f"[{tournament.get('name', 'Unknown')}]({tournament.get('full_challonge_url', url)})",
                inline=False
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
    
    @app_commands.command(
        name="challonge_unlink",
        description="Remove the Challonge bracket link from this channel"
    )
    async def challonge_unlink(self, interaction: discord.Interaction):
        """Remove bracket link from the current channel."""
        
        if not has_permission(interaction.user):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin permissions to unlink brackets.",
                ephemeral=True
            )
            return
        
        bracket = get_channel_bracket(interaction.channel_id)
        if not bracket:
            await interaction.response.send_message(
                "❌ No bracket is linked to this channel.",
                ephemeral=True
            )
            return
        
        tournament_name = bracket.get("tournament_name", "the bracket")
        remove_channel_bracket(interaction.channel_id)
        
        await interaction.response.send_message(
            f"✅ Unlinked **{tournament_name}** from this channel."
        )
    
    @app_commands.command(
        name="challonge_matches",
        description="List matches from the linked Challonge bracket"
    )
    @app_commands.describe(show_completed="Include completed matches (default: False)")
    async def challonge_matches(
        self, 
        interaction: discord.Interaction, 
        show_completed: bool = False
    ):
        """Display matches from the linked bracket."""
        
        bracket = get_channel_bracket(interaction.channel_id)
        if not bracket:
            await interaction.response.send_message(
                "❌ No bracket linked to this channel. Use `/challonge_link` first.",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(thinking=True)
        
        try:
            client = self._get_client()
            slug = bracket["tournament_slug"]
            
            # Fetch matches
            if show_completed:
                matches = await client.get_matches(slug, state="all")
            else:
                matches = await client.get_matches(slug, state="open")
            
            # Refresh participant cache
            participants = await client.get_participants(slug)
            participant_cache = build_participant_cache(participants)
            
            # Update cache in storage
            bracket["participants_cache"] = {str(k): v for k, v in participant_cache.items()}
            set_channel_bracket(interaction.channel_id, bracket)
            
            if not matches:
                state_desc = "open or pending" if not show_completed else ""
                await interaction.followup.send(
                    f"📋 No {state_desc} matches found in **{bracket['tournament_name']}**."
                )
                return
            
            # Sort by suggested play order
            matches.sort(key=lambda m: m.get("suggested_play_order") or m.get("id") or 0)
            
            # Build match list
            embed = discord.Embed(
                title=f"📋 Matches: {bracket['tournament_name']}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            # Group by state
            open_matches = [m for m in matches if m.get("state") == "open"]
            pending_matches = [m for m in matches if m.get("state") == "pending"]
            complete_matches = [m for m in matches if m.get("state") == "complete"]
            
            if open_matches:
                lines = [format_match_display(m, participant_cache) for m in open_matches[:10]]
                embed.add_field(
                    name=f"🔵 Open ({len(open_matches)})",
                    value="\n".join(lines) or "None",
                    inline=False
                )
            
            if pending_matches:
                lines = [format_match_display(m, participant_cache) for m in pending_matches[:5]]
                embed.add_field(
                    name=f"⏳ Pending ({len(pending_matches)})",
                    value="\n".join(lines) or "None",
                    inline=False
                )
            
            if show_completed and complete_matches:
                lines = [format_match_display(m, participant_cache, include_state=True) for m in complete_matches[-5:]]
                embed.add_field(
                    name=f"✅ Completed ({len(complete_matches)})",
                    value="\n".join(lines) or "None",
                    inline=False
                )
            
            embed.set_footer(text="Use /challonge_report to submit results")
            
            await interaction.followup.send(embed=embed)
            
        except ChallongeAPIError as e:
            await interaction.followup.send(f"❌ Challonge API error: {e.message}")
        except Exception as e:
            await interaction.followup.send(f"❌ Error fetching matches: {e}")
    
    @app_commands.command(
        name="challonge_report",
        description="Report a match result to the linked Challonge bracket"
    )
    @app_commands.describe(
        match_number="Match number from the bracket (shown in /challonge_matches)",
        winner="Name of the winning team/player",
        score="Score in X-Y format (e.g., 2-1)"
    )
    async def challonge_report(
        self,
        interaction: discord.Interaction,
        match_number: int,
        winner: str,
        score: str
    ):
        """Report a match result to Challonge."""
        
        if not has_permission(interaction.user):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin permissions to report results.",
                ephemeral=True
            )
            return
        
        bracket = get_channel_bracket(interaction.channel_id)
        if not bracket:
            await interaction.response.send_message(
                "❌ No bracket linked to this channel. Use `/challonge_link` first.",
                ephemeral=True
            )
            return
        
        # Validate score format
        import re
        if not re.match(r"^\d+-\d+$", score):
            await interaction.response.send_message(
                "❌ Invalid score format. Use X-Y format (e.g., `2-1`, `3-0`).",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(thinking=True)
        
        try:
            client = self._get_client()
            slug = bracket["tournament_slug"]
            
            # Fetch matches to find the one by match number
            matches = await client.get_matches(slug, state="all")
            
            # Find match by suggested_play_order
            target_match = None
            for m in matches:
                if (m.get("suggested_play_order") or m.get("id")) == match_number:
                    target_match = m
                    break
            
            if not target_match:
                await interaction.followup.send(
                    f"❌ Match #{match_number} not found in the bracket."
                )
                return
            
            # Check if already complete
            if target_match.get("state") == "complete":
                await interaction.followup.send(
                    f"❌ Match #{match_number} already has a result.\n"
                    f"Score: {target_match.get('scores_csv', 'N/A')}"
                )
                return
            
            # Check if match is ready (both players known)
            if not target_match.get("player1_id") or not target_match.get("player2_id"):
                await interaction.followup.send(
                    f"❌ Match #{match_number} is pending - waiting for previous matches to complete."
                )
                return
            
            # Get fresh participant list
            participants = await client.get_participants(slug)
            participant_cache = build_participant_cache(participants)
            
            # Find winner
            found = find_participant_by_name(participant_cache, winner)
            if not found:
                # List available participants for this match
                p1_name = participant_cache.get(target_match["player1_id"], "Unknown")
                p2_name = participant_cache.get(target_match["player2_id"], "Unknown")
                await interaction.followup.send(
                    f"❌ Participant '{winner}' not found.\n"
                    f"This match is between: **{p1_name}** vs **{p2_name}**"
                )
                return
            
            winner_id, winner_name = found
            
            # Verify winner is in this match
            if winner_id not in [target_match.get("player1_id"), target_match.get("player2_id")]:
                p1_name = participant_cache.get(target_match["player1_id"], "Unknown")
                p2_name = participant_cache.get(target_match["player2_id"], "Unknown")
                await interaction.followup.send(
                    f"❌ **{winner_name}** is not in match #{match_number}.\n"
                    f"This match is between: **{p1_name}** vs **{p2_name}**"
                )
                return
            
            # Report the result
            updated_match = await client.update_match(
                slug,
                target_match["id"],
                winner_id,
                score
            )
            
            # Get opponent name
            loser_id = target_match["player1_id"] if winner_id == target_match["player2_id"] else target_match["player2_id"]
            loser_name = participant_cache.get(loser_id, "Unknown")
            
            # Success embed
            embed = discord.Embed(
                title="✅ Match Result Reported",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
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
    
    @app_commands.command(
        name="challonge_bracket",
        description="Show info about the linked Challonge bracket"
    )
    async def challonge_bracket(self, interaction: discord.Interaction):
        """Display bracket info and quick link."""
        
        bracket = get_channel_bracket(interaction.channel_id)
        if not bracket:
            await interaction.response.send_message(
                "❌ No bracket linked to this channel. Use `/challonge_link` first.",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(thinking=True)
        
        try:
            client = self._get_client()
            slug = bracket["tournament_slug"]
            
            # Fetch fresh tournament data
            tournament = await client.get_tournament(slug)
            matches = await client.get_matches(slug, state="all")
            
            # Calculate stats
            total_matches = len(matches)
            complete_matches = len([m for m in matches if m.get("state") == "complete"])
            open_matches = len([m for m in matches if m.get("state") == "open"])
            
            # Build embed
            embed = discord.Embed(
                title=f"🏆 {tournament.get('name', bracket['tournament_name'])}",
                url=tournament.get("full_challonge_url", bracket.get("url")),
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            
            state = tournament.get("state", "unknown")
            state_emoji = {"pending": "⏳", "underway": "🔵", "complete": "✅"}.get(state, "❓")
            
            embed.add_field(name="State", value=f"{state_emoji} {state.title()}", inline=True)
            embed.add_field(name="Participants", value=str(tournament.get("participants_count", "?")), inline=True)
            embed.add_field(name="Game", value=tournament.get("game_name", "N/A"), inline=True)
            
            embed.add_field(
                name="Progress",
                value=f"{complete_matches}/{total_matches} matches completed\n"
                      f"{open_matches} matches ready to play",
                inline=False
            )
            
            linked_at = bracket.get("linked_at", "Unknown")
            if linked_at != "Unknown":
                try:
                    dt = datetime.fromisoformat(linked_at)
                    linked_at = discord.utils.format_dt(dt, style="R")
                except:
                    pass
            
            embed.set_footer(text=f"Linked {linked_at}")
            
            await interaction.followup.send(embed=embed)
            
        except ChallongeAPIError as e:
            # Fall back to cached data
            embed = discord.Embed(
                title=f"🏆 {bracket['tournament_name']}",
                url=bracket.get("url"),
                color=discord.Color.orange(),
                description=f"⚠️ Could not fetch live data: {e.message}\n\nShowing cached information."
            )
            embed.add_field(name="State", value=bracket.get("state", "unknown").title(), inline=True)
            embed.add_field(
                name="Cached Participants",
                value=str(len(bracket.get("participants_cache", {}))),
                inline=True
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ Error fetching bracket info: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Challonge(bot))
