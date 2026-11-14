"""Governed memory: add (source required), correct (supersede), expire, forget (tombstone). Every change is
audited. Tenant isolation comes from the repositories; these functions also refuse ids the tenant does not own."""
from datetime import timedelta
import logging
from typing import Optional

from agent.ledger import Repos
from agent.memory.embed import Embedder
from shared.models import AuditEvent, Memory, MemoryStatus, Source, now

log = logging.getLogger("memory")


class Governance:
    def __init__(self, repos: Repos, embedder: Optional[Embedder] = None):
        self.r, self.embedder = repos, embedder

    def _audit(self, tenant_id: str, actor: str, event: str, ids: list[str], **extra) -> None:
        self.r.audit.insert(tenant_id, AuditEvent(tenant_id=tenant_id, actor=actor, event=event, ids=ids, **extra))

    def _owned(self, tenant_id: str, memory_id: str) -> Memory:
        m = self.r.memories.get(tenant_id, memory_id)
        if m is None:
            raise LookupError("memory not found for this tenant")
        return m

    def _embed(self, m: Memory) -> Memory:
        if self.embedder and m.text:
            try:
                m.embedding = self.embedder.embed([m.text], "document")[0]
            except Exception as e:  # still readable, but Vector Search cannot see it (the context pack tops up with it)
                log.warning("embedding failed (%s); memory saved without a vector", type(e).__name__)
                m.embedding = None
        return m

    def add(self, tenant_id: str, kind: str, text: str, source: Optional[Source], structured: Optional[dict] = None,
            actor: str = "agent", seeded: bool = False, expires_at=None, confidence: float = 1.0) -> str:
        if source is None or not source.ref:
            raise ValueError("a memory needs a source")
        m = Memory(tenant_id=tenant_id, kind=kind, text=text, structured=structured or {}, source=source,
                   seeded=seeded, confidence=confidence, **({"expires_at": expires_at} if expires_at else {}))
        mid = self.r.memories.insert(tenant_id, self._embed(m))
        self._audit(tenant_id, actor, "memory.add", [mid])
        return mid

    def correct(self, tenant_id: str, old_id: str, text: str, source: Optional[Source], structured: Optional[dict] = None,
                actor: str = "owner") -> str:
        """New memory with supersedes -> old; old becomes `superseded` and leaves retrieval, history stays."""
        if source is None or not source.ref:
            raise ValueError("a correction needs a source")
        old = self._owned(tenant_id, old_id)
        if old.status != MemoryStatus.active:
            raise ValueError(f"only an active memory can be corrected (status={old.status.value})")
        new = Memory(tenant_id=tenant_id, kind=old.kind, text=text, structured=structured or old.structured,
                     source=source, expires_at=old.expires_at)
        new_id = self.r.memories.supersede(tenant_id, old_id, self._embed(new))
        self._audit(tenant_id, actor, "memory.supersede", [old_id, new_id])
        return new_id

    def expire(self, tenant_id: str, memory_id: str, actor: str = "owner", purge_after: Optional[timedelta] = None) -> None:
        self._owned(tenant_id, memory_id)
        self.r.memories.expire(tenant_id, memory_id, purge_at=(now() + purge_after) if purge_after else None)
        self._audit(tenant_id, actor, "memory.expire", [memory_id])

    def forget(self, tenant_id: str, memory_id: str) -> None:
        """Content deleted now; tombstone in audit_events (written by the repo); the TTL index purges the shell."""
        self._owned(tenant_id, memory_id)
        self.r.memories.forget(tenant_id, memory_id, self.r.audit)

    def history(self, tenant_id: str, memory_id: str) -> list[Memory]:
        """The chain of corrections ending at `memory_id`, newest first."""
        chain, cur = [], self._owned(tenant_id, memory_id)
        while cur:
            chain.append(cur)
            cur = self.r.memories.get(tenant_id, cur.supersedes) if cur.supersedes else None
        return chain
