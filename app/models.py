"""
Pydantic models for request/response validation and internal data flow.
Strict types prevent runtime surprises.
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------- Aadhaar OCR Result ----------

class Gender(str, enum.Enum):
    MALE = "Male"
    FEMALE = "Female"
    OTHER = "Other"


class AadhaarData(BaseModel):
    """Validated Aadhaar data extracted from OCR."""
    full_name: str = Field(..., min_length=2, max_length=200)
    aadhaar_number: str = Field(..., pattern=r"^\d{12}$")
    date_of_birth: date | None = None
    gender: Gender | None = None
    address: str | None = None
    village: str | None = None
    name_confidence: float = Field(..., ge=0.0, le=100.0)
    aadhaar_confidence: float = Field(..., ge=0.0, le=100.0)
    raw_ocr_response: dict[str, Any] = Field(default_factory=dict)


class MaskedAadhaarData(BaseModel):
    """Compliance-safe version — first 8 digits masked."""
    full_name: str
    masked_aadhaar: str = Field(..., pattern=r"^XXXX-XXXX-\d{4}$")
    date_of_birth: date | None = None
    gender: Gender | None = None
    address: str | None = None


# ---------- Tally ----------

class VoucherType(str, enum.Enum):
    RECEIPT = "Receipt"
    PAYMENT = "Payment"


class TallyVoucher(BaseModel):
    """Data needed to generate a Tally voucher XML."""
    voucher_number: str
    voucher_type: VoucherType
    party_name: str
    contra_ledger: str
    amount: float = Field(..., ge=0.0)
    narration: str = ""
    voucher_date: date = Field(default_factory=date.today)
    guid: str = ""  # Duplicate prevention


class TallyResponse(BaseModel):
    """Parsed response from Tally HTTP server."""
    success: bool
    created: int = 0
    altered: int = 0
    errors: list[str] = Field(default_factory=list)
    raw_response: str = ""


# ---------- WhatsApp ----------

class WhatsAppMessage(BaseModel):
    """Incoming WhatsApp message metadata."""
    from_number: str
    message_id: str
    media_id: str | None = None
    media_type: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    text: str | None = None


# ---------- Pipeline Job ----------

class PipelineJob(BaseModel):
    """A unit of work flowing through the async pipeline."""
    job_id: str
    whatsapp_msg: WhatsAppMessage
    image_path: str | None = None
    aadhaar_data: AadhaarData | None = None
    masked_data: MaskedAadhaarData | None = None
    voucher: TallyVoucher | None = None
    tally_response: TallyResponse | None = None
    cheque_path: str | None = None
    status: str = "received"
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------- Audit Log ----------

class AuditRecord(BaseModel):
    """What gets stored in SQLite — no full Aadhaar ever."""
    job_id: str
    from_number: str
    masked_aadhaar: str
    party_name: str
    voucher_number: str
    voucher_type: str
    amount: float
    tally_success: bool
    cheque_generated: bool = False
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
