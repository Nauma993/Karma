
import os
import sqlite3
import discord
from discord import app_commands
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = "/app/data/karma.db"

DIRECTIONS = ["Mesa", "Surprise", "Prescott", "Faraway"]

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured!")


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_database():
    os.makedirs("/app/data", exist_ok=True)

    with get_db() as db:
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

SERVER_TZ = ZoneInfo("Europe/Moscow")


def get_work_date():
    now = datetime.now(SERVER_TZ)

    if now.hour < 5:
        now -= timedelta(days=1)

    return now.strftime("%Y-%m-%d")
intents = discord.Intents.default()
intents.message_content = True


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
bot.waiting_for_report = {}

REVIEW_CHANNEL_NAME = "проверка-отчётов"

WORK_CHANNELS = {
    "Mesa": "mesa・работа",
    "Surprise": "surprise・работа",
    "Prescott": "prescott・работа",
    "Faraway": "faraway・работа",
}


class SubmitView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Отправить отчёт",
        emoji="📸",
        style=discord.ButtonStyle.primary,
        custom_id="karma:submit_report"
    )
    async def submit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        bot.waiting_for_report[
            (interaction.guild_id, interaction.user.id)
        ] = interaction.channel_id

        await interaction.response.send_message(
            "📸 Теперь отправь скриншот автофинки следующим сообщением в этом канале.",
            ephemeral=True
        )

class ApprovalModal(discord.ui.Modal, title="Подтверждение отчёта"):
    points = discord.ui.TextInput(
        label="Количество начисляемых баллов",
        placeholder="Например: 5",
        required=True,
        max_length=3
    )

    def __init__(self, submission_id, business_count):
        super().__init__()
        self.submission_id = submission_id
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

        with get_db() as db:
            submission = db.execute(
                """
                SELECT employee_id, work_date
                FROM submissions
                WHERE id = ? AND status = 'pending'
                """,
                (self.submission_id,)
            ).fetchone()

            if not submission:
                await interaction.response.send_message(
                    "Заявка уже обработана или не найдена.",
                    ephemeral=True
                )
                return

            employee_id, work_date = submission

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
                    discord.utils.utcnow().isoformat(),
                    self.submission_id
                )
            )

            if cursor.rowcount == 0:
                await interaction.response.send_message(
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
                (employee_id, work_date, points)
            )

            db.execute(
                """
                INSERT INTO audit_log
                (action, details, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    "REPORT_APPROVED",
                    f"Submission {self.submission_id}, points {points}",
                    discord.utils.utcnow().isoformat()
                )
            )

        embed = interaction.message.embeds[0]
        embed.add_field(
            name="СТАТУС",
            value=f"ПРИНЯТ • {points} баллов",
            inline=False
        )

        await interaction.message.edit(embed=embed, view=None)

        await interaction.response.send_message(
            f"Отчёт принят. Начислено баллов: {points}.",
            ephemeral=True
        )


class RejectionModal(discord.ui.Modal, title="Отклонение отчёта"):
    reason = discord.ui.TextInput(
        label="Причина отклонения",
        placeholder="Укажи, что нужно исправить",
        required=True,
        max_length=500
    )

    def __init__(self, submission_id):
        super().__init__()
        self.submission_id = submission_id

    async def on_submit(self, interaction: discord.Interaction):
        if not is_manager(interaction.user):
            await interaction.response.send_message(
                "Недостаточно прав.",
                ephemeral=True
            )
            return

        with get_db() as db:
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
                    discord.utils.utcnow().isoformat(),
                    self.reason.value,
                    self.submission_id
                )
            )

            if cursor.rowcount == 0:
                await interaction.response.send_message(
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
                    f"Submission {self.submission_id}: {self.reason.value}",
                    discord.utils.utcnow().isoformat()
                )
            )

        embed = interaction.message.embeds[0]
        embed.add_field(
            name="СТАТУС",
            value=f"ОТКЛОНЁН\nПричина: {self.reason.value}",
            inline=False
        )

        await interaction.message.edit(embed=embed, view=None)

        await interaction.response.send_message(
            "Отчёт отклонён. Сотрудник сможет отправить его повторно.",
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

        with get_db() as db:
            submission = db.execute(
                """
                SELECT id, employee_id
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

            business_count = db.execute(
                """
                SELECT s.business_count
                FROM employees e
                JOIN servers s ON s.name = e.direction
                WHERE e.id = ?
                """,
                (submission[1],)
            ).fetchone()

        if not business_count or business_count[0] < 1:
            await interaction.response.send_message(
                "Сначала настрой количество бизнесов для этого направления.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            ApprovalModal(submission[0], business_count[0])
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

        with get_db() as db:
            submission = db.execute(
                """
                SELECT id
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
            RejectionModal(submission[0])
        )


bot.add_view(ReviewView())

bot.add_view(SubmitView())
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
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
            attachment
            for attachment in message.attachments
            if attachment.content_type
            and attachment.content_type.startswith("image/")
        ),
        None
    )

    if image_attachment is None:
        await message.reply("❌ Пришли именно изображение.")
        return

    review_channel = discord.utils.get(
        message.guild.text_channels,
        name=REVIEW_CHANNEL_NAME
    )

    if review_channel is None:
        await message.reply("❌ Не найден канал проверки отчётов.")
        return

    await review_channel.send(
        f"📸 **Новый отчёт автофинки**\n"
        f"Сотрудник: {message.author.mention}\n"
        f"Рабочий канал: {message.channel.mention}\n"
        f"Время отправки: {message.created_at.strftime('%d.%m.%Y %H:%M')}",
        file=await image_attachment.to_file()
    )

    del bot.waiting_for_report[key]

    await message.reply(
        "✅ Скриншот отправлен администрации на проверку!"
    )
def is_manager(member):
    return any(
        role.name in ("Владелец", "Администратор")
        for role in member.roles
    )


@bot.tree.command(
    name="ping",
    description="Check if Karma is online"
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Karma is online! Everything works. ✅"
    )


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

    with get_db() as db:
        active_employee = db.execute(
            """
            SELECT direction FROM employees
            WHERE discord_id = ? AND status = 'active'
            """,
            (str(member.id),)
        ).fetchone()

        if active_employee:
            await interaction.response.send_message(
                f"{member.mention} уже работает на направлении "
                f"{active_employee[0]}.",
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
                f"Направление {direction_name} уже занято.",
                ephemeral=True
            )
            return

    role = discord.utils.get(
        interaction.guild.roles,
        name=direction_name
    )

    if role is None:
        await interaction.response.send_message(
            f"Не найдена роль {direction_name}.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        await member.add_roles(
            role,
            reason=f"Принят на {direction_name} через Karma"
        )

        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO employees
                (discord_id, direction, joined_at, status)
                VALUES (?, ?, ?, 'active')
                """,
                (
                    str(member.id),
                    direction_name,
                    discord.utils.utcnow().isoformat()
                )
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
                    f"Employee {member.id}, direction {direction_name}",
                    discord.utils.utcnow().isoformat()
                )
            )

        await interaction.followup.send(
            f"✅ {member.mention} принят на направление "
            f"**{direction_name}**!\n"
            f"Номер сотрудника: `{employee_id}`",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "Karma не может выдать роль. Проверь права бота "
            "и положение его роли в иерархии.",
            ephemeral=True
        )

    except Exception as error:
        try:
            await member.remove_roles(role)
        except discord.HTTPException:
            pass

        print(f"Invite error: {error}")

        await interaction.followup.send(
            "Не удалось принять сотрудника. "
            "Подробности находятся в логах Railway.",
            ephemeral=True
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

    with get_db() as db:
        employee = db.execute(
            """
            SELECT id, direction FROM employees
            WHERE discord_id = ? AND status = 'active'
            """,
            (str(member.id),)
        ).fetchone()

        if not employee:
            await interaction.response.send_message(
                "Этот пользователь не числится активным сотрудником.",
                ephemeral=True
            )
            return

        employee_id, direction = employee

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

    amount = unpaid * 20_000_000

    class UninviteView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)

        @discord.ui.button(
            label="Подтвердить увольнение",
            style=discord.ButtonStyle.danger
        )
        async def confirm(
            self,
            button_interaction: discord.Interaction,
            button: discord.ui.Button
        ):
            if button_interaction.user.id != interaction.user.id:
                await button_interaction.response.send_message(
                    "Только инициатор команды может подтвердить увольнение.",
                    ephemeral=True
                )
                return

            await button_interaction.response.defer(ephemeral=True)

            role = discord.utils.get(
                button_interaction.guild.roles,
                name=direction
            )

            try:
                if role and role in member.roles:
                    await member.remove_roles(
                        role,
                        reason="Увольнение через Karma"
                    )

                with get_db() as db:
                    cursor = db.execute(
                        """
                        UPDATE employees
                        SET status = 'archived',
                            left_at = ?
                        WHERE id = ? AND status = 'active'
                        """,
                        (
                            discord.utils.utcnow().isoformat(),
                            employee_id
                        )
                    )

                    if cursor.rowcount == 0:
                        raise RuntimeError(
                            "Сотрудник уже не является активным."
                        )

                    db.execute(
                        """
                        INSERT INTO audit_log
                        (action, details, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (
                            "UNINVITE",
                            f"Employee {member.id}, direction {direction}",
                            discord.utils.utcnow().isoformat()
                        )
                    )

                for item in self.children:
                    item.disabled = True


                await button_interaction.followup.send(
                    f"✅ {member.mention} уволен с направления "
                    f"**{direction}**.\n"
                    f"Неоплаченные баллы: **{unpaid}**\n"
                    f"К выплате: **{amount:,}**\n"
                    f"История сотрудника сохранена в архиве.",
                    ephemeral=True
                )

            except Exception as error:
                print(f"Uninvite error: {error}")

                if role:
                    try:
                        await member.add_roles(
                            role,
                            reason="Восстановление роли после ошибки увольнения"
                        )
                    except discord.HTTPException:
                        pass

                await button_interaction.followup.send(
                    "Не удалось завершить увольнение. "
                    "Проверь логи Railway.",
                    ephemeral=True
                )

        @discord.ui.button(
            label="Отмена",
            style=discord.ButtonStyle.secondary
        )
        async def cancel(
            self,
            button_interaction: discord.Interaction,
            button: discord.ui.Button
        ):
            if button_interaction.user.id != interaction.user.id:
                await button_interaction.response.send_message(
                    "Отменить действие может только его инициатор.",
                    ephemeral=True
                )
                return

            for item in self.children:
                item.disabled = True

            await button_interaction.response.edit_message(
                content="Увольнение отменено.",
                view=self
            )

    view = UninviteView()

    await interaction.response.send_message(
        f"**Подтверди увольнение сотрудника**\n\n"
        f"Сотрудник: {member.mention}\n"
        f"Направление: **{direction}**\n"
        f"Неоплаченные баллы: **{unpaid}**\n"
        f"Сумма к выплате: **{amount:,}**\n\n"
        f"История будет сохранена.",
        view=view,
        ephemeral=True
    )
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

        await channel.send(
            "📸 **Ежедневный отчёт автофинки**\n"
            "Нажми кнопку ниже, чтобы отправить скриншот "
            "выполненной автофинки.",
            view=SubmitView()
        )

        posted.append(direction)

    result = "✅ Кнопки размещены в: " + ", ".join(posted)

    if missing:
        result += "\n⚠️ Не найдены каналы: " + ", ".join(missing)

    await interaction.followup.send(result, ephemeral=True)


bot.run(TOKEN)
