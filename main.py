
import os
import sqlite3
import discord
from discord import app_commands

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

bot.run(TOKEN)
