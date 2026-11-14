"""DeepSeek smoke test: plain call, tool call, structured output. Run: python scripts/deepseek_smoke.py"""
import os
import sys

import httpx
from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_deepseek import ChatDeepSeek
from pydantic import BaseModel

load_dotenv()
MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash"  # ids come from GET /models; override in .env


class Order(BaseModel):
    item: str
    quantity: int


@tool
def get_price(sku: str) -> float:
    """Return the unit price for a SKU."""
    return 3.2


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def main():
    key = os.environ.get("DEEPSEEK_API_KEY") or fail("DEEPSEEK_API_KEY not set")
    r = httpx.get("https://api.deepseek.com/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    if r.status_code != 200:
        fail(f"/models returned {r.status_code}: {r.text[:200]}")
    ids = [m["id"] for m in r.json()["data"]]
    print("available models:", ids)
    if MODEL not in ids:
        fail(f"model {MODEL!r} not available; set DEEPSEEK_MODEL to one of {ids}")
    llm = ChatDeepSeek(model=MODEL, temperature=0)
    tokens = 0

    msg = llm.invoke("Reply with the single word: pong")
    tokens += msg.usage_metadata["total_tokens"]
    print("plain:", msg.content.strip()[:60])
    if not msg.content.strip():
        fail("plain call returned empty content")

    msg = llm.bind_tools([get_price]).invoke("What is the price of SKU FLOUR-20KG? Use the tool.")
    tokens += msg.usage_metadata["total_tokens"]
    print("tool_calls:", msg.tool_calls)
    if not msg.tool_calls or msg.tool_calls[0]["name"] != "get_price":
        fail("no get_price tool call returned")

    worked = []
    for method in ("function_calling", "json_mode"):
        try:
            prompt = "Extract the order as JSON with keys item and quantity: 'need 40 bags of flour'"
            out = llm.with_structured_output(Order, method=method).invoke(prompt)
            print(f"structured[{method}]:", out)
            if isinstance(out, Order) and out.quantity == 40:
                worked.append(method)
        except Exception as e:
            print(f"structured[{method}] error: {type(e).__name__}: {str(e)[:150]}")
    if not worked:
        fail("neither structured-output method returned a valid Order")
    print(f"total tokens (plain+tool only): {tokens}")
    print(f"PASS: model={MODEL} structured methods working={worked}")


main()
