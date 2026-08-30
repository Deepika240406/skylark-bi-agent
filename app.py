"""
app.py — Streamlit chat interface for the monday.com BI agent.

Visual direction: survey document. The data describes aerial site surveys —
hectares, route-kilometres, inspection runs — so the interface borrows from
that world: cartographic paper, survey-green ink, monospace figures, and a
ticked coverage rule as the signature element.

That rule is deliberate. The premise of this project is that no figure should
appear without the share of records behind it, so coverage is given the most
distinctive visual treatment on the page rather than being buried in a footnote.
"""

from __future__ import annotations

import logging

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

import agent as agent_mod
import analytics
import clean
import monday_client

logging.basicConfig(level=logging.INFO)

st.set_page_config(
    page_title="Skylark Intelligence",
    page_icon="◧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Design tokens
#   paper   #EDEFEA   pale cartographic stock
#   ink     #16211C   deep green-black, all body text
#   survey  #1F4B3F   primary accent, ordnance-map green
#   signal  #9A3412   used only where data is thin
#   partial #8A6A16   used only for mid coverage
#   rule    #C6CCC1   hairlines and ticks
# ---------------------------------------------------------------------------

THEME = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

:root {
  --paper:   #EDEFEA;
  --paper-2: #E3E7DE;
  --card:    #F6F7F3;
  --ink:     #16211C;
  --ink-2:   #4A574F;
  --survey:  #1F4B3F;
  --signal:  #9A3412;
  --partial: #8A6A16;
  --rule:    #C6CCC1;
}

.stApp { background: var(--paper); }

html, body, [class*="css"], .stMarkdown, p, li, span, div {
  font-family: 'Archivo', system-ui, sans-serif;
  color: var(--ink);
}

/* Faint survey grid on the canvas */
.stApp::before {
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background-image:
    linear-gradient(var(--rule) 1px, transparent 1px),
    linear-gradient(90deg, var(--rule) 1px, transparent 1px);
  background-size: 64px 64px;
  opacity: .30;
}
.main .block-container { position: relative; z-index: 1; padding-top: 2.2rem; max-width: 1080px; }

/* ---------- Masthead ---------- */
.masthead { border-bottom: 2px solid var(--ink); padding-bottom: .7rem; margin-bottom: .4rem; }
.masthead h1 {
  font-family: 'Archivo', sans-serif; font-weight: 700; font-size: 2.0rem;
  letter-spacing: -.02em; margin: 0; color: var(--ink);
}
.eyebrow {
  font-family: 'IBM Plex Mono', monospace; font-size: .68rem; font-weight: 500;
  letter-spacing: .16em; text-transform: uppercase; color: var(--survey);
  margin-bottom: .35rem;
}
.masthead .sub {
  font-family: 'IBM Plex Mono', monospace; font-size: .74rem; color: var(--ink-2);
  margin-top: .45rem; letter-spacing: .01em;
}

/* ---------- Figures render as instrument readouts ---------- */
.stChatMessage strong, .stChatMessage b { font-weight: 600; }
.stChatMessage code {
  font-family: 'IBM Plex Mono', monospace; background: var(--paper-2);
  color: var(--survey); padding: .06em .3em; border-radius: 2px; font-size: .88em;
}
.stChatMessage table {
  font-family: 'IBM Plex Mono', monospace; font-size: .8rem;
  border-collapse: collapse; width: 100%;
}
.stChatMessage th {
  text-align: left; font-weight: 600; font-size: .66rem; letter-spacing: .1em;
  text-transform: uppercase; border-bottom: 1.5px solid var(--ink);
  padding: .45rem .6rem; color: var(--survey);
}
.stChatMessage td { border-bottom: 1px solid var(--rule); padding: .42rem .6rem; }
.stChatMessage h3 {
  font-size: .95rem; font-weight: 600; letter-spacing: -.01em;
  margin: 1.1rem 0 .4rem; padding-bottom: .25rem;
  border-bottom: 1px solid var(--rule);
}

/* ---------- Chat ---------- */
[data-testid="stChatMessage"] {
  background: var(--card); border: 1px solid var(--rule);
  border-radius: 3px; padding: 1rem 1.15rem; margin-bottom: .85rem;
}
/* The question reads as a marginal note; the answer is the document. */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
  background: transparent; border: none; border-left: 2px solid var(--survey);
  border-radius: 0; padding: .1rem 0 .1rem .85rem; margin-bottom: .5rem;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) p {
  font-family: 'IBM Plex Mono', monospace; font-size: .82rem; color: var(--ink-2);
}
[data-testid="stChatMessageAvatarUser"],
[data-testid="stChatMessageAvatarAssistant"] { display: none; }

[data-testid="stChatInput"] {
  border: 1.5px solid var(--ink); border-radius: 3px; background: var(--card);
}
[data-testid="stChatInput"] textarea { font-family: 'Archivo', sans-serif; }

/* ---------- Prompt chips ----------
   Streamlit renames its button testids between releases, so target the
   element directly and win on specificity rather than relying on one id. */
.stButton button, [data-testid="stBaseButton-secondary"],
[data-testid="baseButton-secondary"] {
  background: transparent !important; color: var(--survey) !important;
  border: 1px solid var(--rule) !important; border-radius: 2px !important;
  font-family: 'IBM Plex Mono', monospace !important; font-size: .72rem !important;
  font-weight: 500 !important; letter-spacing: .01em; text-align: left !important;
  padding: .65rem .7rem !important; white-space: normal !important;
  line-height: 1.4 !important; height: 100% !important; box-shadow: none !important;
  transition: background .12s ease, border-color .12s ease;
}
.stButton button p, [data-testid="stBaseButton-secondary"] p {
  font-family: 'IBM Plex Mono', monospace !important; font-size: .72rem !important;
  color: var(--survey) !important; margin: 0 !important;
}
.stButton button:hover, [data-testid="stBaseButton-secondary"]:hover {
  background: var(--paper-2) !important; border-color: var(--survey) !important;
}
.stButton button:focus-visible {
  outline: 2px solid var(--survey) !important; outline-offset: 2px;
}

/* ---------- Sidebar: the survey log ---------- */
[data-testid="stSidebar"] { background: var(--card); border-right: 1px solid var(--rule); }
[data-testid="stSidebar"] .block-container { padding-top: 1.6rem; }

.log-head {
  font-family: 'IBM Plex Mono', monospace; font-size: .64rem; font-weight: 600;
  letter-spacing: .16em; text-transform: uppercase; color: var(--survey);
  border-bottom: 1.5px solid var(--ink); padding-bottom: .35rem; margin-bottom: .8rem;
}
.board-line {
  display: flex; justify-content: space-between; align-items: baseline;
  font-family: 'IBM Plex Mono', monospace; font-size: .78rem;
  font-weight: 600; margin: 1.1rem 0 .55rem;
  border-bottom: 1px solid var(--rule); padding-bottom: .25rem;
}
.board-line .n { color: var(--survey); }

/* Signature element: the coverage rule */
.cov { margin: .55rem 0 .7rem; }
.cov-label {
  font-family: 'IBM Plex Mono', monospace; font-size: .655rem;
  color: var(--ink-2); display: flex; justify-content: space-between;
  margin-bottom: .22rem; letter-spacing: .01em;
}
.cov-label .pct { font-weight: 600; }
.cov-rule {
  position: relative; height: 13px;
  border-left: 1px solid var(--ink); border-right: 1px solid var(--ink);
  border-bottom: 1px solid var(--ink);
}
.cov-rule::before {
  content: ""; position: absolute; inset: 0; bottom: 0;
  background-image: linear-gradient(90deg, var(--rule) 1px, transparent 1px);
  background-size: 10% 5px; background-repeat: repeat-x;
  background-position: bottom left;
}
.cov-fill { position: absolute; left: 0; bottom: 0; top: 0; opacity: .82; }
.cov-fill.high { background: var(--survey); }
.cov-fill.mid  { background: var(--partial); }
.cov-fill.low  { background: var(--signal); }

.note {
  font-family: 'IBM Plex Mono', monospace; font-size: .655rem; line-height: 1.5;
  color: var(--ink-2); margin: .45rem 0 0; padding-left: .6rem;
  border-left: 2px solid var(--rule);
}
.note.warn { border-left-color: var(--signal); color: var(--signal); }

.provenance {
  font-family: 'IBM Plex Mono', monospace; font-size: .64rem;
  letter-spacing: .06em; text-transform: uppercase; color: var(--ink-2);
  border-top: 1px solid var(--rule); padding-top: .45rem; margin-top: .7rem;
}

[data-testid="stSidebar"] .stButton button {
  width: 100%; text-align: center !important;
  border-color: var(--ink) !important; color: var(--ink) !important;
}
[data-testid="stSidebar"] .stButton button p { color: var(--ink) !important; }

/* Keep the survey log reachable when the sidebar starts collapsed */
[data-testid="stSidebarCollapsedControl"] { color: var(--survey); }

#MainMenu, footer, header { visibility: hidden; }

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
</style>
"""

SUGGESTIONS = [
    "How's our pipeline looking for the energy sector this quarter?",
    "What's our win rate by sector?",
    "Which projects are running late?",
    "How much have we billed versus collected?",
    "Give me a leadership update.",
]


@st.cache_resource(show_spinner=False)
def load_data():
    """Fetch and clean both boards. Cached per session; cleared by Refresh."""
    boards = monday_client.fetch_all_boards()
    errors = boards.pop("__errors", {}) or {}

    deals = work_orders = None
    quality = []

    if "deals" in boards:
        b = boards["deals"]
        deals = clean.clean_deals(clean.items_to_dataframe(b.columns, b.items))
        quality.append(clean.quality_report(
            deals, "Deals", ["Masked Deal value", "Close Date (A)", "Sector"]))

    if "work_orders" in boards:
        b = boards["work_orders"]
        work_orders = clean.clean_work_orders(
            clean.items_to_dataframe(b.columns, b.items))
        quality.append(clean.quality_report(
            work_orders, "Work Orders",
            [analytics.ORDER_VALUE, analytics.BILLED, analytics.COLLECTED]))

    return deals, work_orders, quality, errors


def coverage_rule(label: str, pct: float) -> str:
    """The signature element: a ticked survey rule showing how much of a column
    is actually populated. Colour steps at 75% and 40%."""
    band = "high" if pct >= 75 else ("mid" if pct >= 40 else "low")
    short = label if len(label) <= 30 else label[:28] + "…"
    return (
        f'<div class="cov">'
        f'<div class="cov-label"><span>{short}</span>'
        f'<span class="pct">{pct:.0f}%</span></div>'
        f'<div class="cov-rule"><div class="cov-fill {band}" '
        f'style="width:{pct:.1f}%"></div></div>'
        f'</div>'
    )


def sidebar(quality, errors):
    with st.sidebar:
        st.markdown('<div class="log-head">Survey log</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="note">Read live from monday.com over the API. '
            'No local copy of the data exists.</div>',
            unsafe_allow_html=True,
        )

        if st.button("Refresh from monday.com", use_container_width=True):
            st.cache_resource.clear()
            st.session_state.pop("agent", None)
            st.session_state.messages = []
            st.rerun()

        for board, msg in (errors or {}).items():
            st.markdown(f'<div class="note warn">{board}: {msg}</div>',
                        unsafe_allow_html=True)

        for report in quality:
            st.markdown(
                f'<div class="board-line"><span>{report["board"]}</span>'
                f'<span class="n">{report["total_rows"]} rows</span></div>',
                unsafe_allow_html=True,
            )
            for col, cov in report["key_column_coverage"].items():
                st.markdown(coverage_rule(col, cov["pct"]), unsafe_allow_html=True)

            if report["empty_columns"]:
                names = ", ".join(report["empty_columns"][:3])
                more = len(report["empty_columns"]) - 3
                st.markdown(
                    f'<div class="note warn">Not tracked at all: {names}'
                    + (f" and {more} more" if more > 0 else "")
                    + ". Questions about these cannot be answered.</div>",
                    unsafe_allow_html=True,
                )

        st.markdown(
            '<div class="provenance">Figures come from fixed analysis functions. '
            'The model chooses which to run and explains the result — it does not '
            'calculate.</div>',
            unsafe_allow_html=True,
        )


def main():
    st.markdown(THEME, unsafe_allow_html=True)

    st.markdown(
        '<div class="masthead">'
        '<div class="eyebrow">Skylark Drones &middot; Pipeline &amp; Delivery</div>'
        '<h1>Business Intelligence</h1>'
        '<div class="sub">Ask about pipeline, revenue, delivery or sector '
        'performance. Every figure carries the share of records behind it.</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    try:
        deals, work_orders, quality, errors = load_data()
    except monday_client.MondayError as exc:
        st.error(f"Cannot reach monday.com. {exc}")
        st.markdown(
            '<div class="note">Set MONDAY_TOKEN, WORK_ORDERS_BOARD_ID and '
            'DEALS_BOARD_ID, then refresh.</div>', unsafe_allow_html=True)
        st.stop()

    if deals is None or work_orders is None:
        st.warning(
            "One board did not load. Questions spanning both sales and delivery "
            "are unavailable until it is reachable."
        )

    sidebar(quality, errors)

    if "agent" not in st.session_state:
        try:
            st.session_state.agent = agent_mod.BIAgent(deals, work_orders)
        except Exception as exc:  # noqa: BLE001
            st.error(f"The agent could not start. {exc}")
            st.markdown('<div class="note">Set GEMINI_API_KEY and refresh.</div>',
                        unsafe_allow_html=True)
            st.stop()

    if "messages" not in st.session_state:
        st.session_state.messages = []

    if not st.session_state.messages:
        st.markdown('<div class="eyebrow" style="margin-top:1.6rem">Start here</div>',
                    unsafe_allow_html=True)
        cols = st.columns(len(SUGGESTIONS))
        for col, text in zip(cols, SUGGESTIONS):
            if col.button(text, use_container_width=True, key=f"s_{text[:18]}"):
                st.session_state.pending = text
                st.rerun()

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("tools"):
                st.markdown(
                    '<div class="provenance">Computed via '
                    + ", ".join(dict.fromkeys(msg["tools"])) + '</div>',
                    unsafe_allow_html=True,
                )

    question = st.chat_input("Ask about the business…")
    if not question and "pending" in st.session_state:
        question = st.session_state.pop("pending")

    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Reading boards…"):
                answer, tools = st.session_state.agent.ask(question)
            st.markdown(answer)
            if tools:
                st.markdown(
                    '<div class="provenance">Computed via '
                    + ", ".join(dict.fromkeys(tools)) + '</div>',
                    unsafe_allow_html=True,
                )

        st.session_state.messages.append(
            {"role": "assistant", "content": answer, "tools": tools}
        )


if __name__ == "__main__":
    main()