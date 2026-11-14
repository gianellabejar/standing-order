"""Role-based contact env vars (CLIENT1_*, SUPPLIER1_*, ...), supplier names and email detection. Offline."""
import pytest

from agent.voice import ElevenLabsVoiceProvider
from ingest.quotes import detect_supplier
from scripts.seed import seed as seedmod
from shared.models import Order
from shared.suppliers import env_first, get_supplier, match_supplier, supplier_label

IDS = ["supplier_a", "supplier_b"]
ENVS = ("CLIENT1_PHONE", "OWNER_PHONE_MAPLE", "CLIENT1_EMAIL", "OWNER_EMAIL_MAPLE", "SUPPLIER1_PHONE", "SUPPLIER_B_PHONE",
        "TEST_SUPPLIER_PHONE", "SUPPLIER1_EMAIL", "SUPPLIER2_EMAIL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENVS:
        monkeypatch.delenv(k, raising=False)


def test_labels_and_matching_accept_any_way_the_owner_names_a_supplier():
    assert supplier_label("supplier_b") == "Gianella's Ingredients" and supplier_label("supplier_a") == "Valley Foods"
    for hint in ("B", "b", "Supplier B", "supplier_b", "Gianella's", "Gianellas Ingredients", "Gianella's Ingredients"):
        assert match_supplier(hint, IDS) == ["supplier_b"], hint
    for hint in ("A", "Supplier A", "Valley", "Valley Foods"):
        assert match_supplier(hint, IDS) == ["supplier_a"], hint
    for hint in ("", "x", "val", "supplier", "ab"):      # short or vague hints never match by substring
        assert match_supplier(hint, IDS) == [], hint


def test_new_env_names_win_and_legacy_names_still_work(monkeypatch):
    t = {"phone_env": "CLIENT1_PHONE", "phone_env_legacy": "OWNER_PHONE_MAPLE", "phone_default": "+15550100001",
         "email_env": "CLIENT1_EMAIL", "email_env_legacy": "OWNER_EMAIL_MAPLE", "email_default": "d@example.com"}
    assert seedmod.contacts(t) == ("+15550100001", "d@example.com")                      # nothing set: synthetic defaults
    monkeypatch.setenv("OWNER_PHONE_MAPLE", "+16470000001")
    monkeypatch.setenv("OWNER_EMAIL_MAPLE", "Old@Example.com")
    assert seedmod.contacts(t) == ("+16470000001", "old@example.com")                    # legacy fallback
    monkeypatch.setenv("CLIENT1_PHONE", "+16470000002")
    monkeypatch.setenv("CLIENT1_EMAIL", " New@Example.com ")
    assert seedmod.contacts(t) == ("+16470000002", "new@example.com")                    # new name wins, trimmed, lower-cased
    assert env_first("NOPE", "CLIENT1_PHONE") == "+16470000002" and env_first("NOPE") is None


def test_supplier_phone_and_email_come_from_env_not_the_repo(monkeypatch):
    assert get_supplier("supplier_b")["phone"] == "+15550100099"                         # synthetic default
    monkeypatch.setenv("SUPPLIER1_PHONE", "+16470000003")
    monkeypatch.setenv("SUPPLIER1_EMAIL", "Supplier.One@Example.com")
    s = get_supplier("supplier_b")
    assert s["phone"] == "+16470000003" and s["email"] == "supplier.one@example.com"
    monkeypatch.delenv("SUPPLIER1_PHONE")
    monkeypatch.setenv("SUPPLIER_B_PHONE", "+16470000004")
    assert get_supplier("supplier_b")["phone"] == "+16470000004"                         # legacy name


def test_supplier_is_detected_by_full_email_address_never_by_a_shared_domain(monkeypatch):
    monkeypatch.setenv("SUPPLIER1_EMAIL", "ingredients.person@gmail.com")
    monkeypatch.setenv("SUPPLIER2_EMAIL", "valley.person@hotmail.com")
    assert detect_supplier("From: Ingredients Person <Ingredients.Person@gmail.com>") == "supplier_b"
    assert detect_supplier("From: valley.person@hotmail.com") == "supplier_a"
    assert detect_supplier("From: some.stranger@gmail.com") is None                       # gmail.com alone proves nothing
    assert detect_supplier("From: some.stranger@hotmail.com") is None
    assert detect_supplier("quote from Gianella's Ingredients") == "supplier_b"            # by name
    assert detect_supplier("Valley Foods and Gianella's Ingredients") is None             # two suppliers: do not guess
    assert detect_supplier("quote from Birchway") == "supplier_b"                          # the old synthetic names still work


def test_voice_calls_only_the_configured_supplier_phone(monkeypatch):
    calls = []

    class R:
        status_code, content = 200, b"x"
        def json(self): return {"success": True, "conversation_id": "c1"}

    monkeypatch.setattr("agent.voice.httpx.post", lambda url, **kw: calls.append(kw["json"]) or R())
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "a")
    monkeypatch.setenv("ELEVENLABS_PHONE_NUMBER_ID", "p")
    order = Order(tenant_id="maple", idempotency_key="k")
    with pytest.raises(ValueError):                                                       # nothing configured: no call
        ElevenLabsVoiceProvider().place_call(order, {})
    monkeypatch.setenv("SUPPLIER1_PHONE", "+16470000003")
    assert ElevenLabsVoiceProvider().place_call(order, {"shop_name": "x"}) == "c1"
    assert calls[0]["to_number"] == "+16470000003"                                        # default: the supplier's phone
    with pytest.raises(ValueError):                                                       # any other number is refused
        ElevenLabsVoiceProvider().place_call(order, {"to_number": "+16479999999"})
    monkeypatch.delenv("SUPPLIER1_PHONE")
    monkeypatch.setenv("TEST_SUPPLIER_PHONE", "+16470000005")                             # legacy name still dials
    ElevenLabsVoiceProvider().place_call(order, {})
    assert calls[1]["to_number"] == "+16470000005" and len(calls) == 2
