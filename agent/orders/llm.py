"""Shared structured-output call for DeepSeek. Thinking is disabled: in thinking mode the hidden reasoning used the
whole token budget and returned nothing (seen on the close-summary call)."""
import os
from typing import Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def structured_call(schema: Type[T], prompt: str, max_tokens: int = 500) -> tuple[T, dict]:
    """One json_mode call validated by `schema`. Returns (parsed, {"prompt": n, "completion": n}) from model usage."""
    from langchain_deepseek import ChatDeepSeek
    llm = ChatDeepSeek(model=os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash", temperature=0,
                       max_tokens=max_tokens, extra_body={"thinking": {"type": "disabled"}})
    out = llm.with_structured_output(schema, method="json_mode", include_raw=True).invoke(prompt)
    if out.get("parsing_error") or out.get("parsed") is None:
        raise ValueError(f"LLM output was not valid {schema.__name__}: {out.get('parsing_error')}")
    u = getattr(out["raw"], "usage_metadata", None) or {}
    return out["parsed"], {"prompt": u.get("input_tokens", 0), "completion": u.get("output_tokens", 0)}
