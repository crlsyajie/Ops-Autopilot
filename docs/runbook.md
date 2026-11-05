# Runbook

## Run it

```bash
source .venv/bin/activate
python -m autopilot serve            # dashboard + API on http://127.0.0.1:8000
python -m autopilot watch            # in a second terminal: process files as they land in var/inbox
```

Configuration lives in `.env` (see `.env.example`), `config/policy.json` (limits) and `config/suppliers.json` (master data). Restart after changing them.

## Connect it

### Email (Gmail)
1. Turn on 2-step verification on the Google account and create an **app password**.
2. In `.env`: `IMAP_HOST=imap.gmail.com`, `IMAP_USER=you@gmail.com`, `IMAP_PASSWORD=<app password>`.
3. `python -m autopilot email` saves unread emails (with PDF and photo attachments) to the inbox, marks them read and processes them. Schedule it with cron every few minutes, or run it from n8n.

### Slack (review alerts)
Create an incoming webhook in a Slack app, put its URL in `SLACK_WEBHOOK_URL`. Each document that needs review posts a message with a link to its page. Set `AUTOPILOT_PUBLIC_URL` to the address people open the dashboard at.

### Google Sheets (ledger mirror)
1. In a Google Sheet: Extensions → Apps Script, paste:
   ```javascript
   function doPost(e) {
     const body = JSON.parse(e.postData.contents);
     const ss = SpreadsheetApp.getActiveSpreadsheet();
     const sheet = ss.getSheetByName(body.sheet) || ss.insertSheet(body.sheet);
     const cols = Object.keys(body.row);
     if (sheet.getLastRow() === 0) sheet.appendRow(cols);
     sheet.appendRow(cols.map(c => body.row[c]));
     return ContentService.createTextOutput("ok");
   }
   ```
2. Deploy → New deployment → Web app, access "Anyone with the link". Put the URL in `SHEETS_WEBHOOK_URL`.

Rows go to tabs named `payables`, `expenses` and `orders`. If the sheet is down, the CSV ledger still records the row and the trace shows `action_sheets_failed`.

### n8n, Make or Zapier
Send any file to the webhook:

```
POST /api/webhook/document
{"filename": "invoice.pdf", "content_base64": "<base64 of the file>"}
```

The response has the document `id`, `status`, `route` and `reason`, so the flow can branch on it (for example, post to a different channel when `route` is `review`). `GET /api/documents?status=needs_review` lists the queue and `GET /api/metrics` returns the dashboard numbers.

## When something goes wrong

| You see | Meaning | Do this |
|---|---|---|
| Status `failed` | An attempt crashed (timeout, API down, unreadable file) | `python -m autopilot retry`. `watch` also retries each cycle |
| Status `dead` | Failed 3 times | Read `error` and the trace on the document page; fix the cause, then delete the row from `documents` to let it run again |
| Many `low_confidence` reviews | Poor photos or a new document layout | Add a few of them to `evals/` with the right answers and check the extraction prompt |
| Agent keeps saying `needs_human` for a supplier | The name is not in `suppliers.json` | Add the supplier, or add the variant to its `aliases` |
| `bank_details_changed` | The document asks for payment to a different account than the one on file | Phone the supplier on a number you already have. Never trust the email |

## Costs

Cost per document is shown on the dashboard and in each trace, from the token counts the API returns. A clean document is one extraction call; a document that needs triage adds 2 to 4 agent calls. To lower cost, measure first with `python evals/run_evals.py --mode live`, then try `AUTOPILOT_EXTRACT_EFFORT=low` or a smaller model and keep the change only if routing accuracy holds.

## Before putting it on the internet

The dashboard has no login. Keep it on localhost, or put it behind an authenticating proxy (Cloudflare Access, Tailscale, an OAuth proxy) and set `reviewer` from the signed-in user instead of the form field.
