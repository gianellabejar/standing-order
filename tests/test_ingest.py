"""Email + PDF ingestion. In-memory Mongo (mongomock) runs the real repo code. The LLM is replaced by
responses recorded from real DeepSeek runs (tests/fixtures/llm_*.json), so these tests are
deterministic and cost no tokens. Set RUN_LLM_TESTS=1 to also run the live extraction test."""
import json
import os
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import mongomock
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.seed import make_docs  # noqa: E402
from ingest import csv_quotes, email_ingest, llm, poller, quotes  # noqa: E402
from shared import db as dbm  # noqa: E402
from shared.models import Tenant  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
ISSUED = datetime(2026, 11, 14).date()            # the date the fixtures were recorded against
NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
VALID_UNTIL = datetime(2026, 12, 14, 23, 59, 59, tzinfo=timezone.utc)
MAPLE_EMAIL, HARBOUR_EMAIL = "owner@maple-bakery.example", "owner@harbour-cafe.example"


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))["response"]


class CountingExtractor:
    """Replays recorded responses keyed by which document is being read; counts LLM calls."""

    def __init__(self, by_marker: dict[str, str]):
        self.by_marker, self.calls = by_marker, 0

    def __call__(self, text: str, kind: str):
        self.calls += 1
        for marker, name in self.by_marker.items():
            if marker in text:
                return llm.recorded(fixture(name), {"prompt": 400, "completion": 300})(text, kind)
        raise AssertionError("no recorded response for this document")


@pytest.fixture
def docs():
    return {k: p.read_bytes() for k, p in make_docs.generate(ISSUED).items()}


@pytest.fixture
def db(monkeypatch):
    d = mongomock.MongoClient(tz_aware=True)["t"]
    d.documents.create_index([("tenant_id", 1), ("sha256", 1)], unique=True)
    dbm.Tenants(d).insert("maple", Tenant(name="Maple Bakery", phones=["+15550100001"], emails=[MAPLE_EMAIL]))
    dbm.Tenants(d).insert("harbour", Tenant(name="Harbour Cafe", phones=["+15550100002"], emails=[HARBOUR_EMAIL]))
    monkeypatch.setattr(quotes, "now", lambda: NOW)
    return d


@pytest.fixture
def texts(monkeypatch):
    sent = []
    monkeypatch.setattr(quotes, "notify_owner", lambda tenant_id, body, db=None: sent.append((tenant_id, body)))
    return sent


@pytest.fixture
def extractor(monkeypatch):
    ex = CountingExtractor({"UPDATED prices": "llm_pdf_update", "List price catalogue": "llm_pdf_catalogue",
                            "Alder Mill": "llm_email_quote", "Valley Foods": "llm_email_quote"})
    monkeypatch.setattr(llm, "EXTRACTOR", ex)
    return ex


def quote_rows(qs):
    return sorted((q.supplier_id, q.sku, q.unit, q.price) for q in qs)


def test_pdf_catalogue_quotes_validity_and_provenance(db, docs, extractor, texts):
    r = quotes.ingest_pdf("maple", docs["pdf"], filename="supplier_b_catalogue.pdf", db=db)
    assert quote_rows(r.quotes) == [("supplier_b", "FLR-AP-2KG", "bag", 3.48), ("supplier_b", "OAT-RL-1KG", "bag", 3.10),
                                    ("supplier_b", "SUG-GR-2KG", "bag", 2.55)]
    assert all(q.valid_until == VALID_UNTIL for q in r.quotes)
    assert all(q.source == {"document_id": r.document.id, "page": 1, "kind": "pdf"} for q in r.quotes)
    stored = dbm.PriceQuotes(db).list_valid("maple", sku="FLR-AP-2KG")
    assert [(q.price, q.source["document_id"]) for q in stored] == [(3.48, r.document.id)]
    d = r.document
    assert (d.status, d.supplier_id, d.page_count, d.sha256) == ("parsed", "supplier_b", 1, quotes.sha256(docs["pdf"]))
    assert d.tokens == {"prompt": 400, "completion": 300} and sorted(d.quote_ids) == sorted(q.id for q in r.quotes)
    assert extractor.calls == 1 and texts == []


def test_email_body_quotes(db, docs, extractor):
    r = email_ingest.ingest_email("maple", docs["eml"], db=db)
    assert quote_rows(r.quotes) == [("supplier_a", "FLR-AP-2KG", "bag", 3.45), ("supplier_a", "OAT-RL-1KG", "bag", 2.95),
                                    ("supplier_a", "SUG-GR-2KG", "bag", 2.60)]
    assert all(q.valid_until == VALID_UNTIL for q in r.quotes)
    assert all(q.source == {"document_id": r.document.id, "page": None, "kind": "email"} for q in r.quotes)
    assert r.document.kind == "email" and r.document.message_id == "<quote-2026-11-14@alder-mill.example>"


def test_each_document_is_parsed_once(db, docs, extractor):
    first = quotes.ingest_pdf("maple", docs["pdf"], db=db)
    again = quotes.ingest_pdf("maple", docs["pdf"], db=db)
    assert again.duplicate and again.document.id == first.document.id and again.quotes == []
    assert extractor.calls == 1
    assert db.documents.count_documents({"tenant_id": "maple"}) == 1
    assert db.price_quotes.count_documents({"tenant_id": "maple"}) == 3


def test_forwarded_email_routes_by_owner_and_parses_attachment(db, docs, extractor):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = f"Maple Owner <{MAPLE_EMAIL.upper()}>", "inbox@example.com", "Fwd: new prices"
    m.set_content("---------- Forwarded message ---------\nFrom: Gianella's Ingredients <orders@gianellas-ingredients.example>\n"
                  "Subject: updated list\n\nSee attached.")
    m.add_attachment(docs["pdf_update"], maintype="application", subtype="pdf", filename="valley_foods_update.pdf")
    raw = bytes(m)

    assert email_ingest.route_tenant(email_ingest.parse(raw), db).id == "maple"  # case-insensitive
    r = email_ingest.ingest_email("maple", raw, ref="imap:1:5", db=db)
    assert extractor.calls == 1                   # body has no prices: no LLM call for it
    assert r.document.status_reason == "no prices in the body; no LLM call"
    [pdf] = r.children
    assert pdf.document.parent_id == r.document.id and pdf.document.ref == "imap:1:5#valley_foods_update.pdf"
    assert quote_rows(pdf.quotes) == [("supplier_b", "BUT-UN-454G", "block", 5.95), ("supplier_b", "SUG-GR-2KG", "bag", 2.49)]
    assert all(q.source["document_id"] == pdf.document.id and q.source["page"] == 1 for q in pdf.quotes)


def test_routing_harbour_and_unknown_sender(db):
    def msg(sender):
        m = EmailMessage()
        m["From"] = sender
        m.set_content("hi")
        return email_ingest.parse(bytes(m))
    assert email_ingest.route_tenant(msg(HARBOUR_EMAIL), db).id == "harbour"
    assert email_ingest.route_tenant(msg("stranger@example.com"), db) is None


def test_newer_quote_supersedes_older_and_keeps_history(db, docs, extractor):
    old = quotes.ingest_pdf("maple", docs["pdf"], db=db)
    new = quotes.ingest_pdf("maple", docs["pdf_update"], db=db)
    old_sugar = next(q for q in old.quotes if q.sku == "SUG-GR-2KG")
    new_sugar = next(q for q in new.quotes if q.sku == "SUG-GR-2KG")
    assert [q.price for q in dbm.PriceQuotes(db).list_valid("maple", sku="SUG-GR-2KG")] == [2.49]
    hist = dbm.PriceQuotes(db).get("maple", old_sugar.id)
    assert (hist.status, hist.superseded_by, hist.price) == ("superseded", new_sugar.id, 2.55)
    assert [q.price for q in dbm.PriceQuotes(db).list_valid("maple", sku="FLR-AP-2KG")] == [3.48]  # untouched


def test_scanned_pdf_texts_owner_instead_of_guessing(db, docs, extractor, texts):
    r = quotes.ingest_pdf("maple", docs["pdf_scanned"], filename="scan.pdf", db=db)
    assert r.document.status == "no_text" and r.quotes == []
    assert extractor.calls == 0
    assert db.price_quotes.count_documents({}) == 0
    assert texts and texts[0][0] == "maple" and "scan.pdf" in texts[0][1] and "didn't guess" in texts[0][1]


def test_code_rejects_lines_without_date_and_fixes_bad_page(db, docs, monkeypatch):
    resp = fixture("llm_pdf_catalogue")
    resp["quotes"][0]["page"] = 9                 # page that does not exist
    resp["quotes"][1]["valid_until"] = None       # no validity date
    monkeypatch.setattr(llm, "EXTRACTOR", llm.recorded(resp))
    r = quotes.ingest_pdf("maple", docs["pdf"], db=db)
    assert [q.sku for q in r.quotes] == ["FLR-AP-2KG", "SUG-GR-2KG"]
    assert r.quotes[0].source["page"] is None
    assert r.rejected == [{"sku": "OAT-RL-1KG", "reason": "no validity date in the document"}]
    assert "OAT-RL-1KG" in r.document.status_reason


def test_invalid_llm_output_fails_the_document(db, docs, monkeypatch):
    resp = fixture("llm_pdf_catalogue")
    resp["quotes"][0]["price"] = -1               # fails Pydantic (price > 0)
    monkeypatch.setattr(llm, "EXTRACTOR", llm.recorded(resp))
    with pytest.raises(Exception):
        quotes.ingest_pdf("maple", docs["pdf"], db=db)
    [d] = dbm.Documents(db).list("maple")
    assert d.status == "failed" and db.price_quotes.count_documents({}) == 0


def test_unknown_supplier_saves_nothing_and_asks(db, monkeypatch, texts):
    monkeypatch.setattr(llm, "EXTRACTOR", llm.recorded({"supplier_name": "Mystery Co", "quotes": [
        {"sku": "X-1", "unit": "bag", "price": 1.0, "valid_until": "2026-12-01"}]}))
    m = EmailMessage()
    m["From"] = MAPLE_EMAIL
    m.set_content("Prices: X-1 $1.00 per bag, valid until 2026-12-01")
    r = email_ingest.ingest_email("maple", bytes(m), db=db)
    assert r.document.status == "unknown_supplier" and r.quotes == []
    assert db.price_quotes.count_documents({}) == 0 and "which supplier" in texts[0][1].lower()


def test_tenants_do_not_share_documents_or_quotes(db, docs, extractor):
    quotes.ingest_pdf("maple", docs["pdf"], db=db)
    assert dbm.PriceQuotes(db).list_valid("harbour") == []
    assert dbm.Documents(db).list("harbour") == []
    h = quotes.ingest_pdf("harbour", docs["pdf"], db=db)  # same bytes, other tenant: its own copy
    assert not h.duplicate and len(h.quotes) == 3 and extractor.calls == 2


class FakeImap:
    def __init__(self, messages: dict[int, bytes], uidvalidity: int = 7):
        self.messages, self.untagged_responses = messages, {"UIDVALIDITY": [str(uidvalidity).encode()]}
        self.readonly, self.fetch_specs = None, []

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            crit = args[1]
            lo = int(crit.split()[1].split(":")[0]) if crit.startswith("UID") else 1
            hits = [u for u in self.messages if u >= lo] or ([max(self.messages)] if self.messages else [])
            return "OK", [" ".join(map(str, hits)).encode()]
        uid, spec = int(args[0]), args[1]
        self.fetch_specs.append(spec)
        return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[uid]), b")"]

    def logout(self):
        pass


def test_poller_skips_existing_mail_then_ingests_new(db, docs, extractor, monkeypatch):
    monkeypatch.setenv("IMAP_USER", "inbox@example.com")
    def fwd(sender, attachment):
        m = EmailMessage()
        m["From"], m["Subject"] = sender, "Fwd: prices"
        m.set_content("From: orders@gianellas-ingredients.example\nsee attached")
        m.add_attachment(attachment, maintype="application", subtype="pdf", filename="b.pdf")
        return bytes(m)
    imap = FakeImap({1: b"From: old@example.com\n\nold mail", 2: b"From: old@example.com\n\nold mail 2"})

    first = poller.poll_once(db, connect_fn=lambda: imap)
    assert first == {"initialized": True, "skipped_existing": 2, "processed": []}
    assert extractor.calls == 0

    imap.messages[3] = fwd(MAPLE_EMAIL, docs["pdf"])
    imap.messages[4] = fwd("stranger@example.com", docs["pdf_update"])
    second = poller.poll_once(db, connect_fn=lambda: imap)
    by_uid = {p["uid"]: p for p in second["processed"]}
    assert set(by_uid) == {3, 4}
    assert by_uid[3]["result"] == "ingested" and by_uid[3]["tenant"] == "maple" and by_uid[3]["quotes"] == 3
    assert by_uid[4]["result"] == "unknown_sender"
    assert imap.readonly is True and set(imap.fetch_specs) == {"(BODY.PEEK[])"}  # never marks mail read

    third = poller.poll_once(db, connect_fn=lambda: imap)  # nothing new
    assert third["processed"] == [] and extractor.calls == 1
    assert dbm.IngestState(db).get("inbox@example.com/INBOX")["last_uid"] == 4


def test_poll_route_requires_secret(monkeypatch):
    from fastapi.testclient import TestClient
    from ingest import routes
    from ingest.app import app
    monkeypatch.setattr(routes.poller, "poll_once", lambda: {"ok": True})
    c = TestClient(app)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    assert c.post("/ingest/poll").status_code == 403
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    assert c.get("/ingest/poll", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/ingest/poll", headers={"Authorization": "Bearer s3cret"}).json() == {"ok": True}


@pytest.mark.skipif(os.environ.get("RUN_LLM_TESTS") != "1", reason="live DeepSeek test; set RUN_LLM_TESTS=1")
def test_live_deepseek_extraction(db, docs, monkeypatch):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    monkeypatch.setattr(llm, "EXTRACTOR", llm.deepseek_extract)
    r = quotes.ingest_pdf("maple", docs["pdf"], db=db)
    assert quote_rows(r.quotes) == [("supplier_b", "FLR-AP-2KG", "bag", 3.48), ("supplier_b", "OAT-RL-1KG", "bag", 3.10),
                                    ("supplier_b", "SUG-GR-2KG", "bag", 2.55)]
    assert all(q.source["page"] == 1 for q in r.quotes) and r.document.tokens["prompt"] > 0


def test_poller_output_is_readable():
    assert poller.describe({"initialized": True, "skipped_existing": 3, "processed": []})[0].startswith(
        "started fresh: skipped 3")
    assert poller.describe({"initialized": False, "processed": []}) == ["no new mail"]
    lines = poller.describe({"initialized": False, "processed": [
        {"uid": 5, "tenant": "maple", "result": "ingested", "quotes": 2, "documents": ["d1", "d2"],
         "statuses": ["parsed", "parsed"]},
        {"uid": 6, "result": "unknown_sender"}]})
    assert lines == ["email #5 -> maple: 2 quote(s); documents d1 (parsed), d2 (parsed)", "email #6: unknown_sender"]


def test_sync_contacts_updates_in_place(db):
    assert dbm.Tenants(db).set_contacts("harbour", ["+15550109999"], [" New@Example.COM "])
    t = dbm.Tenants(db).get("harbour")
    assert t.phones == ["+15550109999"] and t.emails == ["new@example.com"]
    assert dbm.Tenants(db).resolve_by_phone("+15550109999").id == "harbour"
    assert dbm.Tenants(db).get("maple").phones == ["+15550100001"]  # other tenant untouched


# --- CSV price lists (plain code, no LLM) -------------------------------------------------------------------

VALLEY_CSV = (ROOT / "data" / "catalogues" / "valley_foods_catalogue.csv").read_bytes()


def test_valley_foods_csv_is_read_without_an_llm_call(db, extractor, texts):
    r = csv_quotes.ingest_csv("maple", VALLEY_CSV, filename="valley_foods_catalogue.csv", db=db)
    assert extractor.calls == 0 and r.document.tokens == {}                      # no tokens spent
    assert r.document.kind == "csv" and r.document.status == "parsed" and r.document.supplier_id == "supplier_a"
    assert quote_rows(r.quotes) == [("supplier_a", "BUT-UN-454G", "block", 6.10), ("supplier_a", "CRM-35-1L", "carton", 4.79),
                                    ("supplier_a", "EGG-LG-30", "tray", 8.95), ("supplier_a", "FLR-AP-2KG", "bag", 3.45),
                                    ("supplier_a", "MLK-2P-4L", "bag", 6.29), ("supplier_a", "YST-ID-500G", "pack", 6.49)]
    assert all(q.valid_until == VALID_UNTIL for q in r.quotes)
    flour = next(q for q in r.quotes if q.sku == "FLR-AP-2KG")
    assert flour.source == {"document_id": r.document.id, "page": None, "row": 2, "kind": "csv"}  # line 2 of the file
    again = csv_quotes.ingest_csv("maple", VALLEY_CSV, db=db)
    assert again.duplicate and db.price_quotes.count_documents({"tenant_id": "maple"}) == 6   # parsed once
    assert texts == []


def test_csv_rows_are_validated_in_code(db, extractor):
    raw = ("Supplier;SKU;Unit;Price;Valid Until\n"            # semicolons and other header spellings
           "Valley Foods;FLR-AP-2KG;bag;$3.40;2026-12-01\n"
           "Valley Foods;EGG-LG-30;tray;;2026-12-01\n"        # no price
           "Valley Foods;MLK-2P-4L;bag;6.10;\n"               # no date
           "Valley Foods;CRM-35-1L;carton;4.50;03/04/2027\n"  # ambiguous date: not guessed
           "Valley Foods;YST-ID-500G;pack;6.00;2026-11-01\n"  # expired
           "Mystery Co;BUT-UN-454G;block;5.00;2026-12-01\n").encode()
    r = csv_quotes.ingest_csv("maple", raw, filename="mixed.csv", db=db)
    assert quote_rows(r.quotes) == [("supplier_a", "FLR-AP-2KG", "bag", 3.40)] and extractor.calls == 0
    reasons = {x["sku"]: x["reason"] for x in r.rejected}
    assert reasons == {"EGG-LG-30": "line 3: price missing or unreadable",
                       "MLK-2P-4L": "line 4: no readable validity date (use YYYY-MM-DD)",
                       "CRM-35-1L": "line 5: no readable validity date (use YYYY-MM-DD)",
                       "YST-ID-500G": "line 6: already expired (2026-11-01)",
                       "BUT-UN-454G": "line 7: supplier not recognised"}


def test_csv_without_a_known_supplier_saves_nothing_and_asks(db, extractor, texts):
    raw = b"sku,unit,price,valid_until\nFLR-AP-2KG,bag,3.10,2026-12-01\n"
    r = csv_quotes.ingest_csv("maple", raw, filename="prices.csv", db=db)
    assert r.document.status == "unknown_supplier" and r.quotes == [] and extractor.calls == 0
    assert db.price_quotes.count_documents({}) == 0 and "which supplier" in texts[0][1].lower()
    named = csv_quotes.ingest_csv("maple", raw + b"\n", filename="valley_foods_prices.csv", db=db)  # name in filename
    assert [q.supplier_id for q in named.quotes] == ["supplier_a"]


def test_unfamiliar_csv_layout_falls_back_to_one_llm_call(db, monkeypatch):
    calls = []

    def fake(text, kind):
        calls.append(kind)
        return llm.recorded({"supplier_name": "Valley Foods", "quotes": [
            {"sku": "FLR-AP-2KG", "unit": "bag", "price": 3.48, "valid_until": "2026-12-14"}]})(text, kind)
    monkeypatch.setattr(llm, "EXTRACTOR", fake)
    raw = b"Article,Packaging,Cost per pack,Good through\nFlour FLR-AP-2KG,bag,3.48,2026-12-14\n"
    r = csv_quotes.ingest_csv("maple", raw, filename="list.csv", db=db)
    assert calls == ["csv"] and quote_rows(r.quotes) == [("supplier_a", "FLR-AP-2KG", "bag", 3.48)]
    assert r.quotes[0].source == {"document_id": r.document.id, "page": None, "kind": "csv"}


def test_forwarded_email_with_csv_attachment(db, extractor):
    m = EmailMessage()
    m["From"], m["Subject"] = MAPLE_EMAIL, "Fwd: Valley Foods price list"
    m.set_content("See attached.")
    m.add_attachment(VALLEY_CSV, maintype="text", subtype="csv", filename="valley_foods_catalogue.csv")
    r = email_ingest.ingest_email("maple", bytes(m), ref="imap:1:9", db=db)
    [c] = r.children
    assert c.document.kind == "csv" and c.document.parent_id == r.document.id and len(c.quotes) == 6
    assert extractor.calls == 0                                                    # body had no prices, CSV is code


def test_csv_quotes_are_tenant_scoped(db, extractor):
    csv_quotes.ingest_csv("maple", VALLEY_CSV, db=db)
    assert dbm.PriceQuotes(db).list_valid("harbour") == [] and dbm.Documents(db).list("harbour") == []
