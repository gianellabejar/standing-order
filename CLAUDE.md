# CLAUDE.md: Order Desk Agent (working title)

Read this whole file before writing code. It is the single source of truth for scope, architecture and who builds what. Scope changed on Nov 14 at 4 AM: drift healing is CUT. The priority is the ElevenLabs phone call, and the problem statement is now #2.

## 1. What we are building

One sentence: a purchasing agent for small shops that the owner texts, that reads supplier prices from emails and PDF catalogues, remembers how each supplier works, and places the order by phone call (ElevenLabs), with memory the owner can read, correct, expire and forget.

Event: MongoDB "Agents in Context" hackathon, Toronto, Sat Nov 14 2026. Hacking starts 10:45 AM. Submission due 5:00 PM (target: submitted by 4:30). Round one judging 5:15 PM.

Problem statement 2, "Memory you can govern". The three questions we must answer, with a demo moment each:
1. How do you keep one client's memory from leaking into another's? (tenant isolation, demo: second business sees nothing)
2. How does the agent fix or drop a memory that turns out wrong or expired? (supersede, expire, forget; demo: owner corrects a price by text)
3. How do you prove a memory changed the agent's behavior and didn't just fill the prompt? (memory receipt plus replay without memory)

Judging (round one): Technical Demo 35%, Implementation Difficulty 30%, Creativity 15%, Impact 20%. Round two: equal weights. A reliable, visible demo beats extra features.

## 2. Rules that bind the code

- Everything we submit is original work written during the event. No product code before 10:45. This file, a README skeleton, a blank repo and account setup are fine before then.
- Build inside the MongoDB Atlas Hackathon Sandbox (invite is in email). Use that cluster, not a personal one.
- Public GitHub repo, accessible demo link, 1-minute demo video with audio, short description, all teammates added on Cerebral Valley.
- All data is synthetic. Never use real customer, supplier, employer or telecom data. Do not name or hint at any employer.
- Secrets live in `.env` only, `.gitignore`d from the first commit. Commit `.env.example` with names only.
- Claims must be true. Seeded memories are marked `seeded: true` and disclosed in the README and in the demo. Numbers shown on screen (tokens, memory-changed counts) come from real runs. Never invent statistics.
- Voice calls: the agent says at the start that it is an AI assistant ordering on behalf of the shop. Only ever call a teammate's verified phone, never a real business. Tell the person that the call is transcribed.

## 3. Stack (decided)

- Python 3.11, FastAPI for the agent API, webhooks and supplier mocks. Dashboard is server-rendered HTML with a little JS (no front-end build step).
- LangGraph with `langgraph-checkpoint-mongodb` (`MongoDBSaver`): one `thread_id` per session. `interrupt` for owner approval.
- LLM: DeepSeek through `langchain-deepseek` (`ChatDeepSeek`), key in `DEEPSEEK_API_KEY`. Model names change: check current docs on the day and use the fast chat model. Within the first 30 minutes run a tool-calling and structured-output smoke test. Keep a fallback provider key. As far as we know DeepSeek's API is text-only, so catalogues must be text PDFs.
- SMS: Twilio. Trial accounts can only message verified numbers (up to 5), so the demo uses team phones. An SMS simulator posts to the same `/sms/inbound` webhook, so a carrier problem cannot kill the demo.
- Voice: ElevenLabs Agents with the native Twilio integration (import the Twilio number with Account SID and Auth Token), outbound call triggered from our code with per-call variables. Post-call webhook returns the transcript.
- Email: a dedicated throwaway inbox the owner forwards supplier emails to, polled over IMAP. No Gmail OAuth.
- PDFs: `pdfplumber` or `pypdf` for text, then LLM structured extraction. Text PDFs only.
- Atlas: `pymongo`, Vector Search on memories (Voyage AI embeddings), TTL index for purging.
- Auth for the dashboard: email and password (bcrypt) with a signed cookie. Seed two tenants plus a register flow.
- Everything the LLM returns is structured output validated with Pydantic. Prefer plain code over an LLM call.

## 4. Architecture

### Channels (adapters)

- `POST /sms/inbound` (Twilio form fields). The sender's phone number maps to a tenant. The simulator posts the same shape.
- Email poller (every 30 to 60 s) stores raw documents and extracts quotes.
- Dashboard (section 6).
- Voice call via ElevenLabs.

### Sessions

- One text thread is MANY sessions. A session closes after 30 minutes of silence or when its task completes.
- A new session gets a new LangGraph `thread_id` and starts from a context pack, never the old transcript.
- On close: one cheap LLM call writes a summary of at most 150 tokens and proposes memory candidates. Candidates become memories only if they have a source.

### Context pack (built by code, hard token cap)

Contents: tenant profile (fixed, about 150 tokens), open tasks, top 5 relevant memories (Vector Search filtered by `tenant_id`, `status=active`, `expires_at > now`), last session summary. Target cap about 1,500 tokens, to be measured and shown in the dashboard. If over the cap, drop lowest-score memories first.

### Token efficiency rules

- Catalogues and emails are parsed once into `price_quotes`. They are never pasted into a prompt a second time.
- Supplier choice, price math and quantity defaults are plain code.
- Rolling summary of the current session after about 8 turns.
- Log prompt and completion tokens for every LLM call, per session and per order.

### Agent flow for an order text

1. Parse the text (LLM, structured): items, quantities, deadline, "same as last time".
2. Resolve with memory and orders (code): aliases, default quantities.
3. Lookup quotes (code): valid quotes only (`valid_until` in the future).
4. Choose supplier (code): apply memory-derived terms, e.g. "B gives us 8% off list". Record exactly which memory IDs and quote IDs were used (the memory receipt).
5. Approval (owner, by SMS): "Reply YES". LangGraph `interrupt`.
6. Place the order: phone call by default for suppliers whose memory says they take phone orders. Email as fallback.
7. Confirm: an order is `confirmed` only when a confirmation number is captured from the call transcript (or an email reply). A call that ended is not a confirmed order.
8. Text the owner the outcome. Write new memories (with source = this call or session).

### Voice call

1. Code builds a call brief from memory: item, quantity, price to confirm, PO number, who to ask for, delivery day.
2. Trigger the ElevenLabs outbound call to the supplier number with the brief as per-call variables.
3. The agent introduces itself as an AI assistant, places the order, asks for a confirmation number.
4. Post-call webhook delivers the transcript. Code extracts the outcome (accepted or not, confirmation number, anything the supplier said that changes terms) with one structured LLM call.
5. Ledger: `calling`, then `confirmed` only with a number, else `needs_owner`.
6. Fallbacks, in this order: order by email; play a clearly labelled recording of a tested call during the demo.
7. Test calls in the first 30 minutes and again at 3:30 PM. Run the demo call 5 times in a row before the freeze.

### Governance (the statement 2 work)

- Tenant isolation: every record has `tenant_id`. One data-access layer injects the tenant filter into every query, including Vector Search pre-filters. No prompt code builds its own query. Add an automated leak test that asks tenant B for tenant A's facts.
- Fix or drop: memory `status` is `active`, `superseded` or `retracted`. Correction by text writes a new memory with `supersedes` pointing at the old one. `expires_at` excludes a memory from retrieval. `purge_at` plus a TTL index hard-deletes after a grace period. "Forget X" deletes the content, keeps a tombstone in `audit_events`.
- Proof: each order stores `memories_used`. `replay_without_memory(order_id)` reruns the deterministic decision with those memory-derived inputs removed. It is code, so it costs no tokens. The dashboard shows both outcomes and a running count "memory changed X of Y decisions".

### Collections

- `tenants`, `users`: tenant name, phone numbers, owner prefs.
- `sessions`: `tenant_id`, `thread_id`, `started_at`, `closed_at`, `summary`, `embedding`, `tokens`.
- `memories`: `tenant_id`, `kind`, `text`, `structured` (supplier, key, value), `source` (type: sms, email, pdf, call, session; ref), `status`, `supersedes`, `confidence`, `created_at`, `expires_at`, `purge_at`, `seeded`, `embedding`, `last_used_at`.
- `price_quotes`: `tenant_id`, `supplier_id`, `sku`, `unit`, `price`, `valid_until`, `source` (document id, page), `status`.
- `documents`: raw emails and PDFs (reference plus hash), for provenance.
- `orders`: ledger with `status` (planned, calling, confirmed, needs_owner, failed), `idempotency_key` (unique), `memories_used`, `quotes_used`, `confirmation_number`, `call_id`, `tokens`.
- `calls`: ElevenLabs conversation id, brief, transcript, outcome.
- `audit_events`: `ts`, `actor` (code, agent, owner), `event`, `ids`, tombstones.
- LangGraph checkpoint collections are created by `MongoDBSaver`.

## 5. Synthetic data

- 2 tenants: Maple Bakery (main) and Harbour Cafe (for the leak demo). Each has an owner phone number (the team phones) and a seeded profile.
- 2 suppliers. Supplier B's phone number is a teammate's verified phone. Supplier A quotes by email text. Supplier B sends a PDF catalogue (generate it, text-based).
- Quote design: B list price for flour is $3.48, A is $3.45. Seeded memory (disclosed): "Supplier B gives Maple Bakery 8% off list." With the memory B is $3.20 and wins. Without it A wins. This is the memory-changed-the-order moment.
- Other seeded memories (disclosed): B takes orders by phone, ask for Dave, PO number format; approval needed above a threshold; "flour" means a specific SKU.
- 6 to 8 pre-written owner texts, including "same as last time" and a price correction ("Oats from B are $2.80 now").

## 6. Dashboard (reference, not the main feature)

Pages: register and log in; Orders (status, memory receipt, replay with and without memory, tokens); Quotes (supplier, price, validity, source document); Memory (each memory with source, status, expiry, buttons: correct, expire, forget; superseded history); Account (phone numbers, forwarding address). The Memory page and the receipt on an order are the parts that matter for judging.

## 7. Demo beats (3 minutes, round one)

1. 0:00 Owner texts: "Need 40 bags of flour for Friday, same as last time." A fresh session starts.
2. 0:20 The agent picks B because of a valid quote plus the 8% memory. Owner replies YES.
3. 0:45 The agent calls the supplier (teammate). Judges listen. The call runs while we show the next beats.
4. 1:15 Memory receipt and replay: with memory B at $3.20, without it A at $3.45.
5. 1:55 Owner corrects a price by text. The old quote is superseded and the history stays. The call finishes: confirmation number, ledger confirmed, confirmation text arrives.
6. 2:20 Second business asks the same question and sees nothing from the first. Show tokens used for the order (measured).

Definition of done for each beat: it works three times in a row from a clean reset. The call must be tested at least 5 times before the freeze.

## 8. Plan and owners

Owners: Gia = voice call first, then sessions and the memory engine. Partner = email and PDF ingestion, quotes, dashboard, leak test, token counter. Shared = rehearsal.

| Time | Gia | Partner |
|---|---|---|
| 10:45 to 11:30 | Repo scaffold, Atlas connection, DeepSeek smoke test, one test call through ElevenLabs | Twilio SMS round trip and simulator, IMAP inbox test |
| 11:30 to 1:00 | Voice call end to end with a hard-coded brief, post-call webhook to ledger | PDF generation, extraction to `price_quotes`, `documents` |
| 1:00 to 2:30 | Sessions, context pack, memory engine (supersede, expire, forget), receipts and replay | Dashboard: auth, tenants, Orders, Quotes, Memory pages; leak test |
| 2:30 to 3:30 | Wire memory into the call brief, approval by SMS, call reliability runs | Token counter, polish, seed data, README |
| 3:30 | FEATURE FREEZE. Bug fixes only | |
| 3:30 to 4:30 | Record the 1-minute video (audio, show code), repo public, submit | Demo link tested from a phone, teammates added |

Decide hosting for the demo link and the webhook endpoint by noon (free tier host or a tunnel). Twilio and ElevenLabs webhooks need a public URL.

### Must have
SMS channel (real plus simulator). Governed memory (sessions, supersede, expire, forget, tenant isolation, receipts). Quotes from emails and PDFs. Order placement by email. Minimal dashboard.

### Priority, built first
The ElevenLabs call, working end to end by 1:00 PM, with a tested fallback.

### Stretch
Voyage matching polish, blast-radius style retraction, more voices, voice texting back summaries.

### Cut
Drift healing and kill-and-resume demos. The LangGraph checkpointer stays for sessions.

## 9. Pre-work allowed before 10:45 (accounts only, no product code)

- Twilio account, verify both phones, one real test text. Trial limits apply (verified recipients only, one number).
- ElevenLabs account and API key. Creator plan coupon from Discord `#coupon-codes` once checked in on Cerebral Valley.
- Throwaway inbox for supplier emails, DeepSeek key, Atlas sandbox invite accepted.

## 10. Working agreements for Claude Code

- Plan first: restate the task, list the files you will touch, then code. Small commits.
- Prefer plain code over an LLM call. If a step is deterministic, write code.
- Do not build stretch items before the must-haves and the call work end to end.
- Never commit secrets. Never hardcode a key. Never add real data.
- Run the relevant test or script after every change and report the actual output.
- If blocked for more than 10 minutes, stop and ask a human.
- When unsure whether something is a claim we can make on stage, do not write it in the README.
