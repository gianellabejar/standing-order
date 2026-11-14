"""Tenant B must never read tenant A's memories through any repository method."""
import inspect
import os

import pytest
from pymongo import MongoClient

from shared import db as dbmod
from shared.models import Memory, Source

A, B = "maple", "harbour"
UNSCOPED = {"resolve_by_phone", "resolve_by_email"}  # the only allowed tenant-less reads
SECRET = "Supplier B gives Maple Bakery 8% off list"


def repo_classes():
    return [c for _, c in inspect.getmembers(dbmod, inspect.isclass) if issubclass(c, dbmod._Repo) and c is not dbmod._Repo]


def test_every_public_method_requires_tenant_id_first():
    for cls in repo_classes():
        for name, fn in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_") or name in UNSCOPED:
                continue
            params = list(inspect.signature(fn).parameters)
            assert params[1] == "tenant_id", f"{cls.__name__}.{name} must take tenant_id first"


@pytest.fixture
def mem():
    if not os.environ.get("MONGODB_URI"):
        pytest.skip("MONGODB_URI not set")
    client = MongoClient(os.environ["MONGODB_URI"])
    db = client["orderdesk_test"]
    repo = dbmod.Memories(db)
    yield repo
    client.drop_database("orderdesk_test")


def test_tenant_b_cannot_read_tenant_a_memories(mem):
    mid = mem.insert(A, Memory(tenant_id=A, kind="terms", text=SECRET, source=Source(type="sms", ref="x"),
                               embedding=[0.1] * dbmod.EMBED_DIM))
    assert mem.get(A, mid) is not None  # sanity: owner can read it
    assert mem.get(B, mid) is None
    assert mem.list(B) == []
    assert mem.list_active(B) == []
    assert mem.search(B, [0.1] * dbmod.EMBED_DIM, k=5) == []  # needs the vector index to exist
    mem.expire(B, mid)
    with pytest.raises(LookupError):
        mem.supersede(B, mid, Memory(tenant_id=B, kind="terms", text="x", source=Source(type="sms", ref="y")))
    assert mem.get(A, mid).status.value == "active"  # B's writes did not touch A's record
    with pytest.raises(ValueError):
        mem.list("")
