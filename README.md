# Expense Automator

Automates expense-report entry by driving a real browser against the legacy expense portal. Instead of tedious copy-paste, the tool scrapes your credit-card transactions, matches them to receipt photos, and fills everything in for you.

## Download

| Platform | Installer |
|----------|-----------|
| **macOS** | [Expense Automator.dmg](https://github.com/elijah286/expense-automation/releases/latest/download/Expense.Automator.dmg) |
| **Windows** | [ExpenseAutomator_Setup.exe](https://github.com/elijah286/expense-automation/releases/latest/download/ExpenseAutomator_Setup.exe) |

---

### How it works & what it does with your data

This app does **exactly what you would do by hand** — it opens a real browser window, logs into Oracle with your credentials, reads your credit-card transactions, and types values into the expense-report form. You can watch every step happen live on screen.

- **Passwords are stored in your system keychain** (macOS Keychain / Windows Credential Manager) — the same secure vault your OS uses for Wi-Fi passwords and website logins. They are **never** written to plain-text files, logs, or sent anywhere other than the Oracle login page itself.
- **The app runs locally on your machine.** External calls include logging into Oracle (the same site you'd open in a normal browser) and using the OpenAI API for enabled AI features, such as receipt extraction, matching, and merchant expense-type selection.
- **The code is open-source** — you (or anyone you trust) can read every line to verify there is no data collection, telemetry, or hidden network calls.

---

## Vendor classification

In **Classification**, click **AI pick** beside a merchant's expense-type dropdown
to choose and save the best available option, even if a type is already assigned.
This sends only the merchant name and available dropdown options to OpenAI, using
the API key, model, and TLS configuration in Settings. The button shows a loading
indicator while working. If the request fails or returns an unavailable type,
the existing selection is kept and an error is shown. You can always override
the result manually with the dropdown. Saved choices apply to that exact merchant.

Use **AI pick all** at the top to classify every merchant, including existing
selections and merchants hidden by search. Progress and a final updated/failed
count are shown. Merchants are processed one at a time; failed picks keep their
previous types while the remaining merchants continue. Error details appear in
Terminal. Dropdowns and AI buttons are disabled during the bulk run.

## Submission history

After submitting a report in Oracle, choose **Mark as Submitted** in the app.
This saves a permanent, read-only snapshot in **History**, including the report's
expenses, classifications, matching data, automation results, and independent
copies of attached receipts. Cash and mileage entries confirmed by automation
are included; unsubmitted local drafts are not.

- Search the submissions table by report name, submission date, expense, or receipt.
- Open a report to search its expenses and extracted receipt metadata, view expense
  details, and open receipt images (with zoom) or full, multi-page PDFs.
- History remains unchanged if you edit, restore, or delete active reports or
  original receipts. Restoring and re-submitting a report creates another snapshot.
- The existing five-day restore window in Settings still applies to **active**
  data cleanup, not History. **Keep receipt documents** keeps originals available
  for reuse; History always keeps its own copies.
- Reports still in the old restore window are recovered automatically before
  cleanup. Previously purged reports cannot be recovered; missing receipts in
  recovered reports are flagged explicitly.

History is stored locally under `~/.expense-automator/submission_history/` with no
automatic expiry. Include this folder in your backups. **Reset to Defaults**
deletes History along with all other app data.

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
git clone https://github.com/elijah286/expense-automation.git
cd expense-automation
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

When you run from source (`python -m web`), the UI opens in your **default browser** by default. On the **macOS `.app` build**, the UI opens in an **embedded window** (not Safari): one Dock icon and the window is driven by the app’s main thread so Activity Monitor should stay responsive. To use Safari with the installed app instead, set `EXPENSE_AUTOMATOR_USE_BROWSER=1`. For an alternate embedded mode from source, set `EXPENSE_AUTOMATOR_EMBEDDED=1`. `EXPENSE_AUTOMATOR_NATIVE=1` uses NiceGUI’s built-in pywebview path (an extra process and often duplicate Dock icons).

**On first launch, fill in Settings** — Oracle portal URL, approver display name, and your OpenAI API key are required. Oracle username and password are optional: save them to your local system keychain to sign in automatically for transaction scraping and report submission. Enable **Secure storage** first if prompted, then click **Save Settings**. Oracle credentials are never saved in a plain-text settings file; a keychain failure is reported rather than falling back to a file.

Saved sign-in is tried **once per browser launch**, only on the saved HTTPS portal origin. Redirected SSO on a different origin, unsupported login forms, and MFA still require you to sign in in Chromium; automation continues once the Oracle session is detected. No stored sign-in is required to use the workflow.

If Oracle rejects the saved login, reports an expired password, or locks the account, the app notifies you and pauses automatic sign-in across launches to avoid repeated bad-password attempts. You can finish signing in manually for the current run. After corporate password rotation, enter the current password in **Settings** and save to re-enable automatic login. A blank password keeps the existing one for the same account and portal; changing either requires a new password. **Forget saved sign-in** removes the Oracle credentials without affecting your API key. Factory reset removes both.

> **Every time you come back later**, open a terminal, `cd` into the project folder, activate the environment, and run `python -m web`:
>
> Mac:
> ```bash
> cd expense-automation
> source .venv-rpa/bin/activate
> python -m web
> ```
>
> Windows:
> ```powershell
> cd expense-automation
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

In the Matching receipt panel, outlined **Accept** and **Reject** buttons are actions,
not selected states. Accepting changes the button to **Accepted** and updates approval
counts without navigating away, advancing selection, or resetting the receipt preview.
Use **Unapprove** to undo acceptance. Rejecting clears the match and shows **Rejected**
while keeping the transaction selected. The **A** / **R** shortcuts use the same behavior.

### Optional cash and mileage expenses

- Use **Cash** in the left sidebar to add, edit, or delete a manual expense with its
  date, merchant/payee, amount, currency, expense type, and description/business purpose.
  Non-USD cash also requires the actual exchange rate in **USD per 1 receipt
  currency**. Oracle does not always supply a rate; the app never invents one.
  Existing non-USD drafts without a rate must be edited before inclusion.
- Use **Mileage** for a trip date, miles (up to three decimal places), and business
  purpose. The app automatically calculates a USD estimate using the
  [US IRS business mileage rate](https://www.irs.gov/tax-professionals/standard-mileage-rates)
  for the trip date, including midyear rate changes. Verified periods currently cover
  2024-2026; unsupported dates show an error instead of guessing a rate. This is an
  IRS estimate, **not yet a verified employer/Oracle reimbursement rate**.
- Either type can be assigned to an existing report or left unassigned. Create a
  report from Transactions if needed. Drafts persist locally in
  `~/.expense-automator/manual_expenses.json`, separate from scraped transactions,
  receipt matching, and credit-card submission data.
- The Submit screen lets you independently opt into **cash** and **mileage** for
  the report. Both default to off; **Submit credit-card items only** preserves the
  existing workflow without reading or entering manual drafts.
  Selected drafts are added to Step 4's Receipt-Based Expenses / Mileage Expenses
  tables, saved, and read back for verification. Cash uses an exact Oracle expense
  type and receipt currency; merchant/payee is saved in Details' Merchant Name
  field and included in the justification. Mileage sends the trip distance in
  Miles and uses **Details → Calculate Amount → Return** before saving, leaving
  the rate and reimbursement to Oracle rather than copying the local IRS estimate.
  Saved cash conversion and mileage reimbursement are checked against the amount/
  distance and rate, including USD cent rounding.
  Justifications include a stable `[EA:...]` draft identifier to prevent duplicate
  entry on retries. Existing identified rows are verified, not overwritten.
  If you edit a draft already entered in Oracle, reconcile the existing Oracle row
  before retrying; automation will not silently overwrite it.
- Optional-entry errors are logged and returned in the submission result, which is
  retained in the local submission state. Newly entered failing rows are removed
  and cleanup is verified so credit-card processing can continue. If cleanup cannot
  be verified, automation pauses with the browser open rather than risking a partial
  or incorrect claim. Final submission always remains a user action in Oracle.
  Drafts remain saved even when their report is removed or marked submitted; use
  their Cash/Mileage screen to reassign or delete them.
- Cash save/read-back (including Merchant Name), mileage calculation/save, and
  removal/save cleanup have been verified against the live Step 4 page.
  Required-field layouts were inspected for all 18 available cash expense types,
  and temporary cash/mileage entries reached Allocations and Review. Foreign-currency
  entry was verified with an explicit exchange rate. Step 4 recovery checks the
  actual destination, not just whether Next was clicked.
  Oracle Review errors are surfaced as warnings, not hidden by a successful
  navigation. Employer policy can still block final submission: for example,
  Oracle rejects cash-only reports while older credit-card transactions remain
  outstanding. Cash/mileage automation supplements a ready credit-card report;
  final approval and policy acceptance cannot be guaranteed by field entry.
  Field discovery uses rendered table headers and dropdown labels, not Oracle's
  session-encoded option values. A portal with
  different fields or mandatory Details data will produce an explicit warning
  instead of guessing. Cash/mileage currently supplement a ready credit-card report.
- Both screens are optional. Skipping them leaves the existing credit-card scraping,
  matching, and submission workflow unchanged.

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
