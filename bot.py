import hashlib
import logging
import os
import secrets
import time

import aiosqlite
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import ui as bigv_ui
from database import (
    delete_expired_verifications,
    delete_pending_verification,
    get_guild_settings,
    get_pending_verification,
    get_pending_verifications,
    increment_verification_attempts,
    save_guild_settings,
    save_pending_verification,
    setup_database,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("bigv")

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")


intents = discord.Intents.default()


class VerifierClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.verification_channels = set()

    async def setup_hook(self):
        await setup_database()
        await bigv_ui.load_application_emojis(self)
        self.add_view(VerifyView())

        commands = await self.tree.sync()
        repair_configurations.start()
        cleanup_expired_verifications.start()
        logger.info("Synced %s global command(s).", len(commands))


client = VerifierClient()


@client.tree.command(
    name="ping",
    description="Check whether BigV is online."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"{bigv_ui.emoji('success')} Pong - BigV is online.",
        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
    )


@client.tree.command(
    name="help",
    description="Learn how to set up and use BigV verification."
)
async def help_command(interaction: discord.Interaction):
    logo_file = bigv_ui.brand_logo_file()
    embed = bigv_ui.help_embed(interaction.guild, logo_file)
    if logo_file is None:
        await interaction.response.send_message(
            embed=embed,
            ephemeral=interaction.guild is not None,
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    await interaction.response.send_message(
        embed=embed,
        file=logo_file,
        ephemeral=interaction.guild is not None,
        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
    )


@tasks.loop(hours=1)
async def cleanup_expired_verifications():
    current_time = int(time.time())
    await delete_expired_verifications(current_time)
    logger.info("Cleaned expired verification challenges.")


class VerifyActionRow(discord.ui.ActionRow):
    def __init__(self):
        super().__init__()
        self.verify_button.emoji = bigv_ui.emoji("verify")

    @discord.ui.button(
        label="Send verification code",
        style=discord.ButtonStyle.primary,
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
        try:
            await interaction.user.send(
                embed=bigv_ui.verification_dm_embed(
                    interaction.guild,
                    code,
                    expires_at,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
        except discord.Forbidden:
            logger.warning(
                "Could not send verification DM for guild %s user %s",
                guild_id,
                user_id
            )
            await delete_pending_verification(guild_id, user_id)
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "warning",
                    "I couldn't reach your DMs",
                    "Enable direct messages for this server, then press **Verify** again.",
                    interaction.guild,
                ),
                ephemeral=True,
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
        await interaction.response.send_message(
            f"{bigv_ui.emoji('success')} Code sent - check your DMs.",
            ephemeral=True,
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )


class VerifyView(discord.ui.LayoutView):
    def __init__(self, guild=None, role=None, logo_file=None):
        super().__init__(timeout=None)
        action_row = VerifyActionRow()
        self.add_item(
            bigv_ui.verification_panel(
                guild,
                role,
                action_row,
                logo_file,
            )
        )


async def send_verification_panel(guild, channel, role):
    logo_file = bigv_ui.brand_logo_file()
    view = VerifyView(guild, role, logo_file)
    if logo_file is None:
        return await channel.send(
            view=view,
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
    return await channel.send(
        view=view,
        file=logo_file,
        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
    )
            
@client.tree.command(
    name="setup",
    description="Configure BigV verification for this server."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction
):
    guild_id=interaction.guild.id
    guild = interaction.guild
    settings = await get_guild_settings(guild_id)
    role = None
    verification_channel = None
    if  settings is None:
        ## Error Handling For permissions 
        bot_member = guild.me
        if not bot_member.guild_permissions.manage_roles:
            logger.warning(
                "Setup blocked for guild %s: BigV lacks Manage Roles",
                guild_id
            )
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "warning",
                    "Manage Roles is required",
                    "Give BigV the **Manage Roles** permission, then run `/setup` again.",
                    guild,
                ),
                ephemeral=True,
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
        
        if not bot_member.guild_permissions.manage_channels:
            logger.warning(
                "Setup blocked for guild %s: BigV lacks Manage Channels",
                guild_id
            )
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "warning",
                    "Manage Channels is required",
                    "Give BigV the **Manage Channels** permission, then run `/setup` again.",
                    guild,
                ),
                ephemeral=True,
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
        if not bot_member.guild_permissions.manage_messages:
            logger.warning(
                "Setup blocked for guild %s: BigV lacks Manage Messages",
                guild_id
            )
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "warning",
                    "Manage Messages is required",
                    "Give BigV the **Manage Messages** permission, then run `/setup` again.",
                    guild,
                ),
                ephemeral=True,
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        try:
            
            role = await guild.create_role(
                    name="Verified",
                    permissions=discord.Permissions.none(),
                    reason=f"BigV setup requested by {interaction.user}"
                )
            if not role.is_assignable():
                logger.warning(
                    "Setup blocked for guild %s: Verified role is not assignable",
                    guild_id
                )
                await role.delete(
                    reason="BigV setup rollback"
                )
                await interaction.response.send_message(
                    embed=bigv_ui.status_embed(
                        "warning",
                        "The Verified role is out of reach",
                        "Move BigV's highest role above **Verified**, then run `/setup` again.",
                        guild,
                    ),
                    ephemeral=True,
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                    )
                return
            
            verification_channel = await guild.create_text_channel(
                "bigv-verification",
                reason=f"BigV setup requested by {interaction.user}"
            )
            await lock_verification_channel(guild, verification_channel)
            verification_message = await send_verification_panel(
                guild,
                verification_channel,
                role,
            )
            client.verification_channels.add(verification_channel.id)
            await  save_guild_settings(
                guild_id,
                role.id,
                verification_channel.id,
                verification_message.id
            )
        except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
            logger.exception(
                "Setup failed for guild %s; rolling back",
                guild_id
            )
            if verification_channel is not None:
                client.verification_channels.discard(verification_channel.id)
                try:
                    await verification_channel.delete(
                        reason="BigV setup rollback"
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Failed to delete channel %s during setup rollback for guild %s",
                        verification_channel.id,
                        guild_id
                    )
                    pass
            if role is not None:
                try:
                    await role.delete(
                        reason="BigV setup rollback"
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Failed to delete role %s during setup rollback for guild %s",
                        role.id,
                        guild_id
                    )
                    pass
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "error",
                    "Setup couldn't be completed",
                    "Check BigV's server permissions and role position, then run `/setup` again.",
                    guild,
                ),
                ephemeral=True,
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
            

        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "success",
                "BigV is ready",
                (
                    f"{bigv_ui.emoji('channel')} {verification_channel.mention} is locked and ready.\n"
                    f"{bigv_ui.emoji('role')} Members verify there to receive {role.mention}."
                ),
                guild,
            ),
            ephemeral=True,
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
    else:
        verification_channel = guild.get_channel(settings["verified_channel_id"])
        channel_text = (
            verification_channel.mention
            if verification_channel is not None
            else "the verification channel"
        )
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "neutral",
                "BigV is already configured",
                (
                    f"{bigv_ui.emoji('channel')} Members can use {channel_text} to "
                    f"verify in **{bigv_ui.guild_name(guild)}**."
                ),
                guild,
            ),
            ephemeral=True,
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )

@setup.error
async def setup_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        logger.warning(
            "Setup denied for user %s in guild %s: administrator permission missing",
            interaction.user.id,
            interaction.guild_id
        )
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "warning",
                "Administrator permission is required",
                "Ask a server administrator to run `/setup` for BigV.",
                interaction.guild,
            ),
            ephemeral=True,
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    raise error

@client.tree.command(
    name="verify",
    description="Submit your BigV verification code."
)
@app_commands.describe(code="The private six-digit code BigV sent by DM.")
@app_commands.dm_only()
async def verify(
    interaction: discord.Interaction,
    code: str
):
    user_id = interaction.user.id
    pending = await get_pending_verifications(user_id)
    if not pending:
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "neutral",
                "No verification is waiting",
                "Return to the server's verification channel and press **Verify** first.",
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
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
        if len(pending) == 1 :
            guild_id = pending[0]['guild_id']
            await increment_verification_attempts(guild_id, user_id)
            verification = await get_pending_verification(
            guild_id,
            user_id
            )
            if verification['attempts'] >= 5:
                await delete_pending_verification(guild_id,user_id)
                await interaction.response.send_message(
                    embed=bigv_ui.status_embed(
                        "warning",
                        "Too many incorrect attempts",
                        "This request was cancelled. Return to the server and press **Verify** for a new code.",
                    ),
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                )
                return
            remaining = 5 - verification["attempts"]
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "warning",
                    "That code isn't correct",
                    f"Check the latest BigV DM and try again. **{remaining} of 5 attempts remain.**",
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
        else:
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "warning",
                    "That code doesn't match",
                    "You have requests from multiple servers. Use the code from the DM for the server you want to join.",
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
    if int(time.time()) > matched_verification['expires_at']:
        await delete_pending_verification(matched_verification["guild_id"],user_id)
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "warning",
                "This code has expired",
                "Return to the server's verification channel and press **Verify** for a new code.",
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    guild_id = matched_verification["guild_id"]

    guild = client.get_guild(guild_id)
    if guild is None:
        logger.warning(
            "Verification guild %s is unavailable for user %s",
            guild_id,
            user_id
        )
        await delete_pending_verification(guild_id, user_id)
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "error",
                "That server is unavailable",
                "BigV can no longer access the server for this request. Start again from a server where BigV is active.",
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return

    try:
        await repair_guild_setup(guild)
        settings = await get_guild_settings(guild_id)
    except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
        logger.exception(
            "Failed to prepare guild %s for verification by user %s",
            guild_id,
            user_id
        )
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "error",
                "The server isn't ready yet",
                "BigV couldn't prepare the verification setup. Your code is still valid, so try again shortly.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return

    role_id = settings["verified_role_id"]

    role = guild.get_role(role_id)
    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        logger.warning(
            "Verification user %s is not a member of guild %s",
            user_id,
            guild_id
        )
        await delete_pending_verification(guild_id, user_id)
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "warning",
                "You're no longer in that server",
                f"Rejoin **{bigv_ui.guild_name(guild)}**, then start verification again.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return

    if role in member.roles:
        await delete_pending_verification(guild_id, user_id)
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "success",
                "You're already verified",
                f"You already have access to **{bigv_ui.guild_name(guild)}**.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    if not role.is_assignable():
        logger.warning(
            "Verification role %s is not assignable in guild %s",
            role.id,
            guild_id
        )
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "warning",
                "The Verified role is out of reach",
                "A server administrator needs to move BigV's highest role above **Verified**. Your code remains valid.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return

    try:
        await member.add_roles(
        role,
        reason="BigV verification completed"
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Failed to assign verification role %s in guild %s to user %s",
            role.id,
            guild_id,
            user_id
        )
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "error",
                "The role couldn't be assigned",
                f"BigV couldn't finish verification in **{bigv_ui.guild_name(guild)}**. Your code remains valid; try again shortly.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    await delete_pending_verification(guild_id,user_id)
    await interaction.response.send_message(
        embed=bigv_ui.status_embed(
            "success",
            "Verification complete",
            f"You now have verified access to **{bigv_ui.guild_name(guild)}**.",
            guild,
        ),
        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
    )


## Repair Role
async def repair_guild_setup(guild):
    settings = await get_guild_settings(guild.id)
    
    if settings is None:
        return
    message_id = settings["verification_message_id"]
    role = guild.get_role(settings["verified_role_id"])

    if role is None:
        role = await guild.create_role(
                name="Verified",
                permissions=discord.Permissions.none(),
                reason="BigV Auto repair"
            )
    channel = guild.get_channel(
    settings["verified_channel_id"]
    ) 
    if channel is None:
        channel = await guild.create_text_channel(
        "bigv-verification",
        reason="BigV auto repair: verification channel missing"
        )
        message_id = None
    await lock_verification_channel(guild, channel)
    if message_id is None:
        verification_message = await send_verification_panel(
            guild,
            channel,
            role,
        )
    else:
        try:
            verification_message = await channel.fetch_message(
            message_id
            )
        except discord.NotFound:
            verification_message = await send_verification_panel(
                guild,
                channel,
                role,
            )
    client.verification_channels.add(channel.id)
    await  save_guild_settings(
        guild.id,
        role.id,
        channel.id,
        verification_message.id
    )


##Verification Message Deletion detecter
@client.event
async def on_raw_message_delete(payload):
    if payload.guild_id is None:
        return
    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return
    try:
        settings = await get_guild_settings(payload.guild_id)
        if settings is None:
            return
        if payload.message_id != settings["verification_message_id"]:
            return
        await repair_guild_setup(guild)
    except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
        logger.exception(
            "Failed to repair verification message after deletion for guild %s",
            guild.id
        )
        pass


@client.event
async def on_raw_bulk_message_delete(payload):
    if payload.guild_id is None:
        return
    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return
    try:
        settings = await get_guild_settings(payload.guild_id)
        if settings is None:
            return
        if settings["verification_message_id"] not in payload.message_ids:
            return
        await repair_guild_setup(guild)
    except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
        logger.exception(
            "Failed to repair verification message after bulk deletion for guild %s",
            guild.id
        )
        pass


## Role Deletion detection 
@client.event
async def on_guild_role_delete(role):
    try:
        settings = await get_guild_settings(role.guild.id)
        if settings is None:
            return
        if role.id == settings["verified_role_id"] :
            await repair_guild_setup(role.guild)
    except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
        logger.exception(
            "Failed to repair configuration after role deletion for guild %s",
            role.guild.id
        )
        pass

#delete anything else but bots message 
@client.event
async def on_message(message):
    if message.guild is None:
            return
    if message.author == client.user:
        return
    if message.channel.id not in client.verification_channels:
        return
    try:
        await message.delete()
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Failed to delete unauthorized message in verification channel %s for guild %s",
            message.channel.id,
            message.guild.id
        )
        pass
#missing event: detect channel deletion
@client.event
async def on_guild_channel_delete(channel):
    try:
        settings = await get_guild_settings(channel.guild.id)
        if not settings :
            return
        if channel.id != settings["verified_channel_id"]:
            return
        client.verification_channels.discard(channel.id)
        await repair_guild_setup(channel.guild)
    except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
        logger.exception(
            "Failed to repair configuration after channel deletion for guild %s",
            channel.guild.id
        )
        pass

#channel lock helper
async def lock_verification_channel(guild, channel):
    overwrite = channel.overwrites_for(
    guild.default_role)
    overwrite.view_channel = True
    overwrite.send_messages = False
    bot_overwrite = channel.overwrites_for(guild.me)
    bot_overwrite.view_channel = True
    bot_overwrite.send_messages = True
    bot_overwrite.embed_links = True
    bot_overwrite.read_message_history = True
    bot_overwrite.manage_messages = True
    await channel.set_permissions(
    guild.default_role,
    overwrite=overwrite,
    reason="BigV verification channel lockdown"
)
    await channel.set_permissions(
    guild.me,
    overwrite=bot_overwrite,
    reason="BigV verification channel lockdown"
)

@tasks.loop(minutes=5)
async def repair_configurations():
    for guild in client.guilds:
        try:
            await repair_guild_setup(guild)
        except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
            logger.exception(
                "Periodic configuration repair failed for guild %s",
                guild.id
            )
            continue
@repair_configurations.before_loop
async def before_repair_configurations():
    await client.wait_until_ready()
@client.event
async def on_ready():
    logger.info("Logged in as %s", client.user)


logger.info("Starting BigV")
client.run(TOKEN)
