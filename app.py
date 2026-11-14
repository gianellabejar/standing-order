"""Vercel entrypoint (zero-config FastAPI) and the one combined app: SMS ingest + voice webhook.
Local: uvicorn app:app --port 8000"""
import logging

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402

from agent.app import router as voice_router  # noqa: E402
from ingest import routes as ingest_routes  # noqa: E402
from ingest import sms  # noqa: E402

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Order Desk")
app.include_router(sms.router)
app.include_router(ingest_routes.router)
app.include_router(voice_router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
