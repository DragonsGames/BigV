import hashlib
import os
import sqlite3
import tempfile
import unittest

import database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_directory = os.getcwd()
        self.temporary_directory = tempfile.TemporaryDirectory()
        os.chdir(self.temporary_directory.name)
        await database.setup_database()

    async def asyncTearDown(self):
        os.chdir(self.original_directory)
        self.temporary_directory.cleanup()

    async def test_database_initialization_creates_all_tables(self):
        connection = sqlite3.connect("BigV.db")
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            {row[0] for row in rows},
            {"guild_settings", "pending_verifications", "guild_logging"},
        )

    async def test_guild_settings_insert_update_and_read_are_committed(self):
        await database.save_guild_settings(1, 2, 3, 4)
        first = await database.get_guild_settings(1)
        assert first is not None
        self.assertEqual(tuple(first), (1, 2, 3, 4))

        await database.save_guild_settings(1, 20, 30, 40)
        updated = await database.get_guild_settings(1)
        assert updated is not None
        self.assertEqual(tuple(updated), (1, 20, 30, 40))

    async def test_pending_challenge_resets_attempts_when_replaced(self):
        first_hash = hashlib.sha256(b"001234").hexdigest()
        second_hash = hashlib.sha256(b"654321").hexdigest()
        await database.save_pending_verification(1, 10, first_hash, 100)
        await database.increment_verification_attempts(1, 10)
        await database.save_pending_verification(1, 10, second_hash, 200)

        row = await database.get_pending_verification(1, 10)
        assert row is not None
        self.assertEqual(row["code_hash"], second_hash)
        self.assertEqual(row["expires_at"], 200)
        self.assertEqual(row["attempts"], 0)

    async def test_one_user_can_have_pending_challenges_for_multiple_guilds(self):
        await database.save_pending_verification(1, 10, "hash-one", 100)
        await database.save_pending_verification(2, 10, "hash-two", 200)

        rows = await database.get_pending_verifications(10)
        self.assertEqual({row["guild_id"] for row in rows}, {1, 2})

    async def test_attempt_increment_and_challenge_deletion_are_committed(self):
        await database.save_pending_verification(1, 10, "hash", 100)
        await database.increment_verification_attempts(1, 10)
        row = await database.get_pending_verification(1, 10)
        assert row is not None
        self.assertEqual(row["attempts"], 1)

        await database.delete_pending_verification(1, 10)
        self.assertIsNone(await database.get_pending_verification(1, 10))

    async def test_expired_cleanup_only_deletes_expired_rows(self):
        await database.save_pending_verification(1, 10, "expired", 99)
        await database.save_pending_verification(2, 10, "current", 100)
        await database.save_pending_verification(3, 10, "future", 101)

        await database.delete_expired_verifications(100)
        rows = await database.get_pending_verifications(10)
        self.assertEqual({row["guild_id"] for row in rows}, {2, 3})

    async def test_guild_logging_save_read_and_enable_state(self):
        await database.save_guild_logging(1, 20, 30, True)
        settings = await database.get_guild_logging(1)
        assert settings is not None
        self.assertEqual(tuple(settings), (1, 20, 30, 1))

        await database.set_guild_logging_enabled(1, False)
        disabled = await database.get_guild_logging(1)
        assert disabled is not None
        self.assertEqual(disabled["enabled"], 0)
        self.assertEqual(disabled["log_channel_id"], 30)

        await database.set_guild_logging_enabled(1, True)
        enabled = await database.get_guild_logging(1)
        assert enabled is not None
        self.assertEqual(enabled["enabled"], 1)

    async def test_logging_migration_preserves_existing_guild_settings(self):
        await database.save_guild_settings(1, 2, 3, 4)
        connection = sqlite3.connect("BigV.db")
        try:
            connection.execute("DROP TABLE guild_logging")
            connection.commit()
        finally:
            connection.close()

        await database.setup_database()

        settings = await database.get_guild_settings(1)
        assert settings is not None
        self.assertEqual(tuple(settings), (1, 2, 3, 4))
        self.assertIsNone(await database.get_guild_logging(1))

    async def test_stale_logging_resources_can_be_cleared(self):
        await database.save_guild_logging(1, 20, 30, True)
        await database.clear_guild_logging_category(1)
        settings = await database.get_guild_logging(1)
        assert settings is not None
        self.assertIsNone(settings["log_category_id"])
        self.assertEqual(settings["log_channel_id"], 30)
        self.assertEqual(settings["enabled"], 1)

        await database.clear_guild_logging_channel(1)
        settings = await database.get_guild_logging(1)
        assert settings is not None
        self.assertIsNone(settings["log_channel_id"])
        self.assertEqual(settings["enabled"], 0)

    async def test_guild_teardown_is_scoped_and_disables_logging(self):
        await database.save_guild_settings(1, 2, 3, 4)
        await database.save_guild_settings(2, 20, 30, 40)
        await database.save_pending_verification(1, 10, "one", 100)
        await database.save_pending_verification(2, 10, "two", 100)
        await database.save_guild_logging(1, 5, 6, True)

        await database.delete_guild_setup(1)

        self.assertIsNone(await database.get_guild_settings(1))
        self.assertIsNotNone(await database.get_guild_settings(2))
        rows = await database.get_pending_verifications(10)
        self.assertEqual([row["guild_id"] for row in rows], [2])
        logging_settings = await database.get_guild_logging(1)
        assert logging_settings is not None
        self.assertEqual(logging_settings["enabled"], 0)
        self.assertEqual(logging_settings["log_channel_id"], 6)


if __name__ == "__main__":
    unittest.main()
