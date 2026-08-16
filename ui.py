"""Discord presentation helpers and centralized BigV branding."""

import logging
from pathlib import Path

import discord

logger = logging.getLogger("bigv.ui")

# Brand and semantic colors remain separate so status meaning stays clear.
BRAND_BLUE = discord.Colour(0x2C6AF7)
BRAND_COLOR = discord.Colour(0x5064EB)
BRAND_VIOLET = discord.Colour(0x685EE3)
BRAND_INK = discord.Colour(0x090B10)
SUCCESS_COLOR = discord.Colour(0x3BA55D)
WARNING_COLOR = discord.Colour(0xF0B232)
ERROR_COLOR = discord.Colour(0xED4245)
NEUTRAL_COLOR = discord.Colour(0x6D7480)

SAFE_ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False,
    users=False,
    roles=False,
    replied_user=False,
)

ASSET_ROOT = Path(__file__).resolve().parent / "assets"
BRAND_LOGO_PATH = ASSET_ROOT / "brand" / "bigv_logo.png"
BRAND_LOGO_FILENAME = "bigv_logo.png"

EMOJI_NAMES = {
    "verify": "bigv_verify",
    "success": "bigv_success",
    "error": "bigv_error",
    "warning": "bigv_warning",
    "shield": "bigv_shield",
    "lock": "bigv_lock",
    "code": "bigv_code",
    "help": "bigv_help",
    "role": "bigv_role",
    "channel": "bigv_channel",
    "repair": "bigv_repair",
}

EMOJI_FALLBACKS = {
    "verify": "✅",
    "success": "✅",
    "error": "❌",
    "warning": "⚠️",
    "shield": "🛡️",
    "lock": "🔒",
    "code": "🔑",
    "help": "❓",
    "role": "👤",
    "channel": "#",
    "repair": "🔧",
}

_application_emojis = {}
_missing_logo_warning_sent = False


async def load_application_emojis(client):
    """Load BigV application emojis, retaining Unicode fallbacks on failure."""
    _application_emojis.clear()
    try:
        emojis = await client.fetch_application_emojis()
    except discord.HTTPException as error:
        logger.warning(
            "Could not load BigV application emojis; using Unicode fallbacks: %s",
            error,
        )
        return

    expected_names = set(EMOJI_NAMES.values())
    _application_emojis.update(
        (item.name, item) for item in emojis if item.name in expected_names
    )
    logger.info(
        "Loaded %s of %s expected BigV application emoji(s).",
        len(_application_emojis),
        len(expected_names),
    )
    for missing_name in sorted(expected_names - _application_emojis.keys()):
        logger.warning(
            "Missing BigV application emoji: %s; using Unicode fallback.",
            missing_name,
        )


def emoji(key):
    name = EMOJI_NAMES[key]
    return _application_emojis.get(name, EMOJI_FALLBACKS[key])


def guild_name(guild):
    """Return a display-safe guild name that cannot create mentions or Markdown."""
    name = guild.name if guild is not None else "this server"
    return discord.utils.escape_mentions(discord.utils.escape_markdown(name))


def status_embed(state, title, description, guild=None):
    """Build the shared semantic status presentation used by bot responses."""
    states = {
        "success": ("success", SUCCESS_COLOR),
        "warning": ("warning", WARNING_COLOR),
        "error": ("error", ERROR_COLOR),
        "neutral": ("shield", NEUTRAL_COLOR),
        "brand": ("shield", BRAND_COLOR),
    }
    emoji_name, color = states[state]
    embed = discord.Embed(
        title=f"{emoji(emoji_name)} {title}",
        description=description,
        colour=color,
    )
    if guild is not None and guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def verification_dm_embed(guild, expires_at):
    embed = discord.Embed(
        title=f"{emoji('shield')} BigV verification",
        description=(
            f"Use the 6-digit code shown in the image below to verify your access to "
            f"**{guild_name(guild)}**."
        ),
        colour=BRAND_COLOR,
    )

    embed.add_field(
        name=f"{emoji('verify')} Complete verification",
        value="Run `/verify` in this DM and enter the code shown in the image.",
        inline=False,
    )

    embed.add_field(
        name=f"{emoji('lock')} Expires",
        value=f"<t:{expires_at}:R>",
        inline=False,
    )
    embed.add_field(
        name=f"{emoji('code')} Your private code : ",
        value="",
        inline=True,
    )
    embed.set_image(url="attachment://bigv_verification.png")

    embed.set_footer(text="Private • One-time • Never share this code")

    if guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)

    return embed


def help_embed(guild, logo_file=None):
    embed = discord.Embed(
        title=f"{emoji('help')} BigV help",
        description=(
            "Everything you need to set up verification or get through it. "
            "Most members finish in under a minute."
        ),
        colour=BRAND_COLOR,
    )
    embed.add_field(
        name="Member commands",
        value=(
            f"{emoji('code')} `/verify code:123456` — submit the private code sent by BigV.\n"
            f"{emoji('success')} `/ping` — check whether BigV is online.\n"
            f"{emoji('help')} `/help` — open this guide."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{emoji('verify')} How verification works",
        value=(
            "**1.** Press **Send verification code** in the server.\n"
            "**2.** Open the private DM from BigV.\n"
            "**3.** Run `/verify` in that DM and enter the six-digit code.\n"
            "**4.** BigV adds the server's Verified role automatically."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{emoji('shield')} Administrator tools",
        value=(
            "`/setup` — create or repair verification.\n"
            "`/unsetup confirm:true` — safely remove tracked verification resources.\n"
            "`/forceverify user:@member` — verify a member manually.\n"
            "`/config` — inspect the current setup.\n"
            "`/log action:enable|disable|status` — manage optional audit logs.\n"
            "Required: **Manage Roles**, **Manage Channels**, and **Manage Messages**."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{emoji('repair')} Quick fixes",
        value=(
            f"{emoji('lock')} **No DM?** Allow direct messages from server members, then press the button again.\n"
            f"{emoji('warning')} **Code expired?** Return to the server and request a new one.\n"
            f"{emoji('repair')} **Setup item deleted?** BigV automatically repairs its role, channel, and panel."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{emoji('help')} Support",
        value="Need help with BigV? [Join the official support server](https://discord.gg/MBRY3QdCvk).",
        inline=False,
    )
    embed.set_footer(text="BigV • Private codes expire after 10 minutes")

    if logo_file is not None:
        embed.set_thumbnail(url=f"attachment://{BRAND_LOGO_FILENAME}")
    elif guild is not None and guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def configuration_embed(
    guild,
    settings,
    role,
    channel,
    panel_status,
    logging_settings,
    log_category,
    log_channel,
):
    """Present stored IDs alongside their current Discord resource status."""
    embed = discord.Embed(
        title=f"{emoji('shield')} BigV configuration",
        description=f"Current status for **{guild_name(guild)}**.",
        colour=BRAND_COLOR,
    )
    if settings is None:
        embed.add_field(
            name="Verification",
            value="Not configured. Run `/setup` to create verification resources.",
            inline=False,
        )
    else:
        role_id = settings["verified_role_id"]
        channel_id = settings["verified_channel_id"]
        message_id = settings["verification_message_id"]
        role_label = role.mention if role is not None else "Missing"
        channel_label = channel.mention if channel is not None else "Missing"
        hoisted = "yes" if role is not None and role.hoist else "no"
        embed.add_field(
            name="Verification",
            value="Configured",
            inline=False,
        )
        embed.add_field(
            name=f"{emoji('role')} Verified role",
            value=(
                f"{role_label}\nID: `{role_id}`\n"
                f"Exists: **{'yes' if role is not None else 'no'}**\nHoisted: **{hoisted}**"
            ),
            inline=True,
        )
        embed.add_field(
            name=f"{emoji('channel')} Verification channel",
            value=(
                f"{channel_label}\nID: `{channel_id}`\n"
                f"Exists: **{'yes' if channel is not None else 'no'}**"
            ),
            inline=True,
        )
        embed.add_field(
            name=f"{emoji('verify')} Verification panel",
            value=f"ID: `{message_id}`\nStatus: **{panel_status}**",
            inline=True,
        )

    logging_enabled = bool(logging_settings and logging_settings["enabled"])
    log_category_text = (
        log_category.name if log_category is not None else "Missing/not configured"
    )
    log_channel_text = log_channel.mention if log_channel is not None else "Unavailable"
    embed.add_field(
        name=f"{emoji('lock')} Logging",
        value=(
            f"Enabled: **{'yes' if logging_enabled else 'no'}**\n"
            f"Category: **{log_category_text}**\n"
            f"Channel: {log_channel_text}\n"
            f"Channel status: **{'exists' if log_channel is not None else 'missing/not configured'}**"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{emoji('repair')} Self-healing",
        value="Enabled for tracked verification resources.",
        inline=True,
    )
    embed.add_field(
        name=f"{emoji('shield')} Visibility",
        value="Verified members are hidden from the verification channel; staff retain access.",
        inline=True,
    )
    if guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def logging_status_embed(guild, settings, category, channel):
    """Present optional audit logging state without changing it."""
    enabled = bool(settings and settings["enabled"])
    category_text = category.name if category is not None else "Missing/not configured"
    channel_text = channel.mention if channel is not None else "Missing/not configured"
    return status_embed(
        "success" if enabled and channel is not None else "neutral",
        f"Audit logging is {'enabled' if enabled else 'disabled'}",
        (
            f"Category: **{category_text}**\n"
            f"Channel: {channel_text}\n"
            f"Channel exists: **{'yes' if channel is not None else 'no'}**"
        ),
        guild,
    )


def brand_logo_file():
    """Open the canonical logo, or return None so callers can use a fallback."""
    global _missing_logo_warning_sent

    if not BRAND_LOGO_PATH.is_file():
        if not _missing_logo_warning_sent:
            logger.warning(
                "BigV logo is missing at %s; panels will use the server icon when available.",
                BRAND_LOGO_PATH,
            )
            _missing_logo_warning_sent = True
        return None

    try:
        return discord.File(BRAND_LOGO_PATH, filename=BRAND_LOGO_FILENAME)
    except OSError:
        logger.warning(
            "BigV logo could not be opened; panels will use the server icon when available."
        )
        return None


def verification_panel(guild, role, action_row, logo_file=None):
    """Build the persistent Components V2 panel posted during setup and repair."""
    role_text = role.mention if role is not None else "the Verified role"
    heading = (
        f"## {emoji('shield')} You're almost in\n"
        f"Complete one quick check to unlock **{guild_name(guild)}**."
    )
    thumbnail = logo_file
    if thumbnail is None and guild is not None and guild.icon is not None:
        thumbnail = guild.icon.url

    if thumbnail is not None:
        header = discord.ui.Section(
            heading,
            accessory=discord.ui.Thumbnail(
                thumbnail,
                description="BigV logo",
            ),
        )
    else:
        header = discord.ui.TextDisplay(heading)

    steps = discord.ui.TextDisplay(
        f"{emoji('verify')} **1. Start verification**\n"
        "Press **Send verification code** below.\n\n"
        f"{emoji('code')} **2. Check your DMs**\n"
        "BigV sends you a private six-digit code."
    )
    privacy = discord.ui.TextDisplay(
        f"{emoji('lock')} **3. Keep it private**\n"
        "Your code expires after **10 minutes**.\n\n"
        f"{emoji('role')} **4. Unlock the server**\n"
        f"Submit the code to receive {role_text}."
    )
    return discord.ui.Container(
        header,
        discord.ui.Separator(),
        steps,
        privacy,
        discord.ui.Separator(),
        action_row,
        accent_colour=BRAND_COLOR,
    )
