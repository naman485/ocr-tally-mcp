"""
Shared pytest fixtures for all tests.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

# Set test env vars BEFORE importing app modules
os.environ.update({
    "WHATSAPP_VERIFY_TOKEN": "test_verify_token",
    "WHATSAPP_API_TOKEN": "test_api_token",
    "WHATSAPP_PHONE_NUMBER_ID": "123456789",
    "AZAPI_API_KEY": "test_azapi_key",
    "OCR_PROVIDER": "azapi",
    "OCR_MIN_CONFIDENCE": "95",
    "TALLY_HOST": "http://localhost",
    "TALLY_PORT": "9000",
    "TALLY_COMPANY_NAME": "Test Company",
    "TALLY_VOUCHER_TYPE": "Receipt",
    "TALLY_LEDGER_GROUP": "Sundry Debtors",
    "TALLY_CONTRA_LEDGER": "Cash",
    "TALLY_DEFAULT_AMOUNT": "1000.00",
    "DB_PATH": ":memory:",
    "RATE_LIMIT_PER_MINUTE": "1000",
})

from config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Clear the lru_cache between tests so env changes take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    """Temporary SQLite database path."""
    return str(tmp_path / "test_audit.db")


@pytest.fixture
def tmp_cheque_dir(tmp_path: Path) -> Path:
    """Temporary directory for cheque PDFs."""
    d = tmp_path / "cheques"
    d.mkdir()
    return d


@pytest.fixture
def sample_webhook_image_payload() -> dict:
    """Realistic WhatsApp webhook payload with an image message."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "BIZ_ACCOUNT_ID",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "919876543210",
                        "phone_number_id": "123456789",
                    },
                    "messages": [{
                        "from": "919999888877",
                        "id": "wamid.test123",
                        "timestamp": "1700000000",
                        "type": "image",
                        "image": {
                            "id": "media_id_12345",
                            "mime_type": "image/jpeg",
                            "sha256": "abc123",
                        },
                    }],
                },
                "field": "messages",
            }],
        }],
    }


@pytest.fixture
def sample_webhook_text_payload() -> dict:
    """WhatsApp webhook payload with a text message."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "BIZ_ACCOUNT_ID",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "919876543210",
                        "phone_number_id": "123456789",
                    },
                    "messages": [{
                        "from": "919999888877",
                        "id": "wamid.text456",
                        "timestamp": "1700000000",
                        "type": "text",
                        "text": {"body": "Hello, I want to send my Aadhaar"},
                    }],
                },
                "field": "messages",
            }],
        }],
    }


@pytest.fixture
def sample_aadhaar_data() -> dict:
    """Sample AZAPI response for a valid Aadhaar."""
    return {
        "name": "Rajesh Kumar Sharma",
        "aadhaar_number": "234567891234",
        "dob": "15/08/1990",
        "gender": "Male",
        "address": "123 MG Road, Bengaluru, Karnataka 560001",
        "confidence": {
            "name": 98.5,
            "aadhaar_number": 99.2,
        },
    }
