"""SQLite persistence for guild configuration, logging, and verification state."""

import aiosqlite


# Schema initialization
async def setup_database():
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """ 
            CREATE TABLE IF NOT EXISTS guild_settings(
            guild_id INTEGER PRIMARY KEY ,
            verified_role_id INTEGER NOT NULL,
            verified_channel_id INTEGER NOT NULL,
            verification_message_id INTEGER
            
            );"""
        )
        await db.commit()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_verifications(
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL default  0,
            PRIMARY KEY (guild_id, user_id)
            );"""
        )
        await db.commit()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_logging(
            guild_id INTEGER PRIMARY KEY,
            log_category_id INTEGER,
            log_channel_id INTEGER,
            enabled INTEGER NOT NULL DEFAULT 0
            );"""
        )
        await db.commit()


# Guild configuration stores the Discord resource IDs owned by BigV.
async def save_guild_settings(
    guild_id, verified_role_id, verified_channel_id, verification_message_id
):
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """
        INSERT INTO guild_settings (
                guild_id,
                verified_role_id,
                verified_channel_id,
                verification_message_id
        )
        VALUES(?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET 
        verified_role_id = excluded.verified_role_id,
        verified_channel_id = excluded.verified_channel_id,
        verification_message_id = excluded.verification_message_id
        """,
            (guild_id, verified_role_id, verified_channel_id, verification_message_id),
        )
        await db.commit()


async def get_guild_settings(guild_id):
    async with aiosqlite.connect("BigV.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * 
            FROM Guild_settings 
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        row = await cursor.fetchone()
    return row


async def delete_guild_setup(guild_id):
    """Remove verification state and disable logging in one transaction."""
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            "DELETE FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        await db.execute(
            "DELETE FROM pending_verifications WHERE guild_id = ?",
            (guild_id,),
        )
        await db.execute(
            "UPDATE guild_logging SET enabled = 0 WHERE guild_id = ?",
            (guild_id,),
        )
        await db.commit()


# Optional audit logging is stored separately from verification configuration.
async def get_guild_logging(guild_id):
    async with aiosqlite.connect("BigV.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM guild_logging WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
    return row


async def save_guild_logging(guild_id, log_category_id, log_channel_id, enabled):
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """
            INSERT INTO guild_logging (
                guild_id,
                log_category_id,
                log_channel_id,
                enabled
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                log_category_id = excluded.log_category_id,
                log_channel_id = excluded.log_channel_id,
                enabled = excluded.enabled
            """,
            (guild_id, log_category_id, log_channel_id, int(enabled)),
        )
        await db.commit()


async def set_guild_logging_enabled(guild_id, enabled):
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """
            INSERT INTO guild_logging (guild_id, enabled)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                enabled = excluded.enabled
            """,
            (guild_id, int(enabled)),
        )
        await db.commit()


async def clear_guild_logging_channel(guild_id):
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """
            UPDATE guild_logging
            SET log_channel_id = NULL, enabled = 0
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        await db.commit()


async def clear_guild_logging_category(guild_id):
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """
            UPDATE guild_logging
            SET log_category_id = NULL
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        await db.commit()


# Pending challenges are keyed by guild and user so one member can verify in
# multiple servers without one request replacing another.
async def save_pending_verification(guild_id, user_id, code_hash, expires_at):
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """
            INSERT INTO pending_verifications (
                guild_id,
                user_id,
                code_hash,
                expires_at,
                attempts
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                code_hash = excluded.code_hash,
                expires_at = excluded.expires_at,
                attempts = 0
            """,
            (guild_id, user_id, code_hash, expires_at, 0),
        )
        await db.commit()


async def get_pending_verifications(user_id):
    async with aiosqlite.connect("BigV.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM pending_verifications
            WHERE user_id = ?
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
    return rows


async def delete_pending_verification(guild_id, user_id):
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """
                DELETE FROM pending_verifications
                WHERE guild_id = ?
                AND user_id = ?
                """,
            (guild_id, user_id),
        )
        await db.commit()


async def increment_verification_attempts(guild_id, user_id):
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """
        UPDATE pending_verifications
        SET attempts = attempts + 1
        WHERE guild_id=?
        AND user_id = ?
        """,
            (guild_id, user_id),
        )
        await db.commit()


async def get_pending_verification(guild_id, user_id):
    async with aiosqlite.connect("BigV.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * 
            FROM pending_verifications 
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
    return row


# Periodic maintenance
async def delete_expired_verifications(current_time):
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """
                DELETE FROM pending_verifications 
                WHERE expires_at<?""",
            (current_time,),
        )
        await db.commit()
