"""
Cog: Verification

Admin commands:
  /setup_verification        -- send panel with Verify + Staff buttons
  /set_verification_sheet    -- point to a public Google Sheet for validation
  /set_verification_guide    -- set the guide image URL shown during verification
  /toggle_verification_test  -- enable/disable test mode
  /refresh_verification_data -- force-refresh cached sheet data
  /set_oppo_passphrase       -- set secret passphrase for OPPO role
  /set_production_passphrase -- set secret passphrase + role for production team
  /set_staff_code            -- set access code for coach/manager verification
  /mention_team              -- mention all verified members of a team
  /reset_verifications       -- wipe verification records (granular)

Player flow:
  1. User clicks "Verify" on the panel
  2. Ephemeral guide image is shown (if configured), then modal opens
  3. User enters UID and Server (integers)
  4. Bot validates against the sheet (or test data)
  5. On match: assigns role, sets nickname, saves to DB, sends DM

Staff flow (coaches/managers):
  1. User clicks "Staff / Coach" on the panel
  2. Modal asks for: access code, IGN, team name, role (Coach/Manager)
  3. Bot validates code + team name against sheet
  4. On match: assigns Staff role, sets nickname, saves to DB with staff_type
"""
import discord
from discord.ext import commands
from discord import app_commands
from db.database import Database
from utils.sheet_validator import validator
from utils.constants import VERIFICATION_ROLES

# Channel where Marshals will mention teams on tournament day
MATCH_CHANNEL_ID = 1471154639893168129


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

        # Show guide image if configured, then open modal
        guide_url = await Database.get_config(interaction.guild_id, "verification_guide_image")

        if guide_url:
            guide_embed = discord.Embed(
                title="How to Find Your UID and Server ID",
                description=(
                    "Both your **UID** and **Server ID** are numbers found in your game profile.\n"
                    "Refer to the image below, then click **Continue** to proceed."
                ),
                color=0xF2C21A,
            )
            guide_embed.set_image(url=guide_url)

            if validator.is_test_mode:
                guide_embed.set_footer(text="TEST MODE")

            await interaction.response.send_message(
                embed=guide_embed,
                view=ContinueToModalView(),
                ephemeral=True,
            )
        else:
            # No guide image, go straight to modal
            await interaction.response.send_modal(VerifyModal())

    @discord.ui.button(
        label="Staff / Coach", style=discord.ButtonStyle.primary,
        custom_id="staff_verification_start",
    )
    async def start_staff_verify(self, interaction: discord.Interaction, button: discord.ui.Button):
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

        await interaction.response.send_message(
            "**Staff / Coach Verification**\n\n"
            "Please select your role below, then click **Continue**.",
            view=StaffRoleSelectView(),
            ephemeral=True,
        )


# -- Continue button (shown after guide image) -------------------------------

class ContinueToModalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyModal())


# -- Staff role selection (Coach / Manager dropdown) -------------------------

class StaffRoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.selected_role: str | None = None

    @discord.ui.select(
        placeholder="Select your role...",
        options=[
            discord.SelectOption(label="Coach", value="coach", emoji="🏆"),
            discord.SelectOption(label="Manager", value="manager", emoji="📋"),
        ],
    )
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_role = select.values[0]
        await interaction.response.edit_message(
            content=(
                f"**Staff / Coach Verification**\n\n"
                f"Selected role: **{self.selected_role.title()}**\n"
                f"Click **Continue** to proceed."
            ),
        )

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success, row=1)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_role:
            await interaction.response.send_message(
                "Please select a role first (Coach or Manager).", ephemeral=True
            )
            return
        await interaction.response.send_modal(StaffCodeModal(staff_type=self.selected_role))


# -- Verification modal ------------------------------------------------------

class VerifyModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Verification")

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

        # Validate numeric input
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

        # Race condition guard
        existing = await Database.fetchone(
            "SELECT id FROM verified_users WHERE guild_id = %s AND discord_id = %s",
            (guild.id, user.id),
        )
        if existing:
            await interaction.followup.send("You are already verified!", ephemeral=True)
            return

        # Validate against sheet / test data
        matched = await validator.validate(uid_raw, server_raw)

        # Fallback: check league ops manual entries
        if not matched:
            lops_row = await Database.fetchone(
                "SELECT ign FROM lops_entries WHERE guild_id = %s AND uid = %s AND server = %s",
                (guild.id, uid_raw, server_raw),
            )
            if lops_row:
                matched = {
                    "team_name": "League Operations",
                    "abbrev": "LOps",
                    "ign": lops_row["ign"],
                    "role": "league ops",
                    "uid": uid_raw,
                    "server": server_raw,
                }

        if not matched:
            mode_hint = " (test mode)" if validator.is_test_mode else ""
            await interaction.followup.send(
                f"Verification failed{mode_hint}.\n"
                "Your UID and Server ID combination was not found in our records.\n"
                "Please double-check your details and try again.",
                ephemeral=True,
            )
            return

        # Extract data from matched entry
        team_name = matched.get("team_name", "Unknown").strip()
        abbrev = matched.get("abbrev", "").strip()
        ign = matched.get("ign", "").strip()
        role_key = matched.get("role", "").strip().lower()

        # Insert into DB
        await Database.execute(
            "INSERT INTO verified_users (guild_id, discord_id, team_name, game_uid, server) "
            "VALUES (%s, %s, %s, %s, %s)",
            (guild.id, user.id, team_name, uid_raw, server_raw),
        )

        # Assign role based on sheet data
        role_id = VERIFICATION_ROLES.get(role_key)
        assigned_role_name = role_key.title() if role_key else "Verified"
        if role_id:
            role = guild.get_role(role_id)
            if role:
                try:
                    await user.add_roles(role, reason=f"Verification: {role_key}")
                except discord.Forbidden:
                    pass

        # Also assign fallback role if configured
        fallback_role_id_str = await Database.get_config(guild.id, "verification_role_id")
        if fallback_role_id_str:
            fallback_role = guild.get_role(int(fallback_role_id_str))
            if fallback_role:
                try:
                    await user.add_roles(fallback_role, reason="Verification completed")
                except discord.Forbidden:
                    pass

        # Set nickname to ABBREV | IGN
        if abbrev and ign:
            new_nick = f"{abbrev} | {ign}"
            try:
                await user.edit(nick=new_nick, reason="Verification nickname")
            except discord.Forbidden:
                pass  # Bot may not be able to change owner's nick

        # Respond in channel
        await interaction.followup.send(
            f"You have been successfully verified as **{assigned_role_name}**!\n"
            f"**Team:** {team_name}\n"
            f"Check your DMs for more details.",
            ephemeral=True,
        )

        # Send DM with confirmation and instructions
        try:
            dm_embed = discord.Embed(
                title="Verification Confirmed",
                description=(
                    f"You have been verified and assigned the **{assigned_role_name}** role "
                    f"under **{team_name}**.\n\n"
                    f"Please wait for a Marshal to mention you in your designated match thread "
                    f"in <#{MATCH_CHANNEL_ID}> on tournament day.\n\n"
                    f"Good luck and have fun!"
                ),
                color=0x00CC66,
            )
            dm_embed.add_field(name="Team", value=team_name, inline=True)
            dm_embed.add_field(name="Role", value=assigned_role_name, inline=True)
            if abbrev and ign:
                dm_embed.add_field(name="Nickname", value=f"{abbrev} | {ign}", inline=True)
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            pass  # DMs disabled


# -- Staff / Coach verification modal ----------------------------------------

class StaffCodeModal(discord.ui.Modal):
    def __init__(self, staff_type: str):
        super().__init__(title=f"{staff_type.title()} Verification")
        self.staff_type = staff_type

        self.code_input = discord.ui.TextInput(
            label="Access Code",
            placeholder="Enter the staff access code",
            max_length=100,
        )
        self.ign_input = discord.ui.TextInput(
            label="Your IGN (In-Game Name)",
            placeholder="e.g. CoachJohn",
            max_length=50,
        )
        self.team_input = discord.ui.TextInput(
            label="Team Name (from the registration sheet)",
            placeholder="e.g. NU BULLDOGS",
            max_length=100,
        )
        self.add_item(self.code_input)
        self.add_item(self.ign_input)
        self.add_item(self.team_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        user = interaction.user

        code = self.code_input.value.strip()
        ign = self.ign_input.value.strip()
        team_input = self.team_input.value.strip()
        role_input = self.staff_type  # Already validated via dropdown

        # Validate access code
        stored_code = await Database.get_config(guild.id, "staff_access_code")
        if not stored_code:
            await interaction.followup.send(
                "Staff verification is not configured yet. Please contact an admin.",
                ephemeral=True,
            )
            return

        if code != stored_code:
            await interaction.followup.send(
                "Invalid access code. Please check with your tournament admin.",
                ephemeral=True,
            )
            return

        # Race condition guard
        existing = await Database.fetchone(
            "SELECT id FROM verified_users WHERE guild_id = %s AND discord_id = %s",
            (guild.id, user.id),
        )
        if existing:
            await interaction.followup.send("You are already verified!", ephemeral=True)
            return

        # Fuzzy-match team name against sheet
        teams = await validator.get_teams()
        matched_team = None
        input_lower = team_input.lower()

        # Exact match (case-insensitive)
        for t in teams:
            if t.lower() == input_lower:
                matched_team = t
                break

        # Partial / substring match
        if not matched_team:
            candidates = [t for t in teams if input_lower in t.lower() or t.lower() in input_lower]
            if len(candidates) == 1:
                matched_team = candidates[0]
            elif len(candidates) > 1:
                suggestion_list = "\n".join(f"• {c}" for c in candidates[:10])
                await interaction.followup.send(
                    f"Multiple teams match **{team_input}**. "
                    f"Please enter the exact team name:\n{suggestion_list}",
                    ephemeral=True,
                )
                return

        if not matched_team:
            # No match at all — suggest similar teams
            all_list = "\n".join(f"• {t}" for t in teams[:20])
            await interaction.followup.send(
                f"Team **{team_input}** was not found in the registration sheet.\n\n"
                f"Available teams (showing first 20):\n{all_list}",
                ephemeral=True,
            )
            return

        # Get team abbreviation from sheet
        roster = await validator.get_team_roster(matched_team)
        abbrev = roster[0].get("abbrev", "") if roster else ""

        # Enforce max 1 coach + 1 manager per team
        existing_staff = await Database.fetchone(
            "SELECT discord_id FROM verified_users "
            "WHERE guild_id = %s AND team_name = %s AND staff_type = %s",
            (guild.id, matched_team, self.staff_type),
        )
        if existing_staff:
            role_display = self.staff_type.title()
            await interaction.followup.send(
                f"**{matched_team}** already has a **{role_display}** "
                f"(<@{existing_staff['discord_id']}>).\n"
                f"Each team can only have 1 Coach and 1 Manager.\n\n"
                f"If this is an error, ask an admin to reset that person's verification first.",
                ephemeral=True,
            )
            return

        # Insert into DB with staff_type
        await Database.execute(
            "INSERT INTO verified_users (guild_id, discord_id, team_name, game_uid, server, staff_type) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (guild.id, user.id, matched_team, "STAFF", "0", self.staff_type),
        )

        # Assign Staff role (1471152576366907534)
        staff_role_id = VERIFICATION_ROLES.get("staff")
        if staff_role_id:
            role = guild.get_role(staff_role_id)
            if role:
                try:
                    await user.add_roles(role, reason=f"Staff verification: {role_input}")
                except discord.Forbidden:
                    pass

        # Also assign fallback role if configured
        fallback_role_id_str = await Database.get_config(guild.id, "verification_role_id")
        if fallback_role_id_str:
            fallback_role = guild.get_role(int(fallback_role_id_str))
            if fallback_role:
                try:
                    await user.add_roles(fallback_role, reason="Staff verification completed")
                except discord.Forbidden:
                    pass

        # Set nickname to ABBREV | IGN
        if abbrev and ign:
            new_nick = f"{abbrev} | {ign}"
            try:
                await user.edit(nick=new_nick, reason=f"Staff verification ({role_input})")
            except discord.Forbidden:
                pass

        role_display = role_input.title()  # "Coach" or "Manager"

        await interaction.followup.send(
            f"You have been verified as **{role_display}** for **{matched_team}**!\n"
            f"Check your DMs for more details.",
            ephemeral=True,
        )

        # Send DM
        try:
            dm_embed = discord.Embed(
                title="Staff Verification Confirmed",
                description=(
                    f"You have been verified as **{role_display}** for **{matched_team}**.\n\n"
                    f"Please wait for a Marshal to mention you in your designated match thread "
                    f"in <#{MATCH_CHANNEL_ID}> on tournament day.\n\n"
                    f"Good luck and have fun!"
                ),
                color=0x00CC66,
            )
            dm_embed.add_field(name="Team", value=matched_team, inline=True)
            dm_embed.add_field(name="Role", value=role_display, inline=True)
            if abbrev and ign:
                dm_embed.add_field(name="Nickname", value=f"{abbrev} | {ign}", inline=True)
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            pass  # DMs disabled


# -- Cog ---------------------------------------------------------------------

class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(VerifyButtonView())

        # Load sheet config from DB if previously set (guild_id=0 for global)
        sheet_id = await Database.get_config(0, "verification_sheet_id")
        sheet_gid = await Database.get_config(0, "verification_sheet_gid") or "0"
        sheet_tab = await Database.get_config(0, "verification_sheet_tab")
        test_mode = await Database.get_config(0, "verification_test_mode")

        if sheet_id:
            validator.configure_sheet(sheet_id, sheet_gid, tab_name=sheet_tab)
            print(f"   Verification sheet loaded: {sheet_id}")
            if sheet_tab:
                print(f"   Sheet tab: {sheet_tab}")

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
                "Welcome! Please verify your identity by clicking the appropriate button below.\n\n"
                "**Players:** Click **Verify** — you will need your **In-Game UID** and **Server ID**.\n"
                "**Coaches / Managers:** Click **Staff / Coach** — you will need the staff access code."
            ),
            color=0xF2C21A,
        )

        guide_url = await Database.get_config(interaction.guild_id, "verification_guide_image")
        if guide_url:
            embed.set_image(url=guide_url)

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
        tab_name="Sheet tab name (default: FINAL Teams Database)",
        gid="Sheet tab GID (fallback if tab_name is not set)",
    )
    async def set_verification_sheet(
        self, interaction: discord.Interaction, url: str,
        tab_name: str = "FINAL Teams Database", gid: str = "0",
    ):
        await interaction.response.defer(ephemeral=True)

        sheet_id = validator.configure_sheet(url, gid, tab_name=tab_name)

        await Database.set_config(0, "verification_sheet_id", sheet_id)
        await Database.set_config(0, "verification_sheet_gid", gid)
        await Database.set_config(0, "verification_sheet_tab", tab_name)
        await Database.set_config(0, "verification_test_mode", "0")

        try:
            count = await validator.refresh()
            teams = await validator.get_teams()
            await interaction.followup.send(
                f"Verification sheet configured.\n"
                f"**Sheet ID:** `{sheet_id}`\n"
                f"**Tab:** `{tab_name}`\n"
                f"**Entries loaded:** {count} players across {len(teams)} teams\n"
                f"Test mode has been disabled.\n\n"
                f"Sheet columns: `Team Name`, `Abbrev`, `IGN`, `UID`, `Server`\n"
                f"All entries are assigned the **Player** role.",
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
                "UID: 123456789 | Server: 1001 | Team: Test Team  | Nick: TT | TestPlayer1  | Role: Player\n"
                "UID: 987654321 | Server: 1002 | Team: Test Team  | Nick: TT | TestPlayer2  | Role: Player\n"
                "UID: 111111111 | Server: 1003 | Team: Test Team  | Nick: TT | TestPlayer3  | Role: Player\n"
                "UID: 222222222 | Server: 1001 | Team: Alpha Squad| Nick: AS | AlphaLead    | Role: Player\n"
                "UID: 333333333 | Server: 1001 | Team: Alpha Squad| Nick: AS | AlphaSub     | Role: Player\n"
                "UID: 100000001 | Server: 1001 | Team: Staff Team | Nick: STAFF | StaffMember1| Role: Staff\n"
                "UID: 100000002 | Server: 1001 | Team: Staff Team | Nick: STAFF | StaffMember2| Role: League Ops\n"
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
        description="Clear all cached sheet data and re-fetch from Google Sheets.",
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

        # Fully nuke in-memory cache first
        validator.clear_cache()

        # Force fresh fetch
        count = await validator.refresh()

        # Show diagnostic info: sample teams so you can verify it's fresh
        entries = await validator.get_all_entries()
        teams = sorted(set(e.get("team_name", "") for e in entries if e.get("team_name")))
        sample = ", ".join(teams[:10]) + ("..." if len(teams) > 10 else "")

        await interaction.followup.send(
            f"✅ Cache cleared and data re-fetched.\n"
            f"**Entries loaded:** {count}\n"
            f"**Teams found ({len(teams)}):** {sample}",
            ephemeral=True,
        )

    # -- Admin: set OPPO passphrase ------------------------------------------

    @app_commands.command(
        name="set_oppo_passphrase",
        description="Set the secret passphrase for OPPO team verification.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(passphrase="The passphrase users type to get the OPPO role (e.g. !OPPOteam)")
    async def set_oppo_passphrase(
        self, interaction: discord.Interaction, passphrase: str
    ):
        clean = passphrase.strip()
        await Database.set_config(interaction.guild_id, "oppo_passphrase", clean)
        await interaction.response.send_message(
            f"OPPO passphrase set. Users who type `{clean}` will receive the OPPO role "
            "and their message will be deleted instantly.",
            ephemeral=True,
        )

    # -- Admin: set production passphrase ------------------------------------

    @app_commands.command(
        name="set_production_passphrase",
        description="Set the secret passphrase and role for production team verification.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        passphrase="The passphrase users type to get the production role",
        role="The role to assign when the passphrase is used",
    )
    async def set_production_passphrase(
        self, interaction: discord.Interaction, passphrase: str, role: discord.Role
    ):
        clean = passphrase.strip()
        await Database.set_config(interaction.guild_id, "production_passphrase", clean)
        await Database.set_config(interaction.guild_id, "production_role_id", str(role.id))
        await interaction.response.send_message(
            f"Production passphrase set. Users who type `{clean}` will receive {role.mention} "
            "and their message will be deleted instantly.",
            ephemeral=True,
        )

    # -- Admin: set staff access code ----------------------------------------

    @app_commands.command(
        name="set_staff_code",
        description="Set the access code for coach/manager staff verification.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(code="The access code coaches/managers enter to verify")
    async def set_staff_code(
        self, interaction: discord.Interaction, code: str
    ):
        clean = code.strip()
        await Database.set_config(interaction.guild_id, "staff_access_code", clean)
        await interaction.response.send_message(
            f"✅ Staff access code set to `{clean}`.\n\n"
            "Coaches and managers can now use the **Staff / Coach** button on the "
            "verification panel. They will need to enter this code, their IGN, "
            "their team name, and whether they are a Coach or Manager.\n\n"
            "They will receive the **Staff** role and their nickname will be "
            "set to **ABBREV | IGN**.",
            ephemeral=True,
        )

    # -- Admin: manage League Ops entries ------------------------------------

    @app_commands.command(
        name="add_lops",
        description="Add a League Ops member entry for verification.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        uid="In-game UID (numbers only)",
        server="Server ID (numbers only)",
        ign="In-game name",
    )
    async def add_lops(
        self, interaction: discord.Interaction,
        uid: str, server: str, ign: str,
    ):
        uid = uid.strip()
        server = server.strip()
        ign = ign.strip()

        if not uid.isdigit():
            await interaction.response.send_message("❌ UID must be numbers only.", ephemeral=True)
            return
        if not server.isdigit():
            await interaction.response.send_message("❌ Server must be numbers only.", ephemeral=True)
            return
        if not ign:
            await interaction.response.send_message("❌ IGN cannot be empty.", ephemeral=True)
            return

        try:
            await Database.execute(
                "INSERT INTO lops_entries (guild_id, uid, server, ign, added_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (interaction.guild_id, uid, server, ign, interaction.user.id),
            )
        except Exception:
            await interaction.response.send_message(
                f"❌ An entry with UID `{uid}` and Server `{server}` already exists.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ League Ops entry added:\n"
            f"**IGN:** {ign}\n"
            f"**UID:** {uid}\n"
            f"**Server:** {server}\n\n"
            f"When this person verifies, they'll get the **League Ops** role "
            f"and their nickname will be set to **LOps | {ign}**.",
            ephemeral=True,
        )

    @app_commands.command(
        name="remove_lops",
        description="Remove a League Ops member entry.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(uid="The UID of the entry to remove")
    async def remove_lops(self, interaction: discord.Interaction, uid: str):
        uid = uid.strip()
        rows_deleted = await Database.execute(
            "DELETE FROM lops_entries WHERE guild_id = %s AND uid = %s",
            (interaction.guild_id, uid),
        )
        if rows_deleted:
            await interaction.response.send_message(
                f"✅ Removed League Ops entry with UID `{uid}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ No entry found with UID `{uid}`.", ephemeral=True
            )

    @app_commands.command(
        name="list_lops",
        description="List all League Ops verification entries.",
    )
    @app_commands.default_permissions(administrator=True)
    async def list_lops(self, interaction: discord.Interaction):
        rows = await Database.fetchall(
            "SELECT uid, server, ign, added_by FROM lops_entries "
            "WHERE guild_id = %s ORDER BY ign",
            (interaction.guild_id,),
        )
        if not rows:
            await interaction.response.send_message(
                "No League Ops entries found. Use `/add_lops` to add one.",
                ephemeral=True,
            )
            return

        lines = []
        for r in rows:
            lines.append(
                f"• **{r['ign']}** — UID: `{r['uid']}` | Server: `{r['server']}` "
                f"(added by <@{r['added_by']}>)"
            )

        embed = discord.Embed(
            title="League Ops Entries",
            description="\n".join(lines),
            color=0xF2C21A,
        )
        embed.set_footer(text=f"{len(rows)} entry/entries")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- Autocomplete helpers -------------------------------------------------

    async def verified_team_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for teams that have at least one verified member."""
        rows = await Database.fetchall(
            "SELECT DISTINCT team_name FROM verified_users WHERE guild_id = %s ORDER BY team_name",
            (interaction.guild_id,),
        )
        teams = [row["team_name"] for row in rows if row["team_name"]]
        filtered = [t for t in teams if current.lower() in t.lower()]
        return [
            app_commands.Choice(name=t, value=t)
            for t in filtered[:25]
        ]

    async def sheet_team_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for ALL teams from the sheet (not just verified)."""
        try:
            teams = await validator.get_teams()
        except Exception:
            teams = []
        filtered = [t for t in teams if current.lower() in t.lower()]
        return [
            app_commands.Choice(name=t, value=t)
            for t in filtered[:25]
        ]

    # -- Marshal: mention a team ---------------------------------------------

    @app_commands.command(
        name="mention_team",
        description="Mention all verified members of a team in the current channel.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(team="Team to mention (only shows teams with verified members)")
    @app_commands.autocomplete(team=verified_team_autocomplete)
    async def mention_team(self, interaction: discord.Interaction, team: str):
        rows = await Database.fetchall(
            "SELECT discord_id FROM verified_users WHERE guild_id = %s AND team_name = %s",
            (interaction.guild_id, team),
        )
        if not rows:
            await interaction.response.send_message(
                f"No verified members found for **{team}**.", ephemeral=True
            )
            return

        mentions = " ".join(f"<@{row['discord_id']}>" for row in rows)
        await interaction.response.send_message(
            f"**{team}** — {mentions}"
        )

    # -- Marshal: list verified teams ----------------------------------------

    @app_commands.command(
        name="list_teams",
        description="List all teams that have at least one verified member.",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def list_teams(self, interaction: discord.Interaction):
        rows = await Database.fetchall(
            "SELECT DISTINCT team_name FROM verified_users WHERE guild_id = %s ORDER BY team_name",
            (interaction.guild_id,),
        )
        teams = [row["team_name"] for row in rows if row["team_name"]]
        if not teams:
            await interaction.response.send_message(
                "No verified teams found.", ephemeral=True
            )
            return

        listing = "\n".join(f"• {t}" for t in teams)
        embed = discord.Embed(
            title="Verified Teams",
            description=listing,
            color=0xF2C21A,
        )
        embed.set_footer(text=f"{len(teams)} team(s) with verified members")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- Marshal: team verification stats (enhanced) -------------------------

    @app_commands.command(
        name="team_stats",
        description="Show verification stats for all teams (cross-referenced with sheet).",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def team_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Get all sheet entries grouped by team
        sheet_entries = await validator.get_all_entries()
        sheet_teams: dict[str, int] = {}
        for e in sheet_entries:
            name = e.get("team_name", "").strip()
            if name:
                sheet_teams[name] = sheet_teams.get(name, 0) + 1

        # Get verified counts from DB
        db_rows = await Database.fetchall(
            "SELECT team_name, COUNT(*) AS count FROM verified_users "
            "WHERE guild_id = %s GROUP BY team_name ORDER BY team_name",
            (interaction.guild_id,),
        )
        verified_counts = {row["team_name"]: row["count"] for row in db_rows}

        if not sheet_teams and not verified_counts:
            await interaction.followup.send(
                "No teams found. Is the verification sheet configured?",
                ephemeral=True,
            )
            return

        # Merge: all teams from sheet + any extras from DB
        all_teams = set(sheet_teams.keys()) | set(verified_counts.keys())
        lines = []
        total_players = 0
        total_verified = 0
        fully_verified = 0

        for team in sorted(all_teams):
            roster_size = sheet_teams.get(team, 0)
            v_count = verified_counts.get(team, 0)
            total_players += roster_size
            total_verified += v_count

            if roster_size > 0:
                if v_count >= roster_size:
                    icon = "✅"
                    fully_verified += 1
                elif v_count > 0:
                    icon = "⚠️"
                else:
                    icon = "❌"
                lines.append(f"{icon} **{team}** — {v_count}/{roster_size} verified")
            else:
                # Team from DB but not in sheet (e.g. League Ops)
                lines.append(f"🔹 **{team}** — {v_count} verified (not in sheet)")

        description = "\n".join(lines)

        # Paginate if too long for one embed
        if len(description) > 4000:
            description = description[:4000] + "\n...truncated"

        embed = discord.Embed(
            title="Team Verification Stats",
            description=description,
            color=0xF2C21A,
        )
        embed.set_footer(
            text=(
                f"{len(all_teams)} team(s) • {total_verified}/{total_players} players verified • "
                f"{fully_verified} fully verified"
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -- Marshal: per-team roster status -------------------------------------

    @app_commands.command(
        name="team_roster",
        description="Show per-player verification status for a team.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(team="Team to inspect (shows all teams from the sheet)")
    @app_commands.autocomplete(team=sheet_team_autocomplete)
    async def team_roster(self, interaction: discord.Interaction, team: str):
        await interaction.response.defer(ephemeral=True)

        # Get roster from sheet
        roster = await validator.get_team_roster(team)
        if not roster:
            await interaction.followup.send(
                f"No players found for **{team}** in the sheet.", ephemeral=True
            )
            return

        # Get verified users for this team from DB
        db_rows = await Database.fetchall(
            "SELECT game_uid, discord_id, staff_type FROM verified_users "
            "WHERE guild_id = %s AND team_name = %s",
            (interaction.guild_id, team),
        )
        verified_uids = {row["game_uid"]: row["discord_id"] for row in db_rows}

        v_count = 0
        lines = []
        for entry in roster:
            uid = entry.get("uid", "")
            ign = entry.get("ign", "")
            if uid in verified_uids:
                discord_id = verified_uids[uid]
                lines.append(f"✅ **{ign}** — <@{discord_id}>")
                v_count += 1
            else:
                lines.append(f"❌ **{ign}** — not verified")

        # Append coaches/managers (staff entries not in sheet)
        staff_rows = [r for r in db_rows if r.get("staff_type")]
        for sr in staff_rows:
            staff_label = sr["staff_type"].title()  # "Coach" or "Manager"
            lines.append(f"🔷 **{staff_label}** — <@{sr['discord_id']}>")

        abbrev = roster[0].get("abbrev", "") if roster else ""
        header = f"{team}"
        if abbrev:
            header += f" [{abbrev}]"

        footer_parts = [f"{v_count}/{len(roster)} players verified"]
        if staff_rows:
            footer_parts.append(f"{len(staff_rows)} staff")

        embed = discord.Embed(
            title=header,
            description="\n".join(lines),
            color=0x00CC66 if v_count == len(roster) else (0xF2C21A if v_count > 0 else 0xFF4444),
        )
        embed.set_footer(text=" • ".join(footer_parts))
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -- Marshal: list all unverified players ---------------------------------

    @app_commands.command(
        name="unverified",
        description="List all unverified players from the sheet, grouped by team.",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def unverified(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        sheet_entries = await validator.get_all_entries()
        if not sheet_entries:
            await interaction.followup.send(
                "No sheet data available. Is the verification sheet configured?",
                ephemeral=True,
            )
            return

        # Get all verified UIDs for this guild
        db_rows = await Database.fetchall(
            "SELECT game_uid FROM verified_users WHERE guild_id = %s",
            (interaction.guild_id,),
        )
        verified_uids = {row["game_uid"] for row in db_rows}

        # Group unverified by team
        unverified_by_team: dict[str, list[str]] = {}
        for entry in sheet_entries:
            uid = entry.get("uid", "")
            if uid not in verified_uids:
                team = entry.get("team_name", "Unknown").strip()
                ign = entry.get("ign", "?").strip()
                if team not in unverified_by_team:
                    unverified_by_team[team] = []
                unverified_by_team[team].append(ign)

        if not unverified_by_team:
            await interaction.followup.send(
                "🎉 All players from the sheet have been verified!",
                ephemeral=True,
            )
            return

        lines = []
        total_unverified = 0
        for team in sorted(unverified_by_team.keys()):
            players = unverified_by_team[team]
            total_unverified += len(players)
            player_list = ", ".join(players)
            lines.append(f"**{team}** ({len(players)}): {player_list}")

        description = "\n".join(lines)
        if len(description) > 4000:
            description = description[:4000] + "\n...truncated"

        embed = discord.Embed(
            title="Unverified Players",
            description=description,
            color=0xFF4444,
        )
        embed.set_footer(
            text=f"{total_unverified} unverified player(s) across {len(unverified_by_team)} team(s)"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -- Marshal: overall verification progress ------------------------------

    @app_commands.command(
        name="verification_progress",
        description="Show an overall verification progress dashboard.",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def verification_progress(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        sheet_entries = await validator.get_all_entries()

        # Sheet stats
        sheet_teams: dict[str, int] = {}
        for e in sheet_entries:
            name = e.get("team_name", "").strip()
            if name:
                sheet_teams[name] = sheet_teams.get(name, 0) + 1
        total_teams = len(sheet_teams)
        total_players = sum(sheet_teams.values())

        # DB stats
        db_rows = await Database.fetchall(
            "SELECT team_name, COUNT(*) AS count FROM verified_users "
            "WHERE guild_id = %s GROUP BY team_name",
            (interaction.guild_id,),
        )
        verified_counts = {row["team_name"]: row["count"] for row in db_rows}
        total_verified = sum(verified_counts.values())

        # Fully verified teams
        fully_verified = 0
        partially_verified = 0
        not_started = 0
        for team, size in sheet_teams.items():
            v = verified_counts.get(team, 0)
            if v >= size:
                fully_verified += 1
            elif v > 0:
                partially_verified += 1
            else:
                not_started += 1

        pct = (total_verified / total_players * 100) if total_players > 0 else 0

        # Progress bar
        bar_len = 20
        filled = round(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)

        embed = discord.Embed(
            title="Verification Progress",
            color=0x00CC66 if pct == 100 else (0xF2C21A if pct >= 50 else 0xFF4444),
        )
        embed.description = (
            f"```\n{bar} {pct:.1f}%\n```\n"
            f"**Players:** {total_verified}/{total_players} verified\n"
            f"**Teams:** {total_teams} total\n"
            f"\n"
            f"✅ Fully verified: {fully_verified}\n"
            f"⚠️ Partially verified: {partially_verified}\n"
            f"❌ Not started: {not_started}"
        )

        if validator.is_test_mode:
            embed.set_footer(text="TEST MODE")
        elif validator.tab_name:
            embed.set_footer(text=f"Sheet tab: {validator.tab_name}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # -- Admin: verification reset helpers ------------------------------------

    async def _get_verification_roles(self, guild: discord.Guild) -> list[discord.Role]:
        """Collect all verification role objects for a guild."""
        roles = []
        for role_id in VERIFICATION_ROLES.values():
            role = guild.get_role(role_id)
            if role:
                roles.append(role)

        fallback_role_id_str = await Database.get_config(guild.id, "verification_role_id")
        if fallback_role_id_str:
            fallback_role = guild.get_role(int(fallback_role_id_str))
            if fallback_role:
                roles.append(fallback_role)
        return roles

    async def _strip_member_verification(
        self, member: discord.Member, roles_to_remove: list[discord.Role], reason: str
    ) -> bool:
        """Strip verification roles and reset nickname for a single member. Returns True on success."""
        try:
            member_roles = [r for r in roles_to_remove if r in member.roles]
            if member_roles:
                await member.remove_roles(*member_roles, reason=reason)
            if member.nick:
                try:
                    await member.edit(nick=None, reason=reason)
                except discord.Forbidden:
                    pass
            return True
        except discord.Forbidden:
            return False

    # -- Admin: reset verifications (granular) -------------------------------

    @app_commands.command(
        name="reset_verifications",
        description="Reset verifications: a specific user, a team, or everything.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        user="Reset a specific user's verification",
        team="Reset all verifications for a specific team",
        confirm_all="Type 'yes' to reset ALL verifications (required when no user/team specified)",
    )
    @app_commands.autocomplete(team=verified_team_autocomplete)
    async def reset_verifications(
        self, interaction: discord.Interaction,
        user: discord.Member | None = None,
        team: str | None = None,
        confirm_all: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        # Force-refresh sheet cache so re-verification uses latest data
        if not validator.is_test_mode:
            await validator.refresh()

        guild = interaction.guild
        roles_to_remove = await self._get_verification_roles(guild)

        # --- Reset a specific user ---
        if user:
            row = await Database.fetchone(
                "SELECT id FROM verified_users WHERE guild_id = %s AND discord_id = %s",
                (guild.id, user.id),
            )
            if not row:
                await interaction.followup.send(
                    f"{user.mention} has no verification record.", ephemeral=True
                )
                return

            await self._strip_member_verification(user, roles_to_remove, "Verification reset (user)")
            await Database.execute(
                "DELETE FROM verified_users WHERE guild_id = %s AND discord_id = %s",
                (guild.id, user.id),
            )
            await interaction.followup.send(
                f"✅ Verification reset for {user.mention}.\n"
                "Their roles and nickname have been cleared.",
                ephemeral=True,
            )
            return

        # --- Reset a specific team ---
        if team:
            rows = await Database.fetchall(
                "SELECT discord_id FROM verified_users WHERE guild_id = %s AND team_name = %s",
                (guild.id, team),
            )
            if not rows:
                await interaction.followup.send(
                    f"No verified members found for **{team}**.", ephemeral=True
                )
                return

            cleared = 0
            for row in rows:
                member = guild.get_member(row["discord_id"])
                if member:
                    if await self._strip_member_verification(member, roles_to_remove, f"Verification reset ({team})"):
                        cleared += 1

            await Database.execute(
                "DELETE FROM verified_users WHERE guild_id = %s AND team_name = %s",
                (guild.id, team),
            )
            await interaction.followup.send(
                f"✅ Verification reset for **{team}**.\n"
                f"**Records deleted:** {len(rows)}\n"
                f"**Roles/nicknames cleared:** {cleared}",
                ephemeral=True,
            )
            return

        # --- Reset ALL (requires confirmation) ---
        if not confirm_all or confirm_all.strip().lower() != "yes":
            await interaction.followup.send(
                "⚠️ **This will reset ALL verifications for the entire server.**\n\n"
                "To confirm, run the command again with `confirm_all: yes`.\n\n"
                "💡 You can also reset selectively:\n"
                "• `/reset_verifications user:@someone` — reset one user\n"
                "• `/reset_verifications team:Team Name` — reset one team",
                ephemeral=True,
            )
            return

        rows = await Database.fetchall(
            "SELECT discord_id FROM verified_users WHERE guild_id = %s",
            (guild.id,),
        )

        cleared = 0
        for row in rows:
            member = guild.get_member(row["discord_id"])
            if member:
                if await self._strip_member_verification(member, roles_to_remove, "Verification reset (all)"):
                    cleared += 1

        await Database.execute(
            "DELETE FROM verified_users WHERE guild_id = %s",
            (guild.id,),
        )

        await interaction.followup.send(
            f"✅ All verifications have been reset.\n"
            f"**Records deleted:** {len(rows)}\n"
            f"**Roles/nicknames cleared:** {cleared}",
            ephemeral=True,
        )

    # -- Listener: OPPO passphrase -------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()

        # --- OPPO passphrase ---
        oppo_passphrase = await Database.get_config(message.guild.id, "oppo_passphrase")
        if not oppo_passphrase:
            oppo_passphrase = "!OPPOteam"

        if content == oppo_passphrase:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

            oppo_role_id = VERIFICATION_ROLES.get("oppo")
            if not oppo_role_id:
                return

            role = message.guild.get_role(oppo_role_id)
            if not role:
                return

            if role in message.author.roles:
                try:
                    await message.author.send("You already have the OPPO role!")
                except discord.Forbidden:
                    pass
                return

            try:
                await message.author.add_roles(role, reason="OPPO passphrase verification")
            except discord.Forbidden:
                try:
                    await message.author.send("Could not assign the OPPO role (missing permissions).")
                except discord.Forbidden:
                    pass
                return

            try:
                await message.author.send(
                    "You have been verified as **OPPO** team. Welcome!"
                )
            except discord.Forbidden:
                pass
            return

        # --- Production passphrase ---
        prod_passphrase = await Database.get_config(message.guild.id, "production_passphrase")
        if prod_passphrase and content == prod_passphrase:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

            prod_role_id_str = await Database.get_config(message.guild.id, "production_role_id")
            if not prod_role_id_str:
                return

            role = message.guild.get_role(int(prod_role_id_str))
            if not role:
                return

            if role in message.author.roles:
                try:
                    await message.author.send("You already have the Production role!")
                except discord.Forbidden:
                    pass
                return

            try:
                await message.author.add_roles(role, reason="Production passphrase verification")
            except discord.Forbidden:
                try:
                    await message.author.send("Could not assign the Production role (missing permissions).")
                except discord.Forbidden:
                    pass
                return

            try:
                await message.author.send(
                    f"You have been verified as **Production** team and received the **{role.name}** role. Welcome!"
                )
            except discord.Forbidden:
                pass
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
