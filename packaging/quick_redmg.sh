#!/usr/bin/env bash
# Rebuild only the DMG (and background PNG) when dist/Expense Automator.app already exists.
# Use after ./packaging/build_macos.sh for faster iteration on dmg_background art or create-dmg flags.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP="$ROOT/dist/Expense Automator.app"
DMG="$ROOT/dist/Expense Automator.dmg"

if [[ ! -d "$APP" ]]; then
  echo "Missing $APP — run ./packaging/build_macos.sh first." >&2
  exit 1
fi

if ! command -v create-dmg >/dev/null 2>&1; then
  echo "create-dmg not found. brew install create-dmg" >&2
  exit 1
fi

"$ROOT/.venv-build/bin/python" "$ROOT/packaging/generate_dmg_background.py" 2>/dev/null || \
  python3 "$ROOT/packaging/generate_dmg_background.py"

DMG_STAGE="$ROOT/dist/.dmg-staging"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE"
cp -R "$APP" "$DMG_STAGE/"

rm -f "$DMG"
shopt -s nullglob
for _rw in "$ROOT"/dist/rw.*.dmg; do rm -f "$_rw"; done
shopt -u nullglob

_attempt=1
_max=4
while [[ "$_attempt" -le "$_max" ]]; do
  if create-dmg \
    --volname "Expense Automator" \
    --volicon "$ROOT/packaging/icons/ExpenseAutomator.icns" \
    --background "$ROOT/packaging/dmg_background.png" \
    --window-pos 200 120 \
    --window-size 900 520 \
    --icon-size 100 \
    --icon "Expense Automator.app" 200 238 \
    --hide-extension "Expense Automator.app" \
    --app-drop-link 700 238 \
    "$DMG" \
    "$DMG_STAGE"; then
    break
  fi
  if [[ "$_attempt" -eq "$_max" ]]; then
    echo "create-dmg failed after $_max attempts." >&2
    exit 1
  fi
  echo "create-dmg attempt $_attempt failed; retrying in 8s..." >&2
  sleep 8
  _attempt=$((_attempt + 1))
done

rm -rf "$DMG_STAGE"
echo "Wrote $DMG"
