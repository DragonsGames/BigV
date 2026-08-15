import os

import discord
from discord import app_commands
from dotenv import load_dotenv

from database import (
    setup_database,
    save_guild_settings,
    get_guild_settings,
)
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
        await setup_database()

        commands = await self.tree.sync()
        self.add_view(VerifyView())
        print(f"Synced {len(commands)} global command(s).")


client = VerifierClient()


@client.tree.command(
    name="ping",
    description="Check whether BigV is online."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")
class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(
    label="Verify",
    emoji="✅",
    style=discord.ButtonStyle.green,
    custom_id="bigv_verify"
    )
    async def verify_button(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button
        ):
            await interaction.response.send_message(
                f"Verification requested\n"
                f"User : {interaction.user.mention}\n"
                f"Server: {interaction.guild.name}\n",
                ephemeral=True
            )
@client.tree.command(
    name="setup",
    description="setup server settings."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    verification_channel="Choose the verification channel",
)
async def setup(
    interaction: discord.Interaction,
    verification_channel: discord.TextChannel,
):
    guild_id=interaction.guild.id
    guild = interaction.guild
    settings = await get_guild_settings(guild_id)
    if  settings is None:
        role = await guild.create_role(
                name="Verified",
                permissions=discord.Permissions.none(),
                reason=f"BigV setup requested by {interaction.user}"
            )
        verification_message = await verification_channel.send(
            "Click below to begin verification.",
            view=VerifyView()
        )
        await  save_guild_settings(
            guild_id,
            role.id,
            verification_channel.id,
            verification_message.id
        )
        await interaction.response.send_message(
            f"Created Role:{role.mention}\n"
            f"Verification Channel :{verification_channel.mention}\n"
            f"Verification message ID : {verification_message.id}\n",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "BigV is already configured in this server.",
            ephemeral=True
        )

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


client.run(TOKEN)