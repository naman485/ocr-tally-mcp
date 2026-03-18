"""
Tally Prime XML integration — ledger + voucher push over HTTP.

Tally accepts XML POSTed to http://{host}:{port} (default 9000).
Every XML must be wrapped in <ENVELOPE><HEADER>...<BODY><DATA><TALLYMESSAGE>...

Critical correctness rules:
1. Amount in ALLLEDGERENTRIES must be negative for debit, positive for credit.
2. LEDGERNAME must exactly match the ledger in Tally (case-sensitive).
3. REMOTEID prevents duplicate imports — always set it.
4. For Receipt voucher: party is credited, contra (Cash/Bank) is debited.
5. For Payment voucher: party is debited, contra is credited.
"""
from __future__ import annotations

import asyncio
from datetime import date

import httpx
import structlog

from app.models import TallyResponse, TallyVoucher, VoucherType
from app.utils import generate_guid, sanitize_for_xml
from config import get_settings

log = structlog.get_logger()


# ---------- Ledger Creation XML ----------

def build_ledger_xml(
    ledger_name: str,
    group: str = "Sundry Debtors",
    company: str = "",
) -> str:
    """
    Generate XML to create a ledger in Tally Prime.
    Uses REMOTEID for idempotency — re-posting won't create duplicates.
    """
    s = get_settings()
    company = company or s.tally_company_name
    safe_name = sanitize_for_xml(ledger_name)
    safe_group = sanitize_for_xml(group)
    safe_company = sanitize_for_xml(company)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <LEDGER NAME="{safe_name}" REMOTEID="WA-LEDGER-{safe_name}" ACTION="Create">
            <NAME.LIST>
              <NAME>{safe_name}</NAME>
            </NAME.LIST>
            <PARENT>{safe_group}</PARENT>
            <ISBILLWISEON>No</ISBILLWISEON>
            <AFFECTSSTOCK>No</AFFECTSSTOCK>
          </LEDGER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


# ---------- Voucher Creation XML ----------

def build_voucher_xml(voucher: TallyVoucher, company: str = "") -> str:
    """
    Generate XML for a Receipt or Payment voucher.

    Receipt: Credit party, Debit contra (Cash/Bank)
      - Party entry amount: positive (credit)
      - Contra entry amount: negative (debit)
    Payment: Debit party, Credit contra (Cash/Bank)
      - Party entry amount: negative (debit)
      - Contra entry amount: positive (credit)

    This ensures "totals do not match" errors never occur —
    the sum of all ALLLEDGERENTRIES amounts MUST equal zero.
    """
    s = get_settings()
    company = company or s.tally_company_name
    safe_company = sanitize_for_xml(company)
    safe_party = sanitize_for_xml(voucher.party_name)
    safe_contra = sanitize_for_xml(voucher.contra_ledger)
    safe_narration = sanitize_for_xml(voucher.narration)
    safe_vtype = sanitize_for_xml(voucher.voucher_type.value)
    guid = voucher.guid or generate_guid()

    # Format date as Tally expects: YYYYMMDD
    # TODO: Educational Mode only allows 1st, 2nd, 31st — pin to 1st for testing
    tally_date = voucher.voucher_date.strftime("%Y%m") + "01"

    # Compute financial year (Apr–Mar) that contains the voucher date
    fy_start_year = (
        voucher.voucher_date.year
        if voucher.voucher_date.month >= 4
        else voucher.voucher_date.year - 1
    )
    sv_from = f"{fy_start_year}0401"
    sv_to = f"{fy_start_year + 1}0331"

    # Amount signs — critical for zero-sum balance
    if voucher.voucher_type == VoucherType.RECEIPT:
        # Receipt: cash comes IN (debit cash = negative), party pays (credit party = positive)
        party_amount = f"{voucher.amount:.2f}"
        contra_amount = f"-{voucher.amount:.2f}"
    else:
        # Payment: cash goes OUT (credit cash = positive), party receives (debit party = negative)
        party_amount = f"-{voucher.amount:.2f}"
        contra_amount = f"{voucher.amount:.2f}"

    is_receipt = voucher.voucher_type == VoucherType.RECEIPT
    party_deemed = "No" if is_receipt else "Yes"
    contra_deemed = "Yes" if is_receipt else "No"

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<ENVELOPE>"
        "<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>"
        "<BODY><IMPORTDATA>"
        "<REQUESTDESC>"
        "<REPORTNAME>Vouchers</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{safe_company}</SVCURRENTCOMPANY>"
        f"<SVFROMDATE>{sv_from}</SVFROMDATE>"
        f"<SVTODATE>{sv_to}</SVTODATE>"
        "</STATICVARIABLES>"
        "</REQUESTDESC>"
        "<REQUESTDATA>"
        '<TALLYMESSAGE xmlns:UDF="TallyUDF">'
        f'<VOUCHER REMOTEID="WA-{guid}" VCHTYPE="{safe_vtype}" ACTION="Create">'
        f"<DATE>{tally_date}</DATE>"
        f"<VOUCHERTYPENAME>{safe_vtype}</VOUCHERTYPENAME>"
        f"<VOUCHERNUMBER>{sanitize_for_xml(voucher.voucher_number)}</VOUCHERNUMBER>"
        f"<NARRATION>{safe_narration}</NARRATION>"
        f"<PARTYLEDGERNAME>{safe_party}</PARTYLEDGERNAME>"
        f"<EFFECTIVEDATE>{tally_date}</EFFECTIVEDATE>"
        "<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{safe_party}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{party_deemed}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{party_amount}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
        "<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{safe_contra}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{contra_deemed}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{contra_amount}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
        "</VOUCHER>"
        "</TALLYMESSAGE>"
        "</REQUESTDATA>"
        "</IMPORTDATA></BODY>"
        "</ENVELOPE>"
    )
    log.debug("voucher_xml_payload", xml=xml[:800])
    return xml


# ---------- HTTP Push to Tally ----------

async def push_to_tally(xml_data: str) -> TallyResponse:
    """
    POST XML to Tally HTTP server.
    Retries 3 times with exponential backoff.
    Tally returns XML response with CREATED/ALTERED counts.
    """
    s = get_settings()
    url = s.tally_url

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    content=xml_data,
                    headers={"Content-Type": "application/xml"},
                )
                body = resp.text
                log.debug("tally_raw_response", status=resp.status_code, body=body[:500])
                return _parse_tally_response(body)

        except httpx.HTTPError as exc:
            wait = 2 ** attempt
            log.warning(
                "tally_push_retry",
                attempt=attempt + 1,
                error=str(exc),
                wait_seconds=wait,
            )
            await asyncio.sleep(wait)

    log.error("tally_push_failed_all_retries")
    return TallyResponse(
        success=False,
        errors=["Failed to connect to Tally after 3 attempts"],
    )


def _parse_tally_response(raw_xml: str) -> TallyResponse:
    """
    Parse Tally's XML response to extract success/error info.
    Tally returns:
      <RESPONSE>
        <CREATED>1</CREATED>
        <ALTERED>0</ALTERED>
        <LASTVCHID>123</LASTVCHID>
      </RESPONSE>
    Or on error:
      <LINEERROR>...</LINEERROR>
    """
    import re

    created = 0
    altered = 0
    errors: list[str] = []

    # Extract CREATED count
    m = re.search(r"<CREATED>(\d+)</CREATED>", raw_xml)
    if m:
        created = int(m.group(1))

    # Extract ALTERED count
    m = re.search(r"<ALTERED>(\d+)</ALTERED>", raw_xml)
    if m:
        altered = int(m.group(1))

    # Extract errors
    for err_match in re.finditer(r"<LINEERROR>(.*?)</LINEERROR>", raw_xml, re.DOTALL):
        errors.append(err_match.group(1).strip())

    # Also check for generic error
    if re.search(r"<ERROR>(.*?)</ERROR>", raw_xml):
        for err_match in re.finditer(r"<ERROR>(.*?)</ERROR>", raw_xml, re.DOTALL):
            errors.append(err_match.group(1).strip())

    success = (created > 0 or altered > 0) and len(errors) == 0

    return TallyResponse(
        success=success,
        created=created,
        altered=altered,
        errors=errors,
        raw_response=raw_xml[:1000],
    )


# ---------- High-Level Operations ----------

async def create_or_update_ledger(party_name: str) -> TallyResponse:
    """Create a ledger in Tally for the Aadhaar party name."""
    s = get_settings()
    xml = build_ledger_xml(party_name, group=s.tally_ledger_group)
    log.info("creating_tally_ledger", party=party_name)
    result = await push_to_tally(xml)

    # If ledger already exists, Tally returns an error — that's OK
    if not result.success and any("already exists" in e.lower() for e in result.errors):
        log.info("ledger_already_exists", party=party_name)
        result.success = True
        result.altered = 1

    return result


async def create_voucher(voucher: TallyVoucher) -> TallyResponse:
    """Create a voucher in Tally. Ledger must exist first."""
    xml = build_voucher_xml(voucher)
    log.info(
        "creating_tally_voucher",
        voucher_number=voucher.voucher_number,
        party=voucher.party_name,
        amount=voucher.amount,
    )
    return await push_to_tally(xml)
