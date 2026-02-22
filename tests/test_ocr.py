"""Tests for OCR service — AZAPI response parsing and tesseract helpers."""
from __future__ import annotations

import pytest

from app.ocr_service import (
    _extract_address_from_text,
    _extract_dob_from_text,
    _extract_gender_from_text,
    _extract_name_from_text,
    _is_likely_name,
    _parse_dob,
    _parse_gender,
)
from app.models import Gender


# ---------- Name Extraction from OCR Text ----------

class TestNameExtraction:
    def test_name_after_keyword(self):
        text = "Government of India\nName: Rajesh Kumar Sharma\n1234 5678 9012"
        name = _extract_name_from_text(text)
        assert name == "Rajesh Kumar Sharma"

    def test_name_on_next_line(self):
        text = "Government of India\nName\nRajesh Kumar\n1234 5678 9012"
        name = _extract_name_from_text(text)
        assert name == "Rajesh Kumar"

    def test_name_with_colon(self):
        text = "Name: Priya Devi\nDOB: 01/01/1990"
        name = _extract_name_from_text(text)
        assert name == "Priya Devi"

    def test_name_fallback_first_line(self):
        """When no keyword found, picks first name-like line."""
        text = "Rajesh Kumar\n1234 5678 9012\nSome Address"
        name = _extract_name_from_text(text)
        assert name == "Rajesh Kumar"

    def test_empty_text(self):
        assert _extract_name_from_text("") == ""

    def test_no_name_found(self):
        assert _extract_name_from_text("12345\n67890\n!@#$%") == ""


class TestIsLikelyName:
    def test_valid_name(self):
        assert _is_likely_name("Rajesh Kumar") is True

    def test_single_word(self):
        assert _is_likely_name("Rajesh") is False

    def test_with_digits(self):
        assert _is_likely_name("Rajesh 123") is False

    def test_short(self):
        assert _is_likely_name("AB") is False

    def test_empty(self):
        assert _is_likely_name("") is False


# ---------- DOB Extraction ----------

class TestDobExtraction:
    def test_dd_mm_yyyy_slash(self):
        text = "DOB: 15/08/1990"
        dob = _extract_dob_from_text(text)
        assert dob is not None
        assert dob.day == 15
        assert dob.month == 8
        assert dob.year == 1990

    def test_dd_mm_yyyy_dash(self):
        text = "Date of Birth 01-12-1985"
        dob = _extract_dob_from_text(text)
        assert dob is not None
        assert dob.year == 1985

    def test_no_dob(self):
        assert _extract_dob_from_text("No date here") is None

    def test_invalid_date(self):
        assert _extract_dob_from_text("DOB: 32/13/2000") is None


class TestParseDob:
    def test_slash_format(self):
        dob = _parse_dob("15/08/1990")
        assert dob is not None
        assert dob.year == 1990

    def test_dash_format(self):
        dob = _parse_dob("15-08-1990")
        assert dob is not None

    def test_iso_format(self):
        dob = _parse_dob("1990-08-15")
        assert dob is not None

    def test_none(self):
        assert _parse_dob(None) is None

    def test_garbage(self):
        assert _parse_dob("not a date") is None


# ---------- Gender Extraction ----------

class TestGenderExtraction:
    def test_male(self):
        assert _extract_gender_from_text("Gender: MALE\n") == Gender.MALE

    def test_female(self):
        assert _extract_gender_from_text("FEMALE") == Gender.FEMALE

    def test_male_not_female(self):
        """'MALE' should not match inside 'FEMALE'."""
        assert _extract_gender_from_text("Gender: FEMALE") == Gender.FEMALE

    def test_no_gender(self):
        assert _extract_gender_from_text("Name: Rajesh") is None


class TestParseGender:
    def test_m(self):
        assert _parse_gender("M") == Gender.MALE

    def test_female(self):
        assert _parse_gender("Female") == Gender.FEMALE

    def test_none(self):
        assert _parse_gender(None) is None


# ---------- Address Extraction ----------

class TestAddressExtraction:
    def test_address_after_keyword(self):
        text = "Name: Test\nAddress: 123 MG Road\nBengaluru 560001\n1234 5678 9012"
        addr = _extract_address_from_text(text)
        assert addr is not None
        assert "MG Road" in addr
        assert "Bengaluru" in addr

    def test_no_address(self):
        assert _extract_address_from_text("No address here") is None

    def test_empty(self):
        assert _extract_address_from_text("") is None
