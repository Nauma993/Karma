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
                FOREIGN KEY (employee_id) REFERENCES employees(id)
            )
        """)

        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            one_pending_submission_per_employee
            ON submissions(employee_id)
            WHERE status = 'pending'
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
                FOREIGN KEY (employee_id) REFERENCES employees(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS report_sessions (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
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

        for name in DIRECTIONS:
            db.execute(
                "INSERT OR IGNORE INTO servers (name) VALUES (?)",
                (name,)
            )

    print("Karma database initialized!")


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

    async def setup_hook(self):
        init_database()

        # Persistent buttons keep working after a bot restart.
        self.add_view(SubmitView())
        self.add_view(ReviewView())
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())

        await self.tree.sync()
        print("Karma commands synchronized!")

        if not daily_reminder.is_running():
            daily_reminder.start()
        if not daily_cleanup.is_running():
            daily_cleanup.start()

    async def on_ready(self):
        print(f"Karma is online as {self.user}")


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

class ApprovalModal(discord.ui.Modal, title="Подтверждение отчёта"):
    points = discord.ui.TextInput(
        label="Количество начисляемых баллов",
        placeholder="Например: 5",
        required=True,
        max_length=4
    )

    def __init__(self, submission_id, review_message_id, business_count):
        super().__init__()
        self.submission_id = submission_id
        self.review_message_id = review_message_id
        self.business_count = business_count

    async def on_submit(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message(
                "Недостаточно прав.",
                ephemeral=True
            )
            return

        try:
            points = int(self.points.value)
        except ValueError:
            await interaction.response.send_message(
                "Введи целое число.",
                ephemeral=True
            )
            return

        if points < 1 or points > self.business_count:
            await interaction.response.send_message(
                f"Можно начислить от 1 до {self.business_count} баллов.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        with database() as db:
            submission = db.execute(
                """
                SELECT employee_id, work_date
                FROM submissions
                WHERE id = ? AND status = 'pending'
                """,
                (self.submission_id,)
            ).fetchone()

            if not submission:
                await interaction.followup.send(
                    "Заявка уже обработана или не найдена.",
                    ephemeral=True
                )
                return

            already_approved = db.execute(
                """
                SELECT id FROM submissions
                WHERE employee_id = ?
                  AND work_date = ?
                  AND status = 'approved'
                """,
                (submission["employee_id"], submission["work_date"])
            ).fetchone()

            if already_approved:
                await interaction.followup.send(
                    "За этот рабочий день уже принят другой отчёт.",
                    ephemeral=True
                )
                return

            cursor = db.execute(
                """
                UPDATE submissions
                SET status = 'approved',
                    points = ?,
                    reviewer_id = ?,
                    reviewed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    points,
                    str(interaction.user.id),
                    now_iso(),
                    self.submission_id
                )
            )

            if cursor.rowcount == 0:
                await interaction.followup.send(
                    "Заявка уже обработана.",
                    ephemeral=True
                )
                return

            db.execute(
                """
                INSERT INTO daily_entries
                (employee_id, work_date, points)
                VALUES (?, ?, ?)
                """,
                (
                    submission["employee_id"],
                    submission["work_date"],
                    points
                )
            )

            db.execute(
                """
                INSERT INTO audit_log
                (action, details, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    "REPORT_APPROVED",
                    f"Submission {self.submission_id}; points={points}",
                    now_iso()
                )
            )

        if interaction.guild:
            await update_review_message(
                interaction.guild,
                self.review_message_id,
                f"ПРИНЯТ • {points} баллов • проверил {interaction.user.mention}"
            )

        await interaction.followup.send(
            f"Отчёт принят. Начислено **{points} {points_word(points)}** "
            f"({money(points * POINT_VALUE)}).",
            ephemeral=True
        )


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

    @discord.ui.button(
        label="Принять",
        style=discord.ButtonStyle.secondary,
        custom_id="karma:review:approve"
    )
    async def approve(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not is_manager(interaction.user):
            await interaction.response.send_message(
                "Недостаточно прав.",
                ephemeral=True
            )
            return

        with database() as db:
            submission = db.execute(
                """
                SELECT id, employee_id, review_message_id
                FROM submissions
                WHERE review_message_id = ? AND status = 'pending'
                """,
                (str(interaction.message.id),)
            ).fetchone()

            if not submission:
                await interaction.response.send_message(
                    "Заявка не найдена или уже обработана.",
                    ephemeral=True
                )
                return

            server = db.execute(
                """
                SELECT s.business_count
                FROM employees e
                JOIN servers s ON s.name = e.direction
                WHERE e.id = ?
                """,
                (submission["employee_id"],)
            ).fetchone()

        business_count = server["business_count"] if server else 0

        if business_count < 1:
            await interaction.response.send_message(
                "Для этого Server ещё не настроено количество бизнесов. "
                "Сначала используй команду /businesses.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            ApprovalModal(
                submission["id"],
                submission["review_message_id"],
                business_count
            )
        )

    @discord.ui.button(
        label="Отклонить",
        style=discord.ButtonStyle.secondary,
        custom_id="karma:review:reject"
    )
    async def reject(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if not is_manager(interaction.user):
            await interaction.response.send_message(
                "Недостаточно прав.",
                ephemeral=True
            )
            return

        with database() as db:
            submission = db.execute(
                """
                SELECT id, review_message_id
                FROM submissions
                WHERE review_message_id = ? AND status = 'pending'
                """,
                (str(interaction.message.id),)
            ).fetchone()

        if not submission:
            await interaction.response.send_message(
                "Заявка не найдена или уже обработана.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            RejectionModal(
                submission["id"],
                submission["review_message_id"]
            )
        )


# ============================================================
# RECEIVING EMPLOYEE SCREENSHOTS
# ============================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    channel_direction = next(
        (server for server, channel_name in WORK_CHANNELS.items() if channel_name == message.channel.name),
        None
    )
    if channel_direction is None or is_manager(message.author):
        return

    # Work channels are submission-only: no ordinary chat is retained.
    key = (str(message.guild.id), str(message.author.id))
    with database() as db:
        session = db.execute(
            "SELECT channel_id FROM report_sessions WHERE guild_id=? AND user_id=?",
            key
        ).fetchone()

    is_expected = session and str(message.channel.id) == session["channel_id"]
    attachment = next((a for a in message.attachments if (a.content_type or "").startswith("image/")), None)

    if not is_expected or attachment is None:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if is_expected and attachment is None:
            try:
                await message.author.send("В рабочем канале принимаются только скриншоты. Нажми кнопку «Отправить отчёт» и приложи изображение.")
            except discord.HTTPException:
                pass
        return

    work_date = get_work_date()
    review_channel = discord.utils.get(message.guild.text_channels, name=REVIEW_CHANNEL_NAME)
    if review_channel is None:
        try:
            await message.delete()
            await message.author.send("Не найден канал проверки отчётов. Сообщи администрации.")
        except discord.HTTPException:
            pass
        return

    with database() as db:
        employee = db.execute(
            "SELECT id, direction FROM employees WHERE discord_id=? AND status='active'",
            (str(message.author.id),)
        ).fetchone()
        if not employee or employee["direction"] != channel_direction:
            db.execute("DELETE FROM report_sessions WHERE guild_id=? AND user_id=?", key)
            try:
                await message.delete()
                await message.author.send("У тебя нет активного назначения на этот Server.")
            except discord.HTTPException:
                pass
            return

        server = db.execute("SELECT business_count FROM servers WHERE name=?", (channel_direction,)).fetchone()
        approved = db.execute(
            "SELECT id FROM submissions WHERE employee_id=? AND work_date=? AND status='approved'",
            (employee["id"], work_date)
        ).fetchone()
        pending = db.execute(
            "SELECT id FROM submissions WHERE employee_id=? AND status='pending'",
            (employee["id"],)
        ).fetchone()
        if not server or server["business_count"] < 1 or approved or pending:
            db.execute("DELETE FROM report_sessions WHERE guild_id=? AND user_id=?", key)
            try:
                await message.delete()
                await message.author.send("Отчёт не принят: проверь количество бизнесов, текущие заявки и статус отчёта за сегодня.")
            except discord.HTTPException:
                pass
            return

        cursor = db.execute(
            "INSERT INTO submissions (employee_id, work_date, attachment_url, submitted_at) VALUES (?, ?, ?, ?)",
            (employee["id"], work_date, attachment.url, now_iso())
        )
        submission_id = cursor.lastrowid

    embed = make_embed("KARMA NOIRE | BIZINFO", "НОВАЯ ЗАЯВКА НА ПРОВЕРКУ")
    embed.add_field(name="СОТРУДНИК", value=message.author.mention, inline=True)
    embed.add_field(name="SERVER", value=channel_direction, inline=True)
    embed.add_field(name="РАБОЧАЯ ДАТА", value=work_date, inline=True)
    embed.add_field(name="СТАТУС", value="ОЖИДАЕТ ПРОВЕРКИ", inline=False)
    embed.set_image(url=f"attachment://{attachment.filename}")
    embed.set_footer(text=f"KARMA NOIRE • REPORT #{submission_id}")

    try:
        review_message = await review_channel.send(embed=embed, file=await attachment.to_file(), view=ReviewView())
        with database() as db:
            db.execute("UPDATE submissions SET review_message_id=? WHERE id=?", (str(review_message.id), submission_id))
            db.execute("DELETE FROM report_sessions WHERE guild_id=? AND user_id=?", key)
        await message.delete()
        try:
            await message.author.send(f"Твой отчёт по Server **{channel_direction}** передан администрации на проверку.")
        except discord.HTTPException:
            pass
    except Exception as error:
        print(f"Report sending error: {error}")
        with database() as db:
            db.execute("DELETE FROM submissions WHERE id=? AND status='pending'", (submission_id,))
            db.execute("DELETE FROM report_sessions WHERE guild_id=? AND user_id=?", key)
        try:
            await message.delete()
            await message.author.send("Не удалось передать отчёт. Попробуй снова через кнопку.")
        except discord.HTTPException:
            pass


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
                    SELECT s.name, s.business_count, e.discord_id
                    FROM servers s LEFT JOIN employees e
                    ON e.direction=s.name AND e.status='active'
                    ORDER BY s.name
                """).fetchall()
            overview = make_embed("KARMA NOIRE | ЕЖЕДНЕВНАЯ СВОДКА", f"Рабочая дата: **{get_work_date()}**")
            for row in server_rows:
                person = f"<@{row['discord_id']}>" if row['discord_id'] else "Свободный слот"
                overview.add_field(name=f"SERVER: {row['name']}", value=f"Бизнесов: **{row['business_count']}**\nСотрудник: {person}", inline=True)
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

@bot.tree.command(
    name="invite",
    description="Принять сотрудника на Server"
)
@app_commands.guild_only()
@app_commands.choices(server=[
    app_commands.Choice(name=name, value=name)
    for name in DIRECTIONS
])
async def invite(
    interaction: discord.Interaction,
    member: discord.Member,
    server: app_commands.Choice[str]
):
    if not is_manager(interaction.user):
        await interaction.response.send_message(
            "У тебя нет прав для этой команды.",
            ephemeral=True
        )
        return

    if member.bot:
        await interaction.response.send_message(
            "Нельзя принять бота на работу.",
            ephemeral=True
        )
        return

    direction_name = server.value

    with database() as db:
        active_employee = get_employee_by_discord_id(db, member.id)
        if active_employee:
            await interaction.response.send_message(
                f"{member.mention} уже работает на Server "
                f"**{active_employee['direction']}**.",
                ephemeral=True
            )
            return

        occupied = db.execute(
            """
            SELECT discord_id FROM employees
            WHERE direction = ? AND status = 'active'
            """,
            (direction_name,)
        ).fetchone()

        if occupied:
            await interaction.response.send_message(
                f"Server **{direction_name}** уже занят.",
                ephemeral=True
            )
            return

    role = discord.utils.get(
        interaction.guild.roles,
        name=direction_name
    )

    if role is None:
        await interaction.response.send_message(
            f"Не найдена роль **{direction_name}**.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        await member.add_roles(
            role,
            reason=f"Принят на {direction_name} через Karma"
        )

        with database() as db:
            cursor = db.execute(
                """
                INSERT INTO employees
                (discord_id, direction, joined_at, status)
                VALUES (?, ?, ?, 'active')
                """,
                (str(member.id), direction_name, now_iso())
            )
            employee_id = cursor.lastrowid

            db.execute(
                """
                INSERT INTO audit_log
                (action, details, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    "INVITE",
                    f"Employee {member.id}; direction={direction_name}",
                    now_iso()
                )
            )

        await interaction.followup.send(
            f"{member.mention} принят на Server **{direction_name}**.\n"
            f"Номер сотрудника: `{employee_id}`",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "Karma не может выдать роль. Проверь права бота "
            "и положение его роли в списке ролей.",
            ephemeral=True
        )

    except Exception as error:
        print(f"Invite error: {error}")
        try:
            await member.remove_roles(role)
        except discord.HTTPException:
            pass

        await interaction.followup.send(
            "Не удалось принять сотрудника. Проверь логи Railway.",
            ephemeral=True
        )


# ============================================================
# UNINVITE EMPLOYEE
# ============================================================

class UninviteView(discord.ui.View):
    def __init__(self, requester_id, member, employee_id, direction):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.member = member
        self.employee_id = employee_id
        self.direction = direction

    async def interaction_check(self, interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Подтвердить это действие может только инициатор команды.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Подтвердить увольнение",
        style=discord.ButtonStyle.secondary
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)

        role = discord.utils.get(
            interaction.guild.roles,
            name=self.direction
        )

        try:
            if role and role in self.member.roles:
                await self.member.remove_roles(
                    role,
                    reason="Увольнение через Karma"
                )

            with database() as db:
                cursor = db.execute(
                    """
                    UPDATE employees
                    SET status = 'archived', left_at = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (now_iso(), self.employee_id)
                )

                if cursor.rowcount == 0:
                    raise RuntimeError("Сотрудник уже не активен.")

                db.execute(
                    """
                    INSERT INTO audit_log
                    (action, details, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        "UNINVITE",
                        f"Employee {self.member.id}; direction={self.direction}",
                        now_iso()
                    )
                )

            with database() as db:
                _, _, unpaid = get_employee_totals(db, self.employee_id)

            for item in self.children:
                item.disabled = True

            await interaction.followup.send(
                f"{self.member.mention} уволен с Server "
                f"**{self.direction}**.\n"
                f"Неоплаченные баллы: **{unpaid} {points_word(unpaid)}**\n"
                f"К выплате: **{money(unpaid * POINT_VALUE)}**\n"
                "История сотрудника сохранена.",
                ephemeral=True
            )

        except Exception as error:
            print(f"Uninvite error: {error}")

            if role:
                try:
                    await self.member.add_roles(
                        role,
                        reason="Восстановление роли после ошибки увольнения"
                    )
                except discord.HTTPException:
                    pass

            await interaction.followup.send(
                "Не удалось завершить увольнение. Проверь логи Railway.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Отмена",
        style=discord.ButtonStyle.secondary
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content="Увольнение отменено.",
            view=self
        )


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
        _, _, unpaid = get_employee_totals(db, employee_id)

    embed = make_embed(
        "KARMA NOIRE | УВОЛЬНЕНИЕ",
        "Проверь данные перед подтверждением."
    )
    embed.add_field(name="СОТРУДНИК", value=member.mention, inline=True)
    embed.add_field(name="SERVER", value=direction, inline=True)
    embed.add_field(name="НЕОПЛАЧЕННЫЕ БАЛЛЫ", value=str(unpaid), inline=True)
    embed.add_field(
        name="К ВЫПЛАТЕ",
        value=money(unpaid * POINT_VALUE),
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


# ============================================================
# BUSINESS COUNT SETTINGS
# ============================================================

@bot.tree.command(
    name="businesses",
    description="Установить количество бизнесов на Server"
)
@app_commands.guild_only()
@app_commands.choices(server=[
    app_commands.Choice(name=name, value=name)
    for name in DIRECTIONS
])
async def businesses(
    interaction: discord.Interaction,
    server: app_commands.Choice[str],
    count: app_commands.Range[int, 0, 1000]
):
    if not is_manager(interaction.user):
        await interaction.response.send_message(
            "У тебя нет прав для этой команды.",
            ephemeral=True
        )
        return

    with database() as db:
        db.execute(
            "UPDATE servers SET business_count = ? WHERE name = ?",
            (count, server.value)
        )
        db.execute(
            """
            INSERT INTO audit_log
            (action, details, created_at)
            VALUES (?, ?, ?)
            """,
            (
                "BUSINESSES_UPDATED",
                f"{server.value}; count={count}",
                now_iso()
            )
        )

    await interaction.response.send_message(
        f"Для Server **{server.value}** установлено "
        f"**{count}** бизнесов.",
        ephemeral=True
    )


# ============================================================
# EMPLOYEE STATS
# ============================================================

@bot.tree.command(
    name="stats",
    description="Посмотреть статистику и баланс сотрудника"
)
@app_commands.guild_only()
async def stats(
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
                "История этого пользователя не найдена.",
                ephemeral=True
            )
            return

        earned, paid, unpaid = get_employee_totals(db, employee["id"])

        daily = db.execute(
            """
            SELECT work_date, SUM(points) AS points
            FROM daily_entries
            WHERE employee_id = ?
            GROUP BY work_date
            ORDER BY work_date DESC
            LIMIT 7
            """,
            (employee["id"],)
        ).fetchall()

        approved = db.execute(
            """
            SELECT COUNT(*) FROM submissions
            WHERE employee_id = ? AND status = 'approved'
            """,
            (employee["id"],)
        ).fetchone()[0]

        rejected = db.execute(
            """
            SELECT COUNT(*) FROM submissions
            WHERE employee_id = ? AND status = 'rejected'
            """,
            (employee["id"],)
        ).fetchone()[0]

    embed = make_embed(
        "KARMA NOIRE | СТАТИСТИКА",
        f"Сотрудник: {member.mention}"
    )
    embed.add_field(name="SERVER", value=employee["direction"], inline=True)
    embed.add_field(name="СТАТУС", value=employee["status"], inline=True)
    embed.add_field(name="ВСЕГО БАЛЛОВ", value=str(earned), inline=True)
    embed.add_field(name="ОПЛАЧЕНО", value=str(paid), inline=True)
    embed.add_field(name="К ВЫПЛАТЕ", value=f"{unpaid} {points_word(unpaid)}\n{money(unpaid * POINT_VALUE)}", inline=True)
    embed.add_field(name="ОТЧЁТЫ", value=f"Принято: {approved}\nОтклонено: {rejected}", inline=True)

    if daily:
        daily_text = "\n".join(
            f"`{row['work_date']}` — **{row['points']} {points_word(row['points'])}**"
            for row in daily
        )
    else:
        daily_text = "Начислений пока нет."

    embed.add_field(
        name="ПОСЛЕДНИЕ РАБОЧИЕ ДНИ",
        value=daily_text,
        inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# MARK EMPLOYEE AS PAID
# ============================================================

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

        amount = unpaid * POINT_VALUE

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
        f"Сумма: **{money(amount)}**",
        ephemeral=True
    )
    finance_channel = discord.utils.get(interaction.guild.text_channels, name=FINANCE_CHANNEL_NAME)
    if finance_channel:
        finance_embed = make_embed("KARMA NOIRE | ВЫПЛАТА ЗАФИКСИРОВАНА", f"Получатель: {member.mention}")
        finance_embed.add_field(name="БАЛЛЫ", value=f"{unpaid} {points_word(unpaid)}", inline=True)
        finance_embed.add_field(name="СУММА", value=money(amount), inline=True)
        finance_embed.add_field(name="SERVER", value=employee["direction"], inline=True)
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
                SELECT employee_id, work_date, points FROM daily_entries
                UNION ALL
                SELECT employee_id, substr(created_at, 1, 10) AS work_date, points FROM point_adjustments
            )
            SELECT
                e.id AS employee_id,
                e.discord_id,
                e.direction,
                e.status,
                SUM(d.points) AS points
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
                SELECT employee_id, work_date, points FROM daily_entries
                UNION ALL
                SELECT employee_id, substr(created_at, 1, 10) AS work_date, points FROM point_adjustments
            )
            SELECT employee_id, work_date, SUM(points) AS points
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

    for row in rows:
        points = int(row["points"] or 0)
        total_points += points

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
            f"Баллы: **{points} {points_word(points)}** • Сумма: **{money(points * POINT_VALUE)}**\n"
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
            f"Сумма: **{money(total_points * POINT_VALUE)}**"
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


@bot.tree.command(
    name="servers",
    description="Показать Server и количество бизнесов"
)
@app_commands.guild_only()
async def servers(interaction: discord.Interaction):
    if not is_manager(interaction.user):
        await interaction.response.send_message(
            "У тебя нет прав для этой команды.",
            ephemeral=True
        )
        return

    with database() as db:
        rows = db.execute(
            """
            SELECT
                s.name,
                s.business_count,
                e.discord_id
            FROM servers s
            LEFT JOIN employees e
              ON e.direction = s.name AND e.status = 'active'
            ORDER BY s.name
            """
        ).fetchall()

    embed = make_embed("KARMA NOIRE | SERVERS")

    for row in rows:
        worker = f"<@{row['discord_id']}>" if row["discord_id"] else "Свободно"
        embed.add_field(
            name=row["name"],
            value=(
                f"Бизнесов: **{row['business_count']}**\n"
                f"Сотрудник: {worker}"
            ),
            inline=True
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# PLACE REPORT BUTTONS IN WORK CHANNELS
# ============================================================

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
        db.execute("INSERT INTO point_adjustments (employee_id, points, reason, admin_id, created_at) VALUES (?, ?, ?, ?, ?)",
                   (employee["id"], signed, reason, str(interaction.user.id), now_iso()))
        db.execute("INSERT INTO audit_log (action, details, created_at) VALUES (?, ?, ?)",
                   ("POINTS_ADDED" if action == "add" else "POINTS_REMOVED",
                    f"employee={member.id}; points={signed}; reason={reason}; owner={interaction.user.id}", now_iso()))
        _, _, unpaid = get_employee_totals(db, employee["id"])
    embed = make_embed("KARMA NOIRE | КОРРЕКТИРОВКА БАЛАНСА", f"Баланс сотрудника {member.mention} обновлён.")
    embed.add_field(name="SERVER", value=employee["direction"], inline=True)
    embed.add_field(name="ИЗМЕНЕНИЕ", value=f"{ '+' if signed > 0 else ''}{signed} {points_word(abs(signed))}", inline=True)
    embed.add_field(name="ПРИЧИНА", value=reason, inline=False)
    embed.add_field(name="ТЕКУЩИЙ ДОЛГ", value=f"{unpaid} {points_word(unpaid)} • {money(unpaid * POINT_VALUE)}", inline=False)
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


@bot.tree.command(name="help", description="Показать справочник команд Karma Noire")
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction):
    owner = is_owner(interaction.user)
    manager = is_manager(interaction.user)
    embed = make_embed("KARMA NOIRE | СПРАВОЧНИК", "Команды и их назначение")
    if manager:
        embed.add_field(name="СОТРУДНИКИ", value="`/invite` — принять сотрудника на Server.\n`/uninvite` — уволить и сохранить историю.\n`/employees` — список сотрудников.", inline=False)
        embed.add_field(name="ОТЧЁТЫ", value="`/setup_reports` — установить кнопки отчётов.\n`/stats` — статистика сотрудника.\n`/logs` — журнал действий.", inline=False)
        embed.add_field(name="ФИНАНСЫ", value="`/payroll` — недельная ведомость.\n`/paid` — отметить фактическую выплату.\n`/businesses` — изменить число бизнесов.\n`/servers` — обзор серверов.", inline=False)
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
