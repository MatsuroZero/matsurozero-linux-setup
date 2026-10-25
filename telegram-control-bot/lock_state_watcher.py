#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYTHON = BASE_DIR / ".venv" / "bin" / "python"
NOTIFIER = BASE_DIR / "lifecycle_notify.py"

runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
lock_dir = Path(os.environ.get("QS_RUN_LOCK", str(runtime / "serpantinum" / "lock")))
LOCK_FILE = lock_dir / "locked"

STATE_DIR = Path.home() / ".local" / "state" / "telegram-control-bot"
QUEUE_FILE = STATE_DIR / "lock-notification-queue.jsonl"

POLL_SECONDS = 0.25
STABLE_SECONDS = 1.0
RETRY_SECONDS = 10.0


def is_locked():
    return LOCK_FILE.exists()


def enqueue(event):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with QUEUE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": event}) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print(f"Queued notification: {event}", flush=True)


def read_queue():
    if not QUEUE_FILE.exists():
        return []
    entries = []
    with QUEUE_FILE.open(encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                if item.get("event") in ("locked", "unlocked"):
                    entries.append(item)
            except (json.JSONDecodeError, AttributeError):
                print("Ignoring malformed queue entry.", file=sys.stderr, flush=True)
    return entries


def remove_first_queue_entry():
    entries = read_queue()
    if entries:
        entries.pop(0)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = QUEUE_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, QUEUE_FILE)


def main():
    print(f"Watching Serpantinum lock state: {LOCK_FILE}", flush=True)

    # Start silently; do not invent a lock/unlock event on service startup.
    current = is_locked()
    candidate = current
    candidate_since = time.monotonic()
    last_attempt = 0.0

    print(f"Initial state: {'locked' if current else 'unlocked'}", flush=True)

    while True:
        time.sleep(POLL_SECONDS)
        observed = is_locked()
        now = time.monotonic()

        if observed != candidate:
            candidate = observed
            candidate_since = now
            continue

        # Record every stable transition even if Telegram is offline.
        if candidate != current and now - candidate_since >= STABLE_SECONDS:
            event = "locked" if candidate else "unlocked"
            enqueue(event)
            current = candidate

        # Deliver queued events in order, retrying after failures.
        if now - last_attempt < RETRY_SECONDS:
            continue

        queue = read_queue()
        if not queue:
            continue

        event = queue[0]["event"]
        try:
            result = subprocess.run(
                [str(PYTHON), str(NOTIFIER), event],
                cwd=str(BASE_DIR),
                timeout=12,
                check=False,
            )
            last_attempt = time.monotonic()

            if result.returncode == 0:
                remove_first_queue_entry()
                print(f"Telegram notification delivered: {event}", flush=True)
            else:
                print(
                    f"Notification failed for {event}; it remains queued.",
                    file=sys.stderr,
                    flush=True,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_attempt = time.monotonic()
            print(
                f"Notification error: {exc}; event remains queued: {event}",
                file=sys.stderr,
                flush=True,
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
