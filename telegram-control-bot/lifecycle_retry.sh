#!/bin/sh
set -u

EVENT="${1:-}"
case "$EVENT" in
    boot|resume) ;;
    *)
        echo "Usage: lifecycle_retry.sh {boot|resume}" >&2
        exit 2
        ;;
esac

BASE="/home/marwan/Projects/telegram-control-bot"
PY="$BASE/.venv/bin/python"
HELPER="$BASE/lifecycle_notify.py"

attempt=1
max_attempts=6

while [ "$attempt" -le "$max_attempts" ]; do
    echo "Lifecycle event '$EVENT': attempt $attempt/$max_attempts"

    if /usr/bin/timeout 8s "$PY" "$HELPER" "$EVENT"; then
        echo "Lifecycle event '$EVENT' delivered or already handled."
        exit 0
    fi

    attempt=$((attempt + 1))
    if [ "$attempt" -le "$max_attempts" ]; then
        sleep 10
    fi
done

echo "Lifecycle event '$EVENT' failed after $max_attempts attempts." >&2
exit 1
