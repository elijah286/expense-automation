# Standalone desktop builds

These scripts produce a self-contained **Expense Automator** app (no separate Python install). The app is the same NiceGUI workflow as `python -m web`: it starts a local server on port 8080 and opens a **native window** (pywebview), not a separate browser tab. Set `EXPENSE_AUTOMATOR_USE_BROWSER=1` in the environment to use the system browser instead (e.g. for devtools).

## Prerequisites (build machine only)

- Python 3.10+ with pip
- Same OS as the target: build macOS artifacts on a Mac, Windows artifacts on Windows
- ~1 GB free disk for Chromium + PyInstaller output

Icons (`.icns` / `.ico`) are generated automatically by the build scripts from `packaging/generate_icons.py`. To regenerate only the icons:

```bash
./.venv-build/bin/python packaging/generate_icons.py   # or any env with Pillow
```

## macOS

From the repository root:

```bash
./packaging/build_macos.sh
```

Outputs:

- `dist/Expense Automator.app` — drag to Applications (custom icon in the Dock / Finder)
- `dist/Expense Automator.dmg` — compressed disk image for distribution (`create-dmg` if installed, otherwise `hdiutil`)

Playwright’s Chromium bundle is copied into `Contents/Resources/ms-playwright` (it is not embedded by PyInstaller; nested browser `.app` bundles break codesign).

The DMG uses **[create-dmg](https://github.com/create-dmg/create-dmg)** when installed (`brew install create-dmg`): custom **background** (arrow + “Drag to Applications…” text), **volume icon**, and an **Applications** drop link. Without `create-dmg`, the script falls back to `hdiutil` (symlink only, no background art).

First launch: macOS may show a security prompt for an unsigned app. Control-click → Open, or allow in **System Settings → Privacy & Security**.

## Windows

In PowerShell, from the repository root:

```powershell
.\packaging\build_windows.ps1
```

Outputs:

- `dist\ExpenseAutomator\` — folder containing `ExpenseAutomator.exe`, `ms-playwright\`, and dependencies (must be kept together)
- Run `iscc packaging\ExpenseAutomator.iss` (Inno Setup) to build `dist\ExpenseAutomator_Setup.exe` if you install [Inno Setup](https://jrsoftware.org/isinfo.php)

Build the Windows artifacts **on a Windows machine** (PyInstaller targets the host OS). macOS cannot produce a Windows `.exe`.

## Data and config

- Settings and caches: `~/.expense-automator` (same as the Python-from-source workflow).
- First run of a frozen build creates `~/.expense-automator/.env` from the bundled `.env.example` if missing.

## Signing & notarization (optional)

For wide distribution on macOS, sign and notarize the `.app` with your Apple Developer ID. The scripts here do not automate that.
