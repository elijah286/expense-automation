# Standalone desktop builds

These scripts produce a self-contained **Expense Automator** app (no separate Python install). The macOS `.app` opens an **embedded window** (pywebview on the main thread, not Safari) on port 8080 — one Dock icon and no separate browser chrome. Set `EXPENSE_AUTOMATOR_USE_BROWSER=1` to open the system browser instead. `EXPENSE_AUTOMATOR_NATIVE=1` selects NiceGUI’s multiprocessing webview (often duplicate Dock icons).

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

After a full build, you can **iterate on the DMG background only** (regenerates `dmg_background.png` and re-runs `create-dmg`; ~seconds instead of a full PyInstaller run):

```bash
./packaging/quick_redmg.sh
```

Outputs:

- `dist/Expense Automator.app` — drag to Applications (custom icon in the Dock / Finder)
- `dist/Expense Automator.dmg` — illustrated “drag to Applications” disk image (Firefox-style night scene + arrow; see `packaging/generate_dmg_background.py`)

Playwright’s Chromium bundle is copied into `Contents/Resources/ms-playwright` (it is not embedded by PyInstaller; nested browser `.app` bundles break codesign).

### DMG layout (create-dmg)

The script **requires [create-dmg](https://github.com/create-dmg/create-dmg)** for the custom Finder background. If Homebrew is available, the build runs `brew install create-dmg` automatically. Otherwise install it yourself: `brew install create-dmg`.

If you cannot use `create-dmg`, set **`ALLOW_PLAIN_DMG=1`** to build a minimal DMG with `hdiutil` only (no illustrated background).

### CI: rebuild on every push to `main`

GitHub Actions workflow **`.github/workflows/macos-dmg.yml`** runs `./packaging/build_macos.sh` on each push to `main` and uploads **`Expense Automator.dmg`** as a workflow artifact (Actions → latest run → Artifacts).

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
