"""Context pack: what a NEW session starts from (never the old transcript). Built by code under a hard token cap.
Contents: tenant profile, open tasks, top-5 relevant memories, last session summary."""
import logging
from dataclasses import dataclass, field
from typing import Optional

from agent.ledger import Repos
from agent.memory.embed import Embedder, count_tokens, truncate_tokens
from shared.models import OrderStatus, Session

log = logging.getLogger("context_pack")
OPEN = [OrderStatus.planned.value, OrderStatus.calling.value, OrderStatus.needs_owner.value]


@dataclass
class ContextPack:
    text: str
    tokens: int                                  # estimate (cl100k), see embed.count_tokens
    memory_ids: list[str] = field(default_factory=list)
    dropped_memory_ids: list[str] = field(default_factory=list)


def _render(profile: str, tasks: list[str], mems: list[str], summary: str) -> str:
    parts = [f"## Shop\n{profile}"] if profile else []
    parts.append("## Open tasks\n" + ("\n".join(f"- {t}" for t in tasks) if tasks else "none"))
    parts.append("## Memories\n" + ("\n".join(f"- {m}" for m in mems) if mems else "none"))
    if summary:
        parts.append(f"## Last session\n{summary}")
    return "\n\n".join(parts)


def _retrieve(repos: Repos, tenant_id: str, query: str, embedder: Optional[Embedder], k: int):
    """Vector Search through the repository (tenant, status and expiry are in its pre-filter). Without an
    embedder or on failure: plain-code fallback, active memories most recently used first (score 0)."""
    hits = []
    if embedder and query:
        try:
            hits = repos.memories.search_scored(tenant_id, embedder.embed([query], "query")[0], k)
            if len(hits) >= k:
                return hits
        except Exception as e:
            log.warning("vector search failed (%s); using fallback", type(e).__name__)
    # Fallback / top-up: active memories Vector Search cannot see (no vector yet, e.g. embedding was rate-limited),
    # or everything when there is no embedder. Most recently used first, score 0.
    seen = {m.id for m, _ in hits}
    live = [m for m in repos.memories.list_active(tenant_id, limit=50)
            if m.id not in seen and (not m.embedding or not embedder or not query)]
    live.sort(key=lambda m: m.last_used_at.timestamp() if m.last_used_at else 0, reverse=True)
    return hits + [(m, 0.0) for m in live[:k - len(hits)]]


def build_context_pack(tenant_id: str, session: Session, repos: Optional[Repos] = None, query: str = "",
                       embedder: Optional[Embedder] = None, max_tokens: int = 1500, k: int = 5) -> ContextPack:
    repos = repos or Repos()
    tenant = repos.tenants.get(tenant_id)
    profile = tenant.profile if tenant else ""
    tasks = [f"order {o.id[:8]} {o.status.value}" + (f": {o.needs_owner_reason}" if o.needs_owner_reason else "")
             for o in repos.orders.list(tenant_id, limit=10, status={"$in": OPEN})]
    last = repos.sessions.latest_closed(tenant_id)
    summary = last.summary if last and last.summary else ""
    scored = sorted(_retrieve(repos, tenant_id, query, embedder, k), key=lambda p: p[1], reverse=True)

    kept, dropped = list(scored), []
    while kept and count_tokens(_render(profile, tasks, [m.text for m, _ in kept], summary)) > max_tokens:
        dropped.append(kept.pop()[0].id)  # lowest score first
    text = _render(profile, tasks, [m.text for m, _ in kept], summary)
    if count_tokens(text) > max_tokens:  # memories are gone and it is still over: shrink the summary, then hard-cut
        room = max_tokens - count_tokens(_render(profile, tasks, [], ""))
        text = _render(profile, tasks, [], truncate_tokens(summary, max(room, 0)))
        text = truncate_tokens(text, max_tokens)
    tokens = count_tokens(text)
    repos.sessions.record_context_tokens(tenant_id, session.id, tokens)
    if kept:
        repos.memories.touch_used(tenant_id, [m.id for m, _ in kept])
    log.info("context pack tenant=%s session=%s tokens=%d/%d memories=%d dropped=%d", tenant_id, session.id, tokens,
             max_tokens, len(kept), len(dropped))
    return ContextPack(text=text, tokens=tokens, memory_ids=[m.id for m, _ in kept], dropped_memory_ids=dropped)
