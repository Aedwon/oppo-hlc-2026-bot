"""
Cog: Teams
- /add_teams     — bulk add team names (modal, one per line)
- /remove_team   — dropdown to remove a team
"""
import discord
from discord.ext import commands
from discord import app_commands
from db.database import Database


class BulkAddTeamsModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Add Teams (one per line)")
        self.teams_input = discord.ui.TextInput(
            label="Team Names",
            style=discord.TextStyle.paragraph,
            placeholder="Team Alpha\nTeam Bravo\nTeam Charlie",
            max_length=2000,
        )
        self.add_item(self.teams_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        raw = self.teams_input.value.strip()
        if not raw:
            await interaction.followup.send("No team names provided.", ephemeral=True)
            return

        names = [line.strip() for line in raw.splitlines() if line.strip()]
        if not names:
            await interaction.followup.send("No valid team names found.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        added = 0
        skipped = 0

        for name in names:
            try:
                await Database.execute(
                    "INSERT IGNORE INTO teams (guild_id, team_name) VALUES (%s, %s)",
                    (guild_id, name),
                )
                added += 1
            except Exception:
                skipped += 1

        await interaction.followup.send(
            f"Added **{added}** teams. Skipped **{skipped}** (duplicates or errors).",
            ephemeral=True,
        )


class RemoveTeamView(discord.ui.View):
    def __init__(self, teams: list[dict], author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id

        options = [
            discord.SelectOption(label=t["team_name"], value=t["team_name"])
            for t in teams[:25]
        ]

        self.select = discord.ui.Select(
            placeholder="Select a team to remove…",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This is not for you.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        team_name = self.select.values[0]
        await Database.execute(
            "DELETE FROM teams WHERE guild_id = %s AND team_name = %s",
            (interaction.guild_id, team_name),
        )
        await interaction.response.edit_message(
            content=f"Removed team **{team_name}**.", view=None
        )
        self.stop()


class Teams(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="add_teams", description="Bulk add team names (one per line).")
    @app_commands.default_permissions(administrator=True)
    async def add_teams(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BulkAddTeamsModal())

    @app_commands.command(name="remove_team", description="Remove a team from the list.")
    @app_commands.default_permissions(administrator=True)
    async def remove_team(self, interaction: discord.Interaction):
        teams = await Database.fetchall(
            "SELECT team_name FROM teams WHERE guild_id = %s ORDER BY team_name",
            (interaction.guild_id,),
        )
        if not teams:
            await interaction.response.send_message("No teams registered.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select a team to remove:", view=RemoveTeamView(teams, interaction.user.id), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Teams(bot))
