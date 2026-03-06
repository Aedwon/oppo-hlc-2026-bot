"""
Centralised logging helper.

Every cog uses `get_log_channel()` to resolve the correct Discord channel
for a given log type.  Channel IDs are persisted in ``guild_config``
via the Database helper, so they survive bot restarts.

Log types:
  commands  – all slash-command invocations
  tickets   – ticket transcripts and lifecycle
  embeds    – embed send / schedule / edit operations
"""
from __future__ import annotations

import discord
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext.commands import Bot

from db.database import Database

# DB config key convention: log_channel_{type}
_CONFIG_KEY = "log_channel_{}"

LOG_TYPES = ("commands", "tickets", "embeds")


async def get_log_channel(
    bot: "Bot",
    guild_id: int,
    log_type: str,
) -> Optional[discord.TextChannel]:
    """Return the configured log channel for *log_type*, or ``None``."""
    key = _CONFIG_KEY.format(log_type)
    channel_id_str = await Database.get_config(guild_id, key)
    if not channel_id_str:
        return None

    channel = bot.get_channel(int(channel_id_str))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id_str))
        except Exception:
            return None

    return channel


async def set_log_channel(
    guild_id: int,
    log_type: str,
    channel_id: int,
) -> None:
    """Persist a log channel for *log_type*."""
    key = _CONFIG_KEY.format(log_type)
    await Database.set_config(guild_id, key, str(channel_id))


async def get_all_log_channels(
    bot: "Bot",
    guild_id: int,
) -> dict[str, Optional[discord.TextChannel]]:
    """Return a dict of log_type → channel (or None) for all types."""
    result = {}
    for lt in LOG_TYPES:
        result[lt] = await get_log_channel(bot, guild_id, lt)
    return result
