# OPPO HLC Discord Bot

A feature-rich Discord bot built with discord.py and backed by MySQL. Designed for community management with verification, ticketing, embed management, thread creation, auto voice channels, team management, and a dynamic help system.

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Project Structure](#project-structure)
- [Setup](#setup)
- [Environment Variables](#environment-variables)
- [Commands](#commands)
- [Database Schema](#database-schema)
- [Architecture](#architecture)

## Features

### Verification

Users verify their identity through an interactive panel. Clicking the Verify button opens a team selection dropdown, followed by a modal to input their in-game UID and server. On submission, their data is stored in MySQL and they receive a configurable role.

### Ticketing

A full support ticket system with four categories: League Ops, Technical, Creatives, and General. Tickets are created as private channels with role-based visibility. The system supports claiming (restricted by role), adding and removing users, moving between categories, HTML transcript generation on close, a 5-star rating system sent via DM, and automatic 24-hour and 48-hour escalation reminders for unclaimed tickets.

### Discohook Embeds

Send, edit, and download embeds using Discohook links. Supports both short and long links, scheduled sending with a date/time parameter, and logging of all embed activity to a designated channel. Scheduled embeds persist across bot restarts via MySQL.

### Auto-create Threads

Bulk create private threads in the current channel. Users provide a name prefix, count (up to 50), and optional member IDs. The bot creates each thread, invites the specified members, and posts a numbered list of thread links. Includes rate-limit handling with automatic retry.

### Auto-create Voice Channels

Designate a voice channel as a trigger. When a user joins it, the bot creates a new voice channel named after the user, moves them into it, and grants them manage permissions. When the last person leaves a spawned channel, it is automatically deleted. Configuration persists across restarts.

### Team Management

Admins can bulk-add teams via a modal (one name per line), remove teams via a dropdown, and list all registered teams. The `/mention` command lets any user select a team and ping all verified members belonging to it.

### Dynamic Help

The `/help` command generates an embed listing all available commands grouped by feature. Administrators see the full command list. Non-admin users see only the commands they have permission to use.

## Requirements

- Python 3.10 or later
- Docker and Docker Compose (for MySQL)
- A Discord bot token with the following privileged intents enabled:
  - Message Content
  - Server Members
  - Voice States (for auto-create VC)

## Project Structure

```
oppo-hlc-discord-bot/
├── main.py                  # Bot entrypoint, cog loader, DB init
├── docker-compose.yml       # MySQL 8 container with auto-schema init
├── requirements.txt         # Python dependencies
├── .env.example             # Environment variable template
├── .gitignore
├── db/
│   ├── __init__.py
│   ├── database.py          # Async MySQL connection pool (aiomysql)
│   └── schema.sql           # Full DDL, auto-loaded by Docker
├── cogs/
│   ├── __init__.py
│   ├── verification.py      # Verification panel and flow
│   ├── tickets.py           # Ticketing system
│   ├── embeds.py            # Discohook embed management
│   ├── threads.py           # Bulk private thread creation
│   ├── voice.py             # Auto-create voice channels
│   ├── teams.py             # Team CRUD and /mention
│   └── help.py              # Dynamic help command
└── utils/
    ├── __init__.py
    ├── constants.py          # Centralized IDs, categories, timezone
    └── views.py              # Shared UI components
```

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/Aedwon/oppo-hlc-2026-bot.git
   cd oppo-hlc-2026-bot
   ```

2. **Create your environment file**

   ```bash
   cp .env.example .env
   ```

   Open `.env` and fill in your bot token, role IDs, and channel IDs. See the [Environment Variables](#environment-variables) section for details.

3. **Start MySQL**

   ```bash
   docker compose up -d
   ```

   This starts a MySQL 8 container and automatically runs `db/schema.sql` to create all tables.

4. **Install Python dependencies**

   ```bash
   pip install -r requirements.txt
   ```

5. **Run the bot**

   ```bash
   python main.py
   ```

   On first launch, the bot will sync slash commands with Discord. This may take a few minutes to propagate.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes | Your Discord bot token |
| `COMMAND_PREFIX` | No | Prefix for legacy commands (default: `^`) |
| `DB_HOST` | No | MySQL host (default: `localhost`) |
| `DB_PORT` | No | MySQL port (default: `3306`) |
| `DB_USER` | No | MySQL user (default: `oppo_bot`) |
| `DB_PASSWORD` | Yes | MySQL password |
| `DB_NAME` | No | MySQL database name (default: `oppo_hlc_bot`) |
| `ROLE_LEAGUE_OPS` | Yes | Role ID for League Ops ticket category |
| `ROLE_TECHNICAL` | Yes | Role ID for Technical ticket category |
| `ROLE_CREATIVES` | Yes | Role ID for Creatives ticket category |
| `ROLE_GENERAL` | Yes | Role ID for General ticket category |
| `SUPPORT_ROLE_ID` | Yes | General support role ID (visible in all tickets) |
| `TICKET_LOG_CHANNEL_ID` | Yes | Channel ID for ticket close logs and transcripts |
| `EMBED_LOG_CHANNEL_ID` | Yes | Channel ID for embed send/schedule logs |
| `TICKET_PANEL_CHANNEL_ID` | No | Optional, panels are sent via `/setup_tickets` |

## Commands

### Verification (Admin)

| Command | Description |
|---------|-------------|
| `/setup_verification <channel>` | Send the verification panel to a channel |
| `/set_verification_role <role>` | Set the role granted on successful verification |

### Ticketing (Admin)

| Command | Description |
|---------|-------------|
| `/setup_tickets [channel]` | Send the ticket creation panel |
| `/ticket_test <enabled>` | Toggle test mode for the current ticket (ratings not recorded) |

### Embeds

| Command | Description |
|---------|-------------|
| `/send_embed <channel> [link] [long_link] [schedule_for]` | Send an embed from a Discohook link, optionally at a scheduled time |
| `/edit_embed <message_link> [link] [long_link]` | Edit an existing bot or webhook message using a Discohook link |
| `/dl_embed <message_link>` | Generate a Discohook link from an existing Discord message |
| `/cancel_scheduled_embed` | Cancel a pending scheduled embed via dropdown |

### Threads

| Command | Description |
|---------|-------------|
| `/create_threads` | Opens a modal to create multiple private threads with a name prefix, count, and optional members |

### Voice (Admin)

| Command | Description |
|---------|-------------|
| `/setup_autocreate_vc <channel>` | Designate a voice channel as an auto-create trigger |
| `/remove_autocreate_vc <channel>` | Remove a voice channel from auto-create triggers |

### Teams (Admin, except /mention)

| Command | Description |
|---------|-------------|
| `/add_teams` | Opens a modal to bulk-add team names (one per line) |
| `/remove_team` | Remove a team via dropdown selection |
| `/list_teams` | List all registered teams |
| `/mention` | Select a team and mention all its verified members |

### Help

| Command | Description |
|---------|-------------|
| `/help` | Show available commands (filtered by user permissions) |

## Database Schema

The bot uses 8 MySQL tables, all created automatically on first Docker Compose startup:

| Table | Purpose |
|-------|---------|
| `guild_config` | Key-value configuration per guild (e.g., verification role ID) |
| `teams` | Registered team names per guild |
| `verified_users` | Verified users with team, UID, and server |
| `active_tickets` | Currently open tickets with claim state and metadata |
| `ticket_ratings` | Post-close star ratings and remarks |
| `scheduled_embeds` | Pending scheduled embeds with send time |
| `autocreate_vc_config` | Trigger voice channels per guild |
| `spawned_vcs` | Currently active auto-created voice channels |

The full DDL is in [`db/schema.sql`](db/schema.sql).

## Architecture

### Cog-based Design

Each feature is self-contained in its own cog under `cogs/`. Cogs are loaded automatically by `main.py` on startup. This keeps the codebase modular and allows features to be enabled or disabled independently.

### Database Layer

`db/database.py` provides a singleton async connection pool using `aiomysql`. It exposes class methods for common operations: `execute`, `fetchone`, `fetchall`, `fetchval`, and `executemany`. All queries use parameterized arguments to prevent SQL injection.

### Persistent Views

Discord UI components (buttons, select menus) that need to survive bot restarts use `custom_id` parameters and are re-registered via `bot.add_view()` in each cog's `cog_load` method. This applies to the verification button, ticket creation button, and ticket action buttons.

### Rate Limit Handling

Thread creation and channel rename operations include delays and retry logic to respect Discord API rate limits. The ticket reminder background task runs every 10 minutes to avoid excessive API calls.

## License

This project is not licensed for public use. All rights reserved.
