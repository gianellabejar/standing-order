"""Ingest a local .eml or .pdf through the same pipeline as the poller (no inbox needed).

  python -m ingest.ingest_file data/generated/supplier_b_update.pdf --tenant maple
  python -m ingest.ingest_file data/generated/supplier_a_quote.eml --tenant maple
  python -m ingest.ingest_file some.pdf --tenant maple --recorded tests/fixtures/llm_pdf_update.json  # no LLM call
"""
import argparse
import json
import logging
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", type=Path)
    ap.add_argument("--tenant", required=True, help="tenant id, e.g. maple")
    ap.add_argument("--recorded", type=Path, help="replay a recorded LLM response instead of calling DeepSeek")
    a = ap.parse_args()

    from ingest import email_ingest, llm, quotes
    if a.recorded:
        llm.EXTRACTOR = llm.recorded(json.loads(a.recorded.read_text(encoding="utf-8")))
    raw = a.path.read_bytes()
    ref = f"file:{a.path.name}"
    if a.path.suffix.lower() == ".pdf":
        r = quotes.ingest_pdf(a.tenant, raw, filename=a.path.name, ref=ref)
    else:
        r = email_ingest.ingest_email(a.tenant, raw, ref=ref)

    for res in [r, *r.children]:
        d = res.document
        print(f"document {d.id} kind={d.kind} status={d.status}"
              f"{' (already parsed earlier; no LLM call)' if res.duplicate else ''}"
              f" supplier={d.supplier_id} tokens={d.tokens} reason={d.status_reason}")
        for q in res.quotes:
            print(f"  quote {q.id} {q.supplier_id} {q.sku} ${q.price:.2f}/{q.unit} valid_until={q.valid_until.date()}"
                  f" source={q.source}")
        for rej in res.rejected:
            print(f"  rejected {rej}")


if __name__ == "__main__":
    main()
