"""LIVE: the session thread on the real MongoDBSaver (db orderdesk_test). Skipped without MONGODB_URI."""
import os
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
if not os.environ.get("MONGODB_URI"):
    pytest.skip("MONGODB_URI not set", allow_module_level=True)

from langgraph.checkpoint.mongodb import MongoDBSaver  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from agent.sessions.manager import messages_from_checkpoint  # noqa: E402
from agent.sessions.thread import build_graph, record_turn  # noqa: E402


def test_turns_persist_in_mongodb_per_thread():
    client = MongoClient(os.environ["MONGODB_URI"], tz_aware=True)
    saver = MongoDBSaver(client, db_name="orderdesk_test")
    g = build_graph(saver)
    t1, t2 = f"live:{uuid.uuid4().hex}", f"live:{uuid.uuid4().hex}"
    try:
        record_turn(g, t1, "owner", "Need flour", {"type": "sms", "ref": "SM1"})
        record_turn(g, t1, "agent", "On it")
        record_turn(g, t2, "owner", "Other thread", {"type": "sms", "ref": "SM2"})
        fresh = MongoDBSaver(MongoClient(os.environ["MONGODB_URI"], tz_aware=True), db_name="orderdesk_test")
        m1 = messages_from_checkpoint(fresh, t1)                       # read through a NEW saver: it is in Mongo
        assert [(m["role"], m["text"]) for m in m1] == [("owner", "Need flour"), ("agent", "On it")]
        assert m1[0]["source"] == {"type": "sms", "ref": "SM1"}
        assert [m["text"] for m in messages_from_checkpoint(fresh, t2)] == ["Other thread"]
    finally:
        for coll in ("checkpoints", "checkpoint_writes"):
            client["orderdesk_test"][coll].delete_many({"thread_id": {"$in": [t1, t2]}})
