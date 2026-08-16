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
    def __init__(self, user_id=200):
        self.id = user_id
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
    def __init__(self, role_id=300, assignable=True):
        self.id = role_id
        self.mention = f"<@&{role_id}>"
        self._assignable = assignable
        self.delete = AsyncMock()
        self.guild: Any = None

    def is_assignable(self):
        return self._assignable


class FakeMember:
    def __init__(self, roles=None):
        self.roles = list(roles or [])
        self.add_roles = AsyncMock()


class FakeChannel:
    def __init__(self, channel_id=400):
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.delete = AsyncMock()
        self.fetch_message = AsyncMock()
        self.set_permissions = AsyncMock()
        self.guild: Any = None
        self.overwrites_for: Any = None


class FakeGuild:
    def __init__(
        self,
        guild_id=100,
        role=None,
        channel=None,
        manage_roles=True,
        manage_channels=True,
        manage_messages=True,
    ):
        self.id = guild_id
        self.name = "Test Guild"
        self.icon = None
        self.default_role = object()
        permissions = SimpleNamespace(
            manage_roles=manage_roles,
            manage_channels=manage_channels,
            manage_messages=manage_messages,
        )
        self.me = SimpleNamespace(guild_permissions=permissions)
        self._role = role
        self._channel = channel
        self.create_role = AsyncMock()
        self.create_text_channel = AsyncMock()
        self.fetch_member = AsyncMock()

    def get_role(self, role_id):
        if self._role is not None and self._role.id == role_id:
            return self._role
        return None

    def get_channel(self, channel_id):
        if self._channel is not None and self._channel.id == channel_id:
            return self._channel
        return None
