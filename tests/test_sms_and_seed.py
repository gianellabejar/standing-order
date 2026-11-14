"""SMS webhook + seed. Needs MONGODB_URI (any Atlas or atlas-local); uses a fresh orderdesk_test_<random> database per run."""
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
if not os.environ.get("MONGODB_URI"):
    pytest.skip("MONGODB_URI not set", allow_module_level=True)
import uuid  # noqa: E402

# A fresh database per run: teammates share the sandbox, so a fixed name lets parallel runs collide.
os.environ["MONGODB_DB"] = f"orderdesk_test_{uuid.uuid4().hex[:8]}"

from fastapi.testclient import TestClient  # noqa: E402

from ingest.app import app  # noqa: E402  (import first: it runs load_dotenv, the fixture then overrides)
from scripts import sms_sim  # noqa: E402
from scripts.seed import seed as seedmod  # noqa: E402
from shared import db as dbm  # noqa: E402

MAPLE_PHONE = "+15550100001"


@pytest.fixture(scope="module")
def db():
    for k in ("OWNER_PHONE_MAPLE", "OWNER_PHONE_HARBOUR", "CLIENT1_PHONE", "CLIENT2_PHONE", "CLIENT1_EMAIL", "CLIENT2_EMAIL", "SUPPLIER1_PHONE", "SUPPLIER1_EMAIL", "SUPPLIER2_EMAIL", "TWILIO_AUTH_TOKEN", "PUBLIC_BASE_URL",
              "DASHBOARD_SEED_PASSWORD"):
        os.environ.pop(k, None)
    dbm._db.cache_clear()
    d = dbm._db()
    t0 = time.perf_counter()
    dbm.drop_app_database(d)
    dbm.ensure_indexes(d)
    counts = seedmod.seed(d)
    d.seed_seconds = time.perf_counter() - t0
    d.seed_counts = counts
    yield d
    d.client.drop_database(d.name)


@pytest.fixture
def client(db):
    return TestClient(app)


def test_reset_is_fast_and_complete(db):
    assert db.seed_seconds < 10, db.seed_seconds
    assert db.seed_counts == {"tenants": 2, "documents": 4, "price_quotes": 12, "memories": 7, "orders": 2,
                              "calls": 2, "audit_events": 2, "users": 0}
    assert all(m.seeded for m in dbm.Memories(db).list("maple"))


def test_flour_numbers_make_the_memory_matter(db):
    q = {x.supplier_id: x.price for x in dbm.PriceQuotes(db).list_valid("maple", sku="FLR-AP-2KG")}
    disc = dbm.Memories(db).get("maple", "mem_maple_b_discount").structured["value"]
    assert q == {"supplier_a": 3.45, "supplier_b": 3.48}
    assert round(q["supplier_b"] * (1 - disc / 100), 2) == 3.20 < q["supplier_a"]  # with memory: B
    assert q["supplier_a"] < q["supplier_b"]                                      # without: A


def test_harbour_sees_no_maple_memories(db):
    texts = {m.text for m in dbm.Memories(db).list("harbour")}
    assert texts and not any("Maple" in t or "8%" in t for t in texts)
    assert dbm.Memories(db).get("harbour", "mem_maple_b_discount") is None


def test_known_sender_gets_twiml_reply(client, db):
    r = client.post("/sms/inbound", data=sms_sim.payload(MAPLE_PHONE, "Need 40 bags of flour"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/xml")
    assert "<Message>" in r.text


def test_unknown_sender_gets_empty_response_and_writes_nothing(client, db):
    before = db.audit_events.count_documents({})
    r = client.post("/sms/inbound", data=sms_sim.payload("+15559999999", "hello"))
    assert r.status_code == 200 and "<Message>" not in r.text
    assert db.audit_events.count_documents({}) == before


def test_twilio_retry_is_deduped(client, db):
    p = sms_sim.payload(MAPLE_PHONE, "YES")
    assert "<Message>" in client.post("/sms/inbound", data=p).text
    assert "<Message>" not in client.post("/sms/inbound", data=p).text
    assert db.audit_events.count_documents({"_id": f"sms:{p['MessageSid']}"}) == 1


def test_signature_enforced_when_token_set(client, db, monkeypatch):
    from twilio.request_validator import RequestValidator
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    p = sms_sim.payload(MAPLE_PHONE, "hi")
    assert client.post("/sms/inbound", data=p).status_code == 403
    sig = RequestValidator("test-token").compute_signature("http://testserver/sms/inbound", p)
    assert client.post("/sms/inbound", data=p, headers={"X-Twilio-Signature": sig}).status_code == 200
