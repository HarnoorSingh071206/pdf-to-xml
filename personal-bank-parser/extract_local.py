import os
import io
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from dotenv import load_dotenv
import google.generativeai as genai

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configure Gemini
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

def get_gemini_parsing(file_path):
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
        
        while uploaded_file.state.name == "PROCESSING":
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
        
        genai.delete_file(uploaded_file.name)
        
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return None

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
        key = f"{t.get('Date')}-{t.get('Description')}-{t.get('Debit')}-{t.get('Credit')}"
        if key not in seen:
            seen.add(key)
            unique_txns.append(t)
            
    logger.info(f"Total unique transactions: {len(unique_txns)}")
    return account_info, unique_txns

def generate_xml(account_info, transactions):
    root = ET.Element("BankStatement")
    info_el = ET.SubElement(root, "AccountInfo")
    for k, v in account_info.items():
        ET.SubElement(info_el, k).text = str(v)
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
    pdf_path = "/home/Hsingh/Desktop/REPOTIC/new doc 2026-05-13 19.14.13.pdf"
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
        print(f"\nSUCCESS!")
        print(f"Transactions Extracted: {len(transactions)}")
        print(f"XML saved at: {output_file}")
    else:
        print("\nFAILED: No transactions could be extracted.")
