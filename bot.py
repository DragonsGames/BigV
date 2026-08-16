"""BigV's Discord client, commands, verification flow, and repair tasks.

Database operations and Discord presentation helpers live in ``database.py``
and ``ui.py`` so this module can focus on coordinating the bot's behavior.
"""

import asyncio
import hashlib
import io
import logging
import os
import random
import secrets
import time
from typing import Literal, cast

import aiosqlite
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import ui as bigv_ui
from database import (
    clear_guild_logging_category,
    clear_guild_logging_channel,
    delete_expired_verifications,
    delete_guild_setup,
    delete_pending_verification,
    get_guild_logging,
    get_guild_settings,
    get_pending_verification,
    get_pending_verifications,
    increment_verification_attempts,
    save_guild_logging,
    save_guild_settings,
    save_pending_verification,
    set_guild_logging_enabled,
    setup_database,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("bigv")

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")


intents = discord.Intents.default()


# Client startup and persistent state
class VerifierClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.verification_channels = set()
        self.guild_lifecycle_locks = {}
        self.unsetup_guilds = set()

    async def setup_hook(self):
        """Prepare storage, UI assets, commands, views, and background tasks."""
        await setup_database()
        await bigv_ui.load_application_emojis(self)
        self.add_view(VerifyView())

        commands = await self.tree.sync()
        repair_configurations.start()
        cleanup_expired_verifications.start()
        logger.info("Synced %s global command(s).", len(commands))


client = VerifierClient()


STAFF_PERMISSION_NAMES = (
    "administrator",
    "manage_guild",
    "manage_channels",
    "manage_messages",
    "moderate_members",
    "kick_members",
    "ban_members",
)


def guild_lifecycle_lock(guild_id):
    """Return the shared lock that serializes resource changes for one guild."""
    lock = client.guild_lifecycle_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        client.guild_lifecycle_locks[guild_id] = lock
    return lock


def has_staff_permissions(role):
    """Identify staff roles by Discord permissions rather than role names."""
    permissions = role.permissions
    return any(getattr(permissions, name, False) for name in STAFF_PERMISSION_NAMES)


def interaction_user_is_administrator(interaction):
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


# General slash commands
@client.tree.command(name="ping", description="Check whether BigV is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"{bigv_ui.emoji('success')} Pong - BigV is online.",
        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
    )


@client.tree.command(
    name="help", description="Learn how to set up and use BigV verification."
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
    """Remove expired challenges without stopping the task on DB failure."""
    current_time = int(time.time())
    try:
        await delete_expired_verifications(current_time)
    except aiosqlite.Error:
        logger.exception("Expired verification cleanup failed")
        return
    logger.info("Cleaned expired verification challenges.")


# Verification panel and private challenge delivery
class VerifyActionRow(discord.ui.ActionRow):
    def __init__(self):
        super().__init__()
        self.verify_button.emoji = bigv_ui.emoji("verify")

    @discord.ui.button(
        label="Send verification code",
        style=discord.ButtonStyle.primary,
        custom_id="bigv_verify",
    )
    async def verify_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Create a hashed challenge and DM its CAPTCHA image to the member."""
        guild = interaction.guild
        if guild is None:
            return
        number = secrets.randbelow(1_000_000)
        code = f"{number:06d}"
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        expires_at = int(time.time()) + 600
        guild_id = guild.id
        user_id = interaction.user.id
        try:
            await save_pending_verification(
                guild_id,
                user_id,
                code_hash,
                expires_at,
            )
        except aiosqlite.Error:
            logger.exception(
                "Failed to save pending verification for guild %s user %s",
                guild_id,
                user_id,
            )
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "error",
                    "Verification data is unavailable",
                    "BigV couldn't access its verification data. Please try again shortly.",
                    guild,
                ),
                ephemeral=True,
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
        image_buffer = build_verification_image(code)
        captcha_file = discord.File(fp=image_buffer, filename="bigv_verification.png")
        try:
            await interaction.user.send(
                embed=bigv_ui.verification_dm_embed(
                    guild,
                    expires_at,
                ),
                file=captcha_file,
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
        except discord.Forbidden:
            logger.warning(
                "Could not send verification DM for guild %s user %s", guild_id, user_id
            )
            await delete_pending_verification(guild_id, user_id)
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "warning",
                    "I couldn't reach your DMs",
                    "Enable direct messages for this server, then press **Verify** again.",
                    guild,
                ),
                ephemeral=True,
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
        except discord.HTTPException:
            logger.exception(
                "Discord failed to deliver verification DM for guild %s user %s",
                guild_id,
                user_id,
            )
            await delete_pending_verification(guild_id, user_id)
            await interaction.response.send_message(
                embed=bigv_ui.status_embed(
                    "error",
                    "The code couldn't be delivered",
                    "Discord couldn't deliver your verification DM. Please try again shortly.",
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
    """Post the persistent verification panel with the logo when available."""
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


async def send_guild_log(guild, title, description, channel_id_override=None):
    """Send a best-effort audit embed without affecting the primary operation."""
    channel_id = channel_id_override
    if channel_id is None:
        try:
            settings = await get_guild_logging(guild.id)
        except aiosqlite.Error:
            logger.exception(
                "Failed to load audit logging settings for guild %s", guild.id
            )
            return False

        if settings is None or not settings["enabled"]:
            return False
        channel_id = settings["log_channel_id"]

    channel = guild.get_channel(channel_id) if channel_id is not None else None
    if channel is None:
        try:
            await clear_guild_logging_channel(guild.id)
        except aiosqlite.Error:
            logger.exception(
                "Failed to clear stale audit channel for guild %s", guild.id
            )
        return False

    embed = bigv_ui.status_embed("brand", title, description, guild)
    embed.timestamp = discord.utils.utcnow()
    try:
        await channel.send(
            embed=embed,
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
    except discord.NotFound:
        try:
            await clear_guild_logging_channel(guild.id)
        except aiosqlite.Error:
            logger.exception(
                "Failed to clear stale audit channel for guild %s", guild.id
            )
        return False
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to send audit message for guild %s", guild.id)
        return False
    return True


# Server setup
@client.tree.command(
    name="setup", description="Configure BigV verification for this server."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    """Create, lock, and persist a guild's verification resources."""
    guild = interaction.guild
    if guild is None:
        return
    await interaction.response.defer(ephemeral=True)
    guild_id = guild.id
    async with guild_lifecycle_lock(guild_id):
        try:
            settings = await get_guild_settings(guild_id)
        except aiosqlite.Error:
            logger.exception("Failed to load setup settings for guild %s", guild_id)
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "error",
                    "Verification data is unavailable",
                    "BigV couldn't access its verification data. Please try again shortly.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        if settings is not None:
            try:
                await _repair_guild_setup(guild)
                settings = await get_guild_settings(guild_id)
            except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
                logger.exception(
                    "Failed to repair existing setup for guild %s", guild_id
                )
                await interaction.edit_original_response(
                    embed=bigv_ui.status_embed(
                        "error",
                        "The existing setup couldn't be repaired",
                        "Check BigV's server permissions and role position, then try again.",
                        guild,
                    ),
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                )
                return

            if settings is None:
                await interaction.edit_original_response(
                    embed=bigv_ui.status_embed(
                        "error",
                        "The existing setup couldn't be loaded",
                        "BigV repaired the resources but could not reload their saved IDs.",
                        guild,
                    ),
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                )
                return
            verification_channel = guild.get_channel(settings["verified_channel_id"])
            channel_text = (
                verification_channel.mention
                if verification_channel is not None
                else "the verification channel"
            )
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "neutral",
                    "BigV is already configured",
                    (
                        f"{bigv_ui.emoji('channel')} Members can use {channel_text} to "
                        f"verify in **{bigv_ui.guild_name(guild)}**."
                    ),
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        # Setup cannot safely create or manage its resources without these permissions.
        bot_member = guild.me
        assert bot_member is not None
        required_permissions = (
            ("manage_roles", "Manage Roles"),
            ("manage_channels", "Manage Channels"),
            ("manage_messages", "Manage Messages"),
        )
        for permission_name, display_name in required_permissions:
            if getattr(bot_member.guild_permissions, permission_name):
                continue
            logger.warning(
                "Setup blocked for guild %s: BigV lacks %s", guild_id, display_name
            )
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "warning",
                    f"{display_name} is required",
                    f"Give BigV the **{display_name}** permission, then run `/setup` again.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        role = None
        verification_channel = None
        try:
            role = await guild.create_role(
                name="Verified",
                permissions=discord.Permissions.none(),
                hoist=True,
                reason=f"BigV setup requested by {interaction.user}",
            )
            if not role.is_assignable():
                logger.warning(
                    "Setup blocked for guild %s: Verified role is not assignable",
                    guild_id,
                )
                await role.delete(reason="BigV setup rollback")
                await interaction.edit_original_response(
                    embed=bigv_ui.status_embed(
                        "warning",
                        "The Verified role is out of reach",
                        "Move BigV's highest role above **Verified**, then run `/setup` again.",
                        guild,
                    ),
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                )
                return

            verification_channel = await guild.create_text_channel(
                "bigv-verification",
                reason=f"BigV setup requested by {interaction.user}",
            )
            await lock_verification_channel(guild, verification_channel, role)
            verification_message = await send_verification_panel(
                guild,
                verification_channel,
                role,
            )
            client.verification_channels.add(verification_channel.id)
            await save_guild_settings(
                guild_id, role.id, verification_channel.id, verification_message.id
            )
        except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
            logger.exception("Setup failed for guild %s; rolling back", guild_id)
            if verification_channel is not None:
                client.verification_channels.discard(verification_channel.id)
                try:
                    await verification_channel.delete(reason="BigV setup rollback")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Failed to delete channel %s during setup rollback for guild %s",
                        verification_channel.id,
                        guild_id,
                    )
            if role is not None:
                try:
                    await role.delete(reason="BigV setup rollback")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Failed to delete role %s during setup rollback for guild %s",
                        role.id,
                        guild_id,
                    )
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "error",
                    "Setup couldn't be completed",
                    "Check BigV's server permissions and role position, then run `/setup` again.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        await send_guild_log(
            guild,
            "Setup completed",
            f"{interaction.user} configured BigV with {role.mention} in {verification_channel.mention}.",
        )
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "success",
                "BigV is ready",
                (
                    f"{bigv_ui.emoji('channel')} {verification_channel.mention} is locked and ready.\n"
                    f"{bigv_ui.emoji('role')} Members verify there to receive {role.mention}."
                ),
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )


@setup.error
async def setup_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        logger.warning(
            "Setup denied for user %s in guild %s: administrator permission missing",
            interaction.user.id,
            interaction.guild_id,
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


async def send_admin_required_response(interaction):
    await interaction.edit_original_response(
        embed=bigv_ui.status_embed(
            "warning",
            "Administrator permission is required",
            "Only a server administrator can use this command.",
            interaction.guild,
        ),
        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
    )


async def send_guild_required_response(interaction):
    await interaction.response.send_message(
        embed=bigv_ui.status_embed(
            "warning",
            "Use this command in a server",
            "This administrator command is not available in direct messages.",
        ),
        ephemeral=True,
        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
    )


async def handle_admin_command_error(interaction, error):
    if isinstance(error, app_commands.NoPrivateMessage):
        await send_guild_required_response(interaction)
        return
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            embed=bigv_ui.status_embed(
                "warning",
                "Administrator permission is required",
                "Only a server administrator can use this command.",
                interaction.guild,
            ),
            ephemeral=True,
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    raise error


@client.tree.command(
    name="unsetup", description="Remove BigV verification from this server."
)
@app_commands.describe(confirm="Confirm removal of BigV verification resources.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def unsetup(interaction: discord.Interaction, confirm: bool):
    """Delete only the verification resources tracked for this guild."""
    guild = interaction.guild
    if guild is None:
        await send_guild_required_response(interaction)
        return
    await interaction.response.defer(ephemeral=True)
    if not interaction_user_is_administrator(interaction):
        await send_admin_required_response(interaction)
        return
    if not confirm:
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "neutral",
                "Confirmation is required",
                "Run `/unsetup confirm:true` to remove BigV verification from this server.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return

    guild_id = guild.id
    async with guild_lifecycle_lock(guild_id):
        try:
            settings = await get_guild_settings(guild_id)
        except aiosqlite.Error:
            logger.exception("Failed to load unsetup settings for guild %s", guild_id)
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "error",
                    "Verification data is unavailable",
                    "BigV couldn't inspect this server's setup. Nothing was intentionally removed.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        if settings is None:
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "neutral",
                    "BigV is not configured",
                    "There are no tracked verification resources to remove from this server.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        client.unsetup_guilds.add(guild_id)
        try:
            await send_guild_log(
                guild,
                "Unsetup started",
                f"{interaction.user} started removal of BigV verification resources.",
            )
            role = guild.get_role(settings["verified_role_id"])
            channel = cast(
                discord.TextChannel | None,
                guild.get_channel(settings["verified_channel_id"]),
            )
            message_cleanup_failed = False
            channel_cleanup_failed = False
            role_cleanup_failed = False

            if channel is not None and settings["verification_message_id"] is not None:
                try:
                    message = await channel.fetch_message(
                        settings["verification_message_id"]
                    )
                    await message.delete()
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    message_cleanup_failed = True
                    logger.exception(
                        "Failed to delete verification panel during unsetup for guild %s",
                        guild_id,
                    )

            if channel is not None:
                try:
                    await channel.delete(
                        reason=f"BigV unsetup requested by {interaction.user}"
                    )
                except discord.NotFound:
                    channel = None
                except (discord.Forbidden, discord.HTTPException):
                    channel_cleanup_failed = True
                    logger.exception(
                        "Failed to delete verification channel during unsetup for guild %s",
                        guild_id,
                    )
                else:
                    channel = None

            if role is not None:
                try:
                    await role.delete(
                        reason=f"BigV unsetup requested by {interaction.user}"
                    )
                except discord.NotFound:
                    role = None
                except (discord.Forbidden, discord.HTTPException):
                    role_cleanup_failed = True
                    logger.exception(
                        "Failed to delete verification role during unsetup for guild %s",
                        guild_id,
                    )

            if (
                channel_cleanup_failed
                or role_cleanup_failed
                or (message_cleanup_failed and channel is not None)
            ):
                await interaction.edit_original_response(
                    embed=bigv_ui.status_embed(
                        "error",
                        "Unsetup couldn't be completed",
                        "BigV could not remove every tracked resource. Check its permissions, then try again.",
                        guild,
                    ),
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                )
                return

            completion_log_channel_id = None
            try:
                logging_settings = await get_guild_logging(guild_id)
            except aiosqlite.Error:
                logger.exception(
                    "Failed to preserve completion audit destination for guild %s",
                    guild_id,
                )
            else:
                if logging_settings is not None and logging_settings["enabled"]:
                    completion_log_channel_id = logging_settings["log_channel_id"]
            try:
                await delete_guild_setup(guild_id)
            except aiosqlite.Error:
                logger.exception("Failed to finalize unsetup for guild %s", guild_id)
                await interaction.edit_original_response(
                    embed=bigv_ui.status_embed(
                        "error",
                        "Unsetup couldn't be finalized",
                        "The Discord resources were removed, but BigV could not update its saved configuration.",
                        guild,
                    ),
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                )
                return

            if completion_log_channel_id is not None:
                await send_guild_log(
                    guild,
                    "Unsetup completed",
                    f"{interaction.user} removed BigV verification from this server.",
                    channel_id_override=completion_log_channel_id,
                )
            client.verification_channels.discard(settings["verified_channel_id"])
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "success",
                    "BigV verification removed",
                    "The tracked panel, verification channel, role, and pending requests were removed.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
        finally:
            client.unsetup_guilds.discard(guild_id)


@unsetup.error
async def unsetup_error(interaction, error):
    await handle_admin_command_error(interaction, error)


@client.tree.command(
    name="forceverify", description="Give a member the configured Verified role."
)
@app_commands.describe(user="The member to verify without a CAPTCHA.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def forceverify(interaction: discord.Interaction, user: discord.Member):
    """Assign the tracked role and clear only this guild's pending challenge."""
    guild = interaction.guild
    if guild is None:
        await send_guild_required_response(interaction)
        return
    await interaction.response.defer(ephemeral=True)
    if not interaction_user_is_administrator(interaction):
        await send_admin_required_response(interaction)
        return

    guild_id = guild.id
    async with guild_lifecycle_lock(guild_id):
        try:
            settings = await get_guild_settings(guild_id)
            if settings is None:
                await interaction.edit_original_response(
                    embed=bigv_ui.status_embed(
                        "warning",
                        "BigV is not configured",
                        "Run `/setup` before force-verifying members.",
                        guild,
                    ),
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                )
                return
            await _repair_guild_setup(guild)
            settings = await get_guild_settings(guild_id)
        except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
            logger.exception(
                "Failed to prepare force verification for guild %s", guild_id
            )
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "error",
                    "The server isn't ready yet",
                    "BigV couldn't prepare the verification setup. Check its permissions and try again.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        if settings is None:
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "error",
                    "The server isn't ready yet",
                    "BigV repaired the resources but could not reload their saved IDs.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
        role = guild.get_role(settings["verified_role_id"])
        if role is None:
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "error",
                    "The Verified role is missing",
                    "BigV could not find or repair the configured role.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
        if not role.is_assignable():
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "warning",
                    "The Verified role is out of reach",
                    "Move BigV's highest role above the configured role, then try again.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        if role in user.roles:
            try:
                await delete_pending_verification(guild_id, user.id)
            except aiosqlite.Error:
                logger.exception(
                    "Failed to clear pending verification for guild %s user %s",
                    guild_id,
                    user.id,
                )
                await interaction.edit_original_response(
                    embed=bigv_ui.status_embed(
                        "error",
                        "The member is already verified",
                        "The role is already assigned, but BigV could not clear the pending request.",
                        guild,
                    ),
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                )
                return
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "neutral",
                    "Member already verified",
                    f"<@{user.id}> already has {role.mention}.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        try:
            await user.add_roles(
                role,
                reason=f"BigV force verification requested by {interaction.user}",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Failed to force-assign role %s in guild %s to user %s",
                role.id,
                guild_id,
                user.id,
            )
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "error",
                    "The role couldn't be assigned",
                    "Check BigV's Manage Roles permission and role position, then try again.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        try:
            await delete_pending_verification(guild_id, user.id)
        except aiosqlite.Error:
            logger.exception(
                "Role assigned but pending verification cleanup failed for guild %s user %s",
                guild_id,
                user.id,
            )
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "warning",
                    "Member verified with incomplete cleanup",
                    "The role was assigned, but BigV could not clear the pending request.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        await send_guild_log(
            guild,
            "Member force-verified",
            (
                f"Administrator <@{interaction.user.id}> force-verified <@{user.id}> "
                f"with {role.mention}."
            ),
        )
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "success",
                "Member verified",
                f"<@{user.id}> now has {role.mention}.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )


@forceverify.error
async def forceverify_error(interaction, error):
    await handle_admin_command_error(interaction, error)


@client.tree.command(name="config", description="Inspect BigV's current server setup.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def config(interaction: discord.Interaction):
    """Report stored and live resource status without repairing or mutating it."""
    guild = interaction.guild
    if guild is None:
        await send_guild_required_response(interaction)
        return
    await interaction.response.defer(ephemeral=True)
    if not interaction_user_is_administrator(interaction):
        await send_admin_required_response(interaction)
        return

    try:
        settings = await get_guild_settings(guild.id)
        logging_settings = await get_guild_logging(guild.id)
    except aiosqlite.Error:
        logger.exception("Failed to load configuration status for guild %s", guild.id)
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "error",
                "Configuration is unavailable",
                "BigV couldn't read this server's saved configuration. Try again shortly.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return

    role = None
    channel = None
    panel_status = "not configured"
    if settings is not None:
        role = guild.get_role(settings["verified_role_id"])
        channel = cast(
            discord.TextChannel | None,
            guild.get_channel(settings["verified_channel_id"]),
        )
        panel_status = "missing"
        if channel is not None and settings["verification_message_id"] is not None:
            try:
                await channel.fetch_message(settings["verification_message_id"])
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                panel_status = "unavailable"
            else:
                panel_status = "exists"

    log_category = None
    log_channel = None
    if logging_settings is not None:
        category_id = logging_settings["log_category_id"]
        channel_id = logging_settings["log_channel_id"]
        if category_id is not None:
            log_category = guild.get_channel(category_id)
        if channel_id is not None:
            log_channel = guild.get_channel(channel_id)

    await interaction.edit_original_response(
        embed=bigv_ui.configuration_embed(
            guild,
            settings,
            role,
            channel,
            panel_status,
            logging_settings,
            log_category,
            log_channel,
        ),
        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
    )


@config.error
async def config_error(interaction, error):
    await handle_admin_command_error(interaction, error)


@client.tree.command(name="log", description="Manage optional BigV server audit logs.")
@app_commands.describe(action="Enable, disable, or inspect BigV audit logging.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def log_command(
    interaction: discord.Interaction,
    action: Literal["enable", "disable", "status"],
):
    """Create or update this guild's optional, private audit log channel."""
    guild = interaction.guild
    if guild is None:
        await send_guild_required_response(interaction)
        return
    await interaction.response.defer(ephemeral=True)
    if not interaction_user_is_administrator(interaction):
        await send_admin_required_response(interaction)
        return

    guild_id = guild.id
    if action == "status":
        try:
            settings = await get_guild_logging(guild_id)
        except aiosqlite.Error:
            logger.exception(
                "Failed to load audit logging status for guild %s", guild_id
            )
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "error",
                    "Logging status is unavailable",
                    "BigV couldn't read this server's logging configuration.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        channel = None
        category = None
        if settings is not None:
            if settings["log_channel_id"] is not None:
                channel = guild.get_channel(settings["log_channel_id"])
            if settings["log_category_id"] is not None:
                category = guild.get_channel(settings["log_category_id"])
        await interaction.edit_original_response(
            embed=bigv_ui.logging_status_embed(guild, settings, category, channel),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return

    async with guild_lifecycle_lock(guild_id):
        if action == "disable":
            try:
                settings = await get_guild_logging(guild_id)
                if settings is None or not settings["enabled"]:
                    await interaction.edit_original_response(
                        embed=bigv_ui.status_embed(
                            "neutral",
                            "Audit logging is already disabled",
                            "Existing logging channels and history are unchanged.",
                            guild,
                        ),
                        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                    )
                    return
                await send_guild_log(
                    guild,
                    "Audit logging disabled",
                    f"Administrator <@{interaction.user.id}> disabled BigV audit logging.",
                )
                await set_guild_logging_enabled(guild_id, False)
            except aiosqlite.Error:
                logger.exception(
                    "Failed to disable audit logging for guild %s", guild_id
                )
                await interaction.edit_original_response(
                    embed=bigv_ui.status_embed(
                        "error",
                        "Logging couldn't be disabled",
                        "BigV could not update the saved logging configuration.",
                        guild,
                    ),
                    allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
                )
                return

            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "success",
                    "Audit logging disabled",
                    "No new audit messages will be sent. The channel and its history remain available.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        bot_member = guild.me
        assert bot_member is not None
        if not bot_member.guild_permissions.manage_channels:
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "warning",
                    "Manage Channels is required",
                    "Give BigV **Manage Channels**, then enable audit logging again.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        category = None
        channel = None
        created_category = False
        created_channel = False
        try:
            settings = await get_guild_logging(guild_id)
            if settings is not None:
                if settings["log_category_id"] is not None:
                    category = cast(
                        discord.CategoryChannel | None,
                        guild.get_channel(settings["log_category_id"]),
                    )
                if settings["log_channel_id"] is not None:
                    channel = cast(
                        discord.TextChannel | None,
                        guild.get_channel(settings["log_channel_id"]),
                    )

            if category is None:
                category = await guild.create_category(
                    "BigV",
                    reason=f"BigV audit logging enabled by {interaction.user}",
                )
                created_category = True
            if channel is None:
                channel = await guild.create_text_channel(
                    "bigv-logs",
                    category=category,
                    reason=f"BigV audit logging enabled by {interaction.user}",
                )
                created_channel = True
            elif channel.category_id != category.id:
                await channel.edit(
                    category=category,
                    reason="BigV audit logging category reconciliation",
                )

            await lock_logging_channel(guild, channel)
            await save_guild_logging(guild_id, category.id, channel.id, True)
        except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
            logger.exception("Failed to enable audit logging for guild %s", guild_id)
            if created_channel and channel is not None:
                try:
                    await channel.delete(reason="BigV logging setup rollback")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Failed to delete audit channel during rollback for guild %s",
                        guild_id,
                    )
            if created_category and category is not None:
                try:
                    await category.delete(reason="BigV logging setup rollback")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Failed to delete audit category during rollback for guild %s",
                        guild_id,
                    )
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "error",
                    "Audit logging couldn't be enabled",
                    "Check BigV's channel permissions, then try again.",
                    guild,
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        await send_guild_log(
            guild,
            "Audit logging enabled",
            f"Administrator <@{interaction.user.id}> enabled BigV audit logging.",
        )
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "success",
                "Audit logging enabled",
                f"BigV audit events will be sent to {channel.mention}.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )


@log_command.error
async def log_command_error(interaction, error):
    await handle_admin_command_error(interaction, error)


@client.tree.command(name="verify", description="Submit your BigV verification code.")
@app_commands.describe(code="The private six-digit code BigV sent by DM.")
@app_commands.dm_only()
async def verify(interaction: discord.Interaction, code: str):
    """Validate a deferred DM challenge and assign the configured role."""
    await interaction.response.defer()
    user_id = interaction.user.id
    try:
        pending = await get_pending_verifications(user_id)
    except aiosqlite.Error:
        logger.exception("Failed to load pending verifications for user %s", user_id)
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "error",
                "Verification data is unavailable",
                "BigV couldn't access its verification data. Please try again shortly.",
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    if not pending:
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "neutral",
                "No verification is waiting",
                "Return to the server's verification channel and press **Verify** first.",
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    submitted_hash = hashlib.sha256(code.encode()).hexdigest()
    matched_verification = None
    for verification in pending:
        if verification["code_hash"] == submitted_hash:
            matched_verification = verification
            break
    if matched_verification is None:
        updated_verifications = []
        pending_challenges_remain = False
        for verification in pending:
            guild_id = verification["guild_id"]
            await increment_verification_attempts(guild_id, user_id)
            updated_verification = await get_pending_verification(guild_id, user_id)
            if updated_verification is None:
                continue
            updated_verifications.append(updated_verification)
            if updated_verification["attempts"] >= 5:
                await delete_pending_verification(guild_id, user_id)
            else:
                pending_challenges_remain = True

        if not pending_challenges_remain:
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "warning",
                    "Too many incorrect attempts",
                    "This request was cancelled. Return to the server and press **Verify** for a new code.",
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return

        if len(updated_verifications) == 1:
            verification = updated_verifications[0]
            remaining = 5 - verification["attempts"]
            await interaction.edit_original_response(
                embed=bigv_ui.status_embed(
                    "warning",
                    "That code isn't correct",
                    f"Check the latest BigV DM and try again. **{remaining} of 5 attempts remain.**",
                ),
                allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
            )
            return
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "warning",
                "That code doesn't match",
                "You have requests from multiple servers. Use the code from the DM for the server you want to join.",
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    if int(time.time()) > matched_verification["expires_at"]:
        await delete_pending_verification(matched_verification["guild_id"], user_id)
        await interaction.edit_original_response(
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
            "Verification guild %s is unavailable for user %s", guild_id, user_id
        )
        await delete_pending_verification(guild_id, user_id)
        await interaction.edit_original_response(
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
            "Failed to prepare guild %s for verification by user %s", guild_id, user_id
        )
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "error",
                "The server isn't ready yet",
                "BigV couldn't prepare the verification setup. Your code is still valid, so try again shortly.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return

    if settings is None:
        logger.error(
            "Verification settings are unavailable for guild %s user %s",
            guild_id,
            user_id,
        )
        await interaction.edit_original_response(
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
    if role is None:
        logger.error(
            "Verification role %s is unavailable in guild %s for user %s",
            role_id,
            guild_id,
            user_id,
        )
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "error",
                "The server isn't ready yet",
                "BigV couldn't prepare the verification setup. Your code is still valid, so try again shortly.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        logger.warning(
            "Verification user %s is not a member of guild %s", user_id, guild_id
        )
        await delete_pending_verification(guild_id, user_id)
        await interaction.edit_original_response(
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
        await interaction.edit_original_response(
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
            "Verification role %s is not assignable in guild %s", role.id, guild_id
        )
        await interaction.edit_original_response(
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
        await member.add_roles(role, reason="BigV verification completed")
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Failed to assign verification role %s in guild %s to user %s",
            role.id,
            guild_id,
            user_id,
        )
        await interaction.edit_original_response(
            embed=bigv_ui.status_embed(
                "error",
                "The role couldn't be assigned",
                f"BigV couldn't finish verification in **{bigv_ui.guild_name(guild)}**. Your code remains valid; try again shortly.",
                guild,
            ),
            allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
        )
        return
    await delete_pending_verification(guild_id, user_id)
    await send_guild_log(
        guild,
        "Member verified",
        f"<@{user_id}> completed verification and received {role.mention}.",
    )
    await interaction.edit_original_response(
        embed=bigv_ui.status_embed(
            "success",
            "Verification complete",
            f"You now have verified access to **{bigv_ui.guild_name(guild)}**.",
            guild,
        ),
        allowed_mentions=bigv_ui.SAFE_ALLOWED_MENTIONS,
    )


# CAPTCHA generation
def build_verification_image(code: str) -> io.BytesIO:
    """Render a short-lived verification code as an in-memory PNG."""
    width, height = 260, 100
    image = Image.new("RGB", (width, height), (245, 247, 255))
    draw = ImageDraw.Draw(image)

    try:
        font = ImageFont.truetype("arial.ttf", 42)
    except OSError:
        font = ImageFont.load_default()

    # Noise makes the digits less machine-readable while keeping them legible.
    for _ in range(5):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        draw.line((x1, y1, x2, y2), fill=(180, 190, 220), width=2)

    x = 20
    for digit in code:
        y = random.randint(20, 30)
        draw.text((x, y), digit, fill=(30, 40, 90), font=font)
        x += 36

    for _ in range(120):
        draw.point(
            (random.randint(0, width - 1), random.randint(0, height - 1)),
            fill=random.choice([(170, 180, 210), (80, 100, 235), (30, 40, 90)]),
        )

    image = image.filter(ImageFilter.SMOOTH)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


# Configuration repair
async def repair_guild_setup(guild):
    """Recreate missing setup resources and persist their current Discord IDs."""
    if guild.id in client.unsetup_guilds:
        return
    async with guild_lifecycle_lock(guild.id):
        if guild.id in client.unsetup_guilds:
            return
        await _repair_guild_setup(guild)


async def _repair_guild_setup(guild):
    """Repair one guild while its lifecycle lock is already held."""
    settings = await get_guild_settings(guild.id)

    if settings is None:
        return
    role_created = False
    channel_created = False
    message_created = False
    message_id = settings["verification_message_id"]
    role = guild.get_role(settings["verified_role_id"])

    if role is None:
        role = await guild.create_role(
            name="Verified",
            permissions=discord.Permissions.none(),
            hoist=True,
            reason="BigV Auto repair",
        )
        role_created = True
    elif not role.hoist:
        await role.edit(hoist=True, reason="BigV verification role reconciliation")

    channel = guild.get_channel(settings["verified_channel_id"])
    if channel is None:
        channel = await guild.create_text_channel(
            "bigv-verification", reason="BigV auto repair: verification channel missing"
        )
        message_id = None
        channel_created = True
    await lock_verification_channel(guild, channel, role)
    if message_id is None:
        verification_message = await send_verification_panel(
            guild,
            channel,
            role,
        )
        message_created = True
    else:
        try:
            verification_message = await channel.fetch_message(message_id)
        except discord.NotFound:
            verification_message = await send_verification_panel(
                guild,
                channel,
                role,
            )
            message_created = True
    client.verification_channels.add(channel.id)
    await save_guild_settings(guild.id, role.id, channel.id, verification_message.id)

    if role_created:
        await send_guild_log(
            guild,
            "Verified role repaired",
            f"BigV recreated the missing verification role as {role.mention}.",
        )
    if channel_created:
        await send_guild_log(
            guild,
            "Verification channel repaired",
            f"BigV recreated the missing verification channel as {channel.mention}.",
        )
    if message_created:
        await send_guild_log(
            guild,
            "Verification panel repaired",
            f"BigV recreated the verification panel in {channel.mention}.",
        )


# Discord deletion events trigger immediate repair when a tracked resource changes.
@client.event
async def on_raw_message_delete(payload):
    if payload.guild_id is None:
        return
    if payload.guild_id in client.unsetup_guilds:
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
            guild.id,
        )


@client.event
async def on_raw_bulk_message_delete(payload):
    if payload.guild_id is None:
        return
    if payload.guild_id in client.unsetup_guilds:
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
            guild.id,
        )


@client.event
async def on_guild_role_delete(role):
    if role.guild.id in client.unsetup_guilds:
        return
    try:
        settings = await get_guild_settings(role.guild.id)
        if settings is None:
            return
        if role.id == settings["verified_role_id"]:
            await repair_guild_setup(role.guild)
    except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
        logger.exception(
            "Failed to repair configuration after role deletion for guild %s",
            role.guild.id,
        )


@client.event
async def on_message(message):
    """Keep verification channels limited to BigV's persistent panel."""
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
            message.guild.id,
        )


@client.event
async def on_guild_channel_delete(channel):
    guild_id = channel.guild.id
    if guild_id in client.unsetup_guilds:
        client.verification_channels.discard(channel.id)
        return
    try:
        settings = await get_guild_settings(guild_id)
        if settings and channel.id == settings["verified_channel_id"]:
            client.verification_channels.discard(channel.id)
            await repair_guild_setup(channel.guild)

        logging_settings = await get_guild_logging(guild_id)
        if logging_settings is None:
            return
        if channel.id == logging_settings["log_channel_id"]:
            await clear_guild_logging_channel(guild_id)
        elif channel.id == logging_settings["log_category_id"]:
            await clear_guild_logging_category(guild_id)
    except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
        logger.exception(
            "Failed to repair configuration after channel deletion for guild %s",
            guild_id,
        )


async def lock_verification_channel(guild, channel, verified_role):
    """Lock conversation while hiding the channel from verified members."""
    overwrite = channel.overwrites_for(guild.default_role)
    overwrite.view_channel = True
    overwrite.send_messages = False
    verified_overwrite = channel.overwrites_for(verified_role)
    verified_overwrite.view_channel = False
    bot_overwrite = channel.overwrites_for(guild.me)
    bot_overwrite.view_channel = True
    bot_overwrite.send_messages = True
    bot_overwrite.embed_links = True
    bot_overwrite.read_message_history = True
    bot_overwrite.manage_messages = True
    await channel.set_permissions(
        guild.default_role,
        overwrite=overwrite,
        reason="BigV verification channel lockdown",
    )
    await channel.set_permissions(
        verified_role,
        overwrite=verified_overwrite,
        reason="Hide verification channel from verified members",
    )
    await channel.set_permissions(
        guild.me, overwrite=bot_overwrite, reason="BigV verification channel lockdown"
    )
    for role in guild.roles:
        if role in (guild.default_role, verified_role) or not has_staff_permissions(
            role
        ):
            continue
        staff_overwrite = channel.overwrites_for(role)
        staff_overwrite.view_channel = True
        await channel.set_permissions(
            role,
            overwrite=staff_overwrite,
            reason="Preserve staff visibility in BigV verification channel",
        )


async def lock_logging_channel(guild, channel):
    """Keep optional audit logs private while preserving bot and staff access."""
    everyone_overwrite = channel.overwrites_for(guild.default_role)
    everyone_overwrite.view_channel = False
    bot_overwrite = channel.overwrites_for(guild.me)
    bot_overwrite.view_channel = True
    bot_overwrite.send_messages = True
    bot_overwrite.embed_links = True
    bot_overwrite.read_message_history = True
    await channel.set_permissions(
        guild.default_role,
        overwrite=everyone_overwrite,
        reason="BigV audit channel privacy",
    )
    await channel.set_permissions(
        guild.me,
        overwrite=bot_overwrite,
        reason="BigV audit channel access",
    )
    for role in guild.roles:
        if role == guild.default_role or not has_staff_permissions(role):
            continue
        staff_overwrite = channel.overwrites_for(role)
        staff_overwrite.view_channel = True
        await channel.set_permissions(
            role,
            overwrite=staff_overwrite,
            reason="BigV audit channel staff access",
        )


# Periodic repair covers deletion events missed while BigV was offline.
@tasks.loop(minutes=5)
async def repair_configurations():
    for guild in client.guilds:
        if guild.id in client.unsetup_guilds:
            continue
        try:
            await repair_guild_setup(guild)
        except (discord.Forbidden, discord.HTTPException, aiosqlite.Error):
            logger.exception(
                "Periodic configuration repair failed for guild %s", guild.id
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
