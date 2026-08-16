import hashlib
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import aiosqlite

from tests.support import (
    FakeChannel,
    FakeGuild,
    FakeInteraction,
    FakeMember,
    FakeRole,
    bot,
    forbidden,
    http_exception,
    not_found,
)


def pending_row(code, guild_id=100, expires_at=None, attempts=0):
    return {
        "guild_id": guild_id,
        "user_id": 200,
        "code_hash": hashlib.sha256(code.encode()).hexdigest(),
        "expires_at": expires_at or int(time.time()) + 600,
        "attempts": attempts,
    }


class VerificationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_pending_verification(self):
        interaction = FakeInteraction()
        with patch.object(
            bot,
            "get_pending_verifications",
            new=AsyncMock(return_value=[]),
        ):
            await bot.verify.callback(interaction, "123456")

        embed = interaction.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("No verification", embed.title)

    async def test_correct_leading_zero_code_assigns_role_and_consumes_challenge(self):
        code = "001234"
        role = FakeRole()
        member = FakeMember()
        guild = FakeGuild(role=role)
        guild.fetch_member.return_value = member
        interaction = FakeInteraction()
        delete = AsyncMock()

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=[pending_row(code)]),
            ),
            patch.object(bot.client, "get_guild", return_value=guild),
            patch.object(bot, "repair_guild_setup", new=AsyncMock()),
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value={"verified_role_id": role.id}),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, code)

        member.add_roles.assert_awaited_once_with(
            role,
            reason="BigV verification completed",
        )
        delete.assert_awaited_once_with(guild.id, interaction.user.id)
        embed = interaction.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("Verification complete", embed.title)

    async def test_wrong_code_increments_attempts(self):
        interaction = FakeInteraction()
        increment = AsyncMock()
        delete = AsyncMock()
        current = pending_row("123456", attempts=1)

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=[pending_row("123456")]),
            ),
            patch.object(bot, "increment_verification_attempts", new=increment),
            patch.object(
                bot,
                "get_pending_verification",
                new=AsyncMock(return_value=current),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, "654321")

        increment.assert_awaited_once_with(100, interaction.user.id)
        delete.assert_not_awaited()
        embed = interaction.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("4 of 5 attempts remain", embed.description)

    async def test_fifth_wrong_code_deletes_challenge(self):
        interaction = FakeInteraction()
        delete = AsyncMock()

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=[pending_row("123456", attempts=4)]),
            ),
            patch.object(
                bot,
                "increment_verification_attempts",
                new=AsyncMock(),
            ),
            patch.object(
                bot,
                "get_pending_verification",
                new=AsyncMock(return_value=pending_row("123456", attempts=5)),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, "654321")

        delete.assert_awaited_once_with(100, interaction.user.id)
        embed = interaction.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("Too many", embed.title)

    async def test_wrong_code_with_multiple_guilds_increments_each_challenge(self):
        interaction = FakeInteraction()
        increment = AsyncMock()
        rows = [pending_row("111111", 1), pending_row("222222", 2)]
        updated_rows = [
            pending_row("111111", 1, attempts=1),
            pending_row("222222", 2, attempts=1),
        ]

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=rows),
            ),
            patch.object(bot, "increment_verification_attempts", new=increment),
            patch.object(
                bot,
                "get_pending_verification",
                new=AsyncMock(side_effect=updated_rows),
            ),
        ):
            await bot.verify.callback(interaction, "999999")

        self.assertEqual(
            [await_call.args for await_call in increment.await_args_list],
            [(1, interaction.user.id), (2, interaction.user.id)],
        )
        embed = interaction.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("multiple servers", embed.description)

    async def test_multi_guild_wrong_code_removes_challenge_reaching_five_attempts(self):
        interaction = FakeInteraction()
        increment = AsyncMock()
        delete = AsyncMock()
        rows = [
            pending_row("111111", 1, attempts=4),
            pending_row("222222", 2, attempts=1),
        ]
        updated_rows = [
            pending_row("111111", 1, attempts=5),
            pending_row("222222", 2, attempts=2),
        ]

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=rows),
            ),
            patch.object(bot, "increment_verification_attempts", new=increment),
            patch.object(
                bot,
                "get_pending_verification",
                new=AsyncMock(side_effect=updated_rows),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, "999999")

        self.assertEqual(increment.await_count, 2)
        delete.assert_awaited_once_with(1, interaction.user.id)

    async def test_multi_guild_wrong_code_reports_when_all_challenges_are_removed(self):
        interaction = FakeInteraction()
        delete = AsyncMock()
        rows = [
            pending_row("111111", 1, attempts=4),
            pending_row("222222", 2, attempts=4),
        ]
        updated_rows = [
            pending_row("111111", 1, attempts=5),
            pending_row("222222", 2, attempts=5),
        ]

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=rows),
            ),
            patch.object(
                bot,
                "increment_verification_attempts",
                new=AsyncMock(),
            ),
            patch.object(
                bot,
                "get_pending_verification",
                new=AsyncMock(side_effect=updated_rows),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, "999999")

        self.assertEqual(delete.await_count, 2)
        embed = interaction.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("Too many", embed.title)
        self.assertIn("press **Verify**", embed.description)
        self.assertNotIn("multiple servers", embed.description)

    async def test_matching_one_multi_guild_challenge_does_not_increment_others(self):
        role = FakeRole()
        member = FakeMember()
        guild = FakeGuild(guild_id=2, role=role)
        guild.fetch_member.return_value = member
        interaction = FakeInteraction()
        increment = AsyncMock()
        delete = AsyncMock()
        rows = [pending_row("111111", 1), pending_row("002222", 2)]

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=rows),
            ),
            patch.object(bot, "increment_verification_attempts", new=increment),
            patch.object(bot.client, "get_guild", return_value=guild),
            patch.object(bot, "repair_guild_setup", new=AsyncMock()),
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value={"verified_role_id": role.id}),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, "002222")

        increment.assert_not_awaited()
        delete.assert_awaited_once_with(2, interaction.user.id)

    async def test_expired_code_is_deleted_before_guild_lookup(self):
        interaction = FakeInteraction()
        delete = AsyncMock()
        row = pending_row("123456", expires_at=1)

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=[row]),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
            patch.object(bot.client, "get_guild") as get_guild,
        ):
            await bot.verify.callback(interaction, "123456")

        delete.assert_awaited_once_with(100, interaction.user.id)
        get_guild.assert_not_called()

    async def test_missing_guild_consumes_unusable_challenge(self):
        interaction = FakeInteraction()
        delete = AsyncMock()

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=[pending_row("123456")]),
            ),
            patch.object(bot.client, "get_guild", return_value=None),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, "123456")

        delete.assert_awaited_once_with(100, interaction.user.id)

    async def test_user_left_guild_consumes_challenge(self):
        role = FakeRole()
        guild = FakeGuild(role=role)
        guild.fetch_member.side_effect = not_found()
        interaction = FakeInteraction()
        delete = AsyncMock()

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=[pending_row("123456")]),
            ),
            patch.object(bot.client, "get_guild", return_value=guild),
            patch.object(bot, "repair_guild_setup", new=AsyncMock()),
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value={"verified_role_id": role.id}),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, "123456")

        delete.assert_awaited_once_with(guild.id, interaction.user.id)

    async def test_already_verified_consumes_challenge_without_adding_role(self):
        role = FakeRole()
        member = FakeMember([role])
        guild = FakeGuild(role=role)
        guild.fetch_member.return_value = member
        interaction = FakeInteraction()
        delete = AsyncMock()

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=[pending_row("123456")]),
            ),
            patch.object(bot.client, "get_guild", return_value=guild),
            patch.object(bot, "repair_guild_setup", new=AsyncMock()),
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value={"verified_role_id": role.id}),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, "123456")

        member.add_roles.assert_not_awaited()
        delete.assert_awaited_once()

    async def test_unassignable_role_preserves_challenge(self):
        role = FakeRole(assignable=False)
        member = FakeMember()
        guild = FakeGuild(role=role)
        guild.fetch_member.return_value = member
        interaction = FakeInteraction()
        delete = AsyncMock()

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=[pending_row("123456")]),
            ),
            patch.object(bot.client, "get_guild", return_value=guild),
            patch.object(bot, "repair_guild_setup", new=AsyncMock()),
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value={"verified_role_id": role.id}),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.verify.callback(interaction, "123456")

        member.add_roles.assert_not_awaited()
        delete.assert_not_awaited()

    async def test_add_role_discord_failures_preserve_challenge(self):
        for error in (forbidden(), http_exception()):
            with self.subTest(error=type(error).__name__):
                role = FakeRole()
                member = FakeMember()
                member.add_roles.side_effect = error
                guild = FakeGuild(role=role)
                guild.fetch_member.return_value = member
                interaction = FakeInteraction()
                delete = AsyncMock()

                with (
                    patch.object(
                        bot,
                        "get_pending_verifications",
                        new=AsyncMock(return_value=[pending_row("123456")]),
                    ),
                    patch.object(bot.client, "get_guild", return_value=guild),
                    patch.object(bot, "repair_guild_setup", new=AsyncMock()),
                    patch.object(
                        bot,
                        "get_guild_settings",
                        new=AsyncMock(return_value={"verified_role_id": role.id}),
                    ),
                    patch.object(bot, "delete_pending_verification", new=delete),
                    self.assertLogs("bigv", level="ERROR"),
                ):
                    await bot.verify.callback(interaction, "123456")

                delete.assert_not_awaited()

    async def test_repair_database_failure_preserves_challenge(self):
        guild = FakeGuild()
        interaction = FakeInteraction()
        delete = AsyncMock()

        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(return_value=[pending_row("123456")]),
            ),
            patch.object(bot.client, "get_guild", return_value=guild),
            patch.object(
                bot,
                "repair_guild_setup",
                new=AsyncMock(side_effect=aiosqlite.Error("database unavailable")),
            ),
            patch.object(bot, "delete_pending_verification", new=delete),
            self.assertLogs("bigv", level="ERROR"),
        ):
            await bot.verify.callback(interaction, "123456")

        delete.assert_not_awaited()

    async def test_initial_database_failure_returns_controlled_response(self):
        interaction = FakeInteraction()
        with (
            patch.object(
                bot,
                "get_pending_verifications",
                new=AsyncMock(side_effect=aiosqlite.Error("database unavailable")),
            ),
            self.assertLogs("bigv", level="ERROR"),
        ):
            await bot.verify.callback(interaction, "123456")

        embed = interaction.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("try again", embed.description.lower())
        self.assertNotIn("database unavailable", embed.description)


class VerificationButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_stores_hash_and_sends_code_as_image(self):
        guild = FakeGuild()
        interaction = FakeInteraction(guild=guild)
        save = AsyncMock()
        delete = AsyncMock()
        row = bot.VerifyActionRow()

        with (
            patch.object(bot.secrets, "randbelow", return_value=1234),
            patch.object(bot.time, "time", return_value=100),
            patch.object(bot, "save_pending_verification", new=save),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await row.verify_button.callback(interaction)

        expected_hash = hashlib.sha256(b"001234").hexdigest()
        save.assert_awaited_once_with(guild.id, interaction.user.id, expected_hash, 700)
        dm_embed = interaction.user.send.await_args.kwargs["embed"]
        captcha_file = interaction.user.send.await_args.kwargs["file"]

        dm_text = "\n".join(
            [dm_embed.title or "", dm_embed.description or ""]
            + [field.value or "" for field in dm_embed.fields]
        )

        public_text = interaction.response.send_message.await_args.args[0]

        # The real code must no longer appear as readable DM text.
        self.assertNotIn("001234", dm_text)
        self.assertNotIn("001234", public_text)

        # The code should instead be delivered through the generated image.
        self.assertEqual(
            captcha_file.filename,
            "bigv_verification.png"
        )

        self.assertEqual(
            dm_embed.image.url,
            "attachment://bigv_verification.png"
        )

        # Database still stores the hash rather than the raw code.
        self.assertNotEqual(expected_hash, "001234")

        delete.assert_not_awaited()

    async def test_blocked_dm_deletes_pending_challenge(self):
        guild = FakeGuild()
        interaction = FakeInteraction(guild=guild)
        interaction.user.send.side_effect = forbidden()
        delete = AsyncMock()
        row = bot.VerifyActionRow()

        with (
            patch.object(bot, "save_pending_verification", new=AsyncMock()),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await row.verify_button.callback(interaction)

        delete.assert_awaited_once_with(guild.id, interaction.user.id)
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])

    async def test_dm_http_failure_deletes_pending_challenge_and_responds(self):
        guild = FakeGuild()
        interaction = FakeInteraction(guild=guild)
        interaction.user.send.side_effect = http_exception()
        delete = AsyncMock()
        row = bot.VerifyActionRow()

        with (
            patch.object(bot, "save_pending_verification", new=AsyncMock()),
            patch.object(bot, "delete_pending_verification", new=delete),
            self.assertLogs("bigv", level="ERROR"),
        ):
            await row.verify_button.callback(interaction)

        delete.assert_awaited_once_with(guild.id, interaction.user.id)
        embed = interaction.response.send_message.await_args.kwargs["embed"]
        self.assertIn("delivered", embed.title)
        self.assertNotIn("Enable direct messages", embed.description)

    async def test_pending_save_database_failure_returns_controlled_response(self):
        guild = FakeGuild()
        interaction = FakeInteraction(guild=guild)
        row = bot.VerifyActionRow()

        with (
            patch.object(
                bot,
                "save_pending_verification",
                new=AsyncMock(side_effect=aiosqlite.Error("database unavailable")),
            ),
            self.assertLogs("bigv", level="ERROR"),
        ):
            await row.verify_button.callback(interaction)

        interaction.user.send.assert_not_awaited()
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("try again", kwargs["embed"].description.lower())
        self.assertNotIn("database unavailable", kwargs["embed"].description)


class SetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.client.verification_channels.clear()

    async def test_each_missing_bot_permission_stops_setup(self):
        cases = (
            {"manage_roles": False},
            {"manage_channels": False},
            {"manage_messages": False},
        )
        for permissions in cases:
            with self.subTest(permissions=permissions):
                guild = FakeGuild(**permissions)
                interaction = FakeInteraction(guild=guild)
                with patch.object(
                    bot,
                    "get_guild_settings",
                    new=AsyncMock(return_value=None),
                ):
                    await bot.setup.callback(interaction)
                guild.create_role.assert_not_awaited()
                self.assertTrue(
                    interaction.response.send_message.await_args.kwargs["ephemeral"]
                )

    async def test_initial_database_failure_returns_controlled_response(self):
        guild = FakeGuild()
        interaction = FakeInteraction(guild=guild)

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(side_effect=aiosqlite.Error("database unavailable")),
            ),
            self.assertLogs("bigv", level="ERROR"),
        ):
            await bot.setup.callback(interaction)

        guild.create_role.assert_not_awaited()
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("try again", kwargs["embed"].description.lower())
        self.assertNotIn("database unavailable", kwargs["embed"].description)

    async def test_successful_setup_creates_and_saves_all_resources(self):
        role = FakeRole()
        channel = FakeChannel()
        message = SimpleNamespace(id=500)
        guild = FakeGuild()
        guild.create_role.return_value = role
        guild.create_text_channel.return_value = channel
        interaction = FakeInteraction(guild=guild)
        save = AsyncMock()

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=None),
            ),
            patch.object(bot, "lock_verification_channel", new=AsyncMock()),
            patch.object(
                bot,
                "send_verification_panel",
                new=AsyncMock(return_value=message),
            ),
            patch.object(bot, "save_guild_settings", new=save),
        ):
            await bot.setup.callback(interaction)

        guild.create_role.assert_awaited_once()
        guild.create_text_channel.assert_awaited_once()
        save.assert_awaited_once_with(guild.id, role.id, channel.id, message.id)
        self.assertIn(channel.id, bot.client.verification_channels)

    async def test_role_hierarchy_failure_deletes_role_and_stops(self):
        role = FakeRole(assignable=False)
        guild = FakeGuild()
        guild.create_role.return_value = role
        interaction = FakeInteraction(guild=guild)

        with patch.object(
            bot,
            "get_guild_settings",
            new=AsyncMock(return_value=None),
        ):
            await bot.setup.callback(interaction)

        role.delete.assert_awaited_once()
        guild.create_text_channel.assert_not_awaited()

    async def test_database_failure_rolls_back_channel_cache_and_role(self):
        role = FakeRole()
        channel = FakeChannel()
        message = SimpleNamespace(id=500)
        guild = FakeGuild()
        guild.create_role.return_value = role
        guild.create_text_channel.return_value = channel
        interaction = FakeInteraction(guild=guild)

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=None),
            ),
            patch.object(bot, "lock_verification_channel", new=AsyncMock()),
            patch.object(
                bot,
                "send_verification_panel",
                new=AsyncMock(return_value=message),
            ),
            patch.object(
                bot,
                "save_guild_settings",
                new=AsyncMock(side_effect=aiosqlite.Error("write failed")),
            ),
            self.assertLogs("bigv", level="ERROR"),
        ):
            await bot.setup.callback(interaction)

        channel.delete.assert_awaited_once()
        role.delete.assert_awaited_once()
        self.assertNotIn(channel.id, bot.client.verification_channels)

    async def test_channel_cleanup_failure_does_not_prevent_role_cleanup(self):
        role = FakeRole()
        channel = FakeChannel()
        channel.delete.side_effect = forbidden()
        message = SimpleNamespace(id=500)
        guild = FakeGuild()
        guild.create_role.return_value = role
        guild.create_text_channel.return_value = channel
        interaction = FakeInteraction(guild=guild)

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=None),
            ),
            patch.object(bot, "lock_verification_channel", new=AsyncMock()),
            patch.object(
                bot,
                "send_verification_panel",
                new=AsyncMock(return_value=message),
            ),
            patch.object(
                bot,
                "save_guild_settings",
                new=AsyncMock(side_effect=aiosqlite.Error("write failed")),
            ),
            self.assertLogs("bigv", level="ERROR"),
        ):
            await bot.setup.callback(interaction)

        role.delete.assert_awaited_once()

    async def test_already_configured_does_not_create_resources(self):
        channel = FakeChannel()
        guild = FakeGuild(channel=channel)
        interaction = FakeInteraction(guild=guild)
        settings = {"verified_channel_id": channel.id}

        with patch.object(
            bot,
            "get_guild_settings",
            new=AsyncMock(return_value=settings),
        ):
            await bot.setup.callback(interaction)

        guild.create_role.assert_not_awaited()
        guild.create_text_channel.assert_not_awaited()

    async def test_channel_lock_applies_expected_overwrites(self):
        guild = FakeGuild()
        channel = FakeChannel()
        channel.overwrites_for = lambda target: bot.discord.PermissionOverwrite()

        await bot.lock_verification_channel(guild, channel)

        self.assertEqual(channel.set_permissions.await_count, 2)
        everyone_call, bot_call = channel.set_permissions.await_args_list
        everyone = everyone_call.kwargs["overwrite"]
        bot_overwrite = bot_call.kwargs["overwrite"]
        self.assertTrue(everyone.view_channel)
        self.assertFalse(everyone.send_messages)
        self.assertTrue(bot_overwrite.view_channel)
        self.assertTrue(bot_overwrite.send_messages)
        self.assertTrue(bot_overwrite.embed_links)
        self.assertTrue(bot_overwrite.read_message_history)
        self.assertTrue(bot_overwrite.manage_messages)


class RepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.client.verification_channels.clear()

    async def test_missing_role_is_recreated_and_saved(self):
        role = FakeRole()
        channel = FakeChannel()
        message = SimpleNamespace(id=500)
        channel.fetch_message.return_value = message
        guild = FakeGuild(channel=channel)
        guild.create_role.return_value = role
        save = AsyncMock()
        settings = {
            "verified_role_id": 999,
            "verified_channel_id": channel.id,
            "verification_message_id": message.id,
        }

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=settings),
            ),
            patch.object(bot, "lock_verification_channel", new=AsyncMock()),
            patch.object(bot, "save_guild_settings", new=save),
        ):
            await bot.repair_guild_setup(guild)

        guild.create_role.assert_awaited_once()
        save.assert_awaited_once_with(guild.id, role.id, channel.id, message.id)

    async def test_missing_channel_recreates_channel_and_panel(self):
        role = FakeRole()
        channel = FakeChannel()
        message = SimpleNamespace(id=500)
        guild = FakeGuild(role=role)
        guild.create_text_channel.return_value = channel
        panel = AsyncMock(return_value=message)
        settings = {
            "verified_role_id": role.id,
            "verified_channel_id": 999,
            "verification_message_id": 888,
        }

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=settings),
            ),
            patch.object(bot, "lock_verification_channel", new=AsyncMock()),
            patch.object(bot, "send_verification_panel", new=panel),
            patch.object(bot, "save_guild_settings", new=AsyncMock()),
        ):
            await bot.repair_guild_setup(guild)

        guild.create_text_channel.assert_awaited_once()
        panel.assert_awaited_once_with(guild, channel, role)
        self.assertIn(channel.id, bot.client.verification_channels)

    async def test_missing_message_recreates_same_panel(self):
        role = FakeRole()
        channel = FakeChannel()
        channel.fetch_message.side_effect = not_found()
        message = SimpleNamespace(id=500)
        guild = FakeGuild(role=role, channel=channel)
        panel = AsyncMock(return_value=message)
        settings = {
            "verified_role_id": role.id,
            "verified_channel_id": channel.id,
            "verification_message_id": 999,
        }

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=settings),
            ),
            patch.object(bot, "lock_verification_channel", new=AsyncMock()),
            patch.object(bot, "send_verification_panel", new=panel),
            patch.object(bot, "save_guild_settings", new=AsyncMock()),
        ):
            await bot.repair_guild_setup(guild)

        panel.assert_awaited_once_with(guild, channel, role)

    async def test_raw_single_and_bulk_delete_only_repair_target_message(self):
        guild = FakeGuild()
        settings = {"verification_message_id": 500}
        repair = AsyncMock()

        with (
            patch.object(bot.client, "get_guild", return_value=guild),
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=settings),
            ),
            patch.object(bot, "repair_guild_setup", new=repair),
        ):
            await bot.on_raw_message_delete(
                SimpleNamespace(guild_id=guild.id, message_id=499)
            )
            await bot.on_raw_message_delete(
                SimpleNamespace(guild_id=guild.id, message_id=500)
            )
            await bot.on_raw_bulk_message_delete(
                SimpleNamespace(guild_id=guild.id, message_ids={498, 499})
            )
            await bot.on_raw_bulk_message_delete(
                SimpleNamespace(guild_id=guild.id, message_ids={499, 500})
            )

        self.assertEqual(repair.await_count, 2)

    async def test_role_and_channel_delete_events_trigger_repair(self):
        role = FakeRole()
        channel = FakeChannel()
        guild = FakeGuild(role=role, channel=channel)
        role.guild = guild
        channel.guild = guild
        settings = {
            "verified_role_id": role.id,
            "verified_channel_id": channel.id,
        }
        repair = AsyncMock()

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=settings),
            ),
            patch.object(bot, "repair_guild_setup", new=repair),
        ):
            await bot.on_guild_role_delete(role)
            bot.client.verification_channels.add(channel.id)
            await bot.on_guild_channel_delete(channel)

        self.assertEqual(repair.await_count, 2)
        self.assertNotIn(channel.id, bot.client.verification_channels)

    async def test_unauthorized_message_is_deleted_and_discord_errors_are_contained(self):
        channel = FakeChannel()
        guild = FakeGuild(channel=channel)
        bot.client.verification_channels.add(channel.id)
        message = SimpleNamespace(
            guild=guild,
            author=object(),
            channel=channel,
            delete=AsyncMock(),
        )

        await bot.on_message(message)
        message.delete.assert_awaited_once()

        message.delete.reset_mock()
        message.delete.side_effect = forbidden()
        with self.assertLogs("bigv", level="ERROR"):
            await bot.on_message(message)

    async def test_one_periodic_repair_failure_does_not_stop_other_guilds(self):
        first = FakeGuild(1)
        second = FakeGuild(2)
        repair = AsyncMock(side_effect=[aiosqlite.Error("broken"), None])

        with (
            patch.object(
                type(bot.client),
                "guilds",
                new_callable=PropertyMock,
                return_value=[first, second],
            ),
            patch.object(bot, "repair_guild_setup", new=repair),
            self.assertLogs("bigv", level="ERROR"),
        ):
            await bot.repair_configurations.coro()

        self.assertEqual(repair.await_count, 2)


class BackgroundCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_uses_current_time(self):
        delete_expired = AsyncMock()
        with (
            patch.object(bot.time, "time", return_value=1234),
            patch.object(
                bot,
                "delete_expired_verifications",
                new=delete_expired,
            ),
        ):
            await bot.cleanup_expired_verifications.coro()

        delete_expired.assert_awaited_once_with(1234)

    async def test_cleanup_database_failure_is_logged_and_does_not_escape(self):
        with (
            patch.object(
                bot,
                "delete_expired_verifications",
                new=AsyncMock(side_effect=aiosqlite.Error("database unavailable")),
            ),
            self.assertLogs("bigv", level="ERROR") as logs,
        ):
            await bot.cleanup_expired_verifications.coro()

        self.assertIn("Expired verification cleanup failed", logs.output[0])


if __name__ == "__main__":
    unittest.main()
