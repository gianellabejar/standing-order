# Demo data: all synthetic

Everything under `data/` is made up for the hackathon demo. The businesses, suppliers, prices, SKUs, email addresses (`.example` domains) and phone numbers (`+1555…`) are not real. Real team phones are set only through `.env` and never committed.

| File | What |
|---|---|
| `seed/tenants.json` | Maple Bakery (main) and Harbour Cafe (for the leak demo) |
| `../shared/suppliers.json` | Supplier A (quotes by email) and Supplier B (PDF catalogue, phone orders), with the names and email domains code uses to recognise them. It lives in `shared/` because the app reads it at runtime and `data/` is not deployed |
| `seed/catalogue.json` | The one source for both supplier documents' prices |
| `seed/memories.json` | **Seeded memories.** Each is stored with `seeded: true` and must be disclosed in the demo |
| `seed/owner_texts.json` | 8 prepared owner texts for the simulator (`python scripts/sms_sim.py --list`) |
| `seed/make_docs.py` | Writes, dated today: `generated/supplier_b_catalogue.pdf` (text PDF, seeded), `generated/supplier_a_quote.eml` (seeded), `generated/supplier_b_update.pdf` (new sugar and butter prices, NOT seeded: forward it live in the demo) and `generated/supplier_b_scanned.pdf` (no text layer, to show the agent refuses to guess) |

The seed also writes three Maple orders (`seeded: true`), produced by the real decision and ledger code with a **mock** voice provider (no real call is placed; the transcript is the mock fixture). See `scripts/seed/demo_orders.py`:

| Order | What happened |
|---|---|
| `ord_seed_maple_prev`, MB-0001 | 40 bags of flour, Supplier B at $3.20 (8% memory). Mock call confirmed, number MB-48213 from the transcript. "Same as last time" resolves to it |
| `ord_seed_maple_oats`, MB-0002 | 10 bags of oats, Supplier B. Mock call ended without a number, so Needs you |
| `ord_seed_maple_sugar` | 20 bags of sugar, Supplier B. Planned, waiting for the owner's YES; no call, no PO yet |

Dashboard logins: the two owner emails, with the password in `DASHBOARD_SEED_PASSWORD`.

Flour numbers: A $3.45, B list $3.48. With the seeded "8% off" memory B costs $3.20 and wins. Without it, A wins.

Reset: `make reset`, or without make: `python -m scripts.seed.seed --reset`.
