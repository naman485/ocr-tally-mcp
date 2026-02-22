"""
WhatsApp Business Cloud API webhook handler.
- GET  /webhook  → verification challenge
- POST /webhook  → incoming message processing
- Media download with signed URL
- Reply helper with retry
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Query, Request, Response

from config import get_settings

log = structlog.get_logger()
router = APIRouter()

# Reusable async client — connection pooling, 30s timeout
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        s = get_settings()
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={
                "Authorization": f"Bearer {s.whatsapp_api_token}",
                "Content-Type": "application/json",
            },
        )
    return _client


async def close_client() -> None:
    """Shutdown hook — close HTTP client."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()


# ---------- Webhook Verification (GET) ----------

@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
) -> Response:
    """
    Meta sends GET with hub.mode, hub.verify_token, hub.challenge.
    Return the challenge plaintext if token matches.
    """
    s = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == s.whatsapp_verify_token:
        log.info("webhook_verified")
        return Response(content=hub_challenge, media_type="text/plain")
    log.warning("webhook_verification_failed", mode=hub_mode)
    return Response(status_code=403, content="Forbidden")


# ---------- Incoming Message (POST) ----------

@router.post("/webhook")
async def receive_message(request: Request) -> dict[str, str]:
    """
    Receive incoming WhatsApp message.
    Returns 200 immediately (Meta requires <15s response).
    Actual processing happens via the async pipeline.
    """
    body = await request.json()
    log.debug("webhook_payload_received")

    messages = _extract_messages(body)
    if not messages:
        return {"status": "no_messages"}

    # Import here to avoid circular dependency
    from app.pipeline import enqueue_message

    for msg in messages:
        await enqueue_message(msg)
        log.info(
            "message_enqueued",
            from_number=msg.get("from_number"),
            has_media=bool(msg.get("media_id")),
        )

    return {"status": "ok"}


def _extract_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Parse Meta webhook payload into flat message dicts.
    Handles the deeply nested structure:
    entry[].changes[].value.messages[]
    """
    results: list[dict[str, Any]] = []
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for msg in messages:
                    parsed: dict[str, Any] = {
                        "from_number": msg.get("from", ""),
                        "message_id": msg.get("id", ""),
                        "timestamp": msg.get("timestamp", ""),
                    }
                    # Image message
                    if msg.get("type") == "image":
                        img = msg.get("image", {})
                        parsed["media_id"] = img.get("id")
                        parsed["media_type"] = img.get("mime_type", "image/jpeg")
                    # Text message
                    elif msg.get("type") == "text":
                        parsed["text"] = msg.get("text", {}).get("body", "")
                    # Document (photo sent as doc)
                    elif msg.get("type") == "document":
                        doc = msg.get("document", {})
                        mime = doc.get("mime_type", "")
                        if mime.startswith("image/"):
                            parsed["media_id"] = doc.get("id")
                            parsed["media_type"] = mime

                    results.append(parsed)
    except (KeyError, TypeError, IndexError) as exc:
        log.error("webhook_parse_error", error=str(exc))
    return results


# ---------- Media Download ----------

async def download_media(media_id: str) -> bytes | None:
    """
    Two-step media download:
    1. GET /{media_id} → returns signed URL
    2. GET signed URL → returns binary image data
    Retries 3 times with exponential backoff.
    """
    s = get_settings()
    client = _get_client()

    for attempt in range(3):
        try:
            # Step 1: Get signed URL
            url_resp = await client.get(
                f"{s.whatsapp_api_base}/{media_id}"
            )
            url_resp.raise_for_status()
            signed_url = url_resp.json().get("url")
            if not signed_url:
                log.error("media_no_url", media_id=media_id)
                return None

            # Step 2: Download binary
            img_resp = await client.get(signed_url)
            img_resp.raise_for_status()
            log.info("media_downloaded", media_id=media_id, size=len(img_resp.content))
            return img_resp.content

        except httpx.HTTPError as exc:
            wait = 2 ** attempt
            log.warning(
                "media_download_retry",
                media_id=media_id,
                attempt=attempt + 1,
                error=str(exc),
                wait_seconds=wait,
            )
            await asyncio.sleep(wait)

    log.error("media_download_failed", media_id=media_id)
    return None


# ---------- Send Reply ----------

async def send_whatsapp_reply(to_number: str, text: str) -> bool:
    """
    Send a text reply to the user. Retries 3 times.
    """
    s = get_settings()
    client = _get_client()
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text},
    }

    for attempt in range(3):
        try:
            resp = await client.post(
                f"{s.whatsapp_api_base}/{s.whatsapp_phone_number_id}/messages",
                json=payload,
            )
            resp.raise_for_status()
            log.info("whatsapp_reply_sent", to=to_number)
            return True
        except httpx.HTTPError as exc:
            wait = 2 ** attempt
            log.warning(
                "whatsapp_reply_retry",
                to=to_number,
                attempt=attempt + 1,
                error=str(exc),
            )
            await asyncio.sleep(wait)

    log.error("whatsapp_reply_failed", to=to_number)
    return False
