from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import pikepdf
import io
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
import logging
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("repotic-engine")

app = FastAPI(title="Repotic Python Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Amount Cleaner ──────────────────────────────────────────────────────────────

def clean_amount(raw) -> float:
    """Clean and parse an amount string that may contain commas, newlines, etc."""
    if not raw:
        return 0.0
    s = str(raw).replace("\n", "").replace("\r", "").strip()
    s = re.sub(r'[₹$£€]', '', s)
    s = s.replace(",", "")
    s = re.sub(r'[^\d.]', '', s)
    if not s or s == '.':
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ── Date Normalisation ──────────────────────────────────────────────────────────

MONTH_MAP = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
}

DATE_PATTERNS = [
    # 1. DD/Mon/YYYY or DD.Mon.YYYY
    re.compile(r'(\d{1,2})[/\-\.\s](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[/\-\.\s](\d{2,4})', re.IGNORECASE),
    # 2. DD/MM/YYYY or DD.MM.YYYY
    re.compile(r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})'),
    # 3. YYYY-MM-DD
    re.compile(r'(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})'),
    # 4. DD/MM/YY
    re.compile(r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2})'),
]

def normalise_date(raw: str) -> str:
    if not raw: return ""
    # Join digit pairs split across lines (e.g. "20\n25" -> "2025")
    text = re.sub(r'(\d)\n(\d)', r'\1\2', str(raw))
    # Collapse newlines
    text = re.sub(r'[\n\r]+', ' ', text).strip()
    
    # Try each pattern
    for i, pattern in enumerate(DATE_PATTERNS):
        m = pattern.search(text)
        if m:
            if i == 0: # DD/Mon/YYYY
                day = m.group(1).zfill(2)
                mon = MONTH_MAP.get(m.group(2).lower(), '00')
                yr  = m.group(3)
                if len(yr) == 2: yr = ('20' if int(yr) < 50 else '19') + yr
                return f"{day}-{mon}-{yr}"
            elif i == 1: # DD/MM/YYYY
                return f"{m.group(1).zfill(2)}-{m.group(2).zfill(2)}-{m.group(3)}"
            elif i == 2: # YYYY-MM-DD
                return f"{m.group(3).zfill(2)}-{m.group(2).zfill(2)}-{m.group(1)}"
            elif i == 3: # DD/MM/YY
                day, mon, yr = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
                yr = ('20' if int(yr) < 50 else '19') + yr
                return f"{day}-{mon}-{yr}"
    return ""

# ── Header keyword mapping ──────────────────────────────────────────────────────

HEADER_KWORDS = {
    "date":      ["date", "value date", "txn date", "transaction date", "trans date", "posting date"],
    "narration": ["particulars", "narration", "description", "remarks", "details", "transaction remarks", "transaction details"],
    "debit":     ["debit", "withdrawal", "withdrawals", "dr", "withdra"],
    "credit":    ["credit", "deposit", "deposits", "cr"],
    "amount":    ["amount"],
    "balance":   ["balance"],
}

def map_header(row: list) -> dict:
    m = {k: -1 for k in HEADER_KWORDS}
    for i, cell in enumerate(row):
        c = re.sub(r'[\n\r]+', ' ', str(cell or '')).lower().strip()
        for role, kws in HEADER_KWORDS.items():
            if m[role] == -1 and any(k in c for k in kws):
                m[role] = i
    return m

# ── Extraction Logic ────────────────────────────────────────────────────────────

def _rows_to_transactions(table: list, mapping: dict, start_row: int = 0) -> list:
    transactions = []
    max_idx = max(v for v in mapping.values() if v != -1)
    for row in table[start_row:]:
        if not row or len(row) <= max_idx: continue
        raw_date = str(row[mapping["date"]] or "").strip()
        date_val = normalise_date(raw_date)
        if not date_val:
            for col_idx, cell in enumerate(row):
                if col_idx == mapping["date"]: continue
                candidate = normalise_date(str(cell or ""))
                if candidate:
                    date_val = candidate
                    break
        if not date_val: continue
        narration = str(row[mapping["narration"]] or "").replace("\n", " ").strip() if mapping["narration"] != -1 else ""
        debit = clean_amount(row[mapping["debit"]]) if mapping["debit"] != -1 else 0.0
        credit = clean_amount(row[mapping["credit"]]) if mapping["credit"] != -1 else 0.0
        if mapping["amount"] != -1 and debit == 0.0 and credit == 0.0:
            amt = clean_amount(row[mapping["amount"]])
            if amt < 0: debit = abs(amt)
            else: credit = amt
        balance = str(row[mapping["balance"]] or "").replace("\n", "").replace(",", "").strip() if mapping["balance"] != -1 else ""
        if debit == 0.0 and credit == 0.0 and clean_amount(balance) == 0.0: continue
        transactions.append({"date": date_val, "narration": narration, "debit": str(debit), "credit": str(credit), "balance": balance})
    return transactions

def extract_from_table(table: list, fallback_mapping: dict = None) -> tuple:
    if not table or len(table) < 1: return [], fallback_mapping
    mapping, start_row = None, 0
    for i, row in enumerate(table[:15]):
        m = map_header(row)
        if m["date"] != -1 and (m["narration"] != -1 or m["debit"] != -1 or m["amount"] != -1):
            mapping, start_row = m, i + 1
            break
    if not mapping:
        if fallback_mapping: mapping, start_row = fallback_mapping, 0
        else: return [], None
    return _rows_to_transactions(table, mapping, start_row), mapping

AMOUNT_RE = re.compile(r'[\d,]+\.\d{2}')

def extract_from_text(page) -> list:
    text = page.extract_text()
    if not text: return []
    lines, transactions, pending = text.split("\n"), [], []
    for line in lines:
        line = line.strip()
        if not line: continue
        
        # 1. Identify date
        date_match = None
        date_str = ""
        for pattern in DATE_PATTERNS:
            m = pattern.search(line)
            if m:
                date_match = m
                date_str = m.group(0)
                break
        
        date_val = normalise_date(date_str)
        
        if date_val:
            # Important: Remove the date string from the line before looking for amounts
            # to avoid misidentifying "01.04.2025" as amount "01.04"
            line_without_date = line.replace(date_str, " DATE_HERE ")
            
            amounts = [clean_amount(a) for a in AMOUNT_RE.findall(line_without_date)]
            if not amounts:
                pending.append(line)
                continue
            
            # Narration is everything else
            narration_part = AMOUNT_RE.sub('', line_without_date).replace("DATE_HERE", "").strip()
            # Clean up S No. or garbage from start
            narration_part = re.sub(r'^\d+\s+', '', narration_part)
            
            full_narration = " ".join(pending + [narration_part]).strip()
            pending, balance = [], amounts[-1]
            
            if len(amounts) == 1:
                # Opening balance line usually
                continue
            elif len(amounts) == 2:
                amt = amounts[-2]
                low = full_narration.lower()
                if any(k in low for k in ["cr", "credit", "deposit", "neft cr", "imps cr", "by clg", "cash deposit", "inet-imps-cr", "rtgs-cr", "inward", "received"]):
                    debit, credit = 0.0, amt
                else: debit, credit = amt, 0.0
            elif len(amounts) >= 3:
                # deposit | withdrawal | balance
                credit, debit, balance = amounts[-3], amounts[-2], amounts[-1]
            else:
                debit, credit = 0.0, 0.0
                
            transactions.append({
                "date": date_val,
                "narration": full_narration,
                "debit": str(debit),
                "credit": str(credit),
                "balance": str(balance)
            })
        else:
            # Check if it's a header line to skip
            low_line = line.lower()
            skip_keywords = [
                "date", "particulars", "deposits", "withdrawals", "balance", 
                "opening bala", "closing bala", "statement for", "branch", 
                "customer", "product", "address", "phone", "ifsc", "account no", 
                "pan", "page", "continued", "sl no", "sr no", "tran id", "cheque", 
                "value date", "transactions in", "saving account", "period",
                "base branch", "delhi", "india", "dl, in"
            ]
            if any(kw in low_line for kw in skip_keywords):
                # If we find a header-like line, clear pending to avoid it bleeding into next txn
                pending = []
                continue
            
            # Limit pending buffer to 5 lines to avoid ancient headers sticking around
            if len(pending) > 5:
                pending = pending[-5:]
                
            pending.append(line)
    return transactions

def transactions_to_xml(transactions: list, filename: str = "statement") -> str:
    root = ET.Element("BankStatement")
    root.set("source", filename)
    root.set("totalTransactions", str(len(transactions)))
    for i, txn in enumerate(transactions, 1):
        t = ET.SubElement(root, "Transaction")
        t.set("id", str(i))
        for field in ["date", "narration", "debit", "credit", "balance"]:
            ET.SubElement(t, field.capitalize()).text = str(txn.get(field, ""))
    raw_xml = ET.tostring(root, encoding="unicode")
    return minidom.parseString(raw_xml).toprettyxml(indent="  ", encoding=None)

# ── PDF Decryption Helper with Auto-Unlock ──────────────────────────────────────

def decrypt_pdf(contents: bytes, filename: str, password: Optional[str] = None) -> tuple:
    """
    Returns (decrypted_bytes, is_encrypted, error_message).
    Now includes 'Auto-Unlock' logic to try simple passwords silently.
    """
    try:
        # 1. Try opening without password first
        pdf = pikepdf.open(io.BytesIO(contents))
        out = io.BytesIO()
        pdf.save(out)
        out.seek(0)
        return out.read(), False, None
    except pikepdf.PasswordError:
        pass

    # 2. If user provided a password, try it
    if password:
        try:
            pdf = pikepdf.open(io.BytesIO(contents), password=password)
            out = io.BytesIO()
            pdf.save(out)
            out.seek(0)
            return out.read(), True, None
        except pikepdf.PasswordError:
            return None, True, "wrong_password"

    # 3. AUTO-UNLOCK: Try smart guesses and brute-force
    logger.info(f"Attempting Auto-Unlock for {filename}...")
    import time
    start = time.time()
    
    # Strategy A: Guesses from Filename
    guesses = set(re.findall(r'\d{4,}', filename)) # Long numbers in filename
    # Add common patterns
    guesses.update(["1234", "0000", "1111", "admin", "password"])
    
    for g in guesses:
        try:
            pdf = pikepdf.open(io.BytesIO(contents), password=g)
            out = io.BytesIO()
            pdf.save(out)
            out.seek(0)
            logger.info(f"Auto-unlocked with guess: {g}")
            return out.read(), True, None
        except pikepdf.PasswordError:
            continue

    # Strategy B: 4-digit PIN brute-force (0000-9999)
    logger.info("Starting 4-digit brute-force...")
    pdf_io = io.BytesIO(contents)
    for i in range(10000):
        pin = str(i).zfill(4)
        try:
            pdf_io.seek(0)
            pdf = pikepdf.open(pdf_io, password=pin)
            out = io.BytesIO()
            pdf.save(out)
            out.seek(0)
            logger.info(f"Auto-unlocked with PIN: {pin} in {time.time() - start:.2f}s")
            return out.read(), True, None
        except pikepdf.PasswordError:
            continue
        if i % 2000 == 0: logger.info(f"Tried {i} pins...")

    logger.info(f"Auto-unlock failed after {time.time() - start:.2f}s")
    return None, True, "needs_password"

# ── Endpoints ───────────────────────────────────────────────────────────────────

@app.get("/")
async def health(): return {"status": "online"}

@app.post("/extract-statement/")
async def extract_statement(file: UploadFile = File(...), password: Optional[str] = Form(None)):
    if not file.filename.lower().endswith(".pdf"): raise HTTPException(status_code=400, detail="Only PDF files supported")
    raw = await file.read()
    contents, is_enc, err = decrypt_pdf(raw, file.filename, password)
    if err == "needs_password": return {"status": "encrypted", "message": "Password required"}
    if err == "wrong_password": raise HTTPException(status_code=401, detail="Incorrect password")
    
    transactions, last_mapping = [], None
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for page in pdf.pages:
            page_txns = []
            tables = page.extract_tables() or page.extract_tables(table_settings={"vertical_strategy": "lines", "horizontal_strategy": "lines"}) or page.extract_tables(table_settings={"vertical_strategy": "text", "horizontal_strategy": "text"})
            for table in (tables or []):
                tbl_txns, m = extract_from_table(table, last_mapping)
                if m: last_mapping = m
                if tbl_txns: page_txns.extend(tbl_txns)
            if not page_txns: page_txns.extend(extract_from_text(page))
            transactions.extend(page_txns)
    
    seen = set()
    unique = []
    for t in transactions:
        k = (t["date"], t["narration"][:40], t["debit"], t["credit"])
        if k not in seen:
            seen.add(k)
            unique.append(t)
    
    if not unique: raise HTTPException(status_code=422, detail="No transactions found")
    return {"status": "success", "data": unique}

@app.post("/extract-statement-xml/")
async def extract_statement_xml(file: UploadFile = File(...), password: Optional[str] = Form(None)):
    from fastapi.responses import Response
    res = await extract_statement(file, password)
    if isinstance(res, dict) and res.get("status") == "encrypted":
         raise HTTPException(status_code=401, detail="PDF_ENCRYPTED")
    xml = transactions_to_xml(res["data"], file.filename)
    return Response(content=xml, media_type="application/xml", headers={"Content-Disposition": f"attachment; filename=statement.xml"})