import os
import time
import json
import base64
from dotenv import load_dotenv

# Google APIs
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google import genai
from google.genai import types

load_dotenv()

# --- CONFIGURATION ---
SHEETS_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
GMAIL_SCOPES = ['https://mail.google.com/']
SERVICE_ACCOUNT_FILE = 'credentials.json'
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# --- RECURSIVE EMAIL PARSER ---
def extract_email_body(payload):
    """Recursively search for the text/plain body in complex MIME structures."""
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain':
                data = part['body'].get('data')
                if data:
                    return base64.urlsafe_b64decode(data).decode('utf-8')
            elif 'parts' in part:
                # If it's nested (like an attachment + html + text combo), dig deeper
                found_text = extract_email_body(part)
                if found_text:
                    return found_text
    else:
        # Simple emails with no parts
        data = payload.get('body', {}).get('data')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8')
    return ""

# --- MODULE A: GMAIL INGESTION ---
def get_unread_rfqs():
    creds = Credentials.from_authorized_user_file('token.json', GMAIL_SCOPES)
    service = build('gmail', 'v1', credentials=creds)
    
    print("\n[Module A] Scanning inbox for unread messages...")
    results = service.users().messages().list(userId='me', q='is:unread').execute()
    messages = results.get('messages', [])

    if not messages:
        print("No new emails to process.")
        return [], service

    extracted_emails = []
    for msg in messages:
        msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        payload = msg_data.get('payload', {})
        headers = payload.get('headers', [])
        
        sender = next((h['value'] for h in headers if h['name'] == 'From'), "")
        
        if "no-reply@accounts.google.com" in sender or "googleaistudio-noreply" in sender:
            service.users().messages().modify(userId='me', id=msg['id'], body={'removeLabelIds': ['UNREAD']}).execute()
            continue
            
        body = extract_email_body(payload)

        extracted_emails.append({
            'id': msg['id'],
            'sender': sender,
            'body': body
        })
        print(f"Loaded RFQ from: {sender}")
        
    return extracted_emails, service

# --- MODULE B1: LOAD MASTER STOCK ---
def get_master_stock():
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SHEETS_SCOPES)
    service = build('sheets', 'v4', credentials=creds)
    
    result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range='Master_Stock!A:G').execute()
    rows = result.get('values', [])
    if not rows: return []
    
    headers = [h.strip().upper() for h in rows[0]]
    return [dict(zip(headers, row + [''] * (len(headers) - len(row)))) for row in rows[1:]]

# --- MODULE B2: AI PARSING ---
def match_rfq_with_gemini(raw_rfq_text, stock_list):
    # Failsafe if the email body is completely empty
    if not raw_rfq_text or not raw_rfq_text.strip():
        print(" -> Warning: Extracted email body is empty.")
        return []

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    compact_catalog = []
    for item in stock_list:
        name = item.get("ITEM_NAME", "").strip()
        brand = item.get("BRAND", "").strip()
        if name:
            compact_catalog.append({
                "official_name": name,
                "display_name": f"{brand} {name}".strip() if brand else name,
                "brand": brand,
                "alias": item.get("ALIAS", "").strip()
            })

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
4. Return ONLY a valid JSON list matching this schema exactly:
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
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            return json.loads(response.text)
        except Exception as e:
            if "503" in str(e) or "429" in str(e):
                time.sleep(2 ** attempt)
            else: 
                print(f" -> AI Error: {e}")
                return []
    return []

# --- MODULE C: SHEET WRITER ---
def append_to_bulk_import(matches, sender):
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SHEETS_SCOPES)
    service = build('sheets', 'v4', credentials=creds)
    
    rows_to_insert = []
    for item in matches:
        status = "✅ Matched" if item.get('matched') else "❌ NEEDS REVIEW"
        official_name = item.get('official_name') if item.get('official_name') else "—"
        
        clean_sender = sender.split('<')[-1].replace('>', '') if '<' in sender else sender
        client_req = f"[{clean_sender}] {item.get('raw_input', '')}"
        
        rows_to_insert.append([
            status, client_req, official_name, item.get('quantity', ''), item.get('unit', ''), "", ""
        ])
        
    if rows_to_insert:
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Bulk Import!A:G',
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': rows_to_insert}
        ).execute()
        print(f"[Module C] Pushed {len(rows_to_insert)} items to Bulk Import tab.")

# --- MAIN ORCHESTRATOR ---
if __name__ == '__main__':
    print("=== Jai Electricals AI Quoting Engine ===")
    emails, gmail_service = get_unread_rfqs()
    
    if emails:
        print("\n[Module B] Fetching Master Stock...")
        stock = get_master_stock()
        
        for email in emails:
            print(f"\nProcessing RFQ from {email['sender']}...")
            print(f" -> Email Body Preview: {email['body'][:100].strip()}...")
            
            matches = match_rfq_with_gemini(email['body'], stock)
            
            if matches:
                append_to_bulk_import(matches, email['sender'])
                
                gmail_service.users().messages().modify(
                    userId='me', 
                    id=email['id'], 
                    body={'removeLabelIds': ['UNREAD']}
                ).execute()
                print(" -> Marked email as READ in inbox.")
            else:
                print(" -> No parsable items found (or body was empty/unrecognized).")
                
    print("\n=== Pipeline Execution Complete ===")