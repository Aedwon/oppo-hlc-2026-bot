"""
Cog: Threads
- /create_threads — create private threads with a name prefix, count, and add members by role.
  Sends a plain text list of all thread links in the invoking channel.
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncio


class Threads(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="create_threads",
        description="Create multiple private threads with a name prefix and invite members.",
    )
    @app_commands.default_permissions(manage_threads=True)
    @app_commands.describe(
        prefix="Thread name prefix (e.g. 'Match' creates Match 1, Match 2, ...)",
        count="Number of threads to create (1-50)",
        role="Role whose members will be added to every thread",
    )
    async def create_threads(
        self,
        interaction: discord.Interaction,
        prefix: str,
        count: app_commands.Range[int, 1, 50],
        role: discord.Role = None,
    ):
        await interaction.response.defer(ephemeral=True)

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "This command must be used in a text channel.", ephemeral=True
            )
            return

        # Collect members from role
        members_to_add: list[discord.Member] = []
        if role:
            members_to_add = [m for m in role.members if not m.bot]

        created_threads: list[tuple[str, str]] = []

        await interaction.followup.send(
            f"Creating {count} private threads...", ephemeral=True
        )

        for i in range(1, count + 1):
            thread_name = f"{prefix} {i}"
            try:
                thread = await channel.create_thread(
                    name=thread_name,
                    type=discord.ChannelType.private_thread,
                    auto_archive_duration=10080,  # 7 days
                )

                for member in members_to_add:
                    try:
                        await thread.add_user(member)
                    except Exception:
                        pass

                created_threads.append((thread_name, thread.mention))

                if i < count:
                    await asyncio.sleep(1)

            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = getattr(e, "retry_after", 5)
                    await asyncio.sleep(retry_after)
                    try:
                        thread = await channel.create_thread(
                            name=thread_name,
                            type=discord.ChannelType.private_thread,
                            auto_archive_duration=10080,
                        )
                        for member in members_to_add:
                            try:
                                await thread.add_user(member)
                            except Exception:
                                pass
                        created_threads.append((thread_name, thread.mention))
                    except Exception:
                        created_threads.append((thread_name, "Failed"))
                else:
                    created_threads.append((thread_name, f"Error: {e}"))
            except Exception as e:
                created_threads.append((thread_name, f"Error: {e}"))

        # Send plain text list in the channel
        if created_threads:
            lines = [f"- {name} -- {mention}" for name, mention in created_threads]

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

        success = len([t for t in created_threads if "Error" not in t[1] and "Failed" not in t[1]])
        role_text = f" with {role.mention} members" if role else ""
        await interaction.followup.send(
            f"Created {success}/{count} threads{role_text}.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Threads(bot))
