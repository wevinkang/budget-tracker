"""
Bank CSV importer — Amex, TD, Simplii + Journal CSV export from Google Sheets.
Ported from the original Google Apps Script.
"""

import csv
import io
import re
from datetime import datetime

import db

# ── Category keyword map ──────────────────────────────────────
CATEGORY_MAP = {
    'Paycheck Income':        ['payroll', 'direct deposit', 'employer', 'salary', 'paycheque'],
    'Tax Income':             ['tax refund', 'cra', 'revenue canada', 'gst rebate', 'hst rebate'],
    'Refund Income':          ['refund', 'return credit', 'chargeback', 'reversal'],

    'Groceries expense':      ['loblaw', 'supermarket', 'bestco', 'save-on', 'safeway', 'loblaws',
                               'freshco', 'no frills', 'nofrills', 'walmart', 'costco', 't&t',
                               'whole foods', 'iga', 'metro', 'sobeys', 'superstore'],
    'Food expense':           ['mcdonald', 'tim horton', 'starbucks', 'subway', 'a&w', 'wendy',
                               'pizza', 'sushi', 'ramen', 'pho', 'burrito', 'chipotle', 'burger',
                               'cafe', 'restaurant', 'kitchen', 'bistro', 'grill', 'diner',
                               'doordash', 'skip the dishes', 'uber eats', 'foodora', 'shawarma',
                               'kabab', 'izakaya', 'jollibee', 'tahini', 'five guys'],
    'Drinking expense':       ['lcbo', 'bc liquor', 'beer store', 'liquor', 'wine', 'brewery',
                               'pub', 'bar ', 'taproom', 'distillery', 'bellwoods'],
    'Travel expense':         ['westjet', 'air canada', 'porter', 'flair', 'sunwing', 'airbnb',
                               'vrbo', 'expedia', 'booking.com', 'hotel', 'marriott', 'hilton',
                               'delta hotel', 'via rail', 'amtrak'],
    'Transportation expense': ['translink', 'presto', 'ttc', 'parking', 'spothero', 'impark',
                               'lazypark', 'greenp', 'uber', 'lyft', 'shell', 'petro', 'esso',
                               'husky', 'chevron', 'ultramar', 'pioneer', 'co-op fuel',
                               'bolt services', 'hopp'],
    'Car expense':            ['jiffy lube', 'midas', 'canadian tire', 'oil change', 'tire',
                               'autopart', 'car wash', 'icbc', 'intact auto', 'belair auto'],
    'Entertainment expense':  ['netflix', 'spotify', 'youtube premium', 'disney', 'amazon prime',
                               'apple.com/bill', 'xbox', 'playstation', 'steam', 'cineplex',
                               'cinemas', 'ticketmaster', 'eventbrite', 'primevideo', 'crave',
                               'prime video'],
    'Shopping expense':       ['amazon', 'amzn', 'ebay', 'etsy', 'best buy', 'the bay', 'winners',
                               'homesense', 'indigo', 'sport', 'running room', 'lululemon', 'h&m',
                               'zara', 'uniqlo', 'aritzia', 'arcteryx', 'north face', 'marshalls',
                               'simons', 'rockwell', 'knifewear'],
    'Health expense':         ['shoppers', 'rexall', 'london drugs', 'pharmacy', 'physio', 'clinic',
                               'dental', 'optometry', 'goodlife', 'anytime fitness', 'ymca',
                               'doctor', 'medical'],
    'Personal Care expense':  ['spa', 'salon', 'haircut', 'barber', 'nail', 'sephora', 'ulta',
                               'beauty', 'wellbeing'],
    'Home expense':           ['ikea', 'home depot', 'rona', 'lowe', 'wayfair', 'rent', 'lease',
                               'property', 'strata', 'hydro', 'fortis', 'enmax', 'atco'],
    'Bill expense':           ['alectra', 'telus', 'cik', 'shaw', 'rogers', 'bell', 'fido',
                               'koodo', 'public mobile', 'internet', 'insurance', 'city of',
                               'utility', 'enbridge', 'ups canada', 'sonnet insurance'],
    'Educational expense':    ['udemy', 'coursera', 'linkedin learning', 'tuition', 'university',
                               'college', 'textbook', 'pearson', 'mcgraw'],
    'Gift expense':           ['hallmark', '1-800-flowers', 'gift card'],
    'Date expense':           ['openrice', 'roses', 'bouquet', 'florist'],
    'Credit Card expense':    ['credit card payment', 'card payment', 'visa payment',
                               'mastercard payment', 'amex payment', 'membership fee installment'],
    'Interest expense':       ['interest charge', 'purchase interest', 'cash advance interest'],
    'Loans expense':          ['loan payment', 'student loan', 'nslsc', 'line of credit payment'],
}

# ── Transactions to skip entirely ────────────────────────────
# Payments toward credit cards or investment accounts — already recorded elsewhere
SKIP_PATTERNS = [
    'miscellaneous payments american express',
    'internet bill payment visa',
    'td/banque td',
    'internet bill payment questrade',
]


def should_skip(merchant: str) -> bool:
    m = merchant.lower()
    return any(pattern in m for pattern in SKIP_PATTERNS)


# ── Need / Want classification ────────────────────────────────
NEED_WANT_MAP = {
    'Groceries expense':      'Need',
    'Bill expense':           'Need',
    'Transportation expense': 'Need',
    'Car expense':            'Need',
    'Health expense':         'Need',
    'Home expense':           'Need',
    'Loans expense':          'Need',
    'Interest expense':       'Need',
    'Credit Card expense':    'Need',
    'Educational expense':    'Need',
    'Munchkin expense':       'Need',

    'Food expense':           'Want',
    'Drinking expense':       'Want',
    'Travel expense':         'Want',
    'Entertainment expense':  'Want',
    'Shopping expense':       'Want',
    'Personal Care expense':  'Want',
    'Date expense':           'Want',
    'Gift expense':           'Want',
}

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
          'July', 'August', 'September', 'October', 'November', 'December']


# ── Helpers ───────────────────────────────────────────────────

def parse_date(raw):
    if not raw:
        return None
    s = str(raw).strip().lstrip("'")
    # Normalize "29 Mar. 2026" → "29 Mar 2026"
    s_clean = s.replace('.', '')
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y/%m/%d',
                '%m-%d-%Y', '%d-%m-%Y', '%d %b %Y', '%d %B %Y'):
        for candidate in (s, s_clean):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def month_name(dt):
    if not dt:
        return ''
    return MONTHS[dt.month - 1]


def clean_merchant(raw):
    s = str(raw)
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'#\d+', '', s)
    s = re.sub(r'\d{10,}', '', s)
    s = re.sub(r'\*{2,}', '', s)
    return s.strip().title()


def parse_amount(raw):
    try:
        return float(re.sub(r'[$,\s]', '', str(raw)))
    except (ValueError, TypeError):
        return None


def categorize(merchant):
    m = merchant.lower()
    for category, keywords in CATEGORY_MAP.items():
        if any(kw in m for kw in keywords):
            return category
    return ''


def need_want_label(category):
    if not category:
        return ''
    return NEED_WANT_MAP.get(category, 'Want')


# ── Bank detection ────────────────────────────────────────────

def detect_bank(header_row):
    h = ','.join(str(c).strip().lower() for c in header_row)
    if 'date' in h and 'transaction' in h and 'debit' in h and 'credit' in h:
        return 'td'
    if 'date' in h and 'transaction' in h and ('funds out' in h or 'funds in' in h):
        return 'simplii'
    if 'date' in h and 'description' in h and 'amount' in h:
        return 'amex'
    # Fallback: 3-column with date-like first column
    if len(header_row) >= 3 and 'date' in str(header_row[0]).lower():
        return 'amex'
    return None


def detect_bank_from_data(row):
    """Detect bank from a data row when no header is present (e.g. TD)."""
    if len(row) == 5 and parse_date(str(row[0]).strip()):
        return 'td'
    return None


def find_header_row(rows):
    """
    Scan up to the first 20 rows to find the real header row.
    Handles files like Amex XLS that have metadata before the headers.
    Returns (header_index, data_start_index).
    """
    for i, row in enumerate(rows[:20]):
        h = ','.join(str(c).strip().lower() for c in row)
        if any(kw in h for kw in ['description', 'debit', 'funds out', 'funds in']):
            return i, i + 1
    return 0, 1  # fallback


# ── Bank parsers ──────────────────────────────────────────────

def _make_transaction(dt, merchant, amount, category, bank):
    return {
        'date':     dt.strftime('%Y-%m-%d') if dt else '',
        'merchant': merchant,
        'amount':   amount,
        'category': category,
        'needWant': need_want_label(category),
        'month':    month_name(dt),
        'bank':     bank,
    }


def parse_amex(row):
    try:
        dt = parse_date(row[0])
        # Handle both 3-col (Date, Description, Amount) Amex CSV
        # and 10-col (Date, Date Processed, Description, Amount, ...) Amex XLS
        if len(row) >= 9:
            merchant = clean_merchant(row[2])
            amount   = parse_amount(row[3])
        else:
            merchant = clean_merchant(row[1])
            amount   = parse_amount(row[2])
        if amount is None or amount == 0:
            return None
        if should_skip(merchant):
            return None
        # Amex: positive = charge, negative = credit/refund
        if amount < 0:
            return _make_transaction(dt, merchant, abs(amount), 'Refund Income', 'Amex')
        return _make_transaction(dt, merchant, amount, categorize(merchant), 'Amex')
    except Exception:
        return None


def parse_td(row):
    try:
        dt       = parse_date(row[0])
        merchant = clean_merchant(row[1])
        if should_skip(merchant):
            return None
        debit    = parse_amount(row[2])
        credit   = parse_amount(row[3]) if len(row) > 3 else None
        # Credits (deposits) come in the credit column
        if credit is not None and credit > 0:
            return _make_transaction(dt, merchant, credit, categorize(merchant) or 'Income', 'TD')
        if debit is None or debit <= 0:
            return None
        return _make_transaction(dt, merchant, debit, categorize(merchant), 'TD')
    except Exception:
        return None


def parse_simplii(row):
    try:
        dt       = parse_date(row[0])
        merchant = clean_merchant(row[1])
        if should_skip(merchant):
            return None
        out      = parse_amount(row[2])
        inflow   = parse_amount(row[3]) if len(row) > 3 else None
        if inflow is not None and inflow > 0:
            return _make_transaction(dt, merchant, inflow, 'Income', 'Simplii')
        if out is None or out <= 0:
            return None
        return _make_transaction(dt, merchant, out, categorize(merchant), 'Simplii')
    except Exception:
        return None


# ── Public import functions ───────────────────────────────────

def import_csv_string(content):
    """
    Parse a bank CSV string and write new transactions to the DB.
    Returns (added, skipped, bank_name_or_None).
    """
    try:
        reader = csv.reader(io.StringIO(content.strip()))
    except Exception:
        return 0, 0, None

    rows = list(reader)
    if len(rows) < 2:
        return 0, 0, None

    header_idx, data_start = find_header_row(rows)
    bank = detect_bank(rows[header_idx])

    # TD exports have no header — detect from first data row instead
    if not bank and header_idx == 0:
        bank = detect_bank_from_data(rows[0])
        data_start = 0  # first row is already data

    if not bank:
        import logging
        logging.getLogger(__name__).warning(f'Bank detection failed. Header row: {rows[0]}')
        return 0, 0, None

    existing = db.get_dedupe_keys()
    added = skipped = 0

    parsers = {'amex': parse_amex, 'td': parse_td, 'simplii': parse_simplii}
    parse_fn = parsers[bank]

    for row in rows[data_start:]:
        if all(str(c).strip() == '' for c in row):
            continue
        t = parse_fn(row)
        if not t:
            continue
        key = f"{t['date']}|{t['merchant']}|{t['amount']}"
        if key in existing:
            skipped += 1
            continue
        db.add_transaction({
            'date':         t['date'],
            'account':      t['category'],
            'amount':       t['amount'],
            'notes':        t['merchant'],
            'expense_type': t['needWant'],
            'month':        t['month'],
            'bank':         t['bank'],
        })
        existing.add(key)
        added += 1

    return added, skipped, bank.upper()


def import_journal_csv(content):
    """
    Import a Journal CSV exported from Google Sheets.
    Expected columns: Date, Account, Amount, Notes, Expense Type, Month[, Bank]
    Returns (added, skipped).
    """
    try:
        reader = csv.reader(io.StringIO(content.strip()))
    except Exception:
        return 0, 0

    rows = list(reader)
    if len(rows) < 2:
        return 0, 0

    existing = db.get_dedupe_keys()
    added = skipped = 0

    for row in rows[1:]:
        if len(row) < 3:
            continue
        if all(str(c).strip() == '' for c in row):
            continue
        try:
            date_raw     = row[0].strip()
            account      = row[1].strip() if len(row) > 1 else ''
            amount_raw   = row[2].strip() if len(row) > 2 else ''
            notes        = row[3].strip() if len(row) > 3 else ''
            expense_type = row[4].strip() if len(row) > 4 else ''
            month        = row[5].strip() if len(row) > 5 else ''
            bank         = row[6].strip() if len(row) > 6 else ''

            dt     = parse_date(date_raw)
            amount = parse_amount(amount_raw)
            if amount is None:
                continue

            date_str = dt.strftime('%Y-%m-%d') if dt else date_raw
            if not month and dt:
                month = month_name(dt)

            key = f"{date_str}|{notes}|{amount}"
            if key in existing:
                skipped += 1
                continue

            db.add_transaction({
                'date':         date_str,
                'account':      account,
                'amount':       amount,
                'notes':        notes,
                'expense_type': expense_type,
                'month':        month,
                'bank':         bank,
            })
            existing.add(key)
            added += 1
        except Exception:
            continue

    return added, skipped
