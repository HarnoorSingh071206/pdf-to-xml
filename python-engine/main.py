from fastapi import FastAPI, File, UploadFile, HTTPException 
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import io
import re
import logging

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
    if not raw:
        return 0.0
    s = str(raw).strip().replace(",", "")
    s = re.sub(r'[^\d.]', '', s)
    try:
        return float(s)
    except ValueError:
        return 0.0


# ── Date Detection ──────────────────────────────────────────────────────────────

DATE_RE = re.compile(
    r'\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}'       # DD-MM-YY or DD/MM/YYYY
    r'|\d{4}[-/]\d{1,2}[-/]\d{1,2})\b'         # YYYY-MM-DD
)

def extract_date(text: str):
    """Returns the first date found in text, or None."""
    m = DATE_RE.search(str(text or ""))
    return m.group(0) if m else None


# ── Text-based extraction (the most reliable for this Canara PDF format) ────────

def extract_from_text(page) -> list:
    """
    Parses raw page text line by line.
    This handles multi-line transaction rows perfectly since pdfplumber
    returns the text in reading order.

    Canara Bank Statement format (detected from debug):
      Date Particulars Deposits Withdrawals Balance
    Transactions span multiple lines; the date line contains the numeric amounts.
    """
    text = page.extract_text()
    if not text:
        return []

    lines = text.split("\n")
    transactions = []
    narration_buffer = []

    # regex to match a line that has a date AND at least one amount
    AMOUNT_RE = re.compile(r'[\d,]+\.\d{2}')
    TXN_LINE_RE = re.compile(
        r'(\d{2}[-/]\w{3}[-/]\d{4}|\d{2}[-/]\d{2}[-/]\d{2,4})'  # date
        r'.*?([\d,]+\.\d{2})'                                        # at least 1 amount
    )

    pending_narration = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Try to detect a date in the line
        date = extract_date(line[:20])

        if date:
            # Extract all amounts from this line
            amounts = [float(a.replace(",", "")) for a in AMOUNT_RE.findall(line)]

            if len(amounts) == 0:
                # Date-only line with no amounts – might be part of narration continuation
                pending_narration.append(line)
                continue

            # Build narration from everything between date and the first amount
            # Remove the date from the line
            line_no_date = line.replace(date, "", 1).strip()
            # Remove all amounts from narration
            narration_part = AMOUNT_RE.sub("", line_no_date).strip()
            
            # Combine any buffered narration lines
            full_narration = " ".join(pending_narration + [narration_part]).strip()
            pending_narration = []

            # Determine debit/credit from amount positions
            # Format: Deposits  Withdrawals  Balance
            # → amounts[-1] = balance, amounts[-2] = either deposit or withdrawal
            if len(amounts) == 1:
                # Only balance – this is the "Opening Balance" line, skip
                continue
            elif len(amounts) == 2:
                # One transaction amount + balance
                balance = amounts[-1]
                txn_amount = amounts[-2]
                # Decide debit vs credit from narration keywords
                lower_narration = full_narration.lower()
                if any(k in lower_narration for k in [
                    "inet-imps-cr", "neft cr", "imps cr", "by clg", "cash deposit",
                    "credit", "inet-imps cr", "cr/"
                ]):
                    debit, credit = 0.0, txn_amount
                else:
                    debit, credit = txn_amount, 0.0
            else:
                # Two transaction amounts (deposits + withdrawals) + balance
                # amounts[-3]=deposits, amounts[-2]=withdrawals, amounts[-1]=balance
                balance = amounts[-1]
                credit  = amounts[-3] if len(amounts) >= 3 else 0.0
                debit   = amounts[-2] if len(amounts) >= 2 else 0.0

            transactions.append({
                "date": date,
                "narration": full_narration,
                "debit": str(debit),
                "credit": str(credit),
                "balance": str(amounts[-1]),
            })
        else:
            # Not a date line → accumulate as part of narration for next transaction
            # Skip obvious header lines
            if any(kw in line.lower() for kw in [
                "date", "particulars", "deposits", "withdrawals",
                "balance", "opening bala", "closing bala", "statement for",
                "branch", "customer", "product", "address", "phone", "ifsc"
            ]):
                continue
            pending_narration.append(line)

    return transactions


# ── Table-based extraction (fallback for well-structured PDFs) ──────────────────

HEADER_KWORDS = {
    "date":      ["date"],
    "narration": ["particulars", "narration", "description", "remarks", "details"],
    "debit":     ["debit", "withdrawal", "withdrawals", "dr"],
    "credit":    ["credit", "deposit", "deposits", "cr"],
    "amount":    ["amount"],
    "balance":   ["balance"],
}

def map_header(row: list) -> dict:
    m = {k: -1 for k in HEADER_KWORDS}
    for i, cell in enumerate(row):
        c = str(cell or "").lower().strip()
        for role, kws in HEADER_KWORDS.items():
            if m[role] == -1 and any(k in c for k in kws):
                m[role] = i
    return m

def extract_from_table(table: list) -> list:
    if not table or len(table) < 2:
        return []

    mapping = None
    start_row = 0

    for i, row in enumerate(table[:10]):
        m = map_header(row)
        if m["date"] != -1 and (m["narration"] != -1 or m["debit"] != -1 or m["amount"] != -1):
            mapping = m
            start_row = i + 1
            logger.info(f"Table header at row {i}: {mapping}")
            break

    if not mapping:
        return []

    transactions = []
    max_idx = max(v for v in mapping.values() if v != -1)

    for row in table[start_row:]:
        if not row or len(row) <= max_idx:
            continue

        date_val = str(row[mapping["date"]] or "").strip()
        if not extract_date(date_val):
            continue

        narration = str(row[mapping["narration"]] or "").replace("\n", " ").strip() if mapping["narration"] != -1 else ""
        debit  = abs(clean_amount(row[mapping["debit"]])) if mapping["debit"] != -1 else 0.0
        credit = abs(clean_amount(row[mapping["credit"]])) if mapping["credit"] != -1 else 0.0

        if mapping["amount"] != -1 and debit == 0.0 and credit == 0.0:
            amt = clean_amount(row[mapping["amount"]])
            if amt < 0:
                debit = abs(amt)
            else:
                credit = amt

        balance = str(row[mapping["balance"]] or "").strip() if mapping["balance"] != -1 else ""

        transactions.append({
            "date": date_val, "narration": narration,
            "debit": str(debit), "credit": str(credit), "balance": balance
        })

    return transactions


# ── Endpoints ───────────────────────────────────────────────────────────────────

@app.get("/")
async def health():
    return {"status": "online"}


@app.post("/debug-pdf/")
async def debug_pdf(file: UploadFile = File(...)):
    contents = await file.read()
    result = {"pages": []}
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for i, page in enumerate(pdf.pages[:3]):
            tables = page.extract_tables()
            text = (page.extract_text() or "")[:800]
            result["pages"].append({
                "page": i + 1,
                "tables_found": len(tables),
                "table_row_sample": tables[0][:5] if tables else [],
                "text_snippet": text,
            })
    return result


@app.post("/extract-statement/")
async def extract_statement(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    logger.info(f"Processing: {file.filename}")
    contents = await file.read()
    transactions = []

    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            logger.info(f"=== Page {page_num + 1} ===")

            # Primary: text-line parsing (handles multi-line rows like Canara Bank)
            text_txns = extract_from_text(page)

            if text_txns:
                logger.info(f"Text parsing: {len(text_txns)} transactions")
                transactions.extend(text_txns)
            else:
                # Fallback: table extraction
                tables = page.extract_tables()
                if not tables:
                    tables = page.extract_tables(
                        table_settings={"vertical_strategy": "lines", "horizontal_strategy": "lines"}
                    )
                for table in tables:
                    tbl_txns = extract_from_table(table)
                    transactions.extend(tbl_txns)
                    logger.info(f"Table parsing: {len(tbl_txns)} transactions")

    logger.info(f"Total: {len(transactions)} transactions")
    return {"status": "success", "data": transactions}   