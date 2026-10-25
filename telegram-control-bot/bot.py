import asyncio
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import os
import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import psutil
from dotenv import load_dotenv
from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

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
    if not await guard(update):
        return
    await update.effective_message.reply_text(
        "🖥️ CACHYOS CONTROL CENTER\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Your PC, at your fingertips. 🤖\n\n"
        "📊 System      /status\n"
        "🌡️ Temps       /temps\n"
        "📈 Processes   /top\n"
        "🌐 Network     /network\n"
        "⚙️ Services    /services\n"
        "🐳 Docker      /docker\n"
        "🔗 Dashboards  /links\n"
        "📖 Commands    /help\n\n"
        "🔐 Private access enabled"
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
            InlineKeyboardButton("🔄 Refresh", callback_data=f"menu:{section}"),
            InlineKeyboardButton("⬅️ Back to menu", callback_data="menu:home"),
        ])

    return InlineKeyboardMarkup(buttons)


MENU_HOME_TEXT = (
    "🖥️ CACHYOS CONTROL CENTER\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Your PC, at your fingertips. 🤖\n\n"
    "Choose a section below.\n\n"
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
        lines = ["🐳 Approved container status", ""]
        for name, container in DOCKER_ALLOWLIST.items():
            try:
                result = subprocess.run(
                    [
                        "docker", "inspect", "--format",
                        "{{.State.Status}}", container,
                    ],
                    capture_output=True, text=True, timeout=5, check=False
                )
                state = (
                    result.stdout.strip() if result.returncode == 0
                    else "unavailable"
                )
            except (OSError, subprocess.TimeoutExpired):
                state = "unable to check"
            lines.append(f"{name}: {state or 'unknown'}")
        return "\n".join(lines)

    if action == "links":
        return (
            "🔗 LOCAL DASHBOARDS\n\n"
            "🖥️ System dashboard\nhttp://marwan-pc.local:7171/login\n\n"
            "🧠 Odysseus\nhttp://marwan-pc.local:7011/\n\n"
            "🎬 Jellyfin\nhttp://marwan-pc.local:8096/\n\n"
            "📡 Uptime Kuma\nhttp://marwan-pc.local:3001/\n\n"
            "🔒 Passwords are intentionally omitted here."
        )

    if action == "help":
        return (
            "📖 CONTROL CENTER | COMMAND GUIDE\n\n"
            "📊 SYSTEM\n"
            "/status  Overall system status\n"
            "/cpu     CPU usage and load\n"
            "/ram     RAM and swap usage\n"
            "/disk    Root disk space\n"
            "/temps   Temperatures\n"
            "/top     Top processes\n"
            "/uptime  Uptime and boot time\n"
            "/network Network counters and interfaces\n\n"
            "⚙️ SERVICES\n"
            "/services  Show approved service states\n"
            "/service <name> <status|start|stop|restart>\n"
            "Names: jellyfin, ollama, sunshine\n\n"
            "🐳 DOCKER\n"
            "/docker  List containers\n"
            "/docker <name> <status|start|stop|restart>\n"
            "Use only approved container names.\n"
            "Stop/restart require confirmation."
        )

    return None


async def menu_callback(update, context):
    query = update.callback_query
    await query.answer()

    if not authorized(update):
        await query.edit_message_text("Not authorized.")
        return

    action = (query.data or "").removeprefix("menu:")

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
        text = await menu_page_text(action)
        await query.edit_message_text(
            (text or "No information available.")[:4000],
            reply_markup=menu_keyboard(action),
        )
    except Exception:
        log.exception("Menu section failed: %s", action)
        await query.edit_message_text(
            f"Couldn't load {action}. Check the bot logs.",
            reply_markup=menu_keyboard(action),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    message = (
        "PC CONTROL BOT\\n\\n"
        "SYSTEM\\n"
        "/status - System status\\n"
        "/cpu - CPU information\\n"
        "/ram - Memory usage\\n"
        "/disk - Disk usage\\n\\n"
        "SERVICES\\n"
        "/services - List allowed services\\n"
        "/service - Service controls\\n\\n"
        "DOCKER\\n"
        "/docker - Container controls\\n\\n"
        "POWER\\n"
        "/power - Lock, suspend, reboot or shut down\\n"
        "Power actions require confirmation.\\n\\n"
        "OTHER\\n"
        "/links - Open dashboards and links\\n"
        "/menu - Open the interactive menu\\n"
        "/help - Show this help message"
    )
    await update.effective_message.reply_text(message)


async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.effective_message.reply_text(
        "🔗 YOUR LOCAL DASHBOARDS\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🖥️ Dashboard\n"
        "http://marwan-pc.local:7171/login\n"
        "Username: marwan\n"
        "Password: 7171\n\n"
        "🧠 Odysseus\n"
        "http://marwan-pc.local:7011/\n"
        "Username: admin\n"
        "Password: admin7171\n\n"
        "🎬 Jellyfin\n"
        "http://marwan-pc.local:8096/\n"
        "Username: admin\n"
        "Password: admin\n\n"
        "📡 Uptime Kuma\n"
        "http://marwan-pc.local:3001/\n"
        "Username: admin\n"
        "Password: admin7171\n\n"
        "🔒 Keep these credentials private."
    )



def system_status() -> str:
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
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
        f"Root disk: {disk.used / 1024**3:.1f} / "
        f"{disk.total / 1024**3:.1f} GiB "
        f"({disk.used / disk.total * 100:.1f}%)\n"
        f"Disk free: {disk.free / 1024**3:.1f} GiB\n\n"
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
    d = shutil.disk_usage("/")
    await update.effective_message.reply_text(
        f"💾 Root disk usage: {d.used / 1024**3:.1f} / "
        f"{d.total / 1024**3:.1f} GiB "
        f"({d.used / d.total * 100:.1f}%)\n"
        f"Free: {d.free / 1024**3:.1f} GiB"
    )



# Fixed allowlist. Sunshine is a per-user systemd service.
SERVICE_ALLOWLIST = {
    "jellyfin": ("system", "jellyfin.service"),
    "ollama": ("system", "ollama.service"),
    "sunshine": ("user", "app-dev.lizardbyte.app.Sunshine.service"),
    "dashboard": ("system", "system-dashboard.service"),
}


async def services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    lines = ["🔧 Approved service status", ""]
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
            "Usage: /service <jellyfin|ollama|sunshine> "
            "<status|start|stop|restart>"
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
            f"Confirm {action} for {name}?",
            reply_markup=keyboard,
        )
        return

    await perform_service_action(update, name, action)


async def perform_service_action(update, name: str, action: str):
    scope, unit = SERVICE_ALLOWLIST[name]

    if scope == "user":
        command = ["systemctl", "--user"]
    elif name == "ollama" and action in {"start", "stop", "restart"}:
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


# Explicit container allowlist. Compose projects are handled separately later.
DOCKER_ALLOWLIST = {
    "uptime-kuma": "uptime-kuma",
    "odysseus": "odysseus-odysseus-1",
    "searxng": "odysseus-searxng-1",
    "ntfy": "odysseus-ntfy-1",
    "chromadb": "odysseus-chromadb-1",
    "localnexus-postgres": "localnexus-postgres-1",
}


async def docker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return

    if not context.args:
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--format",
                 "{{.Names}} | {{.Status}}"],
                capture_output=True, text=True, timeout=10, check=False
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "Docker command failed")
            lines = result.stdout.strip().splitlines()
            message = "🐳 Containers\n\n" + ("\n".join(lines) if lines else "No containers found.")
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            message = f"Couldn't list containers: {str(exc)[:500]}"
        await update.effective_message.reply_text(message[:4000])
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage:\n/docker\n/docker <name> <status|start|stop|restart>\n\n"
            "Names: " + ", ".join(DOCKER_ALLOWLIST)
        )
        return

    name, action = (arg.lower() for arg in context.args)
    if name not in DOCKER_ALLOWLIST:
        await update.effective_message.reply_text("Container not allowlisted.")
        return
    if action not in {"status", "start", "stop", "restart"}:
        await update.effective_message.reply_text("Action not allowed.")
        return

    if action in {"stop", "restart"}:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Confirm " + action,
                callback_data=f"dock:{name}:{action}:{update.effective_user.id}"
            ),
            InlineKeyboardButton("Cancel", callback_data="dock:cancel"),
        ]])
        await update.effective_message.reply_text(
            f"Confirm {action} for container {name}?",
            reply_markup=keyboard,
        )
        return

    await perform_docker_action(update, name, action)


async def perform_docker_action(update, name: str, action: str):
    container = DOCKER_ALLOWLIST[name]

    if action == "status":
        command = [
            "docker", "inspect", "--format",
            "{{.State.Status}} | {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}",
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
                message = f"🐳 {name}: {detail or 'unknown status'}"
            else:
                message = f"Requested {action} for {name} successfully."
        else:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            message = f"Could not {action} {name}: {detail[:700]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        message = f"Docker action failed: {str(exc)[:500]}"

    if update.callback_query:
        await update.callback_query.edit_message_text(message)
    else:
        await update.effective_message.reply_text(message)


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

def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing from .env")
    if not ALLOWED_USER_ID.isdigit():
        raise SystemExit(
            "TELEGRAM_ALLOWED_USER_ID must be your numeric Telegram user ID"
        )

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CommandHandler("power", power_command))
    app.add_handler(CallbackQueryHandler(power_callback, pattern=r"^power:"))
    app.add_handler(CommandHandler("links", links_command))
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

    start_health_server()
    log.info("Starting CachyOS Control Bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
