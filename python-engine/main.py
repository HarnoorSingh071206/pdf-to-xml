from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import pikepdf
import io
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
import logging
from typing import Optional, List, Dict, Tuple
from google import genai
from dotenv import load_dotenv
import os
import tempfile
import time
import json
from decimal import Decimal, InvalidOperation
from pydantic import BaseModel, Field, validator
import hashlib

load_dotenv()
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("repotic-engine")

app = FastAPI(title="Repotic Python Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# PYDANTIC MODELS WITH BUILT-IN VALIDATION
# ============================================================

class Transaction(BaseModel):
    date: str
    cheque_number: str = ""
    narration: str = ""
    debit: Decimal = Field(default=Decimal('0.00'))
    credit: Decimal = Field(default=Decimal('0.00'))
    balance: Optional[Decimal] = None
    
    @validator('debit', 'credit', pre=True)
    def clean_decimal(cls, v):
        if v is None or v == '':
            return Decimal('0.00')
        return Decimal(str(v).replace(',', ''))
    
    @validator('balance', pre=True)
    def clean_balance(cls, v):
        if v is None or v == '':
            return None
        try:
            return Decimal(str(v).replace(',', ''))
        except:
            return None
    
    class Config:
        json_encoders = {Decimal: str}

# ============================================================
# AMOUNT & DATE UTILITIES
# ============================================================

def clean_amount(raw) -> Decimal:
    """Convert any amount string to Decimal, handling all edge cases"""
    if not raw:
        return Decimal('0.00')
    try:
        s = str(raw).replace("\n", "").replace("\r", "").strip()
        s = re.sub(r'[₹$£€\s]', '', s)
        s = s.replace(",", "")
        # Handle parentheses (accounting negative notation)
        if s.startswith('(') and s.endswith(')'):
            s = '-' + s[1:-1]
        s = re.sub(r'[^\d.\-]', '', s)
        if not s or s in ['.', '-', '-.']:
            return Decimal('0.00')
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal('0.00')

MONTH_MAP = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
}

DATE_PATTERNS = [
    re.compile(r'(\d{1,2})[/\-\.\s](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[/\-\.\s](\d{2,4})', re.IGNORECASE),
    re.compile(r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})'),
    re.compile(r'(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})'),
    re.compile(r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2})'),
]

def normalise_date(raw: str) -> str:
    """Normalize any date format to DD-MM-YYYY"""
    if not raw:
        return ""
    text = re.sub(r'(\d)\n(\d)', r'\1\2', str(raw))
    text = re.sub(r'[\n\r]+', ' ', text).strip()
    
    for i, pattern in enumerate(DATE_PATTERNS):
        m = pattern.search(text)
        if m:
            if i == 0:
                day = m.group(1).zfill(2)
                mon = MONTH_MAP.get(m.group(2).lower(), '00')
                yr = m.group(3)
                if len(yr) == 2:
                    yr = ('20' if int(yr) < 50 else '19') + yr
                return f"{day}-{mon}-{yr}"
            elif i == 1:
                return f"{m.group(1).zfill(2)}-{m.group(2).zfill(2)}-{m.group(3)}"
            elif i == 2:
                return f"{m.group(3).zfill(2)}-{m.group(2).zfill(2)}-{m.group(1)}"
            elif i == 3:
                day, mon, yr = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
                yr = ('20' if int(yr) < 50 else '19') + yr
                return f"{day}-{mon}-{yr}"
    return ""

# ============================================================
# HEADER DETECTION & COLUMN MAPPING
# ============================================================

HEADER_KEYWORDS = {
    "date": ["date", "value date", "txn date", "transaction date", "trans date", "posting date", "tran date"],
    "narration": ["particulars", "narration", "description", "remarks", "details", "transaction remarks", "transaction details"],
    "cheque_number": ["cheque", "chq", "chq no", "cheque no", "ref no", "reference", "ref.", "instrument no"],
    "debit": ["debit", "withdrawal", "withdrawals", "dr", "withdra", "debits"],
    "credit": ["credit", "deposit", "deposits", "cr", "credits"],
    "balance": ["balance", "closing balance", "available balance"],
}

def detect_columns(headers: List[str]) -> Dict[str, int]:
    """Map column names to their indices using fuzzy matching"""
    mapping = {k: -1 for k in HEADER_KEYWORDS}
    
    for i, cell in enumerate(headers):
        if not cell:
            continue
        c = re.sub(r'[\n\r\s]+', ' ', str(cell)).lower().strip()
        for role, keywords in HEADER_KEYWORDS.items():
            if mapping[role] == -1 and any(k in c for k in keywords):
                mapping[role] = i
                break
    
    # If no credit/debit columns found but "amount" exists
    if mapping["credit"] == -1 and mapping["debit"] == -1:
        for i, cell in enumerate(headers):
            c = re.sub(r'[\n\r\s]+', ' ', str(cell or '')).lower().strip()
            if "amount" in c and i not in mapping.values():
                mapping["credit"] = i  # Will be resolved later
                break
    
    return mapping

# ============================================================
# THE GOLDEN FUNCTION: BALANCE RECONCILIATION
# ============================================================

def reconcile_and_fix_transactions(transactions: List[Dict]) -> List[Dict]:
    """
    THE CRITICAL FIX: Validate every transaction using balance math.
    Strategy (in priority order):
      1. If debit/credit are swapped → swap them.
      2. If reclassifying fixes the balance → reclassify.
      3. AUTHORITATIVE FALLBACK: derive debit/credit from the balance delta.
         balance_delta = curr_bal - prev_bal
         If delta < 0  → it was a debit  (money went OUT)
         If delta >= 0 → it was a credit (money came IN)
         The balance column is a single number — much harder for Gemini to
         misread than the separate debit/credit columns.
    """
    if len(transactions) < 2:
        return transactions
    
    fixed_transactions = []
    
    for i, txn in enumerate(transactions):
        if i == 0:
            fixed_transactions.append(txn)
            continue
        
        prev = fixed_transactions[-1]
        prev_balance = prev.get("balance")
        curr_balance = txn.get("balance")
        
        # Skip validation if balances are missing
        if prev_balance is None or curr_balance is None:
            fixed_transactions.append(txn)
            continue
        
        try:
            prev_bal = clean_amount(prev_balance)
            curr_bal = clean_amount(curr_balance)
            debit = clean_amount(txn.get("debit", "0"))
            credit = clean_amount(txn.get("credit", "0"))
            
            expected_balance = prev_bal - debit + credit
            
            if abs(expected_balance - curr_bal) <= Decimal('0.01'):
                # ✅ Math checks out
                fixed_transactions.append(txn)
                continue
            
            # BALANCE MISMATCH DETECTED
            logger.warning(f"Row {i}: Balance mismatch! Expected {expected_balance}, got {curr_bal}")
            fixed_this_row = False
            
            # FIX #1: Maybe debit and credit are swapped?
            expected_swapped = prev_bal - credit + debit
            if abs(expected_swapped - curr_bal) <= Decimal('0.01'):
                logger.info(f"Row {i}: FIX #1 — swapped debit/credit")
                txn["debit"], txn["credit"] = str(credit), str(debit)
                fixed_this_row = True
            
            # FIX #2a: Reclassify debit-only as credit
            elif debit > 0 and credit == Decimal('0.00'):
                if abs((prev_bal + debit) - curr_bal) <= Decimal('0.01'):
                    logger.info(f"Row {i}: FIX #2a — debit→credit")
                    txn["credit"] = str(debit)
                    txn["debit"] = "0.00"
                    fixed_this_row = True
            
            # FIX #2b: Reclassify credit-only as debit
            elif credit > 0 and debit == Decimal('0.00'):
                if abs((prev_bal - credit) - curr_bal) <= Decimal('0.01'):
                    logger.info(f"Row {i}: FIX #2b — credit→debit")
                    txn["debit"] = str(credit)
                    txn["credit"] = "0.00"
                    fixed_this_row = True
            
            # FIX #3 (AUTHORITATIVE FALLBACK): Derive from balance delta
            if not fixed_this_row:
                balance_delta = curr_bal - prev_bal
                if balance_delta < Decimal('0.00'):
                    correct_debit = abs(balance_delta)
                    logger.info(f"Row {i}: FIX #3 — delta {balance_delta} → debit={correct_debit} (was debit={debit}, credit={credit})")
                    txn["debit"] = str(correct_debit)
                    txn["credit"] = "0.00"
                elif balance_delta > Decimal('0.00'):
                    correct_credit = balance_delta
                    logger.info(f"Row {i}: FIX #3 — delta +{balance_delta} → credit={correct_credit} (was debit={debit}, credit={credit})")
                    txn["credit"] = str(correct_credit)
                    txn["debit"] = "0.00"
                else:
                    logger.info(f"Row {i}: FIX #3 — zero delta → debit=0.00 credit=0.00")
                    txn["debit"] = "0.00"
                    txn["credit"] = "0.00"
            
            fixed_transactions.append(txn)
            
        except Exception as e:
            logger.error(f"Error reconciling row {i}: {e}")
            fixed_transactions.append(txn)
    
    return fixed_transactions

# ============================================================
# EXTRACTION ENGINE
# ============================================================

def extract_transactions_from_page(page) -> List[Dict]:
    """Extract transactions from a single PDF page using tables + text fallback"""
    transactions = []
    
    # Strategy 1: Table extraction
    tables = page.extract_tables()
    if not tables:
        tables = page.extract_tables(table_settings={
            "vertical_strategy": "text", 
            "horizontal_strategy": "text"
        })
    
    if tables:
        for table in tables:
            if not table or len(table) < 2:
                continue
            
            # Find header row
            header_idx = -1
            column_map = None
            
            for i, row in enumerate(table[:10]):  # Check first 10 rows for headers
                if not row:
                    continue
                col_map = detect_columns(row)
                if col_map["date"] != -1 and (col_map["narration"] != -1 or col_map["debit"] != -1 or col_map["credit"] != -1):
                    header_idx = i
                    column_map = col_map
                    break
            
            if not column_map:
                continue
            
            # Extract rows after header
            for row in table[header_idx + 1:]:
                if not row or all(cell is None or str(cell).strip() == '' for cell in row):
                    continue
                
                date_val = normalise_date(str(row[column_map["date"]] or ""))
                if not date_val:
                    continue
                
                narration = str(row[column_map["narration"]] or "").replace("\n", " ").strip() if column_map["narration"] != -1 else ""
                cheque_number = str(row[column_map.get("cheque_number", -1)] or "").replace("\n", " ").strip() if column_map.get("cheque_number", -1) != -1 else ""
                
                # Try to extract cheque from narration if it's empty
                if not cheque_number and narration:
                    chq_match = re.search(r'(?i)(?:chq|cheque)[\s\.]*(?:no[\s\.]*)?(\d{6})', narration)
                    if chq_match:
                        cheque_number = chq_match.group(1)
                
                debit = clean_amount(row[column_map["debit"]]) if column_map["debit"] != -1 else Decimal('0.00')
                credit = clean_amount(row[column_map["credit"]]) if column_map["credit"] != -1 else Decimal('0.00')
                
                # Handle single amount column
                if column_map["credit"] != -1 and debit == Decimal('0.00') and credit == Decimal('0.00'):
                    # Check if this is actually the amount column
                    for i, cell in enumerate(row):
                        c = str(cell or '').lower().strip()
                        if 'cr' in c or 'dr' in c:
                            amt = clean_amount(re.sub(r'[a-zA-Z]', '', str(cell or '')))
                            if 'dr' in c:
                                debit = amt
                            else:
                                credit = amt
                            break
                
                balance = clean_amount(row[column_map["balance"]]) if column_map["balance"] != -1 else None
                
                transactions.append({
                    "date": date_val,
                    "cheque_number": cheque_number,
                    "narration": narration,
                    "debit": str(debit),
                    "credit": str(credit),
                    "balance": str(balance) if balance else None
                })
    
    # Strategy 2: Text fallback for non-table PDFs
    if not transactions:
        text = page.extract_text()
        if text:
            transactions = extract_from_text_fallback(text)
    
    return transactions

def extract_from_text_fallback(text: str) -> List[Dict]:
    """Fallback text-based extraction for non-table PDFs"""
    lines = text.split('\n')
    transactions = []
    AMOUNT_PATTERN = re.compile(r'[\d,]+\.?\d{0,2}')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Try to find a date
        date_val = normalise_date(line)
        if not date_val:
            continue
        
        # Find all amounts
        amounts = [clean_amount(m.group()) for m in AMOUNT_PATTERN.finditer(line)]
        amounts = [a for a in amounts if a > 0]
        
        if len(amounts) < 2:
            continue
        
        # Last amount is usually balance
        balance = amounts[-1]
        
        if len(amounts) == 2:
            # Only one transaction amount + balance
            amt = amounts[0]
            # Determine credit/debit from keywords
            line_lower = line.lower()
            if any(kw in line_lower for kw in ['cr', 'credit', 'deposit', 'by', 'received']):
                debit, credit = Decimal('0.00'), amt
            else:
                debit, credit = amt, Decimal('0.00')
        else:
            # Multiple amounts: penultimate is usually the transaction amount
            credit = amounts[-2]
            debit = amounts[-3] if len(amounts) >= 3 else Decimal('0.00')
        
        # Extract narration
        narration = line
        for a in amounts:
            narration = narration.replace(str(a), '', 1)
        narration = re.sub(r'\s+', ' ', narration).strip()
        narration = re.sub(r'^\d+[\s\-\.]+', '', narration)  # Remove leading serial numbers
        
        # Try to extract cheque from narration
        cheque_number = ""
        chq_match = re.search(r'(?i)(?:chq|cheque)[\s\.]*(?:no[\s\.]*)?(\d{6})', narration)
        if chq_match:
            cheque_number = chq_match.group(1)
        
        transactions.append({
            "date": date_val,
            "cheque_number": cheque_number,
            "narration": narration,
            "debit": str(debit),
            "credit": str(credit),
            "balance": str(balance)
        })
    
    return transactions

# ============================================================
# PDF DECRYPTION
# ============================================================

def decrypt_pdf(contents: bytes, filename: str, password: Optional[str] = None) -> Tuple[bytes, bool, Optional[str]]:
    """Decrypt PDF with auto-unlock capability"""
    try:
        pdf = pikepdf.open(io.BytesIO(contents))
        out = io.BytesIO()
        pdf.save(out)
        out.seek(0)
        return out.read(), False, None
    except pikepdf.PasswordError:
        pass
    
    if password:
        try:
            pdf = pikepdf.open(io.BytesIO(contents), password=password)
            out = io.BytesIO()
            pdf.save(out)
            out.seek(0)
            return out.read(), True, None
        except pikepdf.PasswordError:
            return None, True, "wrong_password"
    
    # Auto-unlock attempts
    for guess in ["1234", "0000", "1111", "password", "admin"] + re.findall(r'\d{4,}', filename):
        try:
            pdf = pikepdf.open(io.BytesIO(contents), password=guess)
            out = io.BytesIO()
            pdf.save(out)
            out.seek(0)
            logger.info(f"Auto-unlocked with: {guess}")
            return out.read(), True, None
        except pikepdf.PasswordError:
            continue
    
    return None, True, "needs_password"

# ============================================================
# XML GENERATION
# ============================================================

def transactions_to_xml(transactions: List[Dict], filename: str = "statement") -> str:
    """Convert transactions to Tally-compatible XML"""
    root = ET.Element("BankStatement")
    root.set("source", filename)
    root.set("totalTransactions", str(len(transactions)))
    
    for i, txn in enumerate(transactions, 1):
        t = ET.SubElement(root, "Transaction")
        t.set("id", str(i))
        for field in ["date", "cheque_number", "narration", "debit", "credit", "balance"]:
            ET.SubElement(t, field.capitalize()).text = str(txn.get(field, ""))
    
    raw_xml = ET.tostring(root, encoding="unicode")
    return minidom.parseString(raw_xml).toprettyxml(indent="  ", encoding=None)

# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/")
async def health():
    return {"status": "online", "engine": "repotic-v2"}

@app.post("/extract-statement/")
async def extract_statement(file: UploadFile = File(...), password: Optional[str] = Form(None)):
    """Main extraction endpoint with balance reconciliation"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files supported")
    
    raw = await file.read()
    contents, is_enc, err = decrypt_pdf(raw, file.filename, password)
    
    if err == "needs_password":
        return {"status": "encrypted", "message": "Password required"}
    if err == "wrong_password":
        raise HTTPException(status_code=401, detail="Incorrect password")
    
    all_transactions = []
    
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for page in pdf.pages:
            page_transactions = extract_transactions_from_page(page)
            all_transactions.extend(page_transactions)
    
    # 🔥 THE CRITICAL FIX: Reconcile balances to catch errors
    all_transactions = reconcile_and_fix_transactions(all_transactions)
    
    # Deduplicate
    seen = set()
    unique = []
    for txn in all_transactions:
        key = (txn.get("date"), txn.get("cheque_number", ""), txn.get("narration", "")[:40], txn.get("debit"), txn.get("credit"), txn.get("balance"))
        if key not in seen:
            seen.add(key)
            unique.append(txn)
    
    if not unique:
        raise HTTPException(status_code=422, detail="No transactions found")
    
    return {
        "status": "success",
        "data": unique,
        "metadata": {
            "total_transactions": len(unique),
            "bank_name": file.filename
        }
    }

@app.post("/extract-statement-ai/")
async def extract_statement_ai(
    file: UploadFile = File(...),
    password: Optional[str] = Form(None),
):
    """AI-powered extraction with Gemini + reconciliation"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files supported")
    
    raw = await file.read()
    contents, is_enc, err = decrypt_pdf(raw, file.filename, password)
    
    if err == "needs_password":
        return {"status": "encrypted", "message": "Password required"}
    if err == "wrong_password":
        raise HTTPException(status_code=401, detail="Incorrect password")
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
        temp_file.write(contents)
        temp_file_path = temp_file.name
    
    try:
        uploaded_file = gemini_client.files.upload(file=temp_file_path)
        
        # Wait for processing
        timeout = 60
        start_time = time.time()
        while uploaded_file.state.name == "PROCESSING":
            if time.time() - start_time > timeout:
                raise HTTPException(status_code=504, detail="Gemini processing timeout")
            time.sleep(2)
            uploaded_file = gemini_client.files.get(name=uploaded_file.name)
        
        if uploaded_file.state.name == "FAILED":
            raise HTTPException(status_code=500, detail="Gemini failed to process document")
        
        # Precision-focused prompt with Indian number format awareness
        prompt = """You are an expert Indian bank statement parser. Extract ALL transactions from this PDF with EXTREME PRECISION on all monetary amounts.

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
6. Opening Balance row: debit="0.00", credit="0.00".

=== DEBIT vs CREDIT — DO NOT SWAP ===
- DEBIT column = money going OUT of the account (withdrawals, payments, UPI paid, EMI, charges).
- CREDIT column = money coming INTO the account (salary, deposits, NEFT received, interest credited, refunds).
- READ each number from the PHYSICAL COLUMN it appears in the PDF table.
- A number in the DEBIT column → "debit" field. A number in the CREDIT column → "credit" field.
- Most rows will have EITHER debit OR credit, not both. The other field MUST be "0.00".
- If a single "Amount" column exists with CR/DR suffix: "CR" → credit field; "DR" → debit field.
- NEVER put a credit-column number in the debit field, or vice versa.

=== COMPLETENESS — DO NOT MISS ANY ROW ===
1. Scan the ENTIRE PDF from page 1 to the last page.
2. Extract EVERY SINGLE transaction row — even if descriptions span multiple lines.
3. Include the opening balance row as the first entry.
4. Multi-line descriptions must be merged into one narration string.
5. Do NOT skip any row, even if amounts seem small or descriptions are long.

EXTRACTION RULES:
1. Date format: DD-MM-YYYY
2. Extract cheque_number or reference number from a dedicated column, or from within the narration text.
3. Include opening balance as first row if present.
4. Do NOT skip any transaction.
5. If balance column is empty, calculate it from prev_balance and the debit/credit.

SELF-VERIFICATION (mandatory before returning):
For every row after the first:
  Previous_Balance - Debit + Credit MUST equal Current_Balance (tolerance: 1 rupee).
If it does NOT match, you have misread a comma as decimal OR swapped debit/credit.
Go back, re-read the EXACT cell value from the PDF, and correct it before returning.

Return ONLY valid JSON — no markdown, no code block, just JSON:
{
  "Transactions": [
    {
      "date": "DD-MM-YYYY",
      "cheque_number": "cheque or ref no if any, else empty string",
      "narration": "description",
      "debit": "0.00",
      "credit": "0.00",
      "balance": "0.00"
    }
  ]
}

FINAL REMINDER: All debit, credit, balance = plain decimal strings, exactly 2 decimal places, no commas, no symbols. Triple-check every single amount."""
        
        response = gemini_client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=[uploaded_file, prompt],
            config={
                "response_mime_type": "application/json",
                "temperature": 0.0,
            }
        )
        gemini_client.files.delete(name=uploaded_file.name)
        
        result = json.loads(response.text)
        ai_transactions = result.get("Transactions", [])
        
        # Convert to our format — clean every amount through clean_amount()
        formatted_transactions = []
        for txn in ai_transactions:
            formatted_transactions.append({
                "date": txn.get("date", ""),
                "cheque_number": txn.get("cheque_number", ""),
                "narration": txn.get("narration", ""),
                "debit": str(clean_amount(txn.get("debit", "0.00"))),
                "credit": str(clean_amount(txn.get("credit", "0.00"))),
                "balance": str(clean_amount(txn.get("balance", "0.00")))
            })
        
        # Apply balance reconciliation to catch and fix any remaining errors
        formatted_transactions = reconcile_and_fix_transactions(formatted_transactions)
        
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        raise HTTPException(status_code=500, detail=f"AI extraction failed: {str(e)}")
    finally:
        os.remove(temp_file_path)
    
    # Deduplicate
    seen = set()
    unique = []
    for txn in formatted_transactions:
        key = (txn.get("date"), txn.get("cheque_number", ""), txn.get("narration", "")[:40], txn.get("debit"), txn.get("credit"), txn.get("balance"))
        if key not in seen:
            seen.add(key)
            unique.append(txn)
    
    if not unique:
        raise HTTPException(status_code=422, detail="AI could not extract any transactions")
    
    return {
        "status": "success",
        "data": unique,
        "metadata": {
            "total_transactions": len(unique),
            "extraction_method": "gemini-ai"
        }
    }

@app.post("/extract-statement-xml/")
async def extract_statement_xml(file: UploadFile = File(...), password: Optional[str] = Form(None)):
    """Generate Tally XML from extracted transactions"""
    result = await extract_statement(file, password)
    if isinstance(result, dict) and result.get("status") == "encrypted":
        raise HTTPException(status_code=401, detail="PDF_ENCRYPTED")
    
    xml_content = transactions_to_xml(result["data"], file.filename)
    return Response(
        content=xml_content,
        media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename=statement.xml"}
    )

# ============================================================
# ERROR HANDLER
# ============================================================

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global Error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": str(exc)},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
        }
    )