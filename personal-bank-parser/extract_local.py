import os
import io
import json
import logging
import time
import re
import xml.etree.ElementTree as ET
from typing import List, Optional
from decimal import Decimal, InvalidOperation
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configure Gemini
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ============================================================
# PYDANTIC SCHEMAS FOR STRICT GEMINI OUTPUT
# ============================================================

class AccountInfoSchema(BaseModel):
    BankName: str = Field(default="", description="Name of the bank")
    AccountNumber: str = Field(default="", description="Account number")
    AccountHolder: str = Field(default="", description="Account holder name")
    StatementPeriod: str = Field(default="", description="Date range of statement")
    AccountType: str = Field(default="", description="Savings, Current, etc.")

class TransactionSchema(BaseModel):
    Date: str = Field(description="Transaction date string from document")
    Description: str = Field(description="Narrative or particulars of transaction")
    Debit: str = Field(default="0.00", description="Withdrawal / money out as plain decimal string (e.g. 1500.00)")
    Credit: str = Field(default="0.00", description="Deposit / money in as plain decimal string (e.g. 0.00)")
    Balance: str = Field(default="0.00", description="Closing balance as plain decimal string")
    TransactionType: str = Field(default="", description="'debit', 'credit', or 'opening'")

class StatementOutputSchema(BaseModel):
    AccountInfo: AccountInfoSchema
    Transactions: List[TransactionSchema]

# ============================================================
# AMOUNT CLEANING
# ============================================================

def clean_amount(raw) -> Decimal:
    """Convert any amount string to Decimal cleanly."""
    if raw is None:
        return Decimal('0.00')
    
    s = str(raw).strip()
    if not s or s.lower() in ('none', 'null', 'nan', '', '-', '--', 'n/a'):
        return Decimal('0.00')
    
    s = re.sub(r'[₹$£€\s\n\r\t]', '', s)
    s = re.sub(r'(?i)\s*(cr|dr)\s*$', '', s)
    
    is_negative = False
    if s.startswith('(') and s.endswith(')'):
        is_negative = True
        s = s[1:-1]
    if s.startswith('-'):
        is_negative = True
        s = s[1:]
    
    s = s.replace(',', '')
    s = re.sub(r'[^\d.]', '', s)
    
    if s.count('.') > 1:
        parts = s.split('.')
        s = ''.join(parts[:-1]) + '.' + parts[-1]
    
    if not s or s == '.':
        return Decimal('0.00')
    
    try:
        result = Decimal(s)
        return -result if is_negative else result
    except (InvalidOperation, ValueError):
        return Decimal('0.00')

# ============================================================
# GEMINI AI PARSER
# ============================================================

SYSTEM_INSTRUCTION = """You are an expert Indian bank statement parser. Analyze the attached PDF bank statement with EXTREME PRECISION on all monetary amounts.

=== DECIMAL vs COMMA ===
In Indian number formatting:
  - COMMA (,) is a THOUSANDS SEPARATOR. It has ZERO effect on numeric value.
  - PERIOD (.) is the DECIMAL point.
  - "1,23,456.78" → 123456.78
  - NEVER treat a comma as a decimal point. "12,542" is twelve-thousand-five-hundred-forty-two (12542.00).

CRITICAL RULES FOR AMOUNTS:
1. "2,12,542.14" → 212542.14. Never drop or shift the decimal point.
2. Return amounts as plain decimal strings with 2 decimal places: "115935.00". No commas, no ₹ symbol.
3. Blank/dash cells = "0.00".

=== DEBIT vs CREDIT ===
- DEBIT column = money going OUT (withdrawals, payments, UPI paid, charges).
- CREDIT column = money coming IN (salary, deposits, NEFT received, refunds).
- Read each number from its physical column. Do NOT swap columns.

=== COMPLETENESS ===
1. Scan the entire PDF from start to end.
2. Extract every single row. Merge multi-line descriptions into one string.
"""

def get_gemini_parsing(file_path):
    try:
        logger.info("Uploading file to Google Gemini API...")
        uploaded_file = client.files.upload(file=file_path)
        
        timeout = 120
        start_time = time.time()
        while uploaded_file.state.name == "PROCESSING":
            if time.time() - start_time > timeout:
                logger.error("Gemini file processing timeout.")
                return None
            time.sleep(2)
            uploaded_file = client.files.get(name=uploaded_file.name)
            
        if uploaded_file.state.name == "FAILED":
            logger.error("Gemini failed to process the PDF.")
            return None

        logger.info("Generating structured content with gemini-3.1-pro-preview...")
        response = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=[uploaded_file, "Extract all account info and transactions."],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=StatementOutputSchema,
                temperature=0.0,
            )
        )
        
        client.files.delete(name=uploaded_file.name)
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return None

# ============================================================
# SAFE BALANCE RECONCILIATION
# ============================================================

def validate_and_fix_transactions(transactions):
    if not transactions:
        return transactions
    
    fixed = []
    errors = 0
    
    for i, txn in enumerate(transactions):
        txn['Debit'] = f"{clean_amount(txn.get('Debit', '0.00')):.2f}"
        txn['Credit'] = f"{clean_amount(txn.get('Credit', '0.00')):.2f}"
        txn['Balance'] = f"{clean_amount(txn.get('Balance', '0.00')):.2f}"
        
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
            fixed.append(txn)
            continue
        
        errors += 1
        logger.warning(
            f"Row {i+1} BALANCE MISMATCH: prev={prev_bal} debit={debit} credit={credit} → expected={expected}, got balance={curr_bal}"
        )
        
        fixed_this_row = False
        
        # FIX #1: Debit and credit swapped
        if abs((prev_bal - credit + debit) - curr_bal) <= Decimal('0.02'):
            logger.info("  → FIX #1 applied: swapped debit/credit")
            txn['Debit'], txn['Credit'] = f"{credit:.2f}", f"{debit:.2f}"
            fixed_this_row = True
        
        # FIX #2a: Debit reclassified as Credit
        elif debit > 0 and credit == Decimal('0.00'):
            if abs((prev_bal + debit) - curr_bal) <= Decimal('0.02'):
                logger.info("  → FIX #2a applied: debit→credit")
                txn['Credit'] = f"{debit:.2f}"
                txn['Debit'] = '0.00'
                fixed_this_row = True
        
        # FIX #2b: Credit reclassified as Debit
        elif credit > 0 and debit == Decimal('0.00'):
            if abs((prev_bal - credit) - curr_bal) <= Decimal('0.02'):
                logger.info("  → FIX #2b applied: credit→debit")
                txn['Debit'] = f"{credit:.2f}"
                txn['Credit'] = '0.00'
                fixed_this_row = True
        
        # FIX #3: Guarded Balance Delta Fallback
        if not fixed_this_row:
            balance_delta = curr_bal - prev_bal
            abs_delta = abs(balance_delta)
            
            # Guard against corrupted balance digits: only trust delta if it closely matches extracted debit/credit
            if abs(abs_delta - debit) <= Decimal('0.05') or abs(abs_delta - credit) <= Decimal('0.05') or (debit == Decimal('0.00') and credit == Decimal('0.00')):
                if balance_delta < Decimal('0.00'):
                    txn['Debit'] = f"{abs_delta:.2f}"
                    txn['Credit'] = '0.00'
                    logger.info(f"  → FIX #3 applied: derived debit {abs_delta}")
                elif balance_delta > Decimal('0.00'):
                    txn['Credit'] = f"{abs_delta:.2f}"
                    txn['Debit'] = '0.00'
                    logger.info(f"  → FIX #3 applied: derived credit {abs_delta}")
                else:
                    txn['Debit'] = '0.00'
                    txn['Credit'] = '0.00'
            else:
                logger.warning("  → FIX #3 skipped: balance delta looks corrupt relative to debit/credit.")

        fixed.append(txn)
    
    if errors > 0:
        logger.warning(f"Total balance mismatches corrected: {errors}")
    else:
        logger.info("✅ All balances verified correctly — zero mismatches!")
    
    return fixed

# ============================================================
# MAIN ORCHESTRATION & XML GENERATION
# ============================================================

def process_pdf(file_path):
    logger.info(f"Reading file: {file_path}")
    result = get_gemini_parsing(file_path)
    
    if not result:
        return {}, []
        
    account_info = result.get("AccountInfo", {})
    all_transactions = result.get("Transactions", [])
    
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
    
    return account_info, validate_and_fix_transactions(unique_txns)

def generate_xml(account_info, transactions):
    root = ET.Element("BankStatement")
    
    info_el = ET.SubElement(root, "AccountInfo")
    for k, v in account_info.items():
        ET.SubElement(info_el, k).text = str(v)
    
    txns_el = ET.SubElement(root, "Transactions")
    total_debits = Decimal('0.00')
    total_credits = Decimal('0.00')
    
    for i, t in enumerate(transactions, 1):
        txn_el = ET.SubElement(txns_el, "Transaction", id=str(i))
        for k, v in t.items():
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
    
    summary_el = ET.SubElement(root, "Summary")
    ET.SubElement(summary_el, "TotalDebits").text = f"{total_debits:.2f}"
    ET.SubElement(summary_el, "TotalCredits").text = f"{total_credits:.2f}"
    ET.SubElement(summary_el, "TotalTransactions").text = str(len(transactions))
    
    tree = ET.ElementTree(root)
    out = io.BytesIO()
    tree.write(out, encoding='utf-8', xml_declaration=True)
    return out.getvalue()

if __name__ == "__main__":
    import sys
    pdf_path = "statement.pdf"
    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
        
    if not os.path.exists(pdf_path):
        print(f"Error: File {pdf_path} not found.")
        sys.exit(1)

    account_info, transactions = process_pdf(pdf_path)
    if transactions:
        xml_data = generate_xml(account_info, transactions)
        output_file = os.path.join(os.path.dirname(pdf_path), "parsed_output.xml")
        with open(output_file, "wb") as f:
            f.write(xml_data)
        
        print(f"\n{'='*60}")
        print("✅ SUCCESS!")
        print(f"Transactions Extracted: {len(transactions)}")
        print(f"XML saved at: {output_file}")
        print(f"{'='*60}\n")
    else:
        print("\n❌ FAILED: No transactions could be extracted.")