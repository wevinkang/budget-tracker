import csv
import io
import json
import logging
import math
import os
import threading
from datetime import datetime
from pathlib import Path

from functools import wraps

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for

import ai
import db
import importer

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)
app.secret_key = 'budget-tracker-2026'

IMPORT_FOLDER = Path(os.environ.get('BUDGET_IMPORTS', Path.home() / 'budget-imports'))
DONE_FOLDER   = IMPORT_FOLDER / 'done'

CURRENT_MONTH = datetime.now().strftime('%B')
PER_PAGE = 50

# Set at startup from the DB password prompt — reused for the web login
_WEB_PASSWORD = ''


# ── Auth ──────────────────────────────────────────────────────

@app.context_processor
def _inject_globals():
    def page_url(page):
        from urllib.parse import urlencode
        args = request.args.to_dict()
        args['page'] = page
        return request.path + '?' + urlencode(args)

    if session.get('logged_in'):
        _accounts = db.get_accounts()
        _stats    = db.get_summary_stats()
    else:
        _accounts = []
        _stats    = {'txn_count': 0, 'month_net': 0, 'ytd_net': 0, 'month': CURRENT_MONTH}

    return dict(page_url=page_url, _accounts=_accounts, _stats=_stats,
                _income_accts=db.INCOME_CATEGORIES)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == _WEB_PASSWORD:
            session['logged_in'] = True
            session.permanent = True
            return redirect(request.args.get('next') or url_for('transactions'))
        flash('Incorrect password.')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Startup ───────────────────────────────────────────────────

def _startup():
    db.init_db()
    db.seed_accounts()
    IMPORT_FOLDER.mkdir(exist_ok=True)
    DONE_FOLDER.mkdir(exist_ok=True)
    from watcher import start_watcher
    t = threading.Thread(target=start_watcher, args=(IMPORT_FOLDER, DONE_FOLDER), daemon=True)
    t.start()


# ── PWA ───────────────────────────────────────────────────────

@app.route('/sw.js')
def service_worker():
    return app.send_static_file('sw.js')


# ── Routes: Transactions ──────────────────────────────────────

@app.route('/')
@login_required
def index():
    months = db.get_months()
    month  = request.args.get('month', CURRENT_MONTH)
    if months and month not in months:
        month = months[-1] if months else CURRENT_MONTH

    category_data  = db.get_category_report(month)
    income_data    = db.get_income_report(month)
    total_expense  = sum(r['total'] for r in category_data)
    total_income   = sum(r['total'] for r in income_data)
    net            = total_income - total_expense
    savings_pct    = (net / total_income * 100) if total_income else 0
    recent         = db.get_transactions(limit=10)
    net_summary    = db.get_net_income_summary()
    trend          = [r for r in net_summary if not r.get('is_total')][-6:]
    accounts       = db.get_accounts()

    raw_insight    = db.get_insight(month)
    insight        = None
    if raw_insight:
        try:
            insight = json.loads(raw_insight['content'])
        except (ValueError, TypeError):
            # Legacy plaintext insight — show it as the summary sentence.
            insight = {'summary': raw_insight['content'], 'net': None,
                       'income': None, 'expenses': None, 'categories': []}
        insight['generated_at'] = raw_insight['generated_at']

    return render_template('dashboard.html',
                           month=month,
                           months=months,
                           total_income=total_income,
                           total_expense=total_expense,
                           net=net,
                           savings_pct=savings_pct,
                           category_data=category_data[:5],
                           recent=recent,
                           trend=trend,
                           accounts=accounts,
                           ai_enabled=ai.enabled(),
                           insight=insight)


@app.route('/insights/<month>', methods=['POST'])
@login_required
def generate_insights(month):
    if not ai.enabled():
        flash('AI insights are off — set BUDGET_AI=1 and ANTHROPIC_API_KEY.')
        return redirect(url_for('index', month=month))

    cat = db.get_category_report(month)
    inc = db.get_income_report(month)
    months = db.get_months()
    prev = months[months.index(month) - 1] if month in months and months.index(month) > 0 else None
    prev_cat = db.get_category_report(prev) if prev else []
    try:
        data = ai.monthly_insights(month, cat, inc, prev, prev_cat)
        db.save_insight(month, json.dumps(data))
    except Exception as e:
        flash(f'Insight generation failed: {e}')
    return redirect(url_for('index', month=month))


@app.route('/transactions')
@login_required
def transactions():
    month        = request.args.get('month', '')
    account      = request.args.get('account', '')
    expense_type = request.args.get('expense_type', '')
    search       = request.args.get('search', '')
    page         = max(1, int(request.args.get('page', 1) or 1))

    total_count  = db.count_transactions(month=month, account=account,
                                         expense_type=expense_type, search=search)
    total_pages  = max(1, math.ceil(total_count / PER_PAGE))
    page         = min(page, total_pages)
    offset       = (page - 1) * PER_PAGE

    txns     = db.get_transactions(month=month, account=account,
                                   expense_type=expense_type, search=search,
                                   limit=PER_PAGE, offset=offset)
    accounts = db.get_accounts()
    months   = db.get_months()
    total    = sum(t['amount'] for t in txns)

    return render_template('transactions.html',
                           transactions=txns,
                           accounts=accounts,
                           months=months,
                           total=total,
                           selected_month=month,
                           selected_account=account,
                           selected_expense_type=expense_type,
                           search=search,
                           page=page,
                           total_pages=total_pages,
                           total_count=total_count,
                           ai_review=session.pop('ai_review', None),
                           skip_review=session.pop('skip_review', None),
                           etransfer_review=session.pop('etransfer_review', None))


@app.route('/transactions/add', methods=['POST'])
@login_required
def add_transaction():
    data = _form_to_transaction(request.form)
    db.add_transaction(data)
    flash('Transaction added.')
    next_url = request.form.get('next_url')
    if next_url:
        return redirect(next_url)
    return redirect(_transactions_redirect())


@app.route('/transactions/export')
@login_required
def export_transactions():
    month        = request.args.get('month', '')
    account      = request.args.get('account', '')
    expense_type = request.args.get('expense_type', '')
    search       = request.args.get('search', '')
    txns = db.get_transactions(month=month, account=account,
                               expense_type=expense_type, search=search)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Date', 'Account', 'Amount', 'Notes', 'Expense Type', 'Month', 'Bank'])
    for t in txns:
        w.writerow([t['date'], t['account'], t['amount'], t['notes'],
                    t['expense_type'], t['month'], t['bank']])
    filename = f"transactions-{month or 'all'}.csv"
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.route('/transactions/<int:tid>/edit', methods=['POST'])
@login_required
def edit_transaction(tid):
    data = _form_to_transaction(request.form)
    db.update_transaction(tid, data)
    remember = request.form.get('remember') == '1'
    skip     = request.form.get('skip') == '1'
    if (remember or skip) and request.form.get('notes'):
        pattern  = request.form['notes'].strip().lower()
        category = request.form.get('account', '') if remember else ''
        reimb    = float(request.form.get('reimbursement', 0) or 0)
        db.save_merchant_rule(pattern, category, reimb, skip)
    return jsonify(success=True)


@app.route('/merchant-rules', methods=['GET'])
@login_required
def merchant_rules():
    rules = db.get_merchant_rules()
    return render_template('merchant_rules.html', rules=rules)


@app.route('/merchant-rules/<int:rule_id>/delete', methods=['POST'])
@login_required
def delete_merchant_rule(rule_id):
    db.delete_merchant_rule(rule_id)
    return redirect(url_for('merchant_rules'))


@app.route('/merchant-rules/reapply', methods=['POST'])
@login_required
def reapply_merchant_rules():
    updated, unchanged, ai_categorized = importer.reapply_rules_to_existing()
    detail = f' ({ai_categorized} via AI)' if ai_categorized else ''
    flash(f'Reapplied rules: {updated} updated{detail}, {unchanged} unchanged.')
    return redirect(url_for('merchant_rules'))


@app.route('/transactions/<int:tid>/delete', methods=['POST'])
@login_required
def delete_transaction(tid):
    db.delete_transaction(tid)
    return jsonify(success=True)


@app.route('/reimbursements/candidates')
@login_required
def reimbursement_candidates():
    """Recent expenses to pick from when applying an incoming payment."""
    return jsonify(db.get_expenses_for_reimbursement())


@app.route('/reimbursements/apply', methods=['POST'])
@login_required
def apply_reimbursement():
    """Apply (part of) an incoming payment to an expense's reimbursement so a
    repaid split nets out of the expense instead of showing as phantom income.
    An optional `amount` applies only a portion, letting one deposit be split
    across several expenses; omit it to apply as much as the expense can absorb."""
    try:
        payment_id = int(request.form.get('payment_id'))
        expense_id = int(request.form.get('expense_id'))
    except (TypeError, ValueError):
        return jsonify(success=False, error='Invalid transaction ids'), 400
    if payment_id == expense_id:
        return jsonify(success=False, error='Cannot reimburse a row against itself'), 400

    raw_amount = request.form.get('amount')
    amount = None
    if raw_amount not in (None, ''):
        try:
            amount = float(raw_amount)
        except ValueError:
            return jsonify(success=False, error='Invalid amount'), 400
        if amount <= 0:
            return jsonify(success=False, error='Amount must be positive'), 400

    result = db.apply_reimbursement(payment_id, expense_id, amount)
    if result is None:
        return jsonify(success=False,
                       error='Nothing to apply — the expense is already fully '
                             'reimbursed, or the row is missing.'), 400
    return jsonify(success=True, **result)


def _form_to_transaction(form):
    date_raw = form.get('date', '')
    month    = form.get('month', '')
    if not month and date_raw:
        dt = importer.parse_date(date_raw)
        month = importer.month_name(dt) if dt else ''
    return {
        'date':          date_raw,
        'account':       form.get('account', ''),
        'amount':        float(form.get('amount', 0) or 0),
        'notes':         form.get('notes', ''),
        'expense_type':  form.get('expense_type', ''),
        'month':         month,
        'bank':          form.get('bank', ''),
        'reimbursement': float(form.get('reimbursement', 0) or 0),
        'label':         form.get('label', ''),
    }


def _transactions_redirect():
    params = {k: v for k, v in request.form.items()
              if k in ('month', 'account', 'expense_type', 'search') and v}
    return url_for('transactions', **params)


# ── Routes: Net Income ────────────────────────────────────────

@app.route('/net-income')
@login_required
def net_income():
    summary = db.get_net_income_summary()
    year = datetime.now().year
    return render_template('net_income.html', summary=summary, year=year)


# ── Routes: Reports ───────────────────────────────────────────

@app.route('/reports')
@login_required
def reports():
    month         = request.args.get('month', CURRENT_MONTH)
    months        = db.get_months()
    if months and month not in months:
        month = months[-1]
    category_data = db.get_category_report(month)
    need_want     = db.get_need_want_report(month)
    income_data   = db.get_income_report(month)
    total_expense = sum(r['total'] for r in category_data)
    total_income  = sum(r['total'] for r in income_data)

    budgets       = db.get_category_budgets()
    spent_by_acct = {r['account']: r['total'] for r in category_data}
    relevant      = (set(budgets) | set(spent_by_acct)) - db.INCOME_CATEGORIES
    budget_rows   = []
    for account in relevant:
        limit = budgets.get(account)
        spent = spent_by_acct.get(account, 0.0)
        pct   = (spent / limit * 100) if limit else 0.0
        budget_rows.append({
            'account': account,
            'limit':   limit,
            'spent':   spent,
            'pct':     pct,
            'over':    bool(limit) and spent > limit,
        })
    # Budgeted rows first (sorted by % consumed desc), then unbudgeted (by spend desc)
    budget_rows.sort(key=lambda b: (b['limit'] is None, -(b['pct'] if b['limit'] else b['spent'])))
    unbudgeted_accounts = [a['name'] for a in db.get_accounts()
                           if a['name'] not in budgets
                           and a['name'] not in db.INCOME_CATEGORIES]
    total_budget = sum(b['limit'] for b in budget_rows if b['limit'])

    return render_template('reports.html',
                           month=month,
                           months=months,
                           category_data=category_data,
                           need_want=need_want,
                           income_data=income_data,
                           total_expense=total_expense,
                           total_income=total_income,
                           budget_rows=budget_rows,
                           unbudgeted_accounts=unbudgeted_accounts,
                           total_budget=total_budget)


@app.route('/budgets/save', methods=['POST'])
@login_required
def save_budget():
    account = request.form.get('account', '').strip()
    limit   = request.form.get('monthly_limit', '').strip()
    if account and limit:
        try:
            db.save_category_budget(account, float(limit))
        except ValueError:
            pass
    return redirect(request.referrer or url_for('reports'))


@app.route('/budgets/delete', methods=['POST'])
@login_required
def delete_budget():
    account = request.form.get('account', '').strip()
    if account:
        db.delete_category_budget(account)
    return redirect(request.referrer or url_for('reports'))


@app.route('/reports/category-transactions')
@login_required
def category_transactions():
    month   = request.args.get('month', '')
    account = request.args.get('account', '')
    txns    = db.get_transactions(month=month, account=account)
    return jsonify([{
        'id':            t['id'],
        'date':          t['date'],
        'account':       t['account'],
        'notes':         t['notes'],
        'label':         t['label'] or '',
        'amount':        t['amount'],
        'reimbursement': t['reimbursement'] or 0,
        'expense_type':  t['expense_type'],
        'month':         t['month'],
        'bank':          t['bank'],
    } for t in txns])


@app.route('/reports/chart-data')
@login_required
def chart_data():
    month = request.args.get('month', CURRENT_MONTH)
    data  = db.get_category_report(month)
    return jsonify(
        labels  = [r['account'] for r in data],
        amounts = [round(r['total'], 2) for r in data],
    )


# ── Routes: Accounts ──────────────────────────────────────────

@app.route('/accounts')
@login_required
def accounts():
    return render_template('accounts.html', accounts=db.get_accounts())


@app.route('/accounts/add', methods=['POST'])
@login_required
def add_account():
    name = request.form.get('name', '').strip()
    if name:
        db.add_account(name)
        flash(f'Account "{name}" added.')
    return redirect(url_for('accounts'))


@app.route('/accounts/<int:aid>/delete', methods=['POST'])
@login_required
def delete_account(aid):
    db.delete_account(aid)
    return jsonify(success=True)


# ── Routes: Import ────────────────────────────────────────────

@app.route('/import')
@login_required
def import_page():
    return render_template('import.html',
                           logs=db.get_import_logs(),
                           import_folder=str(IMPORT_FOLDER),
                           ai_review=session.pop('ai_review', None),
                           skip_review=session.pop('skip_review', None),
                           etransfer_review=session.pop('etransfer_review', None))


BANK_UPLOAD_EXTS = ('.csv', '.xls', '.xlsx')


@app.route('/import/bank-csv', methods=['POST'])
@login_required
def import_bank_csv():
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        flash('No files selected.')
        return redirect(url_for('import_page'))

    ok, bad, review, skips, etransfers = [], [], [], [], []
    for f in files:
        name = f.filename
        if not name.lower().endswith(BANK_UPLOAD_EXTS):
            bad.append(f'{name}: unsupported (use .csv/.xls/.xlsx)')
            continue
        try:
            content = importer.read_upload_to_csv(f, name)
        except Exception as e:
            bad.append(f'{name}: read error ({e})')
            continue
        added, skipped, bank, ai_review, skip_review, etransfer_review = importer.import_csv_string(content)
        if bank:
            db.log_import(name, bank, added, skipped)
            note = f' · {len(ai_review)} AI-categorized' if ai_review else ''
            note += f' · {len(skip_review)} skipped as transfers' if skip_review else ''
            note += f' · {len(etransfer_review)} e-transfers need labeling' if etransfer_review else ''
            ok.append(f'{name} → {bank}: {added} imported, {skipped} skipped{note}')
            review.extend(ai_review)
            skips.extend(skip_review)
            etransfers.extend(etransfer_review)
        else:
            bad.append(f'{name}: could not detect bank format')

    for line in ok:
        flash(line)
    for line in bad:
        flash(line)
    # Surface AI-categorized, heuristically-skipped, and unlabeled e-transfer
    # rows for review on the next page load (caps keep the session cookie small).
    if review:
        session['ai_review'] = review[:60]
    if skips:
        session['skip_review'] = skips[:40]
    if etransfers:
        session['etransfer_review'] = etransfers[:40]
    return redirect(request.form.get('next') or url_for('import_page'))


@app.route('/import/transactions-csv', methods=['POST'])
@login_required
def import_transactions_csv():
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        flash('No files selected.')
        return redirect(url_for('import_page'))

    for f in files:
        try:
            content = importer.read_upload_to_csv(f, f.filename)
        except Exception as e:
            flash(f'{f.filename}: read error ({e})')
            continue
        added, skipped = importer.import_transactions_csv(content)
        db.log_import(f.filename, 'Transactions Export', added, skipped)
        flash(f'{f.filename}: {added} added, {skipped} skipped')
    return redirect(request.form.get('next') or url_for('import_page'))


@app.route('/import/<int:log_id>/undo', methods=['POST'])
@login_required
def undo_import(log_id):
    deleted = db.undo_import(log_id)
    flash(f'Undone — {deleted} transaction(s) removed.')
    return redirect(url_for('import_page'))


@app.route('/import/review/<int:tid>', methods=['POST'])
@login_required
def review_categorize(tid):
    """Save a reviewed AI categorization: set the transaction's category and
    update the learned merchant rule, so future imports (and API spend) reflect
    the corrected category."""
    category = request.form.get('account', '').strip()
    pattern  = request.form.get('pattern', '').strip().lower()
    txn = db.get_transaction(tid)
    if not txn or not category:
        return jsonify(success=False), 400
    data = dict(txn)
    data['account']      = category
    data['expense_type'] = importer.need_want_label(category)
    db.update_transaction(tid, data)
    if pattern:
        db.save_merchant_rule(pattern, category)
    return jsonify(success=True)


@app.route('/import/label/<int:tid>', methods=['POST'])
@login_required
def import_label(tid):
    """Save what an e-transfer was actually for. Interac e-transfer bank text
    is generic ("INTERAC E-TRANSFER") and usually just names the recipient, so
    it can't be auto-categorized — this lets the user say what it was for and
    pick a category after the fact. Only `account`/`expense_type`/`label` are
    touched; `notes` (the raw bank text used for import dedup) is left alone so
    re-importing the same file still skips this row even after labeling."""
    category = request.form.get('account', '').strip()
    label    = request.form.get('label', '').strip()
    txn = db.get_transaction(tid)
    if not txn or not category:
        return jsonify(success=False), 400
    data = dict(txn)
    data['account']      = category
    data['expense_type'] = importer.need_want_label(category)
    data['label']        = label
    db.update_transaction(tid, data)
    return jsonify(success=True)


@app.route('/import/unskip', methods=['POST'])
@login_required
def import_unskip():
    """Import a row that was auto-skipped as a transfer/payment after all. Drops
    the AI skip rule (if any) so the merchant imports going forward, then adds
    the transaction."""
    try:
        amount = float(request.form.get('amount', 0) or 0)
    except ValueError:
        amount = 0
    date = request.form.get('date', '').strip()
    if not date or amount == 0:
        return jsonify(success=False, error='Missing transaction data'), 400

    pattern = request.form.get('pattern', '').strip().lower()
    if pattern:
        db.remove_skip_rule(pattern)

    new_id = db.add_transaction({
        'date':          date,
        'account':       request.form.get('account', ''),
        'amount':        amount,
        'notes':         request.form.get('notes', ''),
        'expense_type':  request.form.get('expense_type', ''),
        'month':         request.form.get('month', ''),
        'bank':          request.form.get('bank', ''),
        'reimbursement': 0,
    })
    return jsonify(success=True, id=new_id)


# ── Entry point ───────────────────────────────────────────────

if __name__ == '__main__':
    import getpass
    pwd = os.environ.get('BUDGET_PASSWORD') or getpass.getpass('Budget Tracker password: ')
    db.set_password(pwd)
    db.migrate_plaintext_to_encrypted()
    _WEB_PASSWORD = pwd
    _startup()
    import webbrowser
    webbrowser.open('http://localhost:5000')
    app.run(debug=False, port=5000, use_reloader=False, host='0.0.0.0')
