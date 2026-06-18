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
# Auto-update attempt tracking (prevents infinite update loops)
# ---------------------------------------------------------------------------

def _update_state_path() -> Path:
    from web.env_paths import user_data_dir
    return user_data_dir() / "update_attempts.json"


def auto_update_attempts(target_version: str, current_version: str) -> int:
    """Return how many times we've already auto-attempted current -> target."""
    try:
        data = json.loads(_update_state_path().read_text())
    except Exception:
        return 0
    if data.get("target") == target_version and data.get("from") == current_version:
        try:
            return int(data.get("attempts", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def record_update_attempt(target_version: str, current_version: str) -> int:
    """Record an auto-update attempt for current -> target. Returns new count."""
    path = _update_state_path()
    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {}
    if data.get("target") != target_version or data.get("from") != current_version:
        data = {"target": target_version, "from": current_version, "attempts": 0}
    try:
        data["attempts"] = int(data.get("attempts", 0)) + 1
    except (TypeError, ValueError):
        data["attempts"] = 1
    data["last_attempt"] = time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except Exception:
        pass
    return int(data["attempts"])


def clear_update_attempts() -> None:
    """Forget any recorded auto-update attempts (call when already up to date)."""
    try:
        _update_state_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


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
            _RELEASES_URL + "?per_page=30",
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
        "changelog": _build_changelog(releases, current_version),
    }


def _build_changelog(
    releases: list[dict[str, Any]], current_version: str
) -> list[dict[str, str]]:
    """Build a changelog from releases newer than *current_version*.

    Each entry has keys: version, description, date.
    Falls back to commit messages via the compare API when release bodies
    are empty, then to per-tag commit message lookups.
    """
    current_ver = _parse_version(current_version)
    entries: list[tuple[tuple[int, ...], dict[str, str]]] = []

    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel.get("tag_name", "")
        ver = _parse_version(tag)
        if ver <= current_ver:
            continue
        version_str = tag.lstrip("v")
        body = (rel.get("body") or "").strip()
        # Strip body that is just the version tag (e.g. "v1.1.9")
        body_desc = _strip_version_prefix(body) if body else ""
        pub = (rel.get("published_at") or "")[:10]  # YYYY-MM-DD
        if body_desc:
            entries.append((ver, {"version": version_str, "description": body_desc, "date": pub}))
        else:
            # Release body is empty — try the release name (often the commit
            # subject, e.g. "v1.1.2: fix Chromium CDP...").
            name = rel.get("name", "")
            desc = _strip_version_prefix(name)
            entries.append((ver, {"version": version_str, "description": desc, "date": pub}))

    # If all descriptions are empty, try the compare API as a last resort.
    if entries and all(not e[1]["description"] for e in entries):
        latest_entry = max(entries, key=lambda t: t[0])
        compare_entries = _changelog_from_compare(
            f"v{current_version}",
            latest_entry[1]["version"],
        )
        if compare_entries:
            return compare_entries

    # If still all empty, try fetching each tag's commit message individually.
    if entries and all(not e[1]["description"] for e in entries):
        for ver_tuple, entry in entries:
            desc = _commit_message_for_tag(f"v{entry['version']}")
            if desc:
                entry["description"] = desc

    # Sort newest-first
    entries.sort(key=lambda t: t[0], reverse=True)
    return [e for _, e in entries]


def _strip_version_prefix(name: str) -> str:
    """'v1.1.2: fix foo' → 'fix foo'.  Returns '' for bare tags like 'v1.1.2'."""
    import re as _re
    # If name is purely a version tag (e.g. "v1.1.5"), return empty.
    if _re.fullmatch(r"v?\d+\.\d+(?:\.\d+)?", name.strip()):
        return ""
    m = _re.match(r"^v?\d+\.\d+(?:\.\d+)?[:\s]+(.+)", name)
    return m.group(1).strip() if m else name.strip()


def _commit_message_for_tag(tag: str) -> str:
    """Fetch the commit message for a specific tag via GitHub API."""
    url = f"https://api.github.com/repos/{_GITHUB_REPO}/git/ref/tags/{tag}"
    try:
        ctx = _ssl_context()
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            ref = json.loads(resp.read())
        # Lightweight tag → commit SHA directly; annotated tag → dereference
        obj = ref.get("object", {})
        sha_url = obj.get("url", "")
        if obj.get("type") == "tag":
            # Annotated tag — follow to the commit
            req2 = urllib.request.Request(sha_url, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req2, timeout=10, context=ctx) as resp2:
                tag_obj = json.loads(resp2.read())
            sha_url = tag_obj.get("object", {}).get("url", sha_url)
        req3 = urllib.request.Request(sha_url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req3, timeout=10, context=ctx) as resp3:
            commit = json.loads(resp3.read())
        msg = (commit.get("message", "") or "").split("\n")[0]
        return _strip_version_prefix(msg)
    except Exception:
        return ""


def _changelog_from_compare(
    base_tag: str, head_tag: str
) -> list[dict[str, str]]:
    """Fetch commit messages between two tags via the GitHub compare API."""
    compare_url = (
        f"https://api.github.com/repos/{_GITHUB_REPO}"
        f"/compare/{base_tag}...v{head_tag}"
    )
    try:
        ctx = _ssl_context()
        req = urllib.request.Request(
            compare_url,
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []

    entries: list[dict[str, str]] = []
    for commit in reversed(data.get("commits", [])):
        msg = (commit.get("commit", {}).get("message", "") or "").split("\n")[0]
        desc = _strip_version_prefix(msg)
        if desc:
            # Extract version from message if present
            import re as _re
            m = _re.match(r"^v?(\d+\.\d+(?:\.\d+)?)", msg)
            ver = m.group(1) if m else ""
            date_str = (commit.get("commit", {}).get("committer", {}).get("date", "") or "")[:10]
            entries.append({"version": ver, "description": desc, "date": date_str})
    return entries


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
    """Run the Inno Setup installer after the app exits, then relaunch it.

    A small hidden batch script waits for this process to exit, waits a short
    grace period so Windows releases file locks on the app directory (otherwise
    the in-place upgrade can fail and Inno Setup shows "reverting install"),
    runs the installer silently with logging, and relaunches the app.
    """
    if not exe_path.is_file():
        raise RuntimeError(f"Installer not found: {exe_path}")

    app_exe = Path(sys.executable)

    try:
        from web.env_paths import user_data_dir
        log_dir = user_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        log_dir = Path(tempfile.gettempdir())
    updater_log = log_dir / "windows_update.log"
    install_log = log_dir / "windows_install_inno.log"

    # Batch script:
    #   1. Wait for the running app (PID) to exit.
    #   2. Grace delay so the OS releases locks on the app files.
    #   3. Run the installer silently (with an Inno log for diagnostics).
    #   4. Relaunch the app (whatever version is now installed).
    script = tempfile.NamedTemporaryFile(
        mode="w", suffix=".cmd", delete=False, prefix="ea_update_",
    )
    script_path = script.name
    script.write(f"""@echo off
set "APP_PID={os.getpid()}"
set "INSTALLER={exe_path}"
set "APPEXE={app_exe}"
set "ULOG={updater_log}"

echo [%date% %time%] updater started, waiting for pid %APP_PID%>>"%ULOG%"

:wait_loop
tasklist /FI "PID eq %APP_PID%" /NH 2>NUL | find "%APP_PID%" >NUL 2>NUL
if not errorlevel 1 (
    ping -n 2 127.0.0.1 >NUL 2>NUL
    goto wait_loop
)

rem Grace period so Windows fully releases locks on the app files.
ping -n 4 127.0.0.1 >NUL 2>NUL

echo [%date% %time%] running installer>>"%ULOG%"
"%INSTALLER%" /SILENT /SUPPRESSMSGBOXES /NORESTART /LOG="{install_log}"
echo [%date% %time%] installer exit code %ERRORLEVEL%>>"%ULOG%"

if exist "%APPEXE%" (
    echo [%date% %time%] relaunching app>>"%ULOG%"
    start "" "%APPEXE%"
)
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
