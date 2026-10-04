
import asyncio
import os
import random
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from urllib.parse import urlparse

import discord
import psutil
import yt_dlp
from discord import app_commands
from discord.ext import commands
from static_ffmpeg import run as static_ffmpeg_run

try:
    from dotenv import load_dotenv
    HAS_DOTENV=True
except ImportError:
    HAS_DOTENV=False
    def load_dotenv(*a,**k): return False

from flask import Flask
from openai import OpenAI

# ---------------- ВЕБ-СЕРВЕР ДЛЯ RENDER ----------------
app = Flask("")
@app.route("/")
def home():
    return "Nekery Bot is Running!"
def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
def keep_alive():
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()

# ---------------- ИНИЦИАЛИЗАЦИЯ ----------------
if HAS_DOTENV:
    try:
        load_dotenv()
    except:
        pass

TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-nano")
openai_client = (
    OpenAI(
        api_key=OPENAI_API_KEY,
        timeout=90.0,
    )
    if OPENAI_API_KEY
    else None
)

AUTOROLE_ID = 1549170760948645939
MOD_LOG_CHANNEL_ID = 1554433117354459188
WELCOME_CHANNEL_ID = 1554437920931192873
STAFF_ROLE_ID = 1549170708603469947

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- ДАННЫЕ В ПАМЯТИ ----------------
spam_tracker = defaultdict(deque)
gif_spam_tracker = defaultdict(deque)
spam_last_timeout = defaultdict(float)
SPAM_LIMIT = 3
SPAM_INTERVAL = 1.0
GIF_SPAM_LIMIT = 3
GIF_SPAM_INTERVAL = 5.0
SPAM_MUTE_MINUTES = 15
SPAM_REPEAT_MUTE_MINUTES = 20
SPAM_REPEAT_WINDOW = 60 * 60
warns = {}
xp_data = defaultdict(lambda: {"xp": 0, "level": 1})
xp_cooldown = {}
RANK_ACTIVITY = defaultdict(lambda: {
    "messages": 0,
    "word_messages": 0,
    "long_messages": 0,
    "characters": 0,
})
XP_PER_MESSAGE = 15
XP_COOLDOWN_SECONDS = 60
reaction_roles = {}
chat_history = {}
MAX_HISTORY = 10
loop_status = defaultdict(bool)
MUSIC_QUEUES = defaultdict(deque)
MUSIC_NOW_PLAYING = {}
MUSIC_SOURCES = {}
MUSIC_STARTING = set()
MUSIC_CONNECT_LOCKS = {}
MUSIC_CHANNELS = {}
MUSIC_RECOVERY_TASKS = {}
MUSIC_SKIP_PENDING = set()
MUSIC_VOLUME = defaultdict(lambda: 100)
FFMPEG_EXECUTABLE = None
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": False,
    "extract_flat": False,
    "source_address": "0.0.0.0",
}

# ---------------- VEYR: SERVER SIGNAL / STATE / ECHO MEMORY ----------------
VEYR_STATES = (
    "DORMANT",
    "ACTIVE",
    "UNSTABLE",
    "ECHO",
    "VOID",
    "ANOMALY",
    "RITUAL",
)
SERVER_SIGNAL = defaultdict(lambda: {"value": 0.0, "updated_at": time.time()})
SERVER_STATE = defaultdict(lambda: "DORMANT")
ECHO_MEMORY = defaultdict(list)
SIGNAL_MEMORY_LIMIT = 200
CHRONICLE = defaultdict(list)
CHRONICLE_LIMIT = 100
DISCOVERED_LAYERS = defaultdict(set)
STATE_CHANGED_BEFORE = set()
SIGNAL_RARE_LAST = defaultdict(float)
ANOMALY_LAST_TRIGGER = defaultdict(float)
ANOMALY_PUBLIC_NOTICE = defaultdict(float)
RITUAL_SESSIONS = defaultdict(dict)
RITUAL_LAST_COMPLETE = defaultdict(dict)


def record_chronicle(guild_id: int, kind: str, detail: str, visible_at: float = 0.0) -> None:
    if kind not in {"DISCOVERY", "FIRST", "RARE", "LEGENDARY", "MYTHIC", "ANOMALY"}:
        return
    records = CHRONICLE[guild_id]
    records.append({"at": time.time(), "kind": kind, "detail": detail[:240], "visible_at": visible_at})
    if len(records) > CHRONICLE_LIMIT:
        del records[:-CHRONICLE_LIMIT]


def discover_layer(guild_id: int, section: str) -> None:
    if section in DISCOVERED_LAYERS[guild_id]:
        return
    DISCOVERED_LAYERS[guild_id].add(section)
    record_chronicle(guild_id, "DISCOVERY", f"{section} layer disclosed")


def _recent_echo_count(guild_id: int, now: float, window_seconds: int) -> int:
    return sum(1 for item in ECHO_MEMORY[guild_id] if now - item["at"] <= window_seconds)


def _change_server_state(guild_id: int, state: str, source: str) -> bool:
    previous = SERVER_STATE[guild_id]
    if previous == state:
        return False
    SERVER_STATE[guild_id] = state
    store_echo(guild_id, "state_change", f"{previous} → {state} ({source})")
    if guild_id not in STATE_CHANGED_BEFORE:
        STATE_CHANGED_BEFORE.add(guild_id)
        record_chronicle(guild_id, "FIRST", f"First state transition: {previous} → {state}")
    return True


def evaluate_anomaly(guild_id: int, now=None) -> bool:
    """Deterministic anomaly gate: state, signal band, UTC window, echo density, cooldown."""
    now = time.time() if now is None else now
    state = SERVER_STATE[guild_id]
    value = SERVER_SIGNAL[guild_id]["value"]
    utc_minute = time.gmtime(now).tm_min
    if state not in {"ECHO", "UNSTABLE"}:
        return False
    if not 18.0 <= value <= 80.0 or utc_minute not in {0, 1}:
        return False
    if _recent_echo_count(guild_id, now, 20 * 60) < 4:
        return False
    if now - ANOMALY_LAST_TRIGGER[guild_id] < 6 * 60 * 60:
        return False

    ANOMALY_LAST_TRIGGER[guild_id] = now
    hidden = state == "ECHO" or value < 35.0
    visible_at = now + 24 * 60 * 60 if hidden else 0.0
    record_chronicle(
        guild_id,
        "ANOMALY",
        f"Signal {value:.2f}; state {state}; echo density {_recent_echo_count(guild_id, now, 1200)}",
        visible_at=visible_at,
    )
    store_echo(guild_id, "anomaly", f"Anomaly registered ({'internal' if hidden else 'surface'})")
    if not hidden:
        _change_server_state(guild_id, "ANOMALY", "anomaly engine")
        ANOMALY_PUBLIC_NOTICE[guild_id] = now + 60 * 60
    return True


def update_server_signal(guild_id: int, activity: float = 0.0) -> float:
    """Apply quiet time drift and a small activity pulse to one server's signal."""
    now = time.time()
    signal = SERVER_SIGNAL[guild_id]
    elapsed_hours = max(0.0, now - signal["updated_at"]) / 3600
    # Activity fades toward baseline over time; this internal model is not shown.
    previous = signal["value"]
    value = previous * (0.98 ** elapsed_hours)
    signal["value"] = round(max(0.0, min(100.0, value + activity)), 2)
    signal["updated_at"] = now
    if signal["value"] - previous >= 14.0 and now - SIGNAL_RARE_LAST[guild_id] >= 6 * 60 * 60:
        SIGNAL_RARE_LAST[guild_id] = now
        record_chronicle(guild_id, "RARE", f"Signal shifted to {signal['value']:.2f}")
    evaluate_anomaly(guild_id, now)
    return signal["value"]


def store_echo(guild_id: int, kind: str, detail: str) -> None:
    echoes = ECHO_MEMORY[guild_id]
    echoes.append({"at": time.time(), "kind": kind, "detail": detail[:240]})
    if len(echoes) > SIGNAL_MEMORY_LIMIT:
        del echoes[:-SIGNAL_MEMORY_LIMIT]


async def set_server_state(guild_id: int, state: str, source: str) -> None:
    if _change_server_state(guild_id, state, source):
        evaluate_anomaly(guild_id)


RITUAL_RULES = {
    "CONVERGENCE": {
        "actions": ("SURVEY", "ANCHOR"), "participants": 3, "window": 10 * 60,
        "states": {"ACTIVE", "ECHO"}, "signal": (10.0, 60.0), "record": "LEGENDARY",
    },
    "AFTERIMAGE": {
        "actions": ("LISTEN", "RELEASE"), "participants": 2, "window": 5 * 60,
        "states": {"ECHO", "UNSTABLE"}, "signal": (25.0, 75.0), "record": "MYTHIC",
    },
}
RITUAL_COOLDOWN_SECONDS = 12 * 60 * 60


def _ritual_readout(guild_id: int) -> str:
    lines = ["RITUAL SIGNALS", "Collective traces. Each window closes on its own."]
    now = time.time()
    for ritual, rules in RITUAL_RULES.items():
        session = RITUAL_SESSIONS[guild_id].get(ritual)
        last_done = RITUAL_LAST_COMPLETE[guild_id].get(ritual, 0.0)
        if last_done and now - last_done < 60 * 60:
            status = "RITUAL COMPLETE"
        elif session and session["expires_at"] > now:
            participants = len(session["participants"])
            actions = len(session["actions"])
            status = f"WINDOW OPEN  ·  {participants}/{rules['participants']}  ·  {actions}/{len(rules['actions'])} traces"
        elif now - last_done < RITUAL_COOLDOWN_SECONDS:
            status = "RESIDUAL INTERVAL"
        else:
            low, high = rules["signal"]
            eligible = SERVER_STATE[guild_id] in rules["states"] and low <= SERVER_SIGNAL[guild_id]["value"] <= high
            status = "WINDOW UNFORMED" if eligible else "SIGNAL INSUFFICIENT"
        lines.append(f"\n`{ritual}`\n{status}")
    latest = max(RITUAL_LAST_COMPLETE[guild_id].values(), default=0.0)
    if latest and now - latest < 60 * 60:
        lines.insert(0, "RITUAL COMPLETE\n")
    return "\n".join(lines)


async def contribute_ritual(guild_id: int, ritual: str, action: str, user_id: int) -> str:
    rules = RITUAL_RULES[ritual]
    now = time.time()
    session = RITUAL_SESSIONS[guild_id].get(ritual)
    if session is None or session["expires_at"] <= now:
        low, high = rules["signal"]
        if now - RITUAL_LAST_COMPLETE[guild_id].get(ritual, 0.0) < RITUAL_COOLDOWN_SECONDS:
            return "Residual interval. The pattern is not available."
        if SERVER_STATE[guild_id] not in rules["states"] or not low <= SERVER_SIGNAL[guild_id]["value"] <= high:
            return "No response from the current signal."
        session = {"started_at": now, "expires_at": now + rules["window"], "participants": set(), "actions": set(), "users": {}}
        RITUAL_SESSIONS[guild_id][ritual] = session

    user_actions = session["users"].setdefault(user_id, set())
    if action in user_actions:
        return "Trace already registered."
    user_actions.add(action)
    session["participants"].add(user_id)
    session["actions"].add(action)
    store_echo(guild_id, "ritual_trace", f"{ritual}: {action}")

    if len(session["participants"]) >= rules["participants"] and set(rules["actions"]).issubset(session["actions"]):
        RITUAL_LAST_COMPLETE[guild_id][ritual] = now
        RITUAL_SESSIONS[guild_id].pop(ritual, None)
        record_chronicle(guild_id, rules["record"], f"RITUAL COMPLETE: {ritual}")
        store_echo(guild_id, "ritual_complete", ritual)
        await set_server_state(guild_id, "RITUAL", f"ritual:{ritual}")
        update_server_signal(guild_id, activity=16.0)
        return "RITUAL COMPLETE"
    return "Trace registered."


class RitualActionSelect(discord.ui.Select):
    def __init__(self, ritual: str, actions, row: int):
        self.ritual = ritual
        super().__init__(
            placeholder=f"{ritual} / ADD TRACE",
            min_values=1,
            max_values=1,
            row=row,
            options=[discord.SelectOption(label=action, value=action) for action in actions],
        )

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        if interaction.guild is None or interaction.guild.id != view.guild_id:
            return await view.reject(interaction)
        result = await contribute_ritual(view.guild_id, self.ritual, self.values[0], interaction.user.id)
        try:
            await interaction.response.edit_message(embed=_veyr_panel(view.guild_id, "RITUALS", result), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


VEYR_SECTIONS = (
    ("SYSTEM", "Control Center"),
    ("IDENTITY", "Identity"),
    ("MUSIC", "Music"),
    ("SIGNAL", "Signal"),
    ("EVENTS", "Events"),
    ("ARTIFACTS", "Artifacts"),
    ("QUESTS", "Quests"),
    ("CHRONICLE", "Chronicle"),
    ("RITUALS", "Rituals"),
    ("PROFILE", "Profile"),
)


def _signal_readout(value: float) -> str:
    filled = max(0, min(10, round(value / 10)))
    return f"{('▰' * filled) + ('▱' * (10 - filled))}  {value:05.2f}"


def _veyr_panel(guild_id: int, section: str = "SYSTEM", notice: str = "") -> discord.Embed:
    value = update_server_signal(guild_id)
    state = SERVER_STATE[guild_id]
    embed = discord.Embed(color=0x30343B)
    embed.set_author(name="VEYR")

    if section == "SYSTEM":
        texture = {
            "DORMANT": "No active surface.",
            "ACTIVE": "Surface stable.",
            "UNSTABLE": "Surface variance detected.",
            "ECHO": "Residual traces present.",
            "VOID": "",
            "ANOMALY": "Sequence does not resolve.",
            "RITUAL": "Pattern held open.",
        }.get(state, "")
        anomaly_banner = "ANOMALY DETECTED\n\n" if ANOMALY_PUBLIC_NOTICE[guild_id] > time.time() else ""
        embed.description = (
            f"{anomaly_banner}SYSTEM STATUS\n`PRESENT`\n\nSERVER SIGNAL\n`{_signal_readout(value)}`"
            f"\n\nCURRENT STATE\n`{state}`"
            + (f"\n\n{texture}" if texture else "")
        )
    elif section == "CHRONICLE":
        now = time.time()
        records = [item for item in CHRONICLE[guild_id] if item["visible_at"] <= now]
        embed.description = "CHRONICLE / SERVER RECORD"
        if records:
            lines = [
                f"`{item['kind']}`  {item['detail']}  ·  <t:{int(item['at'])}:R>"
                for item in reversed(records[-8:])
            ]
            embed.add_field(name="TRACE", value="\n".join(lines)[:1000], inline=False)
        else:
            embed.add_field(name="TRACE", value="This layer has left no trace.", inline=False)
    elif section == "RITUALS":
        embed.description = _ritual_readout(guild_id) + (f"\n\n{notice}" if notice else "")
    elif section in ("SIGNAL", "EVENTS"):
        echoes = ECHO_MEMORY[guild_id]
        heading = {"SIGNAL": "SIGNAL TRACE", "EVENTS": "RECENT EVENTS"}[section]
        embed.description = f"{heading}\nSTATE  `{state}`\nSIGNAL  `{_signal_readout(value)}`"
        if echoes:
            recent = echoes[-(3 if section == "SIGNAL" else 6):]
            lines = [f"`{item['kind']}`  {item['detail']}  ·  <t:{int(item['at'])}:R>" for item in reversed(recent)]
            embed.add_field(name="ECHO MEMORY", value="\n".join(lines)[:1000], inline=False)
        else:
            embed.add_field(name="ECHO MEMORY", value="No residual records.", inline=False)
    else:
        # Quiet surfaces disclose only that no readable layer is available yet.
        embed.description = f"{section}\n\n`LAYER NOT DISCLOSED`\n`SIGNAL INSUFFICIENT`"
    embed.set_footer(text=f"{section}  ·  {state}")
    return embed


class VeyrSectionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="OPEN LAYER",
            min_values=1,
            max_values=1,
            row=0,
            options=[discord.SelectOption(label=label, value=value) for value, label in VEYR_SECTIONS],
        )

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        section = self.values[0]
        discover_layer(view.guild_id, section)
        view.show_section(section)
        try:
            await interaction.response.edit_message(embed=_veyr_panel(view.guild_id, section), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


class VeyrStateSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="STATE TRACE",
            min_values=1,
            max_values=1,
            row=1,
            options=[discord.SelectOption(label=value, value=value) for value in VEYR_STATES],
        )

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
            return await view.reject(interaction, "Access to this layer is restricted.")
        state = self.values[0]
        if state not in VEYR_STATES:
            return await view.reject(interaction)
        await set_server_state(view.guild_id, state, f"operator:{interaction.user.id}")
        try:
            view.show_system_layer()
            await interaction.response.edit_message(embed=_veyr_panel(view.guild_id), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


class VeyrStateLayerButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="STATE TRACE", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
            return await view.reject(interaction, "Access to this layer is restricted.")
        view.show_state_layer()
        try:
            await interaction.response.edit_message(embed=_veyr_state_layer(view.guild_id), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


class VeyrReturnButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="RETURN", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
            return await view.reject(interaction, "Access to this layer is restricted.")
        view.show_system_layer()
        try:
            await interaction.response.edit_message(embed=_veyr_panel(view.guild_id), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


def _veyr_state_layer(guild_id: int) -> discord.Embed:
    embed = discord.Embed(color=0x30343B)
    embed.set_author(name="VEYR")
    embed.description = f"STATE TRACE\n\nCURRENT STATE\n`{SERVER_STATE[guild_id]}`\n\nSelect a transition."
    return embed


class VeyrControlView(discord.ui.View):
    def __init__(self, guild_id: int, owner_id: int, can_manage_guild: bool):
        super().__init__(timeout=3600)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.can_manage_guild = can_manage_guild
        self.show_system_layer()

    def show_system_layer(self):
        self.show_section("SYSTEM")

    def show_section(self, section: str):
        self.current_section = section
        self.clear_items()
        self.add_item(VeyrSectionSelect())
        self.add_item(VeyrRefreshButton())
        if section == "RITUALS":
            self.add_item(RitualActionSelect("CONVERGENCE", ("SURVEY", "ANCHOR"), row=2))
            self.add_item(RitualActionSelect("AFTERIMAGE", ("LISTEN", "RELEASE"), row=3))
        if self.can_manage_guild:
            self.add_item(VeyrStateLayerButton())

    def show_state_layer(self):
        self.clear_items()
        self.add_item(VeyrStateSelect())
        self.add_item(VeyrReturnButton())

    async def reject(self, interaction: discord.Interaction, message: str = "This panel is bound to its opener."):
        try:
            await interaction.response.send_message(message, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            return

class VeyrRefreshButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="RE-SCAN", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: VeyrControlView = self.view
        if interaction.user.id != view.owner_id:
            return await view.reject(interaction)
        try:
            await interaction.response.edit_message(embed=_veyr_panel(view.guild_id, view.current_section), view=view)
        except (discord.NotFound, discord.HTTPException):
            return


@bot.tree.command(name="veyr", description="Open the VEYR Control Center")
async def veyr_control_center(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("VEYR has no server signal in this context.", ephemeral=True)
    discover_layer(interaction.guild.id, "SYSTEM")
    view = VeyrControlView(
        interaction.guild.id,
        interaction.user.id,
        interaction.user.guild_permissions.manage_guild,
    )
    try:
        await interaction.response.send_message(embed=_veyr_panel(interaction.guild.id), view=view, ephemeral=True)
    except (discord.NotFound, discord.HTTPException):
        return

# ---------------- DISCORD / VOICE ----------------
@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user} | {len(bot.guilds)} серверов", flush=True)
    if openai_client:
        print(f"[AI] OpenAI enabled; model={OPENAI_MODEL}", flush=True)
    else:
        print("[AI] OPENAI_API_KEY is missing; AI replies are disabled.", flush=True)
    if not getattr(bot, "commands_synced", False):
        try:
            synced = await bot.tree.sync()
            bot.commands_synced = True
            print(f"Синхронизировано {len(synced)} команд")
        except Exception as e:
            print(f"Ошибка синхронизации: {e}")

# ---------------- Вспомогательные функции (старые команды) ----------------
async def send_mod_log(guild: discord.Guild, message: str):
    channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(message)
        except discord.HTTPException as error:
            print(f"[Мод-лог] Не удалось отправить запись на сервере {guild.id}: {error!r}")


async def send_spam_incident_log(
    guild: discord.Guild,
    member: discord.Member,
    reason: str,
    timeout_minutes: int,
    messages: list[discord.Message],
) -> None:
    """Record spam evidence in this guild's configured private moderation channel."""
    channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if channel is None:
        print(f"[Анти-спам] Мод-лог {MOD_LOG_CHANNEL_ID} недоступен на сервере {guild.id}")
        return

    embed = discord.Embed(
        title="VEYR // SPAM TRACE",
        description="Зафиксирована серия сообщений, требующая модерации.",
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Участник", value=f"{member.mention} (`{member.id}`)", inline=False)
    embed.add_field(name="Канал", value=messages[0].channel.mention, inline=True)
    embed.add_field(name="Сигнал", value=reason, inline=True)
    embed.add_field(
        name="Автоматическое действие",
        value=f"Удаление {len(messages)} сообщений; таймаут {timeout_minutes} минут.",
        inline=False,
    )
    evidence = "\n".join(
        f"[{index}. Сообщение]({item.jump_url})"
        for index, item in enumerate(messages, start=1)
    )
    embed.add_field(name="Материалы", value=evidence[:1024], inline=False)
    embed.set_footer(
        text="Внутренняя запись. Жалобу в Discord при необходимости отправляет модератор вручную."
    )

    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as error:
        print(f"[Анти-спам] Не удалось записать инцидент на сервере {guild.id}: {error!r}")

# --- тут идут твои старые команды бана, кика, slowmode, lock и т.д. (оставляем без изменений) ---
# Я сократил для примера, в файле ниже они все есть - копипаст из старого bot.py

# ---------------- АНТИ-СПАМ / XP / WELCOME / AI ----------------
@bot.event
async def on_member_join(member):
    update_server_signal(member.guild.id, activity=2.0)
    store_echo(member.guild.id, "member_join", f"Участник присоединился: {member.id}")
    # Autorole
    try:
        role = member.guild.get_role(AUTOROLE_ID)
        if role:
            await member.add_roles(role, reason="Autorole")
    except:
        pass
    # Welcome
    try:
        ch = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if ch:
            await ch.send(f"VEYR зафиксировал новое присутствие: {member.mention}.")
    except:
        pass


def _message_contains_gif(message: discord.Message) -> bool:
    for attachment in message.attachments:
        filename = attachment.filename.lower()
        content_type = (attachment.content_type or "").lower()
        if content_type == "image/gif" or filename.endswith((".gif", ".gifv")):
            return True

    content = (message.content or "").lower()
    if any(marker in content for marker in (
        "tenor.com/view/", "media.tenor.com/", "giphy.com/gifs/", "media.giphy.com/media/", ".gifv", ".gif"
    )):
        return True

    for embed in message.embeds:
        provider = (embed.provider.name or "").lower() if embed.provider else ""
        if "gif" in (embed.type or "").lower() or provider in {"tenor", "giphy"}:
            return True
    return False


async def _moderate_message_spam(message: discord.Message, now: float) -> bool:
    guild = message.guild
    if guild is None:
        return False

    member = message.author
    permissions = getattr(member, "guild_permissions", None)
    if permissions and (permissions.administrator or permissions.manage_messages):
        return False

    key = (guild.id, member.id)
    recent_messages = spam_tracker[key]
    while recent_messages and now - recent_messages[0][0] >= SPAM_INTERVAL:
        recent_messages.popleft()
    recent_messages.append((now, message))

    recent_gifs = gif_spam_tracker[key]
    while recent_gifs and now - recent_gifs[0][0] >= GIF_SPAM_INTERVAL:
        recent_gifs.popleft()
    if _message_contains_gif(message):
        recent_gifs.append((now, message))

    message_flood = len(recent_messages) >= SPAM_LIMIT
    gif_flood = len(recent_gifs) >= GIF_SPAM_LIMIT
    if not message_flood and not gif_flood:
        return False

    reason = "флуд сообщениями" if message_flood else "флуд GIF"
    messages_to_delete = list(recent_messages) if message_flood else list(recent_gifs)
    unique_messages = {item.id: item for _, item in messages_to_delete}
    recent_messages.clear()
    recent_gifs.clear()

    previous_timeout = spam_last_timeout[key]
    timeout_minutes = (
        SPAM_REPEAT_MUTE_MINUTES
        if previous_timeout and now - previous_timeout < SPAM_REPEAT_WINDOW
        else SPAM_MUTE_MINUTES
    )
    spam_last_timeout[key] = now

    await send_spam_incident_log(
        guild,
        member,
        reason,
        timeout_minutes,
        list(unique_messages.values()),
    )

    deletion_results = await asyncio.gather(
        *(item.delete() for item in unique_messages.values()),
        return_exceptions=True,
    )
    for result in deletion_results:
        if isinstance(result, discord.Forbidden):
            print(f"[Анти-спам] Нет права удалять сообщения на сервере {guild.id}")
            break
        if isinstance(result, discord.HTTPException) and result.status != 404:
            print(f"[Анти-спам] Ошибка удаления сообщения на сервере {guild.id}: {result!r}")

    timeout_applied = False
    try:
        until = discord.utils.utcnow() + timedelta(minutes=timeout_minutes)
        await member.timeout(until, reason=f"VEYR: {reason}")
        timeout_applied = True
        store_echo(guild.id, "spam_timeout", f"Timeout участника {member.id}: {reason}")
    except discord.Forbidden:
        print(f"[Анти-спам] Нет права выдать таймаут участнику {member.id} на сервере {guild.id}")
    except discord.HTTPException as error:
        print(f"[Анти-спам] Не удалось выдать таймаут: {error!r}")

    if timeout_applied:
        try:
            await message.channel.send(
                f"{member.mention} — сообщения удалены. Таймаут: {timeout_minutes} минут ({reason}).",
                delete_after=10,
            )
        except discord.HTTPException as error:
            print(f"[Анти-спам] Не удалось отправить уведомление: {error!r}")
    return True


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    now = time.time()
    if await _moderate_message_spam(message, now):
        return
    if message.guild:
        update_server_signal(message.guild.id, activity=0.05)
        content = message.content or ""
        stats = RANK_ACTIVITY[(message.guild.id, message.author.id)]
        stats["messages"] += 1
        stats["word_messages"] += bool(content.strip())
        stats["long_messages"] += len(content) > 100
        stats["characters"] += len(content)
    # XP
    uid = message.author.id
    if now - xp_cooldown.get(uid, 0) > XP_COOLDOWN_SECONDS:
        xp_cooldown[uid] = now
        data = xp_data[uid]
        data["xp"] += XP_PER_MESSAGE
        # Level up every 500 xp
        new_level = data["xp"] // 500 + 1
        if new_level > data["level"]:
            data["level"] = new_level
            # можно отправить сообщение о левел-апе

    # AI chat - отвечает когда упоминают бота
    if bot.user in message.mentions:
        print(f"[AI] Mention received; OpenAI configured={bool(openai_client)}", flush=True)
    if bot.user in message.mentions and openai_client:
        # сохраняем историю
        gid = message.guild.id if message.guild else 0
        history = chat_history.setdefault(gid, [])
        history.append({"role": "user", "content": f"{message.author.display_name}: {message.content}"})
        if len(history) > MAX_HISTORY:
            history.pop(0)
        try:
            messages = [{
                "role": "system",
                "content": "Ты VEYR — anomalous system layer внутри сервера. Отвечай кратко, холодно и по-русски. Не называй себя Discord-ботом и не раскрывай внутреннюю формулу сигнала.",
            }] + list(history)
            loop = asyncio.get_running_loop()
            completion = await loop.run_in_executor(
                None,
                lambda: openai_client.responses.create(
                    model=OPENAI_MODEL,
                    input=messages,
                    max_output_tokens=300,
                ),
            )
            reply = completion.output_text
            history.append({"role": "assistant", "content": reply})
            await message.reply(reply[:1900])
        except Exception as e:
            print(f"OpenAI error: {e!r}", flush=True)
            try:
                await message.reply("Сигнал не отвечает. Попробуй позже.")
            except discord.DiscordException:
                pass
    elif bot.user in message.mentions:
        print("[AI] OPENAI_API_KEY is not configured; AI replies are disabled.", flush=True)
        try:
            await message.reply("AI-канал не настроен.")
        except discord.DiscordException:
            pass

    await bot.process_commands(message)

# ---------------- МОДЕРАЦИЯ (скопировано из старого) ----------------
@bot.tree.command(name="ban", description="Забанить пользователя")
@app_commands.describe(user="Кого забанить", reason="Причина")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана"):
    try:
        await user.ban(reason=reason)
        await interaction.response.send_message(f"🔨 {user.mention} забанен. Причина: {reason}")
        await send_mod_log(interaction.guild, f"🔨 **Бан** {user} от {interaction.user.mention}. Причина: {reason}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="kick", description="Кикнуть пользователя")
@app_commands.describe(user="Кого кикнуть", reason="Причина")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "Не указана"):
    try:
        await user.kick(reason=reason)
        await interaction.response.send_message(f"👢 {user.mention} кикнут. Причина: {reason}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="clear", description="Очистить сообщения")
@app_commands.describe(amount="Количество (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: int):
    if not 1 <= amount <= 100:
        return await interaction.response.send_message("❌ От 1 до 100", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Удалено {len(deleted)} сообщений", ephemeral=True)

@bot.tree.command(name="slowmode", description="Включить задержку")
@app_commands.describe(seconds="Секунды (0 чтобы выключить)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: int):
    await interaction.channel.edit(slowmode_delay=seconds)
    await interaction.response.send_message(f"⏱️ Slowmode: {seconds} сек" if seconds else "🐇 Slowmode выключен")

@bot.tree.command(name="lock", description="Закрыть канал")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.response.send_message("🔒 Канал закрыт")

@bot.tree.command(name="unlock", description="Открыть канал")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
    await interaction.response.send_message("🔓 Канал открыт")


RANK_SECTIONS = (
    ("Активность", (
        ("👋", "Первое слово", "Написать первое сообщение в чате", "word_messages", 1),
        ("💬", "Освоился", "Написать 100 сообщений", "messages", 100),
        ("🪑", "Завсегдатай", "Написать 300 сообщений", "messages", 300),
        ("🏛", "Старожил", "Написать 1000 сообщений", "messages", 1000),
        ("", "", "Написать 10 000 сообщений", "messages", 10000),
        ("", "", "Отправить 10 сообщений длиннее 100 символов", "long_messages", 10),
        ("", "", "Настучать 10 000 символов", "characters", 10000),
        ("", "", "Настучать 100 000 символов", "characters", 100000),
    )),
    ("Сигнал", (
        ("", "", "Впервые открыть SERVER SIGNAL", None, 1),
        ("", "", "Пережить первое изменение STATE", None, 1),
        ("", "", "Оставить первый Echo", None, 1),
        ("", "", "Стать свидетелем редкого скачка SIGNAL", None, 1),
        ("", "", "Стать свидетелем ANOMALY", None, 1),
        ("", "", "Раскрыть скрытую частоту", None, 1),
    )),
    ("Присутствие", (
        ("", "", "Вернуться на сервер в другой день", None, 1),
        ("", "", "Провести на сервере 7 дней", None, 1),
        ("", "", "Провести на сервере 30 дней", None, 1),
        ("", "", "Впервые войти в голосовой канал", None, 1),
        ("", "", "Побывать в голосе вместе с 5 участниками", None, 1),
        ("", "", "Остаться, когда сервер затих", None, 1),
    )),
    ("Музыка", (
        ("", "", "Впервые добавить трек в очередь", None, 1),
        ("", "", "Прослушать 10 треков", None, 1),
        ("", "", "Прослушать 100 треков", None, 1),
        ("", "", "Собрать очередь из 10 треков", None, 1),
        ("", "", "Открыть музыкальный слой VEYR", None, 1),
    )),
    ("Хроника", (
        ("", "", "Оставить первый след в CHRONICLE", None, 1),
        ("", "", "Стать свидетелем события RARE", None, 1),
        ("", "", "Стать свидетелем события LEGENDARY", None, 1),
        ("", "", "Стать свидетелем события MYTHIC", None, 1),
        ("", "", "Найти запись, скрытую от остальных", None, 1),
    )),
    ("Коллектив", (
        ("", "", "Завершить первый RITUAL", None, 1),
        ("", "", "Участвовать в ритуале с 3 участниками", None, 1),
        ("", "", "Завершить 5 ритуалов", None, 1),
        ("", "", "Открыть слой вместе с сервером", None, 1),
        ("", "", "Пережить общее событие ANOMALY", None, 1),
        ("", "", "Оставить след, который заметили другие", None, 1),
    )),
)
RANK_ACHIEVEMENT_TOTAL = sum(len(achievements) for _, achievements in RANK_SECTIONS)


def _rank_page_text(guild_id: int, member: discord.Member, page: int) -> str:
    section_name, achievements = RANK_SECTIONS[page]
    stats = RANK_ACTIVITY.get((guild_id, member.id), {})
    unlocked = [
        metric is not None and stats.get(metric, 0) >= threshold
        for _, _, _, metric, threshold in achievements
    ]
    unlocked_count = sum(unlocked)
    total_unlocked = sum(
        1
        for section_index, (_, section_achievements) in enumerate(RANK_SECTIONS)
        for _, _, _, metric, threshold in section_achievements
        if section_index == 0 and metric is not None and stats.get(metric, 0) >= threshold
    )
    filled = round(total_unlocked / RANK_ACHIEVEMENT_TOTAL * 12)
    lines = [
        f"🏆 **Достижения — {member.mention}**",
        f"{'▰' * filled}{'▱' * (12 - filled)}  {total_unlocked} из {RANK_ACHIEVEMENT_TOTAL}",
        "",
        f"**{section_name}** — {unlocked_count} из {len(achievements)}",
        "",
    ]
    for is_unlocked, (mark, title, description, _, _) in zip(unlocked, achievements):
        if is_unlocked:
            lines.append(f"{mark} **{title}**")
            lines.append(f"    *{description}*")
        else:
            lines.append(f"🔒 *{description}*")
    lines.extend(("", f"*Раздел {page + 1} из {len(RANK_SECTIONS)}*"))
    return "\n".join(lines)


class VeyrRankView(discord.ui.View):
    def __init__(self, guild_id: int, member: discord.Member, owner_id: int):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.member = member
        self.owner_id = owner_id
        self.page = 0
        self.previous_page.disabled = True

    async def _change_page(self, interaction: discord.Interaction, delta: int):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Этот экран открыт другим участником.", ephemeral=True)
        self.page = max(0, min(len(RANK_SECTIONS) - 1, self.page + delta))
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page == len(RANK_SECTIONS) - 1
        try:
            await interaction.response.edit_message(
                content=_rank_page_text(self.guild_id, self.member, self.page),
                view=self,
            )
        except (discord.NotFound, discord.HTTPException):
            return

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, -1)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, 1)


@bot.tree.command(name="rank", description="Показать достижения участника")
@app_commands.describe(member="Чьи достижения показать")
async def rank(interaction: discord.Interaction, member: discord.Member | None = None):
    if interaction.guild is None:
        return await interaction.response.send_message("Достижения доступны только на сервере.", ephemeral=True)
    target = member or interaction.user
    view = VeyrRankView(interaction.guild.id, target, interaction.user.id)
    await interaction.response.send_message(
        content=_rank_page_text(interaction.guild.id, target, view.page),
        view=view,
    )

# ---------------- SOUNDCloud SEARCH / DISCORD VOICE ----------------
def _is_soundcloud_url(query: str) -> bool:
    host = (urlparse(query).hostname or "").lower()
    return host == "soundcloud.com" or host.endswith(".soundcloud.com")


def _entries_from_info(info):
    if not info:
        return []
    entries = info.get("entries")
    if entries is not None:
        return [entry for entry in entries if entry]
    return [info]


def _resolve_soundcloud_url(url: str, fallback_title: str = ""):
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = _entries_from_info(info)
    if not entries:
        return None
    item = entries[0]
    stream_url = item.get("url")
    if not stream_url and item.get("requested_formats"):
        stream_url = item["requested_formats"][0].get("url")
    webpage_url = item.get("webpage_url") or url
    if not stream_url:
        return None
    return {
        "title": item.get("title") or fallback_title or "Untitled SoundCloud track",
        "webpage_url": webpage_url,
        "stream_url": stream_url,
        "duration": item.get("duration"),
    }


def _find_soundcloud_track(query: str):
    if _is_soundcloud_url(query):
        return _resolve_soundcloud_url(query)
    if query.startswith(("http://", "https://")):
        raise ValueError("Only SoundCloud links are accepted.")

    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        result = ydl.extract_info(f"scsearch5:{query}", download=False)
    entries = _entries_from_info(result)
    if not entries:
        return None

    def normalise_title(value):
        return " ".join((value or "").casefold().split())

    exact = [entry for entry in entries if normalise_title(entry.get("title")) == normalise_title(query)]
    chosen = exact[0] if exact else entries[0]
    track_url = chosen.get("webpage_url") or chosen.get("original_url") or chosen.get("url")
    if not track_url:
        return None
    # Search by title first; then resolve the chosen SoundCloud page into a stream URL.
    return _resolve_soundcloud_url(track_url, chosen.get("title", query))


async def _ensure_ffmpeg() -> str:
    global FFMPEG_EXECUTABLE
    if FFMPEG_EXECUTABLE is None:
        loop = asyncio.get_running_loop()
        ffmpeg_path, _ = await loop.run_in_executor(
            None,
            static_ffmpeg_run.get_or_fetch_platform_executables_else_raise,
        )
        FFMPEG_EXECUTABLE = ffmpeg_path
    return FFMPEG_EXECUTABLE


async def _extract_in_executor(function, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, function, *args)


def _music_connect_lock(guild_id: int) -> asyncio.Lock:
    lock = MUSIC_CONNECT_LOCKS.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        MUSIC_CONNECT_LOCKS[guild_id] = lock
    return lock


def _schedule_music_recovery(guild_id: int):
    task = MUSIC_RECOVERY_TASKS.get(guild_id)
    if task is None or task.done():
        MUSIC_RECOVERY_TASKS[guild_id] = asyncio.create_task(_recover_music_after_drop(guild_id))


async def _recover_music_after_drop(guild_id: int):
    """Wait for discord.py voice reconnect; if it fails, make one clean rejoin."""
    current_task = asyncio.current_task()
    try:
        for _ in range(18):
            await asyncio.sleep(5)
            if not MUSIC_QUEUES[guild_id]:
                return
            guild = bot.get_guild(guild_id)
            if guild is None:
                return
            voice = guild.voice_client
            if voice and voice.is_connected():
                if not voice.is_playing() and not voice.is_paused():
                    await _play_next_track(guild_id)
                return

        guild = bot.get_guild(guild_id)
        channel = MUSIC_CHANNELS.get(guild_id)
        if guild is None or channel is None or not MUSIC_QUEUES[guild_id]:
            return

        async with _music_connect_lock(guild_id):
            voice = guild.voice_client
            if voice and voice.is_connected():
                recovered_voice = voice
            else:
                if voice:
                    try:
                        await voice.disconnect(force=True)
                    except discord.DiscordException as error:
                        print(f"[Музыка] Не удалось сбросить voice-сессию {guild_id}: {error!r}")
                if not MUSIC_QUEUES[guild_id]:
                    return
                recovered_voice = await channel.connect(timeout=45, reconnect=True)

        if recovered_voice and recovered_voice.is_connected():
            print(f"[Музыка] Голосовая сессия восстановлена на сервере {guild_id}")
            await _play_next_track(guild_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[Музыка] Не удалось восстановить голосовую сессию {guild_id}: {error!r}")
    finally:
        if MUSIC_RECOVERY_TASKS.get(guild_id) is current_task:
            MUSIC_RECOVERY_TASKS.pop(guild_id, None)


async def _handle_track_end(guild_id: int, error):
    if guild_id in MUSIC_SKIP_PENDING:
        MUSIC_SKIP_PENDING.discard(guild_id)
        MUSIC_NOW_PLAYING.pop(guild_id, None)
        MUSIC_SOURCES.pop(guild_id, None)
        await _play_next_track(guild_id)
        return

    track = MUSIC_NOW_PLAYING.get(guild_id)
    MUSIC_SOURCES.pop(guild_id, None)
    if error:
        print(f"[Музыка] Ошибка воспроизведения на сервере {guild_id}: {error!r}")
        MUSIC_NOW_PLAYING.pop(guild_id, None)
        if track and track.get("_veyr_retry_count", 0) < 1:
            retry_track = dict(track)
            retry_track["_veyr_retry_count"] = retry_track.get("_veyr_retry_count", 0) + 1
            MUSIC_QUEUES[guild_id].appendleft(retry_track)
        if MUSIC_QUEUES[guild_id]:
            _schedule_music_recovery(guild_id)
        else:
            await _play_next_track(guild_id)
        return

    await _play_next_track(guild_id)


async def _play_next_track(guild_id: int):
    if guild_id in MUSIC_STARTING:
        return
    MUSIC_STARTING.add(guild_id)
    retry = False
    try:
        guild = bot.get_guild(guild_id)
        voice = guild.voice_client if guild else None
        if voice is None or not voice.is_connected():
            return

        if loop_status[guild_id] and guild_id in MUSIC_NOW_PLAYING:
            track = MUSIC_NOW_PLAYING[guild_id]
        elif MUSIC_QUEUES[guild_id]:
            track = MUSIC_QUEUES[guild_id].popleft()
        else:
            MUSIC_NOW_PLAYING.pop(guild_id, None)
            MUSIC_SOURCES.pop(guild_id, None)
            asyncio.create_task(_disconnect_when_idle(guild_id, voice))
            return

        # Resolve again when dequeued so queued SoundCloud stream URLs are fresh.
        fresh_track = await _extract_in_executor(_resolve_soundcloud_url, track["webpage_url"], track["title"])
        if not fresh_track:
            raise RuntimeError("SoundCloud did not return a playable audio stream.")
        fresh_track["_veyr_retry_count"] = track.get("_veyr_retry_count", 0)
        ffmpeg_path = await _ensure_ffmpeg()
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                fresh_track["stream_url"],
                executable=ffmpeg_path,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn -loglevel error",
            ),
            volume=MUSIC_VOLUME[guild_id] / 100,
        )
        MUSIC_NOW_PLAYING[guild_id] = fresh_track
        MUSIC_SOURCES[guild_id] = source

        def after_playback(error):
            try:
                asyncio.run_coroutine_threadsafe(_handle_track_end(guild_id, error), bot.loop)
            except RuntimeError:
                pass

        voice.play(source, after=after_playback)
    except Exception as error:
        print(f"[Музыка] Не удалось воспроизвести трек на сервере {guild_id}: {error!r}")
        MUSIC_NOW_PLAYING.pop(guild_id, None)
        retry = True
    finally:
        MUSIC_STARTING.discard(guild_id)
    if retry:
        asyncio.create_task(_play_next_track(guild_id))


async def _disconnect_when_idle(guild_id: int, voice: discord.VoiceClient):
    await asyncio.sleep(120)
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    async with _music_connect_lock(guild_id):
        if (
            guild.voice_client is voice
            and voice.is_connected()
            and not voice.is_playing()
            and not voice.is_paused()
            and not MUSIC_QUEUES[guild_id]
        ):
            try:
                await voice.disconnect()
                MUSIC_CHANNELS.pop(guild_id, None)
            except discord.DiscordException as error:
                print(f"[Музыка] Ошибка отключения на сервере {guild_id}: {error!r}")


@bot.tree.command(name="play", description="Найти трек на SoundCloud и включить его")
@app_commands.describe(query="Название трека или ссылка SoundCloud")
async def play(interaction: discord.Interaction, query: str):
    try:
        await interaction.response.defer(thinking=True)
    except discord.NotFound as error:
        print(f"[Музыка] Interaction /play истёк до defer: {error!r}")
        return
    except discord.HTTPException as error:
        print(f"[Музыка] Не удалось подтвердить /play: {error!r}")
        return

    if interaction.guild is None:
        return await interaction.followup.send("Музыка доступна только на сервере.", ephemeral=True)
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("Сначала зайди в голосовой канал.", ephemeral=True)
    voice_channel = interaction.user.voice.channel

    bot_member = interaction.guild.me
    if bot_member is not None:
        permissions = voice_channel.permissions_for(bot_member)
        missing = [
            name for name, allowed in (("Connect", permissions.connect), ("Speak", permissions.speak))
            if not allowed
        ]
        if missing:
            return await interaction.followup.send(
                f"Нет прав канала: {', '.join(missing)}. Разреши боту подключение и речь в {voice_channel.mention}.",
                ephemeral=True,
            )

    try:
        track = await _extract_in_executor(_find_soundcloud_track, query.strip())
    except ValueError as error:
        return await interaction.followup.send(str(error), ephemeral=True)
    except Exception as error:
        print(f"[SoundCloud] Ошибка поиска {query!r}: {error!r}")
        return await interaction.followup.send("SoundCloud не ответил на поиск. Попробуй позже.", ephemeral=True)
    if not track:
        return await interaction.followup.send("На SoundCloud ничего не найдено.", ephemeral=True)

    guild_id = interaction.guild.id
    voice = interaction.guild.voice_client
    try:
        async with _music_connect_lock(guild_id):
            voice = interaction.guild.voice_client
            if voice is None:
                voice = await voice_channel.connect(timeout=45, reconnect=True)
            elif not voice.is_connected():
                MUSIC_CHANNELS[guild_id] = voice_channel
                MUSIC_QUEUES[guild_id].append(track)
                _schedule_music_recovery(guild_id)
                return await interaction.followup.send(
                    f"Связь с голосовым каналом восстанавливается. Трек поставлен в очередь: **{track['title']}**"
                )
            elif voice.channel != voice_channel:
                if voice.is_playing() or voice.is_paused():
                    return await interaction.followup.send("VEYR уже воспроизводит звук в другом канале.", ephemeral=True)
                await voice.move_to(voice_channel)

            MUSIC_CHANNELS[guild_id] = voice_channel
            was_active = bool(
                voice.is_playing()
                or voice.is_paused()
                or guild_id in MUSIC_STARTING
                or guild_id in MUSIC_RECOVERY_TASKS
            )
            MUSIC_QUEUES[guild_id].append(track)
    except Exception as error:
        print(f"[Музыка] Ошибка подключения на сервере {guild_id}: {error!r}")
        detail = f"{type(error).__name__}: {error}"[:350]
        return await interaction.followup.send(
            f"Не удалось подключиться к голосовому каналу. Причина: `{detail}`",
            ephemeral=True,
        )

    if was_active:
        await interaction.followup.send(f"В очереди: **{track['title']}**")
    else:
        await _play_next_track(guild_id)
        await interaction.followup.send(f"Загружен с SoundCloud: **{track['title']}**")


@bot.tree.command(name="queue", description="Показать очередь SoundCloud")
async def queue_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("Очередь доступна только на сервере.", ephemeral=True)
    guild_id = interaction.guild.id
    lines = []
    current = MUSIC_NOW_PLAYING.get(guild_id)
    if current:
        lines.append(f"СЕЙЧАС\n{current['title']}\n")
    queued = list(MUSIC_QUEUES[guild_id])
    if queued:
        lines.append("ОЧЕРЕДЬ")
        lines.extend(f"{index}. {track['title']}" for index, track in enumerate(queued[:10], 1))
        if len(queued) > 10:
            lines.append(f"…ещё {len(queued) - 10}")
    if not lines:
        return await interaction.response.send_message("Очередь пуста.", ephemeral=True)
    await interaction.response.send_message("\n".join(lines)[:1900])


@bot.tree.command(name="skip", description="Пропустить текущий трек")
async def skip(interaction: discord.Interaction):
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and (voice.is_playing() or voice.is_paused()):
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Трек пропущен.")
            else:
                await interaction.response.send_message("Трек пропущен.")
        except discord.HTTPException as error:
            if getattr(error, "code", None) == 40060:
                try:
                    await interaction.followup.send("Трек пропущен.")
                except discord.HTTPException as followup_error:
                    print(f"[Музыка] Не удалось ответить на /skip: {followup_error!r}")
            else:
                print(f"[Музыка] Не удалось подтвердить /skip: {error!r}")
        MUSIC_SKIP_PENDING.add(interaction.guild.id)
        voice.stop()
    else:
        await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)


@bot.tree.command(name="pause", description="Приостановить воспроизведение")
async def pause(interaction: discord.Interaction):
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and voice.is_playing():
        voice.pause()
        await interaction.response.send_message("Воспроизведение приостановлено.")
    else:
        await interaction.response.send_message("Сейчас нечего приостанавливать.", ephemeral=True)


@bot.tree.command(name="resume", description="Продолжить воспроизведение")
async def resume(interaction: discord.Interaction):
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and voice.is_paused():
        voice.resume()
        await interaction.response.send_message("Воспроизведение продолжено.")
    else:
        await interaction.response.send_message("Нет приостановленного трека.", ephemeral=True)


@bot.tree.command(name="stop", description="Остановить и отключить VEYR")
async def stop(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("VEYR не подключён к голосовому каналу.", ephemeral=True)
    guild_id = interaction.guild.id
    async with _music_connect_lock(guild_id):
        voice = interaction.guild.voice_client
        MUSIC_QUEUES[guild_id].clear()
        MUSIC_NOW_PLAYING.pop(guild_id, None)
        MUSIC_SOURCES.pop(guild_id, None)
        MUSIC_SKIP_PENDING.discard(guild_id)
        MUSIC_CHANNELS.pop(guild_id, None)
        recovery = MUSIC_RECOVERY_TASKS.pop(guild_id, None)
        if recovery and not recovery.done():
            recovery.cancel()
        loop_status[guild_id] = False
        if voice:
            voice.stop()
            await voice.disconnect(force=True)
    if voice:
        await interaction.response.send_message("Воспроизведение остановлено.")
    else:
        await interaction.response.send_message("VEYR не подключён к голосовому каналу.", ephemeral=True)


@bot.tree.command(name="loop", description="Переключить повтор текущего трека")
async def loop_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("Повтор доступен только на сервере.", ephemeral=True)
    guild_id = interaction.guild.id
    loop_status[guild_id] = not loop_status[guild_id]
    state = "включён" if loop_status[guild_id] else "выключен"
    await interaction.response.send_message(f"Повтор {state}.")


@bot.tree.command(name="volume", description="Установить громкость 0–200")
@app_commands.describe(value="0–200")
async def volume(interaction: discord.Interaction, value: int):
    if interaction.guild is None or interaction.guild.voice_client is None:
        return await interaction.response.send_message("VEYR не подключён к голосовому каналу.", ephemeral=True)
    if not 0 <= value <= 200:
        return await interaction.response.send_message("Допустимый диапазон: 0–200.", ephemeral=True)
    guild_id = interaction.guild.id
    MUSIC_VOLUME[guild_id] = value
    source = MUSIC_SOURCES.get(guild_id)
    if source:
        source.volume = value / 100
    await interaction.response.send_message(f"Громкость: {value}%.")

# ---------------- ЗАПУСК ----------------
if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)


