# Budget Tracker

A secure, local-first personal finance tracker built with Python and Flask — replacing Google Sheets with a fully self-hosted web app that runs on your own machine.

Built because spreadsheets don't auto-import bank transactions, can't encrypt your data, and require a Google account to access.

---

## Features

- **Journal** — full transaction ledger with filtering by month, category, expense type, and keyword search
- **Auto-import** — drop a CSV from your bank into a watched folder and transactions are parsed, categorized, and imported automatically
- **Smart categorization** — keyword-based engine maps merchant names to spending categories (Groceries, Transportation, Entertainment, etc.) and classifies them as Need or Want
- **Net Income** — monthly income vs. expense summary with savings rate
- **Reports** — interactive pie chart and category breakdown per month
- **Encrypted database** — SQLCipher AES-256 encryption on the SQLite database file
- **Multi-device access** — accessible from other devices over Tailscale with a web login

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask |
| Database | SQLite via SQLCipher (AES-256 encrypted) |
| Frontend | Jinja2, Bootstrap 5, Chart.js |
| CSV parsing | Python `csv` module, custom bank adapters |
| File watching | Watchdog |
| Networking | Tailscale (WireGuard) |

---

## Supported Banks

Auto-detection from CSV header rows:

- **Amex** — `Date, Description, Amount`
- **TD** — `Date, Transaction, Debit, Credit, Balance`
- **Simplii** — `Date, Transaction, Funds Out, Funds In, Balance`

Other banks can be added by extending the parser in `importer.py`.

---

## Security

Security was a first-class concern in this project:

- **At rest** — `budget.db` is encrypted with SQLCipher (AES-256). The file is unreadable without the password — verified via `strings` showing only binary noise
- **In transit** — all remote access goes through Tailscale (WireGuard), encrypted end-to-end
- **Access control** — web login required for all routes; session cookie invalidated on logout
- **No cloud dependency** — data never leaves your machine

| Threat | Mitigation |
|---|---|
| Stolen hard drive / DB file | SQLCipher encryption |
| Unauthorized network access | Tailscale private network |
| Unauthorized browser access | Web login with session auth |
| Physical access to unlocked machine | OS screen lock |

---

## Setup

**Requirements:** Python 3.10+, Linux (tested on Linux Mint)

```bash
# 1. Install system dependencies
sudo apt install python3-venv libsqlcipher-dev

# 2. Clone the repo
git clone https://github.com/YOUR_USERNAME/budget-tracker.git
cd budget-tracker

# 3. Run — creates venv and installs dependencies automatically
bash start.sh
```

On first run you will be prompted to set a password. This password encrypts the database and is used for the web login.

---

## Usage

### Importing existing data

Go to **Import → Import Google Sheets Journal Export** and upload a CSV with columns:
```
Date, Account, Amount, Notes, Expense Type, Month, Bank
```

### Auto-importing bank transactions

Drop any supported bank CSV into `~/budget-imports/`. The folder watcher detects it, identifies the bank, deduplicates against existing transactions, and imports automatically. Processed files move to `~/budget-imports/done/`.

### Manual import

Go to **Import → Upload Bank CSV** and upload directly through the browser.

---

## Project Structure

```
budget-tracker/
├── app.py          # Flask app and all routes
├── db.py           # SQLite/SQLCipher database layer
├── importer.py     # Bank CSV parser and categorization engine
├── watcher.py      # Folder watcher daemon
├── start.sh        # One-command startup script
├── requirements.txt
├── templates/      # Jinja2 HTML templates
└── static/         # CSS
```

---

## Extending

**Add a new bank:** implement a `parse_bankname(row)` function in `importer.py` following the existing pattern, and add a detection case to `detect_bank()`.

**Add a category:** add keywords to `CATEGORY_MAP` and optionally a Need/Want classification to `NEED_WANT_MAP` in `importer.py`.

---

## License

MIT
