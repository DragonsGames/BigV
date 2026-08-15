import os

import discord
from discord import app_commands
from dotenv import load_dotenv
from database import setup_database


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")


intents = discord.Intents.default()


class VerifierClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await setup_database()
        commands = await self.tree.sync()
        print(f"Synced {len(commands)} global command(s).")


client = VerifierClient()

class TestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(
        label="Verify",
        emoji="✅",
        style=discord.ButtonStyle.green
    )
    async def verify_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_message(
            f"Verification requested\n User ID: {interaction.user.id}\n Server: {interaction.guild.name}\n",
            ephemeral=True
        )
    @discord.ui.button(
        label="Cancel",
        emoji="❌",
        style=discord.ButtonStyle.red
        )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_message(
            f"Verification cancelled\n User ID: {interaction.user.id}\n Server: {interaction.guild.name}\n",
            ephemeral=True
        )

@client.tree.command(
    name="ping",
    description="Check whether the verifier bot is online."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

@client.tree.command(
    name="hello",
    description="says Hello , user."
)
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello, {interaction.user.display_name}")
@client.tree.command(
    name="inspectsetup",
    description="Practice selecting a verification channel and role."
)
@app_commands.describe(
    channel="Choose a verification channel",
    role="Choose a verified role"
)
@app_commands.guild_only()
async def inspectsetup(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    role: discord.Role
):
    await interaction.response.send_message(
        f"Server: {interaction.guild.name}\n"
        f"User: {interaction.user.mention}\n"
        f"Channel: {channel.mention} ({channel.id})\n"
        f"Role: {role.mention} ({role.id})",
        ephemeral=True
    )
@client.tree.command(
        name="memberinfo",
        description="Member info"
)
@app_commands.guild_only()
async def memberinfo(
    interaction: discord.Interaction,
    member:discord.Member,
    ):
    await interaction.response.send_message(
            
            f"Name: {member.display_name}\n"
            f"ID: {member.id}\n"
            f"Joined: {member.joined_at}\n",
            ephemeral=True
        )

@client.tree.command(
    name="admincheck",
    description="Test administrator permissions."
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def admincheck(interaction: discord.Interaction):
    await interaction.response.send_message(
        "You are an administrator ✅",
        ephemeral=True
    )
@client.tree.command(
    name="testbutton",
    description="Show a test verification button."
)
@app_commands.guild_only()
async def testbutton(interaction: discord.Interaction):
    view = TestView()

    await interaction.response.send_message(
        "Click below to verify:",
        view=view
    )
@client.tree.command(
    name="testrole",
    description="Create a test Verified role."
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def testrole(interaction: discord.Interaction):

    guild = interaction.guild

    role = await guild.create_role(
        name="Verified",
        permissions=discord.Permissions.none(),
        reason=f"BigV setup requested by {interaction.user}"
    )

    await interaction.response.send_message(
        f"Created role: {role.mention}\n"
        f"Role ID: {role.id}",
        ephemeral=True
    )
@client.tree.command(
    name="testlock",
    description="Test locking a channel to the Verified role."
)
@app_commands.describe(
    channel="Channel to restrict",
    role="Role that should be allowed to type"
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def testlock(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    role: discord.Role
):
    guild = interaction.guild
    everyone = guild.default_role

    everyone_overwrite = channel.overwrites_for(everyone)
    verified_overwrite = channel.overwrites_for(role)

    everyone_overwrite.send_messages = False
    verified_overwrite.send_messages = True

    await channel.set_permissions(
        everyone,
        overwrite=everyone_overwrite,
        reason=f"BigV test lock requested by {interaction.user}"
    )

    await channel.set_permissions(
        role,
        overwrite=verified_overwrite,
        reason=f"BigV test lock requested by {interaction.user}"
    )

    await interaction.response.send_message(
        f"Configured {channel.mention}\n"
        f"❌ {everyone.mention} cannot send messages\n"
        f"✅ {role.mention} can send messages",
        ephemeral=True
    )
@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


client.run(TOKEN)