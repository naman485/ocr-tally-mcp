# WhatsApp Aadhaar OCR → Tally Prime ERP

Automated pipeline: receive Aadhaar card photos via WhatsApp, extract name/number via OCR, create ledger + voucher in Tally Prime, and set correct cheque payee name — all in under 5 seconds.

## How It Works

```
WhatsApp Photo → Media Download → Aadhaar OCR → Validate
    → Create Tally Ledger → Create Voucher → Generate Cheque PDF
    → Reply on WhatsApp with confirmation
```

1. User sends Aadhaar card photo to your WhatsApp Business number
2. System downloads the image via Meta Cloud API
3. OCR extracts: Name, Aadhaar Number, DOB, Gender, Address
4. If confidence < 95%, replies "Please resend clearer photo"
5. Creates/updates ledger in Tally Prime with exact Aadhaar name
6. Creates Receipt/Payment voucher with correct party name
7. Generates cheque PDF with the exact name in "Pay to" field
8. Replies to user with voucher number confirmation
9. Stores only masked Aadhaar (XXXX-XXXX-1234) in audit log

## Quick Start

```bash
# 1. Clone and setup
git clone https://github.com/naman485/ocr-tally-mcp.git
cd ocr-tally-mcp
cp .env.example .env
# Edit .env with your API keys

# 2. Run with Docker (includes Tally simulator)
docker compose up --build

# 3. Or run locally
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `WHATSAPP_VERIFY_TOKEN` | Yes | Webhook verification token (you choose) |
| `WHATSAPP_API_TOKEN` | Yes | Meta Cloud API bearer token |
| `WHATSAPP_PHONE_NUMBER_ID` | Yes | Your WhatsApp phone number ID |
| `AZAPI_API_KEY` | Yes* | AZAPI.ai API key for Aadhaar OCR |
| `OCR_PROVIDER` | No | `azapi` (default) or `tesseract` |
| `OCR_MIN_CONFIDENCE` | No | Minimum confidence % (default: 95) |
| `TALLY_HOST` | No | Tally server host (default: http://localhost) |
| `TALLY_PORT` | No | Tally HTTP port (default: 9000) |
| `TALLY_COMPANY_NAME` | Yes | Exact company name in Tally |
| `TALLY_VOUCHER_TYPE` | No | `Receipt` (default) or `Payment` |
| `TALLY_LEDGER_GROUP` | No | Ledger group (default: Sundry Debtors) |
| `TALLY_CONTRA_LEDGER` | No | Contra ledger (default: Cash) |

*Not required if using `tesseract` provider.

## Tally Prime Configuration

1. Open Tally Prime → press `F12` (Configure)
2. Enable **Tally.NET Features** → set "Yes" for:
   - Accept External XML Messages: **Yes**
3. Go to **F12 → Connectivity** → ensure HTTP port is set to **9000**
4. Create the ledger group "Sundry Debtors" if it doesn't exist
5. Create the "Cash" or "Bank" ledger as your contra account

## WhatsApp Business Setup

1. Create a Meta Business account at [business.facebook.com](https://business.facebook.com)
2. Set up WhatsApp Business API in Meta Developer dashboard
3. Configure webhook URL: `https://your-domain.com/webhook`
4. Set verification token to match your `WHATSAPP_VERIFY_TOKEN`
5. Subscribe to `messages` webhook field

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info |
| GET | `/health` | Health check |
| GET | `/webhook` | WhatsApp verification |
| POST | `/webhook` | Incoming messages |

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

## Architecture

- **FastAPI** async server with Uvicorn
- **asyncio.Queue** decouples webhook response from processing
- **httpx** async HTTP client with connection pooling
- **structlog** structured JSON logging
- **SQLite** audit log (no full Aadhaar stored — UIDAI compliant)
- **ReportLab** PDF cheque generation

## Security

- Full Aadhaar number is never stored — only masked (XXXX-XXXX-1234)
- Verhoeff algorithm validates Aadhaar checksum
- Rate limiting per IP
- All secrets via environment variables
- XML injection prevention in Tally payloads
