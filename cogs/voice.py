"""
Cog: Voice — Auto-create voice channels
- /setup_autocreate_vc  — designate a trigger VC
- /remove_autocreate_vc — remove a trigger VC
- on_voice_state_update — create new VC when user joins trigger, delete when empty
"""
import discord
from discord.ext import commands
from discord import app_commands

from db.database import Database


class Voice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory set of spawned channel IDs for fast lookup
        self._spawned: set[int] = set()
        self._trigger_channels: set[int] = set()
        self.bot.loop.create_task(self._load_state())

    async def _load_state(self):
        """Load trigger channels and spawned VCs from DB on startup."""
        await self.bot.wait_until_ready()

        rows = await Database.fetchall("SELECT trigger_channel_id FROM autocreate_vc_config")
        self._trigger_channels = {r["trigger_channel_id"] for r in rows}

        rows = await Database.fetchall("SELECT channel_id FROM spawned_vcs")
        self._spawned = {r["channel_id"] for r in rows}

        # Clean up spawned VCs that no longer exist
        to_remove = []
        for cid in list(self._spawned):
            ch = self.bot.get_channel(cid)
            if ch is None:
                to_remove.append(cid)
        if to_remove:
            for cid in to_remove:
                self._spawned.discard(cid)
                await Database.execute("DELETE FROM spawned_vcs WHERE channel_id = %s", (cid,))

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

            # Track it
            self._spawned.add(new_vc.id)
            await Database.execute(
                "INSERT INTO spawned_vcs (channel_id, guild_id, owner_id) VALUES (%s, %s, %s)",
                (new_vc.id, guild.id, member.id),
            )

            # Move user into the new VC
            try:
                await member.move_to(new_vc, reason="Auto-create VC")
            except discord.HTTPException:
                pass

        # --- User left a spawned VC → delete if empty ---
        if (
            before.channel
            and before.channel.id in self._spawned
        ):
            vc = before.channel
            # Check if empty (no members left)
            if len(vc.members) == 0:
                self._spawned.discard(vc.id)
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
