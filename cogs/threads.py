"""
Cog: Threads
- /create_threads — create private threads with a name prefix, count, and tagged members.
  Sends a plain text list of all thread links in the invoking channel.
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio


class CreateThreadsModal(discord.ui.Modal):
    """Modal to collect thread creation parameters."""

    def __init__(self):
        super().__init__(title="Create Private Threads")

        self.prefix_input = discord.ui.TextInput(
            label="Thread Name Prefix",
            placeholder="e.g. Match",
            max_length=80,
        )
        self.count_input = discord.ui.TextInput(
            label="Number of Threads",
            placeholder="e.g. 10 (max 50)",
            max_length=3,
        )
        self.members_input = discord.ui.TextInput(
            label="Member IDs (comma separated)",
            style=discord.TextStyle.paragraph,
            placeholder="e.g. 123456789, 987654321 or @User1, @User2",
            required=False,
            max_length=2000,
        )
        self.add_item(self.prefix_input)
        self.add_item(self.count_input)
        self.add_item(self.members_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        prefix = self.prefix_input.value.strip()
        try:
            count = int(self.count_input.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Count must be a number.", ephemeral=True)
            return

        if count < 1 or count > 50:
            await interaction.followup.send("❌ Count must be between 1 and 50.", ephemeral=True)
            return

        # Parse member IDs
        member_ids: list[int] = []
        if self.members_input.value.strip():
            import re
            raw = self.members_input.value.strip()
            # Extract numeric IDs (handles both raw IDs and <@123456> mention format)
            found = re.findall(r"(\d{17,20})", raw)
            member_ids = [int(mid) for mid in found]

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ This command must be used in a text channel.", ephemeral=True)
            return

        created_threads: list[tuple[str, str]] = []  # (name, mention)

        await interaction.followup.send(
            f"⏳ Creating {count} private threads...", ephemeral=True,
        )

        for i in range(1, count + 1):
            thread_name = f"{prefix} {i}"
            try:
                thread = await channel.create_thread(
                    name=thread_name,
                    type=discord.ChannelType.private_thread,
                    auto_archive_duration=10080,  # 7 days
                )

                # Add members
                for mid in member_ids:
                    member = interaction.guild.get_member(mid)
                    if member:
                        try:
                            await thread.add_user(member)
                        except Exception:
                            pass

                created_threads.append((thread_name, thread.mention))

                # Respect rate limits: small delay between creations
                if i < count:
                    await asyncio.sleep(1)

            except discord.HTTPException as e:
                if e.status == 429:
                    # Rate limited — wait and retry
                    retry_after = getattr(e, "retry_after", 5)
                    await asyncio.sleep(retry_after)
                    try:
                        thread = await channel.create_thread(
                            name=thread_name,
                            type=discord.ChannelType.private_thread,
                            auto_archive_duration=10080,
                        )
                        for mid in member_ids:
                            member = interaction.guild.get_member(mid)
                            if member:
                                try:
                                    await thread.add_user(member)
                                except Exception:
                                    pass
                        created_threads.append((thread_name, thread.mention))
                    except Exception:
                        created_threads.append((thread_name, "❌ Failed"))
                else:
                    created_threads.append((thread_name, f"❌ Error: {e}"))
            except Exception as e:
                created_threads.append((thread_name, f"❌ Error: {e}"))

        # Send plain text list (not embed) in the channel
        if created_threads:
            lines = [f"{idx}. {name} — {mention}" for idx, (name, mention) in enumerate(created_threads, 1)]

            # Split into chunks if needed (Discord 2000 char limit)
            chunks: list[str] = []
            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) + 1 > 1900:
                    chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += ("\n" + line if current_chunk else line)
            if current_chunk:
                chunks.append(current_chunk)

            for chunk in chunks:
                await channel.send(chunk)

        await interaction.followup.send(
            f"✅ Created {len([t for t in created_threads if '❌' not in t[1]])} / {count} threads.",
            ephemeral=True,
        )


class Threads(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="create_threads",
        description="Create multiple private threads with a name prefix and invite members.",
    )
    @app_commands.default_permissions(manage_threads=True)
    async def create_threads(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CreateThreadsModal())


async def setup(bot: commands.Bot):
    await bot.add_cog(Threads(bot))
