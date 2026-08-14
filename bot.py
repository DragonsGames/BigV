import os

import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")


intents = discord.Intents.default()


class VerifierClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        commands = await self.tree.sync()
        print(f"Synced {len(commands)} global command(s).")


client = VerifierClient()


@client.tree.command(
    name="ping",
    description="Check whether the verifier bot is online."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

@client.tree.command(
    name="hello",
    description="says Hello , user."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello, {interaction.user.display_name}")


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


client.run(TOKEN)