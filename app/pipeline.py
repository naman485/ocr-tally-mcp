"""
Async pipeline: WhatsApp message → OCR → Tally → Reply.

Uses asyncio.Queue for decoupled, non-blocking processing.
The webhook returns 200 immediately; actual work happens here.
Each step has independent error handling and retry logic.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date
from pathlib import Path

import structlog

from app.cheque_generator import generate_cheque_pdf
from app.models import (
    AuditRecord,
    MaskedAadhaarData,
    PipelineJob,
    TallyVoucher,
    VoucherType,
    WhatsAppMessage,
)
from app.ocr_service import extract_aadhaar
from app.tally_service import create_or_update_ledger, create_voucher
from app.utils import (
    generate_guid,
    generate_job_id,
    generate_voucher_number,
    insert_audit,
    mask_aadhaar,
)
from app.whatsapp_webhook import download_media, send_whatsapp_reply
from config import get_settings

log = structlog.get_logger()

# Global pipeline queue — bounded to prevent memory bloat
_queue: asyncio.Queue[PipelineJob] = asyncio.Queue(maxsize=100)
_worker_task: asyncio.Task | None = None


async def enqueue_message(msg_data: dict) -> str:
    """
    Create a PipelineJob from raw webhook data and enqueue it.
    Returns the job_id for tracking.
    """
    job_id = generate_job_id()
    msg = WhatsAppMessage(
        from_number=msg_data.get("from_number", ""),
        message_id=msg_data.get("message_id", ""),
        media_id=msg_data.get("media_id"),
        media_type=msg_data.get("media_type"),
        text=msg_data.get("text"),
    )
    job = PipelineJob(job_id=job_id, whatsapp_msg=msg)
    await _queue.put(job)
    log.info("job_enqueued", job_id=job_id, queue_size=_queue.qsize())
    return job_id


async def start_pipeline_worker() -> None:
    """Start the background worker that processes the queue."""
    global _worker_task
    _worker_task = asyncio.create_task(_worker_loop())
    log.info("pipeline_worker_started")


async def stop_pipeline_worker() -> None:
    """Graceful shutdown of the pipeline worker."""
    global _worker_task
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    log.info("pipeline_worker_stopped")


async def _worker_loop() -> None:
    """
    Main worker loop — pulls jobs from queue and processes them.
    Never crashes: each job is wrapped in try/except.
    """
    while True:
        try:
            job = await _queue.get()
            start = time.monotonic()
            log.info("job_processing_start", job_id=job.job_id)

            await _process_job(job)

            elapsed_ms = (time.monotonic() - start) * 1000
            log.info(
                "job_processing_complete",
                job_id=job.job_id,
                elapsed_ms=round(elapsed_ms, 1),
                status=job.status,
            )
            _queue.task_done()

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("worker_loop_error", error=str(exc))


async def _process_job(job: PipelineJob) -> None:
    """
    Full pipeline for a single job:
    1. Check if image message
    2. Download media
    3. OCR
    4. Validate confidence
    5. Create Tally ledger
    6. Create Tally voucher
    7. Generate cheque PDF (optional)
    8. Reply to user
    9. Audit log
    """
    s = get_settings()
    msg = job.whatsapp_msg

    # Step 1: Must be an image message
    if not msg.media_id:
        if msg.text:
            await send_whatsapp_reply(
                msg.from_number,
                "Please send a photo of an Aadhaar card (front side with name and number visible).",
            )
        job.status = "skipped_no_image"
        return

    # Step 2: Download image
    job.status = "downloading"
    image_data = await download_media(msg.media_id)
    if not image_data:
        await send_whatsapp_reply(
            msg.from_number,
            "Could not download the image. Please resend.",
        )
        job.status = "error_download"
        job.error = "Media download failed"
        return

    # Step 3: OCR
    job.status = "ocr_processing"
    try:
        aadhaar_data = await extract_aadhaar(image_data)
        job.aadhaar_data = aadhaar_data
    except ValueError as exc:
        log.warning("ocr_extraction_failed", job_id=job.job_id, error=str(exc))
        await send_whatsapp_reply(
            msg.from_number,
            "Could not read Aadhaar details from the image. "
            "Please resend a clearer photo with name and number fully visible.",
        )
        job.status = "error_ocr"
        job.error = str(exc)
        return

    # Step 4: Confidence check
    if (
        aadhaar_data.name_confidence < s.ocr_min_confidence
        or aadhaar_data.aadhaar_confidence < s.ocr_min_confidence
    ):
        log.warning(
            "low_confidence",
            job_id=job.job_id,
            name_conf=aadhaar_data.name_confidence,
            aadhaar_conf=aadhaar_data.aadhaar_confidence,
        )
        await send_whatsapp_reply(
            msg.from_number,
            f"Image quality too low (confidence: name={aadhaar_data.name_confidence:.0f}%, "
            f"number={aadhaar_data.aadhaar_confidence:.0f}%). "
            "Please resend a clearer photo.",
        )
        job.status = "error_low_confidence"
        job.error = "Low OCR confidence"
        return

    # Aadhaar checksum validation
    from app.utils import validate_aadhaar_checksum
    if not validate_aadhaar_checksum(aadhaar_data.aadhaar_number):
        log.warning("aadhaar_checksum_fail", job_id=job.job_id)
        await send_whatsapp_reply(
            msg.from_number,
            "Aadhaar number checksum validation failed. Please resend a clearer image.",
        )
        job.status = "error_checksum"
        job.error = "Aadhaar checksum failed"
        return

    # Create masked version for storage
    masked = mask_aadhaar(aadhaar_data.aadhaar_number)
    job.masked_data = MaskedAadhaarData(
        full_name=aadhaar_data.full_name,
        masked_aadhaar=masked,
        date_of_birth=aadhaar_data.date_of_birth,
        gender=aadhaar_data.gender,
        address=aadhaar_data.address,
    )

    # Step 5: Create/update Tally ledger
    job.status = "tally_ledger"
    ledger_result = await create_or_update_ledger(aadhaar_data.full_name)
    if not ledger_result.success:
        log.warning(
            "tally_ledger_failed",
            job_id=job.job_id,
            errors=ledger_result.errors,
        )
        # Non-fatal: proceed to voucher creation (ledger may already exist)

    # Step 6: Create voucher
    job.status = "tally_voucher"
    voucher_number = generate_voucher_number()
    voucher = TallyVoucher(
        voucher_number=voucher_number,
        voucher_type=VoucherType(s.tally_voucher_type),
        party_name=aadhaar_data.full_name,
        contra_ledger=s.tally_contra_ledger,
        amount=s.tally_default_amount,
        narration=f"Auto-created from Aadhaar via WhatsApp. Masked: {masked}",
        voucher_date=date.today(),
        guid=generate_guid(),
    )
    job.voucher = voucher

    tally_result = await create_voucher(voucher)
    job.tally_response = tally_result

    # Step 7: Generate cheque PDF (best-effort, non-blocking)
    cheque_path = None
    try:
        cheque_path = await asyncio.get_event_loop().run_in_executor(
            None,
            generate_cheque_pdf,
            aadhaar_data.full_name,
            s.tally_default_amount,
            date.today(),
            None,
        )
        job.cheque_path = cheque_path
    except Exception as exc:
        log.warning("cheque_generation_failed", job_id=job.job_id, error=str(exc))

    # Step 8: Reply to user
    if tally_result.success:
        reply = (
            f"Aadhaar processed.\n"
            f"Name: {aadhaar_data.full_name}\n"
            f"Voucher {voucher_number} created in Tally.\n"
            f"Cheque name updated."
        )
        job.status = "completed"
    else:
        reply = (
            f"Aadhaar read successfully.\n"
            f"Name: {aadhaar_data.full_name}\n"
            f"Tally import pending — errors: {'; '.join(tally_result.errors[:2])}\n"
            f"Our team will process manually."
        )
        job.status = "partial_tally_error"
        job.error = "; ".join(tally_result.errors[:3])

    await send_whatsapp_reply(msg.from_number, reply)

    # Step 9: Audit log (never stores full Aadhaar)
    audit = AuditRecord(
        job_id=job.job_id,
        from_number=msg.from_number,
        masked_aadhaar=masked,
        party_name=aadhaar_data.full_name,
        voucher_number=voucher_number,
        voucher_type=s.tally_voucher_type,
        amount=s.tally_default_amount,
        tally_success=tally_result.success,
        cheque_generated=cheque_path is not None,
        error=job.error,
    )
    try:
        await insert_audit(s.db_path, audit)
    except Exception as exc:
        log.error("audit_insert_failed", job_id=job.job_id, error=str(exc))
