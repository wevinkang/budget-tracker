"""
Bank CSV importer — Amex, TD, Simplii, Scotiabank + Journal CSV export from
Google Sheets. Ported from the original Google Apps Script.
"""

import csv
import io
import re
import threading
from datetime import datetime

import db

# Per-import context (thread-local) used to surface which transactions were
# categorized by the AI fallback, so they can be shown for review afterward.
_ctx = threading.local()


def _note_ai(merchant, pattern):
    """Record that `merchant` was just categorized by the AI fallback so the
    active import can flag it for review. No-op outside of an import."""
    hits = getattr(_ctx, 'ai_hits', None)
    if hits is not None:
        hits.append((merchant, pattern))


def _note_skip(merchant, pattern, reason):
    """Record that `merchant` was skipped as a transfer/payment by a *heuristic*
    (masked-card regex or the AI transfer verdict — not an exact SKIP_PATTERN) so
    the active import can list it for review. No-op outside of an import."""
    hits = getattr(_ctx, 'skip_hits', None)
    if hits is not None:
        hits.append((merchant, pattern, reason))


def read_upload_to_csv(file, filename: str) -> str:
    """Read a CSV/XLS/XLSX upload and return CSV text. `file` is a binary file-like
    (e.g. open(path,'rb') or werkzeug FileStorage)."""
    suffix = ('.' + filename.lower().rsplit('.', 1)[-1]) if '.' in filename else ''

    if suffix == '.xlsx':
        import openpyxl
        wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
        ws = wb.active
        out = io.StringIO()
        w = csv.writer(out)
        for row in ws.iter_rows(values_only=True):
            w.writerow(['' if v is None else str(v) for v in row])
        wb.close()
        return out.getvalue()

    if suffix == '.xls':
        import xlrd
        data = file.read()
        wb = xlrd.open_workbook(file_contents=data)
        ws = wb.sheet_by_index(0)
        out = io.StringIO()
        w = csv.writer(out)
        for i in range(ws.nrows):
            w.writerow([str(v) if v != '' else '' for v in ws.row_values(i)])
        return out.getvalue()

    data = file.read()
    if isinstance(data, bytes):
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return data.decode('latin-1')
    return data

# ── Category keyword map ──────────────────────────────────────
CATEGORY_MAP = {
    'Paycheck Income':        ['payroll', 'direct deposit', 'employer', 'salary', 'paycheque'],
    'Tax Income':             ['tax refund', 'cra', 'revenue canada', 'gst rebate', 'hst rebate'],
    'Refund Income':          ['refund', 'return credit', 'chargeback', 'reversal', 'eft credit'],
    'Groceries expense':      ['loblaw', 'supermarket', 'bestco', 'save-on', 'safeway', 'loblaws',
                               'freshco', 'no frills', 'nofrills', 'walmart', 'costco', 't&t',
                               'whole foods', 'iga', 'metro', 'sobeys', 'superstore', 'sanagans',
                               'farm boy', 'longo', 'zehrs', 'fortinos', 'bulk barn', 'adonis',
                               'h mart', 'galleria', 'foodland', 'quality foods', 'choices market',
                               'nesters', 'real canadian', 'pacific fresh', 'food basics',
                               'farm fresh', 'foody mart', 'farm fresh supermarket'],
    'Food expense':           ['mcdonald', 'tim horton', 'starbucks', 'subway', 'a&w', 'wendy',
                               'pizza', 'sushi', 'ramen', 'pho', 'burrito', 'chipotle', 'burger',
                               'cafe', 'restaurant', 'kitchen', 'bistro', 'grill', 'diner',
                               'doordash', 'skip the dishes', 'uber eats', 'foodora', 'shawarma',
                               'kabab', 'izakaya', 'jollibee', 'tahini', 'five guys', 'food',
                               'kfc', 'popeyes', 'taco bell', 'domino', 'pizza pizza', 'pizza hut',
                               'boston pizza', 'swiss chalet', 'harvey', 'dairy queen', 'arby',
                               'panera', 'cora', 'denny', 'ihop', 'earls', 'cactus club',
                               'milestones', 'the keg', 'montana', 'st-hubert', 'mary brown',
                               'booster juice', 'freshii', 'mucho burrito', 'quesada',
                               'second cup', 'blenz', 'jj bean', 'mccafe', 'balzac', 'pilot coffee',
                               'eatery', 'taqueria', 'noodle', 'dumpling', 'hot pot',
                               'smoke poutinerie', 'fatburger', 'panda express'],
    'Drinking expense':       ['lcbo', 'bc liquor', 'beer store', 'liquor', 'wine', 'brewery',
                               'pub', 'bar ', 'taproom', 'distillery', 'bellwoods',
                               'cocktail', 'lounge', 'tap house', 'craft beer'],
    'Travel expense':         ['airline', 'airalo', 'westjet', 'air canada', 'porter', 'flair', 'sunwing', 'airbnb',
                               'vrbo', 'expedia', 'booking.com', 'hotel', 'marriott', 'hilton',
                               'delta hotel', 'via rail', 'amtrak',
                               'air transat', 'best western', 'holiday inn', 'hyatt', 'sandman',
                               'ramada', 'comfort inn', 'days inn', 'super 8', 'fairmont',
                               'four seasons', 'sheraton', 'westin', 'travelodge'],
    'Transportation expense': ['translink', 'presto', 'ttc', 'parking', 'spothero', 'impark',
                               'lazypark', 'greenp', 'uber', 'lyft', 'shell', 'petro', 'esso',
                               'husky', 'chevron', 'ultramar', 'pioneer', 'co-op fuel',
                               'bolt services', 'hopp',
                               'go transit', 'bc ferries', 'compass card', '407 etr', 'mobil',
                               'irving', 'tesla supercharger', 'chargepoint', 'electrify canada',
                               'flo charging', 'evgo'],
    'Car expense':            ['jiffy lube', 'midas', 'canadian tire', 'oil change', 'tire',
                               'autopart', 'car wash', 'icbc', 'intact auto', 'belair auto',
                               'mr lube', 'mr. lube', 'great canadian oil', 'kal tire', 'ok tire',
                               'fountain tire', 'speedy glass', 'napa auto', 'partsource'],
    'Entertainment expense':  ['netflix', 'spotify', 'youtube premium', 'disney', 'amazon prime',
                               'xbox', 'playstation', 'steam', 'cineplex',
                               'cinemas', 'ticketmaster', 'eventbrite', 'primevideo', 'crave',
                               'prime video',
                               'hulu', 'paramount', 'hbo', 'apple tv', 'tidal', 'audible',
                               'patreon', 'twitch', 'roblox', 'nintendo', 'bandcamp',
                               'landmark cinemas', 'imax'],
    'Shopping expense':       ['amazon', 'amzn', 'ebay', 'etsy', 'best buy', 'the bay', 'winners',
                               'homesense', 'indigo', 'sport', 'running room', 'lululemon', 'h&m',
                               'zara', 'uniqlo', 'aritzia', 'arcteryx', 'north face', 'marshalls',
                               'simons', 'rockwell', 'knifewear',
                               'old navy', 'banana republic', 'nike', 'adidas', 'new balance',
                               'roots canada', 'reitmans', 'la vie en rose', 'ardene',
                               'mountain equipment', 'atmosphere', 'sport chek', 'sportchek',
                               'decathlon', 'apple store', 'microsoft store', 'newegg',
                               'memory express', 'canada computers', 'staples', 'the source',
                               'lee valley', 'kobo', 'bookoutlet', 'toys r us', 'mastermind toys',
                               'petsmart', 'pet valu', 'global pet'],
    'Health expense':         ['shoppers', 'rexall', 'london drugs', 'pharmacy', 'physio', 'clinic',
                               'dental', 'optometry', 'goodlife', 'anytime fitness', 'ymca',
                               'doctor', 'medical',
                               'lifelabs', 'dynacare', 'telus health', 'maple health',
                               'felix health', 'massage', 'chiropract', 'acupuncture', 'naturopath',
                               'f45', 'orangetheory', 'orange theory', 'crunch fitness',
                               'planet fitness', 'fit4less', 'steve nash'],
    'Personal Care expense':  ['spa', 'salon', 'haircut', 'barber', 'nail', 'sephora', 'ulta',
                               'beauty', 'wellbeing',
                               'great clips', 'magicuts', 'tommy gun', 'chatters',
                               'first choice haircut', 'lush handmade'],
    'Home expense':           ['ikea', 'home depot', 'rona', 'lowe', 'wayfair', 'rent', 'lease',
                               'property', 'strata', 'home hardware',
                               'mortgage', 'condo fee', 'maintenance fee', 'property tax'],
    'Bill expense':           ['alectra', 'telus', 'cik', 'shaw', 'rogers', 'bell', 'fido',
                               'koodo', 'public mobile', 'internet', 'insurance', 'city of',
                               'utility', 'enbridge', 'ups canada', 'sonnet insurance',
                               # Electricity / gas / energy utilities (billed monthly)
                               'hydro', 'bc hydro', 'toronto hydro', 'hydro one', 'fortis',
                               'enmax', 'atco', 'epcor', 'direct energy', 'enercare',
                               'reliance home', 'just energy',
                               'virgin mobile', 'lucky mobile', 'chatr', 'freedom mobile', 'oxio',
                               'teksavvy', 'beanfield', 'distributel', 'vmedia', 'eastlink',
                               'cogeco', 'sasktel', 'aviva', 'allstate', 'manulife', 'sun life',
                               'service ontario', 'service bc', 'nsf fee', 'overdraft fee',
                               'foreign currency', 'icloud', 'google one', 'dropbox',
                               'microsoft 365', 'office 365', 'adobe', 'notion', '1password',
                               'lastpass', 'nordvpn', 'expressvpn', 'protonmail'],
    'Educational expense':    ['udemy', 'coursera', 'linkedin learning', 'tuition', 'university',
                               'college', 'textbook', 'pearson', 'mcgraw', 'claude',
                               'skillshare', 'codecademy', 'pluralsight', 'brilliant',
                               'masterclass', 'duolingo', 'babbel', 'openai', 'chatgpt',
                               'github', 'cursor.com', 'anthropic'],
    'Gift expense':           ['hallmark', '1-800-flowers', 'gift card',
                               'edible arrangement', 'ftd ', 'teleflora', 'proflowers'],
    'Girlfriend expense':     ['openrice', 'roses', 'bouquet', 'florist'],
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
    'internet bill payment mastercard bmo',
    'td/banque td',
    'internet bill payment questrade',
    'payment received - thank you',
    'payment - thank you',
    'payment thank you',
    'Cibc',
    'mb-td visa',
    'mb-bmo mastercard',
    'mb-rogers bank mastercard',
    'scotiabank payment',
    'bill payment mb-american express cards',
    # Internal transfers / credit-card & line-of-credit payments — already
    # recorded on the source statement. Broad substrings catch label variants.
    'mb-credit card',
    'credit card/loc pay',
    'scotiabank transit',
]

# A masked card/account number left in the description (e.g. "13*51", "1234*5678")
# is a strong tell for a credit-card payment or internal transfer. clean_merchant
# strips runs of 2+ asterisks, so this targets the surviving single-asterisk form.
MASKED_CARD_RE = re.compile(r'\d\*+\d')


def should_skip(merchant: str) -> bool:
    """True when a row is an internal transfer / credit-card payment to drop.

    Exact SKIP_PATTERNS are silent (intentional, known bank labels). The
    masked-card heuristic is *noted* so the importer can list it for review,
    since a heuristic can occasionally misfire. During a suppress pass (used to
    re-parse a skipped row for the review list) nothing is skipped."""
    if getattr(_ctx, 'suppress_skip', False):
        return False
    m = merchant.lower()
    if any(pattern in m for pattern in SKIP_PATTERNS):
        return True
    if MASKED_CARD_RE.search(merchant):
        _note_skip(merchant, '', 'masked-card')
        return True
    return False


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
    'Girlfriend expense':     'Need',

    'Food expense':           'Want',
    'Drinking expense':       'Want',
    'Travel expense':         'Want',
    'Entertainment expense':  'Want',
    'Shopping expense':       'Want',
    'Personal Care expense':  'Want',
    'Gift expense':           'Want',
}

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
          'July', 'August', 'September', 'October', 'November', 'December']


# ── Helpers ───────────────────────────────────────────────────

def parse_date(raw):
    if not raw:
        return None
    s = str(raw).strip().lstrip("'")
    # Strip trailing time component (e.g., "2026-04-30 12:00:00 AM" → "2026-04-30")
    s = re.sub(r'\s+\d{1,2}:\d{2}(:\d{2})?(\s*[AaPp][Mm])?$', '', s)
    # Normalize "29 Mar. 2026" → "29 Mar 2026"
    s_clean = s.replace('.', '')
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y/%m/%d',
                '%m-%d-%Y', '%d-%m-%Y', '%d %b %Y', '%d %B %Y', '%Y%m%d'):
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


E_TRANSFER_PATTERNS = ['e-transfer', 'etransfer', 'e transfer', 'interac']


def is_e_transfer(merchant):
    m = merchant.lower()
    return any(p in m for p in E_TRANSFER_PATTERNS)


def categorize(merchant):
    """Return (category, reimbursement, skip) for the given merchant string.

    During a suppress pass (re-parsing a skipped row for the review list) saved
    skip rules are ignored and the AI is never consulted, so the row resolves to
    a normal transaction we can show and optionally import."""
    suppress = getattr(_ctx, 'suppress_skip', False)
    saved_cat, saved_reimb, saved_skip = db.apply_merchant_rules(merchant)
    if saved_skip and not suppress:
        return '', 0, True
    if saved_cat:
        return saved_cat, saved_reimb, False

    if is_e_transfer(merchant):
        return '', 0, False   # leave uncategorized — too ambiguous to auto-map

    m = merchant.lower()
    for category, keywords in CATEGORY_MAP.items():
        if any(kw in m for kw in keywords):
            return category, 0, False

    if suppress:
        return '', 0, False   # re-parse pass: never hit the API, never skip

    # AI fallback for genuinely unknown merchants (opt-in: BUDGET_AI=1 + key).
    # On a hit, the result is saved as a merchant rule so it's deterministic and
    # free on every future import (and never re-sent to the API).
    import ai
    if ai.enabled() and merchant.strip():
        result = ai.classify_merchant(merchant)
        if result:
            category, pattern = result
            if category == ai.TRANSFER:
                # AI judged this an internal transfer / credit-card payment.
                # Save a skip rule (deterministic + free hereafter) and flag it
                # for review so a wrong call can be un-skipped.
                db.save_merchant_rule(pattern, '', skip=True)
                _note_skip(merchant, pattern, 'ai')
                return '', 0, True
            db.save_merchant_rule(pattern, category)
            _note_ai(merchant, pattern)
            return category, 0, False

    return '', 0, False


def need_want_label(category):
    if not category:
        return ''
    if category in db.INCOME_CATEGORIES or category.endswith('Income'):
        return ''
    return NEED_WANT_MAP.get(category, 'Want')


def reapply_rules_to_existing():
    """Re-run the saved merchant rules against every existing transaction, and
    (when AI is enabled) categorize still-uncategorized merchants via the AI
    classifier.

    For each transaction whose merchant (stored in `notes`) matches a rule, set
    the category/reimbursement to the rule's values and recompute Need/Want.
    Skip-rules don't apply retroactively (they only suppress new imports), so
    matched skip-rules leave the transaction untouched.

    For transactions that no rule matches and that are still uncategorized, the
    AI classifier is consulted (opt-in: BUDGET_AI=1 + key). A hit is saved as a
    merchant rule, so it's deterministic on future imports and the API is never
    asked about the same merchant twice. Existing categories are never
    overwritten by the AI — it only fills blanks.

    Returns (updated, unchanged, ai_categorized).
    """
    import ai
    ai_on = ai.enabled()
    ai_seen = {}  # merchant (lowercased) -> category or '' — cache for this pass

    updated = unchanged = ai_categorized = 0
    for row in db.get_transactions():
        merchant = row['notes'] or ''
        cat, reimb, skip = db.apply_merchant_rules(merchant)
        if skip:
            unchanged += 1
            continue

        from_ai = False
        # No rule matched and the transaction is still uncategorized → try AI.
        if (not cat and ai_on and merchant.strip()
                and not (row['account'] or '').strip()
                and not is_e_transfer(merchant)):
            key = merchant.lower().strip()
            if key not in ai_seen:
                result = ai.classify_merchant(merchant)
                if result:
                    ai_cat, pattern = result
                    if ai_cat == ai.TRANSFER:
                        # Transfer/payment → save a skip rule for future imports;
                        # leave the existing row untouched (don't recategorize).
                        db.save_merchant_rule(pattern, '', skip=True)
                        ai_seen[key] = ''
                    else:
                        db.save_merchant_rule(pattern, ai_cat)
                        ai_seen[key] = ai_cat
                else:
                    ai_seen[key] = ''
            cat = ai_seen[key]
            from_ai = bool(cat)

        if not cat:
            unchanged += 1
            continue
        if cat == row['account'] and (reimb or 0) == (row['reimbursement'] or 0):
            unchanged += 1
            continue

        data = dict(row)
        data['account'] = cat
        data['reimbursement'] = reimb
        data['expense_type'] = need_want_label(cat)
        db.update_transaction(row['id'], data)
        updated += 1
        if from_ai:
            ai_categorized += 1
    return updated, unchanged, ai_categorized


# ── Bank detection ────────────────────────────────────────────

def detect_bank(header_row):
    h = ','.join(str(c).strip().lower() for c in header_row)
    if 'date' in h and 'transaction' in h and 'debit' in h and 'credit' in h:
        return 'td'
    if 'date' in h and 'transaction' in h and ('funds out' in h or 'funds in' in h):
        return 'simplii'
    if 'symbol' in h and 'net amount' in h:
        return 'questrade'
    if 'reference number' in h and 'rewards' in h:
        return 'rogers'
    if 'transaction date' in h and 'posting date' in h and 'transaction amount' in h:
        return 'bmo'
    if 'sub-description' in h and 'type of transaction' in h:
        return 'scotiabank'
    # Amex only claims shapes its parser handles: the slim 3-4 col export or the
    # 9+ col detailed export. Wider files fall through to the generic fallback.
    if 'date' in h and 'description' in h and 'amount' in h \
            and (len(header_row) <= 4 or len(header_row) >= 9):
        return 'amex'
    # Fallback: slim 3-4 column file with a date-like first column.
    if 3 <= len(header_row) <= 4 and 'date' in str(header_row[0]).lower():
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

def _make_transaction(dt, merchant, amount, category, bank, reimbursement=0):
    return {
        'date':          dt.strftime('%Y-%m-%d') if dt else '',
        'merchant':      merchant,
        'amount':        amount,
        'category':      category,
        'needWant':      need_want_label(category),
        'month':         month_name(dt),
        'bank':          bank,
        'reimbursement': reimbursement,
    }


def parse_amex(row):
    try:
        dt = parse_date(row[0])
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
        if amount < 0:
            return _make_transaction(dt, merchant, abs(amount), 'Refund Income', 'Amex')
        cat, reimb, skip = categorize(merchant)
        if skip:
            return None
        return _make_transaction(dt, merchant, amount, cat, 'Amex', reimb)
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
        if credit is not None and credit > 0:
            cat, reimb, skip = categorize(merchant)
            if skip:
                return None
            if cat and cat not in db.INCOME_CATEGORIES and not cat.endswith('Income'):
                cat = 'Refund Income'
            return _make_transaction(dt, merchant, credit, cat or 'Income', 'TD', reimb)
        if debit is None or debit <= 0:
            return None
        cat, reimb, skip = categorize(merchant)
        if skip:
            return None
        return _make_transaction(dt, merchant, debit, cat, 'TD', reimb)
    except Exception:
        return None


def parse_questrade(row):
    """
    Questrade activity export. Columns:
    Transaction Date, Settlement Date, Action, Symbol, Description, Quantity,
    Price, Gross Amount, Commission, Net Amount, Currency, Account #,
    Activity Type, Account Type
    Records every positive Net Amount row as Investment Income.
    """
    try:
        if len(row) < 10:
            return None
        dt = parse_date(row[0])
        if not dt:
            return None
        amount = parse_amount(row[9])
        if amount is None or amount <= 0:
            return None
        action      = str(row[2]).strip()
        description = clean_merchant(row[4])
        notes       = f'{action}: {description}' if action and description else (action or description)
        if should_skip(notes):
            return None
        return _make_transaction(dt, notes, amount, 'Investment Income', 'Questrade')
    except Exception:
        return None


def parse_rogers(row):
    """
    Rogers Bank CSV export. Columns:
    Date, Posted Date, Reference Number, Activity Type, Activity Status,
    Card Number, Merchant Category Description, Merchant Name, Merchant City,
    Merchant State or Province, Merchant Country Code, Merchant Postal Code,
    Amount, Rewards, Name on Card
    """
    try:
        if len(row) < 13:
            return None
        dt       = parse_date(row[0])
        merchant = clean_merchant(row[7])
        amount   = parse_amount(row[12])
        if amount is None or amount == 0:
            return None
        if should_skip(merchant):
            return None
        if amount < 0:
            return _make_transaction(dt, merchant, abs(amount), 'Refund Income', 'Rogers')
        cat, reimb, skip = categorize(merchant)
        if skip:
            return None
        return _make_transaction(dt, merchant, amount, cat, 'Rogers', reimb)
    except Exception:
        return None


def parse_bmo(row):
    """
    BMO Mastercard CSV export. Columns:
    Item #, Card #, Transaction Date, Posting Date, Transaction Amount, Description
    Positive = charge (expense); negative = payment/credit.
    """
    try:
        if len(row) < 6:
            return None
        dt       = parse_date(str(row[2]).strip().lstrip("'"))
        amount   = parse_amount(row[4])
        merchant = clean_merchant(row[5])
        if amount is None or amount == 0:
            return None
        if should_skip(merchant):
            return None
        if amount < 0:
            return _make_transaction(dt, merchant, abs(amount), 'Refund Income', 'BMO')
        cat, reimb, skip = categorize(merchant)
        if skip:
            return None
        return _make_transaction(dt, merchant, amount, cat, 'BMO', reimb)
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
            cat, reimb, skip = categorize(merchant)
            if skip:
                return None
            if cat and cat not in db.INCOME_CATEGORIES and not cat.endswith('Income'):
                cat = 'Refund Income'
            return _make_transaction(dt, merchant, inflow, cat or 'Income', 'Simplii', reimb)
        if out is None or out <= 0:
            return None
        cat, reimb, skip = categorize(merchant)
        if skip:
            return None
        return _make_transaction(dt, merchant, out, cat, 'Simplii', reimb)
    except Exception:
        return None


def parse_scotia(row):
    """
    Scotiabank CSV export (chequing or credit card). Columns vary slightly:
      chequing:    Filter, Date, Description, Sub-description, Type of Transaction, Amount, Balance
      credit card: Filter, Date, Description, Sub-description, Status, Type of Transaction, Amount
    The amount may be signed (chequing) or unsigned with the sign carried by a
    "Type of Transaction" cell of Debit/Credit (credit card). We locate that
    cell to recover the sign and read the amount from the column after it.
    The merchant detail lives in Sub-description; Description is the bank's
    activity label (e.g. "bill payment"), so we combine the two for categorizing.
    """
    try:
        if len(row) < 6:
            return None
        dt       = parse_date(row[1])
        desc     = str(row[2]).strip()
        sub      = str(row[3]).strip()
        merchant = clean_merchant(f'{desc} {sub}'.strip())
        if should_skip(merchant):
            return None
        # Find the Debit/Credit cell; the amount sits in the next column.
        type_idx = next((i for i, c in enumerate(row)
                         if str(c).strip().lower() in ('debit', 'credit')), None)
        if type_idx is not None and type_idx + 1 < len(row):
            amount = parse_amount(row[type_idx + 1])
            if amount is not None and str(row[type_idx]).strip().lower() == 'debit':
                amount = -abs(amount)
            elif amount is not None:
                amount = abs(amount)
        else:
            amount = parse_amount(row[5])
        if amount is None or amount == 0:
            return None
        if amount > 0:
            cat, reimb, skip = categorize(merchant)
            if skip:
                return None
            if cat and cat not in db.INCOME_CATEGORIES and not cat.endswith('Income'):
                cat = 'Refund Income'
            return _make_transaction(dt, merchant, amount, cat or 'Income', 'Scotiabank', reimb)
        cat, reimb, skip = categorize(merchant)
        if skip:
            return None
        return _make_transaction(dt, merchant, abs(amount), cat, 'Scotiabank', reimb)
    except Exception:
        return None


# ── Generic fallback (unknown banks) ──────────────────────────
# Used only when detect_bank() fails — e.g. a friend running the app with a
# bank we haven't hardcoded. We infer which columns hold the date, amount, and
# merchant by inspecting the data, then run the same categorize/dedupe pipeline.
# Assumptions (documented because guesses can be wrong):
#   • Single signed-amount layouts: negative = money out (expense), positive =
#     money in (income). This is the common chequing/credit export convention.
#   • Split debit/credit layouts: the earlier numeric column is outflow.
# Known banks (Amex, etc.) keep their own parsers and are never routed here.

def _has_decimal(v):
    return bool(re.search(r'\d[.,]\d', v))


def infer_layout(data_rows):
    """Inspect data rows and infer column roles.
    Returns a dict like {'date': i, 'merchant': i, 'amount': i} or
    {'date': i, 'merchant': i, 'out': i, 'in': i}, or None if it can't find a
    date column plus at least one amount column."""
    sample = [r for r in data_rows
              if not all(str(c).strip() == '' for c in r)][:50]
    if not sample:
        return None
    ncols = max(len(r) for r in sample)

    def cell(r, i):
        return str(r[i]).strip() if i < len(r) else ''

    nonempty   = [0] * ncols
    date_hits  = [0] * ncols
    num_hits   = [0] * ncols
    dec_hits   = [0] * ncols
    neg_hits   = [0] * ncols
    text_len   = [0] * ncols
    for r in sample:
        for i in range(ncols):
            v = cell(r, i)
            if not v:
                continue
            nonempty[i] += 1
            text_len[i] += len(v)
            if parse_date(v):
                date_hits[i] += 1
            if re.search(r'\d', v) and parse_amount(v) is not None:
                num_hits[i] += 1
                if _has_decimal(v):
                    dec_hits[i] += 1
                if parse_amount(v) < 0:
                    neg_hits[i] += 1

    # Date column: best date-parse rate, covering a majority of its cells.
    date_idx = max(range(ncols), key=lambda i: date_hits[i])
    if date_hits[date_idx] < max(2, 0.5 * nonempty[date_idx]):
        return None

    n = len(sample)
    # A "monetary" column parses as an amount whenever it's populated and shows
    # at least one decimal value — this rejects integer reference-number columns.
    # Judged per-column (not by a global frequency), so a sparse Deposits column
    # with only a couple of rows still counts.
    monetary = [i for i in range(ncols)
                if i != date_idx and nonempty[i] >= 1
                and num_hits[i] >= 0.8 * nonempty[i] and dec_hits[i] >= 1]
    if not monetary:
        return None

    # "full" = populated in nearly every row (single signed amount, or balance);
    # "sparse" = populated in only some rows (debit/credit split columns).
    full   = [i for i in monetary if nonempty[i] >= 0.9 * n]
    sparse = [i for i in monetary if i not in full]

    layout = {'date': date_idx}
    if len(sparse) >= 2:
        # Split debit/credit: take the two most-populated sparse columns, then
        # order by position (earlier column = outflow, matching TD/Simplii).
        chosen = sorted(sorted(sparse, key=lambda i: num_hits[i],
                               reverse=True)[:2])
        layout['out'], layout['in'] = chosen[0], chosen[1]
        used = {date_idx, chosen[0], chosen[1]}
    else:
        # Single signed amount. Among full columns, prefer the one with the most
        # negative values (amount has expenses; balance is usually positive),
        # then the earliest (amount typically precedes balance).
        cand = full or sparse
        amt = sorted(cand, key=lambda i: (-neg_hits[i], i))[0]
        layout['amount'] = amt
        used = {date_idx, amt}

    rest = [i for i in range(ncols) if i not in used]
    if not rest:
        return None
    layout['merchant'] = max(rest, key=lambda i: text_len[i])
    return layout


def make_generic_parser(layout):
    """Build a per-row parser closure from an inferred column layout."""
    def parse(row):
        try:
            def get(i):
                return row[i] if i < len(row) else ''
            dt       = parse_date(get(layout['date']))
            merchant = clean_merchant(get(layout['merchant']))
            if should_skip(merchant):
                return None

            if 'amount' in layout:
                amount = parse_amount(get(layout['amount']))
                if amount is None or amount == 0:
                    return None
                if amount > 0:
                    cat, reimb, skip = categorize(merchant)
                    if skip:
                        return None
                    return _make_transaction(dt, merchant, amount,
                                             cat or 'Income', 'Generic', reimb)
                # negative = expense
                cat, reimb, skip = categorize(merchant)
                if skip:
                    return None
                return _make_transaction(dt, merchant, abs(amount), cat,
                                         'Generic', reimb)

            outflow = parse_amount(get(layout['out']))
            inflow  = parse_amount(get(layout['in']))
            if inflow is not None and inflow > 0:
                cat, reimb, skip = categorize(merchant)
                if skip:
                    return None
                return _make_transaction(dt, merchant, inflow,
                                         cat or 'Income', 'Generic', reimb)
            if outflow is None or outflow <= 0:
                return None
            cat, reimb, skip = categorize(merchant)
            if skip:
                return None
            return _make_transaction(dt, merchant, outflow, cat,
                                     'Generic', reimb)
        except Exception:
            return None
    return parse


# ── Public import functions ───────────────────────────────────

def import_csv_string(content):
    """
    Parse a bank CSV string and write new transactions to the DB.
    Returns (added, skipped, bank_name_or_None, ai_review, skip_review,
    etransfer_review) where ai_review lists rows the AI fallback categorized,
    skip_review lists rows dropped as likely transfers/payments by a heuristic
    (masked-card regex or the AI transfer verdict) — each a {date, merchant,
    amount, account, expense_type, month, bank, pattern, reason} dict — and
    etransfer_review lists newly-added e-transfer expenses (generic bank text,
    left uncategorized by categorize()) for the user to label, each a
    {id, date, merchant, amount} dict.
    """
    try:
        reader = csv.reader(io.StringIO(content.strip()))
    except Exception:
        return 0, 0, None, [], [], []

    rows = list(reader)
    if len(rows) < 2:
        return 0, 0, None, [], [], []

    header_idx, data_start = find_header_row(rows)
    bank = detect_bank(rows[header_idx])

    # TD exports have no header — detect from first data row instead
    if not bank and header_idx == 0:
        td_bank = detect_bank_from_data(rows[0])
        if td_bank:
            bank = td_bank
            data_start = 0  # first row is already data

    parsers = {'amex': parse_amex, 'td': parse_td, 'simplii': parse_simplii, 'questrade': parse_questrade, 'rogers': parse_rogers, 'bmo': parse_bmo, 'scotiabank': parse_scotia}

    if bank:
        parse_fn = parsers[bank]
    else:
        # Unknown bank — try inferring the columns from the data itself.
        layout = infer_layout(rows[data_start:])
        if not layout:
            import logging
            logging.getLogger(__name__).warning(f'Bank detection failed and column inference failed. Header row: {rows[0]}')
            return 0, 0, None, [], [], []
        bank = 'generic'
        parse_fn = make_generic_parser(layout)

    existing = db.get_dedupe_keys()
    added = skipped = 0
    ai_review = []
    skip_review = []
    etransfer_review = []

    _ctx.ai_hits = []
    _ctx.skip_hits = []
    _ctx.suppress_skip = False
    try:
        for row in rows[data_start:]:
            if all(str(c).strip() == '' for c in row):
                continue
            before = len(_ctx.ai_hits)
            before_skip = len(_ctx.skip_hits)
            t = parse_fn(row)
            if not t:
                # A heuristic (masked-card / AI) skipped this as a transfer →
                # re-parse with skipping suppressed to recover its details and
                # list it for review.
                if len(_ctx.skip_hits) > before_skip:
                    _, pattern, reason = _ctx.skip_hits[-1]
                    _ctx.suppress_skip = True
                    try:
                        full = parse_fn(row)
                    finally:
                        _ctx.suppress_skip = False
                    if full:
                        skip_review.append({
                            'date':         full['date'],
                            'merchant':     full['merchant'],
                            'amount':       full['amount'],
                            'account':      full['category'],
                            'expense_type': full['needWant'],
                            'month':        full['month'],
                            'bank':         full['bank'],
                            'pattern':      pattern,
                            'reason':       reason,
                        })
                continue
            key = f"{t['date']}|{t['merchant']}|{t['amount']}"
            if key in existing:
                skipped += 1
                continue
            new_id = db.add_transaction({
                'date':          t['date'],
                'account':       t['category'],
                'amount':        t['amount'],
                'notes':         t['merchant'],
                'expense_type':  t['needWant'],
                'month':         t['month'],
                'bank':          t['bank'],
                'reimbursement': t.get('reimbursement', 0),
            })
            existing.add(key)
            added += 1
            # This row's category came from the AI fallback → flag it for review.
            if len(_ctx.ai_hits) > before and t['category']:
                _, pattern = _ctx.ai_hits[-1]
                ai_review.append({
                    'id':       new_id,
                    'date':     t['date'],
                    'merchant': t['merchant'],
                    'amount':   t['amount'],
                    'category': t['category'],
                    'pattern':  pattern,
                })
            # An e-transfer expense: bank text is generic ("INTERAC E-TRANSFER"),
            # so categorize() left it uncategorized — ask the user what it was
            # for. The label is saved separately from `notes` (see /import/label
            # in app.py), so re-importing this file still dedupes on the
            # untouched raw bank text even after labeling.
            elif not t['category'] and is_e_transfer(t['merchant']):
                etransfer_review.append({
                    'id':       new_id,
                    'date':     t['date'],
                    'merchant': t['merchant'],
                    'amount':   t['amount'],
                })
    finally:
        _ctx.ai_hits = None
        _ctx.skip_hits = None

    return added, skipped, bank.upper(), ai_review, skip_review, etransfer_review


def import_transactions_csv(content):
    """
    Import a Transactions CSV exported from Google Sheets.
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
