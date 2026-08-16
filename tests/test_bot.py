import asyncio
import hashlib
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import aiosqlite

from tests.support import (
    FakeCategory,
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
    def setUp(self):
        self.audit = AsyncMock(return_value=False)
        self.audit_patcher = patch.object(bot, "send_guild_log", new=self.audit)
        self.audit_patcher.start()
        self.addCleanup(self.audit_patcher.stop)

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
        self.audit.assert_awaited_once()
        audit_text = " ".join(str(value) for value in self.audit.await_args.args)
        self.assertNotIn(code, audit_text)
        self.assertNotIn(hashlib.sha256(code.encode()).hexdigest(), audit_text)
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

    async def test_multi_guild_wrong_code_removes_challenge_reaching_five_attempts(
        self,
    ):
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
        self.assertEqual(captcha_file.filename, "bigv_verification.png")

        self.assertEqual(dm_embed.image.url, "attachment://bigv_verification.png")

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
        bot.client.guild_lifecycle_locks.clear()
        self.audit_patcher = patch.object(
            bot, "send_guild_log", new=AsyncMock(return_value=False)
        )
        self.audit_patcher.start()
        self.addCleanup(self.audit_patcher.stop)

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
                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                interaction.edit_original_response.assert_awaited_once()

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
        kwargs = interaction.edit_original_response.await_args.kwargs
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
        self.assertTrue(guild.create_role.await_args.kwargs["hoist"])
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
        settings = {
            "verified_role_id": 300,
            "verified_channel_id": channel.id,
            "verification_message_id": 500,
        }
        repair = AsyncMock()

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=settings),
            ),
            patch.object(bot, "_repair_guild_setup", new=repair),
        ):
            await bot.setup.callback(interaction)

        repair.assert_awaited_once_with(guild)
        guild.create_role.assert_not_awaited()
        guild.create_text_channel.assert_not_awaited()

    async def test_channel_lock_applies_expected_overwrites(self):
        guild = FakeGuild()
        channel = FakeChannel()
        role = FakeRole()
        staff_role = FakeRole(role_id=301, name="Helpers", manage_messages=True)
        guild.roles.extend((role, staff_role))

        await bot.lock_verification_channel(guild, channel, role)

        self.assertEqual(channel.set_permissions.await_count, 4)
        everyone_call, verified_call, bot_call, staff_call = (
            channel.set_permissions.await_args_list
        )
        everyone = everyone_call.kwargs["overwrite"]
        verified = verified_call.kwargs["overwrite"]
        bot_overwrite = bot_call.kwargs["overwrite"]
        self.assertTrue(everyone.view_channel)
        self.assertFalse(everyone.send_messages)
        self.assertFalse(verified.view_channel)
        self.assertTrue(bot_overwrite.view_channel)
        self.assertTrue(bot_overwrite.send_messages)
        self.assertTrue(bot_overwrite.embed_links)
        self.assertTrue(bot_overwrite.read_message_history)
        self.assertTrue(bot_overwrite.manage_messages)
        self.assertTrue(staff_call.kwargs["overwrite"].view_channel)

    async def test_concurrent_setup_creates_only_one_resource_set(self):
        role = FakeRole()
        channel = FakeChannel()
        message = SimpleNamespace(id=500)
        guild = FakeGuild()
        guild.create_role.return_value = role
        guild.create_text_channel.return_value = channel
        configured = False

        async def get_settings(guild_id):
            if not configured:
                return None
            return {
                "verified_role_id": role.id,
                "verified_channel_id": channel.id,
                "verification_message_id": message.id,
            }

        async def save_settings(*args):
            nonlocal configured
            configured = True

        first = FakeInteraction(guild=guild)
        second = FakeInteraction(guild=guild)
        with (
            patch.object(bot, "get_guild_settings", side_effect=get_settings),
            patch.object(bot, "save_guild_settings", side_effect=save_settings),
            patch.object(bot, "lock_verification_channel", new=AsyncMock()),
            patch.object(
                bot,
                "send_verification_panel",
                new=AsyncMock(return_value=message),
            ),
            patch.object(bot, "_repair_guild_setup", new=AsyncMock()),
        ):
            await asyncio.gather(
                bot.setup.callback(first),
                bot.setup.callback(second),
            )

        guild.create_role.assert_awaited_once()
        guild.create_text_channel.assert_awaited_once()

    async def test_setup_does_not_delete_unrelated_verified_role(self):
        unrelated = FakeRole(role_id=999, name="Verified")
        role = FakeRole()
        channel = FakeChannel()
        guild = FakeGuild(roles=[unrelated])
        guild.create_role.return_value = role
        guild.create_text_channel.return_value = channel

        with (
            patch.object(bot, "get_guild_settings", new=AsyncMock(return_value=None)),
            patch.object(bot, "lock_verification_channel", new=AsyncMock()),
            patch.object(
                bot,
                "send_verification_panel",
                new=AsyncMock(return_value=SimpleNamespace(id=500)),
            ),
            patch.object(bot, "save_guild_settings", new=AsyncMock()),
        ):
            await bot.setup.callback(FakeInteraction(guild=guild))

        unrelated.delete.assert_not_awaited()


class AdminCommandContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_admin_commands_reject_direct_messages(self):
        cases = (
            (bot.unsetup.callback, (True,)),
            (bot.forceverify.callback, (FakeMember(),)),
            (bot.config.callback, ()),
            (bot.log_command.callback, ("status",)),
        )

        for callback, arguments in cases:
            with self.subTest(command=callback.__name__):
                interaction = FakeInteraction()
                await callback(interaction, *arguments)
                interaction.response.send_message.assert_awaited_once()
                interaction.response.defer.assert_not_awaited()


class UnsetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.client.verification_channels.clear()
        bot.client.guild_lifecycle_locks.clear()
        bot.client.unsetup_guilds.clear()

    async def test_confirmation_false_does_not_load_or_delete_configuration(self):
        guild = FakeGuild()
        interaction = FakeInteraction(guild=guild)
        get_settings = AsyncMock()

        with patch.object(bot, "get_guild_settings", new=get_settings):
            await bot.unsetup.callback(interaction, False)

        get_settings.assert_not_awaited()
        self.assertIn(
            "Confirmation",
            interaction.edit_original_response.await_args.kwargs["embed"].title,
        )

    async def test_runtime_administrator_check_stops_unsetup(self):
        guild = FakeGuild()
        interaction = FakeInteraction(guild=guild)
        interaction.user.guild_permissions.administrator = False
        get_settings = AsyncMock()

        with patch.object(bot, "get_guild_settings", new=get_settings):
            await bot.unsetup.callback(interaction, True)

        get_settings.assert_not_awaited()
        self.assertIn(
            "Administrator",
            interaction.edit_original_response.await_args.kwargs["embed"].title,
        )

    async def test_no_configuration_is_idempotent(self):
        guild = FakeGuild()
        interaction = FakeInteraction(guild=guild)
        delete_setup = AsyncMock()

        with (
            patch.object(bot, "get_guild_settings", new=AsyncMock(return_value=None)),
            patch.object(bot, "delete_guild_setup", new=delete_setup),
        ):
            await bot.unsetup.callback(interaction, True)

        delete_setup.assert_not_awaited()
        self.assertIn(
            "not configured",
            interaction.edit_original_response.await_args.kwargs["embed"].title,
        )

    async def test_successful_unsetup_removes_only_tracked_resources(self):
        role = FakeRole()
        unrelated_role = FakeRole(role_id=301, name="Verified")
        channel = FakeChannel()
        unrelated_channel = FakeChannel(401, name="bigv-verification")
        message = SimpleNamespace(delete=AsyncMock())
        channel.fetch_message.return_value = message
        guild = FakeGuild(
            role=role,
            channel=channel,
            roles=[unrelated_role],
            channels=[unrelated_channel],
        )
        settings = {
            "verified_role_id": role.id,
            "verified_channel_id": channel.id,
            "verification_message_id": 500,
        }
        delete_setup = AsyncMock()
        audit = AsyncMock(return_value=True)
        bot.client.verification_channels.add(channel.id)

        with (
            patch.object(
                bot, "get_guild_settings", new=AsyncMock(return_value=settings)
            ),
            patch.object(
                bot,
                "get_guild_logging",
                new=AsyncMock(return_value={"enabled": 1, "log_channel_id": 700}),
            ),
            patch.object(bot, "delete_guild_setup", new=delete_setup),
            patch.object(bot, "send_guild_log", new=audit),
        ):
            await bot.unsetup.callback(FakeInteraction(guild=guild), True)

        message.delete.assert_awaited_once()
        channel.delete.assert_awaited_once()
        role.delete.assert_awaited_once()
        unrelated_channel.delete.assert_not_awaited()
        unrelated_role.delete.assert_not_awaited()
        delete_setup.assert_awaited_once_with(guild.id)
        self.assertNotIn(channel.id, bot.client.verification_channels)
        self.assertNotIn(guild.id, bot.client.unsetup_guilds)
        self.assertEqual(audit.await_count, 2)
        self.assertEqual(
            audit.await_args_list[1].kwargs["channel_id_override"],
            700,
        )

    async def test_already_missing_resources_still_clear_saved_state(self):
        guild = FakeGuild()
        settings = {
            "verified_role_id": 300,
            "verified_channel_id": 400,
            "verification_message_id": 500,
        }
        delete_setup = AsyncMock()

        with (
            patch.object(
                bot, "get_guild_settings", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "get_guild_logging", new=AsyncMock(return_value=None)),
            patch.object(bot, "delete_guild_setup", new=delete_setup),
            patch.object(bot, "send_guild_log", new=AsyncMock(return_value=False)),
        ):
            await bot.unsetup.callback(FakeInteraction(guild=guild), True)

        delete_setup.assert_awaited_once_with(guild.id)

    async def test_discord_cleanup_failure_does_not_claim_success(self):
        for error in (forbidden(), http_exception()):
            with self.subTest(error=type(error).__name__):
                role = FakeRole()
                channel = FakeChannel()
                channel.fetch_message.side_effect = not_found()
                channel.delete.side_effect = error
                guild = FakeGuild(role=role, channel=channel)
                settings = {
                    "verified_role_id": role.id,
                    "verified_channel_id": channel.id,
                    "verification_message_id": 500,
                }
                delete_setup = AsyncMock()

                with (
                    patch.object(
                        bot, "get_guild_settings", new=AsyncMock(return_value=settings)
                    ),
                    patch.object(bot, "delete_guild_setup", new=delete_setup),
                    patch.object(
                        bot, "send_guild_log", new=AsyncMock(return_value=False)
                    ),
                    self.assertLogs("bigv", level="ERROR"),
                ):
                    interaction = FakeInteraction(guild=guild)
                    await bot.unsetup.callback(interaction, True)

                delete_setup.assert_not_awaited()
                self.assertIn(
                    "couldn't be completed",
                    interaction.edit_original_response.await_args.kwargs["embed"].title,
                )

    async def test_database_failure_after_deletion_is_reported(self):
        guild = FakeGuild()
        settings = {
            "verified_role_id": 300,
            "verified_channel_id": 400,
            "verification_message_id": 500,
        }

        with (
            patch.object(
                bot, "get_guild_settings", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "get_guild_logging", new=AsyncMock(return_value=None)),
            patch.object(
                bot,
                "delete_guild_setup",
                new=AsyncMock(side_effect=aiosqlite.Error("unavailable")),
            ),
            patch.object(bot, "send_guild_log", new=AsyncMock(return_value=False)),
            self.assertLogs("bigv", level="ERROR"),
        ):
            interaction = FakeInteraction(guild=guild)
            await bot.unsetup.callback(interaction, True)

        self.assertIn(
            "finalized",
            interaction.edit_original_response.await_args.kwargs["embed"].title,
        )
        self.assertNotIn(guild.id, bot.client.unsetup_guilds)

    async def test_intentional_deletion_events_do_not_trigger_repair(self):
        role = FakeRole()
        channel = FakeChannel()
        guild = FakeGuild(role=role, channel=channel)
        role.guild = guild
        channel.guild = guild
        bot.client.unsetup_guilds.add(guild.id)
        repair = AsyncMock()
        get_settings = AsyncMock()

        with (
            patch.object(bot, "repair_guild_setup", new=repair),
            patch.object(bot, "get_guild_settings", new=get_settings),
        ):
            await bot.on_raw_message_delete(
                SimpleNamespace(guild_id=guild.id, message_id=500)
            )
            await bot.on_raw_bulk_message_delete(
                SimpleNamespace(guild_id=guild.id, message_ids={500})
            )
            await bot.on_guild_role_delete(role)
            await bot.on_guild_channel_delete(channel)

        repair.assert_not_awaited()
        get_settings.assert_not_awaited()


class ForceVerifyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.client.guild_lifecycle_locks.clear()

    def configured(self, role, channel):
        return {
            "verified_role_id": role.id,
            "verified_channel_id": channel.id,
            "verification_message_id": 500,
        }

    async def test_runtime_administrator_check_stops_forceverify(self):
        guild = FakeGuild()
        interaction = FakeInteraction(guild=guild)
        interaction.user.guild_permissions.administrator = False
        get_settings = AsyncMock()

        with patch.object(bot, "get_guild_settings", new=get_settings):
            await bot.forceverify.callback(interaction, FakeMember())

        get_settings.assert_not_awaited()

    async def test_missing_setup_stops_forceverify(self):
        guild = FakeGuild()
        target = FakeMember(user_id=201)
        with patch.object(bot, "get_guild_settings", new=AsyncMock(return_value=None)):
            interaction = FakeInteraction(guild=guild)
            await bot.forceverify.callback(interaction, target)

        target.add_roles.assert_not_awaited()
        self.assertIn(
            "not configured",
            interaction.edit_original_response.await_args.kwargs["embed"].title,
        )

    async def test_success_assigns_role_and_clears_only_current_guild(self):
        role = FakeRole()
        channel = FakeChannel()
        guild = FakeGuild(role=role, channel=channel)
        target = FakeMember(user_id=201)
        settings = self.configured(role, channel)
        delete = AsyncMock()
        audit = AsyncMock(return_value=True)

        with (
            patch.object(
                bot, "get_guild_settings", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "_repair_guild_setup", new=AsyncMock()),
            patch.object(bot, "delete_pending_verification", new=delete),
            patch.object(bot, "send_guild_log", new=audit),
        ):
            interaction = FakeInteraction(guild=guild)
            await bot.forceverify.callback(interaction, target)

        target.add_roles.assert_awaited_once_with(
            role,
            reason=f"BigV force verification requested by {interaction.user}",
        )
        delete.assert_awaited_once_with(guild.id, target.id)
        audit.assert_awaited_once()
        self.assertIn(
            "Member verified",
            interaction.edit_original_response.await_args.kwargs["embed"].title,
        )

    async def test_already_verified_clears_pending_without_adding_role(self):
        role = FakeRole()
        channel = FakeChannel()
        guild = FakeGuild(role=role, channel=channel)
        target = FakeMember([role], user_id=201)
        delete = AsyncMock()

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=self.configured(role, channel)),
            ),
            patch.object(bot, "_repair_guild_setup", new=AsyncMock()),
            patch.object(bot, "delete_pending_verification", new=delete),
        ):
            await bot.forceverify.callback(FakeInteraction(guild=guild), target)

        target.add_roles.assert_not_awaited()
        delete.assert_awaited_once_with(guild.id, target.id)

    async def test_unassignable_role_stops_forceverify(self):
        role = FakeRole(assignable=False)
        channel = FakeChannel()
        guild = FakeGuild(role=role, channel=channel)
        target = FakeMember(user_id=201)

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=self.configured(role, channel)),
            ),
            patch.object(bot, "_repair_guild_setup", new=AsyncMock()),
        ):
            await bot.forceverify.callback(FakeInteraction(guild=guild), target)

        target.add_roles.assert_not_awaited()

    async def test_role_assignment_failure_preserves_pending_request(self):
        for error in (forbidden(), http_exception()):
            with self.subTest(error=type(error).__name__):
                role = FakeRole()
                channel = FakeChannel()
                guild = FakeGuild(role=role, channel=channel)
                target = FakeMember(user_id=201)
                target.add_roles.side_effect = error
                delete = AsyncMock()

                with (
                    patch.object(
                        bot,
                        "get_guild_settings",
                        new=AsyncMock(return_value=self.configured(role, channel)),
                    ),
                    patch.object(bot, "_repair_guild_setup", new=AsyncMock()),
                    patch.object(bot, "delete_pending_verification", new=delete),
                    self.assertLogs("bigv", level="ERROR"),
                ):
                    await bot.forceverify.callback(FakeInteraction(guild=guild), target)

                delete.assert_not_awaited()

    async def test_pending_cleanup_database_failure_is_reported_after_assignment(self):
        role = FakeRole()
        channel = FakeChannel()
        guild = FakeGuild(role=role, channel=channel)
        target = FakeMember(user_id=201)

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=self.configured(role, channel)),
            ),
            patch.object(bot, "_repair_guild_setup", new=AsyncMock()),
            patch.object(
                bot,
                "delete_pending_verification",
                new=AsyncMock(side_effect=aiosqlite.Error("unavailable")),
            ),
            self.assertLogs("bigv", level="ERROR"),
        ):
            interaction = FakeInteraction(guild=guild)
            await bot.forceverify.callback(interaction, target)

        target.add_roles.assert_awaited_once()
        self.assertIn(
            "incomplete cleanup",
            interaction.edit_original_response.await_args.kwargs["embed"].title.lower(),
        )

    async def test_logging_failure_does_not_fail_forceverify(self):
        role = FakeRole()
        channel = FakeChannel()
        guild = FakeGuild(role=role, channel=channel)
        target = FakeMember(user_id=201)

        with (
            patch.object(
                bot,
                "get_guild_settings",
                new=AsyncMock(return_value=self.configured(role, channel)),
            ),
            patch.object(bot, "_repair_guild_setup", new=AsyncMock()),
            patch.object(bot, "delete_pending_verification", new=AsyncMock()),
            patch.object(bot, "send_guild_log", new=AsyncMock(return_value=False)),
        ):
            interaction = FakeInteraction(guild=guild)
            await bot.forceverify.callback(interaction, target)

        self.assertIn(
            "Member verified",
            interaction.edit_original_response.await_args.kwargs["embed"].title,
        )


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_reports_not_configured(self):
        with (
            patch.object(bot, "get_guild_settings", new=AsyncMock(return_value=None)),
            patch.object(bot, "get_guild_logging", new=AsyncMock(return_value=None)),
        ):
            interaction = FakeInteraction(guild=FakeGuild())
            await bot.config.callback(interaction)

        text = "\n".join(
            field.value
            for field in interaction.edit_original_response.await_args.kwargs[
                "embed"
            ].fields
        )
        self.assertIn("Not configured", text)

    async def test_config_is_read_only_and_reports_healthy_resources(self):
        role = FakeRole()
        channel = FakeChannel()
        channel.fetch_message.return_value = SimpleNamespace(id=500)
        category = FakeCategory()
        log_channel = FakeChannel(700, name="bigv-logs", category=category)
        guild = FakeGuild(
            role=role,
            channel=channel,
            channels=[category, log_channel],
        )
        settings = {
            "verified_role_id": role.id,
            "verified_channel_id": channel.id,
            "verification_message_id": 500,
        }
        logging_settings = {
            "log_category_id": category.id,
            "log_channel_id": log_channel.id,
            "enabled": 1,
        }

        with (
            patch.object(
                bot, "get_guild_settings", new=AsyncMock(return_value=settings)
            ),
            patch.object(
                bot, "get_guild_logging", new=AsyncMock(return_value=logging_settings)
            ),
            patch.object(bot, "repair_guild_setup", new=AsyncMock()) as repair,
            patch.object(bot, "save_guild_settings", new=AsyncMock()) as save,
        ):
            interaction = FakeInteraction(guild=guild)
            await bot.config.callback(interaction)

        repair.assert_not_awaited()
        save.assert_not_awaited()
        text = "\n".join(
            field.value
            for field in interaction.edit_original_response.await_args.kwargs[
                "embed"
            ].fields
        )
        self.assertIn("Configured", text)
        self.assertIn("Channel status: **exists**", text)

    async def test_config_reports_missing_resources_without_repairing(self):
        settings = {
            "verified_role_id": 300,
            "verified_channel_id": 400,
            "verification_message_id": 500,
        }
        guild = FakeGuild()
        with (
            patch.object(
                bot, "get_guild_settings", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "get_guild_logging", new=AsyncMock(return_value=None)),
            patch.object(bot, "repair_guild_setup", new=AsyncMock()) as repair,
        ):
            interaction = FakeInteraction(guild=guild)
            await bot.config.callback(interaction)

        repair.assert_not_awaited()
        text = "\n".join(
            field.value
            for field in interaction.edit_original_response.await_args.kwargs[
                "embed"
            ].fields
        )
        self.assertIn("Missing", text)

    async def test_config_reports_missing_panel_without_repairing(self):
        role = FakeRole()
        channel = FakeChannel()
        channel.fetch_message.side_effect = not_found()
        guild = FakeGuild(role=role, channel=channel)
        settings = {
            "verified_role_id": role.id,
            "verified_channel_id": channel.id,
            "verification_message_id": 500,
        }

        with (
            patch.object(
                bot, "get_guild_settings", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "get_guild_logging", new=AsyncMock(return_value=None)),
            patch.object(bot, "repair_guild_setup", new=AsyncMock()) as repair,
        ):
            interaction = FakeInteraction(guild=guild)
            await bot.config.callback(interaction)

        repair.assert_not_awaited()
        text = "\n".join(
            field.value
            for field in interaction.edit_original_response.await_args.kwargs[
                "embed"
            ].fields
        )
        self.assertIn("Status: **missing**", text)

    async def test_runtime_administrator_check_stops_config(self):
        interaction = FakeInteraction(guild=FakeGuild())
        interaction.user.guild_permissions.administrator = False
        get_settings = AsyncMock()
        with patch.object(bot, "get_guild_settings", new=get_settings):
            await bot.config.callback(interaction)
        get_settings.assert_not_awaited()


class LoggingCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.client.guild_lifecycle_locks.clear()

    async def test_enable_creates_private_resources_and_saves_configuration(self):
        category = FakeCategory()
        channel = FakeChannel(700, name="bigv-logs", category=category)
        guild = FakeGuild()
        guild.create_category.return_value = category
        guild.create_text_channel.return_value = channel
        save = AsyncMock()

        with (
            patch.object(bot, "get_guild_logging", new=AsyncMock(return_value=None)),
            patch.object(bot, "lock_logging_channel", new=AsyncMock()) as lock,
            patch.object(bot, "save_guild_logging", new=save),
            patch.object(bot, "send_guild_log", new=AsyncMock(return_value=True)),
        ):
            await bot.log_command.callback(FakeInteraction(guild=guild), "enable")

        guild.create_category.assert_awaited_once()
        guild.create_text_channel.assert_awaited_once()
        lock.assert_awaited_once_with(guild, channel)
        save.assert_awaited_once_with(guild.id, category.id, channel.id, True)

    async def test_enable_twice_reuses_stored_resources(self):
        category = FakeCategory()
        channel = FakeChannel(700, name="bigv-logs", category=category)
        guild = FakeGuild(channels=[category, channel])
        settings = {
            "log_category_id": category.id,
            "log_channel_id": channel.id,
            "enabled": 1,
        }

        with (
            patch.object(
                bot, "get_guild_logging", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "lock_logging_channel", new=AsyncMock()),
            patch.object(bot, "save_guild_logging", new=AsyncMock()) as save,
            patch.object(bot, "send_guild_log", new=AsyncMock(return_value=True)),
        ):
            await bot.log_command.callback(FakeInteraction(guild=guild), "enable")
            await bot.log_command.callback(FakeInteraction(guild=guild), "enable")

        guild.create_category.assert_not_awaited()
        guild.create_text_channel.assert_not_awaited()
        self.assertEqual(save.await_count, 2)

    async def test_enable_database_failure_rolls_back_new_resources(self):
        category = FakeCategory()
        channel = FakeChannel(700, name="bigv-logs", category=category)
        guild = FakeGuild()
        guild.create_category.return_value = category
        guild.create_text_channel.return_value = channel

        with (
            patch.object(bot, "get_guild_logging", new=AsyncMock(return_value=None)),
            patch.object(bot, "lock_logging_channel", new=AsyncMock()),
            patch.object(
                bot,
                "save_guild_logging",
                new=AsyncMock(side_effect=aiosqlite.Error("unavailable")),
            ),
            self.assertLogs("bigv", level="ERROR"),
        ):
            interaction = FakeInteraction(guild=guild)
            await bot.log_command.callback(interaction, "enable")

        channel.delete.assert_awaited_once()
        category.delete.assert_awaited_once()
        self.assertIn(
            "couldn't be enabled",
            interaction.edit_original_response.await_args.kwargs["embed"].title,
        )

    async def test_disable_preserves_channel_and_history(self):
        category = FakeCategory()
        channel = FakeChannel(700, name="bigv-logs", category=category)
        guild = FakeGuild(channels=[category, channel])
        settings = {
            "log_category_id": category.id,
            "log_channel_id": channel.id,
            "enabled": 1,
        }
        enabled = AsyncMock()

        with (
            patch.object(
                bot, "get_guild_logging", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "set_guild_logging_enabled", new=enabled),
            patch.object(bot, "send_guild_log", new=AsyncMock(return_value=True)),
        ):
            await bot.log_command.callback(FakeInteraction(guild=guild), "disable")

        enabled.assert_awaited_once_with(guild.id, False)
        channel.delete.assert_not_awaited()
        category.delete.assert_not_awaited()

    async def test_status_reports_without_mutating_configuration(self):
        guild = FakeGuild()
        settings = {
            "log_category_id": None,
            "log_channel_id": None,
            "enabled": 0,
        }
        with (
            patch.object(
                bot, "get_guild_logging", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "save_guild_logging", new=AsyncMock()) as save,
        ):
            interaction = FakeInteraction(guild=guild)
            await bot.log_command.callback(interaction, "status")

        save.assert_not_awaited()
        self.assertIn(
            "disabled",
            interaction.edit_original_response.await_args.kwargs["embed"].title,
        )

    async def test_runtime_administrator_check_stops_log_command(self):
        interaction = FakeInteraction(guild=FakeGuild())
        interaction.user.guild_permissions.administrator = False
        get_logging = AsyncMock()
        with patch.object(bot, "get_guild_logging", new=get_logging):
            await bot.log_command.callback(interaction, "status")
        get_logging.assert_not_awaited()

    async def test_logging_channel_deletion_disables_without_verification_repair(self):
        channel = FakeChannel(700, name="bigv-logs")
        guild = FakeGuild(channel=channel)
        channel.guild = guild
        logging_settings = {
            "log_category_id": 600,
            "log_channel_id": channel.id,
            "enabled": 1,
        }

        with (
            patch.object(bot, "get_guild_settings", new=AsyncMock(return_value=None)),
            patch.object(
                bot, "get_guild_logging", new=AsyncMock(return_value=logging_settings)
            ),
            patch.object(bot, "clear_guild_logging_channel", new=AsyncMock()) as clear,
            patch.object(bot, "repair_guild_setup", new=AsyncMock()) as repair,
        ):
            await bot.on_guild_channel_delete(channel)

        clear.assert_awaited_once_with(guild.id)
        repair.assert_not_awaited()

    async def test_logging_category_deletion_clears_only_category_reference(self):
        category = FakeCategory()
        guild = FakeGuild(channels=[category])
        category.guild = guild
        logging_settings = {
            "log_category_id": category.id,
            "log_channel_id": 700,
            "enabled": 1,
        }

        with (
            patch.object(bot, "get_guild_settings", new=AsyncMock(return_value=None)),
            patch.object(
                bot, "get_guild_logging", new=AsyncMock(return_value=logging_settings)
            ),
            patch.object(bot, "clear_guild_logging_category", new=AsyncMock()) as clear,
        ):
            await bot.on_guild_channel_delete(category)

        clear.assert_awaited_once_with(guild.id)
        guild.create_category.assert_not_awaited()

    async def test_log_channel_permissions_hide_members_and_allow_staff(self):
        staff_role = FakeRole(role_id=301, name="Helpers", moderate_members=True)
        guild = FakeGuild(roles=[staff_role])
        channel = FakeChannel(700)

        await bot.lock_logging_channel(guild, channel)

        everyone_call, bot_call, staff_call = channel.set_permissions.await_args_list
        self.assertFalse(everyone_call.kwargs["overwrite"].view_channel)
        self.assertTrue(bot_call.kwargs["overwrite"].view_channel)
        self.assertTrue(bot_call.kwargs["overwrite"].send_messages)
        self.assertTrue(staff_call.kwargs["overwrite"].view_channel)


class AuditLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_log_channel_is_disabled_without_recreation(self):
        guild = FakeGuild()
        settings = {
            "log_category_id": 600,
            "log_channel_id": 700,
            "enabled": 1,
        }
        clear = AsyncMock()

        with (
            patch.object(
                bot, "get_guild_logging", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "clear_guild_logging_channel", new=clear),
        ):
            sent = await bot.send_guild_log(guild, "Event", "Details")

        self.assertFalse(sent)
        clear.assert_awaited_once_with(guild.id)
        guild.create_text_channel.assert_not_awaited()

    async def test_send_failure_is_contained(self):
        channel = FakeChannel(700)
        channel.send.side_effect = forbidden()
        guild = FakeGuild(channel=channel)
        settings = {
            "log_category_id": 600,
            "log_channel_id": channel.id,
            "enabled": 1,
        }

        with (
            patch.object(
                bot, "get_guild_logging", new=AsyncMock(return_value=settings)
            ),
            self.assertLogs("bigv", level="ERROR"),
        ):
            sent = await bot.send_guild_log(guild, "Member verified", "User 200")

        self.assertFalse(sent)

    async def test_audit_embed_uses_safe_mentions_and_contains_no_captcha_data(self):
        channel = FakeChannel(700)
        guild = FakeGuild(channel=channel)
        settings = {
            "log_category_id": 600,
            "log_channel_id": channel.id,
            "enabled": 1,
        }

        with patch.object(
            bot, "get_guild_logging", new=AsyncMock(return_value=settings)
        ):
            await bot.send_guild_log(
                guild,
                "Member verified",
                "<@200> completed verification.",
            )

        kwargs = channel.send.await_args.kwargs
        text = f"{kwargs['embed'].title}\n{kwargs['embed'].description}"
        self.assertNotIn("001234", text)
        self.assertNotIn("code_hash", text)
        self.assertIs(kwargs["allowed_mentions"], bot.bigv_ui.SAFE_ALLOWED_MENTIONS)


class RepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.client.verification_channels.clear()
        bot.client.guild_lifecycle_locks.clear()
        bot.client.unsetup_guilds.clear()
        self.audit_patcher = patch.object(
            bot, "send_guild_log", new=AsyncMock(return_value=False)
        )
        self.audit_patcher.start()
        self.addCleanup(self.audit_patcher.stop)

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
            patch.object(bot, "lock_verification_channel", new=AsyncMock()) as lock,
            patch.object(bot, "save_guild_settings", new=save),
        ):
            await bot.repair_guild_setup(guild)

        guild.create_role.assert_awaited_once()
        self.assertTrue(guild.create_role.await_args.kwargs["hoist"])
        lock.assert_awaited_once_with(guild, channel, role)
        save.assert_awaited_once_with(guild.id, role.id, channel.id, message.id)

    async def test_existing_role_is_hoisted_without_being_renamed(self):
        role = FakeRole(hoist=False, name="Community Access")
        channel = FakeChannel()
        message = SimpleNamespace(id=500)
        channel.fetch_message.return_value = message
        guild = FakeGuild(role=role, channel=channel)
        settings = {
            "verified_role_id": role.id,
            "verified_channel_id": channel.id,
            "verification_message_id": message.id,
        }

        with (
            patch.object(
                bot, "get_guild_settings", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "lock_verification_channel", new=AsyncMock()),
            patch.object(bot, "save_guild_settings", new=AsyncMock()),
        ):
            await bot.repair_guild_setup(guild)

        role.edit.assert_awaited_once_with(
            hoist=True,
            reason="BigV verification role reconciliation",
        )
        self.assertEqual(role.name, "Community Access")

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
            patch.object(bot, "lock_verification_channel", new=AsyncMock()) as lock,
            patch.object(bot, "send_verification_panel", new=panel),
            patch.object(bot, "save_guild_settings", new=AsyncMock()),
        ):
            await bot.repair_guild_setup(guild)

        guild.create_text_channel.assert_awaited_once()
        lock.assert_awaited_once_with(guild, channel, role)
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
            patch.object(bot, "get_guild_logging", new=AsyncMock(return_value=None)),
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
            patch.object(bot, "get_guild_logging", new=AsyncMock(return_value=None)),
            patch.object(bot, "repair_guild_setup", new=repair),
        ):
            await bot.on_guild_role_delete(role)
            bot.client.verification_channels.add(channel.id)
            await bot.on_guild_channel_delete(channel)

        self.assertEqual(repair.await_count, 2)
        self.assertNotIn(channel.id, bot.client.verification_channels)

    async def test_unauthorized_message_is_deleted_and_discord_errors_are_contained(
        self,
    ):
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

    async def test_periodic_repair_skips_guild_during_unsetup(self):
        guild = FakeGuild()
        bot.client.unsetup_guilds.add(guild.id)
        repair = AsyncMock()

        with (
            patch.object(
                type(bot.client),
                "guilds",
                new_callable=PropertyMock,
                return_value=[guild],
            ),
            patch.object(bot, "repair_guild_setup", new=repair),
        ):
            await bot.repair_configurations.coro()

        repair.assert_not_awaited()

    async def test_periodic_repair_reapplies_verified_visibility(self):
        role = FakeRole()
        channel = FakeChannel()
        channel.fetch_message.return_value = SimpleNamespace(id=500)
        guild = FakeGuild(role=role, channel=channel)
        settings = {
            "verified_role_id": role.id,
            "verified_channel_id": channel.id,
            "verification_message_id": 500,
        }

        with (
            patch.object(
                type(bot.client),
                "guilds",
                new_callable=PropertyMock,
                return_value=[guild],
            ),
            patch.object(
                bot, "get_guild_settings", new=AsyncMock(return_value=settings)
            ),
            patch.object(bot, "save_guild_settings", new=AsyncMock()),
        ):
            await bot.repair_configurations.coro()

        verified_call = next(
            call
            for call in channel.set_permissions.await_args_list
            if call.args[0] is role
        )
        self.assertFalse(verified_call.kwargs["overwrite"].view_channel)


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
