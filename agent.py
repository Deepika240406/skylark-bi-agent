"""
agent.py — the conversational layer.

The model never sees the raw boards and never does arithmetic. It chooses which
analytics function to call, receives a dict of computed numbers, and writes prose
around it. Any figure in an answer therefore traces back to a function in
analytics.py — the agent cannot invent one.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time

import pandas as pd
from google import genai
from google.genai import types

import analytics

log = logging.getLogger(__name__)

# Model IDs get retired. Try in order and use the first that responds, so a
# deprecation does not take the deployed app down.
MODEL_CANDIDATES = [
    os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
]

_RESOLVED_MODEL: str | None = None

MAX_TOOL_ROUNDS = 6
RATE_LIMIT_RETRIES = 4


# --------------------------------------------------------------------------
# Tool declarations
# --------------------------------------------------------------------------

_STR = types.Schema(type=types.Type.STRING)


def _schema(**props) -> types.Schema:
    return types.Schema(type=types.Type.OBJECT, properties=props)


TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="pipeline_summary",
        description=(
            "Open sales pipeline: number of open deals, total value, breakdown by "
            "deal stage and by closure probability. Use for questions about what is "
            "in the pipeline, how it looks for a sector, or what might close."
        ),
        parameters=_schema(
            sector=types.Schema(type=types.Type.STRING,
                                description="Sector filter, e.g. Mining, Renewables, Railways, Powerline, Construction, Others"),
            owner=types.Schema(type=types.Type.STRING, description="Owner code, e.g. OWNER_001"),
            start=types.Schema(type=types.Type.STRING, description="Start date YYYY-MM-DD, filters on tentative close date"),
            end=types.Schema(type=types.Type.STRING, description="End date YYYY-MM-DD"),
        ),
    ),
    types.FunctionDeclaration(
        name="win_rate",
        description=(
            "Win rate over decided deals (Won vs Dead), with won/lost values, "
            "mean/median deal size and data coverage. Use for questions about how "
            "well the team converts, or whether deals of a certain size close."
        ),
        parameters=_schema(sector=_STR, owner=_STR),
    ),
    types.FunctionDeclaration(
        name="deals_by_sector",
        description=(
            "Deal counts, values and win rates for every sector. Use to compare "
            "sectors against each other."
        ),
        parameters=_schema(),
    ),
    types.FunctionDeclaration(
        name="revenue_summary",
        description=(
            "Delivered work: order book value, billed value, collected value and "
            "outstanding receivables from the Work Orders board. Use for questions "
            "about revenue, billing, collections or money owed."
        ),
        parameters=_schema(
            sector=_STR,
            start=types.Schema(type=types.Type.STRING, description="Start date YYYY-MM-DD, filters on PO date"),
            end=_STR,
        ),
    ),
    types.FunctionDeclaration(
        name="operational_health",
        description=(
            "Execution status of work orders, invoice status, and projects past "
            "their probable end date. Use for questions about delivery, what is "
            "late, or operational risk."
        ),
        parameters=_schema(sector=_STR),
    ),
    types.FunctionDeclaration(
        name="sector_performance",
        description=(
            "Cross-board view joining deals to work orders by deal name: sales "
            "performance next to delivery performance per sector. Use when a "
            "question spans both sales and execution."
        ),
        parameters=_schema(),
    ),
    types.FunctionDeclaration(
        name="leadership_brief",
        description=(
            "A full leadership update bundle: pipeline, revenue, operations, sector "
            "breakdown, win rate, top open deals and known data blind spots. Use "
            "when asked for a summary, board update, weekly review or 'how are we "
            "doing overall'."
        ),
        parameters=_schema(),
    ),
]


SYSTEM_PROMPT = """You are a business intelligence assistant for Skylark Drones' \
leadership team. You answer questions about the sales pipeline and project \
delivery using two monday.com boards.

DATA YOU CAN SEE
- Deals board ({n_deals} deals): sales pipeline. Deal Status is Won, Dead, Open \
or On Hold. Deal Stage runs from Lead Generated through to Project Won or \
Project Lost. Each deal has an owner code, client code, sector, and sometimes a \
value in rupees.
- Work Orders board ({n_wos} work orders): delivered and in-flight projects, with \
order value, billed value, collections, receivables, execution status and dates.

Sectors present: {sectors}
Today's date: {today}

HOW YOU WORK
1. Call a tool to get numbers. Never calculate, estimate or recall a figure \
yourself. If a number is not in a tool result, you do not have it and you say so.
2. Every tool result includes a "caveats" list. You MUST surface relevant caveats \
in your answer. Data coverage is not a footnote here — a founder acting on an \
incomplete number is the failure this system exists to prevent.
3. Give insight, not just figures. Say what the number means, what is unusual, \
and what it implies. A founder can read a total; they need to know why it matters.
4. Currency is Indian rupees. Format large numbers in lakhs and crores as well as \
raw figures, e.g. "₹4.8 crore (₹48,000,000)".

ASKING QUESTIONS
Ask a clarifying question only when the answer would materially change and you \
genuinely cannot pick a sensible default. Otherwise state your assumption inline \
and answer. "This quarter" — assume the current calendar quarter and say so. An \
unspecified sector means all sectors. Do not interrogate the user before every \
answer.

KNOWN LIMITS OF THIS DATA
- Deal value is missing on roughly 60% of closed deals, so historical totals are \
a floor, not a full picture. Open-deal coverage is much better.
- Collection dates and collection status are not tracked at all. You cannot answer \
questions about when money arrived.
- Most closed deals have no actual close date, which limits trend analysis.
- Quantities are recorded in mixed units (hectares, acres, route-km, days, months) \
and must never be summed across unit types.
- Some sectors have very few deals. Never present a percentage from a handful of \
records as a reliable trend.
- 'Tender' appears in the sector column but is a deal type, not an industry.

If a question cannot be answered from these two boards, say so plainly and \
explain what data would be needed."""


class _RateLimited(RuntimeError):
    """Raised when retries are exhausted against a quota limit.

    `daily` distinguishes a per-minute burst, which resolves itself, from an
    exhausted daily allowance, which does not — the person needs to be told
    which, because only one of them is worth waiting out.
    """

    def __init__(self, message: str = "", daily: bool = False,
                 busy: bool = False):
        super().__init__(message)
        self.daily = daily
        self.busy = busy


def _suggested_model(message: str) -> str | None:
    """Extract the replacement model Google names in a retirement 404.

    Its message reads "Please update your code to use models/gemini-3.5-flash-lite".
    Following that pointer is more reliable than a hardcoded fallback list,
    because it stays correct as models are retired.
    """
    match = re.search(r"use\s+models/([a-z0-9.\-]+)", message)
    return match.group(1) if match else None


def _retry_delay(message: str) -> float | None:
    """Read the server's suggested wait, if it sent one."""
    match = re.search(r"retryDelay[\"':\s]+(\d+(?:\.\d+)?)s", message)
    return float(match.group(1)) if match else None


class BIAgent:
    """Holds the cleaned data and runs the tool-calling loop."""

    def __init__(self, deals: pd.DataFrame, work_orders: pd.DataFrame,
                 api_key: str | None = None):
        self.deals = deals
        self.work_orders = work_orders
        self.history: list[types.Content] = []

        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        self.client = genai.Client(api_key=key)
        self.model = self._pick_model()

    def _pick_model(self) -> str:
        """Resolve a working model ID once per process.

        The first candidate is used optimistically without a probe request —
        probing cost a full round trip on every startup. Fallback happens in
        ask() only if the model actually errors, so a retirement is still
        survivable but costs nothing in the normal case.
        """
        global _RESOLVED_MODEL
        if _RESOLVED_MODEL:
            return _RESOLVED_MODEL
        _RESOLVED_MODEL = next(m for m in MODEL_CANDIDATES if m)
        return _RESOLVED_MODEL

    def _fallback_model(self) -> bool:
        """Move to the next candidate after a model-level failure."""
        global _RESOLVED_MODEL
        remaining = [m for m in MODEL_CANDIDATES if m and m != self.model]
        if not remaining:
            return False
        self.model = _RESOLVED_MODEL = remaining[0]
        log.warning("Falling back to model %s", self.model)
        return True

    # ------------------------------------------------------------------
    def _system_prompt(self) -> str:
        sectors = []
        if "Sector" in self.deals:
            sectors = sorted({str(s) for s in self.deals["Sector"].dropna().unique()})
        return SYSTEM_PROMPT.format(
            n_deals=len(self.deals),
            n_wos=len(self.work_orders),
            sectors=", ".join(sectors) or "unknown",
            today=pd.Timestamp.today().date(),
        )

    def _dispatch(self, name: str, args: dict) -> dict:
        """Run one analytics function with the frames injected."""
        args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
        try:
            if name == "pipeline_summary":
                return analytics.pipeline_summary(self.deals, **args)
            if name == "win_rate":
                return analytics.win_rate(self.deals, **args)
            if name == "deals_by_sector":
                return analytics.deals_by_sector(self.deals)
            if name == "revenue_summary":
                return analytics.revenue_summary(self.work_orders, **args)
            if name == "operational_health":
                return analytics.operational_health(self.work_orders, **args)
            if name == "sector_performance":
                return analytics.sector_performance(self.deals, self.work_orders)
            if name == "leadership_brief":
                return analytics.leadership_brief(self.deals, self.work_orders)
            return {"error": f"Unknown tool '{name}'."}
        except TypeError as exc:
            return {"error": f"Invalid arguments for {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - surface, never crash the chat
            log.exception("Tool %s failed", name)
            return {"error": f"{name} failed: {exc}"}

    # ------------------------------------------------------------------
    def _generate(self, config):
        """Call the model, absorbing free-tier rate limits transparently.

        The free tier allows only a handful of requests per minute and each
        question costs at least two, so a person clicking through several
        questions will hit the limit. Retrying with backoff turns that from a
        visible failure into a slightly slower answer.
        """
        for attempt in range(RATE_LIMIT_RETRIES):
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=self.history, config=config
                )
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                is_quota = "429" in msg or "RESOURCE_EXHAUSTED" in msg.upper()
                # 503 means the model is momentarily oversubscribed, not broken.
                # It clears on its own, so it is retried like a rate limit.
                is_busy = "503" in msg or "UNAVAILABLE" in msg.upper()
                if not (is_quota or is_busy):
                    raise
                if attempt == RATE_LIMIT_RETRIES - 1:
                    if is_busy and self._fallback_model():
                        continue  # a different model may have capacity
                    daily = "PerDay" in msg or "per day" in msg.lower()
                    raise _RateLimited(msg[:200], daily=daily, busy=is_busy) from exc
                # Honour the server's own delay when given; jitter avoids
                # several sessions retrying in lockstep.
                wait = _retry_delay(msg) or (2 ** attempt * 2)
                wait = min(wait, 20) + random.uniform(0, 0.6)
                log.warning("Rate limited, retrying in %.1fs (%s/%s)",
                            wait, attempt + 1, RATE_LIMIT_RETRIES)
                time.sleep(wait)
        raise _RateLimited("retries exhausted")

    def ask(self, question: str) -> tuple[str, list[str]]:
        """Answer one question. Returns (answer_text, names_of_tools_used)."""
        self.history.append(
            types.Content(role="user", parts=[types.Part(text=question)])
        )

        # These questions need tool selection and clear writing, not deep
        # reasoning — the arithmetic is already done. Capping the thinking
        # budget is the single largest latency saving available.
        config = types.GenerateContentConfig(
            system_instruction=self._system_prompt(),
            tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
            temperature=0.2,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        )

        tools_used: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = self._generate(config)
            except _RateLimited as exc:
                # Show the person what to do, not the provider's error body.
                if getattr(exc, "busy", False):
                    return (
                        "The model is oversubscribed right now — this is a "
                        "provider-side spike and usually passes within a minute. "
                        "The monday.com data loaded fine; try the question again.",
                        tools_used,
                    )
                if exc.daily:
                    return (
                        "The model provider's daily free-tier quota for this key "
                        "is used up. It resets at midnight US Pacific time. The "
                        "monday.com data is still loading correctly — only the "
                        "answer-writing step is blocked.",
                        tools_used,
                    )
                return (
                    "The model provider is limiting requests at the moment. This "
                    "clears within about a minute — ask again shortly.",
                    tools_used,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Model call failed")
                msg = str(exc)
                if "404" in msg or "NOT_FOUND" in msg.upper():
                    suggested = _suggested_model(msg)
                    if suggested and suggested != self.model:
                        log.warning("Model retired; provider suggests %s", suggested)
                        self.model = suggested
                        globals()["_RESOLVED_MODEL"] = suggested
                        continue
                    if self._fallback_model():
                        continue
                return (f"The model could not be reached: {exc}", tools_used)

            candidate = response.candidates[0]
            parts = candidate.content.parts or []
            self.history.append(candidate.content)

            calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

            if not calls:
                text = "".join(p.text for p in parts if getattr(p, "text", None))
                return (text.strip() or "I wasn't able to produce an answer.", tools_used)

            response_parts = []
            for call in calls:
                args = dict(call.args) if call.args else {}
                log.info("Tool call: %s(%s)", call.name, args)
                tools_used.append(call.name)
                result = self._dispatch(call.name, args)
                # Round-trip through JSON so numpy types don't break serialization
                safe = json.loads(json.dumps(result, default=str))
                response_parts.append(
                    types.Part.from_function_response(name=call.name, response=safe)
                )

            self.history.append(types.Content(role="user", parts=response_parts))

        # Tool rounds exhausted. Rather than giving up, ask once more with the
        # tools removed: the model already has every figure it requested in
        # history, so it can only write. This turns a dead end into an answer.
        try:
            final = self.client.models.generate_content(
                model=self.model,
                contents=self.history + [types.Content(
                    role="user",
                    parts=[types.Part(text=(
                        "Answer the original question now using only the tool "
                        "results already gathered. Do not request more data. "
                        "State the relevant caveats."))],
                )],
                config=types.GenerateContentConfig(
                    system_instruction=self._system_prompt(),
                    temperature=0.2,
                ),
            )
            text = "".join(
                p.text for p in (final.candidates[0].content.parts or [])
                if getattr(p, "text", None)
            ).strip()
            if text:
                return (text, tools_used)
        except Exception:  # noqa: BLE001
            log.exception("Final no-tool attempt failed")

        return ("I gathered the data but could not compose an answer. Try asking "
                "about one thing at a time.", tools_used)

    def reset(self) -> None:
        self.history = []