"""HTTP trigger for one poll pass. Vercel cron calls GET with `Authorization: Bearer $CRON_SECRET`."""
import os

from fastapi import APIRouter, Header, HTTPException

from ingest import poller

router = APIRouter()


@router.get("/ingest/poll", operation_id="ingest_poll_get")
@router.post("/ingest/poll", operation_id="ingest_poll_post")
def poll(authorization: str = Header(default="")) -> dict:
    secret = os.environ.get("CRON_SECRET")
    if secret:
        if authorization != f"Bearer {secret}":
            raise HTTPException(status_code=401, detail="bad or missing CRON_SECRET")
    elif os.environ.get("VERCEL"):  # never leave the trigger open on a deployment
        raise HTTPException(status_code=403, detail="set CRON_SECRET to enable /ingest/poll")
    return poller.poll_once()
