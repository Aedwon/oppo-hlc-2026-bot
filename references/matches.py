import discord
from discord.ext import commands
from discord import app_commands, ui
from datetime import datetime, timezone
import logging
from typing import Dict, List, Optional
import json
import os
from utils.verification_tools import load_verified_users, fetch_filtered_players

# State Management
# Key: Channel ID (int)
# Value: MatchSession (dict)
active_matches: Dict[int, 'MatchSession'] = {}
ACTIVE_MATCHES_FILE = "data/active_matches.json"

def save_matches():
    data = {str(cid): session.to_dict() for cid, session in active_matches.items()}
    os.makedirs(os.path.dirname(ACTIVE_MATCHES_FILE), exist_ok=True)
    with open(ACTIVE_MATCHES_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def load_matches_from_disk(bot: commands.Bot):
    if not os.path.exists(ACTIVE_MATCHES_FILE):
        return

    try:
        with open(ACTIVE_MATCHES_FILE, 'r') as f:
            data = json.load(f)
        
        for cid_str, session_data in data.items():
            cid = int(cid_str)
            try:
                session = MatchSession.from_dict(session_data, bot)
                active_matches[cid] = session
                
                # Re-attach views based on state
                if session.last_message_id:
                   if session.is_disputed:
                       bot.add_view(ResolveDisputeView(session, None), message_id=session.last_message_id) # Original message obj is lost, pass None and fix view logic if needed
                       # Actually ResolveDisputeView needs original message to edit it back. 
                       # We might need to fetch it in resolve if None.
                   elif session.status == "checking_ack":
                       bot.add_view(DisputeView(session), message_id=session.last_message_id)
            except Exception as e:
                print(f"Failed to load match session for {cid}: {e}")
                
    except Exception as e:
        print(f"Failed to load active matches: {e}")

class MatchSession:
    def __init__(self, best_of: int, marshal: discord.Member):
        self.best_of = best_of
        self.marshal = marshal
        self.start_time = datetime.now(timezone.utc)
        self.games: List[Dict] = []  # List of {result: str, acks: set(team_abbrev)}
        self.status = "ongoing" # ongoing, checking_ack, ended
        self.current_game_index = -1
        self.last_message_id: Optional[int] = None
        
        # Dispute & Timer Logic
        self.ack_start_time: Optional[datetime] = None
        self.dispute_start_time: Optional[datetime] = None
        self.total_dispute_duration = discord.utils.format_dt(datetime.now(timezone.utc), style="R").replace(" ", "") # Dummy init
        self.total_dispute_seconds = 0
        self.is_disputed = False

    def to_dict(self):
        return {
            "best_of": self.best_of,
            "marshal_id": self.marshal.id,
            "start_time": self.start_time.isoformat(),
            "games": [
                {
                    "result": g["result"],
                    "acks": { # Serialize dict
                        team: {
                            "user": details["user"],
                            "timestamp": details["timestamp"].isoformat()
                        } for team, details in g["acks"].items()
                    },
                    "timestamp": g["timestamp"].isoformat()
                } for g in self.games
            ],
            "status": self.status,
            "current_game_index": self.current_game_index,
            "last_message_id": self.last_message_id,
            "ack_start_time": self.ack_start_time.isoformat() if self.ack_start_time else None,
            "dispute_start_time": self.dispute_start_time.isoformat() if self.dispute_start_time else None,
            "total_dispute_seconds": self.total_dispute_seconds,
            "is_disputed": self.is_disputed
        }

    @classmethod
    def from_dict(cls, data, bot):
        marshal = bot.get_user(data["marshal_id"]) 
        if not marshal: pass 

        inst = cls(data["best_of"], marshal) # marshal might be None! 
        inst.start_time = datetime.fromisoformat(data["start_time"])
        inst.games = []
        for g in data["games"]:
            # Deserialize acks
            acks_data = g["acks"] if isinstance(g["acks"], dict) else {} # Handling legacy if needed, but we are overwriting
            # Legacy handling: if it was a list (old schema), convert to empty dict or dumb dict? 
            # The tool overwrites previous code so we assume new schema.
            # But wait, if we load OLD json, it might crash. 
            # Ideally we check type. 
            deserialized_acks = {}
            if isinstance(acks_data, list): # Old schema was list
                for team in acks_data:
                    deserialized_acks[team] = {"user": "Unknown", "timestamp": datetime.now(timezone.utc)}
            else:
                for team, details in acks_data.items():
                    deserialized_acks[team] = {
                        "user": details["user"],
                        "timestamp": datetime.fromisoformat(details["timestamp"])
                    }

            inst.games.append({
                "result": g["result"],
                "acks": deserialized_acks,
                "timestamp": datetime.fromisoformat(g["timestamp"])
            })
        inst.status = data["status"]
        inst.current_game_index = data["current_game_index"]
        inst.last_message_id = data.get("last_message_id") # Safe get
        
        if data["ack_start_time"]: inst.ack_start_time = datetime.fromisoformat(data["ack_start_time"])
        if data["dispute_start_time"]: inst.dispute_start_time = datetime.fromisoformat(data["dispute_start_time"])
        inst.total_dispute_seconds = data.get("total_dispute_seconds", 0)
        inst.is_disputed = data.get("is_disputed", False)
        
        return inst

    def add_game(self, result: str):
        self.current_game_index += 1
        self.games.append({
            "result": result,
            "acks": {}, # Dictionary: team_abbrev -> {user: str, timestamp: datetime}
            "timestamp": datetime.now(timezone.utc)
        })
        self.status = "checking_ack"
        # Reset timer state for new game
        self.ack_start_time = datetime.now(timezone.utc)
        self.dispute_start_time = None
        self.total_dispute_seconds = 0
        self.is_disputed = False
        save_matches()

    def undo_game(self):
        if self.games:
            self.games.pop()
            self.current_game_index -= 1
            self.status = "ongoing"
            self.ack_start_time = None
            save_matches() 
            return True
        return False

    def ack_game(self, team_abbrev: str, user_display_name: str):
        if self.status == "checking_ack" and self.games:
            # Store detail
            self.games[-1]["acks"][team_abbrev] = {
                "user": user_display_name,
                "timestamp": datetime.now(timezone.utc)
            }
            
            # Check if 2 teams acked
            if len(self.games[-1]["acks"]) >= 2:
                self.status = "ongoing"
                save_matches()
                return True
            save_matches()
        return False

    def is_current_game_acked(self):
        if self.games and len(self.games[-1]["acks"]) >= 2:
            return True
        return False

    def get_summary(self):
        summary = f"**Match Session (BO{self.best_of})**\n"
        for i, game in enumerate(self.games, 1):
            acks_keys = list(game["acks"].keys())
            acks_str = ", ".join(acks_keys)
            status = "✅ Acknowledged" if len(game["acks"]) >= 2 else f"⚠️ Waiting ({len(game['acks'])}/2)"
            summary += f"Game {i}: {game['result']} - {status}\n"
        return summary
    
    def get_effective_elapsed_time(self):
        """Returns seconds elapsed since result posted, excluding dispute time."""
        if not self.ack_start_time:
            return 0
        
        now = datetime.now(timezone.utc)
        total_elapsed = (now - self.ack_start_time).total_seconds()
        
        current_dispute_duration = 0
        if self.is_disputed and self.dispute_start_time:
            current_dispute_duration = (now - self.dispute_start_time).total_seconds()
            
        return total_elapsed - self.total_dispute_seconds - current_dispute_duration
    
    def get_min_games_required(self) -> int:
        """Returns the minimum number of games required to end the match."""
        # Even BO (e.g., BO2) requires all games to be played.
        if self.best_of % 2 == 0:
            return self.best_of
        # Odd BO (e.g., BO1, BO3, BO5) requires majority to win (e.g., 2 for BO3).
        return (self.best_of // 2) + 1

class ResolveDisputeView(ui.View):
    def __init__(self, session: MatchSession, original_message: discord.Message):
        super().__init__(timeout=None)
        self.session = session
        self.original_message = original_message

    @ui.button(label="Resolve Dispute", style=discord.ButtonStyle.success, emoji="✅")
    async def resolve(self, interaction: discord.Interaction, button: ui.Button):
        # Only Marshal/Admin
        marshal_role_id = 1176872289501974529
        has_marshal_role = discord.utils.get(interaction.user.roles, id=marshal_role_id) is not None
        
        # Robust user check for marshal (persistence might make objects None/different)
        is_session_marshal = False
        if self.session.marshal:
             if interaction.user.id == self.session.marshal.id: is_session_marshal = True
        
        is_admin = interaction.user.guild_permissions.administrator

        if not (is_session_marshal or is_admin or has_marshal_role):
             await interaction.response.send_message("❌ Only the Marshal can resolve this dispute.", ephemeral=True)
             return

        if not self.session.is_disputed:
            await interaction.response.send_message("❌ Dispute already resolved.", ephemeral=True)
            return

        # Calculate duration
        now = datetime.now(timezone.utc)
        if self.session.dispute_start_time:
            duration = (now - self.session.dispute_start_time).total_seconds()
            self.session.total_dispute_seconds += duration
        
        self.session.is_disputed = False
        self.session.dispute_start_time = None
        save_matches()

        await interaction.response.send_message(f"✅ **Dispute Resolved.** Timer resumed.", ephemeral=False)
        
        # Revert message to normal state with Dispute button
        if self.original_message:
             await self.original_message.edit(view=DisputeView(self.session))
        else:
            # Try to fetch if missing (reload scenario)
            try:
                msg = await interaction.channel.fetch_message(interaction.message.id)
                await msg.edit(view=DisputeView(self.session))
            except:
                pass
        self.stop()

class DisputeView(ui.View):
    def __init__(self, session: MatchSession):
        super().__init__(timeout=None)
        self.session = session

    @ui.button(label="File Dispute", style=discord.ButtonStyle.danger, emoji="🚨")
    async def file_dispute(self, interaction: discord.Interaction, button: ui.Button):
        if self.session.status != "checking_ack":
             await interaction.response.send_message("❌ Cannot dispute now.", ephemeral=True)
             return
             
        if self.session.is_disputed:
             await interaction.response.send_message("⚠️ A dispute is already in progress.", ephemeral=True)
             return

        self.session.is_disputed = True
        self.session.dispute_start_time = datetime.now(timezone.utc)
        save_matches()
        
        await interaction.response.send_message(
            f"🚨 **DISPUTE FILED by {interaction.user.mention}**\n"
            "The acknowledgement timer has been **PAUSED**.\n"
            "Marshals immediately attend to this channel.",
            ephemeral=False
        )
        
        # Switch view to Resolve
        await interaction.message.edit(view=ResolveDisputeView(self.session, interaction.message))

class EndMatchView(ui.View):
    def __init__(self, session: MatchSession, channel_id: int):
        super().__init__(timeout=60)
        self.session = session
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Check if user is the session marshal, an admin, or has the Marshal role
        marshal_role_id = 1176872289501974529
        has_marshal_role = discord.utils.get(interaction.user.roles, id=marshal_role_id) is not None
        
        is_session_marshal = False
        if self.session.marshal:
             if interaction.user.id == self.session.marshal.id: is_session_marshal = True
             
        is_admin = interaction.user.guild_permissions.administrator

        if is_session_marshal or is_admin or has_marshal_role:
            return True
        
        await interaction.response.send_message("❌ **Access Denied**: Only the Marshal or Admins can use these buttons.", ephemeral=True)
        return False

    @ui.button(label="Confirm End Match", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        # Cleanup
        if self.channel_id in active_matches:
            del active_matches[self.channel_id]
            save_matches()
        
        await interaction.response.send_message("Match session ended. ✅", ephemeral=False)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Cancelled match end.", ephemeral=True)
        self.stop()

class Matches(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Load attempts
        try:
             load_matches_from_disk(bot)
        except Exception as e:
             print(f"Error loading matches: {e}")

    def get_player_team(self, user_id: int) -> Optional[str]:
        verified_users = load_verified_users()
        user = next((u for u in verified_users if str(u["discord_id"]) == str(user_id)), None)
        if user:
            return user["abbrev"]
        return None

    @app_commands.command(name="match_start", description="Start a match session in this channel.")
    @app_commands.describe(best_of="Best of X (default 3)")
    async def match_start(self, interaction: discord.Interaction, best_of: int = 3):
        if interaction.channel_id in active_matches:
            await interaction.response.send_message("❌ A match is already ongoing in this channel!", ephemeral=True)
            return

        active_matches[interaction.channel_id] = MatchSession(best_of, interaction.user)
        save_matches()
        await interaction.response.send_message(
            f"🏆 **Match Start!** (BO{best_of})\n"
            f"Marshal: {interaction.user.mention}",
        )

    @app_commands.command(name="game_result", description="Log a game result and wait for acknowledgement.")
    async def game_result(self, interaction: discord.Interaction, result: str):
        session = active_matches.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message("❌ No active match in this channel. Start one with `/match_start`.", ephemeral=True)
            return

        if session.status == "checking_ack":
            await interaction.response.send_message("⚠️ Still waiting for acknowledgement of the previous game!", ephemeral=True)
            return

        session.add_game(result)
        view = DisputeView(session)
        msg = await interaction.response.send_message(
            f"📢 **Game {len(session.games)} Result:**\n"
            f"# {result}\n\n"
            "**Waiting for Acknowledgement...**\n"
            "Team Captains/Members, please reply with **'I acknowledge'**.\n"
            "*(This process will auto-advance in 5 minutes)*",
            view=view
        )
        # Fetch the message object to store ID
        message = await interaction.original_response()
        session.last_message_id = message.id
        save_matches()

    @app_commands.command(name="match_undo_game", description="Remove the last logged game.")
    async def match_undo_game(self, interaction: discord.Interaction):
        session = active_matches.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)
            return

        if session.undo_game():
            await interaction.response.send_message("✅ Last game entry removed.", ephemeral=False)
        else:
            await interaction.response.send_message("❌ No games to undo.", ephemeral=True)

    @app_commands.command(name="match_force_ack", description="Force acknowledge a team for the current game.")
    async def match_force_ack(self, interaction: discord.Interaction, team_abbrev: str):
        session = active_matches.get(interaction.channel_id)
        if not session or session.status != "checking_ack":
            await interaction.response.send_message("❌ No game is currently waiting for acknowledgement.", ephemeral=True)
            return

        if session.is_disputed:
            await interaction.response.send_message("❌ A dispute is currently in progress. Resolve the dispute first.", ephemeral=True)
            return

        # Check 5-minute timer
        elapsed_seconds = session.get_effective_elapsed_time()
        required_seconds = 5 * 60
        if elapsed_seconds < required_seconds:
             remaining = required_seconds - elapsed_seconds
             minutes = int(remaining // 60)
             seconds = int(remaining % 60)
             await interaction.response.send_message(f"⏳ **Too Early:** You must wait 5 minutes before force acknowledging.\nTime Remaining: {minutes}m {seconds}s", ephemeral=True)
             return

        if session.ack_game(team_abbrev, f"{interaction.user.display_name} (Forced)"):
            await interaction.response.send_message(
                f"✅ **Game {len(session.games)} Acknowledged** (Forced for {team_abbrev}).\n"
                f"Result: {session.games[-1]['result']}\n"
                "Ready for next game or match end."
            )
        else:
            await interaction.response.send_message(f"✅ Forced acknowledgement for {team_abbrev}. Waiting for other team...", ephemeral=False)

    @app_commands.command(name="match_end", description="End the match session.")
    async def match_end(self, interaction: discord.Interaction):
        session = active_matches.get(interaction.channel_id)
        if not session:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)
            return

        # Check if enough games have been played
        min_games = session.get_min_games_required()
        if len(session.games) < min_games:
             await interaction.response.send_message(
                 f"❌ **Cannot End Match Yet:** Not enough games played.\n"
                 f"Required: {min_games} | Played: {len(session.games)}\n"
                 f"For BO{session.best_of}, you need at least {min_games} games.\n\n"
                 "💡 Use `/match_cancel` if you need to abort the session early.",
                 ephemeral=True
             )
             return

        summary = session.get_summary()
        view = EndMatchView(session, interaction.channel_id)
        
        # Check for un-acked games
        unacked = [i+1 for i, g in enumerate(session.games) if len(g["acks"]) < 2]
        warning = ""
        if unacked:
            warning = f"\n⚠️ **WARNING: Games {unacked} are not fully acknowledged!**"

        await interaction.response.send_message(
            f"🛑 **End Match Session?**\n{summary}{warning}",
            view=view,
            ephemeral=False # Show to everyone so they know it's ending
        )
    
    @app_commands.command(name="match_cancel", description="Force cancel the session without saving.")
    async def match_cancel(self, interaction: discord.Interaction):
        if interaction.channel_id in active_matches:
            del active_matches[interaction.channel_id]
            save_matches()
            await interaction.response.send_message("🗑️ Match session cancelled.", ephemeral=False)
        else:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        session = active_matches.get(message.channel.id)
        if not session or session.status != "checking_ack":
            return

        if "i acknowledge" in message.content.lower():
            # Check verification
            team = self.get_player_team(message.author.id)
            if not team:
                # User is not verified or not found in the CSV
                await message.add_reaction("❓") 
                return

            if team in session.games[-1]["acks"]:
                await message.add_reaction("⚠️") # Already acked
                return

            # Ack the game
            is_complete = session.ack_game(team, message.author.display_name)
            await message.add_reaction("✅")
            
            if is_complete:
                acks_dict = session.games[-1]["acks"]
                # Convert dict items to a list for indexing
                # Expected len is 2
                ack_list = list(acks_dict.items())
                
                # Format: match_abbrev: {'user': ..., 'timestamp': ...}
                team1 = ack_list[0][0]
                details1 = ack_list[0][1]
                team2 = ack_list[1][0]
                details2 = ack_list[1][1]
                
                timestamp_now = discord.utils.format_dt(datetime.now(timezone.utc), style="f")
                
                await message.channel.send(
                    f"✅ **The result of Game {len(session.games)} has been acknowledged by "
                    f"{details1['user']} of {team1} and {details2['user']} of {team2} on {timestamp_now}.**\n"
                    "Disputes for this are no longer valid beyond this point."
                )

async def setup(bot: commands.Bot):
    await bot.add_cog(Matches(bot))
