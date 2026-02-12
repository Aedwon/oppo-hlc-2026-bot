"""
Cog: Verification
- /setup_verification  — send panel with Verify button
- /set_verification_role — configure which role to grant
- Button → team select → modal (UID + Server) → DB insert + role grant
"""
import discord
from discord.ext import commands
from discord import app_commands
from db.database import Database


# ── Persistent button on the panel ──────────────────────────────

class VerifyButtonView(discord.ui.View):
    """Persistent view attached to the verification panel message."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Verify", style=discord.ButtonStyle.success,
        custom_id="verification_start",
    )
    async def start_verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if already verified
        row = await Database.fetchone(
            "SELECT id FROM verified_users WHERE guild_id = %s AND discord_id = %s",
            (interaction.guild_id, interaction.user.id),
        )
        if row:
            await interaction.response.send_message(
                "⚠️ You are already verified!", ephemeral=True
            )
            return

        # Fetch team list
        teams = await Database.fetchall(
            "SELECT team_name FROM teams WHERE guild_id = %s ORDER BY team_name",
            (interaction.guild_id,),
        )
        if not teams:
            await interaction.response.send_message(
                "⚠️ No teams have been configured yet. Ask an admin to add teams first.",
                ephemeral=True,
            )
            return

        # Show team select
        await interaction.response.send_message(
            "Select your team:", view=TeamSelectView(teams), ephemeral=True
        )


# ── Team select dropdown ────────────────────────────────────────

class TeamSelectView(discord.ui.View):
    def __init__(self, teams: list[dict]):
        super().__init__(timeout=120)

        options = [
            discord.SelectOption(label=t["team_name"], value=t["team_name"])
            for t in teams[:25]  # Discord max 25 options
        ]

        self.select = discord.ui.Select(
            placeholder="Choose your team…",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        team_name = self.select.values[0]
        await interaction.response.send_modal(VerifyModal(team_name))


# ── Verification modal ──────────────────────────────────────────

class VerifyModal(discord.ui.Modal):
    def __init__(self, team_name: str):
        super().__init__(title="Verification")
        self.team_name = team_name

        self.uid_input = discord.ui.TextInput(
            label="In-Game UID",
            placeholder="e.g. 123456789",
            max_length=50,
        )
        self.server_input = discord.ui.TextInput(
            label="Server",
            placeholder="e.g. SEA / NA / EU",
            max_length=50,
        )
        self.add_item(self.uid_input)
        self.add_item(self.server_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        user = interaction.user

        # Double-check not already verified (race condition guard)
        existing = await Database.fetchone(
            "SELECT id FROM verified_users WHERE guild_id = %s AND discord_id = %s",
            (guild.id, user.id),
        )
        if existing:
            await interaction.followup.send("⚠️ You are already verified!", ephemeral=True)
            return

        # Insert into DB
        await Database.execute(
            "INSERT INTO verified_users (guild_id, discord_id, team_name, game_uid, server) "
            "VALUES (%s, %s, %s, %s, %s)",
            (guild.id, user.id, self.team_name, self.uid_input.value, self.server_input.value),
        )

        # Assign role
        role_id_str = await Database.get_config(guild.id, "verification_role_id")
        if role_id_str:
            role = guild.get_role(int(role_id_str))
            if role:
                try:
                    await user.add_roles(role, reason="Verification completed")
                except discord.Forbidden:
                    await interaction.followup.send(
                        "✅ Verified! However, I couldn't assign the role (missing permissions).",
                        ephemeral=True,
                    )
                    return

        await interaction.followup.send(
            f"✅ Successfully verified!\n"
            f"**Team:** {self.team_name}\n"
            f"**UID:** {self.uid_input.value}\n"
            f"**Server:** {self.server_input.value}",
            ephemeral=True,
        )


# ── Cog ─────────────────────────────────────────────────────────

class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(VerifyButtonView())

    # ── Admin: send panel ───────────────────────────────────────

    @app_commands.command(
        name="setup_verification",
        description="Send the verification panel to a channel.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Channel to send the verification panel in")
    async def setup_verification(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        embed = discord.Embed(
            title="Verification",
            description=(
                "Welcome! Please verify your identity by clicking the button below.\n\n"
                "You will be asked for your **Team**, **In-Game UID**, and **Server**."
            ),
            color=0xF2C21A,
        )
        embed.set_footer(text="System developed by Aedwon")
        await channel.send(embed=embed, view=VerifyButtonView())
        await interaction.response.send_message(
            f"✅ Verification panel sent to {channel.mention}.", ephemeral=True
        )

    # ── Admin: configure role ───────────────────────────────────

    @app_commands.command(
        name="set_verification_role",
        description="Set the role given upon verification.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="Role to assign on successful verification")
    async def set_verification_role(
        self, interaction: discord.Interaction, role: discord.Role
    ):
        await Database.set_config(interaction.guild_id, "verification_role_id", str(role.id))
        await interaction.response.send_message(
            f"✅ Verification role set to {role.mention}.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
