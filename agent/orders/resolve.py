"""Plain-code resolution of what the owner meant: aliases ("flour" -> a SKU), quantity defaults and "same as last
time" come from memory and past orders, never from the LLM."""
import re
from typing import Optional

STOP = set("a an the that this those my our us we you i it is are was were be to of for from and or but with about "
           "forget remember please now on in at as by do does did have has".split())


def alias_map(memories) -> dict:
    """{"flour": (sku, memory_id)} from memories whose structured key is "alias:<word>"."""
    return {str(m.structured["key"])[6:].lower(): (m.structured["value"], m.id)
            for m in memories if str(m.structured.get("key", "")).startswith("alias:")}


def resolve_items(parsed_items, same_as_last: bool, aliases: dict, skus: list, last_items: list,
                  base_items: list) -> tuple[Optional[list], list, Optional[str]]:
    """Returns (items, alias_memory_ids_used, problem). `problem` is a question/notice for the owner."""
    names = {sku: word for word, (sku, _) in aliases.items()}
    last_qty = {i["sku"]: i.get("qty") for i in last_items}
    out, used = {}, []

    def add(sku, name, qty):
        row = out.setdefault(sku, {"sku": sku, "name": names.get(sku) or name or sku, "qty": 0, "unit": "bag"})
        row["qty"] += qty

    for b in base_items:  # items already in the pending order come first
        add(b["sku"], b.get("name"), b["qty"])
    for it in parsed_items:
        words = re.findall(r"[a-z]+", (it.name or "").lower())
        sku = None
        for word, (alias_sku, mem_id) in aliases.items():
            if word in words:
                sku = alias_sku
                used.append(mem_id)
                break
        if sku is None and it.sku in skus:
            sku = it.sku
        if sku is None:
            return None, used, f"I don't have a current price for '{it.name or it.sku}'. Forward me a quote and I'll use it."
        qty = it.qty or last_qty.get(sku)
        if not qty:
            return None, used, f"How many {it.name or sku}?"
        add(sku, it.name, qty)
    if not out and same_as_last:
        for i in last_items:
            add(i["sku"], i.get("name"), i["qty"])
    if not out:
        return None, used, "What would you like to order, and how many?"
    return list(out.values()), sorted(set(used)), None


def words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9%]+", text.lower()) if w not in STOP}


def best_memory_match(target: str, memories) -> tuple[Optional[object], bool]:
    """(memory, ambiguous). Word overlap between the owner's words and each memory's text."""
    tw = words(target)
    scored = sorted(((len(tw & words(m.text)), m) for m in memories), key=lambda p: p[0], reverse=True)
    if not scored or scored[0][0] == 0:
        return None, False
    ambiguous = len(scored) > 1 and scored[1][0] == scored[0][0]
    return scored[0][1], ambiguous
