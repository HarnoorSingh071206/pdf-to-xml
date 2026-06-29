import os
import io
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime
import tempfile

from flask import Flask, render_template, request, send_file, flash, redirect
from dotenv import load_dotenv
import google.generativeai as genai

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = "secret_bank_parser_key"

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure Gemini
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# ─── LLM Interaction Logic (Gemini) ──────────────────────────────────────────

def get_gemini_parsing(file_path):
    """Uploads the PDF to Gemini and expects JSON output."""
    prompt = """
    You are a professional bank statement parser. Analyze the attached PDF bank statement.
    Extract ALL transaction data and account information into a structured JSON format.

    REQUIREMENTS:
    1. Extract account details: BankName, AccountNumber, AccountHolder, StatementPeriod, AccountType.
    2. Extract EVERY SINGLE transaction: Date, Description, Debit, Credit, Balance, TransactionType.
    3. If Debit or Credit is missing or empty, use 0.0. Do not put null.
    4. Carefully distinguish Debits and Credits based on their columns.
    5. Do NOT skip any transactions. Be exhaustive and 100% accurate.
    6. Ensure that if "Opening Balance" exists, it is also logged as the first transaction.
    7. Return ONLY a valid JSON object matching the schema below.

    JSON SCHEMA:
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
          "Debit": 0.0,
          "Credit": 0.0,
          "Balance": 0.0,
          "TransactionType": ""
        }
      ]
    }
    """
    
    try:
        logger.info("Uploading file to Google Gemini API...")
        uploaded_file = genai.upload_file(path=file_path)
        
        # Wait for the file to be processed
        timeout = 60
        start_time = time.time()
        while uploaded_file.state.name == "PROCESSING":
            if time.time() - start_time > timeout:
                logger.error("Gemini file processing timeout.")
                return None
            logger.info("Waiting for PDF to be processed by Gemini...")
            time.sleep(2)
            uploaded_file = genai.get_file(uploaded_file.name)
            
        if uploaded_file.state.name == "FAILED":
            logger.error("Gemini failed to process the PDF.")
            return None

        logger.info("File uploaded. Generating content...")
        model = genai.GenerativeModel(
            'gemini-2.5-flash',
            generation_config={"response_mime_type": "application/json"}
        )
        
        response = model.generate_content([uploaded_file, prompt])
        
        # Clean up the file from Google's servers
        genai.delete_file(uploaded_file.name)
        
        text = response.text
        return json.loads(text)
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return None

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

    logger.info(f"Total transactions found: {len(all_transactions)}")

    # Deduplicate transactions (just in case)
    seen = set()
    unique_txns = []
    for t in all_transactions:
        key = f"{t.get('Date')}-{t.get('Description')}-{t.get('Debit')}-{t.get('Credit')}"
        if key not in seen:
            seen.add(key)
            unique_txns.append(t)
            
    return account_info, unique_txns

# ─── XML Generation ─────────────────────────────────────────────────────────

def generate_xml(account_info, transactions):
    root = ET.Element("BankStatement")
    
    # AccountInfo
    info_el = ET.SubElement(root, "AccountInfo")
    for k, v in account_info.items():
        ET.SubElement(info_el, k).text = str(v)
        
    # Transactions
    txns_el = ET.SubElement(root, "Transactions")
    total_debits = 0.0
    total_credits = 0.0
    
    for i, t in enumerate(transactions, 1):
        txn_el = ET.SubElement(txns_el, "Transaction", id=str(i))
        for k, v in t.items():
            ET.SubElement(txn_el, k).text = str(v)
            try:
                if k == "Debit": total_debits += float(v or 0)
                if k == "Credit": total_credits += float(v or 0)
            except: pass
            
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
