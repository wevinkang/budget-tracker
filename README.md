# Budget Tracker

A secure, local-first personal finance tracker built with Python and Flask — a fully self-hosted replacement for budgeting spreadsheets that runs on your own machine.

Built because spreadsheets don't auto-import bank transactions, aren't encrypted, and depend on Google to access.

---

## Features

- **Transactions** — full transaction ledger with filtering by month, category, expense type, and keyword search; inline edit/delete and CSV export
- **Auto-import** — drop a CSV (or `.xls`/`.xlsx`) from your bank into a watched folder and transactions are parsed, categorized, deduplicated, and imported automatically
- **Multi-bank parsing** — auto-detects the bank from the file's columns; falls back to a generic column-inference parser for banks it doesn't recognize
- **Smart categorization** — keyword engine maps merchant names to spending categories (Groceries, Transportation, Entertainment, …) and classifies each as **Need** or **Want**
- **Merchant rules** — pin a merchant to a category, mark it as reimbursable, or skip it entirely; rules can be re-applied retroactively to existing transactions
- **Budgets** — set a monthly limit per category and track **Budget vs. Actual** on the Reports page
- **Reimbursements** — flag transactions (or whole merchants) as reimbursable so they don't distort spending totals
- **Net Income** — monthly income-vs-expense summary with savings rate
- **Reports** — interactive pie chart, per-category breakdown, and drill-down into the transactions behind any category (with edit/delete in place)
- **Encrypted database** — SQLCipher AES-256 encryption on the SQLite file
- **PWA / mobile** — installable web app with a mobile bottom-nav layout
- **Remote access** — reachable from other devices over Tailscale with a web login

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask |
| Database | SQLite via SQLCipher (AES-256 encrypted) |
| Frontend | Jinja2, Chart.js, custom CSS (HUD theme) |
| CSV/Excel parsing | Python `csv`, `openpyxl`, `xlrd` |
| File watching | Watchdog |
| Packaging | Docker / Docker Compose |
| Networking | Tailscale (WireGuard) |

---

## Supported Banks

The importer auto-detects the bank from the CSV header (or, for headerless TD exports, from the data shape):

| Bank | Layout |
|---|---|
| **Amex** | `Date, Description, Amount` (slim or detailed export) |
| **TD** | `Date, Transaction, Debit, Credit, Balance` (no header) |
| **Simplii** | `Date, Transaction, Funds Out, Funds In, Balance` |
| **Scotiabank** | `Filter, Date, Description, Sub-description, Type, Amount, Balance` |
| **BMO** | `Item #, Card #, Transaction Date, Posting Date, Amount, Description` |
| **Rogers Bank** | full credit-card export with merchant + rewards columns |
| **Questrade** | activity export (records positive Net Amount as Investment Income) |
| **Generic** | any other layout — columns inferred automatically from the data |

Don't see your bank? It will usually still import via the generic parser. To add a first-class adapter, see [Extending](#extending).

---

## Security

Security was a first-class concern:

- **At rest** — `budget.db` is encrypted with SQLCipher (AES-256); the file is unreadable without the password.
- **In transit** — remote access goes through Tailscale (WireGuard), encrypted end-to-end.
- **Access control** — web login required on all routes; session invalidated on logout.
- **No cloud dependency** — data never leaves your machine.

| Threat | Mitigation |
|---|---|
| Stolen disk / DB file | SQLCipher encryption |
| Unauthorized network access | Tailscale private network |
| Unauthorized browser access | Web login with session auth |

> **Note:** The Flask `secret_key` is currently a hardcoded constant. For a public-facing deployment, move it to an environment variable.

---

## Setup

You can run it directly with Python, or in Docker.

### Option A — Bare metal (development)

**Requirements:** Python 3.10+, Linux (tested on Linux Mint / Raspberry Pi OS)

```bash
# 1. Install system dependencies (SQLCipher native lib)
sudo apt install python3-venv libsqlcipher-dev

# 2. Clone
git clone https://github.com/wevinkang/budget-tracker.git
cd budget-tracker

# 3. Run — creates the venv and installs dependencies on first run
bash start.sh
```

On first run you're prompted to set a password. This password **encrypts the database** and is also the **web login** password. There is no recovery — if you lose it, the data is unreadable, so store it somewhere safe.

The app starts at <http://localhost:5000>.

### Option B — Docker (recommended for an always-on server / Raspberry Pi)

```bash
# 1. Clone
git clone https://github.com/wevinkang/budget-tracker.git
cd budget-tracker

# 2. Create a .env with your password (used for DB encryption + login)
echo "BUDGET_PASSWORD=choose-a-strong-password" > .env

# 3. Create the host data + import folders (these are bind-mounted)
mkdir -p ~/budget-data ~/budget-imports

# 4. Edit docker-compose.yml so the volume paths point at the folders
#    you just created (the committed paths are absolute, e.g. /home/wev/...),
#    then build and start:
docker compose up -d --build
```

The app listens on port `5000`. Persisted data lives in the `budget-data` volume; drop bank files into `budget-imports` to auto-import.

**Environment variables**

| Variable | Purpose | Default |
|---|---|---|
| `BUDGET_PASSWORD` | DB encryption + web login password (required in Docker, since there's no interactive prompt) | — |
| `BUDGET_DB` | Path to the database file | `./budget.db` (Docker: `/data/budget.db`) |
| `BUDGET_IMPORTS` | Watched import folder | `~/budget-imports` (Docker: `/imports`) |

### Deploying to a Raspberry Pi

`deploy.sh` rsyncs the working tree to a Pi and rebuilds the container over SSH. Point it at your host and run it:

```bash
PI_HOST="user@raspberrypi.local" bash deploy.sh
```

It excludes the database, `.env`, and the venv, so the Pi keeps its own copy of each.

---

## Usage

### Auto-importing bank transactions
Drop any supported bank file (`.csv`, `.xls`, `.xlsx`) into your import folder (`~/budget-imports` by default). The watcher detects it, identifies the bank, deduplicates against existing transactions, imports, and moves the file to `…/done/`.

### Manual import
**Import → Upload Bank CSV** uploads a file directly through the browser.

### Importing existing data
**Import → Import Transactions Export** accepts a CSV with columns:
```
Date, Account, Amount, Notes, Expense Type, Month, Bank
```

### Budgets
On **Reports**, set a monthly limit per category to see Budget vs. Actual for the selected month.

---

## Project Structure

```
budget-tracker/
├── app.py              # Flask app and all routes
├── db.py               # SQLite/SQLCipher layer + schema migrations
├── importer.py         # Bank CSV/Excel parsers + categorization engine
├── watcher.py          # Folder-watcher daemon for auto-import
├── start.sh            # One-command bare-metal startup
├── deploy.sh           # rsync + rebuild deploy to a remote Pi
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── templates/          # Jinja2 HTML templates
├── static/             # CSS, PWA manifest, service worker, icons
└── k8s/                # Kubernetes manifests (optional)
```

---

## Extending

**Add a new bank:** implement a `parse_<bank>(row)` function in `importer.py` following the existing parsers, register it in the `parsers` dict inside `import_csv_string()`, and add a detection case to `detect_bank()`.

**Add a category:** add keywords to `CATEGORY_MAP`, add the category to `DEFAULT_ACCOUNTS` in `db.py`, and optionally set a Need/Want classification in `NEED_WANT_MAP` (both in `importer.py`).

---

## License

MIT
