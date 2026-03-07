"""
Cog: Matches

Admin commands:
  /set_marshal_role    -- configure which role acts as Marshal

Marshal / Admin commands:
  /match_start         -- start a BO(X) match session (with team names)
  /game_started        -- signal game begun; cancels grace period
  /game_result         -- log a game result, triggers ack flow + 5-min auto-ack
  /match_undo_game     -- remove the last logged game
  /match_force_ack     -- force-acknowledge for a team (5 min cooldown)
  /match_end           -- end the match (validates enough games)
  /match_cancel        -- force-cancel without saving
  /match_history       -- view recent match results
  /grace_period        -- start a 15-min grace period countdown

Anyone:
  /match_status        -- view current match state

Flow:
  1. Marshal starts grace period with /grace_period (optional)
  2. Marshal starts a match session with /match_start (with team names)
  3. Marshal signals game begin with /game_started (cancels grace period)
  4. After each game, marshal logs the result with /game_result
  5. 5-minute dispute window auto-starts; teams type "I acknowledge"
  6. If both teams ack -> window closes, result is final
  7. If 5 min expires -> auto-force-ack, result is final
  8. Anyone can file a dispute (pauses ack timer)
  9. Match ends when enough games are played and marshal uses /match_end
"""
import asyncio
import re
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from db.database import Database
from utils.constants import ROLE_MARSHAL

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


# ────────────────────────────────────────────────────────────────
# In-memory session cache  (channel_id → MatchSession)
# ────────────────────────────────────────────────────────────────

active_matches: Dict[int, "MatchSession"] = {}

# In-memory: channel_id → asyncio.Task for dispute auto-ack countdowns
_ack_countdown_tasks: Dict[int, asyncio.Task] = {}

# In-memory: channel_id → (asyncio.Task, marshal_id) for grace period timers
_grace_period_tasks: Dict[int, tuple[asyncio.Task, int]] = {}


# ────────────────────────────────────────────────────────────────
# MatchSession model
# ────────────────────────────────────────────────────────────────

class MatchSession:
    """Represents an active match session, backed by DB rows."""

    def __init__(
        self,
        *,
        db_id: int,
        guild_id: int,
        channel_id: int,
        marshal_id: int,
        best_of: int,
        team1: Optional[str] = None,
        team2: Optional[str] = None,
        status: str = "ongoing",
        is_disputed: bool = False,
        ack_start_time: Optional[datetime] = None,
        dispute_start_time: Optional[datetime] = None,
        total_dispute_seconds: int = 0,
        last_message_id: Optional[int] = None,
        started_at: Optional[datetime] = None,
        games: Optional[list] = None,
    ):
        self.db_id = db_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.marshal_id = marshal_id
        self.best_of = best_of
        self.team1 = team1
        self.team2 = team2
        self.status = status
        self.is_disputed = is_disputed
        self.ack_start_time = ack_start_time
        self.dispute_start_time = dispute_start_time
        self.total_dispute_seconds = total_dispute_seconds
        self.last_message_id = last_message_id
        self.started_at = started_at or datetime.now(timezone.utc)
        # Each game: {db_id, game_number, result, acks: {team: {user, timestamp}}, created_at}
        self.games: list[dict] = games or []

    # -- DB sync helpers -----------------------------------------------------

    async def _sync_session(self):
        """Persist session-level fields to DB."""
        await Database.execute(
            "UPDATE match_sessions SET status=%s, is_disputed=%s, "
            "ack_start_time=%s, dispute_start_time=%s, total_dispute_seconds=%s, "
            "last_message_id=%s, team1=%s, team2=%s, ended_at=%s WHERE id=%s",
            (
                self.status,
                self.is_disputed,
                self.ack_start_time,
                self.dispute_start_time,
                self.total_dispute_seconds,
                self.last_message_id,
                self.team1,
                self.team2,
                datetime.now(timezone.utc) if self.status == "ended" else None,
                self.db_id,
            ),
        )

    async def add_game(self, result: str) -> dict:
        """Log a new game result and enter checking_ack state."""
        game_number = len(self.games) + 1
        now = datetime.now(timezone.utc)

        game_db_id = await Database.insert_get_id(
            "INSERT INTO match_games (session_id, game_number, result) VALUES (%s, %s, %s)",
            (self.db_id, game_number, result),
        )

        game = {
            "db_id": game_db_id,
            "game_number": game_number,
            "result": result,
            "acks": {},  # team_abbrev → {user: str, timestamp: datetime}
            "created_at": now,
        }
        self.games.append(game)

        self.status = "checking_ack"
        self.ack_start_time = now
        self.dispute_start_time = None
        self.total_dispute_seconds = 0
        self.is_disputed = False
        await self._sync_session()
        return game

    async def undo_game(self) -> bool:
        """Remove the last game entry. Returns True if successful."""
        if not self.games:
            return False

        game = self.games.pop()
        await Database.execute("DELETE FROM match_games WHERE id = %s", (game["db_id"],))

        self.status = "ongoing"
        self.ack_start_time = None
        self.dispute_start_time = None
        self.total_dispute_seconds = 0
        self.is_disputed = False
        await self._sync_session()
        return True

    async def ack_game(self, team_abbrev: str, user_display_name: str) -> bool:
        """Record a team ack. Returns True when both teams have acked."""
        if self.status != "checking_ack" or not self.games:
            return False

        now = datetime.now(timezone.utc)
        game = self.games[-1]
        game["acks"][team_abbrev] = {"user": user_display_name, "timestamp": now}

        # Determine which ack slot to use in DB
        slot = "ack_team1" if len(game["acks"]) <= 1 else "ack_team2"
        # If this team overwrote slot 1, recalculate
        ack_list = list(game["acks"].items())
        if len(ack_list) == 1:
            slot = "ack_team1"
        else:
            slot = "ack_team2"

        await Database.execute(
            f"UPDATE match_games SET {slot}=%s, {slot}_user=%s, {slot}_at=%s WHERE id=%s",
            (team_abbrev, user_display_name, now, game["db_id"]),
        )

        if len(game["acks"]) >= 2:
            self.status = "ongoing"
            await self._sync_session()
            return True

        return False

    def is_current_game_acked(self) -> bool:
        if self.games and len(self.games[-1]["acks"]) >= 2:
            return True
        return False

    def get_effective_elapsed_time(self) -> float:
        """Seconds elapsed since result posted, excluding dispute time."""
        if not self.ack_start_time:
            return 0

        now = datetime.now(timezone.utc)
        total_elapsed = (now - self.ack_start_time).total_seconds()

        current_dispute = 0
        if self.is_disputed and self.dispute_start_time:
            current_dispute = (now - self.dispute_start_time).total_seconds()

        return total_elapsed - self.total_dispute_seconds - current_dispute

    def get_min_games_required(self) -> int:
        """Minimum games to end the match. Even BO plays all; odd BO needs majority."""
        if self.best_of % 2 == 0:
            return self.best_of
        return (self.best_of // 2) + 1

    def get_team_label(self, slot: int = 1) -> str:
        """Return team1 or team2 label, with fallback."""
        name = self.team1 if slot == 1 else self.team2
        return name if name else ("Team A" if slot == 1 else "Team B")

    def get_series_score(self) -> tuple[int, int]:
        """Parse game results to tally wins. Returns (team1_wins, team2_wins).

        Looks for 'X - Y' pattern in the result string.
        The first number is attributed to team1, second to team2.
        """
        import re
        t1_wins = 0
        t2_wins = 0
        for game in self.games:
            result = game.get("result", "")
            match = re.search(r'(\d+)\s*[-\u2013]\s*(\d+)', result)
            if match:
                s1, s2 = int(match.group(1)), int(match.group(2))
                if s1 > s2:
                    t1_wins += 1
                elif s2 > s1:
                    t2_wins += 1
        return t1_wins, t2_wins

    def get_summary_embed(self, *, final: bool = False) -> discord.Embed:
        """Build a rich embed summarising the match."""
        title_parts = ["🏆"]
        if self.team1 and self.team2:
            title_parts.append(f"{self.team1} vs {self.team2}")
        title_parts.append(f"(BO{self.best_of})")

        embed = discord.Embed(title=" ".join(title_parts), color=0xF2C21A)

        if not self.games:
            embed.description = "No games logged yet."
            return embed

        lines = []
        for game in self.games:
            ack_count = len(game["acks"])
            if ack_count >= 2:
                status = "✅ Acknowledged"
            else:
                status = f"⚠️ Waiting ({ack_count}/2)"
            lines.append(f"**Game {game['game_number']}:** {game['result']} — {status}")

        # Series score
        t1_wins, t2_wins = self.get_series_score()
        t1_label = self.get_team_label(1)
        t2_label = self.get_team_label(2)
        lines.append(f"\n**Series Score:** {t1_label} **{t1_wins}** – **{t2_wins}** {t2_label}")

        if final:
            # Duration
            if self.started_at:
                duration = datetime.now(timezone.utc) - self.started_at
                mins = int(duration.total_seconds() // 60)
                lines.append(f"**Duration:** {mins} minute{'s' if mins != 1 else ''}")
            # Winner
            if t1_wins > t2_wins:
                lines.append(f"\n🥇 **Winner: {t1_label}**")
            elif t2_wins > t1_wins:
                lines.append(f"\n🥇 **Winner: {t2_label}**")
            else:
                lines.append("\n🤝 **Series Tied**")

        embed.description = "\n".join(lines)
        return embed


# ────────────────────────────────────────────────────────────────
# Permission helper
# ────────────────────────────────────────────────────────────────

async def _is_marshal_or_admin(interaction: discord.Interaction, session: Optional[MatchSession] = None) -> bool:
    """Check if the user is the session marshal, has the marshal role, or is admin."""
    user = interaction.user

    # Admin always passes
    if user.guild_permissions.administrator:
        return True

    # Session marshal
    if session and user.id == session.marshal_id:
        return True

    # Configured marshal role (from guild_config or env)
    marshal_role_id = ROLE_MARSHAL
    if not marshal_role_id:
        cfg = await Database.get_config(interaction.guild_id, "marshal_role_id")
        if cfg:
            marshal_role_id = int(cfg)

    if marshal_role_id and discord.utils.get(user.roles, id=marshal_role_id):
        return True

    return False


# ────────────────────────────────────────────────────────────────
# UI Views
# ────────────────────────────────────────────────────────────────

class DisputeView(discord.ui.View):
    """Shows a 'File Dispute' button attached to game result messages."""

    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(
        label="File Dispute", style=discord.ButtonStyle.danger,
        emoji="🚨", custom_id="match_file_dispute",
    )
    async def file_dispute(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = active_matches.get(self.channel_id)
        if not session:
            await interaction.response.send_message("❌ No active match in this channel.", ephemeral=True)
            return

        if session.status != "checking_ack":
            await interaction.response.send_message("❌ Cannot dispute now — no result is pending.", ephemeral=True)
            return

        if session.is_disputed:
            await interaction.response.send_message("⚠️ A dispute is already in progress.", ephemeral=True)
            return

        session.is_disputed = True
        session.dispute_start_time = datetime.now(timezone.utc)
        await session._sync_session()

        await interaction.response.send_message(
            f"🚨 **DISPUTE FILED by {interaction.user.mention}**\n"
            "The acknowledgement timer has been **PAUSED**.\n"
            "Marshals, please attend to this channel immediately.",
        )

        # Switch the button to Resolve
        await interaction.message.edit(view=ResolveDisputeView(self.channel_id))


class ResolveDisputeView(discord.ui.View):
    """Shows a 'Resolve Dispute' button (marshal/admin only)."""

    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(
        label="Resolve Dispute", style=discord.ButtonStyle.success,
        emoji="✅", custom_id="match_resolve_dispute",
    )
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = active_matches.get(self.channel_id)
        if not session:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)
            return

        if not await _is_marshal_or_admin(interaction, session):
            await interaction.response.send_message("❌ Only the Marshal or an Admin can resolve disputes.", ephemeral=True)
            return

        if not session.is_disputed:
            await interaction.response.send_message("❌ No dispute to resolve.", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        if session.dispute_start_time:
            duration = (now - session.dispute_start_time).total_seconds()
            session.total_dispute_seconds += int(duration)

        session.is_disputed = False
        session.dispute_start_time = None
        await session._sync_session()

        await interaction.response.send_message("✅ **Dispute Resolved.** Timer resumed.")

        # Revert to Dispute button
        try:
            await interaction.message.edit(view=DisputeView(self.channel_id))
        except discord.NotFound:
            pass


class EndMatchView(discord.ui.View):
    """Confirm / Cancel buttons for ending a match."""

    def __init__(self, session: MatchSession):
        super().__init__(timeout=60)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await _is_marshal_or_admin(interaction, self.session):
            await interaction.response.send_message(
                "❌ Only the Marshal or an Admin can use these buttons.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm End Match", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session.status = "ended"
        await self.session._sync_session()

        if self.session.channel_id in active_matches:
            del active_matches[self.session.channel_id]

        # Cancel any running auto-ack countdown
        task = _ack_countdown_tasks.pop(self.session.channel_id, None)
        if task:
            task.cancel()

        await interaction.response.edit_message(
            content="✅ **Match session ended.**",
            embed=self.session.get_summary_embed(final=True),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled match end.", view=None)
        self.stop()


class GracePeriodCancelView(discord.ui.View):
    """Cancel button for a running grace period countdown."""

    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(
        label="Cancel Grace Period", style=discord.ButtonStyle.danger,
        emoji="⏹️", custom_id="grace_period_cancel",
    )
    async def cancel_grace(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message(
                "❌ Only a Marshal or Admin can cancel the grace period.", ephemeral=True
            )
            return

        task_info = _grace_period_tasks.pop(self.channel_id, None)
        if task_info:
            task_info[0].cancel()

        await interaction.response.edit_message(
            content=f"⏹️ **Grace period cancelled** by {interaction.user.mention}.",
            embed=None,
            view=None,
        )


# ────────────────────────────────────────────────────────────────
# Cog
# ────────────────────────────────────────────────────────────────

class Matches(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """Reload active sessions from DB on startup and re-attach persistent views."""
        rows = await Database.fetchall(
            "SELECT * FROM match_sessions WHERE status != 'ended'"
        )
        for row in rows:
            session = MatchSession(
                db_id=row["id"],
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                marshal_id=row["marshal_id"],
                best_of=row["best_of"],
                team1=row.get("team1"),
                team2=row.get("team2"),
                status=row["status"],
                is_disputed=bool(row["is_disputed"]),
                ack_start_time=row["ack_start_time"],
                dispute_start_time=row["dispute_start_time"],
                total_dispute_seconds=row["total_dispute_seconds"],
                last_message_id=row["last_message_id"],
                started_at=row["started_at"],
            )

            # Load games for this session
            game_rows = await Database.fetchall(
                "SELECT * FROM match_games WHERE session_id = %s ORDER BY game_number",
                (row["id"],),
            )
            for g in game_rows:
                acks = {}
                if g["ack_team1"]:
                    acks[g["ack_team1"]] = {
                        "user": g["ack_team1_user"],
                        "timestamp": g["ack_team1_at"],
                    }
                if g["ack_team2"]:
                    acks[g["ack_team2"]] = {
                        "user": g["ack_team2_user"],
                        "timestamp": g["ack_team2_at"],
                    }
                session.games.append({
                    "db_id": g["id"],
                    "game_number": g["game_number"],
                    "result": g["result"],
                    "acks": acks,
                    "created_at": g["created_at"],
                })

            active_matches[session.channel_id] = session

            # Re-attach persistent views
            if session.last_message_id:
                if session.is_disputed:
                    self.bot.add_view(
                        ResolveDisputeView(session.channel_id),
                        message_id=session.last_message_id,
                    )
                elif session.status == "checking_ack":
                    self.bot.add_view(
                        DisputeView(session.channel_id),
                        message_id=session.last_message_id,
                    )

        if rows:
            print(f"   Matches: reloaded {len(rows)} active session(s).")

    # -- Helpers ---------------------------------------------------------------

    async def _get_player_team(self, guild_id: int, user_id: int) -> Optional[str]:
        """Look up a verified user's team abbreviation."""
        row = await Database.fetchone(
            "SELECT team_name FROM verified_users WHERE guild_id = %s AND discord_id = %s",
            (guild_id, user_id),
        )
        if not row:
            return None

        # Need the abbreviation. The verified_users table stores team_name, but we
        # need the abbrev from the sheet data.  The nick is set to "ABBREV | IGN"
        # during verification, so we can parse it from the member's nickname.
        # Alternatively, query the sheet validator cache.
        # Safest: parse nickname.  Format is always "ABBREV | IGN".
        return row["team_name"]  # We'll use team_name as the identifier.

    async def _get_player_team_abbrev(self, guild: discord.Guild, user_id: int) -> Optional[str]:
        """Get the team abbreviation from the member's nickname (ABBREV | IGN format)."""
        member = guild.get_member(user_id)
        if not member or not member.nick:
            # Fallback: look up team_name from DB
            row = await Database.fetchone(
                "SELECT team_name FROM verified_users WHERE guild_id = %s AND discord_id = %s",
                (guild.id, user_id),
            )
            return row["team_name"] if row else None

        # Parse "ABBREV | IGN"
        if " | " in member.nick:
            return member.nick.split(" | ")[0].strip()

        return member.nick  # Fallback to full nick

    async def _team_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team names from verified users."""
        rows = await Database.fetchall(
            "SELECT DISTINCT team_name FROM verified_users WHERE guild_id = %s ORDER BY team_name",
            (interaction.guild_id,),
        )
        teams = [r["team_name"] for r in rows if r["team_name"]]
        filtered = [t for t in teams if current.lower() in t.lower()]
        return [app_commands.Choice(name=t, value=t) for t in filtered[:25]]

    # -- Setup command ---------------------------------------------------------

    @app_commands.command(
        name="set_marshal_role",
        description="Set which role is treated as Marshal for match commands.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="The role to assign as Marshal")
    async def set_marshal_role(self, interaction: discord.Interaction, role: discord.Role):
        await Database.set_config(interaction.guild_id, "marshal_role_id", str(role.id))
        await interaction.response.send_message(
            f"✅ Marshal role set to {role.mention}.\n"
            "Users with this role can manage match sessions.",
            ephemeral=True,
        )

    # -- /match_start ----------------------------------------------------------

    @app_commands.command(name="match_start", description="Start a match session in this channel.")
    @app_commands.describe(
        best_of="Best of X (1, 2, 3, 5). Default: 3",
        team1="Name of team 1 (optional)",
        team2="Name of team 2 (optional)",
    )
    @app_commands.autocomplete(team1=_team_autocomplete, team2=_team_autocomplete)
    async def match_start(
        self,
        interaction: discord.Interaction,
        best_of: int = 3,
        team1: str | None = None,
        team2: str | None = None,
    ):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message("❌ You need the Marshal role or Admin to do this.", ephemeral=True)
            return

        if best_of < 1 or best_of > 7:
            await interaction.response.send_message("❌ Best-of must be between 1 and 7.", ephemeral=True)
            return

        if interaction.channel_id in active_matches:
            await interaction.response.send_message(
                "❌ A match is already ongoing in this channel!\n"
                "Use `/match_end` or `/match_cancel` to finish it first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        t1 = team1.strip() if team1 else None
        t2 = team2.strip() if team2 else None

        db_id = await Database.insert_get_id(
            "INSERT INTO match_sessions (guild_id, channel_id, marshal_id, best_of, team1, team2) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, best_of, t1, t2),
        )

        session = MatchSession(
            db_id=db_id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            marshal_id=interaction.user.id,
            best_of=best_of,
            team1=t1,
            team2=t2,
        )
        active_matches[interaction.channel_id] = session

        # Build title with team names if provided
        title = f"🏆 Match Started! (BO{best_of})"
        if t1 and t2:
            title = f"🏆 {t1} vs {t2} — BO{best_of}"

        desc_lines = [f"**Marshal:** {interaction.user.mention}"]
        if t1 and t2:
            desc_lines.append(f"**Teams:** {t1} vs {t2}")
        desc_lines.append("")
        desc_lines.append("Use `/game_started` to confirm the game has begun.")
        desc_lines.append("Use `/game_result` to log each game's outcome.")
        desc_lines.append('Team members can type **"I acknowledge"** to confirm results.')

        embed = discord.Embed(
            title=title,
            description="\n".join(desc_lines),
            color=0x00CC66,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Good luck and have fun!")
        await interaction.followup.send(embed=embed)

    # -- /game_started ---------------------------------------------------------

    @app_commands.command(
        name="game_started",
        description="Signal that the game has begun. Cancels any active grace period.",
    )
    async def game_started(self, interaction: discord.Interaction):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin to do this.", ephemeral=True
            )
            return

        parts = []

        # Cancel grace period if active
        task_info = _grace_period_tasks.pop(interaction.channel_id, None)
        if task_info:
            task_info[0].cancel()
            parts.append("⏹️ Grace period countdown **cancelled**.")

        # Check for active match session
        session = active_matches.get(interaction.channel_id)
        if session:
            if session.team1 and session.team2:
                parts.append(f"🎮 **{session.team1} vs {session.team2}** — game has started!")
            else:
                parts.append("🎮 **Game has started!**")
            parts.append("Use `/game_result` to log the outcome when the game ends.")
        else:
            if not task_info:
                # No grace period and no match — still allow it
                parts.append("🎮 **Game has started!**")
                parts.append("💡 No match session is active. Use `/match_start` to track results.")

        await interaction.response.send_message("\n".join(parts))

    # -- /game_result ----------------------------------------------------------

    @app_commands.command(name="game_result", description="Log a game result and wait for acknowledgement.")
    @app_commands.describe(result="The game result (e.g. 'TNC 1 - 0 BTK')")
    async def game_result(self, interaction: discord.Interaction, result: str):
        session = active_matches.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message(
                "❌ No active match in this channel. Start one with `/match_start`.", ephemeral=True
            )
            return

        if not await _is_marshal_or_admin(interaction, session):
            await interaction.response.send_message("❌ Only the Marshal or an Admin can log results.", ephemeral=True)
            return

        if session.status == "checking_ack":
            await interaction.response.send_message(
                "⚠️ Still waiting for acknowledgement of the previous game!\n"
                "Wait for both teams to ack, or use `/match_force_ack`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game = await session.add_game(result)

        view = DisputeView(interaction.channel_id)
        embed = discord.Embed(
            title=f"📢 Game {game['game_number']} Result",
            description=f"# {result}",
            color=0xF2C21A,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Acknowledgement",
            value=(
                "**Waiting for team acknowledgements...**\n"
                "Team captains/members, please reply with **\"I acknowledge\"**.\n"
                "*(Auto-advances via `/match_force_ack` after 5 minutes)*"
            ),
            inline=False,
        )

        msg = await interaction.followup.send(embed=embed, view=view, wait=True)
        session.last_message_id = msg.id
        await session._sync_session()

        # Start 5-minute auto-ack countdown
        self._start_dispute_countdown(interaction.channel_id, session)

    # -- /match_undo_game ------------------------------------------------------

    @app_commands.command(name="match_undo_game", description="Remove the last logged game result.")
    async def match_undo_game(self, interaction: discord.Interaction):
        session = active_matches.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)
            return

        if not await _is_marshal_or_admin(interaction, session):
            await interaction.response.send_message("❌ Only the Marshal or an Admin can undo games.", ephemeral=True)
            return

        if await session.undo_game():
            # Cancel the auto-ack countdown
            task = _ack_countdown_tasks.pop(interaction.channel_id, None)
            if task:
                task.cancel()
            await interaction.response.send_message(
                f"✅ Game entry removed. {len(session.games)} game(s) remain.",
            )
        else:
            await interaction.response.send_message("❌ No games to undo.", ephemeral=True)

    # -- /match_force_ack ------------------------------------------------------

    @app_commands.command(name="match_force_ack", description="Force-acknowledge for a team (5 min cooldown).")
    @app_commands.describe(team="The team to force-acknowledge for")
    @app_commands.autocomplete(team=_team_autocomplete)
    async def match_force_ack(self, interaction: discord.Interaction, team: str):
        session = active_matches.get(interaction.channel_id)
        if not session or session.status != "checking_ack":
            await interaction.response.send_message(
                "❌ No game is currently waiting for acknowledgement.", ephemeral=True
            )
            return

        if not await _is_marshal_or_admin(interaction, session):
            await interaction.response.send_message("❌ Only the Marshal or an Admin can force ack.", ephemeral=True)
            return

        if session.is_disputed:
            await interaction.response.send_message(
                "❌ A dispute is in progress. Resolve it first before force-acknowledging.", ephemeral=True
            )
            return

        # Check 5-minute timer
        elapsed = session.get_effective_elapsed_time()
        required = 5 * 60
        if elapsed < required:
            remaining = required - elapsed
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            await interaction.response.send_message(
                f"⏳ **Too Early:** You must wait 5 minutes before force-acknowledging.\n"
                f"Time Remaining: **{minutes}m {seconds}s**",
                ephemeral=True,
            )
            return

        # Check if this team already acked
        if session.games and team in session.games[-1]["acks"]:
            await interaction.response.send_message(
                f"⚠️ **{team}** has already acknowledged this game.", ephemeral=True
            )
            return

        is_complete = await session.ack_game(team, f"{interaction.user.display_name} (Forced)")

        if is_complete:
            game = session.games[-1]
            ack_list = list(game["acks"].items())
            t1, d1 = ack_list[0]
            t2, d2 = ack_list[1]
            timestamp_str = discord.utils.format_dt(datetime.now(timezone.utc), style="f")

            await interaction.response.send_message(
                f"✅ **Game {len(session.games)} acknowledged** (forced for **{team}**).\n"
                f"Acknowledged by {d1['user']} ({t1}) and {d2['user']} ({t2}) on {timestamp_str}.\n"
                "Ready for next game or match end."
            )
        else:
            await interaction.response.send_message(
                f"✅ Forced acknowledgement for **{team}**. Waiting for other team..."
            )

    # -- /match_end ------------------------------------------------------------

    @app_commands.command(name="match_end", description="End the match session.")
    async def match_end(self, interaction: discord.Interaction):
        session = active_matches.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)
            return

        if not await _is_marshal_or_admin(interaction, session):
            await interaction.response.send_message("❌ Only the Marshal or an Admin can end the match.", ephemeral=True)
            return

        # Check minimum games
        min_games = session.get_min_games_required()
        if len(session.games) < min_games:
            await interaction.response.send_message(
                f"❌ **Cannot End Match Yet:** Not enough games played.\n"
                f"**Required:** {min_games} | **Played:** {len(session.games)}\n"
                f"For BO{session.best_of}, you need at least {min_games} games.\n\n"
                "💡 Use `/match_cancel` if you need to abort the session early.",
                ephemeral=True,
            )
            return

        # Check for un-acked games
        unacked = [g["game_number"] for g in session.games if len(g["acks"]) < 2]
        warning = ""
        if unacked:
            nums = ", ".join(str(n) for n in unacked)
            warning = f"\n\n⚠️ **WARNING:** Game(s) {nums} are not fully acknowledged!"

        embed = session.get_summary_embed()
        view = EndMatchView(session)

        await interaction.response.send_message(
            f"🛑 **End Match Session?**{warning}",
            embed=embed,
            view=view,
        )

    # -- /match_cancel ---------------------------------------------------------

    @app_commands.command(name="match_cancel", description="Force-cancel the match session without saving.")
    async def match_cancel(self, interaction: discord.Interaction):
        session = active_matches.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)
            return

        if not await _is_marshal_or_admin(interaction, session):
            await interaction.response.send_message("❌ Only the Marshal or an Admin can cancel.", ephemeral=True)
            return

        session.status = "ended"
        await session._sync_session()
        del active_matches[interaction.channel_id]

        # Cancel any running auto-ack countdown
        task = _ack_countdown_tasks.pop(interaction.channel_id, None)
        if task:
            task.cancel()

        await interaction.response.send_message("🗑️ **Match session cancelled.**")

    # -- /match_skip_ack (testing) ---------------------------------------------

    @app_commands.command(
        name="match_skip_ack",
        description="Skip acknowledgement — instantly mark both teams as acked (testing/admin).",
    )
    @app_commands.default_permissions(administrator=True)
    async def match_skip_ack(self, interaction: discord.Interaction):
        session = active_matches.get(interaction.channel_id)
        if not session or session.status != "checking_ack":
            await interaction.response.send_message(
                "❌ No game is currently waiting for acknowledgement.", ephemeral=True
            )
            return

        if not await _is_marshal_or_admin(interaction, session):
            await interaction.response.send_message("❌ Only the Marshal or an Admin can do this.", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        game = session.games[-1]
        user_name = f"{interaction.user.display_name} (Skipped)"

        # Fill in any missing ack slots — use real team names if available
        t1_label = session.get_team_label(1)
        t2_label = session.get_team_label(2)
        if t1_label not in game["acks"] and len(game["acks"]) < 1:
            game["acks"][t1_label] = {"user": user_name, "timestamp": now}
        if len(game["acks"]) < 2:
            existing = list(game["acks"].keys())
            label = t2_label if t2_label not in existing else f"{t2_label} (auto)"
            game["acks"][label] = {"user": user_name, "timestamp": now}

        # Sync to DB
        ack_list = list(game["acks"].items())
        await Database.execute(
            "UPDATE match_games SET ack_team1=%s, ack_team1_user=%s, ack_team1_at=%s, "
            "ack_team2=%s, ack_team2_user=%s, ack_team2_at=%s WHERE id=%s",
            (
                ack_list[0][0], ack_list[0][1]["user"], ack_list[0][1]["timestamp"],
                ack_list[1][0], ack_list[1][1]["user"], ack_list[1][1]["timestamp"],
                game["db_id"],
            ),
        )

        session.status = "ongoing"
        session.is_disputed = False
        session.dispute_start_time = None
        await session._sync_session()

        await interaction.response.send_message(
            f"⏭️ **Game {game['game_number']} acknowledgement skipped** by {interaction.user.mention}.\n"
            "Both teams marked as acknowledged. Ready for next game or match end."
        )

        # Cancel the auto-ack countdown
        task = _ack_countdown_tasks.pop(interaction.channel_id, None)
        if task:
            task.cancel()

    # -- /match_force_end (testing) --------------------------------------------

    @app_commands.command(
        name="match_force_end",
        description="Force-end the match regardless of game count or ack status (testing/admin).",
    )
    @app_commands.default_permissions(administrator=True)
    async def match_force_end(self, interaction: discord.Interaction):
        session = active_matches.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)
            return

        if not await _is_marshal_or_admin(interaction, session):
            await interaction.response.send_message("❌ Only the Marshal or an Admin can do this.", ephemeral=True)
            return

        session.status = "ended"
        await session._sync_session()
        del active_matches[interaction.channel_id]

        # Cancel any running auto-ack countdown
        task = _ack_countdown_tasks.pop(interaction.channel_id, None)
        if task:
            task.cancel()

        embed = session.get_summary_embed(final=True)
        embed.color = 0xFF4444
        embed.title = "🛑 Match Force-Ended"

        await interaction.response.send_message(
            f"⚠️ **Match session force-ended** by {interaction.user.mention}.",
            embed=embed,
        )

    # -- /match_status ---------------------------------------------------------

    @app_commands.command(name="match_status", description="View the current match session status.")
    async def match_status(self, interaction: discord.Interaction):
        session = active_matches.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message("No active match in this channel.", ephemeral=True)
            return

        embed = session.get_summary_embed()

        # Add extra status info
        if session.status == "checking_ack" and session.games:
            game = session.games[-1]
            acked_teams = list(game["acks"].keys())
            if acked_teams:
                embed.add_field(
                    name="Acknowledged So Far",
                    value=", ".join(f"**{t}**" for t in acked_teams),
                    inline=True,
                )

            elapsed = session.get_effective_elapsed_time()
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            embed.add_field(
                name="Ack Timer",
                value=f"{minutes}m {seconds}s / 5m 0s",
                inline=True,
            )

            if session.is_disputed:
                embed.add_field(
                    name="⚠️ Status",
                    value="**DISPUTE IN PROGRESS** — Timer paused",
                    inline=False,
                )

        marshal = interaction.guild.get_member(session.marshal_id)
        marshal_text = marshal.mention if marshal else f"ID {session.marshal_id}"
        embed.set_footer(text=f"Marshal: {marshal_text} • Started")
        embed.timestamp = session.started_at

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- "I acknowledge" listener ----------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        session = active_matches.get(message.channel.id)
        if not session or session.status != "checking_ack":
            return

        if "i acknowledge" not in message.content.lower():
            return

        # Dispute blocks acks
        if session.is_disputed:
            await message.add_reaction("⏸️")
            return

        # Look up team
        team = await self._get_player_team_abbrev(message.guild, message.author.id)
        if not team:
            await message.add_reaction("❓")  # Not verified
            return

        # Already acked by this team?
        if team in session.games[-1]["acks"]:
            await message.add_reaction("⚠️")
            return

        # Record ack
        is_complete = await session.ack_game(team, message.author.display_name)
        await message.add_reaction("✅")

        if is_complete:
            game = session.games[-1]
            ack_list = list(game["acks"].items())
            t1, d1 = ack_list[0]
            t2, d2 = ack_list[1]
            timestamp_str = discord.utils.format_dt(datetime.now(timezone.utc), style="f")

            await message.channel.send(
                f"✅ **Game {len(session.games)} result acknowledged by "
                f"{d1['user']} ({t1}) and {d2['user']} ({t2}) on {timestamp_str}.**\n"
                "Disputes for this game are no longer valid beyond this point."
            )

            # Cancel the auto-ack countdown since both teams acknowledged
            task = _ack_countdown_tasks.pop(message.channel.id, None)
            if task:
                task.cancel()

    # -- Dispute countdown background task ------------------------------------

    def _start_dispute_countdown(self, channel_id: int, session: "MatchSession"):
        """Start a 5-minute auto-ack countdown for the current game."""
        # Cancel any existing countdown for this channel
        old_task = _ack_countdown_tasks.pop(channel_id, None)
        if old_task:
            old_task.cancel()

        async def _countdown():
            # We need to account for dispute pauses, so we poll every 5 seconds
            while True:
                await asyncio.sleep(5)

                # Session might have been ended or cancelled
                if channel_id not in active_matches:
                    return
                s = active_matches[channel_id]

                # If no longer checking_ack, the game was acked or undone
                if s.status != "checking_ack":
                    return

                # Effective time excludes dispute pauses
                elapsed = s.get_effective_elapsed_time()
                if elapsed >= 300:  # 5 minutes
                    break

            # Time's up — auto-force-ack
            s = active_matches.get(channel_id)
            if not s or s.status != "checking_ack" or not s.games:
                return

            game = s.games[-1]
            now = datetime.now(timezone.utc)
            user_name = "System (Auto-ack)"

            # Fill missing ack slots — use real team names if available
            t1_label = s.get_team_label(1)
            t2_label = s.get_team_label(2)
            if len(game["acks"]) < 2:
                for placeholder in [t1_label, t2_label]:
                    if len(game["acks"]) >= 2:
                        break
                    if placeholder not in game["acks"]:
                        game["acks"][placeholder] = {"user": user_name, "timestamp": now}

            # Sync to DB
            ack_list = list(game["acks"].items())
            if len(ack_list) >= 2:
                await Database.execute(
                    "UPDATE match_games SET ack_team1=%s, ack_team1_user=%s, ack_team1_at=%s, "
                    "ack_team2=%s, ack_team2_user=%s, ack_team2_at=%s WHERE id=%s",
                    (
                        ack_list[0][0], ack_list[0][1]["user"], ack_list[0][1]["timestamp"],
                        ack_list[1][0], ack_list[1][1]["user"], ack_list[1][1]["timestamp"],
                        game["db_id"],
                    ),
                )

            s.status = "ongoing"
            s.is_disputed = False
            s.dispute_start_time = None
            await s._sync_session()

            # Send alert
            channel = self.bot.get_channel(channel_id)
            if channel:
                marshal = channel.guild.get_member(s.marshal_id) if hasattr(channel, 'guild') else None
                marshal_ping = marshal.mention if marshal else f"<@{s.marshal_id}>"
                await channel.send(
                    f"⏰ **5-minute dispute window has closed.**\n"
                    f"Game {game['game_number']} result has been **auto-acknowledged**.\n"
                    f"The result is now final. {marshal_ping}"
                )

            _ack_countdown_tasks.pop(channel_id, None)

        task = asyncio.create_task(_countdown())
        _ack_countdown_tasks[channel_id] = task

    # -- /match_history --------------------------------------------------------

    @app_commands.command(
        name="match_history",
        description="View recent match results.",
    )
    @app_commands.describe(limit="Number of matches to show (default 10, max 25)")
    async def match_history(self, interaction: discord.Interaction, limit: int = 10):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin to do this.", ephemeral=True
            )
            return

        limit = max(1, min(limit, 25))
        await interaction.response.defer(ephemeral=True)

        rows = await Database.fetchall(
            "SELECT * FROM match_sessions WHERE guild_id = %s AND status = 'ended' "
            "ORDER BY ended_at DESC LIMIT %s",
            (interaction.guild_id, limit),
        )

        if not rows:
            await interaction.followup.send(
                "📭 No completed matches found.", ephemeral=True
            )
            return

        lines = []
        for i, row in enumerate(rows, 1):
            t1 = row.get("team1") or "Team A"
            t2 = row.get("team2") or "Team B"
            bo = row["best_of"]

            # Load games for this session to compute score
            game_rows = await Database.fetchall(
                "SELECT result FROM match_games WHERE session_id = %s ORDER BY game_number",
                (row["id"],),
            )

            t1_wins = 0
            t2_wins = 0
            for g in game_rows:
                m = re.search(r'(\d+)\s*[-\u2013]\s*(\d+)', g["result"])
                if m:
                    s1, s2 = int(m.group(1)), int(m.group(2))
                    if s1 > s2:
                        t1_wins += 1
                    elif s2 > s1:
                        t2_wins += 1

            # Format date
            ended = row.get("ended_at")
            date_str = discord.utils.format_dt(ended, style="d") if ended else "Unknown"

            marshal = interaction.guild.get_member(row["marshal_id"])
            marshal_name = marshal.display_name if marshal else f"ID {row['marshal_id']}"

            winner_icon = ""
            if t1_wins > t2_wins:
                winner_icon = f" 🥇 {t1}"
            elif t2_wins > t1_wins:
                winner_icon = f" 🥇 {t2}"
            else:
                winner_icon = " 🤝 Tied"

            lines.append(
                f"**{i}.** {t1} vs {t2} (BO{bo}) — **{t1_wins}–{t2_wins}**{winner_icon}\n"
                f"   📅 {date_str} · Marshal: {marshal_name}"
            )

        embed = discord.Embed(
            title="📜 Match History",
            description="\n\n".join(lines),
            color=0xF2C21A,
        )
        embed.set_footer(text=f"Showing {len(rows)} most recent match(es)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -- /coinflip -------------------------------------------------------------

    @app_commands.command(
        name="coinflip",
        description="Flip a coin — Heads or Tails.",
    )
    async def coinflip(self, interaction: discord.Interaction):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin to do this.", ephemeral=True
            )
            return

        import random

        result = random.choice(["Heads", "Tails"])

        # Animated coin flip sequence
        frames = ["🪙 Flipping...", "🔄 .", "🪙 ..", "🔄 ..."]
        await interaction.response.send_message(frames[0])
        msg = await interaction.original_response()

        for frame in frames[1:]:
            await asyncio.sleep(0.6)
            await msg.edit(content=frame)

        await asyncio.sleep(0.8)
        await msg.edit(content=f"🪙 **{result}!**")

    # -- /remind ---------------------------------------------------------------

    @app_commands.command(
        name="set_remind_message",
        description="Set the message that /remind will send (from a message link).",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(message_link="Discord message link to copy content from")
    async def set_remind_message(self, interaction: discord.Interaction, message_link: str):
        # Parse Discord message link: https://discord.com/channels/GUILD/CHANNEL/MESSAGE
        import re as _re
        match = _re.match(
            r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)",
            message_link.strip(),
        )
        if not match:
            await interaction.response.send_message(
                "❌ Invalid message link. Right-click a message → **Copy Message Link** and paste it here.",
                ephemeral=True,
            )
            return

        guild_id, channel_id, message_id = int(match.group(1)), int(match.group(2)), int(match.group(3))

        # Fetch the message
        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                channel = await self.bot.fetch_channel(channel_id)
            msg = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "❌ Could not fetch that message. Make sure the bot has access to that channel.",
                ephemeral=True,
            )
            return

        if not msg.content:
            await interaction.response.send_message(
                "❌ That message has no text content (it may be embed-only).",
                ephemeral=True,
            )
            return

        await Database.set_config(interaction.guild_id, "remind_message", msg.content)
        await interaction.response.send_message(
            f"✅ Remind message saved.\n\n**Preview:**\n{msg.content}",
            ephemeral=True,
        )

    @app_commands.command(
        name="remind",
        description="Send the preset reminder message to this channel.",
    )
    async def remind(self, interaction: discord.Interaction):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin to do this.", ephemeral=True
            )
            return

        message = await Database.get_config(interaction.guild_id, "remind_message")
        if not message:
            await interaction.response.send_message(
                "❌ No remind message configured. An admin must use `/set_remind_message` first.",
                ephemeral=True,
            )
            return

        await interaction.channel.send(message)
        await interaction.response.send_message("✅ Reminder sent.", ephemeral=True)

    # -- Grace period command --------------------------------------------------

    @app_commands.command(
        name="grace_period",
        description="Start a 15-minute grace period countdown from a specified start time.",
    )
    @app_commands.describe(
        time="Round start time in HH:MM format (e.g. 15:30 or 3:30)",
    )
    async def grace_period(self, interaction: discord.Interaction, time: str):
        if not await _is_marshal_or_admin(interaction):
            await interaction.response.send_message(
                "❌ You need the Marshal role or Admin to do this.", ephemeral=True
            )
            return

        # Cancel existing grace period in this channel
        existing = _grace_period_tasks.pop(interaction.channel_id, None)
        if existing:
            existing[0].cancel()

        # Parse time (HH:MM format, 24h or 12h)
        manila_tz = ZoneInfo("Asia/Manila")
        now_manila = datetime.now(manila_tz)

        try:
            # Try 24-hour format first
            parts = time.strip().replace(".", ":").split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0

            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError("Invalid time")

            start_time = now_manila.replace(hour=hour, minute=minute, second=0, microsecond=0)

            # If the start time is more than 1 hour in the past, assume tomorrow
            if start_time < now_manila - timedelta(hours=1):
                start_time += timedelta(days=1)

        except (ValueError, IndexError):
            await interaction.response.send_message(
                "❌ Invalid time format. Use **HH:MM** (e.g. `15:30` or `3:30`).",
                ephemeral=True,
            )
            return

        end_time = start_time + timedelta(minutes=15)

        # Convert to UTC timestamps for Discord formatting
        start_ts = int(start_time.timestamp())
        end_ts = int(end_time.timestamp())
        alert_10_ts = int((start_time + timedelta(minutes=5)).timestamp())
        alert_5_ts = int((start_time + timedelta(minutes=10)).timestamp())

        embed = discord.Embed(
            title="⏱️ Grace Period Started",
            description=(
                f"**Round Start Time:** <t:{start_ts}:T> (<t:{start_ts}:R>)\n"
                f"**Grace Period Ends:** <t:{end_ts}:T> (<t:{end_ts}:R>)\n\n"
                f"📢 Alerts will be sent at:\n"
                f"• 10 min remaining (<t:{alert_10_ts}:T>)\n"
                f"• 5 min remaining (<t:{alert_5_ts}:T>)\n"
                f"• Grace period over (<t:{end_ts}:T>)"
            ),
            color=0xF2C21A,
        )
        embed.set_footer(text=f"Marshal: {interaction.user.display_name}")

        await interaction.response.send_message(
            embed=embed,
            view=GracePeriodCancelView(interaction.channel_id),
        )

        # Start background countdown
        marshal_id = interaction.user.id
        channel_id = interaction.channel_id

        async def _grace_countdown():
            try:
                now_utc = datetime.now(timezone.utc)
                start_utc = start_time.astimezone(timezone.utc)
                end_utc = end_time.astimezone(timezone.utc)
                t_10min = start_utc + timedelta(minutes=5)   # 10 min remaining
                t_5min = start_utc + timedelta(minutes=10)   # 5 min remaining

                channel = self.bot.get_channel(channel_id)
                if not channel:
                    return

                marshal_ping = f"<@{marshal_id}>"

                # Wait until round start time
                wait_until_start = (start_utc - now_utc).total_seconds()
                if wait_until_start > 0:
                    await asyncio.sleep(wait_until_start)

                # Wait until 10 min remaining alert (5 min after start)
                now_utc = datetime.now(timezone.utc)
                wait_10 = (t_10min - now_utc).total_seconds()
                if wait_10 > 0:
                    await asyncio.sleep(wait_10)

                await channel.send(
                    f"⚠️ **10 MINUTES REMAINING** in the grace period.\n"
                    f"Teams must start their game and send a **screenshot of the lobby "
                    f"with their full team**. {marshal_ping}"
                )

                # Wait until 5 min remaining alert (10 min after start)
                now_utc = datetime.now(timezone.utc)
                wait_5 = (t_5min - now_utc).total_seconds()
                if wait_5 > 0:
                    await asyncio.sleep(wait_5)

                await channel.send(
                    f"🚨 **5 MINUTES REMAINING** in the grace period.\n"
                    f"If the game has not started, **defaults will be issued**.\n"
                    f"A screenshot of the lobby with your full team is REQUIRED. {marshal_ping}"
                )

                # Wait until grace period ends (15 min after start)
                now_utc = datetime.now(timezone.utc)
                wait_end = (end_utc - now_utc).total_seconds()
                if wait_end > 0:
                    await asyncio.sleep(wait_end)

                await channel.send(
                    f"🔴 **GRACE PERIOD IS OVER.**\n"
                    f"Teams that have not started their game are subject to **DEFAULT LOSS**.\n"
                    f"A lobby screenshot with your full team must have been submitted. {marshal_ping}"
                )

                _grace_period_tasks.pop(channel_id, None)

            except asyncio.CancelledError:
                pass  # Timer was cancelled via button

        task = asyncio.create_task(_grace_countdown())
        _grace_period_tasks[channel_id] = (task, marshal_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Matches(bot))
