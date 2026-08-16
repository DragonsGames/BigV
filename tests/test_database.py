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

    async def test_database_initialization_creates_both_tables(self):
        connection = sqlite3.connect("BigV.db")
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            {row[0] for row in rows},
            {"guild_settings", "pending_verifications"},
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


if __name__ == "__main__":
    unittest.main()
