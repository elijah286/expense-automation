# Expense Automator

Automates expense-report entry by driving a real browser against the legacy expense portal. Instead of tedious copy-paste, the tool scrapes your credit-card transactions, matches them to receipt photos, and fills everything in for you.

### How it works & what it does with your data

This app does **exactly what you would do by hand** — it opens a real browser window, logs into Oracle with your credentials, reads your credit-card transactions, and types values into the expense-report form. You can watch every step happen live on screen.

- **Passwords are stored in your system keychain** (macOS Keychain / Windows Credential Manager) — the same secure vault your OS uses for Wi-Fi passwords and website logins. They are **never** written to plain-text files, logs, or sent anywhere other than the Oracle login page itself.
- **Nothing is sent to a remote server.** The app runs 100% locally on your machine. The only external calls are (1) logging into Oracle (the same site you'd open in a normal browser) and (2) sending receipt images to the OpenAI API for amount/date extraction, if you enable that option.
- **The code is open-source** — you (or anyone you trust) can read every line to verify there is no data collection, telemetry, or hidden network calls.

---

## Quick Start — get the app running in ~5 minutes

### Prerequisites

You need two things installed before you begin:

| What | Why | How to install |
|------|-----|----------------|
| **Python 3.10+** | Runs the app | See Step 0 below |
| **Git** | Downloads the code | See Step 0 below |

### Step 0: Install Python and Git (skip if you already have them)

<details>
<summary><strong>Mac</strong></summary>

1. Open the **Terminal** app (press `Cmd + Space`, type `Terminal`, hit Enter).
2. Paste this line and press Enter — it installs Homebrew, a tool manager for Mac:
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   Follow any on-screen prompts (you may need to type your password).
3. Once Homebrew is installed, paste these two lines and press Enter:
   ```bash
   brew install python@3.12 git
   ```
4. Verify both are installed:
   ```bash
   python3 --version
   git --version
   ```
   You should see version numbers printed (e.g. `Python 3.12.x` and `git version 2.x.x`).

</details>

<details>
<summary><strong>Windows</strong></summary>

1. **Install Python**: go to https://www.python.org/downloads/ and click the big yellow **Download Python 3.12** button. Run the installer and **check the box "Add python.exe to PATH"** before clicking Install Now.
2. **Install Git**: go to https://git-scm.com/download/win and download the installer. Run it and accept all defaults.
3. Open **PowerShell** (press the Windows key, type `PowerShell`, hit Enter).
4. Verify both are installed:
   ```powershell
   python --version
   git --version
   ```
   You should see version numbers printed.

</details>

---

### Step 1: Download the code

Open a terminal (Mac: **Terminal**, Windows: **PowerShell**) and run:

```bash
git clone https://github.com/elijah286/oracle-expense-automation.git
cd oracle-expense-automation
```

---

### Step 2: Install dependencies

Copy and paste the commands for your OS. This creates an isolated environment and installs everything the app needs:

<details>
<summary><strong>Mac</strong></summary>

```bash
python3 -m venv .venv-rpa
source .venv-rpa/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

</details>

<details>
<summary><strong>Windows (PowerShell)</strong></summary>

```powershell
python -m venv .venv-rpa
.venv-rpa\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

> If you get an error about "execution policies", run this first and try again:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

</details>

You will know it worked when you see `(.venv-rpa)` at the beginning of your terminal prompt.

---

### Step 3: Launch the app

Make sure you still see `(.venv-rpa)` in your terminal. If you don't, re-activate the environment first (see Step 2). Then run:

```bash
python -m web
```

A browser window will automatically open to **http://localhost:8080**.

**On first launch the app will ask you to enter your credentials** — Oracle portal URL, username, password, and your OpenAI API key. The rest of the tool stays locked until you do this. Fill them in on the Settings page, click **Save Settings**, and you're ready to go.

> **Every time you come back later**, open a terminal, `cd` into the project folder, activate the environment, and run `python -m web`:
>
> Mac:
> ```bash
> cd oracle-expense-automation
> source .venv-rpa/bin/activate
> python -m web
> ```
>
> Windows:
> ```powershell
> cd oracle-expense-automation
> .venv-rpa\Scripts\Activate.ps1
> python -m web
> ```

---

## How to use the app

Once you've entered your credentials, the Dashboard unlocks and the workflow is:

1. **Scrape transactions** — the app pulls your credit-card lines from Oracle.
2. **Import & analyze receipts** — point the app at your receipt photos and it reads amounts/dates.
3. **Match & review** — the app suggests which receipt goes with which transaction; you approve or adjust.
4. **Create & submit the report** — one click fills out the expense report in the portal for you.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `python3: command not found` (Mac) | Re-run `brew install python@3.12` or use `python` instead of `python3`. |
| `python: command not found` (Windows) | Re-install Python and make sure **"Add python.exe to PATH"** is checked. |
| `(.venv-rpa)` disappeared from my prompt | You closed the terminal. Re-activate: `source .venv-rpa/bin/activate` (Mac) or `.venv-rpa\Scripts\Activate.ps1` (Windows). |
| `pip: command not found` | Make sure the virtual environment is activated (see above). |
| Port 8080 already in use | Another instance is running. Close it, or the app will handle it automatically. |
| macOS asks for permission to control Photos | Click **OK** — the app needs this to export receipt images from Apple Photos. |

---

## Notes

- App data (settings, browser profile) is stored in `~/.expense-automator`.
- The automated browser stays open after each step so you can watch and verify what it did before moving on — nothing happens behind the scenes.
