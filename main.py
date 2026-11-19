import os
import sqlite3
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks


# ============================================================
# KARMA NOIRE — BIZINFO
# Existing database is kept at /app/data/karma.db
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = "/app/data/karma.db"

DIRECTIONS = ["Mesa", "Surprise", "Prescott", "Faraway"]
SERVER_CODES = {"Mesa": "07", "Surprise": "10", "Prescott": "11", "Faraway": "26"}
BASE_DIRECTIONS = list(DIRECTIONS)
BASE_WORK_CHANNELS = {
    "Mesa": "mesa・работа",
    "Surprise": "surprise・работа",
    "Prescott": "prescott・работа",
    "Faraway": "faraway・работа",
}
MAX_EMPLOYEE_BUSINESSES = 100

REVIEW_CHANNEL_NAME = "проверка-отчётов"
REJECTION_CHANNEL_NAME = "журнал-действий"
FINANCE_CHANNEL_NAME = "финансы"
STATS_CHANNEL_NAME = "статистика"
TICKET_PANEL_CHANNEL_NAME = "создать-заявку"
TICKET_CATEGORY_NAME = "05・НОВОБРАНЦЫ"
WORK_CATEGORY_NAME = "03・РАБОТА"
TICKET_LOG_CHANNEL_NAME = "заявки-на-вступление"
ADMIN_ROLE_NAMES = ("Владелец", "Администратор")
OWNER_ROLE_NAME = "Владелец"
RECRUIT_ROLE_NAME = "Новобранец"

WORK_CHANNELS = dict(BASE_WORK_CHANNELS)

SERVER_TZ = ZoneInfo("Europe/Moscow")
POINT_VALUE = 20_000_000
DARK_COLOR = discord.Color.from_rgb(23, 23, 23)
SILVER_COLOR = discord.Color.from_rgb(175, 175, 175)

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured!")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


@contextmanager
def database():
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()



def init_database():
    os.makedirs("/app/data", exist_ok=True)
    with database() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                business_count INTEGER NOT NULL DEFAULT 0,
                server_code TEXT
            )
        """)
        # Existing tables are deliberately retained. Migrations only add fields/tables.
        def add_column(table, column, declaration):
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        db.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                left_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                owner_id TEXT,
                point_rate INTEGER NOT NULL DEFAULT 20000000,
                guild_id TEXT
            )
        """)
        add_column("servers", "server_code", "TEXT")
        add_column("employees", "owner_id", "TEXT")
        add_column("employees", "point_rate", "INTEGER NOT NULL DEFAULT 20000000")
        add_column("employees", "guild_id", "TEXT")

        # The old design allowed only one employee per game server. Remove that restriction.
        db.execute("DROP INDEX IF EXISTS one_active_employee_per_direction")
        # Keep one active employee record per Discord user (one work assignment per person).
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_discord_id
            ON employees(discord_id) WHERE status='active'
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS businesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id TEXT NOT NULL,
                server_name TEXT NOT NULL,
                employee_id INTEGER,
                added_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY(employee_id) REFERENCES employees(id)
            )
        """)
        db.execute("DROP INDEX IF EXISTS uq_business_server_id")
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_business_server_id_active
            ON businesses(server_name, business_id) WHERE status='active'
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_business_server
            ON businesses(server_name, status)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_business_employee
            ON businesses(employee_id, status)
        """)
        add_column("businesses", "owner_id", "TEXT")
        db.execute("""
            CREATE TABLE IF NOT EXISTS divisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                server_name TEXT NOT NULL,
                role_name TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                division_number INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by TEXT
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_divisions_server ON divisions(server_name, division_number)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_name TEXT NOT NULL,
                organization_name TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                added_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                UNIQUE(server_name, organization_name)
            )
        """)

        # Existing employee-linked businesses are migrated to a named owner.
        # Use the employee record's explicitly assigned responsible owner.
        # If none exists, leave ownership blank rather than guessing.
        db.execute("""
            UPDATE businesses
            SET owner_id = (SELECT NULLIF(e.owner_id, '') FROM employees e WHERE e.id=businesses.employee_id)
            WHERE owner_id IS NULL AND employee_id IS NOT NULL
        """)
        # Old code stored Mesa №2 as a separate server. Keep it as a division,
        # while consolidating its business assets under the real game server Mesa.
        db.execute("""
            INSERT OR IGNORE INTO divisions(name,server_name,role_name,channel_name,division_number,created_at)
            SELECT 'Mesa №2','Mesa','Mesa №2','mesa・работа・2',2,?
            WHERE EXISTS (SELECT 1 FROM employees WHERE direction='Mesa №2')
               OR EXISTS (SELECT 1 FROM businesses WHERE server_name='Mesa №2')
        """, (now_iso(),))
        db.execute("""
            UPDATE businesses SET status='removed'
            WHERE server_name='Mesa №2' AND status='active'
              AND EXISTS (
                SELECT 1 FROM businesses current
                WHERE current.server_name='Mesa'
                  AND current.business_id=businesses.business_id
                  AND current.status='active'
              )
        """)
        db.execute("UPDATE businesses SET server_name='Mesa' WHERE server_name='Mesa №2'")
        db.execute("""
            UPDATE servers SET business_count=business_count + COALESCE(
                (SELECT business_count FROM servers old WHERE old.name='Mesa №2'), 0
            ) WHERE name='Mesa' AND EXISTS (SELECT 1 FROM servers WHERE name='Mesa №2')
        """)
        db.execute("DELETE FROM servers WHERE name='Mesa №2'")

        db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                paid_at TEXT NOT NULL,
                FOREIGN KEY(employee_id) REFERENCES employees(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS daily_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                points INTEGER NOT NULL,
                point_rate INTEGER NOT NULL DEFAULT 20000000,
                FOREIGN KEY(employee_id) REFERENCES employees(id)
            )
        """)
        add_column("daily_entries", "point_rate", "INTEGER NOT NULL DEFAULT 20000000")
        db.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                attachment_url TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                points INTEGER NOT NULL DEFAULT 0,
                review_message_id TEXT,
                reviewer_id TEXT,
                reviewed_at TEXT,
                rejection_reason TEXT,
                guild_id TEXT,
                FOREIGN KEY(employee_id) REFERENCES employees(id)
            )
        """)
        add_column("submissions", "guild_id", "TEXT")
        db.execute("DROP INDEX IF EXISTS one_pending_submission_per_employee")
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_submissions_employee_date
            ON submissions(employee_id, work_date, status)
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS submission_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL,
                attachment_url TEXT NOT NULL,
                filename TEXT,
                FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE CASCADE
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
        db.execute("""
            CREATE TABLE IF NOT EXISTS point_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                reason TEXT NOT NULL,
                admin_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(employee_id) REFERENCES employees(id)
            )
        """)
        # Safe migration: point adjustments store the rate that applied when
        # the adjustment was made. Existing rows receive the historical default.
        add_column("point_adjustments", "point_rate", "INTEGER NOT NULL DEFAULT 20000000")
        db.execute("""
            CREATE TABLE IF NOT EXISTS report_sessions (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(guild_id, user_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS bot_messages (
                channel_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                purpose TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                game_server TEXT NOT NULL,
                age TEXT NOT NULL,
                real_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                closed_at TEXT,
                reviewer_id TEXT
            )
        """)
        server_codes = {"Mesa": "07", "Surprise": "10", "Prescott": "11", "Faraway": "26"}
        for name, code in server_codes.items():
            db.execute("INSERT OR IGNORE INTO servers(name, server_code) VALUES (?,?)", (name, code))
            db.execute("UPDATE servers SET server_code=COALESCE(server_code, ?) WHERE name=?", (code, name))

        # Legacy employees receive a default rate. Their owner can be assigned later.
        db.execute("UPDATE employees SET point_rate=? WHERE point_rate IS NULL OR point_rate<=0", (POINT_VALUE,))
        division_rows = db.execute("SELECT name,server_name,channel_name FROM divisions ORDER BY server_name,division_number").fetchall()
        # Also preserve any legacy employee directions as divisions.
        legacy_rows = db.execute("SELECT DISTINCT direction FROM employees WHERE direction NOT IN ('Mesa','Surprise','Prescott','Faraway')").fetchall()
        for legacy in legacy_rows:
            name = legacy["direction"]
            if name.endswith(" №2") and name[:-3] in server_codes:
                base = name[:-3]
                channel = f"{BASE_WORK_CHANNELS[base]}・2"
                db.execute("INSERT OR IGNORE INTO divisions(name,server_name,role_name,channel_name,division_number,created_at) VALUES(?,?,?,?,2,?)",
                           (name,base,name,channel,now_iso()))
        division_rows = db.execute("SELECT name,server_name,channel_name FROM divisions ORDER BY server_name,division_number").fetchall()
    DIRECTIONS[:] = list(BASE_DIRECTIONS)
    SERVER_CODES.clear()
    SERVER_CODES.update({"Mesa": "07", "Surprise": "10", "Prescott": "11", "Faraway": "26"})
    WORK_CHANNELS.clear()
    WORK_CHANNELS.update(BASE_WORK_CHANNELS)
    for division in division_rows:
        DIRECTIONS.append(division["name"])
        SERVER_CODES[division["name"]] = SERVER_CODES.get(division["server_name"], "—")
        WORK_CHANNELS[division["name"]] = division["channel_name"]
    print("Karma database initialized (safe migration completed)!")


def get_work_date():
    now = datetime.now(SERVER_TZ)
    if now.hour < 5:
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def now_iso():
    return discord.utils.utcnow().isoformat()


def is_manager(member):
    return isinstance(member, discord.Member) and any(
        role.name in ADMIN_ROLE_NAMES for role in member.roles
    )


def is_owner(member):
    return isinstance(member, discord.Member) and any(
        role.name == OWNER_ROLE_NAME for role in member.roles
    )


def money(value):
    return f"{int(value):,}".replace(",", ".") + "$"


def make_embed(title, description=None):
    embed = discord.Embed(
        title=title,
        description=description,
        color=DARK_COLOR,
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="K A R M A  N O I R E  •  BIZINFO")
    return embed


def get_employee_by_discord_id(db, discord_id, active_only=True):
    if active_only:
        return db.execute(
            """
            SELECT * FROM employees
            WHERE discord_id = ? AND status = 'active'
            ORDER BY id DESC LIMIT 1
            """,
            (str(discord_id),)
        ).fetchone()

    return db.execute(
        """
        SELECT * FROM employees
        WHERE discord_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (str(discord_id),)
    ).fetchone()


def get_employee_totals(db, employee_id):
    earned = db.execute(
        """
        SELECT COALESCE(SUM(points), 0)
        FROM daily_entries WHERE employee_id = ?
        """,
        (employee_id,)
    ).fetchone()[0]

    paid = db.execute(
        """
        SELECT COALESCE(SUM(points), 0)
        FROM payments WHERE employee_id = ?
        """,
        (employee_id,)
    ).fetchone()[0]

    adjustments = db.execute(
        "SELECT COALESCE(SUM(points), 0) FROM point_adjustments WHERE employee_id = ?",
        (employee_id,)
    ).fetchone()[0]
    earned = int(earned) + int(adjustments)
    unpaid = max(0, earned - paid)
    return int(earned), int(paid), int(unpaid)



def get_employee_earned_amount(db, employee_id):
    daily = db.execute("SELECT COALESCE(SUM(points * point_rate),0) FROM daily_entries WHERE employee_id=?", (employee_id,)).fetchone()[0]
    adjustments = db.execute("SELECT COALESCE(SUM(points * point_rate),0) FROM point_adjustments WHERE employee_id=?", (employee_id,)).fetchone()[0]
    return int(daily or 0) + int(adjustments or 0)


def get_employee_unpaid_amount(db, employee_id):
    earned_amount = get_employee_earned_amount(db, employee_id)
    paid_amount = db.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE employee_id=?", (employee_id,)).fetchone()[0]
    return max(0, int(earned_amount) - int(paid_amount or 0))


async def update_review_message(guild, review_message_id, status_text):
    try:
        channel = discord.utils.get(
            guild.text_channels,
            name=REVIEW_CHANNEL_NAME
        )
        if channel is None:
            return

        message = await channel.fetch_message(int(review_message_id))
        if not message.embeds:
            return

        embed = message.embeds[0]

        # Remove a previous status field, if one exists.
        fields = [
            field for field in embed.fields
            if field.name != "СТАТУС"
        ]

        updated = discord.Embed.from_dict(embed.to_dict())
        updated.clear_fields()

        for field in fields:
            updated.add_field(
                name=field.name,
                value=field.value,
                inline=field.inline
            )

        updated.add_field(
            name="СТАТУС",
            value=status_text,
            inline=False
        )

        await message.edit(embed=updated, view=None)

    except Exception as error:
        print(f"Review message update error: {error}")


# ============================================================
# DISCORD CLIENT
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class KarmaBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.waiting_for_report = {}
        self._guild_commands_synced = False

    async def setup_hook(self):
        init_database()

        # Persistent buttons keep working after a bot restart.
        self.add_view(SubmitView())
        self.add_view(ReviewView())
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())

        # Slash-команды будут синхронизированы с конкретными серверами Discord
        # в on_ready: так они появляются на сервере сразу, не ожидая глобального
        # распространения Discord.

        if not daily_reminder.is_running():
            daily_reminder.start()
        if not daily_cleanup.is_running():
            daily_cleanup.start()

    async def on_ready(self):
        print(f"Karma is online as {self.user}")

        if self._guild_commands_synced:
            return

        if not self.guilds:
            print("WARNING: Karma is connected, but no Discord servers are available for command sync.")
            return

        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Karma commands synchronized for {guild.name}: {len(synced)} commands")

        # Удаляем старую глобальную регистрацию, чтобы не оставлять дубликаты.
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        print("Old global Karma commands cleared.")
        self._guild_commands_synced = True


bot = KarmaBot()


async def restrict_new_member(member: discord.Member):
    """Hide the server from a new member except the application panel."""
    guild = member.guild
    panel = discord.utils.get(guild.text_channels, name=TICKET_PANEL_CHANNEL_NAME)
    deny = discord.PermissionOverwrite(view_channel=False)
    allow_panel = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    # Temporary member-specific overwrites are reversible and do not alter other users.
    for category in guild.categories:
        try:
            await category.set_permissions(member, overwrite=deny, reason="Karma: новый участник — доступ только к заявке")
        except discord.HTTPException as error:
            print(f"Could not restrict category {category.name} for {member.id}: {error}")
    for channel in guild.channels:
        if isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
            try:
                overwrite = allow_panel if panel and channel.id == panel.id else deny
                await channel.set_permissions(member, overwrite=overwrite, reason="Karma: новый участник — доступ только к заявке")
            except discord.HTTPException as error:
                print(f"Could not restrict channel {channel.name} for {member.id}: {error}")
    if panel:
        try:
            await panel.set_permissions(member, overwrite=allow_panel, reason="Karma: доступ к панели заявок")
        except discord.HTTPException as error:
            print(f"Could not allow application panel for {member.id}: {error}")


async def restore_member_visibility(member: discord.Member):
    """Remove temporary member-specific visibility locks after acceptance."""
    for channel in member.guild.channels:
        try:
            if channel.overwrites_for(member) != discord.PermissionOverwrite():
                await channel.set_permissions(member, overwrite=None, reason="Karma: заявка принята — снять временные ограничения")
        except discord.HTTPException as error:
            print(f"Could not restore channel {channel.name} for {member.id}: {error}")
    for category in member.guild.categories:
        try:
            if category.overwrites_for(member) != discord.PermissionOverwrite():
                await category.set_permissions(member, overwrite=None, reason="Karma: заявка принята — снять временные ограничения")
        except discord.HTTPException as error:
            print(f"Could not restore category {category.name} for {member.id}: {error}")


@bot.event
async def on_member_join(member: discord.Member):
    await restrict_new_member(member)
    panel = discord.utils.get(member.guild.text_channels, name=TICKET_PANEL_CHANNEL_NAME)
    if panel:
        try:
            await member.send(
                f"Добро пожаловать на **KARMA NOIRE / BIZINFO**. Для вступления заполни заявку: https://discord.com/channels/{member.guild.id}/{panel.id}. "
                "До рассмотрения заявки тебе доступен только канал подачи заявки."
            )
        except discord.HTTPException:
            pass



@bot.event
async def on_member_remove(member: discord.Member):
    with database() as db:
        rows = db.execute("SELECT id,direction FROM employees WHERE discord_id=? AND status='active'",
                          (str(member.id),)).fetchall()
        for row in rows:
            db.execute("UPDATE employees SET status='archived',left_at=? WHERE id=?",
                       (now_iso(), row["id"]))
            db.execute("UPDATE businesses SET employee_id=NULL WHERE employee_id=? AND status='active'",
                       (row["id"],))
            db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('MEMBER_LEFT',?,?)",
                       (f"employee={member.id}; direction={row['direction']}", now_iso()))
        db.execute("DELETE FROM report_sessions WHERE user_id=?", (str(member.id),))


# ============================================================
# EMPLOYEE REPORT SUBMISSION BUTTON
# ============================================================

class SubmitView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Отправить отчёт",
        emoji="📷",
        style=discord.ButtonStyle.secondary,
        custom_id="karma:submit_report"
    )
    async def submit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not interaction.guild or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "Эта кнопка доступна только на сервере.",
                ephemeral=True
            )
            return

        direction = next(
            (
                name for name, channel_name in WORK_CHANNELS.items()
                if channel_name == interaction.channel.name
            ),
            None
        )

        if direction is None:
            await interaction.response.send_message(
                "Эта кнопка находится не в рабочем канале.",
                ephemeral=True
            )
            return

        with database() as db:
            employee = get_employee_by_discord_id(
                db, interaction.user.id
            )

            if not employee or employee["direction"] != direction:
                await interaction.response.send_message(
                    "У тебя нет активного назначения на этот Server.",
                    ephemeral=True
                )
                return

            work_date = get_work_date()

            approved = db.execute(
                """
                SELECT id FROM submissions
                WHERE employee_id = ?
                  AND work_date = ?
                  AND status = 'approved'
                """,
                (employee["id"], work_date)
            ).fetchone()

            if approved:
                await interaction.response.send_message(
                    "Отчёт за сегодняшний рабочий день уже принят.",
                    ephemeral=True
                )
                return

            pending = db.execute(
                """
                SELECT id FROM submissions
                WHERE employee_id = ? AND status = 'pending'
                """,
                (employee["id"],)
            ).fetchone()

            if pending:
                await interaction.response.send_message(
                    "У тебя уже есть отчёт, ожидающий проверки.",
                    ephemeral=True
                )
                return

        with database() as db:
            db.execute(
                "INSERT OR REPLACE INTO report_sessions (guild_id, user_id, channel_id, created_at) VALUES (?, ?, ?, ?)",
                (str(interaction.guild_id), str(interaction.user.id), str(interaction.channel_id), now_iso())
            )

        await interaction.response.send_message(
            "Пришли скриншот следующим сообщением. Текстовые сообщения в рабочем канале не сохраняются.",
            ephemeral=True
        )


# ============================================================
# REPORT REVIEW MODALS AND BUTTONS
# ============================================================


class ApprovalConfirmView(discord.ui.View):
    def __init__(self, submission_id, points, reviewer_id):
        super().__init__(timeout=120)
        self.submission_id = submission_id
        self.points = points
        self.reviewer_id = reviewer_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.reviewer_id or not is_manager(interaction.user):
            await interaction.response.send_message("Подтвердить начисление может только проверяющий владелец.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Да, начислить", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        await finalize_approval(interaction, self.submission_id, self.points)
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Начисление отменено. Отчёт остался ожидающим проверки.", view=self)


async def finalize_approval(interaction, submission_id, points):
    await interaction.response.defer(ephemeral=True)
    with database() as db:
        submission = db.execute("""
            SELECT s.*, e.discord_id, e.direction, e.point_rate
            FROM submissions s JOIN employees e ON e.id=s.employee_id
            WHERE s.id=? AND s.status='pending'
        """, (submission_id,)).fetchone()
        if not submission:
            await interaction.followup.send("Заявка уже обработана или не найдена.", ephemeral=True)
            return
        cur = db.execute("""
            UPDATE submissions SET status='approved', points=?, reviewer_id=?, reviewed_at=?
            WHERE id=? AND status='pending'
        """, (points, str(interaction.user.id), now_iso(), submission_id))
        if cur.rowcount != 1:
            await interaction.followup.send("Заявка уже обработана.", ephemeral=True)
            return
        db.execute("INSERT INTO daily_entries(employee_id,work_date,points,point_rate) VALUES(?,?,?,?)",
                   (submission["employee_id"], submission["work_date"], points, int(submission["point_rate"] or POINT_VALUE)))
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES(?,?,?)",
                   ("REPORT_APPROVED", f"submission={submission_id}; employee={submission['discord_id']}; points={points}; reviewer={interaction.user.id}", now_iso()))
        rate = int(submission["point_rate"] or POINT_VALUE)
        amount = points * rate
        employee_id = int(submission["discord_id"])
        direction = submission["direction"]
        work_date = submission["work_date"]
        review_message_id = submission["review_message_id"]

    if interaction.guild and review_message_id:
        await update_review_message(interaction.guild, review_message_id,
            f"ПРИНЯТ • {points} {points_word(points)} • {money(amount)} • проверил {interaction.user.mention}")
    try:
        user = bot.get_user(employee_id) or await bot.fetch_user(employee_id)
        await user.send(
            f"† KARMA NOIRE • ОТЧЁТ ОДОБРЕН †\n"
            f"Твой отчёт за **{work_date}** по серверу **{direction}** принят.\n"
            f"Начислено: **{points} {points_word(points)}**\n"
            f"Твоя ставка: **{money(rate)} за балл**\n"
            f"Зачислено: **{money(amount)}**."
        )
    except discord.HTTPException:
        pass
    await interaction.followup.send(
        f"Отчёт принят. Начислено **{points} {points_word(points)}** ({money(amount)}). Сотрудник уведомлён в ЛС (если сообщения доступны).",
        ephemeral=True
    )


class ApprovalModal(discord.ui.Modal, title="Подтверждение отчёта"):
    points = discord.ui.TextInput(label="Количество начисляемых баллов", placeholder="Например: 5", required=True, max_length=7)

    def __init__(self, submission_id):
        super().__init__()
        self.submission_id = submission_id

    async def on_submit(self, interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
            return
        try:
            points = int(self.points.value)
            if points < 1 or points > 100000:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Введи целое число от 1 до 100000.", ephemeral=True)
            return
        if points > 10:
            await interaction.response.send_message(
                f"⚠️ Ты собираешься начислить **{points} баллов** — это больше 10. Уверен?",
                view=ApprovalConfirmView(self.submission_id, points, interaction.user.id),
                ephemeral=True
            )
            return
        await finalize_approval(interaction, self.submission_id, points)


class RejectionModal(discord.ui.Modal, title="Отклонение отчёта"):
    reason = discord.ui.TextInput(
        label="Причина отклонения",
        placeholder="Укажи, что нужно исправить",
        required=True,
        max_length=500,
        style=discord.TextStyle.paragraph
    )

    def __init__(self, submission_id, review_message_id):
        super().__init__()
        self.submission_id = submission_id
        self.review_message_id = review_message_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message(
                "Недостаточно прав.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        with database() as db:
            cursor = db.execute(
                """
                UPDATE submissions
                SET status = 'rejected',
                    reviewer_id = ?,
                    reviewed_at = ?,
                    rejection_reason = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    str(interaction.user.id),
                    now_iso(),
                    self.reason.value,
                    self.submission_id
                )
            )

            if cursor.rowcount == 0:
                await interaction.followup.send(
                    "Заявка уже обработана или не найдена.",
                    ephemeral=True
                )
                return

            db.execute(
                """
                INSERT INTO audit_log
                (action, details, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    "REPORT_REJECTED",
                    f"Submission {self.submission_id}; reason={self.reason.value}",
                    now_iso()
                )
            )

        if interaction.guild:
            await update_review_message(
                interaction.guild,
                self.review_message_id,
                f"ОТКЛОНЁН • {self.reason.value} • проверил {interaction.user.mention}"
            )

        # Notify the employee privately and publish a mention in the admin action log.
        with database() as db:
            info = db.execute(
                "SELECT e.discord_id, e.direction FROM submissions s JOIN employees e ON e.id=s.employee_id WHERE s.id=?",
                (self.submission_id,)
            ).fetchone()
        if info and interaction.guild:
            employee_id = int(info["discord_id"])
            reason_text = self.reason.value
            try:
                user = bot.get_user(employee_id) or await bot.fetch_user(employee_id)
                await user.send(
                    f"Твой отчёт по Server **{info['direction']}** отклонён.\n"
                    f"Причина: {reason_text}\n"
                    "Исправь ошибку и отправь новый скриншот через кнопку в рабочем канале."
                )
            except discord.HTTPException:
                pass
            log_channel = discord.utils.get(interaction.guild.text_channels, name=REJECTION_CHANNEL_NAME)
            if log_channel:
                notice = make_embed("KARMA NOIRE | ОТЧЁТ ОТКЛОНЁН", f"{f'<@{employee_id}>'}, твоя заявка не одобрена.")
                notice.add_field(name="SERVER", value=info["direction"], inline=True)
                notice.add_field(name="ПРИЧИНА", value=reason_text[:1024], inline=False)
                notice.add_field(name="ДАЛЬШЕ", value="Исправь замечания и отправь отчёт повторно.", inline=False)
                await log_channel.send(content=f"<@{employee_id}>", embed=notice)

        await interaction.followup.send(
            "Отчёт отклонён. Сотрудник получил уведомление с причиной отказа.",
            ephemeral=True
        )



class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.secondary, custom_id="karma:review:approve")
    async def approve(self, interaction, button):
        if not is_manager(interaction.user):
            await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
            return
        with database() as db:
            submission = db.execute("SELECT id FROM submissions WHERE review_message_id=? AND status='pending'",
                                    (str(interaction.message.id),)).fetchone()
        if not submission:
            await interaction.response.send_message("Заявка не найдена или уже обработана.", ephemeral=True)
            return
        await interaction.response.send_modal(ApprovalModal(submission["id"]))

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.secondary, custom_id="karma:review:reject")
    async def reject(self, interaction, button):
        if not is_manager(interaction.user):
            await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
            return
        with database() as db:
            submission = db.execute("SELECT id, review_message_id FROM submissions WHERE review_message_id=? AND status='pending'",
                                    (str(interaction.message.id),)).fetchone()
        if not submission:
            await interaction.response.send_message("Заявка не найдена или уже обработана.", ephemeral=True)
            return
        await interaction.response.send_modal(RejectionModal(submission["id"], submission["review_message_id"]))


# ============================================================
# RECEIVING EMPLOYEE SCREENSHOTS
# ============================================================


class RevokeSubmissionView(discord.ui.View):
    def __init__(self, submission_id, employee_id):
        super().__init__(timeout=86400)
        self.submission_id = submission_id
        self.employee_id = employee_id

    @discord.ui.button(label="Отозвать отчёт", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def revoke(self, interaction, button):
        if interaction.user.id != self.employee_id:
            await interaction.response.send_message("Это не твоя заявка.", ephemeral=True)
            return
        with database() as db:
            row = db.execute("SELECT status, review_message_id FROM submissions WHERE id=? AND employee_id=?",
                             (self.submission_id, str(self.employee_id))).fetchone()
            if not row or row["status"] != "pending":
                await interaction.response.send_message("Отозвать можно только отчёт, который ещё ожидает проверки.", ephemeral=True)
                return
            db.execute("UPDATE submissions SET status='withdrawn', reviewed_at=? WHERE id=? AND status='pending'",
                       (now_iso(), self.submission_id))
            db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('REPORT_WITHDRAWN',?,?)",
                       (f"submission={self.submission_id}; employee={self.employee_id}", now_iso()))
        if row["review_message_id"]:
            with database() as db:
                guild_row = db.execute("SELECT guild_id FROM submissions WHERE id=?", (self.submission_id,)).fetchone()
            guild = bot.get_guild(int(guild_row["guild_id"])) if guild_row and guild_row["guild_id"] else None
            if guild:
                await update_review_message(guild, row["review_message_id"], "ОТОЗВАН СОТРУДНИКОМ")
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Заявка отозвана. Можешь отправить правильный отчёт.", view=self)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    direction = next((name for name, channel_name in WORK_CHANNELS.items()
                     if channel_name == message.channel.name), None)
    if direction is None or is_manager(message.author):
        return

    key = (str(message.guild.id), str(message.author.id))
    with database() as db:
        session = db.execute("SELECT channel_id FROM report_sessions WHERE guild_id=? AND user_id=?", key).fetchone()
    expected = bool(session and str(message.channel.id) == session["channel_id"])
    images = [a for a in message.attachments if (a.content_type or "").startswith("image/")]

    if not expected or not (1 <= len(images) <= 3) or len(images) != len(message.attachments):
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if expected:
            try:
                await message.author.send("Для отчёта прикрепи от 1 до 3 изображений одним сообщением. Другие файлы и текстовые сообщения не принимаются.")
            except discord.HTTPException:
                pass
        return

    review_channel = discord.utils.get(message.guild.text_channels, name=REVIEW_CHANNEL_NAME)
    if review_channel is None:
        try:
            await message.delete()
            await message.author.send("Не найден канал проверки отчётов. Сообщи владельцам.")
        except discord.HTTPException:
            pass
        return

    work_date = get_work_date()
    with database() as db:
        employee = db.execute("SELECT * FROM employees WHERE discord_id=? AND status='active'",
                              (str(message.author.id),)).fetchone()
        if not employee or employee["direction"] != direction:
            db.execute("DELETE FROM report_sessions WHERE guild_id=? AND user_id=?", key)
            try:
                await message.delete()
                await message.author.send("У тебя нет активного назначения на этот игровой сервер.")
            except discord.HTTPException:
                pass
            return
        pending = db.execute("SELECT id FROM submissions WHERE employee_id=? AND status='pending'",
                             (employee["id"],)).fetchone()
        approved = db.execute("SELECT id FROM submissions WHERE employee_id=? AND work_date=? AND status='approved'",
                              (employee["id"], work_date)).fetchone()
        if pending or approved:
            db.execute("DELETE FROM report_sessions WHERE guild_id=? AND user_id=?", key)
            try:
                await message.delete()
                await message.author.send("На сегодня уже есть ожидающий или одобренный отчёт. Если отчёт ожидает проверки, используй кнопку отзыва.")
            except discord.HTTPException:
                pass
            return

        cur = db.execute("""
            INSERT INTO submissions(employee_id,work_date,attachment_url,submitted_at,guild_id)
            VALUES(?,?,?,?,?)
        """, (employee["id"], work_date, images[0].url, now_iso(), str(message.guild.id)))
        submission_id = cur.lastrowid
        for attachment in images:
            db.execute("INSERT INTO submission_attachments(submission_id,attachment_url,filename) VALUES(?,?,?)",
                       (submission_id, attachment.url, attachment.filename))
        db.execute("DELETE FROM report_sessions WHERE guild_id=? AND user_id=?", key)

    owner_text = f"<@{employee['owner_id']}>" if employee["owner_id"] else "Не назначен"
    embed = make_embed("KARMA NOIRE | BIZINFO", "НОВАЯ ЗАЯВКА НА ПРОВЕРКУ")
    embed.add_field(name="СОТРУДНИК", value=message.author.mention, inline=True)
    embed.add_field(name="ВЛАДЕЛЕЦ СОТРУДНИКА", value=owner_text, inline=True)
    embed.add_field(name="SERVER", value=f"{direction} • №{SERVER_CODES.get(direction, '—')}", inline=True)
    embed.add_field(name="РАБОЧАЯ ДАТА", value=work_date, inline=True)
    embed.add_field(name="СКРИНШОТЫ", value=str(len(images)), inline=True)
    embed.add_field(name="СТАТУС", value="ОЖИДАЕТ ПРОВЕРКИ", inline=False)
    embed.set_footer(text=f"KARMA NOIRE • REPORT #{submission_id}")
    files = []
    embeds = [embed]
    try:
        for index, attachment in enumerate(images):
            file = await attachment.to_file()
            files.append(file)
            if index == 0:
                embed.set_image(url=f"attachment://{attachment.filename}")
            else:
                image_embed = make_embed(f"СКРИНШОТ {index + 1}", "")
                image_embed.set_image(url=f"attachment://{attachment.filename}")
                embeds.append(image_embed)
        review_message = await review_channel.send(embeds=embeds, files=files, view=ReviewView())
        with database() as db:
            db.execute("UPDATE submissions SET review_message_id=? WHERE id=?", (str(review_message.id), submission_id))
        await message.delete()
        await message.author.send(
            f"Твой отчёт #{submission_id} по серверу **{direction}** передан на проверку. "
            "Если отправил не те изображения, можешь отозвать заявку кнопкой ниже.",
            view=RevokeSubmissionView(submission_id, message.author.id)
        )
    except Exception as error:
        print(f"Report sending error: {error}")
        with database() as db:
            db.execute("UPDATE submissions SET status='failed' WHERE id=? AND status='pending'", (submission_id,))
            db.execute("DELETE FROM report_sessions WHERE guild_id=? AND user_id=?", key)
        try:
            await message.delete()
            await message.author.send("Не удалось передать отчёт. Попробуй снова через кнопку.")
        except discord.HTTPException:
            pass



@bot.tree.command(name="withdraw_report", description="Отозвать свою заявку, пока она не проверена")
@app_commands.guild_only()
async def withdraw_report(interaction: discord.Interaction, report_id: app_commands.Range[int, 1, 999999999]):
    with database() as db:
        row = db.execute("""
            SELECT s.status,s.review_message_id,s.guild_id
            FROM submissions s JOIN employees e ON e.id=s.employee_id
            WHERE s.id=? AND e.discord_id=?
        """, (report_id, str(interaction.user.id))).fetchone()
        if not row:
            await interaction.response.send_message("Заявка не найдена или не принадлежит тебе.", ephemeral=True); return
        if row["status"] != "pending":
            await interaction.response.send_message("Отозвать можно только заявку, которая ожидает проверки.", ephemeral=True); return
        db.execute("UPDATE submissions SET status='withdrawn',reviewed_at=? WHERE id=? AND status='pending'",
                   (now_iso(), report_id))
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('REPORT_WITHDRAWN',?,?)",
                   (f"submission={report_id}; employee={interaction.user.id}", now_iso()))
    guild = bot.get_guild(int(row["guild_id"])) if row["guild_id"] else interaction.guild
    if guild and row["review_message_id"]:
        await update_review_message(guild, row["review_message_id"], "ОТОЗВАН СОТРУДНИКОМ")
    await interaction.response.send_message(f"Заявка #{report_id} отозвана. Можешь отправить новый отчёт.", ephemeral=True)


# ============================================================
# DAILY REMINDER AT 18:00 MOSCOW TIME
# ============================================================

@tasks.loop(time=time(hour=18, minute=0, tzinfo=SERVER_TZ))
async def daily_reminder():
    work_date = get_work_date()

    with database() as db:
        employees = db.execute(
            """
            SELECT id, discord_id, direction, guild_id
            FROM employees
            WHERE status = 'active'
            """
        ).fetchall()

        for employee in employees:
            # Owners and administrators are not expected to submit staff reports.
            member = None
            for guild in bot.guilds:
                if employee["guild_id"] and str(guild.id) != str(employee["guild_id"]):
                    continue
                member = guild.get_member(int(employee["discord_id"]))
                if member is None:
                    try:
                        member = await guild.fetch_member(int(employee["discord_id"]))
                    except (discord.HTTPException, ValueError):
                        member = None
                if member:
                    break
            if member and is_manager(member):
                continue

            submitted = db.execute(
                """
                SELECT id FROM submissions
                WHERE employee_id = ?
                  AND work_date = ?
                  AND status IN ('pending', 'approved')
                """,
                (employee["id"], work_date)
            ).fetchone()

            if submitted:
                continue

            reminder_key = f"{employee['id']}:{work_date}"
            already_sent = db.execute(
                """
                SELECT id FROM audit_log
                WHERE action = 'REMINDER_SENT' AND details = ?
                """,
                (reminder_key,)
            ).fetchone()

            if already_sent:
                continue

            user = bot.get_user(int(employee["discord_id"]))
            if user is None:
                try:
                    user = await bot.fetch_user(int(employee["discord_id"]))
                except discord.HTTPException:
                    continue

            try:
                await user.send(
                    f"Напоминание Karma: сегодня ещё не отправлен отчёт "
                    f"по Server **{employee['direction']}**."
                )

                db.execute(
                    """
                    INSERT INTO audit_log
                    (action, details, created_at)
                    VALUES ('REMINDER_SENT', ?, ?)
                    """,
                    (reminder_key, now_iso())
                )

            except discord.Forbidden:
                print(
                    f"Cannot DM employee {employee['discord_id']}: "
                    "private messages are disabled."
                )


async def ensure_report_panel(channel, server_name):
    with database() as db:
        saved = db.execute("SELECT message_id FROM bot_messages WHERE channel_id=? AND purpose='report_panel'", (str(channel.id),)).fetchone()
    message = None
    if saved:
        try:
            message = await channel.fetch_message(int(saved["message_id"]))
        except discord.HTTPException:
            message = None
    embed = make_embed("KARMA NOIRE | BIZINFO", "ЕЖЕДНЕВНАЯ ПОДАЧА ОТЧЁТА")
    embed.add_field(name="SERVER", value=server_name, inline=True)
    embed.add_field(name="ПОРЯДОК", value="Нажми кнопку и отправь скриншот следующим сообщением. Рабочий канал предназначен только для отчётов.", inline=False)
    if message:
        await message.edit(embed=embed, view=SubmitView())
    else:
        message = await channel.send(embed=embed, view=SubmitView())
    with database() as db:
        db.execute("INSERT OR REPLACE INTO bot_messages (channel_id, message_id, purpose) VALUES (?, ?, 'report_panel')", (str(channel.id), str(message.id)))
    # Remove everything else, leaving exactly one panel message.
    async for old in channel.history(limit=None):
        if old.id != message.id:
            try:
                await old.delete()
            except discord.HTTPException:
                pass


@tasks.loop(time=time(hour=5, minute=0, tzinfo=SERVER_TZ))
async def daily_cleanup():
    with database() as db:
        db.execute("DELETE FROM report_sessions")
    for guild in bot.guilds:
        for server_name, channel_name in WORK_CHANNELS.items():
            channel = discord.utils.get(guild.text_channels, name=channel_name)
            if channel:
                try:
                    await ensure_report_panel(channel, server_name)
                except Exception as error:
                    print(f"Daily cleanup failed for {channel_name}: {error}")
        stats_channel = discord.utils.get(guild.text_channels, name=STATS_CHANNEL_NAME)
        if stats_channel:
            with database() as db:
                server_rows = db.execute("""
                    SELECT s.name, s.business_count,
                           COUNT(e.id) AS employee_count,
                           GROUP_CONCAT(e.discord_id, ',') AS employee_ids
                    FROM servers s LEFT JOIN employees e
                    ON e.direction=s.name AND e.status='active'
                    GROUP BY s.name
                    ORDER BY s.name
                """).fetchall()
            overview = make_embed("KARMA NOIRE | ЕЖЕДНЕВНАЯ СВОДКА", f"Рабочая дата: **{get_work_date()}**")
            for row in server_rows:
                people = ", ".join(f"<@{uid}>" for uid in (row["employee_ids"] or "").split(",") if uid) or "Нет сотрудников"
                overview.add_field(name=f"SERVER: {SERVER_CODES.get(row['name'],'—')} • {row['name']}",
                                   value=f"Сотрудников: **{row['employee_count']}**\n{people}", inline=True)
            with database() as db:
                saved_stats = db.execute("SELECT message_id FROM bot_messages WHERE channel_id=? AND purpose='daily_stats'", (str(stats_channel.id),)).fetchone()
            stats_message = None
            if saved_stats:
                try:
                    stats_message = await stats_channel.fetch_message(int(saved_stats["message_id"]))
                except discord.HTTPException:
                    pass
            if stats_message:
                await stats_message.edit(embed=overview)
            else:
                stats_message = await stats_channel.send(embed=overview)
            with database() as db:
                db.execute("INSERT OR REPLACE INTO bot_messages (channel_id,message_id,purpose) VALUES (?,?,'daily_stats')", (str(stats_channel.id), str(stats_message.id)))


@daily_cleanup.before_loop
async def before_daily_cleanup():
    await bot.wait_until_ready()


@daily_reminder.before_loop
async def before_daily_reminder():
    await bot.wait_until_ready()


# ============================================================
# BASIC COMMANDS
# ============================================================

@bot.tree.command(
    name="ping",
    description="Проверить, работает ли Karma"
)
@app_commands.guild_only()
async def ping(interaction: discord.Interaction):
    embed = make_embed(
        "KARMA NOIRE | BIZINFO",
        "Система работает. Все основные службы на связи."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# INVITE EMPLOYEE
# ============================================================


async def division_autocomplete(interaction: discord.Interaction, current: str):
    with database() as db:
        rows = db.execute("SELECT name,server_name FROM divisions ORDER BY server_name,division_number").fetchall()
    options = [(name, SERVER_CODES[name]) for name in BASE_DIRECTIONS]
    options.extend((row["name"], SERVER_CODES.get(row["server_name"], "—")) for row in rows)
    return [app_commands.Choice(name=f"{code} • {name}", value=name)
            for name, code in options if current.casefold() in name.casefold()][:25]


@bot.tree.command(name="invite", description="Принять сотрудника в подразделение")
@app_commands.guild_only()
@app_commands.autocomplete(server=division_autocomplete)
async def invite(interaction: discord.Interaction, member: discord.Member, server: str):
    if not is_manager(interaction.user):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True); return
    if member.bot:
        await interaction.response.send_message("Нельзя принять бота на работу.", ephemeral=True); return
    with database() as db:
        active = get_employee_by_discord_id(db, member.id)
        if active:
            await interaction.response.send_message(f"Участник уже числится сотрудником: **{active['direction']}**.", ephemeral=True); return
        cur = db.execute("""
            INSERT INTO employees(discord_id,direction,joined_at,status,owner_id,point_rate,guild_id)
            VALUES(?,?,?,'active',?,?,?)
        """, (str(member.id), server, now_iso(), str(interaction.user.id), POINT_VALUE, str(interaction.guild_id)))
        employee_id = cur.lastrowid
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('INVITE',?,?)",
                   (f"employee={member.id}; server={server}; owner={interaction.user.id}", now_iso()))
    role = discord.utils.get(interaction.guild.roles, name=server)
    if role:
        try:
            await member.add_roles(role, reason="Принят на работу через Karma")
        except discord.Forbidden:
            with database() as db:
                db.execute("UPDATE employees SET status='archived',left_at=? WHERE id=? AND status='active'",
                           (now_iso(), employee_id))
            await interaction.response.send_message("Не удалось выдать роль. Запись о сотруднике отменена; проверь иерархию ролей.", ephemeral=True)
            return
    await interaction.response.send_message(
        f"{member.mention} принят в **{SERVER_CODES.get(server, '—')} • {server}**.\n"
        f"Ответственный владелец: {interaction.user.mention}\nID сотрудника: `{employee_id}`",
        ephemeral=True
    )



class UninviteView(discord.ui.View):
    def __init__(self, requester_id, member, employee_id, direction):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.member = member
        self.employee_id = employee_id
        self.direction = direction

    async def interaction_check(self, interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Подтвердить это действие может только инициатор команды.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Подтвердить увольнение", style=discord.ButtonStyle.secondary)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        role = discord.utils.get(interaction.guild.roles, name=self.direction)
        role_removed = True
        if role and role in self.member.roles:
            try:
                await self.member.remove_roles(role, reason="Увольнение через Karma")
            except discord.Forbidden:
                role_removed = False
            except discord.HTTPException:
                role_removed = False

        try:
            with database() as db:
                cur = db.execute("UPDATE employees SET status='archived',left_at=? WHERE id=? AND status='active'",
                                 (now_iso(), self.employee_id))
                if cur.rowcount == 0:
                    raise RuntimeError("Сотрудник уже не активен.")
                db.execute("UPDATE businesses SET employee_id=NULL WHERE employee_id=? AND status='active'", (self.employee_id,))
                db.execute("DELETE FROM report_sessions WHERE user_id=?", (str(self.member.id),))
                db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('UNINVITE',?,?)",
                           (f"employee={self.member.id}; direction={self.direction}; role_removed={role_removed}; actor={interaction.user.id}", now_iso()))
            with database() as db:
                _, _, unpaid = get_employee_totals(db, self.employee_id)
            for child in self.children:
                child.disabled = True
            role_note = "" if role_removed else "\n⚠️ Запись сотрудника закрыта, но Discord-роль не удалось снять. Подними роль Karma выше роли направления и сними её вручную."
            await interaction.followup.send(
                f"{self.member.mention} уволен с **{self.direction}**.\n"
                f"Неоплаченные баллы: **{unpaid}**.\n"
                f"История сохранена.{role_note}",
                ephemeral=True
            )
        except Exception as error:
            print(f"Uninvite error: {error}")
            await interaction.followup.send("Не удалось завершить увольнение в базе. Проверь логи Railway.", ephemeral=True)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Увольнение отменено.", view=self)


@bot.tree.command(
    name="uninvite",
    description="Уволить сотрудника и сохранить его историю"
)
@app_commands.guild_only()
async def uninvite(
    interaction: discord.Interaction,
    member: discord.Member
):
    if not is_manager(interaction.user):
        await interaction.response.send_message(
            "У тебя нет прав для этой команды.",
            ephemeral=True
        )
        return

    with database() as db:
        employee = get_employee_by_discord_id(db, member.id)

        if not employee:
            await interaction.response.send_message(
                "Этот пользователь не числится активным сотрудником.",
                ephemeral=True
            )
            return

        employee_id = employee["id"]
        direction = employee["direction"]
        rate = int(employee["point_rate"] or POINT_VALUE)
        _, _, unpaid = get_employee_totals(db, employee_id)
        unpaid_amount = get_employee_unpaid_amount(db, employee_id)

    embed = make_embed(
        "KARMA NOIRE | УВОЛЬНЕНИЕ",
        "Проверь данные перед подтверждением."
    )
    embed.add_field(name="СОТРУДНИК", value=member.mention, inline=True)
    embed.add_field(name="SERVER", value=direction, inline=True)
    embed.add_field(name="НЕОПЛАЧЕННЫЕ БАЛЛЫ", value=str(unpaid), inline=True)
    embed.add_field(
        name="К ВЫПЛАТЕ",
        value=money(unpaid_amount),
        inline=False
    )
    embed.set_footer(text="История сотрудника останется в архиве.")

    await interaction.response.send_message(
        embed=embed,
        view=UninviteView(
            interaction.user.id,
            member,
            employee_id,
            direction
        ),
        ephemeral=True
    )



async def render_employee_stats(interaction: discord.Interaction, target: discord.abc.User):
    with database() as db:
        employee = get_employee_by_discord_id(db, target.id, active_only=False)
        if not employee:
            await interaction.response.send_message("История сотрудника не найдена.", ephemeral=True); return
        earned, paid, unpaid = get_employee_totals(db, employee["id"])
        earned_amount = get_employee_earned_amount(db, employee["id"])
        unpaid_amount = get_employee_unpaid_amount(db, employee["id"])
        rate = int(employee["point_rate"] or POINT_VALUE)
        approved = db.execute("SELECT COUNT(*) FROM submissions WHERE employee_id=? AND status='approved'", (employee["id"],)).fetchone()[0]
        rejected = db.execute("SELECT COUNT(*) FROM submissions WHERE employee_id=? AND status='rejected'", (employee["id"],)).fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM submissions WHERE employee_id=? AND status='pending'", (employee["id"],)).fetchone()[0]
        daily = db.execute("SELECT work_date,SUM(points) points FROM daily_entries WHERE employee_id=? GROUP BY work_date ORDER BY work_date DESC LIMIT 7", (employee["id"],)).fetchall()
    embed = make_embed("KARMA NOIRE | СТАТИСТИКА", f"Сотрудник: {target.mention}")
    embed.add_field(name="SERVER", value=f"{employee['direction']} • №{SERVER_CODES.get(employee['direction'],'—')}", inline=True)
    embed.add_field(name="СТАТУС", value=employee["status"], inline=True)
    embed.add_field(name="СТАВКА", value=f"{money(rate)} / балл", inline=True)
    embed.add_field(name="ЗАРАБОТАНО", value=f"{earned} баллов\n{money(earned_amount)}", inline=True)
    embed.add_field(name="ОПЛАЧЕНО", value=f"{paid} баллов", inline=True)
    embed.add_field(name="К ВЫПЛАТЕ", value=f"{unpaid} баллов\n{money(unpaid_amount)}", inline=True)
    embed.add_field(name="ОТЧЁТЫ", value=f"Принято: {approved}\nОтклонено: {rejected}\nОжидает: {pending}", inline=True)
    if daily:
        embed.add_field(name="ПОСЛЕДНИЕ РАБОЧИЕ ДНИ", value="\n".join(f"`{r['work_date']}` — **{r['points']}** баллов" for r in daily), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="Посмотреть собственную статистику")
@app_commands.guild_only()
async def stats(interaction: discord.Interaction):
    await render_employee_stats(interaction, interaction.user)


@bot.tree.command(name="employee_stats", description="Владельцу: посмотреть статистику сотрудника")
@app_commands.guild_only()
async def employee_stats(interaction: discord.Interaction, member: discord.Member):
    if not is_manager(interaction.user):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True); return
    await render_employee_stats(interaction, member)


@bot.tree.command(
    name="paid",
    description="Зафиксировать выплату сотруднику"
)
@app_commands.guild_only()
async def paid(
    interaction: discord.Interaction,
    member: discord.Member
):
    if not is_manager(interaction.user):
        await interaction.response.send_message(
            "У тебя нет прав для этой команды.",
            ephemeral=True
        )
        return

    with database() as db:
        employee = get_employee_by_discord_id(
            db, member.id, active_only=False
        )

        if not employee:
            await interaction.response.send_message(
                "История этого сотрудника не найдена.",
                ephemeral=True
            )
            return

        _, _, unpaid = get_employee_totals(db, employee["id"])

        if unpaid <= 0:
            await interaction.response.send_message(
                "У сотрудника нет неоплаченных баллов.",
                ephemeral=True
            )
            return

        amount = get_employee_unpaid_amount(db, employee["id"])
        rate = int(employee["point_rate"] or POINT_VALUE)

        db.execute(
            """
            INSERT INTO payments
            (employee_id, points, amount, paid_at)
            VALUES (?, ?, ?, ?)
            """,
            (employee["id"], unpaid, amount, now_iso())
        )

        db.execute(
            """
            INSERT INTO audit_log
            (action, details, created_at)
            VALUES (?, ?, ?)
            """,
            (
                "PAYMENT_RECORDED",
                f"Employee {member.id}; points={unpaid}; amount={amount}",
                now_iso()
            )
        )

    await interaction.response.send_message(
        f"Выплата зафиксирована.\n"
        f"Сотрудник: {member.mention}\n"
        f"Баллы: **{unpaid}**\n"
        f"Сумма: **{money(amount)}** (ставка {money(rate)} за балл)",
        ephemeral=True
    )
    finance_channel = discord.utils.get(interaction.guild.text_channels, name=FINANCE_CHANNEL_NAME)
    if finance_channel:
        finance_embed = make_embed("KARMA NOIRE | ВЫПЛАТА ЗАФИКСИРОВАНА", f"Получатель: {member.mention}")
        finance_embed.add_field(name="БАЛЛЫ", value=f"{unpaid} {points_word(unpaid)}", inline=True)
        finance_embed.add_field(name="СУММА", value=money(amount), inline=True)
        finance_embed.add_field(name="SERVER", value=employee["direction"], inline=True)
        finance_embed.add_field(name="СТАВКА", value=money(rate), inline=True)
        await finance_channel.send(embed=finance_embed)


# ============================================================
# WEEKLY PAYROLL
# ============================================================

@bot.tree.command(
    name="payroll",
    description="Сформировать недельный отчёт по всем серверам"
)
@app_commands.guild_only()
async def payroll(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        await interaction.response.send_message(
            "У тебя нет прав для этой команды.",
            ephemeral=True
        )
        return

    today = datetime.strptime(get_work_date(), "%Y-%m-%d").date()
    monday = today - timedelta(days=today.weekday())
    start_date = monday.strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    with database() as db:
        rows = db.execute(
            """
            WITH entries AS (
                SELECT employee_id, work_date, points, (points * point_rate) AS amount FROM daily_entries
                UNION ALL
                SELECT employee_id, substr(created_at, 1, 10) AS work_date, points, (points * point_rate) AS amount FROM point_adjustments
            )
            SELECT
                e.id AS employee_id,
                e.discord_id,
                e.direction,
                e.status,
                e.point_rate,
                SUM(d.points) AS points,
                SUM(d.amount) AS amount
            FROM employees e
            JOIN entries d ON d.employee_id = e.id
            WHERE d.work_date BETWEEN ? AND ?
            GROUP BY e.id
            ORDER BY e.direction, e.id
            """,
            (start_date, end_date)
        ).fetchall()

        daily_rows = db.execute(
            """
            WITH entries AS (
                SELECT employee_id, work_date, points, (points * point_rate) AS amount FROM daily_entries
                UNION ALL
                SELECT employee_id, substr(created_at, 1, 10) AS work_date, points, (points * point_rate) AS amount FROM point_adjustments
            )
            SELECT employee_id, work_date, SUM(points) AS points, SUM(amount) AS amount
            FROM entries
            WHERE work_date BETWEEN ? AND ?
            GROUP BY employee_id, work_date
            ORDER BY work_date
            """,
            (start_date, end_date)
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            f"За период **{start_date} — {end_date}** начислений пока нет.",
            ephemeral=True
        )
        return

    daily_by_employee = {}
    for row in daily_rows:
        daily_by_employee.setdefault(row["employee_id"], []).append(
            f"{row['work_date']}: {row['points']}"
        )

    lines = []
    total_points = 0
    total_amount = 0

    for row in rows:
        points = int(row["points"] or 0)
        total_points += points
        row_amount = int(row["amount"] or 0)
        total_amount += row_amount

        try:
            user = await bot.fetch_user(int(row["discord_id"]))
            user_name = user.name
        except (discord.HTTPException, ValueError):
            user_name = f"ID {row['discord_id']}"

        daily_text = "; ".join(
            daily_by_employee.get(row["employee_id"], [])
        )

        lines.append(
            f"**{row['direction']}** — {user_name} ({row['status']})\n"
            f"Баллы: **{points} {points_word(points)}** • Сумма: **{money(row_amount)}**\n"
            f"По дням: {daily_text or 'нет данных'}"
        )

    embed = make_embed(
        "KARMA NOIRE | НЕДЕЛЬНАЯ ВЕДОМОСТЬ",
        f"Период: **{start_date} — {end_date}**"
    )

    # Keep each embed safely below Discord's field/description limits.
    chunks = []
    current = ""
    for line in lines:
        candidate = current + ("\n\n" if current else "") + line
        if len(candidate) > 3500:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    embed.description = chunks[0]
    embed.add_field(
        name="ОБЩИЙ ИТОГ",
        value=(
            f"Баллы: **{total_points} {points_word(total_points)}**\n"
            f"Сумма: **{money(total_amount)}**"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

    for chunk in chunks[1:]:
        extra = make_embed("KARMA NOIRE | НЕДЕЛЬНАЯ ВЕДОМОСТЬ")
        extra.description = chunk
        await interaction.followup.send(embed=extra, ephemeral=True)


# ============================================================
# ACTIVE EMPLOYEES AND SERVER OVERVIEW
# ============================================================

@bot.tree.command(
    name="employees",
    description="Показать список действующих сотрудников"
)
@app_commands.guild_only()
async def employees(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        await interaction.response.send_message(
            "У тебя нет прав для этой команды.",
            ephemeral=True
        )
        return

    with database() as db:
        rows = db.execute(
            """
            SELECT discord_id, direction, joined_at
            FROM employees
            WHERE status = 'active'
            ORDER BY direction
            """
        ).fetchall()

    embed = make_embed("KARMA NOIRE | СОТРУДНИКИ")

    if not rows:
        embed.description = "Активных сотрудников нет."
    else:
        for row in rows:
            embed.add_field(
                name=f"Server: {row['direction']}",
                value=f"<@{row['discord_id']}>\nПринят: {row['joined_at'][:10]}",
                inline=True
            )

    await interaction.response.send_message(embed=embed, ephemeral=True)



@bot.tree.command(name="servers", description="Показать игровые серверы, сотрудников и активы")
@app_commands.guild_only()
async def servers(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True); return
    with database() as db:
        divisions = db.execute("SELECT name,server_name,division_number FROM divisions ORDER BY server_name,division_number").fetchall()
        result = []
        for name in BASE_DIRECTIONS:
            division_names = [row["name"] for row in divisions if row["server_name"] == name]
            names = [name] + division_names
            placeholders = ",".join("?" for _ in names)
            staff = db.execute(f"SELECT discord_id,direction FROM employees WHERE direction IN ({placeholders}) AND status='active' ORDER BY direction,id", names).fetchall()
            biz_count = db.execute("SELECT COUNT(*) FROM businesses WHERE server_name=? AND status='active'", (name,)).fetchone()[0]
            result.append((name, division_names, staff, biz_count))
    embed = make_embed("KARMA NOIRE | SERVERS", "Общая статистика игровых серверов и их подразделений")
    for name, division_names, staff, biz_count in result:
        staff_text = ", ".join(f"<@{r['discord_id']}>" for r in staff) or "Нет сотрудников"
        embed.add_field(name=f"{SERVER_CODES[name]} • {name} — ВСЕГО",
                        value=f"Подразделений: **{1 + len(division_names)}**\nСотрудников: **{len(staff)}**\nБизнесов: **{biz_count}**\n{staff_text}"[:1024], inline=False)
        for division in [name] + division_names:
            division_staff = [r for r in staff if r["direction"] == division]
            division_text = ", ".join(f"<@{r['discord_id']}>" for r in division_staff) or "Нет сотрудников"
            embed.add_field(name=f"↳ {division}", value=f"Сотрудников: **{len(division_staff)}**\n{division_text}"[:1024], inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="setup_reports",
    description="Разместить кнопки отправки отчётов"
)
@app_commands.guild_only()
async def setup_reports(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        await interaction.response.send_message(
            "У тебя нет прав для этой команды.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    posted = []
    missing = []

    for direction, channel_name in WORK_CHANNELS.items():
        channel = discord.utils.get(
            interaction.guild.text_channels,
            name=channel_name
        )

        if channel is None:
            missing.append(channel_name)
            continue

        await ensure_report_panel(channel, direction)
        posted.append(direction)

    result = "Панели отчётов готовы: " + (", ".join(posted) if posted else "нет")

    if missing:
        result += "\nНе найдены каналы: " + ", ".join(missing)

    await interaction.followup.send(result, ephemeral=True)


# ============================================================
# OWNER POINT ADJUSTMENTS
# ============================================================

async def adjust_employee_points(interaction, member, points, reason, action):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Эта команда доступна только владельцу.", ephemeral=True)
        return
    with database() as db:
        employee = get_employee_by_discord_id(db, member.id, active_only=False)
        if not employee:
            await interaction.response.send_message("История сотрудника не найдена.", ephemeral=True)
            return
        signed = points if action == "add" else -points
        current = get_employee_totals(db, employee["id"])[2]
        if action == "remove" and points > current:
            await interaction.response.send_message(f"Нельзя снять {points} баллов: доступно к корректировке не более {current} неоплаченных баллов.", ephemeral=True)
            return
        db.execute("INSERT INTO point_adjustments (employee_id, points, reason, admin_id, created_at, point_rate) VALUES (?, ?, ?, ?, ?, ?)",
                   (employee["id"], signed, reason, str(interaction.user.id), now_iso(), int(employee["point_rate"] or POINT_VALUE)))
        db.execute("INSERT INTO audit_log (action, details, created_at) VALUES (?, ?, ?)",
                   ("POINTS_ADDED" if action == "add" else "POINTS_REMOVED",
                    f"employee={member.id}; points={signed}; reason={reason}; owner={interaction.user.id}", now_iso()))
        _, _, unpaid = get_employee_totals(db, employee["id"])
        unpaid_amount = get_employee_unpaid_amount(db, employee["id"])
    embed = make_embed("KARMA NOIRE | КОРРЕКТИРОВКА БАЛАНСА", f"Баланс сотрудника {member.mention} обновлён.")
    embed.add_field(name="SERVER", value=employee["direction"], inline=True)
    embed.add_field(name="ИЗМЕНЕНИЕ", value=f"{ '+' if signed > 0 else ''}{signed} {points_word(abs(signed))}", inline=True)
    embed.add_field(name="ПРИЧИНА", value=reason, inline=False)
    embed.add_field(name="ТЕКУЩИЙ ДОЛГ", value=f"{unpaid} {points_word(unpaid)} • {money(unpaid_amount)}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    channel = discord.utils.get(interaction.guild.text_channels, name=FINANCE_CHANNEL_NAME)
    if channel:
        await channel.send(embed=embed)


def points_word(value):
    n = abs(int(value)) % 100
    if 11 <= n <= 14:
        return "баллов"
    n %= 10
    if n == 1:
        return "балл"
    if 2 <= n <= 4:
        return "балла"
    return "баллов"


@bot.tree.command(name="add_points", description="Владелец: начислить сотруднику баллы")
@app_commands.guild_only()
async def add_points(interaction: discord.Interaction, member: discord.Member,
                     points: app_commands.Range[int, 1, 100000], reason: app_commands.Range[str, 3, 300]):
    await adjust_employee_points(interaction, member, points, reason, "add")


@bot.tree.command(name="remove_points", description="Владелец: снять у сотрудника баллы")
@app_commands.guild_only()
async def remove_points(interaction: discord.Interaction, member: discord.Member,
                        points: app_commands.Range[int, 1, 100000], reason: app_commands.Range[str, 3, 300]):
    await adjust_employee_points(interaction, member, points, reason, "remove")


# ============================================================
# TICKETS: APPLICATION PANEL, PRIVATE CHANNELS AND REVIEW
# ============================================================

class TicketApplicationModal(discord.ui.Modal, title="KARMA NOIRE • АНКЕТА"):
    nickname = discord.ui.TextInput(label="Игровой никнейм", placeholder="Укажи свой игровой ник", max_length=80)
    game_server = discord.ui.TextInput(label="Игровой сервер", placeholder="Mesa / Surprise / Prescott / Faraway", max_length=80)
    age = discord.ui.TextInput(label="Возраст", placeholder="Полных лет", max_length=3)
    real_name = discord.ui.TextInput(label="Реальное имя", placeholder="Как к тебе обращаться", max_length=80)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Заявка создаётся только на сервере.", ephemeral=True)
            return
        if not self.age.value.strip().isdigit() or not (13 <= int(self.age.value.strip()) <= 99):
            await interaction.response.send_message("Укажи корректный возраст цифрами (от 13 до 99).", ephemeral=True)
            return
        with database() as db:
            existing = db.execute("SELECT id FROM tickets WHERE guild_id=? AND user_id=? AND status='pending'",
                                  (str(interaction.guild.id), str(interaction.user.id))).fetchone()
        if existing:
            await interaction.response.send_message("У тебя уже есть открытая заявка. Продолжай общение в своём приватном канале.", ephemeral=True)
            return
        category = discord.utils.get(interaction.guild.categories, name=TICKET_CATEGORY_NAME)
        if not category:
            await interaction.response.send_message(f"Не найдена категория **{TICKET_CATEGORY_NAME}**. Сообщи администрации.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        overwrites = {interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        overwrites[interaction.user] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        for role in interaction.guild.roles:
            if role.name in ADMIN_ROLE_NAMES:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        if interaction.guild.me:
            overwrites[interaction.guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True, attach_files=True, embed_links=True)
        try:
            channel = await interaction.guild.create_text_channel(
                name=f"заявка-{interaction.user.id % 100000:05d}", category=category,
                overwrites=overwrites, topic=f"KARMA NOIRE • заявка пользователя {interaction.user.id}"
            )
        except discord.Forbidden:
            await interaction.followup.send("Karma не может создать приватный канал. Проверь право Manage Channels.", ephemeral=True)
            return
        except discord.HTTPException as error:
            print(f"Ticket channel creation error: {error}")
            await interaction.followup.send("Не удалось создать приватный канал. Попробуй позже или сообщи администрации.", ephemeral=True)
            return
        with database() as db:
            cur = db.execute("INSERT INTO tickets (guild_id,user_id,channel_id,nickname,game_server,age,real_name,created_at) VALUES (?,?,?,?,?,?,?,?)",
                             (str(interaction.guild.id), str(interaction.user.id), str(channel.id), self.nickname.value.strip(), self.game_server.value.strip(), self.age.value.strip(), self.real_name.value.strip(), now_iso()))
            ticket_id = cur.lastrowid
            db.execute("INSERT INTO audit_log (action,details,created_at) VALUES ('TICKET_CREATED',?,?)",
                       (f"ticket={ticket_id}; user={interaction.user.id}", now_iso()))
        embed = make_embed("K A R M A  N O I R E  •  ЗАЯВКА НА ВСТУПЛЕНИЕ",
                           "**НОВЫЙ КАНДИДАТ**\\nАнкета передана администрации. До принятия решения общение проходит в этом приватном канале.")
        embed.add_field(name="01  •  ИГРОВОЙ НИК", value=self.nickname.value.strip(), inline=True)
        embed.add_field(name="02  •  ИГРОВОЙ СЕРВЕР", value=self.game_server.value.strip(), inline=True)
        embed.add_field(name="03  •  ВОЗРАСТ", value=self.age.value.strip(), inline=True)
        embed.add_field(name="04  •  РЕАЛЬНОЕ ИМЯ", value=self.real_name.value.strip(), inline=True)
        embed.add_field(name="СТАТУС", value="`ОЖИДАЕТ РАССМОТРЕНИЯ`", inline=False)
        embed.set_footer(text="KARMA NOIRE  •  RECRUITMENT DIVISION")
        await channel.send(content=interaction.user.mention, embed=embed, view=TicketControlView())
        log = discord.utils.get(interaction.guild.text_channels, name=TICKET_LOG_CHANNEL_NAME)
        if log:
            await log.send(embed=make_embed("KARMA NOIRE | НОВАЯ ЗАЯВКА", f"{interaction.user.mention} создал {channel.mention}. Заявка №{ticket_id}."))
        await interaction.followup.send(f"Приватная заявка создана: {channel.mention}.", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать запрос", emoji="✦", style=discord.ButtonStyle.secondary, custom_id="karma:ticket:create")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketApplicationModal())


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def get_ticket(self, interaction):
        with database() as db:
            return db.execute("SELECT * FROM tickets WHERE channel_id=? AND status='pending'", (str(interaction.channel_id),)).fetchone()

    @discord.ui.button(label="Принять • Новобранец", emoji="✦", style=discord.ButtonStyle.success, custom_id="karma:ticket:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction.user):
            await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
            return
        ticket = await self.get_ticket(interaction)
        if not ticket:
            await interaction.response.send_message("Открытая заявка не найдена.", ephemeral=True)
            return
        role = discord.utils.get(interaction.guild.roles, name=RECRUIT_ROLE_NAME)
        member = interaction.guild.get_member(int(ticket["user_id"]))
        if not role or not member:
            await interaction.response.send_message("Не найдена роль «Новобранец» или пользователь уже покинул сервер.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Karma Noire: заявка принята")
            await restore_member_visibility(member)
        except discord.Forbidden:
            await interaction.response.send_message("Бот не может выдать роль или восстановить доступ. Проверь права и иерархию ролей.", ephemeral=True)
            return
        with database() as db:
            db.execute("UPDATE tickets SET status='accepted', reviewer_id=?, closed_at=? WHERE id=? AND status='pending'",
                       (str(interaction.user.id), now_iso(), ticket["id"]))
            db.execute("INSERT INTO audit_log (action,details,created_at) VALUES ('TICKET_ACCEPTED',?,?)",
                       (f"ticket={ticket['id']}; user={ticket['user_id']}; reviewer={interaction.user.id}", now_iso()))
        await interaction.response.edit_message(
            embed=make_embed("K A R M A  N O I R E  •  ДОПУСК ОДОБРЕН",
                             f"{member.mention} принят в организацию.\\nВыдана роль **{RECRUIT_ROLE_NAME}**.\\n\\nПерсональный канал будет удалён."),
            view=None
        )
        try:
            await member.send("Твоя заявка в KARMA NOIRE одобрена. Тебе выдана роль «Новобранец». Добро пожаловать.")
        except discord.HTTPException:
            pass
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"Karma: заявка {ticket['id']} принята")
        except discord.HTTPException as error:
            print(f"Accepted ticket channel deletion failed: {error}")

    @discord.ui.button(label="Отклонить", emoji="✕", style=discord.ButtonStyle.danger, custom_id="karma:ticket:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction.user):
            await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
            return
        ticket = await self.get_ticket(interaction)
        if not ticket:
            await interaction.response.send_message("Открытая заявка не найдена.", ephemeral=True)
            return
        member = interaction.guild.get_member(int(ticket["user_id"]))
        if member:
            try:
                await member.send("Твоя заявка в KARMA NOIRE отклонена. Доступ к серверу закрыт.")
            except discord.HTTPException:
                pass
            try:
                await member.kick(reason=f"Karma Noire: заявка {ticket['id']} отклонена")
            except discord.Forbidden:
                await interaction.response.send_message("Не удалось исключить пользователя. Проверь право Kick Members и иерархию ролей. Канал заявки пока оставлен.", ephemeral=True)
                return
            except discord.HTTPException as error:
                await interaction.response.send_message(f"Discord не смог исключить пользователя: {error}. Канал заявки пока оставлен.", ephemeral=True)
                return
        with database() as db:
            db.execute("UPDATE tickets SET status='rejected', reviewer_id=?, closed_at=? WHERE id=? AND status='pending'",
                       (str(interaction.user.id), now_iso(), ticket["id"]))
            db.execute("INSERT INTO audit_log (action,details,created_at) VALUES ('TICKET_REJECTED',?,?)",
                       (f"ticket={ticket['id']}; user={ticket['user_id']}; reviewer={interaction.user.id}", now_iso()))
        await interaction.response.edit_message(
            embed=make_embed("K A R M A  N O I R E  •  ЗАЯВКА ОТКЛОНЕНА",
                             "Заявка отклонена. Пользователь исключён с сервера.\\nПерсональный канал будет удалён."),
            view=None
        )
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"Karma: заявка {ticket['id']} отклонена")
        except discord.HTTPException as error:
            print(f"Rejected ticket channel deletion failed: {error}")

    @discord.ui.button(label="Закрыть тикет", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="karma:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction.user):
            await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
            return
        with database() as db:
            db.execute("UPDATE tickets SET status=CASE WHEN status='pending' THEN 'closed' ELSE status END, reviewer_id=COALESCE(reviewer_id, ?), closed_at=? WHERE channel_id=? AND closed_at IS NULL",
                       (str(interaction.user.id), now_iso(), str(interaction.channel_id)))
            db.execute("INSERT INTO audit_log (action,details,created_at) VALUES ('TICKET_CLOSED',?,?)",
                       (f"channel={interaction.channel_id}; closer={interaction.user.id}", now_iso()))
        await interaction.response.send_message("Тикет будет закрыт через 5 секунд.", ephemeral=True)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass


@bot.tree.command(name="setup_tickets", description="Опубликовать панель создания заявок")
@app_commands.guild_only()
async def setup_tickets(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    channel = discord.utils.get(interaction.guild.text_channels, name=TICKET_PANEL_CHANNEL_NAME)
    if not channel:
        await interaction.followup.send(f"Не найден канал {TICKET_PANEL_CHANNEL_NAME}.", ephemeral=True)
        return
    with database() as db:
        saved = db.execute("SELECT message_id FROM bot_messages WHERE channel_id=? AND purpose='ticket_panel'", (str(channel.id),)).fetchone()
    msg = None
    if saved:
        try:
            msg = await channel.fetch_message(int(saved["message_id"]))
        except discord.HTTPException:
            pass
    embed = make_embed(
        "K A R M A  N O I R E",
        "**RECRUITMENT • BIZINFO**\n\n"
        "Добро пожаловать. Вступление в организацию начинается с анкеты.\n\n"
        "Нажми кнопку ниже и укажи игровой ник, реальное имя, игровой сервер и возраст. "
        "После отправки Karma откроет персональный приватный канал для рассмотрения заявки."
    )
    embed.add_field(name="ПОРЯДОК ВСТУПЛЕНИЯ", value="`01` Заполни анкету\n`02` Дождись проверки администрации\n`03` Получи решение по заявке", inline=False)
    embed.set_footer(text="KARMA NOIRE  •  PRIVATE RECRUITMENT")
    if msg:
        await msg.edit(embed=embed, view=TicketPanelView())
    else:
        msg = await channel.send(embed=embed, view=TicketPanelView())
    with database() as db:
        db.execute("INSERT OR REPLACE INTO bot_messages (channel_id,message_id,purpose) VALUES (?,?,'ticket_panel')", (str(channel.id), str(msg.id)))
    await interaction.followup.send("Панель заявок готова.", ephemeral=True)




@bot.tree.command(name="set_employee_owner", description="Закрепить сотрудника за одним из владельцев")
@app_commands.guild_only()
async def set_employee_owner(interaction: discord.Interaction, member: discord.Member, owner: discord.Member):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Команда доступна только владельцам.", ephemeral=True); return
    if not is_owner(owner):
        await interaction.response.send_message("У выбранного участника нет роли «Владелец».", ephemeral=True); return
    with database() as db:
        employee = get_employee_by_discord_id(db, member.id)
        if not employee:
            await interaction.response.send_message("Активный сотрудник не найден.", ephemeral=True); return
        db.execute("UPDATE employees SET owner_id=? WHERE id=?", (str(owner.id), employee["id"]))
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('EMPLOYEE_OWNER_CHANGED',?,?)",
                   (f"employee={member.id}; owner={owner.id}; actor={interaction.user.id}", now_iso()))
    await interaction.response.send_message(f"{member.mention} закреплён за владельцем {owner.mention}.", ephemeral=True)


# ============================================================
# BUSINESS REGISTER AND EMPLOYEE ASSIGNMENTS
# ============================================================

@bot.tree.command(name="add_biz", description="Добавить бизнес и указать его владельца")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in BASE_DIRECTIONS])
async def add_biz(interaction: discord.Interaction, server: app_commands.Choice[str],
                  owner: discord.Member, business_id: app_commands.Range[int, 1, 999999999]):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Добавлять бизнесы могут только участники с ролью «Владелец».", ephemeral=True); return
    try:
        with database() as db:
            db.execute("INSERT INTO businesses(business_id,server_name,employee_id,added_by,created_at,status,owner_id) VALUES(?,?,NULL,?,?,'active',?)",
                       (str(business_id), server.value, str(interaction.user.id), now_iso(), str(owner.id)))
            db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('BIZ_ADDED',?,?)",
                       (f"biz={business_id}; server={server.value}; owner={owner.id}; actor={interaction.user.id}", now_iso()))
    except sqlite3.IntegrityError:
        await interaction.response.send_message("Бизнес с таким ID уже есть в реестре этого игрового сервера.", ephemeral=True); return
    await interaction.response.send_message(
        f"Бизнес **{business_id}** добавлен.\nИгровой сервер: **{SERVER_CODES[server.value]} • {server.value}**\nВладелец: {owner.mention}", ephemeral=True)


@bot.tree.command(name="remove_biz", description="Убрать бизнес из общего реестра")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in BASE_DIRECTIONS])
async def remove_biz(interaction: discord.Interaction, server: app_commands.Choice[str],
                     business_id: app_commands.Range[int, 1, 999999999]):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Команда доступна только владельцам.", ephemeral=True); return
    with database() as db:
        cur = db.execute("UPDATE businesses SET status='removed' WHERE business_id=? AND server_name=? AND status='active'",
                         (str(business_id), server.value))
        if cur.rowcount == 0:
            await interaction.response.send_message("Активный бизнес с таким ID не найден.", ephemeral=True); return
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('BIZ_REMOVED',?,?)",
                   (f"biz={business_id}; server={server.value}; actor={interaction.user.id}", now_iso()))
    await interaction.response.send_message(f"Бизнес **{business_id}** удалён из активного реестра. История сохранена.", ephemeral=True)


@bot.tree.command(name="assign_biz", description="Изменить владельца зарегистрированного бизнеса")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in BASE_DIRECTIONS])
async def assign_biz(interaction: discord.Interaction, server: app_commands.Choice[str],
                     business_id: app_commands.Range[int, 1, 999999999], owner: discord.Member):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Команда доступна только владельцам.", ephemeral=True); return
    with database() as db:
        cur = db.execute("UPDATE businesses SET owner_id=?,employee_id=NULL,added_by=? WHERE business_id=? AND server_name=? AND status='active'",
                         (str(owner.id), str(interaction.user.id), str(business_id), server.value))
        if cur.rowcount == 0:
            await interaction.response.send_message("Активный бизнес не найден.", ephemeral=True); return
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('BIZ_OWNER_CHANGED',?,?)",
                   (f"biz={business_id}; server={server.value}; owner={owner.id}; actor={interaction.user.id}", now_iso()))
    await interaction.response.send_message(f"Владелец бизнеса **{business_id}** изменён: {owner.mention}.", ephemeral=True)


@bot.tree.command(name="unassign_biz", description="Снять владельца с бизнеса")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in BASE_DIRECTIONS])
async def unassign_biz(interaction: discord.Interaction, server: app_commands.Choice[str],
                       business_id: app_commands.Range[int, 1, 999999999]):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Команда доступна только владельцам.", ephemeral=True); return
    with database() as db:
        cur = db.execute("UPDATE businesses SET owner_id=NULL,added_by=? WHERE business_id=? AND server_name=? AND status='active'",
                         (str(interaction.user.id), str(business_id), server.value))
        if cur.rowcount == 0:
            await interaction.response.send_message("Активный бизнес не найден.", ephemeral=True); return
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('BIZ_OWNER_CLEARED',?,?)",
                   (f"biz={business_id}; server={server.value}; actor={interaction.user.id}", now_iso()))
    await interaction.response.send_message(f"Владелец бизнеса **{business_id}** снят. Бизнес остался в реестре.", ephemeral=True)


@bot.tree.command(name="bizinfo", description="Показать общий реестр бизнесов по владельцам")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in BASE_DIRECTIONS])
async def bizinfo(interaction: discord.Interaction, server: app_commands.Choice[str] = None,
                  owner: discord.Member = None):
    if not is_manager(interaction.user):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True); return
    query = "SELECT business_id,server_name,owner_id FROM businesses WHERE status='active'"
    params = []
    if server:
        query += " AND server_name=?"; params.append(server.value)
    if owner:
        query += " AND owner_id=?"; params.append(str(owner.id))
    with database() as db:
        rows = db.execute(query, params).fetchall()
        org_query = "SELECT server_name,organization_name,owner_id FROM organizations WHERE status='active'"
        org_params = []
        if server:
            org_query += " AND server_name=?"; org_params.append(server.value)
        if owner:
            org_query += " AND owner_id=?"; org_params.append(str(owner.id))
        orgs = db.execute(org_query, org_params).fetchall()
    owner_names = {}
    owner_ids = {str(r["owner_id"]) for r in rows + orgs if r["owner_id"]}
    for owner_id in owner_ids:
        member = interaction.guild.get_member(int(owner_id)) if interaction.guild else None
        if member is None:
            try:
                member = await bot.fetch_user(int(owner_id))
            except (discord.HTTPException, ValueError):
                member = None
        owner_names[owner_id] = (member.display_name if isinstance(member, discord.Member) else member.name).casefold() if member else f"~{owner_id}"
    def owner_sort_key(uid):
        return (owner_names.get(str(uid), "~"), str(uid or ""))
    rows = sorted(rows, key=lambda r: (r["server_name"], owner_sort_key(r["owner_id"]), int(r["business_id"]) if str(r["business_id"]).isdigit() else str(r["business_id"])))
    orgs = sorted(orgs, key=lambda r: (r["server_name"], owner_sort_key(r["owner_id"]), r["organization_name"].casefold()))
    embed = make_embed("KARMA NOIRE | BIZINFO", "ОБЩИЙ РЕЕСТР АКТИВОВ")
    if orgs:
        embed.add_field(name="ЧАСТНЫЕ ОРГАНИЗАЦИИ", value="\u200b", inline=False)
        grouped_orgs = {}
        for row in orgs:
            grouped_orgs.setdefault(row["server_name"], []).append(f"**{row['organization_name']}** — <@{row['owner_id']}>")
        for name, lines in grouped_orgs.items():
            for offset in range(0, len(lines), 15):
                embed.add_field(name=f"{SERVER_CODES.get(name,'—')} • {name}", value="\n".join(lines[offset:offset+15])[:1024], inline=False)
    if rows:
        embed.add_field(name="БИЗНЕСЫ", value="\u200b", inline=False)
        grouped = {}
        for row in rows:
            grouped.setdefault(row["server_name"], []).append(f"`{row['business_id']}` — <@{row['owner_id']}>" if row["owner_id"] else f"`{row['business_id']}` — владелец не указан")
        for name, lines in grouped.items():
            for offset in range(0, len(lines), 15):
                embed.add_field(name=f"{SERVER_CODES.get(name,'—')} • {name} ({len(lines)})", value="\n".join(lines[offset:offset+15])[:1024], inline=False)
    if not rows and not orgs:
        embed.description = "Активных организаций и бизнесов по заданным параметрам нет."
    embed.set_footer(text=f"Всего организаций: {len(orgs)} • Всего активных бизнесов: {len(rows)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="my_biz", description="Посмотреть бизнесы, владельцем которых ты являешься")
@app_commands.guild_only()
async def my_biz(interaction: discord.Interaction):
    with database() as db:
        rows = db.execute("SELECT business_id,server_name FROM businesses WHERE owner_id=? AND status='active' ORDER BY server_name,CAST(business_id AS INTEGER)", (str(interaction.user.id),)).fetchall()
    if not rows:
        await interaction.response.send_message("За тобой не зарегистрировано активных бизнесов.", ephemeral=True); return
    lines = [f"**{SERVER_CODES.get(r['server_name'],'—')} • {r['server_name']}** — `{r['business_id']}`" for r in rows]
    await interaction.response.send_message("**Твои бизнесы:**\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="add_org", description="Добавить частную организацию в реестр активов")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in BASE_DIRECTIONS])
async def add_org(interaction: discord.Interaction, server: app_commands.Choice[str],
                  organization_name: app_commands.Range[str, 2, 80], owner: discord.Member):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Добавлять организации могут только участники с ролью «Владелец».", ephemeral=True); return
    name = organization_name.strip()
    if not name:
        await interaction.response.send_message("Название организации не может быть пустым.", ephemeral=True); return
    try:
        with database() as db:
            db.execute("INSERT INTO organizations(server_name,organization_name,owner_id,added_by,created_at,status) VALUES(?,?,?,?,?,'active')",
                       (server.value, name, str(owner.id), str(interaction.user.id), now_iso()))
            db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('ORG_ADDED',?,?)",
                       (f"organization={name}; server={server.value}; owner={owner.id}; actor={interaction.user.id}", now_iso()))
    except sqlite3.IntegrityError:
        await interaction.response.send_message("Организация с таким названием уже зарегистрирована на этом сервере.", ephemeral=True); return
    await interaction.response.send_message(f"Организация **{name}** добавлена.\nСервер: **{SERVER_CODES[server.value]} • {server.value}**\nВладелец: {owner.mention}", ephemeral=True)


def division_channel_sort_key(channel):
    name = channel.name
    for server_index, base in enumerate(BASE_DIRECTIONS):
        base_channel = BASE_WORK_CHANNELS[base]
        if name == base_channel:
            return (server_index, 1)
        prefix = base_channel + "・"
        if name.startswith(prefix):
            suffix = name[len(prefix):]
            if suffix.isdigit():
                return (server_index, int(suffix))
    return (len(BASE_DIRECTIONS), channel.position)


async def reorder_work_channels(category):
    known_names = set(WORK_CHANNELS.values())
    children = list(category.channels)
    known = [c for c in children if isinstance(c, discord.TextChannel) and c.name in known_names]
    other = [c for c in children if c not in known]
    ordered = sorted(known, key=division_channel_sort_key) + sorted(other, key=lambda c: c.position)
    for position, channel in enumerate(ordered):
        if channel.position != position:
            await channel.edit(position=position, reason="Karma: автоматическая сортировка рабочих каналов")


@bot.tree.command(name="create_division", description="Создать новое подразделение и его Discord-ресурсы")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in BASE_DIRECTIONS])
async def create_division(interaction: discord.Interaction, server: app_commands.Choice[str]):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Создавать подразделения могут только участники с ролью «Владелец».", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    base = server.value
    guild = interaction.guild
    work_category = discord.utils.get(guild.categories, name=WORK_CATEGORY_NAME)
    if work_category is None:
        await interaction.followup.send(f"Не найдена категория **{WORK_CATEGORY_NAME}**. Проверь её название.", ephemeral=True)
        return
    base_role = discord.utils.get(guild.roles, name=base)
    base_channel = discord.utils.get(guild.text_channels, name=BASE_WORK_CHANNELS[base])
    if base_role is None or base_channel is None:
        await interaction.followup.send(f"Не найдены базовая роль **{base}** или канал **{BASE_WORK_CHANNELS[base]}**.", ephemeral=True)
        return

    with database() as db:
        rows = db.execute("SELECT name,division_number,role_name,channel_name FROM divisions WHERE server_name=? ORDER BY division_number", (base,)).fetchall()
        pending = None
        for row in rows:
            existing_role = discord.utils.get(guild.roles, name=row["role_name"])
            existing_channel = discord.utils.get(guild.text_channels, name=row["channel_name"])
            if existing_role is None or existing_channel is None or existing_channel.category_id != work_category.id:
                pending = row
                break
        if pending:
            name = pending["name"]
            number = int(pending["division_number"])
            channel_name = pending["channel_name"]
            role_name = pending["role_name"]
        else:
            used = {int(r["division_number"]) for r in rows}
            number = max([1, *used]) + 1
            name = f"{base} №{number}"
            channel_name = f"{BASE_WORK_CHANNELS[base]}・{number}"
            role_name = name
            db.execute("INSERT INTO divisions(name,server_name,role_name,channel_name,division_number,created_at,created_by) VALUES(?,?,?,?,?,?,?)",
                       (name, base, role_name, channel_name, number, now_iso(), str(interaction.user.id)))

    if name not in DIRECTIONS:
        DIRECTIONS.append(name)
    SERVER_CODES[name] = SERVER_CODES[base]
    WORK_CHANNELS[name] = channel_name
    role = discord.utils.get(guild.roles, name=role_name)
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    try:
        if role is None:
            role = await guild.create_role(
                name=role_name, permissions=base_role.permissions, colour=base_role.colour,
                hoist=base_role.hoist, mentionable=base_role.mentionable,
                reason=f"Karma: создание подразделения {name}"
            )
            try:
                await role.edit(position=base_role.position, reason="Karma: сортировка ролей подразделений")
            except discord.HTTPException:
                pass

        # Category permissions + base channel overrides; replace the base division role.
        overwrites = dict(work_category.overwrites)
        overwrites.update(base_channel.overwrites)
        base_role_overwrite = overwrites.pop(base_role, None)
        overwrites[role] = base_role_overwrite or discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
        )
        overwrites[guild.default_role] = overwrites.get(guild.default_role, discord.PermissionOverwrite(view_channel=False))
        bot_member = guild.get_member(bot.user.id) if bot.user else None
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_messages=True, manage_channels=True, attach_files=True, embed_links=True
            )
        if channel is None:
            channel = await guild.create_text_channel(
                channel_name, category=work_category, overwrites=overwrites,
                topic=f"KARMA NOIRE • {name} • рабочие отчёты",
                reason=f"Karma: создание подразделения {name}"
            )
        else:
            await channel.edit(category=work_category, overwrites=overwrites,
                               reason=f"Karma: размещение подразделения {name} в рабочей категории")
        await ensure_report_panel(channel, name)
        await reorder_work_channels(work_category)
    except discord.Forbidden:
        await interaction.followup.send(
            "Не хватило прав. Karma нужны Manage Roles и Manage Channels, а её роль должна быть выше роли основного подразделения. Запись сохранена — повторный запуск продолжит создание.",
            ephemeral=True
        )
        return
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"Discord не завершил создание подразделения: {error}. Запись сохранена — повторный запуск продолжит с недостающего шага.",
            ephemeral=True
        )
        return
    await interaction.followup.send(
        f"**KARMA NOIRE • ПОДРАЗДЕЛЕНИЕ СОЗДАНО**\n\n"
        f"Игровой сервер: **{SERVER_CODES[base]} • {base}**\n"
        f"Подразделение: **{name}**\nРоль: {role.mention}\nКанал: {channel.mention}\n\n"
        "Канал размещён в категории `03・РАБОТА` и отсортирован автоматически.",
        ephemeral=True
    )


@bot.tree.command(name="set_rate", description="Установить индивидуальную стоимость балла сотрудника")
@app_commands.guild_only()
async def set_rate(interaction: discord.Interaction, member: discord.Member,
                   amount: app_commands.Range[int, 1, 1000000000000]):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Команда доступна только владельцам.", ephemeral=True); return
    with database() as db:
        employee = get_employee_by_discord_id(db, member.id)
        if not employee:
            await interaction.response.send_message("Активный сотрудник не найден.", ephemeral=True); return
        db.execute("UPDATE employees SET point_rate=? WHERE id=?", (amount, employee["id"]))
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('RATE_CHANGED',?,?)",
                   (f"employee={member.id}; rate={amount}; actor={interaction.user.id}", now_iso()))
    await interaction.response.send_message(f"Ставка {member.mention} установлена: **{money(amount)} за балл**.", ephemeral=True)



@bot.tree.command(name="help", description="Показать справочник команд Karma Noire")
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction):
    owner = is_owner(interaction.user)
    manager = is_manager(interaction.user)
    embed = make_embed("KARMA NOIRE | СПРАВОЧНИК", "Команды и их назначение")
    if manager:
        embed.add_field(name="СОТРУДНИКИ", value="`/invite` — принять сотрудника.\n`/uninvite` — уволить и сохранить историю.\n`/employees` — список сотрудников.\n`/stats` — своя статистика.\n`/employee_stats` — статистика сотрудника.\n`/withdraw_report` — отозвать ожидающий отчёт.\n`/set_rate` — индивидуальная ставка.\n`/set_employee_owner` — закрепить сотрудника за владельцем.", inline=False)
        embed.add_field(name="ОТЧЁТЫ", value="`/setup_reports` — установить кнопки отчётов.\n`/stats` — личная статистика.\n`/employee_stats` — статистика сотрудника.\n`/logs` — журнал действий.", inline=False)
        embed.add_field(name="ФИНАНСЫ", value="`/payroll` — недельная ведомость.\n`/paid` — отметить фактическую выплату.\n`/servers` — обзор игровых серверов.", inline=False)
        embed.add_field(name="БИЗНЕСЫ", value="`/add_biz` — добавить бизнес и владельца.\n`/remove_biz` — убрать из реестра.\n`/assign_biz` — сменить владельца.\n`/unassign_biz` — снять владельца.\n`/add_org` — добавить организацию.\n`/create_division` — создать подразделение.\n`/bizinfo` — общий реестр активов.\n`/my_biz` — свои бизнесы.", inline=False)
        embed.add_field(name="ТИКЕТЫ", value="`/setup_tickets` — опубликовать панель заявок. Принять, отклонить и закрыть тикет можно кнопками внутри тикета.", inline=False)
    else:
        embed.add_field(name="СОТРУДНИК", value="`/help` — справка по доступным командам.\nВ рабочем канале нажми «Отправить отчёт» и приложи скриншот.", inline=False)
    if owner:
        embed.add_field(name="КОМАНДЫ ВЛАДЕЛЬЦА", value="`/add_points` — начислить баллы вручную.\n`/remove_points` — снять баллы вручную.", inline=False)
    embed.add_field(name="ПРОВЕРКА", value="`/ping` — проверить доступность бота.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# AUDIT LOG VIEW
# ============================================================

@bot.tree.command(
    name="logs",
    description="Показать последние действия Karma"
)
@app_commands.guild_only()
async def logs(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        await interaction.response.send_message(
            "У тебя нет прав для этой команды.",
            ephemeral=True
        )
        return

    with database() as db:
        rows = db.execute(
            """
            SELECT action, details, created_at
            FROM audit_log
            ORDER BY id DESC
            LIMIT 15
            """
        ).fetchall()

    embed = make_embed("KARMA NOIRE | ЖУРНАЛ ДЕЙСТВИЙ")

    if not rows:
        embed.description = "Записей пока нет."
    else:
        embed.description = "\n\n".join(
            f"**{row['action']}**\n"
            f"{row['details'] or '—'}\n"
            f"`{row['created_at'][:19]}`"
            for row in rows
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# START
# ============================================================

bot.run(TOKEN)
