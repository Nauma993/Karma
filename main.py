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
# KARMA — AUTO FINKA MANAGER
# Existing database is kept at /app/data/karma.db
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = "/app/data/karma.db"

DIRECTIONS = ["Mesa", "Surprise", "Prescott", "Faraway"]

REVIEW_CHANNEL_NAME = "проверка-отчётов"

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
        role.name in ("Владелец", "Администратор")
        for role in member.roles
    )


def money(value):
    return f"{int(value):,}".replace(",", " ")


def make_embed(title, description=None):
    embed = discord.Embed(
        title=title,
        description=description,
        color=DARK_COLOR,
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="K A R M A  •  AUTO FINKA")
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

        await self.tree.sync()
        print("Karma commands synchronized!")

        if not daily_reminder.is_running():
            daily_reminder.start()

    async def on_ready(self):
        print(f"Karma is online as {self.user}")


bot = KarmaBot()


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
                    "У тебя нет активного назначения на это направление.",
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

        bot.waiting_for_report[
            (interaction.guild_id, interaction.user.id)
        ] = interaction.channel_id

        await interaction.response.send_message(
            "Отправь скриншот автофинки следующим сообщением в этом канале.",
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
            f"Отчёт принят. Начислено **{points}** баллов "
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

        await interaction.followup.send(
            "Отчёт отклонён. Сотрудник сможет отправить исправленный скриншот.",
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
                "Для этого направления ещё не настроено количество бизнесов. "
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

    key = (message.guild.id, message.author.id)
    expected_channel = bot.waiting_for_report.get(key)

    if expected_channel is None:
        return

    if message.channel.id != expected_channel:
        return

    if not message.attachments:
        return

    image_attachment = next(
        (
            attachment for attachment in message.attachments
            if attachment.content_type
            and attachment.content_type.startswith("image/")
        ),
        None
    )

    if image_attachment is None:
        await message.reply("Пришли именно изображение.")
        return

    direction = next(
        (
            name for name, channel_name in WORK_CHANNELS.items()
            if channel_name == message.channel.name
        ),
        None
    )

    if direction is None:
        await message.reply("Не удалось определить направление.")
        return

    review_channel = discord.utils.get(
        message.guild.text_channels,
        name=REVIEW_CHANNEL_NAME
    )

    if review_channel is None:
        await message.reply("Не найден канал проверки отчётов.")
        return

    work_date = get_work_date()

    with database() as db:
        employee = db.execute(
            """
            SELECT id, direction
            FROM employees
            WHERE discord_id = ? AND status = 'active'
            """,
            (str(message.author.id),)
        ).fetchone()

        if not employee or employee["direction"] != direction:
            bot.waiting_for_report.pop(key, None)
            await message.reply(
                "У тебя нет активного назначения на этот рабочий канал."
            )
            return

        server = db.execute(
            "SELECT business_count FROM servers WHERE name = ?",
            (direction,)
        ).fetchone()

        if not server or server["business_count"] < 1:
            await message.reply(
                "Для этого направления ещё не настроено количество бизнесов. "
                "Обратись к администрации."
            )
            return

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
            bot.waiting_for_report.pop(key, None)
            await message.reply(
                "Отчёт за сегодняшний рабочий день уже принят."
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
            bot.waiting_for_report.pop(key, None)
            await message.reply(
                "У тебя уже есть отчёт, ожидающий проверки."
            )
            return

        cursor = db.execute(
            """
            INSERT INTO submissions
            (employee_id, work_date, attachment_url, submitted_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                employee["id"],
                work_date,
                image_attachment.url,
                now_iso()
            )
        )
        submission_id = cursor.lastrowid

    embed = make_embed(
        "K A R M A  |  AUTO FINKA",
        "НОВЫЙ ОТЧЁТ НА ПРОВЕРКУ"
    )

    embed.add_field(
        name="СОТРУДНИК",
        value=message.author.mention,
        inline=True
    )
    embed.add_field(
        name="НАПРАВЛЕНИЕ",
        value=direction.upper(),
        inline=True
    )
    embed.add_field(
        name="РАБОЧАЯ ДАТА",
        value=work_date,
        inline=True
    )
    embed.add_field(
        name="СТАТУС",
        value="ОЖИДАЕТ ПРОВЕРКИ",
        inline=False
    )

    embed.set_image(
        url=f"attachment://{image_attachment.filename}"
    )
    embed.set_footer(
        text=f"KARMA NOIRE  •  REPORT #{submission_id}"
    )

    try:
        review_message = await review_channel.send(
            embed=embed,
            file=await image_attachment.to_file(),
            view=ReviewView()
        )

        with database() as db:
            db.execute(
                """
                UPDATE submissions
                SET review_message_id = ?
                WHERE id = ?
                """,
                (str(review_message.id), submission_id)
            )

    except Exception as error:
        print(f"Report sending error: {error}")

        with database() as db:
            db.execute(
                "DELETE FROM submissions WHERE id = ? AND status = 'pending'",
                (submission_id,)
            )

        await message.reply(
            "Не удалось передать отчёт администрации. Попробуй ещё раз."
        )
        return

    bot.waiting_for_report.pop(key, None)

    await message.reply(
        "Твой отчёт передан администрации на проверку."
    )


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
                    f"по направлению **{employee['direction']}**."
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
        "K A R M A",
        "Система работает. Все основные службы на связи."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# INVITE EMPLOYEE
# ============================================================

@bot.tree.command(
    name="invite",
    description="Принять сотрудника на направление"
)
@app_commands.guild_only()
@app_commands.choices(direction=[
    app_commands.Choice(name=name, value=name)
    for name in DIRECTIONS
])
async def invite(
    interaction: discord.Interaction,
    member: discord.Member,
    direction: app_commands.Choice[str]
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

    direction_name = direction.value

    with database() as db:
        active_employee = get_employee_by_discord_id(db, member.id)
        if active_employee:
            await interaction.response.send_message(
                f"{member.mention} уже работает на направлении "
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
                f"Направление **{direction_name}** уже занято.",
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
            f"{member.mention} принят на направление **{direction_name}**.\n"
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
                f"{self.member.mention} уволен с направления "
                f"**{self.direction}**.\n"
                f"Неоплаченные баллы: **{unpaid}**\n"
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
        "K A R M A  |  УВОЛЬНЕНИЕ",
        "Проверь данные перед подтверждением."
    )
    embed.add_field(name="СОТРУДНИК", value=member.mention, inline=True)
    embed.add_field(name="НАПРАВЛЕНИЕ", value=direction, inline=True)
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
    description="Установить количество бизнесов на направлении"
)
@app_commands.guild_only()
@app_commands.choices(direction=[
    app_commands.Choice(name=name, value=name)
    for name in DIRECTIONS
])
async def businesses(
    interaction: discord.Interaction,
    direction: app_commands.Choice[str],
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
            (count, direction.value)
        )
        db.execute(
            """
            INSERT INTO audit_log
            (action, details, created_at)
            VALUES (?, ?, ?)
            """,
            (
                "BUSINESSES_UPDATED",
                f"{direction.value}; count={count}",
                now_iso()
            )
        )

    await interaction.response.send_message(
        f"Для направления **{direction.value}** установлено "
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
        "K A R M A  |  СТАТИСТИКА",
        f"Сотрудник: {member.mention}"
    )
    embed.add_field(name="НАПРАВЛЕНИЕ", value=employee["direction"], inline=True)
    embed.add_field(name="СТАТУС", value=employee["status"], inline=True)
    embed.add_field(name="ВСЕГО БАЛЛОВ", value=str(earned), inline=True)
    embed.add_field(name="ОПЛАЧЕНО", value=str(paid), inline=True)
    embed.add_field(name="К ВЫПЛАТЕ", value=f"{unpaid} баллов\n{money(unpaid * POINT_VALUE)}", inline=True)
    embed.add_field(name="ОТЧЁТЫ", value=f"Принято: {approved}\nОтклонено: {rejected}", inline=True)

    if daily:
        daily_text = "\n".join(
            f"`{row['work_date']}` — **{row['points']}** баллов"
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


# ============================================================
# WEEKLY PAYROLL
# ============================================================

@bot.tree.command(
    name="payroll",
    description="Сформировать недельный отчёт по всем направлениям"
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
            SELECT
                e.id AS employee_id,
                e.discord_id,
                e.direction,
                e.status,
                SUM(d.points) AS points
            FROM employees e
            JOIN daily_entries d ON d.employee_id = e.id
            WHERE d.work_date BETWEEN ? AND ?
            GROUP BY e.id
            ORDER BY e.direction, e.id
            """,
            (start_date, end_date)
        ).fetchall()

        daily_rows = db.execute(
            """
            SELECT employee_id, work_date, SUM(points) AS points
            FROM daily_entries
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
            f"Баллы: **{points}** • Сумма: **{money(points * POINT_VALUE)}**\n"
            f"По дням: {daily_text or 'нет данных'}"
        )

    embed = make_embed(
        "K A R M A  |  НЕДЕЛЬНАЯ ВЕДОМОСТЬ",
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
            f"Баллы: **{total_points}**\n"
            f"Сумма: **{money(total_points * POINT_VALUE)}**"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

    for chunk in chunks[1:]:
        extra = make_embed("K A R M A  |  НЕДЕЛЬНАЯ ВЕДОМОСТЬ")
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

    embed = make_embed("K A R M A  |  СОТРУДНИКИ")

    if not rows:
        embed.description = "Активных сотрудников нет."
    else:
        for row in rows:
            embed.add_field(
                name=row["direction"],
                value=f"<@{row['discord_id']}>\nПринят: {row['joined_at'][:10]}",
                inline=True
            )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="servers",
    description="Показать направления и количество бизнесов"
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

    embed = make_embed("K A R M A  |  НАПРАВЛЕНИЯ")

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

        embed = make_embed(
            "K A R M A  |  ЕЖЕДНЕВНЫЙ ОТЧЁТ",
            "Нажми кнопку ниже, чтобы отправить скриншот выполненной автофинки."
        )
        embed.add_field(
            name="ПОРЯДОК",
            value=(
                "1. Нажми «Отправить отчёт».\n"
                "2. Пришли скриншот следующим сообщением.\n"
                "3. Дождись проверки администрации."
            ),
            inline=False
        )

        await channel.send(
            embed=embed,
            view=SubmitView()
        )
        posted.append(direction)

    result = "Кнопки размещены: " + (", ".join(posted) if posted else "нет")

    if missing:
        result += "\nНе найдены каналы: " + ", ".join(missing)

    await interaction.followup.send(result, ephemeral=True)


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

    embed = make_embed("K A R M A  |  ЖУРНАЛ ДЕЙСТВИЙ")

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
