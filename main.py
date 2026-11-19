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

DIRECTIONS = ["Mesa №2","Mesa", "Surprise", "Prescott", "Faraway"]
SERVER_CODES = {"Mesa": "07", "Surprise": "10", "Prescott": "11", "Faraway": "26"}
MAX_EMPLOYEE_BUSINESSES = 100

REVIEW_CHANNEL_NAME = "проверка-отчётов"
REJECTION_CHANNEL_NAME = "журнал-действий"
FINANCE_CHANNEL_NAME = "финансы"
STATS_CHANNEL_NAME = "статистика"
TICKET_PANEL_CHANNEL_NAME = "создать-заявку"
TICKET_CATEGORY_NAME = "05・ТИКЕТЫ"
TICKET_LOG_CHANNEL_NAME = "заявки-на-вступление"
ADMIN_ROLE_NAMES = ("Владелец", "Администратор")
OWNER_ROLE_NAME = "Владелец"
RECRUIT_ROLE_NAME = "Новобранец"

WORK_CHANNELS = {
    "Mesa": "mesa・работа",
    "Mesa №2": "mesa・работа・2",
    "Surprise": "surprise・работа",
    "Prescott": "prescott・работа",
    "Faraway": "faraway・работа",
}

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
        server_codes = {"Mesa": "07", "Mesa №2": "07", "Surprise": "10", "Prescott": "11", "Faraway": "26"}
        for name, code in server_codes.items():
            db.execute("INSERT OR IGNORE INTO servers(name, server_code) VALUES (?,?)", (name, code))
            db.execute("UPDATE servers SET server_code=COALESCE(server_code, ?) WHERE name=?", (code, name))

        # Legacy employees receive a default rate. Their owner can be assigned later.
        db.execute("UPDATE employees SET point_rate=? WHERE point_rate IS NULL OR point_rate<=0", (POINT_VALUE,))
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


@bot.event
async def on_member_join(member: discord.Member):
    panel = discord.utils.get(member.guild.text_channels, name=TICKET_PANEL_CHANNEL_NAME)
    if panel:
        try:
            await member.send(
                f"Добро пожаловать на **Karma Noire / BIZINFO**. Для вступления создай заявку здесь: https://discord.com/channels/{member.guild.id}/{panel.id}. "
                "После заполнения анкеты откроется приватный тикет с администрацией."
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
            SELECT id, discord_id, direction
            FROM employees
            WHERE status = 'active'
            """
        ).fetchall()

        for employee in employees:
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


@bot.tree.command(name="invite", description="Принять сотрудника на игровой сервер")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in DIRECTIONS])
async def invite(interaction: discord.Interaction, member: discord.Member, server: app_commands.Choice[str]):
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
        """, (str(member.id), server.value, now_iso(), str(interaction.user.id), POINT_VALUE, str(interaction.guild_id)))
        employee_id = cur.lastrowid
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('INVITE',?,?)",
                   (f"employee={member.id}; server={server.value}; owner={interaction.user.id}", now_iso()))
    role = discord.utils.get(interaction.guild.roles, name=server.value)
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
        f"{member.mention} принят на **{SERVER_CODES[server.value]} • {server.value}**.\n"
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
        biz = db.execute("SELECT business_id FROM businesses WHERE employee_id=? AND status='active' ORDER BY business_id", (employee["id"],)).fetchall()
        daily = db.execute("SELECT work_date,SUM(points) points FROM daily_entries WHERE employee_id=? GROUP BY work_date ORDER BY work_date DESC LIMIT 7", (employee["id"],)).fetchall()
    embed = make_embed("KARMA NOIRE | СТАТИСТИКА", f"Сотрудник: {target.mention}")
    embed.add_field(name="SERVER", value=f"{employee['direction']} • №{SERVER_CODES.get(employee['direction'],'—')}", inline=True)
    embed.add_field(name="СТАТУС", value=employee["status"], inline=True)
    embed.add_field(name="СТАВКА", value=f"{money(rate)} / балл", inline=True)
    embed.add_field(name="ЗАРАБОТАНО", value=f"{earned} баллов\n{money(earned_amount)}", inline=True)
    embed.add_field(name="ОПЛАЧЕНО", value=f"{paid} баллов", inline=True)
    embed.add_field(name="К ВЫПЛАТЕ", value=f"{unpaid} баллов\n{money(unpaid_amount)}", inline=True)
    embed.add_field(name="ОТЧЁТЫ", value=f"Принято: {approved}\nОтклонено: {rejected}\nОжидает: {pending}", inline=True)
    embed.add_field(name="ЗАКРЕПЛЁННЫЕ БИЗНЕСЫ", value=", ".join(f"`{r['business_id']}`" for r in biz) or "Пока нет", inline=False)
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
        result = []
        for name in DIRECTIONS:
            staff = db.execute("SELECT discord_id FROM employees WHERE direction=? AND status='active' ORDER BY id", (name,)).fetchall()
            biz_count = db.execute("SELECT COUNT(*) FROM businesses WHERE server_name=? AND status='active'", (name,)).fetchone()[0]
            result.append((name, staff, biz_count))
    embed = make_embed("KARMA NOIRE | SERVERS")
    for name, staff, biz_count in result:
        staff_text = ", ".join(f"<@{r['discord_id']}>" for r in staff) or "Нет сотрудников"
        embed.add_field(name=f"{SERVER_CODES[name]} • {name}",
                        value=f"Сотрудников: **{len(staff)}**\nБизнесов: **{biz_count}**\n{staff_text}"[:1024], inline=False)
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

class TicketApplicationModal(discord.ui.Modal, title="Заявка в Karma Noire"):
    nickname = discord.ui.TextInput(label="Игровой никнейм", max_length=80)
    game_server = discord.ui.TextInput(label="Игровой сервер", max_length=80)
    age = discord.ui.TextInput(label="Возраст", max_length=3)
    real_name = discord.ui.TextInput(label="Настоящее имя", max_length=80)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Заявка создаётся только на сервере.", ephemeral=True)
            return
        with database() as db:
            existing = db.execute("SELECT id FROM tickets WHERE guild_id=? AND user_id=? AND status='pending'",
                                  (str(interaction.guild.id), str(interaction.user.id))).fetchone()
        if existing:
            await interaction.response.send_message("У тебя уже есть открытый тикет. Продолжай общение в нём.", ephemeral=True)
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
                name=f"ticket-{interaction.user.id % 100000:05d}", category=category,
                overwrites=overwrites, topic=f"Заявка пользователя {interaction.user.id}"
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Karma не может создать тикет. Выдай боту право **Manage Channels** и проверь права категории тикетов.",
                ephemeral=True
            )
            return
        except discord.HTTPException as error:
            print(f"Ticket channel creation error: {error}")
            await interaction.followup.send("Не удалось создать тикет. Попробуй позже или сообщи администрации.", ephemeral=True)
            return
        with database() as db:
            cur = db.execute("INSERT INTO tickets (guild_id,user_id,channel_id,nickname,game_server,age,real_name,created_at) VALUES (?,?,?,?,?,?,?,?)",
                             (str(interaction.guild.id), str(interaction.user.id), str(channel.id), self.nickname.value.strip(), self.game_server.value.strip(), self.age.value.strip(), self.real_name.value.strip(), now_iso()))
            ticket_id = cur.lastrowid
            db.execute("INSERT INTO audit_log (action,details,created_at) VALUES ('TICKET_CREATED',?,?)",
                       (f"ticket={ticket_id}; user={interaction.user.id}", now_iso()))
        embed = make_embed("KARMA NOIRE | НОВАЯ ЗАЯВКА", "Администрация рассмотрит анкету и ответит здесь.")
        embed.add_field(name="ИГРОВОЙ НИКНЕЙМ", value=self.nickname.value, inline=True)
        embed.add_field(name="SERVER", value=self.game_server.value, inline=True)
        embed.add_field(name="ВОЗРАСТ", value=self.age.value, inline=True)
        embed.add_field(name="НАСТОЯЩЕЕ ИМЯ", value=self.real_name.value, inline=True)
        await channel.send(content=interaction.user.mention, embed=embed, view=TicketControlView())
        log = discord.utils.get(interaction.guild.text_channels, name=TICKET_LOG_CHANNEL_NAME)
        if log:
            await log.send(embed=make_embed("KARMA NOIRE | НОВЫЙ ТИКЕТ", f"{interaction.user.mention} создал {channel.mention}. Заявка №{ticket_id}."))
        await interaction.followup.send(f"Тикет создан: {channel.mention}. Только ты и администрация имеют к нему доступ.", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать заявку", style=discord.ButtonStyle.secondary, custom_id="karma:ticket:create")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketApplicationModal())


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def get_ticket(self, interaction):
        with database() as db:
            return db.execute("SELECT * FROM tickets WHERE channel_id=? AND status='pending'", (str(interaction.channel_id),)).fetchone()

    @discord.ui.button(label="Принять • Новобранец", style=discord.ButtonStyle.secondary, custom_id="karma:ticket:accept")
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
            await interaction.response.send_message("Не найдена роль «Новобранец» или пользователь.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Заявка принята через Karma Noire")
        except discord.Forbidden:
            await interaction.response.send_message("Бот не может выдать роль. Проверь порядок ролей и права.", ephemeral=True)
            return
        with database() as db:
            db.execute("UPDATE tickets SET status='accepted', reviewer_id=? WHERE id=? AND status='pending'", (str(interaction.user.id), ticket["id"]))
            db.execute("INSERT INTO audit_log (action,details,created_at) VALUES ('TICKET_ACCEPTED',?,?)", (f"ticket={ticket['id']}; user={ticket['user_id']}; reviewer={interaction.user.id}", now_iso()))
        for child in self.children:
            child.disabled = child.custom_id != "karma:ticket:close"
        await interaction.response.edit_message(embed=make_embed("KARMA NOIRE | ЗАЯВКА ПРИНЯТА", f"{member.mention} получил роль **{RECRUIT_ROLE_NAME}**."), view=self)
        try:
            await member.send("Твоя заявка в Karma Noire принята. Тебе выдана роль «Новобранец».")
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.secondary, custom_id="karma:ticket:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction.user):
            await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
            return
        ticket = await self.get_ticket(interaction)
        if not ticket:
            await interaction.response.send_message("Открытая заявка не найдена.", ephemeral=True)
            return
        with database() as db:
            db.execute("UPDATE tickets SET status='rejected', reviewer_id=? WHERE id=?", (str(interaction.user.id), ticket["id"]))
            db.execute("INSERT INTO audit_log (action,details,created_at) VALUES ('TICKET_REJECTED',?,?)", (f"ticket={ticket['id']}; user={ticket['user_id']}; reviewer={interaction.user.id}", now_iso()))
        for child in self.children:
            child.disabled = child.custom_id != "karma:ticket:close"
        await interaction.response.edit_message(embed=make_embed("KARMA NOIRE | ЗАЯВКА ОТКЛОНЕНА", "Администрация отклонила заявку."), view=self)
        member = interaction.guild.get_member(int(ticket["user_id"]))
        if member:
            try:
                await member.send("Твоя заявка в Karma Noire отклонена. Если считаешь, что произошла ошибка, свяжись с администрацией.")
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Закрыть тикет", style=discord.ButtonStyle.secondary, custom_id="karma:ticket:close")
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
    embed = make_embed("KARMA NOIRE | BIZINFO", "ДОБРО ПОЖАЛОВАТЬ. ДЛЯ ВСТУПЛЕНИЯ СОЗДАЙ ТИКЕТ.")
    embed.add_field(name="АНКЕТА", value="Нажми кнопку ниже и заполни игровой никнейм, Server, возраст и настоящее имя. После отправки откроется приватный канал для общения с администрацией.", inline=False)
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

@bot.tree.command(name="add_biz", description="Добавить бизнес в общий реестр и закрепить за сотрудником")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in DIRECTIONS])
async def add_biz(interaction: discord.Interaction, server: app_commands.Choice[str],
                  owner: discord.Member, business_id: app_commands.Range[int, 1, 999999999]):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Команда доступна только владельцам.", ephemeral=True); return
    with database() as db:
        employee = get_employee_by_discord_id(db, owner.id)
        if not employee:
            await interaction.response.send_message("Указанный участник не числится активным сотрудником.", ephemeral=True); return
        if employee["direction"] != server.value:
            await interaction.response.send_message(f"Сотрудник закреплён за {employee['direction']}, а бизнес относится к {server.value}.", ephemeral=True); return
        count = db.execute("SELECT COUNT(*) FROM businesses WHERE employee_id=? AND status='active'", (employee["id"],)).fetchone()[0]
        if count >= MAX_EMPLOYEE_BUSINESSES:
            await interaction.response.send_message(f"У сотрудника уже максимально допустимые {MAX_EMPLOYEE_BUSINESSES} бизнесов.", ephemeral=True); return
        try:
            db.execute("INSERT INTO businesses(business_id,server_name,employee_id,added_by,created_at,status) VALUES(?,?,?,?,?,'active')",
                       (str(business_id), server.value, employee["id"], str(interaction.user.id), now_iso()))
        except sqlite3.IntegrityError:
            await interaction.response.send_message("Бизнес с таким ID уже есть в общем реестре.", ephemeral=True); return
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('BIZ_ADDED',?,?)",
                   (f"biz={business_id}; server={server.value}; employee={owner.id}; actor={interaction.user.id}", now_iso()))
    await interaction.response.send_message(
        f"Бизнес **{business_id}** добавлен в реестр и закреплён за {owner.mention}.\n"
        f"Сервер: **{SERVER_CODES[server.value]} • {server.value}**.", ephemeral=True)


@bot.tree.command(name="remove_biz", description="Убрать бизнес из общего реестра")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in DIRECTIONS])
async def remove_biz(interaction: discord.Interaction, server: app_commands.Choice[str],
                     business_id: app_commands.Range[int, 1, 999999999]):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Команда доступна только владельцам.", ephemeral=True); return
    with database() as db:
        row = db.execute("SELECT * FROM businesses WHERE business_id=? AND server_name=? AND status='active'", (str(business_id), server.value)).fetchone()
        if not row:
            await interaction.response.send_message("Активный бизнес с таким ID не найден.", ephemeral=True); return
        db.execute("UPDATE businesses SET status='removed',employee_id=NULL WHERE business_id=? AND server_name=?", (str(business_id), server.value))
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('BIZ_REMOVED',?,?)",
                   (f"biz={business_id}; server={server.value}; actor={interaction.user.id}", now_iso()))
    await interaction.response.send_message(f"Бизнес **{business_id}** удалён из активного реестра. История сохранена.", ephemeral=True)


@bot.tree.command(name="assign_biz", description="Переназначить бизнес другому сотруднику")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in DIRECTIONS])
async def assign_biz(interaction: discord.Interaction, server: app_commands.Choice[str],
                     business_id: app_commands.Range[int, 1, 999999999], employee_member: discord.Member):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Команда доступна только владельцам.", ephemeral=True); return
    with database() as db:
        biz = db.execute("SELECT * FROM businesses WHERE business_id=? AND server_name=? AND status='active'", (str(business_id), server.value)).fetchone()
        employee = get_employee_by_discord_id(db, employee_member.id)
        if not biz or not employee:
            await interaction.response.send_message("Бизнес или активный сотрудник не найден.", ephemeral=True); return
        if employee["direction"] != biz["server_name"]:
            await interaction.response.send_message("Нельзя закрепить бизнес за сотрудником другого игрового сервера.", ephemeral=True); return
        count = db.execute("SELECT COUNT(*) FROM businesses WHERE employee_id=? AND status='active'", (employee["id"],)).fetchone()[0]
        already = biz["employee_id"] == employee["id"]
        if not already and count >= MAX_EMPLOYEE_BUSINESSES:
            await interaction.response.send_message(f"У сотрудника уже {MAX_EMPLOYEE_BUSINESSES} бизнесов.", ephemeral=True); return
        db.execute("UPDATE businesses SET employee_id=?,added_by=? WHERE business_id=?",
                   (employee["id"], str(interaction.user.id), str(business_id)))
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('BIZ_ASSIGNED',?,?)",
                   (f"biz={business_id}; employee={employee_member.id}; actor={interaction.user.id}", now_iso()))
    await interaction.response.send_message(f"Бизнес **{business_id}** закреплён за {employee_member.mention}.", ephemeral=True)


@bot.tree.command(name="unassign_biz", description="Освободить бизнес от закреплённого сотрудника")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in DIRECTIONS])
async def unassign_biz(interaction: discord.Interaction, server: app_commands.Choice[str],
                       business_id: app_commands.Range[int, 1, 999999999]):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Команда доступна только владельцам.", ephemeral=True); return
    with database() as db:
        cur = db.execute("UPDATE businesses SET employee_id=NULL,added_by=? WHERE business_id=? AND server_name=? AND status='active'",
                         (str(interaction.user.id), str(business_id), server.value))
        if cur.rowcount == 0:
            await interaction.response.send_message("Активный бизнес не найден.", ephemeral=True); return
        db.execute("INSERT INTO audit_log(action,details,created_at) VALUES('BIZ_UNASSIGNED',?,?)",
                   (f"biz={business_id}; actor={interaction.user.id}", now_iso()))
    await interaction.response.send_message(f"Бизнес **{business_id}** освобождён. Он остаётся в общем реестре.", ephemeral=True)


@bot.tree.command(name="bizinfo", description="Показать общий реестр бизнесов")
@app_commands.guild_only()
@app_commands.choices(server=[app_commands.Choice(name=f"{SERVER_CODES[n]} • {n}", value=n) for n in DIRECTIONS])
async def bizinfo(interaction: discord.Interaction, server: app_commands.Choice[str] = None,
                  owner: discord.Member = None):
    if not is_manager(interaction.user):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True); return
    query = """
        SELECT b.business_id,b.server_name,b.employee_id,e.discord_id
        FROM businesses b LEFT JOIN employees e ON e.id=b.employee_id
        WHERE b.status='active'
    """
    params = []
    if server:
        query += " AND b.server_name=?"; params.append(server.value)
    if owner:
        query += " AND e.discord_id=?"; params.append(str(owner.id))
    query += " ORDER BY b.server_name,CAST(b.business_id AS INTEGER)"
    with database() as db:
        rows = db.execute(query, params).fetchall()
    embed = make_embed("KARMA NOIRE | BIZINFO", "ОБЩИЙ РЕЕСТР АКТИВОВ")
    if not rows:
        embed.description = "По заданным параметрам активных бизнесов нет."
    else:
        grouped = {}
        for row in rows:
            grouped.setdefault(row["server_name"], []).append(
                f"`{row['business_id']}` — <@{row['discord_id']}>" if row["discord_id"] else f"`{row['business_id']}` — не закреплён"
            )
        for name, values in grouped.items():
            embed.add_field(name=f"{SERVER_CODES.get(name,'—')} • {name} ({len(values)})",
                            value="\n".join(values)[:1024], inline=False)
        embed.set_footer(text=f"Всего активных бизнесов: {len(rows)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="my_biz", description="Посмотреть закреплённые за собой бизнесы")
@app_commands.guild_only()
async def my_biz(interaction: discord.Interaction):
    with database() as db:
        employee = get_employee_by_discord_id(db, interaction.user.id)
        if not employee:
            await interaction.response.send_message("Ты не числишься активным сотрудником.", ephemeral=True); return
        rows = db.execute("SELECT business_id FROM businesses WHERE employee_id=? AND status='active' ORDER BY CAST(business_id AS INTEGER)",
                          (employee["id"],)).fetchall()
    await interaction.response.send_message(
        f"**Твои бизнесы — {SERVER_CODES.get(employee['direction'],'—')} • {employee['direction']}**\n" +
        (", ".join(f"`{r['business_id']}`" for r in rows) if rows else "Пока нет закреплённых бизнесов.") +
        f"\n\nКоличество: **{len(rows)}/{MAX_EMPLOYEE_BUSINESSES}**", ephemeral=True)


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
        embed.add_field(name="БИЗНЕСЫ", value="`/add_biz` — добавить бизнес и закрепить.\n`/remove_biz` — убрать из реестра.\n`/assign_biz` — переназначить.\n`/unassign_biz` — освободить.\n`/bizinfo` — общий реестр.\n`/my_biz` — свои бизнесы.", inline=False)
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
