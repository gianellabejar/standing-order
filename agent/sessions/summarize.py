"""The one cheap LLM call at session close: a summary (<=150 tokens) plus memory candidates.
Candidates must cite a message ref we gave the model; code (not the model) decides what counts as sourced."""
import os
from typing import Optional

from pydantic import BaseModel

from agent.memory.embed import truncate_tokens

SUMMARY_TOKENS = 150
PROMPT = """You close a session between a shop owner and a purchasing assistant. Return json with:
"summary": at most 100 words on what happened and what is still open.
"candidates": durable facts worth remembering (supplier terms, preferences, aliases, corrections). Each:
{{"text": str, "kind": "terms|supplier_pref|alias|policy|note", "structured": {{"supplier": str|null, "key": str, "value": any}},
"source_ref": the [id] of the message that states it}}. Only durable facts stated by the owner or a supplier. Do NOT record one-off events (an order was placed,
a confirmation number), prices the assistant computed, or anything only the assistant said.
Return {{"summary": "", "candidates": []}} if nothing durable happened.

Conversation:
{conversation}
"""


class Candidate(BaseModel):
    text: str
    kind: str = "note"
    structured: dict = {}
    source_ref: Optional[str] = None


class CloseResult(BaseModel):
    summary: str = ""
    candidates: list[Candidate] = []


def llm_close(conversation: str) -> tuple[CloseResult, dict]:
    """Returns (result, {"prompt": n, "completion": n}) with the model's own usage numbers."""
    from langchain_deepseek import ChatDeepSeek
    llm = ChatDeepSeek(model=os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash", temperature=0, max_tokens=600,
                      extra_body={"thinking": {"type": "disabled"}})  # thinking mode spent the whole budget on hidden reasoning
    out = llm.with_structured_output(CloseResult, method="json_mode", include_raw=True).invoke(
        PROMPT.format(conversation=conversation))
    if out.get("parsing_error") or out.get("parsed") is None:
        raise ValueError(f"close summary was not valid: {out.get('parsing_error')}")
    u = getattr(out["raw"], "usage_metadata", None) or {}
    return out["parsed"], {"prompt": u.get("input_tokens", 0), "completion": u.get("output_tokens", 0)}


def clamp_summary(summary: str) -> str:
    return truncate_tokens(summary.strip(), SUMMARY_TOKENS)
