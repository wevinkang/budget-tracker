import os
import sqlite3 as _plain_sqlite3
from pathlib import Path
from sqlcipher3 import dbapi2 as sqlite3

DB_PATH = Path(os.environ.get('BUDGET_DB', Path(__file__).parent / 'budget.db'))

_PASSWORD = None


def set_password(pwd: str):
    global _PASSWORD
    _PASSWORD = pwd


def _pragma_key(conn):
    """Apply the encryption key — must be the first statement on a connection."""
    conn.execute(f'PRAGMA key="{_PASSWORD}"')

INCOME_CATEGORIES = {'Paycheck Income', 'Tax Income', 'Refund Income', 'Income'}

DEFAULT_ACCOUNTS = [
    'Paycheck Income', 'Tax Income', 'Refund Income', 'Income',
    'Groceries expense', 'Food expense', 'Drinking expense', 'Travel expense',
    'Transportation expense', 'Car expense', 'Entertainment expense',
    'Shopping expense', 'Health expense', 'Personal Care expense',
    'Home expense', 'Bill expense', 'Educational expense',
    'Gift expense', 'Date expense', 'Munchkin expense',
    'Credit Card expense', 'Interest expense', 'Loans expense',
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    _pragma_key(conn)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def migrate_plaintext_to_encrypted():
    """
    If budget.db exists as a plain (unencrypted) SQLite file, convert it
    to SQLCipher in-place. Called once at startup before set_password().
    """
    if not DB_PATH.exists():
        return  # nothing to migrate

    # Check if it's already encrypted by trying to open without a key
    try:
        conn = _plain_sqlite3.connect(DB_PATH)
        conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
        conn.close()
        is_plain = True
    except Exception:
        is_plain = False

    if not is_plain:
        return  # already encrypted, nothing to do

    print('Migrating existing budget.db to encrypted format...')
    backup = DB_PATH.with_suffix('.db.bak')
    tmp = DB_PATH.with_suffix('.db.tmp')
    import shutil
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(DB_PATH, backup)

    # Read everything from the plaintext DB
    src = _plain_sqlite3.connect(DB_PATH)
    src.row_factory = _plain_sqlite3.Row

    tables = src.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    table_data = {}
    for t in tables:
        rows = src.execute(f'SELECT * FROM "{t["name"]}"').fetchall()
        table_data[t['name']] = (t['sql'], [tuple(r) for r in rows])
    src.close()

    # Write to a temp encrypted file, then replace
    dst = sqlite3.connect(str(tmp))
    dst.execute(f'PRAGMA key="{_PASSWORD}"')
    for name, (create_sql, rows) in table_data.items():
        dst.execute(create_sql)
        if rows:
            placeholders = ','.join('?' * len(rows[0]))
            dst.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
    dst.commit()
    dst.close()

    DB_PATH.unlink()
    tmp.rename(DB_PATH)
    print(f'Migration complete. Plaintext backup saved to {backup}')
    print('You can delete the backup once you confirm everything works.')


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT NOT NULL,
            account      TEXT,
            amount       REAL NOT NULL,
            notes        TEXT,
            expense_type TEXT,
            month        TEXT,
            bank         TEXT,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS import_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT,
            bank        TEXT,
            imported    INTEGER,
            skipped     INTEGER,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS merchant_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern    TEXT UNIQUE NOT NULL,
            category   TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


def seed_accounts():
    conn = get_db()
    for name in DEFAULT_ACCOUNTS:
        conn.execute('INSERT OR IGNORE INTO accounts (name) VALUES (?)', (name,))
    conn.commit()
    conn.close()


# ── Transactions ─────────────────────────────────────────────

def _txn_where(month, account, expense_type, search):
    clause, params = 'WHERE 1=1', []
    if month:
        clause += ' AND month = ?'; params.append(month)
    if account:
        clause += ' AND account = ?'; params.append(account)
    if expense_type:
        clause += ' AND expense_type = ?'; params.append(expense_type)
    if search:
        clause += ' AND (notes LIKE ? OR account LIKE ?)'
        params += [f'%{search}%', f'%{search}%']
    return clause, params


def count_transactions(month='', account='', expense_type='', search=''):
    clause, params = _txn_where(month, account, expense_type, search)
    conn = get_db()
    n = conn.execute(f'SELECT COUNT(*) FROM transactions {clause}', params).fetchone()[0]
    conn.close()
    return n


def get_transactions(month='', account='', expense_type='', search='', limit=None, offset=0):
    clause, params = _txn_where(month, account, expense_type, search)
    query = f'SELECT * FROM transactions {clause} ORDER BY date DESC, id DESC'
    if limit:
        query += ' LIMIT ? OFFSET ?'
        params += [limit, offset]
    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def get_transaction(id):
    conn = get_db()
    row = conn.execute('SELECT * FROM transactions WHERE id=?', (id,)).fetchone()
    conn.close()
    return row


def add_transaction(data):
    conn = get_db()
    conn.execute(
        'INSERT INTO transactions (date, account, amount, notes, expense_type, month, bank) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (data['date'], data['account'], data['amount'], data['notes'],
         data['expense_type'], data['month'], data.get('bank', ''))
    )
    conn.commit()
    conn.close()


def update_transaction(id, data):
    conn = get_db()
    conn.execute(
        'UPDATE transactions SET date=?, account=?, amount=?, notes=?, '
        'expense_type=?, month=?, bank=? WHERE id=?',
        (data['date'], data['account'], data['amount'], data['notes'],
         data['expense_type'], data['month'], data.get('bank', ''), id)
    )
    conn.commit()
    conn.close()


def delete_transaction(id):
    conn = get_db()
    conn.execute('DELETE FROM transactions WHERE id=?', (id,))
    conn.commit()
    conn.close()


def get_dedupe_keys():
    conn = get_db()
    rows = conn.execute('SELECT date, notes, amount FROM transactions').fetchall()
    conn.close()
    return {f"{r['date']}|{r['notes']}|{r['amount']}" for r in rows}


# ── Accounts ─────────────────────────────────────────────────

def get_accounts():
    conn = get_db()
    rows = conn.execute('SELECT * FROM accounts ORDER BY name').fetchall()
    conn.close()
    return rows


def add_account(name):
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO accounts (name) VALUES (?)', (name,))
    conn.commit()
    conn.close()


def delete_account(id):
    conn = get_db()
    conn.execute('DELETE FROM accounts WHERE id=?', (id,))
    conn.commit()
    conn.close()


# ── Month helpers ─────────────────────────────────────────────

_MONTH_ORDER = (
    'January February March April May June '
    'July August September October November December'
).split()


def _month_sort_key(m):
    try:
        return _MONTH_ORDER.index(m)
    except ValueError:
        return 99


def get_months():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT month FROM transactions WHERE month != '' AND month IS NOT NULL"
    ).fetchall()
    conn.close()
    months = [r['month'] for r in rows if r['month']]
    return sorted(months, key=_month_sort_key)


# ── Net Income ────────────────────────────────────────────────

def get_net_income_summary():
    conn = get_db()
    months = get_months()
    income_placeholders = ','.join('?' * len(INCOME_CATEGORIES))
    income_list = list(INCOME_CATEGORIES)

    summary = []
    total_income = 0.0
    total_expenses = 0.0

    for month in months:
        income = conn.execute(
            f'SELECT COALESCE(SUM(amount),0) FROM transactions '
            f'WHERE month=? AND account IN ({income_placeholders})',
            [month] + income_list
        ).fetchone()[0]

        expenses = conn.execute(
            f'SELECT COALESCE(SUM(amount),0) FROM transactions '
            f'WHERE month=? AND account NOT IN ({income_placeholders}) AND account != ""',
            [month] + income_list
        ).fetchone()[0]

        net = income - expenses
        total_income += income
        total_expenses += expenses
        summary.append({
            'month': month,
            'income': income,
            'expenses': expenses,
            'net': net,
            'savings_rate': (net / income * 100) if income > 0 else 0,
        })

    conn.close()

    net_total = total_income - total_expenses
    summary.append({
        'month': 'Total',
        'income': total_income,
        'expenses': total_expenses,
        'net': net_total,
        'savings_rate': (net_total / total_income * 100) if total_income > 0 else 0,
        'is_total': True,
    })
    return summary


# ── Reports ───────────────────────────────────────────────────

def get_category_report(month):
    conn = get_db()
    income_placeholders = ','.join('?' * len(INCOME_CATEGORIES))
    income_list = list(INCOME_CATEGORIES)
    rows = conn.execute(
        f'SELECT account, SUM(amount) as total, COUNT(*) as count '
        f'FROM transactions '
        f'WHERE month=? AND account NOT IN ({income_placeholders}) AND account != "" '
        f'GROUP BY account ORDER BY total DESC',
        [month] + income_list
    ).fetchall()
    conn.close()

    total = sum(r['total'] for r in rows)
    return [
        {
            'account': r['account'],
            'total': r['total'],
            'count': r['count'],
            'pct': (r['total'] / total * 100) if total > 0 else 0,
        }
        for r in rows
    ]


def get_need_want_report(month):
    conn = get_db()
    income_placeholders = ','.join('?' * len(INCOME_CATEGORIES))
    income_list = list(INCOME_CATEGORIES)
    rows = conn.execute(
        f"SELECT expense_type, SUM(amount) as total, COUNT(*) as count "
        f"FROM transactions "
        f"WHERE month=? AND account NOT IN ({income_placeholders}) "
        f"AND expense_type IN ('Need', 'Want') "
        f"GROUP BY expense_type",
        [month] + income_list
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_income_report(month):
    conn = get_db()
    income_placeholders = ','.join('?' * len(INCOME_CATEGORIES))
    income_list = list(INCOME_CATEGORIES)
    rows = conn.execute(
        f'SELECT account, SUM(amount) as total, COUNT(*) as count '
        f'FROM transactions '
        f'WHERE month=? AND account IN ({income_placeholders}) '
        f'GROUP BY account ORDER BY total DESC',
        [month] + income_list
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Merchant rules ───────────────────────────────────────────

def get_merchant_rules():
    conn = get_db()
    rows = conn.execute('SELECT * FROM merchant_rules ORDER BY pattern').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_merchant_rule(pattern, category):
    """Insert or update a rule for the given pattern."""
    conn = get_db()
    conn.execute(
        'INSERT INTO merchant_rules (pattern, category) VALUES (?, ?)'
        ' ON CONFLICT(pattern) DO UPDATE SET category=excluded.category',
        (pattern.lower().strip(), category)
    )
    conn.commit()
    conn.close()


def delete_merchant_rule(rule_id):
    conn = get_db()
    conn.execute('DELETE FROM merchant_rules WHERE id=?', (rule_id,))
    conn.commit()
    conn.close()


def apply_merchant_rules(merchant):
    """Return the saved category for this merchant, or '' if none match."""
    conn = get_db()
    rules = conn.execute('SELECT pattern, category FROM merchant_rules').fetchall()
    conn.close()
    m = merchant.lower()
    for rule in rules:
        if rule['pattern'] in m:
            return rule['category']
    return ''


# ── Import log ────────────────────────────────────────────────

def log_import(filename, bank, imported, skipped):
    conn = get_db()
    conn.execute(
        'INSERT INTO import_log (filename, bank, imported, skipped) VALUES (?, ?, ?, ?)',
        (filename, bank, imported, skipped)
    )
    conn.commit()
    conn.close()


def get_summary_stats():
    """Topbar readouts: total txn count, current month net, YTD net."""
    from datetime import datetime as _dt
    month = _dt.now().strftime('%B')
    conn = get_db()
    ph = ','.join('?' * len(INCOME_CATEGORIES))
    il = list(INCOME_CATEGORIES)

    txn_count = conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]

    mi = conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE month=? AND account IN ({ph})', [month]+il).fetchone()[0]
    me = conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE month=? AND account NOT IN ({ph}) AND account!=""', [month]+il).fetchone()[0]

    yi = conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE account IN ({ph})', il).fetchone()[0]
    ye = conn.execute(f'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE account NOT IN ({ph}) AND account!=""', il).fetchone()[0]

    conn.close()
    return {'txn_count': txn_count, 'month_net': mi - me, 'ytd_net': yi - ye, 'month': month}


def get_import_logs():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM import_log ORDER BY imported_at DESC LIMIT 30'
    ).fetchall()
    conn.close()
    return rows


def undo_import(log_id):
    """Delete all transactions imported in the same batch as the given log entry."""
    conn = get_db()
    log = conn.execute('SELECT * FROM import_log WHERE id=?', (log_id,)).fetchone()
    if not log:
        conn.close()
        return 0
    # Transactions created within 60 seconds of the import log timestamp and matching bank
    deleted = conn.execute(
        '''DELETE FROM transactions
           WHERE UPPER(bank)=UPPER(?)
           AND created_at BETWEEN
               datetime(?, '-60 seconds') AND datetime(?, '+60 seconds')''',
        (log['bank'], log['imported_at'], log['imported_at'])
    ).rowcount
    conn.execute('DELETE FROM import_log WHERE id=?', (log_id,))
    conn.commit()
    conn.close()
    return deleted
