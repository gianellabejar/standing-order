# Standing Order

[![tests](https://github.com/gianellabejar/standing-order/actions/workflows/tests.yml/badge.svg)](https://github.com/gianellabejar/standing-order/actions/workflows/tests.yml)

> **Archived hackathon project.** The hosted demo and its database were shut down after the event, so the live link no longer works. The code, tests and demo video are here to read and run locally.

**A purchasing agent for small shops, with memory the owner can govern.** The owner texts an order. The agent prices it from supplier emails and PDF catalogues, remembers how each supplier works, asks for approval by text, and then phones the supplier to place the order. Every memory it uses can be read, corrected, expired or forgotten, and every order shows which memories changed the decision.

Built in one day at the MongoDB *Agents in Context* hackathon (Toronto, November 2026) for problem statement 2, "Memory you can govern".

<!-- TODO: embed the 1-minute demo video and add 2-3 dashboard screenshots here -->

---

## The problem

Agents that "remember" usually just stuff old text into the prompt. That raises three questions, and this project answers each one in code:

| Question | Answer in this repo |
|---|---|
| How do you keep one client's memory from leaking into another's? | One data-access layer ([shared/db.py](shared/db.py)) injects `tenant_id` into every query, including the `$vectorSearch` pre-filter. No other module touches Mongo collections. A leak test asks tenant B for tenant A's memories ([tests/test_tenant_isolation.py](tests/test_tenant_isolation.py)). |
| How does the agent fix or drop a memory that is wrong or out of date? | Memories are `active`, `superseded` or `retracted`. A correction writes a new memory that points at the old one, so the history is kept. `expires_at` takes a memory out of retrieval, and a TTL index on `purge_at` hard-deletes it later. "Forget" removes the content and leaves a tombstone in `audit_events` ([agent/memory/governance.py](agent/memory/governance.py)). |
| How do you prove a memory changed the agent's behavior? | Each order stores the memories and quotes it used (a *memory receipt*). `replay_without_memory` re-runs the same supplier decision with those memories removed, and the dashboard shows both outcomes ([agent/decision.py](agent/decision.py)). |

## The demo moment

The seed data is built so the decision depends on one memory:

- Supplier A quotes flour at **$3.45**.
- Supplier B's list price is **$3.48**.
- A seeded memory says Supplier B gives this shop **8% off list**.

With the memory, B costs **$3.20** and wins. Replayed without it, A wins at $3.45. Supplier choice and price math are plain Python, so the replay uses no LLM call and costs no tokens.

## How an order flows

```
Owner SMS ──► /sms/inbound (Twilio)
                │
                ▼
   Session (LangGraph thread, MongoDBSaver checkpointer)
   starts from a context pack, never the old transcript
                │
                ▼
   Parse the text (LLM, structured output)
   ► resolve aliases and "same as last time" (code)
   ► valid quotes only (code)
   ► choose supplier with memory-derived terms (code) ──► memory receipt
                │
                ▼
   "Reply YES" approval by SMS
                │
                ▼
   ElevenLabs outbound call to the supplier, with a call brief built from memory
                │
                ▼
   Post-call webhook ► extract outcome from the transcript (LLM, structured)
   ► order is `confirmed` only if a confirmation number was captured, else `needs_owner`
                │
                ▼
   Outcome texted to the owner; new memories written with this call as their source
```

Supplier prices come in separately: forwarded emails are polled over IMAP, and PDF catalogues are parsed once into `price_quotes` with a link back to the source document and page. They are never pasted into a prompt again.

## Design choices worth noting

- **Code over LLM calls.** The LLM parses free text and reads transcripts. Everything deterministic (supplier choice, price math, quantities, approval thresholds) is plain code, and every LLM response is validated with Pydantic.
- **Sessions with a hard token cap.** A text thread is split into many sessions. Each new session starts from a context pack (shop profile, open tasks, the top 5 relevant memories from Atlas Vector Search, the last session summary) that is built under a token cap, dropping the lowest-scoring memories first ([agent/memory/context_pack.py](agent/memory/context_pack.py)).
- **A memory needs a source.** A memory cannot be written without a source (SMS, email, PDF, call or session), so every fact the agent acts on can be traced back.
- **A finished call is not a confirmed order.** The ledger only marks an order `confirmed` when the supplier's side of the transcript contains a confirmation number. Voicemail, no answer or a call that ends without a number goes to `needs_owner`.
- **Honest voice calls.** The voice agent says it is an AI assistant ordering for the shop and that the call is transcribed. Code only allows calls to the one configured supplier number.

## Stack

Python, FastAPI, LangGraph (`langgraph-checkpoint-mongodb`), MongoDB Atlas (Vector Search, TTL indexes), Voyage AI embeddings, DeepSeek, Twilio SMS, ElevenLabs Agents (outbound calls over Twilio), pypdf, Jinja2 server-rendered dashboard, deployed on Vercel.

## Repo map

| Path | What |
|---|---|
| `agent/` | Order flow, supplier decision and replay, sessions, memory governance, context pack, voice calls |
| `ingest/` | SMS webhook, IMAP poller, email and PDF quote extraction |
| `shared/` | Tenant-scoped data-access layer, Pydantic models, supplier and product data |
| `ui/` | Owner dashboard: orders, memory receipts, correct / expire / forget |
| `fixtures/` | Real ElevenLabs post-call payloads (phone numbers and ids redacted) used in tests |
| `data/seed/` | Synthetic tenants, suppliers, catalogue and seeded memories ([data/README.md](data/README.md)) |
| `docs/` | Webhook and API contracts, deployment notes |

## Running it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in your own Atlas URI and API keys
make docs              # generate the synthetic supplier PDFs and email
make reset             # seed the database
make serve             # http://localhost:8000
make sms-sim           # send owner texts without a phone
```

`VOICE_PROVIDER=mock` (the default) simulates the supplier call by posting a canned post-call payload back to the app, so the full flow runs without placing a real call. Set it to `elevenlabs` to place real calls.

Tests: `make test`. They run on in-memory Mongo (mongomock) with recorded LLM responses, so they need no keys; CI runs them on every push. With `MONGODB_URI` set, the Atlas tests run too (sessions on the real checkpointer, the tenant leak test including Vector Search), and the live DeepSeek test runs only with `RUN_LLM_TESTS=1`.

## Data and disclosures

- **All data is synthetic.** Businesses, suppliers, prices, SKUs, emails (`.example` domains) and phone numbers (`+1555…`) are made up. Real phones and inboxes used during the event were set only through environment variables and are not in this repo.
- **Seeded memories are marked `seeded: true`** in the database and listed in [data/seed/memories.json](data/seed/memories.json), including the 8% discount memory behind the demo.
- **Shop names.** The demo shop is Sean's Bakery. The recorded test calls in `fixtures/` were made before a rename and still say "Maple Bakery"; the internal tenant id is still `maple`.
- **The two seeded orders were produced with the mock voice provider**, not a real call. The files in `fixtures/` are real ElevenLabs responses from test calls to a teammate's phone, with phone numbers and ids redacted.

## Team

Built by **Gianella Bejar** (voice calls, sessions, memory engine, receipts and replay) and [**Sean Rodriguez Lee**](https://github.com/SeanR27) (email and PDF ingestion, quotes, dashboard, tenant leak test).
