#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

MESSAGES = {
    "boot": (
        "🟢 PC ONLINE\n\n"
        "Your CachyOS computer has booted and is ready."
    ),
    "suspend": (
        "🌙 PC SUSPENDING\n\n"
        "Your computer is about to enter sleep/suspend."
    ),
    "resume": (
        "☀️ PC RESUMED\n\n"
        "Your computer has woken from sleep/suspend."
    ),
    "locked": (
        "🔒 PC LOCKED\n\n"
        "Your computer's screen is now locked."
    ),
    "unlocked": (
        "🔓 PC UNLOCKED\n\n"
        "Your computer's screen has been unlocked."
    ),
    "test": (
        "🔔 Lifecycle notification test successful.\n\n"
        "Telegram delivery is configured correctly."
    ),
}

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in MESSAGES:
        print("Usage: lifecycle_notify.py {boot|suspend|resume|locked|unlocked|test}")
        return 2

    if not TOKEN or not CHAT_ID.isdigit():
        print("Telegram configuration is missing or invalid.")
        return 2

    event = sys.argv[1]

    # Send a boot alert only once for each Linux boot.
    marker = os.path.join(
        os.path.expanduser("~"),
        ".local", "state", "telegram-control-bot", "boot_id"
    )
    if event == "boot":
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as f:
                current_boot = f.read().strip()
            try:
                with open(marker, encoding="utf-8") as f:
                    stored_boot = f.read().strip()
                    if stored_boot in (current_boot, current_boot + r"\n"):
                        print("Boot notification already sent this boot.")
                        return 0
            except FileNotFoundError:
                pass
        except OSError:
            current_boot = None

    payload = json.dumps({
        "chat_id": int(CHAT_ID),
        "text": MESSAGES[event],
    }).encode()

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read())
        if not result.get("ok"):
            print("Telegram rejected the notification.")
            return 1
        if event == "boot" and current_boot:
            os.makedirs(os.path.dirname(marker), mode=0o700, exist_ok=True)
            temp_marker = marker + ".tmp"
            with open(temp_marker, "w", encoding="utf-8") as f:
                f.write(current_boot + "\n")
            os.replace(temp_marker, marker)
        print("Notification sent.")
        return 0
    except (urllib.error.URLError, TimeoutError, ValueError):
        print("Notification failed; Telegram may be unreachable.")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
