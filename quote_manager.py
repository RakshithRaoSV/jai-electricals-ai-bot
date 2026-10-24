import os
import io
import re
import base64
import socket
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

from google.auth.transport.requests import Request, AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

try:
    from num2words import num2words
except ImportError:
    print("Please run: pip install num2words")
    num2words = None

load_dotenv()

socket.setdefaulttimeout(300)
SCOPES = [
    'https://mail.google.com/',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
ARCHIVE_FOLDER_ID = os.getenv('ARCHIVE_FOLDER_ID')
STAFF_EMAIL_ADDRESS = "jaielectri@gmail.com" 

def get_oauth_credentials(force_refresh=False):
    if force_refresh and os.path.exists('token.json'): os.remove('token.json')
    creds = None
    if os.path.exists('token.json'):
        try: creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        except Exception: os.remove('token.json')
    if not creds or not creds.valid or not creds.has_scopes(SCOPES):
        if creds and creds.expired and creds.refresh_token and creds.has_scopes(SCOPES):
            try: creds.refresh(Request())
            except Exception:
                os.remove('token.json')
                creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
            creds = flow.run_local_server(port=0, prompt='consent')
        with open('token.json', 'w') as token_file: token_file.write(creds.to_json())
    return creds

def get_services(force_refresh=False):
    creds = get_oauth_credentials(force_refresh)
    return build('sheets', 'v4', credentials=creds), build('drive', 'v3', credentials=creds), build('gmail', 'v1', credentials=creds), AuthorizedSession(creds)

def load_master_stock_map(sheets_service):
    res = sheets_service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range='Master_Stock!A:F').execute(num_retries=3)
    rows = res.get('values', [])
    if not rows: return {}, []
    headers = [h.strip().upper() for h in rows[0]]
    stock_map = {}
    for row in rows[1:]:
        row += [''] * (len(headers) - len(row))
        item_dict = dict(zip(headers, row))
        name = item_dict.get('ITEM_NAME', '').strip()
        if name: stock_map[name.upper()] = item_dict
    return stock_map, rows[0]

def sync_item_to_master_stock(sheets_service, stock_map, item_name, hsn, uom, brand, base_price, raw_alias=""):
    clean_name = str(item_name).strip().upper()
    if not clean_name or clean_name == "—" or clean_name in stock_map: return
    new_row = [clean_name, str(hsn or '').upper(), str(uom or 'NO').upper(), str(brand or '').upper(), str(base_price or ''), str(raw_alias or '').upper()]
    sheets_service.spreadsheets().values().append(spreadsheetId=SPREADSHEET_ID, range='Master_Stock!A:F', valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS', body={'values': [new_row]}).execute(num_retries=3)
    stock_map[clean_name] = {'BRAND': str(brand or '').upper(), 'HSN_CODE': str(hsn or '').upper(), 'BASE_PRICE': str(base_price or '')}

def get_or_create_company_folder(drive_service, company_name):
    safe_name = company_name.replace("'", "\\'")
    query = f"'{ARCHIVE_FOLDER_ID}' in parents and name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = drive_service.files().list(q=query, fields="files(id, name)").execute(num_retries=3)
    if res.get('files'): return res['files'][0]['id']
    folder = drive_service.files().create(body={'name': company_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [ARCHIVE_FOLDER_ID]}, fields='id').execute(num_retries=3)
    return folder['id']

def export_sheet_to_pdf_InDrive(sheets_service, drive_service, authed_session, spreadsheet_id, gid, pdf_filename, folder_id):
    try:
        sheets_service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={'requests': [{'updateDimensionProperties': {'range': {'sheetId': gid, 'dimension': 'COLUMNS', 'startIndex': 10, 'endIndex': 26}, 'properties': {'hiddenByUser': True}, 'fields': 'hiddenByUser'}}]}).execute(num_retries=3)
        export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=pdf&gid={gid}&size=A4&portrait=true&fitw=true&gridlines=false&printtitle=false"
        resp = authed_session.get(export_url, timeout=300)
        if resp.status_code != 200: raise RuntimeError(f"Failed to export PDF: {resp.status_code} {resp.text}")
        pdf_bytes = resp.content
        safe_pdf_name = pdf_filename.replace("'", "\\'")
        q = f"'{folder_id}' in parents and name = '{safe_pdf_name}' and trashed = false"
        existing = drive_service.files().list(q=q, fields="files(id)").execute(num_retries=3).get('files', [])
        media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype='application/pdf', resumable=True)
        if existing: drive_service.files().update(fileId=existing[0]['id'], media_body=media).execute(num_retries=5)
        else: drive_service.files().create(body={'name': pdf_filename, 'parents': [folder_id]}, media_body=media, fields='id').execute(num_retries=5)
        return pdf_bytes
    finally:
        sheets_service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={'requests': [{'updateDimensionProperties': {'range': {'sheetId': gid, 'dimension': 'COLUMNS', 'startIndex': 10, 'endIndex': 26}, 'properties': {'hiddenByUser': False}, 'fields': 'hiddenByUser'}}]}).execute(num_retries=3)

def send_quote_email(gmail_service, recipient_email, cc_emails, quote_no, company_name, pdf_bytes, pdf_filename, po_ref, thread_id="", message_id=""):
    if not recipient_email or '@' not in recipient_email: return
    msg = EmailMessage()
    
    # --- NEW: GMAIL SUBJECT MATCHING LOGIC ---
    original_subject = ""
    if thread_id:
        try:
            # Safely fetch the exact subject of the original email thread
            thread_data = gmail_service.users().threads().get(userId='me', id=thread_id, format='metadata', metadataHeaders=['Subject']).execute(num_retries=3)
            for t_msg in thread_data.get('messages', []):
                for header in t_msg.get('payload', {}).get('headers', []):
                    if header['name'].lower() == 'subject':
                        original_subject = header['value']
                        break
                if original_subject: break
        except Exception as e:
            print(f"    [!] Warning: Could not fetch original thread subject: {e}")
            
    # If we found the original subject, we MUST use it so Gmail threads it properly.
    if original_subject:
        if not original_subject.lower().startswith('re:'):
            original_subject = f"Re: {original_subject}"
        msg['Subject'] = original_subject
    else:
        # Fallback if no thread is found
        subject_tail = f"({company_name})" if po_ref.upper() == 'GENERAL' else f"({company_name} - {po_ref})"
        msg['Subject'] = f"Quotation {quote_no} - Jai Electricals {subject_tail}"
        
    msg['From'] = "Jai Electricals <jaielectri.quotes@gmail.com>"
    msg['To'] = recipient_email
    
    all_ccs = [STAFF_EMAIL_ADDRESS]
    if cc_emails: all_ccs.append(cc_emails)
    msg['Cc'] = ", ".join(all_ccs)
    
    # Bind the email into the original thread for the client & staff
    if thread_id and message_id:
        msg['In-Reply-To'] = message_id
        msg['References'] = message_id
    
    msg.set_content(f"Dear {company_name},\n\nThank you for your inquiry. Please find attached our quotation ({quote_no}) for your requested materials.\n\nBest Regards,\nJAI ELECTRICALS")
    msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=pdf_filename)
    
    send_body = {'raw': base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')}
    if thread_id:
        send_body['threadId'] = thread_id
        
    gmail_service.users().messages().send(userId='me', body=send_body).execute(num_retries=3)

def get_amount_in_words(amount):
    if not num2words: return f"Rupees {amount} Only"
    return f"Rupees {num2words(amount, lang='en_IN').title()} Only"

def clean_num(v):
    if not v: return 0.0
    s = str(v).replace(',', '')
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    return float(m.group()) if m else 0.0

def _process_single_quote(sheets_service, drive_service, gmail_service, authed_session, quote_no, data):
    print(f" -> [Worker] Generating PDF for '{data['company']}' (PO: {data['po_ref']})...")
    company, address, items, po_ref = data['company'], data['address'], data['items'], data['po_ref']
    folder_id = get_or_create_company_folder(drive_service, company)
    file_tail = "" if po_ref.upper() == 'GENERAL' else f" - {re.sub(r'[\\/*?:\"<>|]', '', po_ref)}"
    sheet_title = f"{quote_no} {company}{file_tail}"

    for old_f in drive_service.files().list(q=f"'{folder_id}' in parents and name = '{sheet_title.replace('''\'''', '''\\\'''')}' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false", fields="files(id)").execute(num_retries=3).get('files', []):
        drive_service.files().delete(fileId=old_f['id']).execute(num_retries=3)

    new_sheet_id = drive_service.files().copy(fileId=SPREADSHEET_ID, body={'name': sheet_title, 'parents': [folder_id]}).execute(num_retries=3)['id']
    meta = sheets_service.spreadsheets().get(spreadsheetId=new_sheet_id).execute(num_retries=3)
    
    quotation_gid = 0
    current_cols = 10
    for s in meta['sheets']:
        if s['properties']['title'].lower() == 'quotation':
            quotation_gid = s['properties']['sheetId']
            current_cols = s['properties'].get('gridProperties', {}).get('columnCount', 10)
            
    if current_cols < 13:
        sheets_service.spreadsheets().batchUpdate(spreadsheetId=new_sheet_id, body={'requests': [{'appendDimension': {'sheetId': quotation_gid, 'dimension': 'COLUMNS', 'length': 13 - current_cols}}]}).execute(num_retries=3)

    sheet_data = sheets_service.spreadsheets().values().get(spreadsheetId=new_sheet_id, range='Quotation!I1:I150').execute(num_retries=3).get('values', [])
    subtotal_idx = 111 
    for i, row in enumerate(sheet_data):
        if row and len(row) > 0 and 'SUBTOTAL' in str(row[0]).upper():
            subtotal_idx = i
            break

    sheets_service.spreadsheets().values().clear(spreadsheetId=new_sheet_id, range=f'Quotation!A12:J{subtotal_idx}').execute(num_retries=3)

    item_rows, python_subtotal = [], 0.0
    for idx, item in enumerate(items, start=1):
        row_num = 11 + idx  
        bp_val, qty_val = clean_num(item.get('base_price')), clean_num(item.get('qty'))
        d_raw = clean_num(item.get('discount'))
        d_val = (d_raw / 100.0) if d_raw > 1.0 else d_raw
        sheet_disc = d_val if d_val > 0 else ""
        net_rate_fmla = f'=IF(B{row_num}="", "", G{row_num}*(1-IF(ISNUMBER(H{row_num}), H{row_num}, 0)))'
        amt_fmla = f'=IF(B{row_num}="", "", D{row_num}*I{row_num})'
        item_rows.append([idx, str(item.get('official_name', '')).upper(), str(item.get('hsn', '')).upper(), qty_val, str(item.get('uom', '')).upper(), str(item.get('brand', '')).upper(), bp_val, sheet_disc, net_rate_fmla, amt_fmla])
        python_subtotal += (qty_val * bp_val) * (1.0 - d_val)

    last_row = 11 + len(items)
    frt_val = clean_num(data.get('freight'))
    taxable = python_subtotal + frt_val
    gst_mode, tax_reg = str(data.get('gst', '')).upper(), str(data.get('tax_reg', '')).upper()

    structural_reqs = []
    gt_idx = subtotal_idx + 3 
    
    if 'WITHOUT' in gst_mode or 'IGST' in tax_reg:
        structural_reqs.append({'deleteDimension': {'range': {'sheetId': quotation_gid, 'dimension': 'ROWS', 'startIndex': subtotal_idx + 2, 'endIndex': subtotal_idx + 3}}})
        gt_idx -= 1
    if 'WITHOUT' in gst_mode:
        structural_reqs.append({'deleteDimension': {'range': {'sheetId': quotation_gid, 'dimension': 'ROWS', 'startIndex': subtotal_idx + 1, 'endIndex': subtotal_idx + 2}}})
        gt_idx -= 1
    if frt_val > 0:
        structural_reqs.append({'insertDimension': {'range': {'sheetId': quotation_gid, 'dimension': 'ROWS', 'startIndex': subtotal_idx + 1, 'endIndex': subtotal_idx + 2}, 'inheritFromBefore': True}})
        gt_idx += 1
        
    structural_reqs.append({'insertDimension': {'range': {'sheetId': quotation_gid, 'dimension': 'ROWS', 'startIndex': gt_idx + 1, 'endIndex': gt_idx + 2}, 'inheritFromBefore': False}})
    if structural_reqs: sheets_service.spreadsheets().batchUpdate(spreadsheetId=new_sheet_id, body={'requests': structural_reqs}).execute(num_retries=3)

    new_subtotal_row = subtotal_idx + 1
    current_row = new_subtotal_row
    
    freight_row = None
    if frt_val > 0:
        current_row += 1
        freight_row = current_row
        
    tax1_row, tax2_row = None, None
    if 'WITHOUT' in gst_mode:
        current_row += 1
        gt_row = current_row
        est_gt = taxable
    elif 'IGST' in tax_reg:
        current_row += 1
        tax1_row = current_row
        current_row += 1
        gt_row = current_row
        est_gt = taxable * 1.18
    else:
        current_row += 1
        tax1_row = current_row
        current_row += 1
        tax2_row = current_row
        current_row += 1
        gt_row = current_row
        est_gt = taxable * 1.18
        
    words_row = gt_row + 1
    to_text = f"To:\n{str(company).upper()}\n{address}"
    if po_ref.upper() != 'GENERAL': to_text += f"\nPO Ref: {po_ref}"

    update_data = [
        {'range': 'Quotation!B4', 'values': [[quote_no]]},
        {'range': 'Quotation!J4', 'values': [[datetime.now().strftime('%d/%m/%Y')]]},
        {'range': 'Quotation!A6', 'values': [[to_text]]},
        {'range': f'Quotation!A12:J{last_row}', 'values': item_rows},
        {'range': f'Quotation!A{words_row}', 'values': [[f"Amount in Words: {get_amount_in_words(round(est_gt))}"]]},
        {'range': f'Quotation!J{new_subtotal_row}', 'values': [[f"=SUM(J12:J{last_row})"]]},
        {'range': 'Quotation!L3:L5', 'values': [["STAFF REVISION CONTROL"], ["Type REVISE in M4 ->"], ["Status: Initial Quote Sent"]]},
        {'range': 'Quotation!M4', 'values': [[""]]}
    ]
    
    tax_base = f"J{new_subtotal_row}"
    if freight_row:
        update_data.append({'range': f'Quotation!I{freight_row}:J{freight_row}', 'values': [["Freight", frt_val]]})
        tax_base = f"(J{new_subtotal_row}+J{freight_row})"
        
    if tax1_row and 'IGST' in tax_reg:
        update_data.append({'range': f'Quotation!I{tax1_row}:J{tax1_row}', 'values': [["IGST (18%)", f"=IFERROR({tax_base}*0.18, 0)"]]})
    elif tax1_row:
        update_data.append({'range': f'Quotation!I{tax1_row}:J{tax1_row}', 'values': [["CGST (9%)", f"=IFERROR({tax_base}*0.09, 0)"]]})
        update_data.append({'range': f'Quotation!I{tax2_row}:J{tax2_row}', 'values': [["SGST (9%)", f"=IFERROR({tax_base}*0.09, 0)"]]})
        
    update_data.append({'range': f'Quotation!J{gt_row}', 'values': [[f"=SUM(J{new_subtotal_row}:J{gt_row-1})"]]})
    sheets_service.spreadsheets().values().batchUpdate(spreadsheetId=new_sheet_id, body={'valueInputOption': 'USER_ENTERED', 'data': update_data}).execute(num_retries=3)

    post_reqs = [
        {'mergeCells': {'range': {'sheetId': quotation_gid, 'startRowIndex': words_row - 1, 'endRowIndex': words_row, 'startColumnIndex': 0, 'endColumnIndex': 10}, 'mergeType': 'MERGE_ALL'}},
        {'updateDimensionProperties': {'range': {'sheetId': quotation_gid, 'dimension': 'ROWS', 'startIndex': last_row, 'endIndex': new_subtotal_row - 1}, 'properties': {'hiddenByUser': True}, 'fields': 'hiddenByUser'}},
        {'repeatCell': {'range': {'sheetId': quotation_gid, 'startRowIndex': new_subtotal_row - 1, 'endRowIndex': gt_row, 'startColumnIndex': 9, 'endColumnIndex': 10}, 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₹\"#,##0.00'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': {'sheetId': quotation_gid, 'startRowIndex': 11, 'endRowIndex': last_row, 'startColumnIndex': 7, 'endColumnIndex': 8}, 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0%'}}}, 'fields': 'userEnteredFormat.numberFormat'}}
    ]
    sheets_service.spreadsheets().batchUpdate(spreadsheetId=new_sheet_id, body={'requests': post_reqs}).execute(num_retries=3)

    pdf_filename = f"{sheet_title}.pdf"
    pdf_bytes = export_sheet_to_pdf_InDrive(sheets_service, drive_service, authed_session, new_sheet_id, quotation_gid, pdf_filename, folder_id)
    
    send_quote_email(gmail_service, data['email'], data.get('cc_emails', ''), quote_no, company, pdf_bytes, pdf_filename, po_ref, data.get('thread_id', ''), data.get('message_id', ''))
    
    print(f" -> [Worker] Successfully finalized {quote_no} for {company}.")
    return quote_no, data

def process_approved_quotes(sheets_service, drive_service, gmail_service, authed_session):
    stock_map, _ = load_master_stock_map(sheets_service)
    
    meta = sheets_service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute(num_retries=3)
    bulk_import_gid = next(s['properties']['sheetId'] for s in meta['sheets'] if s['properties']['title'] == 'Bulk Import')
    
    result = sheets_service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range='Bulk Import!A:R').execute(num_retries=3)
    rows = result.get('values', [])
    if len(rows) <= 1: return

    approved_batches = {}
    current_block_key = None

    for i, row in enumerate(rows[1:], start=2): 
        row += [''] * (18 - len(row))
        col_a, address, po_ref, source_link, status, client_req, official_name, brand, hsn, qty, uom, unit_price, discount, total, freight, gst_mode, tax_region, action = row[:18]
        
        thread_id, message_id = "", ""
        if " | TID:" in source_link:
            s_parts = source_link.split(" | ")
            for p in s_parts:
                if p.startswith("TID:"): thread_id = p.replace("TID:", "").strip()
                elif p.startswith("MID:"): message_id = p.replace("MID:", "").strip()
        
        if col_a.strip():
            parts = col_a.split('\n')
            company = parts[0].strip()
            email = parts[1].strip() if len(parts) > 1 else ""
            
            client_cc_str = ""
            if len(parts) > 2:
                cc_parts = [p.replace("CC:", "").strip() for p in parts[2:] if p.upper().startswith("CC:")]
                if cc_parts: client_cc_str = ", ".join(cc_parts)
            
            clean_po = po_ref.strip() if po_ref.strip() else 'General'
            
            if action.strip().upper() == 'APPROVE' and company:
                current_block_key = f"{company}_{clean_po}_{i}"
                approved_batches[current_block_key] = {
                    'company': company, 'email': email, 'cc_emails': client_cc_str, 'address': address, 
                    'po_ref': clean_po, 'freight': freight, 'gst': gst_mode, 'tax_reg': tax_region, 
                    'action_row': i, 'items': [], 'row_indices': [],
                    'thread_id': thread_id, 'message_id': message_id
                }
            else:
                current_block_key = None 
                
        if current_block_key and client_req.strip():
            cat = stock_map.get(official_name.upper(), {})
            final_unit_price = unit_price.strip() if unit_price.strip() else cat.get('BASE_PRICE', '0')
            final_brand = brand.strip() if brand.strip() else cat.get('BRAND', '')
            final_hsn = hsn.strip() if hsn.strip() else cat.get('HSN_CODE', '')
            
            approved_batches[current_block_key]['items'].append({
                'official_name': official_name, 'qty': qty, 'uom': uom, 'discount': discount, 
                'brand': final_brand, 'hsn': final_hsn, 'base_price': final_unit_price
            })
            approved_batches[current_block_key]['row_indices'].append(i)

    if not approved_batches: 
        return

    lock_requests = []
    for data in approved_batches.values():
        lock_requests.append({'range': f"Bulk Import!R{data['action_row']}", 'values': [['⌛ PROCESSING...']]})
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, 
        body={'valueInputOption': 'USER_ENTERED', 'data': lock_requests}
    ).execute(num_retries=3)
    
    print(f"\n[Module] Found {len(approved_batches)} approved quotes. Processing sequentially to protect APIs...")
    
    history_records = []
    blocks_to_delete = []
    base_quote_num = int(datetime.now().timestamp()) % 10000
    
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = []
        for index, (key, data) in enumerate(approved_batches.items()):
            quote_no = f"QTN-{base_quote_num + index:04d}"
            futures.append(executor.submit(_process_single_quote, sheets_service, drive_service, gmail_service, authed_session, quote_no, data))
            time.sleep(1)
            
        for future in as_completed(futures):
            try:
                completed_quote_no, completed_data = future.result()
                
                history_records.append([
                    datetime.now().strftime('%d/%m/%Y %H:%M'),
                    completed_quote_no,
                    completed_data['company'],
                    completed_data['email'],
                    completed_data['po_ref']
                ])
                
                start_row = completed_data['action_row']
                end_row = completed_data['row_indices'][-1] + 2
                blocks_to_delete.append((start_row, end_row))
                
            except Exception as e:
                print(f" -> [Error] A worker thread failed: {e}")

    if history_records:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range='History!A:E', valueInputOption='USER_ENTERED', 
            insertDataOption='INSERT_ROWS', body={'values': history_records}
        ).execute(num_retries=3)
        print(f"[Module] Archived {len(history_records)} quotes to the History tab.")

    if blocks_to_delete:
        blocks_to_delete.sort(key=lambda x: x[0], reverse=True)
        delete_requests = []
        for start_r, end_r in blocks_to_delete:
            delete_requests.append({'deleteDimension': {'range': {'sheetId': bulk_import_gid, 'dimension': 'ROWS', 'startIndex': start_r - 1, 'endIndex': end_r}}})
        sheets_service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': delete_requests}).execute(num_retries=3)
        print(f"[Module] Cleaned up 'Bulk Import' tab by removing {len(blocks_to_delete)} completed blocks.")

def process_revisions(sheets_service, drive_service, authed_session):
    stock_map, _ = load_master_stock_map(sheets_service)
    
    time_limit = (datetime.now(timezone.utc) - timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
    query = f"name contains 'QTN-' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false and modifiedTime > '{time_limit}'"
    
    files = drive_service.files().list(
        q=query, 
        pageSize=5, 
        orderBy="modifiedTime desc", 
        fields="files(id, name, parents)"
    ).execute(num_retries=3).get('files', [])

    for f in files:
        sheet_id, sheet_name, folder_id = f['id'], f['name'], f.get('parents', [ARCHIVE_FOLDER_ID])[0]
        try:
            time.sleep(2)
            val = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range='Quotation!M4').execute(num_retries=3).get('values', [['']])[0][0]
            if str(val).strip().upper() not in ('REVISE', 'TRUE', 'YES', '1'): 
                continue
        except Exception: 
            continue
            
        print(f"\n[Revision] Processing '{sheet_name}'...")
        
        sheet_data = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range='Quotation!A12:J120').execute(num_retries=3).get('values', [])
        gt_val, words_row = 0, -1
        
        for idx, r in enumerate(sheet_data):
            r += [''] * (10 - len(r))
            if r[1].strip(): sync_item_to_master_stock(sheets_service, stock_map, r[1], r[2], r[4], r[5], r[6])
            if "GRAND TOTAL" in str(r[8]).upper():
                try: gt_val = round(float(str(r[9]).replace('₹', '').replace(',', '').strip()))
                except: pass
            if "AMOUNT IN WORDS:" in str(r[0]).upper(): words_row = 12 + idx
        
        if gt_val > 0 and words_row > 0:
            sheets_service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body={'valueInputOption': 'USER_ENTERED', 'data': [{'range': f'Quotation!A{words_row}', 'values': [[f"Amount in Words: {get_amount_in_words(gt_val)}"]]}]}).execute(num_retries=3)

        revised_pdf_name = f"{sheet_name.split()[0]} {sheet_name.split()[1] if len(sheet_name.split()) > 1 else 'Client'} Revised.pdf"
        meta = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute(num_retries=3)
        export_sheet_to_pdf_InDrive(sheets_service, drive_service, authed_session, sheet_id, meta['sheets'][0]['properties']['sheetId'], revised_pdf_name, folder_id)

        now_str = datetime.now().strftime('%d/%m %H:%M')
        sheets_service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body={'valueInputOption': 'USER_ENTERED', 'data': [{'range': 'Quotation!M4', 'values': [['']]}, {'range': 'Quotation!L5', 'values': [[f'✅ {revised_pdf_name} ready ({now_str})']]}]}).execute(num_retries=3)

if __name__ == '__main__':
    print("=== Jai Electricals Quote Manager (Optimized) ===")
    try:
        sheets_svc, drive_svc, gmail_svc, session = get_services()
        try: drive_svc.files().list(pageSize=1).execute(num_retries=3)
        except Exception as e:
            if "403" in str(e) or "401" in str(e) or "insufficientPermissions" in str(e):
                print("\n[Auto-Heal] Detected insufficient permissions! Resetting auth...")
                sheets_svc, drive_svc, gmail_svc, session = get_services(force_refresh=True)
            else: raise e
        
        process_approved_quotes(sheets_svc, drive_svc, gmail_svc, session)
        process_revisions(sheets_svc, drive_svc, session)
        print("\n=== Quote Manager Complete ===")
        
    except Exception as e:
        if "429" in str(e) or "Quota" in str(e):
            print("\n[API Limit] Google's 60-requests-per-minute limit was hit. The bot is safely pausing and will resume on the next cycle.")
        else:
            raise e