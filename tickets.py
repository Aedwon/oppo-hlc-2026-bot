import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import io
import datetime
import pytz
import html
import json
import os
import re

# --- Configuration & Constants ---
TICKET_PANEL_CHANNEL_ID = 1217135394471022753
TICKET_LOG_CHANNEL_ID = 1217142468403793941

# Updated Role IDs
ROLE_LEAGUE_OPS = 1453237544287473877
ROLE_REWARDS = 1453238047515742310
ROLE_CONTENT = 1170484589253365872
ROLE_OTHERS = 1453237544287473877 # Same as League Ops (Escalation)
SUPPORT_ROLE_ID = 1170167517449302147 # Default fallback

# Timezone
TZ_MANILA = pytz.timezone('Asia/Manila')

# Persistence
ACTIVE_TICKETS_FILE = "data/active_tickets.json"
RATINGS_FILE = "data/ratings.json"
active_tickets = {} # {channel_id: {created_at, category_key, claimed, creator_id, is_test, ...}}

def save_tickets():
    os.makedirs(os.path.dirname(ACTIVE_TICKETS_FILE), exist_ok=True)
    with open(ACTIVE_TICKETS_FILE, 'w') as f:
        json.dump(active_tickets, f, indent=4)

def load_tickets():
    global active_tickets
    if os.path.exists(ACTIVE_TICKETS_FILE):
        try:
            with open(ACTIVE_TICKETS_FILE, "r") as f:
                active_tickets = json.load(f)
        except Exception as e:
            print(f"Error loading tickets: {e}")

def save_ratings(rating_data):
    ratings = []
    if os.path.exists(RATINGS_FILE):
        try:
            with open(RATINGS_FILE, "r") as f:
                ratings = json.load(f)
        except: pass # Start fresh if corrupt/empty
    
    ratings.append(rating_data)
    
    try:
        os.makedirs(os.path.dirname(RATINGS_FILE), exist_ok=True)
        with open(RATINGS_FILE, "w") as f:
            json.dump(ratings, f, indent=4)
    except Exception as e:
        print(f"Error saving rating: {e}")

# Categories
TICKET_CATEGORIES = {
    "A": {
        "label": "League Operations",
        "desc": "Registration, Rules, Roster, Schedules",
        "emoji": "⚔️",
        "tag": "a",
        "role_id": ROLE_LEAGUE_OPS
    },
    "B": {
        "label": "Rewards & Payouts",
        "desc": "Diamonds, Monetary Prizes, Incentives",
        "emoji": "💎",
        "tag": "b",
        "role_id": ROLE_REWARDS
    },
    "C": {
        "label": "Contents & Socials",
        "desc": "PubMats, Logos, Stream Assets",
        "emoji": "🎨",
        "tag": "c",
        "role_id": ROLE_CONTENT
    },
    "D": {
        "label": "General & Tech Support",
        "desc": "Server Assistance, Bug Reports, Inquiries",
        "emoji": "🛠️",
        "tag": "d",
        "role_id": ROLE_OTHERS
    }
}

# --- HTML Transcript Generator ---
def generate_html_transcript(messages: list[discord.Message], channel_name: str) -> str:
    """Generates a beautiful HTML transcript of the chat history."""
    
    style = """
    <style>
        body { font-family: 'gg sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #313338; color: #dbdee1; margin: 0; padding: 20px; }
        .header { border-bottom: 1px solid #3f4147; padding-bottom: 10px; margin-bottom: 20px; }
        .header h1 { color: #f2f3f5; margin: 0; font-size: 20px; }
        .chat-container { display: block; width: 100%; } /* Default block behavior stacks vertically */
        .message-group { display: flex; margin-bottom: 16px; align-items: flex-start; width: 100%; } /* Full width */
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
        
        /* Embed Styles */
        .embed { 
            display: flex; 
            max-width: 520px; 
            background-color: #2b2d31; 
            border-radius: 4px; 
            border-left: 4px solid #202225; 
            margin-top: 8px; 
            font-size: 14px;
        }
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
     
    # Pre-fetch guild for mention resolution if possible
    try:
        guild = messages[0].guild if messages else None
    except:
        guild = None

    for msg in messages:
        try:
            # User Info
            avatar_url = msg.author.display_avatar.url if msg.author.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"
            username = html.escape(msg.author.display_name)
            
            # Convert to Manila Time
            try:
                created_at_pht = msg.created_at.astimezone(TZ_MANILA)
            except Exception:
                # Fallback to whatever time is available or UTC
                created_at_pht = msg.created_at
                
            timestamp = created_at_pht.strftime('%m/%d/%Y %I:%M %p')
            
            # --- Content Formatting (Markdown & Mentions) ---
            # IMPORTANT: escape html first, THEN substitutions
            content = html.escape(msg.content or "")
            
            # 1. User Mentions <@ID> -> &lt;@ID&gt;
            def replace_user(match):
                uid = int(match.group(1))
                name = f"@{uid}"
                if guild:
                    member = guild.get_member(uid)
                    if member: name = f"@{member.display_name}"
                else:
                    pass
                return f'<span class="mention">{html.escape(name)}</span>'
            
            # Match &lt;@123&gt; or &lt;@!123&gt;
            content = re.sub(r'&lt;@!?(\d+)&gt;', replace_user, content)

            # 2. Role Mentions <@&ID> -> &lt;@&ID&gt;
            def replace_role(match):
                rid = int(match.group(1))
                name = f"@{rid}"
                if guild:
                    role = guild.get_role(rid)
                    if role: name = f"@{role.name}"
                return f'<span class="mention">{html.escape(name)}</span>'
            
            content = re.sub(r'&lt;@&(\d+)&gt;', replace_role, content)

            # 3. Channel Mentions <#ID> -> &lt;#ID&gt;
            def replace_channel(match):
               cid = int(match.group(1))
               name = f"#{cid}"
               if guild:
                   chan = guild.get_channel(cid)
                   if chan: name = f"#{chan.name}"
               return f'<span class="mention">{html.escape(name)}</span>'
            
            content = re.sub(r'&lt;#(\d+)&gt;', replace_channel, content)

            # 4. Basic Markdown
            # Bold (**text**)
            content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', content)
            # Italic (*text* or _text_)
            content = re.sub(r'\*(.*?)\*', r'<em>\1</em>', content)
            # Underline (__text__)
            content = re.sub(r'__(.*?)__', r'<u>\1</u>', content)
            # Strikethrough (~~text~~)
            content = re.sub(r'~~(.*?)~~', r'<s>\1</s>', content)

            # Special Mentions
            content = content.replace("@everyone", '<span class="mention">@everyone</span>')
            content = content.replace("@here", '<span class="mention">@here</span>')
            
            bot_tag = '<span class="bot-tag">BOT</span>' if msg.author.bot else ''
           
            attachments_html = ""
            if msg.attachments:
                for att in msg.attachments:
                    if att.content_type and att.content_type.startswith('image/'):
                        attachments_html += f'<div class="attachment"><a href="{att.url}" target="_blank"><img src="{att.url}" alt="Attachment"></a></div>'
                    else:
                         attachments_html += f'<div class="attachment"><a href="{att.url}" target="_blank">📄 {html.escape(att.filename)}</a></div>'

            # Process Embeds
            embeds_html = ""
            if msg.embeds:
                for embed in msg.embeds:
                    color = f"#{embed.color.value:06x}" if embed.color else "#202225"
                    border_style = f"border-left-color: {color};"
                    
                    author_html = ""
                    if embed.author:
                        icon = f'<img src="{embed.author.icon_url}">' if embed.author.icon_url else ""
                        author_html = f'<div class="embed-author">{icon} {html.escape(embed.author.name or "")}</div>'
                        
                    title_html = f'<div class="embed-title">{html.escape(embed.title)}</div>' if embed.title else ""
                    desc_html = f'<div class="embed-desc">{html.escape(embed.description)}</div>' if embed.description else ""
                    
                    fields_html = '<div class="embed-fields">'
                    for field in embed.fields:
                        inline = "embed-field-inline" if field.inline else "embed-field"
                        fields_html += f'''
                        <div class="{inline}">
                            <div class="embed-field-name">{html.escape(field.name)}</div>
                            <div class="embed-field-value">{html.escape(field.value)}</div>
                        </div>
                        '''
                    fields_html += '</div>' if embed.fields else ""
                    
                    footer_text = ""
                    footer_icon = ""
                    if embed.footer:
                        footer_text = html.escape(embed.footer.text or "")
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

                    embeds_html += f'''
                    <div class="embed" style="{border_style}">
                        <div class="embed-content">
                            {author_html}
                            {title_html}
                            {desc_html}
                            {fields_html}
                            {footer_html}
                        </div>
                    </div>
                    '''

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
            # Robustness: Log and skip/continue
            print(f"Error processing message {msg.id}: {e}")
            continue

    html_content += """
        </div> <!-- End chat-container -->
    </body>
    </html>
    """
    
    return html_content

# --- UI Components ---

class TicketTopicSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for key, data in TICKET_CATEGORIES.items():
            options.append(discord.SelectOption(
                label=data["label"],
                description=data["desc"],
                emoji=data["emoji"],
                value=key
            ))
        super().__init__(placeholder="Select the category of your concern...", min_values=1, max_values=1, custom_id="ticket_category_select", options=options)

    async def callback(self, interaction: discord.Interaction):
        # Open Modal
        selected_key = self.values[0]
        category_data = TICKET_CATEGORIES.get(selected_key)
        await interaction.response.send_modal(TicketModal(category_key=selected_key, category_data=category_data))

class TicketTopicView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300) # Ephemeral view, short timeout
        self.add_item(TicketTopicSelect())

class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="📩 Create Ticket", style=discord.ButtonStyle.primary, custom_id="create_ticket_base")
    async def create_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Please select the category below:", view=TicketTopicView(), ephemeral=True)

class TicketModal(discord.ui.Modal):
    def __init__(self, category_key, category_data):
        super().__init__(title=f"New {category_data['label']} Ticket")
        self.category_key = category_key
        self.category_data = category_data
        
        self.ticket_subject = discord.ui.TextInput(
            label="Subject",
            placeholder="Briefly state your concern...",
            max_length=100
        )
        self.ticket_desc = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            placeholder="Please provide more details...",
            max_length=1000
        )
        self.add_item(self.ticket_subject)
        self.add_item(self.ticket_desc)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        user = interaction.user
        category_channel = interaction.channel.category # The Discord Category (e.g. "Support Tickets")

        if not category_channel:
             # Fallback if command used outside category
             category_channel = discord.utils.get(guild.categories, name="🎟⎮tickets") # Try reasonable default

        if not category_channel:
             await interaction.followup.send("❌ Error: Could not determine category.", ephemeral=True)
             return

        # Naming Convention: [tag]-username
        tag = self.category_data["tag"]
        channel_name = f"[{tag}]-{user.name}"
        
        existing = discord.utils.get(guild.text_channels, name=channel_name)
        if existing:
             await interaction.followup.send(f"❌ You already have a ticket of this type open: {existing.mention}", ephemeral=True)
             return

        # Overwrites
        role_to_ping_id = self.category_data["role_id"]
        role_to_ping = guild.get_role(role_to_ping_id)
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True)
        }
        
        if role_to_ping:
             overwrites[role_to_ping] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        
        # Also always add the main support role if different
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
        except Exception as e:
            await interaction.followup.send(f"❌ Unexpected Error: {e}", ephemeral=True)
            return

        # Save to Persistence
        active_tickets[str(ticket_channel.id)] = {
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "category_key": self.category_key,
            "creator_id": user.id,
            "claimed": False,
            "reminded_24h": False,
            "escalated_48h": False,
            "is_test": False # Default to not a test ticket
        }
        save_tickets()

        embed = discord.Embed(
            title=f"{self.category_data['emoji']} {self.category_data['label']}",
            description=f"**Subject:** {self.ticket_subject.value}\n\n{self.ticket_desc.value}",
            color=0xF2C21A
        )
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url or user.default_avatar.url)
        embed.set_footer(text=f"System developed by Aedwon")
        
        view = TicketActionsView(creator=user)
        
        mention_text = role_to_ping.mention if role_to_ping else ""
        try:
            await ticket_channel.send(content=f"{user.mention} {mention_text}", embed=embed, view=view)
        except Exception:
             pass # Channel created but failed to send message? Unlikely but safe.

        await interaction.followup.send(f"✅ Ticket created: {ticket_channel.mention}", ephemeral=True)

class TicketActionsView(discord.ui.View):
    def __init__(self, creator: discord.User | None = None):
        super().__init__(timeout=None)
        self.creator = creator
        self.claimed_by = None # To store the user who claimed it for rating purposes

    @discord.ui.button(label="🔄 Move Category", style=discord.ButtonStyle.secondary, custom_id="move_category_ticket")
    async def move_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Select the new category:", view=MoveCategoryView(), ephemeral=True)

    @discord.ui.button(label="🛠 Claim Ticket", style=discord.ButtonStyle.success, custom_id="claim_ticket")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True) # Check persistence/logic safely
        
        cid = str(interaction.channel_id)
        
        # Concurrency Check: Reload from source of truth
        if cid not in active_tickets:
             # Try refreshing from disk in case another process updated it (unlikely here but safe)
             load_tickets()
        
        ticket_data = active_tickets.get(cid)
        if not ticket_data:
            await interaction.followup.send("❌ Ticket data not found. This ticket may be broken.", ephemeral=True)
            return

        # Double check claimed status
        if ticket_data.get("claimed"):
            await interaction.followup.send("❌ This ticket is already claimed.", ephemeral=True)
            # Update UI if possible
            button.disabled = True
            button.label = "Already Claimed"
            await interaction.message.edit(view=self)
            return

        user = interaction.user
        
        # 1. Block Creator
        creator_id = ticket_data.get("creator_id")
        if creator_id and user.id == creator_id:
             await interaction.followup.send("❌ You cannot claim your own ticket.", ephemeral=True)
             return

        # 2. Block Added Users
        added_users = ticket_data.get("added_users", [])
        if user.id in added_users:
             await interaction.followup.send("❌ Added users cannot claim the ticket.", ephemeral=True)
             return

        # 3. Role Validation
        allowed = False
        if user.guild_permissions.administrator: 
            allowed = True
        else:
            cat_key = ticket_data.get("category_key")
            cat_data = TICKET_CATEGORIES.get(cat_key)
            cat_role_id = cat_data["role_id"] if cat_data else None
            
            # Check primary category role
            cat_role = interaction.guild.get_role(cat_role_id)
            if cat_role and cat_role in user.roles:
                allowed = True
            
            # Check Escalation: League Ops can claim escalated tickets
            # "Unless escalations have happened, which allows the league ops team"
            if ticket_data.get("escalated_48h"):
                league_ops_role = interaction.guild.get_role(ROLE_LEAGUE_OPS)
                if league_ops_role and league_ops_role in user.roles:
                    allowed = True

        if not allowed:
             await interaction.followup.send("❌ You do not have permission to claim this ticket.", ephemeral=True)
             return

        # Success
        ticket_data["claimed"] = True
        ticket_data["claimed_by"] = user.id
        self.claimed_by = user # Store for rating
        save_tickets()
        
        button.disabled = True
        button.label = f"Claimed by {user.display_name}"
        try:
            # Edit the message on which the button was clicked
            await interaction.message.edit(view=self)
            
            embed = discord.Embed(description=f"✅ **Ticket claimed by {user.mention}**", color=0x00FF00)
            embed.set_footer(text="System developed by Aedwon")
            await interaction.channel.send(content=user.mention, embed=embed)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Error updating UI: {e}", ephemeral=True)

    @discord.ui.button(label="👥 Add User", style=discord.ButtonStyle.secondary, custom_id="add_user_ticket")
    async def add_user_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Select users to add:", view=AddUserView(), ephemeral=True)

    @discord.ui.button(label="🚫 Remove User", style=discord.ButtonStyle.secondary, custom_id="remove_user_ticket")
    async def remove_user_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only show users that are currently added
        cid = str(interaction.channel_id)
        ticket_data = active_tickets.get(cid)
        added_ids = ticket_data.get("added_users", []) if ticket_data else []
        
        if not added_ids:
            await interaction.response.send_message("❌ No users have been added to this ticket.", ephemeral=True)
            return
            
        await interaction.response.send_message("Select users to remove:", view=RemoveUserView(added_ids), ephemeral=True)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Trigger Reason Selection instead of immediate closure
        await interaction.response.send_message("Please select a reason for closing this ticket:", view=CloseReasonView(self), ephemeral=True)
        
class AddUserView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
    
    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select users to add...", min_values=1, max_values=5)
    async def select_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        if not isinstance(interaction.channel, discord.TextChannel): return

        # Defer immediately to avoid timeout during permissions updates
        await interaction.response.defer()
        
        cid = str(interaction.channel_id)
        ticket_data = active_tickets.get(cid)
        if not ticket_data:
             # Should practically never happen unless cache desolate
             active_tickets[cid] = {"created_at": datetime.datetime.now().isoformat(), "added_users": []}
             ticket_data = active_tickets[cid]
        
        current_added = ticket_data.setdefault("added_users", [])
        
        added_mentions = []
        errors = []
        for user in select.values:
            if user.bot: continue
            
            # Persist
            if user.id not in current_added:
                current_added.append(user.id)
                
            # Permission
            try:
                await interaction.channel.set_permissions(user, view_channel=True, send_messages=True)
                added_mentions.append(user.mention)
            except Exception as e:
                errors.append(f"{user.display_name}: {e}")
            
        save_tickets()
        
        if added_mentions:
            msg = f"👥 **{', '.join(added_mentions)}** have been added to the ticket."
            await interaction.followup.send(f"✅ Users added.")
            await interaction.channel.send(msg)
        else:
            await interaction.followup.send("No valid users selected or permission updates failed.", ephemeral=True)
        
        if errors:
            await interaction.followup.send(f"⚠️ Some errors occurred:\n" + "\n".join(errors), ephemeral=True)

        self.stop()

# Redefine RemoveUserView to use UserSelect for UX, but filter logic
class RemoveUserView(discord.ui.View):
    def __init__(self, allowed_ids):
        super().__init__(timeout=60)
        self.allowed_ids = allowed_ids

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select users to remove...", min_values=1, max_values=5)
    async def select_remove(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        # Defer immediately
        await interaction.response.defer()

        ticket_data = active_tickets.get(str(interaction.channel_id))
        if not ticket_data: return
        
        removed_names = []
        errors = []
        current_added = ticket_data.get("added_users", [])
        
        for user in select.values:
            if user.id in self.allowed_ids and user.id in current_added:
                # Remove Permission
                try:
                    await interaction.channel.set_permissions(user, overwrite=None)
                    # Remove from Persistence
                    current_added.remove(user.id)
                    removed_names.append(user.display_name)
                except Exception as e:
                    errors.append(f"{user.display_name}: {e}")
        
        save_tickets()
        
        if removed_names:
             await interaction.followup.send(f"🚫 Removed users: {', '.join(removed_names)}")
        else:
             await interaction.followup.send("❌ Selected user was not in the 'Added Users' list.", ephemeral=True)
        
        if errors:
             await interaction.followup.send(f"⚠️ Errors removing some users:\n" + "\n".join(errors), ephemeral=True)

        self.stop()

class MoveCategorySelect(discord.ui.Select):
    def __init__(self):
        options = []
        for key, data in TICKET_CATEGORIES.items():
            options.append(discord.SelectOption(
                label=data["label"],
                emoji=data["emoji"],
                value=key
            ))
        super().__init__(placeholder="Select new category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False) # Public visibility for transparency
        
        new_key = self.values[0]
        new_cat_data = TICKET_CATEGORIES.get(new_key)
        
        # Get persistence data
        cid = str(interaction.channel_id)
        ticket_data = active_tickets.get(cid)
        old_cat_key = ticket_data.get("category_key") if ticket_data else None
        
        # Determine Creator Name for Rename
        creator_name = "unknown"
        # Try view reference
        if self.view and hasattr(self.view, 'creator') and self.view.creator:
             creator_name = self.view.creator.name
        elif ticket_data and ticket_data.get("creator_id"):
             # Try to fetch
             c_id = ticket_data.get("creator_id")
             mem = interaction.guild.get_member(c_id)
             if mem: creator_name = mem.name
             else:
                 # Last resort: parse existing channel name [tag]-username
                 match = re.search(r"-\s*(.*)$", interaction.channel.name) # rough regex
                 if match: creator_name = match.group(1)
        else:
             # Last resort parsing
             # Expected format: [tag]-username or tag-username
             parts = interaction.channel.name.split("-", 1)
             if len(parts) > 1:
                 creator_name = parts[1]

        # Update Overwrites
        overwrites = interaction.channel.overwrites
        guild = interaction.guild
        
        # Add new role
        new_role = guild.get_role(new_cat_data["role_id"])
        if new_role:
             overwrites[new_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
             
        # Remove old role (if different and not support/admin)
        if old_cat_key:
             old_cat_data = TICKET_CATEGORIES.get(old_cat_key)
             if old_cat_data and old_cat_data["role_id"] != new_cat_data["role_id"]:
                  old_role = guild.get_role(old_cat_data["role_id"])
                  if old_role and old_role != guild.get_role(SUPPORT_ROLE_ID):
                       # Check if we can just delete query or set to None
                       overwrites.pop(old_role, None)

        # Rename Channel & Apply Overwrites
        new_channel_name = f"[{new_cat_data['tag']}]-{creator_name}"
        msg = f"✅ Ticket moved to **{new_cat_data['label']}**.\n"
        
        try:
            await interaction.channel.edit(name=new_channel_name, overwrites=overwrites)
        except Exception as e:
            # Handle Rate Limits Gracefully
            if "RateLimited" in str(e) or "429" in str(e):
                 msg += f"\n⚠️ **Warning:** Channel name update skipped due to Discord rate limits (2/10m). Permissions were NOT updated. Please try again later."
                 # In this case, we might NOT want to update persistence if permissions failed too (edit() does both).
                 # So we abort updates.
                 await interaction.followup.send(msg)
                 return
            else:
                 msg += f"\n⚠️ Failed to update channel: {e}"
        
        # Update persistence only if successful/partial success
        if ticket_data:
             ticket_data["category_key"] = new_key
             save_tickets()
             
        msg += f"Pinged: {new_role.mention if new_role else 'None'}"
        await interaction.followup.send(msg)

# --- Rating System ---

class FeedbackModal(discord.ui.Modal):
    def __init__(self, stars, view_ref):
        super().__init__(title=f"You rated {stars} Stars!")
        self.stars = stars
        self.view_ref = view_ref
        
        self.remarks = discord.ui.TextInput(
            label="Any comments? (Optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Let us know how we can improve...",
            required=False,
            max_length=1000
        )
        self.add_item(self.remarks)

    async def on_submit(self, interaction: discord.Interaction):
        # 1. Update Message (Remove buttons by setting view=None)
        await interaction.response.edit_message(content=f"✅ Thank you for your feedback! You rated us **{self.stars}/5** ⭐", view=None, embed=None)
        
        # 2. Check for Test Mode exclusion
        if self.view_ref.is_test:
             # Skip logging/saving
             return

        # 3. Log to Server (Channel)
        log_channel = self.view_ref.bot.get_channel(self.view_ref.log_channel_id)
        if log_channel:
             embed = discord.Embed(title="🌟 New Feedback Received", color=0xFFD700, timestamp=datetime.datetime.now(TZ_MANILA))
             embed.add_field(name="User", value=interaction.user.mention, inline=True)
             embed.add_field(name="Ticket", value=self.view_ref.ticket_name, inline=True)
             embed.add_field(name="Handler", value=self.view_ref.handler_mention, inline=True)
             embed.add_field(name="Rating", value=f"{'⭐' * self.stars} ({self.stars}/5)", inline=False)
             if self.remarks.value:
                 embed.add_field(name="Remarks", value=self.remarks.value, inline=False)
                 
             embed.set_footer(text="System developed by Aedwon")
             try: await log_channel.send(embed=embed)
             except: pass

        # 4. Save to JSON (Persistence)
        rating_data = {
            "timestamp": datetime.datetime.now(TZ_MANILA).isoformat(),
            "ticket_name": self.view_ref.ticket_name,
            "user_id": interaction.user.id,
            "user_name": interaction.user.name,
            "handler_mention": self.view_ref.handler_mention,
            "stars": self.stars,
            "remarks": self.remarks.value
        }
        save_ratings(rating_data)


class RatingView(discord.ui.View):
    def __init__(self, bot, log_channel_id, ticket_name, handler_mention, is_test=False):
        super().__init__(timeout=None)
        self.bot = bot
        self.log_channel_id = log_channel_id
        self.ticket_name = ticket_name
        self.handler_mention = handler_mention
        self.is_test = is_test
        self.value = None

    async def prompt_feedback(self, interaction: discord.Interaction, stars: int):
        self.value = stars
        # We trigger the modal BUT we cannot edit the message *and* send a modal in one response.
        # Interaction rule: One response per interaction.
        # So we response_modal.
        
        await interaction.response.send_modal(FeedbackModal(stars, self))

    @discord.ui.button(label="1", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="rate_1")
    async def rate_1(self, interaction: discord.Interaction, button: discord.ui.Button): await self.prompt_feedback(interaction, 1)

    @discord.ui.button(label="2", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="rate_2")
    async def rate_2(self, interaction: discord.Interaction, button: discord.ui.Button): await self.prompt_feedback(interaction, 2)

    @discord.ui.button(label="3", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="rate_3")
    async def rate_3(self, interaction: discord.Interaction, button: discord.ui.Button): await self.prompt_feedback(interaction, 3)

    @discord.ui.button(label="4", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="rate_4")
    async def rate_4(self, interaction: discord.Interaction, button: discord.ui.Button): await self.prompt_feedback(interaction, 4)

    @discord.ui.button(label="5", emoji="⭐", style=discord.ButtonStyle.success, custom_id="rate_5")
    async def rate_5(self, interaction: discord.Interaction, button: discord.ui.Button): await self.prompt_feedback(interaction, 5)


# --- Close Reason Logic ---

class CloseReasonModal(discord.ui.Modal):
    def __init__(self, reason_selected, view_ref):
        super().__init__(title=f"Closing: {reason_selected}")
        self.reason_selected = reason_selected
        self.view_ref = view_ref # To access creator/context
        
        self.remarks = discord.ui.TextInput(
            label="Additional Remarks (Optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Any specific details? (Leave blank if none)",
            required=False,
            max_length=500
        )
        self.add_item(self.remarks)

    async def on_submit(self, interaction: discord.Interaction):
        # Proceed to actual closure
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
        # Open Modal
        reason = self.values[0]
        await interaction.response.send_modal(CloseReasonModal(reason, self.view.origin_view))

class CloseReasonView(discord.ui.View):
    def __init__(self, origin_view):
        super().__init__(timeout=60)
        self.origin_view = origin_view
        self.add_item(CloseReasonSelect())


async def finish_closure(interaction: discord.Interaction, reason: str, remarks: str, origin_view):
    await interaction.response.defer()
    
    cid = str(interaction.channel_id)
    
    # Concurrency Check again
    if cid not in active_tickets:
        # Maybe deleted just now?
        await interaction.followup.send("❌ Ticket appears to be already closed.", ephemeral=True)
        return

    # Fetch data before deletion from persistence
    ticket_data = active_tickets.get(cid)
    creator_id = ticket_data.get("creator_id") if ticket_data else None
    added_users_ids = ticket_data.get("added_users", []) if ticket_data else []
    
    # Remove from Persistence
    if cid in active_tickets:
        del active_tickets[cid]
        save_tickets()

    # Transcript Generation
    messages = [message async for message in interaction.channel.history(limit=500, oldest_first=True)]
    html_content = generate_html_transcript(messages, interaction.channel.name)
    
    file = discord.File(io.StringIO(html_content), filename=f"transcript-{interaction.channel.name}.html")
    
    # Log Channel
    log_channel = interaction.guild.get_channel(TICKET_LOG_CHANNEL_ID)
    embed = discord.Embed(title="Ticket Closed", color=0xFF0000, timestamp=datetime.datetime.now(TZ_MANILA))
    embed.add_field(name="Ticket", value=interaction.channel.name, inline=True)
    embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
    embed.add_field(name="Reason", value=reason, inline=True)
    if remarks:
        embed.add_field(name="Remarks", value=remarks, inline=False)
    
    # Determine Creator for Logging/DM
    creator = origin_view.creator
    if not creator and creator_id:
        try: creator = await interaction.client.fetch_user(creator_id)
        except: creator = None
        
    if creator:
        embed.add_field(name="Creator", value=creator.mention, inline=True)

    if log_channel:
        try:
            await log_channel.send(embed=embed, file=file)
        except Exception: pass
        
    # DM Transcript + Rating
    # We need fresh IO for each send
    
    # 1. Creator (Get Rating Request)
    if creator:
        try:
            dm_embed = discord.Embed(
                title="Ticket Closed", 
                description=f"Your ticket `{interaction.channel.name}` has been closed.",
                color=0xF2C21A,
                timestamp=datetime.datetime.now(TZ_MANILA)
            )
            dm_embed.add_field(name="Reason", value=reason)
            if remarks: dm_embed.add_field(name="Remarks", value=remarks)
            dm_embed.set_footer(text="System developed by Aedwon")
            
            f_creator = discord.File(io.StringIO(html_content), filename=f"transcript-{interaction.channel.name}.html")
            
            # Rating View
            claimed_by_name = "Staff"
            if origin_view.claimed_by:
                claimed_by_name = origin_view.claimed_by.mention
            
            # Use Flag from persistence
            is_test = ticket_data.get("is_test", False)
            
            # Send Transcript first
            await creator.send(embed=dm_embed, file=f_creator)
            
            # Send Rating Request
            rate_embed = discord.Embed(
                title="How was our service?",
                description=f"Please rate your experience with {claimed_by_name}.",
                color=0x5865F2
            )
            if is_test:
                rate_embed.set_footer(text="🧪 Test Ticket Mode: Ratings will NOT be recorded.")

            await creator.send(embed=rate_embed, view=RatingView(interaction.client, TICKET_LOG_CHANNEL_ID, interaction.channel.name, claimed_by_name, is_test=is_test))
            
        except discord.Forbidden:
            pass # DMs blocked
        except Exception as e:
            print(f"Error sending DM to creator: {e}")

    # 2. Added Users (Transcript Only)
    for uid in added_users_ids:
        try:
            u = await interaction.client.fetch_user(uid)
            dm_embed = discord.Embed(
                title="Ticket Closed", 
                description=f"Ticket `{interaction.channel.name}` has been closed.",
                color=0xF2C21A
            )
            dm_embed.add_field(name="Reason", value=reason)
            f_added = discord.File(io.StringIO(html_content), filename=f"transcript-{interaction.channel.name}.html")
            await u.send(embed=dm_embed, file=f_added)
        except: pass

    try:
        await interaction.channel.delete()
    except discord.NotFound:
        pass
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to delete channel: {e}", ephemeral=True)


class MoveCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(MoveCategorySelect())



# --- Cog Setup ---

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        load_tickets()
        self.check_ticket_reminders.start()

    def cog_unload(self):
        self.check_ticket_reminders.cancel()

    async def cog_load(self):
        self.bot.add_view(TicketCreateView())
        self.bot.add_view(TicketActionsView())
        # Try to ensure panel on load (async)
        self.bot.loop.create_task(self.ensure_ticket_panel())

    @commands.Cog.listener()
    async def on_ready(self):
        await self.ensure_ticket_panel()

    @app_commands.command(name="ticket_test", description="Toggle Test Mode for this ticket (Excludes from stats/ratings)")
    @app_commands.checks.has_any_role(SUPPORT_ROLE_ID, 1334382585141203010) # Admin/Support
    async def ticket_test(self, interaction: discord.Interaction, enabled: bool):
        # 1. Defer immediately to avoid timeout on rate limits
        await interaction.response.defer(ephemeral=True)
        
        cid = str(interaction.channel_id)
        if cid not in active_tickets:
            await interaction.followup.send("❌ This command can only be used in active ticket channels.", ephemeral=True)
            return

        ticket_data = active_tickets[cid]
        ticket_data["is_test"] = enabled
        save_tickets()

        # Update Channel Name
        msg = ""
        try:
            current_name = interaction.channel.name
            new_name = current_name
            
            if enabled:
                # Add [TEST] prefix if not present
                if not current_name.startswith("[TEST]"):
                    # Strip existing tag if present [Tag]-
                    new_name = re.sub(r"^\[.*?\]-", "", current_name) 
                    new_name = f"[TEST]-{new_name}"
                    msg = f"🧪 **Test Mode ENABLED**.\nUpdated channel to `{new_name}`.\nRatings for this ticket will **NOT** be recorded."
            else:
                # Restore Category Tag
                if current_name.startswith("[TEST]"):
                    # Remove [TEST]-
                    base_name = current_name.replace("[TEST]-", "")
                    cat_key = ticket_data.get("category_key", "D")
                    cat_data = TICKET_CATEGORIES.get(cat_key)
                    tag = cat_data["tag"] if cat_data else "Support"
                    new_name = f"[{tag}]-{base_name}"
                    msg = f"✅ **Test Mode DISABLED**.\nUpdated channel to `{new_name}`.\nRatings for this ticket **WILL** be recorded."
                else:
                    msg = "✅ **Test Mode DISABLED**. (Channel name verified)."

            if new_name != current_name:
                await interaction.channel.edit(name=new_name)
            
        except Exception as e:
            if "RateLimited" in str(e) or "429" in str(e):
                 msg += f"\n⚠️ **Warning:** Channel rename skipped due to Discord rate limits (2 per 10 mins). Please try renaming manually or wait."
            else:
                 msg += f"\n⚠️ Failed to rename channel: {e}"

        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="setup_tickets", description="Setup or recreate the ticket panel (Admin only)")
    @app_commands.default_permissions(administrator=True)
    async def setup_tickets(self, interaction: discord.Interaction):
        """Force recreate the ticket panel."""
        await interaction.response.send_message("🔄 Setting up ticket panel...", ephemeral=True)
        await self.ensure_ticket_panel()
        await interaction.followup.send("✅ Done.", ephemeral=True)

    async def ensure_ticket_panel(self):
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(TICKET_PANEL_CHANNEL_ID)
        if not channel:
            print(f"❌ Ticket panel channel {TICKET_PANEL_CHANNEL_ID} not found in cache.")
            return

        print(f"Checking ticket panel in {channel.name}...")

        # Check existing
        async for msg in channel.history(limit=20):
             if msg.author == self.bot.user and msg.embeds and msg.embeds[0].title == "Support Tickets":
                 # Already exists
                 # We can edit it to be sure it has latest View
                 await msg.edit(view=TicketCreateView())
                 print("✅ Updated existing ticket panel.")
                 return
        
        # Create new
        embed = discord.Embed(
            title="Support Tickets",
            description="**How can we help you?**\n\nPlease select the category that best matches your concern from the dropdown menu below.",
            color=0xF2C21A
        )
        embed.set_footer(text="System developed by Aedwon")
        await channel.send(embed=embed, view=TicketCreateView())
        print("✅ Created new ticket panel.")

    @tasks.loop(minutes=10) # Check every 10 minutes
    async def check_ticket_reminders(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # Iterate over copy to be safe
        for channel_id_str, data in list(active_tickets.items()):
            if data.get("claimed"):
                continue # Skip claimed tickets
                
            created_at = datetime.datetime.fromisoformat(data["created_at"])
            elapsed = now - created_at
            
            channel = self.bot.get_channel(int(channel_id_str))
            if not channel:
                # Double check with fetch (in case uncached but exists, though unlikely for bot)
                try:
                    channel = await self.bot.fetch_channel(int(channel_id_str))
                except (discord.NotFound, discord.Forbidden):
                    # Channel deleted manually? cleanup
                    print(f"🧹 Cleaning up deleted ticket data: {channel_id_str}")
                    del active_tickets[channel_id_str]
                    save_tickets()
                    continue
                except Exception:
                     continue # Temporary network error? Skip
            
            cat_key = data.get("category_key", "D")
            cat_data = TICKET_CATEGORIES.get(cat_key)
            role_id = cat_data["role_id"] if cat_data else SUPPORT_ROLE_ID

            try:
                # 48 Hour Escalation (Rewards B & Content C)
                if elapsed > datetime.timedelta(hours=48) and not data.get("escalated_48h"):
                    if cat_key in ["B", "C"]:
                        others_role_id = ROLE_OTHERS
                        
                        msg = (f"🚨 **UNCLAIMED TICKET ESCALATION (48h)**\n"
                               f"Attention <@&{role_id}> and <@&{others_role_id}>!\n"
                               "This ticket has been unattended for 2 days. Please resolve immediately.")
                        await channel.send(msg)
                        data["escalated_48h"] = True
                        save_tickets()
                        continue

                # 24 Hour Reminder (All)
                if elapsed > datetime.timedelta(hours=24) and not data.get("reminded_24h"):
                    msg = (f"⏳ **Reminder:** This ticket has been unclaimed for 24 hours.\n"
                           f"<@&{role_id}> please review.")
                    await channel.send(msg)
                    data["reminded_24h"] = True
                    save_tickets()
            except discord.Forbidden:
                 pass # Cannot send message
            except Exception as e:
                 print(f"❌ Reminder Error: {e}")

    @check_ticket_reminders.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(Tickets(bot))