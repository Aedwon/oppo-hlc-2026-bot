"""
Cog: Help
- /help — dynamic command listing, permission-aware (admin sees all, non-admin sees limited)
"""
import discord
from discord.ext import commands
from discord import app_commands


# Map cog names to display-friendly names and emojis
COG_DISPLAY = {
    "Verification": ("✅ Verification", "Verify your identity"),
    "Tickets": ("🎫 Tickets", "Support ticket system"),
    "Embeds": ("📨 Embeds", "Send / edit embeds via Discohook"),
    "Threads": ("🧵 Threads", "Auto-create private threads"),
    "Voice": ("🔊 Voice", "Auto-create voice channels"),
    "Teams": ("👥 Teams", "Team management and mentions"),
    "Matches": ("🏆 Matches", "Tournament match management"),
    "Help": ("❓ Help", "View available commands"),
}


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="View all available bot commands.")
    async def help_command(self, interaction: discord.Interaction):
        is_admin = interaction.user.guild_permissions.administrator
        tree = self.bot.tree

        # Gather all app commands
        all_commands = tree.get_commands()

        # Group by cog
        cog_commands: dict[str, list[app_commands.Command]] = {}
        for cmd in all_commands:
            # Try to find which cog owns this command
            cog_name = None
            for name, cog in self.bot.cogs.items():
                cog_cmds = cog.__cog_app_commands__
                for cc in cog_cmds:
                    if cc.name == cmd.name:
                        cog_name = name
                        break
                if cog_name:
                    break

            if not cog_name:
                cog_name = "Other"

            cog_commands.setdefault(cog_name, []).append(cmd)

        embed = discord.Embed(
            title="📖 Bot Commands",
            description="Here are all available commands." if is_admin else "Showing commands you have access to.",
            color=0xF2C21A,
        )

        for cog_name in COG_DISPLAY:
            cmds = cog_commands.get(cog_name, [])
            if not cmds:
                continue

            display_name, display_desc = COG_DISPLAY.get(cog_name, (cog_name, ""))

            lines = []
            for cmd in cmds:
                # Check permissions for non-admin
                if not is_admin:
                    # Skip admin-only commands
                    if cmd.default_permissions and cmd.default_permissions.administrator:
                        continue

                params = ""
                if cmd.parameters:
                    param_parts = []
                    for p in cmd.parameters:
                        if p.required:
                            param_parts.append(f"`<{p.name}>`")
                        else:
                            param_parts.append(f"`[{p.name}]`")
                    params = " " + " ".join(param_parts)

                lines.append(f"**/{cmd.name}**{params}\n> {cmd.description}")

            if lines:
                # Split into chunks that fit Discord's 1024-char field limit
                chunks = []
                current_chunk: list[str] = []
                current_len = 0
                for line in lines:
                    line_len = len(line) + 1  # +1 for newline
                    if current_len + line_len > 1024 and current_chunk:
                        chunks.append("\n".join(current_chunk))
                        current_chunk = [line]
                        current_len = line_len
                    else:
                        current_chunk.append(line)
                        current_len += line_len
                if current_chunk:
                    chunks.append("\n".join(current_chunk))

                for i, chunk in enumerate(chunks):
                    field_name = display_name if i == 0 else f"{display_name} (cont.)"
                    embed.add_field(name=field_name, value=chunk, inline=False)

        # Handle "Other" cog
        other_cmds = cog_commands.get("Other", [])
        if other_cmds:
            lines = []
            for cmd in other_cmds:
                if not is_admin and cmd.default_permissions and cmd.default_permissions.administrator:
                    continue
                lines.append(f"**/{cmd.name}** — {cmd.description}")
            if lines:
                embed.add_field(name="🔧 Other", value="\n".join(lines), inline=False)


        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
