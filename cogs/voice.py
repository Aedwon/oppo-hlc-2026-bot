"""
Cog: Voice — Auto-create voice channels with team-based restrictions
- /setup_autocreate_vc  — designate a trigger VC
- /remove_autocreate_vc — remove a trigger VC
- on_voice_state_update — create new VC when user joins trigger, delete when empty
                        — mute/deafen non-team members, unmute/undeafen on leave
"""
import discord
from discord.ext import commands
from discord import app_commands

from db.database import Database
from utils.constants import VERIFICATION_ROLES


class Voice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory set of spawned channel IDs for fast lookup
        self._spawned: set[int] = set()
        self._trigger_channels: set[int] = set()
        # Map spawned channel_id -> team_name (for quick team checks)
        self._spawned_teams: dict[int, str | None] = {}
        # Track members we muted/deafened so we can restore them on leave
        self._restricted_members: set[tuple[int, int]] = set()  # (channel_id, member_id)
        self.bot.loop.create_task(self._load_state())

    # -- League Ops role ID (from verification roles) --
    @property
    def _league_ops_role_id(self) -> int:
        return VERIFICATION_ROLES.get("league ops", 0)

    async def _load_state(self):
        """Load trigger channels and spawned VCs from DB on startup."""
        await self.bot.wait_until_ready()

        rows = await Database.fetchall("SELECT trigger_channel_id FROM autocreate_vc_config")
        self._trigger_channels = {r["trigger_channel_id"] for r in rows}

        rows = await Database.fetchall("SELECT channel_id, team_name FROM spawned_vcs")
        self._spawned = {r["channel_id"] for r in rows}
        self._spawned_teams = {r["channel_id"]: r.get("team_name") for r in rows}

        # Clean up spawned VCs that no longer exist
        to_remove = []
        for cid in list(self._spawned):
            ch = self.bot.get_channel(cid)
            if ch is None:
                to_remove.append(cid)
        if to_remove:
            for cid in to_remove:
                self._spawned.discard(cid)
                self._spawned_teams.pop(cid, None)
                await Database.execute("DELETE FROM spawned_vcs WHERE channel_id = %s", (cid,))

    # ── Helper: check if a member belongs to the VC's team ─────────

    async def _is_team_or_ops(self, member: discord.Member, team_name: str | None) -> bool:
        """
        Return True if the member should be allowed to speak/hear:
        - They are the VC owner (handled separately before calling this)
        - They have the League Ops verification role
        - They are on the same team (verified_users.team_name matches)
        """
        # League Ops role holders are always allowed
        league_ops_role_id = self._league_ops_role_id
        if league_ops_role_id and any(r.id == league_ops_role_id for r in member.roles):
            return True

        # If the VC has no team association, allow everyone
        if not team_name:
            return True

        # Check the member's team in the DB
        row = await Database.fetchone(
            "SELECT team_name FROM verified_users WHERE guild_id = %s AND discord_id = %s",
            (member.guild.id, member.id),
        )
        if row and row.get("team_name") == team_name:
            return True

        return False

    # ── Admin commands ─────────────────────────────────────────

    @app_commands.command(
        name="setup_autocreate_vc",
        description="Designate a voice channel as an auto-create trigger.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The voice channel to use as trigger")
    async def setup_autocreate_vc(
        self, interaction: discord.Interaction, channel: discord.VoiceChannel
    ):
        try:
            await Database.execute(
                "INSERT IGNORE INTO autocreate_vc_config (guild_id, trigger_channel_id) VALUES (%s, %s)",
                (interaction.guild_id, channel.id),
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            return

        self._trigger_channels.add(channel.id)
        await interaction.response.send_message(
            f"✅ {channel.mention} is now an auto-create trigger. "
            "When someone joins it, a new VC will be created for them.",
            ephemeral=True,
        )

    @app_commands.command(
        name="remove_autocreate_vc",
        description="Remove a voice channel from auto-create triggers.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The trigger voice channel to remove")
    async def remove_autocreate_vc(
        self, interaction: discord.Interaction, channel: discord.VoiceChannel
    ):
        await Database.execute(
            "DELETE FROM autocreate_vc_config WHERE guild_id = %s AND trigger_channel_id = %s",
            (interaction.guild_id, channel.id),
        )
        self._trigger_channels.discard(channel.id)
        await interaction.response.send_message(
            f"✅ {channel.mention} is no longer an auto-create trigger.", ephemeral=True,
        )

    # ── Voice state listener ───────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        # --- User joined a trigger channel → create new VC ---
        if (
            after.channel
            and after.channel.id in self._trigger_channels
        ):
            guild = member.guild
            category = after.channel.category

            # Look up the creator's team from verified_users
            row = await Database.fetchone(
                "SELECT team_name FROM verified_users WHERE guild_id = %s AND discord_id = %s",
                (guild.id, member.id),
            )
            creator_team = row["team_name"] if row and row.get("team_name") else None

            # Create a new VC in the same category
            new_vc = await guild.create_voice_channel(
                name=f"{member.display_name}'s Channel",
                category=category,
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(connect=True),
                    member: discord.PermissionOverwrite(
                        connect=True, manage_channels=True,
                        move_members=True, mute_members=True,
                    ),
                },
                reason=f"Auto-created for {member}",
            )

            # Track it (with team name)
            self._spawned.add(new_vc.id)
            self._spawned_teams[new_vc.id] = creator_team
            await Database.execute(
                "INSERT INTO spawned_vcs (channel_id, guild_id, owner_id, team_name) VALUES (%s, %s, %s, %s)",
                (new_vc.id, guild.id, member.id, creator_team),
            )

            # Move user into the new VC
            try:
                await member.move_to(new_vc, reason="Auto-create VC")
            except discord.HTTPException:
                pass

        # --- User joined a spawned VC → check team and mute/deafen if needed ---
        if (
            after.channel
            and after.channel.id in self._spawned
            and (before.channel is None or before.channel.id != after.channel.id)
        ):
            vc_id = after.channel.id
            team_name = self._spawned_teams.get(vc_id)

            # Look up the VC owner to skip restriction for them
            owner_row = await Database.fetchone(
                "SELECT owner_id FROM spawned_vcs WHERE channel_id = %s",
                (vc_id,),
            )
            owner_id = owner_row["owner_id"] if owner_row else None

            # Don't restrict the VC owner
            if member.id != owner_id:
                allowed = await self._is_team_or_ops(member, team_name)
                if not allowed:
                    try:
                        await member.edit(
                            mute=True, deafen=True,
                            reason=f"Not on team '{team_name}' — auto-restricted in spawned VC",
                        )
                        self._restricted_members.add((vc_id, member.id))
                    except discord.Forbidden:
                        pass
                    except discord.HTTPException:
                        pass

        # --- User left a spawned VC → restore mute/deafen if we restricted them ---
        if (
            before.channel
            and before.channel.id in self._spawned
            and (after.channel is None or after.channel.id != before.channel.id)
        ):
            vc_id = before.channel.id
            key = (vc_id, member.id)

            if key in self._restricted_members:
                self._restricted_members.discard(key)
                # Only un-mute/deafen if the member is still in a voice channel
                # (they might have moved to another VC)
                if after.channel is not None:
                    try:
                        await member.edit(
                            mute=False, deafen=False,
                            reason="Restored after leaving team-restricted VC",
                        )
                    except discord.Forbidden:
                        pass
                    except discord.HTTPException:
                        pass

            # Check if empty (no members left) → delete
            vc = before.channel
            if len(vc.members) == 0:
                self._spawned.discard(vc.id)
                self._spawned_teams.pop(vc.id, None)
                # Clean up any lingering restricted member entries for this VC
                self._restricted_members = {
                    (cid, mid) for cid, mid in self._restricted_members if cid != vc.id
                }
                await Database.execute(
                    "DELETE FROM spawned_vcs WHERE channel_id = %s", (vc.id,)
                )
                try:
                    await vc.delete(reason="Auto-created VC is empty")
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
