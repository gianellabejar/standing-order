# Deploying to Vercel

Entrypoint: root `app.py` (zero-config FastAPI). Python is pinned by `.python-version` (3.12; Vercel lists 3.12+, so 3.11 is not available there).
`requirements.txt` is runtime only; `requirements-dev.txt` adds test tools. `.vercelignore` keeps tests, seed data and fixtures out of the deploy.

## Environment variables (Vercel dashboard, never in the repo)
MONGODB_URI, MONGODB_DB, DEEPSEEK_API_KEY, DEEPSEEK_MODEL, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER,
PUBLIC_BASE_URL (the deployed URL, no trailing slash), VOICE_PROVIDER (`mock` until a real call is verified),
ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID, ELEVENLABS_PHONE_NUMBER_ID, ELEVENLABS_WEBHOOK_SECRET, TEST_SUPPLIER_PHONE,
SESSION_SECRET (required: signs dashboard logins), VOYAGE_API_KEY (optional: corrected memories get embeddings). Mock delivery runs inline on Vercel (`VERCEL` is set), so MOCK_VOICE_DELIVERY is not needed.

## After the first deploy
1. Atlas -> Network Access: Vercel has no fixed IPs, so allow 0.0.0.0/0 on the sandbox cluster (synthetic data only).
2. Twilio: inbound SMS webhook -> `<URL>/sms/inbound` (POST). Set PUBLIC_BASE_URL first if the signature check uses it.
3. ElevenLabs: post-call webhook -> `<URL>/voice/post-call`, copy its signing secret into ELEVENLABS_WEBHOOK_SECRET, redeploy.
4. Check `<URL>/healthz` and `<URL>/health` (the latter reports the active voice provider).

## Limits to know
- No background threads or pollers: the IMAP poller needs a cron or an external worker.
- Keep webhooks fast; function time limits and cold starts apply.
