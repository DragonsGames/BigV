import importlib
import os
import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "unit-test-token")

try:
    importlib.import_module("audioop")
except ModuleNotFoundError:
    audioop = types.ModuleType("audioop")
    audioop.__dict__["error"] = Exception
    sys.modules["audioop"] = audioop


discord: Any = importlib.import_module("discord")


with patch.object(discord.Client, "run", return_value=None):
    bot: Any = importlib.import_module("bot")


class FakeHTTPResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "Test response"
        self.headers = {}


def forbidden():
    return discord.Forbidden(
        FakeHTTPResponse(403),
        {"message": "Forbidden", "code": 0},
    )


def http_exception():
    return discord.HTTPException(
        FakeHTTPResponse(500),
        {"message": "Server error", "code": 0},
    )


def not_found():
    return discord.NotFound(
        FakeHTTPResponse(404),
        {"message": "Not found", "code": 0},
    )


class FakeResponse:
    def __init__(self):
        self.send_message: Any = AsyncMock()
        self.defer: Any = AsyncMock()


class FakeUser:
    def __init__(self, user_id=200, administrator=True):
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.send: Any = AsyncMock()

    def __str__(self):
        return f"test-user-{self.id}"


class FakeInteraction:
    def __init__(self, guild=None, user=None):
        self.guild = guild
        self.guild_id = guild.id if guild is not None else None
        self.user = user or FakeUser()
        self.response = FakeResponse()
        self.edit_original_response: Any = AsyncMock()


class FakeRole:
    def __init__(
        self,
        role_id=300,
        assignable=True,
        hoist=True,
        name="Verified",
        managed=False,
        **permissions,
    ):
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.hoist = hoist
        self.managed = managed
        permission_names = (
            "administrator",
            "manage_guild",
            "manage_channels",
            "manage_messages",
            "moderate_members",
            "kick_members",
            "ban_members",
        )
        self.permissions = SimpleNamespace(
            **{name: permissions.get(name, False) for name in permission_names}
        )
        self._assignable = assignable
        self.delete = AsyncMock()
        self.edit = AsyncMock()
        self.guild: Any = None

    def is_assignable(self):
        return self._assignable


class FakeMember:
    def __init__(self, roles=None, user_id=200):
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.roles = list(roles or [])
        self.add_roles = AsyncMock()


class FakeChannel:
    def __init__(self, channel_id=400, name="channel", category=None):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.category = category
        self.category_id = category.id if category is not None else None
        self.delete = AsyncMock()
        self.edit = AsyncMock()
        self.send = AsyncMock()
        self.fetch_message = AsyncMock()
        self.set_permissions = AsyncMock()
        self.guild: Any = None
        self.overwrites_for: Any = lambda target: discord.PermissionOverwrite()


class FakeCategory:
    def __init__(self, category_id=600, name="BigV"):
        self.id = category_id
        self.name = name
        self.delete = AsyncMock()
        self.guild: Any = None


class FakeGuild:
    def __init__(
        self,
        guild_id=100,
        role=None,
        channel=None,
        manage_roles=True,
        manage_channels=True,
        manage_messages=True,
        roles=None,
        channels=None,
    ):
        self.id = guild_id
        self.name = "Test Guild"
        self.icon = None
        self.default_role = FakeRole(role_id=1, name="@everyone")
        permissions = SimpleNamespace(
            manage_roles=manage_roles,
            manage_channels=manage_channels,
            manage_messages=manage_messages,
        )
        self.me = SimpleNamespace(guild_permissions=permissions)
        self._role = role
        self._channel = channel
        self.roles = [self.default_role, *(roles or [])]
        if role is not None and role not in self.roles:
            self.roles.append(role)
        self.channels = list(channels or [])
        if channel is not None and channel not in self.channels:
            self.channels.append(channel)
        self.create_role = AsyncMock()
        self.create_text_channel = AsyncMock()
        self.create_category = AsyncMock()
        self.fetch_member = AsyncMock()

        for item in self.roles + self.channels:
            item.guild = self

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_channel(self, channel_id):
        return next(
            (channel for channel in self.channels if channel.id == channel_id), None
        )
