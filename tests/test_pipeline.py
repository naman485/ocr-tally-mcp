"""Tests for the async processing pipeline."""
from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from app.models import AadhaarData, Gender, WhatsAppMessage, PipelineJob
from app.pipeline import enqueue_message, _process_job
from app.utils import generate_job_id


class TestEnqueueMessage:
    @pytest.mark.asyncio
    async def test_enqueue_returns_job_id(self):
        msg_data = {
            "from_number": "919999888877",
            "message_id": "wamid.test123",
            "media_id": "media_123",
            "media_type": "image/jpeg",
        }
        job_id = await enqueue_message(msg_data)
        assert job_id.startswith("JOB-")


class TestProcessJob:
    @pytest.mark.asyncio
    async def test_text_message_replies_with_instructions(self):
        """Text messages should get a reply asking for an image."""
        msg = WhatsAppMessage(
            from_number="919999888877",
            message_id="wamid.text1",
            text="Hello",
        )
        job = PipelineJob(job_id=generate_job_id(), whatsapp_msg=msg)

        with patch("app.pipeline.send_whatsapp_reply", new_callable=AsyncMock) as mock_reply:
            await _process_job(job)
            mock_reply.assert_called_once()
            assert "photo" in mock_reply.call_args[0][1].lower()

        assert job.status == "skipped_no_image"

    @pytest.mark.asyncio
    async def test_download_failure_replies_error(self):
        """If media download fails, user gets an error reply."""
        msg = WhatsAppMessage(
            from_number="919999888877",
            message_id="wamid.img1",
            media_id="bad_media_id",
            media_type="image/jpeg",
        )
        job = PipelineJob(job_id=generate_job_id(), whatsapp_msg=msg)

        with patch("app.pipeline.download_media", new_callable=AsyncMock, return_value=None), \
             patch("app.pipeline.send_whatsapp_reply", new_callable=AsyncMock) as mock_reply:
            await _process_job(job)
            mock_reply.assert_called_once()
            assert "resend" in mock_reply.call_args[0][1].lower()

        assert job.status == "error_download"

    @pytest.mark.asyncio
    async def test_ocr_failure_replies_error(self):
        """If OCR fails to extract data, user gets a clear error."""
        msg = WhatsAppMessage(
            from_number="919999888877",
            message_id="wamid.img2",
            media_id="good_media_id",
            media_type="image/jpeg",
        )
        job = PipelineJob(job_id=generate_job_id(), whatsapp_msg=msg)

        with patch("app.pipeline.download_media", new_callable=AsyncMock, return_value=b"fake_image"), \
             patch("app.pipeline.extract_aadhaar", new_callable=AsyncMock, side_effect=ValueError("OCR failed")), \
             patch("app.pipeline.send_whatsapp_reply", new_callable=AsyncMock) as mock_reply:
            await _process_job(job)
            mock_reply.assert_called_once()
            assert "clearer" in mock_reply.call_args[0][1].lower()

        assert job.status == "error_ocr"

    @pytest.mark.asyncio
    async def test_low_confidence_replies_warning(self):
        """Low confidence OCR results prompt user to resend."""
        msg = WhatsAppMessage(
            from_number="919999888877",
            message_id="wamid.img3",
            media_id="media_id",
            media_type="image/jpeg",
        )
        job = PipelineJob(job_id=generate_job_id(), whatsapp_msg=msg)

        low_conf_data = AadhaarData(
            full_name="Rajesh Kumar",
            aadhaar_number="234567891234",
            name_confidence=70.0,  # Below threshold
            aadhaar_confidence=80.0,
        )

        with patch("app.pipeline.download_media", new_callable=AsyncMock, return_value=b"img"), \
             patch("app.pipeline.extract_aadhaar", new_callable=AsyncMock, return_value=low_conf_data), \
             patch("app.pipeline.send_whatsapp_reply", new_callable=AsyncMock) as mock_reply:
            await _process_job(job)
            mock_reply.assert_called_once()
            assert "quality" in mock_reply.call_args[0][1].lower()

        assert job.status == "error_low_confidence"

    @pytest.mark.asyncio
    async def test_successful_pipeline(self, tmp_db, monkeypatch):
        """Full happy path: image → OCR → Tally → reply."""
        monkeypatch.setenv("DB_PATH", tmp_db)
        from config import get_settings
        get_settings.cache_clear()

        from app.utils import init_db
        await init_db(tmp_db)

        from app.models import TallyResponse

        msg = WhatsAppMessage(
            from_number="919999888877",
            message_id="wamid.img4",
            media_id="media_id",
            media_type="image/jpeg",
        )
        job = PipelineJob(job_id=generate_job_id(), whatsapp_msg=msg)

        # Aadhaar number that passes Verhoeff (we mock the checksum check)
        good_data = AadhaarData(
            full_name="Rajesh Kumar",
            aadhaar_number="234567891234",
            name_confidence=99.0,
            aadhaar_confidence=99.0,
        )
        tally_ok = TallyResponse(success=True, created=1)

        with patch("app.pipeline.download_media", new_callable=AsyncMock, return_value=b"img"), \
             patch("app.pipeline.extract_aadhaar", new_callable=AsyncMock, return_value=good_data), \
             patch("app.utils.validate_aadhaar_checksum", return_value=True), \
             patch("app.pipeline.create_or_update_ledger", new_callable=AsyncMock, return_value=tally_ok), \
             patch("app.pipeline.create_voucher", new_callable=AsyncMock, return_value=tally_ok), \
             patch("app.pipeline.generate_cheque_pdf", return_value="/tmp/test.pdf"), \
             patch("app.pipeline.send_whatsapp_reply", new_callable=AsyncMock) as mock_reply:
            await _process_job(job)
            mock_reply.assert_called_once()
            reply_text = mock_reply.call_args[0][1]
            assert "Rajesh Kumar" in reply_text
            assert "Voucher" in reply_text

        assert job.status == "completed"
