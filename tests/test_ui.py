"""Owner dashboard. In-memory Mongo (mongomock) seeded by the REAL seed code (demo orders included),
then driven through the FastAPI app with a test client: login, the page, the three memory actions,
CSRF and tenant isolation."""
import re
import sys
from pathlib import Path

import mongomock
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from scripts.seed import seed as seedmod  # noqa: E402
from shared import db as dbm  # noqa: E402
from shared.models import now  # noqa: E402

PASSWORD = "correct-horse-battery"
MAPLE, HARBOUR = "owner@maple-bakery.example", "owner@harbour-cafe.example"


@pytest.fixture
def db(monkeypatch):
    for k in ("OWNER_PHONE_MAPLE", "OWNER_PHONE_HARBOUR", "CLIENT1_PHONE", "CLIENT2_PHONE", "CLIENT1_EMAIL", "CLIENT2_EMAIL", "SUPPLIER1_PHONE", "SUPPLIER1_EMAIL", "SUPPLIER2_EMAIL", "OWNER_EMAIL_MAPLE", "OWNER_EMAIL_HARBOUR",
              "PUBLIC_BASE_URL", "VOYAGE_API_KEY", "IMAP_USER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-production")
    monkeypatch.setenv("DASHBOARD_SEED_PASSWORD", PASSWORD)
    d = mongomock.MongoClient(tz_aware=True)["ui_test"]
    d.users.create_index("email", unique=True)
    d.orders.create_index([("tenant_id", 1), ("idempotency_key", 1)], unique=True)
    monkeypatch.setattr(dbm, "_db", lambda: d)
    seedmod.seed(d)
    return d


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client: TestClient, email: str, password: str = PASSWORD):
    token = csrf_of(client.get("/login").text)
    return client.post("/login", data={"email": email, "password": password, "csrf": token}, follow_redirects=False)


@pytest.fixture
def maple(db):
    c = TestClient(app_module.app)
    assert login(c, MAPLE).status_code == 303
    return c


def page(client, **q) -> str:
    r = client.get("/", params=q)
    assert r.status_code == 200, r.text[:300]
    return r.text


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", __import__("html").unescape(re.sub(r"<[^>]+>", " ", html)))  # unescape: "Sean&#39;s"


def test_login_required_and_bad_password(db):
    c = TestClient(app_module.app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert login(c, MAPLE, "wrong-password").status_code == 401
    assert login(c, "nobody@example.com").status_code == 401
    assert c.post("/login", data={"email": MAPLE, "password": PASSWORD}).status_code == 403  # no CSRF token


def test_page_shows_tenant_tiles_and_table(maple, db):
    t = text_of(page(maple))
    assert "Sample data" in t and "Signed in as Sean's Bakery" in t
    assert "Every order, with its reasons." in t
    # tiles from the database: 1 needs you (oats call ended without a number), 1 confirmed, 2 of 2 used memory
    assert re.search(r"Needs you 1 ", t) and re.search(r"Confirmed 1 ", t) and "2 of 2" in t
    for po in ("MB-0001", "MB-0002"):
        assert po in t
    assert "40 bags of flour" in t and "Gianella's Ingredients" in t and "$128.00" in t


def test_confirmation_block_only_with_a_number(maple, db):
    t = text_of(page(maple, order="MB-0001"))
    assert "Order MB-0001" in t and "40 bags of flour from Gianella's Ingredients" in t
    assert "Confirmation number MB-48213 Captured from the call transcript." in t
    assert "“Need 40 bags of flour for Friday.”" in t
    assert "$3.20 per bag" in t and "Friday" in t

    html = page(maple, order="MB-0002")
    t = text_of(html)
    assert "No confirmation number yet" in t and "Call ended without confirmation number." in t
    detail = html[html.index('id="detail"'):]
    assert "Confirmed" not in text_of(detail)  # never shown for an order without a number

    from shared.models import Order
    dbm.Orders(db).insert("maple", Order(id="ord_pending", tenant_id="maple", idempotency_key="sms:SMx",
                                         supplier_id="supplier_b", awaiting_approval=True, total=47.0,
                                         items=[{"sku": "SUG-GR-2KG", "qty": 20, "unit": "bag", "name": "sugar"}]))
    t = text_of(page(maple, order="ord_pending"))  # an agent order waiting for YES: no PO, no call yet
    assert "Order (no PO number yet)" in t and "No confirmation number yet Waiting for your YES" in t
    assert "No call has been placed for this order yet." in t and "Planned" in t


def test_confirmed_status_without_number_is_not_shown_confirmed(maple, db):
    db.orders.update_one({"_id": "ord_seed_maple_prev"}, {"$set": {"confirmation_number": None}})
    t = text_of(page(maple, order="MB-0001"))
    assert "No confirmation number yet" in t and re.search(r"Confirmed 0 ", t)


def test_memory_receipt_lists_each_memory_with_source_flags(maple):
    t = text_of(page(maple, order="MB-0001"))
    assert "Memory receipt" in t
    for mid, text in [("mem_maple_b_discount", "Gianella's Ingredients gives Sean's Bakery 8% off list."),
                      ("mem_maple_b_contact", "When calling Gianella's Ingredients, ask for Gianella."),
                      ("mem_maple_b_channel", "Gianella's Ingredients takes orders by phone."),
                      ("mem_maple_b_po_format", "PO numbers")]:
        assert f"{mid} · Seeded · active" in t and text in t


def test_transcript_is_inline_and_labelled_simulated(maple):
    t = text_of(page(maple, order="MB-0001", transcript="1"))
    assert "Simulated call (mock voice provider)" in t
    assert "AI assistant Hello, this is an AI assistant" in t and "confirmation number is MB-48213" in t


def test_tokens_line_hidden_unless_recorded(maple, db):
    assert "Tokens used" not in page(maple, order="MB-0001")
    db.orders.update_one({"_id": "ord_seed_maple_prev"}, {"$set": {"tokens": {"prompt": 600, "completion": 212}}})
    assert "Tokens used for this order: 812" in text_of(page(maple, order="MB-0001"))


def test_correct_supersedes_and_updates_the_value(maple, db):
    html = page(maple, order="MB-0001", correct="mem_maple_b_discount")
    assert "Save correction" in html and "Value the agent uses (discount pct)" in html
    r = maple.post("/memories/mem_maple_b_discount/correct", follow_redirects=False,
                   data={"text": "Gianella's Ingredients gives Sean's Bakery 10% off list.", "value": "10", "order": "MB-0001",
                         "csrf": csrf_of(html)})
    assert r.status_code == 303 and r.headers["location"] == "/?order=MB-0001&notice=corrected"
    old = dbm.Memories(db).get("maple", "mem_maple_b_discount")
    [new] = dbm.Memories(db).list("maple", supersedes="mem_maple_b_discount")
    assert old.status.value == "superseded" and new.status.value == "active"
    assert new.structured["value"] == 10 and new.source.type == "dashboard"
    assert "mem_maple_b_discount" not in {m.id for m in dbm.Memories(db).list_active("maple")}
    t = text_of(page(maple, order="MB-0001"))
    assert "mem_maple_b_discount · Seeded · superseded" in t and "Corrected to: “Gianella's Ingredients gives Sean's Bakery 10% off list.”" in t
    assert db.audit_events.count_documents({"event": "memory.supersede"}) == 1


def test_expire_stops_retrieval(maple, db):
    r = maple.post("/memories/mem_maple_b_contact/expire", follow_redirects=False,
                   data={"order": "MB-0001", "csrf": csrf_of(page(maple))})
    assert r.status_code == 303
    assert dbm.Memories(db).get("maple", "mem_maple_b_contact").expires_at <= now()
    assert "mem_maple_b_contact" not in {m.id for m in dbm.Memories(db).list_active("maple")}
    assert "mem_maple_b_contact · Seeded · expired" in text_of(page(maple, order="MB-0001"))


def test_forget_asks_then_retracts_with_tombstone(maple, db):
    html = page(maple, order="MB-0001", forget="mem_maple_b_po_format")
    assert "Forget this memory? This removes it and leaves an audit entry." in html
    r = maple.post("/memories/mem_maple_b_po_format/forget", follow_redirects=False,
                   data={"order": "MB-0001", "csrf": csrf_of(html)})
    assert r.status_code == 303
    m = dbm.Memories(db).get("maple", "mem_maple_b_po_format")
    assert m.status.value == "retracted" and m.text == "" and m.structured == {}
    tomb = db.audit_events.find_one({"event": "memory.forget"})
    assert tomb["tenant_id"] == "maple" and tomb["tombstone"] == {"memory_id": "mem_maple_b_po_format"}
    assert "Forgotten. The content was deleted" in text_of(page(maple, order="MB-0001"))


def test_actions_require_csrf(maple, db):
    r = maple.post("/memories/mem_maple_b_contact/expire", data={"order": "MB-0001", "csrf": "forged"})
    assert r.status_code == 403
    assert dbm.Memories(db).get("maple", "mem_maple_b_contact").expires_at > now()


def test_other_tenant_sees_nothing_and_cannot_touch_maple_memory(db):
    h = TestClient(app_module.app)
    assert login(h, HARBOUR).status_code == 303
    html = page(h)
    t = text_of(html)
    assert "Signed in as Cerebral Cafe" in t and "0 of 0" in t and "No orders yet" in t
    assert "Maple" not in t and "mem_maple" not in html and "MB-0001" not in t
    assert "MB-0001" not in text_of(page(h, order="MB-0001"))  # cannot select another tenant's order
    r = h.post("/memories/mem_maple_b_discount/forget", data={"order": "", "csrf": csrf_of(html)})
    assert r.status_code == 404
    assert dbm.Memories(db).get("maple", "mem_maple_b_discount").status.value == "active"


def test_register_creates_an_empty_business(db):
    c = TestClient(app_module.app)
    token = csrf_of(c.get("/register").text)
    r = c.post("/register", follow_redirects=False, data={"business": "Corner Deli", "email": "Deli@Example.com",
                                                          "phone": "+15550107777", "password": "longenough1",
                                                          "csrf": token})
    assert r.status_code == 303
    t = text_of(page(c))
    assert "Signed in as Corner Deli" in t and "Sample data" not in t and "0 of 0" in t and "+15550107777" in t
    user = dbm.Users(db).resolve_by_email("deli@example.com")
    assert user and user.password_hash.startswith("$2") and "longenough1" not in user.password_hash
    assert dbm.Tenants(db).resolve_by_phone("+15550107777").id == user.tenant_id
    c2 = TestClient(app_module.app)
    dup = c2.post("/register", data={"business": "Other", "email": "deli@example.com", "password": "longenough1",
                                      "csrf": csrf_of(c2.get("/register").text)})
    assert dup.status_code == 400 and "already exists" in dup.text


def test_tampered_or_forged_session_cookie_is_rejected(maple):
    from itsdangerous import URLSafeTimedSerializer
    good = maple.cookies.get("od_session")
    forged = URLSafeTimedSerializer("guessed-secret", salt="orderdesk-session").dumps(
        {"uid": "user_maple", "tid": "maple"})
    for bad in (good[:-3] + "abc", forged):
        c = TestClient(app_module.app, cookies={"od_session": bad})  # only the bad cookie is sent
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_logout_ends_the_session(maple):
    r = maple.post("/logout", data={"csrf": csrf_of(page(maple))}, follow_redirects=False)
    assert r.status_code == 303 and maple.get("/", follow_redirects=False).headers["location"] == "/login"


def test_cookies_secure_only_over_https(db, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.vercel.app")  # must not force Secure on localhost
    c = TestClient(app_module.app)
    assert login(c, MAPLE).status_code == 303 and page(c)  # login works over plain http
    r = TestClient(app_module.app).get("/login", headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()
    r = TestClient(app_module.app).get("/login")
    assert "secure" not in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()


def test_channel_for_orders_without_the_field():
    from shared.models import Order
    from ui import views
    base = dict(tenant_id="maple", idempotency_key="k")
    assert views.channel_label(Order(**base, supplier_id="supplier_b")) == "Call"    # B takes phone orders
    assert views.channel_label(Order(**base, supplier_id="supplier_a")) == "Email"
    assert views.channel_label(Order(**base, supplier_id="supplier_a", call_id="c1")) == "Call"
    assert views.channel_label(Order(**base, supplier_id="supplier_b", channel="email")) == "Email"
