"""Atlas smoke test: write, read, list collections. Run: python scripts/atlas_smoke.py"""
import os
import sys
import uuid

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


def main():
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        print("FAIL: MONGODB_URI not set")
        sys.exit(1)
    try:
        db = MongoClient(uri, serverSelectionTimeoutMS=10000)[os.environ.get("MONGODB_DB", "orderdesk")]
        marker = str(uuid.uuid4())
        col = db["_smoke"]  # scratch collection, no tenant data
        col.insert_one({"marker": marker})
        got = col.find_one({"marker": marker})
        col.delete_one({"marker": marker})
        if not got:
            raise RuntimeError("document written but not read back")
        print("collections:", db.list_collection_names())
        print(f"PASS: wrote and read a document in db {db.name!r}")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {str(e)[:300]}")
        sys.exit(1)


main()
