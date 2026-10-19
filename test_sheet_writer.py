import os
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = 'credentials.json'
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')

def append_to_bulk_import(matches):
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    service = build('sheets', 'v4', credentials=creds)
    
    # Format the JSON data into a flat list of rows matching your A-G columns
    rows_to_insert = []
    for item in matches:
        # A: Match Status, B: Client Request, C: Official Item Name, D: Qty, E: Unit, F: Price, G: Total
        status = "✅ Matched" if item['matched'] else "❌ NEEDS REVIEW"
        official_name = item['official_name'] if item['official_name'] else "—"
        
        row = [
            status,                # A: Match Status
            item['raw_input'],     # B: Client Request (Raw)
            official_name,         # C: Official Item Name
            item['quantity'],      # D: Qty
            item['unit'] or "",    # E: Unit
            "",                    # F: Unit Price (Left blank for staff to fill)
            ""                     # G: Total
        ]
        rows_to_insert.append(row)
        
    body = {
        'values': rows_to_insert
    }
    
    # Push the rows to the Bulk Import tab
    result = service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range='Bulk Import!A:G',
        valueInputOption='USER_ENTERED', # Allows Sheets to format numbers automatically
        insertDataOption='INSERT_ROWS',
        body=body
    ).execute()
    
    updates = result.get('updates', {})
    print(f"\nSuccess! Pushed {updates.get('updatedRows', 0)} rows to the Bulk Import tab.")

if __name__ == '__main__':
    # We will simulate the exact output your AI generated in the last step
    sample_ai_output = [
      {
        "raw_input": "fan regulator switch type fybros - 10 nos",
        "official_name": "FAN REGULATOR (SWITCH TYPE)",
        "quantity": 10,
        "unit": "nos",
        "matched": True
      },
      {
        "raw_input": "havls 1.5 wire - 10 coils",
        "official_name": "Havells 1.5 sq mm Wire",
        "quantity": 10,
        "unit": "coils",
        "matched": True
      },
      {
        "raw_input": "2.5 sq mm anchor wire red 5 bundles",
        "official_name": None,
        "quantity": 5,
        "unit": "bundles",
        "matched": False
      }
    ]
    
    print("Connecting to Google Sheets via Service Account...")
    append_to_bulk_import(sample_ai_output)