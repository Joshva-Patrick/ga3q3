import re
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dateutil import parser as dateparser

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class InvoiceRequest(BaseModel):
    invoice_text: str


class InvoiceFields(BaseModel):
    invoice_no: Optional[str] = None
    date: Optional[str] = None
    vendor: Optional[str] = None
    amount: Optional[float] = None
    tax: Optional[float] = None
    currency: Optional[str] = None


@app.get("/")
def home():
    return {"status": "running"}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def parse_number(raw: str) -> Optional[float]:
    """'2,199.00' / '1600' / '395.82' -> float. Returns None if unparsable."""
    if raw is None:
        return None
    cleaned = raw.replace(",", "").strip()
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def find_first(patterns, text: str) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def extract_invoice_no(text: str) -> Optional[str]:
    patterns = [
        r"invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/_.]*)",
        r"\bINV[-\/]?[A-Za-z0-9\-\/]+\b",
    ]
    val = find_first(patterns[:1], text)
    if val:
        return val
    match = re.search(patterns[1], text, re.IGNORECASE)
    return match.group(0).strip() if match else None


def extract_date(text: str) -> Optional[str]:
    match = re.search(r"date\s*[:\-]?\s*(.+)", text, re.IGNORECASE)
    if not match:
        return None
    raw_date = match.group(1).strip().split("\n")[0]
    # Trim trailing junk after the date (e.g. if the line has extra text)
    raw_date = re.split(r"\s{2,}|\||;", raw_date)[0].strip()

    # If the string already starts with a 4-digit year (e.g. "2026-07-01"
    # or "2026/07/01"), that year is unambiguous -- don't let dayfirst
    # swap the month/day around. Otherwise default to dayfirst, since
    # "15 March 2026" / "03/04/2026" style dates are day-first in most
    # non-US invoice formats.
    year_first = bool(re.match(r"^\d{4}[\/\-]", raw_date))

    try:
        parsed = dateparser.parse(
            raw_date,
            dayfirst=not year_first,
            yearfirst=year_first,
            fuzzy=True,
        )
        if parsed:
            return parsed.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        pass
    return None


def extract_vendor(text: str) -> Optional[str]:
    patterns = [
        r"vendor\s*[:\-]?\s*(.+)",
        r"seller\s*[:\-]?\s*(.+)",
        r"billed\s*by\s*[:\-]?\s*(.+)",
        r"from\s*[:\-]?\s*(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            line = match.group(1).strip().split("\n")[0]
            line = re.split(r"\s{2,}|\||;", line)[0].strip()
            if line:
                return line
    return None


def extract_amount(text: str) -> Optional[float]:
    patterns = [
        r"sub[\s\-]?total\s*[.:\-]*\s*(?:rs\.?|₹|\$|inr|usd|eur|gbp|£|€)?\s*([\d,]+\.?\d*)",
    ]
    raw = find_first(patterns, text)
    if raw:
        return parse_number(raw)

    # Fallback: total - tax, if both are found explicitly elsewhere
    total = find_first(
        [r"(?:grand\s*)?total\s*[.:\-]*\s*(?:rs\.?|₹|\$|inr|usd|eur|gbp|£|€)?\s*([\d,]+\.?\d*)"],
        text,
    )
    tax = extract_tax(text)
    total_val = parse_number(total)
    if total_val is not None and tax is not None:
        return round(total_val - tax, 2)
    return None


def extract_tax(text: str) -> Optional[float]:
    patterns = [
        r"(?:gst|vat|tax)\s*\([\d.]+%\)\s*[.:\-]*\s*(?:rs\.?|₹|\$|inr|usd|eur|gbp|£|€)?\s*([\d,]+\.?\d*)",
        r"(?:gst|vat|tax)\s*[.:\-]*\s*(?:rs\.?|₹|\$|inr|usd|eur|gbp|£|€)?\s*([\d,]+\.?\d*)",
    ]
    raw = find_first(patterns, text)
    return parse_number(raw) if raw else None


def extract_currency(text: str) -> Optional[str]:
    match = re.search(r"currency\s*[:\-]?\s*([A-Za-z]{3})", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    if re.search(r"₹|Rs\.?\s*\d|INR", text, re.IGNORECASE):
        return "INR"
    if re.search(r"\$\s*\d|USD", text, re.IGNORECASE):
        return "USD"
    if re.search(r"€\s*\d|EUR", text, re.IGNORECASE):
        return "EUR"
    if re.search(r"£\s*\d|GBP", text, re.IGNORECASE):
        return "GBP"
    return None


# ------------------------------------------------------------------
# Endpoint
# ------------------------------------------------------------------
@app.post("/extract", response_model=InvoiceFields)
async def extract(req: InvoiceRequest):
    text = req.invoice_text or ""

    return InvoiceFields(
        invoice_no=extract_invoice_no(text),
        date=extract_date(text),
        vendor=extract_vendor(text),
        amount=extract_amount(text),
        tax=extract_tax(text),
        currency=extract_currency(text),
    )