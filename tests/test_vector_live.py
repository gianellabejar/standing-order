"""LIVE Atlas Vector Search with real Voyage embeddings (db orderdesk_test). Skipped without MONGODB_URI and
VOYAGE_API_KEY. The first run creates the search index and waits for it to become queryable (up to ~3 min)."""
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
if not (os.environ.get("MONGODB_URI") and os.environ.get("VOYAGE_API_KEY")):
    pytest.skip("MONGODB_URI / VOYAGE_API_KEY not set", allow_module_level=True)

from pymongo import MongoClient  # noqa: E402

from agent.memory.embed import VoyageEmbedder  # noqa: E402
from agent.memory.governance import Governance  # noqa: E402
from agent import ledger  # noqa: E402
from shared import db as dbm  # noqa: E402
from shared.models import Source  # noqa: E402

A, B = "maple", "harbour"
SRC = Source(type="sms", ref="live")


@pytest.fixture(scope="module")
def live():
    client = MongoClient(os.environ["MONGODB_URI"], tz_aware=True)
    db = client["orderdesk_test"]
    dbm.ensure_indexes(db)
    db.memories.delete_many({})
    emb = VoyageEmbedder(os.environ["VOYAGE_API_KEY"])
    repos = ledger.Repos(db)
    yield repos, Governance(repos, emb), emb
    db.memories.delete_many({})


def wait_queryable(repos, tenant, q):
    deadline, last = time.time() + 180, None
    while time.time() < deadline:
        try:
            if repos.memories.search_scored(tenant, q, 5):
                return
            last = "no hits yet"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:200]}"
        time.sleep(5)
    pytest.fail(f"vector index did not become queryable in 180s (last: {last})")


def test_vector_search_prefilter_isolates_tenants_and_honours_status_and_expiry(live):
    repos, gov, emb = live
    secret = gov.add(A, "terms", "Gianella's Ingredients gives Sean's Bakery 8% off list price.", SRC)
    gov.add(B, "note", "Cerebral Cafe closes on Mondays.", SRC)
    q = emb.embed(["what discount does supplier B give?"], "query")[0]
    wait_queryable(repos, A, q)

    hits_a = repos.memories.search_scored(A, q, 5)
    assert secret in [m.id for m, _ in hits_a] and all(m.tenant_id == A for m, _ in hits_a)
    assert hits_a[0][1] > 0                                                        # a real similarity score
    hits_b = repos.memories.search_scored(B, q, 5)                                 # same query, other tenant
    assert secret not in [m.id for m, _ in hits_b] and all(m.tenant_id == B for m, _ in hits_b)
    assert "8%" not in " ".join(m.text for m, _ in hits_b)

    new = gov.correct(A, secret, "Gianella's Ingredients gives Sean's Bakery 5% off list price.", Source(type="sms", ref="live2"))
    deadline = time.time() + 60                                                   # index sync is eventually consistent
    while time.time() < deadline:
        ids = [m.id for m, _ in repos.memories.search_scored(A, q, 5)]
        if secret not in ids and new in ids:
            break
        time.sleep(3)
    assert secret not in ids and new in ids                                       # superseded left retrieval
    gov.expire(A, new)
    deadline = time.time() + 60
    while time.time() < deadline:
        ids = [m.id for m, _ in repos.memories.search_scored(A, q, 5)]
        if new not in ids:
            break
        time.sleep(3)
    assert new not in ids                                                         # expired left retrieval
