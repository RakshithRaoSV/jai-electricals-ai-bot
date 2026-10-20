import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

def format_quotation_template():
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    sheets_service = build('sheets', 'v4', credentials=creds)

    # Get the Sheet ID for 'Quotation' tab
    meta = sheets_service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    quotation_gid = next(s['properties']['sheetId'] for s in meta['sheets'] if s['properties']['title'].lower() == 'quotation')

    # Define the precise column widths for a professional PDF layout
    col_widths = [40, 260, 80, 50, 60, 100, 90, 60, 90, 100]
    
    requests = []
    
    # 1. Hide Gridlines
    requests.append({
        'updateSheetProperties': {
            'properties': {'sheetId': quotation_gid, 'gridProperties': {'hideGridlines': True}},
            'fields': 'gridProperties.hideGridlines'
        }
    })

    # 2. Set Column Widths (A through J)
    for i, width in enumerate(col_widths):
        requests.append({
            'updateDimensionProperties': {
                'range': {'sheetId': quotation_gid, 'dimension': 'COLUMNS', 'startIndex': i, 'endIndex': i+1},
                'properties': {'pixelSize': width},
                'fields': 'pixelSize'
            }
        })

    # 3. Style the Table Headers (Row 11) -> Bold, Gray Background, Center Align, Borders
    requests.append({
        'repeatCell': {
            'range': {'sheetId': quotation_gid, 'startRowIndex': 10, 'endRowIndex': 11, 'startColumnIndex': 0, 'endColumnIndex': 10},
            'cell': {
                'userEnteredFormat': {
                    'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9},
                    'textFormat': {'bold': True},
                    'horizontalAlignment': 'CENTER',
                    'borders': {
                        'top': {'style': 'SOLID'}, 'bottom': {'style': 'SOLID'},
                        'left': {'style': 'SOLID'}, 'right': {'style': 'SOLID'}
                    }
                }
            },
            'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,borders)'
        }
    })

    # 4. Set Currency Format (₹) for Price Columns (G, I, J) from Row 12 downwards
    currency_format = {'numberFormat': {'type': 'CURRENCY', 'pattern': '[$₹-en-IN]#,##0.00'}}
    for col in [6, 8, 9]: # G=6, I=8, J=9
        requests.append({
            'repeatCell': {
                'range': {'sheetId': quotation_gid, 'startRowIndex': 11, 'endRowIndex': 100, 'startColumnIndex': col, 'endColumnIndex': col+1},
                'cell': {'userEnteredFormat': currency_format},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })

    # Execute the formatting
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={'requests': requests}
    ).execute()
    print("✅ Master Template Formatting Complete! PDF exports will now look like professional invoices.")

if __name__ == '__main__':
    format_quotation_template()