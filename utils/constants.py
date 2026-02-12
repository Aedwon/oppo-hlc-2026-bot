"""
Shared constants and configuration values.
All channel / role IDs are read from environment or set here as defaults.
Update these to match your Discord server.
"""
import os

# -------------------------------------------------------------------
# Channel IDs  (update to match your server)
# -------------------------------------------------------------------
TICKET_PANEL_CHANNEL_ID = int(os.getenv("TICKET_PANEL_CHANNEL_ID", "0"))
TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0"))
EMBED_LOG_CHANNEL_ID = int(os.getenv("EMBED_LOG_CHANNEL_ID", "0"))

# -------------------------------------------------------------------
# Role IDs  (update to match your server)
# -------------------------------------------------------------------
ROLE_LEAGUE_OPS = int(os.getenv("ROLE_LEAGUE_OPS", "0"))
ROLE_TECHNICAL = int(os.getenv("ROLE_TECHNICAL", "0"))
ROLE_CREATIVES = int(os.getenv("ROLE_CREATIVES", "0"))
ROLE_GENERAL = int(os.getenv("ROLE_GENERAL", "0"))
SUPPORT_ROLE_ID = int(os.getenv("SUPPORT_ROLE_ID", "0"))

# -------------------------------------------------------------------
# Ticket categories
# -------------------------------------------------------------------
TICKET_CATEGORIES = {
    "LO": {
        "label": "League Ops",
        "desc": "League Operations, Rules, Scheduling",
        "emoji": "⚔️",
        "tag": "lo",
        "role_id": ROLE_LEAGUE_OPS,
    },
    "TE": {
        "label": "Technical",
        "desc": "Bug Reports, Technical Issues",
        "emoji": "🛠️",
        "tag": "te",
        "role_id": ROLE_TECHNICAL,
    },
    "CR": {
        "label": "Creatives",
        "desc": "PubMats, Logos, Stream Assets",
        "emoji": "🎨",
        "tag": "cr",
        "role_id": ROLE_CREATIVES,
    },
    "GN": {
        "label": "General",
        "desc": "General Inquiries, Server Assistance",
        "emoji": "💬",
        "tag": "gn",
        "role_id": ROLE_GENERAL,
    },
}

# -------------------------------------------------------------------
# Timezone
# -------------------------------------------------------------------
import pytz

TZ_MANILA = pytz.timezone("Asia/Manila")
