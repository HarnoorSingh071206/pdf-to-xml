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
import google.generativeai as genai
from dotenv import load_dotenv
import os
import tempfile
import time
import json
from decimal import Decimal, InvalidOperation
from pydantic import BaseModel, Field, validator
import hashlib

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
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
    If Previous Balance - Debit + Credit != Current Balance, we KNOW something is wrong.
    """
    if len(transactions) < 2:
        return transactions
    
    fixed_transactions = []
    error_log = []
    
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
            
            # THE MATH CHECK
            expected_balance = prev_bal - debit + credit
            
            if abs(expected_balance - curr_bal) > Decimal('0.01'):
                # BALANCE MISMATCH DETECTED!
                logger.warning(f"Row {i}: Balance mismatch! Expected {expected_balance}, got {curr_bal}")
                
                # TRY FIX #1: Maybe debit and credit are swapped?
                expected_swapped = prev_bal - credit + debit
                if abs(expected_swapped - curr_bal) < Decimal('0.01'):
                    logger.info(f"Row {i}: Auto-fixed by swapping debit/credit")
                    txn["debit"], txn["credit"] = str(credit), str(debit)
                
                # TRY FIX #2: Maybe the amount column sign convention is wrong
                elif abs(prev_bal + debit - curr_bal) < Decimal('0.01'):
                    logger.info(f"Row {i}: Auto-fixed by negating debit")
                    txn["debit"] = str(-debit)
                elif abs(prev_bal - credit - curr_bal) < Decimal('0.01'):
                    logger.info(f"Row {i}: Auto-fixed by negating credit")
                    txn["credit"] = str(-credit)
                
                # TRY FIX #3: Check if this row is a duplicate
                elif i > 0:
                    prev_txn = transactions[i-1]
                    if (prev_txn.get("date") == txn.get("date") and 
                        prev_txn.get("narration")[:20] == txn.get("narration")[:20] and
                        abs(clean_amount(prev_txn.get("debit", "0")) - debit) < Decimal('0.01') and
                        abs(clean_amount(prev_txn.get("credit", "0")) - credit) < Decimal('0.01')):
                        logger.warning(f"Row {i}: Skipping duplicate transaction")
                        continue  # Skip this duplicate
                
                else:
                    error_log.append({
                        "row": i,
                        "expected_balance": str(expected_balance),
                        "actual_balance": str(curr_balance),
                        "transaction": txn
                    })
            
            fixed_transactions.append(txn)
            
        except Exception as e:
            logger.error(f"Error reconciling row {i}: {e}")
            fixed_transactions.append(txn)
    
    if error_log:
        logger.warning(f"Found {len(error_log)} unreconciled transactions")
    
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
        uploaded_file = genai.upload_file(path=temp_file_path)
        
        # Wait for processing
        timeout = 60
        start_time = time.time()
        while uploaded_file.state.name == "PROCESSING":
            if time.time() - start_time > timeout:
                raise HTTPException(status_code=504, detail="Gemini processing timeout")
            time.sleep(2)
            uploaded_file = genai.get_file(uploaded_file.name)
        
        if uploaded_file.state.name == "FAILED":
            raise HTTPException(status_code=500, detail="Gemini failed to process document")
        
        # Enhanced prompt with explicit debit/credit rules
        prompt = """
        You are a professional bank statement parser. Extract ALL transactions from this PDF.

        CRITICAL RULES:
        1. Date format: DD-MM-YYYY
        2. Extract cheque_number or reference number from a dedicated column, or from within the narration text.
        3. Debit means money OUT (withdrawal, payment)
        4. Credit means money IN (deposit, receipt)
        5. Balance = Previous Balance - Debit + Credit
        6. If a row has both Debit and Credit columns, use the non-empty one
        7. If only one amount column exists, check transaction description:
           - Words like "NEFT CR", "IMPS CR", "BY CLG", "DEPOSIT" = Credit
           - Words like "NEFT DR", "IMPS DR", "PAYMENT", "WITHDRAWAL" = Debit
        8. Include opening balance as first row if present
        9. Do NOT skip any transaction
        10. If balance column is empty, calculate it
        11. Return ONLY valid JSON

        Return format:
        {
          "Transactions": [
            {
              "date": "DD-MM-YYYY",
              "cheque_number": "cheque or ref no if any, else empty",
              "narration": "description",
              "debit": "0.00",
              "credit": "0.00",
              "balance": "0.00"
            }
          ]
        }
        """
        
        model = genai.GenerativeModel(
            'gemini-2.5-flash',
            generation_config={"response_mime_type": "application/json"}
        )
        
        response = model.generate_content([uploaded_file, prompt])
        genai.delete_file(uploaded_file.name)
        
        result = json.loads(response.text)
        ai_transactions = result.get("Transactions", [])
        
        # Convert to our format and reconcile
        formatted_transactions = []
        for txn in ai_transactions:
            formatted_transactions.append({
                "date": txn.get("date", ""),
                "cheque_number": txn.get("cheque_number", ""),
                "narration": txn.get("narration", ""),
                "debit": str(txn.get("debit", "0.00")),
                "credit": str(txn.get("credit", "0.00")),
                "balance": str(txn.get("balance", "0.00"))
            })
        
        # 🔥 Apply balance reconciliation to AI output too!
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