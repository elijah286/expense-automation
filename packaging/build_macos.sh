#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

pick_python() {
  local c
  for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3; do
    [[ -z "$c" ]] && continue
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

PY="$(pick_python)" || {
  echo "Python 3.10 or newer is required (nicegui / build). Install via https://www.python.org or Homebrew." >&2
  exit 1
}

VENV="${VENV:-$ROOT/.venv-build}"
if [[ -d "$VENV" ]] && ! "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "Removing $VENV (Python 3.10+ required for this project)." >&2
  rm -rf "$VENV"
fi
if [[ ! -d "$VENV" ]]; then
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

python -m pip install -q -U pip
python -m pip install -q -r requirements.txt -r requirements-build.txt

python "$ROOT/packaging/generate_icons.py"
python "$ROOT/packaging/generate_dmg_background.py"

# Chromium is downloaded on first launch (not bundled — saves ~500 MB).

rm -rf "$ROOT/build" "$ROOT/dist"

pyinstaller "$ROOT/packaging/expense_automator.spec" --noconfirm --clean

APP="$ROOT/dist/Expense Automator.app"
if [[ ! -d "$APP" ]]; then
  echo "Expected $APP" >&2
  exit 1
fi

DMG="$ROOT/dist/Expense Automator.dmg"
rm -f "$DMG"

# Illustrated DMG requires create-dmg (sets Finder background + icon positions).
if ! command -v create-dmg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing create-dmg (brew) for DMG background art..." >&2
    brew install create-dmg
  fi
fi
if ! command -v create-dmg >/dev/null 2>&1; then
  if [[ "${ALLOW_PLAIN_DMG:-}" == "1" ]]; then
    echo "ALLOW_PLAIN_DMG=1: building a plain DMG without background (install create-dmg for Firefox-style layout)." >&2
  else
    echo "ERROR: create-dmg is required for the illustrated DMG background." >&2
    echo "Install: brew install create-dmg" >&2
    echo "Or set ALLOW_PLAIN_DMG=1 to build a basic DMG without custom art." >&2
    exit 1
  fi
fi

# Staging folder: only the .app (create-dmg adds the Applications link itself).
DMG_STAGE="$ROOT/dist/.dmg-staging"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE"
cp -R "$APP" "$DMG_STAGE/"

if command -v create-dmg >/dev/null 2>&1; then
  # Window size must match packaging/dmg_background.png (900×520) and generate_dmg_background.py
  # Finder AppleScript can time out (-1712) when the system is busy; retry a few times.
  _attempt=1
  _max=4
  while [[ "$_attempt" -le "$_max" ]]; do
    rm -f "$DMG" 2>/dev/null || true
    shopt -s nullglob
    for _rw in "$ROOT"/dist/rw.*.dmg; do rm -f "$_rw"; done
    shopt -u nullglob
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
      echo "create-dmg failed after $_max attempts (Finder/AppleScript). Close other Disk Images, wait, and retry." >&2
      exit 1
    fi
    echo "create-dmg attempt $_attempt failed; retrying in 8s..." >&2
    sleep 8
    _attempt=$((_attempt + 1))
  done
else
  ln -sf /Applications "$DMG_STAGE/Applications"
  hdiutil create -volname "Expense Automator" -srcfolder "$DMG_STAGE" -ov -format UDZO "$DMG"
fi
rm -rf "$DMG_STAGE"

echo "Built: $APP"
echo "Disk image: $DMG"
