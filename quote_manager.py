import os
import io
import re
from datetime import datetime
from email.message import EmailMessage
import base64
from dotenv import load_dotenv

from google.auth.transport.requests import Request, AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

SCOPES = [
    'https://mail.google.com/',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
ARCHIVE_FOLDER_ID = os.getenv('ARCHIVE_FOLDER_ID')


def get_oauth_credentials():
    """Loads or refreshes token.json with Gmail, Sheets, and Drive permissions."""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid or not creds.has_scopes(SCOPES):
        if creds and creds.expired and creds.refresh_token and creds.has_scopes(SCOPES):
            creds.refresh(Request())
        else:
            print("\nOpening browser to authorize Google Drive & Sheets access for your bot account...")
            flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token_file:
            token_file.write(creds.to_json())

    return creds


def get_services():
    creds = get_oauth_credentials()
    sheets_service = build('sheets', 'v4', credentials=creds)
    drive_service = build('drive', 'v3', credentials=creds)
    gmail_service = build('gmail', 'v1', credentials=creds)
    authed_session = AuthorizedSession(creds)
    return sheets_service, drive_service, gmail_service, authed_session


def load_master_stock_map(sheets_service):
    """Loads Master_Stock into a dictionary keyed by uppercase ITEM_NAME."""
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range='Master_Stock!A:F'
    ).execute()
    rows = res.get('values', [])
    if not rows:
        return {}, []

    headers = [h.strip().upper() for h in rows[0]]
    stock_map = {}
    for idx, row in enumerate(rows[1:], start=2):
        row += [''] * (len(headers) - len(row))
        item_dict = dict(zip(headers, row))
        item_dict['_row_index'] = idx
        name = item_dict.get('ITEM_NAME', '').strip()
        if name:
            stock_map[name.upper()] = item_dict
    return stock_map, rows[0]


def sync_item_to_master_stock(sheets_service, stock_map, item_name, hsn, uom, brand, base_price, raw_alias=""):
    """Silently adds new items to Master_Stock if they don't exist yet."""
    clean_name = item_name.strip()
    if not clean_name or clean_name == "—":
        return

    key = clean_name.upper()
    if key not in stock_map:
        new_row = [
            clean_name,
            str(hsn or ''),
            str(uom or 'NO').upper(),
            str(brand or ''),
            str(base_price or ''),
            str(raw_alias or '')
        ]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Master_Stock!A:F',
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': [new_row]}
        ).execute()
        stock_map[key] = {
            'ITEM_NAME': clean_name,
            'HSN_CODE': str(hsn or ''),
            'UOM': str(uom or 'NO').upper(),
            'BRAND': str(brand or ''),
            'BASE_PRICE': str(base_price or ''),
            'ALIAS': str(raw_alias or '')
        }
        print(f" -> [Auto-Learn] Added new item to Master_Stock: '{clean_name}'")


def get_or_create_company_folder(drive_service, company_name):
    """Finds or creates Archived Quotes -> Company Name folder."""
    safe_name = company_name.replace("'", "\\'")
    query = (
        f"'{ARCHIVE_FOLDER_ID}' in parents and "
        f"name = '{safe_name}' and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"trashed = false"
    )
    res = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = res.get('files', [])
    if files:
        return files[0]['id']

    folder_meta = {
        'name': company_name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [ARCHIVE_FOLDER_ID]
    }
    folder = drive_service.files().create(body=folder_meta, fields='id').execute()
    print(f" -> Created company folder: '{company_name}'")
    return folder['id']


def get_next_quote_number(drive_service):
    """Scans Drive for completed QTN-XX PDFs to determine the next quote number."""
    query = (
        "name contains 'QTN-' and "
        "mimeType = 'application/pdf' and "
        "not name contains 'Revised' and "
        "trashed = false"
    )
    res = drive_service.files().list(q=query, fields="files(name)").execute()
    max_num = 0
    for f in res.get('files', []):
        match = re.search(r'QTN-(\d+)', f['name'])
        if match:
            max_num = max(max_num, int(match.group(1)))
    return f"QTN-{max_num + 1:02d}"


def export_sheet_to_pdf_InDrive(drive_service, authed_session, spreadsheet_id, gid, pdf_filename, folder_id):
    """Exports columns A:J of the Quotation sheet as a PDF and saves to the company folder."""
    export_url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
        f"?format=pdf&gid={gid}"
        f"&size=A4&portrait=true&fitw=true"
        f"&gridlines=false&printtitle=false"
        f"&c1=0&c2=10"
    )
    resp = authed_session.get(export_url)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to export PDF: {resp.status_code} {resp.text}")

    pdf_bytes = resp.content

    safe_pdf_name = pdf_filename.replace("'", "\\'")
    q = f"'{folder_id}' in parents and name = '{safe_pdf_name}' and trashed = false"
    existing = drive_service.files().list(q=q, fields="files(id)").execute().get('files', [])

    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype='application/pdf', resumable=False)
    if existing:
        drive_service.files().update(fileId=existing[0]['id'], media_body=media).execute()
    else:
        file_meta = {
            'name': pdf_filename,
            'parents': [folder_id]
        }
        drive_service.files().create(body=file_meta, media_body=media, fields='id').execute()

    print(f" -> Saved PDF to Drive: '{pdf_filename}'")
    return pdf_bytes


def send_quote_email(gmail_service, recipient_email, quote_no, company_name, pdf_bytes, pdf_filename):
    """Emails the initial quotation PDF to the client."""
    if not recipient_email or '@' not in recipient_email:
        print(" -> No valid recipient email found; skipping email send.")
        return

    msg = EmailMessage()
    msg['Subject'] = f"Quotation {quote_no} - Jai Electricals ({company_name})"
    msg['From'] = "Jai Electricals <jaielectri.quotes@gmail.com>"
    msg['To'] = recipient_email
    msg.set_content(
        f"Dear {company_name},\n\n"
        f"Thank you for your inquiry. Please find attached our quotation ({quote_no}) for your requested electrical materials.\n\n"
        f"For any modifications or inquiries, please feel free to contact us.\n\n"
        f"Best Regards,\n"
        f"JAI ELECTRICALS\n"
        f"Hosur - 635 109\n"
        f"Phone: 9585577966"
    )

    msg.add_attachment(
        pdf_bytes,
        maintype='application',
        subtype='pdf',
        filename=pdf_filename
    )

    raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
    gmail_service.users().messages().send(userId='me', body={'raw': raw_msg}).execute()
    print(f" -> Emailed '{pdf_filename}' to {recipient_email}")


def create_quote_spreadsheet_and_pdf(
    sheets_service, drive_service, gmail_service, authed_session,
    quote_no, company_name, client_email, items
):
    """Clones template into company folder, populates Row 12+, exports PDF, and emails client."""
    folder_id = get_or_create_company_folder(drive_service, company_name)
    sheet_title = f"{quote_no} {company_name}"

    # Remove any incomplete sheet with the same name from a prior failed run
    safe_title = sheet_title.replace("'", "\\'")
    old_q = (
        f"'{folder_id}' in parents and name = '{safe_title}' and "
        f"mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
    )
    for old_f in drive_service.files().list(q=old_q, fields="files(id)").execute().get('files', []):
        drive_service.files().delete(fileId=old_f['id']).execute()

    # 1. Copy master spreadsheet into the company folder
    copied_file = drive_service.files().copy(
        fileId=SPREADSHEET_ID,
        body={'name': sheet_title, 'parents': [folder_id]}
    ).execute()
    new_sheet_id = copied_file['id']
    print(f" -> Created standalone Google Sheet: '{sheet_title}'")

    # 2. Keep 'Quotation' tab, delete 'Master_Stock' & 'Bulk Import', and ensure at least 13 columns (A-M)
    meta = sheets_service.spreadsheets().get(spreadsheetId=new_sheet_id).execute()
    quotation_gid = 0
    current_cols = 10
    structural_requests = []

    for s in meta.get('sheets', []):
        title = s['properties']['title']
        sid = s['properties']['sheetId']
        if title.strip().lower() == 'quotation':
            quotation_gid = sid
            current_cols = s['properties'].get('gridProperties', {}).get('columnCount', 10)
        else:
            structural_requests.append({'deleteSheet': {'sheetId': sid}})

    if current_cols < 13:
        structural_requests.append({
            'appendDimension': {
                'sheetId': quotation_gid,
                'dimension': 'COLUMNS',
                'length': 13 - current_cols
            }
        })

    if structural_requests:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=new_sheet_id,
            body={'requests': structural_requests}
        ).execute()

    # 3. Clear old sample item rows (Row 12 to 110) in Quotation tab
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=new_sheet_id,
        range='Quotation!A12:J110'
    ).execute()

    # 4. Prepare header cells (B4, J4, A6) and item rows starting at Row 12
    today_str = datetime.now().strftime('%d/%m/%Y')
    item_rows = []
    for idx, item in enumerate(items, start=1):
        row_num = 11 + idx  # Starts at Row 12
        net_rate_formula = f'=IF(ISBLANK(B{row_num}), "", G{row_num}*(1-IF(ISNUMBER(H{row_num}), H{row_num}, 0)))'
        amount_formula = f'=IF(ISBLANK(D{row_num}), "", D{row_num}*I{row_num})'

        item_rows.append([
            idx,                        # A: S.no
            item['official_name'],      # B: Item Description
            item['hsn'],                # C: HSN
            item['qty'],                # D: Qty
            item['uom'],                # E: UOM
            item['brand'],              # F: Brand
            item['base_price'],         # G: Base Price
            "",                         # H: Disc %
            net_rate_formula,           # I: Net Rate
            amount_formula              # J: Amount
        ])

    # Add Staff Revision Control Box in Columns L & M (outside print area A:J)
    batch_data = [
        {'range': 'Quotation!B4', 'values': [[quote_no]]},
        {'range': 'Quotation!J4', 'values': [[today_str]]},
        {'range': 'Quotation!A6', 'values': [[f"To:\n{company_name}"]]},
        {'range': f'Quotation!A12:J{11 + len(item_rows)}', 'values': item_rows},
        {'range': 'Quotation!L3:L5', 'values': [
            ["STAFF REVISION CONTROL"],
            ["Type REVISE in M4 ->"],
            ["Status: Initial Quote Sent"]
        ]},
        {'range': 'Quotation!M4', 'values': [[""]]}
    ]

    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=new_sheet_id,
        body={'valueInputOption': 'USER_ENTERED', 'data': batch_data}
    ).execute()

    # 5. Export initial PDF and email to client
    pdf_filename = f"{sheet_title}.pdf"
    pdf_bytes = export_sheet_to_pdf_InDrive(
        drive_service, authed_session, new_sheet_id, quotation_gid, pdf_filename, folder_id
    )
    send_quote_email(gmail_service, client_email, quote_no, company_name, pdf_bytes, pdf_filename)


def process_approved_quotes(sheets_service, drive_service, gmail_service, authed_session):
    """Scans Bulk Import for Action == APPROVE, syncs new items to Master_Stock, and creates quotes."""
    stock_map, _ = load_master_stock_map(sheets_service)

    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range='Bulk Import!A:I'
    ).execute()
    rows = res.get('values', [])
    if len(rows) <= 1:
        print("No rows found in Bulk Import.")
        return

    batches = {}
    for row_idx, row in enumerate(rows[1:], start=2):
        row += [''] * (9 - len(row))
        status, raw_req, official_name, qty, uom, unit_price, total, company, action = row[:9]

        if action.strip().upper() != 'APPROVE':
            continue

        company_clean = company.strip() or "Walk-in Client"
        email_match = re.search(r'\[(.*?)\]', raw_req)
        client_email = email_match.group(1).strip() if email_match else ""
        clean_raw_alias = re.sub(r'^\[.*?\]\s*', '', raw_req).strip()

        stock_entry = stock_map.get(official_name.strip().upper(), {})
        final_hsn = stock_entry.get('HSN_CODE', '')
        final_uom = (uom.strip() or stock_entry.get('UOM', 'NO')).upper()
        final_brand = stock_entry.get('BRAND', '')
        final_price = unit_price.strip() or stock_entry.get('BASE_PRICE', '0')

        sync_item_to_master_stock(
            sheets_service, stock_map,
            official_name, final_hsn, final_uom, final_brand, final_price, clean_raw_alias
        )

        if company_clean not in batches:
            batches[company_clean] = {
                'client_email': client_email,
                'items': [],
                'row_indices': []
            }

        batches[company_clean]['items'].append({
            'official_name': official_name.strip(),
            'hsn': final_hsn,
            'qty': qty,
            'uom': final_uom,
            'brand': final_brand,
            'base_price': final_price
        })
        batches[company_clean]['row_indices'].append(row_idx)

    if not batches:
        print("No rows marked 'APPROVE' in Bulk Import.")
        return

    for company_name, batch in batches.items():
        quote_no = get_next_quote_number(drive_service)
        print(f"\nProcessing Approved Quote {quote_no} for '{company_name}' ({len(batch['items'])} items)...")
        create_quote_spreadsheet_and_pdf(
            sheets_service, drive_service, gmail_service, authed_session,
            quote_no, company_name, batch['client_email'], batch['items']
        )

        update_data = [
            {'range': f'Bulk Import!I{r}', 'values': [[f'✅ SENT ({quote_no})']]}
            for r in batch['row_indices']
        ]
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={'valueInputOption': 'USER_ENTERED', 'data': update_data}
        ).execute()


def process_revisions(sheets_service, drive_service, authed_session):
    """Scans saved QTN spreadsheets; if staff typed REVISE in M4, syncs new items & generates Revised PDF."""
    stock_map, _ = load_master_stock_map(sheets_service)

    query = (
        "name contains 'QTN-' and "
        "mimeType = 'application/vnd.google-apps.spreadsheet' and "
        "trashed = false"
    )
    files = drive_service.files().list(q=query, fields="files(id, name, parents)").execute().get('files', [])

    for f in files:
        sheet_id = f['id']
        sheet_name = f['name']
        parents = f.get('parents', [ARCHIVE_FOLDER_ID])
        folder_id = parents[0] if parents else ARCHIVE_FOLDER_ID

        try:
            trigger_res = sheets_service.spreadsheets().values().get(
                spreadsheetId=sheet_id, range='Quotation!M4'
            ).execute()
            trigger_val = trigger_res.get('values', [['']])[0][0].strip().upper()
        except Exception:
            continue

        if trigger_val not in ('REVISE', 'TRUE', 'YES', '1'):
            continue

        print(f"\n[Revision Detected] Processing '{sheet_name}'...")

        rows_res = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range='Quotation!A12:G100'
        ).execute()
        for r in rows_res.get('values', []):
            r += [''] * (7 - len(r))
            _, desc, hsn, qty, uom, brand, base_price = r[:7]
            if desc.strip():
                sync_item_to_master_stock(
                    sheets_service, stock_map, desc, hsn, uom, brand, base_price
                )

        parts = sheet_name.split()
        quote_no = parts[0] if parts else "QTN-01"
        first_word = parts[1] if len(parts) > 1 else "Client"
        revised_pdf_name = f"{quote_no} {first_word} Revised.pdf"

        meta = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        quotation_gid = meta['sheets'][0]['properties']['sheetId']

        export_sheet_to_pdf_InDrive(
            drive_service, authed_session, sheet_id, quotation_gid, revised_pdf_name, folder_id
        )

        now_str = datetime.now().strftime('%d/%m %H:%M')
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                'valueInputOption': 'USER_ENTERED',
                'data': [
                    {'range': 'Quotation!M4', 'values': [['']]},
                    {'range': 'Quotation!L5', 'values': [[f'✅ {revised_pdf_name} ready ({now_str})']]}
                ]
            }
        ).execute()


if __name__ == '__main__':
    print("=== Jai Electricals Quote Manager & Auto-Learner ===")
    sheets_svc, drive_svc, gmail_svc, session = get_services()
    process_approved_quotes(sheets_svc, drive_svc, gmail_svc, session)
    process_revisions(sheets_svc, drive_svc, session)
    print("\n=== Quote Manager Complete ===")