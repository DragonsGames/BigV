import aiosqlite

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
async def save_guild_settings(
    guild_id,
    verified_role_id,
    verified_channel_id,
    verification_message_id
): 
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute( """
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
        (
        guild_id,
        verified_role_id,
        verified_channel_id,
        verification_message_id))
        await db.commit()
async def get_guild_settings(guild_id):
    async with aiosqlite.connect("BigV.db") as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
            SELECT * 
            FROM Guild_settings 
            WHERE guild_id = ?
            """,(guild_id,)
            )
            row = await cursor.fetchone()
    return row