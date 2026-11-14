"""One LangGraph thread per session. The graph is deliberately trivial: it stores the session's turns in the
`messages` channel through the MongoDBSaver checkpointer, so the close-time summary can read exactly this
session's turns (and nothing from older sessions, which live under other thread_ids)."""
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

ROLES = ("owner", "agent", "supplier")


def build_graph(saver):
    g = StateGraph(MessagesState)
    g.add_node("record", lambda state: {})  # nothing to compute; the checkpoint is the record
    g.add_edge(START, "record")
    g.add_edge("record", END)
    return g.compile(checkpointer=saver)


def record_turn(graph, thread_id: str, role: str, text: str, source: Optional[dict] = None) -> None:
    """Append one turn to the session's thread. `source` is {"type": "sms"|"call"|"email"|..., "ref": id}."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    cls = AIMessage if role == "agent" else HumanMessage
    msg = cls(content=text, additional_kwargs={"role": role, **({"source": source} if source else {})})
    graph.invoke({"messages": [msg]}, {"configurable": {"thread_id": thread_id}})
