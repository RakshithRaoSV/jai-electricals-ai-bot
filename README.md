# Jai Electricals AI Quoting Engine (Virtual Employee)

A fully automated, zero-touch quoting pipeline for Jai Electricals. This Python-based Virtual Employee reads incoming RFQ emails, uses Google Gemini AI to extract and normalize electrical items, queues them in Google Sheets for staff approval, and automatically generates and emails PDF quotations back to the client in the original email thread.

## Core Features
- **AI-Powered Inbox Parsing:** Uses `gemini-1.5-flash-latest` to read raw emails and attached spreadsheets, automatically auto-correcting sloppy client input into professional electrical item names, brands, and HSN codes.
- **Smart Thread Replying:** Injects `In-Reply-To` and `References` headers so automated PDF quotes appear natively within the client's original email conversation.
- **Auto-Rollback & Rate Limiting:** Built-in safeguards against Google API 60-second burst limits. Safely queues approvals and retries failed PDFs without crashing or sending duplicate emails.
- **Invisible Desktop Execution:** Runs via `master_loop.py` continuously in the background on a standard Windows laptop using a minimized taskbar footprint.

## System Architecture
1. `main.py`: Ingestion module. Scans Gmail, parses via Gemini, pushes structured JSON to Google Sheets.
2. `quote_manager.py`: Fulfillment module. Reads `APPROVE` commands from Sheets, generates PDFs via Google Drive, and dispatches threaded emails.
3. `master_loop.py`: The background daemon that executes the pipeline securely every 2 minutes.

## Setup Instructions

### 1. Prerequisites
- Python 3.10+
- Google Cloud Console Project with Gmail, Drive, and Sheets APIs enabled.
- Gemini API Key (Google AI Studio).

### 2. Installation
Clone the repo and set up the virtual environment:
```bash
git clone [https://github.com/YOUR_USERNAME/jai-electricals-quote-bot.git](https://github.com/YOUR_USERNAME/jai-electricals-quote-bot.git)
cd jai-electricals-quote-bot
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt