"""
Shared UI views and components used across multiple cogs.
"""
import discord
from db.database import Database


class ConfirmView(discord.ui.View):
    """Generic Yes / No confirmation view."""

    def __init__(self, author_id: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.edit_message(content="✅ Confirmed.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="❌ Cancelled.", view=None)


class CancelScheduledEmbedView(discord.ui.View):
    """Dropdown to cancel a scheduled embed."""

    def __init__(self, scheduled_list: list[dict], embeds_cog, author: discord.User):
        super().__init__(timeout=60)
        self.embeds_cog = embeds_cog
        self.author = author

        options = []
        for entry in scheduled_list[:25]:  # Discord max 25 options
            label = f"{entry['identifier']} — {entry.get('schedule_for', '?')}"
            options.append(discord.SelectOption(label=label[:100], value=entry["identifier"]))

        if not options:
            return

        self.select = discord.ui.Select(
            placeholder="Select a scheduled embed to cancel...",
            options=options,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This is not for you.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        identifier = self.select.values[0]
        # Remove from DB
        await Database.execute(
            "DELETE FROM scheduled_embeds WHERE identifier = %s", (identifier,)
        )
        await interaction.response.edit_message(
            content=f"✅ Cancelled scheduled embed `{identifier}`.", view=None
        )
        self.stop()
