import os
import hashlib
import secrets
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

from database import (
    setup_database,
    save_guild_settings,
    get_guild_settings,
    save_pending_verification,
    get_pending_verifications,
    delete_pending_verification,
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
            number = secrets.randbelow(1_000_000)
            code = f"{number:06d}"
            code_hash = hashlib.sha256(
            code.encode()
            ).hexdigest()
            expires_at = int(time.time()) + 600
            guild_id = interaction.guild.id
            user_id = interaction.user.id
            await save_pending_verification(
                guild_id,
                user_id,
                code_hash,
                expires_at,
            )
            await interaction.user.send(
                "BigV Verification\n\n\n"
                "Your verification code is:   "
                f"||{code}||\n\n"
                "It expires in 10 minutes.\n\n"
                "Use:"
                f"**   /verify ||{code}||**\n\n"
            )
            await interaction.response.send_message(
                "Verification code sent. Check your DMs.",
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
        ## Error Handling For permissions 
        bot_member = guild.me
        channel_permissions = verification_channel.permissions_for(bot_member)
        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
            "BigV doesn't have Manage Roles permission !",
            ephemeral=True
            )
            return
        
        if not channel_permissions.view_channel:
            await interaction.response.send_message(
            "BigV cant view that channel ! ❌",
            ephemeral=True
            )
            return
        if not channel_permissions.send_messages:
            await interaction.response.send_message(
            "BigV cant send messages in that channel !❌",
            ephemeral=True
            )
            return
        if not channel_permissions.embed_links:
            await interaction.response.send_message(
            "BigV cant Embed messages in that channel !❌",
            ephemeral=True
            )
            return


        
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

@setup.error
async def setup_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator permission to configure BigV.",ephemeral=True)
        return
    raise error

@client.tree.command(
    name="verify",
    description="Submit your BigV verification code."
)
@app_commands.dm_only()
async def verify(
    interaction: discord.Interaction,
    code: str
):
    user_id = interaction.user.id
    pending = await get_pending_verifications(user_id)
    if not pending:
        await interaction.response.send_message(
            "You don't have any pending verification requests."
        )
        return
    submitted_hash = hashlib.sha256(
    code.encode()
    ).hexdigest()
    matched_verification = None
    for verification in pending:
        if verification["code_hash"] == submitted_hash:
            matched_verification = verification
            break
    if matched_verification is None:
        await interaction.response.send_message(
                    "Invalid Code."
                )
        return
    if int(time.time()) > matched_verification['expires_at']:
        await interaction.response.send_message(
            "This verification code has expired.\n"
            "Click Verify again to get a new one."
        )
        return
    guild_id = matched_verification["guild_id"]

    guild = client.get_guild(guild_id)

    settings = await get_guild_settings(guild_id)

    role_id = settings["verified_role_id"]

    role = guild.get_role(role_id)

    member = await guild.fetch_member(user_id)

    await member.add_roles(
    role,
    reason="BigV verification completed"
    )
    await delete_pending_verification(guild_id,user_id)
    await interaction.response.send_message(
    f"Verification successful ✅\n"
    f"You are now verified in **{guild.name}**."
    )

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


client.run(TOKEN)