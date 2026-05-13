from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import io
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
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
    """Clean and parse an amount string that may contain commas, newlines, etc."""
    if not raw:
        return 0.0
    # Join across newlines (e.g. "1,08,000.\n00" → "1,08,000.00")
    s = str(raw).replace("\n", "").replace("\r", "").strip()
    # Remove currency symbols
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

# Matches dates like:
#  01/Apr/2025, 01-Apr-2025, 01 Apr 2025
#  01/04/2025, 01-04-2025, 2025-04-01
#  01/Apr/20\n25  (split across cell lines — handled by caller stripping \n)

DATE_PATTERNS = [
    # DD/Mon/YYYY or DD-Mon-YYYY  (e.g. 01/Apr/2025)
    re.compile(
        r'\b(\d{1,2})[/\-\s](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[/\-\s](\d{2,4})\b',
        re.IGNORECASE
    ),
    # DD/MM/YYYY or DD-MM-YYYY
    re.compile(r'\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b'),
    # YYYY-MM-DD
    re.compile(r'\b(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\b'),
    # DD/MM/YY
    re.compile(r'\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2})\b'),
]

def normalise_date(raw: str) -> str:
    """
    Extract and normalise a date from a (possibly multi-line) string.
    Returns ISO date string 'DD-MM-YYYY' or the raw match if parsing fails.
    """
    if not raw:
        return ""
    # First: join digit pairs split across lines (e.g. "20\n25" → "2025")
    # This handles ICICI's Value Date column: "01/Apr/20\n25"
    text = re.sub(r'(\d)\n(\d)', r'\1\2', str(raw))
    # Then collapse any remaining newlines to space
    text = re.sub(r'[\n\r]+', ' ', text).strip()

    # Pattern 1: DD/Mon/YYYY
    m = DATE_PATTERNS[0].search(text)
    if m:
        day = m.group(1).zfill(2)
        mon = MONTH_MAP.get(m.group(2).lower(), '00')
        yr  = m.group(3)
        if len(yr) == 2:
            yr = ('20' if int(yr) < 50 else '19') + yr
        return f"{day}-{mon}-{yr}"

    # Pattern 2: DD/MM/YYYY
    m = DATE_PATTERNS[1].search(text)
    if m:
        day = m.group(1).zfill(2)
        mon = m.group(2).zfill(2)
        yr  = m.group(3)
        return f"{day}-{mon}-{yr}"

    # Pattern 3: YYYY-MM-DD
    m = DATE_PATTERNS[2].search(text)
    if m:
        yr  = m.group(1)
        mon = m.group(2).zfill(2)
        day = m.group(3).zfill(2)
        return f"{day}-{mon}-{yr}"

    # Pattern 4: DD/MM/YY
    m = DATE_PATTERNS[3].search(text)
    if m:
        day = m.group(1).zfill(2)
        mon = m.group(2).zfill(2)
        yr  = m.group(3)
        yr  = ('20' if int(yr) < 50 else '19') + yr
        return f"{day}-{mon}-{yr}"

    return ""


def has_date(text: str) -> bool:
    """Return True if text contains any recognisable date."""
    return bool(normalise_date(text))


# ── Header keyword mapping ──────────────────────────────────────────────────────

HEADER_KWORDS = {
    "date":      ["date", "value date", "txn date", "transaction date", "trans date", "posting date"],
    # Note: "cheque no" / "ref no" intentionally excluded — they are separate empty columns
    # in ICICI statements. True narration columns use "remarks", "particulars", "description".
    "narration": ["particulars", "narration", "description", "remarks", "details",
                  "transaction remarks", "transaction details"],
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


# ── Table-based extraction ──────────────────────────────────────────────────────

def _rows_to_transactions(table: list, mapping: dict, start_row: int = 0) -> list:
    """
    Convert table rows to transaction dicts using a pre-discovered column mapping.
    Handles ICICI-style multi-line cells (years split across lines, etc.).
    """
    transactions = []
    max_idx = max(v for v in mapping.values() if v != -1)

    for row in table[start_row:]:
        if not row or len(row) <= max_idx:
            continue

        # --- Date: try primary date column first, then any other date-like column ---
        raw_date = str(row[mapping["date"]] or "").strip()
        date_val = normalise_date(raw_date)

        # Fallback: scan OTHER columns in case primary date col is ambiguous
        if not date_val:
            for col_idx, cell in enumerate(row):
                if col_idx == mapping["date"]:
                    continue
                candidate = normalise_date(str(cell or ""))
                if candidate:
                    date_val = candidate
                    break

        if not date_val:
            continue

        # --- Narration ---
        narration = ""
        if mapping["narration"] != -1:
            narration = str(row[mapping["narration"]] or "").replace("\n", " ").strip()

        # --- Debit ---
        debit = 0.0
        if mapping["debit"] != -1:
            debit = clean_amount(row[mapping["debit"]])

        # --- Credit ---
        credit = 0.0
        if mapping["credit"] != -1:
            credit = clean_amount(row[mapping["credit"]])

        # --- Single Amount column ---
        if mapping["amount"] != -1 and debit == 0.0 and credit == 0.0:
            amt = clean_amount(row[mapping["amount"]])
            if amt < 0:
                debit = abs(amt)
            else:
                credit = amt

        # --- Balance ---
        balance = ""
        if mapping["balance"] != -1:
            balance = str(row[mapping["balance"]] or "").replace("\n", "").strip()
            balance = re.sub(r',', '', balance)

        # Skip rows where both debit and credit are zero AND balance is also zero
        if debit == 0.0 and credit == 0.0:
            bal_num = clean_amount(balance)
            if bal_num == 0.0:
                continue

        transactions.append({
            "date": date_val,
            "narration": narration,
            "debit": str(debit),
            "credit": str(credit),
            "balance": balance,
        })

    return transactions


def extract_from_table(table: list, fallback_mapping: dict = None) -> tuple:
    """
    Extract transactions from a pdfplumber table.
    Returns (transactions, mapping) so callers can reuse the mapping on later pages
    that lack a header row.

    If fallback_mapping is provided and no header is found in this table,
    it will be used directly (handles ICICI pages 2+).
    """
    if not table or len(table) < 1:
        return [], fallback_mapping

    mapping = None
    start_row = 0

    # Scan up to the first 15 rows to find the header
    for i, row in enumerate(table[:15]):
        m = map_header(row)
        if m["date"] != -1 and (m["narration"] != -1 or m["debit"] != -1 or m["amount"] != -1):
            mapping = m
            start_row = i + 1
            logger.info(f"Table header at row {i}: {mapping}")
            break

    if not mapping:
        if fallback_mapping:
            logger.info("No header in this page — reusing mapping from previous page")
            mapping = fallback_mapping
            start_row = 0  # all rows are data rows
        else:
            logger.info("No header found and no fallback mapping available")
            return [], None

    txns = _rows_to_transactions(table, mapping, start_row)
    return txns, mapping


# ── Text-based extraction ───────────────────────────────────────────────────────

AMOUNT_RE = re.compile(r'[\d,]+\.\d{2}')

def extract_from_text(page) -> list:
    """
    Fallback line-by-line text parser for PDFs that don't have extractable tables.
    Handles formats: Canara, SBI, HDFC, Axis, etc.
    """
    text = page.extract_text()
    if not text:
        return []

    lines = text.split("\n")
    transactions = []
    pending_narration = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        date_val = normalise_date(line[:30])

        if date_val:
            amounts = [clean_amount(a) for a in AMOUNT_RE.findall(line)]
            if not amounts:
                pending_narration.append(line)
                continue

            # Build narration: everything between the date and the first amount
            line_no_date = re.sub(DATE_PATTERNS[0].pattern, '', line, flags=re.IGNORECASE)
            for dp in DATE_PATTERNS[1:]:
                line_no_date = dp.sub('', line_no_date)
            narration_part = AMOUNT_RE.sub('', line_no_date).strip()
            full_narration = " ".join(pending_narration + [narration_part]).strip()
            pending_narration = []

            balance = amounts[-1]

            if len(amounts) == 1:
                continue  # Only balance — opening balance line
            elif len(amounts) == 2:
                txn_amount = amounts[-2]
                lower_narration = full_narration.lower()
                credit_keywords = [
                    "cr", "credit", "deposit", "neft cr", "imps cr",
                    "by clg", "cash deposit", "inet-imps-cr", "rtgs-cr",
                    "inward", "received"
                ]
                if any(k in lower_narration for k in credit_keywords):
                    debit, credit = 0.0, txn_amount
                else:
                    debit, credit = txn_amount, 0.0
            elif len(amounts) >= 3:
                # Three-column format: deposit | withdrawal | balance
                credit = amounts[-3]
                debit  = amounts[-2]
                balance = amounts[-1]
            else:
                debit, credit = 0.0, 0.0

            transactions.append({
                "date": date_val,
                "narration": full_narration,
                "debit": str(debit),
                "credit": str(credit),
                "balance": str(balance),
            })
        else:
            # Skip obvious header/footer lines
            skip_keywords = [
                "date", "particulars", "deposits", "withdrawals",
                "balance", "opening bala", "closing bala", "statement for",
                "branch", "customer", "product", "address", "phone", "ifsc",
                "account no", "pan", "page", "continued", "sl no", "sr no",
                "tran id", "cheque", "value date",
            ]
            if any(kw in line.lower() for kw in skip_keywords):
                continue
            pending_narration.append(line)

    return transactions


# ── XML Builder ─────────────────────────────────────────────────────────────────

def transactions_to_xml(transactions: list, filename: str = "statement") -> str:
    """Convert list of transaction dicts to a formatted XML string."""
    root = ET.Element("BankStatement")
    root.set("source", filename)
    root.set("totalTransactions", str(len(transactions)))

    for i, txn in enumerate(transactions, 1):
        t = ET.SubElement(root, "Transaction")
        t.set("id", str(i))

        for field in ["date", "narration", "debit", "credit", "balance"]:
            el = ET.SubElement(t, field.capitalize())
            el.text = str(txn.get(field, ""))

    # Pretty-print
    raw_xml = ET.tostring(root, encoding="unicode")
    parsed = minidom.parseString(raw_xml)
    return parsed.toprettyxml(indent="  ", encoding=None)


# ── Endpoints ───────────────────────────────────────────────────────────────────

@app.get("/")
async def health():
    return {"status": "online"}


@app.post("/debug-pdf/")
async def debug_pdf(file: UploadFile = File(...)):
    """Returns raw structure of the first 3 pages for debugging."""
    contents = await file.read()
    result = {"pages": []}
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for i, page in enumerate(pdf.pages[:3]):
            tables = page.extract_tables()
            text = (page.extract_text() or "")[:1000]
            sample_table = []
            if tables:
                for row in tables[0][:5]:
                    sample_table.append([str(c or '') for c in row])
            result["pages"].append({
                "page": i + 1,
                "tables_found": len(tables),
                "table_row_sample": sample_table,
                "text_snippet": text,
            })
    return result


@app.post("/extract-statement/")
async def extract_statement(file: UploadFile = File(...)):
    """
    Main endpoint: accepts any bank statement PDF and returns transactions as JSON.
    Strategy:
      1. Try table extraction with cross-page header memory (ICICI, HDFC, Axis, etc.)
      2. Fall back to line-by-line text parsing (Canara, SBI, etc.)
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    logger.info(f"Processing: {file.filename}")
    contents = await file.read()
    transactions = []
    last_mapping = None  # persisted across pages

    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            logger.info(f"=== Page {page_num + 1} ===")

            page_txns = []

            # ── Step 1: Try table extraction ──────────────────────────────────
            tables = page.extract_tables()
            if not tables:
                tables = page.extract_tables(
                    table_settings={"vertical_strategy": "lines", "horizontal_strategy": "lines"}
                )
            if not tables:
                tables = page.extract_tables(
                    table_settings={"vertical_strategy": "text", "horizontal_strategy": "text"}
                )

            for table in tables:
                # Pass last_mapping so headerless pages (ICICI p2+) still parse
                tbl_txns, discovered_mapping = extract_from_table(table, fallback_mapping=last_mapping)
                if discovered_mapping:
                    last_mapping = discovered_mapping  # remember for next pages
                if tbl_txns:
                    page_txns.extend(tbl_txns)
                    logger.info(f"  Table parsing: {len(tbl_txns)} transactions")

            # ── Step 2: If tables gave nothing, use text parsing ──────────────
            if not page_txns:
                text_txns = extract_from_text(page)
                if text_txns:
                    page_txns.extend(text_txns)
                    logger.info(f"  Text parsing: {len(text_txns)} transactions")

            transactions.extend(page_txns)

    # Deduplicate (same date + narration + amount)
    seen = set()
    unique_txns = []
    for txn in transactions:
        key = (txn["date"], txn["narration"][:40], txn["debit"], txn["credit"])
        if key not in seen:
            seen.add(key)
            unique_txns.append(txn)

    logger.info(f"Total unique transactions: {len(unique_txns)}")

    if not unique_txns:
        raise HTTPException(
            status_code=422,
            detail=(
                "No transactions could be extracted from this PDF. "
                "The file may be scanned/image-based, password-protected, or in an unsupported format. "
                "Please use /debug-pdf/ to inspect the raw structure."
            )
        )

    return {"status": "success", "data": unique_txns}


@app.post("/extract-statement-xml/")
async def extract_statement_xml(file: UploadFile = File(...)):
    """
    Same as /extract-statement/ but returns the data as an XML file download.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    from fastapi.responses import Response

    logger.info(f"Processing (XML): {file.filename}")
    contents = await file.read()
    transactions = []
    last_mapping = None  # persisted across pages

    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            page_txns = []

            tables = page.extract_tables()
            if not tables:
                tables = page.extract_tables(
                    table_settings={"vertical_strategy": "lines", "horizontal_strategy": "lines"}
                )
            if not tables:
                tables = page.extract_tables(
                    table_settings={"vertical_strategy": "text", "horizontal_strategy": "text"}
                )

            for table in tables:
                tbl_txns, discovered_mapping = extract_from_table(table, fallback_mapping=last_mapping)
                if discovered_mapping:
                    last_mapping = discovered_mapping
                if tbl_txns:
                    page_txns.extend(tbl_txns)

            if not page_txns:
                text_txns = extract_from_text(page)
                page_txns.extend(text_txns)

            transactions.extend(page_txns)

    seen = set()
    unique_txns = []
    for txn in transactions:
        key = (txn["date"], txn["narration"][:40], txn["debit"], txn["credit"])
        if key not in seen:
            seen.add(key)
            unique_txns.append(txn)

    if not unique_txns:
        raise HTTPException(status_code=422, detail="No transactions found in this PDF.")

    xml_str = transactions_to_xml(unique_txns, filename=file.filename)

    return Response(
        content=xml_str,
        media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename=statement.xml"}
    )