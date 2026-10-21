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
    'https://mail.google.com/',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

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
                if data: text_body += base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
            
            elif 'attachmentId' in part['body']:
                att_id = part['body']['attachmentId']
                if filename.endswith('.csv') or filename.endswith('.xlsx'):
                    print(f" -> Found spreadsheet: {filename}. Lazy-loading pandas...")
                    import pandas as pd
                    att = gmail_service.users().messages().attachments().get(userId='me', messageId=msg_id, id=att_id).execute()
                    file_data = base64.urlsafe_b64decode(att['data'])
                    try:
                        if filename.endswith('.csv'): df = pd.read_csv(io.BytesIO(file_data))
                        else: df = pd.read_excel(io.BytesIO(file_data))
                        attachments_text += f"\n--- ATTACHMENT: {filename} ---\n{df.to_string()}\n"
                    except Exception as e:
                        print(f" -> Could not read spreadsheet {filename}: {e}")
                        
                elif mime_type.startswith('image/') or mime_type == 'application/pdf':
                    att = gmail_service.users().messages().attachments().get(userId='me', messageId=msg_id, id=att_id).execute()
                    file_data = base64.urlsafe_b64decode(att['data'])
                    media_attachments.append({'mime_type': mime_type, 'data': file_data, 'filename': filename})
            
            elif 'parts' in part:
                parse_parts(part['parts'])

    if 'parts' in payload:
        parse_parts(payload['parts'])
    else:
        data = payload.get('body', {}).get('data')
        if data: text_body = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')

    return (text_body + "\n" + attachments_text), media_attachments

def get_unread_rfqs(gmail_service):
    print("\n[Module A] Scanning inbox for unread messages...")
    results = gmail_service.users().messages().list(userId='me', q='is:unread').execute()
    messages = results.get('messages', [])
    if not messages: return []

    extracted_emails = []
    for msg in messages:
        msg_data = gmail_service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        payload = msg_data.get('payload', {})
        headers = payload.get('headers', [])
        
        sender = next((h['value'] for h in headers if h['name'] == 'From'), "")
        cc_header = next((h['value'] for h in headers if h['name'] == 'Cc'), "")
        
        if "no-reply@accounts.google.com" in sender or "googleaistudio-noreply" in sender:
            gmail_service.users().messages().modify(userId='me', id=msg['id'], body={'removeLabelIds': ['UNREAD']}).execute()
            continue
            
        print(f"Loaded RFQ from: {sender}")
        body_text, media_attachments = extract_email_content(gmail_service, msg['id'], payload)
        extracted_emails.append({'id': msg['id'], 'sender': sender, 'cc': cc_header, 'body': body_text, 'media': media_attachments})
        
    return extracted_emails

def _upload_single_media(client, media):
    ext = ".pdf" if "pdf" in media['mime_type'].lower() else ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_file.write(media['data'])
        temp_path = temp_file.name
    try:
        g_file = client.files.upload(file=temp_path)
        print(f"    [+] Uploaded {media['filename']}")
        return g_file
    except Exception: return None
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)

def match_rfq_with_gemini(client, raw_rfq_text, media_attachments, sender, cc_header):
    if not raw_rfq_text.strip() and not media_attachments: return {}

    prompt_text = f"""
You are an expert procurement assistant for Jai Electricals based in India.
Analyze the client's email, spreadsheets, and attached files to extract the request.

Raw Email Data:
\"\"\"Sender: {sender}
CC: {cc_header}
{raw_rfq_text}\"\"\"

Instructions:
1. Extract "company_name" and "address".
2. TRUE CLIENT IDENTIFICATION: If this is a FORWARDED message, extract the original client's email address from the forwarded body text as 'client_email', and any original client CCs as 'client_cc'. If it is NOT forwarded, extract the email from the 'Sender' header and use the 'CC' header.
3. DOMAIN EXPERTISE: For each requested item, intelligently auto-correct the raw input into a clean, professional electrical 'official_name'.
4. Identify the standard electrical 'brand' (e.g., Dowells, Polycab, Legrand) if implied, otherwise output "General".
5. Infer the correct Indian GST 'hsn_code' for the electrical item (e.g., 8536, 8544, etc.).
6. Map every single item to its exact 'po_reference' from the specific document it belongs to.

Return ONLY a valid JSON object matching this structure:
{{
  "company_name": "string",
  "client_email": "client@example.com",
  "client_cc": "cc1@example.com, cc2@example.com or empty",
  "gst_mode": "With GST",
  "tax_region": "CGST/SGST",
  "freight": 0,
  "items": [
    {{
      "raw_input": "original sloppy item text",
      "official_name": "Corrected Professional Name",
      "brand": "Brand Name or General",
      "hsn_code": "8536 or relevant code",
      "quantity": 10,
      "unit": "NOS/coils/mtrs",
      "po_reference": "PO-101 or General"
    }}
  ]
}}
"""
    contents = [prompt_text]
    uploaded_files = []

    if media_attachments:
        print(f" -> Parallel uploading {len(media_attachments)} attachments to Gemini Storage...")
        with ThreadPoolExecutor(max_workers=min(len(media_attachments), 4)) as executor:
            for result in executor.map(lambda m: _upload_single_media(client, m), media_attachments):
                if result:
                    uploaded_files.append(result)
                    contents.append(result)

    parsed_data = {}
    for attempt in range(3):
        try:
            print(f" -> [Attempt {attempt+1}/3] Processing payload with Gemini 3.8 Vision...")
            response = client.models.generate_content(model='gemini-3.8-flash', contents=contents, config=types.GenerateContentConfig(response_mime_type="application/json"))
            raw_text = response.text.strip()
            if raw_text.startswith("```json"): raw_text = raw_text[7:-3].strip()
            elif raw_text.startswith("```"): raw_text = raw_text[3:-3].strip()
            
            parsed_data = json.loads(raw_text)
            break
        except Exception as e:
            print(f"    [!] AI Error Details: {e}") # This will expose the hidden error!
            if "503" in str(e) or "429" in str(e) or "quota" in str(e).lower():
                wait_time = 10 * (attempt + 1)
                print(f"    [!] AI Server busy or Rate Limited. Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else: 
                break

    for f in uploaded_files:
        try: client.files.delete(name=f.name)
        except Exception: pass
    return parsed_data

def append_to_bulk_import(sheets_service, parsed_data, sender, cc_header, email_id):
    client_email = parsed_data.get('client_email', '').strip()
    if not client_email or client_email.lower() == 'empty':
        email_match = re.search(r'<(.+?)>', sender)
        client_email = email_match.group(1) if email_match else sender
        
    client_cc = parsed_data.get('client_cc', '').strip()
    if not client_cc or client_cc.lower() == 'empty':
        client_cc = cc_header
    
    company = parsed_data.get('company_name', 'Walk-in Client')
    address = parsed_data.get('address', '')
    gst_mode = parsed_data.get('gst_mode', 'With GST')
    tax_region = parsed_data.get('tax_region', 'CGST/SGST')
    freight = parsed_data.get('freight', 0)
    
    col_a_header = f"{company}\n{client_email}"
    if client_cc: col_a_header += f"\nCC: {client_cc}"

    po_groups = {}
    for item in parsed_data.get('items', []):
        po_ref = item.get('po_reference', 'General').strip() or 'General'
        if po_ref not in po_groups: po_groups[po_ref] = []
        po_groups[po_ref].append(item)

    rows_to_insert = []
    source_link = f"https://mail.google.com/mail/u/0/#inbox/{email_id}"
    status_formula = '=IF(ISBLANK(INDIRECT("F"&ROW())), "", IF(ISBLANK(INDIRECT("G"&ROW())), "❌ NEEDS REVIEW", "✅ Matched"))'
    total_formula = '=IF(OR(ISBLANK(INDIRECT("J"&ROW())), ISBLANK(INDIRECT("L"&ROW()))), "", INDIRECT("J"&ROW())*INDIRECT("L"&ROW())*(1-IF(ISBLANK(INDIRECT("M"&ROW())), 0, INDIRECT("M"&ROW()))))'

    for po_ref, group_items in po_groups.items():
        for idx, item in enumerate(group_items):
            # Fetch the AI-generated Brand and HSN Code
            gen_brand = item.get('brand', '')
            gen_hsn = item.get('hsn_code', '')
            
            if idx == 0:
                rows_to_insert.append([
                    col_a_header, address, po_ref, source_link, status_formula, item.get('raw_input', ''), 
                    item.get('official_name', ''), gen_brand, gen_hsn, item.get('quantity', ''), item.get('unit', ''),
                    "", "", total_formula, freight, gst_mode, tax_region, ""
                ])
            else:
                rows_to_insert.append([
                    " ", "", "", "", status_formula, item.get('raw_input', ''), 
                    item.get('official_name', ''), gen_brand, gen_hsn, item.get('quantity', ''), item.get('unit', ''), 
                    "", "", total_formula, "", "", "", ""
                ])
        rows_to_insert.append([" "] + [""] * 17)
        rows_to_insert.append([" "] + [""] * 17)
        
    if rows_to_insert:
        sheets_service.spreadsheets().values().append(spreadsheetId=SPREADSHEET_ID, range='Bulk Import!A:A', valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS', body={'values': rows_to_insert}).execute()

def run_ingestion_pipeline():
    print("=== Jai Electricals AI Quoting Engine (High-Speed Mode) ===")
    creds = get_oauth_credentials()
    gmail_service = build('gmail', 'v1', credentials=creds)
    sheets_service = build('sheets', 'v4', credentials=creds)
    genai_client = genai.Client(api_key=GEMINI_API_KEY)

    emails = get_unread_rfqs(gmail_service)
    if emails:
        for email in emails:
            print(f"\nProcessing RFQ from {email['sender']}...")
            # Bypassed Master Stock entirely!
            parsed_payload = match_rfq_with_gemini(genai_client, email['body'], email['media'], email['sender'], email['cc'])
            if parsed_payload and parsed_payload.get('items'):
                append_to_bulk_import(sheets_service, parsed_payload, email['sender'], email['cc'], email['id'])
                gmail_service.users().messages().modify(userId='me', id=email['id'], body={'removeLabelIds': ['UNREAD']}).execute()
                print(" -> Marked email as READ in inbox.")
            else: print(" -> No parsable items found.")
    print("\n=== Pipeline Execution Complete ===")

if __name__ == '__main__':
    run_ingestion_pipeline()