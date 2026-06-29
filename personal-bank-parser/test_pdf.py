import pdfplumber
import sys
import pytesseract
from pdf2image import convert_from_path
import io

file_path = "/home/Hsingh/Desktop/REPOTIC/new doc 2026-05-13 19.14.13.pdf"

try:
    print(f"Opening PDF: {file_path}")
    with pdfplumber.open(file_path) as pdf:
        print(f"Total Pages: {len(pdf.pages)}")
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text and text.strip():
                print(f"--- Page {i+1} Text (First 200 chars) ---")
                print(text[:200])
                print("-" * 30)
            else:
                print(f"--- Page {i+1}: NO TEXT EXTRACTED, trying OCR... ---")
                # Convert page to image
                images = convert_from_path(file_path, first_page=i+1, last_page=i+1)
                if images:
                    ocr_text = pytesseract.image_to_string(images[0])
                    print(f"--- Page {i+1} OCR Text (First 200 chars) ---")
                    print(ocr_text[:200])
                    print("-" * 30)
except Exception as e:
    print(f"Error: {e}")

