# Gemini Bank Statement Parser

A production-ready, AI-powered bank statement parser built for personal use. It uses **Google Gemini 2.5 Flash** for universal, zero-shot transaction parsing with native PDF vision.

## Features

- **Universal Support**: Works with HDFC, ICICI, SBI, Axis, and more without any hardcoded rules.
- **AI-Powered**: Uses Gemini 2.5 Flash for high-accuracy multimodal parsing.
- **Large PDF Handling**: Handles up to 1-million tokens natively without needing text chunking.
- **Tally XML Output**: Generates clean XML ready for Tally integration.
- **Silent Auto-Retry**: Automatically retries parsing if the LLM output is not valid JSON.

## Setup

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure API Key**:
   Create a `.env` file in the root directory and add your Gemini API key:
   ```env
   GEMINI_API_KEY=your_key_here
   ```

3. **Run the Application**:
   ```bash
   python app.py
   ```

4. **Access the UI**:
   Open your browser and navigate to `http://127.0.0.1:5000`.

## XML Schema

The output XML follows this strict structure:
- `<AccountInfo>`: Bank name, account number, holder name, etc.
- `<Transactions>`: Detailed list of all transactions with Date, Description, Debit, Credit, and Balance.
- `<Summary>`: Totals for debits, credits, and transaction count.

## Tech Stack
- **Backend**: Flask
- **Extraction**: Google Generative AI (Native PDF handling)
- **AI Engine**: Gemini 2.5 Flash
- **Styling**: Tailwind CSS
