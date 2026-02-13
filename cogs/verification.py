"""
Cog: Verification

Admin commands:
  /setup_verification       -- send panel with Verify button
  /set_verification_sheet   -- point to a public Google Sheet for validation
  /set_verification_guide   -- set the guide image URL shown during verification
  /toggle_verification_test -- enable/disable test mode
  /refresh_verification_data -- force-refresh cached sheet data
  /set_oppo_passphrase      -- set secret passphrase for OPPO role
  /mention_team             -- mention all verified members of a team
  /reset_verifications      -- wipe all verification records and roles

Flow:
  1. User clicks "Verify" on the panel
  2. Ephemeral guide image is shown (if configured), then modal opens
  3. User enters UID and Server (integers)
  4. Bot validates against the sheet (or test data)
  5. On match:
     - Assigns Discord role based on sheet "role" column
     - Sets nickname to "ABBREV | IGN" from sheet data
     - Saves to DB
     - Sends DM with confirmation and tournament instructions
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


# -- Continue button (shown after guide image) -------------------------------

class ContinueToModalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyModal())


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
                f"Expected columns: `uid`, `server`, `team_name`, `abbrev`, `ign`, `role`\n"
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

    # -- League Ops: mention a team ------------------------------------------

    async def team_autocomplete(
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

    @app_commands.command(
        name="mention_team",
        description="Mention all verified members of a team in the current channel.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(team="Team to mention (only shows teams with verified members)")
    @app_commands.autocomplete(team=team_autocomplete)
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

    # -- League Ops: list verified teams -------------------------------------

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

    # -- League Ops: team verification stats ---------------------------------

    @app_commands.command(
        name="team_stats",
        description="Show verification stats for all teams.",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def team_stats(self, interaction: discord.Interaction):
        rows = await Database.fetchall(
            "SELECT team_name, COUNT(*) AS count FROM verified_users "
            "WHERE guild_id = %s GROUP BY team_name ORDER BY team_name",
            (interaction.guild_id,),
        )
        if not rows:
            await interaction.response.send_message(
                "No verified members found.", ephemeral=True
            )
            return

        total = sum(row["count"] for row in rows)
        listing = "\n".join(
            f"• **{row['team_name']}** — {row['count']} verified"
            for row in rows
        )

        embed = discord.Embed(
            title="Verification Stats",
            description=listing,
            color=0xF2C21A,
        )
        embed.set_footer(text=f"{len(rows)} team(s) • {total} total verified")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- Admin: reset all verifications --------------------------------------

    @app_commands.command(
        name="reset_verifications",
        description="Remove all verification records and strip verification roles from members.",
    )
    @app_commands.default_permissions(administrator=True)
    async def reset_verifications(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild

        # Get all verified users
        rows = await Database.fetchall(
            "SELECT discord_id FROM verified_users WHERE guild_id = %s",
            (guild.id,),
        )

        # Collect all verification role objects
        roles_to_remove = []
        for role_id in VERIFICATION_ROLES.values():
            role = guild.get_role(role_id)
            if role:
                roles_to_remove.append(role)

        fallback_role_id_str = await Database.get_config(guild.id, "verification_role_id")
        if fallback_role_id_str:
            fallback_role = guild.get_role(int(fallback_role_id_str))
            if fallback_role:
                roles_to_remove.append(fallback_role)

        # Strip roles and reset nicknames
        removed_count = 0
        for row in rows:
            member = guild.get_member(row["discord_id"])
            if member:
                try:
                    member_roles_to_remove = [r for r in roles_to_remove if r in member.roles]
                    if member_roles_to_remove:
                        await member.remove_roles(*member_roles_to_remove, reason="Verification reset")
                    # Reset nickname
                    if member.nick:
                        try:
                            await member.edit(nick=None, reason="Verification reset")
                        except discord.Forbidden:
                            pass
                    removed_count += 1
                except discord.Forbidden:
                    pass

        # Delete all records
        await Database.execute(
            "DELETE FROM verified_users WHERE guild_id = %s",
            (guild.id,),
        )

        await interaction.followup.send(
            f"All verifications have been reset.\n"
            f"**Records deleted:** {len(rows)}\n"
            f"**Roles/nicknames cleared:** {removed_count}",
            ephemeral=True,
        )

    # -- Listener: OPPO passphrase -------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        passphrase = await Database.get_config(message.guild.id, "oppo_passphrase")
        if not passphrase:
            passphrase = "!OPPOteam"

        if message.content.strip() != passphrase:
            return

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


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
