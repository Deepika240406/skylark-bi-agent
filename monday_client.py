"""
monday_client.py — read-only monday.com data access for the BI agent.

Everything the agent knows about the business comes through here. There is no
CSV fallback anywhere in this project: boards are queried live over the GraphQL
API on every refresh, per the assignment's integration requirement.

Board IDs and the API token are read from the environment, never hardcoded.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

API_URL = "https://api.monday.com/v2"
API_VERSION = "2024-10"

# Rows fetched per request. monday caps complexity rather than page size, and
# 100 keeps us well inside the budget while limiting round trips.
PAGE_SIZE = 100

MAX_RETRIES = 3
BACKOFF_SECONDS = 2


class MondayError(RuntimeError):
    """Raised when monday.com returns an error we cannot recover from."""


@dataclass
class BoardData:
    """One board's raw payload: its metadata, column definitions, and items."""
    board_id: str
    name: str
    columns: list[dict]
    items: list[dict]

    @property
    def row_count(self) -> int:
        return len(self.items)


def _token() -> str:
    token = os.environ.get("MONDAY_TOKEN")
    if not token:
        raise MondayError(
            "MONDAY_TOKEN is not set. Add it to your .env file or your host's "
            "environment variables."
        )
    return token


def _headers() -> dict:
    return {
        "Authorization": _token(),
        "Content-Type": "application/json",
        "API-Version": API_VERSION,
    }


def _post(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL request with retries on transient failures.

    monday returns HTTP 200 with an "errors" array for GraphQL-level problems,
    so checking status_code alone is not enough.
    """
    payload = {"query": query, "variables": variables or {}}
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(API_URL, json=payload, headers=_headers(), timeout=30)

            # 429 = rate limited, 5xx = monday-side problem. Both worth retrying.
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = BACKOFF_SECONDS * attempt
                log.warning("monday returned %s, retrying in %ss (attempt %s/%s)",
                            resp.status_code, wait, attempt, MAX_RETRIES)
                time.sleep(wait)
                last_error = MondayError(f"HTTP {resp.status_code}")
                continue

            if resp.status_code == 401:
                raise MondayError(
                    "monday.com rejected the token (401). It may have been "
                    "regenerated — copy a fresh one from Administration > "
                    "Connections > Personal API token."
                )

            resp.raise_for_status()
            body = resp.json()

            if "errors" in body:
                messages = "; ".join(
                    e.get("message", str(e)) for e in body["errors"]
                )
                raise MondayError(f"monday.com GraphQL error: {messages}")

            if "data" not in body:
                raise MondayError(f"Unexpected response shape: {body}")

            return body["data"]

        except requests.Timeout as exc:
            last_error = exc
            wait = BACKOFF_SECONDS * attempt
            log.warning("Request timed out, retrying in %ss (attempt %s/%s)",
                        wait, attempt, MAX_RETRIES)
            time.sleep(wait)
        except requests.RequestException as exc:
            last_error = exc
            wait = BACKOFF_SECONDS * attempt
            log.warning("Network error (%s), retrying in %ss", exc, wait)
            time.sleep(wait)

    raise MondayError(
        f"Could not reach monday.com after {MAX_RETRIES} attempts: {last_error}"
    )


_BOARD_META_QUERY = """
query ($ids: [ID!]) {
  boards(ids: $ids) {
    id
    name
    items_count
    columns { id title type }
  }
}
"""

_ITEMS_QUERY = """
query ($ids: [ID!], $limit: Int!, $cursor: String) {
  boards(ids: $ids) {
    id
    name
    columns { id title type }
    items_page(limit: $limit, cursor: $cursor) {
      cursor
      items {
        id
        name
        column_values { id text value }
      }
    }
  }
}
"""


def fetch_board(board_id: str | int) -> BoardData:
    """Fetch one board in full, following the pagination cursor to the end.

    Ignoring the cursor is the classic silent failure here: you get the first
    page, every total is quietly wrong, and nothing raises. We loop until the
    cursor comes back null and verify the row count against items_count.
    """
    board_id = str(board_id)
    columns: list[dict] = []
    items: list[dict] = []
    cursor: str | None = None
    name = ""
    pages = 0

    while True:
        data = _post(_ITEMS_QUERY, {
            "ids": [board_id],
            "limit": PAGE_SIZE,
            "cursor": cursor,
        })

        boards = data.get("boards") or []
        if not boards:
            raise MondayError(
                f"Board {board_id} returned no data. Check the ID is correct and "
                f"that your token's user has access to it."
            )

        board = boards[0]
        name = board.get("name", "")
        if not columns:
            columns = board.get("columns", [])

        page = board.get("items_page") or {}
        items.extend(page.get("items", []))
        cursor = page.get("cursor")
        pages += 1

        if not cursor:
            break

        if pages > 200:  # ~20k rows; a runaway cursor should not hang the app
            raise MondayError(f"Pagination did not terminate for board {board_id}.")

    log.info("Fetched %s items from board '%s' across %s page(s)",
             len(items), name, pages)

    return BoardData(board_id=board_id, name=name, columns=columns, items=items)


def verify_board_counts(board_id: str | int, fetched: int) -> str:
    """Compare what we fetched against the board's own items_count.

    A mismatch means pagination dropped rows — the agent must never report
    confident totals over a partial fetch, so this surfaces as a caveat.
    """
    data = _post(_BOARD_META_QUERY, {"ids": [str(board_id)]})
    boards = data.get("boards") or []
    if not boards:
        return ""
    expected = boards[0].get("items_count")
    if expected is not None and expected != fetched:
        return (f"Warning: fetched {fetched} rows but board reports {expected}. "
                f"Results may be incomplete.")
    return ""


def fetch_all_boards() -> dict[str, BoardData]:
    """Fetch both configured boards. Returns {'work_orders': ..., 'deals': ...}.

    If one board fails we still return the other, so a founder asking a
    deals-only question is not blocked by a work-orders outage.
    """
    wo_id = os.environ.get("WORK_ORDERS_BOARD_ID")
    deals_id = os.environ.get("DEALS_BOARD_ID")

    if not wo_id or not deals_id:
        raise MondayError(
            "WORK_ORDERS_BOARD_ID and DEALS_BOARD_ID must both be set in the "
            "environment."
        )

    results: dict[str, BoardData] = {}
    errors: dict[str, str] = {}

    for key, board_id in (("work_orders", wo_id), ("deals", deals_id)):
        try:
            results[key] = fetch_board(board_id)
        except MondayError as exc:
            log.error("Failed to fetch %s (board %s): %s", key, board_id, exc)
            errors[key] = str(exc)

    if not results:
        raise MondayError(
            "Could not fetch either board: " +
            "; ".join(f"{k}: {v}" for k, v in errors.items())
        )

    results["__errors"] = errors  # type: ignore[assignment]
    return results


if __name__ == "__main__":
    # Smoke test: python monday_client.py
    logging.basicConfig(level=logging.INFO)
    for key, board in fetch_all_boards().items():
        if key == "__errors":
            if board:
                print("Errors:", board)
            continue
        print(f"{board.name}: {board.row_count} items, {len(board.columns)} columns")
        print("  ", verify_board_counts(board.board_id, board.row_count) or "counts OK")
