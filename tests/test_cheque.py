"""Tests for cheque PDF generation."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from app.cheque_generator import (
    _amount_to_words,
    _two_digit_words,
    generate_cheque_pdf,
)


# ---------- Amount to Words (Indian Numbering) ----------

class TestAmountToWords:
    def test_zero(self):
        assert _amount_to_words(0) == "Zero Rupees Only"

    def test_single_digit(self):
        assert _amount_to_words(5) == "Five Rupees Only"

    def test_teens(self):
        assert _amount_to_words(15) == "Fifteen Rupees Only"

    def test_tens(self):
        assert _amount_to_words(50) == "Fifty Rupees Only"

    def test_hundreds(self):
        assert _amount_to_words(500) == "Five Hundred Rupees Only"

    def test_thousands(self):
        assert _amount_to_words(5000) == "Five Thousand Rupees Only"

    def test_lakhs(self):
        result = _amount_to_words(150000)
        assert "Lakh" in result
        assert "Fifty Thousand" in result

    def test_crores(self):
        result = _amount_to_words(10000000)
        assert "One Crore" in result

    def test_with_paise(self):
        result = _amount_to_words(1000.50)
        assert "One Thousand Rupees" in result
        assert "Fifty Paise" in result

    def test_complex_amount(self):
        result = _amount_to_words(1234567.89)
        assert "Twelve Lakh" in result
        assert "Thirty Four Thousand" in result
        assert "Five Hundred" in result

    def test_negative(self):
        assert _amount_to_words(-100) == "Invalid Amount"


class TestTwoDigitWords:
    def test_zero(self):
        assert _two_digit_words(0) == ""

    def test_one(self):
        assert _two_digit_words(1) == "One"

    def test_twenty_one(self):
        assert _two_digit_words(21) == "Twenty One"

    def test_ninety_nine(self):
        assert _two_digit_words(99) == "Ninety Nine"


# ---------- PDF Generation ----------

class TestChequeGeneration:
    def test_generates_pdf(self, tmp_cheque_dir, monkeypatch):
        monkeypatch.setenv("CHEQUE_OUTPUT_DIR", str(tmp_cheque_dir))
        # Clear settings cache so new env is picked up
        from config import get_settings
        get_settings.cache_clear()

        path = generate_cheque_pdf(
            payee_name="Rajesh Kumar Sharma",
            amount=5000.00,
            cheque_date=date(2026, 1, 15),
            output_filename="test_cheque.pdf",
        )
        assert Path(path).exists()
        assert Path(path).stat().st_size > 0

    def test_payee_name_in_filename(self, tmp_cheque_dir, monkeypatch):
        monkeypatch.setenv("CHEQUE_OUTPUT_DIR", str(tmp_cheque_dir))
        from config import get_settings
        get_settings.cache_clear()

        path = generate_cheque_pdf(
            payee_name="Priya Devi",
            amount=1000.00,
        )
        assert "Priya_Devi" in path

    def test_zero_amount(self, tmp_cheque_dir, monkeypatch):
        monkeypatch.setenv("CHEQUE_OUTPUT_DIR", str(tmp_cheque_dir))
        from config import get_settings
        get_settings.cache_clear()

        path = generate_cheque_pdf(
            payee_name="Test Name",
            amount=0.0,
            output_filename="zero_cheque.pdf",
        )
        assert Path(path).exists()

    def test_large_amount(self, tmp_cheque_dir, monkeypatch):
        monkeypatch.setenv("CHEQUE_OUTPUT_DIR", str(tmp_cheque_dir))
        from config import get_settings
        get_settings.cache_clear()

        path = generate_cheque_pdf(
            payee_name="Large Amount Test",
            amount=99999999.99,
            output_filename="large_cheque.pdf",
        )
        assert Path(path).exists()
