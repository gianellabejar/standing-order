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

The seed also writes one prior Maple order (`ord_seed_maple_prev`, 40 bags of flour from B) so that "same as last time" has something to resolve. It is seeded, not a real past order.

Flour numbers: A $3.45, B list $3.48. With the seeded "8% off" memory B costs $3.20 and wins. Without it, A wins.

Reset: `make reset`, or without make: `python -m scripts.seed.seed --reset`.
