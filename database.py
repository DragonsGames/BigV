import aiosqlite

async def setup_database():
    async with aiosqlite.connect("BigV.db") as db:
        await db.execute(
            """ 
            CREATE TABLE IF NOT EXISTS guild_settings(
            guild_id INTEGER PRIMARY KEY ,
            verified_role_id INTEGER NOT NULL,
            verified_channel_id INTEGER NOT NULL,
            unlock_channel_id INTEGER NOT NULL,
            verification_message_id INTEGER
            
            );"""
        )
        await db.commit()