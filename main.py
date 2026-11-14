
import os
import sqlite3
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = "/app/data/karma.db"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured!")


def init_database():
    os.makedirs("/app/data", exist_ok=True)

    with sqlite3.connect(DB_PATH) as db:
        db.execute("PRAGMA foreign_keys = ON")

        db.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                business_count INTEGER NOT NULL DEFAULT 0
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                left_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY (direction) REFERENCES servers(name)
            )
        """)

        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            one_active_employee_per_direction
            ON employees(direction)
            WHERE status = 'active'
        """)

        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            one_active_job_per_discord_id
            ON employees(discord_id)
            WHERE status = 'active'
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                paid_at TEXT NOT NULL,
                FOREIGN KEY (employee_id) REFERENCES employees(id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS daily_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                points INTEGER NOT NULL,
                FOREIGN KEY (employee_id) REFERENCES employees(id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)

        for name in ("Mesa", "Surprise", "Prescott", "Faraway"):
            db.execute(
                "INSERT OR IGNORE INTO servers (name) VALUES (?)",
                (name,)
            )

    print("Karma database initialized!")


intents = discord.Intents.default()


class KarmaBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        init_database()
        await self.tree.sync()
        print("Karma commands synchronized!")

    async def on_ready(self):
        print(f"Karma is online as {self.user}")


bot = KarmaBot()


@bot.tree.command(name="ping", description="Check if Karma is online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Karma is online! Everything works. ✅"
    )


bot.run(TOKEN)
