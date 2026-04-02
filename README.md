# Expense automator

Browser-backed automation for expense entry: a visible browser session drives the legacy expense portal (instead of raw desktop mouse movement only).

## 1) Setup

```bash
python3 -m venv .venv-rpa
source .venv-rpa/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

## 2) Configure

Edit `.env`:

- `LEGACY_URL`: page where you enter receipts
- `PLUS_SELECTOR`: CSS selector for the green plus button
- `RECEIPT_IMAGE_PATH`: optional direct receipt image path (if you already exported)
- `PHOTOS_ALBUM`: optional Apple Photos album name containing receipts
- `USE_PHOTOS_SELECTION`: set `true` to use current Photos app selection
- `INTERACTIVE_LOGIN`: pauses and asks you to login before continuing
- `INTERACTIVE_PHOTOS_SELECTION`: opens Photos and asks you to select receipts
- `PHOTOS_LIMIT`: number of photos to export per run (default `5`)
- `PHOTOS_EXPORT_DIR`: local staging folder for exported Photos files
- `LLM_REVIEW`: when `true`, sends receipt images to an LLM for amount matching
- `OPENAI_MODEL`: model used for image inspection (default `gpt-4.1-mini`)
- `OPENAI_API_KEY`: required for LLM receipt inspection
- `HEADLESS`: `false` keeps browser visible

## 3) Run

```bash
source .venv-rpa/bin/activate
python browser_automation.py
```

## Desktop UI (recommended)

Launch the Expense automator desktop app:

```bash
source .venv-rpa/bin/activate
python receipt_automation_ui.py
```

The UI includes:

- `1) Select Receipts`: opens Apple Photos, exports selected images, and runs LLM review
- `2) Login to Expense Report Tool`: opens the Oracle login page in browser
- `3) Assign Images`: lets you map imported images to expense-line labels (UX scaffold)
- `Settings`: stores URL/model/preferences and saves your OpenAI key in macOS keychain

Default flow now is:

1) load Oracle login page  
2) pause for your manual login  
3) prompt you to select receipts in Photos  
4) export selected photos and run LLM receipt amount matching  
5) print results and write a JSON report in `PHOTOS_EXPORT_DIR`

Use Apple Photos selection explicitly:

```bash
python browser_automation.py --use-photos-selection
```

Use a specific Apple Photos album:

```bash
python browser_automation.py --photos-album "Receipts"
```

If selector-based click is not possible yet, use fallback coordinates:

```bash
python browser_automation.py --url "https://your-legacy-app" --x 900 --y 430
```

## Notes

- App data lives in `~/.expense-automator` (settings, browser profile). If you used an older build that stored data in `~/.automated-expenses`, copy that folder to `~/.expense-automator` and re-save secrets in Settings if keychain entries do not appear (service name is now `expense-automator`).
- Selector click is more stable than coordinate click.
- macOS may ask permission for Terminal/Python to control Photos on first run.
- LLM review requires `OPENAI_API_KEY` in `.env`.
- The browser remains open after automation so you can verify each step.
- Press `Ctrl+C` to stop.
