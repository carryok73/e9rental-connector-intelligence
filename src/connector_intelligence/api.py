import json

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .config import Settings
from .db import connect
from .query import answer
from .connectors.whatsapp import ingest_cloud_api_event, verify_signature, verify_token
from .exports import export_source

app = FastAPI(title="Connector Intelligence API", version="0.1.0")


class Question(BaseModel):
    question: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
def whatsapp_verify(mode: str = Query(default="", alias="hub.mode"), token: str = Query(default="", alias="hub.verify_token"), challenge: str = Query(default="", alias="hub.challenge")):
    settings = Settings()
    if mode == "subscribe" and verify_token(token, settings.whatsapp_verify_token):
        return PlainTextResponse(challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request):
    settings = Settings()
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(raw_body, signature, settings.whatsapp_app_secret):
        raise HTTPException(status_code=403, detail="Webhook signature validation failed")
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")
    settings.ensure_dirs()
    with connect(settings.database_path) as conn:
        accepted = ingest_cloud_api_event(conn, payload, settings.selected_whatsapp_groups(),
                                          settings.whatsapp_group_id_path)
        export_source(conn, "whatsapp", f"{settings.output_dir}/whatsapp_output.json",
                      selected_whatsapp_groups=settings.selected_whatsapp_groups())
    return {"accepted": True, "stored_messages": accepted}


@app.post("/query")
def query(body: Question):
    settings = Settings()
    with connect(settings.database_path) as conn:
        return answer(conn, body.question, settings.selected_whatsapp_groups())

