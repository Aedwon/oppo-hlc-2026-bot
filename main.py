"""
OPPO HLC Discord Bot — Main Entrypoint
"""
import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from db.database import Database

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("COMMAND_PREFIX", "^")

# -------------------------------------------------------------------
# Intents — enable everything the bot needs
# -------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# -------------------------------------------------------------------
# Cog list — loaded in order (dependencies first)
# -------------------------------------------------------------------
COGS = [
    "cogs.verification",
    "cogs.tickets",
    "cogs.embeds",
    "cogs.threads",
    "cogs.voice",
    "cogs.teams",
    "cogs.help",
]


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Guilds: {len(bot.guilds)}")

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"   Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"   ⚠️ Failed to sync commands: {e}")


async def setup_hook():
    """Called before the bot connects — initialise DB and load cogs."""
    # Database pool
    await Database.create_pool()
    print("✅ Database pool initialised.")

    # Load cogs
    for cog in COGS:
        try:
            await bot.load_extension(cog)
            print(f"   ✅ Loaded {cog}")
        except Exception as e:
            print(f"   ❌ Failed to load {cog}: {e}")


bot.setup_hook = setup_hook


async def main():
    async with bot:
        try:
            await bot.start(TOKEN)
        finally:
            await Database.close()
            print("🔌 Database pool closed.")


if __name__ == "__main__":
    asyncio.run(main())
