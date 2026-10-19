import base64
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ['https://mail.google.com/']

def get_unread_emails():
    # Load the persistent token we just generated
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    service = build('gmail', 'v1', credentials=creds)
    
    print("Scanning inbox for unread messages...")
    results = service.users().messages().list(userId='me', q='is:unread').execute()
    messages = results.get('messages', [])

    if not messages:
        print("No unread messages found.")
        return []

    print(f"Found {len(messages)} unread message(s).\n")
    
    extracted_emails = []
    for msg in messages:
        # Fetch the full email payload
        msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        payload = msg_data.get('payload', {})
        headers = payload.get('headers', [])
        
        # Extract Subject and Sender
        subject = next((header['value'] for header in headers if header['name'] == 'Subject'), "No Subject")
        sender = next((header['value'] for header in headers if header['name'] == 'From'), "Unknown Sender")
        
        # Extract Email Body
        body = ""
        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain':
                    data = part['body'].get('data')
                    if data:
                        body = base64.urlsafe_b64decode(data).decode('utf-8')
                        break
        else:
            data = payload['body'].get('data')
            if data:
                body = base64.urlsafe_b64decode(data).decode('utf-8')

        print(f"--- Email from {sender} ---")
        print(f"Subject: {subject}")
        print(f"Body Preview: {body[:150].strip()}...\n")
        
        extracted_emails.append({
            'id': msg['id'],
            'sender': sender,
            'subject': subject,
            'body': body
        })
        
    return extracted_emails

if __name__ == '__main__':
    get_unread_emails()