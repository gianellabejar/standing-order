"""Static synthetic supplier directory (shared/suppliers.json; deployed, unlike data/). Not tenant data: per-tenant terms
live in memories. Phone numbers come from env (a teammate's verified phone), never from the repo."""
import json
import os
from functools import lru_cache
from pathlib import Path

FILE = Path(__file__).resolve().parent / "suppliers.json"


@lru_cache
def _all() -> dict[str, dict]:
    return {s["id"]: s for s in json.loads(FILE.read_text(encoding="utf-8"))}


def get_supplier(supplier_id: str) -> dict:
    s = dict(_all()[supplier_id])
    s["phone"] = (os.environ.get(s["phone_env"]) if s.get("phone_env") else None) or s.get("phone_default")
    return s


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
