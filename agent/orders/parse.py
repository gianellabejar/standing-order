"""Understand an owner text: ONE structured LLM call. Everything after it (SKU validation, quantities, supplier
choice, money) is plain code."""
from typing import Callable, Literal, Optional

from pydantic import BaseModel

from agent.orders.llm import structured_call


class Item(BaseModel):
    name: str = ""
    sku: Optional[str] = None       # must be one of the SKUs we listed; code re-validates
    qty: Optional[int] = None


class Correction(BaseModel):
    sku: Optional[str] = None
    supplier: Optional[str] = None  # as written, e.g. "B"
    new_price: Optional[float] = None


class ParsedText(BaseModel):
    intent: Literal["order", "add_to_order", "price_correction", "forget", "question", "other"] = "other"
    items: list[Item] = []
    same_as_last_time: bool = False
    deadline: Optional[str] = None
    correction: Optional[Correction] = None
    forget_target: Optional[str] = None
    question_sku: Optional[str] = None


PROMPT = """You read one text message from a small shop owner to their purchasing assistant. Return json:
{{"intent": "order" | "add_to_order" | "price_correction" | "forget" | "question" | "other",
 "items": [{{"name": str, "sku": one of the SKUs below or null, "qty": int or null}}],
 "same_as_last_time": bool, "deadline": str or null,
 "correction": {{"sku": SKU or null, "supplier": str or null, "new_price": number}} or null,
 "forget_target": str or null, "question_sku": SKU or null}}
Meanings: order = wants to buy; add_to_order = adds items to an order already in progress; price_correction = says a
supplier's price is now X; forget = asks to forget a remembered fact (forget_target = the fact in their words);
question = asks about past orders or prices. "same as last time" -> same_as_last_time true, items may be empty.
Use only these SKUs: {skus}

What you know about this shop:
{context}

Message: {text}
"""


def llm_parse(text: str, context: str, skus: list[str]) -> tuple[ParsedText, dict]:
    return structured_call(ParsedText, PROMPT.format(skus=", ".join(skus), context=context, text=text), 400)


def parse_text(text: str, context: str, skus: list[str], llm: Callable = llm_parse) -> tuple[ParsedText, dict]:
    return llm(text, context, skus)
