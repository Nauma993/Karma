
import os
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured!")

intents = discord.Intents.default()

class KarmaBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Karma commands synchronized!")

    async def on_ready(self):
        print(f"Karma is online as {self.user}")

bot = KarmaBot()

@bot.tree.command(name="ping", description="Check if Karma is online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Karma is online! Everything works. ✅"
    )

bot.run(TOKEN)
