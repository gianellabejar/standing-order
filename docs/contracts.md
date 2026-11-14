# Contracts

Items marked (unverified) were not confirmed in the docs I could reach. Check them with a real call before relying on them.

## POST /sms/inbound
Twilio sends `application/x-www-form-urlencoded`. The simulator posts the same shape.

| Field | Use |
|---|---|
| `MessageSid` | idempotency key (Twilio can retry) |
| `From` | E.164 sender, mapped to a tenant via `Tenants.resolve_by_phone` |
| `To` | our Twilio number |
| `Body` | owner text |
| `NumMedia` | ignored |

`X-Twilio-Signature` is validated with the Twilio SDK when `TWILIO_AUTH_TOKEN` is set.

`X-Twilio-Signature` is checked against `PUBLIC_BASE_URL + /sms/inbound` when `PUBLIC_BASE_URL` is set (tunnels and proxies change the URL the app sees), else against the request URL. `scripts/sms_sim.py` signs its requests the same way, so the simulator also works with the token set. Bad signature: `403`.

Response: `200`, `Content-Type: text/xml`, body `<Response><Message>reply</Message></Response>`. An empty `<Response/>` sends nothing; later messages (approval, outcome) go through `ingest.sms.send_sms(tenant_id, to, body)` (Twilio REST; logged only when `SMS_DRY_RUN=1` or Twilio creds are missing).
Unknown `From` returns an empty `<Response/>` and writes no data.

Handling (implemented in `ingest/sms.py`):
1. Each accepted message writes `audit_events` `{_id: "sms:<MessageSid>", event: "sms.inbound", detail: {from, body}}`. A retried `MessageSid` hits the duplicate `_id` and gets an empty `<Response/>`.
2. The text goes to `shared.interfaces.handle_owner_text(tenant_id, text, message_sid) -> str | None` (the agent's entry point). Until the agent implements it, a fixed "not connected yet" reply is sent. It must answer within a few seconds (Twilio's webhook timeout is 15 s).
3. Outbound messages write `audit_events` `{event: "sms.outbound", detail: {to, body, sent}}`.

## ElevenLabs outbound call
`POST https://api.elevenlabs.io/v1/convai/twilio/outbound-call` (header `xi-api-key`). Confirm the base URL for your account's data residency.

```json
{
  "agent_id": "$ELEVENLABS_AGENT_ID",
  "agent_phone_number_id": "$ELEVENLABS_PHONE_NUMBER_ID",
  "to_number": "$SUPPLIER1_PHONE",
  "conversation_initiation_client_data": {
    "dynamic_variables": {
      "shop_name": "Maple Bakery",
      "supplier_contact": "Dave",
      "item": "all-purpose flour 20kg",
      "quantity": 40,
      "unit": "bag",
      "price_to_confirm": 3.20,
      "po_number": "MB-0001",
      "delivery_day": "Friday"
    }
  }
}
```
Rules: dynamic variable values are string/number/boolean; `system__` names can't be set; passing `conversation_initiation_client_data` means the conversation-initiation webhook is NOT called for this call. The agent prompt references them as `{{item}}` etc. and must say it is an AI assistant ordering for the shop and that the call is transcribed. The variable names above are ours; the agent prompt must use the same names.

## Post-call webhook (`post_call_transcription`)
Return `200` or it is retried. Verify the signature with `ELEVENLABS_WEBHOOK_SECRET`.

Fields we rely on:
- `type` (`post_call_transcription`), `event_timestamp`
- `data.conversation_id` : joins to `calls.conversation_id`
- `data.status`
- `data.transcript[]` : entries with `role` (`agent` or `user`) and `message` (message field name unverified)
- `data.conversation_initiation_client_data.dynamic_variables` : shown in the docs' example payload, so we send `tenant_id` and `order_id` as variables and read them back (still unverified against a real call). The tenant is then checked against our own `calls` record, so a forged id cannot cross tenants.
- Not relied on: `analysis` and `data_collection_results` (names unverified). Outcome is extracted from the transcript by our own structured LLM call.

An order is `confirmed` only if the extracted outcome has a confirmation number.

## Env vars
See `.env.example`: MONGODB_URI, MONGODB_DB, DEEPSEEK_API_KEY, FALLBACK_LLM_API_KEY, VOYAGE_API_KEY (voyage-3.5, 1024 dims), TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID, ELEVENLABS_PHONE_NUMBER_ID, ELEVENLABS_WEBHOOK_SECRET, SUPPLIER1_PHONE (old name SUPPLIER_B_PHONE), SUPPLIER1_EMAIL, SUPPLIER2_EMAIL, CLIENT1_EMAIL, CLIENT2_EMAIL, APP_EMAIL, CLIENT1_PHONE, CLIENT2_PHONE (team phones; fake +1555 numbers if unset; old names OWNER_PHONE_MAPLE/OWNER_PHONE_HARBOUR still read), SMS_DRY_RUN, IMAP_HOST, IMAP_USER, IMAP_PASSWORD, SESSION_SECRET, PUBLIC_BASE_URL.

## Seed data ids (stable across resets)
Tenants `maple`, `harbour` (a tenant's `_id` is its `tenant_id`). Suppliers `supplier_a`, `supplier_b` (`shared/suppliers.py`; B's phone from `SUPPLIER_B_PHONE`). Flour SKU `FLR-AP-2KG`. Quotes `q_<tenant>_<supplier>_<sku>`, documents `doc_<tenant>_<supplier>_<pdf|email>`, memories `mem_*` (see `data/seed/memories.json`), prior order `ord_seed_maple_prev`. Records inserted without an id get a uuid4 hex string `_id`.

## Email and PDF ingestion (`ingest/`)
- Inbox: `IMAP_HOST` / `IMAP_USER` / `IMAP_PASSWORD`, `INBOX` opened read-only, messages fetched with `BODY.PEEK[]` (never marked read). A UID watermark in `ingest_state` (`_id = "<imap user>/INBOX"`) tracks progress. With no watermark (first run, or after a reset wiped the database) the poller backfills: it reads the last 7 days of mail (`--backfill-days N`; 0 = skip everything already there), oldest first, so recently forwarded catalogues are reloaded after a reset. Only mail from an owner's address is ingested.
- Trigger: `python -m ingest.poller` (loop, 30 to 60 s), `--once`, or `GET|POST /ingest/poll` with `Authorization: Bearer $CRON_SECRET` (on Vercel the route refuses to run without `CRON_SECRET`).
- Routing: the forwarder's `From` address, lowercased, matched against `tenants.emails` (`OWNER_EMAIL_MAPLE`, `OWNER_EMAIL_HARBOUR`). Unknown sender: nothing stored.
- CSV price lists (`ingest/csv_quotes.py`, `kind: "csv"`): read in PLAIN CODE, no LLM call, `tokens: {}`. Recognised headers (case and spacing ignored): sku (`sku`, `item_code`, `product_code`, ...), price (`list_price_cad`, `price`, `unit_price`, ...), unit (`unit`, `uom`), valid until (`valid_until`, `expires`, ...), supplier (`supplier`, `vendor`). Comma, semicolon or tab delimited. Dates must be `YYYY-MM-DD` (03/04/2027 is ambiguous and is rejected, not guessed). Supplier per row from the supplier column, else from the filename or text. Quote `source = {"document_id", "page": null, "row": <CSV line number>, "kind": "csv"}`. A CSV whose columns are not recognised falls back to the one LLM call. Arrives as an email attachment or via `python -m ingest.ingest_file <file>.csv --tenant <id>`.
- Storage: every email and every PDF or CSV attachment is a `documents` record with `sha256` of the raw bytes; `(tenant_id, sha256)` is unique, so a document is parsed once per tenant. Attachments have `parent_id` = the email. `status`: `parsed`, `no_text` (scanned PDF: owner texted, nothing guessed), `unknown_supplier` (owner texted), `failed`.
- Extraction: one DeepSeek call per document (`json_mode`), validated by `ingest.llm.Extraction`. Code then picks the supplier (names and domains in `shared/suppliers.json`), drops lines with no validity date or an expired one, and keeps `page` only if that page exists. An email body with no price-like text gets no LLM call.
- Quotes: `source = {"document_id", "page" (PDF) or null (email), "kind"}`. A new quote for the same supplier and SKU sets older active ones to `status: "superseded"`, `superseded_by: <new id>` (history kept).
- Interfaces: `ingest_email`, `extract_quotes_from_pdf`, `extract_quotes_from_email_text`, `extract_quotes_from_csv` in `shared/interfaces.py`.
- Supplier matching uses `shared/suppliers.json` `names`, `domains` and the supplier's email: supplier_a = Valley Foods (email ordering), supplier_b = Gianella's Ingredients (phone ordering, PDF catalogue). Sample catalogues: `data/catalogues/gianellas_ingredients_catalogue.pdf` and `data/catalogues/valley_foods_catalogue.csv`.
