"""
clean.py — normalization layer for the monday.com BI agent.

Takes raw monday.com API output and returns tidy pandas DataFrames plus a
data-quality report. Every transformation here is driven by messiness actually
observed in the two source boards, not hypothetical.

The rule this module follows: NEVER silently drop or guess. If a value cannot be
parsed, it becomes NaN/None and gets counted in the quality report so the agent
can tell the user about it.
"""

from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

# --------------------------------------------------------------------------
# 1. monday.com API response  ->  DataFrame
# --------------------------------------------------------------------------

def items_to_dataframe(columns: list[dict], items: list[dict]) -> pd.DataFrame:
    """Convert a monday board's items_page into a DataFrame keyed by column TITLE.

    monday returns column_values keyed by opaque column IDs (e.g. "text_mkq1").
    We map those back to human titles so the rest of the code is readable.
    The item's own `name` becomes the first column, since monday makes the
    spreadsheet's first column the item name.
    """
    # The item-name column has id "name" and is not a real column value —
    # monday exposes it as item.name instead. Skipping it avoids a useless
    # "Name" column colliding with our "Item Name".
    id_to_title = {c["id"]: c["title"] for c in columns if c["id"] != "name"}

    records = []
    for item in items:
        row = {"__item_id": item.get("id"), "Item Name": item.get("name")}
        for cv in item.get("column_values", []):
            if cv["id"] == "name":
                continue
            title = id_to_title.get(cv["id"], cv["id"])
            text = cv.get("text")
            row[title] = text if text not in ("", None) else None
        records.append(row)

    return pd.DataFrame(records)


# --------------------------------------------------------------------------
# 2. Generic cleaners
# --------------------------------------------------------------------------

def drop_repeated_header_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Remove rows where a cell equals its own column name.

    The Deal funnel sheet has its header row repeated inside the data (twice),
    which imported into monday as real items. Those rows are structurally
    invalid, not merely incomplete, so they are removed rather than nulled.
    """
    mask = pd.Series(False, index=df.index)
    for col in df.columns:
        if col.startswith("__"):
            continue
        mask |= df[col].astype(str).str.strip().eq(col)
    removed = int(mask.sum())
    return df[~mask].copy(), removed


_CURRENCY_JUNK = re.compile(r"[^\d.\-]")


def to_number(value) -> float | None:
    """Parse a money/quantity cell. Returns None when unparseable.

    Handles: thousands separators, currency symbols, stray whitespace,
    and the literal Excel error string '#VALUE!' found in the Work Order sheet.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s == "" or s.upper() in {"#VALUE!", "#REF!", "#N/A", "NA", "N/A", "-"}:
        return None
    s = _CURRENCY_JUNK.sub("", s)
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


_DATE_FORMATS = [
    "%Y-%m-%d",          # monday's canonical output
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%b %d, %Y",         # "Sep 27, 2025"
    "%d %b %Y",
    "%d-%b-%Y",
]


def to_date(value) -> pd.Timestamp | None:
    """Parse a date across every format observed. Returns None if none match."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s == "" or s.upper() in {"NA", "N/A", "-", "TBD"}:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return pd.Timestamp(datetime.strptime(s, fmt))
        except ValueError:
            continue
    parsed = pd.to_datetime(s, errors="coerce", dayfirst=True)
    return None if pd.isna(parsed) else parsed


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def to_month_number(value) -> int | None:
    """'Dec' -> 12, 'November' -> 11, 'June' -> 6.

    Both boards store bare month names with no year, abbreviated inconsistently
    ('Dec' next to 'November'). We return the month number and deliberately do
    NOT infer a year — the agent must treat these as year-less.
    """
    if value is None:
        return None
    s = str(value).strip().lower().rstrip(".")
    return _MONTHS.get(s)


def canon_text(value) -> str | None:
    """Trim, collapse internal whitespace. Preserves original casing."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def canon_category(value, mapping: dict[str, str]) -> str | None:
    """Lowercase+trim, then look up a canonical label.

    Unmapped values pass through title-cased rather than being dropped — an
    unexpected category is still real data.
    """
    s = canon_text(value)
    if s is None:
        return None
    return mapping.get(s.lower(), s)


# --------------------------------------------------------------------------
# 3. Canonical vocabularies (built from the observed distinct values)
# --------------------------------------------------------------------------

SECTOR_MAP = {
    "mining": "Mining",
    "renewables": "Renewables",
    "renewable": "Renewables",
    "railways": "Railways",
    "railway": "Railways",
    "powerline": "Powerline",
    "power line": "Powerline",
    "construction": "Construction",
    "manufacturing": "Manufacturing",
    "aviation": "Aviation",
    "security and surveillance": "Security and Surveillance",
    "dsp": "DSP",
    "others": "Others",
    "other": "Others",
    # 'Tender' is a deal type, not a sector. Kept distinct so the agent can
    # exclude it from sector analysis rather than silently miscounting it.
    "tender": "Tender (not a sector)",
}

DEAL_STATUS_MAP = {
    "won": "Won",
    "dead": "Dead",
    "open": "Open",
    "on hold": "On Hold",
}

BILLING_STATUS_MAP = {
    "billed": "Billed",
    "bilLed".lower(): "Billed",   # observed typo: 'BIlled'
    "partially billed": "Partially Billed",
    "not billable": "Not Billable",
    "update required": "Update Required",
    "stuck": "Stuck",
}

INVOICE_STATUS_MAP = {
    "fully billed": "Fully Billed",
    "partially billed": "Partially Billed",
    "not billed yet": "Not Billed",
    "stuck": "Stuck",
}


def canon_invoice_status(value) -> str | None:
    """Collapse free-text escapees like 'Billed- Visit 7' into 'Partially Billed'."""
    s = canon_text(value)
    if s is None:
        return None
    low = s.lower()
    if low in INVOICE_STATUS_MAP:
        return INVOICE_STATUS_MAP[low]
    if low.startswith("billed-"):
        return "Partially Billed"
    return s


def canon_deal_stage(value) -> tuple[str | None, str | None]:
    """Split 'B. Sales Qualified Leads' into ('B', 'Sales Qualified Leads').

    Every stage carries an A–O ordering prefix except 'Project Completed',
    which has none. That one gets a None prefix and sorts last.
    """
    s = canon_text(value)
    if s is None:
        return None, None
    m = re.match(r"^([A-Z])\.\s*(.+)$", s)
    if m:
        return m.group(1), m.group(2)
    return None, s


_QTY_UNITS = {
    "ha": "hectares", "hectare": "hectares", "hectares": "hectares",
    "acre": "acres", "acres": "acres",
    "rkm": "route_km", "km": "km",
    "day": "days", "days": "days",
    "month": "months", "months": "months",
    "nos": "count", "no": "count",
}


def split_quantity(value) -> tuple[float | None, str | None]:
    """'5360 HA' -> (5360.0, 'hectares'); '30 days' -> (30.0, 'days'); '4' -> (4.0, None).

    This column mixes hectares, acres, route-km, days and months in one field.
    Splitting number from unit is what lets the agent REFUSE to sum across
    incompatible units instead of producing a meaningless total.
    """
    if value is None:
        return None, None
    s = str(value).strip()
    if s == "" or s.upper() in {"NA", "N/A", "-"}:
        return None, None
    m = re.match(r"^\s*([\d.,]+)\s*([A-Za-z ]*)\s*$", s)
    if not m:
        return to_number(s), None
    num = to_number(m.group(1))
    unit_raw = m.group(2).strip().lower()
    return num, _QTY_UNITS.get(unit_raw, unit_raw or None)


# --------------------------------------------------------------------------
# 4. Board-specific pipelines
# --------------------------------------------------------------------------

WO_MONEY_COLS = [
    "Amount in Rupees (Excl of GST) (Masked)",
    "Amount in Rupees (Incl of GST) (Masked)",
    "Billed Value in Rupees (Excl of GST.) (Masked)",
    "Billed Value in Rupees (Incl of GST.) (Masked)",
    "Collected Amount in Rupees (Incl of GST.) (Masked)",
    "Amount to be billed in Rs. (Exl. of GST) (Masked)",
    "Amount to be billed in Rs. (Incl. of GST) (Masked)",
    "Amount Receivable (Masked)",
]

WO_DATE_COLS = [
    "Data Delivery Date", "Date of PO/LOI", "Probable Start Date",
    "Probable End Date", "Last invoice date",
]

# Columns observed to be 100% empty across all 176 rows. Kept in the frame so
# the agent can explicitly say "this board does not track that" rather than
# returning a misleading zero.
WO_EMPTY_COLS = [
    "Expected Billing Month", "Actual Collection Month",
    "Collection status", "Collection Date",
]


def clean_work_orders(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df, _ = drop_repeated_header_rows(df)

    for col in WO_MONEY_COLS:
        if col in df:
            df[col] = df[col].map(to_number)

    for col in WO_DATE_COLS:
        if col in df:
            df[col] = df[col].map(to_date)

    if "Sector" in df:
        df["Sector"] = df["Sector"].map(lambda v: canon_category(v, SECTOR_MAP))
    if "Billing Status" in df:
        df["Billing Status"] = df["Billing Status"].map(
            lambda v: canon_category(v, BILLING_STATUS_MAP))
    if "Invoice Status" in df:
        df["Invoice Status"] = df["Invoice Status"].map(canon_invoice_status)

    for col in ["Last executed month of recurring project", "Actual Billing Month"]:
        if col in df:
            df[col + " (month #)"] = df[col].map(to_month_number)

    if "Quantities as per PO" in df:
        parsed = df["Quantities as per PO"].map(split_quantity)
        df["PO Quantity"] = [p[0] for p in parsed]
        df["PO Quantity Unit"] = [p[1] for p in parsed]

    for col in ["Item Name", "Customer Name Code", "Serial #", "Nature of Work",
                "Execution Status", "Type of Work", "BD/KAM Personnel code",
                "Document Type", "WO Status (billed)"]:
        if col in df:
            df[col] = df[col].map(canon_text)

    return df


DEALS_DATE_COLS = ["Close Date (A)", "Tentative Close Date", "Created Date"]


def clean_deals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df, _ = drop_repeated_header_rows(df)

    if "Masked Deal value" in df:
        df["Masked Deal value"] = df["Masked Deal value"].map(to_number)

    for col in DEALS_DATE_COLS:
        if col in df:
            df[col] = df[col].map(to_date)

    if "Sector/service" in df:
        df["Sector"] = df["Sector/service"].map(lambda v: canon_category(v, SECTOR_MAP))
    if "Deal Status" in df:
        df["Deal Status"] = df["Deal Status"].map(lambda v: canon_category(v, DEAL_STATUS_MAP))

    if "Deal Stage" in df:
        parsed = df["Deal Stage"].map(canon_deal_stage)
        df["Stage Order"] = [p[0] for p in parsed]
        df["Deal Stage"] = [p[1] for p in parsed]

    for col in ["Item Name", "Owner code", "Client Code", "Closure Probability",
                "Product deal"]:
        if col in df:
            df[col] = df[col].map(canon_text)

    return df


# --------------------------------------------------------------------------
# 5. Joining the two boards
# --------------------------------------------------------------------------

def join_deals_to_work_orders(deals: pd.DataFrame, wos: pd.DataFrame) -> pd.DataFrame:
    """Join on deal name — NOT on the client codes.

    Work Orders uses codes like 'WOCOMPANY_002' while Deals uses 'COMPANY089'.
    These are independently masked namespaces with zero literal overlap; the
    numeric parts appearing to line up is coincidence, and joining on them would
    attribute revenue to the wrong client. Deal name gives ~90% coverage of the
    work-order side, which is the honest join.
    """
    d = deals.copy()
    w = wos.copy()
    d["_key"] = d["Item Name"].astype(str).str.strip().str.lower()
    w["_key"] = w["Item Name"].astype(str).str.strip().str.lower()
    return w.merge(d, on="_key", how="left", suffixes=("_wo", "_deal"))


# --------------------------------------------------------------------------
# 6. Data quality report — this is what the agent quotes back to the user
# --------------------------------------------------------------------------

def quality_report(df: pd.DataFrame, board_name: str,
                   key_columns: list[str] | None = None) -> dict:
    total = len(df)
    report = {
        "board": board_name,
        "total_rows": total,
        "empty_columns": [],
        "sparse_columns": {},
        "key_column_coverage": {},
    }

    for col in df.columns:
        if col.startswith("__"):
            continue
        non_null = int(df[col].notna().sum())
        if non_null == 0:
            report["empty_columns"].append(col)
        elif total and non_null / total < 0.5:
            report["sparse_columns"][col] = f"{non_null}/{total} populated"

    for col in (key_columns or []):
        if col in df:
            non_null = int(df[col].notna().sum())
            report["key_column_coverage"][col] = {
                "populated": non_null,
                "missing": total - non_null,
                "pct": round(100 * non_null / total, 1) if total else 0.0,
            }

    return report


def caveat_sentence(report: dict, column: str) -> str:
    """Turn a coverage figure into a sentence the agent can append to an answer."""
    cov = report.get("key_column_coverage", {}).get(column)
    if not cov or cov["missing"] == 0:
        return ""
    return (f"Based on {cov['populated']} of {report['total_rows']} records on the "
            f"{report['board']} board; {cov['missing']} have no {column} recorded.")


def small_sample_warning(n: int, label: str, threshold: int = 5) -> str:
    """Sectors like Construction (n=2) and Aviation (n=1) must not be reported
    as if they were comparable to Mining (n=100)."""
    if n < threshold:
        return f"Only {n} record(s) for {label} — too few to draw a reliable conclusion."
    return ""
