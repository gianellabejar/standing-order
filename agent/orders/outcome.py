"""After a call: ONE structured LLM call for what plain code cannot read, namely terms the supplier changed and
anything unclear. It never decides whether the order is confirmed; that is the confirmation-number rule in code."""
from typing import Callable

from pydantic import BaseModel

from agent.orders.llm import structured_call


class CallNotes(BaseModel):
    changed_terms: list[str] = []   # e.g. "price is now 3.50", "delivery moved to Monday"
    unclear: list[str] = []


PROMPT = """A purchasing assistant phoned a supplier to place this order: {brief}
Read the call transcript. Return json {{"changed_terms": [...], "unclear": [...]}}.
changed_terms: anything the SUPPLIER said that differs from the order (price, quantity, item, delivery date, availability).
unclear: things that were ambiguous or cut off. Use empty lists if there are none. Do not invent anything.

Transcript:
{transcript}
"""


def llm_notes(brief: dict, transcript: list[dict]) -> tuple[CallNotes, dict]:
    text = "\n".join(f"{t.get('role')}: {t.get('message')}" for t in transcript if t.get("message"))
    keep = {k: brief.get(k) for k in ("item", "quantity", "unit", "price", "po_number")}
    return structured_call(CallNotes, PROMPT.format(brief=keep, transcript=text[:6000]), 300)


def supplier_spoke(transcript: list[dict]) -> bool:
    return any(t.get("role") == "user" and (t.get("message") or "").strip("… .") for t in transcript)


def extract_notes(brief: dict, transcript: list[dict], llm: Callable = llm_notes):
    """None when no supplier spoke (voicemail, no answer): nothing to read, no tokens spent."""
    return llm(brief, transcript) if supplier_spoke(transcript) else None
