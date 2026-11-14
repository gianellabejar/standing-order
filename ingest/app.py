"""Minimal app for the ingest channels. Run: uvicorn ingest.app:app --port 8000
The agent's main app can include the same router instead: app.include_router(ingest.sms.router)."""
import logging

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402

from ingest import routes, sms  # noqa: E402

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Order Desk ingest")
app.include_router(sms.router)
app.include_router(routes.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
