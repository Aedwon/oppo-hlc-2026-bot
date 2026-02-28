"""
Cog: Matches

Admin commands:
  /set_marshal_role    -- configure which role acts as Marshal

Marshal / Admin commands:
  /match_start         -- start a BO(X) match session in the channel
  /game_result         -- log a game result, triggers ack flow
  /match_undo_game     -- remove the last logged game
  /match_force_ack     -- force-acknowledge for a team (5 min cooldown)
  /match_end           -- end the match (validates enough games)
  /match_cancel        -- force-cancel without saving

Anyone:
  /match_status        -- view current match state

Flow:
  1. Marshal starts a match session with /match_start
  2. After each game, marshal logs the result with /game_result
  3. Verified team members type "I acknowledge" to confirm
  4. Anyone can file a dispute (pauses ack timer)
  5. Marshal resolves disputes, force_acks after 5 min if needed
  6. Match ends when enough games are played and marshal uses /match_end
"""
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone
from typing import Dict, Optional
from db.database import Database
from utils.constants import ROLE_MARSHAL


# ────────────────────────────────────────────────────────────────
# In-memory session cache  (channel_id → MatchSession)
# ────────────────────────────────────────────────────────────────

active_matches: Dict[int, "MatchSession"] = {}


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
            "last_message_id=%s, ended_at=%s WHERE id=%s",
            (
                self.status,
                self.is_disputed,
                self.ack_start_time,
                self.dispute_start_time,
                self.total_dispute_seconds,
                self.last_message_id,
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

    def get_summary_embed(self) -> discord.Embed:
        """Build a rich embed summarising the match."""
        embed = discord.Embed(
            title=f"🏆 Match Session (BO{self.best_of})",
            color=0xF2C21A,
        )

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

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Marshal: ID {self.marshal_id}")
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

        await interaction.response.edit_message(
            content="✅ **Match session ended.**",
            embed=self.session.get_summary_embed(),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled match end.", view=None)
        self.stop()


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
    @app_commands.describe(best_of="Best of X (1, 2, 3, 5). Default: 3")
    async def match_start(self, interaction: discord.Interaction, best_of: int = 3):
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

        db_id = await Database.insert_get_id(
            "INSERT INTO match_sessions (guild_id, channel_id, marshal_id, best_of) VALUES (%s, %s, %s, %s)",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, best_of),
        )

        session = MatchSession(
            db_id=db_id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            marshal_id=interaction.user.id,
            best_of=best_of,
        )
        active_matches[interaction.channel_id] = session

        embed = discord.Embed(
            title=f"🏆 Match Started! (BO{best_of})",
            description=(
                f"**Marshal:** {interaction.user.mention}\n\n"
                "Use `/game_result` to log each game's outcome.\n"
                "Team members can type **\"I acknowledge\"** to confirm results."
            ),
            color=0x00CC66,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Good luck and have fun!")
        await interaction.followup.send(embed=embed)

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

        await interaction.response.send_message("🗑️ **Match session cancelled.**")

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


async def setup(bot: commands.Bot):
    await bot.add_cog(Matches(bot))
