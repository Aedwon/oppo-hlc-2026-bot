"""
Cog: Tickets
Ported from reference tickets.py — MySQL-backed, categories: League Ops, Technical, Creatives, General.
Includes: claim restrictions, HTML transcripts, close flow, rating system, 24h/48h escalation.
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import io
import html as html_mod
import re
import json

from db.database import Database
from utils.constants import (
    TICKET_CATEGORIES,
    TICKET_LOG_CHANNEL_ID,
    SUPPORT_ROLE_ID,
    ROLE_LEAGUE_OPS,
    TZ_MANILA,
)

# ────────────────────────────────────────────────────────────────
# HTML Transcript Generator  (kept from reference)
# ────────────────────────────────────────────────────────────────

def generate_html_transcript(messages: list[discord.Message], channel_name: str) -> str:
    style = """
    <style>
        body { font-family: 'gg sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #313338; color: #dbdee1; margin: 0; padding: 20px; }
        .header { border-bottom: 1px solid #3f4147; padding-bottom: 10px; margin-bottom: 20px; }
        .header h1 { color: #f2f3f5; margin: 0; font-size: 20px; }
        .chat-container { display: block; width: 100%; }
        .message-group { display: flex; margin-bottom: 16px; align-items: flex-start; width: 100%; }
        .avatar { width: 40px; height: 40px; border-radius: 50%; margin-right: 16px; flex-shrink: 0; background-color: #2b2d31; }
        .content { flex: 1; }
        .meta { display: flex; align-items: baseline; margin-bottom: 4px; }
        .username { font-weight: 500; color: #f2f3f5; margin-right: 8px; font-size: 16px; }
        .bot-tag { background-color: #5865f2; color: #fff; font-size: 10px; padding: 1px 4px; border-radius: 3px; vertical-align: middle; margin-left: 4px; }
        .timestamp { font-size: 12px; color: #949ba4; }
        .text { font-size: 16px; line-height: 1.375rem; white-space: pre-wrap; word-wrap: break-word; color: #dbdee1; }
        .text strong { font-weight: 700; color: #f2f3f5; }
        .text em { font-style: italic; }
        .text u { text-decoration: underline; }
        .text s { text-decoration: line-through; }
        .text .mention { background-color: #3c4270; color: #c9cdfb; padding: 0 2px; border-radius: 3px; cursor: pointer; font-weight: 500;}
        .attachment { margin-top: 8px; }
        .attachment img { max-width: 400px; max-height: 300px; border-radius: 4px; }
        a { color: #00a8fc; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .embed { display: flex; max-width: 520px; background-color: #2b2d31; border-radius: 4px; border-left: 4px solid #202225; margin-top: 8px; font-size: 14px; }
        .embed-content { padding: 12px 16px; width: 100%; }
        .embed-author { display: flex; align-items: center; margin-bottom: 8px; font-weight: 600; color: #f2f3f5; font-size: 14px; }
        .embed-author img { width: 24px; height: 24px; border-radius: 50%; margin-right: 8px; }
        .embed-title { color: #f2f3f5; margin: 0 0 8px 0; font-weight: 600; font-size: 16px; }
        .embed-desc { color: #dbdee1; line-height: 1.375rem; white-space: pre-wrap; margin-bottom: 8px; }
        .embed-fields { display: grid; grid-template-columns: auto auto; grid-gap: 8px; margin-top: 8px; }
        .embed-field { min-width: 0; }
        .embed-field-inline { display: inline-block; flex: 1; }
        .embed-field-name { color: #f2f3f5; font-weight: 600; margin-bottom: 2px; }
        .embed-field-value { color: #dbdee1; white-space: pre-wrap; line-height: 1rem; }
        .embed-footer { margin-top: 8px; font-size: 12px; color: #949ba4; display: flex; align-items: center; }
        .embed-footer img { width: 20px; height: 20px; border-radius: 50%; margin-right: 8px; }
    </style>
    """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Transcript - {channel_name}</title>
        {style}
    </head>
     <body>
         <div class="header">
             <h1>#{channel_name}</h1>
             <p>Transcript generated on {datetime.datetime.now(TZ_MANILA).strftime('%Y-%m-%d %H:%M:%S')} (PHT)</p>
         </div>
         <div class="chat-container">
     """

    guild = messages[0].guild if messages else None

    for msg in messages:
        try:
            avatar_url = msg.author.display_avatar.url if msg.author.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"
            username = html_mod.escape(msg.author.display_name)
            try:
                created_at_pht = msg.created_at.astimezone(TZ_MANILA)
            except Exception:
                created_at_pht = msg.created_at
            timestamp = created_at_pht.strftime('%m/%d/%Y %I:%M %p')

            content = html_mod.escape(msg.content or "")

            def replace_user(match):
                uid = int(match.group(1))
                name = f"@{uid}"
                if guild:
                    member = guild.get_member(uid)
                    if member:
                        name = f"@{member.display_name}"
                return f'<span class="mention">{html_mod.escape(name)}</span>'
            content = re.sub(r'&lt;@!?(\d+)&gt;', replace_user, content)

            def replace_role(match):
                rid = int(match.group(1))
                name = f"@{rid}"
                if guild:
                    role = guild.get_role(rid)
                    if role:
                        name = f"@{role.name}"
                return f'<span class="mention">{html_mod.escape(name)}</span>'
            content = re.sub(r'&lt;@&amp;(\d+)&gt;', replace_role, content)

            def replace_channel(match):
                cid = int(match.group(1))
                name = f"#{cid}"
                if guild:
                    chan = guild.get_channel(cid)
                    if chan:
                        name = f"#{chan.name}"
                return f'<span class="mention">{html_mod.escape(name)}</span>'
            content = re.sub(r'&lt;#(\d+)&gt;', replace_channel, content)

            content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', content)
            content = re.sub(r'\*(.*?)\*', r'<em>\1</em>', content)
            content = re.sub(r'__(.*?)__', r'<u>\1</u>', content)
            content = re.sub(r'~~(.*?)~~', r'<s>\1</s>', content)
            content = content.replace("@everyone", '<span class="mention">@everyone</span>')
            content = content.replace("@here", '<span class="mention">@here</span>')

            bot_tag = '<span class="bot-tag">BOT</span>' if msg.author.bot else ''

            attachments_html = ""
            if msg.attachments:
                for att in msg.attachments:
                    if att.content_type and att.content_type.startswith('image/'):
                        attachments_html += f'<div class="attachment"><a href="{att.url}" target="_blank"><img src="{att.url}" alt="Attachment"></a></div>'
                    else:
                        attachments_html += f'<div class="attachment"><a href="{att.url}" target="_blank">📄 {html_mod.escape(att.filename)}</a></div>'

            embeds_html = ""
            if msg.embeds:
                for embed in msg.embeds:
                    color = f"#{embed.color.value:06x}" if embed.color else "#202225"
                    border_style = f"border-left-color: {color};"
                    author_html = ""
                    if embed.author:
                        icon = f'<img src="{embed.author.icon_url}">' if embed.author.icon_url else ""
                        author_html = f'<div class="embed-author">{icon} {html_mod.escape(embed.author.name or "")}</div>'
                    title_html = f'<div class="embed-title">{html_mod.escape(embed.title)}</div>' if embed.title else ""
                    desc_html = f'<div class="embed-desc">{html_mod.escape(embed.description)}</div>' if embed.description else ""
                    fields_html = '<div class="embed-fields">'
                    for field in embed.fields:
                        inline = "embed-field-inline" if field.inline else "embed-field"
                        fields_html += f'<div class="{inline}"><div class="embed-field-name">{html_mod.escape(field.name)}</div><div class="embed-field-value">{html_mod.escape(field.value)}</div></div>'
                    fields_html += '</div>' if embed.fields else ""
                    footer_text = ""
                    footer_icon = ""
                    if embed.footer:
                        footer_text = html_mod.escape(embed.footer.text or "")
                        if embed.footer.icon_url:
                            footer_icon = f'<img src="{embed.footer.icon_url}">'
                    ts_html = ""
                    if embed.timestamp:
                        try:
                            ts_pht = embed.timestamp.astimezone(TZ_MANILA)
                            ts_html = f" • {ts_pht.strftime('%m/%d/%Y %I:%M %p')}"
                        except:
                            pass
                    footer_html = ""
                    if footer_text or ts_html:
                        footer_html = f'<div class="embed-footer">{footer_icon}{footer_text}{ts_html}</div>'
                    embeds_html += f'<div class="embed" style="{border_style}"><div class="embed-content">{author_html}{title_html}{desc_html}{fields_html}{footer_html}</div></div>'

            html_content += f"""
            <div class="message-group">
                <img class="avatar" src="{avatar_url}" alt="{username}">
                <div class="content">
                    <div class="meta">
                        <span class="username">{username}</span>
                        {bot_tag}
                        <span class="timestamp">{timestamp}</span>
                    </div>
                    <div class="text">{content}</div>
                    {attachments_html}
                    {embeds_html}
                </div>
            </div>
            """
        except Exception as e:
            print(f"Error processing message {msg.id}: {e}")
            continue

    html_content += """
        </div>
    </body>
    </html>
    """
    return html_content


# ────────────────────────────────────────────────────────────────
# UI Components
# ────────────────────────────────────────────────────────────────

class TicketTopicSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for key, data in TICKET_CATEGORIES.items():
            options.append(discord.SelectOption(
                label=data["label"], description=data["desc"],
                emoji=data["emoji"], value=key,
            ))
        super().__init__(
            placeholder="Select the category of your concern...",
            min_values=1, max_values=1,
            custom_id="ticket_category_select", options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_key = self.values[0]
        category_data = TICKET_CATEGORIES.get(selected_key)
        await interaction.response.send_modal(TicketModal(category_key=selected_key, category_data=category_data))


class TicketTopicView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(TicketTopicSelect())


class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📩 Create Ticket", style=discord.ButtonStyle.primary, custom_id="create_ticket_base")
    async def create_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Please select the category below:", view=TicketTopicView(), ephemeral=True
        )


class TicketModal(discord.ui.Modal):
    def __init__(self, category_key, category_data):
        super().__init__(title=f"New {category_data['label']} Ticket")
        self.category_key = category_key
        self.category_data = category_data

        self.ticket_subject = discord.ui.TextInput(
            label="Subject", placeholder="Briefly state your concern...", max_length=100,
        )
        self.ticket_desc = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph,
            placeholder="Please provide more details...", max_length=1000,
        )
        self.add_item(self.ticket_subject)
        self.add_item(self.ticket_desc)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        category_channel = interaction.channel.category

        if not category_channel:
            category_channel = discord.utils.get(guild.categories, name="🎟⎮tickets")
        if not category_channel:
            await interaction.followup.send("❌ Error: Could not determine category.", ephemeral=True)
            return

        tag = self.category_data["tag"]
        channel_name = f"[{tag}]-{user.name}"

        existing = discord.utils.get(guild.text_channels, name=channel_name)
        if existing:
            await interaction.followup.send(f"❌ You already have a ticket of this type open: {existing.mention}", ephemeral=True)
            return

        role_to_ping_id = self.category_data["role_id"]
        role_to_ping = guild.get_role(role_to_ping_id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
        }
        if role_to_ping:
            overwrites[role_to_ping] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        support_role = guild.get_role(SUPPORT_ROLE_ID)
        if support_role and support_role != role_to_ping:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        try:
            ticket_channel = await guild.create_text_channel(channel_name, category=category_channel, overwrites=overwrites)
        except discord.Forbidden:
            await interaction.followup.send("❌ Error: I do not have permission to create channels in this category.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Discord API Error: {e}", ephemeral=True)
            return

        # Save to DB
        await Database.execute(
            "INSERT INTO active_tickets (channel_id, guild_id, creator_id, category_key, subject) "
            "VALUES (%s, %s, %s, %s, %s)",
            (ticket_channel.id, guild.id, user.id, self.category_key, self.ticket_subject.value),
        )

        embed = discord.Embed(
            title=f"{self.category_data['emoji']} {self.category_data['label']}",
            description=f"**Subject:** {self.ticket_subject.value}\n\n{self.ticket_desc.value}",
            color=0xF2C21A,
        )
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)


        view = TicketActionsView(creator=user)
        mention_text = role_to_ping.mention if role_to_ping else ""
        try:
            await ticket_channel.send(content=f"{user.mention} {mention_text}", embed=embed, view=view)
        except Exception:
            pass

        await interaction.followup.send(f"✅ Ticket created: {ticket_channel.mention}", ephemeral=True)


class TicketActionsView(discord.ui.View):
    def __init__(self, creator: discord.User | None = None):
        super().__init__(timeout=None)
        self.creator = creator
        self.claimed_by = None

    @discord.ui.button(label="🔄 Move Category", style=discord.ButtonStyle.secondary, custom_id="move_category_ticket")
    async def move_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Select the new category:", view=MoveCategoryView(), ephemeral=True)

    @discord.ui.button(label="🛠 Claim Ticket", style=discord.ButtonStyle.success, custom_id="claim_ticket")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cid = interaction.channel_id

        ticket_data = await Database.fetchone(
            "SELECT * FROM active_tickets WHERE channel_id = %s", (cid,)
        )
        if not ticket_data:
            await interaction.followup.send("❌ Ticket data not found.", ephemeral=True)
            return

        if ticket_data["claimed"]:
            await interaction.followup.send("❌ This ticket is already claimed.", ephemeral=True)
            button.disabled = True
            button.label = "Already Claimed"
            await interaction.message.edit(view=self)
            return

        user = interaction.user

        # Block creator
        if user.id == ticket_data["creator_id"]:
            await interaction.followup.send("❌ You cannot claim your own ticket.", ephemeral=True)
            return

        # Block added users
        added_users = json.loads(ticket_data["added_users"]) if ticket_data.get("added_users") else []
        if user.id in added_users:
            await interaction.followup.send("❌ Added users cannot claim the ticket.", ephemeral=True)
            return

        # Role validation
        allowed = False
        if user.guild_permissions.administrator:
            allowed = True
        else:
            cat_key = ticket_data["category_key"]
            cat_data = TICKET_CATEGORIES.get(cat_key)
            cat_role_id = cat_data["role_id"] if cat_data else None
            cat_role = interaction.guild.get_role(cat_role_id)
            if cat_role and cat_role in user.roles:
                allowed = True
            if ticket_data.get("escalated_48h"):
                league_ops_role = interaction.guild.get_role(ROLE_LEAGUE_OPS)
                if league_ops_role and league_ops_role in user.roles:
                    allowed = True

        if not allowed:
            await interaction.followup.send("❌ You do not have permission to claim this ticket.", ephemeral=True)
            return

        await Database.execute(
            "UPDATE active_tickets SET claimed = TRUE, claimed_by = %s WHERE channel_id = %s",
            (user.id, cid),
        )
        self.claimed_by = user

        button.disabled = True
        button.label = f"Claimed by {user.display_name}"
        try:
            await interaction.message.edit(view=self)
            embed = discord.Embed(description=f"✅ **Ticket claimed by {user.mention}**", color=0x00FF00)
    
            await interaction.channel.send(content=user.mention, embed=embed)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Error updating UI: {e}", ephemeral=True)

    @discord.ui.button(label="👥 Add User", style=discord.ButtonStyle.secondary, custom_id="add_user_ticket")
    async def add_user_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Select users to add:", view=AddUserView(), ephemeral=True)

    @discord.ui.button(label="🚫 Remove User", style=discord.ButtonStyle.secondary, custom_id="remove_user_ticket")
    async def remove_user_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket_data = await Database.fetchone(
            "SELECT added_users FROM active_tickets WHERE channel_id = %s", (interaction.channel_id,)
        )
        added_ids = json.loads(ticket_data["added_users"]) if ticket_data and ticket_data.get("added_users") else []
        if not added_ids:
            await interaction.response.send_message("❌ No users have been added to this ticket.", ephemeral=True)
            return
        await interaction.response.send_message("Select users to remove:", view=RemoveUserView(added_ids), ephemeral=True)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Please select a reason for closing this ticket:", view=CloseReasonView(self), ephemeral=True
        )


# ── Add / Remove user views ────────────────────────────────────

class AddUserView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select users to add...", min_values=1, max_values=5)
    async def select_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        if not isinstance(interaction.channel, discord.TextChannel):
            return
        await interaction.response.defer()
        cid = interaction.channel_id

        ticket_data = await Database.fetchone(
            "SELECT added_users FROM active_tickets WHERE channel_id = %s", (cid,)
        )
        current_added = json.loads(ticket_data["added_users"]) if ticket_data and ticket_data.get("added_users") else []

        added_mentions = []
        for user in select.values:
            if user.bot:
                continue
            if user.id not in current_added:
                current_added.append(user.id)
            try:
                await interaction.channel.set_permissions(user, view_channel=True, send_messages=True)
                added_mentions.append(user.mention)
            except Exception:
                pass

        await Database.execute(
            "UPDATE active_tickets SET added_users = %s WHERE channel_id = %s",
            (json.dumps(current_added), cid),
        )

        if added_mentions:
            await interaction.followup.send("✅ Users added.")
            await interaction.channel.send(f"👥 **{', '.join(added_mentions)}** have been added to the ticket.")
        else:
            await interaction.followup.send("No valid users selected.", ephemeral=True)
        self.stop()


class RemoveUserView(discord.ui.View):
    def __init__(self, allowed_ids):
        super().__init__(timeout=60)
        self.allowed_ids = allowed_ids

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select users to remove...", min_values=1, max_values=5)
    async def select_remove(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer()
        cid = interaction.channel_id

        ticket_data = await Database.fetchone(
            "SELECT added_users FROM active_tickets WHERE channel_id = %s", (cid,)
        )
        current_added = json.loads(ticket_data["added_users"]) if ticket_data and ticket_data.get("added_users") else []

        removed_names = []
        for user in select.values:
            if user.id in self.allowed_ids and user.id in current_added:
                try:
                    await interaction.channel.set_permissions(user, overwrite=None)
                    current_added.remove(user.id)
                    removed_names.append(user.display_name)
                except Exception:
                    pass

        await Database.execute(
            "UPDATE active_tickets SET added_users = %s WHERE channel_id = %s",
            (json.dumps(current_added), cid),
        )

        if removed_names:
            await interaction.followup.send(f"🚫 Removed users: {', '.join(removed_names)}")
        else:
            await interaction.followup.send("❌ Selected user was not in the 'Added Users' list.", ephemeral=True)
        self.stop()


# ── Move category ──────────────────────────────────────────────

class MoveCategorySelect(discord.ui.Select):
    def __init__(self):
        options = []
        for key, data in TICKET_CATEGORIES.items():
            options.append(discord.SelectOption(label=data["label"], emoji=data["emoji"], value=key))
        super().__init__(placeholder="Select new category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        new_key = self.values[0]
        new_cat_data = TICKET_CATEGORIES.get(new_key)
        cid = interaction.channel_id

        ticket_data = await Database.fetchone(
            "SELECT * FROM active_tickets WHERE channel_id = %s", (cid,)
        )
        old_cat_key = ticket_data["category_key"] if ticket_data else None

        # Determine creator name for rename
        creator_name = "unknown"
        if ticket_data and ticket_data.get("creator_id"):
            mem = interaction.guild.get_member(ticket_data["creator_id"])
            if mem:
                creator_name = mem.name
            else:
                match = re.search(r"-\s*(.*)$", interaction.channel.name)
                if match:
                    creator_name = match.group(1)
        else:
            parts = interaction.channel.name.split("-", 1)
            if len(parts) > 1:
                creator_name = parts[1]

        overwrites = interaction.channel.overwrites
        guild = interaction.guild

        new_role = guild.get_role(new_cat_data["role_id"])
        if new_role:
            overwrites[new_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        if old_cat_key:
            old_cat_data = TICKET_CATEGORIES.get(old_cat_key)
            if old_cat_data and old_cat_data["role_id"] != new_cat_data["role_id"]:
                old_role = guild.get_role(old_cat_data["role_id"])
                if old_role and old_role != guild.get_role(SUPPORT_ROLE_ID):
                    overwrites.pop(old_role, None)

        new_channel_name = f"[{new_cat_data['tag']}]-{creator_name}"
        msg = f"✅ Ticket moved to **{new_cat_data['label']}**.\n"

        try:
            await interaction.channel.edit(name=new_channel_name, overwrites=overwrites)
        except Exception as e:
            if "429" in str(e):
                msg += "\n⚠️ Channel rename skipped due to Discord rate limits. Try again later."
                await interaction.followup.send(msg)
                return
            else:
                msg += f"\n⚠️ Failed to update channel: {e}"

        if ticket_data:
            await Database.execute(
                "UPDATE active_tickets SET category_key = %s WHERE channel_id = %s",
                (new_key, cid),
            )

        msg += f"Pinged: {new_role.mention if new_role else 'None'}"
        await interaction.followup.send(msg)


class MoveCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(MoveCategorySelect())


# ── Rating system ──────────────────────────────────────────────

class FeedbackModal(discord.ui.Modal):
    def __init__(self, stars, view_ref):
        super().__init__(title=f"You rated {stars} Stars!")
        self.stars = stars
        self.view_ref = view_ref
        self.remarks = discord.ui.TextInput(
            label="Any comments? (Optional)", style=discord.TextStyle.paragraph,
            placeholder="Let us know how we can improve...", required=False, max_length=1000,
        )
        self.add_item(self.remarks)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=f"✅ Thank you for your feedback! You rated us **{self.stars}/5** ⭐",
            view=None, embed=None,
        )
        if self.view_ref.is_test:
            return

        log_channel = self.view_ref.bot.get_channel(self.view_ref.log_channel_id)
        if log_channel:
            embed = discord.Embed(title="🌟 New Feedback Received", color=0xFFD700, timestamp=datetime.datetime.now(TZ_MANILA))
            embed.add_field(name="User", value=interaction.user.mention, inline=True)
            embed.add_field(name="Ticket", value=self.view_ref.ticket_name, inline=True)
            embed.add_field(name="Handler", value=self.view_ref.handler_mention, inline=True)
            embed.add_field(name="Rating", value=f"{'⭐' * self.stars} ({self.stars}/5)", inline=False)
            if self.remarks.value:
                embed.add_field(name="Remarks", value=self.remarks.value, inline=False)
    
            try:
                await log_channel.send(embed=embed)
            except Exception:
                pass

        await Database.execute(
            "INSERT INTO ticket_ratings (guild_id, ticket_name, user_id, user_name, handler_id, stars, remarks) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                self.view_ref.guild_id,
                self.view_ref.ticket_name,
                interaction.user.id,
                interaction.user.name,
                self.view_ref.handler_id,
                self.stars,
                self.remarks.value,
            ),
        )


class RatingView(discord.ui.View):
    def __init__(self, bot, log_channel_id, ticket_name, handler_mention, handler_id=None, guild_id=None, is_test=False):
        super().__init__(timeout=None)
        self.bot = bot
        self.log_channel_id = log_channel_id
        self.ticket_name = ticket_name
        self.handler_mention = handler_mention
        self.handler_id = handler_id
        self.guild_id = guild_id
        self.is_test = is_test

    async def prompt_feedback(self, interaction: discord.Interaction, stars: int):
        await interaction.response.send_modal(FeedbackModal(stars, self))

    @discord.ui.button(label="1", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="rate_1")
    async def rate_1(self, interaction, button): await self.prompt_feedback(interaction, 1)
    @discord.ui.button(label="2", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="rate_2")
    async def rate_2(self, interaction, button): await self.prompt_feedback(interaction, 2)
    @discord.ui.button(label="3", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="rate_3")
    async def rate_3(self, interaction, button): await self.prompt_feedback(interaction, 3)
    @discord.ui.button(label="4", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="rate_4")
    async def rate_4(self, interaction, button): await self.prompt_feedback(interaction, 4)
    @discord.ui.button(label="5", emoji="⭐", style=discord.ButtonStyle.success, custom_id="rate_5")
    async def rate_5(self, interaction, button): await self.prompt_feedback(interaction, 5)


# ── Close reason logic ─────────────────────────────────────────

class CloseReasonModal(discord.ui.Modal):
    def __init__(self, reason_selected, view_ref):
        super().__init__(title=f"Closing: {reason_selected}")
        self.reason_selected = reason_selected
        self.view_ref = view_ref
        self.remarks = discord.ui.TextInput(
            label="Additional Remarks (Optional)", style=discord.TextStyle.paragraph,
            placeholder="Any specific details? (Leave blank if none)", required=False, max_length=500,
        )
        self.add_item(self.remarks)

    async def on_submit(self, interaction: discord.Interaction):
        await finish_closure(interaction, self.reason_selected, self.remarks.value, self.view_ref)


class CloseReasonSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Solved / Addressed", emoji="✅", value="Solved"),
            discord.SelectOption(label="Assistance Provided", emoji="🤝", value="Assistance Provided"),
            discord.SelectOption(label="Duplicate Ticket", emoji="📄", value="Duplicate"),
            discord.SelectOption(label="Invalid / Spam", emoji="🚫", value="Invalid"),
            discord.SelectOption(label="Inactivity", emoji="💤", value="Inactivity"),
            discord.SelectOption(label="Other", emoji="🔌", value="Other"),
        ]
        super().__init__(placeholder="Select a reason for closing...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        reason = self.values[0]
        await interaction.response.send_modal(CloseReasonModal(reason, self.view.origin_view))


class CloseReasonView(discord.ui.View):
    def __init__(self, origin_view):
        super().__init__(timeout=60)
        self.origin_view = origin_view
        self.add_item(CloseReasonSelect())


async def finish_closure(interaction: discord.Interaction, reason: str, remarks: str, origin_view):
    await interaction.response.defer()
    cid = interaction.channel_id

    ticket_data = await Database.fetchone(
        "SELECT * FROM active_tickets WHERE channel_id = %s", (cid,)
    )
    if not ticket_data:
        await interaction.followup.send("❌ Ticket appears to be already closed.", ephemeral=True)
        return

    creator_id = ticket_data.get("creator_id")
    added_users_ids = json.loads(ticket_data["added_users"]) if ticket_data.get("added_users") else []

    # Remove from DB
    await Database.execute("DELETE FROM active_tickets WHERE channel_id = %s", (cid,))

    # Generate transcript
    messages = [message async for message in interaction.channel.history(limit=500, oldest_first=True)]
    html_content = generate_html_transcript(messages, interaction.channel.name)

    file = discord.File(io.StringIO(html_content), filename=f"transcript-{interaction.channel.name}.html")

    # Log channel
    log_channel = interaction.guild.get_channel(TICKET_LOG_CHANNEL_ID)
    embed = discord.Embed(title="Ticket Closed", color=0xFF0000, timestamp=datetime.datetime.now(TZ_MANILA))
    embed.add_field(name="Ticket", value=interaction.channel.name, inline=True)
    embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
    embed.add_field(name="Reason", value=reason, inline=True)
    if remarks:
        embed.add_field(name="Remarks", value=remarks, inline=False)

    # Always fetch creator from DB (survives restarts)
    creator = None
    if creator_id:
        try:
            creator = interaction.guild.get_member(creator_id) or await interaction.client.fetch_user(creator_id)
        except Exception:
            creator = None

    if creator:
        embed.add_field(name="Creator", value=creator.mention, inline=True)

    if log_channel:
        try:
            await log_channel.send(embed=embed, file=file)
        except Exception:
            pass

    # DM creator with transcript + rating
    if creator:
        try:
            dm_embed = discord.Embed(
                title="Ticket Closed",
                description=f"Your ticket `{interaction.channel.name}` has been closed.",
                color=0xF2C21A,
                timestamp=datetime.datetime.now(TZ_MANILA),
            )
            dm_embed.add_field(name="Reason", value=reason)
            if remarks:
                dm_embed.add_field(name="Remarks", value=remarks)


            f_creator = discord.File(io.StringIO(html_content), filename=f"transcript-{interaction.channel.name}.html")
            await creator.send(embed=dm_embed, file=f_creator)

            # Read handler from DB (survives restarts)
            claimed_by_name = "Staff"
            handler_id = ticket_data.get("claimed_by")
            if handler_id:
                handler_member = interaction.guild.get_member(handler_id)
                if handler_member:
                    claimed_by_name = handler_member.mention
                else:
                    claimed_by_name = f"<@{handler_id}>"

            is_test = bool(ticket_data.get("is_test"))

            rate_embed = discord.Embed(
                title="How was our service?",
                description=f"Please rate your experience with {claimed_by_name}.",
                color=0x5865F2,
            )
            if is_test:
                rate_embed.set_footer(text="🧪 Test Ticket Mode: Ratings will NOT be recorded.")

            await creator.send(
                embed=rate_embed,
                view=RatingView(
                    interaction.client, TICKET_LOG_CHANNEL_ID, interaction.channel.name,
                    claimed_by_name, handler_id=handler_id, guild_id=interaction.guild_id, is_test=is_test,
                ),
            )
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"Error sending DM to creator: {e}")

    # DM added users transcript only
    for uid in added_users_ids:
        try:
            u = await interaction.client.fetch_user(uid)
            dm_embed = discord.Embed(
                title="Ticket Closed",
                description=f"Ticket `{interaction.channel.name}` has been closed.",
                color=0xF2C21A,
            )
            dm_embed.add_field(name="Reason", value=reason)
            f_added = discord.File(io.StringIO(html_content), filename=f"transcript-{interaction.channel.name}.html")
            await u.send(embed=dm_embed, file=f_added)
        except Exception:
            pass

    try:
        await interaction.channel.delete()
    except discord.NotFound:
        pass
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to delete channel: {e}", ephemeral=True)


# ────────────────────────────────────────────────────────────────
# Cog
# ────────────────────────────────────────────────────────────────

class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_ticket_reminders.start()

    def cog_unload(self):
        self.check_ticket_reminders.cancel()

    async def cog_load(self):
        self.bot.add_view(TicketCreateView())
        self.bot.add_view(TicketActionsView())
        self.bot.loop.create_task(self.ensure_ticket_panel())

    @commands.Cog.listener()
    async def on_ready(self):
        await self.ensure_ticket_panel()

    # ── Admin: setup panel ─────────────────────────────────────

    @app_commands.command(name="setup_tickets", description="Setup or recreate the ticket panel (Admin only)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Channel to send the ticket panel in")
    async def setup_tickets(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        target = channel or interaction.channel
        await interaction.response.send_message("🔄 Setting up ticket panel...", ephemeral=True)
        await self.send_ticket_panel(target)
        await interaction.followup.send(f"✅ Ticket panel sent to {target.mention}.", ephemeral=True)

    async def send_ticket_panel(self, channel: discord.TextChannel):
        embed = discord.Embed(
            title="Support Tickets",
            description="**How can we help you?**\n\nPlease select the category that best matches your concern from the dropdown menu below.",
            color=0xF2C21A,
        )

        await channel.send(embed=embed, view=TicketCreateView())

    async def ensure_ticket_panel(self):
        """Check for existing panel and update its view, or skip if no channel configured."""
        await self.bot.wait_until_ready()
        # We rely on /setup_tickets to send panels now; this just re-registers views.

    # ── Admin: test mode ───────────────────────────────────────

    @app_commands.command(name="ticket_test", description="Toggle Test Mode for this ticket")
    @app_commands.default_permissions(administrator=True)
    async def ticket_test(self, interaction: discord.Interaction, enabled: bool):
        await interaction.response.defer(ephemeral=True)
        cid = interaction.channel_id

        ticket_data = await Database.fetchone(
            "SELECT * FROM active_tickets WHERE channel_id = %s", (cid,)
        )
        if not ticket_data:
            await interaction.followup.send("❌ This command can only be used in active ticket channels.", ephemeral=True)
            return

        await Database.execute(
            "UPDATE active_tickets SET is_test = %s WHERE channel_id = %s",
            (enabled, cid),
        )

        msg = ""
        try:
            current_name = interaction.channel.name
            new_name = current_name
            if enabled:
                if not current_name.startswith("[TEST]"):
                    new_name = re.sub(r"^\[.*?\]-", "", current_name)
                    new_name = f"[TEST]-{new_name}"
                    msg = f"🧪 **Test Mode ENABLED**.\nRatings will **NOT** be recorded."
            else:
                if current_name.startswith("[TEST]"):
                    base_name = current_name.replace("[TEST]-", "")
                    cat_key = ticket_data.get("category_key", "GN")
                    cat_data = TICKET_CATEGORIES.get(cat_key)
                    tag = cat_data["tag"] if cat_data else "gn"
                    new_name = f"[{tag}]-{base_name}"
                    msg = "✅ **Test Mode DISABLED**.\nRatings **WILL** be recorded."
                else:
                    msg = "✅ **Test Mode DISABLED**."

            if new_name != current_name:
                await interaction.channel.edit(name=new_name)
        except Exception as e:
            if "429" in str(e):
                msg += "\n⚠️ Channel rename skipped due to rate limits."
            else:
                msg += f"\n⚠️ Failed to rename channel: {e}"

        await interaction.followup.send(msg, ephemeral=True)

    # ── Background task: reminders ─────────────────────────────

    @tasks.loop(minutes=10)
    async def check_ticket_reminders(self):
        now = datetime.datetime.now(datetime.timezone.utc)

        tickets = await Database.fetchall(
            "SELECT * FROM active_tickets WHERE claimed = FALSE"
        )

        for data in tickets:
            created_at = data["created_at"]
            if created_at.tzinfo is None:
                import pytz
                created_at = pytz.utc.localize(created_at)
            elapsed = now - created_at

            channel = self.bot.get_channel(data["channel_id"])
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(data["channel_id"])
                except (discord.NotFound, discord.Forbidden):
                    await Database.execute("DELETE FROM active_tickets WHERE channel_id = %s", (data["channel_id"],))
                    continue
                except Exception:
                    continue

            cat_key = data.get("category_key", "GN")
            cat_data = TICKET_CATEGORIES.get(cat_key)
            role_id = cat_data["role_id"] if cat_data else SUPPORT_ROLE_ID

            try:
                # 48h escalation
                if elapsed > datetime.timedelta(hours=48) and not data.get("escalated_48h"):
                    others_role_id = ROLE_LEAGUE_OPS
                    msg = (
                        f"🚨 **UNCLAIMED TICKET ESCALATION (48h)**\n"
                        f"Attention <@&{role_id}> and <@&{others_role_id}>!\n"
                        "This ticket has been unattended for 2 days. Please resolve immediately."
                    )
                    await channel.send(msg)
                    await Database.execute(
                        "UPDATE active_tickets SET escalated_48h = TRUE WHERE channel_id = %s",
                        (data["channel_id"],),
                    )
                    continue

                # 24h reminder
                if elapsed > datetime.timedelta(hours=24) and not data.get("reminded_24h"):
                    msg = f"⏳ **Reminder:** This ticket has been unclaimed for 24 hours.\n<@&{role_id}> please review."
                    await channel.send(msg)
                    await Database.execute(
                        "UPDATE active_tickets SET reminded_24h = TRUE WHERE channel_id = %s",
                        (data["channel_id"],),
                    )
            except discord.Forbidden:
                pass
            except Exception as e:
                print(f"❌ Reminder Error: {e}")

    @check_ticket_reminders.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
