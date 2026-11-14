"""Static synthetic supplier directory (shared/suppliers.json; deployed, unlike data/). Not tenant data: per-tenant terms
live in memories. Phone numbers come from env (a teammate's verified phone), never from the repo."""
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

FILE = Path(__file__).resolve().parent / "suppliers.json"


@lru_cache
def _all() -> dict[str, dict]:
    return {s["id"]: s for s in json.loads(FILE.read_text(encoding="utf-8"))}


def env_first(*names: Optional[str]) -> Optional[str]:
    """First non-empty env var among `names` (new role-based names first, then the legacy ones)."""
    return next((os.environ[n].strip() for n in names if n and os.environ.get(n, "").strip()), None)


def get_supplier(supplier_id: str) -> dict:
    """Directory entry plus `phone` and `email` from env (SUPPLIER1_PHONE, SUPPLIER1_EMAIL, ...); the committed
    defaults are synthetic placeholders. `email` is lower-cased so it can be matched in text."""
    s = dict(_all()[supplier_id])
    s["phone"] = env_first(s.get("phone_env"), s.get("phone_env_legacy")) or s.get("phone_default")
    s["email"] = (env_first(s.get("email_env")) or s.get("email") or "").lower()
    return s


def supplier_label(supplier_id: str) -> str:
    try:
        return _all()[supplier_id].get("label") or supplier_id
    except KeyError:
        return supplier_id


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", x.lower())


def match_supplier(hint: str, supplier_ids: list[str]) -> list[str]:
    """Which of `supplier_ids` the owner's wording names: the id, the letter ("B"), the label, or a distinctive part
    of the name ("Gianella's"). Several matches means ambiguous; the caller must ask."""
    h, out = _norm(hint or ""), []
    for sid in supplier_ids:
        s = _all().get(sid, {})
        exact = {_norm(sid), _norm(s.get("short", "")), _norm(s.get("label", ""))} - {""}
        names = [_norm(n) for n in s.get("names", []) + [s.get("label", "")] if len(_norm(n)) >= 4]
        if h and (h in exact or (len(h) >= 4 and any(h in n or n in h for n in names))):
            out.append(sid)
    return out


def all_suppliers() -> list[dict]:
    return [get_supplier(i) for i in _all()]


PRODUCTS_FILE = Path(__file__).resolve().parent / "products.json"


@lru_cache
def products() -> dict[str, dict]:
    """sku -> {name, unit, short}. Synthetic catalogue names, deployed with the app."""
    return json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))


def product_name(sku: str, short: bool = False) -> str:
    p = products().get(sku)
    return (p["short"] if short else p["name"]) if p else sku
