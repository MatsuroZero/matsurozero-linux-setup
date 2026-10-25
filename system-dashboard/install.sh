#!/usr/bin/env bash
set -Eeuo pipefail

PACKAGE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${SYSTEM_DASHBOARD_DIR:-$HOME/system-dashboard}"
TEMPLATE="$TARGET/templates/dashboard.html"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [[ ! -f "$PACKAGE_DIR/app.py" || ! -f "$PACKAGE_DIR/dashboard.html" ]]; then
  echo "Package is missing app.py or dashboard.html." >&2
  exit 1
fi
if [[ ! -f "$TARGET/app.py" || ! -d "$TARGET/templates" ]]; then
  echo "Could not find the existing dashboard at: $TARGET" >&2
  echo "Set SYSTEM_DASHBOARD_DIR to its location and rerun." >&2
  exit 1
fi

python -m py_compile "$PACKAGE_DIR/app.py"
BACKEND_BACKUP="$TARGET/app.py.backup.$STAMP"
FRONTEND_BACKUP="$TEMPLATE.backup.$STAMP"
cp -a "$TARGET/app.py" "$BACKEND_BACKUP"
cp -a "$TEMPLATE" "$FRONTEND_BACKUP"

restore() {
  local rc=$?
  if (( rc != 0 )); then
    echo "Install/restart failed. Restoring backups..." >&2
    cp -a "$BACKEND_BACKUP" "$TARGET/app.py" || true
    cp -a "$FRONTEND_BACKUP" "$TEMPLATE" || true
    sudo systemctl restart system-dashboard.service || true
  fi
  exit "$rc"
}
trap restore ERR
cp "$PACKAGE_DIR/app.py" "$TARGET/app.py"
cp "$PACKAGE_DIR/dashboard.html" "$TEMPLATE"
python -m py_compile "$TARGET/app.py"
sudo systemctl restart system-dashboard.service
systemctl is-active --quiet system-dashboard.service
trap - ERR
printf 'Dashboard updated successfully.\nBackend backup: %s\nFrontend backup: %s\n' "$BACKEND_BACKUP" "$FRONTEND_BACKUP"
