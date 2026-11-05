# Ops Autopilot

An AI operations agent for a small business. It reads the invoices, receipts and customer orders that arrive by email, upload or webhook, checks them, records the safe ones on its own and sends the rest to a person with a clear reason.

Built for **Kapehan sa Kanto**, a (fictional) café in Quezon City that gets documents in English, Filipino and Taglish, as emails, PDFs and phone photos.

```
inbox ─► extract (Claude) ─► validate (rules) ─► triage agent (only if needed) ─► decide ─► record / draft reply
                                                                                       └──► review queue (Slack + dashboard)
```

## What it shows

| Skill an automation role asks for | Where it is |
|---|---|
| Triggers and integrations | Folder watcher, IMAP email (Gmail), upload, JSON webhook for n8n / Make / Zapier, Slack, Google Sheets ([runbook](docs/runbook.md)) |
| AI with structure, not chat | Claude structured outputs fill a strict schema ([extract.py](autopilot/extract.py)) |
| A real agent, with guardrails | Tool-using triage agent with a hand-written loop, read-only tools, step limit and propose-then-verify ([agent.py](autopilot/agent.py)) |
| Human in the loop | Review queue with approve / edit / reject, every action signed by who approved it |
| Reliability | Idempotent processing (same file never recorded twice), retries, failed → dead after 3 attempts, one bad document never stops the rest |
| Observability | Every step written to a trace; cost, tokens and time per document |
| Evals | 13 labelled edge cases scored on field accuracy, routing and issue recall; CI gate ([evals](evals/)) |
| Business impact | Dashboard: automation rate, review queue, hours saved, AI cost per document |

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m autopilot demo     # processes the 13 sample documents
python -m autopilot serve    # dashboard at http://127.0.0.1:8000
pytest -q                    # 28 tests, offline
```

Without an API key everything runs in **replay mode**: the model's answers come from the hand-labelled files in `evals/expected/`. That tests the rules, routing, agent loop and plumbing, not the model. To use Claude, copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`. The CLI prints which mode it is in.

## Commands

| Command | What it does |
|---|---|
| `python -m autopilot demo` | Copy the sample documents into the inbox and process them |
| `python -m autopilot run` | Process the inbox once |
| `python -m autopilot watch` | Keep watching the inbox |
| `python -m autopilot email` | Pull unread emails over IMAP, then process |
| `python -m autopilot retry` | Retry failed documents |
| `python -m autopilot serve` | Dashboard and API |
| `python evals/run_evals.py` | Score the system on the eval set |

## The sample documents

They are deliberately hard: most of a bookkeeper's time goes to the exceptions, not the clean invoices.

| Case | What it tests | Expected |
|---|---|---|
| Clean supplier invoice, dairy receipt, bakery receipt | The happy path, VAT-inclusive totals | Recorded automatically |
| Taglish office order by email | Customer orders, reply drafting | Recorded, reply drafted (not sent) |
| Invoice from "Manila Paper Supply Company" with no due date | Agent matches the supplier and adds its payment terms | Recorded after the agent and a re-check |
| Same invoice re-sent under a short name | Duplicate payments | Review |
| Lines that don't add up | Arithmetic | Review |
| Invoice above ₱50,000 | Approval limits | Review |
| "URGENT: our NEW bank account" email | Payment fraud | Review |
| New foreign supplier in USD | Currency, unknown supplier | Review |
| Blurry torn receipt | Low confidence | Review |
| Price inquiry | Order with no total | Review |
| Newsletter | Noise | Ignored |

## Project layout

```
autopilot/
  extract.py    Step 1: Claude reads the document into a strict schema
  validate.py   Step 2: rules (math, limits, duplicates, bank changes, suppliers)
  agent.py      Step 3: triage agent, only for issues it is allowed to fix
  decide.py     Step 4: routing policy
  actions.py    Step 5: ledgers, Google Sheets, reply drafts, Slack
  pipeline.py   The orchestrator, retries and review actions
  llm.py        Claude client, cost meter, replay mode
  store.py      SQLite documents + event trace
  api.py        Dashboard, review queue, JSON API, webhook
  triggers.py   Folder watcher, IMAP email
config/         policy.json (limits) and suppliers.json (master data)
evals/          cases, labels and the eval runner
docs/           architecture, agent structure guide, runbook
```

## Docs

- [Architecture](docs/architecture.md): how a document flows and why each step is where it is
- [How agents are built](docs/agent-structure.md): the parts of any agent, mapped to this code
- [Runbook](docs/runbook.md): running it, integrations, failure handling, costs

## Status

Working: the full pipeline, agent, review flow, dashboard, API, tests, replay evals and CI.
Not yet measured: live-model eval scores (needs an API key; run `python evals/run_evals.py --mode live` and paste the table from `evals/results/latest_live.md` here). The IMAP, Slack and Sheets connectors are written but have not been run against real accounts.
