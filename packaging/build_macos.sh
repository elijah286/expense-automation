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

BUNDLE_DIR="$ROOT/packaging/build/ms-playwright"
export PLAYWRIGHT_BROWSERS_PATH="$BUNDLE_DIR"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"
python -m playwright install chromium

rm -rf "$ROOT/build" "$ROOT/dist"

pyinstaller "$ROOT/packaging/expense_automator.spec" --noconfirm --clean

APP="$ROOT/dist/Expense Automator.app"
if [[ ! -d "$APP" ]]; then
  echo "Expected $APP" >&2
  exit 1
fi

# Chromium lives beside the bundle (PyInstaller cannot embed nested .app browsers — codesign).
RES="$APP/Contents/Resources"
mkdir -p "$RES"
rm -rf "$RES/ms-playwright"
cp -R "$BUNDLE_DIR" "$RES/ms-playwright"

DMG="$ROOT/dist/Expense Automator.dmg"
rm -f "$DMG"
if command -v create-dmg >/dev/null 2>&1; then
  create-dmg --volname "Expense Automator" "$DMG" "$ROOT/dist"
else
  echo "create-dmg not found; building a plain DMG with hdiutil"
  TMP_DMG="$ROOT/dist/.tmp-dmg"
  rm -rf "$TMP_DMG"
  mkdir -p "$TMP_DMG"
  cp -R "$APP" "$TMP_DMG/"
  hdiutil create -volname "Expense Automator" -srcfolder "$TMP_DMG" -ov -format UDZO "$DMG"
  rm -rf "$TMP_DMG"
fi

echo "Built: $APP"
echo "Disk image: $DMG"
