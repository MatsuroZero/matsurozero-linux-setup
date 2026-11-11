#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js is not installed."
  exit 1
fi

node -e 'const major = Number(process.versions.node.split(".")[0]); if (major < 22) { console.error("ERROR: Node.js 22 or newer is required (found " + process.versions.node + ")."); process.exit(1); } console.log("Node.js " + process.versions.node + " ✓");'

echo
echo "== JavaScript syntax =="
npm run check

echo
echo "== Installer shell syntax =="
bash -n deploy/install-user-service.sh
echo "Installer syntax OK ✓"

echo
echo "== Unit and API tests =="
npm test

echo
echo "Preflight passed. This validates local code/tests only; it does not configure Discord HTTPS mapping or prove an Activity launch."
