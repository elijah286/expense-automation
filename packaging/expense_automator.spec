# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: NiceGUI app + bundled Playwright Chromium (see packaging/build_*. scripts)."""

import sys
from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all

# Directory containing this .spec file (packaging/)
_SPEC_DIR = Path(SPEC).resolve().parent
PROJECT = _SPEC_DIR.parent
ICON_DIR = PROJECT / "packaging" / "icons"
ICON_MAC = ICON_DIR / "ExpenseAutomator.icns"
ICON_WIN = ICON_DIR / "ExpenseAutomator.ico"

datas: list[tuple[str, str]] = [
    (str(PROJECT / ".env.example"), "."),
    (str(PROJECT / "VERSION"), "."),
]
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = []

for pkg in (
    "playwright",
    "nicegui",
    "webview",
    "uvicorn",
    "starlette",
    "websockets",
    "httptools",
    "watchfiles",
    "keyring",
    "PIL",
    "openai",
    "dotenv",
    "dns",
    "jaraco",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Playwright Chromium is *not* bundled inside PyInstaller (nested .app bundles break codesign).
# build_macos.sh / build_windows.ps1 copy packaging/build/ms-playwright next to the app.

a = Analysis(
    [str(PROJECT / "web" / "__main__.py")],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

if IS_MAC and not ICON_MAC.is_file():
    raise SystemExit(
        f"Missing {ICON_MAC}. Run: python3 packaging/generate_icons.py"
    )
if IS_WIN and not ICON_WIN.is_file():
    raise SystemExit(
        f"Missing {ICON_WIN}. Run: python packaging/generate_icons.py"
    )

if IS_MAC:
    from PyInstaller.building.osx import BUNDLE

    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="ExpenseAutomator",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="ExpenseAutomator",
    )
    app = BUNDLE(
        coll,
        name="Expense Automator.app",
        icon=str(ICON_MAC),
        bundle_identifier="com.elijah286.expense-automator",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleDisplayName": "Expense Automator",
            "CFBundleName": "Expense Automator",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
        },
    )
elif IS_WIN:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="ExpenseAutomator",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ICON_WIN),
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="ExpenseAutomator",
    )
else:
    raise SystemExit(f"Unsupported platform for this spec: {sys.platform}")
