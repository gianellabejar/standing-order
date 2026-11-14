"""Sessions: one text thread is MANY sessions. A session closes after 30 minutes of silence or when its task
completes. A new session gets a new LangGraph thread_id and starts from a context pack, never the old transcript."""
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional

from agent.ledger import Repos
from agent.memory.embed import Embedder
from agent.memory.governance import Governance
from agent.sessions.thread import record_turn
from agent.sessions.summarize import CloseResult, clamp_summary, llm_close
from shared.models import Session, Source, now

log = logging.getLogger("sessions")
IDLE = timedelta(minutes=30)
Message = dict  # {"role": "owner"|"agent"|"supplier", "text": str, "source": {"type": ..., "ref": ...}}


def for_app(repos: Optional[Repos] = None, embedder: Optional[Embedder] = None) -> "SessionManager":
    """The production wiring: MongoDBSaver-backed thread per session, real LLM and embedder."""
    from agent.sessions.thread import build_graph
    saver = get_checkpointer()
    return SessionManager(repos or Repos(), embedder=embedder, graph=build_graph(saver),
                          messages_for=lambda t: messages_from_checkpoint(saver, t))


def get_checkpointer():
    """LangGraph MongoDBSaver on the app database. One thread_id per session."""
    from langgraph.checkpoint.mongodb import MongoDBSaver
    from shared.db import get_db
    return MongoDBSaver(get_db().client, db_name=get_db().name)


def messages_from_checkpoint(saver, thread_id: str) -> list[Message]:
    """Turns of ONE thread (only this session's), from its latest checkpoint's `messages` channel."""
    tup = saver.get_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    out = []
    for i, m in enumerate(((tup.checkpoint.get("channel_values") or {}).get("messages") or []) if tup else []):
        kw = getattr(m, "additional_kwargs", {}) or {}
        role = kw.get("role") or {"human": "owner", "ai": "agent"}.get(getattr(m, "type", ""), "agent")
        src = kw.get("source") or {"type": "session", "ref": f"{thread_id}#{i}"}
        out.append({"role": role, "text": str(m.content), "source": src})
    return out


class SessionManager:
    @staticmethod
    def for_app(repos=None, embedder=None):
        return for_app(repos, embedder)

    def __init__(self, repos: Repos, llm: Callable = llm_close, embedder: Optional[Embedder] = None,
                 messages_for: Optional[Callable[[str], list[Message]]] = None, idle: timedelta = IDLE, graph=None):
        self.r, self.llm, self.idle, self.graph = repos, llm, idle, graph
        self.gov = Governance(repos, embedder)
        self.messages_for = messages_for or (lambda thread_id: [])

    def current(self, tenant_id: str, at: Optional[datetime] = None) -> Session:
        """The open session for a message arriving now. A session idle for 30+ minutes, or whose task is done,
        is closed first and replaced by a fresh one with a NEW thread_id."""
        at = at or now()
        s = self.r.sessions.get_open(tenant_id)
        if s and (s.task_done or at - s.last_activity_at >= self.idle):
            self.close(tenant_id, s.id)
            s = None
        if s is None:
            s = Session(tenant_id=tenant_id, thread_id=f"{tenant_id}:{uuid.uuid4().hex}", started_at=at,
                        last_activity_at=at)
            s.id = self.r.sessions.insert(tenant_id, s)
        else:
            self.r.sessions.touch(tenant_id, s.id, at)
        return s

    def record(self, tenant_id: str, session: Session, role: str, text: str, source: Optional[dict] = None) -> None:
        """Store one turn in the session's LangGraph thread (needs `graph=`) and mark the session active."""
        if self.graph is None:
            raise RuntimeError("SessionManager was built without a graph; pass graph=build_graph(saver)")
        record_turn(self.graph, session.thread_id, role, text, source)
        self.r.sessions.touch(tenant_id, session.id)

    def complete_task(self, tenant_id: str, session_id: str) -> str:
        """The task finished: close now instead of waiting for silence."""
        return self.close(tenant_id, session_id)

    def sweep(self, tenant_id: str, at: Optional[datetime] = None) -> Optional[str]:
        """Close the tenant's open session if it has been silent for 30+ minutes (call from a cron or lazily)."""
        s = self.r.sessions.get_open(tenant_id)
        if s and (at or now()) - s.last_activity_at >= self.idle:
            return self.close(tenant_id, s.id)
        return None

    def close(self, tenant_id: str, session_id: str) -> str:
        s = self.r.sessions.get(tenant_id, session_id)
        if s is None:
            raise LookupError("session not found for this tenant")
        if s.closed_at is not None:
            return s.summary or ""  # idempotent
        msgs = self.messages_for(s.thread_id)
        if not msgs:  # nothing happened: no LLM call, no tokens
            self.r.sessions.close(tenant_id, session_id, "", {"prompt": 0, "completion": 0})
            return ""
        # Only owner/supplier turns can be a source: the assistant's own statements are not facts about the shop.
        refs = {f"m{i}": m["source"] for i, m in enumerate(msgs) if m["role"] != "agent"}
        convo = "\n".join(f"[m{i}] {m['role']}: {m['text']}" for i, m in enumerate(msgs))
        result, usage = self.llm(convo)
        summary = clamp_summary(result.summary)
        written = self._write_candidates(tenant_id, result, refs)
        self.r.sessions.close(tenant_id, session_id, summary, usage)
        log.info("session closed tenant=%s session=%s tokens=%s candidates=%d written=%d", tenant_id, session_id,
                 usage, len(result.candidates), len(written))
        return summary

    def _write_candidates(self, tenant_id: str, result: CloseResult, refs: dict) -> list[str]:
        """A candidate becomes a memory only if its source_ref is one of the messages we showed the model."""
        existing = {m.text.strip().lower() for m in self.r.memories.list_active(tenant_id)}
        out = []
        for c in result.candidates:
            src = refs.get((c.source_ref or "").strip("[]"))
            if not src or not c.text.strip() or c.text.strip().lower() in existing:
                continue
            out.append(self.gov.add(tenant_id, c.kind, c.text.strip(), Source(**src), c.structured, actor="agent",
                                    confidence=0.7))
        return out
