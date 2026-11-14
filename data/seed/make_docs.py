"""Generate the two synthetic supplier documents from data/seed/catalogue.json:
  data/generated/supplier_b_catalogue.pdf  (text PDF, reportlab)
  data/generated/supplier_a_quote.eml      (plain-text email)
Dates are relative to `issued`, so a reset always produces currently valid documents."""
import json
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

SEED = Path(__file__).resolve().parent
OUT = SEED.parent / "generated"


def load() -> dict:
    return json.loads((SEED / "catalogue.json").read_text(encoding="utf-8"))


def make_pdf(cat: dict, issued: date, path: Path) -> None:
    valid = issued + timedelta(days=cat["valid_days"])
    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)  # invariant: same bytes for same input
    c.setTitle("Birchway Wholesale price list (synthetic)")
    y = 740
    for line, size in [("Birchway Wholesale - Supplier B (SYNTHETIC DEMO DATA)", 14),
                       (f"List price catalogue. Issued {issued.isoformat()}. Valid until {valid.isoformat()}.", 10),
                       ("Prices in CAD per unit, before any account discount. Phone orders welcome.", 10)]:
        c.setFont("Helvetica-Bold" if size > 10 else "Helvetica", size)
        c.drawString(72, y, line)
        y -= 22
    y -= 10
    c.setFont("Helvetica-Bold", 10)
    for x, h in ((72, "SKU"), (180, "Description"), (400, "Unit"), (470, "List price")):
        c.drawString(x, y, h)
    c.setFont("Helvetica", 10)
    for sku, price in cat["supplier_b"].items():
        y -= 18
        meta = cat["skus"][sku]
        for x, v in ((72, sku), (180, meta["name"]), (400, meta["unit"]), (470, f"${price:.2f}")):
            c.drawString(x, y, v)
    c.showPage()
    c.save()


def make_eml(cat: dict, issued: date, path: Path) -> None:
    valid = issued + timedelta(days=cat["valid_days"])
    rows = "\n".join(f"  {sku:<12} {cat['skus'][sku]['name']:<30} ${p:.2f} per {cat['skus'][sku]['unit']}"
                     for sku, p in cat["supplier_a"].items())
    m = EmailMessage()
    m["From"] = "Alder Mill Supply <quotes@alder-mill.example>"
    m["To"] = "orders-inbox@orderdesk.example"
    m["Subject"] = "Updated price quote (synthetic demo data)"
    m["Date"] = format_datetime(datetime.combine(issued, time(9, 0), tzinfo=timezone.utc))
    m["Message-ID"] = f"<quote-{issued.isoformat()}@alder-mill.example>"
    m.set_content(f"Hi,\n\nHere are our current prices (CAD), valid until {valid.isoformat()}:\n\n{rows}\n\n"
                  "Order by replying to this email.\n\nAlder Mill Supply - Supplier A\n"
                  "(This is synthetic data for a hackathon demo.)\n")
    path.write_bytes(bytes(m))


def generate(issued: date | None = None) -> dict[str, Path]:
    issued = issued or date.today()
    cat = load()
    OUT.mkdir(exist_ok=True)
    paths = {"pdf": OUT / "supplier_b_catalogue.pdf", "eml": OUT / "supplier_a_quote.eml"}
    make_pdf(cat, issued, paths["pdf"])
    make_eml(cat, issued, paths["eml"])
    return paths


if __name__ == "__main__":
    for kind, p in generate().items():
        print(kind, p, p.stat().st_size, "bytes")
