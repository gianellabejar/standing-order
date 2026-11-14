"""Static synthetic supplier directory (data/seed/suppliers.json). Not tenant data: per-tenant terms
live in memories. Phone numbers come from env (a teammate's verified phone), never from the repo."""
import json
import os
from functools import lru_cache
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "data" / "seed" / "suppliers.json"


@lru_cache
def _all() -> dict[str, dict]:
    return {s["id"]: s for s in json.loads(FILE.read_text(encoding="utf-8"))}


def get_supplier(supplier_id: str) -> dict:
    s = dict(_all()[supplier_id])
    s["phone"] = (os.environ.get(s["phone_env"]) if s.get("phone_env") else None) or s.get("phone_default")
    return s


def all_suppliers() -> list[dict]:
    return [get_supplier(i) for i in _all()]
