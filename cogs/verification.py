"""
Cog: Verification
- /setup_verification       -- send panel with Verify button and guide image
- /set_verification_role    -- (legacy) set a single fallback role
- /set_verification_sheet   -- point to a public Google Sheet for validation
- /set_verification_guide   -- set the guide image URL shown during verification
- /toggle_verification_test -- enable/disable test mode
- /refresh_verification_data -- force-refresh cached sheet data

Flow:
  1. User clicks "Verify" on the panel
  2. Ephemeral message shows a guide image (where to find UID/Server)
     plus a team dropdown populated from the sheet data
  3. User selects team -> modal opens asking for UID and Server (integers)
  4. Bot validates against the sheet (or test data)
  5. On match: inserts into DB, assigns Discord role based on the sheet's "role" column
"""
import discord
from discord.ext import commands
from discord import app_commands
from db.database import Database
from utils.sheet_validator import validator
from utils.constants import VERIFICATION_ROLES

# Default guide image (can be overridden with /set_verification_guide)
DEFAULT_GUIDE_IMAGE = None  # Set via command


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

        # Get teams from the validator
        teams = await validator.get_teams()
        if not teams:
            await interaction.response.send_message(
                "No teams available. Ask an admin to configure the verification sheet.",
                ephemeral=True,
            )
            return

        # Build the guide embed
        guide_embed = discord.Embed(
            title="How to Verify",
            description=(
                "**Step 1:** Select your team from the dropdown below.\n"
                "**Step 2:** You will be asked for your **In-Game UID** and **Server ID**.\n\n"
                "Both UID and Server ID are **numbers**. "
                "See the image below for where to find them in-game."
            ),
            color=0xF2C21A,
        )

        # Load guide image URL from DB config
        guide_url = await Database.get_config(interaction.guild_id, "verification_guide_image")
        if guide_url:
            guide_embed.set_image(url=guide_url)

        mode_text = ""
        if validator.is_test_mode:
            mode_text = "\n\n*Test mode is active. Use the test entries to verify.*"
            guide_embed.set_footer(text="TEST MODE")

        if mode_text:
            guide_embed.description += mode_text

        await interaction.response.send_message(
            embed=guide_embed, view=TeamSelectView(teams), ephemeral=True
        )


# -- Team select dropdown ----------------------------------------------------

class TeamSelectView(discord.ui.View):
    def __init__(self, teams: list[str]):
        super().__init__(timeout=120)

        options = [
            discord.SelectOption(label=t, value=t)
            for t in teams[:25]
        ]

        self.select = discord.ui.Select(
            placeholder="Select your team...",
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
            label="In-Game UID (numbers only)",
            placeholder="e.g. 123456789",
            max_length=20,
        )
        self.server_input = discord.ui.TextInput(
            label="Server ID (numbers only)",
            placeholder="e.g. 1001",
            max_length=20,
        )
        self.add_item(self.uid_input)
        self.add_item(self.server_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        user = interaction.user

        # Validate that UID and Server are integers
        uid_raw = self.uid_input.value.strip()
        server_raw = self.server_input.value.strip()

        if not uid_raw.isdigit():
            await interaction.followup.send(
                "Your UID must contain only numbers. Please try again.",
                ephemeral=True,
            )
            return

        if not server_raw.isdigit():
            await interaction.followup.send(
                "Your Server ID must contain only numbers. Please try again.",
                ephemeral=True,
            )
            return

        # Double-check not already verified (race condition guard)
        existing = await Database.fetchone(
            "SELECT id FROM verified_users WHERE guild_id = %s AND discord_id = %s",
            (guild.id, user.id),
        )
        if existing:
            await interaction.followup.send("You are already verified!", ephemeral=True)
            return

        # Validate against sheet / test data
        matched = await validator.validate(self.team_name, uid_raw, server_raw)
        if not matched:
            mode_hint = " (test mode)" if validator.is_test_mode else ""
            await interaction.followup.send(
                f"Verification failed{mode_hint}.\n"
                "The team, UID, and server combination was not found in our records.\n"
                "Please double-check your details and try again.",
                ephemeral=True,
            )
            return

        # Insert into DB
        await Database.execute(
            "INSERT INTO verified_users (guild_id, discord_id, team_name, game_uid, server) "
            "VALUES (%s, %s, %s, %s, %s)",
            (guild.id, user.id, self.team_name, uid_raw, server_raw),
        )

        # Determine which role to assign based on the sheet's "role" column
        role_key = matched.get("role", "").strip().lower()
        role_id = VERIFICATION_ROLES.get(role_key)

        role_assigned = None
        if role_id:
            role = guild.get_role(role_id)
            if role:
                try:
                    await user.add_roles(role, reason=f"Verification: {role_key}")
                    role_assigned = role
                except discord.Forbidden:
                    pass

        # Fallback: also try the legacy per-guild verification role
        fallback_role_id_str = await Database.get_config(guild.id, "verification_role_id")
        if fallback_role_id_str:
            fallback_role = guild.get_role(int(fallback_role_id_str))
            if fallback_role and fallback_role != role_assigned:
                try:
                    await user.add_roles(fallback_role, reason="Verification completed")
                except discord.Forbidden:
                    pass

        role_name = role_key.title() if role_key else "Verified"
        await interaction.followup.send(
            f"Successfully verified as **{role_name}**!\n"
            f"**Team:** {self.team_name}\n"
            f"**UID:** {uid_raw}\n"
            f"**Server:** {server_raw}",
            ephemeral=True,
        )


# -- Cog ---------------------------------------------------------------------

class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(VerifyButtonView())

        # Load sheet config from DB if previously set (guild_id=0 for global)
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
                "You will need your **In-Game UID** and **Server ID** ready.\n"
                "Both are numbers you can find in your game profile."
            ),
            color=0xF2C21A,
        )

        # Add guide image to the panel itself too
        guide_url = await Database.get_config(interaction.guild_id, "verification_guide_image")
        if guide_url:
            embed.set_image(url=guide_url)

        embed.set_footer(text="System developed by Aedwon")
        await channel.send(embed=embed, view=VerifyButtonView())
        await interaction.response.send_message(
            f"Verification panel sent to {channel.mention}.", ephemeral=True
        )

    # -- Admin: configure fallback role --------------------------------------

    @app_commands.command(
        name="set_verification_role",
        description="Set an additional role given to everyone upon verification.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="Role to assign on successful verification (in addition to the sheet role)")
    async def set_verification_role(
        self, interaction: discord.Interaction, role: discord.Role
    ):
        await Database.set_config(interaction.guild_id, "verification_role_id", str(role.id))
        await interaction.response.send_message(
            f"Verification fallback role set to {role.mention}.\n"
            "This will be assigned in addition to the role determined by the sheet.",
            ephemeral=True,
        )

    # -- Admin: set guide image ----------------------------------------------

    @app_commands.command(
        name="set_verification_guide",
        description="Set the guide image shown during verification (URL to an image).",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(image_url="Direct URL to the guide image (png/jpg)")
    async def set_verification_guide(
        self, interaction: discord.Interaction, image_url: str
    ):
        await Database.set_config(interaction.guild_id, "verification_guide_image", image_url)

        preview = discord.Embed(title="Guide Image Preview", color=0xF2C21A)
        preview.set_image(url=image_url)
        await interaction.response.send_message(
            "Guide image saved. It will be shown when users click the Verify button.",
            embed=preview, ephemeral=True,
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

        await Database.set_config(0, "verification_sheet_id", sheet_id)
        await Database.set_config(0, "verification_sheet_gid", gid)
        await Database.set_config(0, "verification_test_mode", "0")

        try:
            count = await validator.refresh()
            await interaction.followup.send(
                f"Verification sheet configured.\n"
                f"**Sheet ID:** `{sheet_id}`\n"
                f"**Tab GID:** `{gid}`\n"
                f"**Entries loaded:** {count}\n"
                f"Test mode has been disabled.\n\n"
                f"Expected columns: `team_name`, `uid`, `server`, `role`\n"
                f"Valid role values: `player`, `staff`, `league ops`, `oppo`",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"Sheet ID saved but the initial fetch failed: {e}\n"
                "Make sure the sheet is shared as 'Anyone with the link can view'.",
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
            test_info = (
                "Verification test mode **enabled**.\n\n"
                "**Test entries (use these to verify):**\n"
                "```\n"
                "Team: Test Team   | UID: 123456789 | Server: 1001 | Role: Player\n"
                "Team: Test Team   | UID: 987654321 | Server: 1002 | Role: Player\n"
                "Team: Test Team   | UID: 111111111 | Server: 1003 | Role: Player\n"
                "Team: Staff Team  | UID: 100000001 | Server: 1001 | Role: Staff\n"
                "Team: Staff Team  | UID: 100000002 | Server: 1001 | Role: League Ops\n"
                "Team: OPPO        | UID: 200000001 | Server: 1001 | Role: OPPO\n"
                "```"
            )
            await interaction.response.send_message(test_info, ephemeral=True)
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
