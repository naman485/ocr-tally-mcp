"""Tests for WhatsApp webhook verification, message parsing, and media download."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app
from app.whatsapp_webhook import _extract_messages


client = TestClient(app)


# ---------- Webhook Verification (GET) ----------

class TestWebhookVerification:
    def test_valid_verification(self):
        resp = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test_verify_token",
                "hub.challenge": "challenge_123",
            },
        )
        assert resp.status_code == 200
        assert resp.text == "challenge_123"

    def test_wrong_token(self):
        resp = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "challenge_123",
            },
        )
        assert resp.status_code == 403

    def test_missing_params(self):
        resp = client.get("/webhook")
        assert resp.status_code == 403

    def test_wrong_mode(self):
        resp = client.get(
            "/webhook",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": "test_verify_token",
                "hub.challenge": "challenge_123",
            },
        )
        assert resp.status_code == 403


# ---------- Message Parsing ----------

class TestMessageParsing:
    def test_parse_image_message(self, sample_webhook_image_payload):
        messages = _extract_messages(sample_webhook_image_payload)
        assert len(messages) == 1
        msg = messages[0]
        assert msg["from_number"] == "919999888877"
        assert msg["media_id"] == "media_id_12345"
        assert msg["media_type"] == "image/jpeg"

    def test_parse_text_message(self, sample_webhook_text_payload):
        messages = _extract_messages(sample_webhook_text_payload)
        assert len(messages) == 1
        msg = messages[0]
        assert msg["from_number"] == "919999888877"
        assert msg["text"] == "Hello, I want to send my Aadhaar"
        assert msg.get("media_id") is None

    def test_parse_empty_payload(self):
        messages = _extract_messages({})
        assert messages == []

    def test_parse_no_messages(self):
        payload = {"entry": [{"changes": [{"value": {"messages": []}}]}]}
        messages = _extract_messages(payload)
        assert messages == []

    def test_parse_malformed_payload(self):
        """Should not crash on unexpected structure."""
        messages = _extract_messages({"entry": [{"changes": [{}]}]})
        assert messages == []

    def test_parse_document_image(self):
        """Image sent as document attachment."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": "919999888877",
                            "id": "wamid.doc123",
                            "timestamp": "1700000000",
                            "type": "document",
                            "document": {
                                "id": "doc_media_id",
                                "mime_type": "image/png",
                            },
                        }],
                    },
                }],
            }],
        }
        messages = _extract_messages(payload)
        assert len(messages) == 1
        assert messages[0]["media_id"] == "doc_media_id"
        assert messages[0]["media_type"] == "image/png"


# ---------- Root & Health endpoints ----------

class TestEndpoints:
    def test_root(self):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "aadhaar-ocr-tally"
        assert "endpoints" in data

    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
