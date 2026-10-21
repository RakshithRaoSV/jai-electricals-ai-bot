import os
import time
import json
import base64
import io
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Google APIs
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google import genai
from google.genai import types

load_dotenv()

# --- CONFIGURATION ---
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://mail.google.com/'
]
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# In-memory cache for Master Stock (TTL: 30 minutes)
_STOCK_CACHE = {
    'timestamp': 0.0,
    'data': []
}
STOCK_CACHE_TTL_SECONDS = 1800

def get_oauth_credentials():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid or not creds.has_scopes(SCOPES):
        if creds and creds.expired and creds.refresh_token and creds.has_scopes(SCOPES):
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token_file:
            token_file.write(creds.to_json())
    return creds

# --- RECURSIVE EMAIL & ATTACHMENT PARSER (LAZY IMPORT ENABLED) ---
def extract_email_content(gmail_service, msg_id, payload):
    text_body = ""
    attachments_text = ""
    media_attachments = []

    def parse_parts(parts):
        nonlocal text_body, attachments_text, media_attachments
        for part in parts:
            mime_type = part.get('mimeType', '')
            filename = part.get('filename', '').lower()

            if mime_type == 'text/plain' and not filename:
                data = part['body'].get('data')
                if data:
                    text_body += base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
            
            elif 'attachmentId' in part['body']:
                att_id = part['body']['attachmentId']
                
                # Lazy-import pandas only when a spreadsheet actually arrives
                if filename.endswith('.csv') or filename.endswith('.xlsx'):
                    print(f" -> Found spreadsheet: {filename}. Lazy-loading pandas...")
                    import pandas as pd
                    
                    att = gmail_service.users().messages().attachments().get(
                        userId='me', messageId=msg_id, id=att_id
                    ).execute()
                    file_data = base64.urlsafe_b64decode(att['data'])
                    try:
                        if filename.endswith('.csv'):
                            df = pd.read_csv(io.BytesIO(file_data))
                        else:
                            df = pd.read_excel(io.BytesIO(file_data))
                        attachments_text += f"\n--- ATTACHMENT: {filename} ---\n{df.to_string()}\n"
                    except Exception as e:
                        print(f" -> Could not read spreadsheet {filename}: {e}")
                        
                elif mime_type.startswith('image/') or mime_type == 'application/pdf':
                    att = gmail_service.users().messages().attachments().get(
                        userId='me', messageId=msg_id, id=att_id
                    ).execute()
                    file_data = base64.urlsafe_b64decode(att['data'])
                    media_attachments.append({'mime_type': mime_type, 'data': file_data, 'filename': filename})
            
            elif 'parts' in part:
                parse_parts(part['parts'])

    if 'parts' in payload:
        parse_parts(payload['parts'])
    else:
        data = payload.get('body', {}).get('data')
        if data:
            text_body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')

    return (text_body + "\n" + attachments_text), media_attachments

# --- MODULE A: GMAIL INGESTION ---
def get_unread_rfqs(gmail_service):
    print("\n[Module A] Scanning inbox for unread messages...")
    results = gmail_service.users().messages().list(userId='me', q='is:unread').execute()
    messages = results.get('messages', [])

    if not messages:
        print("No new emails to process.")
        return []

    extracted_emails = []
    for msg in messages:
        msg_data = gmail_service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        payload = msg_data.get('payload', {})
        headers = payload.get('headers', [])
        sender = next((h['value'] for h in headers if h['name'] == 'From'), "")
        
        # Filter automated Google notices
        if "no-reply@accounts.google.com" in sender or "googleaistudio-noreply" in sender:
            gmail_service.users().messages().modify(userId='me', id=msg['id'], body={'removeLabelIds': ['UNREAD']}).execute()
            continue
            
        print(f"Loaded RFQ from: {sender}")
        body_text, media_attachments = extract_email_content(gmail_service, msg['id'], payload)
        extracted_emails.append({'id': msg['id'], 'sender': sender, 'body': body_text, 'media': media_attachments})
        
    return extracted_emails

# --- MODULE B1: LOAD MASTER STOCK (WITH CACHING) ---
def get_master_stock(sheets_service):
    global _STOCK_CACHE
    now = time.time()
    
    # Return cache if valid
    if _STOCK_CACHE['data'] and (now - _STOCK_CACHE['timestamp'] < STOCK_CACHE_TTL_SECONDS):
        print(f"[Cache Hit] Reusing Master Stock in memory ({len(_STOCK_CACHE['data'])} SKUs).")
        return _STOCK_CACHE['data']

    print("[Module B1] Fetching fresh Master Stock from Google Sheets...")
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, 
        range='Master_Stock!A:G'
    ).execute()
    rows = result.get('values', [])
    if not rows:
        return []
    
    headers = [h.strip().upper() for h in rows[0]]
    catalog = [dict(zip(headers, row + [''] * (len(headers) - len(row)))) for row in rows[1:]]
    
    # Update cache
    _STOCK_CACHE['data'] = catalog
    _STOCK_CACHE['timestamp'] = now
    return catalog

# --- MODULE B2: CONCURRENT FILE UPLOADER HELPER ---
def _upload_single_media(client, media):
    ext = ".pdf" if "pdf" in media['mime_type'].lower() else ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_file.write(media['data'])
        temp_path = temp_file.name
    
    try:
        g_file = client.files.upload(file=temp_path)
        print(f"    [+] Uploaded {media['filename']}")
        return g_file
    except Exception as e:
        print(f"    [!] Failed to upload {media['filename']}: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# --- MODULE B3: AI MATCHING ENGINE ---
def match_rfq_with_gemini(client, raw_rfq_text, media_attachments, stock_list, sender):
    if not raw_rfq_text.strip() and not media_attachments:
        print(" -> Warning: Extracted email is completely empty.")
        return {}

    # Build concise representation of catalog to save token bandwidth
    compact_catalog = [
        {
            "official_name": item.get("ITEM_NAME", "").strip(),
            "brand": item.get("BRAND", "").strip(),
            "alias": item.get("ALIAS", "").strip()
        }
        for item in stock_list if item.get("ITEM_NAME", "").strip()
    ]

    prompt_text = f"""
You are an expert procurement assistant for Jai Electricals based in Hosur, Tamil Nadu.
Analyze the client's email, spreadsheets, and any attached images/PDFs (Purchase Orders, handwritten notes) to extract the requested items and the client's profile.

Official Catalog:
{json.dumps(compact_catalog, indent=2)}

Client Raw RFQ Text:
\"\"\"Sender: {sender}
{raw_rfq_text}\"\"\"

Instructions:
1. Read all text, spreadsheets, images, and PDFs provided.
2. Extract "company_name" from the signature, sender name, or letterhead.
3. Extract the "address" if provided anywhere.
4. Determine "tax_region": If outside Tamil Nadu, output "IGST". If in Tamil Nadu or unknown, output "CGST/SGST".
5. Determine "gst_mode": Default to "With GST" unless explicitly requested otherwise.
6. Match requested items to exact 'official_name' from Catalog. If unknown, set official_name to null.
7. CRITICAL: There may be MULTIPLE Purchase Orders attached. You MUST map every single item to its exact PO Number from the specific document it belongs to. If no PO is found, use "General".

Return ONLY a valid JSON object matching this structure:
{{
  "company_name": "string",
  "address": "string or empty",
  "gst_mode": "With GST or Without GST",
  "tax_region": "CGST/SGST or IGST",
  "freight": 0,
  "items": [
    {{
      "raw_input": "original item text",
      "official_name": "Exact ITEM_NAME from Catalog or null",
      "quantity": 10,
      "unit": "NOS/coils/mtrs or null",
      "matched": true,
      "po_reference": "PO-101 or General"
    }}
  ]
}}
"""
    contents = [prompt_text]
    uploaded_files = []

    # Parallel file upload using ThreadPoolExecutor
    if media_attachments:
        print(f" -> Found {len(media_attachments)} attachments. Parallel uploading to Gemini Storage...")
        max_workers = min(len(media_attachments), 4)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_upload_single_media, client, media) for media in media_attachments]
            for f in futures:
                result = f.result()
                if result:
                    uploaded_files.append(result)
                    contents.append(result)

    parsed_data = {}
    for attempt in range(3):
        try:
            print(" -> Processing payload with Gemini 3.8 Vision...")
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            raw_text = response.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:-3].strip()
            elif raw_text.startswith("```"):
                raw_text = raw_text[3:-3].strip()
            
            parsed_data = json.loads(raw_text)
            if not parsed_data.get('items'):
                print(f" -> [Debug] Gemini found no items! Output:\n{raw_text}")
            break
            
        except Exception as e:
            if "503" in str(e) or "429" in str(e):
                wait_time = 10 * (attempt + 1)
                print(f" -> AI Server busy. Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                print(f" -> CRITICAL AI ERROR: {e}")
                break

    # Clean up uploaded files in Gemini storage
    for f in uploaded_files:
        try:
            client.files.delete(name=f.name)
        except Exception:
            pass

    return parsed_data

# --- MODULE C: SHEET WRITER (SPARSE LAYOUT SAFE) ---
def append_to_bulk_import(sheets_service, parsed_data, sender, email_id):
    email_match = re.search(r'<(.+?)>', sender)
    sender_email = email_match.group(1) if email_match else sender
    
    company = parsed_data.get('company_name', 'Walk-in Client')
    address = parsed_data.get('address', '')
    gst_mode = parsed_data.get('gst_mode', 'With GST')
    tax_region = parsed_data.get('tax_region', 'CGST/SGST')
    freight = parsed_data.get('freight', 0)
    items = parsed_data.get('items', [])

    # Group items by PO
    po_groups = {}
    for item in items:
        po_ref = item.get('po_reference', 'General').strip()
        if not po_ref:
            po_ref = 'General'
        if po_ref not in po_groups:
            po_groups[po_ref] = []
        po_groups[po_ref].append(item)

    rows_to_insert = []
    source_link = f"https://mail.google.com/mail/u/0/#inbox/{email_id}"

    status_formula = '=IF(ISBLANK(INDIRECT("F"&ROW())), "", IF(ISBLANK(INDIRECT("G"&ROW())), "❌ NEEDS REVIEW", "✅ Matched"))'
    total_formula = '=IF(OR(ISBLANK(INDIRECT("J"&ROW())), ISBLANK(INDIRECT("L"&ROW()))), "", INDIRECT("J"&ROW())*INDIRECT("L"&ROW())*(1-IF(ISBLANK(INDIRECT("M"&ROW())), 0, INDIRECT("M"&ROW()))))'

    for po_ref, group_items in po_groups.items():
        for idx, item in enumerate(group_items):
            official_name = item.get('official_name') if item.get('official_name') else ""
            client_req = item.get('raw_input', '')
            
            if idx == 0:
                rows_to_insert.append([
                    f"{company}\n{sender_email}", address, po_ref, source_link,
                    status_formula, client_req, official_name, "", "",
                    item.get('quantity', ''), item.get('unit', ''),
                    "", "", total_formula,
                    freight, gst_mode, tax_region, ""
                ])
            else:
                # Invisible space " " prevents Google Sheets API table misalignment
                rows_to_insert.append([
                    " ", "", "", "",
                    status_formula, client_req, official_name, "", "",
                    item.get('quantity', ''), item.get('unit', ''),
                    "", "", total_formula,
                    "", "", "", ""
                ])
                
        # Space-padded visual separator rows
        rows_to_insert.append([" "] + [""] * 17)
        rows_to_insert.append([" "] + [""] * 17)
        
    if rows_to_insert:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Bulk Import!A:A',
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': rows_to_insert}
        ).execute()
        print(f"[Module C] Appended {len(po_groups)} separate PO block(s) for '{company}'.")

# --- MAIN ORCHESTRATOR ---
def run_ingestion_pipeline():
    print("=== Jai Electricals AI Quoting Engine (Optimized) ===")
    creds = get_oauth_credentials()
    gmail_service = build('gmail', 'v1', credentials=creds)
    sheets_service = build('sheets', 'v4', credentials=creds)
    genai_client = genai.Client(api_key=GEMINI_API_KEY)

    emails = get_unread_rfqs(gmail_service)
    if emails:
        stock = get_master_stock(sheets_service)
        for email in emails:
            print(f"\nProcessing RFQ from {email['sender']}...")
            parsed_payload = match_rfq_with_gemini(
                genai_client, email['body'], email['media'], stock, email['sender']
            )
            if parsed_payload and parsed_payload.get('items'):
                append_to_bulk_import(sheets_service, parsed_payload, email['sender'], email['id'])
                gmail_service.users().messages().modify(
                    userId='me', id=email['id'], body={'removeLabelIds': ['UNREAD']}
                ).execute()
                print(" -> Marked email as READ in inbox.")
            else:
                print(" -> No parsable items found.")
    print("\n=== Pipeline Execution Complete ===")

if __name__ == '__main__':
    run_ingestion_pipeline()