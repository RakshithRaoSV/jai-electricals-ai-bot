import os
import json
import time
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google import genai
from google.genai import types

load_dotenv()

# --- 1. Load Master Stock from Google Sheets ---
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = 'credentials.json'
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')

def get_master_stock():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    service = build('sheets', 'v4', credentials=creds)
    
    # Read columns A through G to ensure ITEM_NAME, BRAND, and ALIAS are all captured
    sheet = service.spreadsheets()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range='Master_Stock!A:G'
    ).execute()
    
    rows = result.get('values', [])
    if not rows:
        return []
    
    # Normalize headers by stripping whitespace and converting to uppercase
    headers = [h.strip().upper() for h in rows[0]]
    stock_items = []
    
    for row in rows[1:]:
        # Pad row if trailing columns (like ALIAS) are empty
        row += [''] * (len(headers) - len(row))
        stock_items.append(dict(zip(headers, row)))
        
    return stock_items

# --- 2. AI Parsing & Mapping Function ---
def match_rfq_with_gemini(raw_rfq_text, stock_list):
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
    
    # Build clean catalog using your exact sheet headers: ITEM_NAME, BRAND, ALIAS
    compact_catalog = []
    for item in stock_list:
        name = item.get("ITEM_NAME", "").strip()
        brand = item.get("BRAND", "").strip()
        alias = item.get("ALIAS", "").strip()
        
        if name:
            display_name = f"{brand} {name}".strip() if brand else name
            compact_catalog.append({
                "official_name": name,
                "display_name": display_name,
                "brand": brand,
                "alias": alias
            })

    print(f"Catalog entries sent to Gemini: {len(compact_catalog)}")
    if compact_catalog:
        print(f"Sample catalog entry sent to Gemini: {compact_catalog[0]}")

    prompt = f"""
You are an expert procurement assistant for Jai Electricals.
Map raw, messy RFQ items sent by clients to the official catalog.

Official Catalog:
{json.dumps(compact_catalog, indent=2)}

Client Raw RFQ:
\"\"\"{raw_rfq_text}\"\"\"

Instructions:
1. Parse every item and its requested quantity and unit from the raw RFQ.
2. Match it to the exact 'official_name' from the Official Catalog using item names, brand names, and aliases.
3. If an item cannot be matched to the catalog with high confidence, set 'official_name' to null and 'matched' to false.
4. Return ONLY a valid JSON list matching this schema:
[
  {{
    "raw_input": "original item text",
    "official_name": "Exact ITEM_NAME from Catalog or null",
    "quantity": 10,
    "unit": "coils/pcs/mtrs or null",
    "matched": true/false
  }}
]
"""

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )
            return json.loads(response.text)
            
        except Exception as e:
            if "503" in str(e) or "429" in str(e):
                wait_time = 2 ** attempt
                print(f"\n[Warning] API busy. Retrying in {wait_time}s (Attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                raise e
                
    print("\n[Error] Unable to reach Gemini API after retries.")
    return []

# --- 3. Execution ---
if __name__ == '__main__':
    print("Fetching Master Stock from Google Sheets...")
    stock = get_master_stock()
    print(f"Loaded {len(stock)} items from catalog.")

    # Test RFQ including items that match your stock (like fan regulators) plus unmatched ones
    sample_raw_email = """
    Hi team, please share quotation for:
    1. fan regulator switch type fybros - 10 nos
    2. havls 1.5 wire - 10 coils
    3. 2.5 sq mm anchor wire red 5 bundles
    4. 16A modular switch roma 50 pcs
    5. Solar inverter 5kva (need urgent)
    """

    print("\nSending RFQ to Gemini for mapping...")
    matches = match_rfq_with_gemini(sample_raw_email, stock)
    
    print("\n--- AI Mapping Output ---")
    print(json.dumps(matches, indent=2))