"""Give memories that have no vector one (Voyage, one batched request per tenant), so Vector Search can find them.
Idempotent. A `make reset` re-seeds without vectors, so run this after it. Run: python scripts/embed_memories.py"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import ledger  # noqa: E402
from agent.memory.embed import get_embedder  # noqa: E402

TENANTS = ("maple", "harbour")


def main():
    emb = get_embedder()
    if emb is None:
        sys.exit("FAIL: VOYAGE_API_KEY not set")
    repos = ledger.Repos()
    for t in TENANTS:
        todo = [m for m in repos.memories.list_active(t) if not m.embedding and m.text]
        if not todo:
            print(f"{t}: nothing to embed")
            continue
        for m, v in zip(todo, emb.embed([m.text for m in todo], "document")):
            repos.memories.set_embedding(t, m.id, v)
        print(f"{t}: embedded {len(todo)} memories")
    print("PASS")


main()
