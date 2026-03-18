"""
Aadhaar OCR service — dual provider:
  1. Primary: AZAPI.ai (production-grade Aadhaar-specific API)
  2. Fallback: pytesseract + OpenCV (local, no network dependency)

Returns structured AadhaarData with confidence scores.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime

import cv2
import httpx
import numpy as np
import pytesseract
import structlog

# Set Tesseract path for Windows
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

from app.models import AadhaarData, Gender
from app.utils import sanitize_name_for_tally, validate_aadhaar_checksum
from config import get_settings

log = structlog.get_logger()


async def extract_aadhaar(image_data: bytes) -> AadhaarData:
    """
    Main entry point. Routes to configured provider.
    Raises ValueError if OCR completely fails.
    """
    s = get_settings()
    if s.ocr_provider == "mock":
        log.info("using_mock_ocr")
        return AadhaarData(
            full_name="Rahil Mavani",
            aadhaar_number="499118665246",
            dob=date(1995, 1, 15),
            gender=Gender.MALE,
            address="123 Test Street, Ahmedabad, Gujarat",
            name_confidence=99.0,
            aadhaar_confidence=99.0,
        )
    if s.ocr_provider == "azapi":
        return await _ocr_via_azapi(image_data)
    return await _ocr_via_tesseract(image_data)


# ---------- Image Preprocessing ----------

def _auto_crop_card(image_data: bytes) -> bytes:
    """
    Detect and crop the card from the image, removing background.
    Uses edge detection to find the largest rectangular contour.
    Returns cropped JPEG bytes, or original if detection fails.
    """
    try:
        arr = np.frombuffer(image_data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return image_data

        h, w = img.shape[:2]
        orig_area = h * w

        # Resize for faster processing (keep aspect ratio)
        scale = 1.0
        if max(h, w) > 1000:
            scale = 1000 / max(h, w)
            small = cv2.resize(img, None, fx=scale, fy=scale)
        else:
            small = img.copy()

        # Preprocessing: grayscale → blur → edge detection
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edged = cv2.Canny(blurred, 30, 100)

        # Dilate to close gaps in edges
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        edged = cv2.dilate(edged, kernel, iterations=2)

        # Find contours
        contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return image_data

        # Sort by area, largest first
        contours = sorted(contours, key=cv2.contourArea, reverse=True)

        card_contour = None
        for cnt in contours[:5]:
            peri = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
            # Card should be roughly rectangular (4 corners)
            if len(approx) == 4:
                area = cv2.contourArea(approx)
                # Card should be at least 10% of image area
                small_area = small.shape[0] * small.shape[1]
                if area > small_area * 0.1:
                    card_contour = approx
                    break

        if card_contour is None:
            # No rectangle found — try bounding box of largest contour
            cnt = contours[0]
            area = cv2.contourArea(cnt)
            small_area = small.shape[0] * small.shape[1]
            if area < small_area * 0.1:
                return image_data
            x, y, cw, ch = cv2.boundingRect(cnt)
            # Scale back to original image coordinates
            x, y, cw, ch = int(x/scale), int(y/scale), int(cw/scale), int(ch/scale)
            # Add small padding
            pad = 10
            x, y = max(0, x - pad), max(0, y - pad)
            cw, ch = min(w - x, cw + 2*pad), min(h - y, ch + 2*pad)
            cropped = img[y:y+ch, x:x+cw]
        else:
            # Perspective transform for 4-point crop
            pts = card_contour.reshape(4, 2).astype(np.float32) / scale
            # Order points: top-left, top-right, bottom-right, bottom-left
            rect = _order_points(pts)
            width = int(max(np.linalg.norm(rect[0] - rect[1]), np.linalg.norm(rect[2] - rect[3])))
            height = int(max(np.linalg.norm(rect[0] - rect[3]), np.linalg.norm(rect[1] - rect[2])))
            dst = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
            matrix = cv2.getPerspectiveTransform(rect, dst)
            cropped = cv2.warpPerspective(img, matrix, (width, height))

        # Encode back to JPEG
        _, buf = cv2.imencode(".jpg", cropped, [cv2.IMWRITE_JPEG_QUALITY, 95])
        log.info("card_auto_cropped", original_size=len(image_data), cropped_size=len(buf))
        return buf.tobytes()

    except Exception as exc:
        log.warning("auto_crop_failed", error=str(exc))
        return image_data


def _order_points(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as: top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]   # top-left has smallest sum
    rect[2] = pts[np.argmax(s)]   # bottom-right has largest sum
    d = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(d)]   # top-right has smallest difference
    rect[3] = pts[np.argmax(d)]   # bottom-left has largest difference
    return rect


# ---------- AZAPI.ai Provider ----------

async def _ocr_via_azapi(image_data: bytes) -> AadhaarData:
    """
    POST image to AZAPI.ai Aadhaar OCR endpoint.
    Expected response:
    {
      "name": "...", "aadhaar_number": "...",
      "dob": "...", "gender": "...", "address": "...",
      "confidence": { "name": 98.5, "aadhaar_number": 99.1 }
    }
    """
    s = get_settings()

    # Auto-crop card before sending to AZAPI
    image_data = _auto_crop_card(image_data)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                s.azapi_endpoint,
                headers={"Authorization": s.azapi_api_key},
                files={
                    "front": ("aadhaar.jpg", image_data, "image/jpeg"),
                },
            )
            data = resp.json()
            log.info("azapi_response", status=resp.status_code, data=data)
            if resp.status_code != 200 or data.get("status") == "Failed":
                log.error("azapi_request_failed", status=resp.status_code, message=data.get("message", ""))
                log.info("falling_back_to_tesseract")
                return await _ocr_via_tesseract(image_data)
        except httpx.HTTPError as exc:
            log.error("azapi_request_failed", error=str(exc))
            log.info("falling_back_to_tesseract")
            return await _ocr_via_tesseract(image_data)

    # AZAPI nests extracted fields under "output" with "id_" prefix
    output = data.get("output", {}) or {}
    raw_name = (
        output.get("id_name", "")
        or data.get("name", "")
        or data.get("full_name", "")
    )
    raw_aadhaar = (
        output.get("id_number", "")
        or data.get("aadhaar_number", "")
        or data.get("uid", "")
    )
    raw_aadhaar = re.sub(r"\D", "", raw_aadhaar)  # strip non-digits

    confidence = data.get("confidence", {})
    name_conf = float(confidence.get("name", 99.0 if raw_name else 0))
    aadhaar_conf = float(confidence.get("aadhaar_number", 99.0 if raw_aadhaar else 0))

    # Parse DOB
    dob = _parse_dob(
        output.get("id_dob")
        or data.get("dob")
        or data.get("date_of_birth")
    )

    # Parse gender
    gender = _parse_gender(output.get("id_gender") or data.get("gender"))

    cleaned_name = sanitize_name_for_tally(raw_name)

    # Extract village from AZAPI output or parse from address
    raw_address = output.get("id_address") or data.get("address") or ""
    village = _extract_village(raw_address)

    if not cleaned_name or len(raw_aadhaar) != 12:
        raise ValueError(
            f"OCR incomplete: name='{cleaned_name}', aadhaar_len={len(raw_aadhaar)}"
        )

    return AadhaarData(
        full_name=cleaned_name,
        aadhaar_number=raw_aadhaar,
        date_of_birth=dob,
        gender=gender,
        address=raw_address or None,
        village=village,
        name_confidence=name_conf,
        aadhaar_confidence=aadhaar_conf,
        raw_ocr_response=data,
    )


# ---------- Tesseract + OpenCV Fallback ----------

async def _ocr_via_tesseract(image_data: bytes) -> AadhaarData:
    """
    Local OCR using pytesseract + OpenCV preprocessing.
    Optimized for Indian Aadhaar card layout:
    - Grayscale + adaptive threshold for low contrast
    - Deskew for angled photos
    - Regex extraction of name, aadhaar number, DOB, gender
    """
    import asyncio
    # Heavy imports only when needed — saves startup RAM if using AZAPI
    import cv2
    import numpy as np
    import pytesseract

    log.info("tesseract_ocr_start")

    def _process() -> AadhaarData:
        # Decode image
        nparr = np.frombuffer(image_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Cannot decode image")

        # Preprocessing pipeline for Aadhaar cards
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Adaptive threshold handles varying lighting
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        # Denoise
        denoised = cv2.medianBlur(thresh, 3)
        # Slight sharpen
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
        sharpened = cv2.filter2D(denoised, -1, kernel)

        # OCR with Indian language support
        text = pytesseract.image_to_string(sharpened, lang="eng", config="--psm 6")
        # Also try with different page segmentation for robustness
        text_alt = pytesseract.image_to_string(sharpened, lang="eng", config="--psm 4")
        full_text = text + "\n" + text_alt

        log.debug("tesseract_raw_output", text_length=len(full_text))

        # Extract Aadhaar number (12 digits, possibly with spaces)
        aadhaar_match = re.search(r"\b(\d{4}\s?\d{4}\s?\d{4})\b", full_text)
        raw_aadhaar = re.sub(r"\s", "", aadhaar_match.group(1)) if aadhaar_match else ""

        # Extract name — typically the line above the Aadhaar number
        # or after specific keywords
        name = _extract_name_from_text(full_text)

        # Extract DOB
        dob = _extract_dob_from_text(full_text)

        # Extract gender
        gender = _extract_gender_from_text(full_text)

        # Extract address
        address = _extract_address_from_text(full_text)

        cleaned_name = sanitize_name_for_tally(name)

        # Confidence estimation based on pattern matching quality
        name_conf = 85.0 if cleaned_name and len(cleaned_name) > 3 else 40.0
        aadhaar_conf = 90.0 if len(raw_aadhaar) == 12 else 30.0

        # Boost confidence if Verhoeff checksum passes
        if validate_aadhaar_checksum(raw_aadhaar):
            aadhaar_conf = min(aadhaar_conf + 8.0, 100.0)

        if not cleaned_name or len(raw_aadhaar) != 12:
            raise ValueError(
                f"Tesseract OCR incomplete: name='{cleaned_name}', "
                f"aadhaar_len={len(raw_aadhaar)}"
            )

        return AadhaarData(
            full_name=cleaned_name,
            aadhaar_number=raw_aadhaar,
            date_of_birth=dob,
            gender=gender,
            address=address,
            name_confidence=name_conf,
            aadhaar_confidence=aadhaar_conf,
            raw_ocr_response={"raw_text": full_text[:500]},
        )

    # Run CPU-bound OCR in thread pool to avoid blocking event loop
    return await asyncio.get_event_loop().run_in_executor(None, _process)


# ---------- Text Extraction Helpers ----------

def _extract_name_from_text(text: str) -> str:
    """
    Extract name from Aadhaar OCR text.
    Aadhaar cards have name after keywords like 'Name', or as the
    first prominent English text line.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Strategy 1: Look for line after "Name" or "name" keyword
    for i, line in enumerate(lines):
        if re.search(r"\b(name|naam)\b", line, re.IGNORECASE):
            # Name could be on same line after colon
            after_colon = re.split(r"[:\-]", line, maxsplit=1)
            if len(after_colon) > 1 and after_colon[1].strip():
                candidate = after_colon[1].strip()
                if _is_likely_name(candidate):
                    return candidate
            # Or on the next line
            if i + 1 < len(lines) and _is_likely_name(lines[i + 1]):
                return lines[i + 1]

    # Strategy 2: First line that looks like a name (all alpha + spaces)
    for line in lines:
        if _is_likely_name(line) and len(line) > 3:
            return line

    return ""


def _is_likely_name(text: str) -> bool:
    """Heuristic: a name is 2+ words, all alphabetic, no digits."""
    cleaned = text.strip()
    if not cleaned or len(cleaned) < 3:
        return False
    # Must be mostly alphabetic
    alpha_ratio = sum(1 for c in cleaned if c.isalpha() or c.isspace()) / len(cleaned)
    if alpha_ratio < 0.8:
        return False
    # Should have at least 2 "words"
    words = cleaned.split()
    return len(words) >= 2


def _extract_dob_from_text(text: str) -> date | None:
    """Extract date of birth in DD/MM/YYYY or DD-MM-YYYY format."""
    patterns = [
        r"(\d{2})[/\-](\d{2})[/\-](\d{4})",
        r"DOB\s*[:\-]?\s*(\d{2})[/\-](\d{2})[/\-](\d{4})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            try:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                continue
    return None


def _extract_gender_from_text(text: str) -> Gender | None:
    """Extract gender from text."""
    upper = text.upper()
    if re.search(r"\bMALE\b", upper) and not re.search(r"\bFEMALE\b", upper):
        return Gender.MALE
    if re.search(r"\bFEMALE\b", upper):
        return Gender.FEMALE
    if re.search(r"\b(TRANSGENDER|OTHER)\b", upper):
        return Gender.OTHER
    return None


def _extract_address_from_text(text: str) -> str | None:
    """Extract address — typically after 'Address' keyword."""
    lines = text.split("\n")
    collecting = False
    addr_parts: list[str] = []

    for line in lines:
        stripped = line.strip()
        if re.search(r"\b(address|addr)\b", stripped, re.IGNORECASE):
            collecting = True
            after = re.split(r"[:\-]", stripped, maxsplit=1)
            if len(after) > 1 and after[1].strip():
                addr_parts.append(after[1].strip())
            continue
        if collecting:
            # Stop collecting at next keyword or empty line
            if not stripped or re.match(r"^\d{4}\s?\d{4}\s?\d{4}$", stripped):
                break
            addr_parts.append(stripped)

    return " ".join(addr_parts) if addr_parts else None


def _extract_village(address: str) -> str | None:
    """
    Extract village/town name from Aadhaar address.
    Typical format: "Village, Taluka, District, State - Pincode"
    The first comma-separated part is usually the village.
    """
    if not address:
        return None
    # Remove common prefixes like "S/O", "D/O", "W/O", "C/O" lines
    cleaned = re.sub(r"(?i)^[SDWC]/[Oo]\s+[^,]+,?\s*", "", address.strip())
    # Split by comma and take the first meaningful part
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if parts:
        # Skip parts that look like pincode or state
        for part in parts:
            if not re.match(r"^\d{6}$", part) and len(part) > 1:
                return part
    return None


def _parse_dob(raw: str | None) -> date | None:
    """Parse DOB from API response."""
    if not raw:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_gender(raw: str | None) -> Gender | None:
    """Parse gender from API response."""
    if not raw:
        return None
    upper = raw.upper().strip()
    if upper in ("M", "MALE"):
        return Gender.MALE
    if upper in ("F", "FEMALE"):
        return Gender.FEMALE
    return Gender.OTHER
