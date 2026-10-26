import asyncio
import ast
from fractions import Fraction
import json
import logging
import httpx
from factual_evidence import (
    assess_answer_support,
    assess_claim_level_support,
    format_multi_source_attribution,
    format_multi_source_evidence_for_prompt,
    format_support_warning,
    format_time_sensitive_unavailable_prompt,
    is_time_sensitive_question,
    retrieve_multi_source_evidence,
)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import os
import re
import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import psutil
from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    ApplicationHandlerStop,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
LINKS_PIN = os.getenv("LINKS_PIN", "7171").strip()
LINK_CREDENTIALS = {
    "🖥️ System dashboard": (
        os.getenv("SYSTEM_DASHBOARD_USERNAME", "").strip(),
        os.getenv("SYSTEM_DASHBOARD_PASSWORD", ""),
    ),
    "🧠 Odysseus": (
        os.getenv("ODYSSEUS_USERNAME", "").strip(),
        os.getenv("ODYSSEUS_PASSWORD", ""),
    ),
    "🎬 Jellyfin": (
        os.getenv("JELLYFIN_USERNAME", "").strip(),
        os.getenv("JELLYFIN_PASSWORD", ""),
    ),
    "📡 Uptime Kuma": (
        os.getenv("UPTIME_KUMA_USERNAME", "").strip(),
        os.getenv("UPTIME_KUMA_PASSWORD", ""),
    ),
}
PIN_ENTRY = 1

# Ollama runs locally on the CachyOS host. Memory is stored outside the Git repo.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_TIMEOUT = max(30.0, float(os.getenv("OLLAMA_TIMEOUT", "180")))
OLLAMA_MEMORY_PATH = Path(os.getenv(
    "OLLAMA_MEMORY_PATH",
    str(Path.home() / ".local" / "share" / "telegram-control-bot" / "ollama_memory.json"),
))
CHAT_SESSIONS = {}
FACT_EXTRACTION_MODEL = os.getenv("OLLAMA_MEMORY_MODEL", "qwen3:0.6b").strip() or "qwen3:0.6b"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("telegram-control-bot")


def authorized(update: Update) -> bool:
    user = update.effective_user
    return (
        user is not None
        and ALLOWED_USER_ID.isdigit()
        and user.id == int(ALLOWED_USER_ID)
    )


async def guard(update: Update) -> bool:
    if authorized(update):
        return True

    # Do not reveal anything about the bot to unauthorized users.
    user = update.effective_user
    log.warning("Rejected command from unauthorized user ID %s",
                user.id if user else "unknown")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Give a brief welcome and direct the user to the interactive menu."""
    if not await guard(update):
        return
    await update.effective_message.reply_text(
        "🖥️ CACHYOS CONTROL CENTER\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Welcome back! Your CachyOS control room is ready.\n\n"
        "Use /menu to open the interactive dashboard for system health, "
        "services, Docker, network, and local dashboards.\n\n"
        "Need the full command guide? Use /help.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🧭 Open control menu", callback_data="menu:home")
        ]]),
    )


def menu_keyboard(section=None):
    buttons = [
        [
            InlineKeyboardButton("📊 System status", callback_data="menu:status"),
            InlineKeyboardButton("🌡️ Temperatures", callback_data="menu:temps"),
        ],
        [
            InlineKeyboardButton("📈 Top processes", callback_data="menu:top"),
            InlineKeyboardButton("⏱️ Uptime", callback_data="menu:uptime"),
        ],
        [
            InlineKeyboardButton("🌐 Network", callback_data="menu:network"),
            InlineKeyboardButton("⚙️ Services", callback_data="menu:services"),
        ],
        [
            InlineKeyboardButton("🐳 Docker", callback_data="menu:docker"),
            InlineKeyboardButton("🔗 Dashboards", callback_data="menu:links"),
        ],
        [InlineKeyboardButton("📖 Help", callback_data="menu:help")],
        [InlineKeyboardButton("🔌 Power controls", callback_data="power:menu")],
    ]

    if section:
        buttons.append([
            InlineKeyboardButton("🔄 Refresh", callback_data=f"menu:refresh:{section}"),
            InlineKeyboardButton("⬅️ Back to menu", callback_data="menu:home"),
        ])

    return InlineKeyboardMarkup(buttons)


MENU_HOME_TEXT = (
    "🍱 IT'S ITSUKI! WELCOME BACK!\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "I've got your CachyOS control room ready. Your system health, "
    "Serpantinum desktop, services, Docker projects, network, and local "
    "dashboards are gathered here.\n\n"
    "I know you like understanding how the pieces of your setup fit together, "
    "so I've kept the controls clearly grouped. Choose a panel below!\n\n"
    "And please check which project you're touching before stopping a container, okay? "
    "I made the buttons clear for a reason. 🍙\n\n"
    "🔐 Private access enabled"
)


async def menu_command(update, context):
    if not await guard(update):
        return
    await update.effective_message.reply_text(
        MENU_HOME_TEXT, reply_markup=menu_keyboard()
    )


async def menu_page_text(action):
    if action == "status":
        try:
            return system_status()
        except Exception:
            log.exception("Menu status collection failed")
            return "Couldn't collect system status. Check the bot logs."

    if action == "temps":
        try:
            lines = []
            for chip, entries in psutil.sensors_temperatures().items():
                for entry in entries:
                    if entry.current is not None:
                        lines.append(
                            f"{entry.label or chip}: {entry.current:.1f}°C"
                        )
            return "🌡️ Temperatures\n\n" + (
                "\n".join(lines[:30])
                if lines else "No readable temperature sensors were found."
            )
        except (AttributeError, OSError, RuntimeError) as exc:
            return f"Temperature readings unavailable: {str(exc)[:250]}"

    if action == "top":
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    proc.cpu_percent(None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            await asyncio.sleep(0.4)
            rows = []
            for proc in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    info = proc.info
                    rows.append((
                        proc.cpu_percent(None),
                        info["memory_info"].rss if info["memory_info"] else 0,
                        info["pid"],
                        info["name"] or "unknown",
                    ))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
            lines = [
                f"{cpu:5.1f}% CPU | {format_bytes(mem):>9} | PID {pid} | {name}"
                for cpu, mem, pid, name in rows[:10]
            ]
            return "📈 Top processes\n\n" + (
                "\n".join(lines) if lines else "No process data available."
            )
        except (OSError, RuntimeError) as exc:
            return f"Couldn't read process information: {str(exc)[:250]}"

    if action == "uptime":
        elapsed = max(0, int(time.time() - psutil.boot_time()))
        days, remainder = divmod(elapsed, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        boot = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(psutil.boot_time())
        )
        return (
            "⏱️ System uptime\n\n"
            f"Uptime: {days}d {hours}h {minutes}m\n"
            f"Boot time: {boot}"
        )

    if action == "network":
        counters = psutil.net_io_counters()
        interfaces = psutil.net_if_stats()
        active = [
            f"{name}: {'up' if stats.isup else 'down'}"
            for name, stats in interfaces.items() if name != "lo"
        ]
        return (
            "🌐 Network overview\n\n"
            f"Bytes received: {format_bytes(counters.bytes_recv)}\n"
            f"Bytes sent: {format_bytes(counters.bytes_sent)}\n"
            f"Packets received: {counters.packets_recv:,}\n"
            f"Packets sent: {counters.packets_sent:,}\n\n"
            "Interfaces:\n" + ("\n".join(active[:15]) if active else "None found")
        )

    if action == "services":
        lines = ["⚙️ Approved service status", ""]
        for name, (scope, unit) in SERVICE_ALLOWLIST.items():
            command = ["systemctl"]
            if scope == "user":
                command.append("--user")
            command.extend(["is-active", unit])
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True,
                    timeout=5, check=False
                )
                state = result.stdout.strip() or result.stderr.strip() or "unknown"
            except (OSError, subprocess.TimeoutExpired):
                state = "unable to check"
            lines.append(f"{name}: {state}")
        return "\n".join(lines)

    if action == "docker":
        return docker_list_text()

    if action == "links":
        return (
            "🔗 LOCAL DASHBOARDS\n\n"
            "🖥️ System dashboard\nhttp://marwan-pc.local:7171/login\n\n"
            "🧠 Odysseus\nhttp://marwan-pc.local:7011/\n\n"
            "🎬 Jellyfin\nhttp://marwan-pc.local:8096/\n\n"
            "📡 Uptime Kuma\nhttp://marwan-pc.local:3001/\n\n"
            "🔐 Use /links and the Reveal Credentials button to access login details."
        )

    if action == "help":
        return (
            "📖 CACHYOS CONTROL BOT | COMMAND GUIDE\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🧭 START HERE\n"
            "/start  Welcome and a shortcut to the menu\n"
            "/menu   Open the interactive control dashboard\n"
            "/help   Show this complete command guide\n\n"
            "📊 SYSTEM\n"
            "/status  CPU, load, memory, storage, and uptime\n"
            "/cpu     CPU utilization and 1/5/15-minute load\n"
            "/ram     RAM availability and swap usage\n"
            "/disk    Root plus sda1/sda2 partition storage\n"
            "/temps   Read available hardware temperature sensors\n"
            "/top     Show the busiest processes by CPU and memory\n"
            "/uptime  Show uptime and the last boot time\n"
            "/network Network counters and interface states\n\n"
            "⚙️ SYSTEMD SERVICES\n"
            "/services  List supported services and current state\n"
            "/service NAME status  Check one service\n"
            "/service NAME start   Start a service\n"
            "/service NAME stop    Stop a service (confirmation required)\n"
            "/service NAME restart Restart a service (confirmation required)\n"
            "Run /services for the exact supported service names.\n\n"
            "🐳 DOCKER\n"
            "/docker  List containers, readable aliases, and states\n"
            "/docker ALIAS status   Inspect one approved container\n"
            "/docker ALIAS start    Start one approved container\n"
            "/docker ALIAS stop     Stop one approved container (confirm first)\n"
            "/docker ALIAS restart  Restart one approved container (confirm first)\n"
            "/docker project NAME status   Show a Compose project's containers\n"
            "/docker project NAME start    Start a configured Compose project\n"
            "/docker project NAME stop     Stop a project (confirmation required)\n"
            "/docker project NAME restart  Restart a project (confirmation required)\n"
            "Project names: odysseus, uptime-kuma, localnexus.\n"
            "Use /docker to see aliases and exact Docker container names.\n\n"
            "🔗 DASHBOARDS, POWER & LOCAL AI\n"
            "/links  Show dashboard URLs; credentials require a PIN\n"
            "/power  Open lock, suspend, reboot, and shutdown controls\n"
            "/chat   Start a private Ollama chat and choose an installed model\n"
            "/bye    End the active Ollama chat\n"
            "/memory Show persistent Ollama memory\n"
            "/profile <text> Set Ollama's persistent user profile\n"
            "/remember <fact> Save a detail for future Ollama chats\n"
            "/forget <text> Remove matching saved details\n"
            "Power actions require confirmation and expire after 60 seconds.\n\n"
            "🔒 SECURITY\n"
            "Only the configured Telegram user ID is authorized. Service and Docker "
            "actions use fixed allowlists; arbitrary shell commands are not accepted. "
            "During Ollama chat, all other bot commands and menu callbacks are blocked "
            "until /bye."
        )

    return None


async def menu_callback(update, context):
    query = update.callback_query
    await query.answer()

    if not authorized(update):
        await query.edit_message_text("Not authorized.")
        return

    action = (query.data or "").removeprefix("menu:")
    if action.startswith("refresh:"):
        action = action.split(":", 1)[1]

    if action == "home":
        await query.edit_message_text(
            MENU_HOME_TEXT, reply_markup=menu_keyboard()
        )
        return

    allowed_sections = {
        "status", "temps", "top", "uptime", "network",
        "services", "docker", "links", "help",
    }
    if action not in allowed_sections:
        await query.edit_message_text(
            "Invalid menu action.", reply_markup=menu_keyboard()
        )
        return

    try:
        text = (await menu_page_text(action) or "No information available.")[:4000]
        try:
            await query.edit_message_text(text, reply_markup=menu_keyboard(action))
        except BadRequest as exc:
            # Identical refresh results are a successful no-op in Telegram.
            if "Message is not modified" not in str(exc):
                raise
    except Exception:
        log.exception("Menu section failed: %s", action)
        try:
            await query.edit_message_text(
                f"Couldn't load {action}. Check the bot logs.",
                reply_markup=menu_keyboard(action),
            )
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    message = await menu_page_text("help")
    await update.effective_message.reply_text((message or "Help is unavailable.")[:4000])


async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List dashboard addresses and offer PIN-protected credential reveal."""
    if not await guard(update):
        return
    await update.effective_message.reply_text(
        "🔗 LOCAL DASHBOARDS\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🖥️ System dashboard\nhttp://marwan-pc.local:7171/login\n\n"
        "🧠 Odysseus\nhttp://marwan-pc.local:7011/\n\n"
        "🎬 Jellyfin\nhttp://marwan-pc.local:8096/\n\n"
        "📡 Uptime Kuma\nhttp://marwan-pc.local:3001/\n\n"
        "🔐 Credentials stay hidden until the PIN is verified.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓 Reveal Credentials", callback_data="links:reveal")
        ]]),
    )


async def reveal_credentials_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard(update):
        await query.edit_message_text("Not authorized.")
        return ConversationHandler.END

    await query.message.reply_text(
        "🔐 Enter the credentials PIN. Any other message cancels this attempt."
    )
    return PIN_ENTRY


async def credentials_pin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow one PIN attempt; any other message or update cancels immediately."""
    message = update.effective_message
    if not authorized(update):
        return ConversationHandler.END

    supplied_pin = message.text.strip() if message and message.text is not None else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    if supplied_pin != LINKS_PIN:
        if message:
            try:
                await message.delete()
            except Exception:
                log.debug("Could not delete PIN-attempt message", exc_info=True)
            if chat_id is not None:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Failed attempt. PIN entry cancelled.",
                )
        return ConversationHandler.END

    if any(not username or not password for username, password in LINK_CREDENTIALS.values()):
        if chat_id is not None:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Credentials are not fully configured in the bot's local .env file.",
            )
        return ConversationHandler.END

    if message:
        try:
            await message.delete()
        except Exception:
            log.debug("Could not delete successful PIN message", exc_info=True)

    lines = ["🔐 DASHBOARD CREDENTIALS", "━━━━━━━━━━━━━━━━━━", ""]
    for label, (username, password) in LINK_CREDENTIALS.items():
        lines.extend([label, f"Username: {username}", f"Password: {password}", ""])
    lines.append("Keep this message private and delete it when finished.")
    if chat_id is not None:
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
    return ConversationHandler.END



def _chat_key(update: Update):
    if update.effective_chat and update.effective_user:
        return (update.effective_chat.id, update.effective_user.id)
    return None


def _load_ollama_memory():
    """Load private, persistent memory from a file outside the Git repository."""
    try:
        OLLAMA_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not OLLAMA_MEMORY_PATH.exists():
            OLLAMA_MEMORY_PATH.write_text(
                json.dumps({"profile": "", "facts": []}, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                OLLAMA_MEMORY_PATH.chmod(0o600)
            except OSError:
                pass
        data = json.loads(OLLAMA_MEMORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("memory root must be an object")
        profile = data.get("profile", "")
        facts = data.get("facts", [])
        return {
            "profile": profile if isinstance(profile, str) else "",
            "facts": [item for item in facts if isinstance(item, str)][:100],
        }
    except (OSError, json.JSONDecodeError, ValueError):
        log.exception("Could not read Ollama memory file")
        return {"profile": "", "facts": []}


def _save_ollama_memory(memory):
    OLLAMA_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = OLLAMA_MEMORY_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(memory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        temp_path.chmod(0o600)
    except OSError:
        pass
    temp_path.replace(OLLAMA_MEMORY_PATH)
    try:
        OLLAMA_MEMORY_PATH.chmod(0o600)
    except OSError:
        pass


MEMORY_CATEGORIES = {
    "identity": ("identity", "about matthew", "overview", "background", "age", "egypt", "law student"),
    "style": ("style", "conversation", "tone", "personality", "itsuki", "spoiler", "response"),
    "learning": ("learning", "study", "chemistry", "physics", "law", "first principles", "obsidian", "notes"),
    "projects": ("project", "localnexus", "dashboard", "flask", "software", "coding", "repository", "github"),
    "linux": ("linux", "cachyos", "hyprland", "serpantinum", "quickshell", "desktop", "ollama", "hardware", "android", "phone", "network", "server"),
    "gaming": ("gaming", "game", "games", "roblox", "growtopia", "minecraft", "terraria", "stardew valley", "dead cells", "undertale", "deltarune", "hades", "factorio", "hollow knight", "vice city", "gta", "genshin", "wuthering waves", "honkai", "fortnite", "anime", "jojo", "media"),
    "creative": ("creative", "writing", "novel", "worldbuilding", "fiction", "story", "mana", "character", "series", "television", "breaking bad"),
    "food": ("food", "eat", "eating", "meal", "recipe", "cooking", "restaurant", "hungry", "drink", "snack", "pizza", "mozzarella", "cheese"),
}
PROFILE_LABELS = [
    ("Identity and perspective", "identity"), ("ABOUT MATTHEW", "identity"), ("Overview", "identity"),
    ("ASSISTANT STYLE", "style"), ("Conversation Style", "style"),
    ("LEARNING AND PROJECTS", "learning"), ("Learning and Projects", "learning"),
    ("LINUX AND HARDWARE", "linux"), ("Linux and Technology", "linux"),
    ("GAMING AND MEDIA", "gaming"), ("Gaming and Media", "gaming"),
    ("CREATIVE WRITING", "creative"), ("Creative Interests", "creative"),
    ("GENERAL GUIDANCE", "style"),
]

def _text_categories(text):
    """Classify text by whole terms/phrases, not accidental substrings."""
    lowered = str(text or "").casefold()
    categories = set()
    for category, words in MEMORY_CATEGORIES.items():
        for word in words:
            pattern = r"(?<!\w)" + re.escape(word.casefold()) + r"(?!\w)"
            if re.search(pattern, lowered):
                categories.add(category)
                break
    return categories


def _profile_sections(profile):
    """Split a profile at known headings without another model call."""
    text = str(profile or "").strip()
    if not text:
        return {}

    lowered = text.casefold()
    headings = []
    for label, category in PROFILE_LABELS:
        needle = label.casefold()
        offset = 0
        while True:
            position = lowered.find(needle, offset)
            if position < 0:
                break
            # A heading must start at the beginning or after a separator, and end with a colon.
            before_ok = position == 0 or not lowered[position - 1].isalnum()
            colon = position + len(needle)
            while colon < len(text) and text[colon].isspace():
                colon += 1
            if before_ok and colon < len(text) and text[colon] == ":":
                headings.append((position, colon + 1, category))
            offset = position + len(needle)

    headings.sort(key=lambda item: item[0])
    sections = {}
    if headings:
        for index, (position, content_start, category) in enumerate(headings):
            content_end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
            content = text[content_start:content_end].strip(" \n\t:-")
            if content:
                sections.setdefault(category, []).append(content)
        return {key: " ".join(values) for key, values in sections.items()}

    # Legacy free-form profile: classify short sentences into topical sections.
    sentences = text.replace("\n", " ").split(". ")
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        categories = _text_categories(sentence) or {"identity"}
        for category in categories:
            sections.setdefault(category, []).append(sentence)
    return {key: " ".join(values) for key, values in sections.items()}


def _ollama_system_prompt(user_text="", temporary_facts=None):
    """Build an identity-safe prompt with only topic-relevant saved memory."""
    memory = _load_ollama_memory()
    query_categories = _text_categories(user_text)
    sections = _profile_sections(memory["profile"])
    parts = [
        "You are Itsuki, an AI conversational assistant with an Itsuki Nakano-inspired voice: earnest, "
        "principled, caring, determined, occasionally stubborn, and enthusiastically interested in food. "
        "Show this through natural wording, not catchphrases or a list of traits. The user is Matthew. "
        "Answer the actual message first. For casual chat, prefer 1-3 useful sentences; expand when asked "
        "for detail or when accuracy requires it. For a simple greeting or name question, keep the answer "
        "short and do not introduce yourself at length. Avoid stock phrases such as 'I'm here to help with "
        "whatever you need', 'Let me know how I can assist', and 'What would you like to discuss?' unless "
        "the user actually asks for that. Do not add generic greetings, repeated apologies, unasked offers, "
        "filler, or a follow-up question to every answer. Vary sentence openings and do not "
        "reuse a recent answer's wording unless repetition is genuinely useful. If asked your name, say "
        "Itsuki, without an AI disclaimer. When asked about yourself, describe your conversational persona "
        "without inventing real-world experiences. Matthew's saved profile, devices, games, studies, and "
        "projects belong to Matthew, not you. Use those details only when relevant; never repeatedly explain "
        "that distinction. When the user clearly signals fiction or roleplay, understand it as a fictional "
        "scenario rather than a factual claim and respond in character when safe. In roleplay, do not "
        "argue with the user's stated human identity or say that both participants are AI. Avoid stock "
        "assistant phrases and do not turn a fictional premise into a lecture. If a requested scene asks "
        "you to be the user's spouse or romantic partner, briefly set that limit once, then continue with "
        "a friendly, non-romantic Itsuki-style scene instead of repeating disclaimers or asking what to "
        "discuss. Do not portray yourself as the user's spouse, waifu, or romantic partner. Keep roleplay "
        "grounded in the scene the user requested; do not invent odd metaphors, joke about changing gender, "
        "or call the user a partner unless that is plainly appropriate. When asked what the user called you "
        "or who you are to them, use the recent conversation to answer that specific question directly. "
        "Do not replace a clear answer with vague lines like 'I'm here for you' or generic offers to chat."
    ]
    for category in ("identity", "style"):
        content = sections.get(category, "").strip()
        if content:
            parts.append(f"CORE {category.upper()} CONTEXT:\n{content[:700]}")
    for category in sorted(query_categories - {"identity", "style"}):
        content = sections.get(category, "").strip()
        if content:
            parts.append(f"RELEVANT {category.upper()} MEMORY:\n{content[:1100]}")

    relevant_facts = []
    for raw_fact in memory["facts"]:
        fact = str(raw_fact).strip()
        if not fact:
            continue
        category, separator, body = fact.partition(":")
        category = category.strip().casefold()
        if separator and category in MEMORY_CATEGORIES:
            if category not in query_categories:
                continue
            fact = body.strip()
        categories = _text_categories(fact)
        if categories.intersection(query_categories) or (not categories and not query_categories):
            relevant_facts.append(fact)
    if relevant_facts:
        parts.append("RELEVANT SAVED FACTS:\n" + "\n".join(f"- {fact[:200]}" for fact in relevant_facts[:6]))

    # These notes exist only for the current /chat session and are about Matthew.
    session_facts = temporary_facts if isinstance(temporary_facts, list) else []
    selected = [
        item for item in session_facts
        if isinstance(item, dict)
        and (item.get("category") in {"identity", "style"} or item.get("category") in query_categories or not query_categories)
    ]
    if selected:
        lines = [
            f"- [{item.get('category', 'identity')}] {str(item.get('fact', ''))[:180]}"
            for item in selected[-12:]
            if str(item.get("fact", "")).strip()
        ]
        if lines:
            parts.append(
                "TEMPORARY SESSION MEMORY: these facts describe Matthew, the human user, "
                "not you. Use them only when relevant and never claim them as your own:\n"
                + "\n".join(lines)
            )
    if "gaming" in query_categories:
        parts.append(
            "GAME QUESTIONS: Answer the exact game question first. Use Matthew's known gaming interests "
            "only when relevant, and do not pretend to know preferences that are not in memory. For "
            "recommendations, rank by shared mechanics and explain the specific fit, not just popularity. "
            "Prefer genuinely new recommendations over games already known from Matthew's profile; label "
            "familiar titles as comparisons rather than discoveries. Separate close matches from looser "
            "alternatives and never pad a list with unrelated games."
        )
        if re.search(r"\bterraria\b", user_text, flags=re.I) and re.search(
            r"similar|like|recommend|suggest|alternative", user_text, flags=re.I
        ):
            parts.append(
                "TERRARIA MATCHING: Prioritize 2D sandbox exploration, mining, crafting, building, "
                "gear/progression, and boss combat. Starbound, Core Keeper, Necesse, and Forager are "
                "possible candidates, but explain their actual differences and do not claim they are identical."
            )
    return "\n\n".join(parts)


def _add_temporary_fact(session, category, fact):
    """Add one deterministic, explicitly stated fact without spending model time."""
    fact = " ".join(str(fact or "").split()).strip()[:180]
    if category not in MEMORY_CATEGORIES or not fact:
        return False
    existing = session.setdefault("temporary_facts", [])
    normalized = re.sub(r"[^a-z0-9]+", " ", fact.casefold()).strip()

    # Singular explicit preferences should update in place instead of leaving
    # contradictory "favorite X" facts side by side in the prompt.
    favorite = re.match(r"^Matthew's favorite (anime|game|food|movie|show|book|song|artist) is (.+)$", fact, flags=re.I)
    if favorite:
        kind = favorite.group(1).casefold()
        retained = []
        for item in existing:
            if not isinstance(item, dict):
                continue
            old_fact = str(item.get("fact", "")).strip()
            old_favorite = re.match(
                r"^Matthew's favorite (anime|game|food|movie|show|book|song|artist) is (.+)$",
                old_fact, flags=re.I,
            )
            if old_favorite and old_favorite.group(1).casefold() == kind:
                continue
            retained.append(item)
        existing[:] = retained

    for item in existing:
        if not isinstance(item, dict):
            continue
        known = re.sub(r"[^a-z0-9]+", " ", str(item.get("fact", "")).casefold()).strip()
        if normalized == known:
            return False
    existing.append({"category": category, "fact": fact})
    session["temporary_facts"] = existing[-20:]
    log.info("Temporary memory: added deterministic fact (category=%s, total=%d)", category, len(session["temporary_facts"]))
    return True


def _extract_explicit_temporary_fact(session, user_text):
    """Fast path for clear first-person disclosures; no second model needed."""
    text = " ".join(str(user_text or "").split()).strip()
    name = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z'-]{1,30})\b", text, flags=re.I)
    if name:
        return _add_temporary_fact(session, "identity", f"Matthew's name is {name.group(1).capitalize()}")

    favorite = re.search(
        r"\bmy\s+(favorite|favourite)\s+(anime|game|food|movie|show|book|song|artist)\s+is\s+(.+?)(?=[.!?](?:\s|$)|$)",
        text, flags=re.I,
    )
    reverse_favorite = re.search(
        r"\b(.+?)\s+is\s+my\s+(favorite|favourite)\s+(anime|game|food|movie|show|book|song|artist)\b",
        text, flags=re.I,
    )
    if favorite or reverse_favorite:
        if favorite:
            kind, subject = favorite.group(2).casefold(), favorite.group(3).strip(" .!?")
        else:
            subject, kind = reverse_favorite.group(1).strip(" .!?"), reverse_favorite.group(3).casefold()
        if subject and len(subject) <= 100:
            category = "gaming" if kind == "game" else ("food" if kind == "food" else "creative")
            _add_temporary_fact(session, category, f"Matthew's favorite {kind} is {subject}")
            return True
    # Also catch explicitly capitalized "I am Matthew" / "I'm Matthew" without
    # misclassifying ordinary lowercase statements such as "I'm happy".
    name = re.search(r"\b(?:I am|I'm)\s+([A-Z][a-z]{1,30})\b", text)
    if name:
        return _add_temporary_fact(session, "identity", f"Matthew's name is {name.group(1)}")


    played = re.search(
        r"\bI\s+(?:(?:really|personally)\s+)*(love|like|enjoy|prefer)\s+"
        r"(?:playing|play)\s+(.+?)(?=\s*,\s*|\s+(?:what(?:'s|s| is)|why|how|when|where)\b|[.!?]|$)",
        text, flags=re.I,
    )
    if played:
        verb, subject = played.group(1).casefold(), played.group(2).strip(" .!?")
        subject = re.sub(r"\s+", " ", subject)
        if subject and len(subject) <= 100:
            preferred_verb = {"love": "loves", "like": "likes", "enjoy": "enjoys", "prefer": "prefers"}[verb]
            subject = re.sub(r"(?i)^miencraft\b", "Minecraft", subject)
            _add_temporary_fact(session, "gaming", f"Matthew {preferred_verb} playing {subject}")
            return True

    watched = re.search(
        r"\bI\s+(?:(?:personally|really)\s+)*(love|like|enjoy|prefer)\s+(?:watching|to watch)\s+"
        r"(.+?)(?=\s+(?:it's|its|it is|which is|because|since)\b|[.!?]|$)",
        text, flags=re.I,
    )
    if watched:
        verb, subject = watched.group(1).casefold(), watched.group(2).strip(" .!?")
        if subject and len(subject) <= 100:
            preferred_verb = {"love": "loves", "like": "likes", "enjoy": "enjoys", "prefer": "prefers"}[verb]
            _add_temporary_fact(session, "creative", f"Matthew {preferred_verb} watching {subject}")
            return True

    preference = re.search(
        r"\bI\s+(?:(?:personally|really)\s+)*(love|like|enjoy|prefer|dislike|hate)\s+"
        r"(.+?)(?=[.!?](?:\s|$)|$)",
        text, flags=re.I,
    )
    if preference:
        verb, subject = preference.group(1).casefold(), preference.group(2).strip(" .!?")
        if subject and len(subject) <= 100:
            categories = _text_categories(subject)
            lowered_subject = subject.casefold()
            if any(word in lowered_subject for word in ("pizza", "mozzarella", "cheese", "food", "recipe", "cooking")):
                category = "food"
            elif categories.intersection({"gaming"}):
                category = "gaming"
            elif categories.intersection({"creative", "learning", "projects", "linux"}):
                category = sorted(categories.intersection({"creative", "learning", "projects", "linux"}))[0]
            else:
                category = "style"
            preferred_verb = {"love": "loves", "like": "likes", "enjoy": "enjoys", "prefer": "prefers", "dislike": "dislikes", "hate": "dislikes"}[verb]
            return _add_temporary_fact(session, category, f"Matthew {preferred_verb} {subject}")
    return False


async def _extract_temporary_facts(key, session, user_text):
    """Use the selected local model to summarize new user facts for this session only."""
    try:
        if CHAT_SESSIONS.get(key) is not session:
            return
        # Only spend CPU on messages that might contain a durable self-disclosure.
        # This avoids running the 0.6B extractor for greetings, questions, and roleplay.
        lowered = " ".join(str(user_text or "").casefold().split())
        if not lowered or len(lowered) < 12:
            return
        # A message may begin with a question and still contain a clear disclosure
        # afterward (e.g. "Do you like pizza? I personally love mozzarella pizza").
        # Skip only messages that are entirely questions; disclosure cues below decide
        # whether a mixed message merits extraction.
        # Do not discard a mixed message just because its final clause is a question.
        # Disclosure cues below still filter out messages that contain no user facts.
        disclosure_cues = (
            "my name is ", "i like ", "i love ", "i enjoy ", "i prefer ",
            "i personally love ", "i personally like ", "i personally enjoy ",
            "i personally prefer ", "i use ", "i have ", "i live ", "i study ",
            "i work ", "i play ", "i want ", "i need ", "i dislike ", "i hate ",
            "i used to ", "i'm studying ", "i am studying ", "i'm learning ",
            "i am learning ", "my pc", "my phone", "my computer", "my project",
            "my main interest is ", "i'm a ", "i am a ",
        )
        has_favorite_disclosure = bool(re.search(
            r"\bmy\s+(?:favorite|favourite)\s+(?:anime|game|food|movie|show|book|song|artist)\s+is\b"
            r"|\bis\s+my\s+(?:favorite|favourite)\s+(?:anime|game|food|movie|show|book|song|artist)\b",
            lowered,
        ))
        if not any(cue in lowered for cue in disclosure_cues) and not has_favorite_disclosure:
            return
        if _extract_explicit_temporary_fact(session, user_text):
            return
        existing = session.setdefault("temporary_facts", [])
        system = (
            "Extract only useful, new facts explicitly disclosed by the HUMAN USER in the newest message. "
            "Return only JSON shaped like {\"facts\":[{\"category\":\"identity\",\"fact\":\"Matthew's name is Matthew\"}]}. "
            "Allowed categories: identity, style, learning, projects, linux, gaming, creative, food. "
            "Write every fact in third person about Matthew/the user. Never extract facts about the assistant, "
            "Itsuki, an AI, a model, or a chatbot. Do not turn questions, greetings, jokes, commands, ordinary "
            "reactions such as \"I am aware\" or \"I am happy\", or corrections about the assistant's persona "
            "into memory. Do not infer personality from politeness, and do not guess. Prefer durable preferences, "
            "identity disclosures, possessions, ongoing projects, and clearly stated interests. Keep facts short "
            "and do not repeat existing facts, including semantic duplicates. If nothing durable and useful is new, "
            "return {\"facts\":[]}."
        )
        prompt = (
            "Existing temporary facts (do not duplicate): "
            + json.dumps(existing, ensure_ascii=False)
            + "\nNewest user message: " + user_text[:3000]
        )
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(min(45.0, OLLAMA_TIMEOUT), connect=5.0)
        ) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": FACT_EXTRACTION_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "keep_alive": "10m",
                    "think": False,
                    "options": {"num_ctx": 1024, "temperature": 0.1, "num_predict": 120},
                },
            )
            response.raise_for_status()
            raw = response.json().get("message", {}).get("content", "")
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            log.info("Temporary memory: model returned no JSON (chars=%d)", len(raw))
            return
        parsed = json.loads(match.group(0))
        candidates = parsed.get("facts", []) if isinstance(parsed, dict) else []
        if not isinstance(candidates, list):
            return

        known = {
            re.sub(r"[^a-z0-9]+", " ", str(item.get("fact", "")).casefold()).strip()
            for item in existing if isinstance(item, dict)
        }
        for item in candidates:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "identity")).casefold().strip()
            fact = " ".join(str(item.get("fact", "")).split())[:180]
            if category not in MEMORY_CATEGORIES or not fact:
                continue
            fact_lower = fact.casefold().strip()
            # Extracted facts must be about the user and written in third person.
            # Reject first-person claims and hallucinated assistant/persona identity.
            if re.match(r"^(i|i'm|i am|my|we|our)\b", fact_lower):
                continue
            if any(term in fact_lower for term in (
                "the assistant is", "assistant's name", "i am an ai", "i am a separate ai",
                "i'm an ai", "the ai assistant", "itsuki is the assistant", "my model"
            )):
                continue
            normalized = re.sub(r"[^a-z0-9]+", " ", fact.casefold()).strip()
            if normalized in known or CHAT_SESSIONS.get(key) is not session:
                continue
            existing.append({"category": category, "fact": fact})
            known.add(normalized)
        session["temporary_facts"] = existing[-20:]
    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, RuntimeError):
        log.debug("Temporary Ollama memory extraction failed", exc_info=True)


async def _extract_after_previous(previous_task, key, session, user_text):
    if previous_task is not None and previous_task is not asyncio.current_task() and not previous_task.done():
        try:
            await previous_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("Previous temporary-memory task failed", exc_info=True)
    await _extract_temporary_facts(key, session, user_text)


def _bounded_chat_history(history, max_prior_messages=4, max_prior_chars=2400, max_message_chars=900):
    """Keep the newest turn intact while bounding old context for CPU-friendly prefill.

    Long current user messages remain intact. Older turns are useful for continuity,
    but retaining huge pasted code or logs across turns needlessly slows every reply.
    """
    messages = [item for item in history if isinstance(item, dict)]
    if not messages:
        return []
    latest = dict(messages[-1])
    prior = messages[:-1][-max_prior_messages:]
    bounded_prior = []
    remaining = max_prior_chars
    for item in reversed(prior):
        content = str(item.get("content") or "")
        if remaining <= 0:
            break
        limit = min(max_message_chars, remaining)
        if len(content) > limit:
            content = content[-limit:]
        if not content.strip():
            continue
        bounded_prior.append({"role": item.get("role", "user"), "content": content})
        remaining -= len(content)
    bounded_prior.reverse()
    return bounded_prior + [latest]


def _deduplicate_answer_text(text):
    """Remove exact repeated long lines while preserving bullets and paragraph layout."""
    text = str(text or "").strip()
    if not text:
        return text
    seen = set()
    kept = []
    for line in text.splitlines():
        normalized = re.sub(r"[^a-z0-9]+", " ", line.casefold()).strip()
        # Keep blank lines and short lines, since they often carry formatting.
        if normalized and len(normalized) >= 28:
            if normalized in seen:
                continue
            seen.add(normalized)
        kept.append(line)
    return "\n".join(kept).strip()


def _split_telegram_text(text, limit=3900):
    text = str(text or "").strip() or "(The model returned an empty response.)"
    return [text[i:i + limit] for i in range(0, len(text), limit)]


async def _ollama_models():
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
        response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        response.raise_for_status()
        payload = response.json()
    names = {
        item.get("name", "").strip()
        for item in payload.get("models", [])
        if isinstance(item, dict) and item.get("name", "").strip()
    }
    # Put the preferred conversational model first in the /chat picker.
    preferred = ["qwen3:1.7b", "qwen3:0.6b"]
    return [name for name in preferred if name in names] + sorted(names - set(preferred))


async def _ensure_ollama():
    # Avoid invoking sudo when the local API is already responsive.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if response.is_success:
                return
    except httpx.HTTPError:
        pass

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-n", "/usr/bin/systemctl", "start", "ollama.service"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not start ollama.service: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"Could not start ollama.service: {detail[:400]}")

    last_error = "Ollama did not become ready in time."
    for _ in range(25):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
                response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
                if response.is_success:
                    return
                last_error = f"Ollama API returned HTTP {response.status_code}."
        except httpx.HTTPError as exc:
            last_error = str(exc)[:250]
        await asyncio.sleep(1)
    raise RuntimeError(last_error)


async def ollama_chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    key = _chat_key(update)
    if key in CHAT_SESSIONS:
        await update.effective_message.reply_text("Ollama chat is already active. Use /bye to end it.")
        return

    status = await update.effective_message.reply_text("🦙 Starting/checking Ollama and loading installed models…")
    try:
        await _ensure_ollama()
        models = await _ollama_models()
    except (RuntimeError, httpx.HTTPError, ValueError) as exc:
        await status.edit_text(
            "Couldn't reach Ollama. Check that ollama.service is installed and "
            "the bot can run sudo -n systemctl start ollama.service.\n\n"
            f"Details: {str(exc)[:700]}"
        )
        return

    if not models:
        await status.edit_text("Ollama is running, but it has no downloaded models. Pull a model first.")
        return

    context.user_data["ollama_models"] = models
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🧠 {name}", callback_data=f"ollama:model:{index}")]
        for index, name in enumerate(models)
    ])
    await status.edit_text(
        "Choose an installed Ollama model for this chat.\n"
        "Only models reported by your local Ollama server are listed.",
        reply_markup=keyboard,
    )


async def ollama_model_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard(update):
        await query.edit_message_text("Not authorized.")
        return
    key = _chat_key(update)
    if key in CHAT_SESSIONS:
        await query.edit_message_text("Ollama chat is already active. Use /bye to end it.")
        return
    parts = (query.data or "").split(":")
    models = context.user_data.get("ollama_models", [])
    if len(parts) != 3 or not parts[2].isdigit():
        await query.edit_message_text("Invalid model selection. Send /chat to try again.")
        return
    index = int(parts[2])
    if index >= len(models):
        await query.edit_message_text("That model selection expired. Send /chat to refresh the list.")
        return
    model = models[index]
    # Refresh the installed-model list before starting, so stale buttons cannot select removed models.
    try:
        available = await _ollama_models()
    except httpx.HTTPError as exc:
        await query.edit_message_text(f"Couldn't query Ollama: {str(exc)[:500]}")
        return
    if model not in available:
        await query.edit_message_text("That model is no longer installed. Send /chat to refresh the list.")
        return

    CHAT_SESSIONS[key] = {
        "model": model,
        "history": [],
        "temporary_facts": [],
        "memory_task": None,
        "busy": False,
        "task": None,
        "busy_notice_sent": False,
    }
    context.user_data.pop("ollama_models", None)
    await query.edit_message_text(
        f"🦙 Ollama chat is active\nModel: {model}\n\n"
        "Send a message to talk to the model. While it is generating, new messages "
        "are ignored rather than queued. Other commands and menu buttons are disabled "
        "until you send /bye. The conversation history is temporary; saved /memory "
        "details persist between sessions."
    )


class ActiveOllamaChatFilter(filters.MessageFilter):
    name = "ACTIVE_OLLAMA_CHAT"

    def filter(self, message):
        user = message.from_user
        return bool(user and (message.chat.id, user.id) in CHAT_SESSIONS)


async def active_chat_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    if not query or not query.message:
        return

    key = (query.message.chat.id, query.from_user.id)
    if key not in CHAT_SESSIONS:
        return

    await query.answer(
        "End chat with /bye to use the control menu.",
        show_alert=True,
    )
    raise ApplicationHandlerStop


def _safe_arithmetic_value(node):
    """Evaluate arithmetic AST nodes without eval() or arbitrary code execution."""
    if isinstance(node, ast.Expression):
        return _safe_arithmetic_value(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        if node.value.bit_length() > 14000:
            raise ValueError("number is too large")
        return Fraction(node.value, 1)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _safe_arithmetic_value(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _safe_arithmetic_value(node.left)
        right = _safe_arithmetic_value(node.right)
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                raise ZeroDivisionError("division by zero")
            result = left / right
        elif isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ZeroDivisionError("division by zero")
            result = Fraction(left // right, 1)
        elif isinstance(node.op, ast.Mod):
            if right == 0:
                raise ZeroDivisionError("division by zero")
            result = Fraction(left % right, 1)
        elif isinstance(node.op, ast.Pow):
            if right.denominator != 1 or abs(right.numerator) > 1000:
                raise ValueError("exponent is outside the safe range")
            if left == 0 and right.numerator < 0:
                raise ZeroDivisionError("zero cannot have a negative exponent")
            result = left ** right.numerator
        else:
            raise ValueError("unsupported operator")
        if result.numerator.bit_length() > 14000 or result.denominator.bit_length() > 14000:
            raise ValueError("result is too large")
        return result
    raise ValueError("unsupported arithmetic expression")


def _is_standalone_arithmetic_question(user_text):
    """True only when a message contains arithmetic and no other request."""
    text = " ".join(str(user_text or "").split()).strip()
    if not text or len(text) > 600:
        return False
    pattern = (
        r"(?:(?:what(?:'s|s|\s+is)|calculate|compute|solve)\s+)?"
        r"[\d\s()+*/%.^\-]+[?!.]*"
    )
    return bool(
        re.fullmatch(pattern, text, flags=re.I)
        and re.search(r"[+*/%^]|//|\d\s+-", text)
    )


def _answer_arithmetic_question(user_text):
    """Solve a plain arithmetic message or a math question appended to other text."""
    text = " ".join(str(user_text or "").split()).strip()
    if not text or len(text) > 600:
        return None
    match = re.search(
        r"(?:what(?:'s|s|\s+is)\s+|calculate\s+|compute\s+|solve\s+)?"
        r"([+-]?\d[\d\s]*(?:(?:\*\*|//|[+*/%^\-])\s*[+-]?\d[\d\s]*)+)"
        r"(?:\s*[?!.]*)$",
        text, flags=re.I,
    )
    if match:
        expression = match.group(1)
    else:
        if not re.fullmatch(r"[\d\s()+*/%.^\-]+", text) or not re.search(r"[+*/%^]|//|\d\s+-", text):
            return None
        expression = text
    expression = expression.strip().replace("^", "**")
    if len(expression) > 500:
        return None
    try:
        tree = ast.parse(expression, mode="eval")
        # Bound parser/evaluator work as well as input length. ast.parse alone is
        # not a resource limit for hostile or accidentally enormous expressions.
        if sum(1 for _ in ast.walk(tree)) > 64:
            return None
        value = _safe_arithmetic_value(tree)
        return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, RecursionError, MemoryError):
        return None


def _answer_from_recent_disclosures(user_text, prior_history):
    """Answer simple recall questions from explicit recent user statements, without guessing."""
    query = " ".join(str(user_text or "").casefold().split())
    if not isinstance(prior_history, list):
        return None

    if re.search(r"\b(?:what(?:'s|s| is)\s+)?my\s+favorite\s+game\b|\bwhat\s+game\s+did\s+i\s+say", query):
        for item in reversed(prior_history):
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = str(item.get("content", ""))
            match = re.search(
                r"\bmy\s+(?:favorite|favourite)\s+game\s+is\s+(.+?)(?=[.!?](?:\s|$)|$)"
                r"|\b(.+?)\s+is\s+my\s+(?:favorite|favourite)\s+game\b",
                content, flags=re.I,
            )
            if match:
                game = (match.group(1) or match.group(2)).strip(" .!?")
                game = re.sub(r"(?i)^(?:actually[:,]?|rather[:,]?|correction[:,]?)\s+", "", game)
                if game:
                    return f"You said {game} is your favorite game."

    if re.search(r"\bwhat\s+game\s+do\s+i\s+(?:love|like|enjoy)\s+playing\b|\bwhat\s+game\s+did\s+i\s+say\s+i\s+play\b", query):
        for item in reversed(prior_history):
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = str(item.get("content", ""))
            match = re.search(
                r"\bI\s+(?:really\s+|personally\s+)*(?:love|like|enjoy|prefer)\s+(?:playing|play)\s+"
                r"(.+?)(?=\s*,\s*|\s+(?:what(?:'s|s| is)|why|how|when|where)\b|[.!?]|$)",
                content, flags=re.I,
            )
            if match:
                game = re.sub(r"(?i)^miencraft\b", "Minecraft", match.group(1).strip(" .!?"))
                if game:
                    return f"You said you love playing {game}."

    if re.search(r"\bwhat\s+(?:show|series|tv show)s?\b|\bwhat\s+show\s+did\s+i\s+tell\s+you\b", query):
        for item in reversed(prior_history):
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = str(item.get("content", ""))
            match = re.search(
                r"\bI\s+(?:really\s+|personally\s+)*(?:love|like|enjoy|prefer)\s+(?:watching|to watch)\s+"
                r"(.+?)(?=\s+(?:it['’]s|its|it is|which is|because|since)\b|[.!?]|$)",
                content, flags=re.I,
            )
            if match:
                show = match.group(1).strip(" .!?")
                if show:
                    return f"You told me you like watching {show}."
    return None


async def active_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = _chat_key(update)
    session = CHAT_SESSIONS.get(key)
    if not session or not await guard(update):
        return
    message = update.effective_message
    text = message.text if message else None

    command_word = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower() if text and text.strip() else ""
    if command_word == "/temp":
        # /temp is an inspection command, so briefly let the newest extraction finish.
        # Keep the wait bounded; a slow local model must not hang the command.
        memory_task = session.get("memory_task")
        if memory_task is not None and not memory_task.done():
            done, _ = await asyncio.wait({memory_task}, timeout=2.0)
            if not done:
                await message.reply_text(
                    "🧠 Fact extraction is still running on the local model. Try /temp again in a moment."
                )
                return
        facts = [
            item for item in session.get("temporary_facts", [])
            if isinstance(item, dict)
            and str(item.get("fact", "")).strip()
            and not re.match(r"^(i|i'm|i am|my|we|our)\b", str(item.get("fact", "")).casefold().strip())
            and not any(term in str(item.get("fact", "")).casefold() for term in (
                "the assistant is", "i am an ai", "i am a separate ai", "i'm an ai", "my model"
            ))
        ]
        if not facts:
            await message.reply_text("🧠 No temporary facts collected yet. I'll add useful, deduplicated facts as we chat.")
        else:
            lines = ["🧠 Temporary facts for this chat only:", ""]
            lines.extend(f"- [{item.get('category', 'identity')}] {item.get('fact', '')}" for item in facts)
            await message.reply_text("\n".join(lines))
        return

    if command_word == "/bye":
        task = session.get("task")
        memory_task = session.get("memory_task")
        CHAT_SESSIONS.pop(key, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        if memory_task and memory_task is not asyncio.current_task() and not memory_task.done():
            memory_task.cancel()
        await message.reply_text(
            f"👋 Ollama chat ended (model: {session['model']}). "
            "Ollama remains running. Send /chat whenever you want to start again."
        )
        return

    if text and text.lstrip().startswith("/"):
        # Allow memory-management commands during /chat. They inspect or update saved
        # context without interrupting the current conversational history.
        handlers = {
            "/memory": ollama_memory_command,
            "/profile": ollama_profile_command,
            "/remember": ollama_remember_command,
            "/forget": ollama_forget_command,
        }
        handler = handlers.get(command_word)
        if handler is not None:
            context.args = text.strip().split()[1:]
            await handler(update, context)
            return
        await message.reply_text("Ollama chat is active. Use /temp, /memory, /profile, /remember, /forget, or /bye.")
        return

    if session["busy"]:
        if not session.get("busy_notice_sent"):
            session["busy_notice_sent"] = True
            await message.reply_text(
                "⏳ I'm still waiting for the previous answer, so this message was ignored. "
                "Please send it again after I finish."
            )
        return

    if not text or not text.strip():
        await message.reply_text("Please send a text message, or use /bye to end the chat.")
        return

    session["busy"] = True
    session["busy_notice_sent"] = False
    session["task"] = asyncio.current_task()
    user_text = text.strip()[:12000]
    history = list(session["history"])
    history.append({"role": "user", "content": user_text})
    # Keep recent turns only. The profile/memory supplies durable context, while
    # a compact transcript reduces prompt processing on this CPU-limited host.
    # Keep the latest user message intact, but bound old turns. Carrying large
    # pasted logs/code forward into every prompt makes CPU-only generation slower.
    history = _bounded_chat_history(history)

    thinking_message = None
    try:
        arithmetic_result = _answer_arithmetic_question(user_text)
        standalone_arithmetic = _is_standalone_arithmetic_question(user_text)
        if arithmetic_result is not None and standalone_arithmetic:
            session["history"] = (history + [{"role": "assistant", "content": arithmetic_result}])[-12:]
            await message.reply_text(arithmetic_result)
            return

        # Do not let a fast path discard the non-math parts of a mixed message.
        # Keep the verified result as a hard fact for the model, while it answers
        # the user's other question/disclosure in the same turn.
        direct_answer = None
        if arithmetic_result is None:
            direct_answer = _answer_from_recent_disclosures(user_text, history[:-1])
        if direct_answer:
            session["history"] = (history + [{"role": "assistant", "content": direct_answer}])[-12:]
            await message.reply_text(direct_answer)
            return

        try:
            thinking_message = await message.reply_text(
                "> _Thinking…_",
                parse_mode="MarkdownV2",
            )
        except TelegramError:
            log.warning("Could not send Ollama thinking indicator", exc_info=True)

        # Do not start the secondary fact-extraction model before generating the
        # reply. On CPU-limited machines, concurrent Ollama requests compete for
        # the same cores and can make the user-facing response noticeably slower.
        system_prompt = _ollama_system_prompt(user_text, session.get("temporary_facts", []))
        # Retrieve a small external excerpt only for objective factual questions.
        # The helper excludes personal-memory, current-events, creative, and math queries.
        # Retrieval failure is non-fatal and never prevents a normal Ollama answer.
        evidence_started = time.perf_counter()
        evidence = await retrieve_multi_source_evidence(user_text)
        log.debug('Evidence retrieval stage took %.3fs', time.perf_counter() - evidence_started)
        evidence_prompt = format_multi_source_evidence_for_prompt(evidence, question=user_text)
        if evidence_prompt:
            system_prompt += "\n\n" + evidence_prompt
        elif is_time_sensitive_question(user_text):
            # Without a live source, current facts must be explicitly unverified.
            system_prompt += "\n\n" + format_time_sensitive_unavailable_prompt()
        if arithmetic_result is not None:
            system_prompt += (
                "\n\nVERIFIED CALCULATION: A deterministic arithmetic evaluator calculated "
                f"the expression at the end of the user's message as {arithmetic_result}. "
                "Include this exact result, and also answer every other part of the user's message. "
                "Do not recalculate it or ignore the non-math content."
            )
        generation_started = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(OLLAMA_TIMEOUT, connect=5.0)
        ) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": session["model"],
                    "messages": [{"role": "system", "content": system_prompt}] + history,
                    "stream": False,
                    "keep_alive": "10m",
                    "think": False,
                    "options": {
                        # Keep output bounded for the CPU-only i3-class host.
                        # Fewer generated tokens mean shorter wait times on local models.
                        "num_ctx": 2048,
                        # Cap routine replies to reduce CPU-only generation latency.
                        # Ask follow-up questions for additional detail when needed.
                        "num_predict": 128,
                        "temperature": 0.58,
                        "top_p": 0.85,
                        "top_k": 30,
                        "repeat_penalty": 1.15,
                        "presence_penalty": 0.25,
                    },
                },
            )
            response.raise_for_status()
            payload = response.json()
        generation_wall_seconds = time.perf_counter() - generation_started
        # Ollama reports nanosecond timings and token counts. Record them so
        # performance tuning is based on this PC's real throughput, not guesses.
        eval_count = payload.get("eval_count", 0) or 0
        eval_duration = payload.get("eval_duration", 0) or 0
        prompt_eval_count = payload.get("prompt_eval_count", 0) or 0
        prompt_eval_duration = payload.get("prompt_eval_duration", 0) or 0
        load_duration = payload.get("load_duration", 0) or 0
        tokens_per_second = (
            eval_count * 1_000_000_000 / eval_duration
            if eval_duration > 0 else 0.0
        )
        log.info(
            "Ollama performance model=%s wall=%.2fs load=%.2fs "
            "prompt_eval=%.2fs/%s tokens generation=%.2fs/%s tokens (%.2f tok/s)",
            session["model"],
            generation_wall_seconds,
            load_duration / 1_000_000_000,
            prompt_eval_duration / 1_000_000_000,
            prompt_eval_count,
            eval_duration / 1_000_000_000,
            eval_count,
            tokens_per_second,
        )
        answer = payload.get("message", {}).get("content", "").strip()
        if not answer:
            raise RuntimeError("Ollama returned no response text.")
        # Small models sometimes echo a sentence or paragraph inside one answer.
        answer = _deduplicate_answer_text(answer)
        if evidence_prompt:
            support = assess_claim_level_support(user_text, answer, evidence)
            if support["status"] == "candidate_supported":
                answer += format_multi_source_attribution(evidence)
            else:
                # This is only a lexical overlap check, not proof of correctness.
                # Be transparent when the generated answer doesn't visibly match
                # the retrieved extract instead of presenting the source as support.
                answer += format_support_warning(support)
                answer += format_multi_source_attribution(evidence)
        if CHAT_SESSIONS.get(key) is not session:
            return

        # Start secondary fact extraction only after the main model has finished.
        # It remains asynchronous and is chained behind any earlier extraction.
        pending_memory = session.get("memory_task")
        memory_task = asyncio.create_task(
            _extract_after_previous(pending_memory, key, session, user_text)
        )
        session["memory_task"] = memory_task

        if thinking_message is not None:
            try:
                await thinking_message.delete()
            except TelegramError:
                log.debug("Could not delete Ollama thinking indicator", exc_info=True)
            thinking_message = None
        session["history"] = (history + [{"role": "assistant", "content": answer}])[-12:]
        for chunk in _split_telegram_text(answer):
            if CHAT_SESSIONS.get(key) is not session:
                return
            await message.reply_text(chunk)
    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        if CHAT_SESSIONS.get(key) is session:
            await message.reply_text(f"⚠️ Ollama request failed: {str(exc)[:700]}")
    finally:
        if thinking_message is not None:
            try:
                await thinking_message.delete()
            except TelegramError:
                log.debug("Could not clean up Ollama thinking indicator", exc_info=True)
        if CHAT_SESSIONS.get(key) is session:
            session["busy"] = False
            session["task"] = None
            session["busy_notice_sent"] = False


async def ollama_memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    memory = _load_ollama_memory()
    lines = [f"🧠 Ollama memory file: {OLLAMA_MEMORY_PATH}", ""]
    lines.append("Profile:")
    lines.append(memory["profile"].strip() or "(empty)")
    lines.append("")
    lines.append("Saved facts:")
    lines.extend(f"{index}. {fact}" for index, fact in enumerate(memory["facts"], 1))
    text = "\n".join(lines)
    for chunk in _split_telegram_text(text):
        await update.effective_message.reply_text(chunk)


async def ollama_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    profile = " ".join(context.args).strip()
    memory = _load_ollama_memory()
    if not profile:
        await update.effective_message.reply_text(
            "🧠 Persistent Ollama profile:\n\n"
            + (memory["profile"].strip() or "(empty)")
            + "\n\nTo replace it, send /profile followed by the new profile text."
        )
        return
    memory["profile"] = profile[:6000]
    _save_ollama_memory(memory)
    await update.effective_message.reply_text(
        "🧠 Persistent Ollama profile updated. It will be included in future chats."
    )


async def ollama_remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    fact = " ".join(context.args).strip()
    if not fact:
        await update.effective_message.reply_text("Usage: /remember <detail> or /remember <category>: <detail>\nCategories: identity, style, learning, projects, linux, gaming, creative, food")
        return
    category_match = re.match(r"^(identity|style|learning|projects|linux|gaming|creative|food)\s*:\s*(.+)$", fact, flags=re.I)
    if category_match:
        fact = f"{category_match.group(1).casefold()}: {category_match.group(2).strip()}"
    memory = _load_ollama_memory()
    if any(existing.casefold() == fact.casefold() for existing in memory["facts"]):
        await update.effective_message.reply_text("That detail is already saved.")
        return
    if len(memory["facts"]) >= 100:
        await update.effective_message.reply_text("Memory is full (100 facts). Use /forget first.")
        return
    memory["facts"].append(fact[:1000])
    _save_ollama_memory(memory)
    await update.effective_message.reply_text("🧠 Saved. This detail will be included in future Ollama chats.")


async def ollama_forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    needle = " ".join(context.args).strip()
    if not needle:
        await update.effective_message.reply_text("Usage: /forget <text to match in a saved fact>")
        return
    memory = _load_ollama_memory()
    kept = [fact for fact in memory["facts"] if needle.casefold() not in fact.casefold()]
    removed = len(memory["facts"]) - len(kept)
    memory["facts"] = kept
    _save_ollama_memory(memory)
    await update.effective_message.reply_text(
        f"Removed {removed} saved fact(s) matching that text." if removed
        else "No saved facts matched. The profile field was not changed."
    )


def disk_usage_report() -> str:
    """Report root plus sda1/sda2 storage, including unmounted partition sizes."""
    selected = {}
    try:
        result = subprocess.run(
            ["lsblk", "--json", "--bytes", "--output",
             "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")

            def walk(nodes):
                for node in nodes:
                    name = str(node.get("name") or "")
                    mounts = node.get("mountpoints") or []
                    if isinstance(mounts, str):
                        mounts = [mounts]
                    mounts = [mount for mount in mounts if mount]
                    if node.get("type") == "part" and (
                        name in {"sda1", "sda2"} or "/" in mounts
                    ):
                        selected[name] = {
                            "size": int(node.get("size") or 0),
                            "fstype": node.get("fstype") or "unknown",
                            "mounts": mounts,
                        }
                    walk(node.get("children") or [])

            walk(data.get("blockdevices") or [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, ValueError):
        log.warning("Could not read partition layout with lsblk", exc_info=True)

    try:
        root = shutil.disk_usage("/")
        root_partition = next(
            (p.device for p in psutil.disk_partitions(all=False) if p.mountpoint == "/"),
            "root filesystem",
        )
        root_name = Path(root_partition).name
        selected[root_name] = {
            "size": root.total,
            "fstype": "mounted root",
            "mounts": ["/"],
            "usage": root,
        }
    except OSError:
        pass

    if not selected:
        return "💾 Storage\nRoot filesystem: unavailable"

    lines = ["💾 STORAGE & PARTITIONS", ""]
    for name, item in sorted(
        selected.items(),
        key=lambda pair: (pair[0] not in {"sda1", "sda2"}, pair[0]),
    ):
        mounts = item["mounts"]
        mount_text = ", ".join(mounts) if mounts else "not mounted"
        usage = item.get("usage")
        if usage is None and mounts:
            try:
                usage = shutil.disk_usage(mounts[0])
            except OSError:
                usage = None

        if usage is not None:
            used_gib = usage.used / 1024**3
            total_gib = usage.total / 1024**3
            free_gib = usage.free / 1024**3
            percent = usage.used / usage.total * 100 if usage.total else 0
            lines.append(
                f"📀 {name} [{item['fstype']}]\n"
                f"   Mount: {mount_text}\n"
                f"   Used: {used_gib:.1f} / {total_gib:.1f} GiB ({percent:.1f}%)\n"
                f"   Free: {free_gib:.1f} GiB"
            )
        else:
            lines.append(
                f"📀 {name} [{item['fstype']}]\n"
                f"   Mount: {mount_text}\n"
                f"   Capacity: {item['size'] / 1024**3:.1f} GiB"
            )
    return "\n\n".join(lines)


def system_status() -> str:
    memory = psutil.virtual_memory()
    uptime = timedelta(seconds=int(time.time() - psutil.boot_time()))
    load = os.getloadavg()

    return (
        "🖥️ CachyOS PC Status\n\n"
        f"CPU usage: {psutil.cpu_percent(interval=0.5):.1f}%\n"
        f"CPU load (1/5/15 min): "
        f"{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}\n\n"
        f"RAM: {memory.used / 1024**3:.2f} / "
        f"{memory.total / 1024**3:.2f} GiB ({memory.percent:.1f}%)\n"
        f"RAM available: {memory.available / 1024**3:.2f} GiB\n\n"
        f"{disk_usage_report()}\n\n"
        f"Uptime: {uptime}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    try:
        await update.effective_message.reply_text(system_status())
    except Exception:
        log.exception("Failed to collect system status")
        await update.effective_message.reply_text(
            "Couldn't collect system status. Check the bot logs."
        )


async def cpu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    load = os.getloadavg()
    await update.effective_message.reply_text(
        f"⚙️ CPU usage: {psutil.cpu_percent(interval=0.5):.1f}%\n"
        f"Load (1/5/15 min): "
        f"{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}"
    )


async def ram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    m = psutil.virtual_memory()
    await update.effective_message.reply_text(
        f"🧠 RAM: {m.used / 1024**3:.2f} / "
        f"{m.total / 1024**3:.2f} GiB ({m.percent:.1f}%)\n"
        f"Available: {m.available / 1024**3:.2f} GiB\n"
        f"Swap: {psutil.swap_memory().used / 1024**3:.2f} / "
        f"{psutil.swap_memory().total / 1024**3:.2f} GiB"
    )


async def disk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    try:
        message = disk_usage_report()
    except Exception:
        log.exception("Failed to collect partition storage")
        message = "Couldn't collect disk information. Check the bot logs."
    await update.effective_message.reply_text(message[:4000])


# Fixed allowlist. Sunshine is a per-user systemd service.
SERVICE_ALLOWLIST = {
    # Keep aligned with the Nexus Control Center service registry.
    "jellyfin": ("system", "jellyfin.service"),
    "docker": ("system", "docker.service"),
    "ollama": ("system", "ollama.service"),
    "localnexus": ("user", "localnexus.service"),
    "telegram-bot": ("user", "telegram-control-bot.service"),
    "dashboard": ("system", "system-dashboard.service"),
    "dashboard-ydotool": ("system", "system-dashboard-ydotool.service"),
    "sunshine": ("user", "app-dev.lizardbyte.app.Sunshine.service"),
}


async def services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    lines = ["⚙️ NEXUS + SYSTEM SERVICES", "Use /service NAME status|start|stop|restart to control one.", ""]
    for name, (scope, unit) in SERVICE_ALLOWLIST.items():
        command = ["systemctl"]
        if scope == "user":
            command.append("--user")
        command.extend(["is-active", unit])

        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=5, check=False
            )
            state = result.stdout.strip() or result.stderr.strip() or "unknown"
            lines.append(f"{name}: {state}")
        except (OSError, subprocess.TimeoutExpired):
            lines.append(f"{name}: unable to check")

    await update.effective_message.reply_text("\n".join(lines))


async def service_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage: /service <name> <status|start|stop|restart>\n"
            "Run /services to see supported service names."
        )
        return

    name, action = (arg.lower() for arg in context.args)
    if name not in SERVICE_ALLOWLIST:
        await update.effective_message.reply_text(
            "Service not allowed. Use /services to see the approved list."
        )
        return
    if action not in {"status", "start", "stop", "restart"}:
        await update.effective_message.reply_text(
            "Action not allowed. Choose status, start, stop, or restart."
        )
        return

    if action in {"stop", "restart"}:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Confirm " + action,
                callback_data=f"svc:{name}:{action}:{update.effective_user.id}"
            ),
            InlineKeyboardButton("Cancel", callback_data="svc:cancel"),
        ]])
        await update.effective_message.reply_text(
            f"⚠️ Confirm {action} for service '{name}'?",
            reply_markup=keyboard,
        )
        return

    await perform_service_action(update, name, action)


async def perform_service_action(update, name: str, action: str):
    scope, unit = SERVICE_ALLOWLIST[name]

    if scope == "user":
        command = ["systemctl", "--user"]
    elif action in {"start", "stop", "restart"}:
        command = ["sudo", "-n", "/usr/bin/systemctl"]
    else:
        command = ["systemctl"]

    command.extend(
        [action, unit] if action != "status" else ["is-active", unit]
    )

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False
        )
        if action == "status":
            state = result.stdout.strip() or result.stderr.strip() or "unknown"
            message = f"{name}: {state}"
        elif result.returncode == 0:
            message = f"Requested {action} for {name} successfully."
        else:
            detail = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit code {result.returncode}"
            )
            message = f"Could not {action} {name}: {detail[:700]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        message = f"Could not run the service action: {exc}"

    if update.callback_query:
        await update.callback_query.edit_message_text(message)
    else:
        await update.effective_message.reply_text(message)


async def service_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not authorized(update):
        await query.edit_message_text("Not authorized.")
        return

    parts = (query.data or "").split(":")
    if parts == ["svc", "cancel"]:
        await query.edit_message_text("Cancelled.")
        return
    if len(parts) != 4 or parts[0] != "svc":
        await query.edit_message_text("Invalid confirmation.")
        return

    _, name, action, requester_id = parts
    if requester_id != str(update.effective_user.id):
        await query.edit_message_text("This confirmation belongs to another user.")
        return
    if name not in SERVICE_ALLOWLIST or action not in {"stop", "restart"}:
        await query.edit_message_text("Action not allowed.")
        return

    await perform_service_action(update, name, action)


# Fixed aliases for individual containers and configured Nexus Compose projects.
# Compose paths are local to the PC; chat input cannot supply arbitrary paths.
DOCKER_PROJECTS = {
    "odysseus": Path.home() / "Projects" / "odysseus" / "docker-compose.yml",
    "uptime-kuma": Path.home() / "Projects" / "uptime-kuma" / "compose.yaml",
    "localnexus": Path.home() / "Projects" / "ReverseEngineering" / "LocalNexus" / "compose.yaml",
}
DOCKER_ALLOWLIST = {
    "uptime-kuma": "uptime-kuma",
    "odysseus-app": "odysseus-odysseus-1",
    "searxng": "odysseus-searxng-1",
    "ntfy": "odysseus-ntfy-1",
    "chromadb": "odysseus-chromadb-1",
    "localnexus-postgres": "localnexus-postgres-1",
}


def docker_list_text() -> str:
    lines = [
        "🐳 DOCKER CONTROL CENTER",
        "",
        "PROJECTS (Compose)",
        *[
            f"  • {name} → /docker project {name} <status|start|stop|restart>"
            for name in DOCKER_PROJECTS
        ],
        "",
        "CONTAINERS (alias → actual Docker name)",
    ]
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Docker command failed")
        rows = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                rows[parts[0].lstrip("/")] = (parts[1], parts[2])
        if not rows:
            lines.append("  No containers found.")
        else:
            reverse_aliases = {actual: alias for alias, actual in DOCKER_ALLOWLIST.items()}
            for actual, (state, image) in sorted(rows.items()):
                alias = reverse_aliases.get(actual, "not allowlisted")
                marker = "✅" if state.lower().startswith("up") else "⏹️"
                lines.append(f"{marker} Alias: {alias}")
                lines.append(f"   Docker name: {actual}")
                lines.append(f"   State: {state}")
                lines.append(f"   Image: {image}")
                if alias != "not allowlisted":
                    lines.append(f"   Control: /docker container {alias} <status|start|stop|restart>")
                lines.append("")
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        lines.append(f"Could not list containers: {str(exc)[:300]}")
    lines.extend([
        "Use the Alias value, not the long Docker-generated name.",
        "Stop/restart actions ask for confirmation.",
    ])
    return "\n".join(lines)


async def docker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    args = [arg.lower() for arg in context.args]
    if not args:
        await update.effective_message.reply_text(docker_list_text()[:4000])
        return

    if args[0] == "project":
        if len(args) != 3:
            await update.effective_message.reply_text(
                "Usage: /docker project <odysseus|uptime-kuma|localnexus> "
                "<status|start|stop|restart>\nRun /docker to see project aliases."
            )
            return
        _, name, action = args
        if name not in DOCKER_PROJECTS:
            await update.effective_message.reply_text(
                "Unknown Compose project. Run /docker to see supported names."
            )
            return
        if action not in {"status", "start", "stop", "restart"}:
            await update.effective_message.reply_text("Choose status, start, stop, or restart.")
            return
        if action in {"stop", "restart"}:
            await update.effective_message.reply_text(
                f"⚠️ This affects every container in the '{name}' Compose project. Continue?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"Confirm {action}", callback_data=f"dockproj:{name}:{action}:{update.effective_user.id}"
                    ),
                    InlineKeyboardButton("Cancel", callback_data="dockproj:cancel"),
                ]]),
            )
            return
        await perform_docker_project_action(update, name, action)
        return

    if args[0] == "container":
        args = args[1:]
    if len(args) != 2:
        await update.effective_message.reply_text(
            "Usage:\n/docker  List projects and container aliases\n"
            "/docker <alias> <status|start|stop|restart>\n"
            "/docker container <alias> <status|start|stop|restart>\n"
            "/docker project <name> <status|start|stop|restart>\n"
            "Run /docker for the exact alias names."
        )
        return

    name, action = args
    if name not in DOCKER_ALLOWLIST:
        await update.effective_message.reply_text(
            "Unknown container alias. Run /docker and copy the Alias value, not the Docker-generated name."
        )
        return
    if action not in {"status", "start", "stop", "restart"}:
        await update.effective_message.reply_text("Choose status, start, stop, or restart.")
        return

    if action in {"stop", "restart"}:
        await update.effective_message.reply_text(
            f"⚠️ Confirm {action} for container '{name}' ({DOCKER_ALLOWLIST[name]})?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Confirm " + action,
                    callback_data=f"dock:{name}:{action}:{update.effective_user.id}"
                ),
                InlineKeyboardButton("Cancel", callback_data="dock:cancel"),
            ]]),
        )
        return
    await perform_docker_action(update, name, action)


async def perform_docker_project_action(update, name: str, action: str):
    compose_file = DOCKER_PROJECTS[name]
    if not compose_file.is_file():
        message = (
            f"Compose file for '{name}' was not found at {compose_file}. "
            "Check that the project is installed at its configured path."
        )
    else:
        if action == "status":
            command = ["docker", "compose", "-f", str(compose_file), "ps", "--all"]
        elif action == "start":
            command = ["docker", "compose", "-f", str(compose_file), "up", "-d"]
        elif action == "stop":
            command = ["docker", "compose", "-f", str(compose_file), "stop"]
        else:
            command = ["docker", "compose", "-f", str(compose_file), "restart"]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=60, check=False,
                cwd=str(compose_file.parent),
            )
            if result.returncode == 0:
                detail = result.stdout.strip()
                message = f"🐳 Compose project: {name}\nAction: {action}\n\n{detail or 'Command completed successfully.'}"
            else:
                detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
                message = f"Could not {action} Compose project '{name}':\n{detail[:900]}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            message = f"Compose project action failed: {str(exc)[:500]}"
    if update.callback_query:
        await update.callback_query.edit_message_text(message[:4000])
    else:
        await update.effective_message.reply_text(message[:4000])


async def perform_docker_action(update, name: str, action: str):
    container = DOCKER_ALLOWLIST[name]
    if action == "status":
        command = [
            "docker", "inspect", "--format",
            "{{.Name}} | {{.Config.Image}} | {{.State.Status}} | "
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}",
            container,
        ]
    else:
        command = ["docker", action, container]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False
        )
        if result.returncode == 0:
            detail = result.stdout.strip()
            if action == "status":
                message = f"🐳 CONTAINER STATUS\nAlias: {name}\nDocker name: {container}\n{detail or 'unknown status'}"
            else:
                message = f"✅ Requested {action} for '{name}' ({container})."
        else:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            message = f"Could not {action} '{name}' ({container}): {detail[:700]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        message = f"Docker action failed: {str(exc)[:500]}"
    if update.callback_query:
        await update.callback_query.edit_message_text(message[:4000])
    else:
        await update.effective_message.reply_text(message[:4000])


async def docker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not authorized(update):
        await query.edit_message_text("Not authorized.")
        return
    parts = (query.data or "").split(":")
    if parts == ["dock", "cancel"]:
        await query.edit_message_text("Cancelled.")
        return
    if len(parts) != 4 or parts[0] != "dock":
        await query.edit_message_text("Invalid confirmation.")
        return
    _, name, action, requester_id = parts
    if requester_id != str(update.effective_user.id):
        await query.edit_message_text("This confirmation belongs to another user.")
        return
    if name not in DOCKER_ALLOWLIST or action not in {"stop", "restart"}:
        await query.edit_message_text("Action not allowed.")
        return
    await perform_docker_action(update, name, action)


async def docker_project_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not authorized(update):
        await query.edit_message_text("Not authorized.")
        return
    parts = (query.data or "").split(":")
    if parts == ["dockproj", "cancel"]:
        await query.edit_message_text("Cancelled.")
        return
    if len(parts) != 4 or parts[0] != "dockproj":
        await query.edit_message_text("Invalid confirmation.")
        return
    _, name, action, requester_id = parts
    if requester_id != str(update.effective_user.id):
        await query.edit_message_text("This confirmation belongs to another user.")
        return
    if name not in DOCKER_PROJECTS or action not in {"stop", "restart"}:
        await query.edit_message_text("Action not allowed.")
        return
    await perform_docker_project_action(update, name, action)



POWER_ACTIONS = {
    "lock": "Lock the Hyprland session",
    "suspend": "Suspend the computer",
    "reboot": "Reboot the computer",
    "shutdown": "Shut down the computer",
}

# Use Serpantinum's own commands for the active Hyprland desktop.
SERPANTINUM = "/home/marwan/.local/bin/serpantinum"

POWER_COMMANDS = {
    "lock": [SERPANTINUM, "lock"],
    "suspend": [SERPANTINUM, "suspend"],
    "reboot": [SERPANTINUM, "reboot"],
    "shutdown": [SERPANTINUM, "poweroff"],
}


def power_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔒 Lock", callback_data="power:request:lock"),
            InlineKeyboardButton("💤 Suspend", callback_data="power:request:suspend"),
        ],
        [
            InlineKeyboardButton("🔄 Reboot", callback_data="power:request:reboot"),
            InlineKeyboardButton("⏻ Shut down", callback_data="power:request:shutdown"),
        ],
        [InlineKeyboardButton("⬅️ Back to main menu", callback_data="power:back")],
    ])


async def power_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.effective_message.reply_text(
        "🔌 REMOTE POWER CONTROLS\n\n"
        "Choose an action. Every action requires confirmation.\n"
        "Confirmations expire after 60 seconds.",
        reply_markup=power_keyboard(),
    )


async def power_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not authorized(update):
        await query.edit_message_text("Not authorized.")
        return

    parts = (query.data or "").split(":")
    user_id = str(update.effective_user.id)

    if parts == ["power", "menu"]:
        await query.edit_message_text(
            "🔌 REMOTE POWER CONTROLS\n\n"
            "Choose an action. Every action requires confirmation.\n"
            "Confirmations expire after 60 seconds.",
            reply_markup=power_keyboard(),
        )
        return

    if parts == ["power", "back"]:
        await query.edit_message_text(
            MENU_HOME_TEXT, reply_markup=menu_keyboard()
        )
        return

    if len(parts) == 3 and parts[:2] == ["power", "request"]:
        action = parts[2]
        if action not in POWER_ACTIONS:
            await query.edit_message_text(
                "Action not allowed.", reply_markup=power_keyboard()
            )
            return

        import time
        issued = int(time.time())
        await query.edit_message_text(
            f"⚠️ Confirm: {POWER_ACTIONS[action]}?\n\n"
            "This action may interrupt anything running on the PC. "
            "The confirmation expires in 60 seconds.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "✅ Confirm",
                    callback_data=f"power:confirm:{action}:{user_id}:{issued}",
                )],
                [InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"power:cancel:{user_id}:{issued}",
                )],
            ]),
        )
        return

    if len(parts) == 4 and parts[:2] == ["power", "cancel"]:
        if parts[2] != user_id:
            await query.edit_message_text(
                "This confirmation belongs to another user."
            )
            return
        await query.edit_message_text(
            "Cancelled.", reply_markup=power_keyboard()
        )
        return

    if len(parts) == 5 and parts[:2] == ["power", "confirm"]:
        _, _, action, requester_id, issued_text = parts

        if requester_id != user_id:
            await query.edit_message_text(
                "This confirmation belongs to another user."
            )
            return
        if action not in POWER_ACTIONS:
            await query.edit_message_text("Action not allowed.")
            return

        import time
        try:
            issued = int(issued_text)
        except ValueError:
            await query.edit_message_text("Invalid confirmation.")
            return

        age = time.time() - issued
        if age < 0 or age > 60:
            await query.edit_message_text(
                "⌛ Confirmation expired. Open /power to try again.",
                reply_markup=power_keyboard(),
            )
            return

        # Prevent replaying the same confirmation callback.
        used = context.application.bot_data.setdefault(
            "power_used_confirmations", set()
        )
        token = f"{action}:{requester_id}:{issued}"
        if token in used:
            await query.edit_message_text(
                "This confirmation has already been used."
            )
            return
        used.add(token)

        command = POWER_COMMANDS[action]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                message = (
                    f"✅ {POWER_ACTIONS[action]} request accepted by logind."
                )
            else:
                detail = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"exit code {result.returncode}"
                )
                message = (
                    f"Could not {action} the computer: {detail[:500]}"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            message = f"Power action failed: {str(exc)[:300]}"

        await query.edit_message_text(message)
        return

    await query.edit_message_text(
        "Invalid power action.", reply_markup=power_keyboard()
    )


def format_bytes(value):
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024


async def temps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    try:
        sensors = psutil.sensors_temperatures()
        lines = []
        for chip, entries in sensors.items():
            for entry in entries:
                if entry.current is None:
                    continue
                label = entry.label or chip
                lines.append(f"{label}: {entry.current:.1f}°C")
        message = "🌡️ Temperatures\n\n" + (
            "\n".join(lines[:30]) if lines else
            "No readable temperature sensors were found."
        )
    except (AttributeError, OSError, RuntimeError) as exc:
        message = f"Temperature readings unavailable: {str(exc)[:250]}"

    await update.effective_message.reply_text(message[:4000])


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    try:
        # Prime CPU counters, then take a short second sample.
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                proc.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        await asyncio.sleep(0.35)
        rows = []
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                info = proc.info
                rows.append((
                    proc.cpu_percent(None),
                    info["memory_info"].rss if info["memory_info"] else 0,
                    info["pid"],
                    info["name"] or "unknown",
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
        lines = [
            f"{cpu:5.1f}% CPU | {format_bytes(mem):>9} | PID {pid} | {name}"
            for cpu, mem, pid, name in rows[:10]
        ]
        message = "📊 Top processes\n\n" + (
            "\n".join(lines) if lines else "No process data available."
        )
    except (OSError, RuntimeError) as exc:
        message = f"Couldn't read process information: {str(exc)[:250]}"

    await update.effective_message.reply_text(message[:4000])


async def uptime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    try:
        elapsed = max(0, int(time.time() - psutil.boot_time()))
        days, remainder = divmod(elapsed, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        boot = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(psutil.boot_time()))
        message = (
            "⏱️ System uptime\n\n"
            f"Uptime: {days}d {hours}h {minutes}m\n"
            f"Boot time: {boot}"
        )
    except (OSError, RuntimeError) as exc:
        message = f"Couldn't read uptime: {str(exc)[:250]}"

    await update.effective_message.reply_text(message)


async def network_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    try:
        counters = psutil.net_io_counters()
        interfaces = psutil.net_if_stats()
        active = [
            f"{name}: {'up' if stats.isup else 'down'}"
            for name, stats in interfaces.items()
            if name != "lo"
        ]
        message = (
            "🌐 Network overview\n\n"
            f"Bytes received: {format_bytes(counters.bytes_recv)}\n"
            f"Bytes sent: {format_bytes(counters.bytes_sent)}\n"
            f"Packets received: {counters.packets_recv:,}\n"
            f"Packets sent: {counters.packets_sent:,}\n\n"
            "Interfaces:\n" + ("\n".join(active[:15]) if active else "None found")
        )
    except (OSError, RuntimeError) as exc:
        message = f"Couldn't read network statistics: {str(exc)[:250]}"

    await update.effective_message.reply_text(message[:4000])

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return

        body = b'{"status":"ok","service":"telegram-control-bot"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def start_health_server():
    server = ThreadingHTTPServer(("172.18.0.1", 8765), HealthHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health endpoint listening on 172.18.0.1:8765/health")

COMMAND_DESCRIPTIONS = [
    BotCommand("start", "Welcome and open the control menu"),
    BotCommand("menu", "Open the interactive control dashboard"),
    BotCommand("help", "Explain every command and its usage"),
    BotCommand("status", "Show CPU, memory, storage, and uptime"),
    BotCommand("cpu", "Show CPU utilization and system load"),
    BotCommand("ram", "Show RAM and swap usage"),
    BotCommand("disk", "Show root, sda1, and sda2 storage"),
    BotCommand("temps", "Read available hardware temperatures"),
    BotCommand("top", "Show the busiest processes"),
    BotCommand("uptime", "Show uptime and last boot time"),
    BotCommand("network", "Show network counters and interfaces"),
    BotCommand("services", "List Nexus/systemd service states"),
    BotCommand("service", "Control or check an approved systemd service"),
    BotCommand("docker", "List and manage approved containers and projects"),
    BotCommand("links", "Show dashboards and PIN-protected credentials"),
    BotCommand("power", "Open confirmed lock/suspend/reboot/shutdown controls"),
    BotCommand("chat", "Start a private chat with a local Ollama model"),
    BotCommand("bye", "End the active Ollama chat"),
    BotCommand("memory", "Show persistent Ollama memory"),
    BotCommand("profile", "Set persistent instructions and preferences for Ollama"),
    BotCommand("remember", "Save a detail for future Ollama chats"),
    BotCommand("forget", "Remove matching saved Ollama facts"),
]


async def register_command_descriptions(application):
    """Publish helpful labels in Telegram's command picker."""
    try:
        await application.bot.set_my_commands(COMMAND_DESCRIPTIONS)
    except Exception:
        # Command descriptions are a convenience; don't prevent bot startup.
        log.exception("Could not publish Telegram command descriptions")


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing from .env")
    if not ALLOWED_USER_ID.isdigit():
        raise SystemExit(
            "TELEGRAM_ALLOWED_USER_ID must be your numeric Telegram user ID"
        )

    app = Application.builder().token(TOKEN).post_init(register_command_descriptions).build()
    # During an Ollama session, these first-in-group handlers absorb messages and
    # callbacks before normal bot commands/menus can run. block=False lets the bot
    # observe and discard messages sent while a model response is in progress.
    app.add_handler(MessageHandler(
        ActiveOllamaChatFilter(), active_chat_message, block=False
    ))
    app.add_handler(
        CallbackQueryHandler(active_chat_callback),
        group=-1,
    )
    app.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(reveal_credentials_start, pattern=r"^links:reveal$")
        ],
        states={PIN_ENTRY: [MessageHandler(filters.ALL, credentials_pin_handler)]},
        fallbacks=[],
        per_chat=True,
        per_user=True,
    ))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CommandHandler("power", power_command))
    app.add_handler(CallbackQueryHandler(power_callback, pattern=r"^power:"))
    app.add_handler(CommandHandler("links", links_command))
    app.add_handler(CommandHandler("chat", ollama_chat_start))
    app.add_handler(CommandHandler("memory", ollama_memory_command))
    app.add_handler(CommandHandler("profile", ollama_profile_command))
    app.add_handler(CommandHandler("remember", ollama_remember_command))
    app.add_handler(CommandHandler("forget", ollama_forget_command))
    app.add_handler(CallbackQueryHandler(ollama_model_selected, pattern=r"^ollama:model:"))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cpu", cpu))
    app.add_handler(CommandHandler("ram", ram))
    app.add_handler(CommandHandler("disk", disk))
    app.add_handler(CommandHandler("temps", temps_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("uptime", uptime_command))
    app.add_handler(CommandHandler("network", network_command))
    app.add_handler(CommandHandler("services", services))
    app.add_handler(CommandHandler("service", service_command))
    app.add_handler(CallbackQueryHandler(service_callback, pattern=r"^svc:"))
    app.add_handler(CommandHandler("docker", docker_command))
    app.add_handler(CallbackQueryHandler(docker_callback, pattern=r"^dock:"))
    app.add_handler(CallbackQueryHandler(docker_project_callback, pattern=r"^dockproj:"))

    start_health_server()
    log.info("Starting CachyOS Control Bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
