"""
Indian bank cheque PDF generator using ReportLab.
Positions are calibrated for standard CTS-2010 cheque format (8.25" x 3.66").

The critical requirement: the "Pay to" name must be EXACTLY the Aadhaar name
with zero alteration — this is the whole point of the OCR→Tally→Cheque flow.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import structlog
from reportlab.lib.pagesizes import landscape
from reportlab.lib.units import inch, mm
from reportlab.pdfgen import canvas

from config import get_settings

log = structlog.get_logger()

# CTS-2010 standard cheque dimensions
CHEQUE_WIDTH = 8.25 * inch
CHEQUE_HEIGHT = 3.66 * inch

# Field positions (x, y from bottom-left, calibrated for standard CTS-2010)
POS_DATE = (6.8 * inch, 3.1 * inch)
POS_PAY_TO = (1.5 * inch, 2.5 * inch)
POS_AMOUNT_WORDS_LINE1 = (1.0 * inch, 2.1 * inch)
POS_AMOUNT_WORDS_LINE2 = (1.0 * inch, 1.8 * inch)
POS_AMOUNT_FIGURES = (6.5 * inch, 2.1 * inch)
POS_ACCOUNT_NO = (0.6 * inch, 0.6 * inch)


def generate_cheque_pdf(
    payee_name: str,
    amount: float,
    cheque_date: date | None = None,
    output_filename: str | None = None,
) -> str:
    """
    Generate a cheque PDF with the payee name from Aadhaar.

    Args:
        payee_name: EXACT name from Aadhaar OCR (never modify this)
        amount: Amount in INR
        cheque_date: Date to print; defaults to today
        output_filename: Custom filename; auto-generated if None

    Returns:
        Path to the generated PDF file.
    """
    s = get_settings()
    cheque_date = cheque_date or date.today()

    if not output_filename:
        safe_name = payee_name.replace(" ", "_")[:30]
        output_filename = f"cheque_{safe_name}_{cheque_date.isoformat()}.pdf"

    output_path = s.cheque_dir / output_filename

    try:
        c = canvas.Canvas(str(output_path), pagesize=(CHEQUE_WIDTH, CHEQUE_HEIGHT))

        # Light border for visual reference (won't print on actual cheque stock)
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.5)
        c.rect(2 * mm, 2 * mm, CHEQUE_WIDTH - 4 * mm, CHEQUE_HEIGHT - 4 * mm)

        # Bank name header
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(0.5 * inch, 3.3 * inch, s.cheque_bank_name)

        # Date (DD-MM-YYYY in separate boxes)
        c.setFont("Courier", 11)
        date_str = cheque_date.strftime("%d%m%Y")
        x_start = POS_DATE[0]
        for i, digit in enumerate(date_str):
            c.drawString(x_start + (i * 0.15 * inch), POS_DATE[1], digit)

        # Pay to line — CRITICAL: exact Aadhaar name, no modification
        c.setFont("Helvetica-Bold", 12)
        c.drawString(POS_PAY_TO[0], POS_PAY_TO[1], payee_name.upper())
        # "Pay" label
        c.setFont("Helvetica", 8)
        c.drawString(0.4 * inch, 2.5 * inch, "Pay")

        # Amount in words
        amount_words = _amount_to_words(amount)
        c.setFont("Helvetica", 10)
        # Split into two lines if too long
        if len(amount_words) > 55:
            line1 = amount_words[:55].rsplit(" ", 1)[0]
            line2 = amount_words[len(line1):].strip()
            c.drawString(POS_AMOUNT_WORDS_LINE1[0], POS_AMOUNT_WORDS_LINE1[1], line1)
            c.drawString(POS_AMOUNT_WORDS_LINE2[0], POS_AMOUNT_WORDS_LINE2[1], line2)
        else:
            c.drawString(POS_AMOUNT_WORDS_LINE1[0], POS_AMOUNT_WORDS_LINE1[1], amount_words)

        # Amount in figures (in the box)
        c.setFont("Courier-Bold", 12)
        amount_str = f"{amount:,.2f}"
        c.drawString(POS_AMOUNT_FIGURES[0], POS_AMOUNT_FIGURES[1], amount_str)
        # Rupee symbol
        c.setFont("Helvetica", 10)
        c.drawString(POS_AMOUNT_FIGURES[0] - 0.2 * inch, POS_AMOUNT_FIGURES[1], "Rs.")

        # Account number (bottom)
        if s.cheque_account_number:
            c.setFont("Courier", 8)
            c.drawString(POS_ACCOUNT_NO[0], POS_ACCOUNT_NO[1], f"A/C: {s.cheque_account_number}")

        # "A/C Payee Only" crossing lines
        c.setStrokeColorRGB(0, 0, 0)
        c.setLineWidth(1)
        c.line(0.3 * inch, 2.8 * inch, 0.3 * inch, 3.4 * inch)
        c.line(0.5 * inch, 2.8 * inch, 0.5 * inch, 3.4 * inch)
        c.setFont("Helvetica", 6)
        c.drawString(0.15 * inch, 3.0 * inch, "A/C Payee")

        c.save()

        log.info("cheque_pdf_generated", path=str(output_path), payee=payee_name)
        return str(output_path)

    except Exception as exc:
        log.error("cheque_pdf_error", error=str(exc), payee=payee_name)
        raise


# ---------- Indian Rupee Amount to Words ----------

_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
]
_TENS = [
    "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety",
]


def _amount_to_words(amount: float) -> str:
    """
    Convert INR amount to Indian English words.
    Uses Indian numbering: Lakh, Crore (not Million, Billion).
    E.g., 1234567.50 → "Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven Rupees and Fifty Paise Only"
    """
    if amount < 0:
        return "Invalid Amount"
    if amount == 0:
        return "Zero Rupees Only"

    rupees = int(amount)
    paise = round((amount - rupees) * 100)

    parts: list[str] = []

    if rupees >= 10_000_000:
        crore = rupees // 10_000_000
        parts.append(f"{_two_digit_words(crore)} Crore")
        rupees %= 10_000_000

    if rupees >= 100_000:
        lakh = rupees // 100_000
        parts.append(f"{_two_digit_words(lakh)} Lakh")
        rupees %= 100_000

    if rupees >= 1_000:
        thousand = rupees // 1_000
        parts.append(f"{_two_digit_words(thousand)} Thousand")
        rupees %= 1_000

    if rupees >= 100:
        hundred = rupees // 100
        parts.append(f"{_ONES[hundred]} Hundred")
        rupees %= 100

    if rupees > 0:
        parts.append(_two_digit_words(rupees))

    result = " ".join(parts) + " Rupees"

    if paise > 0:
        result += f" and {_two_digit_words(paise)} Paise"

    return result + " Only"


def _two_digit_words(n: int) -> str:
    """Convert 0-99 to words."""
    if n < 20:
        return _ONES[n]
    return f"{_TENS[n // 10]} {_ONES[n % 10]}".strip()
