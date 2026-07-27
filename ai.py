"""Optional Claude-powered helpers — both strictly opt-in.

Enable by setting BUDGET_AI=1 and ANTHROPIC_API_KEY in the environment (the Pi's
.env file). When disabled, none of this code runs and nothing leaves the device.

Privacy boundary, by design (this app keeps an encrypted SQLCipher DB on a
self-hosted box): the only data ever sent to the API is
  • merchant name *strings* for unknown merchants (no amounts, no account
    numbers, no full transaction rows), and
  • aggregated per-category totals for one month (sums, not individual txns).
"""

import json
import os
import functools

import db

MODEL = os.environ.get('BUDGET_AI_MODEL', 'claude-opus-4-8')

# Sentinel returned by classify_merchant when the merchant is an internal
# transfer / credit-card or line-of-credit payment (already recorded on the
# source statement) and should be skipped rather than categorized.
TRANSFER = '__transfer__'
_TRANSFER_LABEL = 'Transfer or internal payment'


def enabled() -> bool:
    """True only when the user has explicitly opted in and a key is present."""
    return os.environ.get('BUDGET_AI') == '1' and bool(os.environ.get('ANTHROPIC_API_KEY'))


@functools.lru_cache(maxsize=1)
def _client():
    import anthropic
    return anthropic.Anthropic()


def _expense_categories():
    """The category list the model may choose from — the user's own accounts,
    falling back to the seed list before any custom accounts exist."""
    try:
        names = [r['name'] for r in db.get_accounts()]
    except Exception:
        names = []
    return names or list(db.DEFAULT_ACCOUNTS)


# ── Feature 1: categorize an unknown merchant ─────────────────────────────────

def classify_merchant(merchant: str):
    """Return (category, pattern) for an unknown merchant, or None.

    `category` is one of the user's accounts, or the TRANSFER sentinel when the
    merchant is an internal transfer / credit-card payment that should be
    skipped. `pattern` is a short lowercase substring of the merchant suitable
    for saving as a reusable merchant rule. Returns None when the model is unsure
    (so the txn stays uncategorized) or on any API error — categorization must
    never break an import.
    """
    categories = _expense_categories()
    schema = {
        'type': 'object',
        'properties': {
            'category': {'type': 'string', 'enum': categories + [_TRANSFER_LABEL, 'Unknown']},
            'pattern': {'type': 'string'},
        },
        'required': ['category', 'pattern'],
        'additionalProperties': False,
    }
    prompt = (
        "You categorize bank-transaction merchant strings for a personal budget app.\n"
        f"Choose the single best category for this merchant from the list, or "
        f'"Unknown" if you cannot tell confidently.\n\n'
        f'Choose "{_TRANSFER_LABEL}" if the string is not a real purchase but money '
        "moving between the person's own accounts or paying down revolving credit — "
        "e.g. a credit-card payment, line-of-credit (LOC) payment, internal account "
        "transfer, or a bank 'customer transfer'. These are recorded elsewhere, so "
        "they must be excluded from spending.\n\n"
        f"Merchant: {merchant!r}\n\n"
        "Also return `pattern`: a short, lowercase, stable substring that appears "
        "verbatim in the merchant text and identifies this merchant for future "
        "transactions (drop transaction/reference numbers, store numbers, dates).\n"
        f"Allowed categories: {', '.join(categories)}"
    )
    try:
        resp = _client().messages.create(
            model=MODEL,
            max_tokens=256,
            output_config={'format': {'type': 'json_schema', 'schema': schema}},
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = next(b.text for b in resp.content if b.type == 'text')
        data = json.loads(text)
    except Exception:
        return None

    category = data.get('category', 'Unknown')
    is_transfer = category == _TRANSFER_LABEL
    if not is_transfer and (category == 'Unknown' or category not in categories):
        return None

    pattern = (data.get('pattern') or '').lower().strip()
    # The rule only works if the pattern actually occurs in the merchant string;
    # otherwise fall back to the whole merchant so the rule still matches a repeat.
    if not pattern or pattern not in merchant.lower():
        pattern = merchant.lower().strip()
    return (TRANSFER if is_transfer else category), pattern


# ── Feature 2: natural-language spending insights for a month ──────────────────

def monthly_insights(month, category_data, income_data, prev_month, prev_category_data):
    """Return a structured insight object for the month's spending.

    Shape (consumed by the AI Insights dashboard widget):
      {
        'month':      str,
        'income':     float,
        'expenses':   float,
        'net':        float,            # income − expenses (negative = overspend)
        'summary':    str,              # one-sentence hero callout
        'categories': [                 # the month's top expense categories
          {'label', 'current', 'prior', 'delta', 'count', 'share', 'caption'}, ...
        ],
      }

    Only aggregated category totals are sent to the API — never individual
    transactions. Every numeric field is computed locally; the model supplies
    only the narrative `summary` sentence and the per-category captions.
    """
    total_expense = sum(r['total'] for r in category_data)
    total_income = sum(r['total'] for r in income_data)
    net = total_income - total_expense

    prior = {r['account']: r['total'] for r in (prev_category_data or [])}
    categories = []
    for r in category_data[:4]:
        cur = r['total']
        pri = prior.get(r['account'], 0.0)
        categories.append({
            'label': r['account'],
            'current': cur,
            'prior': pri,
            'delta': cur - pri,
            'count': r['count'],
            'share': r['pct'],
            'caption': '',
        })

    parts = [
        f"Month: {month}",
        f"Total income: ${total_income:.2f}",
        f"Total expenses: ${total_expense:.2f}",
        f"Net (income − expenses): ${net:.2f}",
        f"Previous month for comparison: {prev_month or 'n/a'}",
        "Top expense categories this month:",
    ]
    for c in categories:
        parts.append(
            f"  {c['label']}: ${c['current']:.2f} — {c['count']} txns, "
            f"{c['share']:.0f}% of spending; prior month ${c['prior']:.2f}, "
            f"change ${c['delta']:+.2f}"
        )
    summary_input = '\n'.join(parts)

    labels = [c['label'] for c in categories]
    schema = {
        'type': 'object',
        'properties': {
            'summary': {'type': 'string'},
            'captions': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'label': {'type': 'string', 'enum': labels or ['']},
                        'caption': {'type': 'string'},
                    },
                    'required': ['label', 'caption'],
                    'additionalProperties': False,
                },
            },
        },
        'required': ['summary', 'captions'],
        'additionalProperties': False,
    }
    prompt = (
        "You are a concise personal-finance analyst writing copy for a budget "
        "dashboard widget. Using only the aggregated figures below — do not invent "
        "data — produce two things:\n"
        "1. `summary`: a single sentence (~30 words max) for a hero callout stating "
        "whether the month was a net overspend or surplus and the main driver, "
        "referencing the income and expense totals.\n"
        "2. `captions`: for each listed category, one short caption (~12 words max) "
        "noting its share of spending or its month-over-month change. Be specific "
        "with dollar figures; no preamble.\n\n"
        f"{summary_input}"
    )
    resp = _client().messages.create(
        model=MODEL,
        max_tokens=1024,
        output_config={'format': {'type': 'json_schema', 'schema': schema}},
        messages=[{'role': 'user', 'content': prompt}],
    )
    text = next(b.text for b in resp.content if b.type == 'text')
    data = json.loads(text)

    summary = (data.get('summary') or '').strip()
    cap_map = {
        c.get('label'): (c.get('caption') or '').strip()
        for c in data.get('captions', []) if isinstance(c, dict)
    }
    for c in categories:
        ai_cap = cap_map.get(c['label'], '')
        if ai_cap:
            c['caption'] = ai_cap
        elif c['delta'] < 0 and c['prior']:
            c['caption'] = f"Down ${abs(c['delta']):.2f} from ${c['prior']:.2f} last month"
        else:
            c['caption'] = f"{c['share']:.0f}% of total spending · {c['count']} transactions"

    if not summary:
        verb = 'an overspend of' if net < 0 else 'a surplus of'
        summary = (f"Expenses of ${total_expense:.2f} against income of "
                   f"${total_income:.2f} — {verb} ${abs(net):.2f}.")

    return {
        'month': month,
        'income': total_income,
        'expenses': total_expense,
        'net': net,
        'summary': summary,
        'categories': categories,
    }
