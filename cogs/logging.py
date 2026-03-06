"""
Cog: Logging
- /set_log_channel   -- configure a log channel (commands, tickets, embeds)
- /view_log_channels -- show current log channel configuration
- Global command listener that logs all slash command invocations
"""
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone

from utils.logger import (
    get_log_channel,
    set_log_channel,
    get_all_log_channels,
    LOG_TYPES,
)


class Logging(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -- Admin: set log channel -----------------------------------------------

    @app_commands.command(
        name="set_log_channel",
        description="Set a log channel for a specific log type.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        log_type="The type of log to configure",
        channel="The channel to send logs to",
    )
    @app_commands.choices(
        log_type=[
            app_commands.Choice(name="Commands Log", value="commands"),
            app_commands.Choice(name="Tickets Log", value="tickets"),
            app_commands.Choice(name="Embeds Log", value="embeds"),
        ]
    )
    async def set_log_channel_cmd(
        self,
        interaction: discord.Interaction,
        log_type: str,
        channel: discord.TextChannel,
    ):
        await set_log_channel(interaction.guild_id, log_type, channel.id)
        await interaction.response.send_message(
            f"✅ **{log_type.title()} log** channel set to {channel.mention}.\n"
            "This setting is saved and will persist across bot restarts.",
            ephemeral=True,
        )

    # -- Admin: view log channels ---------------------------------------------

    @app_commands.command(
        name="view_log_channels",
        description="View the current log channel configuration.",
    )
    @app_commands.default_permissions(administrator=True)
    async def view_log_channels(self, interaction: discord.Interaction):
        channels = await get_all_log_channels(self.bot, interaction.guild_id)

        embed = discord.Embed(
            title="📋 Log Channel Configuration",
            color=0xF2C21A,
        )

        for log_type, channel in channels.items():
            label = log_type.title()
            if channel:
                value = f"{channel.mention} (`{channel.id}`)"
            else:
                value = "❌ Not configured"
            embed.add_field(name=f"{label} Log", value=value, inline=False)

        embed.set_footer(text="Use /set_log_channel to configure")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- Global command listener ----------------------------------------------

    @commands.Cog.listener()
    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.ContextMenu,
    ):
        log_channel = await get_log_channel(self.bot, interaction.guild_id, "commands")
        if not log_channel:
            return

        # Build parameter string
        params = ""
        if interaction.namespace:
            parts = []
            for key, value in interaction.namespace:
                # Format mentions for roles, channels, users
                if isinstance(value, (discord.Member, discord.User)):
                    display = f"{value.mention} ({value})"
                elif isinstance(value, discord.Role):
                    display = f"{value.mention}"
                elif isinstance(value, (discord.TextChannel, discord.VoiceChannel, discord.Thread)):
                    display = f"{value.mention}"
                else:
                    display = f"`{value}`"
                parts.append(f"**{key}:** {display}")
            params = "\n".join(parts)

        embed = discord.Embed(
            title=f"/{command.name}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=str(interaction.user),
            icon_url=interaction.user.display_avatar.url,
        )
        embed.add_field(
            name="User",
            value=f"{interaction.user.mention} (`{interaction.user.id}`)",
            inline=True,
        )
        embed.add_field(
            name="Channel",
            value=f"{interaction.channel.mention}" if interaction.channel else "Unknown",
            inline=True,
        )
        if params:
            # Truncate if too long
            if len(params) > 1024:
                params = params[:1020] + "..."
            embed.add_field(name="Parameters", value=params, inline=False)

        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Logging(bot))
