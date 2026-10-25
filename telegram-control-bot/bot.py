import asyncio
import json
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
from telegram import BotCommand, Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
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
            "🔗 DASHBOARDS & POWER\n"
            "/links  Show dashboard URLs; credentials require a PIN\n"
            "/power  Open lock, suspend, reboot, and shutdown controls\n"
            "Power actions require confirmation and expire after 60 seconds.\n\n"
            "🔒 SECURITY\n"
            "Only the configured Telegram user ID is authorized. Service and Docker "
            "actions use fixed allowlists; arbitrary shell commands are not accepted."
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
