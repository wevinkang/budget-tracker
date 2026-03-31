import logging
import threading
from datetime import datetime
from pathlib import Path

from functools import wraps

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

import db
import importer

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)
app.secret_key = 'budget-tracker-2026'

IMPORT_FOLDER = Path.home() / 'budget-imports'
DONE_FOLDER   = IMPORT_FOLDER / 'done'

CURRENT_MONTH = datetime.now().strftime('%B')

# Set at startup from the DB password prompt — reused for the web login
_WEB_PASSWORD = ''


# ── Auth ──────────────────────────────────────────────────────

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
            return redirect(request.args.get('next') or url_for('journal'))
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


# ── Routes: Journal ───────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return redirect(url_for('journal'))


@app.route('/journal')
@login_required
def journal():
    month        = request.args.get('month', '')
    account      = request.args.get('account', '')
    expense_type = request.args.get('expense_type', '')
    search       = request.args.get('search', '')

    transactions = db.get_transactions(month=month, account=account,
                                       expense_type=expense_type, search=search)
    accounts     = db.get_accounts()
    months       = db.get_months()
    total        = sum(t['amount'] for t in transactions)

    return render_template('journal.html',
                           transactions=transactions,
                           accounts=accounts,
                           months=months,
                           total=total,
                           selected_month=month,
                           selected_account=account,
                           selected_expense_type=expense_type,
                           search=search)


@app.route('/journal/add', methods=['POST'])
@login_required
def add_transaction():
    data = _form_to_transaction(request.form)
    db.add_transaction(data)
    flash('Transaction added.')
    return redirect(_journal_redirect())


@app.route('/journal/<int:tid>/edit', methods=['POST'])
@login_required
def edit_transaction(tid):
    data = _form_to_transaction(request.form)
    db.update_transaction(tid, data)
    return jsonify(success=True)


@app.route('/journal/<int:tid>/delete', methods=['POST'])
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


def _journal_redirect():
    params = {k: v for k, v in request.form.items()
              if k in ('month', 'account', 'expense_type', 'search') and v}
    return url_for('journal', **params)


# ── Routes: Net Income ────────────────────────────────────────

@app.route('/net-income')
@login_required
def net_income():
    summary = db.get_net_income_summary()
    return render_template('net_income.html', summary=summary)


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


@app.route('/import/journal-csv', methods=['POST'])
@login_required
def import_journal_csv():
    f = request.files.get('file')
    if not f or not f.filename:
        flash('No file selected.')
        return redirect(url_for('import_page'))

    try:
        content = f.read().decode('utf-8')
    except UnicodeDecodeError:
        content = f.read().decode('latin-1')

    added, skipped = importer.import_journal_csv(content)
    db.log_import(f.filename, 'Journal Export', added, skipped)
    flash(f'Journal import: {added} transaction(s) added, {skipped} skipped.')
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
    pwd = getpass.getpass('Budget Tracker password: ')
    db.set_password(pwd)
    db.migrate_plaintext_to_encrypted()
    _WEB_PASSWORD = pwd
    _startup()
    import webbrowser
    webbrowser.open('http://localhost:5000')
    app.run(debug=False, port=5000, use_reloader=False, host='0.0.0.0')
