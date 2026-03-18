# LLM Context — WhatsApp Aadhaar OCR → Tally Prime ERP

## Project Purpose
Automated pipeline: WhatsApp photo of Aadhaar card → OCR extracts identity → creates Tally Prime ledger/voucher → generates CTS-2010 cheque PDF → sends WhatsApp confirmation. Target <5s end-to-end.

## Tech Stack
- Python 3.12, FastAPI + Uvicorn (async)
- httpx (async HTTP with connection pooling)
- AZAPI.ai (primary OCR, endpoint: `https://ocr.azapi.ai/ind0001d`)
- Tesseract + OpenCV (fallback OCR)
- Tally Prime via XML over HTTP (localhost:9000)
- ReportLab (cheque PDF generation)
- SQLite via aiosqlite (audit log)
- Pydantic + pydantic-settings (config/validation)
- structlog (JSON structured logging)
- Docker Compose (app + tally-simulator)

## File Structure & Roles

### `main.py` (161 lines)
FastAPI app entry point. Lifespan manager (init DB → start pipeline worker → shutdown). Request logging middleware. Per-IP rate limiting (30 req/min, in-memory sliding window). Endpoints: `GET /` (info), `GET /health`, `GET/POST /webhook`.

### `config.py` (75 lines)
Pydantic `Settings` class loading from `.env`. LRU-cached singleton via `get_settings()`. Computed properties: `tally_url`, `whatsapp_api_base`, `cheque_dir`. All secrets from env vars, never hardcoded.

### `app/models.py` (115 lines)
Pydantic models:
- `AadhaarData` — full OCR result (name, aadhaar_number, dob, gender, address, confidence scores)
- `MaskedAadhaarData` — UIDAI compliant (XXXX-XXXX-LastFour)
- `TallyVoucher` — voucher creation params (amount, party, contra, type)
- `TallyResponse` — parsed Tally XML response (success, created/altered counts, errors)
- `WhatsAppMessage` — incoming message metadata (from_number, media_id, text)
- `PipelineJob` — state container tracking job through all stages
- `AuditRecord` — database record (never stores full Aadhaar)
- Enums: `Gender` (Male/Female/Other), `VoucherType` (Receipt/Payment)

### `app/whatsapp_webhook.py` (218 lines)
WhatsApp Business Cloud API integration:
- `verify_webhook()` — GET handler for Meta webhook subscription (challenge-response)
- `receive_message()` — POST handler, extracts messages, enqueues to pipeline
- `_extract_messages()` — parses deeply nested Meta payload → flat dicts with from_number, media_id, text
- `download_media()` — 2-step: GET media_id → signed URL → GET binary. 3x retry with exponential backoff
- `send_whatsapp_reply()` — sends text reply, 3x retry
- `_get_client()` — reusable httpx.AsyncClient with Bearer token and connection pooling

### `app/ocr_service.py` (~450 lines)
Aadhaar OCR with dual-provider + auto-crop:
- `extract_aadhaar()` — routes to mock/azapi/tesseract based on `OCR_PROVIDER` env var
- `_auto_crop_card()` — OpenCV preprocessing: edge detection → find largest rectangular contour → perspective transform crop. Removes background/edges before sending to AZAPI
- `_order_points()` — orders 4 corner points for perspective warp
- `_ocr_via_azapi()` — auto-crops image, POSTs to `https://ocr.azapi.ai/ind0001d` with `Authorization` header and `front` form field. **AZAPI response format**: `data.output.id_name`, `data.output.id_number`, `data.output.id_dob`, `data.output.id_gender`, `data.output.id_address`
- `_ocr_via_tesseract()` — fallback: grayscale → adaptive threshold → denoise → sharpen → pytesseract → regex extraction
- Mock mode: returns hardcoded test data (name="Rahil Mavani", aadhaar="499118665246")
- Helpers: `_extract_name_from_text()`, `_extract_dob_from_text()`, `_extract_gender_from_text()`, `_extract_address_from_text()`, `_parse_dob()`, `_parse_gender()`
- Tesseract path hardcoded for Windows: `C:\Program Files\Tesseract-OCR\tesseract.exe`

### `app/pipeline.py` (290 lines)
Async pipeline orchestrator:
- `enqueue_message()` — creates PipelineJob, adds to asyncio.Queue (bounded at 100)
- `start_pipeline_worker()` / `stop_pipeline_worker()` — lifecycle management
- `_worker_loop()` — infinite loop pulling jobs, never crashes (each job wrapped in try/except)
- `_process_job()` — 9-step pipeline:
  1. Check if image (reply with prompt if text-only)
  2. Download media from WhatsApp
  3. OCR extraction (raises ValueError on failure)
  4. Confidence validation (reject if <95%)
  5. Aadhaar Verhoeff checksum validation
  6. Create/update Tally ledger (idempotent, non-fatal if exists)
  7. Create Tally voucher
  8. Generate cheque PDF (best-effort)
  9. Reply to user (success/error) + insert audit record

### `app/tally_service.py` (274 lines)
Tally Prime XML integration:
- `build_ledger_xml()` — creates ledger with REMOTEID for idempotency
- `build_voucher_xml()` — Receipt/Payment voucher. **Critical**: Receipt = party credited (+amount), contra debited (-amount). Payment = opposite. Sum must equal zero.
- `push_to_tally()` — POSTs XML to `http://localhost:9000`, 3x retry
- `_parse_tally_response()` — extracts CREATED/ALTERED counts and LINEERROR/ERROR from Tally XML
- `create_or_update_ledger()` — treats "already exists" error as success
- `create_voucher()` — wrapper around push_to_tally

### `app/cheque_generator.py` (196 lines)
CTS-2010 format cheque PDF (8.25" x 3.66"):
- `generate_cheque_pdf()` — ReportLab canvas with calibrated field positions: date, payee (ALL CAPS, exact Aadhaar name), amount in words/figures, account number, "A/C Payee Only" crossing
- `_amount_to_words()` — Indian English (Crore, Lakh, Thousand)
- Output: `{cheque_output_dir}/cheque_{safe_name}_{date}.pdf`

### `app/utils.py` (215 lines)
Shared helpers:
- **Logging**: structlog configured for console rendering with ISO timestamps
- **ID generation**: `generate_job_id()` (JOB-{12hex}), `generate_voucher_number()` (WA-{ts}-{6hex}), `generate_guid()` (UUID4)
- **Aadhaar validation**: `validate_aadhaar_checksum()` — Verhoeff algorithm (12 digits, last digit is check)
- **Masking**: `mask_aadhaar()` — '123456789012' → 'XXXX-XXXX-9012'
- **Sanitization**: `sanitize_name_for_tally()` (remove XML chars, title case), `sanitize_for_xml()` (escape &<>'")
- **Database**: `init_db()` (creates audit_log table with indexes), `insert_audit()`, `get_audit_by_job()`

## Data Flow
```
WhatsApp Image → POST /webhook → _extract_messages() → enqueue_message()
  → asyncio.Queue → _worker_loop() → _process_job():
    → download_media(media_id) [2-step signed URL]
    → _auto_crop_card(image_bytes) [OpenCV edge detection + perspective crop]
    → _ocr_via_azapi(cropped_bytes) [POST to ocr.azapi.ai/ind0001d]
       → response.output.{id_name, id_number, id_dob, id_gender}
    → validate confidence ≥ 95% + Verhoeff checksum
    → mask_aadhaar() [XXXX-XXXX-LastFour]
    → create_or_update_ledger() [XML POST to Tally:9000]
    → create_voucher() [XML POST to Tally:9000, REMOTEID for idempotency]
    → generate_cheque_pdf() [ReportLab, CTS-2010]
    → send_whatsapp_reply() [success/error message]
    → insert_audit() [SQLite, masked Aadhaar only]
```

## Key Architecture Decisions
- **Non-blocking webhook**: returns 200 immediately, processing via asyncio.Queue (Meta requires <15s)
- **Idempotent Tally ops**: UUID-based REMOTEID prevents duplicates on retry
- **Auto-crop before OCR**: OpenCV detects card edges, perspective-warps to flat rectangle, then sends to AZAPI
- **AZAPI field mapping**: response nests data under `output` with `id_` prefix (id_name, id_number, id_dob, id_gender, id_address)
- **Confidence defaults**: AZAPI doesn't return confidence scores, so code defaults to 99.0 if name/aadhaar present
- **Tesseract Windows path**: hardcoded at `C:\Program Files\Tesseract-OCR\tesseract.exe`
- **Mock OCR mode**: `OCR_PROVIDER=mock` returns test data for pipeline testing without real Aadhaar

## Environment Variables (from .env)
| Variable | Current Value | Purpose |
|---|---|---|
| `WHATSAPP_VERIFY_TOKEN` | user-chosen string | Webhook verification handshake |
| `WHATSAPP_API_TOKEN` | Meta system user token | Bearer auth for WhatsApp API |
| `WHATSAPP_PHONE_NUMBER_ID` | `1005106476021467` | WhatsApp Business phone ID |
| `WHATSAPP_API_VERSION` | `v21.0` | Meta Graph API version |
| `AZAPI_API_KEY` | `sand-...` (sandbox key) | AZAPI OCR auth |
| `AZAPI_ENDPOINT` | `https://ocr.azapi.ai/ind0001d` | Correct AZAPI endpoint |
| `OCR_PROVIDER` | `azapi` | Options: azapi, tesseract, mock |
| `OCR_MIN_CONFIDENCE` | `95` | Minimum confidence threshold % |
| `TALLY_HOST` | `http://localhost` | Tally Prime server |
| `TALLY_PORT` | `9000` | Tally HTTP port |
| `TALLY_COMPANY_NAME` | needs real value | Must match Tally exactly |
| `TALLY_VOUCHER_TYPE` | `Receipt` | Receipt or Payment |
| `TALLY_LEDGER_GROUP` | `Sundry Debtors` | Tally ledger group |
| `TALLY_CONTRA_LEDGER` | `Cash` | Contra account |
| `TALLY_DEFAULT_AMOUNT` | `0.00` | Default voucher amount |
| `CHEQUE_OUTPUT_DIR` | `./cheques` | PDF output directory |
| `CHEQUE_BANK_NAME` | `State Bank of India` | Printed on cheque |
| `CHEQUE_ACCOUNT_NUMBER` | needs real value | Printed on cheque |
| `DB_PATH` | `./audit.db` | SQLite audit log |
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |
| `RATE_LIMIT_PER_MINUTE` | `30` | Per-IP rate limit |

## Storage
- **Audit log**: `./audit.db` (SQLite) — job_id, masked_aadhaar, party_name, voucher_number, tally_success, cheque_generated
- **Cheque PDFs**: `./cheques/cheque_{name}_{date}.pdf`
- **Tally data**: lives in Tally Prime (not stored locally)
- **Images**: processed in memory, never persisted

## Known Issues & Session History
1. **AZAPI endpoint was wrong** — original code had `https://api.azapi.ai/v1/aadhaar-ocr` (404). Correct: `https://ocr.azapi.ai/ind0001d`
2. **AZAPI field mapping was wrong** — code looked for `data.name`, `data.aadhaar_number`. Correct: `data.output.id_name`, `data.output.id_number`
3. **AZAPI auth format** — no `Bearer` prefix, just raw key in `Authorization` header. Form field is `front` (not `image`)
4. **AZAPI rejects sample/fake Aadhaar cards** — returns "No Aadhaar Detected" for non-genuine documents
5. **Images with background/edges fail AZAPI** — solved by adding `_auto_crop_card()` preprocessing
6. **WhatsApp reply failed with 400** — needed system user token with `whatsapp_business_messaging` permission (temporary tokens don't have it)
7. **Tesseract not in PATH on Windows** — solved by hardcoding path in ocr_service.py
8. **Tesseract produces garbage on Aadhaar cards** — layout too complex, multiple languages. Not viable as primary OCR
9. **AZAPI doesn't return confidence scores** — code defaults to 99.0 when name/aadhaar are present
10. **Mock mode available** — set `OCR_PROVIDER=mock` for pipeline testing without real Aadhaar

## Testing
- 114 tests across 6 modules (all passing): test_whatsapp, test_ocr, test_pipeline, test_tally, test_cheque, test_utils
- Run: `pytest tests/ -v`
- Dependencies: `pip install -r requirements.txt`

## Deployment
- Local: `python main.py` (requires ngrok for WhatsApp webhook)
- Docker: `docker-compose up --build` (app on 8000 + tally-simulator on 9000)
- Webhook URL: `https://{ngrok-url}/webhook`
