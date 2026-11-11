#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="$HOME/.config/discord-control-activity"
ENV_FILE="$CONFIG_DIR/env"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/discord-control-activity.service"
TEMPLATE="$APP_DIR/deploy/discord-control-activity.service.example"

mkdir -p "$CONFIG_DIR" "$UNIT_DIR"
chmod 700 "$CONFIG_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 "$APP_DIR/.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE from .env.example."
else
  chmod 600 "$ENV_FILE"
  echo "Keeping existing $ENV_FILE."
fi

python3 - "$TEMPLATE" "$UNIT_FILE" "$APP_DIR" "$ENV_FILE" <<'PY'
from pathlib import Path
import sys

template_path, output_path, app_dir, env_file = map(Path, sys.argv[1:])
text = template_path.read_text(encoding="utf-8")

def systemd_quote(value: str) -> str:
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'

lines = []
for line in text.splitlines():
    if line.startswith("WorkingDirectory="):
        line = "WorkingDirectory=" + systemd_quote(str(app_dir))
    elif line.startswith("EnvironmentFile="):
        line = "EnvironmentFile=" + systemd_quote(str(env_file))
    lines.append(line)

output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
output_path.chmod(0o600)
PY

systemctl --user daemon-reload
echo
echo "Installed the user service unit at:"
echo "  $UNIT_FILE"
echo
echo "Next steps:"
echo "  1. Edit $ENV_FILE and fill in Discord application ID/client secret, redirect URI, and a private LINKS_PIN."
echo "  2. Keep all secrets in that file; never commit it."
echo "  3. Check the app locally with: cd \"$APP_DIR\" && npm run check && npm test"
echo "  4. Start the service after configuration: systemctl --user enable --now discord-control-activity.service"
echo "  5. Inspect logs with: journalctl --user -u discord-control-activity.service -f"
