"""
Cog: Embeds
Ported from reference embeds.py — slash commands only, MySQL-backed scheduling.
Commands: /send_embed, /edit_embed, /dl_embed, /cancel_scheduled_embed
"""
import discord
from discord.ext import commands
from discord import app_commands
import base64
import json
import random
import string
from urllib.parse import urlparse, parse_qs, quote
from io import BytesIO
from datetime import datetime
import pytz
import asyncio

from db.database import Database
from utils.constants import EMBED_LOG_CHANNEL_ID
from utils.views import CancelScheduledEmbedView


def discohook_to_view(components_data):
    """Convert Discohook component JSON to a discord.py View."""
    if not components_data:
        return None
    view = discord.ui.View(timeout=None)
    for row in components_data:
        for comp in row.get("components", []):
            t = comp.get("type")
            if t == 2:  # Button
                style = comp.get("style", 1)
                label = comp.get("label")
                custom_id = comp.get("custom_id")
                url = comp.get("url")
                emoji = None
                if comp.get("emoji"):
                    emoji = comp["emoji"].get("name") or comp["emoji"].get("id")
                disabled = comp.get("disabled", False)
                if style == 5 and url:
                    button = discord.ui.Button(
                        style=discord.ButtonStyle.link, label=label,
                        url=url, emoji=emoji, disabled=disabled,
                    )
                else:
                    button = discord.ui.Button(
                        style=discord.ButtonStyle(style), label=label,
                        custom_id=custom_id, emoji=emoji, disabled=disabled,
                    )
                view.add_item(button)
            elif t == 3:  # String Select Menu
                options = []
                for opt in comp.get("options", []):
                    options.append(discord.SelectOption(
                        label=opt.get("label"), value=opt.get("value"),
                        description=opt.get("description"),
                        emoji=opt.get("emoji", {}).get("name") if opt.get("emoji") else None,
                        default=opt.get("default", False),
                    ))
                select = discord.ui.Select(
                    custom_id=comp.get("custom_id"),
                    placeholder=comp.get("placeholder"),
                    min_values=comp.get("min_values", 1),
                    max_values=comp.get("max_values", 1),
                    options=options, disabled=comp.get("disabled", False),
                )
                view.add_item(select)
    return view if len(view.children) > 0 else None


def generate_identifier(length=6):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


class Embeds(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scheduled_tasks: list[asyncio.Task] = []
        self.bot.loop.create_task(self._load_and_schedule())

    # ── Startup: reschedule pending embeds ─────────────────────

    async def _load_and_schedule(self):
        await self.bot.wait_until_ready()
        tz = pytz.timezone("Asia/Manila")
        now = datetime.now(tz)

        rows = await Database.fetchall("SELECT * FROM scheduled_embeds")
        for entry in rows:
            dt = entry["schedule_for"]
            if dt.tzinfo is None:
                dt = tz.localize(dt)
            if dt > now:
                self.scheduled_tasks.append(
                    asyncio.create_task(self._delayed_send(entry))
                )
            else:
                # Past due — send immediately or clean up
                await Database.execute(
                    "DELETE FROM scheduled_embeds WHERE id = %s", (entry["id"],)
                )

    async def _delayed_send(self, entry):
        try:
            tz = pytz.timezone("Asia/Manila")
            dt = entry["schedule_for"]
            if dt.tzinfo is None:
                dt = tz.localize(dt)
            now = datetime.now(tz)
            delay = (dt - now).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)

            channel = self.bot.get_channel(entry["channel_id"])
            if not channel:
                return

            embeds_data = json.loads(entry["embeds_json"])
            components_data = json.loads(entry["components_json"]) if entry.get("components_json") else None
            embeds = [discord.Embed.from_dict(e) for e in embeds_data]
            view = discohook_to_view(components_data)

            sent_message = await channel.send(
                content=entry.get("content", ""), embeds=embeds, view=view,
            )
            message_link = f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{sent_message.id}"

            log_channel = channel.guild.get_channel(EMBED_LOG_CHANNEL_ID)
            user_mention = f"<@{entry['user_id']}>"
            if log_channel:
                await log_channel.send(
                    content=(
                        f"📢 **Scheduled embed sent**\n"
                        f"**ID:** `{entry['identifier']}`\n"
                        f"**User:** {user_mention}\n"
                        f"**Channel:** {channel.mention}\n"
                        f"[Jump to Message]({message_link})"
                    )
                )
        except Exception as e:
            print(f"Failed to send scheduled embed: {e}")
        finally:
            await Database.execute(
                "DELETE FROM scheduled_embeds WHERE identifier = %s", (entry["identifier"],)
            )

    # ── Discohook link parser ──────────────────────────────────

    async def _parse_discohook(self, link: str, interaction: discord.Interaction):
        """Parse a Discohook link and return (content, embeds_data, components_data) or None on error."""
        if not (link.startswith("https://discohook.org/?data=") or link.startswith("https://discohook.app/?data=")):
            await interaction.followup.send("❌ Invalid Discohook link!", ephemeral=True)
            return None
        try:
            parsed_url = urlparse(link)
            query_params = parse_qs(parsed_url.query)
            encoded_json = query_params.get("data", [None])[0]
            if not encoded_json:
                await interaction.followup.send("❌ No valid data found in the link.", ephemeral=True)
                return None
            missing_padding = len(encoded_json) % 4
            if missing_padding:
                encoded_json += "=" * (4 - missing_padding)
            decoded_json = base64.urlsafe_b64decode(encoded_json).decode("utf-8")
            data = json.loads(decoded_json)
            message_data = data["messages"][0]["data"]
            return (
                message_data.get("content", ""),
                message_data.get("embeds", []),
                message_data.get("components", []),
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to parse Discohook link: {e}", ephemeral=True)
            return None

    # ── /send_embed ────────────────────────────────────────────

    @app_commands.command(name="send_embed", description="Send an embed from a Discohook link, optionally scheduled.")
    @app_commands.describe(
        channel="Channel to send the embed to",
        link="Short Discohook link (if under 512 characters)",
        long_link="Alternative: Paste the full Discohook link here if it's too long",
        schedule_for="(Optional) Date and time to send (DD/MM/YYYY HH:MM, UTC+8)",
    )
    async def send_embed(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel,
        link: str | None = None,
        long_link: str | None = None,
        schedule_for: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        final_link = long_link or link
        if not final_link:
            await interaction.followup.send("❌ Please provide a Discohook link.", ephemeral=True)
            return

        result = await self._parse_discohook(final_link, interaction)
        if result is None:
            return
        message_content, embeds_data, components_data = result

        embeds = [discord.Embed.from_dict(e) for e in embeds_data]
        view = discohook_to_view(components_data)

        # Scheduled
        if schedule_for:
            try:
                tz = pytz.timezone("Asia/Manila")
                dt = datetime.strptime(schedule_for, "%d/%m/%Y %H:%M")
                dt = tz.localize(dt)
                now = datetime.now(tz)
                if (dt - now).total_seconds() <= 0:
                    await interaction.followup.send("❌ The scheduled time must be in the future.", ephemeral=True)
                    return
            except Exception:
                await interaction.followup.send("❌ Invalid date format. Use **DD/MM/YYYY HH:MM** (24-hour, UTC+8).", ephemeral=True)
                return

            identifier = generate_identifier()
            await Database.execute(
                "INSERT INTO scheduled_embeds (identifier, guild_id, channel_id, user_id, content, embeds_json, components_json, schedule_for) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    identifier, interaction.guild_id, channel.id, interaction.user.id,
                    message_content, json.dumps(embeds_data), json.dumps(components_data),
                    dt.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

            entry = await Database.fetchone(
                "SELECT * FROM scheduled_embeds WHERE identifier = %s", (identifier,)
            )
            self.scheduled_tasks.append(asyncio.create_task(self._delayed_send(entry)))

            await interaction.followup.send(
                f"⏰ Embed scheduled for {dt.strftime('%d/%m/%Y %H:%M')} UTC+8 in {channel.mention}.\n"
                f"**Identifier:** `{identifier}`",
                ephemeral=True,
            )

            # Log preview
            log_channel = interaction.guild.get_channel(EMBED_LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    content=(
                        f"📝 **Scheduled embed PREVIEW**\n"
                        f"**ID:** `{identifier}`\n"
                        f"**User:** {interaction.user.mention}\n"
                        f"**Channel:** {channel.mention}\n"
                        f"**Scheduled for:** {dt.strftime('%d/%m/%Y %H:%M')} UTC+8\n\n"
                        f"{message_content or ''}"
                    ),
                    embeds=embeds,
                    view=view,
                )
            return

        # Immediate send
        sent_message = await channel.send(content=message_content, embeds=embeds, view=view)
        message_link = f"https://discord.com/channels/{interaction.guild_id}/{channel.id}/{sent_message.id}"
        await interaction.followup.send(
            f"✅ Embed sent to {channel.mention}: [Jump to Message]({message_link})", ephemeral=True,
        )

        log_channel = interaction.guild.get_channel(EMBED_LOG_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(title="📢 Embed Sent", color=discord.Color.gold())
            log_embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
            log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="Channel", value=channel.mention, inline=True)
            log_embed.add_field(name="Link", value=f"[Jump to Message]({message_link})", inline=False)
            await log_channel.send(embed=log_embed)

    # ── /cancel_scheduled_embed ────────────────────────────────

    @app_commands.command(name="cancel_scheduled_embed", description="Cancel a scheduled embed.")
    async def cancel_scheduled_embed(self, interaction: discord.Interaction):
        rows = await Database.fetchall(
            "SELECT identifier, schedule_for FROM scheduled_embeds WHERE guild_id = %s",
            (interaction.guild_id,),
        )
        if not rows:
            await interaction.response.send_message("No scheduled embeds.", ephemeral=True)
            return

        scheduled_list = [
            {"identifier": r["identifier"], "schedule_for": str(r["schedule_for"])}
            for r in rows
        ]
        view = CancelScheduledEmbedView(scheduled_list, self, interaction.user)
        await interaction.response.send_message("Select a scheduled embed to cancel:", view=view, ephemeral=True)

    # ── /edit_embed ────────────────────────────────────────────

    @app_commands.command(name="edit_embed", description="Edit an existing message using a Discohook link.")
    @app_commands.describe(
        message_link="The message link to edit",
        link="Short Discohook link (if under 512 characters)",
        long_link="Alternative: Paste the full Discohook link here if it's too long",
    )
    async def edit_embed(
        self, interaction: discord.Interaction,
        message_link: str,
        link: str | None = None,
        long_link: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        final_link = long_link or link
        if not final_link:
            await interaction.followup.send("❌ No Discohook link provided.", ephemeral=True)
            return

        result = await self._parse_discohook(final_link, interaction)
        if result is None:
            return
        message_content, embeds_data, components_data = result

        try:
            parts = message_link.strip().split("/")
            if len(parts) < 7:
                await interaction.followup.send("❌ Invalid message link format.", ephemeral=True)
                return

            guild_id, channel_id, msg_id = int(parts[-3]), int(parts[-2]), int(parts[-1])
            channel = interaction.guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            target_message = await channel.fetch_message(msg_id)

            new_embeds = [discord.Embed.from_dict(e) for e in embeds_data]
            new_view = discohook_to_view(components_data)

            if target_message.author.id == self.bot.user.id:
                await target_message.edit(content=message_content, embeds=new_embeds, view=new_view)
                await interaction.followup.send(
                    f"✅ Edited: [Jump to Message]({message_link})", ephemeral=True,
                )
                return

            if target_message.webhook_id:
                webhooks = await channel.webhooks()
                webhook = next((w for w in webhooks if w.id == target_message.webhook_id), None)
                if webhook and webhook.token:
                    await webhook.edit_message(
                        message_id=target_message.id, content=message_content, embeds=new_embeds,
                    )
                    await interaction.followup.send(
                        f"✅ Edited webhook message (components not supported): [Jump]({message_link})",
                        ephemeral=True,
                    )
                    return
                else:
                    await interaction.followup.send("❌ Could not find webhook to edit.", ephemeral=True)
                    return

            await interaction.followup.send("❌ I can only edit my own messages or webhook messages.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    # ── /dl_embed ──────────────────────────────────────────────

    @app_commands.command(name="dl_embed", description="Generate a Discohook link from a Discord message.")
    @app_commands.describe(message_link="Link to the Discord message containing the embed.")
    async def dl_embed(self, interaction: discord.Interaction, message_link: str):
        try:
            parts = message_link.strip().split("/")
            if len(parts) < 7:
                await interaction.response.send_message("❌ Invalid message link format.", ephemeral=True)
                return

            guild_id, channel_id, msg_id = int(parts[-3]), int(parts[-2]), int(parts[-1])
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            message = await channel.fetch_message(msg_id)

            if not message.embeds and not message.content and not message.components:
                await interaction.response.send_message("❌ Message has no embeds, content, or components.", ephemeral=True)
                return

            payload = {
                "messages": [{
                    "data": {
                        "content": message.content or "",
                        "embeds": [embed.to_dict() for embed in message.embeds],
                        "components": [c.to_dict() for c in message.components] if message.components else [],
                    },
                    "type": "message",
                }]
            }

            json_string = json.dumps(payload)
            encoded = base64.urlsafe_b64encode(json_string.encode()).decode().rstrip("=")
            discohook_link = f"https://discohook.app/?data={quote(encoded)}"

            if len(discohook_link) > 2000:
                buffer = BytesIO(discohook_link.encode("utf-8"))
                buffer.seek(0)
                await interaction.response.send_message(
                    content="📄 The link is too long. Here's a file:",
                    ephemeral=True,
                    file=discord.File(fp=buffer, filename="discohook_link.txt"),
                )
            else:
                await interaction.response.send_message(
                    f"✅ [Open in Discohook]({discohook_link})", ephemeral=True,
                )

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: `{e}`", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Embeds(bot))
