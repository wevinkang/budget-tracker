import csv
import io
import logging
import math
import os
import threading
from datetime import datetime
from pathlib import Path

from functools import wraps

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for

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

    return dict(page_url=page_url, _accounts=_accounts, _stats=_stats)


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
                           accounts=accounts)


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
                           total_count=total_count)


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
    if request.form.get('remember') == '1' and request.form.get('notes') and request.form.get('account'):
        pattern = request.form['notes'].strip().lower()
        db.save_merchant_rule(pattern, request.form['account'])
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


@app.route('/transactions/<int:tid>/delete', methods=['POST'])
@login_required
def delete_transaction(tid):
    db.delete_transaction(tid)
    return jsonify(success=True)


def _form_to_transaction(form):
    date_raw = form.get('date', '')
    month    = form.get('month', '')
    if not month and date_raw:
        dt = importer.parse_date(date_raw)
        month = importer.month_name(dt) if dt else ''
    return {
        'date':         date_raw,
        'account':      form.get('account', ''),
        'amount':       float(form.get('amount', 0) or 0),
        'notes':        form.get('notes', ''),
        'expense_type': form.get('expense_type', ''),
        'month':        month,
        'bank':         form.get('bank', ''),
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
    return render_template('reports.html',
                           month=month,
                           months=months,
                           category_data=category_data,
                           need_want=need_want,
                           income_data=income_data,
                           total_expense=total_expense,
                           total_income=total_income)


@app.route('/reports/category-transactions')
@login_required
def category_transactions():
    month   = request.args.get('month', '')
    account = request.args.get('account', '')
    txns    = db.get_transactions(month=month, account=account)
    return jsonify([{
        'date':         t['date'],
        'notes':        t['notes'],
        'amount':       t['amount'],
        'expense_type': t['expense_type'],
        'bank':         t['bank'],
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
                           import_folder=str(IMPORT_FOLDER))


@app.route('/import/bank-csv', methods=['POST'])
@login_required
def import_bank_csv():
    f = request.files.get('file')
    if not f or not f.filename:
        flash('No file selected.')
        return redirect(url_for('import_page'))
    if not f.filename.lower().endswith('.csv'):
        flash('Please upload a .csv file.')
        return redirect(url_for('import_page'))

    try:
        content = f.read().decode('utf-8')
    except UnicodeDecodeError:
        content = f.read().decode('latin-1')

    added, skipped, bank = importer.import_csv_string(content)
    if bank:
        db.log_import(f.filename, bank, added, skipped)
        flash(f'{bank}: {added} transaction(s) imported, {skipped} duplicate(s) skipped.')
    else:
        flash('Could not detect bank format. Make sure the CSV has a header row (Amex, TD, or Simplii).')
    return redirect(url_for('import_page'))


@app.route('/import/transactions-csv', methods=['POST'])
@login_required
def import_transactions_csv():
    f = request.files.get('file')
    if not f or not f.filename:
        flash('No file selected.')
        return redirect(url_for('import_page'))

    try:
        content = f.read().decode('utf-8')
    except UnicodeDecodeError:
        content = f.read().decode('latin-1')

    added, skipped = importer.import_transactions_csv(content)
    db.log_import(f.filename, 'Transactions Export', added, skipped)
    flash(f'Transactions import: {added} transaction(s) added, {skipped} skipped.')
    return redirect(url_for('import_page'))


@app.route('/import/<int:log_id>/undo', methods=['POST'])
@login_required
def undo_import(log_id):
    deleted = db.undo_import(log_id)
    flash(f'Undone — {deleted} transaction(s) removed.')
    return redirect(url_for('import_page'))


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
