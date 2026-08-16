# ruff: noqa: I001

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.support import FakeGuild, FakeRole, bot, discord, http_exception
import ui


class UITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ui._application_emojis.clear()

    def tearDown(self):
        ui._application_emojis.clear()

    def custom_emojis(self):
        return [
            discord.PartialEmoji(name=name, id=index + 1)
            for index, name in enumerate(ui.EMOJI_NAMES.values())
        ]

    async def test_application_emojis_are_preferred_by_semantic_key(self):
        client = SimpleNamespace(
            fetch_application_emojis=AsyncMock(
                return_value=self.custom_emojis()
            )
        )

        await ui.load_application_emojis(client)

        self.assertEqual(len(ui._application_emojis), 11)
        self.assertEqual(ui.emoji("verify").name, "bigv_verify")
        self.assertFalse(isinstance(ui.emoji("success"), str))

    async def test_missing_emoji_logs_warning_and_uses_unicode_fallback(self):
        emojis = self.custom_emojis()
        emojis = [item for item in emojis if item.name != "bigv_warning"]
        client = SimpleNamespace(
            fetch_application_emojis=AsyncMock(return_value=emojis)
        )

        with self.assertLogs("bigv.ui", level="WARNING") as captured:
            await ui.load_application_emojis(client)

        self.assertEqual(ui.emoji("warning"), "⚠️")
        self.assertIn("bigv_warning", "\n".join(captured.output))

    async def test_fetch_failure_keeps_startup_usable(self):
        client = SimpleNamespace(
            fetch_application_emojis=AsyncMock(
                side_effect=http_exception()
            )
        )

        with self.assertLogs("bigv.ui", level="WARNING"):
            await ui.load_application_emojis(client)

        self.assertEqual(ui.emoji("verify"), "✅")

    async def test_startup_loads_emojis_before_registering_persistent_view(self):
        client = bot.VerifierClient()
        order = []

        async def setup_database():
            order.append("database")

        async def load_emojis(current_client):
            self.assertIs(current_client, client)
            order.append("emojis")

        async def sync_commands():
            order.append("sync")
            return []

        with (
            patch.object(bot, "setup_database", side_effect=setup_database),
            patch.object(
                bot.bigv_ui,
                "load_application_emojis",
                side_effect=load_emojis,
            ),
            patch.object(client, "add_view", side_effect=lambda view: order.append("view")),
            patch.object(client.tree, "sync", side_effect=sync_commands),
            patch.object(bot.repair_configurations, "start"),
            patch.object(bot.cleanup_expired_verifications, "start"),
        ):
            await client.setup_hook()

        self.assertEqual(order, ["database", "emojis", "view", "sync"])
        await client.close()

    def test_persistent_view_and_surfaces_use_expected_custom_emojis(self):
        ui._application_emojis.update(
            (item.name, item) for item in self.custom_emojis()
        )
        guild = FakeGuild()
        role = FakeRole()
        view = bot.VerifyView(guild, role)
        button = next(
            item for item in view.walk_children()
            if isinstance(item, discord.ui.Button)
        )
        panel_text = "\n".join(
            item.content for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        help_embed = ui.help_embed(guild)
        help_text = "\n".join(
            [help_embed.title or "", help_embed.description or ""]
            + [f"{field.name}\n{field.value}" for field in help_embed.fields]
        )
        dm_embed = ui.verification_dm_embed(guild, 1893456000)
        dm_text = "\n".join(
            [dm_embed.title or "", dm_embed.description or ""]
            + [f"{field.name}\n{field.value}" for field in dm_embed.fields]
        )

        self.assertTrue(view.is_persistent())
        self.assertIsNone(view.timeout)
        self.assertEqual(button.custom_id, "bigv_verify")
        self.assertEqual(button.emoji.name, "bigv_verify")
        for name in (
            "bigv_shield",
            "bigv_verify",
            "bigv_code",
            "bigv_lock",
            "bigv_role",
        ):
            self.assertIn(name, panel_text)
        for command in ("/help", "/ping", "/setup", "/verify"):
            self.assertIn(command, help_text)
        for phrase in ("No DM?", "Code expired?", "Admin setup"):
            self.assertIn(phrase, help_text)
        for name in ("bigv_shield", "bigv_code", "bigv_verify", "bigv_lock"):
            self.assertIn(name, dm_text)
        self.assertIn("<t:1893456000:R>", dm_text)
        self.assertTrue(
            all(len(field.value or "") <= 1024 for field in help_embed.fields)
        )
        self.assertLessEqual(len(help_embed), 6000)

    def test_status_colors_and_icons_are_semantic(self):
        ui._application_emojis.update(
            (item.name, item) for item in self.custom_emojis()
        )

        success = ui.status_embed("success", "Done", "Done")
        warning = ui.status_embed("warning", "Retry", "Retry")
        error = ui.status_embed("error", "Failed", "Failed")

        self.assertIn("bigv_success", success.title or "")
        self.assertIn("bigv_warning", warning.title or "")
        self.assertIn("bigv_error", error.title or "")
        self.assertEqual(success.colour, ui.SUCCESS_COLOR)
        self.assertEqual(warning.colour, ui.WARNING_COLOR)
        self.assertEqual(error.colour, ui.ERROR_COLOR)

    def test_user_controlled_guild_name_is_escaped_and_mentions_are_disabled(self):
        guild = FakeGuild()
        guild.name = "@everyone **unsafe**"

        escaped = ui.guild_name(guild)

        self.assertNotIn("@everyone", escaped)
        self.assertIn("\\*\\*unsafe\\*\\*", escaped)
        self.assertFalse(ui.SAFE_ALLOWED_MENTIONS.everyone)
        self.assertFalse(ui.SAFE_ALLOWED_MENTIONS.roles)
        self.assertFalse(ui.SAFE_ALLOWED_MENTIONS.users)


if __name__ == "__main__":
    unittest.main()
