"""
Centralized configuration — all env vars loaded once at startup.
Uses pydantic-settings for validation + type coercion.
No secrets ever appear in logs or error messages.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- WhatsApp Business Cloud API ---
    whatsapp_verify_token: str = "default_verify_token"
    whatsapp_api_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_version: str = "v21.0"

    # --- OCR ---
    azapi_api_key: str = ""
    azapi_endpoint: str = "https://api.azapi.ai/v1/aadhaar-ocr"
    ocr_provider: str = "azapi"  # "azapi" | "tesseract"
    ocr_min_confidence: int = 95

    # --- Tally Prime ---
    tally_host: str = "http://localhost"
    tally_port: int = 9000
    tally_company_name: str = "My Company"
    tally_voucher_type: str = "Receipt"  # "Receipt" | "Payment"
    tally_ledger_group: str = "Sundry Debtors"
    tally_contra_ledger: str = "Cash"
    tally_default_amount: float = 0.00

    # --- Cheque PDF ---
    cheque_output_dir: str = "./cheques"
    cheque_bank_name: str = "State Bank of India"
    cheque_account_number: str = ""

    # --- Database ---
    db_path: str = "./audit.db"

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # --- Security ---
    rate_limit_per_minute: int = 30

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def tally_url(self) -> str:
        if self.tally_host.startswith("https://"):
            return self.tally_host
        return f"{self.tally_host}:{self.tally_port}"

    @property
    def whatsapp_api_base(self) -> str:
        return f"https://graph.facebook.com/{self.whatsapp_api_version}"

    @property
    def cheque_dir(self) -> Path:
        p = Path(self.cheque_output_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings — loaded once, cached forever."""
    return Settings()
