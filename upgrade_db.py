import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

def run_upgrade():
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    sheets = build('sheets', 'v4', credentials=creds)

    headers = [
        {'range': 'Bulk Import!J1', 'values': [['Address']]},
        {'range': 'Bulk Import!K1', 'values': [['GST Mode']]},
        {'range': 'Bulk Import!L1', 'values': [['Tax Region']]},
        {'range': 'Bulk Import!M1', 'values': [['Freight']]}
    ]
    
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={'valueInputOption': 'USER_ENTERED', 'data': headers}
    ).execute()
    print("✅ Successfully added Address, GST Mode, Tax Region, and Freight columns to 'Bulk Import'!")

if __name__ == '__main__':
    run_upgrade()
    