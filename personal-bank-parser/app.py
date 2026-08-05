import os
import io
import json
import logging
import time
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation
import tempfile

from flask import Flask, render_template, request, send_file, flash, redirect
from dotenv import load_dotenv
from google import genai

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = "secret_bank_parser_key"

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure Gemini
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ─── Amount Cleaning — handles Indian lakh/crore formatting ─────────────────

def clean_amount(raw) -> Decimal:
    """
    Convert any amount string to Decimal.
    Handles Indian format (1,23,456.78), Western format (123,456.78),
    currency symbols, accounting negatives, CR/DR markers.
    """
    if raw is None:
        return Decimal('0.00')
    
    s = str(raw).strip()
    if not s or s.lower() in ('none', 'null', 'nan', '', '-', '--', 'n/a'):
        return Decimal('0.00')
    
    # Remove currency symbols, whitespace, newlines
    s = re.sub(r'[₹$£€\s\n\r\t]', '', s)
    
    # Remove trailing CR/DR markers
    s = re.sub(r'(?i)\s*(cr|dr)\s*$', '', s)
    
    # Handle accounting negative notation (1,234.56)
    is_negative = False
    if s.startswith('(') and s.endswith(')'):
        is_negative = True
        s = s[1:-1]
    if s.startswith('-'):
        is_negative = True
        s = s[1:]
    
    # Remove all commas (handles both Indian 1,23,456.78 and Western 123,456.78)
    s = s.replace(',', '')
    
    # Remove any remaining non-numeric chars except dot
    s = re.sub(r'[^\d.]', '', s)
    
    # Handle multiple dots (keep only the last one as decimal)
    if s.count('.') > 1:
        parts = s.split('.')
        s = ''.join(parts[:-1]) + '.' + parts[-1]
    
    if not s or s == '.':
        return Decimal('0.00')
    
    try:
        result = Decimal(s)
        if is_negative:
            result = -result
        return result
    except (InvalidOperation, ValueError):
        return Decimal('0.00')


# ─── LLM Interaction Logic (Gemini) ──────────────────────────────────────────

def get_gemini_parsing(file_path):
    """Uploads the PDF to Gemini and expects JSON output with exact amounts."""
    
    prompt = """You are an expert Indian bank statement parser. Analyze the attached PDF bank statement with EXTREME PRECISION on all monetary amounts.

=== DECIMAL vs COMMA — THE MOST CRITICAL RULE ===
In Indian number formatting:
  - COMMA (,) is a THOUSANDS SEPARATOR. It has ZERO effect on the numeric value.
  - PERIOD (.) is the DECIMAL point. Only the digits AFTER the LAST period are paise (cents).
  - Example: "1,23,456.78" → the value is ONE LAKH TWENTY-THREE THOUSAND FOUR HUNDRED FIFTY-SIX and 78 paise = 123456.78
  - NEVER treat a comma as a decimal point. "12,542" is twelve-thousand-five-hundred-forty-two (12542.00), NOT 12.542.
  - NEVER split a number at a comma. "2,12,542.14" is a SINGLE number: 212542.14

CRITICAL RULES FOR AMOUNTS:
1. EXACT DECIMAL RULE: "2,12,542.14" → 212542.14. "1,15,935.00" → 115935.00. NEVER drop or shift the decimal point.
2. A number like "2,12,542" has NO decimal — it is 212542.00. You must add ".00".
3. ZERO-INFLATION RULE: if your extracted balance is 10× or 100× larger than neighbouring balances, you have misread a comma as a decimal — go back and re-read.
4. EVERY amount MUST be returned as a plain decimal STRING with exactly 2 decimal places: "115935.00", "2008.28". No commas, no ₹ symbol.
5. If an amount cell is blank or has a dash (-), return "0.00".
6. Opening Balance row: Debit="0.00", Credit="0.00".

=== DEBIT vs CREDIT — DO NOT SWAP ===
- DEBIT column = money going OUT of the account (withdrawals, payments, UPI paid, EMI, charges).
- CREDIT column = money coming INTO the account (salary, deposits, NEFT received, interest credited, refunds).
- READ each number from the PHYSICAL COLUMN it appears in the PDF table.
- A number in the DEBIT column → "Debit" field. A number in the CREDIT column → "Credit" field.
- Most rows will have EITHER Debit OR Credit, not both. The other field MUST be "0.00".
- If a single "Amount" column exists with CR/DR suffix: "CR" → Credit field; "DR" → Debit field.
- NEVER put a credit-column number in the Debit field, or vice versa.

=== COMPLETENESS — DO NOT MISS ANY ROW ===
1. Scan the ENTIRE PDF from page 1 to the last page.
2. Extract EVERY SINGLE transaction row — even if descriptions span multiple lines.
3. Include the Opening Balance row as the first entry.
4. Multi-line descriptions must be merged into one Description string.
5. Do NOT skip any row, even if amounts seem small or descriptions are long.

EXTRACTION RULES:
1. Extract account details: BankName, AccountNumber, AccountHolder, StatementPeriod, AccountType.
2. Keep the original date string from the PDF as-is.
3. TransactionType: "debit" if money went out, "credit" if money came in, "opening" for opening balance.

SELF-VERIFICATION (mandatory before returning):
For every row after the first:
  Previous_Balance - Debit + Credit MUST equal Current_Balance (tolerance: 1 rupee).
If it does NOT match, you have misread a comma as decimal OR swapped debit/credit.
Go back, re-read the EXACT cell value from the PDF, and correct it before returning.

Return ONLY a valid JSON object with this EXACT schema — no markdown, no code block, just JSON:
{
  "AccountInfo": {
    "BankName": "",
    "AccountNumber": "",
    "AccountHolder": "",
    "StatementPeriod": "",
    "AccountType": ""
  },
  "Transactions": [
    {
      "Date": "",
      "Description": "",
      "Debit": "0.00",
      "Credit": "0.00",
      "Balance": "0.00",
      "TransactionType": ""
    }
  ]
}

FINAL REMINDER: All Debit, Credit, Balance = plain decimal strings, exactly 2 decimal places, no commas, no ₹ symbol. Triple-check every single amount."""
    
    try:
        logger.info("Uploading file to Google Gemini API...")
        uploaded_file = client.files.upload(file=file_path)
        
        # Wait for the file to be processed
        timeout = 120
        start_time = time.time()
        while uploaded_file.state.name == "PROCESSING":
            if time.time() - start_time > timeout:
                logger.error("Gemini file processing timeout.")
                return None
            logger.info("Waiting for PDF to be processed by Gemini...")
            time.sleep(2)
            uploaded_file = client.files.get(name=uploaded_file.name)
            
        if uploaded_file.state.name == "FAILED":
            logger.error("Gemini failed to process the PDF.")
            return None

        logger.info("File uploaded. Generating content with gemini-3.1-pro-preview...")
        response = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=[uploaded_file, prompt],
            config={
                "response_mime_type": "application/json",
                "temperature": 0.0,
            }
        )
        
        # Clean up the file from Google's servers
        client.files.delete(name=uploaded_file.name)
        
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return None


# ─── Post-Processing: Validate & Fix Amounts ─────────────────────────────────

def validate_and_fix_transactions(transactions):
    """
    Post-process Gemini output to ensure amount accuracy.
    Strategy (in priority order):
      1. If debit/credit are swapped → swap them.
      2. If only one of debit/credit is non-zero and reclassifying it fixes balance → reclassify.
      3. AUTHORITATIVE FALLBACK: derive debit/credit from the balance delta.
         balance_delta = curr_bal - prev_bal
         If delta < 0  → it was a debit  (money went OUT)
         If delta >= 0 → it was a credit (money came IN)
    """
    if not transactions:
        return transactions
    
    fixed = []
    errors = 0
    
    for i, txn in enumerate(transactions):
        # Clean all amounts through our robust parser first
        txn['Debit'] = str(clean_amount(txn.get('Debit', '0.00')))
        txn['Credit'] = str(clean_amount(txn.get('Credit', '0.00')))
        txn['Balance'] = str(clean_amount(txn.get('Balance', '0.00')))
        
        if i == 0:
            fixed.append(txn)
            continue
        
        prev_bal = clean_amount(fixed[-1].get('Balance', '0'))
        curr_bal = clean_amount(txn.get('Balance', '0'))
        debit = clean_amount(txn.get('Debit', '0'))
        credit = clean_amount(txn.get('Credit', '0'))
        
        expected = prev_bal - debit + credit
        diff = abs(expected - curr_bal)
        
        if diff <= Decimal('0.02'):
            # ✅ Math checks out — keep as-is
            fixed.append(txn)
            continue
        
        errors += 1
        logger.warning(
            f"Row {i+1} BALANCE MISMATCH: "
            f"prev={prev_bal} debit={debit} credit={credit} → expected={expected}, "
            f"got balance={curr_bal} (diff={diff})"
        )
        
        fixed_this_row = False
        
        # FIX #1: Debit and credit are swapped
        swapped = prev_bal - credit + debit
        if abs(swapped - curr_bal) <= Decimal('0.02'):
            logger.info(f"  → FIX #1 applied: swapped debit/credit")
            txn['Debit'], txn['Credit'] = str(credit), str(debit)
            fixed_this_row = True
        
        # FIX #2a: Reclassify debit-only as credit
        elif debit > 0 and credit == Decimal('0.00'):
            if abs((prev_bal + debit) - curr_bal) <= Decimal('0.02'):
                logger.info(f"  → FIX #2a applied: debit→credit (was credit all along)")
                txn['Credit'] = str(debit)
                txn['Debit'] = '0.00'
                fixed_this_row = True
        
        # FIX #2b: Reclassify credit-only as debit
        elif credit > 0 and debit == Decimal('0.00'):
            if abs((prev_bal - credit) - curr_bal) <= Decimal('0.02'):
                logger.info(f"  → FIX #2b applied: credit→debit (was debit all along)")
                txn['Debit'] = str(credit)
                txn['Credit'] = '0.00'
                fixed_this_row = True
        
        # FIX #3 (AUTHORITATIVE FALLBACK): Derive debit/credit from balance delta.
        # The balance column is a single number — much less likely to be hallucinated
        # than the separate debit/credit columns. Trust it and re-derive.
        if not fixed_this_row:
            balance_delta = curr_bal - prev_bal  # negative = money out, positive = money in
            if balance_delta < Decimal('0.00'):
                correct_debit = abs(balance_delta)
                logger.info(
                    f"  → FIX #3 applied: balance delta {balance_delta} → "
                    f"debit={correct_debit} (was debit={debit}, credit={credit})"
                )
                txn['Debit'] = str(correct_debit)
                txn['Credit'] = '0.00'
            elif balance_delta > Decimal('0.00'):
                correct_credit = balance_delta
                logger.info(
                    f"  → FIX #3 applied: balance delta +{balance_delta} → "
                    f"credit={correct_credit} (was debit={debit}, credit={credit})"
                )
                txn['Credit'] = str(correct_credit)
                txn['Debit'] = '0.00'
            else:
                logger.info(f"  → FIX #3 applied: zero delta → both debit and credit set to 0.00")
                txn['Debit'] = '0.00'
                txn['Credit'] = '0.00'
        
        fixed.append(txn)
    
    if errors > 0:
        logger.warning(f"Total balance mismatches corrected: {errors}")
    else:
        logger.info("✅ All balances verified correctly — zero mismatches!")
    
    return fixed


# ─── PDF Processing ─────────────────────────────────────────────────────────

def process_pdf(file_bytes):
    # Save the file bytes to a temporary file since Gemini's upload_file needs a path
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
        temp_file.write(file_bytes)
        temp_file_path = temp_file.name

    try:
        result = get_gemini_parsing(temp_file_path)
    finally:
        os.remove(temp_file_path)

    if not result:
        return {}, []

    account_info = result.get("AccountInfo", {})
    all_transactions = result.get("Transactions", [])

    logger.info(f"Raw transactions from Gemini: {len(all_transactions)}")

    # Deduplicate transactions
    seen = set()
    unique_txns = []
    for t in all_transactions:
        key = (
            t.get('Date', ''),
            t.get('Description', '')[:50],
            str(clean_amount(t.get('Debit', '0'))),
            str(clean_amount(t.get('Credit', '0'))),
            str(clean_amount(t.get('Balance', '0'))),
        )
        if key not in seen:
            seen.add(key)
            unique_txns.append(t)
    
    logger.info(f"After dedup: {len(unique_txns)} unique transactions")
    
    # Validate and fix amounts using balance chain math
    unique_txns = validate_and_fix_transactions(unique_txns)
            
    return account_info, unique_txns

# ─── XML Generation ─────────────────────────────────────────────────────────

def generate_xml(account_info, transactions):
    """Generate clean XML output with proper decimal formatting."""
    root = ET.Element("BankStatement")
    
    # AccountInfo
    info_el = ET.SubElement(root, "AccountInfo")
    for k, v in account_info.items():
        ET.SubElement(info_el, k).text = str(v)
        
    # Transactions
    txns_el = ET.SubElement(root, "Transactions")
    total_debits = Decimal('0.00')
    total_credits = Decimal('0.00')
    
    for i, t in enumerate(transactions, 1):
        txn_el = ET.SubElement(txns_el, "Transaction", id=str(i))
        for k, v in t.items():
            # Sanitize XML tag names
            tag = re.sub(r'[^a-zA-Z0-9_]', '', k)
            if not tag:
                continue
            
            if k in ('Debit', 'Credit', 'Balance'):
                amount = clean_amount(v)
                ET.SubElement(txn_el, tag).text = f"{amount:.2f}"
                if k == 'Debit':
                    total_debits += amount
                elif k == 'Credit':
                    total_credits += amount
            else:
                ET.SubElement(txn_el, tag).text = str(v) if v else ""
            
    # Summary
    summary_el = ET.SubElement(root, "Summary")
    ET.SubElement(summary_el, "TotalDebits").text = f"{total_debits:.2f}"
    ET.SubElement(summary_el, "TotalCredits").text = f"{total_credits:.2f}"
    ET.SubElement(summary_el, "TotalTransactions").text = str(len(transactions))
    
    # Pretty string
    tree = ET.ElementTree(root)
    out = io.BytesIO()
    tree.write(out, encoding='utf-8', xml_declaration=True)
    return out.getvalue()

# ─── Routes ─────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file uploaded")
            return redirect("/")
            
        file = request.files["file"]
        if file.filename == "":
            flash("No file selected")
            return redirect("/")
            
        if not file.filename.lower().endswith(".pdf"):
            flash("Only PDF files allowed")
            return redirect("/")
            
        try:
            logger.info(f"Processing file: {file.filename}")
            account_info, transactions = process_pdf(file.read())
            
            if not transactions:
                flash("Could not extract any transactions. The PDF might be scanned or in an unsupported format.")
                return redirect("/")
                
            xml_data = generate_xml(account_info, transactions)
            
            # Save a copy locally on the server
            output_dir = os.path.join(app.root_path, "parsed_xmls")
            os.makedirs(output_dir, exist_ok=True)
            local_filename = f"statement_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
            local_path = os.path.join(output_dir, local_filename)
            with open(local_path, "wb") as f:
                f.write(xml_data)
            logger.info(f"Saved XML locally at: {local_path}")
            
            return send_file(
                io.BytesIO(xml_data),
                mimetype="application/xml",
                as_attachment=True,
                download_name=f"parsed_statement_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
            )
        except Exception as e:
            logger.error(f"Processing Error: {e}")
            flash(f"Error processing file: {str(e)}")
            return redirect("/")
            
    return render_template("index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
