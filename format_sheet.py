import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')

def beautify_workspace():
    print("Connecting to Google Sheets...")
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    service = build('sheets', 'v4', credentials=creds)

    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    
    # 1. Format Bulk Import Tab
    bulk_gid = next(s['properties']['sheetId'] for s in meta['sheets'] if s['properties']['title'] == 'Bulk Import')
    requests = [
        {"updateSheetProperties": {"properties": {"sheetId": bulk_gid, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": bulk_gid, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.08, "green": 0.2, "blue": 0.35}, "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)"}},
        {"repeatCell": {"range": {"sheetId": bulk_gid, "startRowIndex": 1}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}}, "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}}
    ]

    # 2. Check for History Tab, Create and Format if missing
    history_exists = any(s['properties']['title'] == 'History' for s in meta['sheets'])
    if not history_exists:
        print("Creating 'History' tab...")
        service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': [{'addSheet': {'properties': {'title': 'History'}}}]}).execute()
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range='History!A1', valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS',
            body={'values': [['Date Sent', 'Quote No', 'Company', 'Client Email', 'PO Reference']]}
        ).execute()
        
        # Fetch the new GID for History to format it
        meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        history_gid = next(s['properties']['sheetId'] for s in meta['sheets'] if s['properties']['title'] == 'History')
        
        requests.extend([
            {"updateSheetProperties": {"properties": {"sheetId": history_gid, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": history_gid, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.08, "green": 0.2, "blue": 0.35}, "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}}
        ])

    service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': requests}).execute()
    print("✅ Workspace successfully built and beautified!")

if __name__ == '__main__':
    beautify_workspace()