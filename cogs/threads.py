"""
Cog: Threads
- /create_threads  -- create private threads with a name prefix, count, and add members by role
- /delete_threads  -- delete all threads in the current channel
- Auto-adds members to linked threads when they receive a linked role
"""
import discord
from discord.ext import commands
from discord import app_commands
from db.database import Database
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

        created_threads: list[tuple[str, str, int]] = []  # (name, mention, thread_id)

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

                created_threads.append((thread_name, thread.mention, thread.id))

                # Save thread-role link for auto-add
                if role:
                    await Database.execute(
                        "INSERT IGNORE INTO thread_role_links (guild_id, thread_id, role_id, channel_id) "
                        "VALUES (%s, %s, %s, %s)",
                        (interaction.guild_id, thread.id, role.id, channel.id),
                    )

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
                        created_threads.append((thread_name, thread.mention, thread.id))
                        if role:
                            await Database.execute(
                                "INSERT IGNORE INTO thread_role_links (guild_id, thread_id, role_id, channel_id) "
                                "VALUES (%s, %s, %s, %s)",
                                (interaction.guild_id, thread.id, role.id, channel.id),
                            )
                    except Exception:
                        created_threads.append((thread_name, "Failed", 0))
                else:
                    created_threads.append((thread_name, f"Error: {e}", 0))
            except Exception as e:
                created_threads.append((thread_name, f"Error: {e}", 0))

        # Send plain text list in the channel
        if created_threads:
            success = len([t for t in created_threads if "Error" not in t[1] and "Failed" not in t[1]])
            role_text = f" with **{role.name}** members" if role else ""
            header = f"**Created {success}/{count} threads{role_text}:**\n"

            lines = [f"- {name} -- {mention}" for name, mention, _ in created_threads]

            chunks: list[str] = []
            current_chunk = header
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

        role_text = f" with {role.mention} members" if role else ""
        await interaction.followup.send(
            f"Created {success}/{count} threads{role_text}.", ephemeral=True
        )

    @app_commands.command(
        name="delete_threads",
        description="Delete all threads in the current channel.",
    )
    @app_commands.default_permissions(manage_threads=True)
    async def delete_threads(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command must be used in a text channel.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Fetch all active and archived threads
        threads_to_delete = []

        for thread in channel.threads:
            threads_to_delete.append(thread)

        async for thread in channel.archived_threads(limit=None):
            threads_to_delete.append(thread)

        try:
            async for thread in channel.archived_threads(private=True, limit=None):
                if thread not in threads_to_delete:
                    threads_to_delete.append(thread)
        except discord.Forbidden:
            pass

        if not threads_to_delete:
            await interaction.followup.send("No threads found in this channel.", ephemeral=True)
            return

        deleted = 0
        for thread in threads_to_delete:
            try:
                # Clean up thread-role links
                await Database.execute(
                    "DELETE FROM thread_role_links WHERE thread_id = %s", (thread.id,)
                )
                await thread.delete()
                deleted += 1
                await asyncio.sleep(0.5)
            except Exception:
                pass

        await interaction.followup.send(
            f"Deleted {deleted}/{len(threads_to_delete)} threads.", ephemeral=True
        )

    # -- Auto-add members when they receive a linked role --------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return

        # Find newly added roles
        added_roles = set(after.roles) - set(before.roles)
        if not added_roles:
            return

        for role in added_roles:
            # Check if this role is linked to any threads
            rows = await Database.fetchall(
                "SELECT thread_id FROM thread_role_links WHERE guild_id = %s AND role_id = %s",
                (after.guild.id, role.id),
            )
            if not rows:
                continue

            for row in rows:
                thread = after.guild.get_thread(row["thread_id"])
                if thread:
                    try:
                        await thread.add_user(after)
                    except Exception:
                        pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Threads(bot))
