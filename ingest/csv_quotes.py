"""CSV price lists -> price_quotes, in PLAIN CODE (no LLM call, no tokens) when the columns are recognised.

A CSV is already structured, so reading it with code is cheaper and exact. Only a CSV whose columns we do not
recognise falls back to the one structured LLM call that PDFs use. Same rules as every other document:
stored once by sha256, the supplier is never guessed, lines without a validity date or already expired are
rejected with a reason, and provenance is the document id plus the CSV line number.
"""
import csv
import io
import logging
import re
from datetime import date
from typing import Optional
from uuid import uuid4

from ingest import quotes as q
from shared.models import PriceQuote, now

log = logging.getLogger("ingest.csv")
MAX_ROWS = 1000

# normalised header -> field. Headers are lowercased, spaces/hyphens become "_", other punctuation dropped.
COLUMNS = {
    "sku": ("sku", "item_code", "product_code", "code", "item_no", "item"),
    "price": ("list_price_cad", "list_price", "price_cad", "price", "unit_price", "cost"),
    "unit": ("unit", "uom", "unit_of_measure"),
    "valid_until": ("valid_until", "price_valid_until", "expires", "expiry", "valid_to"),
    "supplier": ("supplier", "supplier_name", "vendor"),
}
REQUIRED = ("sku", "price", "unit")


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", re.sub(r"[\s\-]+", "_", (h or "").strip().lower()))


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def map_columns(headers: list[str]) -> dict[str, str]:
    """field -> the actual header in the file, for the fields we recognise."""
    by_norm = {_norm(h): h for h in headers if h}
    found = {}
    for field, names in COLUMNS.items():
        hit = next((by_norm[n] for n in names if n in by_norm), None)
        if hit:
            found[field] = hit
    return found


def _price(s: str) -> Optional[float]:
    s = re.sub(r"(?i)cad|\$|\s", "", s or "")
    if re.fullmatch(r"\d+,\d{1,2}", s):  # decimal comma ("3,48", common in semicolon CSVs): 3.48, not 348
        s = s.replace(",", ".")
    else:                                # thousands separators ("1,234.50")
        s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _date(s: str) -> Optional[date]:
    """ISO dates only (YYYY-MM-DD). 03/04/2026 is ambiguous, so it is rejected rather than guessed."""
    try:
        return date.fromisoformat((s or "").strip()[:10])
    except ValueError:
        return None


def _reader(text: str) -> csv.DictReader:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return csv.DictReader(io.StringIO(text), dialect=dialect)


def ingest_csv(tenant_id: str, raw: bytes, filename: str = "price_list.csv", supplier_id: Optional[str] = None,
               parent_id: Optional[str] = None, ref: str = "upload", supplier_hint: Optional[str] = None,
               db=None) -> q.IngestResult:
    doc, created = q.store_document(tenant_id, raw, "csv", ref, db=db, filename=filename, parent_id=parent_id)
    if not created:
        return q.IngestResult(doc, duplicate=True)
    text = _decode(raw)
    reader = _reader(text)
    cols = map_columns(reader.fieldnames or [])
    if any(f not in cols for f in REQUIRED):  # unfamiliar layout: the one LLM call, like a PDF
        log.info("doc %s: CSV columns not recognised (%s); using the LLM", doc.id, reader.fieldnames)
        return q.extract_from_text(tenant_id, text, doc.id, "csv",
                                   supplier_id=supplier_id or q.detect_supplier(text) or supplier_hint, db=db)

    readable_name = re.sub(r"[_\-]+", " ", filename or "")  # valley_foods_prices.csv -> "valley foods prices.csv"
    file_supplier = supplier_id or q.detect_supplier(readable_name, text) or supplier_hint
    quotes, rejected, today = [], [], now()
    for row in reader:
        if reader.line_num > MAX_ROWS + 1:
            rejected.append({"sku": "*", "reason": f"file truncated after {MAX_ROWS} rows"})
            break
        sku = (row.get(cols["sku"]) or "").strip().upper()
        if not sku:
            continue  # blank line
        line = reader.line_num
        sup = q.detect_supplier(row.get(cols["supplier"])) if cols.get("supplier") and row.get(cols["supplier"]) \
            else file_supplier
        if sup is None:
            rejected.append({"sku": sku, "reason": f"line {line}: supplier not recognised"})
            continue
        price = _price(row.get(cols["price"]))
        if price is None:
            rejected.append({"sku": sku, "reason": f"line {line}: price missing or unreadable"})
            continue
        raw_date = row.get(cols["valid_until"]) if cols.get("valid_until") else ""
        until = _date(raw_date)
        if until is None:
            rejected.append({"sku": sku, "reason": f"line {line}: no readable validity date (use YYYY-MM-DD)"})
            continue
        valid_until = q._end_of_day(until)
        if valid_until <= today:
            rejected.append({"sku": sku, "reason": f"line {line}: already expired ({until})"})
            continue
        unit = (row.get(cols["unit"]) or "").strip().lower() or "unit"
        quotes.append(PriceQuote(id=f"q_{uuid4().hex[:16]}", tenant_id=tenant_id, supplier_id=sup, sku=sku, unit=unit,
                                 price=round(price, 4), valid_until=valid_until,
                                 source={"document_id": doc.id, "page": None, "row": line, "kind": "csv"}))

    if not quotes and all("supplier not recognised" in r["reason"] for r in rejected) and rejected:
        return q.unknown_supplier(tenant_id, doc.id, filename, "supplier not recognised in any row", {}, db)
    suppliers = {x.supplier_id for x in quotes}
    label = suppliers.pop() if len(suppliers) == 1 else ("mixed" if suppliers else file_supplier)
    return q.save_quotes(tenant_id, doc.id, label, quotes, rejected, {}, db)
