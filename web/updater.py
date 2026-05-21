"""In-app update checker and installer for Expense Automator.

Checks GitHub Releases for newer versions and, on macOS, can download
the DMG, mount it, replace the running .app bundle, and relaunch.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("expense_automator.updater")

_GITHUB_REPO = "elijah286/expense-automation"
_RELEASES_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/releases"


def _ssl_context() -> ssl.SSLContext:
    """Build an SSL context that works in frozen PyInstaller bundles on macOS."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    # Fallback: try the default context (works on non-frozen installs)
    return ssl.create_default_context()

# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> tuple[int, ...]:
    """Parse '1.0.20' → (1, 0, 20)."""
    parts: list[int] = []
    for p in v.lstrip("v").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


# ---------------------------------------------------------------------------
# Check for updates
# ---------------------------------------------------------------------------

def check_for_update(current_version: str) -> dict[str, Any] | None:
    """Return release info dict if a newer version is available, else None.

    Keys: version, tag, macos_url, windows_url, notes, published_at
    """
    try:
        ctx = _ssl_context()
        req = urllib.request.Request(
            _RELEASES_URL + "?per_page=10",
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            releases = json.loads(resp.read())
    except Exception as exc:
        log.warning("Update check failed: %s", exc)
        return None

    # Find the release with the highest version number
    best = None
    best_ver: tuple[int, ...] = (0,)
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel.get("tag_name", "")
        ver = _parse_version(tag)
        if ver > best_ver:
            best_ver = ver
            best = rel

    if not best:
        return None

    data = best
    tag = data.get("tag_name", "")
    latest = tag.lstrip("v")
    if not latest or not _is_newer(latest, current_version):
        return None

    macos_url = ""
    windows_url = ""
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        url = asset.get("browser_download_url", "")
        if name.endswith(".dmg"):
            macos_url = url
        elif name.endswith(".exe"):
            windows_url = url

    return {
        "version": latest,
        "tag": tag,
        "macos_url": macos_url,
        "windows_url": windows_url,
        "notes": data.get("body", ""),
        "published_at": data.get("published_at", ""),
    }


# ---------------------------------------------------------------------------
# Download with progress
# ---------------------------------------------------------------------------

def download_update(url: str, on_progress=None) -> Path:
    """Download the update asset to a temp file. Returns the path.

    on_progress(bytes_downloaded, total_bytes) is called periodically.
    """
    ctx = _ssl_context()
    req = urllib.request.Request(url)
    dest = Path(tempfile.mkdtemp()) / url.rsplit("/", 1)[-1]
    with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)  # 256 KB chunks
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, total)
    return dest


# ---------------------------------------------------------------------------
# macOS: apply update (mount DMG, copy .app, relaunch)
# ---------------------------------------------------------------------------

def _find_app_bundle() -> Path | None:
    """Find the running .app bundle path on macOS."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    # PyInstaller: .../Expense Automator.app/Contents/MacOS/expense_automator
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def apply_macos_update(dmg_path: Path) -> None:
    """Mount the DMG, copy the .app over the running one, and relaunch."""
    current_app = _find_app_bundle()
    if not current_app:
        raise RuntimeError("Cannot determine current .app bundle path")

    app_dir = current_app.parent  # e.g. /Applications/

    # Create an updater shell script that runs after this process exits.
    script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, prefix="ea_update_"
    )
    script_path = script.name

    script.write(f"""#!/bin/bash
set -e

APP_PID={os.getpid()}
DMG_PATH="{dmg_path}"
APP_DIR="{app_dir}"
CURRENT_APP="{current_app}"

# Wait for the app to quit (up to 30 seconds)
for i in $(seq 1 60); do
    if ! kill -0 "$APP_PID" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

# Mount the DMG
MOUNT_POINT=$(hdiutil attach "$DMG_PATH" -nobrowse -noautoopen -mountrandom /tmp 2>/dev/null | tail -1 | awk '{{print $NF}}')

if [ -z "$MOUNT_POINT" ]; then
    echo "Failed to mount DMG"
    exit 1
fi

# Find the .app inside the mounted volume
NEW_APP=$(find "$MOUNT_POINT" -maxdepth 1 -name "*.app" -print -quit)

if [ -z "$NEW_APP" ]; then
    hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true
    echo "No .app found in DMG"
    exit 1
fi

# Remove the old app and copy the new one
rm -rf "$CURRENT_APP"
cp -R "$NEW_APP" "$APP_DIR/"

# Unmount the DMG
hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true

# Clean up the DMG
rm -f "$DMG_PATH"

# Relaunch
NEW_APP_NAME=$(basename "$NEW_APP")
open "$APP_DIR/$NEW_APP_NAME"
""")
    script.close()
    os.chmod(script_path, 0o755)

    # Launch the updater script and exit the app
    subprocess.Popen(
        ["/bin/bash", script_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# Windows: apply update (run installer, exit app)
# ---------------------------------------------------------------------------

def apply_windows_update(exe_path: Path) -> None:
    """Launch the Inno Setup installer in silent mode and exit the running app.

    The installer will wait for this process to exit, then upgrade in place
    and relaunch the app.
    """
    if not exe_path.is_file():
        raise RuntimeError(f"Installer not found: {exe_path}")

    # Create a small batch script that:
    # 1. Waits for the current process to exit
    # 2. Runs the installer silently
    # 3. Cleans up
    script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".cmd", delete=False, prefix="ea_update_",
    )
    script_path = script.name
    script.write(f"""@echo off
set APP_PID={os.getpid()}
set INSTALLER={exe_path}

:wait_loop
tasklist /FI "PID eq %APP_PID%" /NH 2>NUL | findstr /I /C:"%APP_PID%" >NUL 2>NUL
if %ERRORLEVEL% EQU 0 (
    ping -n 2 127.0.0.1 >NUL 2>NUL
    goto wait_loop
)

start "" "%INSTALLER%" /SILENT /SUPPRESSMSGBOXES /NORESTART
del "%~f0"
""")
    script.close()

    # Launch the batch script completely hidden via wscript
    vbs = tempfile.NamedTemporaryFile(
        mode="w", suffix=".vbs", delete=False, prefix="ea_update_",
    )
    vbs_path = vbs.name
    vbs.write(f'CreateObject("WScript.Shell").Run """{script_path}""", 0, False\n')
    vbs.close()

    subprocess.Popen(
        ["wscript.exe", vbs_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
    )
