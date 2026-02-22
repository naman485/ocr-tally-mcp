"""Tests for Tally XML generation and response parsing."""
from __future__ import annotations

import re
from datetime import date

import httpx
import pytest
import pytest_asyncio

from app.models import TallyVoucher, VoucherType
from app.tally_service import (
    _parse_tally_response,
    build_ledger_xml,
    build_voucher_xml,
    push_to_tally,
    create_or_update_ledger,
    create_voucher,
)


# ---------- Ledger XML ----------

class TestBuildLedgerXml:
    def test_basic_ledger(self):
        xml = build_ledger_xml("Rajesh Kumar", group="Sundry Debtors", company="Test Co")
        assert "<LEDGER NAME=" in xml
        assert "Rajesh Kumar" in xml
        assert "<PARENT>Sundry Debtors</PARENT>" in xml
        assert "SVCURRENTCOMPANY" in xml
        assert "Test Co" in xml

    def test_xml_escaping_in_name(self):
        xml = build_ledger_xml("O'Brien & Sons", company="A & B Ltd")
        assert "O&apos;Brien &amp; Sons" in xml
        assert "A &amp; B Ltd" in xml

    def test_remoteid_set(self):
        xml = build_ledger_xml("TestName")
        assert "REMOTEID=" in xml

    def test_action_create(self):
        xml = build_ledger_xml("TestName")
        assert 'ACTION="Create"' in xml


# ---------- Voucher XML ----------

class TestBuildVoucherXml:
    def _make_voucher(self, vtype: VoucherType = VoucherType.RECEIPT) -> TallyVoucher:
        return TallyVoucher(
            voucher_number="WA-001",
            voucher_type=vtype,
            party_name="Rajesh Kumar",
            contra_ledger="Cash",
            amount=5000.00,
            narration="Test voucher",
            voucher_date=date(2026, 1, 15),
            guid="test-guid-123",
        )

    def test_receipt_voucher_structure(self):
        v = self._make_voucher(VoucherType.RECEIPT)
        xml = build_voucher_xml(v, company="Test Co")

        assert "Receipt" in xml
        assert "Rajesh Kumar" in xml
        assert "Cash" in xml
        assert "20260115" in xml
        assert "WA-001" in xml
        assert "Test voucher" in xml

    def test_receipt_amount_signs(self):
        """Receipt: party positive (credit), contra negative (debit). Sum = 0."""
        v = self._make_voucher(VoucherType.RECEIPT)
        xml = build_voucher_xml(v)

        # Extract all AMOUNT values
        amounts = re.findall(r"<AMOUNT>([-\d.]+)</AMOUNT>", xml)
        assert len(amounts) == 2
        total = sum(float(a) for a in amounts)
        assert total == pytest.approx(0.0), f"Amounts don't balance: {amounts}"

        # Party amount should be positive (credit)
        assert float(amounts[0]) == 5000.00
        # Contra amount should be negative (debit)
        assert float(amounts[1]) == -5000.00

    def test_payment_amount_signs(self):
        """Payment: party negative (debit), contra positive (credit). Sum = 0."""
        v = self._make_voucher(VoucherType.PAYMENT)
        xml = build_voucher_xml(v)

        amounts = re.findall(r"<AMOUNT>([-\d.]+)</AMOUNT>", xml)
        assert len(amounts) == 2
        total = sum(float(a) for a in amounts)
        assert total == pytest.approx(0.0), f"Amounts don't balance: {amounts}"

        # Party amount should be negative (debit)
        assert float(amounts[0]) == -5000.00
        # Contra amount should be positive (credit)
        assert float(amounts[1]) == 5000.00

    def test_remoteid_present(self):
        v = self._make_voucher()
        xml = build_voucher_xml(v)
        assert "REMOTEID=" in xml
        assert "test-guid-123" in xml

    def test_deemed_positive_receipt(self):
        """Receipt: contra is deemed positive (debit side), party is not."""
        v = self._make_voucher(VoucherType.RECEIPT)
        xml = build_voucher_xml(v)
        # Find all ISDEEMEDPOSITIVE values
        deemed = re.findall(r"<ISDEEMEDPOSITIVE>(Yes|No)</ISDEEMEDPOSITIVE>", xml)
        assert deemed == ["No", "Yes"]  # party=No, contra=Yes for Receipt

    def test_deemed_positive_payment(self):
        """Payment: party is deemed positive (debit side), contra is not."""
        v = self._make_voucher(VoucherType.PAYMENT)
        xml = build_voucher_xml(v)
        deemed = re.findall(r"<ISDEEMEDPOSITIVE>(Yes|No)</ISDEEMEDPOSITIVE>", xml)
        assert deemed == ["Yes", "No"]  # party=Yes, contra=No for Payment

    def test_zero_amount(self):
        v = self._make_voucher()
        v.amount = 0.0
        xml = build_voucher_xml(v)
        amounts = re.findall(r"<AMOUNT>([-\d.]+)</AMOUNT>", xml)
        assert all(float(a) == 0.0 for a in amounts)


# ---------- Response Parsing ----------

class TestParseTallyResponse:
    def test_success_response(self):
        xml = """
        <RESPONSE>
            <CREATED>1</CREATED>
            <ALTERED>0</ALTERED>
            <LASTVCHID>12345</LASTVCHID>
        </RESPONSE>
        """
        result = _parse_tally_response(xml)
        assert result.success is True
        assert result.created == 1
        assert result.altered == 0
        assert len(result.errors) == 0

    def test_altered_response(self):
        xml = "<RESPONSE><CREATED>0</CREATED><ALTERED>1</ALTERED></RESPONSE>"
        result = _parse_tally_response(xml)
        assert result.success is True
        assert result.altered == 1

    def test_error_response(self):
        xml = """
        <RESPONSE>
            <CREATED>0</CREATED>
            <ALTERED>0</ALTERED>
            <LINEERROR>Ledger 'Cash' not found</LINEERROR>
        </RESPONSE>
        """
        result = _parse_tally_response(xml)
        assert result.success is False
        assert len(result.errors) == 1
        assert "Cash" in result.errors[0]

    def test_multiple_errors(self):
        xml = """
        <RESPONSE>
            <CREATED>0</CREATED>
            <LINEERROR>Error 1</LINEERROR>
            <LINEERROR>Error 2</LINEERROR>
        </RESPONSE>
        """
        result = _parse_tally_response(xml)
        assert result.success is False
        assert len(result.errors) == 2

    def test_empty_response(self):
        result = _parse_tally_response("")
        assert result.success is False

    def test_generic_error_tag(self):
        xml = "<RESPONSE><ERROR>Something went wrong</ERROR></RESPONSE>"
        result = _parse_tally_response(xml)
        assert result.success is False
        assert "Something went wrong" in result.errors[0]


# ---------- Push to Tally (mocked) ----------

class TestPushToTally:
    @pytest.mark.asyncio
    async def test_connection_refused(self, monkeypatch):
        """Tally offline — should retry 3 times and return failure."""
        import app.tally_service as ts

        call_count = 0
        original_push = ts.push_to_tally

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("Connection refused")

        # Patch httpx.AsyncClient to raise connection error
        monkeypatch.setattr(
            httpx.AsyncClient, "post", mock_post
        )

        result = await push_to_tally("<ENVELOPE></ENVELOPE>")
        assert result.success is False
        assert "3 attempts" in result.errors[0]
