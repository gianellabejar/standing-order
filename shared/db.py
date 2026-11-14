"""The only module that touches Mongo collections. Every method takes tenant_id and injects it into
the query (including the $vectorSearch pre-filter). The two unscoped lookups (`resolve_by_*`) exist
because the tenant is not known yet at SMS routing and login; nothing else may bypass the filter."""
import os
from datetime import timedelta
from functools import lru_cache
from uuid import uuid4
from typing import Optional

from pymongo import ASCENDING, MongoClient
from pymongo.operations import SearchIndexModel

from shared.models import (AuditEvent, Call, Document, Memory, MemoryStatus, Order, OrderStatus,
                           PriceQuote, Session, Tenant, User, now)

PURGE_GRACE = timedelta(days=7)
VECTOR_INDEX = "memories_vec"
EMBED_DIM = 1024  # voyage-3.5 default


@lru_cache
def _db():
    return MongoClient(os.environ["MONGODB_URI"], tz_aware=True)[os.environ.get("MONGODB_DB", "orderdesk")]


def get_db():
    return _db()


class _Repo:
    name: str
    model: type

    def __init__(self, db=None):
        self._c = (db if db is not None else _db())[self.name]

    def _q(self, tenant_id: str, **flt) -> dict:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return {**flt, "tenant_id": tenant_id}  # tenant_id always wins over caller input

    def get(self, tenant_id: str, id: str):
        d = self._c.find_one(self._q(tenant_id, _id=id))
        return self.model(**d) if d else None

    def list(self, tenant_id: str, limit: int = 100, **flt):
        return [self.model(**d) for d in self._c.find(self._q(tenant_id, **flt)).limit(limit)]

    def insert(self, tenant_id: str, obj) -> str:
        d = obj.model_dump(by_alias=True, exclude_none=True)
        d["tenant_id"] = tenant_id
        d.setdefault("_id", uuid4().hex)  # string ids, so get(tenant_id, id) round-trips
        return str(self._c.insert_one(d).inserted_id)


class Tenants(_Repo):
    name, model = "tenants", Tenant

    def get(self, tenant_id: str, id: Optional[str] = None):
        d = self._c.find_one({"_id": self._q(tenant_id)["tenant_id"]})
        return Tenant(**d) if d else None

    def insert(self, tenant_id: str, obj) -> str:
        d = obj.model_dump(by_alias=True, exclude_none=True)
        d["_id"] = tenant_id
        return str(self._c.insert_one(d).inserted_id)

    def resolve_by_phone(self, phone: str) -> Optional[Tenant]:
        """UNSCOPED by necessity: inbound SMS routing. Returns only the tenant record."""
        d = self._c.find_one({"phones": phone})
        return Tenant(**d) if d else None


class Users(_Repo):
    name, model = "users", User

    def resolve_by_email(self, email: str) -> Optional[User]:
        """UNSCOPED by necessity: login."""
        d = self._c.find_one({"email": email})
        return User(**d) if d else None


class Sessions(_Repo):
    name, model = "sessions", Session

    def close(self, tenant_id: str, id: str, summary: str, tokens: dict) -> None:
        self._c.update_one(self._q(tenant_id, _id=id),
                           {"$set": {"closed_at": now(), "summary": summary, "tokens": tokens}})


class Memories(_Repo):
    name, model = "memories", Memory

    def _live(self, tenant_id: str) -> dict:
        return self._q(tenant_id, status=MemoryStatus.active.value, expires_at={"$gt": now()})

    def list_active(self, tenant_id: str, limit: int = 100):
        return [Memory(**d) for d in self._c.find(self._live(tenant_id)).limit(limit)]

    def search(self, tenant_id: str, embedding: list[float], k: int = 5):
        """Vector Search with tenant_id, status and expiry in the pre-filter."""
        flt = self._live(tenant_id)
        pipe = [{"$vectorSearch": {"index": VECTOR_INDEX, "path": "embedding", "queryVector": embedding,
                                   "numCandidates": 10 * k, "limit": k, "filter": flt}},
                {"$addFields": {"score": {"$meta": "vectorSearchScore"}}}]
        return [Memory(**d) for d in self._c.aggregate(pipe)]

    def supersede(self, tenant_id: str, old_id: str, new: Memory) -> str:
        if self.get(tenant_id, old_id) is None:
            raise LookupError("memory not found for this tenant")
        new.supersedes = old_id
        new_id = self.insert(tenant_id, new)
        self._c.update_one(self._q(tenant_id, _id=old_id), {"$set": {"status": "superseded"}})
        return new_id

    def expire(self, tenant_id: str, id: str) -> None:
        self._c.update_one(self._q(tenant_id, _id=id), {"$set": {"expires_at": now()}})

    def forget(self, tenant_id: str, id: str, audit: "AuditEvents") -> None:
        """Delete content, keep a tombstone. The TTL index hard-deletes the shell after PURGE_GRACE."""
        self._c.update_one(self._q(tenant_id, _id=id), {"$set": {
            "text": "", "structured": {}, "embedding": None, "status": "retracted",
            "expires_at": now(), "purge_at": now() + PURGE_GRACE}})
        audit.insert(tenant_id, AuditEvent(tenant_id=tenant_id, actor="owner", event="memory.forget",
                                           ids=[id], tombstone={"memory_id": id}))


class PriceQuotes(_Repo):
    name, model = "price_quotes", PriceQuote

    def list_valid(self, tenant_id: str, sku: Optional[str] = None):
        flt = {"status": "active", "valid_until": {"$gt": now()}}
        return self.list(tenant_id, **flt, **({"sku": sku} if sku else {}))


class Documents(_Repo):
    name, model = "documents", Document


class Orders(_Repo):
    name, model = "orders", Order

    def get_by_key(self, tenant_id: str, idempotency_key: str):
        d = self._c.find_one(self._q(tenant_id, idempotency_key=idempotency_key))
        return Order(**d) if d else None

    def set_status(self, tenant_id: str, id: str, status: OrderStatus, **fields) -> None:
        self._c.update_one(self._q(tenant_id, _id=id), {"$set": {"status": status.value, **fields}})


class Calls(_Repo):
    name, model = "calls", Call

    def get_by_conversation(self, tenant_id: str, conversation_id: str):
        d = self._c.find_one(self._q(tenant_id, conversation_id=conversation_id))
        return Call(**d) if d else None

    def record_outcome(self, tenant_id: str, id: str, transcript: list, outcome: dict) -> None:
        self._c.update_one(self._q(tenant_id, _id=id), {"$set": {"transcript": transcript, "outcome": outcome}})


class AuditEvents(_Repo):
    name, model = "audit_events", AuditEvent


def ensure_indexes(db=None) -> None:
    db = db if db is not None else _db()
    db.orders.create_index([("tenant_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True)
    db.memories.create_index("purge_at", expireAfterSeconds=0)  # TTL
    for c in ("memories", "price_quotes", "sessions", "documents", "calls", "audit_events", "users"):
        db[c].create_index("tenant_id")
    db.tenants.create_index("phones")
    db.memories.create_search_index(SearchIndexModel(name=VECTOR_INDEX, type="vectorSearch", definition={
        "fields": [{"type": "vector", "path": "embedding", "numDimensions": EMBED_DIM, "similarity": "cosine"},
                   *({"type": "filter", "path": p} for p in ("tenant_id", "status", "expires_at"))]}))
