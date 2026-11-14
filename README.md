# Order Desk Agent (working title)

Purchasing agent for small shops with memory the owner can read, correct, expire and forget. See CLAUDE.md.

## Voice provider (read this before the demo)
`VOICE_PROVIDER` selects how supplier calls are placed.
- `mock` (default): **simulated, not a real call.** It posts a canned post-call payload to our own
  `/voice/post-call` after a short delay. Scenarios via `MOCK_VOICE_SCENARIO`: `confirmed`,
  `ended_without_number`, `supplier_asks_question`, `no_answer`. The fixtures are unverified against a real call.
- `elevenlabs`: not implemented yet (needs an API key). Selecting it fails with `NotImplementedError`.

Every call record stores `provider`, and logs say `provider=mock` or `provider=elevenlabs`.
An order is `confirmed` only when a confirmation number appears in the supplier's side of the transcript.
