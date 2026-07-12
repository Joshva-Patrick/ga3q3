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


INVOICE_NO_PATTERNS = [
    # "Invoice No:", "Invoice Number:", "Invoice #:", "Invoice ID:"
    r"invoice\s*(?:no\.?|number|#|id)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/_.]*)",
    # "Invoice Ref:", "Invoice Reference No:"
    r"invoice\s*ref(?:erence)?\.?\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/_.]*)",
    # "Bill No:", "Receipt No:", "Voucher No:", "Doc No:", "Order No:", "PO No:"
    r"(?:bill|receipt|voucher|doc(?:ument)?|order|po|tracking)\s*(?:no\.?|number|#|id)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/_.]*)",
    # "Ref No:", "Reference Number:" (colon/dash required to avoid matching the bare word "reference")
    r"\bref(?:erence)?\.?\s*(?:no\.?|number|#)?\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9\-\/_.]*)",
    # bare "No:" as a last-resort label (still requires a colon/dash so it doesn't match prose)
    r"\bno\.?\s*[:\-]\s*([A-Za-z0-9][A-Za-z0-9\-\/_.]*)",
]


def extract_invoice_no(text: str) -> Optional[str]:
    for pattern in INVOICE_NO_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            val = match.group(1).strip().strip(".,;:")
            # Safety rule: a real invoice number always contains a digit.
            # This is what stops a bare heading word like "Invoice" or
            # "Reference" from ever being accepted as a false match.
            if val and any(ch.isdigit() for ch in val):
                return val

    # Last resort: scan the first few lines for something shaped like an
    # ID code (letters/digits with a dash), still requiring a digit.
    for line in text.split("\n")[:6]:
        match = re.search(r"\b([A-Za-z]{1,6}[-\/]?\d[A-Za-z0-9\-\/]*)\b", line)
        if match and any(ch.isdigit() for ch in match.group(1)):
            return match.group(1)

    return None


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


CURRENCY_PREFIX = r"(?:rs\.?|₹|\$|inr|usd|eur|gbp|£|€)"
PERCENT_EXPR = r"\(?\s*\d+(?:\.\d+)?\s*%\s*\)?"


def extract_line_value(text: str, keyword_pattern: str) -> Optional[str]:
    """
    Find a line containing keyword_pattern, then look ONLY at the text
    after the keyword on that line for a monetary value. Strips any
    percentage expression first (e.g. "(18%)", "18%", "@18%") so a rate
    like "21%" is never mistaken for the actual amount.
    """
    for line in text.split("\n"):
        match = re.search(keyword_pattern, line, re.IGNORECASE)
        if not match:
            continue
        rest = line[match.end():]
        rest = re.sub(PERCENT_EXPR, " ", rest)
        num_match = re.search(rf"{CURRENCY_PREFIX}?\s*([\d,]+\.?\d*)", rest, re.IGNORECASE)
        if num_match:
            return num_match.group(1)
    return None


def extract_amount(text: str) -> Optional[float]:
    raw = extract_line_value(text, r"sub[\s\-]?total")
    if raw:
        return parse_number(raw)

    # Fallback: total - tax, if both are found explicitly elsewhere
    total_raw = extract_line_value(text, r"(?:grand\s*)?total")
    tax = extract_tax(text)
    total_val = parse_number(total_raw)
    if total_val is not None and tax is not None:
        return round(total_val - tax, 2)
    return None


def extract_tax(text: str) -> Optional[float]:
    raw = extract_line_value(text, r"gst|vat|tax")
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