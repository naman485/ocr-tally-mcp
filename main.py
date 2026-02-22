"""
FastAPI entry point — WhatsApp Aadhaar OCR → Tally Prime ERP.

Startup sequence:
1. Initialize structured logging
2. Create SQLite audit table
3. Start async pipeline worker
4. Mount webhook routes

Shutdown sequence:
1. Stop pipeline worker (drain queue)
2. Close HTTP clients
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.pipeline import start_pipeline_worker, stop_pipeline_worker
from app.utils import init_db
from app.whatsapp_webhook import close_client, router as webhook_router
from config import get_settings

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown lifecycle."""
    s = get_settings()
    log.info("app_starting", port=s.port, ocr_provider=s.ocr_provider)

    # Initialize database
    await init_db(s.db_path)

    # Start background pipeline worker
    await start_pipeline_worker()

    yield

    # Shutdown
    log.info("app_shutting_down")
    await stop_pipeline_worker()
    await close_client()
    log.info("app_shutdown_complete")


app = FastAPI(
    title="Aadhaar OCR → Tally ERP",
    description="WhatsApp-based Aadhaar card OCR with automatic Tally voucher creation",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow all origins (webhook + monitoring dashboards)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ---------- Request logging middleware ----------

@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    """Log every request with method, path, status, duration."""
    start = time.monotonic()
    response = await call_next(request)
    elapsed_ms = (time.monotonic() - start) * 1000
    log.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round(elapsed_ms, 1),
    )
    return response


# ---------- Rate limiting (simple in-memory) ----------

_rate_store: dict[str, list[float]] = {}


@app.middleware("http")
async def rate_limit(request: Request, call_next) -> Response:
    """Per-IP rate limiting to prevent abuse."""
    s = get_settings()
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = 60.0  # 1 minute

    # Clean old entries
    if client_ip in _rate_store:
        _rate_store[client_ip] = [
            t for t in _rate_store[client_ip] if now - t < window
        ]
    else:
        _rate_store[client_ip] = []

    if len(_rate_store[client_ip]) >= s.rate_limit_per_minute:
        log.warning("rate_limited", ip=client_ip)
        return Response(
            status_code=429,
            content='{"success":false,"error":{"code":"RATE_LIMITED","message":"Too many requests"}}',
            media_type="application/json",
        )

    _rate_store[client_ip].append(now)
    return await call_next(request)


# ---------- Routes ----------

app.include_router(webhook_router)


@app.get("/")
async def root():
    """Service info endpoint."""
    return {
        "name": "aadhaar-ocr-tally",
        "version": "1.0.0",
        "description": "WhatsApp Aadhaar OCR → Tally Prime auto-voucher ERP",
        "endpoints": [
            {"method": "GET", "path": "/webhook", "description": "WhatsApp webhook verification"},
            {"method": "POST", "path": "/webhook", "description": "Incoming WhatsApp messages"},
            {"method": "GET", "path": "/health", "description": "Health check"},
        ],
    }


@app.get("/health")
async def health():
    """Health check for monitoring."""
    return {
        "status": "ok",
        "version": "1.0.0",
        "pipeline_queue_size": 0,  # Would read from pipeline in production
    }


# ---------- Entry point ----------

if __name__ == "__main__":
    s = get_settings()
    uvicorn.run(
        "main:app",
        host=s.host,
        port=s.port,
        log_level=s.log_level,
        reload=False,
    )
