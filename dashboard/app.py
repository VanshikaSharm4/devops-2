"""
DevOps Intelligence Platform — Adobe Cloud Manager
Program 19905 · IDFC First Bank Limited
"""
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

st.set_page_config(
    page_title="DevOps Intelligence — IDFC · Program 19905",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Design tokens ─────────────────────────────────────────────────────────────
T = {
    # Sidebar — Adobe Spectrum white
    "sidebar_bg":        "#FFFFFF",
    "sidebar_border":    "#F0F0F0",
    "sidebar_nav_hover": "#F5F5F5",
    "sidebar_nav_active":"#EEF2FF",
    "sidebar_accent":    "#1473E6",
    "sidebar_text":      "#4B4B4B",
    "sidebar_text_dim":  "#999999",
    "sidebar_text_hi":   "#1A1A1A",

    # Content — pure white, feather-light borders
    "bg":       "#FFFFFF",
    "surface":  "#FFFFFF",
    "surface2": "#FAFAFA",
    "border":   "#EFEFEF",
    "border2":  "#F7F7F7",

    # Typography — charcoal, not harsh black
    "text":      "#1A1A1A",
    "text_sub":  "#4B4B4B",
    "text_muted":"#909090",

    # Semantic (kept for status/data colour coding)
    "red":    "#E5484D",
    "amber":  "#C98900",
    "green":  "#2D9D5C",
    "blue":   "#1473E6",
    "purple": "#7C53C3",
    "gray":   "#747474",

    # Chart palette
    "chart": ["#1473E6","#E5484D","#C98900","#2D9D5C","#7C53C3","#747474"],
}

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

/* ─────────────────────────────────────────
   RESET & BASE
───────────────────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; }}
html, body, [class*="css"] {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
    -webkit-font-smoothing: antialiased;
}}

/* ── Force light content area ── */
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
.main .block-container,
[data-testid="stMainBlockContainer"] {{
    background-color: {T['bg']} !important;
    color: {T['text']} !important;
}}
.block-container {{
    padding: 1.25rem 2rem 1.5rem !important;
    max-width: 1440px !important;
}}
[data-testid="stMain"] [data-testid="stVerticalBlock"] {{
    gap: 0.35rem !important;
}}
[data-testid="stMain"] [data-testid="stElementContainer"] {{
    margin-bottom: 0.25rem !important;
}}
[data-testid="stMain"] [data-testid="stHorizontalBlock"] {{
    gap: 0.65rem !important;
}}

/* ── Hide Streamlit chrome ── */
#MainMenu, footer, header,
[data-testid="stDecoration"],
[data-testid="stToolbar"],
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapseButton"] {{ display: none !important; }}

/* ─────────────────────────────────────────
   SIDEBAR
───────────────────────────────────────── */
[data-testid="stSidebar"] {{
    transform: translateX(0) !important;
    display: flex !important;
    flex-direction: column !important;
    visibility: visible !important;
    opacity: 1 !important;
    pointer-events: auto !important;
    background-color: #FAFAFA !important;
    border-right: 1px solid #EBEBEB !important;
    border-left: none !important;
    border-top: none !important;
    border-bottom: none !important;
    position: relative !important;
    flex-shrink: 0 !important;
    overflow: hidden !important;
    /* Smooth width transition — labels clip via overflow, not display:none */
    transition: min-width 0.2s ease, max-width 0.2s ease, width 0.2s ease !important;
    /* width set dynamically by fragment */
}}
[data-testid="stSidebar"] > div:first-child {{
    display: flex !important;
    flex-direction: column !important;
    width: 100% !important;
    flex: 1 1 auto !important;
}}
[data-testid="stSidebar"] > div:first-child > * {{
    width: 100% !important;
    max-width: 100% !important;
}}
[data-testid="stSidebar"] > div,
[data-testid="stSidebar"] > div > div,
[data-testid="stSidebarContent"],
[data-testid="stSidebarUserContent"],
[data-testid="stSidebar"] section[data-testid="stSidebar"] {{
    padding: 0 !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    width: 100% !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
    scrollbar-gutter: auto !important;
}}

/* Multipage nav column — hide but don't leave a click-blocking layer */
[data-testid="stSidebarNav"] {{
    display: none !important;
    pointer-events: none !important;
}}

/* Full-width sidebar blocks (no flex-grow — avoids invisible overlays on nav buttons) */
[data-testid="stSidebar"] [data-testid="stVerticalBlock"],
[data-testid="stSidebar"] [data-testid="stVerticalBlockBorderWrapper"],
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"],
[data-testid="stSidebar"] [data-testid="column"],
[data-testid="stSidebar"] .element-container,
[data-testid="stSidebar"] [data-testid="element-container"] {{
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
}}
/* Markdown must not block clicks on nav buttons beneath overlapping layers */
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] {{
    width: 100% !important;
    max-width: 100% !important;
    pointer-events: none !important;
}}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] * {{
    pointer-events: none !important;
}}

[data-testid="stSidebar"] {{
    color: {T['sidebar_text']} !important;
}}
[data-testid="stSidebar"] p, [data-testid="stSidebar"] span, [data-testid="stSidebar"] label {{
    color: inherit !important;
}}
[data-testid="stSidebar"] hr {{
    border-color: {T['sidebar_border']} !important;
    margin: 0.75rem 1rem !important;
}}

/* Zero-height component iframes must not steal clicks */
iframe[height="0"],
iframe[style*="height: 0"] {{
    pointer-events: none !important;
    position: absolute !important;
    z-index: -1 !important;
}}

/* ── Hamburger button — first button in sidebar, borderless ── */
[data-testid="stSidebar"] .stButton:first-of-type > button {{
    font-size: 1rem !important;
    border: none !important;
    color: #888888 !important;
    padding: 0 !important;
    justify-content: center !important;
}}
/* ── Sidebar nav buttons — flat, no boxes ── */
[data-testid="stSidebar"] .stButton {{
    width: 100% !important;
    position: relative !important;
    z-index: 5 !important;
    pointer-events: auto !important;
}}
[data-testid="stSidebar"] .stButton > div,
[data-testid="stSidebar"] .stButton > div > div {{
    width: 100% !important;
}}
[data-testid="stSidebar"] .stButton > button {{
    pointer-events: auto !important;
    cursor: pointer !important;
    position: relative !important;
    z-index: 6 !important;
    display: flex !important;
    align-items: center !important;
    gap: 0.55rem !important;
    background: transparent !important;
    color: #666666 !important;
    border: none !important;
    border-left: 2px solid transparent !important;
    border-radius: 0 !important;
    text-align: left !important;
    justify-content: flex-start !important;
    font-size: 0.78rem !important;
    font-weight: 400 !important;
    padding: 0 0.75rem !important;
    width: 100% !important;
    min-width: 0 !important;
    margin: 0 !important;
    box-shadow: none !important;
    letter-spacing: 0 !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    transition: color 0.1s, border-color 0.1s !important;
    line-height: 1.4 !important;
}}
[data-testid="stSidebar"] .stButton > button [data-testid="stIconMaterial"] {{
    color: currentColor !important;
    flex: 0 0 auto !important;
    margin: 0 !important;
}}
[data-testid="stSidebar"] .stButton > button:hover {{
    background: transparent !important;
    color: #1A1A1A !important;
    border-left-color: #CCCCCC !important;
    box-shadow: none !important;
}}
/* Active nav item */
[data-testid="stSidebar"] [data-testid="baseButton-primary"] {{
    background: transparent !important;
    color: #1A1A1A !important;
    border-left: 2px solid #1473E6 !important;
    border-top: none !important;
    border-right: none !important;
    border-bottom: none !important;
    font-weight: 600 !important;
}}
/* All sidebar buttons — flat, no box, no border radius */
[data-testid="stSidebar"] button[kind="secondary"],
[data-testid="stSidebar"] button[kind="primary"],
[data-testid="stSidebar"] button {{
    height: 36px !important;
    min-height: 36px !important;
    padding: 0 0 0 12px !important;
    text-align: left !important;
    justify-content: flex-start !important;
    gap: 0.55rem !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    border: none !important;
    border-left: 2px solid transparent !important;
    border-right: none !important;
    border-top: none !important;
    border-bottom: none !important;
    border-radius: 4px !important;
    box-shadow: none !important;
    background: transparent !important;
    outline: none !important;
    color: #555555 !important;
    transition: background 0.1s ease, color 0.1s ease !important;
    width: 100% !important;
}}
[data-testid="stSidebar"] button[kind="primary"] {{
    color: #1473E6 !important;
    background: #EEF2FF !important;
    border-left: 2px solid #1473E6 !important;
    font-weight: 600 !important;
}}
[data-testid="stSidebar"] button[kind="secondary"]:hover {{
    background: #F0F0F0 !important;
    color: #1A1A1A !important;
    border: none !important;
    border-left: 2px solid transparent !important;
}}
[data-testid="stSidebar"] button:focus,
[data-testid="stSidebar"] button:active {{
    border: none !important;
    border-left: 2px solid transparent !important;
    box-shadow: none !important;
    outline: none !important;
}}

/* ─────────────────────────────────────────
   METRIC CARDS — flat, no borders
───────────────────────────────────────── */
[data-testid="metric-container"],
[data-testid="stMetric"] {{
    background: #F5F5F5 !important;
    border: none !important;
    border-radius: 4px !important;
    padding: 1rem 1.1rem !important;
    box-shadow: none !important;
}}
[data-testid="stMetricLabel"] > div,
[data-testid="stMetricLabel"] p {{
    font-size: 0.67rem !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.08em !important;
    color: #888888 !important;
    margin-bottom: 0.25rem !important;
    font-family: 'Inter', -apple-system, sans-serif !important;
}}
[data-testid="stMetricValue"] > div,
[data-testid="stMetricValue"] p {{
    font-size: 1.6rem !important;
    font-weight: 700 !important;
    color: #1A1A1A !important;
    line-height: 1.1 !important;
    letter-spacing: -0.02em !important;
    font-family: 'Inter', -apple-system, sans-serif !important;
}}
[data-testid="stMetricDelta"] {{
    font-size: 0.73rem !important;
    font-weight: 500 !important;
    margin-top: 0.15rem !important;
}}

/* ─────────────────────────────────────────
   PANELS — flat sections, separator only
───────────────────────────────────────── */
.panel {{
    background: transparent;
    border: none;
    border-top: 1px solid #F0F0F0;
    border-radius: 0;
    padding: 1rem 0;
    box-shadow: none;
    margin-bottom: 0.5rem;
}}
.panel:empty {{
    display: none !important;
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    min-height: 0 !important;
    height: 0 !important;
}}
.panel-sm {{
    background: transparent;
    border: none;
    border-top: 1px solid #F0F0F0;
    border-radius: 0;
    padding: 0.75rem 0;
    box-shadow: none;
    margin-bottom: 0.35rem;
}}
/* Commit / execution ID chips */
.id-chip,
[data-testid="stMain"] code.id-chip {{
    display: inline-block !important;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
    font-size: 0.78rem !important;
    font-weight: 600 !important;
    background: #F5F5F5 !important;
    color: #1A1A1A !important;
    -webkit-text-fill-color: #1A1A1A !important;
    border: 1px solid #EFEFEF !important;
    padding: 3px 8px !important;
    border-radius: 5px !important;
    letter-spacing: 0.02em !important;
}}

/* ════════════════════════════════════
   TYPOGRAPHY SYSTEM
   ════════════════════════════════════ */
.pg-title {{
    font-size: 1.35rem;
    font-weight: 700;
    color: {T['text']};
    letter-spacing: -0.02em;
    line-height: 1.3;
    margin: 0;
}}
.pg-sub {{
    font-size: 0.8rem;
    color: {T['text_muted']};
    margin-top: 0.2rem;
    font-weight: 400;
}}
.sec-label {{
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: {T['text_muted']};
    margin-bottom: 0.75rem;
}}
.body-text {{
    font-size: 0.85rem;
    color: {T['text']};
    line-height: 1.6;
}}
.caption {{
    font-size: 0.75rem;
    color: {T['text_muted']};
    line-height: 1.5;
}}

/* ════════════════════════════════════
   BADGES / STATUS PILLS
   ════════════════════════════════════ */
.pill {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    line-height: 1.4;
}}
.pill-dot {{
    width: 6px; height: 6px;
    border-radius: 50%;
    display: inline-block;
    flex-shrink: 0;
}}
.pill-red    {{ background: #FEF0F0; color: {T['red']};    border: none; }}
.pill-amber  {{ background: #FEF8E7; color: {T['amber']};  border: none; }}
.pill-green  {{ background: #EDFAF3; color: {T['green']};  border: none; }}
.pill-blue   {{ background: #EEF3FE; color: {T['blue']};   border: none; }}
.pill-gray   {{ background: #F2F2F2; color: #666666;       border: none; }}

/* ─────────────────────────────────────────
   EXPANDERS — flat, row separator only
───────────────────────────────────────── */
[data-testid="stExpander"] {{
    background: transparent !important;
    border: none !important;
    border-bottom: 1px solid #F0F0F0 !important;
    border-radius: 0 !important;
    margin-bottom: 0 !important;
    overflow: hidden !important;
}}
[data-testid="stExpander"] summary {{
    padding: 0.55rem 0 !important;
    font-size: 0.83rem !important;
    font-weight: 500 !important;
    color: #333333 !important;
    background: transparent !important;
}}
[data-testid="stExpander"] summary:hover {{
    background: transparent !important;
    color: #1A1A1A !important;
}}
[data-testid="stExpander"] [data-testid="stExpanderDetails"] {{
    padding: 0.65rem 0 !important;
    background: transparent !important;
}}

/* ─────────────────────────────────────────
   DATAFRAME / TABLE — row dividers only
───────────────────────────────────────── */
[data-testid="stDataFrame"] {{
    border: none;
    border-radius: 0;
    overflow: hidden;
    background: transparent;
}}
[data-testid="stDataFrame"] th {{
    background: transparent !important;
    font-size: 0.68rem !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.07em !important;
    color: #888888 !important;
    padding: 0.5rem 0.85rem !important;
    border-bottom: 1px solid #EBEBEB !important;
}}
[data-testid="stDataFrame"] td {{
    font-size: 0.82rem !important;
    color: #1A1A1A !important;
    padding: 0.5rem 0.85rem !important;
    border-bottom: 1px solid #F5F5F5 !important;
}}



/* ════════════════════════════════════
   INPUTS
   ════════════════════════════════════ */
[data-testid="stTextInput"] label,
[data-testid="stSelectbox"] label {{
    font-size: 0.75rem !important;
    font-weight: 600 !important;
    color: {T['text_sub']} !important;
    text-transform: uppercase !important;
    letter-spacing: 0.06em !important;
}}
[data-testid="stTextInput"] input,
[data-testid="stTextInput"] [data-baseweb="input"],
[data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stSelectbox"] > div > div,
[data-testid="stSelectbox"] [data-baseweb="select"] {{
    background: #FFFFFF !important;
    border: 1px solid #DEDEDE !important;
    border-radius: 4px !important;
    font-size: 0.84rem !important;
    color: #1A1A1A !important;
    -webkit-text-fill-color: #1A1A1A !important;
    caret-color: #1A1A1A !important;
    box-shadow: none !important;
}}
[data-testid="stTextInput"] input::placeholder,
[data-testid="stTextArea"] textarea::placeholder {{
    color: {T['text_muted']} !important;
    -webkit-text-fill-color: {T['text_muted']} !important;
    opacity: 1 !important;
}}
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus {{
    border-color: {T['blue']} !important;
    box-shadow: 0 0 0 3px {T['blue']}22 !important;
    color: {T['text']} !important;
    -webkit-text-fill-color: {T['text']} !important;
}}
/* Prevent code-block white-text rules from affecting inputs */
[data-testid="stTextInput"] *,
[data-testid="stNumberInput"] *,
[data-testid="stTextArea"] *,
[data-testid="stSelectbox"] * {{
    color: inherit !important;
    -webkit-text-fill-color: inherit !important;
}}
[data-testid="stTextInput"] input,
[data-testid="stTextInput"] [data-baseweb="input"] {{
    color: {T['text']} !important;
    -webkit-text-fill-color: {T['text']} !important;
}}

/* ════════════════════════════════════
   BUTTONS
   ════════════════════════════════════ */
/* ─────────────────────────────────────────
   BUTTONS
───────────────────────────────────────── */
button[kind="primary"] {{
    background: #1473E6 !important;
    color: #FFFFFF !important;
    border: none !important;
    border-radius: 4px !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    padding: 0.4rem 1.1rem !important;
    box-shadow: none !important;
    transition: background 0.12s !important;
}}
button[kind="primary"]:hover {{
    background: #1263CC !important;
    box-shadow: none !important;
}}
button[kind="secondary"] {{
    background: transparent !important;
    color: #444444 !important;
    border: 1px solid #DEDEDE !important;
    border-radius: 4px !important;
    font-size: 0.82rem !important;
    font-weight: 400 !important;
    box-shadow: none !important;
}}
button[kind="secondary"]:hover {{
    background: #F5F5F5 !important;
    color: #1A1A1A !important;
    box-shadow: none !important;
}}

/* ════════════════════════════════════
   TABS
   ════════════════════════════════════ */
[data-baseweb="tab-list"] {{
    background: transparent !important;
    border-bottom: 1px solid {T['border']} !important;
    gap: 0 !important;
    padding: 0 !important;
}}
[data-baseweb="tab"] {{
    font-size: 0.8rem !important;
    font-weight: 500 !important;
    color: {T['text_muted']} !important;
    padding: 0.6rem 1.1rem !important;
    border-radius: 0 !important;
    border-bottom: 2px solid transparent !important;
    transition: all 0.15s !important;
}}
[data-baseweb="tab"]:hover {{
    color: {T['text']} !important;
    background: {T['surface2']} !important;
}}
[aria-selected="true"][data-baseweb="tab"] {{
    color: {T['blue']} !important;
    border-bottom: 2px solid {T['blue']} !important;
    font-weight: 600 !important;
}}

/* ─────────────────────────────────────────
   ALERTS
───────────────────────────────────────── */
[data-testid="stAlert"] {{
    border-radius: 3px !important;
    border-width: 1px !important;
    font-size: 0.83rem !important;
    box-shadow: none !important;
    background: #FAFAFA !important;
}}

/* ════════════════════════════════════
   DIVIDER
   ════════════════════════════════════ */
hr {{
    border-color: {T['border']} !important;
    margin: 0.65rem 0 !important;
}}

/* ════════════════════════════════════
   CODE BLOCKS (legacy block — superseded below)
   ════════════════════════════════════ */

/* ════════════════════════════════════
   SPINNER
   ════════════════════════════════════ */
[data-testid="stSpinner"] p {{
    font-size: 0.82rem !important;
    color: {T['text_muted']} !important;
}}

/* ════════════════════════════════════
   UI FIXES — empty blocks, contrast, readable text
   ════════════════════════════════════ */

/* Strip default white shells; cards come from .panel / metrics / bordered containers */
[data-testid="stMain"] [data-testid="stElementContainer"],
[data-testid="stMain"] [data-testid="stVerticalBlockBorderWrapper"] {{
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
}}
[data-testid="stMain"] [data-testid="stVerticalBlockBorderWrapper"] {{
    padding: 0 !important;
}}
[data-testid="stMain"] [data-testid="column"],
[data-testid="stMain"] [data-testid="column"] > div,
[data-testid="stMain"] [data-testid="stHorizontalBlock"] {{
    background-color: transparent !important;
}}

/* Content card containers — flat, top separator only */
[data-testid="stMain"] [data-testid="stVerticalBlockBorderWrapper"][style*="border"] {{
    background-color: transparent !important;
    border: none !important;
    border-top: 1px solid #EBEBEB !important;
    border-radius: 0 !important;
    padding: 0.85rem 0 !important;
    margin-bottom: 0.5rem !important;
    box-shadow: none !important;
}}

/* HTML panel cards */
.panel {{
    background: transparent !important;
    border: none !important;
    border-top: 1px solid #F0F0F0 !important;
    border-radius: 0 !important;
    padding: 1rem 0 !important;
    margin-bottom: 0.5rem !important;
    color: #1A1A1A !important;
    box-shadow: none !important;
}}
.panel .pg-title,
.panel .body-text,
.panel p,
.panel span,
.panel li {{
    color: {T['text']} !important;
}}
/* Inline code in panels only — not st.code blocks */
.panel p code,
.panel span code {{
    color: {T['text_sub']} !important;
    background: {T['surface2']} !important;
}}
.panel .pg-sub,
.panel .sec-label,
.panel .caption {{
    color: {T['text_muted']} !important;
}}

/* Collapse orphan open/close panel fragment blocks (split st.markdown div tags) */
[data-testid="stMain"] [data-testid="stElementContainer"]:has(.panel:empty),
[data-testid="stMain"] [data-testid="stElementContainer"]:has([data-testid="stMarkdownContainer"]:empty),
[data-testid="stMain"] [data-testid="stMarkdownContainer"]:has(> .panel:empty),
[data-testid="stMain"] [data-testid="stMarkdownContainer"]:has(> div:empty) {{
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
    max-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    border: none !important;
}}
/* Stray closing-tag markdown (</div>) */
[data-testid="stMain"] [data-testid="stMarkdownContainer"] p:empty {{
    display: none !important;
    margin: 0 !important;
    padding: 0 !important;
}}

/* Typography classes in main area */
[data-testid="stMain"] .pg-title,
[data-testid="stMain"] .body-text {{
    color: {T['text']} !important;
}}
[data-testid="stMain"] .pg-sub,
[data-testid="stMain"] .sec-label,
[data-testid="stMain"] .caption {{
    color: {T['text_muted']} !important;
}}

/* Code blocks — flat, no border */
[data-testid="stCode"],
[data-testid="stCodeBlock"],
[data-testid="stCodeBlock"] > div,
[data-testid="stCode"] > div,
.stCodeBlock,
div[data-testid="stCode"],
[data-testid="stMain"] pre {{
    background-color: #F5F5F5 !important;
    border: none !important;
    border-radius: 3px !important;
    color: #1A1A1A !important;
    margin-bottom: 0.35rem !important;
}}
[data-testid="stCode"] pre,
[data-testid="stCode"] code,
[data-testid="stCodeBlock"] pre,
[data-testid="stCodeBlock"] code,
[data-testid="stCode"] [class*="code"],
.stCodeBlock pre,
.stCodeBlock code,
[data-testid="stMain"] pre,
[data-testid="stMain"] pre code {{
    background-color: #F5F5F5 !important;
    color: #1A1A1A !important;
    -webkit-text-fill-color: #1A1A1A !important;
    font-size: 0.8rem !important;
    line-height: 1.5 !important;
}}
[data-testid="stCode"] *:not(input):not(textarea):not(button),
[data-testid="stCodeBlock"] *:not(input):not(textarea):not(button),
.stCodeBlock *:not(input):not(textarea):not(button),
[data-testid="stMain"] pre *:not(input):not(textarea):not(button) {{
    color: #1A1A1A !important;
    -webkit-text-fill-color: #1A1A1A !important;
    background-color: transparent !important;
    text-shadow: none !important;
}}
[data-testid="stCode"] .token,
[data-testid="stCode"] span,
[data-testid="stCodeBlock"] span,
[data-testid="stCode"] div,
[data-testid="stMain"] pre span {{
    color: #1A1A1A !important;
    -webkit-text-fill-color: #1A1A1A !important;
}}
[data-testid="stCode"] .token.comment,
[data-testid="stCode"] .token.string {{
    color: #747474 !important;
    -webkit-text-fill-color: #747474 !important;
}}
[data-testid="stCode"] .token.keyword,
[data-testid="stCode"] .token.function {{
    color: #1A1A1A !important;
    -webkit-text-fill-color: #1A1A1A !important;
}}

/* Expanders — light surface, dark text */
[data-testid="stExpander"] summary,
[data-testid="stExpander"] [data-testid="stExpanderDetails"],
[data-testid="stExpander"] [data-testid="stExpanderDetails"] p,
[data-testid="stExpander"] [data-testid="stExpanderDetails"] span,
[data-testid="stExpander"] [data-testid="stExpanderDetails"] li {{
    color: {T['text']} !important;
}}

/* Tables / dataframes (Glide Data Grid — other pages) */
[data-testid="stDataFrame"],
[data-testid="stDataFrame"] > div,
[data-testid="stDataFrame"] [role="gridcell"],
[data-testid="stDataFrame"] [role="columnheader"],
[data-testid="stDataFrame"] span,
[data-testid="stDataFrame"] p {{
    color: {T['text']} !important;
    -webkit-text-fill-color: {T['text']} !important;
    background-color: {T['surface']} !important;
}}
[data-testid="stDataFrame"] [role="columnheader"],
[data-testid="stDataFrame"] th {{
    color: {T['text_sub']} !important;
    -webkit-text-fill-color: {T['text_sub']} !important;
    background: {T['surface2']} !important;
    font-weight: 600 !important;
}}

/* Alerts & info boxes */
[data-testid="stAlert"],
[data-testid="stNotification"],
[data-testid="stAlert"] p,
[data-testid="stAlert"] div,
[data-testid="stNotification"] p {{
    color: {T['text']} !important;
}}

/* Metrics */
[data-testid="stMain"] [data-testid="stMetric"],
[data-testid="stMain"] [data-testid="metric-container"] {{
    background: #F5F5F5 !important;
    border: none !important;
    border-radius: 4px !important;
    box-shadow: none !important;
}}

/* Sidebar: light text on dark background */
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] span,
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] div {{
    color: {T['sidebar_text_hi']} !important;
}}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p[style*="505060"],
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p[style*="sidebar_text_dim"] {{
    color: {T['sidebar_text_dim']} !important;
}}

/* Plotly chart containers — no extra white tray or excess height */
[data-testid="stPlotlyChart"],
[data-testid="stPlotlyChart"] > div,
[data-testid="stPlotlyChart"] .js-plotly-plot {{
    background: transparent !important;
    min-height: 0 !important;
}}
[data-testid="stPlotlyChart"] {{
    margin-bottom: 0 !important;
}}
/* Hide Plotly legend title (reserved space causes overlap with axis labels) */
[data-testid="stPlotlyChart"] .legendtitletext {{
    display: none !important;
}}

/* Glide Data Grid (Streamlit dataframe) — force readable cells */
div[data-testid="stDataFrame"] div[class*="dvn"],
div[data-testid="stDataFrame"] .gdg-style {{
    --gdg-text-color: {T['text']} !important;
    --gdg-bg-cell: {T['surface']} !important;
    --gdg-header-color: {T['text_muted']} !important;
    --gdg-header-bg: {T['surface2']} !important;
}}
div[data-testid="stDataFrame"] [class*="gdg"] {{
    color: {T['text']} !important;
}}

</style>
""", unsafe_allow_html=True)


# ── Reusable HTML components ──────────────────────────────────────────────────

def pill(label: str, kind: str = "gray") -> str:
    color_map = {
        "High": ("red", "#E5484D"),   "Critical": ("red", "#E5484D"),
        "BLOCK": ("red", "#E5484D"),  "P1": ("red", "#E5484D"),
        "Medium": ("amber", "#E79D13"), "WARN": ("amber", "#E79D13"), "P2": ("amber", "#E79D13"),
        "Low": ("green", "#30A46C"),  "PASS": ("green", "#30A46C"),  "P3": ("blue", "#3D6EEA"),
        "High_conf": ("green", "#30A46C"),
    }
    cls, dot = color_map.get(label, ("gray", "#889098"))
    return (f'<span class="pill pill-{cls}">'
            f'<span class="pill-dot" style="background:{dot}"></span>{label}</span>')


def id_chip(value: str, max_len: int = 14) -> str:
    """Dark monospace chip for commit / execution IDs."""
    text = (value or "—").strip()
    if text and text != "—" and len(text) > max_len:
        text = text[:max_len] + "…"
    return f'<code class="id-chip">{text}</code>'


def section_header(title: str, subtitle: str = "") -> None:
    sub = f'<p class="pg-sub">{subtitle}</p>' if subtitle else ""
    st.markdown(
        f'<div style="margin-bottom:1rem">'
        f'<p class="pg-title">{title}</p>{sub}</div>',
        unsafe_allow_html=True,
    )


def section_label(text: str, *, dark: bool = False) -> None:
    color = T["text"] if dark else T["text_muted"]
    st.markdown(
        f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.09em;color:{color};margin:0 0 0.45rem 0">{text}</p>',
        unsafe_allow_html=True,
    )


@contextmanager
def content_card():
    """Single bordered card — avoids empty white blocks from split <div class='panel'> tags."""
    with st.container(border=True):
        yield


def render_failed_executions_table(df, columns: list, max_rows: int = 20) -> None:
    """Render failed executions as a lightweight HTML table."""
    _LABELS = {
        "executionId":       "Execution ID",
        "pipelineName":      "Pipeline",
        "firstFailedStep":   "Failed Step",
        "Deploy Start Time": "Start Time",
        "Duration (Min)":    "Duration (min)",
    }
    show_cols = [c for c in columns if c in df.columns]
    if df.empty or not show_cols:
        return
    display = df[show_cols].head(max_rows).copy()
    for col in display.columns:
        display[col] = display[col].astype(str)
    _html_table(display, show_cols, _LABELS)


_PINPOINT_COL_LABELS = {
    "executionId": "Execution ID",
    "pipelineName": "Pipeline",
    "firstFailedStep": "Failed Step",
    "Deploy Start Time": "Start Time",
}
_PINPOINT_COL_WIDTHS = [1.1, 1.4, 1, 1.2]

_RISK_COMMIT_COL_LABELS = {
    "sha": "Commit",
    "title": "Message",
    "author": "Author",
    "when": "When",
}
_RISK_COMMIT_COL_WIDTHS = [1.2, 3.2, 1.5, 1.1]


def _html_table(df, show_cols: list, col_labels: dict) -> None:
    """Lightweight static HTML table — no JS overhead, renders instantly."""
    headers = "".join(
        f'<th style="font-size:0.67rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:{T["text_muted"]};padding:0.5rem 0.85rem;'
        f'background:{T["surface2"]};border-bottom:1px solid {T["border"]};'
        f'text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{col_labels.get(c, c)}</th>'
        for c in show_cols
    )
    rows = ""
    for _, row in df.iterrows():
        cells = "".join(
            f'<td style="font-size:0.81rem;color:{T["text"]};padding:0.45rem 0.85rem;'
            f'border-bottom:1px solid {T["border2"]};white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis;max-width:280px">'
            f'{str(row[c])[:90]}</td>'
            for c in show_cols
        )
        rows += f'<tr style="background:{T["surface"]}">{cells}</tr>'
    st.markdown(
        f'<div style="overflow-x:auto;border:1px solid {T["border"]};'
        f'border-radius:10px;margin-bottom:0.65rem">'
        f'<table style="width:100%;border-collapse:collapse;table-layout:fixed">'
        f'<thead><tr>{headers}</tr></thead>'
        f'<tbody>{rows}</tbody>'
        f'</table></div>',
        unsafe_allow_html=True,
    )


def render_selectable_table(
    df,
    columns: list,
    id_column: str,
    session_key: str,
    col_labels: dict,
    col_widths: list,
    hint: str,
    key_prefix: str,
    max_rows: int = 15,
    cell_display=None,
    table_class: str = "",
) -> None:
    """Lightweight HTML table + selectbox for selection — no JS grid re-render on clicks."""
    show_cols = [c for c in columns if c in df.columns]
    if df.empty or not show_cols:
        return
    display_cols = [c for c in show_cols if c != id_column]
    if not display_cols:
        display_cols = show_cols

    display = df[show_cols].head(max_rows).copy()
    for col in display.columns:
        display[col] = display[col].astype(str)

    if session_key not in st.session_state:
        st.session_state[session_key] = ""

    # Static HTML table — renders as plain HTML, zero JS overhead
    _html_table(display, display_cols, col_labels)

    # Lightweight selectbox for row selection
    id_vals = [str(row[id_column]).strip() for _, row in display.iterrows()
               if str(row[id_column]).strip() not in ("", "nan")]
    if not id_vals:
        return

    def _fmt(v: str) -> str:
        if not v:
            return "— select —"
        row = display[display[id_column] == v]
        if not row.empty and display_cols:
            extra = str(row.iloc[0][display_cols[0]])[:55]
            short_id = v[:12] + ("…" if len(v) > 12 else "")
            return f"{short_id}  —  {extra}"
        return v[:30] + ("…" if len(v) > 30 else "")

    sel = st.selectbox(
        hint,
        options=[""] + id_vals,
        format_func=_fmt,
        key=f"_qsel_{key_prefix}",
        label_visibility="collapsed",
    )
    if sel:
        st.session_state[session_key] = sel


def render_failed_executions_selectable(df, columns: list, max_rows: int = 15) -> None:
    """Failure Pinpoint — selectable failed executions table."""
    render_selectable_table(
        df,
        columns=columns,
        id_column="executionId",
        session_key="pinpoint_exec_input",
        col_labels=_PINPOINT_COL_LABELS,
        col_widths=_PINPOINT_COL_WIDTHS,
        hint="Click a row to select that execution for analysis below.",
        key_prefix="pinpoint",
        max_rows=max_rows,
    )


def render_commits_selectable(commits: list, max_rows: int = 15) -> None:
    """Risk Assessment — selectable recent commits table."""
    import pandas as pd

    if not commits:
        return
    rows = []
    for c in commits[:max_rows]:
        title = c.get("title", "")
        if len(title) > 72:
            title = title[:72] + "…"
        rows.append({
            "sha": c.get("sha", ""),
            "title": title,
            "author": c.get("author", "—"),
            "when": c.get("when", "—"),
        })
    df = pd.DataFrame(rows)
    render_selectable_table(
        df,
        columns=["sha", "title", "author", "when"],
        id_column="sha",
        session_key="risk_commit_input",
        col_labels=_RISK_COMMIT_COL_LABELS,
        col_widths=_RISK_COMMIT_COL_WIDTHS,
        hint="Click a row to select that commit for assessment below.",
        key_prefix="risk_commit",
        max_rows=max_rows,
    )


def info_row(items: list[tuple[str, str]]) -> None:
    """Render a horizontal row of label: value pairs."""
    cols = "".join(
        f'<div style="margin-right:2.5rem">'
        f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.07em;color:{T["text_muted"]};margin:0 0 0.2rem 0">{k}</p>'
        f'<p style="font-size:0.88rem;font-weight:600;color:{T["text"]};'
        f'margin:0">{v}</p></div>'
        for k, v in items
    )
    st.markdown(
        f'<div style="display:flex;align-items:flex-start;flex-wrap:wrap;gap:1rem 2rem;'
        f'background:{T["surface2"]};border:none;border-bottom:1px solid {T["border"]};'
        f'border-radius:4px;padding:0.65rem 1rem;margin-bottom:0">{cols}</div>',
        unsafe_allow_html=True,
    )


def action_list(items: list[str]) -> None:
    rows = "".join(
        f'<div style="display:flex;align-items:flex-start;gap:0.75rem;'
        f'padding:0.65rem 0;border-bottom:1px solid {T["border2"]}">'
        f'<span style="font-size:0.72rem;font-weight:700;color:{T["blue"]};'
        f'background:{T["blue"]}18;padding:2px 7px;border-radius:4px;'
        f'flex-shrink:0;margin-top:1px">{i+1}</span>'
        f'<span style="font-size:0.84rem;color:{T["text"]};line-height:1.5">{item}</span>'
        f'</div>'
        for i, item in enumerate(items)
    )
    st.markdown(
        f'<div style="background:{T["surface"]};border:1px solid {T["border"]};'
        f'border-radius:10px;padding:0.25rem 1rem;margin-bottom:0.5rem">{rows}</div>',
        unsafe_allow_html=True,
    )


def risk_banner(level: str, step: str, commit: str = "", duration: str = "") -> None:
    bg  = {"High": "#FEF0F0", "Medium": "#FEFAE8", "Low": "#EDFAF3"}.get(level, "#F4F4F8")
    col = {"High": T["red"],  "Medium": T["amber"], "Low": T["green"]}.get(level, T["gray"])
    meta = " &nbsp;&nbsp; ".join(filter(None, [
        f'Most likely failure: <strong>{step}</strong>' if step else "",
        f'Commit: <code style="font-size:0.78rem">{commit[:8]}</code>' if commit else "",
        f'Est. duration: {duration} min' if duration else "",
    ]))
    st.markdown(
        f'<div style="background:{bg};border:1px solid {col}30;'
        f'border-left:3px solid {col};border-radius:10px;'
        f'padding:1rem 1.25rem;margin-bottom:1.25rem">'
        f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.09em;color:{col};margin:0 0 0.3rem 0">Overall Risk Level</p>'
        f'<p style="font-size:1.5rem;font-weight:800;color:{col};'
        f'letter-spacing:-0.02em;margin:0 0 0.3rem 0">{level}</p>'
        f'<p style="font-size:0.78rem;color:{T["text_sub"]};margin:0">{meta}</p>'
        f'</div>',
        unsafe_allow_html=True,
    )


def cause_card(file: str, line: str, explanation: str, confidence: str) -> None:
    conf_col = {
        "High": T["green"], "Medium": T["amber"], "Low": T["red"]
    }.get(confidence, T["gray"])
    st.markdown(
        f'<div style="background:{T["surface"]};border:1px solid {T["border"]};'
        f'border-left:3px solid {T["red"]};border-radius:10px;'
        f'padding:1.25rem 1.5rem;margin-bottom:1rem">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;'
        f'margin-bottom:0.75rem">'
        f'<div>'
        f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 0.25rem 0">Primary Cause</p>'
        f'<p style="font-family:monospace;font-size:0.88rem;font-weight:600;'
        f'color:{T["text"]};margin:0">{file}</p>'
        f'<p style="font-size:0.78rem;color:{T["text_sub"]};margin:0.15rem 0 0 0">'
        f'Line {line}</p>'
        f'</div>'
        f'<div style="text-align:right">'
        f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 0.25rem 0">Confidence</p>'
        f'<p style="font-size:0.88rem;font-weight:700;color:{conf_col};margin:0">'
        f'{confidence}</p>'
        f'</div>'
        f'</div>'
        f'<p style="font-size:0.83rem;color:{T["text"]};line-height:1.6;margin:0">'
        f'{explanation}</p>'
        f'</div>',
        unsafe_allow_html=True,
    )


def stat_bar(label: str, value: int, max_val: int, color: str) -> None:
    pct = int((value / max_val) * 100) if max_val else 0
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:0.75rem;'
        f'padding:0.45rem 0;border-bottom:1px solid {T["border2"]}">'
        f'<span style="font-size:0.8rem;color:{T["text"]};width:110px;'
        f'flex-shrink:0;font-weight:500">{label}</span>'
        f'<div style="flex:1;height:6px;background:{T["border"]};border-radius:3px">'
        f'<div style="width:{pct}%;height:6px;background:{color};'
        f'border-radius:3px;transition:width 0.3s"></div></div>'
        f'<span style="font-size:0.8rem;font-weight:700;color:{T["text"]};'
        f'width:28px;text-align:right;flex-shrink:0">{value}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )


def chart_theme(height: int = 280, show_legend: bool = False) -> dict:
    return dict(
        height=height,
        margin=dict(t=8, b=8, l=0, r=0),
        plot_bgcolor=T["surface"],
        paper_bgcolor=T["surface"],
        font=dict(family="Inter, sans-serif", size=11, color=T["text_sub"]),
        showlegend=show_legend,
        legend=dict(
            orientation="h", yanchor="top", y=-0.15,
            xanchor="left", x=0, font=dict(size=11),
            bgcolor="rgba(0,0,0,0)", borderwidth=0,
        ),
        xaxis=dict(
            showgrid=False, zeroline=False,
            tickfont=dict(size=11, color=T["text_muted"]),
            linecolor=T["border2"], showline=False,
        ),
        yaxis=dict(
            showgrid=True, gridcolor=T["border2"],
            zeroline=False, tickfont=dict(size=11, color=T["text_muted"]),
            linecolor=T["border2"], showline=False,
        ),
        hoverlabel=dict(
            bgcolor=T["sidebar_bg"], bordercolor=T["sidebar_border"],
            font=dict(size=12, color="#FFFFFF"),
        ),
    )


# ── Shared data ───────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def load_splunk_data():
    from analysis.ingest import load_data, CACHE_FILE, load_csv_data
    import io, sys

    # Capture ingest log lines so we can tell the UI which source was used
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        pipeline_df, failed_df, failed_steps_df, share_map = load_data()
    finally:
        sys.stdout = old_stdout

    log_output = buf.getvalue()
    # Determine data source from log output
    if "Splunk API unreachable" in log_output:
        source = "csv_fallback_network"
    elif "stale disk cache" in log_output:
        source = "stale_cache"
    elif "CSV exports" in log_output:
        source = "csv"
    elif "cached" in log_output or "cache" in log_output.lower():
        source = "cache"
    else:
        source = "live"

    # Auto-ingest real Azure logs for fresh failures into ChromaDB
    # Runs silently — only stores records not yet in the store
    try:
        from vector_store.store import ingest_live_failures
        prod_failed = failed_df[failed_df["pipelineName"] == "Production Pipeline"]
        ingest_live_failures(prod_failed, share_map, pipeline_name="Production Pipeline")
    except Exception:
        pass

    return pipeline_df, failed_df, share_map, source


# ── Sidebar ───────────────────────────────────────────────────────────────────
_PAGE_ICONS = {
    "Overview":              ":material/dashboard:",
    "Pipeline Intelligence": ":material/account_tree:",
    "Failure Analysis":      ":material/report_problem:",
    "Risk Assessment":       ":material/security:",
    "Failure Pinpoint":      ":material/my_location:",
    "Memory Search":         ":material/search:",
    "Memory Explorer":       ":material/travel_explore:",
    "Static Analysis":       ":material/code:",
}
_PAGES = list(_PAGE_ICONS.keys())

if "page" not in st.session_state:
    st.session_state["page"] = "Overview"
if "sb_open" not in st.session_state:
    st.session_state["sb_open"] = True


@st.fragment
def _render_sidebar():
    _open = st.session_state.get("sb_open", True)
    _page = st.session_state.get("page", "Overview")
    _w    = "220px" if _open else "56px"

    # Width CSS only. Closed mode keeps a compact rail for centered icons.
    _collapsed_btn = "" if _open else """
[data-testid="stSidebar"] button {
    padding: 0 !important;
    justify-content: center !important;
    gap: 0 !important;
    letter-spacing: 0 !important;
}
[data-testid="stSidebar"] .stButton > button [data-testid="stMarkdownContainer"] {
    display: none !important;
}
"""
    st.markdown(f"""
<style>
[data-testid="stSidebar"] {{
    min-width: {_w} !important;
    max-width: {_w} !important;
    width:     {_w} !important;
}}
{_collapsed_btn}
</style>
""", unsafe_allow_html=True)

    # ── Hamburger — same height as nav items, no border ──
    if st.button(
        "Menu" if _open else " ",
        key="_hamburger",
        use_container_width=True,
        help="Collapse sidebar" if _open else "Expand sidebar",
        icon=":material/menu:",
    ):
        st.session_state["sb_open"] = not _open
        st.rerun()

    st.markdown('<div style="height:2px"></div>', unsafe_allow_html=True)

    if _open:
        st.markdown(
            f'<div style="height:40px;display:flex;align-items:center;padding:0 12px;overflow:hidden;white-space:nowrap">'
            f'<p style="font-size:0.78rem;font-weight:700;color:#1A1A1A;margin:0;white-space:nowrap">DevOps Intelligence</p>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div style="height:4px"></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)

    # ── Nav items ──
    for _p in _PAGES:
        _active = _page == _p
        _icon   = _PAGE_ICONS[_p]
        if st.button(
            _p if _open else " ",
            key=f"_nav_{_p}",
            use_container_width=True,
            type="primary" if _active else "secondary",
            help=_p,
            icon=_icon,
        ):
            st.session_state["page"] = _p
            st.rerun()

    st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)

    # ── Cache status + refresh (always full text, clipped when narrow) ──
    try:
        from analysis.ingest import cache_info, clear_cache
        _info = cache_info()
        if _info.get("exists") and _info.get("fresh"):
            _sc, _st2 = "#30A46C", f"Cache valid — {_info['age_min']} min"
        elif _info.get("exists"):
            _sc, _st2 = "#E79D13", f"Cache stale — {_info['age_min']} min"
        else:
            _sc, _st2 = "#889098", "No cache"
        if _open:
            st.markdown(
                f'<p style="font-size:0.67rem;color:{_sc};padding:0 12px;margin:0 0 4px;white-space:nowrap;overflow:hidden">'
                f'<span style="display:inline-block;width:5px;height:5px;border-radius:50%;'
                f'background:{_sc};margin-right:4px;vertical-align:middle"></span>{_st2}</p>',
                unsafe_allow_html=True,
            )
        if st.button(
            "Refresh" if _open else " ",
            key="_refresh",
            use_container_width=True,
            help="Refresh data",
            icon=":material/refresh:",
        ):
            clear_cache()
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()
    except Exception:
        pass

    if _open:
        st.markdown(
            f'<p style="font-size:0.62rem;color:#CCCCCC;padding:0.6rem 12px 0.5rem;margin:0;white-space:nowrap;overflow:hidden">'
            f'IDFC First Bank Limited · AEM</p>',
            unsafe_allow_html=True,
        )


with st.sidebar:
    _render_sidebar()


page = st.session_state.get("page", "Overview")

# ═══════════════════════════════════════════════════════════
# PAGE 1 — OVERVIEW
# ═══════════════════════════════════════════════════════════
if page == "Overview":
    section_header("Pipeline Health Overview", "Last 30 days &middot; Live from Splunk")

    with st.spinner("Loading pipeline data..."):
        try:
            pipeline_df, failed_df, share_map, _data_source = load_splunk_data()
        except Exception as e:
            st.error(f"Failed to load data: {e}")
            st.stop()

    # ── Data source banner ────────────────────────────────────────────────
    if _data_source in ("csv_fallback_network", "csv"):
        # Read the actual error that was recorded during the failed fetch
        _splunk_err = ""
        try:
            import json as _sj
            from analysis.ingest import CACHE_DIR as _CACHE_DIR
            _err_file = _CACHE_DIR / "splunk_error.json"
            if _err_file.exists():
                _splunk_err = _sj.loads(_err_file.read_text()).get("error", "")
        except Exception:
            pass
        st.markdown(
            f'<div style="background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.3);'
            f'border-radius:8px;padding:9px 14px;margin-bottom:16px;font-size:12px;'
            f'color:{T["amber"]}">'
            f'⚠️ &nbsp;<b>Splunk API failed</b> — showing data from last CSV export.<br>'
            + (f'<code style="font-size:10px;color:{T["text_muted"]}">{_splunk_err}</code>' if _splunk_err else "")
            + f'</div>',
            unsafe_allow_html=True,
        )
    elif _data_source == "stale_cache":
        st.markdown(
            f'<div style="background:rgba(61,110,234,0.06);border:1px solid rgba(61,110,234,0.2);'
            f'border-radius:8px;padding:9px 14px;margin-bottom:16px;'
            f'display:flex;align-items:center;gap:10px">'
            f'<span style="font-size:14px">🕐</span>'
            f'<span style="font-size:12px;color:{T["blue-l"] if "blue-l" in T else T["blue"]}">'
            f'<b>Stale cache</b> — Splunk API was unreachable. Showing cached data. '
            f'Connect to VPN and refresh to get live data.'
            f'</span></div>',
            unsafe_allow_html=True,
        )

    import plotly.graph_objects as go
    import plotly.express as px
    import pandas as pd

    total     = len(pipeline_df)
    finished  = len(pipeline_df[pipeline_df["Status"] == "FINISHED"])
    failed_n  = len(pipeline_df[pipeline_df["Status"].isin(["FAILED", "ERROR"])])
    cancelled = len(pipeline_df[pipeline_df["Status"] == "CANCELLED"])
    rate      = round(finished / total * 100, 1) if total else 0

    # ── KPI row ──
    k1, k2, k3, k4, k5 = st.columns(5, gap="small")
    k1.metric("Total Executions",  total)
    k2.metric("Completed",          finished)
    k3.metric("Failed",            failed_n)
    k4.metric("Cancelled",         cancelled)
    k5.metric("Completion Rate",   f"{rate}%")


    # ── Charts row ──
    left, right = st.columns([3, 2], gap="medium")

    with left:
        with content_card():
            section_label("Failures by Pipeline Step")

            if not failed_df.empty and "firstFailedStep" in failed_df.columns:
                step_df = failed_df["firstFailedStep"].value_counts().reset_index()
                step_df.columns = ["Step", "Count"]
                max_count = step_df["Count"].max()

                step_colors = [T["red"], T["amber"], T["blue"], T["green"], T["gray"]]
                for i, row in step_df.iterrows():
                    stat_bar(row["Step"], row["Count"], max_count,
                             step_colors[i % len(step_colors)])
            else:
                st.info("No failed step data.")

    with right:
        with content_card():
            section_label("Status Distribution")
            status_counts = pipeline_df["Status"].value_counts()
            fig_pie = go.Figure(go.Pie(
                labels=status_counts.index,
                values=status_counts.values,
                hole=0.68,
                marker=dict(
                    colors=[T["green"], T["red"], T["amber"], T["gray"]],
                    line=dict(color=T["surface"], width=3),
                ),
                textinfo="percent",
                textfont=dict(size=11, color=T["text"]),
                hovertemplate="<b>%{label}</b><br>%{value} executions<br>%{percent}<extra></extra>",
            ))
            theme = chart_theme(240)
            theme.update(
                annotations=[dict(
                    text=f'<b style="font-size:20px">{rate}%</b><br>'
                         f'<span style="font-size:11px;color:{T["text_sub"]}">Success</span>',
                    x=0.5, y=0.5, font_size=14, showarrow=False,
                    font=dict(color=T["text"]),
                )]
            )
            fig_pie.update_layout(**theme)
            st.plotly_chart(fig_pie, use_container_width=True, config={"displayModeBar": False})
            legend_items = list(zip(
                status_counts.index,
                [T["green"], T["red"], T["amber"], T["gray"]],
                status_counts.values,
            ))
            legend_html = "".join(
                f'<span style="display:inline-flex;align-items:center;gap:5px;'
                f'margin-right:14px;font-size:0.75rem;color:{T["text_sub"]}">'
                f'<span style="width:8px;height:8px;border-radius:2px;'
                f'background:{c};flex-shrink:0"></span>{l} <strong style="color:{T["text"]}">{v}</strong></span>'
                for l, c, v in legend_items
            )
            st.markdown(
                f'<div style="text-align:center;padding:0 0 0.5rem 0">{legend_html}</div>',
                unsafe_allow_html=True,
            )


    # ── Trend chart ──
    with content_card():
        section_label("Execution Trend — Last 30 Days")
        if "Deploy Start Time" in pipeline_df.columns:
            df_t = pipeline_df.copy()
            df_t["date"] = pd.to_datetime(df_t["Deploy Start Time"], errors="coerce").dt.date
            df_t = df_t.dropna(subset=["date"])
            daily = df_t.groupby(["date", "Status"]).size().reset_index(name="count")
            color_map = {
                "FINISHED": T["green"], "FAILED": T["red"],
                "ERROR": T["amber"],    "CANCELLED": T["gray"],
            }
            status_order = ["CANCELLED", "FAILED", "ERROR", "FINISHED", "RUNNING"]
            fig_bar = go.Figure()
            for status in status_order:
                sub = daily[daily["Status"] == status]
                if sub.empty:
                    continue
                fig_bar.add_trace(go.Bar(
                    x=sub["date"], y=sub["count"],
                    name=status,
                    marker_color=color_map.get(status, T["gray"]),
                    marker_line_width=0,
                ))
            t3 = chart_theme(210, show_legend=False)
            t3["bargap"] = 0.35
            t3["barmode"] = "stack"
            t3["margin"] = dict(t=4, b=4, l=0, r=0)
            fig_bar.update_layout(**t3)
            st.plotly_chart(fig_bar, use_container_width=True, config={"displayModeBar": False})
            _legend_items = [
                ("CANCELLED", T["gray"]), ("FAILED", T["red"]),
                ("ERROR", T["amber"]), ("FINISHED", T["green"]), ("RUNNING", T["blue"]),
            ]
            st.markdown(
                '<div style="display:flex;gap:16px;flex-wrap:wrap;padding:2px 0">'
                + "".join(
                    f'<span style="display:inline-flex;align-items:center;gap:5px;'
                    f'font-size:0.75rem;color:#555">'
                    f'<span style="width:10px;height:10px;border-radius:2px;background:{c}"></span>'
                    f'{l}</span>'
                    for l, c in _legend_items
                )
                + '</div>',
                unsafe_allow_html=True,
            )

    # ── Table ──
    with content_card():
        section_label("Recent Failed Executions", dark=True)
        if not failed_df.empty:
            render_failed_executions_table(
                failed_df,
                ["executionId", "pipelineName", "firstFailedStep", "Deploy Start Time", "Duration (Min)"],
                max_rows=20,
            )
        else:
            st.success("No recent failures found.")

    # ── Memory stats ──
    try:
        from vector_store.store import memory_stats
        stats = memory_stats()
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        section_label("Memory Store")
        m1, m2, m3 = st.columns(3, gap="small")
        m1.metric("Failures in Memory",      stats.get("failure_memory", 0))
        m2.metric("Scan Findings in Memory", stats.get("scan_memory", 0))
        m3.metric("Cache TTL",               f'{os.getenv("SPLUNK_CACHE_TTL_MINUTES","30")} min')
        st.markdown("</div>", unsafe_allow_html=True)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# PAGE 2 — FAILURE ANALYSIS
# ═══════════════════════════════════════════════════════════
elif page == "Failure Analysis":
    section_header("Failure Analysis", "AI-powered root cause analysis across all pipeline executions")

    report_path = Path("reports/latest_report.json")

    if report_path.exists():
        with open(report_path) as f:
            saved = json.load(f)


        k1, k2, k3, k4 = st.columns(4, gap="small")
        k1.metric("Program",           saved.get("program_id", "—"))
        k2.metric("Total Executions",  saved.get("total_executions", "—"))
        k3.metric("Completion Rate",    f"{saved.get('success_rate_pct', 0):.1f}%")
        k4.metric("Hours Wasted",      f"{saved.get('estimated_hours_wasted', 0):.1f}h")


        # Executive summary
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        section_label("Executive Summary")
        for bullet in saved.get("executive_summary", []):
            st.markdown(
                f'<div style="display:flex;gap:0.75rem;padding:0.6rem 0;'
                f'border-bottom:1px solid {T["border2"]};align-items:flex-start">'
                f'<span style="width:4px;height:4px;border-radius:50%;'
                f'background:{T["blue"]};margin-top:0.45rem;flex-shrink:0"></span>'
                f'<span style="font-size:0.85rem;color:{T["text"]};line-height:1.55">'
                f'{bullet}</span></div>',
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)


        col1, col2 = st.columns(2, gap="medium")

        with col1:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            section_label("Critical Findings")
            for f in saved.get("critical_findings", []):
                with st.expander(
                    f"{f['step']}  —  {f['error_type']}  ·  x{f['occurrence_count']}"
                ):
                    st.markdown(
                        f'<p style="font-size:0.75rem;font-weight:600;color:{T["text_muted"]};'
                        f'text-transform:uppercase;letter-spacing:0.06em;margin:0 0 0.25rem 0">'
                        f'Root Cause</p>'
                        f'<p style="font-size:0.84rem;color:{T["text"]};margin:0 0 0.75rem 0">'
                        f'{f["root_cause"]}</p>'
                        f'<p style="font-size:0.75rem;font-weight:600;color:{T["text_muted"]};'
                        f'text-transform:uppercase;letter-spacing:0.06em;margin:0 0 0.25rem 0">'
                        f'Business Impact</p>'
                        f'<p style="font-size:0.84rem;color:{T["text"]};margin:0 0 0.75rem 0">'
                        f'{f["business_impact"]}</p>',
                        unsafe_allow_html=True,
                    )
                    st.code(f['recommended_fix'], language=None)
            st.markdown("</div>", unsafe_allow_html=True)

        with col2:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            section_label("Recurring Findings")
            for f in saved.get("recurring_findings", []):
                with st.expander(
                    f"{f['step']}  —  {f['error_type']}  ·  x{f['occurrence_count']}"
                ):
                    st.markdown(
                        f'<p style="font-size:0.75rem;font-weight:600;color:{T["text_muted"]};'
                        f'text-transform:uppercase;letter-spacing:0.06em;margin:0 0 0.25rem 0">'
                        f'Root Cause</p>'
                        f'<p style="font-size:0.84rem;color:{T["text"]};margin:0 0 0.75rem 0">'
                        f'{f["root_cause"]}</p>',
                        unsafe_allow_html=True,
                    )
                    st.code(f['recommended_fix'], language=None)
            st.markdown("</div>", unsafe_allow_html=True)

        st.markdown('<div class="panel">', unsafe_allow_html=True)
        section_label("Top Recommended Actions")
        action_list(saved.get("top_recommended_actions", []))
        st.markdown("</div>", unsafe_allow_html=True)

        if Path("reports/latest_report.md").exists():
            with st.expander("View full markdown report"):
                st.markdown(Path("reports/latest_report.md").read_text())
    else:
        st.info("No saved report found. Run an analysis to generate one.")

    if st.button("Run Fresh Analysis", type="primary"):
        with st.spinner("Running AI analysis..."):
            try:
                from analysis.ingest import build_base_bundle
                from analysis.context_builder import build_report_context
                from agent.devops_agent import run_analysis
                bundle, _pdf, _fdf, _ = build_base_bundle(fetch_logs=False)
                ctx    = build_report_context(bundle)
                report = run_analysis(ctx, pipeline_df=_pdf, failed_df=_fdf)

                os.makedirs("reports", exist_ok=True)
                with open("reports/latest_report.json", "w") as f:
                    json.dump(report.model_dump(mode="json"), f, indent=2)
                st.success("Analysis complete. Reload the page to view results.")
                st.cache_data.clear()
                st.cache_resource.clear()
            except Exception as e:
                st.error(f"Analysis failed: {e}")


# ═══════════════════════════════════════════════════════════
# PAGE 3 — RISK ASSESSMENT
# ═══════════════════════════════════════════════════════════
elif page == "Risk Assessment":
    section_header("Risk Assessment", "Should this Dev-pipeline result be promoted to Production?")

    # Load Splunk data for this page
    with st.spinner("Loading pipeline data..."):
        try:
            pipeline_df, failed_df, share_map, _ra_source = load_splunk_data()
        except Exception as _e:
            st.error(f"Could not load data: {_e}")
            st.stop()

    # ── Dev-pipeline executions table ─────────────────────────────────────────
    with content_card():
        section_label("Dev-Pipeline Executions", dark=True)
        st.markdown(
            f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:0 0 10px 0">'
            f'Click a <strong>FINISHED</strong> row to assess Production risk. '
            f'Failed/Error rows block promotion immediately — no analysis needed.</p>',
            unsafe_allow_html=True,
        )

        _dev_df = pipeline_df[pipeline_df["pipelineName"] == "Dev-pipeline"].copy()
        _dev_fdf = failed_df[failed_df["pipelineName"] == "Dev-pipeline"].copy()

        # Merge step info onto dev executions
        if not _dev_fdf.empty and "firstFailedStep" in _dev_fdf.columns:
            _step_map = _dev_fdf.drop_duplicates("executionId")[["executionId","firstFailedStep"]]
            _dev_df = _dev_df.merge(_step_map, on="executionId", how="left")
        else:
            _dev_df["firstFailedStep"] = ""

        _dev_df = _dev_df.sort_values("Deploy Start Time", ascending=False)

        if _dev_df.empty:
            st.info("No Dev-pipeline executions found in the current data export.")
        else:
            # Column headers
            _dh = st.columns([1.2, 1.3, 1.5, 1.8, 1.0])
            for _col, _lbl in zip(_dh, ["Execution", "Status", "Failed Step", "Started", "Duration"]):
                _col.markdown(
                    f'<p style="font-size:0.67rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0;padding:4px 0">{_lbl}</p>',
                    unsafe_allow_html=True,
                )
            st.markdown(f'<hr style="margin:0 0 4px 0;border:none;border-top:1px solid {T["border"]}">', unsafe_allow_html=True)

            _sel_dev = st.session_state.get("risk_dev_exec", "")
            for _, _dr in _dev_df.head(15).iterrows():
                _eid    = str(_dr.get("executionId", ""))
                _status = str(_dr.get("Status", ""))
                _step   = str(_dr.get("firstFailedStep", "") or "")
                _step   = "" if _step == "nan" else _step
                _dur    = _dr.get("Duration (Min)", 0)
                _start  = str(_dr.get("Deploy Start Time", ""))
                try:
                    _start_fmt = pd.to_datetime(_start).strftime("%b %d · %H:%M")
                except Exception:
                    _start_fmt = _start[:16]
                _dur_str = f"{int(_dur)}m" if _dur else "—"
                _is_sel = _eid == _sel_dev

                # Status styling
                _sc = {"FINISHED": T["green"], "FAILED": T["red"],
                       "ERROR": T["red"], "CANCELLED": T["text_muted"]}.get(_status, T["text_muted"])
                _si = {"FINISHED": "✓", "FAILED": "✗", "ERROR": "✗", "CANCELLED": "○"}.get(_status, "·")

                _dc1, _dc2, _dc3, _dc4, _dc5 = st.columns([1.2, 1.3, 1.5, 1.8, 1.0])
                with _dc1:
                    if st.button(
                        _eid[-8:],
                        key=f"_dev_{_eid}",
                        use_container_width=True,
                        type="primary" if _is_sel else "secondary",
                        help=f"Execution {_eid}",
                    ):
                        st.session_state["risk_dev_exec"] = _eid
                        st.session_state["risk_dev_status"] = _status
                        st.session_state["risk_dev_step"] = _step
                        st.session_state.pop("risk_commit_input", None)
                        st.session_state.pop("risk_report", None)
                        st.rerun()
                _dc2.markdown(f'<p style="font-size:0.8rem;color:{_sc};font-weight:600;margin:6px 0">{_si} {_status}</p>', unsafe_allow_html=True)
                _dc3.markdown(f'<p style="font-size:0.78rem;color:{T["red"] if _step else T["text_muted"]};margin:6px 0">{_step or "—"}</p>', unsafe_allow_html=True)
                _dc4.markdown(f'<p style="font-size:0.78rem;color:{T["text_muted"]};margin:6px 0">{_start_fmt}</p>', unsafe_allow_html=True)
                _dc5.markdown(f'<p style="font-size:0.78rem;color:{T["text_muted"]};margin:6px 0">{_dur_str}</p>', unsafe_allow_html=True)

    # ── SHA search bar (manual override) ──────────────────────────────────────
    st.markdown(
        f'<p style="font-size:0.72rem;color:{T["text_muted"]};margin:8px 0 4px 0">'
        f'Or paste a commit SHA directly:</p>',
        unsafe_allow_html=True,
    )
    _search_col, _btn_col = st.columns([5, 1], gap="small")
    with _search_col:
        _sha_input = st.text_input(
            "sha_search",
            placeholder="e.g. 8d024d5490c60e9a32e2d5c54d011e3e70d1e7c",
            label_visibility="collapsed",
            key="_sha_search_input",
        )
    with _btn_col:
        _sha_search_clicked = st.button("Analyse", type="primary", use_container_width=True, key="_sha_search_btn")

    if _sha_search_clicked and _sha_input.strip():
        _cleaned = _sha_input.strip()
        st.session_state["risk_commit_input"] = _cleaned
        st.session_state.pop("risk_dev_exec", None)
        st.session_state.pop("risk_report", None)
        st.rerun()

    # ── Handle selected Dev execution ─────────────────────────────────────────
    _sel_exec   = st.session_state.get("risk_dev_exec", "")
    _sel_status = st.session_state.get("risk_dev_status", "")
    _sel_step   = st.session_state.get("risk_dev_step", "")

    if _sel_exec and "risk_report" not in st.session_state:

        # FAILED / ERROR → block immediately, no LLM needed
        if _sel_status in ("FAILED", "ERROR"):
            _step_label = f" at **{_sel_step}**" if _sel_step else ""
            st.markdown(
                f'<div style="background:rgba(229,72,77,0.07);border:1px solid rgba(229,72,77,0.3);'
                f'border-left:4px solid {T["red"]};border-radius:6px;padding:16px 18px;margin:12px 0">'
                f'<p style="font-size:0.9rem;font-weight:700;color:{T["red"]};margin:0 0 6px 0">'
                f'✗  Do Not Promote to Production</p>'
                f'<p style="font-size:0.83rem;color:{T["text"]};margin:0">'
                f'Dev-pipeline execution <code>{_sel_exec}</code> {_sel_status.lower()}{_step_label}. '
                f'Fix the issue in Dev before triggering Production.</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # CANCELLED → warn, ask for commit SHA
        elif _sel_status == "CANCELLED":
            st.markdown(
                f'<div style="background:rgba(201,137,0,0.07);border:1px solid rgba(201,137,0,0.3);'
                f'border-left:4px solid {T["amber"]};border-radius:6px;padding:16px 18px;margin:12px 0">'
                f'<p style="font-size:0.9rem;font-weight:700;color:{T["amber"]};margin:0 0 6px 0">'
                f'○  Dev-pipeline was Cancelled</p>'
                f'<p style="font-size:0.83rem;color:{T["text"]};margin:0">'
                f'Execution <code>{_sel_exec}</code> was manually cancelled. '
                f'No build result available. Paste the commit SHA below to run a code-based risk assessment.</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # FINISHED → run Production risk assessment
        elif _sel_status == "FINISHED":
            st.markdown(
                f'<div style="background:rgba(45,157,92,0.07);border:1px solid rgba(45,157,92,0.3);'
                f'border-left:4px solid {T["green"]};border-radius:6px;padding:12px 16px;margin:12px 0">'
                f'<p style="font-size:0.83rem;color:{T["green"]};font-weight:600;margin:0">'
                f'✓  Dev-pipeline passed — assessing Production-specific risks...</p>'
                f'</div>',
                unsafe_allow_html=True,
            )
            # Need commit SHA to run code analysis — try to find it from git history
            _auto_sha = st.session_state.get("risk_commit_input", "")
            if not _auto_sha:
                st.info("Paste the commit SHA for this execution above to run the full Production risk assessment.")
            else:
                with st.spinner(f"Assessing Production risk for commit {_auto_sha[:8]}..."):
                    try:
                        from analysis.risk_analyzer import run_pre_deploy_risk, save_risk_report
                        from analysis.ingest import build_base_bundle
                        if "risk_base_bundle" not in st.session_state:
                            _base_bundle, _, _, _ = build_base_bundle(fetch_logs=False)
                            st.session_state["risk_base_bundle"] = _base_bundle
                        else:
                            _base_bundle = st.session_state["risk_base_bundle"]
                        _, report, md = run_pre_deploy_risk(
                            commit_sha=_auto_sha, fetch_logs=False, use_llm=True,
                            bundle=_base_bundle,
                        )
                        save_risk_report(report, md, commit_sha=_auto_sha)
                        st.session_state["risk_report"] = report.model_dump(mode="json")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Assessment failed: {e}")

    # ── SHA-only flow (no dev exec selected) ──────────────────────────────────
    _auto_sha = st.session_state.get("risk_commit_input", "")
    if _auto_sha and not _sel_exec and "risk_report" not in st.session_state:
        with st.spinner(f"Analysing commit {_auto_sha[:8]}..."):
            try:
                from analysis.risk_analyzer import run_pre_deploy_risk, save_risk_report
                from analysis.ingest import build_base_bundle
                if "risk_base_bundle" not in st.session_state:
                    _base_bundle, _, _, _ = build_base_bundle(fetch_logs=False)
                    st.session_state["risk_base_bundle"] = _base_bundle
                else:
                    _base_bundle = st.session_state["risk_base_bundle"]
                _, report, md = run_pre_deploy_risk(
                    commit_sha=_auto_sha, fetch_logs=False, use_llm=True,
                    bundle=_base_bundle,
                )
                save_risk_report(report, md, commit_sha=_auto_sha)
                st.session_state["risk_report"] = report.model_dump(mode="json")
                st.rerun()
            except Exception as e:
                st.error(f"Assessment failed: {e}")

    if "risk_report" in st.session_state:
        import plotly.graph_objects as go

        r = st.session_state["risk_report"]

        # ── Dev-pipeline context banner ───────────────────────────────────────
        _dev_eid = st.session_state.get("risk_dev_exec", "")
        if _dev_eid:
            st.markdown(
                f'<div style="background:{T["surface2"]};border-left:3px solid {T["green"]};'
                f'border-radius:4px;padding:8px 14px;margin-bottom:10px;font-size:0.78rem;color:{T["text_sub"]}">'
                f'<strong style="color:{T["green"]}">✓ Dev-pipeline passed</strong> — '
                f'Execution <code>{_dev_eid}</code> completed successfully. '
                f'Assessment below covers Production-specific risks only.</div>',
                unsafe_allow_html=True,
            )

        # ── Commit metadata strip ─────────────────────────────────────────────
        _sha = r.get("commit_sha") or st.session_state.get("risk_commit_input", "") or ""
        try:
            from connectors.git_connector import get_recent_commits
            _all_commits = get_recent_commits(n=30)
            _meta = next((c for c in _all_commits if c["sha"].startswith(_sha[:8])), None)
        except Exception:
            _meta = None

        if _sha or _meta:
            _title_text = _meta["title"] if _meta else "—"
            _author_text = _meta["author"] if _meta else "—"
            _when_text = _meta["when"] if _meta else "—"
            st.markdown(
                f'<div style="background:{T["surface2"]};border:1px solid {T["border"]};'
                f'border-radius:10px;padding:0.7rem 1rem;margin-bottom:0.65rem;'
                f'display:flex;align-items:center;gap:1.5rem;flex-wrap:wrap">'
                f'<div><p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Commit</p>'
                f'{id_chip(_sha, max_len=len(_sha))}</div>'
                f'<div style="flex:1;min-width:160px"><p style="font-size:0.62rem;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Message</p>'
                f'<p style="font-size:0.83rem;color:{T["text"]};margin:0;font-weight:500">{_title_text[:90]}</p></div>'
                f'<div><p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Author</p>'
                f'<p style="font-size:0.8rem;color:{T["text_sub"]};margin:0">{_author_text}</p></div>'
                f'<div><p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">When</p>'
                f'<p style="font-size:0.8rem;color:{T["text_sub"]};margin:0">{_when_text}</p></div>'
                f'<div><p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Environment</p>'
                f'<span style="background:{T["blue"]}18;color:{T["blue"]};border:1px solid {T["blue"]}44;'
                f'padding:2px 8px;border-radius:4px;font-size:0.72rem;font-weight:600">AEM Cloud Manager</span></div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ── Section 1: Release Risk Overview strip ────────────────────────────
        risk_level = r.get("risk_level", "Unknown")
        _rl_bg  = {"Critical": "#FEF0F0", "High": "#FEF0F0", "Medium": "#FEFAE8", "Low": "#EDFAF3"}.get(risk_level, "#F4F4F8")
        _rl_col = {"Critical": T["red"],  "High":  T["red"],  "Medium": T["amber"],  "Low": T["green"]}.get(risk_level, T["gray"])
        _confidence = r.get("confidence_score", 0)
        _intent = r.get("change_intent", "") or "unknown"
        _br_scope = (r.get("blast_radius_analysis") or {}).get("deployment_scope", "—")
        _fail_step = r.get("most_likely_failure_step", "—")

        # Chip color helpers for scope
        _scope_col = {"isolated": T["green"], "service-wide": T["amber"], "platform-wide": T["red"]}.get(_br_scope, T["gray"])
        _scope_bg  = {"isolated": "#EDFAF3",  "service-wide": "#FEFAE8",  "platform-wide": "#FEF0F0"}.get(_br_scope, "#F4F4F8")

        _strip_cols = st.columns(5, gap="small")

        with _strip_cols[0]:
            st.markdown(
                f'<div style="background:{_rl_bg};border:1px solid {_rl_col}44;border-radius:10px;'
                f'padding:0.85rem 1rem;text-align:center">'
                f'<p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.09em;color:{_rl_col};margin:0 0 0.3rem 0">Risk Level</p>'
                f'<p style="font-size:1.45rem;font-weight:800;color:{_rl_col};'
                f'letter-spacing:-0.02em;margin:0;line-height:1.1">{risk_level}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

        with _strip_cols[1]:
            _conf_col = T["green"] if _confidence >= 70 else T["amber"] if _confidence >= 40 else T["red"]
            st.markdown(
                f'<div style="background:{T["surface"]};border:1px solid {T["border"]};border-radius:10px;'
                f'padding:0.85rem 1rem;text-align:center">'
                f'<p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 0.3rem 0">Confidence</p>'
                f'<p style="font-size:1.45rem;font-weight:800;color:{_conf_col};'
                f'letter-spacing:-0.02em;margin:0;line-height:1.1">{_confidence}%</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

        with _strip_cols[2]:
            st.markdown(
                f'<div style="background:{T["surface"]};border:1px solid {T["border"]};border-radius:10px;'
                f'padding:0.85rem 1rem;text-align:center">'
                f'<p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 0.45rem 0">Change Intent</p>'
                f'<span style="background:{T["blue"]}18;color:{T["blue"]};border:1px solid {T["blue"]}44;'
                f'padding:3px 10px;border-radius:20px;font-size:0.72rem;font-weight:600">'
                f'{_intent}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

        with _strip_cols[3]:
            st.markdown(
                f'<div style="background:{_scope_bg};border:1px solid {_scope_col}44;border-radius:10px;'
                f'padding:0.85rem 1rem;text-align:center">'
                f'<p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 0.45rem 0">Blast Radius</p>'
                f'<span style="background:{_scope_bg};color:{_scope_col};border:1px solid {_scope_col}66;'
                f'padding:3px 10px;border-radius:20px;font-size:0.72rem;font-weight:600">'
                f'{_br_scope}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

        with _strip_cols[4]:
            st.markdown(
                f'<div style="background:{T["surface"]};border:1px solid {T["border"]};border-radius:10px;'
                f'padding:0.85rem 1rem;text-align:center">'
                f'<p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 0.35rem 0">Most Likely Failure</p>'
                f'<p style="font-size:0.83rem;font-weight:700;color:{T["red"]};'
                f'margin:0;line-height:1.3;word-break:break-word">{_fail_step}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )


        # ── Row 1: AI Summary + Affected Modules ─────────────────────────────
        _col_sum, _col_mod = st.columns([11, 9], gap="medium")

        with _col_sum:
            with content_card():
                _bullets = []
                for _d in r.get("primary_risk_drivers", [])[:5]:
                    _txt = _d.get("driver", "")
                    _det = _d.get("detail", "")
                    _sig = _d.get("signal_strength", "")
                    _sig_col = T["red"] if _sig == "HIGH" else T["amber"] if _sig == "MEDIUM" else T["green"]
                    _rf = _d.get("related_file", "")
                    _rf_html = f' <code style="font-size:0.72rem;background:{T["surface2"]};border:1px solid {T["border"]};padding:1px 5px;border-radius:3px;color:{T["text_sub"]}">{_rf}</code>' if _rf else ""
                    _det_html = f'<br><span style="font-size:0.75rem;color:{T["text_muted"]}">{_det}</span>' if _det else ''
                    _bullets.append(
                        f'<div style="display:flex;align-items:flex-start;gap:0.6rem;'
                        f'padding:0.45rem 0;border-bottom:1px solid {T["border2"]}">'
                        f'<span style="width:6px;height:6px;border-radius:50%;background:{_sig_col};'
                        f'flex-shrink:0;margin-top:0.45rem"></span>'
                        f'<span style="font-size:0.83rem;color:{T["text"]};line-height:1.55">'
                        f'{_txt}{_rf_html}{_det_html}'
                        f'</span></div>'
                    )
                if not _bullets:
                    import re as _re2
                    _nar = r.get("narrative", "") or r.get("reasoning", "") or ""
                    for _s in _re2.split(r'(?<=[.!?])\s+', _nar)[:5]:
                        if len(_s.strip()) > 20:
                            _bullets.append(
                                f'<div style="display:flex;align-items:flex-start;gap:0.6rem;'
                                f'padding:0.45rem 0;border-bottom:1px solid {T["border2"]}">'
                                f'<span style="width:6px;height:6px;border-radius:50%;background:{T["blue"]};'
                                f'flex-shrink:0;margin-top:0.45rem"></span>'
                                f'<span style="font-size:0.83rem;color:{T["text"]};line-height:1.55">{_s.strip()}</span>'
                                f'</div>'
                            )
                if _bullets:
                    st.markdown("".join(_bullets), unsafe_allow_html=True)
                else:
                    st.markdown(f'<p style="font-size:0.83rem;color:{T["text_muted"]}">No summary available.</p>', unsafe_allow_html=True)

        with _col_mod:
            with content_card():
                section_label("Affected Modules")
                _module_colors = {
                    "ui.frontend": T["blue"],  "ui.apps":    T["purple"],
                    "core":        T["red"],   "dispatcher": T["amber"],
                    "ui.config":   T["green"], "ui.content": T["gray"],
                }
                _modules = r.get("modules_at_risk", [])
                if _modules:
                    for _m in _modules:
                        _mc = next((v for k, v in _module_colors.items() if k in _m.lower()), T["gray"])
                        st.markdown(
                            f'<div style="display:flex;align-items:center;gap:0.75rem;'
                            f'padding:0.45rem 0;border-bottom:1px solid {T["border2"]}">'
                            f'<span style="width:8px;height:8px;border-radius:2px;'
                            f'background:{_mc};flex-shrink:0"></span>'
                            f'<span style="font-family:monospace;font-size:0.83rem;'
                            f'font-weight:600;color:{T["text"]}">{_m}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                else:
                    st.markdown(f'<p style="font-size:0.83rem;color:{T["text_muted"]}">No modules flagged.</p>', unsafe_allow_html=True)
                _bra = r.get("blast_radius_analysis") or {}
                if _bra:
                    _dc = ", ".join(_bra.get("downstream_consumers", [])) or "—"
                    st.markdown(
                        f'<div style="margin-top:0.5rem;padding-top:0.5rem;'
                        f'border-top:1px solid {T["border"]}">'
                        f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                        f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.35rem 0">Blast Radius</p>'
                        f'<div style="display:flex;gap:1rem;flex-wrap:wrap">'
                        f'<span style="font-size:0.78rem;color:{T["text_sub"]}">Scope: <strong style="color:{T["text"]}">{_bra.get("deployment_scope","—")}</strong></span>'
                        f'<span style="font-size:0.78rem;color:{T["text_sub"]}">Rollback: <strong style="color:{T["text"]}">{_bra.get("rollback_complexity","—")}</strong></span>'
                        f'</div>'
                        f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:0.25rem 0 0 0">Downstream: {_dc}</p>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

        # ── Historical Matches ────────────────────────────────────────────────
        with content_card():
            section_label("Historical Matches")
            _hist_hits = []
            try:
                from vector_store.store import find_similar_failures
                import re as _re_hist
                _mods = r.get("modules_at_risk", [])
                _q_signal = " ".join(_mods) + " " + r.get("change_intent", "")
                # Pass the target pipeline name so Dev-pipeline records are excluded
                _target_pipeline = pipeline_df["pipelineName"].mode()[0] if not pipeline_df.empty else ""
                _hist_hits = find_similar_failures(
                    error_type=r.get("most_likely_failure_step", "build"),
                    error_message=_q_signal,
                    key_lines=_mods,
                    step=r.get("most_likely_failure_step", ""),
                    top_k=6,
                    pipeline=_target_pipeline,
                )
            except Exception:
                pass

            # ── Deduplicate near-identical hits ───────────────────────────────
            if _hist_hits:
                from difflib import SequenceMatcher
                def _text_sim(a: str, b: str) -> float:
                    return SequenceMatcher(None, a.lower()[:200], b.lower()[:200]).ratio()
                _deduped = []
                for _h in _hist_hits:
                    _rc  = (_h.get("root_cause") or "").strip()
                    _fix = (_h.get("fix") or "").strip()
                    _stp = (_h.get("step") or "").strip()
                    _dup = any(
                        (_stp == (d.get("step") or "").strip()) and
                        _text_sim(_rc,  (d.get("root_cause") or "")) > 0.65 and
                        _text_sim(_fix, (d.get("fix") or ""))        > 0.65
                        for d in _deduped
                    )
                    if not _dup:
                        _deduped.append(_h)
                _hist_hits = _deduped

            if _hist_hits:
                # ── Detect recurring patterns ─────────────────────────
                _step_counts: dict = {}
                for _h in _hist_hits:
                    _s = (_h.get("step") or "").strip()
                    if _s:
                        _step_counts[_s] = _step_counts.get(_s, 0) + 1
                _recurring_steps = {s for s, n in _step_counts.items() if n >= 2}

                # ── Aggregate banner ──────────────────────────────────
                _avg_score = int(sum(_h.get("similarity_score", 0) for _h in _hist_hits) / len(_hist_hits) * 100)
                _high_matches = sum(1 for _h in _hist_hits if _h.get("similarity_score", 0) >= 0.85)

                # Recurring steps (same step appears ≥2× in results) always escalate
                # to at least SEEN BEFORE — "LOW OVERLAP" must not appear alongside them.
                _has_recurring = bool(_recurring_steps)
                _banner_color = T["red"] if _high_matches >= 2 else T["amber"] if (_high_matches >= 1 or _has_recurring) else T["green"]
                _banner_bg    = "rgba(255,99,99,0.07)" if _high_matches >= 2 else "rgba(245,166,35,0.07)" if (_high_matches >= 1 or _has_recurring) else "rgba(62,207,142,0.07)"
                _banner_label = "HIGH RISK — pattern seen before" if _high_matches >= 2 else "SEEN BEFORE — recurring step pattern matched" if _has_recurring else "SEEN BEFORE — similar failures exist" if _high_matches >= 1 else "LOW OVERLAP — mostly new territory"

                st.markdown(
                    f'<div style="background:{_banner_bg};border:1px solid {_banner_color}44;'
                    f'border-radius:8px;padding:10px 14px;margin-bottom:12px;'
                    f'display:flex;align-items:center;justify-content:space-between;gap:8px">'
                    f'<div style="display:flex;align-items:center;gap:8px">'
                    f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{_banner_color};flex-shrink:0"></span>'
                    f'<div>'
                    f'<p style="font-size:0.7rem;font-weight:800;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:{_banner_color};margin:0">{_banner_label}</p>'
                    f'<p style="font-size:0.72rem;color:{T["text_muted"]};margin:0">'
                    f'{len(_hist_hits)} observed failure records matched &nbsp;·&nbsp; avg similarity {_avg_score}%</p>'
                    f'</div></div>'
                    + (
                        f'<div style="text-align:right">'
                        f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                        f'letter-spacing:0.07em;color:{T["red"]};margin:0">Recurring</p>'
                        f'<p style="font-size:0.72rem;color:{T["text_muted"]};margin:0">'
                        + ", ".join(_recurring_steps) + f'</p></div>'
                        if _recurring_steps else ""
                    )
                    + f'</div>',
                    unsafe_allow_html=True,
                )

                # ── Recurring pattern alert ───────────────────────────
                if _recurring_steps:
                    for _rs in _recurring_steps:
                        _rs_hits = [_h for _h in _hist_hits if (_h.get("step") or "").strip() == _rs]
                        _rs_causes = list({(_h.get("root_cause") or "")[:80] for _h in _rs_hits if _h.get("root_cause")})
                        st.markdown(
                            f'<div style="background:rgba(255,99,99,0.06);border:1px solid {T["red"]}33;'
                            f'border-left:3px solid {T["red"]};border-radius:6px;'
                            f'padding:8px 12px;margin-bottom:8px">'
                            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
                            f'<span style="font-size:0.68rem;font-weight:800;text-transform:uppercase;'
                            f'letter-spacing:0.08em;color:{T["red"]}">Recurring · {_rs}</span>'
                            f'<span style="font-size:0.68rem;color:{T["text_muted"]}">'
                            f'failed {len(_rs_hits)}× in observed memory</span>'
                            f'</div>'
                            + (
                                f'<p style="font-size:0.75rem;color:{T["text_sub"]};margin:0">'
                                + " &nbsp;/&nbsp; ".join(_rs_causes[:2]) + f'</p>'
                                if _rs_causes else ""
                            )
                            + f'</div>',
                            unsafe_allow_html=True,
                        )

                # ── Match cards ───────────────────────────────────────
                for _hi, _h in enumerate(_hist_hits):
                    _eid        = str(_h.get("execution_id", "—"))
                    _step       = (_h.get("step") or "").strip()
                    _root_cause = (_h.get("root_cause") or "").strip()
                    _fix        = (_h.get("fix") or "").strip()
                    _err_msg    = (_h.get("error_message") or "").strip()
                    _score      = int(_h.get("similarity_score", 0) * 100)
                    _sim_col    = T["red"] if _score >= 85 else T["amber"] if _score >= 70 else T["green"]
                    _is_recur   = _step in _recurring_steps

                    # Source type detection
                    _sha_match = _re_hist.search(r"risk-([a-f0-9]{6,12})-(\w+)", _eid)
                    _rpt_match = _re_hist.match(r"report:(.+):\d+", _eid)
                    if _eid.isdigit():
                        _src_label = "#" + _eid[:10]
                        _src_col   = T["blue"]
                        _src_icon  = "#"
                    elif _sha_match:
                        _src_label = "commit " + _sha_match.group(1)
                        _src_col   = T.get("purple", "#A855F7")
                        _src_icon  = "~"
                        _step      = _step or _sha_match.group(2)
                    elif _rpt_match:
                        _src_label = "report"
                        _src_col   = T["amber"]
                        _src_icon  = "F"
                    else:
                        _src_label = _eid[:14]
                        _src_col   = T["text_muted"]
                        _src_icon  = "·"

                    # Changed files
                    _changed_files = [
                        f.strip() for f in _err_msg.split("|") if f.strip()
                    ] if "|" in _err_msg else (
                        [_err_msg] if _err_msg and len(_err_msg) < 120 else []
                    )

                    # Build card parts separately to avoid f-string nesting issues
                    _card_border = T["red"] + "55" if _is_recur else T["border"]
                    _card_left   = f'border-left:3px solid {T["red"]};' if _is_recur else ""

                    _step_badge = (
                        f'<span style="font-size:0.65rem;font-weight:700;text-transform:uppercase;'
                        f'letter-spacing:0.06em;color:{T["text_muted"]};background:{T["surface2"]};'
                        f'border:1px solid {T["border"]};padding:2px 7px;border-radius:4px">{_step}</span>'
                    ) if _step else ""

                    _recur_badge = (
                        f'<span style="font-size:0.62rem;font-weight:800;text-transform:uppercase;'
                        f'letter-spacing:0.06em;color:{T["red"]};background:rgba(255,99,99,0.1);'
                        f'border:1px solid {T["red"]}44;padding:2px 7px;border-radius:4px">recurring</span>'
                    ) if _is_recur else ""

                    _cause_short = (_root_cause[:140] + "…") if len(_root_cause) > 140 else _root_cause
                    _fix_short   = (_fix[:130] + "…") if len(_fix) > 130 else _fix
                    _needs_expand = len(_root_cause) > 140 or len(_fix) > 130

                    _cause_html = (
                        f'<p style="font-size:0.8rem;color:{T["text"]};margin:0 0 5px 0;line-height:1.5">'
                        f'{_cause_short}</p>'
                    ) if _root_cause else ""

                    _fix_html = (
                        f'<div style="display:flex;align-items:flex-start;gap:6px;'
                        f'background:rgba(62,207,142,0.05);border-left:2px solid {T["green"]};'
                        f'padding:4px 8px;border-radius:0 4px 4px 0;margin-bottom:5px">'
                        f'<span style="font-size:0.65rem;font-weight:800;text-transform:uppercase;'
                        f'color:{T["green"]};letter-spacing:0.06em;padding-top:1px;white-space:nowrap">Fix</span>'
                        f'<span style="font-size:0.77rem;color:{T["text_sub"]};line-height:1.4">{_fix_short}</span>'
                        f'</div>'
                    ) if _fix else ""

                    _file_chips = "".join(
                        f'<code style="font-size:0.66rem;background:{T["surface2"]};'
                        f'border:1px solid {T["border"]};padding:1px 6px;'
                        f'border-radius:3px;color:{T["text_muted"]}">{_cf.split("/")[-1]}</code>'
                        for _cf in _changed_files[:5]
                    )
                    _extra_files = (
                        f'<span style="font-size:0.66rem;color:{T["text_muted"]};padding:1px 4px">'
                        f'+{len(_changed_files)-5} more</span>'
                    ) if len(_changed_files) > 5 else ""
                    _files_html = (
                        f'<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:3px">'
                        f'{_file_chips}{_extra_files}</div>'
                    ) if _changed_files else ""

                    _expander_label = f"{_score}%  ·  {_step or '—'}{'  · recurring' if _is_recur else ''}  ·  {_src_label}"
                    with st.expander(_expander_label, expanded=False):
                        st.markdown(
                            f'<div style="background:{T["surface"]};border:1px solid {_card_border};'
                            f'border-radius:8px;padding:10px 13px;{_card_left}">'
                            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:7px">'
                            f'<div style="background:{_sim_col}18;border:1px solid {_sim_col}44;'
                            f'border-radius:5px;padding:2px 9px;min-width:46px;text-align:center">'
                            f'<span style="font-size:0.78rem;font-weight:800;color:{_sim_col}">{_score}%</span>'
                            f'</div>'
                            f'<div style="flex:1;background:{T["border"]};border-radius:3px;height:5px">'
                            f'<div style="width:{_score}%;height:5px;border-radius:3px;background:{_sim_col}"></div>'
                            f'</div>'
                            f'{_step_badge}{_recur_badge}'
                            f'<span style="font-size:0.68rem;color:{_src_col};white-space:nowrap">'
                            f'{_src_icon} {_src_label}</span>'
                            f'</div>'
                            f'{_files_html}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                        if _root_cause:
                            st.markdown(f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:{T["text_muted"]};margin:8px 0 4px 0">Root Cause</p>', unsafe_allow_html=True)
                            st.markdown(f'<p style="font-size:0.82rem;color:{T["text"]};line-height:1.6;margin:0">{_root_cause}</p>', unsafe_allow_html=True)
                        if _fix:
                            st.markdown(f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:{T["green"]};margin:10px 0 4px 0">Fix</p>', unsafe_allow_html=True)
                            st.markdown(f'<p style="font-size:0.82rem;color:{T["text_sub"]};line-height:1.6;margin:0">{_fix}</p>', unsafe_allow_html=True)

            else:
                st.markdown(
                    f'<div style="background:{T["surface2"]};border:1px solid {T["border"]};'
                    f'border-radius:8px;padding:24px 16px;text-align:center;margin-top:8px">'
                    f'<p style="font-size:0.82rem;font-weight:600;color:{T["text"]};margin:0 0 4px 0">'
                    f'No historical matches yet</p>'
                    f'<p style="font-size:0.73rem;color:{T["text_muted"]};margin:0">'
                    f'Run more analyses to build memory.<br>Each run teaches the system your failure patterns.</p>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


        # ── Recommended Actions ───────────────────────────────────────────────
        _rec_actions = r.get("recommended_actions", [])
        if _rec_actions:
            with content_card():
                section_label("Recommended Actions")
                action_list(_rec_actions)

        # ── Technical Details (collapsed) ─────────────────────────────────────
        with st.expander("TECHNICAL DETAILS", expanded=False):
            _hypotheses = r.get("technical_failure_hypotheses", [])
            _has_hypotheses = bool(_hypotheses)

            # ── Technical Failure Hypotheses ──────────────────────────────────
            if _has_hypotheses:
                with content_card():
                    section_label("Technical Failure Hypotheses")

                    def _hyp_chip_color(ftype: str) -> str:
                        _ft = ftype.lower()
                        if any(x in _ft for x in ("osgi_activation", "dependency_injection")):
                            return "red"
                        if any(x in _ft for x in ("classpath_conflict", "api_contract_mismatch")):
                            return "amber"
                        if any(x in _ft for x in ("auth_regression", "security")):
                            return "red"
                        if any(x in _ft for x in ("cache_invalidation", "config_propagation")):
                            return "amber"
                        if "deployment_ordering" in _ft:
                            return "purple"
                        if any(x in _ft for x in ("integration_timeout", "resource_resolver_leak")):
                            return "amber"
                        if any(x in _ft for x in ("serialization_failure", "schema_mismatch")):
                            return "blue"
                        return "gray"

                    _lk_color = {"High": "red", "Medium": "amber", "Low": "green"}
                    _stage_color = {
                        "build": "blue", "deploy": "amber", "securityTest": "red",
                        "activation": "purple", "codeQuality": "gray",
                    }

                    def _chip(label, kind):
                        _bg_map  = {"red": "#FEF0F0", "amber": "#FEFAE8", "green": "#EDFAF3",
                                    "blue": "#EEF3FE", "purple": "#F3EEFF", "gray": "#F4F4F8"}
                        _col_map = {"red": T["red"], "amber": T["amber"], "green": T["green"],
                                    "blue": T["blue"], "purple": T["purple"], "gray": T["gray"]}
                        _brd_map = {"red": "#FBCECE", "amber": "#F5E0A0", "green": "#BDECD3",
                                    "blue": "#C0D2FA", "purple": "#D4C0F8", "gray": "#D8D8E8"}
                        return (
                            f'<span style="background:{_bg_map[kind]};color:{_col_map[kind]};'
                            f'border:1px solid {_brd_map[kind]};padding:2px 9px;border-radius:20px;'
                            f'font-size:0.7rem;font-weight:600;margin-right:5px">{label}</span>'
                        )

                    for _h in _hypotheses:
                        _ft    = _h.get("failure_type", "unknown")
                        _lk    = _h.get("likelihood", "Medium")
                        _cn    = _h.get("confidence", 0)
                        _stage = _h.get("deployment_stage", "")
                        _hc    = _hyp_chip_color(_ft)
                        _lkc   = _lk_color.get(_lk, "gray")
                        _stc   = _stage_color.get(_stage, "gray")

                        _trigger = _h.get("trigger_mechanism", "")
                        _impact  = _h.get("runtime_impact", "")
                        _vsteps  = _h.get("verification_steps", [])
                        _se      = _h.get("supporting_evidence", [])
                        _ce      = _h.get("counterevidence", [])

                        _vstep_html = "".join(
                            f'<li style="font-size:0.82rem;color:{T["text"]};line-height:1.55;margin-bottom:0.25rem">{vs}</li>'
                            for vs in _vsteps
                        )
                        _se_html = "  ·  ".join(
                            f'<span style="font-size:0.74rem;color:{T["text_muted"]}">{e}</span>' for e in _se
                        )
                        _ce_html = " &nbsp;&middot;&nbsp; ".join(
                            f'<em style="font-size:0.74rem;color:{T["text_muted"]}">{e}</em>' for e in _ce
                        )

                        def _field(label, value, label_color=None):
                            lc = label_color or T["text_muted"]
                            return (
                                f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                                f'letter-spacing:0.08em;color:{lc};margin:0.6rem 0 0.2rem 0">{label}</p>'
                                f'<p style="font-size:0.84rem;color:{T["text"]};line-height:1.55;margin:0">{value}</p>'
                            )

                        st.markdown(
                            f'<div style="background:{T["surface"]};border:1px solid {T["border"]};'
                            f'border-radius:8px;padding:12px 14px;margin-bottom:10px">'
                            f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">'
                            f'<div>{_chip(_ft.replace("_", " "), _hc)}{(_chip(_stage, _stc) if _stage else "")}{_chip(_lk, _lkc)}</div>'
                            f'<span style="font-size:0.75rem;font-weight:600;color:{T["text_muted"]}">Confidence: {_cn}%</span>'
                            f'</div>'
                            + (_field("Trigger", _trigger) if _trigger else "")
                            + (_field("Runtime Impact", _impact) if _impact else "")
                            + (f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:{T["text_muted"]};margin:0.6rem 0 0.2rem 0">Verification Steps</p>'
                               f'<ul style="margin:0;padding-left:1.25rem">{_vstep_html}</ul>' if _vsteps else "")
                            + (_field("Evidence", _se_html) if _se else "")
                            + (f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:{T["amber"]};margin:0.6rem 0 0.2rem 0">Counterevidence</p>'
                               f'<p style="margin:0">{_ce_html}</p>' if _ce else "")
                            + f'</div>',
                            unsafe_allow_html=True,
                        )

            # ── Primary Risk Drivers ───────────────────────────────────────────
            _drivers = r.get("primary_risk_drivers", [])
            if _drivers:
                with content_card():
                    section_label("Primary Risk Drivers")
                    _sig_color = {"HIGH": T["red"], "MEDIUM": T["amber"], "LOW": T["green"]}
                    _sig_bg    = {"HIGH": "#FEF0F0", "MEDIUM": "#FEFAE8", "LOW": "#EDFAF3"}
                    _sig_brd   = {"HIGH": "#FBCECE", "MEDIUM": "#F5E0A0", "LOW": "#BDECD3"}
                    for _i, _d in enumerate(_drivers):
                        _sig = _d.get("signal_strength", "MEDIUM")
                        _sc  = _sig_color.get(_sig, T["gray"])
                        _sb  = _sig_bg.get(_sig, "#F4F4F8")
                        _sbr = _sig_brd.get(_sig, "#D8D8E8")
                        _rf  = _d.get("related_file", "")
                        _rf_html = (
                            f'<code style="font-size:0.72rem;background:{T["surface2"]};'
                            f'border:1px solid {T["border"]};padding:1px 6px;border-radius:4px;'
                            f'color:{T["text_sub"]}">{_rf}</code>'
                            if _rf else ""
                        )
                        st.markdown(
                            f'<div style="display:flex;align-items:flex-start;gap:0.75rem;'
                            f'padding:0.55rem 0;border-bottom:1px solid {T["border2"]}">'
                            f'<span style="background:{_sb};color:{_sc};border:1px solid {_sbr};'
                            f'padding:2px 8px;border-radius:4px;font-size:0.68rem;font-weight:700;'
                            f'flex-shrink:0;margin-top:2px;white-space:nowrap">{_sig}</span>'
                            f'<div style="flex:1;min-width:0">'
                            f'<p style="font-size:0.84rem;font-weight:600;color:{T["text"]};'
                            f'margin:0 0 0.15rem 0;line-height:1.4">{_d.get("driver","")}</p>'
                            f'<p style="font-size:0.76rem;color:{T["text_muted"]};margin:0 0 0.2rem 0">'
                            f'{_d.get("evidence_type","")} &nbsp;&middot;&nbsp; {_d.get("detail","")}</p>'
                            f'{_rf_html}'
                            f'</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

            # ── Step Risk Summary ──────────────────────────────────────────────
            _step_risks = r.get("step_risks", [])
            if _step_risks:
                with content_card():
                    section_label("Step Risk Summary")
                    _slv_col = {"Critical": T["red"], "High": T["red"], "Medium": T["amber"], "Low": T["green"]}
                    _slv_bg  = {"Critical": "#FEF0F0", "High": "#FEF0F0", "Medium": "#FEFAE8", "Low": "#EDFAF3"}
                    _slv_brd = {"Critical": "#FBCECE", "High": "#FBCECE", "Medium": "#F5E0A0", "Low": "#BDECD3"}
                    for _sr in _step_risks:
                        _slv = _sr.get("level", "Low")
                        _scc = _slv_col.get(_slv, T["gray"])
                        _scb = _slv_bg.get(_slv, "#F4F4F8")
                        _scbr = _slv_brd.get(_slv, "#D8D8E8")
                        st.markdown(
                            f'<div style="display:flex;align-items:flex-start;gap:0.75rem;'
                            f'padding:0.5rem 0;border-bottom:1px solid {T["border2"]}">'
                            f'<span style="font-size:0.78rem;font-weight:700;color:{T["text_sub"]};'
                            f'width:110px;flex-shrink:0;padding-top:2px">{_sr.get("step","")}</span>'
                            f'<span style="background:{_scb};color:{_scc};border:1px solid {_scbr};'
                            f'padding:2px 8px;border-radius:4px;font-size:0.68rem;font-weight:700;'
                            f'flex-shrink:0;white-space:nowrap">{_slv}</span>'
                            f'<span style="font-size:0.8rem;color:{T["text_sub"]};line-height:1.5">'
                            f'{_sr.get("rationale","")}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )


            # ── Blast Radius Analysis ─────────────────────────────────────────
            _bra = r.get("blast_radius_analysis")
            if _bra:
                with content_card():
                    section_label("Blast Radius Analysis")
                    _dc_list = _bra.get("downstream_consumers", [])
                    _dc_str  = ", ".join(_dc_list) if _dc_list else "—"
                    info_row([
                        ("Deployment Scope",      _bra.get("deployment_scope", "—")),
                        ("Rollback Complexity",   _bra.get("rollback_complexity", "—")),
                        ("Downstream Consumers", _dc_str),
                        ("User-Facing Impact",   _bra.get("user_facing_impact", "—") or "—"),
                    ])

            # ── Technical Rationale ───────────────────────────────────────────
            _reasoning = r.get("reasoning", "") or ""
            _narrative  = r.get("narrative", "") or ""
            if _reasoning or _narrative:
                with st.expander("Technical Rationale", expanded=False):
                    import re as _re
                    _text = _reasoning or _narrative
                    _sentences = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', _text) if len(s.strip()) > 20]

                    if len(_sentences) <= 1:
                        st.code(_text, language=None)
                    else:
                        _node_icons = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
                        _rows = ""
                        for _idx, _sent in enumerate(_sentences[:10]):
                            _icon = _node_icons[_idx] if _idx < len(_node_icons) else "·"
                            _sent_lower = _sent.lower()
                            _dot_col = T["blue"]
                            if any(w in _sent_lower for w in ("risk", "fail", "break", "error", "high")):
                                _dot_col = T["red"]
                            elif any(w in _sent_lower for w in ("environment", "noise", "unrelated", "not caused", "infra")):
                                _dot_col = T["amber"]
                            elif any(w in _sent_lower for w in ("low", "safe", "no risk", "confidence")):
                                _dot_col = T["green"]
                            _is_last = _idx == len(_sentences[:10]) - 1
                            _border = f'border-bottom:1px solid {T["border2"]}' if not _is_last else ''
                            _rows += (
                                f'<div style="display:flex;gap:0.85rem;align-items:flex-start;'
                                f'padding:0.65rem 0;{_border}">'
                                f'<div style="display:flex;flex-direction:column;align-items:center;'
                                f'flex-shrink:0;padding-top:2px">'
                                f'<span style="font-size:0.75rem;font-weight:700;color:{_dot_col};'
                                f'width:20px;text-align:center">{_icon}</span>'
                                + (f'<div style="width:2px;flex:1;background:{T["border2"]};margin-top:4px"></div>' if not _is_last else '')
                                + f'</div>'
                                f'<p style="font-size:0.83rem;color:{T["text"]};line-height:1.6;margin:0">{_sent}</p>'
                                f'</div>'
                            )
                        st.markdown(
                            f'<div style="background:{T["surface2"]};border:1px solid {T["border"]};'
                            f'border-radius:10px;padding:0.5rem 1.25rem">{_rows}</div>',
                            unsafe_allow_html=True,
                        )


# ═══════════════════════════════════════════════════════════
# PAGE 4 — FAILURE PINPOINT
# ═══════════════════════════════════════════════════════════
elif page == "Failure Pinpoint":
    section_header("Failure Pinpoint", "Identify the exact file and line responsible for a pipeline failure")

    with st.spinner("Loading execution data..."):
        try:
            pipeline_df, failed_df, share_map, _data_source = load_splunk_data()
        except Exception as e:
            st.error(f"Could not load data: {e}")
            st.stop()
    if _data_source in ("csv_fallback_network", "csv", "stale_cache"):
        st.markdown(
            f'<div style="background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.3);'
            f'border-radius:8px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:{T["amber"]}">'
            f'⚠️ &nbsp;<b>Offline mode</b> — showing cached/CSV data. Connect to VPN for live results.'
            f'</div>', unsafe_allow_html=True,
        )

    with content_card():
        section_label("Recent Failed Executions", dark=True)
        if not failed_df.empty:
            # ── Column headers ────────────────────────────────────────────
            _phc = st.columns([1.4, 2.0, 1.4, 1.6])
            for _col, _lbl in zip(_phc, ["Execution ID", "Pipeline", "Failed Step", "Start Time"]):
                _col.markdown(
                    f'<p style="font-size:0.67rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0;padding:4px 0">{_lbl}</p>',
                    unsafe_allow_html=True,
                )
            st.markdown(f'<hr style="margin:0 0 4px 0;border:none;border-top:1px solid {T["border"]}">', unsafe_allow_html=True)

            # ── Clickable rows ────────────────────────────────────────────
            _sel_eid = st.session_state.get("pinpoint_exec_input", "")
            for _, _row in failed_df.head(15).iterrows():
                _eid   = str(_row.get("executionId", ""))
                _pname = str(_row.get("pipelineName", "—"))
                _step  = str(_row.get("firstFailedStep", "—"))
                _start = str(_row.get("Deploy Start Time", "—"))
                try:
                    _start = pd.to_datetime(_start).strftime("%b %d · %H:%M")
                except Exception:
                    _start = _start[:16]
                _is_sel = _eid == _sel_eid
                _pc1, _pc2, _pc3, _pc4 = st.columns([1.4, 2.0, 1.4, 1.6])
                with _pc1:
                    if st.button(
                        _eid[:14],
                        key=f"_pp_{_eid}",
                        use_container_width=True,
                        type="primary" if _is_sel else "secondary",
                    ):
                        st.session_state["pinpoint_exec_input"] = _eid
                        st.session_state.pop("pinpoint_md", None)
                        st.session_state.pop("pinpoint_findings", None)
                        st.session_state.pop("pinpoint_eid", None)
                        st.rerun()
                _pc2.markdown(f'<p style="font-size:0.8rem;color:{T["text"]};margin:6px 0">{_pname}</p>', unsafe_allow_html=True)
                _pc3.markdown(f'<p style="font-size:0.8rem;color:{T["red"]};margin:6px 0">{_step}</p>', unsafe_allow_html=True)
                _pc4.markdown(f'<p style="font-size:0.78rem;color:{T["text_muted"]};margin:6px 0">{_start}</p>', unsafe_allow_html=True)
        else:
            st.info("No failed executions in the current data export.")

    # Auto-run pinpoint when an execution is selected
    _auto_eid = st.session_state.get("pinpoint_exec_input", "")
    if _auto_eid and "pinpoint_md" not in st.session_state:
        with st.spinner(f"Analysing execution {_auto_eid}..."):
            try:
                from analysis.code_analyzer import run_pinpoint
                findings, report_md = run_pinpoint(
                    _auto_eid, use_llm=True,
                    failed_df=failed_df, share_map=share_map,
                )
                st.session_state["pinpoint_md"]       = report_md
                st.session_state["pinpoint_findings"] = findings
                st.session_state["pinpoint_eid"]      = _auto_eid
                st.rerun()
            except Exception as e:
                st.error(f"Pinpoint failed: {e}")

    if "pinpoint_md" in st.session_state:
        eid       = st.session_state.get("pinpoint_eid", _auto_eid)
        json_path = Path(f"reports/pinpoint_{eid}.json")

        if json_path.exists():
            with open(json_path) as f:
                p = json.load(f)

            info_row([
                ("Failed Step", p.get("failed_step", "—")),
                ("Error Type",  p.get("error_type",  "—")),
                ("Execution",   eid),
            ])

            # ── Primary cause block ───────────────────────────────────────
            _pcfile = p.get("primary_cause_file", "—")
            _pcline = p.get("primary_cause_line_no")
            _pccode = p.get("primary_cause_line", "")
            _conf   = p.get("confidence", "")
            _conf_col = {"High": T["green"], "Medium": T["amber"], "Low": T["red"]}.get(_conf, T["gray"])

            st.markdown(
                f'<div style="background:{T["surface"]};border:1px solid {T["border"]};'
                f'border-left:3px solid {T["red"]};border-radius:10px;'
                f'padding:1.25rem 1.5rem;margin-bottom:1rem">'
                f'<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.6rem">'
                f'<div>'
                f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 0.25rem 0">Primary Cause</p>'
                f'<p style="font-family:monospace;font-size:0.88rem;font-weight:600;color:{T["text"]};margin:0">'
                f'{_pcfile}'
                + (f'<span style="color:{T["red"]};font-weight:700"> :{_pcline}</span>' if _pcline else "")
                + f'</p>'
                f'</div>'
                f'<div style="text-align:right">'
                f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 0.25rem 0">Confidence</p>'
                f'<p style="font-size:0.88rem;font-weight:700;color:{_conf_col};margin:0">{_conf}</p>'
                f'</div>'
                f'</div>'
                f'<p style="font-size:0.83rem;color:{T["text"]};line-height:1.6;margin:0 0 0.75rem 0">'
                f'{p.get("explanation","")}</p>'
                + (
                    f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.3rem 0">'
                    f'Line {_pcline} — Offending Code</p>'
                    if _pcline and _pccode else ""
                )
                + f'</div>',
                unsafe_allow_html=True,
            )
            if _pccode:
                st.code(_pccode, language=None)

            # ── Recommended fix ───────────────────────────────────────────
            if p.get("fix_before") or p.get("fix_after"):
                st.markdown(
                    f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0.75rem 0 0.5rem 0">'
                    f'Recommended Fix</p>',
                    unsafe_allow_html=True,
                )
                col_b, col_a = st.columns(2, gap="medium")
                with col_b:
                    st.markdown(
                        f'<p style="font-size:0.72rem;font-weight:600;color:{T["red"]};'
                        f'text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.4rem">'
                        f'Before — Broken</p>',
                        unsafe_allow_html=True,
                    )
                    st.code(p.get("fix_before", ""), language=None)
                with col_a:
                    st.markdown(
                        f'<p style="font-size:0.72rem;font-weight:600;color:{T["green"]};'
                        f'text-transform:uppercase;letter-spacing:0.06em;margin-bottom:0.4rem">'
                        f'After — Corrected</p>',
                        unsafe_allow_html=True,
                    )
                    st.code(p.get("fix_after", ""), language=None)

            st.markdown(
                f'<div style="background:{T["surface2"]};border:1px solid {T["border"]};'
                f'border-radius:10px;padding:1rem 1.25rem">'
                f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.07em;color:{T["text_muted"]};margin:0 0 0.35rem 0">Prevention</p>'
                f'<p style="font-size:0.84rem;color:{T["text"]};line-height:1.6;margin:0">'
                f'{p.get("prevention","")}</p></div>',
                unsafe_allow_html=True,
            )

            if p.get("alternative_causes"):
                with st.expander("Alternative causes"):
                    for alt in p["alternative_causes"]:
                        st.markdown(
                            f'<p style="font-size:0.83rem;margin:0.3rem 0">'
                            f'<code>{alt.get("file","")}</code> &mdash; '
                            f'<span style="color:{T["text_sub"]}">{alt.get("reason","")}</span></p>',
                            unsafe_allow_html=True,
                        )
        else:
            st.markdown(st.session_state["pinpoint_md"])

        findings = st.session_state.get("pinpoint_findings", [])
        if findings:
            with st.expander(f"{len(findings)} code location(s) identified by static analysis"):
                for f in findings:
                    st.markdown(
                        f'<div style="padding:0.6rem 0;border-bottom:1px solid {T["border2"]}">'
                        f'<code style="font-size:0.83rem">{f.get("file","")}</code>'
                        f'<span style="font-size:0.78rem;color:{T["text_muted"]}"> &mdash; '
                        f'line {f.get("line_no","?")}</span><br>'
                        f'<span style="font-size:0.78rem;color:{T["text_sub"]}">'
                        f'{f.get("reason","")}</span></div>',
                        unsafe_allow_html=True,
                    )
                    if f.get("line"):
                        st.code(f["line"])


# ═══════════════════════════════════════════════════════════
# PAGE 5 — MEMORY SEARCH
# ═══════════════════════════════════════════════════════════
elif page == "Memory Search":
    section_header("Memory Search", "Search past failures semantically &mdash; find similar incidents and how they were resolved")

    try:
        from vector_store.store import memory_stats, find_similar_failures, find_similar_scan_findings
        stats = memory_stats()
        c1, c2 = st.columns(2, gap="small")
        c1.metric("Failure Records",      stats.get("failure_memory", 0))
        c2.metric("Scan Finding Records", stats.get("scan_memory", 0))

        if stats.get("failure_memory", 0) == 0 and stats.get("scan_memory", 0) == 0:
            st.info("Memory is empty. Run a Failure Analysis or Code Scan first to populate it.")
        else:
            tab1, tab2 = st.tabs(["Failure History", "Scan Findings"])

            with tab1:
                st.markdown('<div class="panel">', unsafe_allow_html=True)
                col_a, col_b = st.columns(2, gap="medium")
                with col_a:
                    error_type = st.selectbox("Error Type", [
                        "", "security_failure", "missing_npm_module", "java_compile_error",
                        "typescript_error", "apache_config_syntax_error", "build_failure",
                        "missing_env_variable", "quality_gate_failure",
                    ])
                with col_b:
                    step = st.selectbox("Pipeline Step", [
                        "", "build", "securityTest", "deploy", "codeQuality", "loadTest",
                    ])
                error_msg = st.text_input("Error message or keyword",
                                          placeholder="e.g. CRXDE Lite is active")

                if st.button("Search", key="sf", type="primary"):
                    if not error_type and not error_msg:
                        st.warning("Provide at least an error type or message.")
                    else:
                        with st.spinner("Searching memory..."):
                            hits = find_similar_failures(
                                error_type=error_type, error_message=error_msg,
                                key_lines=[], step=step, top_k=5,
                            )
                        if not hits:
                            st.info("No similar past failures found.")
                        else:
                            st.markdown(
                                f'<p style="font-size:0.8rem;color:{T["text_muted"]};'
                                f'margin:0.75rem 0">{len(hits)} similar record(s) found</p>',
                                unsafe_allow_html=True,
                            )
                            for h in hits:
                                score = h.get("similarity_score", 0)
                                with st.expander(
                                    f"{int(score*100)}% match  —  "
                                    f"Execution {h.get('execution_id')}  |  "
                                    f"{h.get('step')}  |  {h.get('error_type')}"
                                ):
                                    st.markdown(
                                        f'<div style="height:3px;background:{T["border"]};'
                                        f'border-radius:2px;margin-bottom:0.75rem">'
                                        f'<div style="height:3px;width:{int(score*100)}%;'
                                        f'background:{T["blue"]};border-radius:2px"></div></div>',
                                        unsafe_allow_html=True,
                                    )
                                    st.markdown(
                                        f'<p style="font-size:0.75rem;font-weight:700;'
                                        f'text-transform:uppercase;letter-spacing:0.07em;'
                                        f'color:{T["text_muted"]};margin:0 0 0.2rem 0">Root Cause</p>'
                                        f'<p style="font-size:0.84rem;color:{T["text"]};'
                                        f'margin:0 0 0.75rem 0">{h.get("root_cause","—")}</p>'
                                        f'<p style="font-size:0.75rem;font-weight:700;'
                                        f'text-transform:uppercase;letter-spacing:0.07em;'
                                        f'color:{T["text_muted"]};margin:0 0 0.2rem 0">Fix Applied</p>'
                                        f'<p style="font-size:0.84rem;color:{T["text"]};margin:0">'
                                        f'{h.get("fix","—")}</p>',
                                        unsafe_allow_html=True,
                                    )
                st.markdown("</div>", unsafe_allow_html=True)

            with tab2:
                st.markdown('<div class="panel">', unsafe_allow_html=True)
                col_a, col_b = st.columns(2, gap="medium")
                with col_a:
                    pattern = st.text_input("Pattern keyword",
                                            placeholder="e.g. SNAPSHOT, undefined variable")
                with col_b:
                    file_kw = st.text_input("File keyword (optional)",
                                            placeholder="e.g. pom.xml, dispatcher")

                if st.button("Search", key="ss", type="primary"):
                    if not pattern:
                        st.warning("Enter a pattern to search.")
                    else:
                        with st.spinner("Searching memory..."):
                            hits = find_similar_scan_findings(pattern=pattern, file=file_kw, top_k=5)
                        if not hits:
                            st.info("No similar scan findings found.")
                        else:
                            st.markdown(
                                f'<p style="font-size:0.8rem;color:{T["text_muted"]};'
                                f'margin:0.75rem 0">{len(hits)} similar finding(s) found</p>',
                                unsafe_allow_html=True,
                            )
                            for h in hits:
                                sev = h.get("severity", "")
                                sev_col = T["red"] if sev == "P1" else T["amber"] if sev == "P2" else T["blue"]
                                with st.expander(
                                    f"{sev}  —  {h.get('file','')}  —  {h.get('pattern','')[:60]}"
                                ):
                                    st.markdown(
                                        f'<span style="background:{sev_col}18;color:{sev_col};'
                                        f'border:1px solid {sev_col}44;padding:2px 9px;'
                                        f'border-radius:4px;font-size:0.72rem;font-weight:700;'
                                        f'display:inline-block;margin-bottom:0.75rem">{sev}</span>',
                                        unsafe_allow_html=True,
                                    )
                                    st.markdown(
                                        f'<p style="font-size:0.75rem;font-weight:700;'
                                        f'text-transform:uppercase;letter-spacing:0.07em;'
                                        f'color:{T["text_muted"]};margin:0 0 0.2rem 0">Problem</p>'
                                        f'<p style="font-size:0.84rem;color:{T["text"]};'
                                        f'margin:0 0 0.75rem 0">{h.get("problem","")}</p>'
                                        f'<p style="font-size:0.75rem;font-weight:700;'
                                        f'text-transform:uppercase;letter-spacing:0.07em;'
                                        f'color:{T["text_muted"]};margin:0 0 0.2rem 0">Fix</p>'
                                        f'<p style="font-size:0.84rem;color:{T["text"]};margin:0">'
                                        f'{h.get("fix","")}</p>',
                                        unsafe_allow_html=True,
                                    )
                st.markdown("</div>", unsafe_allow_html=True)

    except Exception as e:
        st.error(f"Memory store error: {e}")


# ═══════════════════════════════════════════════════════════
# PAGE 6 — MEMORY EXPLORER
# ═══════════════════════════════════════════════════════════
elif page == "Memory Explorer":
    section_header("Memory Explorer", "Visualise everything stored in ChromaDB — embeddings, records, clusters")

    import plotly.graph_objects as go
    import plotly.express as px
    import pandas as pd

    try:
        from vector_store.store import _collection, memory_stats
        import numpy as np

        stats = memory_stats()

        # ── Top stats row ──
        s1, s2, s3 = st.columns(3, gap="small")
        s1.metric("Failure Records",      stats.get("failure_memory", 0))
        s2.metric("Scan Finding Records", stats.get("scan_memory", 0))
        s3.metric("DB Path", stats.get("db_path", "—").split("/")[-1])


        # ── Load failure_memory ──
        col_fm = _collection("failure_memory")
        fm_count = col_fm.count()

        if fm_count == 0:
            st.info("Failure memory is empty. Run a Failure Analysis or Risk Assessment first to populate it.")
        else:
            raw = col_fm.get(include=["metadatas", "embeddings", "documents"])
            metas     = raw["metadatas"]
            embeddings = np.array(raw["embeddings"])
            docs      = raw["documents"]

            df = pd.DataFrame(metas)
            df["doc_preview"] = [d[:120] + "..." if len(d) > 120 else d for d in docs]

            tab_scatter, tab_breakdown, tab_records = st.tabs([
                "Embedding Map", "Breakdown", "All Records"
            ])

            # ── TAB 1: 2D Embedding Scatter ──
            with tab_scatter:
                st.markdown('<div class="panel">', unsafe_allow_html=True)
                section_label("2D Embedding Map (PCA projection)")
                st.markdown(
                    f'<p class="caption" style="margin-bottom:1rem">Each dot is one record in ChromaDB. '
                    f'Records that are close together are semantically similar — '
                    f'the model would retrieve them for the same type of failure.</p>',
                    unsafe_allow_html=True,
                )

                from sklearn.decomposition import PCA
                n_components = min(2, fm_count)
                pca = PCA(n_components=n_components)
                coords = pca.fit_transform(embeddings)

                df["x"] = coords[:, 0]
                df["y"] = coords[:, 1] if coords.shape[1] > 1 else [0.0] * len(coords)

                # Color by step
                step_colors = {
                    "build":        T["blue"],
                    "securityTest": T["red"],
                    "deploy":       T["amber"],
                    "codeQuality":  T["purple"],
                    "loadTest":     T["green"],
                }
                df["color"] = df["step"].map(step_colors).fillna(T["gray"])
                df["label"] = df.apply(
                    lambda r: f"<b>{r.get('execution_id','')}</b><br>"
                              f"Step: {r.get('step','')}<br>"
                              f"Type: {r.get('error_type','')}<br>"
                              f"Root cause: {str(r.get('root_cause',''))[:80]}",
                    axis=1,
                )

                fig_scatter = go.Figure()
                for step, grp in df.groupby("step"):
                    fig_scatter.add_trace(go.Scatter(
                        x=grp["x"], y=grp["y"],
                        mode="markers+text",
                        name=step,
                        marker=dict(
                            size=14,
                            color=step_colors.get(step, T["gray"]),
                            line=dict(width=1.5, color="#FFFFFF"),
                            opacity=0.9,
                        ),
                        text=grp["execution_id"],
                        textposition="top center",
                        textfont=dict(size=9, color=T["text_sub"]),
                        hovertext=grp["label"],
                        hoverinfo="text",
                    ))

                t_sc = chart_theme(420, show_legend=True)
                t_sc["xaxis"]["showgrid"] = True
                t_sc["xaxis"]["gridcolor"] = T["border2"]
                t_sc["xaxis"]["title"] = "PCA Component 1"
                t_sc["yaxis"]["title"] = "PCA Component 2"
                explained = pca.explained_variance_ratio_ * 100
                t_sc["title"] = dict(
                    text=f"Variance explained: PC1 {explained[0]:.1f}%"
                         + (f", PC2 {explained[1]:.1f}%" if len(explained) > 1 else ""),
                    font=dict(size=11, color=T["text_muted"]),
                    x=0.5,
                )
                fig_scatter.update_layout(**t_sc)
                st.plotly_chart(fig_scatter, use_container_width=True,
                                config={"displayModeBar": False})

                st.markdown(
                    f'<p class="caption">Colour = pipeline step &nbsp;·&nbsp; '
                    f'Labels = execution ID &nbsp;·&nbsp; '
                    f'Hover for full record details</p>',
                    unsafe_allow_html=True,
                )
                st.markdown("</div>", unsafe_allow_html=True)

            # ── TAB 2: Breakdown charts ──
            with tab_breakdown:
                left, right = st.columns(2, gap="medium")

                with left:
                    st.markdown('<div class="panel">', unsafe_allow_html=True)
                    section_label("Records by Pipeline Step")
                    step_counts = df["step"].value_counts().reset_index()
                    step_counts.columns = ["step", "count"]
                    bar_c = [step_colors.get(s, T["gray"]) for s in step_counts["step"]]
                    fig_step = go.Figure(go.Bar(
                        x=step_counts["step"], y=step_counts["count"],
                        marker=dict(color=bar_c, line=dict(width=0)),
                        text=step_counts["count"], textposition="outside",
                        hovertemplate="<b>%{x}</b><br>%{y} records<extra></extra>",
                    ))
                    t_b = chart_theme(220)
                    t_b["bargap"] = 0.45
                    fig_step.update_layout(**t_b)
                    st.plotly_chart(fig_step, use_container_width=True,
                                    config={"displayModeBar": False})
                    st.markdown("</div>", unsafe_allow_html=True)

                with right:
                    st.markdown('<div class="panel">', unsafe_allow_html=True)
                    section_label("Records by Error Type")
                    type_counts = df["error_type"].value_counts().reset_index()
                    type_counts.columns = ["error_type", "count"]
                    fig_type = go.Figure(go.Bar(
                        x=type_counts["count"], y=type_counts["error_type"],
                        orientation="h",
                        marker=dict(
                            color=T["blue"],
                            opacity=0.85,
                            line=dict(width=0),
                        ),
                        text=type_counts["count"], textposition="outside",
                        hovertemplate="<b>%{y}</b><br>%{x} records<extra></extra>",
                    ))
                    t_h = chart_theme(220)
                    t_h["bargap"] = 0.3
                    t_h["xaxis"]["showgrid"] = True
                    t_h["yaxis"]["showgrid"] = False
                    fig_type.update_layout(**t_h)
                    st.plotly_chart(fig_type, use_container_width=True,
                                    config={"displayModeBar": False})
                    st.markdown("</div>", unsafe_allow_html=True)

                # ── Similarity heatmap ──
                if fm_count >= 3:
                    st.markdown('<div class="panel">', unsafe_allow_html=True)
                    section_label("Similarity Heatmap (cosine)")
                    st.markdown(
                        '<p class="caption" style="margin-bottom:1rem">'
                        'How similar every record is to every other record. '
                        'Bright = very similar (likely same root cause). '
                        'Dark = different failure class.</p>',
                        unsafe_allow_html=True,
                    )
                    # Cosine similarity matrix
                    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
                    normed = embeddings / np.where(norms == 0, 1, norms)
                    sim_matrix = normed @ normed.T

                    labels = [
                        f"{m.get('execution_id','?')[:12]}<br>{m.get('step','')}"
                        for m in metas
                    ]
                    fig_heat = go.Figure(go.Heatmap(
                        z=sim_matrix,
                        x=labels, y=labels,
                        colorscale=[
                            [0.0, "rgb(255,255,255)"],
                            [0.5, "rgba(61,110,234,0.5)"],
                            [1.0, "rgb(61,110,234)"],
                        ],
                        zmin=0, zmax=1,
                        hovertemplate="<b>%{y}</b> vs <b>%{x}</b><br>Similarity: %{z:.2f}<extra></extra>",
                        showscale=True,
                        colorbar=dict(
                            thickness=12, len=0.8,
                            tickfont=dict(size=10, color=T["text_muted"]),
                        ),
                    ))
                    t_heat = chart_theme(max(280, fm_count * 32), show_legend=False)
                    t_heat["xaxis"]["tickfont"] = dict(size=9, color=T["text_muted"])
                    t_heat["yaxis"]["tickfont"] = dict(size=9, color=T["text_muted"])
                    t_heat["xaxis"]["showgrid"] = False
                    t_heat["yaxis"]["showgrid"] = False
                    fig_heat.update_layout(**t_heat)
                    st.plotly_chart(fig_heat, use_container_width=True,
                                    config={"displayModeBar": False})
                    st.markdown("</div>", unsafe_allow_html=True)

            # ── TAB 3: All Records browser ──
            with tab_records:
                st.markdown('<div class="panel">', unsafe_allow_html=True)
                section_label(f"All {fm_count} Records in failure_memory")

                # Filter controls
                fc1, fc2 = st.columns(2, gap="medium")
                with fc1:
                    filter_step = st.selectbox(
                        "Filter by step", ["All"] + sorted(df["step"].unique().tolist()),
                        key="exp_step"
                    )
                with fc2:
                    filter_type = st.selectbox(
                        "Filter by error type", ["All"] + sorted(df["error_type"].unique().tolist()),
                        key="exp_type"
                    )

                filtered = df.copy()
                if filter_step != "All":
                    filtered = filtered[filtered["step"] == filter_step]
                if filter_type != "All":
                    filtered = filtered[filtered["error_type"] == filter_type]

                st.markdown(
                    f'<p class="caption" style="margin:0.5rem 0 1rem 0">'
                    f'Showing {len(filtered)} of {fm_count} records</p>',
                    unsafe_allow_html=True,
                )

                for _, row in filtered.iterrows():
                    step_c = step_colors.get(row.get("step", ""), T["gray"])
                    with st.expander(
                        f"{row.get('execution_id','?')}  ·  "
                        f"{row.get('step','')}  ·  {row.get('error_type','')}"
                    ):
                        st.markdown(
                            f'<div style="display:flex;gap:0.5rem;margin-bottom:0.75rem">'
                            f'<span style="background:{step_c}18;color:{step_c};'
                            f'border:1px solid {step_c}44;padding:2px 9px;border-radius:4px;'
                            f'font-size:0.72rem;font-weight:700">{row.get("step","")}</span>'
                            f'<span style="background:{T["surface2"]};color:{T["text_sub"]};'
                            f'border:1px solid {T["border"]};padding:2px 9px;border-radius:4px;'
                            f'font-size:0.72rem">{row.get("error_type","")}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                        if row.get("error_message"):
                            st.markdown(
                                f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                                f'letter-spacing:0.07em;color:{T["text_muted"]};margin:0 0 0.2rem 0">'
                                f'Error Message</p>'
                                f'<p style="font-size:0.82rem;color:{T["text_sub"]};'
                                f'font-family:monospace;margin:0 0 0.75rem 0">'
                                f'{row.get("error_message","")}</p>',
                                unsafe_allow_html=True,
                            )
                        st.markdown(
                            f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                            f'letter-spacing:0.07em;color:{T["text_muted"]};margin:0 0 0.2rem 0">'
                            f'Root Cause</p>'
                            f'<p style="font-size:0.84rem;color:{T["text"]};'
                            f'line-height:1.55;margin:0 0 0.75rem 0">'
                            f'{row.get("root_cause","—")}</p>'
                            f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                            f'letter-spacing:0.07em;color:{T["text_muted"]};margin:0 0 0.2rem 0">'
                            f'Fix Applied</p>'
                            f'<p style="font-size:0.84rem;color:{T["text"]};'
                            f'line-height:1.55;margin:0 0 0.75rem 0">'
                            f'{row.get("fix","—")}</p>',
                            unsafe_allow_html=True,
                        )
                        st.markdown(
                            f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                            f'letter-spacing:0.07em;color:{T["text_muted"]};margin:0 0 0.3rem 0">'
                            f'Embedded Text (what the model sees)</p>',
                            unsafe_allow_html=True,
                        )
                        st.code(row.get("doc_preview", ""), language=None)

                st.markdown("</div>", unsafe_allow_html=True)

        # ── Scan memory ──
        col_sm = _collection("scan_memory")
        sm_count = col_sm.count()
        if sm_count > 0:
            st.markdown('<div class="panel">', unsafe_allow_html=True)
            section_label(f"Scan Memory — {sm_count} findings")
            raw_sm = col_sm.get(include=["metadatas"])
            df_sm = pd.DataFrame(raw_sm["metadatas"])
            show_cols = [c for c in ["file", "severity", "pattern", "problem", "fix"] if c in df_sm.columns]
            st.dataframe(
                df_sm[show_cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "file":     st.column_config.TextColumn("File",     width="large"),
                    "severity": st.column_config.TextColumn("Severity", width="small"),
                    "pattern":  st.column_config.TextColumn("Pattern",  width="medium"),
                    "problem":  st.column_config.TextColumn("Problem",  width="large"),
                    "fix":      st.column_config.TextColumn("Fix",      width="large"),
                },
            )
            st.markdown("</div>", unsafe_allow_html=True)

    except Exception as e:
        st.error(f"Memory Explorer error: {e}")
        import traceback
        st.code(traceback.format_exc())


# ═══════════════════════════════════════════════════════════
# PAGE — PIPELINE INTELLIGENCE
# ═══════════════════════════════════════════════════════════
elif page == "Pipeline Intelligence":
    import pandas as pd
    import plotly.graph_objects as go

    section_header(
        "Pipeline Intelligence",
        "30-day execution history &middot; click any run to see its failure breakdown",
    )

    with st.spinner("Loading pipeline data..."):
        try:
            pipeline_df, failed_df, share_map, _data_source = load_splunk_data()
        except Exception as _e:
            st.error(f"Failed to load data: {_e}")
            st.stop()

    # ── Merge failed step onto full execution list ──────────────────────────
    _steps_df = failed_df[["executionId", "firstFailedStep"]].copy() if not failed_df.empty else pd.DataFrame(columns=["executionId", "firstFailedStep"])
    _full = pipeline_df.merge(_steps_df, on="executionId", how="left")

    # ── Git-log commit correlation ──────────────────────────────────────────
    # Maps each executionId → {sha, sha_short, title, author}
    # Runs once per page load; result cached by Streamlit's widget state.
    @st.cache_data(ttl=300, show_spinner=False)
    def _load_commit_map(exec_rows_json: str) -> dict:
        """Cached wrapper around correlate_executions_to_commits."""
        import json
        try:
            from connectors.git_connector import correlate_executions_to_commits
            rows = json.loads(exec_rows_json)
            return correlate_executions_to_commits(rows)
        except Exception as _ce:
            return {}

    _exec_rows_for_corr = _full[["executionId", "Deploy Start Time"]].to_dict(orient="records")
    import json as _json
    _commit_map: dict = _load_commit_map(_json.dumps(_exec_rows_for_corr, default=str))

    # ── Compute per-pipeline statistics (used in summary cards + row badges) ─
    def _pipeline_stats(df: pd.DataFrame) -> dict:
        """Return failure probability metrics for a single pipeline's DataFrame."""
        total   = len(df)
        failed  = len(df[df["Status"].isin(["FAILED", "ERROR"])])
        success = len(df[df["Status"] == "FINISHED"])
        canc    = len(df[df["Status"] == "CANCELLED"])
        fail_pct = round(failed / total * 100) if total else 0

        # Step-level breakdown — only rows that actually have a failed step
        step_counts = df["firstFailedStep"].dropna().value_counts().to_dict()

        # Most probable failure stage
        worst_step = max(step_counts, key=step_counts.get) if step_counts else None
        worst_pct  = round(step_counts[worst_step] / total * 100) if worst_step else 0

        # Success streak — consecutive FINISHED from the most recent run back
        streak = 0
        for s in df.sort_values("Deploy Start Time", ascending=False)["Status"]:
            if s == "FINISHED":
                streak += 1
            else:
                break

        # Average duration of FINISHED runs
        finished_rows = df[df["Status"] == "FINISHED"]["Duration (Min)"]
        avg_dur = round(finished_rows.mean(), 1) if not finished_rows.empty else None

        return {
            "total": total, "failed": failed, "success": success,
            "cancelled": canc, "fail_pct": fail_pct, "step_counts": step_counts,
            "worst_step": worst_step, "worst_pct": worst_pct,
            "streak": streak, "avg_dur": avg_dur,
        }

    _all_pipelines = _full["pipelineName"].unique().tolist()
    _stats_by_pipeline = {
        name: _pipeline_stats(_full[_full["pipelineName"] == name])
        for name in _all_pipelines
    }

    # ── Helper: colour for a failure-% value ────────────────────────────────
    def _risk_color(pct: int) -> str:
        if pct >= 60:  return T["red"]
        if pct >= 30:  return T["amber"]
        return T["green"]

    def _risk_label(pct: int) -> str:
        if pct >= 60:  return "HIGH RISK"
        if pct >= 30:  return "MEDIUM RISK"
        return "LOW RISK"

    def _status_color(status: str) -> str:
        return {
            "FINISHED":  T["green"],
            "FAILED":    T["red"],
            "ERROR":     T["red"],
            "CANCELLED": T["amber"],
        }.get(status, T["gray"])

    def _status_icon(status: str) -> str:
        return {
            "FINISHED":  "✓",
            "FAILED":    "✕",
            "ERROR":     "⚠",
            "CANCELLED": "◌",
        }.get(status, "·")

    # ════════════════════════════════════════════════════════
    # SECTION A — Pipeline summary cards
    # ════════════════════════════════════════════════════════
    _ncards = len(_all_pipelines)
    _card_cols = st.columns(_ncards, gap="medium")

    STAGE_ORDER = ["build", "codeQuality", "securityTest", "deploy", "loadTest", "activation"]
    STAGE_LABELS = {
        "build": "Build", "codeQuality": "Code Quality",
        "securityTest": "Security", "deploy": "Deploy",
        "loadTest": "Load Test", "activation": "Activation",
    }

    for _ci, _pname in enumerate(_all_pipelines):
        _s = _stats_by_pipeline[_pname]
        _rc = _risk_color(_s["fail_pct"])
        _rl = _risk_label(_s["fail_pct"])

        with _card_cols[_ci]:
            st.markdown(
                f'<div style="background:{T["surface"]};border:1px solid {T["border"]};'
                f'border-top:3px solid {_rc};border-radius:10px;padding:20px 22px 16px;">'

                # Pipeline name + risk badge
                f'<p style="font-size:0.72rem;font-weight:700;letter-spacing:0.09em;'
                f'text-transform:uppercase;color:{T["text_muted"]};margin:0 0 4px 0">{_pname}</p>'
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">'
                f'<span style="font-size:1.6rem;font-weight:800;color:{_rc}">{_s["fail_pct"]}%</span>'
                f'<span style="font-size:0.68rem;font-weight:700;letter-spacing:0.1em;'
                f'color:{_rc};background:{"rgba(255,99,99,0.1)" if _rc==T["red"] else "rgba(245,166,35,0.1)" if _rc==T["amber"] else "rgba(48,164,108,0.1)"};'
                f'padding:2px 8px;border-radius:10px">{_rl}</div>'

                # Stats row
                f'<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px">'
                + "".join([
                    f'<div><p style="font-size:0.68rem;color:{T["text_muted"]};margin:0">{lbl}</p>'
                    f'<p style="font-size:0.95rem;font-weight:600;color:{col};margin:0">{val}</p></div>'
                    for lbl, val, col in [
                        ("Total Runs", _s["total"], T["text"]),
                        ("Succeeded", _s["success"], T["green"]),
                        ("Failed", _s["failed"], T["red"]),
                        ("Cancelled", _s["cancelled"], T["amber"]),
                    ]
                ])
                + f'</div>'

                # Stage failure bar
                + (
                    f'<p style="font-size:0.7rem;font-weight:600;color:{T["text_muted"]};'
                    f'margin:0 0 6px 0;text-transform:uppercase;letter-spacing:0.07em">Failures by Stage</p>'
                    f'<div style="display:flex;flex-direction:column;gap:4px">'
                    + "".join([
                        f'<div style="display:flex;align-items:center;gap:8px">'
                        f'<span style="font-size:0.72rem;color:{T["text_muted"]};min-width:70px">'
                        f'{STAGE_LABELS.get(st_name, st_name)}</span>'
                        f'<div style="flex:1;background:{T["surface2"]};border-radius:3px;height:6px">'
                        f'<div style="width:{min(100,round(cnt/_s["total"]*100))}%;height:6px;'
                        f'background:{_risk_color(round(cnt/_s["total"]*100))};border-radius:3px"></div>'
                        f'</div>'
                        f'<span style="font-size:0.72rem;color:{T["text_muted"]};min-width:24px;text-align:right">{cnt}</span>'
                        f'</div>'
                        for st_name in STAGE_ORDER
                        if (cnt := _s["step_counts"].get(st_name, 0)) > 0
                    ])
                    + f'</div>'
                    if _s["step_counts"] else
                    f'<p style="font-size:0.78rem;color:{T["green"]};margin:0">No stage failures recorded</p>'
                )

                # Success streak
                + (
                    f'<div style="margin-top:12px;padding-top:10px;border-top:1px solid {T["border"]};'
                    f'font-size:0.75rem;color:{T["text_muted"]}">'
                    + (f'🔥 <span style="color:{T["green"]}"><b>{_s["streak"]}</b> successful run{"s" if _s["streak"]!=1 else ""}</span> in a row'
                       if _s["streak"] > 0 else
                       f'Last run did <span style="color:{T["amber"]}">not</span> succeed')
                    + f'</div>'
                )

                + f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div style="height:24px"></div>', unsafe_allow_html=True)

    # ════════════════════════════════════════════════════════
    # SECTION B — Execution list (Adobe CM style)
    # ════════════════════════════════════════════════════════
    with content_card():
        # ── Filter bar ──
        _fc1, _fc2, _fc3 = st.columns([2, 2, 2], gap="small")
        with _fc1:
            _selected_pipeline = st.selectbox(
                "Pipeline", ["All"] + _all_pipelines, key="_pi_pipe_filter"
            )
        with _fc2:
            _selected_status = st.selectbox(
                "Status", ["All", "FINISHED", "FAILED", "ERROR", "CANCELLED"],
                key="_pi_status_filter"
            )
        with _fc3:
            _max_rows = st.selectbox("Show", [25, 50, 100], key="_pi_rows")

        # Apply filters
        _view = _full.copy()
        if _selected_pipeline != "All":
            _view = _view[_view["pipelineName"] == _selected_pipeline]
        if _selected_status != "All":
            _view = _view[_view["Status"] == _selected_status]
        _view = _view.sort_values("Deploy Start Time", ascending=False).head(_max_rows).reset_index(drop=True)

        st.markdown(
            f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:0 0 12px 0">'
            f'Showing <b style="color:{T["text"]}">{len(_view)}</b> executions</p>',
            unsafe_allow_html=True,
        )

        # ── Column headers ──
        st.markdown(
            f'<div style="display:grid;grid-template-columns:1.8fr 1.1fr 1.3fr 1.4fr 0.9fr 1.6fr 1.2fr 1.1fr;'
            f'gap:0;padding:8px 14px;background:{T["surface2"]};border-radius:6px 6px 0 0;'
            f'border:1px solid {T["border"]};margin-bottom:0">'
            + "".join([
                f'<span style="font-size:0.68rem;font-weight:700;letter-spacing:0.08em;'
                f'text-transform:uppercase;color:{T["text_muted"]}">{h}</span>'
                for h in ["Name", "Action", "Status", "Start Time", "Duration", "Commit", "Details", "Failure Risk"]
            ])
            + f'</div>',
            unsafe_allow_html=True,
        )

        # ── Rows ──
        if _view.empty:
            st.info("No executions match the current filters.")
        else:
            for _i, _row in _view.iterrows():
                _eid   = str(_row["executionId"])
                _pid   = str(_row.get("pipelineId", ""))
                _pname = str(_row.get("pipelineName", ""))
                _st    = str(_row.get("Status", ""))
                _start = str(_row.get("Deploy Start Time", ""))
                _dur   = _row.get("Duration (Min)", 0)
                _step  = str(_row.get("firstFailedStep", "")) if pd.notna(_row.get("firstFailedStep")) else ""

                # Format start time nicely
                try:
                    _dt = pd.to_datetime(_start).strftime("%b %d, %Y · %I:%M %p")
                except Exception:
                    _dt = _start[:19] if len(_start) > 10 else _start

                # Format duration
                _dur_h = int(_dur // 60)
                _dur_m = int(_dur % 60)
                _dur_str = (f"{_dur_h}h {_dur_m}m" if _dur_h else f"{_dur_m}m") if _dur else "—"

                # Failure risk for this pipeline
                _ps    = _stats_by_pipeline.get(_pname, {})
                _fpct  = _ps.get("fail_pct", 0)
                _rc    = _risk_color(_fpct)
                _ws    = _ps.get("worst_step", "")

                # Correlated commit for this execution
                _commit = _commit_map.get(_eid, {})
                _commit_sha   = _commit.get("sha_short", "")
                _commit_title = _commit.get("title", "")
                _commit_author = _commit.get("author", "")
                _commit_full  = _commit.get("sha", "")

                # Status styling
                _sc = _status_color(_st)
                _si = _status_icon(_st)

                # ── Grid row aligned to the 8-column header ──
                _commit_cell = (
                    f'<code style="background:rgba(88,166,255,0.08);color:{T["blue"]};'
                    f'padding:1px 5px;border-radius:3px;font-size:0.72rem">{_commit_sha}</code>'
                    if _commit_sha else
                    f'<span style="color:{T["text_muted"]}">—</span>'
                )
                _details_cell = (
                    f'<span style="color:{T["red"]};font-size:0.78rem">{_step[:28]}{"…" if len(_step) > 28 else ""}</span>'
                    if _step else
                    f'<span style="color:{T["text_muted"]}">—</span>'
                )
                st.markdown(
                    f'<div style="display:grid;grid-template-columns:1.8fr 1.1fr 1.3fr 1.4fr 0.9fr 1.6fr 1.2fr 1.1fr;'
                    f'gap:0;padding:7px 14px;border-left:2px solid {_sc};'
                    f'border-bottom:1px solid {T["border2"]};align-items:center">'
                    f'<span style="font-size:0.8rem;color:{T["text"]};font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{_pname[:32]}{"…" if len(_pname)>32 else ""}</span>'
                    f'<span style="font-size:0.78rem;color:{T["text_muted"]}">Run</span>'
                    f'<span style="font-size:0.78rem;color:{_sc};font-weight:600">{_si} {_st}</span>'
                    f'<span style="font-size:0.75rem;color:{T["text_muted"]}">{_dt[:16] if len(_dt) > 16 else _dt}</span>'
                    f'<span style="font-size:0.78rem;color:{T["text"]}">{_dur_str}</span>'
                    f'{_commit_cell}'
                    f'{_details_cell}'
                    f'<span style="font-size:0.78rem;color:{_rc};font-weight:600">{_fpct}%</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                with st.expander(f"{_si} {_pname} — details", expanded=False):
                    # ── Execution detail header ──────────────────────────────
                    _d1, _d2 = st.columns([1, 1], gap="medium")

                    with _d1:
                        # Build commit block — shown only when we have a match
                        _commit_block = ""
                        if _commit_sha:
                            _commit_block = (
                                f'<div style="margin-top:12px;padding:10px 12px;'
                                f'background:{T["surface"]};border:1px solid {T["border"]};'
                                f'border-left:3px solid {T["blue"]};border-radius:6px">'
                                f'<p style="font-size:0.68rem;font-weight:700;letter-spacing:0.08em;'
                                f'text-transform:uppercase;color:{T["text_muted"]};margin:0 0 6px 0">'
                                f'Correlated Commit</p>'
                                f'<p style="font-size:0.82rem;margin:0 0 3px 0">'
                                f'<code style="background:rgba(88,166,255,0.1);color:{T["blue"]};'
                                f'padding:1px 6px;border-radius:4px;font-size:0.78rem">'
                                f'{_commit_sha}</code>'
                                f'&nbsp;<span style="color:{T["text"]};font-weight:500">'
                                f'{_commit_title[:60]}{"…" if len(_commit_title)>60 else ""}</span></p>'
                                f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:0">'
                                f'by {_commit_author}</p>'
                                f'</div>'
                            )
                        elif _commit_map:
                            # map was built but no match for this execution
                            _commit_block = (
                                f'<p style="font-size:0.75rem;color:{T["text_muted"]};'
                                f'margin-top:10px">No commit found before this execution timestamp.</p>'
                            )
                        else:
                            _commit_block = (
                                f'<p style="font-size:0.75rem;color:{T["text_muted"]};'
                                f'margin-top:10px">Git repo not synced — commit correlation unavailable.</p>'
                            )

                        st.markdown(
                            f'<div style="background:{T["surface2"]};border:1px solid {T["border"]};'
                            f'border-radius:8px;padding:16px 18px">'
                            f'<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;'
                            f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 10px 0">Execution Details</p>'

                            f'<div style="display:flex;flex-direction:column;gap:6px">'
                            + "".join([
                                f'<div style="display:flex;justify-content:space-between;'
                                f'font-size:0.82rem">'
                                f'<span style="color:{T["text_muted"]}">{k}</span>'
                                f'<span style="color:{T["text"]};font-weight:500">{v}</span>'
                                f'</div>'
                                for k, v in [
                                    ("Execution ID", _eid),
                                    ("Pipeline", _pname),
                                    ("Status", _st),
                                    ("Start Time", _dt),
                                    ("Duration", _dur_str),
                                    ("Failed Stage", _step if _step else "—"),
                                ]
                            ])
                            + f'</div>'
                            + _commit_block
                            + f'<div style="margin-top:12px">'
                            f'<a href="https://experience.adobe.com/#/@idfc/cloud-manager/pipelineexecution.html/program/19905/pipeline/{_pid}/execution/{_eid}" '
                            f'target="_blank" style="font-size:0.78rem;color:{T["blue"]};text-decoration:none">'
                            f'View in Cloud Manager →</a>'
                            f'</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                    with _d2:
                        st.markdown(
                            f'<div style="background:{T["surface2"]};border:1px solid {T["border"]};'
                            f'border-radius:8px;padding:16px 18px">'
                            f'<p style="font-size:0.7rem;font-weight:700;text-transform:uppercase;'
                            f'letter-spacing:0.09em;color:{T["text_muted"]};margin:0 0 10px 0">Pipeline Failure Risk (30-day History)</p>'

                            f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">'
                            f'<span style="font-size:2rem;font-weight:800;color:{_rc}">{_fpct}%</span>'
                            f'<div>'
                            f'<p style="font-size:0.72rem;font-weight:700;letter-spacing:0.09em;'
                            f'color:{_rc};margin:0">{_risk_label(_fpct)}</p>'
                            f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:0">'
                            f'{_ps.get("failed", 0)} failures in {_ps.get("total", 0)} runs</p>'
                            f'</div>'
                            f'</div>'

                            # Stage bars
                            + (
                                f'<p style="font-size:0.7rem;font-weight:600;color:{T["text_muted"]};'
                                f'margin:0 0 6px 0;text-transform:uppercase;letter-spacing:0.07em">Most Failures By Stage</p>'
                                + "".join([
                                    f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">'
                                    f'<span style="font-size:0.72rem;color:{T["text_muted"]};min-width:75px">'
                                    f'{STAGE_LABELS.get(st_n, st_n)}</span>'
                                    f'<div style="flex:1;background:{T["border"]};border-radius:3px;height:8px">'
                                    f'<div style="width:{min(100, round(cnt / max(1, _ps["total"]) * 100))}%;height:8px;'
                                    f'border-radius:3px;background:{_risk_color(round(cnt / max(1, _ps["total"]) * 100))}">'
                                    f'</div></div>'
                                    f'<span style="font-size:0.72rem;color:{T["text"]};min-width:28px;text-align:right">'
                                    f'{cnt} ({round(cnt / max(1, _ps["total"]) * 100)}%)</span>'
                                    f'</div>'
                                    for st_n in STAGE_ORDER
                                    if (cnt := _ps.get("step_counts", {}).get(st_n, 0)) > 0
                                ])
                                if _ps.get("step_counts") else
                                f'<p style="font-size:0.78rem;color:{T["green"]};margin:0">No stage failures in this pipeline</p>'
                            )

                            + f'</div>',
                            unsafe_allow_html=True,
                        )




# ═══════════════════════════════════════════════════════════
# PAGE — STATIC ANALYSIS
# ═══════════════════════════════════════════════════════════
elif page == "Static Analysis":
    import pandas as pd
    import plotly.graph_objects as go
    import plotly.express as px

    section_header(
        "Static File Analysis",
        "Track every CSS · JS · image · font changed, deleted, or at risk in the last 30 days",
    )

    # ── Load all data up front ──────────────────────────────────────────────
    @st.cache_data(ttl=300, show_spinner=False)
    def _load_static_data():
        from analysis.static_file_scanner import (
            scan_changed_static_files,
            find_deleted_static_files,
            find_hot_files,
            find_broken_clientlib_refs,
            get_author_ownership,
            get_static_summary,
        )
        changed  = scan_changed_static_files(30)
        deleted  = find_deleted_static_files(30)
        hot      = find_hot_files(30, min_commits=5)
        broken   = find_broken_clientlib_refs(deleted)
        authors  = get_author_ownership(changed)
        summary  = get_static_summary(30)
        return changed, deleted, hot, broken, authors, summary

    with st.spinner("Scanning git history for static file changes…"):
        try:
            _changed, _deleted, _hot, _broken, _authors, _summary = _load_static_data()
        except Exception as _se:
            st.error(f"Static scan failed: {_se}")
            import traceback; st.code(traceback.format_exc())
            st.stop()

    # ── KPI strip ──────────────────────────────────────────────────────────
    _k1, _k2, _k3, _k4, _k5 = st.columns(5, gap="small")
    _k1.metric("Files Changed (30d)",   _summary["total_changed"])
    _k2.metric("Code / Style Changes",  _summary["risk_changes"],
               help="CSS, SCSS, JS, JSX, HTML only — excludes images and fonts")
    _k3.metric("Files Deleted",         _summary["deleted"],
               delta=f"-{_summary['deleted']}" if _summary["deleted"] else None,
               delta_color="inverse")
    _k4.metric("Hot Files",             _summary["hot_files"],
               help="Changed in 5+ commits — high churn = instability risk")
    _k5.metric("Broken Lib Refs",       len(_broken),
               delta=f"⚠ {len(_broken)}" if _broken else None,
               delta_color="inverse" if _broken else "off")

    st.markdown('<div style="height:8px"></div>', unsafe_allow_html=True)

    # ── Charts row ─────────────────────────────────────────────────────────
    _ch1, _ch2 = st.columns([3, 2], gap="medium")

    with _ch1:
        with content_card():
            section_label("Changes by File Type")
            _ext_data = {k: v for k, v in _summary["by_ext"].items() if v > 0}
            if _ext_data:
                _ext_colors = {
                    ".css": T["blue"], ".scss": "#7c5cfc", ".less": "#b39ddb",
                    ".js": T["amber"], ".jsx": "#f97316", ".tsx": "#fb923c", ".ts": "#fbbf24",
                    ".html": T["green"], ".svg": T["red"],
                    ".png": T["gray"], ".jpg": T["gray"], ".gif": T["gray"],
                    ".woff": T["text_muted"], ".woff2": T["text_muted"], ".ttf": T["text_muted"],
                }
                _max_ext = max(_ext_data.values())
                for _ext, _cnt in list(_ext_data.items())[:12]:
                    _col = _ext_colors.get(_ext, T["gray"])
                    stat_bar(_ext, _cnt, _max_ext, _col)

    with _ch2:
        with content_card():
            section_label("Changes by Module")
            _mod_data = {k: v for k, v in _summary["by_module"].items() if v > 0}
            if _mod_data:
                _fig_mod = go.Figure(go.Pie(
                    labels=list(_mod_data.keys()),
                    values=list(_mod_data.values()),
                    hole=0.55,
                    textinfo="percent",
                    textfont=dict(size=10, color=T["text"]),
                    marker=dict(
                        colors=[T["blue"], T["amber"], T["green"], T["red"], T["gray"],
                                "#7c5cfc", "#fb923c"],
                        line=dict(color=T["surface"], width=2),
                    ),
                    hovertemplate="<b>%{label}</b><br>%{value} files<extra></extra>",
                ))
                _fig_mod.update_layout(**chart_theme(200, show_legend=True))
                st.plotly_chart(_fig_mod, use_container_width=True,
                                config={"displayModeBar": False})

    # ── TABS ───────────────────────────────────────────────────────────────
    _tab_changed, _tab_deleted, _tab_hot, _tab_broken, _tab_authors = st.tabs([
        f"📝 Changed ({len(_changed)})",
        f"🗑 Deleted ({len(_deleted)})",
        f"🔥 Hot Files ({len(_hot)})",
        f"⚠️ Broken Refs ({len(_broken)})",
        f"👤 Author Ownership",
    ])

    # ── TAB 1 — CHANGED FILES ──────────────────────────────────────────────
    with _tab_changed:
        _fc1, _fc2, _fc3 = st.columns([2, 2, 2], gap="small")
        with _fc1:
            _filter_mod = st.selectbox(
                "Module", ["All"] + sorted(set(r["module"] for r in _changed)),
                key="_sa_mod"
            )
        with _fc2:
            _filter_ext = st.selectbox(
                "Type", ["All"] + sorted(set(r["ext"] for r in _changed)),
                key="_sa_ext"
            )
        with _fc3:
            _filter_author = st.selectbox(
                "Author", ["All"] + [a["author"] for a in _authors[:15]],
                key="_sa_author"
            )

        _view_changed = [
            r for r in _changed
            if (_filter_mod    == "All" or r["module"] == _filter_mod)
            and (_filter_ext   == "All" or r["ext"]    == _filter_ext)
            and (_filter_author == "All" or r["author"] == _filter_author)
        ]

        st.markdown(
            f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:8px 0">'
            f'Showing <b style="color:{T["text"]}">{len(_view_changed)}</b> files</p>',
            unsafe_allow_html=True,
        )

        if _view_changed:
            _df_changed = pd.DataFrame(_view_changed)[
                ["ts", "ext", "module", "author", "sha", "msg", "file"]
            ]
            _df_changed["ts"] = _df_changed["ts"].str[:19].str.replace("T", " ")
            _df_changed.columns = ["Last Changed", "Type", "Module", "Author",
                                   "Commit", "Message", "Full Path"]
            st.dataframe(
                _df_changed,
                use_container_width=True,
                hide_index=True,
                height=420,
                column_config={
                    "Last Changed": st.column_config.TextColumn("Last Changed", width="medium"),
                    "Type":         st.column_config.TextColumn("Type",         width="small"),
                    "Module":       st.column_config.TextColumn("Module",       width="medium"),
                    "Author":       st.column_config.TextColumn("Author",       width="medium"),
                    "Commit":       st.column_config.TextColumn("Commit",       width="small"),
                    "Message":      st.column_config.TextColumn("Message",      width="large"),
                    "Full Path":    st.column_config.TextColumn("Full Path",    width="large"),
                },
            )
        else:
            st.info("No files match the current filters.")

    # ── TAB 2 — DELETED FILES ──────────────────────────────────────────────
    with _tab_deleted:
        if not _deleted:
            st.success("No static files were deleted in the last 30 days.")
        else:
            st.markdown(
                f'<div style="background:rgba(255,123,114,0.06);border:1px solid '
                f'rgba(255,123,114,0.25);border-radius:8px;padding:10px 14px;'
                f'margin-bottom:14px;font-size:0.83rem;color:{T["red"]}">'
                f'<b>⚠ {len(_deleted)} static files were deleted.</b> '
                f'Any surviving references to these paths will cause broken styles, '
                f'missing images, or JS errors on the next deployment.</div>',
                unsafe_allow_html=True,
            )
            _df_del = pd.DataFrame(_deleted)[["ts", "ext", "module", "author", "msg", "file"]]
            _df_del["ts"] = _df_del["ts"].str[:19].str.replace("T", " ")
            _df_del.columns = ["Deleted At", "Type", "Module", "Deleted By", "Commit", "Full Path"]
            st.dataframe(
                _df_del,
                use_container_width=True,
                hide_index=True,
                height=400,
                column_config={
                    "Deleted At": st.column_config.TextColumn("Deleted At", width="medium"),
                    "Type":       st.column_config.TextColumn("Type",       width="small"),
                    "Module":     st.column_config.TextColumn("Module",     width="medium"),
                    "Deleted By": st.column_config.TextColumn("Deleted By", width="medium"),
                    "Commit":     st.column_config.TextColumn("Commit",     width="medium"),
                    "Full Path":  st.column_config.TextColumn("Full Path",  width="large"),
                },
            )

    # ── TAB 3 — HOT FILES ─────────────────────────────────────────────────
    with _tab_hot:
        if not _hot:
            st.success("No files with high churn detected (threshold: 5+ commits).")
        else:
            st.markdown(
                f'<p style="font-size:0.83rem;color:{T["text_muted"]};margin-bottom:12px">'
                f'Files touched in 5 or more commits — multiple authors or rapid changes '
                f'indicate instability. These files are high-risk when included in a deploy.</p>',
                unsafe_allow_html=True,
            )
            for _hf in _hot[:30]:
                _hc = _hf["commit_count"]
                _authors_str = ", ".join(_hf["authors"][:3])
                if len(_hf["authors"]) > 3:
                    _authors_str += f" +{len(_hf['authors'])-3} more"
                _intensity = min(1.0, _hc / 20)
                _bar_col = T["red"] if _hc >= 15 else T["amber"] if _hc >= 8 else T["blue"]

                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:12px;'
                    f'padding:8px 12px;border-bottom:1px solid {T["border"]};'
                    f'font-size:0.82rem">'
                    f'<span style="min-width:32px;font-size:1rem;font-weight:800;'
                    f'color:{_bar_col}">{_hc}×</span>'
                    f'<div style="flex:1;min-width:0">'
                    f'<p style="margin:0;color:{T["text"]};font-family:monospace;'
                    f'font-size:0.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
                    f'{_hf["file"]}</p>'
                    f'<p style="margin:2px 0 0 0;color:{T["text_muted"]};font-size:0.72rem">'
                    f'{_hf["ext"]} &nbsp;·&nbsp; {_hf["module"]} &nbsp;·&nbsp; '
                    f'by {_authors_str}</p>'
                    f'</div>'
                    f'<div style="min-width:80px;background:{T["border"]};'
                    f'border-radius:3px;height:6px">'
                    f'<div style="width:{int(_intensity*100)}%;height:6px;'
                    f'background:{_bar_col};border-radius:3px"></div>'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── TAB 4 — BROKEN REFERENCES ─────────────────────────────────────────
    with _tab_broken:
        if not _broken:
            st.success(
                "No broken clientlib references detected — "
                "all deleted clientlibs appear to be unreferenced."
            )
        else:
            st.markdown(
                f'<div style="background:rgba(255,166,87,0.06);border:1px solid '
                f'rgba(255,166,87,0.25);border-radius:8px;padding:10px 14px;'
                f'margin-bottom:16px;font-size:0.83rem;color:{T["amber"]}">'
                f'<b>⚠ {len(_broken)} deleted clientlibs are still referenced</b> in HTML / XML. '
                f'These will load with no styling or broken functionality after deploy.</div>',
                unsafe_allow_html=True,
            )

            for _br in _broken:
                with st.expander(
                    f"⚠  {_br['clientlib_name']}  ·  {_br['ref_count']} reference(s)",
                    expanded=False,
                ):
                    _bc1, _bc2 = st.columns([1, 1], gap="medium")
                    with _bc1:
                        st.markdown(
                            f'<p style="font-size:0.72rem;font-weight:700;'
                            f'text-transform:uppercase;letter-spacing:0.08em;'
                            f'color:{T["text_muted"]};margin:0 0 6px 0">Deleted File</p>'
                            f'<code style="font-size:0.74rem;color:{T["red"]}">'
                            f'{_br["deleted_file"]}</code><br><br>'
                            f'<p style="font-size:0.72rem;font-weight:700;'
                            f'text-transform:uppercase;letter-spacing:0.08em;'
                            f'color:{T["text_muted"]};margin:6px 0 4px 0">AEM Category</p>'
                            f'<code style="font-size:0.78rem;color:{T["amber"]}">'
                            f'{_br["category"] or "(could not resolve)"}</code><br><br>'
                            f'<p style="font-size:0.72rem;color:{T["text_muted"]};margin:0">'
                            f'Deleted <b>{_br["deleted_ts"][:10]}</b> '
                            f'by {_br["deleted_by"]}</p>',
                            unsafe_allow_html=True,
                        )
                    with _bc2:
                        st.markdown(
                            f'<p style="font-size:0.72rem;font-weight:700;'
                            f'text-transform:uppercase;letter-spacing:0.08em;'
                            f'color:{T["text_muted"]};margin:0 0 6px 0">'
                            f'Still Referenced In ({_br["ref_count"]} files)</p>',
                            unsafe_allow_html=True,
                        )
                        for _ref in _br["referenced_in"][:10]:
                            st.markdown(
                                f'<p style="font-size:0.75rem;font-family:monospace;'
                                f'color:{T["text_muted"]};margin:2px 0;padding:2px 6px;'
                                f'background:{T["surface2"]};border-radius:3px">'
                                f'{_ref}</p>',
                                unsafe_allow_html=True,
                            )

    # ── TAB 5 — AUTHOR OWNERSHIP ──────────────────────────────────────────
    with _tab_authors:
        if not _authors:
            st.info("No author data available.")
        else:
            # Top authors bar chart
            _top_auth = _authors[:12]
            _fig_auth = go.Figure(go.Bar(
                x=[a["file_count"] for a in _top_auth],
                y=[a["author"].split("(")[0].strip()[:25] for a in _top_auth],
                orientation="h",
                marker_color=T["blue"],
                text=[str(a["file_count"]) for a in _top_auth],
                textposition="outside",
                textfont=dict(color=T["text"], size=11),
                hovertemplate="<b>%{y}</b><br>%{x} files<extra></extra>",
            ))
            _auth_theme = chart_theme(280)
            _auth_theme["yaxis"] = {**_auth_theme.get("yaxis", {}),
                                    "autorange": "reversed",
                                    "tickfont": {"size": 11, "color": T["text_muted"]}}
            _fig_auth.update_layout(**_auth_theme)
            st.plotly_chart(_fig_auth, use_container_width=True,
                            config={"displayModeBar": False})

            # Author detail cards
            for _au in _authors[:10]:
                _ext_str = "  ".join(
                    f'<code style="background:{T["surface2"]};padding:1px 5px;'
                    f'border-radius:3px;font-size:0.72rem;color:{T["amber"]}">'
                    f'{e} {c}</code>'
                    for e, c in _au["top_exts"].items()
                )
                _mod_str = " · ".join(_au["modules"][:4])
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:14px;'
                    f'padding:10px 14px;border-bottom:1px solid {T["border"]}">'
                    f'<div style="min-width:180px">'
                    f'<p style="margin:0;font-size:0.85rem;font-weight:600;'
                    f'color:{T["text"]}">{_au["author"].split("(")[0].strip()}</p>'
                    f'<p style="margin:1px 0 0;font-size:0.72rem;color:{T["text_muted"]}">'
                    f'Last active: {_au["last_active"][:10]}</p>'
                    f'</div>'
                    f'<div style="min-width:60px;text-align:center">'
                    f'<p style="margin:0;font-size:1.3rem;font-weight:800;'
                    f'color:{T["blue"]}">{_au["file_count"]}</p>'
                    f'<p style="margin:0;font-size:0.68rem;color:{T["text_muted"]}">files</p>'
                    f'</div>'
                    f'<div style="flex:1">'
                    f'<p style="margin:0 0 3px;font-size:0.72rem;color:{T["text_muted"]}">'
                    f'{_mod_str}</p>'
                    f'<div>{_ext_str}</div>'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
