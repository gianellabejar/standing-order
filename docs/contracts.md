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

`X-Twilio-Signature` is validated with the Twilio SDK when `TWILIO_AUTH_TOKEN` is set (the simulator skips it).

Response: `200`, `Content-Type: text/xml`, body `<Response><Message>reply</Message></Response>`. An empty `<Response/>` sends nothing; later messages (approval, outcome) are sent through the Twilio REST API.
Unknown `From` returns an empty `<Response/>` and writes no data.

## ElevenLabs outbound call
`POST https://api.elevenlabs.io/v1/convai/twilio/outbound-call` (header `xi-api-key`). Confirm the base URL for your account's data residency.

```json
{
  "agent_id": "$ELEVENLABS_AGENT_ID",
  "agent_phone_number_id": "$ELEVENLABS_PHONE_NUMBER_ID",
  "to_number": "$SUPPLIER_B_PHONE",
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
- `data.conversation_initiation_client_data.dynamic_variables` : to find the order (we add `order_id`; unverified that it is echoed back, otherwise match on `conversation_id`)
- Not relied on: `analysis` and `data_collection_results` (names unverified). Outcome is extracted from the transcript by our own structured LLM call.

An order is `confirmed` only if the extracted outcome has a confirmation number.

## Env vars
See `.env.example`: MONGODB_URI, MONGODB_DB, DEEPSEEK_API_KEY, FALLBACK_LLM_API_KEY, VOYAGE_API_KEY (voyage-3.5, 1024 dims), TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID, ELEVENLABS_PHONE_NUMBER_ID, ELEVENLABS_WEBHOOK_SECRET, SUPPLIER_B_PHONE, IMAP_HOST, IMAP_USER, IMAP_PASSWORD, SESSION_SECRET, PUBLIC_BASE_URL.
