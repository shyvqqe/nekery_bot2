import asyncio
from collections import defaultdict
from datetime import timedelta
import os
import random
import threading
import time

# 1. Инициализация FFmpeg (обязательно до работы со звуком)
import static_ffmpeg

static_ffmpeg.add_paths()

# 2. Сторонние библиотеки
from dotenv import load_dotenv
from flask import Flask
from groq import Groq
import psutil
import yt_dlp

# 3. Discord
import discord
from discord import app_commands
from discord.ext import commands

# ---------------- ВЕБ-СЕРВЕР ДЛЯ КРУГЛОСУТОЧНОГО ХОСТИНГА ----------------

app = Flask("")

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()

keep_alive()

# ---------------- ИНИЦИАЛИЗАЦИЯ И НАСТРОЙКИ ----------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

AUTOROLE_ID = 1549170760948645939
MOD_LOG_CHANNEL_ID = 1554433117354459188
WELCOME_CHANNEL_ID = 1554437920931192873
STAFF_ROLE_ID = 1549170708603469947

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Базы данных в памяти
chat_history = {}
MAX_HISTORY = 10

spam_tracker = defaultdict(list)
SPAM_LIMIT = 5
SPAM_INTERVAL = 5
SPAM_MUTE_MINUTES = 2

warns = {}

xp_data = defaultdict(lambda: {"xp": 0, "level": 1})
xp_cooldown = {}
XP_PER_MESSAGE = 15
XP_COOLDOWN_SECONDS = 60

reaction_roles = {}

# Переменные для музыки
queues = {}
loop_status = defaultdict(bool)
current_track = {}

# ---------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------------

async def send_mod_log(guild: discord.Guild, text: str):
    channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if channel:
        await channel.send(text)
    else:
        print("Канал лога модерации не найден — проверь MOD_LOG_CHANNEL_ID")

# ---------------- СОБЫТИЯ DISCORD ----------------

@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано команд: {len(synced)}")
    except Exception as e:
        print(f"Ошибка синхронизации команд: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    role = member.guild.get_role(AUTOROLE_ID)
    if role:
        try:
            await member.add_roles(role, reason="Автовыдача роли при входе")
        except discord.Forbidden:
            pass

    channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            title="👋 Новый участник!",
            description=f"{member.mention}, добро пожаловать на сервер!",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Теперь нас {member.guild.member_count}")
        await channel.send(embed=embed)

@bot.event
async def on_member_remove(member: discord.Member):
    channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            description=f"👋 {member.display_name} покинул сервер",
            color=discord.Color.red()
        )
        await channel.send(embed=embed)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    mapping = reaction_roles.get(payload.message_id)
    if not mapping:
        return
    role_id = mapping.get(str(payload.emoji))
    if not role_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild:
        role = guild.get_role(role_id)
        member = guild.get_member(payload.user_id)
        if role and member:
            await member.add_roles(role, reason="Reaction role")

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    mapping = reaction_roles.get(payload.message_id)
    if not mapping:
        return
    role_id = mapping.get(str(payload.emoji))
    if not role_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild:
        role = guild.get_role(role_id)
        member = guild.get_member(payload.user_id)
        if role and member:
            await member.remove_roles(role, reason="Reaction role снята")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Защита от спама / флуда
    now = time.time()
    timestamps = spam_tracker[message.author.id]
    timestamps.append(now)
    spam_tracker[message.author.id] = [t for t in timestamps if now - t < SPAM_INTERVAL]

    if len(spam_tracker[message.author.id]) > SPAM_LIMIT:
        spam_tracker[message.author.id] = []
        try:
            duration = discord.utils.utcnow() + timedelta(minutes=SPAM_MUTE_MINUTES)
            await message.author.timeout(duration, reason="Автомут за флуд")
            await message.channel.send(f"🔇 {message.author.mention} замучен на {SPAM_MUTE_MINUTES} мин за флуд")
            await send_mod_log(message.guild, f"🔇 **Автомут за флуд** {message.author.mention} на {SPAM_MUTE_MINUTES} мин")
        except discord.Forbidden:
            print("Не хватает прав замутить за спам")
        return

    # Начисление XP / Левелинг
    last = xp_cooldown.get(message.author.id, 0)
    if now - last > XP_COOLDOWN_SECONDS:
        xp_cooldown[message.author.id] = now
        data = xp_data[message.author.id]
        data["xp"] += XP_PER_MESSAGE
        new_level = data["xp"] // 100 + 1
        if new_level > data["level"]:
            data["level"] = new_level
            await message.channel.send(f"🎉 {message.author.mention} достиг уровня **{new_level}**!")

    # ИИ-ответ при упоминании бота
    if bot.user in message.mentions and groq_client:
        question = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if not question:
            await message.reply("Спроси что-нибудь, я отвечу 🙂")
            return

        channel_id = message.channel.id
        history = chat_history.setdefault(channel_id, [])
        history.append({"role": "user", "content": question})
        history[:] = history[-MAX_HISTORY:]

        async with message.channel.typing():
            messages = [
                {"role": "system", "content": "Ты агрессивный помощник в discord-сервере zev1xbot. Отвечай матом, живо и по-русски либо на финском, без канцелярских фраз вроде «дайте знать» и без воды. Если не знаешь точный факт — честно скажи, что не уверен. Помни контекст переписки в этом канале."}
            ] + history

            try:
                response = groq_client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=messages,
                    max_tokens=500,
                )
                answer = response.choices[0].message.content
                history.append({"role": "assistant", "content": answer})
                history[:] = history[-MAX_HISTORY:]
                await message.reply(answer)
            except Exception as e:
                await message.reply(f"Ошибка ИИ: {e}")

    await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel and before.channel.category and before.channel.category.name == "Приватные комнаты":
        if len(before.channel.members) == 0:
            try:
                await before.channel.delete(reason="Комната опустела")
            except discord.HTTPException:
                pass

# ---------------- БАЗОВЫЕ И УТИЛИТАРНЫЕ КОМАНДЫ ----------------

@bot.tree.command(name="ping", description="Проверка что бот жив")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Понг! {round(bot.latency * 1000)}мс")

@bot.tree.command(name="ram", description="Нагрузка на сервер, где работает бот")
async def ram(interaction: discord.Interaction):
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    used_gb = round(mem.used / (1024**3), 1)
    total_gb = round(mem.total / (1024**3), 1)
    await interaction.response.send_message(f"🖥️ CPU: {cpu}%\n💾 RAM: {used_gb} / {total_gb} ГБ ({mem.percent}%)")

@bot.tree.command(name="stats", description="Посмотреть системную нагрузку сервера")
async def stats(interaction: discord.Interaction):
    await ram.callback(interaction)

@bot.tree.command(name="rank", description="Посмотреть свой уровень или чужой")
@app_commands.describe(member="Чей ранг показать")
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    data = xp_data[member.id]
    await interaction.response.send_message(f"📊 {member.mention} — уровень **{data['level']}**, опыт: {data['xp']}")

@bot.tree.command(name="profile", description="Показать карточку профиля пользователя")
@app_commands.describe(member="Чей профиль показать")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    data = xp_data[member.id]
    roles = ", ".join(r.mention for r in member.roles if r.name != "@everyone") or "нет"
    embed = discord.Embed(title=f"Профиль {member.display_name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Уровень", value=f"**{data['level']}** ({data['xp']} XP)", inline=True)
    embed.add_field(name="Аккаунт создан", value=discord.utils.format_dt(member.created_at, "R"), inline=False)
    embed.add_field(name="Зашёл на сервер", value=discord.utils.format_dt(member.joined_at, "R"), inline=False)
    embed.add_field(name="Роли", value=roles, inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Топ участников по уровню")
async def leaderboard(interaction: discord.Interaction):
    top = sorted(xp_data.items(), key=lambda x: x[1]["xp"], reverse=True)[:10]
    if not top:
        await interaction.response.send_message("Пока никто не набрал опыт")
        return
    lines = []
    for i, (user_id, data) in enumerate(top, 1):
        user = interaction.guild.get_member(user_id)
        name = user.display_name if user else f"ID {user_id}"
        lines.append(f"{i}. {name} — уровень {data['level']} ({data['xp']} xp)")
    await interaction.response.send_message("🏆 **Топ участников**\n" + "\n".join(lines))

@bot.tree.command(name="userinfo", description="Информация об участнике")
@app_commands.describe(member="О ком показать инфо")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    await profile.callback(interaction, member)

@bot.tree.command(name="serverinfo", description="Информация о сервере")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(title=guild.name, color=discord.Color.blurple())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Участников", value=guild.member_count, inline=True)
    embed.add_field(name="Создан", value=discord.utils.format_dt(guild.created_at, "R"), inline=True)
    embed.add_field(name="Владелец", value=guild.owner.mention if guild.owner else "неизвестно", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="avatar", description="Показать аватар участника")
@app_commands.describe(member="Чей аватар показать")
async def avatar(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = discord.Embed(title=f"Аватар {member.display_name}")
    embed.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="remind", description="Напомнить через время")
@app_commands.describe(minutes="Через сколько минут напомнить", text="Текст напоминания")
async def remind(interaction: discord.Interaction, minutes: int, text: str):
    await interaction.response.send_message(f"⏰ Напомню через {minutes} мин: {text}", ephemeral=True)
    await asyncio.sleep(minutes * 60)
    try:
        await interaction.user.send(f"⏰ Напоминание: {text}")
    except discord.Forbidden:
        await interaction.channel.send(f"⏰ {interaction.user.mention}, напоминание: {text}")

# ---------------- ИНТЕРАКТИВ И СЕРВЕРНЫЕ ФУНКЦИИ ----------------

@bot.tree.command(name="reactionrole", description="Создать сообщение с выдачей роли по реакции")
@app_commands.describe(text="Текст сообщения", emoji="Эмодзи для реакции", role="Роль, которая будет выдаваться")
@app_commands.checks.has_permissions(manage_roles=True)
async def reactionrole(interaction: discord.Interaction, text: str, emoji: str, role: discord.Role):
    msg = await interaction.channel.send(f"{text}\n\nЖми {emoji} чтобы получить роль {role.mention}")
    await msg.add_reaction(emoji)
    reaction_roles.setdefault(msg.id, {})[emoji] = role.id
    await interaction.response.send_message("✅ Reaction role создана", ephemeral=True)

class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Закрыть тикет", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Тикет закрывается...")
        await asyncio.sleep(2)
        await interaction.channel.delete(reason="Тикет закрыт")

@bot.tree.command(name="ticket", description="Создать приватный тикет для обращения к админам")
async def ticket(interaction: discord.Interaction):
    guild = interaction.guild
    category = discord.utils.get(guild.categories, name="Тикеты")
    if category is None:
        category = await guild.create_category("Тикеты")

    staff_role = guild.get_role(STAFF_ROLE_ID)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    channel = await guild.create_text_channel(
        name=f"тикет-{interaction.user.name}",
        category=category,
        overwrites=overwrites,
    )
    await channel.send(
        f"🎫 {interaction.user.mention} создал тикет. Опишите вопрос, персонал скоро подключится.",
        view=CloseTicketView()
    )
    await interaction.response.send_message(f"✅ Тикет создан: {channel.mention}", ephemeral=True)

@bot.tree.command(name="giveaway", description="Запустить розыгрыш")
@app_commands.describe(prize="Что разыгрываем", minutes="Через сколько минут подвести итоги")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway(interaction: discord.Interaction, prize: str, minutes: int):
    embed = discord.Embed(
        title="🎉 Розыгрыш!",
        description=f"Приз: **{prize}**\nЖми 🎉 чтобы участвовать\nИтоги через {minutes} мин",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction("🎉")

    await asyncio.sleep(minutes * 60)

    msg = await interaction.channel.fetch_message(msg.id)
    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    users = [u async for u in reaction.users() if not u.bot] if reaction else []

    if not users:
        await interaction.channel.send("Никто не участвовал в розыгрыше 😔")
        return

    winner = random.choice(users)
    await interaction.channel.send(f"🎊 Поздравляем {winner.mention}! Ты выиграл **{prize}**")

class AddMembersView(discord.ui.View):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__(timeout=300)
        self.channel = channel

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Выбери, кого впустить", max_values=10)
    async def select_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        for user in select.values:
            await self.channel.set_permissions(user, connect=True, view_channel=True)
        names = ", ".join(u.mention for u in select.values)
        await interaction.response.send_message(f"✅ Впустил в комнату: {names}", ephemeral=True)

@bot.tree.command(name="room", description="Создать голосовой канал")
@app_commands.describe(private="Сделать приватным", limit="Лимит людей (0 = без лимита)")
async def room(interaction: discord.Interaction, private: bool = True, limit: int = 0):
    guild = interaction.guild
    category = discord.utils.get(guild.categories, name="Приватные комнаты")
    if category is None:
        category = await guild.create_category("Приватные комнаты")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(connect=not private, view_channel=not private),
        interaction.user: discord.PermissionOverwrite(connect=True, view_channel=True, manage_channels=True),
        guild.me: discord.PermissionOverwrite(connect=True, view_channel=True, manage_channels=True),
    }

    channel = await guild.create_voice_channel(
        name=f"комната {interaction.user.display_name}",
        category=category,
        overwrites=overwrites,
        user_limit=limit,
    )

    if private:
        await interaction.response.send_message(
            f"🔒 Приватная комната создана: {channel.mention}\nВыбери, кого впустить:",
            view=AddMembersView(channel),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"🔓 Комната создана: {channel.mention}", ephemeral=True)

# ---------------- БЛОК МОДЕРАЦИИ ----------------

@bot.tree.command(name="warn", description="Выдать предупреждение участнику")
@app_commands.describe(member="Кому выдать варн", reason="Причина")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "Не указана"):
    warns.setdefault(member.id, []).append(reason)
    count = len(warns[member.id])
    await interaction.response.send_message(f"⚠️ {member.mention} получил варн ({count}). Причина: {reason}")
    await send_mod_log(interaction.guild, f"⚠️ **Варн** {member.mention} (всего: {count}) от {interaction.user.mention}\nПричина: {reason}")

@bot.tree.command(name="warnings", description="Посмотреть варны участника")
@app_commands.describe(member="Чьи варны посмотреть")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    user_warns = warns.get(member.id, [])
    if not user_warns:
        await interaction.response.send_message(f"У {member.mention} нет варнов", ephemeral=True)
        return
    text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(user_warns))
    await interaction.response.send_message(f"⚠️ Варны {member.mention} ({len(user_warns)}):\n{text}", ephemeral=True)

@bot.tree.command(name="clearwarns", description="Обнулить варны участника")
@app_commands.describe(member="Кому обнулить варны")
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarns(interaction: discord.Interaction, member: discord.Member):
    warns[member.id] = []
    await interaction.response.send_message(f"✅ Варны {member.mention} обнулены")
    await send_mod_log(interaction.guild, f"✅ **Варны обнулены** {member.mention} от {interaction.user.mention}")

@bot.tree.command(name="mute", description="Замутить участника на время")
@app_commands.describe(member="Кого замутить", minutes="На сколько минут", reason="Причина мута")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "Не указана"):
    duration = discord.utils.utcnow() + timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await interaction.response.send_message(f"🔇 {member.mention} замучен на {minutes} мин. Причина: {reason}")
    await send_mod_log(interaction.guild, f"🔇 **Мут** {member.mention} на {minutes} мин от {interaction.user.mention}\nПричина: {reason}")

@bot.tree.command(name="unmute", description="Снять мут досрочно")
@app_commands.describe(member="С кого снять мут")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    await interaction.response.send_message(f"🔊 {member.mention} размучен")
    await send_mod_log(interaction.guild, f"🔊 **Размут** {member.mention} от {interaction.user.mention}")

@bot.tree.command(name="ban", description="Забанить участника")
@app_commands.describe(member="Кого забанить", reason="Причина бана")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "Не указана"):
    await member.ban(reason=reason)
    await interaction.response.send_message(f"🔨 {member.mention} забанен. Причина: {reason}")
    await send_mod_log(interaction.guild, f"🔨 **Бан** {member.mention} от {interaction.user.mention}\nПричина: {reason}")

@bot.tree.command(name="unban", description="Разбанить участника по ID")
@app_commands.describe(user_id="ID участника для разбана")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        await interaction.response.send_message(f"✅ {user.mention} разбанен")
        await send_mod_log(interaction.guild, f"✅ **Разбан** {user.mention} от {interaction.user.mention}")
    except Exception as e:
        await interaction.response.send_message(f"Не получилось разбанить: {e}", ephemeral=True)

@bot.tree.command(name="kick", description="Кикнуть участника")
@app_commands.describe(member="Кого кикнуть", reason="Причина")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "Не указана"):
    await member.kick(reason=reason)
    await interaction.response.send_message(f"👢 {member.mention} кикнут. Причина: {reason}")
    await send_mod_log(interaction.guild, f"👢 **Кик** {member.mention} от {interaction.user.mention}\nПричина: {reason}")

@bot.tree.command(name="clear", description="Удалить сообщения в канале")
@app_commands.describe(amount="Сколько сообщений удалить (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        await interaction.response.send_message("Число должно быть от 1 до 100", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Удалено {len(deleted)} сообщений", ephemeral=True)
    await send_mod_log(interaction.guild, f"🧹 **Очистка** {len(deleted)} сообщений в {interaction.channel.mention} от {interaction.user.mention}")

@bot.tree.command(name="slowmode", description="Включить задержку между сообщениями в канале")
@app_commands.describe(seconds="Задержка в секундах (0 чтобы выключить)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: int):
    await interaction.channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        await interaction.response.send_message("🐇 Slowmode выключен")
    else:
        await interaction.response.send_message(f"🐢 Slowmode: {seconds} сек между сообщениями")

@bot.tree.command(name="lock", description="Закрыть канал для сообщений")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.response.send_message("🔒 Канал закрыт для сообщений")
    await send_mod_log(interaction.guild, f"🔒 **Канал закрыт** {interaction.channel.mention} от {interaction.user.mention}")

@bot.tree.command(name="unlock", description="Открыть канал для сообщений")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
    await interaction.response.send_message("🔓 Канал открыт для сообщений")
    await send_mod_log(interaction.guild, f"🔓 **Канал открыт** {interaction.channel.mention} от {interaction.user.mention}")

# ---------------- ПОЛНЫЙ МУЗЫКАЛЬНЫЙ БЛОК ----------------

YDL_OPTS = {"format": "bestaudio/best", "noplaylist": True, "quiet": True, "default_search": "ytsearch"}
FFMPEG_OPTS = {"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5", "options": "-vn"}

def extract(query):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return info["url"], info["title"]

def play_next(guild, vc):
    if not vc or not vc.is_connected():
        return

    q = queues.get(guild.id, [])
    is_looped = loop_status.get(guild.id, False)

    # Если включён повтор текущего трека
    if is_looped and current_track.get(guild.id):
        url, title = current_track[guild.id]
    elif q:
        url, title = q.pop(0)
        current_track[guild.id] = (url, title)
    else:
        current_track[guild.id] = None
        return

    def after_playing(error):
        if error:
            print(f"Ошибка воспроизведения: {error}")
        bot.loop.call_soon_threadsafe(play_next, guild, vc)

    vc.play(discord.FFmpegPCMAudio(url, **FFMPEG_OPTS), after=after_playing)

@bot.tree.command(name="play", description="Включить музыку с SoundCloud/YouTube")
@app_commands.describe(query="Ссылка или название трека")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        return await interaction.response.send_message("Зайди в голосовой канал!", ephemeral=True)
    
    await interaction.response.defer()
    vc = interaction.guild.voice_client
    if not vc:
        vc = await interaction.user.voice.channel.connect()

    try:
        url, title = await asyncio.to_thread(extract, query)
    except Exception as e:
        return await interaction.followup.send(f"❌ Не удалось найти трек: {e}")

    queues.setdefault(interaction.guild.id, []).append((url, title))

    if not vc.is_playing() and not vc.is_paused():
        play_next(interaction.guild, vc)
        await interaction.followup.send(f"🎶 Сейчас играет: **{title}**")
    else:
        await interaction.followup.send(f"➕ Добавлено в очередь: **{title}**")

@bot.tree.command(name="queue", description="Посмотреть очередь треков")
async def queue_cmd(interaction: discord.Interaction):
    q = queues.get(interaction.guild.id, [])
    curr = current_track.get(interaction.guild.id)
    
    if not curr and not q:
        return await interaction.response.send_message("Очередь пуста 🎵", ephemeral=True)

    text = ""
    if curr:
        text += f"▶️ **Сейчас играет:** {curr[1]}\n\n"
    
    if q:
        text += "**В очереди:**\n"
        for i, (_, title) in enumerate(q[:10], 1):
            text += f"{i}. {title}\n"
        if len(q) > 10:
            text += f"*...и ещё {len(q) - 10} треков*"
    else:
        text += "Дальше очередь пуста."

    await interaction.response.send_message(text)

@bot.tree.command(name="loop", description="Включить/выключить повтор трека")
async def loop_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    loop_status[guild_id] = not loop_status[guild_id]
    
    if loop_status[guild_id]:
        await interaction.response.send_message("🔂 Повтор трека **включён**")
    else:
        await interaction.response.send_message("➡️ Повтор трека **выключен**")

@bot.tree.command(name="skip", description="Пропустить текущий трек")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        loop_status[interaction.guild.id] = False  # Отключаем луп при скипе
        vc.stop()
        await interaction.response.send_message("⏭️ Трек пропущен")
    else:
        await interaction.response.send_message("Ничего не играет", ephemeral=True)

@bot.tree.command(name="pause", description="Поставить музыку на паузу")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Пауза")
    else:
        await interaction.response.send_message("Ничего не играет", ephemeral=True)

@bot.tree.command(name="resume", description="Продолжить воспроизведение")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Продолжаю играть")
    else:
        await interaction.response.send_message("Пауза не стоит", ephemeral=True)

@bot.tree.command(name="stop", description="Остановить музыку и очистить очередь")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        queues.pop(interaction.guild.id, None)
        loop_status[interaction.guild.id] = False
        current_track[interaction.guild.id] = None
        await vc.disconnect()
        await interaction.response.send_message("⏹️ Музыка остановлена, очередь очищена")
    else:
        await interaction.response.send_message("Я не в голосовом канале", ephemeral=True)

# ---------------- ЗАПУСК БОТА ----------------

if __name__ == "__main__":
    bot.run(TOKEN)