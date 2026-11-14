"""The ONE structured LLM call per document: supplier text in, validated quotes out.

DeepSeek structured output works with method="json_mode" (function_calling is rejected in thinking
mode; see scripts/deepseek_smoke.py). Tests replace EXTRACTOR with a recorded response.
"""
import json
import logging
import os
from datetime import date
from typing import Callable, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("ingest.llm")
MAX_CHARS = 12000  # hard cap on document text sent to the model (token budget)


class ExtractedQuote(BaseModel):
    sku: str = Field(min_length=1)
    description: str = ""
    unit: str = Field(min_length=1)
    price: float = Field(gt=0)
    currency: str = "CAD"
    valid_until: Optional[date] = None   # null when the document states no validity date
    page: Optional[int] = None           # page marker the line came from (PDFs only)


class Extraction(BaseModel):
    supplier_name: Optional[str] = None
    quotes: list[ExtractedQuote] = []
    notes: str = ""


PROMPT = """You extract supplier price quotes from one document for a small shop's purchasing system.
Return ONLY a JSON object with this shape:
{{"supplier_name": string or null,
  "quotes": [{{"sku": string, "description": string, "unit": string, "price": number, "currency": string,
              "valid_until": "YYYY-MM-DD" or null, "page": integer or null}}],
  "notes": string}}
Rules:
- Use only facts written in the document. Never invent a SKU, price or date.
- price is the per-unit price as written, as a number without the currency sign.
- valid_until is the date the prices are valid until, if the document states one; otherwise null.
- page is the number in the nearest "=== page N ===" marker above the line; null if there are no markers.
- One entry per SKU line. If there are no prices, return an empty quotes list.

Document ({kind}):
<<<
{text}
>>>"""

Usage = dict[str, int]
Extractor = Callable[[str, str], tuple[Extraction, Usage]]


def deepseek_extract(text: str, kind: str) -> tuple[Extraction, Usage]:
    from langchain_deepseek import ChatDeepSeek
    model = os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash"
    llm = ChatDeepSeek(model=model, temperature=0).with_structured_output(
        Extraction, method="json_mode", include_raw=True)
    out = llm.invoke(PROMPT.format(kind=kind, text=text[:MAX_CHARS]))
    if out.get("parsing_error") or out.get("parsed") is None:
        raise ValueError(f"LLM output failed validation: {out.get('parsing_error')}")
    meta = getattr(out["raw"], "usage_metadata", None) or {}
    usage = {"prompt": meta.get("input_tokens", 0), "completion": meta.get("output_tokens", 0)}
    log.info("extraction model=%s kind=%s tokens=%s", model, kind, usage)
    return out["parsed"], usage


def recorded(response: dict, usage: Optional[Usage] = None) -> Extractor:
    """Extractor that replays a recorded model response (tests, offline demo). Still Pydantic-validated."""
    def _extract(text: str, kind: str) -> tuple[Extraction, Usage]:
        return Extraction.model_validate(json.loads(json.dumps(response))), dict(usage or {})
    return _extract


EXTRACTOR: Extractor = deepseek_extract
