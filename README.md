# Jai Electricals AI Quoting Engine

An end-to-end automated RFQ and Quotation system built for an electrical hardware procurement business. This backend architecture integrates multiple Google APIs and Gemini AI to eliminate manual data entry and automate quotation generation.

## Features
* **Module A (Email Ingestion):** Uses the Gmail API and OAuth 2.0 to scan, extract, and clean raw RFQ emails.
* **Module B (AI Semantic Mapper):** Uses Gemini 3.8 Flash to intelligently map messy, unstructured client item requests to the official master catalog.
* **Module C (Google Sheets Staging UI):** Pushes structured data into a staging sheet for staff review and price verification via the Google Sheets API.
* **Module D (Automated Dispatch):** Dynamically clones Google Sheet templates, computes financial totals, generates professional PDFs, organizes Google Drive archiving, and emails the client.
* **Module E (Auto-Learning Loop):** Silently updates the master catalog whenever staff add new items during a manual revision.

## Tech Stack
* Python
* Google Sheets API, Gmail API, Google Drive API
* Google Gemini API (3.8 Flash)
* Authentication: Service Accounts & OAuth 2.0