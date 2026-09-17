#!/usr/bin/env python3
"""
Verizon Bill Processor
Reads a PDF from inbox/, sends to Claude API, extracts billing data,
validates totals, and updates data/data.json and data/status.json.
Commits and pushes changes to GitHub automatically.
"""

import os
import sys
import json
import re
import base64
import subprocess
import glob
from datetime import datetime
import anthropic

# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are a billing data extraction assistant for a Verizon family plan.

ACCOUNT NUMBER: 223893379-00001
ACCOUNT HOLDER: Alexander Larson (Alex)
BILLING CYCLE: 7th of each month to 6th of the following month

THE 5 LINES — extract data for all of them:
- Jeff:  971-282-2098  iPhone 16 Pro (Certified)   Unlimited Welcome plan   has device payment + trade-in credit
- Koko:  208-571-6192  iPhone 17 Pro                Unlimited Plus plan      device payment started Aug 2026, trade-in credit active
- Zach:  864-710-6391  iPhone 13 Pro eSIM           5G Start 1.0 plan
- Alex:  904-566-2968  iPhone 16 Pro (Certified)   5G Start 1.0 plan        has device payment + trade-in credit, military service
- Beth:  971-806-0573  iPhone 12 SIM out            5G Start 1.0 plan

MILITARY DISCOUNT: $20/month account-wide credit. Tied to Alex's military service.
Record it in the military_discount field as -20.00. Do NOT add it to any line's total.
Each line's total should be its gross charge with NO portion of the military discount applied.

EXTRACTION RULES:

plan_base_cost: The listed plan rate before any discounts (e.g. 5G Start 1.0 = $40.00, Unlimited Plus = $55.00, Unlimited Welcome = $40.00)

plan_rate_adjustment: The $3.00/month legacy plan rate adjustment Verizon adds to 5G Start 1.0 plans. 0.0 if not present.

autopay_discount: Auto Pay and paper-free billing discount. Record as negative (e.g. -5.00 or -10.00). Varies by plan:
  - 5G Start 1.0 → -5.00
  - Unlimited Plus → -5.00
  - Unlimited Welcome → -10.00
  Always use the actual amount shown on the bill.

net_plan_cost: plan_base_cost + plan_rate_adjustment + autopay_discount

device_payment: Monthly device installment amount. 0.0 if no device payment.
device_payment_number: Current payment number (e.g. 2). 0 if none.
device_payment_total: Total payments in agreement (e.g. 36). 0 if none.
device_balance_remaining: Dollar amount remaining on device agreement. 0.0 if none.

trade_in_credit: Trade-in promo credit as negative number (e.g. -23.05). 0.0 if none.
trade_in_credit_number: Current credit number. Should match device_payment_number. 0 if none.

net_device_cost: device_payment + trade_in_credit (will be positive since trade_in is negative)

one_time_amount: Total of all one-time charges and credits for this line. 0.0 if none. Can be negative.
one_time_description: Plain English description of all one-time items. null if none.
  Examples: "TravelPass Vietnam 2 days $24.00" or "Plan change proration credit -$7.35 + partial charge $9.68" or "Loyalty discount reversal $0.97"

surcharges: Sum of all surcharges (Fed Universal Service, Regulatory, Admin & Telco Recovery, state universal service)

taxes_gov_fees: Sum of all taxes and government fees (state 911 fee, dual party relay, state/county sales taxes)

total: The full line charge as shown on the bill summary page. This is the source of truth.

notes: Any anomaly worth flagging. null if nothing unusual.

EDGE CASES:
- Mid-cycle plan change: Put proration amounts in one_time_amount. net_plan_cost reflects the forward-looking plan.
- TravelPass: Goes in one_time_amount with destination and days in one_time_description.
- International calls: Goes in one_time_amount.
- Slight device payment drift month to month: Normal, record exact amount shown.

VALIDATION NOTE:
After extracting, verify that:
sum(all line totals) + military_discount ≈ account_total (within $0.02)
If not, double-check your extraction before returning.

OUTPUT FORMAT:
Your response must contain ONLY a valid JSON object — no explanation, no markdown fences, no text before or after the JSON.
Start your response with { and end with }.
Exactly match this schema:

{
  "bill_month": "September 2026",
  "billing_period_start": "2026-08-07",
  "billing_period_end": "2026-09-06",
  "due_date": "2026-09-28",
  "invoice_number": "5756198575",
  "account_total": 233.83,
  "military_discount": -20.00,
  "lines": [
    {
      "person": "Jeff",
      "phone": "971-282-2098",
      "device": "Apple iPhone 16 Pro (Certified)",
      "plan": "Unlimited Welcome",
      "plan_base_cost": 40.00,
      "plan_rate_adjustment": 0.00,
      "autopay_discount": -10.00,
      "net_plan_cost": 30.00,
      "device_payment": 36.11,
      "device_payment_number": 16,
      "device_payment_total": 36,
      "device_balance_remaining": 722.20,
      "trade_in_credit": -23.05,
      "trade_in_credit_number": 16,
      "net_device_cost": 13.06,
      "one_time_amount": 0.00,
      "one_time_description": null,
      "surcharges": 4.84,
      "taxes_gov_fees": 1.14,
      "total": 49.04,
      "notes": null
    }
  ]
}

Include all 5 lines in the lines array. Use the person names exactly: Jeff, Koko, Zach, Alex, Beth.
"""

PERSON_ORDER = ['Jeff', 'Koko', 'Zach', 'Alex', 'Beth']

# ── Helper: run git command ───────────────────────────────────────────────────

def git(args):
    result = subprocess.run(['git'] + args, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Git error: {result.stderr}")
    return result

# ── Helper: write status ──────────────────────────────────────────────────────

def write_status(month, success, error=None, invoice=None):
    status_path = 'data/status.json'
    with open(status_path, 'r') as f:
        status = json.load(f)

    today = datetime.now().strftime('%Y-%m-%d')
    entry = {'status': 'success' if success else 'failed', 'processed': today}
    if invoice:
        entry['invoice'] = invoice
    if error:
        entry['error'] = error

    status['months'][month] = entry

    with open(status_path, 'w') as f:
        json.dump(status, f, indent=2)

    return status_path

# ── Helper: extract JSON from response ───────────────────────────────────────

def extract_json(text):
    """Extract JSON object from text even if wrapped in markdown or prose."""
    text = text.strip()

    # Try parsing as-is first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Find outermost { ... } in the text
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass

    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Find PDF
    pdfs = glob.glob('inbox/*.pdf')
    if not pdfs:
        print("No PDFs found in inbox/ — nothing to do.")
        sys.exit(0)

    pdf_path = pdfs[0]
    print(f"Found: {pdf_path}")

    # Read and encode PDF
    with open(pdf_path, 'rb') as f:
        pdf_b64 = base64.standard_b64encode(f.read()).decode('utf-8')

    # Call Claude API
    print("Sending to Claude API...")
    client = anthropic.Anthropic()

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64
                            }
                        },
                        {
                            "type": "text",
                            "text": "Extract all billing data from this Verizon bill. Return ONLY a valid JSON object. Start your response with { and end with }. No other text."
                        }
                    ]
                }
            ]
        )
    except Exception as e:
        print(f"API error: {e}")
        sys.exit(1)

    # Parse JSON response
    raw = response.content[0].text
    print(f"Raw response (first 200 chars): {raw[:200]}")

    bill = extract_json(raw)

    if bill is None:
        print(f"JSON parse error — could not extract JSON from response")
        print(f"Full raw response: {raw[:1000]}")
        sys.exit(1)

    month = bill.get('bill_month', 'Unknown')
    print(f"Extracted: {month}")

    # ── Validate ──────────────────────────────────────────────────────────────
    errors = []

    # Check all 5 people present
    persons_found = [l['person'] for l in bill.get('lines', [])]
    for p in PERSON_ORDER:
        if p not in persons_found:
            errors.append(f"Missing line for {p}")

    # Check account total reconciles
    line_sum = sum(float(l.get('total', 0)) for l in bill.get('lines', []))
    military = float(bill.get('military_discount', -20.00))
    account_total = float(bill.get('account_total', 0))
    calculated = round(line_sum + military, 2)
    expected = round(account_total, 2)

    if abs(calculated - expected) > 0.02:
        errors.append(
            f"Account total mismatch: lines sum to ${line_sum:.2f} + military ${military:.2f} = ${calculated:.2f}, "
            f"but bill shows ${expected:.2f} (off by ${abs(calculated - expected):.2f})"
        )

    # ── Handle failure ────────────────────────────────────────────────────────
    if errors:
        error_msg = ' | '.join(errors)
        print(f"VALIDATION FAILED: {error_msg}")

        status_path = write_status(month, success=False, error=error_msg)

        git(['add', status_path])
        git(['commit', '-m', f'Bill processing failed: {month}'])
        git(['push'])

        print("Status updated. PDF left in inbox for manual review.")
        sys.exit(1)

    # ── Success: write data ───────────────────────────────────────────────────
    print("Validation passed.")

    # Sort lines by PERSON_ORDER
    bill['lines'].sort(key=lambda l: PERSON_ORDER.index(l['person']) if l['person'] in PERSON_ORDER else 99)

    # Load and update data.json
    data_path = 'data/data.json'
    with open(data_path, 'r') as f:
        data = json.load(f)

    bills = data.get('bills', [])

    # Replace if month already exists, otherwise append
    idx = next((i for i, b in enumerate(bills) if b['bill_month'] == month), None)
    if idx is not None:
        bills[idx] = bill
        print(f"Updated existing entry for {month}")
    else:
        bills.append(bill)
        print(f"Added new entry for {month}")

    # Sort newest first
    bills.sort(key=lambda b: b.get('billing_period_start', ''), reverse=True)
    data['bills'] = bills

    with open(data_path, 'w') as f:
        json.dump(data, f, indent=2)

    # Update status
    status_path = write_status(
        month,
        success=True,
        invoice=bill.get('invoice_number')
    )

    # Remove PDF from inbox
    os.remove(pdf_path)

    # Commit everything
    git(['add', data_path, status_path])
    git(['rm', '--cached', pdf_path])  # remove from git tracking (file already deleted)
    git(['commit', '-m', f'Process Verizon bill: {month}'])
    git(['push'])

    print(f"Done. {month} committed to repo and site updated.")


if __name__ == '__main__':
    main()
