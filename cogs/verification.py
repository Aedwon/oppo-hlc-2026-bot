"""
Cog: Verification
- /setup_verification  -- send panel with Verify button
- /set_verification_role -- configure which role to grant
- /set_verification_sheet -- point to a public Google Sheet for validation
- /toggle_verification_test -- enable/disable test mode
- /refresh_verification_data -- force-refresh the cached sheet data
- Button -> team select -> modal (UID + Server) -> validate -> DB insert + role grant
"""
import discord
from discord.ext import commands
from discord import app_commands
from db.database import Database
from utils.sheet_validator import validator


# -- Persistent button on the panel ------------------------------------------

class VerifyButtonView(discord.ui.View):
    """Persistent view attached to the verification panel message."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify", style=discord.ButtonStyle.success,
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
                "You are already verified!", ephemeral=True
            )
            return

        # Get teams from the validator (sheet or test data)
        teams = await validator.get_teams()
        if not teams:
            await interaction.response.send_message(
                "No teams available. Ask an admin to configure the verification sheet.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Select your team:", view=TeamSelectView(teams), ephemeral=True
        )


# -- Team select dropdown ----------------------------------------------------

class TeamSelectView(discord.ui.View):
    def __init__(self, teams: list[str]):
        super().__init__(timeout=120)

        options = [
            discord.SelectOption(label=t, value=t)
            for t in teams[:25]  # Discord max 25 options
        ]

        self.select = discord.ui.Select(
            placeholder="Choose your team...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        team_name = self.select.values[0]
        await interaction.response.send_modal(VerifyModal(team_name))


# -- Verification modal ------------------------------------------------------

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
            await interaction.followup.send("You are already verified!", ephemeral=True)
            return

        # Validate against sheet / test data
        uid_val = self.uid_input.value.strip()
        server_val = self.server_input.value.strip()

        is_valid = await validator.validate(self.team_name, uid_val, server_val)
        if not is_valid:
            mode_hint = " (test mode)" if validator.is_test_mode else ""
            await interaction.followup.send(
                f"Verification failed{mode_hint}. "
                "The team, UID, and server combination was not found in our records. "
                "Please double-check your details and try again.",
                ephemeral=True,
            )
            return

        # Insert into DB
        await Database.execute(
            "INSERT INTO verified_users (guild_id, discord_id, team_name, game_uid, server) "
            "VALUES (%s, %s, %s, %s, %s)",
            (guild.id, user.id, self.team_name, uid_val, server_val),
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
                        "Verified! However, I couldn't assign the role (missing permissions).",
                        ephemeral=True,
                    )
                    return

        await interaction.followup.send(
            f"Successfully verified!\n"
            f"**Team:** {self.team_name}\n"
            f"**UID:** {uid_val}\n"
            f"**Server:** {server_val}",
            ephemeral=True,
        )


# -- Cog ---------------------------------------------------------------------

class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(VerifyButtonView())

        # Load sheet config from DB if previously set
        # (uses guild_id=0 as a global config key)
        sheet_id = await Database.get_config(0, "verification_sheet_id")
        sheet_gid = await Database.get_config(0, "verification_sheet_gid") or "0"
        test_mode = await Database.get_config(0, "verification_test_mode")

        if sheet_id:
            validator.configure_sheet(sheet_id, sheet_gid)
            print(f"   Verification sheet loaded: {sheet_id}")

        if test_mode == "1":
            validator.enable_test_mode()
            print("   Verification test mode: ON")
        elif sheet_id:
            validator.disable_test_mode()

    # -- Admin: send panel ---------------------------------------------------

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
            f"Verification panel sent to {channel.mention}.", ephemeral=True
        )

    # -- Admin: configure role -----------------------------------------------

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
            f"Verification role set to {role.mention}.", ephemeral=True
        )

    # -- Admin: configure Google Sheet ---------------------------------------

    @app_commands.command(
        name="set_verification_sheet",
        description="Set the Google Sheet URL used to validate verification data.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        url="Full Google Sheets URL or sheet ID",
        gid="Sheet tab GID (default: 0, the first tab)",
    )
    async def set_verification_sheet(
        self, interaction: discord.Interaction, url: str, gid: str = "0"
    ):
        await interaction.response.defer(ephemeral=True)

        sheet_id = validator.configure_sheet(url, gid)

        # Persist to DB (guild_id=0 for global config)
        await Database.set_config(0, "verification_sheet_id", sheet_id)
        await Database.set_config(0, "verification_sheet_gid", gid)
        await Database.set_config(0, "verification_test_mode", "0")

        # Try a fetch to confirm it works
        try:
            count = await validator.refresh()
            await interaction.followup.send(
                f"Verification sheet configured.\n"
                f"**Sheet ID:** `{sheet_id}`\n"
                f"**Tab GID:** `{gid}`\n"
                f"**Entries loaded:** {count}\n"
                f"Test mode has been disabled.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"Sheet ID saved, but the initial fetch failed: {e}\n"
                "Make sure the sheet is published or shared as 'Anyone with the link can view'.",
                ephemeral=True,
            )

    # -- Admin: toggle test mode ---------------------------------------------

    @app_commands.command(
        name="toggle_verification_test",
        description="Enable or disable verification test mode.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(enabled="True to enable test mode, False to disable")
    async def toggle_verification_test(
        self, interaction: discord.Interaction, enabled: bool
    ):
        if enabled:
            validator.enable_test_mode()
            await Database.set_config(0, "verification_test_mode", "1")
            await interaction.response.send_message(
                "Verification test mode **enabled**.\n"
                "Test entries: Team=`Test Team`, UID=`123456789`/`987654321`/`111111111`, Server=`SEA`/`NA`/`EU`",
                ephemeral=True,
            )
        else:
            if not validator.is_configured:
                await interaction.response.send_message(
                    "Cannot disable test mode: no Google Sheet is configured. "
                    "Use `/set_verification_sheet` first.",
                    ephemeral=True,
                )
                return
            validator.disable_test_mode()
            await Database.set_config(0, "verification_test_mode", "0")
            await interaction.response.send_message(
                "Verification test mode **disabled**. Validating against the configured sheet.",
                ephemeral=True,
            )

    # -- Admin: force refresh cache ------------------------------------------

    @app_commands.command(
        name="refresh_verification_data",
        description="Force-refresh the cached verification sheet data.",
    )
    @app_commands.default_permissions(administrator=True)
    async def refresh_verification_data(self, interaction: discord.Interaction):
        if validator.is_test_mode:
            await interaction.response.send_message(
                "Currently in test mode. No sheet to refresh.", ephemeral=True
            )
            return
        if not validator.is_configured:
            await interaction.response.send_message(
                "No sheet configured. Use `/set_verification_sheet` first.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        count = await validator.refresh()
        await interaction.followup.send(
            f"Sheet data refreshed. **{count}** entries loaded.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
