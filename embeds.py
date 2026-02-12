import discord
from discord.ext import commands
from discord import app_commands
import base64
import json
import os
import random
import string
from urllib.parse import urlparse, parse_qs, quote
from io import BytesIO
from utils.views import CancelScheduledEmbedView
from utils.constants import EMBED_LOG_CHANNEL_ID
from datetime import datetime, timedelta
import pytz
import asyncio

SCHEDULE_FILE = "data/scheduled_embeds.json"

def discohook_to_view(components_data):
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
                        style=discord.ButtonStyle.link,
                        label=label,
                        url=url,
                        emoji=emoji,
                        disabled=disabled
                    )
                else:
                    button = discord.ui.Button(
                        style=discord.ButtonStyle(style),
                        label=label,
                        custom_id=custom_id,
                        emoji=emoji,
                        disabled=disabled
                    )
                view.add_item(button)
            elif t == 3:  # String Select Menu
                options = []
                for opt in comp.get("options", []):
                    options.append(discord.SelectOption(
                        label=opt.get("label"),
                        value=opt.get("value"),
                        description=opt.get("description"),
                        emoji=opt.get("emoji", {}).get("name") if opt.get("emoji") else None,
                        default=opt.get("default", False)
                    ))
                select = discord.ui.Select(
                    custom_id=comp.get("custom_id"),
                    placeholder=comp.get("placeholder"),
                    min_values=comp.get("min_values", 1),
                    max_values=comp.get("max_values", 1),
                    options=options,
                    disabled=comp.get("disabled", False)
                )
                view.add_item(select)
    return view if len(view.children) > 0 else None

def load_scheduled_embeds():
    if not os.path.exists(SCHEDULE_FILE):
        return []
    with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_scheduled_embeds(scheduled):
    os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(scheduled, f, indent=2)

def generate_identifier(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

class Embeds(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.scheduled_tasks = []
        self.bot.loop.create_task(self.load_and_schedule_embeds())

    async def load_and_schedule_embeds(self):
        await self.bot.wait_until_ready()
        scheduled = load_scheduled_embeds()
        now = datetime.now(pytz.timezone("Asia/Manila"))
        for entry in scheduled:
            dt = datetime.strptime(entry["schedule_for"], "%d/%m/%Y %H:%M")
            dt = pytz.timezone("Asia/Manila").localize(dt)
            if dt > now:
                self.scheduled_tasks.append(
                    asyncio.create_task(self.delayed_send(entry))
                )

    async def delayed_send(self, entry):
        try:
            tz = pytz.timezone("Asia/Manila")
            dt = datetime.strptime(entry["schedule_for"], "%d/%m/%Y %H:%M")
            dt = tz.localize(dt)
            now = datetime.now(tz)
            delay = (dt - now).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            channel = self.bot.get_channel(entry["channel_id"])
            if not channel:
                self.remove_scheduled_embed(entry)
                return
            embeds = [discord.Embed.from_dict(e) for e in entry["embeds"]]
            view = discohook_to_view(entry.get("components"))
            sent_message = await channel.send(content=entry["content"], embeds=embeds, view=view)
            message_link = f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{sent_message.id}"
            log_channel = channel.guild.get_channel(EMBED_LOG_CHANNEL_ID)
            user_mention = f"<@{entry['user_id']}>" if "user_id" in entry else "*(unknown)*"
            if log_channel:
                await log_channel.send(
                    content=(
                        f"📢 **Scheduled embed sent**\n"
                        f"**ID:** `{entry['identifier']}`\n"
                        f"**User:** {user_mention}\n"
                        f"**Channel:** {channel.mention}\n"
                        f"**Scheduled for:** {entry['schedule_for']} UTC+8\n"
                        f"[Jump to Message]({message_link})"
                    )
                )
        except Exception as e:
            print(f"Failed to send scheduled embed: {e}")
        finally:
            self.remove_scheduled_embed(entry)

    def add_scheduled_embed(self, entry):
        scheduled = load_scheduled_embeds()
        scheduled.append(entry)
        save_scheduled_embeds(scheduled)

    def remove_scheduled_embed(self, entry):
        scheduled = load_scheduled_embeds()
        scheduled = [
            e for e in scheduled
            if not (
                e["identifier"] == entry["identifier"]
            )
        ]
        save_scheduled_embeds(scheduled)

    async def _process_embed_link(self, link, interaction_or_ctx):
        # Accepts a discohook link or a .txt file containing the link
        if isinstance(link, discord.Attachment):
            if not link.filename.endswith(".txt"):
                await interaction_or_ctx.send("❌ Only `.txt` files are supported for embed links.", ephemeral=True) if hasattr(interaction_or_ctx, "send") else await interaction_or_ctx.reply("❌ Only `.txt` files are supported for embed links.", mention_author=False)
                return None, None, None, None
            file_bytes = await link.read()
            link = file_bytes.decode("utf-8").strip()
        if not (link.startswith("https://discohook.org/?data=") or link.startswith("https://discohook.app/?data=")):
            await interaction_or_ctx.send("❌ Invalid Discohook link! Please use a valid `discohook.org` or `discohook.app` link.", ephemeral=True) if hasattr(interaction_or_ctx, "send") else await interaction_or_ctx.reply("❌ Invalid Discohook link! Please use a valid `discohook.org` or `discohook.app` link.", mention_author=False)
            return None, None, None, None
        try:
            parsed_url = urlparse(link)
            query_params = parse_qs(parsed_url.query)
            encoded_json = query_params.get("data", [None])[0]
            if not encoded_json:
                await interaction_or_ctx.send("❌ No valid data found in the link.", ephemeral=True) if hasattr(interaction_or_ctx, "send") else await interaction_or_ctx.reply("❌ No valid data found in the link.", mention_author=False)
                return None, None, None, None
            missing_padding = len(encoded_json) % 4
            if missing_padding:
                encoded_json += "=" * (4 - missing_padding)
            decoded_json = base64.urlsafe_b64decode(encoded_json).decode("utf-8")
            data = json.loads(decoded_json)
            message_data = data["messages"][0]["data"]
            message_content = message_data.get("content", "")
            embeds_data = message_data.get("embeds", [])
            components_data = message_data.get("components", [])
            return message_content, embeds_data, components_data, link
        except Exception as e:
            await interaction_or_ctx.send(f"❌ Failed to parse Discohook link: {e}", ephemeral=True) if hasattr(interaction_or_ctx, "send") else await interaction_or_ctx.reply(f"❌ Failed to parse Discohook link: {e}", mention_author=False)
            return None, None, None, None

    @app_commands.command(
        name="send_embed",
        description="Send an embed from a Discohook link, optionally scheduled."
    )
    @app_commands.describe(
        channel="Channel to send the embed to",
        link="Short Discohook link (if under 512 characters)",
        long_link="Alternative: Paste the full Discohook link here if it's too long",
        schedule_for="(Optional) Date and time to send (DD/MM/YYYY HH:MM, UTC+8)"
    )
    async def send_embed(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        link: str = None,
        long_link: str = None,
        schedule_for: str = None
    ):
        final_link = long_link if long_link else link
        message_content, embeds_data, components_data, used_link = await self._process_embed_link(final_link, interaction)
        if message_content is None and embeds_data is None:
            return

        embeds = [discord.Embed.from_dict(embed) for embed in embeds_data]
        view = discohook_to_view(components_data)

        if schedule_for:
            try:
                tz = pytz.timezone("Asia/Manila")
                dt = datetime.strptime(schedule_for, "%d/%m/%Y %H:%M")
                dt = tz.localize(dt)
                now = datetime.now(tz)
                delay = (dt - now).total_seconds()
                if delay <= 0:
                    await interaction.response.send_message("❌ The scheduled time must be in the future (UTC+8).", ephemeral=True)
                    return
            except Exception:
                await interaction.response.send_message("❌ Invalid date format. Use **DD/MM/YYYY HH:MM** (24-hour, UTC+8).", ephemeral=True)
                return

            identifier = generate_identifier()
            entry = {
                "identifier": identifier,
                "channel_id": channel.id,
                "content": message_content,
                "embeds": embeds_data,
                "components": components_data,
                "schedule_for": schedule_for,
                "user_id": interaction.user.id
            }
            self.add_scheduled_embed(entry)
            self.scheduled_tasks.append(
                asyncio.create_task(self.delayed_send(entry))
            )

            await interaction.response.send_message(
                f"⏰ Embed scheduled for {dt.strftime('%d/%m/%Y %H:%M')} UTC+8 in {channel.mention}.\n"
                f"**Identifier:** `{identifier}`",
                ephemeral=True
            )
            # Log scheduling (preview: includes content, embeds, and components)
            log_channel = interaction.guild.get_channel(EMBED_LOG_CHANNEL_ID)
            if log_channel:
                embeds_to_log = [discord.Embed.from_dict(embed) for embed in embeds_data]
                view_to_log = discohook_to_view(components_data)
                await log_channel.send(
                    content=(
                        f"📝 **Scheduled embed PREVIEW**\n"
                        f"**ID:** `{identifier}`\n"
                        f"**User:** {interaction.user.mention}\n"
                        f"**Channel:** {channel.mention}\n"
                        f"**Scheduled for:** {dt.strftime('%d/%m/%Y %H:%M')} UTC+8\n\n"
                        f"{message_content if message_content else ''}"
                    ),
                    embeds=embeds_to_log,
                    view=view_to_log
                )
            return

        sent_message = await channel.send(content=message_content, embeds=embeds, view=view)
        message_link = f"https://discord.com/channels/{interaction.guild.id}/{channel.id}/{sent_message.id}"
        await interaction.response.send_message(
            f"✅ Embed sent to {channel.mention}: [Jump to Message]({message_link})",
            ephemeral=True
        )
        log_channel = interaction.guild.get_channel(EMBED_LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(title="📢 Embed Sent", color=discord.Color.gold())
            embed.set_author(name=f"{interaction.user}", icon_url=interaction.user.display_avatar.url)
            embed.add_field(name="User", value=interaction.user.mention, inline=True)
            embed.add_field(name="Channel", value=channel.mention, inline=True)
            embed.add_field(name="Link", value=f"[Jump to Message]({message_link})", inline=False)
            await log_channel.send(embed=embed)

    @commands.command(name="send_embed")
    async def send_embed_prefix(self, ctx, channel: discord.TextChannel = None, *args):
        """
        Usage: ^send_embed <channel> <link/txt file> [schedule_for]
        """
        if channel is None:
            await ctx.reply("❌ Usage: ^send_embed <channel> <link/txt file> [schedule_for]", mention_author=False)
            return

        link = None
        schedule_for = None

        # If there's an attachment, use it as the link
        if ctx.message.attachments:
            link = ctx.message.attachments[0]
            # If there's an argument, treat it as schedule_for
            if args:
                schedule_for = " ".join(args)
        else:
            # If no attachment, use the first arg as link, rest as schedule
            if not args:
                await ctx.reply("❌ Usage: ^send_embed <channel> <link/txt file> [schedule_for]", mention_author=False)
                return
            link = args[0]
            if len(args) > 1:
                schedule_for = " ".join(args[1:])

        message_content, embeds_data, components_data, used_link = await self._process_embed_link(link, ctx)
        if message_content is None and embeds_data is None:
            return

        embeds = [discord.Embed.from_dict(embed) for embed in embeds_data]
        view = discohook_to_view(components_data)

        # Parse schedule_for if provided
        if schedule_for:
            try:
                tz = pytz.timezone("Asia/Manila")
                dt = datetime.strptime(schedule_for, "%d/%m/%Y %H:%M")
                dt = tz.localize(dt)
                now = datetime.now(tz)
                delay = (dt - now).total_seconds()
                if delay <= 0:
                    await ctx.reply("❌ The scheduled time must be in the future (UTC+8).", mention_author=False)
                    return
            except Exception:
                await ctx.reply("❌ Invalid date format. Use **DD/MM/YYYY HH:MM** (24-hour, UTC+8).", mention_author=False)
                return

            identifier = generate_identifier()
            entry = {
                "identifier": identifier,
                "channel_id": channel.id,
                "content": message_content,
                "embeds": embeds_data,
                "components": components_data,
                "schedule_for": schedule_for,
                "user_id": ctx.author.id
            }
            self.add_scheduled_embed(entry)
            self.scheduled_tasks.append(
                asyncio.create_task(self.delayed_send(entry))
            )

            await ctx.reply(
                f"⏰ Embed scheduled for {dt.strftime('%d/%m/%Y %H:%M')} UTC+8 in {channel.mention}.\n"
                f"**Identifier:** `{identifier}`",
                mention_author=False
            )
            # Log scheduling (preview: includes content, embeds, and components)
            log_channel = ctx.guild.get_channel(EMBED_LOG_CHANNEL_ID)
            if log_channel:
                embeds_to_log = [discord.Embed.from_dict(embed) for embed in embeds_data]
                view_to_log = discohook_to_view(components_data)
                await log_channel.send(
                    content=(
                        f"📝 **Scheduled embed PREVIEW**\n"
                        f"**ID:** `{identifier}`\n"
                        f"**User:** {ctx.author.mention}\n"
                        f"**Channel:** {channel.mention}\n"
                        f"**Scheduled for:** {dt.strftime('%d/%m/%Y %H:%M')} UTC+8\n\n"
                        f"{message_content if message_content else ''}"
                    ),
                    embeds=embeds_to_log,
                    view=view_to_log
                )
            return

        sent_message = await channel.send(content=message_content, embeds=embeds, view=view)
        message_link = f"https://discord.com/channels/{ctx.guild.id}/{channel.id}/{sent_message.id}"
        await ctx.reply(
            f"✅ Embed sent to {channel.mention}: [Jump to Message]({message_link})",
            mention_author=False
        )
        log_channel = ctx.guild.get_channel(EMBED_LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(
                content=(
                    f"📢 **Embed sent**\n"
                    f"**User:** {ctx.author.mention}\n"
                    f"**Channel:** {channel.mention}\n"
                    f"[Jump to Message]({message_link})"
                )
            )
    @app_commands.command(name="cancel_scheduled_embed", description="Cancel a scheduled embed.")
    async def cancel_scheduled_embed(self, interaction: discord.Interaction):
        scheduled = load_scheduled_embeds()
        if not scheduled:
            await interaction.response.send_message("There are no scheduled embeds.", ephemeral=True)
            return
        view = CancelScheduledEmbedView(scheduled, self, interaction.user)
        await interaction.response.send_message("Select a scheduled embed to cancel:", view=view, ephemeral=True)

    @app_commands.command(name="edit_embed", description="Edit an existing message (bot or webhook) using a Discohook link.")
    @app_commands.describe(
        message_link="The message link to edit (must be sent by the bot or a webhook)",
        link="Short Discohook link (if under 512 characters)",
        long_link="Alternative: Paste the full Discohook link here if it's too long"
    )
    async def edit_embed(
        self,
        interaction: discord.Interaction,
        message_link: str,
        link: str = None,
        long_link: str = None
    ):
        await self._edit_embed_common(
            interaction_or_ctx=interaction,
            message_link=message_link,
            link=long_link if long_link else link,
            is_slash=True
        )

    @commands.command(name="edit_embed")
    async def edit_embed_prefix(self, ctx, message_link: str = None, link_or_file = None):
        """
        Usage: ^edit_embed <message_link> <link/txt file>
        """
        if not message_link:
            await ctx.reply("❌ Usage: ^edit_embed <message_link> <link/txt file>", mention_author=False)
            return

        # If link_or_file is not provided, but there's an attachment, use it
        link = None
        if link_or_file is None and ctx.message.attachments:
            link = ctx.message.attachments[0]
        elif isinstance(link_or_file, discord.Attachment):
            link = link_or_file
        elif link_or_file is not None:
            link = link_or_file
        else:
            await ctx.reply("❌ Usage: ^edit_embed <message_link> <link/txt file>", mention_author=False)
            return

        await self._edit_embed_common(
            interaction_or_ctx=ctx,
            message_link=message_link,
            link=link,
            is_slash=False
        )

    async def _edit_embed_common(self, interaction_or_ctx, message_link, link, is_slash):
        # Accepts a discohook link or a .txt file containing the link
        ephemeral = True if is_slash else False
        send_func = (
            interaction_or_ctx.followup.send if is_slash and hasattr(interaction_or_ctx, "followup")
            else interaction_or_ctx.send if is_slash
            else interaction_or_ctx.reply
        )

        if not link:
            await send_func("❌ No Discohook link or file provided.", ephemeral=ephemeral, mention_author=False if not is_slash else None)
            return

        message_content, embeds_data, components_data, used_link = await self._process_embed_link(link, interaction_or_ctx)
        if message_content is None and embeds_data is None:
            return

        try:
            parts = message_link.strip().split("/")
            if len(parts) < 7:
                await send_func("❌ Invalid message link format.", ephemeral=ephemeral, mention_author=False if not is_slash else None)
                return

            guild_id, channel_id, message_id = int(parts[-3]), int(parts[-2]), int(parts[-1])
            channel = interaction_or_ctx.guild.get_channel(channel_id) if hasattr(interaction_or_ctx, "guild") and interaction_or_ctx.guild else None
            if not channel:
                channel = interaction_or_ctx.bot.get_channel(channel_id) if hasattr(interaction_or_ctx, "bot") else None
            if not channel:
                try:
                    channel = await interaction_or_ctx.client.fetch_channel(channel_id)
                except Exception:
                    await send_func("❌ Could not fetch the channel.", ephemeral=ephemeral, mention_author=False if not is_slash else None)
                    return
            target_message = await channel.fetch_message(message_id)

            new_embeds = [discord.Embed.from_dict(e) for e in embeds_data]
            new_view = discohook_to_view(components_data)

            if target_message.author.id == (interaction_or_ctx.client.user.id if hasattr(interaction_or_ctx, "client") else interaction_or_ctx.bot.user.id):
                await target_message.edit(content=message_content, embeds=new_embeds, view=new_view)
                await send_func(f"✅ Successfully edited the message: [Jump to Message]({message_link})", ephemeral=ephemeral, mention_author=False if not is_slash else None)
                return

            if target_message.webhook_id:
                webhooks = await channel.webhooks()
                webhook = next((w for w in webhooks if w.id == target_message.webhook_id), None)
                if webhook and webhook.token:
                    await send_func(
                        "⚠️ Editing components on webhook messages is not supported. Only content and embeds will be updated.",
                        ephemeral=ephemeral, mention_author=False if not is_slash else None
                    )
                    await webhook.edit_message(
                        message_id=target_message.id,
                        content=message_content,
                        embeds=new_embeds
                    )
                    await send_func(f"✅ Successfully edited the webhook message (without components): [Jump to Message]({message_link})", ephemeral=ephemeral, mention_author=False if not is_slash else None)
                    return
                else:
                    await send_func("❌ Could not find the webhook or missing token to edit this message.", ephemeral=ephemeral, mention_author=False if not is_slash else None)
                    return

            await send_func("❌ I can only edit messages sent by myself or by a webhook I can access.", ephemeral=ephemeral, mention_author=False if not is_slash else None)

        except Exception as e:
            await send_func(f"❌ Error: `{e}`", ephemeral=ephemeral, mention_author=False if not is_slash else None)

    @app_commands.command(name="dl_embed", description="Generate a Discohook link from a Discord message.")
    @app_commands.describe(
        message_link="Link to the Discord message containing the embed."
    )
    async def dl_embed(
        self,
        interaction: discord.Interaction,
        message_link: str
    ):
        try:
            parts = message_link.strip().split("/")
            if len(parts) < 7:
                await interaction.response.send_message("❌ Invalid message link format.", ephemeral=True)
                return

            guild_id, channel_id, message_id = int(parts[-3]), int(parts[-2]), int(parts[-1])
            channel = interaction.client.get_channel(channel_id) or await interaction.client.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)

            if not message.embeds and not message.content and not message.components:
                await interaction.response.send_message("❌ Message has no embeds, content, or components to export.", ephemeral=True)
                return

            payload = {
                "messages": [
                    {
                        "data": {
                            "content": message.content or "",
                            "embeds": [embed.to_dict() for embed in message.embeds],
                            "components": [c.to_dict() for c in message.components] if message.components else []
                        },
                        "type": "message"
                    }
                ]
            }

            json_string = json.dumps(payload)
            encoded = base64.urlsafe_b64encode(json_string.encode()).decode().rstrip("=")
            discohook_link = f"https://discohook.app/?data={quote(encoded)}"

            if len(discohook_link) > 2000:
                buffer = BytesIO(discohook_link.encode("utf-8"))
                buffer.seek(0)
                await interaction.response.send_message(
                    content="📄 The generated Discohook link is too long to display here. Here's the link in a file:",
                    ephemeral=True,
                    file=discord.File(fp=buffer, filename="discohook_link.txt")
                )
            else:
                await interaction.response.send_message(
                    f"✅ Discohook link created: [Click here to open in Discohook]({discohook_link})",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: `{e}`", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Embeds(bot))