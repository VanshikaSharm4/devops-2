"""
DevOps Intelligence Platform — Adobe Cloud Manager
Program 19905 · IDFC First Bank Limited
"""
import json
import os
import sys
import html as _html
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from dashboard.argus_home import render_argus_home

st.set_page_config(
    page_title="DevOps Intelligence Platform",
    layout="wide",
    initial_sidebar_state="expanded",
)

_TOPBAR_HEIGHT_PX = 64
_TOPBAR_HEIGHT_CSS = f"{_TOPBAR_HEIGHT_PX}px"

# ── Customer registry — loaded from data/customer_config.json ─────────────────
from analysis.paths import customer_config_path as _customer_config_path, secrets_path as _secrets_path_fn, repo_config_path as _repo_config_path, cache_dir as _cache_dir, splunk_exports_dir as _splunk_exports_dir
_CONFIG_PATH  = _customer_config_path()
_SECRETS_PATH = _secrets_path_fn()


def _load_customers() -> dict:
    """
    Load customer registry from data/customer_config.json + data/.secrets.json.
    Falls back to env vars for backward compat.
    Secrets file is gitignored — passwords never go into the repo.
    """
    customers = {}

    # Load config
    cfg = {}
    if _CONFIG_PATH.exists():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Load secrets
    secrets = {}
    if _SECRETS_PATH.exists():
        try:
            secrets = json.loads(_SECRETS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    for name, c in cfg.items():
        _short_upper = c.get("short", "").upper()
        # Try multiple env var patterns to tolerate naming variations:
        # 1. .secrets.json  2. MAS_CM_GIT_PASSWORD  3. MALAYSIA_CM_GIT_PASSWORD
        # 4. Any key in os.environ containing the customer name + CM_GIT_PASSWORD
        _name_upper = name.upper().replace(" ", "_").replace("-", "_")
        pwd = (
            secrets.get(name, {}).get("git_password", "")
            or os.getenv(f"{_short_upper}_CM_GIT_PASSWORD", "")
            or os.getenv(f"{_name_upper}_CM_GIT_PASSWORD", "")
            or next(
                (v for k, v in os.environ.items()
                 if "CM_GIT_PASSWORD" in k and any(
                     part in k.upper() for part in _name_upper.split("_") if len(part) > 3
                 )),
                ""
            )
        )
        # Allow env vars to override customer_config.json values.
        # Pattern: {NAME_UPPER}_GIT_URL, {NAME_UPPER}_GIT_LOCAL_DIR, {SHORT}_GIT_URL etc.
        _git_url = (
            os.getenv(f"{_name_upper}_GIT_URL")
            or os.getenv(f"{_short_upper}_GIT_URL")
            or c.get("git_url", "")
        )
        _git_local_dir = (
            os.getenv(f"{_name_upper}_GIT_LOCAL_DIR")
            or os.getenv(f"{_short_upper}_GIT_LOCAL_DIR")
            or c.get("git_local_dir", "")
        )
        customers[name] = {
            "program_id":    c.get("program_id") or os.getenv(f"PROGRAM_ID_{_short_upper}", ""),
            "pipeline_prod": c.get("pipeline_prod", ""),
            "pipeline_dev":  c.get("pipeline_dev", ""),
            "org_id":        c.get("org_id", ""),
            "tenant_id":     c.get("tenant_id", ""),
            "short":         c.get("short", name[:4].upper()),
            "git_url":       _git_url,
            "git_local_dir": _git_local_dir,
            "git_branch":    c.get("git_branch", "master"),
            "git_username":  c.get("git_username", ""),
            "git_password":  pwd,
            "splunk_index":  c.get("splunk_index", "ams_linux-os"),
        }

    # Fallback: hardcoded defaults if config file missing
    if not customers:
        customers = {
            "IDFC First Bank": {
                "program_id": os.getenv("PROGRAM_ID_IDFC", "19905"),
                "pipeline_prod": os.getenv("PIPELINE_ID_PROD_IDFC", "2357452"),
                "pipeline_dev": os.getenv("PIPELINE_ID_DEV_IDFC", "47202398"),
                "org_id": "358458CC558C6B5D7F000101@AdobeOrg",
                "tenant_id": "idfc", "short": "IDFC",
                "git_local_dir": os.getenv("IDFC_GIT_LOCAL_DIR", ""),
                "git_username": os.getenv("IDFC_CM_GIT_USERNAME", ""),
                "git_password": os.getenv("IDFC_CM_GIT_PASSWORD", ""),
                "git_url": "", "git_branch": "master",
            },
        }
    return customers


def _save_customer(name: str, config: dict, password: str = "") -> None:
    """Save a customer to customer_config.json and password to .secrets.json."""
    # Load existing
    cfg = json.loads(_CONFIG_PATH.read_text()) if _CONFIG_PATH.exists() else {}
    secrets = json.loads(_SECRETS_PATH.read_text()) if _SECRETS_PATH.exists() else {}

    cfg[name] = {k: v for k, v in config.items() if k != "git_password"}
    if password:
        secrets.setdefault(name, {})["git_password"] = password

    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    _SECRETS_PATH.write_text(json.dumps(secrets, indent=2), encoding="utf-8")


def _delete_customer(name: str) -> None:
    """Remove a customer from both config files."""
    if _CONFIG_PATH.exists():
        cfg = json.loads(_CONFIG_PATH.read_text())
        cfg.pop(name, None)
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    if _SECRETS_PATH.exists():
        secrets = json.loads(_SECRETS_PATH.read_text())
        secrets.pop(name, None)
        _SECRETS_PATH.write_text(json.dumps(secrets, indent=2))


_CUSTOMERS = _load_customers()

# Persist selected customer across reruns
# Restore selected customer from URL query params on refresh
# This keeps the customer selection when the page is reloaded
_qp_customer = st.query_params.get("customer", "")
if _qp_customer in _CUSTOMERS:
    st.session_state["selected_customer"] = _qp_customer
elif "selected_customer" not in st.session_state:
    st.session_state["selected_customer"] = list(_CUSTOMERS.keys())[0]

# Build TenantContext from selected customer — do NOT write to os.environ here.
# Writing to os.environ is process-global: 10 concurrent Streamlit sessions share
# one process, so one user's customer switch overwrites another's mid-analysis.
# Instead, pass TenantContext explicitly; only apply_to_env() inside analysis calls.
from analysis.tenant_context import TenantContext as _TenantContext

_active_customer = _CUSTOMERS.get(
    st.session_state.get("selected_customer", "IDFC First Bank"),
    _CUSTOMERS["IDFC First Bank"],
)
_tenant_ctx = _TenantContext.from_customer_dict(
    st.session_state.get("selected_customer", "IDFC First Bank"),
    _active_customer,
)
# Store in session state so analysis calls can access it without re-reading env
st.session_state["_tenant_ctx"] = _tenant_ctx

# Apply to os.environ for backward compat (legacy code that still reads it directly)
_tenant_ctx.apply_to_env()

# Set thread-safe ContextVar values — zero-race alternative to os.environ
# Each Streamlit session gets its own isolated context snapshot via copy_context()
try:
    from analysis.customer_context import set_customer_context as _set_ctx
    _set_ctx(_active_customer)
except Exception:
    pass

# ── Theme tokens ──────────────────────────────────────────────────────────────
# This is a Streamlit application, so Tailwind utility classes are unavailable.
# Keep all visual values in one token map so every component follows the selected
# theme and no page-local colour literals are needed.
if "ui_theme" not in st.session_state:
    st.session_state["ui_theme"] = "light"

_THEMES = {
    "light": {
        "sidebar_bg": "#F8FAFC", "sidebar_border": "#E5E7EB",
        "sidebar_nav_hover": "#F8FAFC", "sidebar_nav_active": "#EFF6FF",
        "sidebar_accent": "#2563EB", "sidebar_text": "#4B5563",
        "sidebar_text_dim": "#6B7280", "sidebar_text_hi": "#111827",
        "bg": "#FFFFFF", "surface": "#FFFFFF", "surface2": "#F8FAFC",
        "border": "#E5E7EB", "border2": "#F1F5F9",
        "text": "#111827", "text_sub": "#4B5563", "text_muted": "#6B7280",
        "red": "#B91C1C", "amber": "#C98900", "green": "#16A34A",
        "blue": "#2563EB", "purple": "#7C3AED", "gray": "#64748B",
        "shadow": "0 0.5rem 1.5rem rgba(15, 23, 42, 0.06)",
    },
    "dark": {
        "sidebar_bg": "#0F172A", "sidebar_border": "#263244",
        "sidebar_nav_hover": "#111827", "sidebar_nav_active": "#172554",
        "sidebar_accent": "#60A5FA", "sidebar_text": "#FFFFFF",
        "sidebar_text_dim": "#FFFFFF", "sidebar_text_hi": "#FFFFFF",
        "bg": "#0B1120", "surface": "#111827", "surface2": "#0B1220",
        "border": "#263244", "border2": "#1E293B",
        "text": "#FFFFFF", "text_sub": "#FFFFFF", "text_muted": "#F1F5F9",
        "red": "#FCA5A5", "amber": "#FCD34D", "green": "#86EFAC",
        "blue": "#93C5FD", "purple": "#C4B5FD", "gray": "#E2E8F0",
        "shadow": "0 0.5rem 1.5rem rgba(0, 0, 0, 0.24)",
    },
}
T = _THEMES[st.session_state["ui_theme"]]
T["chart"] = [T["blue"], T["red"], T["amber"], T["green"], T["purple"], T["gray"]]
_IS_DARK = st.session_state["ui_theme"] == "dark"

_DARK_MODE_CSS = ""
if _IS_DARK:
    _DARK_MODE_CSS = f"""
/* ── Dark mode readability overrides ── */
[data-testid="stSidebar"] {{
    background-color: {T['sidebar_bg']} !important;
}}
[data-testid="stSidebar"] .stButton:first-of-type > button,
[data-testid="stSidebar"] .stButton > button,
[data-testid="stSidebar"] .stButton > button:hover,
[data-testid="stSidebar"] [data-testid="baseButton-primary"] {{
    color: {T['text']} !important;
}}
[data-testid="stDataFrame"] th,
[data-testid="stDataFrame"] td,
[data-testid="stDataFrame"] [data-testid="stDataFrameCell"],
[data-testid="stDataFrame"] [data-testid="stDataFrameRow"] *,
[data-testid="stExpander"] summary,
[data-testid="stExpander"] summary:hover,
.id-chip,
[data-testid="stMain"] code.id-chip {{
    color: {T['text']} !important;
    -webkit-text-fill-color: {T['text']} !important;
    background: {T['surface2']} !important;
    border-color: {T['border']} !important;
}}
[data-testid="stDataFrame"] th,
[data-testid="stDataFrame"] td {{
    color: {T['text']} !important;
    -webkit-text-fill-color: {T['text']} !important;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.risk-executions-table-marker) [data-testid="stButton"] > button,
[data-testid="stVerticalBlockBorderWrapper"]:has(.risk-commits-table-marker) [data-testid="stButton"] > button {{
    color: {T['text']} !important;
    -webkit-text-fill-color: {T['text']} !important;
}}
[data-testid="stTextInput"] input,
[data-testid="stTextInput"] [data-baseweb="input"],
[data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stSelectbox"] > div > div,
[data-testid="stSelectbox"] [data-baseweb="select"] {{
    background: {T['surface']} !important;
    border-color: {T['border']} !important;
    color: {T['text']} !important;
    -webkit-text-fill-color: {T['text']} !important;
    caret-color: {T['text']} !important;
}}
[data-testid="stSelectbox"] [data-baseweb="select"] span,
[data-testid="stSelectbox"] [data-baseweb="select"] div[role="button"] {{
    color: {T['text']} !important;
    -webkit-text-fill-color: {T['text']} !important;
}}
[data-testid="stTextInput"] input::placeholder,
[data-testid="stTextInput"] [data-baseweb="input"]::placeholder,
[data-testid="stTextArea"] textarea::placeholder {{
    color: #000000 !important;
    -webkit-text-fill-color: #000000 !important;
}}
:root {{
    --cm-bg: {T['bg']};
    --cm-surface: {T['surface2']};
    --cm-border: {T['border']};
    --cm-text: {T['text']};
    --cm-text-muted: {T['text_muted']};
}}
.cm-section {{
    background: {T['surface']} !important;
    border-color: {T['border']} !important;
}}
.cm-section-title,
.cm-section-help {{
    color: {T['text']} !important;
    background: {T['surface2']} !important;
    border-color: {T['border']} !important;
}}
.risk-execution-header {{
    color: {T['text']} !important;
}}
.argus-page,
.argus-page .hero-title,
.argus-page .hero-subtitle,
.argus-page .section-title,
.argus-page .link-title,
.argus-page .external-icon,
.argus-page .hero-copy,
.argus-page .link-desc {{
    color: {T['text']} !important;
}}
.argus-page .hero-card,
.argus-page .link-card {{
    background: {T['surface']} !important;
    border-color: {T['border']} !important;
}}
.pg-sub,
.sec-label,
.caption,
.risk-workspace-help,
.risk-customer-label {{
    color: {T['text']} !important;
}}
.ra-ui-card {{
    background-color: {T['surface']} !important;
    box-shadow: {T['shadow']} !important;
}}
.ra-ui-title-text,
.ra-ui-data .ra-ui-headline,
.ra-ui-details p,
.ra-ui-details .ra-ui-finding,
.ra-ui-details .ra-ui-section-label {{
    color: {T['text']} !important;
}}
"""

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
{_DARK_MODE_CSS}

/* ─────────────────────────────────────────
   RESET & BASE
───────────────────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; }}
:root {{
    --space-1: 8px;
    --space-2: 16px;
    --space-3: 24px;
    --space-4: 32px;
}}
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
    padding: 8px 32px 32px !important;
    max-width: 1600px !important;
}}
[data-testid="stMain"] {{
    overflow-y: auto !important;
    overflow-x: hidden !important;
    height: calc(100vh - {_TOPBAR_HEIGHT_CSS}) !important;
    margin-top: {_TOPBAR_HEIGHT_CSS} !important;
}}
[data-testid="stSidebar"] {{
    position: sticky !important;
    top: {_TOPBAR_HEIGHT_CSS} !important;
    left: 0 !important;
    height: calc(100vh - {_TOPBAR_HEIGHT_CSS}) !important;
    min-height: calc(100vh - {_TOPBAR_HEIGHT_CSS}) !important;
    max-height: calc(100vh - {_TOPBAR_HEIGHT_CSS}) !important;
}}
[data-testid="stMain"] [data-testid="stVerticalBlock"] {{
    gap: var(--space-1) !important;
}}
[data-testid="stMain"] [data-testid="stElementContainer"] {{
    margin-bottom: var(--space-1) !important;
}}
[data-testid="stMain"] [data-testid="stHorizontalBlock"] {{
    gap: var(--space-2) !important;
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
    align-items: stretch !important;
    visibility: visible !important;
    opacity: 1 !important;
    pointer-events: auto !important;
    background-color: #FAFAFA !important;
    border: none !important;
    position: sticky !important;
    top: {_TOPBAR_HEIGHT_CSS} !important;
    height: calc(100vh - {_TOPBAR_HEIGHT_CSS}) !important;
    min-height: calc(100vh - {_TOPBAR_HEIGHT_CSS}) !important;
    max-height: calc(100vh - {_TOPBAR_HEIGHT_CSS}) !important;
    flex-shrink: 0 !important;
    overflow: hidden !important;
    overflow-y: hidden !important;
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
[data-testid="stSidebarHeader"],
[data-testid="stSidebar"] header {{
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
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
/* Keep navigation compact and aligned to the top of the sidebar. */
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{
    gap: 0 !important;
}}
[data-testid="stSidebar"] .element-container,
[data-testid="stSidebar"] [data-testid="element-container"] {{
    margin-bottom: 0 !important;
}}
/* Selectbox always fully clickable and elevated */
[data-testid="stSidebar"] [data-testid="stSelectbox"],
[data-testid="stSidebar"] [data-testid="stSelectbox"] *,
[data-testid="stSidebar"] [data-baseweb="select"],
[data-testid="stSidebar"] [data-baseweb="select"] *,
[data-testid="stSidebar"] [data-testid="element-container"]:has([data-testid="stSelectbox"]) {{
    pointer-events: auto !important;
    cursor: pointer !important;
    z-index: 99 !important;
}}
[data-baseweb="popover"], [data-baseweb="popover"] *,
[role="listbox"], [role="listbox"] * {{
    pointer-events: auto !important;
    cursor: pointer !important;
}}

[data-testid="stSidebar"] {{
    color: {T['sidebar_text']} !important;
}}
[data-testid="stSidebar"] p, [data-testid="stSidebar"] span, [data-testid="stSidebar"] label {{
    color: inherit !important;
    font-size: 0.98rem !important;
    line-height: 1.3 !important;
}}
[data-testid="stSidebar"], [data-testid="stSidebar"] * {{
    cursor: default !important;
}}
[data-testid="stSidebar"] button,
[data-testid="stSidebar"] a,
[data-testid="stSidebar"] [role="button"],
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] select,
[data-testid="stSidebar"] [data-baseweb="select"],
[data-testid="stSidebar"] label {{
    cursor: pointer !important;
}}
[data-testid="stSidebar"] > div:first-child,
[data-testid="stSidebar"] > div:first-child > div,
[data-testid="stSidebar"] > div:first-child > div > div {{
    padding-top: 0 !important;
    margin-top: 0 !important;
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
[data-testid="stSidebar"] .stButton > button [data-testid="stIconMaterial"],
[data-testid="stSidebar"] button [data-testid="stIconMaterial"],
[data-testid="stSidebar"][data-testid="stSidebar"] button[data-testid^="stBaseButton"] [data-testid="stIconMaterial"] {{
    color: currentColor !important;
    flex: 0 0 auto !important;
    margin: 0 !important;
    font-size: 1.35rem !important;
    width: 1.35rem !important;
    height: 1.35rem !important;
    line-height: 1 !important;
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
    height: 30px !important;
    min-height: 30px !important;
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
   METRIC CARDS
───────────────────────────────────────── */
[data-testid="metric-container"],
[data-testid="stMetric"] {{
    background: #FFFFFF !important;
    border: 1px solid #E5E7EB !important;
    border-radius: 14px !important;
    padding: 24px !important;
    box-shadow: 0 1px 2px rgba(15,23,42,.04) !important;
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
    font-size: 1.75rem;
    font-weight: 750;
    color: {T['text']};
    letter-spacing: -0.02em;
    line-height: 1.3;
    margin: 0;
}}
.pg-sub {{
    font-size: 0.82rem;
    color: {T['text_muted']};
    margin-top: 0.35rem;
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
    padding: 0.55rem 1rem !important;
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
    padding: 0.65rem 1rem !important;
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
[data-testid="stSelectbox"] > div > div {{
    background: #FFFFFF !important;
    border: 1px solid #DEDEDE !important;
    border-radius: 4px !important;
    font-size: 0.84rem !important;
    color: #1A1A1A !important;
    -webkit-text-fill-color: #1A1A1A !important;
    caret-color: #1A1A1A !important;
    box-shadow: none !important;
}}
[data-testid="stSelectbox"] [data-baseweb="select"] {{
    background: #FFFFFF !important;
    border: 1px solid #DEDEDE !important;
    border-radius: 4px !important;
    font-size: 0.84rem !important;
    color: #1A1A1A !important;
    -webkit-text-fill-color: #1A1A1A !important;
    caret-color: #1A1A1A !important;
    box-shadow: none !important;
    padding-left: 10px !important;
    padding-right: 10px !important;
}}
[data-testid="stTextInput"] input::placeholder,
[data-testid="stTextArea"] textarea::placeholder {{
    color: {T['text_muted']} !important;
    -webkit-text-fill-color: {T['text_muted']} !important;
    opacity: 1 !important;
}}
[placeholder="e.g. 70190dfe144ba4e1d92972a329dea9d4f3f540eb"]::placeholder {{
    color: #000000 !important;
    -webkit-text-fill-color: #000000 !important;
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
    padding: 10px 14px !important;
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

/* Content card containers */
[data-testid="stMain"] [data-testid="stVerticalBlockBorderWrapper"][style*="border"] {{
    background: #FFFFFF !important;
    border: 1px solid #E5E7EB !important;
    border-radius: 14px !important;
    padding: 24px !important;
    margin-bottom: var(--space-3) !important;
    box-shadow: 0 1px 2px rgba(15,23,42,.04) !important;
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
div[data-testid="stCode"] {{
    background-color: #F5F5F5 !important;
    border: none !important;
    border-radius: 3px !important;
    color: #1A1A1A !important;
    margin-bottom: 0.35rem !important;
}}
[data-testid="stMain"] pre {{
    background-color: #F5F5F5 !important;
    border: none !important;
    border-radius: 3px !important;
    color: #1A1A1A !important;
    margin-bottom: 0.35rem !important;
    padding: 10px 14px !important;
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
    background: #FFFFFF !important;
    border: 1px solid #E5E7EB !important;
    border-radius: 14px !important;
    box-shadow: 0 1px 2px rgba(15,23,42,.04) !important;
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
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] .sidebar-cache-status {{
    color: {T['sidebar_text_dim']} !important;
    font-size: 0.625rem !important;
    opacity: 0.72;
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

/* AI analyzing loader — From Uiverse.io by mobinkakei */
.ai-analyze-loader {{
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    padding: 28px 0 16px;
}}
.ai-analyze-loader-msg {{
    font-size: 0.85rem;
    font-weight: 500;
    color: {T['text_sub']};
    margin: 0;
    text-align: center;
}}
.ai-analyze-loader-wrapper {{
    width: 200px;
    height: 60px;
    position: relative;
    z-index: 1;
}}
.ai-analyze-circle {{
    width: 20px;
    height: 20px;
    position: absolute;
    border-radius: 50%;
    background-color: {T['blue']};
    left: 15%;
    transform-origin: 50%;
    animation: ai-analyze-circle .5s alternate infinite ease;
}}
@keyframes ai-analyze-circle {{
    0% {{
        top: 60px;
        height: 5px;
        border-radius: 50px 50px 25px 25px;
        transform: scaleX(1.7);
    }}
    40% {{
        height: 20px;
        border-radius: 50%;
        transform: scaleX(1);
    }}
    100% {{
        top: 0%;
    }}
}}
.ai-analyze-circle:nth-child(2) {{
    left: 45%;
    animation-delay: .2s;
}}
.ai-analyze-circle:nth-child(3) {{
    left: auto;
    right: 15%;
    animation-delay: .3s;
}}
.ai-analyze-shadow {{
    width: 20px;
    height: 4px;
    border-radius: 50%;
    background-color: rgba(20, 115, 230, 0.35);
    position: absolute;
    top: 62px;
    transform-origin: 50%;
    z-index: -1;
    left: 15%;
    filter: blur(1px);
    animation: ai-analyze-shadow .5s alternate infinite ease;
}}
@keyframes ai-analyze-shadow {{
    0% {{
        transform: scaleX(1.5);
    }}
    40% {{
        transform: scaleX(1);
        opacity: .7;
    }}
    100% {{
        transform: scaleX(.2);
        opacity: .4;
    }}
}}
.ai-analyze-shadow:nth-child(4) {{
    left: 45%;
    animation-delay: .2s;
}}
.ai-analyze-shadow:nth-child(5) {{
    left: auto;
    right: 15%;
    animation-delay: .3s;
}}

/* Risk assessment summary cards — From Uiverse.io by Yaya12085 */
.ra-hero-stack {{
    display: flex;
    flex-direction: column;
    gap: 14px;
    margin: 0 0 18px 0;
}}
.ra-ui-card {{
    padding: 1rem;
    background-color: #fff;
    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
    width: 100%;
    border-radius: 20px;
    box-sizing: border-box;
}}
.ra-ui-card-hero .ra-ui-data .ra-ui-headline {{
    font-size: 1.4rem;
    line-height: 1.7rem;
}}
.ra-ui-card-compact {{
    padding: 0.85rem 1rem;
    border-radius: 14px;
    box-shadow: 0 4px 10px -2px rgba(0, 0, 0, 0.08), 0 2px 4px -1px rgba(0, 0, 0, 0.04);
}}
.ra-ui-card-compact .ra-ui-title-text {{
    font-size: 15px;
}}
.ra-ui-card-compact .ra-ui-data .ra-ui-headline {{
    font-size: 1.35rem;
    line-height: 1.5rem;
    margin-top: 0.65rem;
    margin-bottom: 0.65rem;
}}
.ra-ui-card-compact .ra-ui-details {{
    margin-top: 0.65rem;
}}
.ra-ui-card-compact .ra-ui-details p {{
    font-size: 0.74rem;
    margin-bottom: 6px;
}}
.ra-ui-title {{
    display: flex;
    align-items: center;
    gap: 0.5rem;
}}
.ra-ui-title span {{
    position: relative;
    padding: 0.5rem;
    width: 1.5rem;
    height: 1.5rem;
    border-radius: 9999px;
    flex-shrink: 0;
}}
.ra-ui-title span svg {{
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    color: #ffffff;
    height: 1rem;
    width: 1rem;
}}
.ra-ui-title-text {{
    margin: 0 0 0 0.5rem;
    color: #374151;
    font-size: 18px;
    font-weight: 600;
    flex: 1;
}}
.ra-ui-percent {{
    margin: 0 0 0 0.5rem;
    font-weight: 400;
    font-size: 0.62rem;
    display: flex;
    white-space: nowrap;
    opacity: 0.75;
}}
.ra-ui-data {{
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
}}
.ra-ui-data .ra-ui-headline {{
    margin-top: 0.75rem;
    margin-bottom: 0.75rem;
    color: #1F2937;
    font-size: 1.75rem;
    line-height: 2rem;
    font-weight: 700;
    text-align: left;
}}
.ra-ui-details {{
    margin-top: 1rem;
}}
.ra-ui-details p {{
    margin: 0 0 8px 0;
    line-height: 1.6;
    color: {T['text']};
    font-size: 0.85rem;
}}
.ra-ui-details .ra-ui-sub {{
    color: {T['text_muted']};
    font-size: 0.80rem;
}}
.ra-ui-details .ra-ui-footnote {{
    color: {T['text_muted']};
    font-size: 0.68rem;
    margin-top: 10px !important;
}}
.ra-ui-details .ra-ui-section-label {{
    font-size: 0.62rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: {T['text_muted']};
    margin: 12px 0 6px 0 !important;
}}
.ra-ui-details .ra-ui-finding {{
    font-size: 0.76rem;
    padding: 4px 0;
    border-bottom: 1px solid {T['border2']};
}}
.ra-ui-details .ra-ui-action {{
    font-size: 0.74rem;
    font-weight: 600;
}}

/* Historical matches — compact horizontal carousel */
.hist-carousel-wrap {{
    margin: 0 0 18px 0;
}}
.hist-carousel-label {{
    font-size: 0.62rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: {T['text_muted']};
    margin: 0 0 8px 0;
}}
.hist-carousel {{
    display: flex;
    gap: 10px;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    padding-bottom: 6px;
    -webkit-overflow-scrolling: touch;
}}
.hist-carousel::-webkit-scrollbar {{
    height: 4px;
}}
.hist-carousel::-webkit-scrollbar-thumb {{
    background: {T['border']};
    border-radius: 4px;
}}
.hist-slide {{
    scroll-snap-align: start;
    flex: 0 0 min(272px, 78vw);
    padding: 10px 12px;
    background: #fff;
    border: 1px solid {T['border']};
    border-radius: 10px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    box-sizing: border-box;
}}
.hist-slide-top {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    flex-wrap: wrap;
}}
.hist-slide-score {{
    font-size: 0.72rem;
    font-weight: 800;
    padding: 2px 7px;
    border-radius: 4px;
    white-space: nowrap;
}}
.hist-slide-step {{
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: {T['text_muted']};
    background: {T['surface2']};
    border: 1px solid {T['border']};
    padding: 2px 6px;
    border-radius: 4px;
}}
.hist-slide-src {{
    font-size: 0.65rem;
    color: {T['text_muted']};
    margin-left: auto;
    white-space: nowrap;
}}
.hist-slide-cause {{
    font-size: 0.74rem;
    color: {T['text']};
    line-height: 1.45;
    margin: 0;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
}}
.hist-carousel-empty {{
    font-size: 0.74rem;
    color: {T['text_muted']};
    padding: 10px 0;
    margin: 0;
}}

</style>
""", unsafe_allow_html=True)

# ── Premium workspace overrides ───────────────────────────────────────────────
# Kept after the legacy stylesheet so the page shell, navigation, and assessment
# workspace consistently inherit the active token map.
st.markdown(f"""
<style>
:root {{
    --app-bg: {T["bg"]};
    --panel-bg: {T["surface"]};
    --panel-soft: {T["surface2"]};
    --border: {T["border"]};
    --border-soft: {T["border2"]};
    --text: {T["text"]};
    --text-subtle: {T["text_sub"]};
    --text-muted: {T["text_muted"]};
    --sidebar-bg: {T["sidebar_bg"]};
    --sidebar-text: {T["sidebar_text"]};
    --sidebar-active: {T["sidebar_nav_active"]};
    --accent: {T["sidebar_accent"]};
    --elevation: {T["shadow"]};
}}

[data-testid="stAppViewContainer"],
[data-testid="stMain"],
.main .block-container,
[data-testid="stMainBlockContainer"] {{
    background: var(--app-bg) !important;
    color: var(--text) !important;
}}
.block-container {{
    max-width: 1600px !important;
    margin-inline: auto !important;
    padding: 8px 32px 32px !important;
}}
[data-testid="stSidebar"] {{
    background: var(--sidebar-bg) !important;
    border: none !important;
}}
[data-testid="stSidebar"] .stButton > button,
[data-testid="stSidebar"] button[kind="secondary"],
[data-testid="stSidebar"] button[kind="primary"] {{
    min-height: 2.75rem !important;
    margin-block: 0.125rem !important;
    padding-inline: 0.75rem !important;
    border: 0 !important;
    border-radius: 0.75rem !important;
    color: var(--sidebar-text) !important;
}}
[data-testid="stSidebar"] button[kind="secondary"]:hover {{
    background: {T["sidebar_nav_hover"]} !important;
    color: var(--text) !important;
}}
[data-testid="stSidebar"] button[kind="primary"] {{
    background: var(--sidebar-active) !important;
    color: var(--accent) !important;
    box-shadow: inset 0 0 0 0.0625rem var(--border-soft) !important;
}}
[data-testid="stSidebar"] button:focus-visible,
[data-testid="stMain"] button:focus-visible {{
    outline: 0.125rem solid var(--accent) !important;
    outline-offset: 0.125rem !important;
}}

/* ── Adobe CM design tokens ── */
:root {{
    --cm-bg: #FFFFFF;
    --cm-surface: #F5F5F5;
    --cm-border: #E0E0E0;
    --cm-text: #1E1E1E;
    --cm-text-muted: #6E6E6E;
    --cm-blue: #1473E6;
    --cm-green: #12805C;
    --cm-red: #D7373F;
    --cm-amber: #E68619;
    --cm-selected-bg: #EBF4FF;
    --cm-selected-border: #1473E6;
}}

/* ── CM page header / breadcrumb ── */
.cm-page-header {{
    padding: 20px 0 16px 0;
    border-bottom: 1px solid var(--cm-border);
    margin-bottom: 20px;
}}
.cm-breadcrumb {{
    font-size: 12px;
    color: var(--cm-text-muted);
    margin: 0 0 6px 0;
    letter-spacing: 0.01em;
    line-height: 1.4;
}}
.cm-page-title {{
    font-size: 20px;
    font-weight: 700;
    color: var(--cm-text);
    margin: 0;
    line-height: 1.3;
}}

/* ── CM section container (flat, no heavy card) ── */
.cm-section {{
    background: #FFFFFF;
    border: 1px solid #E0E0E0;
    border-radius: 4px;
    margin-bottom: 16px;
    overflow: hidden;
}}
.cm-section-title {{
    font-size: 13px;
    font-weight: 600;
    color: #1E1E1E;
    padding: 12px 16px 10px;
    border-bottom: 1px solid #E0E0E0;
    margin: 0;
    background: #FAFAFA;
}}
.cm-section-help {{
    font-size: 12px;
    color: #6E6E6E;
    padding: 8px 16px;
    margin: 0;
    border-bottom: 1px solid #E0E0E0;
    line-height: 1.5;
    background: #FAFAFA;
}}

/* ── CM status chips ── */
.risk-status {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    width: fit-content;
    border-radius: 3px;
    padding: 2px 6px;
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.01em;
    background: transparent;
    border: none;
}}
.risk-status-finished {{ color: #12805C; }}
.risk-status-failed,
.risk-status-error {{ color: #D7373F; }}
.risk-status-cancelled {{ color: #E68619; }}
.risk-status-running {{ color: #1473E6; }}

/* ── CM execution header label ── */
.risk-execution-header {{
    color: #6E6E6E;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}}

/* ── Workspace marker / card target ── */
.risk-workspace-marker {{
    display: none;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.risk-workspace-marker) {{
    background: #FFFFFF !important;
    border: 1px solid #E0E0E0 !important;
    border-radius: 4px !important;
    box-shadow: none !important;
    padding: 0 !important;
}}

/* Overview chart cards */
.overview-card-marker {{
    display: none;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.overview-card-marker) {{
    background: #FFFFFF !important;
    border: 1px solid #E5E7EB !important;
    border-radius: 14px !important;
    padding: 24px !important;
    min-height: 200px !important;
    margin-bottom: var(--space-3) !important;
    box-shadow: 0 1px 2px rgba(15,23,42,.04) !important;
}}
.risk-workspace-help {{
    color: #6E6E6E;
    font-size: 12.5px;
    line-height: 1.5;
    margin: 0;
}}

/* ── Selected execution row highlight ── */
.cm-row-selected {{
    background: #EBF4FF !important;
    border-left: 3px solid #1473E6 !important;
}}

/* ── SHA input bar ── */
.cm-sha-bar {{
    background: #FFFFFF;
    border: 1px solid #E0E0E0;
    border-radius: 4px;
    padding: 12px 16px;
    margin: 8px 0 6px 0;
}}
.cm-sha-bar-title {{
    font-size: 13px;
    font-weight: 600;
    color: #1E1E1E;
    margin: 0 0 4px 0;
}}
.cm-sha-bar-help {{
    font-size: 12px;
    color: #6E6E6E;
    margin: 0;
    line-height: 1.6;
}}

/* ── Legacy compat (unchanged pages) ── */
.risk-customer-label {{
    margin: 0 0 0.25rem;
    color: var(--text-muted);
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}}
/* Hide the legacy standalone risk-level label immediately before the verdict. */
[data-testid="stElementContainer"]:has(+ [data-testid="stElementContainer"] .risk-verdict-banner) {{
    display: none !important;
}}
.risk-workspace {{
    background: var(--panel-bg);
    border: 0.0625rem solid var(--border);
    border-radius: 1.25rem;
    box-shadow: var(--elevation);
    padding: clamp(1rem, 2vw, 1.5rem);
}}
.risk-workspace-header {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1rem;
}}
.risk-execution-row {{
    border-top: 0.0625rem solid var(--border-soft);
}}
.risk-execution-table {{
    overflow-x: auto;
    border: 0.0625rem solid var(--border);
    border-radius: 1rem;
}}
.ra-table-wrap {{
    padding: 0 1rem 1rem;
    overflow-x: auto;
}}
.ra-table {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    font-size: 0.8125rem;
    color: var(--text);
}}
.ra-table th {{
    padding: 0.625rem 0.75rem;
    color: var(--text-muted);
    background: var(--panel-soft);
    border-bottom: 0.0625rem solid var(--border);
    font-size: 0.6875rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    text-align: left;
    white-space: nowrap;
}}
.ra-table td {{
    padding: 0.6875rem 0.75rem;
    border-bottom: 0.0625rem solid var(--border-soft);
    vertical-align: middle;
    line-height: 1.35;
}}
.ra-table tbody tr:nth-child(even) {{
    background: rgba(148, 163, 184, 0.05);
}}
.ra-table tbody tr.ra-selected {{
    background: var(--sidebar-active);
    box-shadow: inset 0.1875rem 0 0 var(--accent);
}}
.ra-table tbody tr:last-child td {{
    border-bottom: 0;
}}
.ra-mono {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.78rem;
    font-weight: 700;
    color: var(--text);
}}
.ra-muted {{
    color: var(--text-muted);
    font-size: 0.75rem;
}}
.ra-strong {{
    color: var(--text);
    font-weight: 600;
}}
.ra-chip {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 5.75rem;
    border-radius: 999px;
    padding: 0.1875rem 0.625rem;
    font-size: 0.6875rem;
    font-weight: 700;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    border: 0.0625rem solid transparent;
}}
.ra-chip-finished {{
    color: #12805C;
    background: #EDFAF3;
    border-color: #BDECD3;
}}
.ra-chip-failed, .ra-chip-error {{
    color: #D7373F;
    background: #FEF0F0;
    border-color: #FBCECE;
}}
.ra-chip-cancelled {{
    color: #9A5B00;
    background: #FEF7E1;
    border-color: #F8D49A;
}}
.ra-chip-running {{
    color: #1473E6;
    background: #EBF4FF;
    border-color: #B7D7FF;
}}
.ra-table-select {{
    padding: 0 1rem 1rem;
    max-width: 32rem;
}}
.risk-executions-table-marker,
.risk-commits-table-marker {{
    display: none;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.risk-executions-table-marker),
[data-testid="stVerticalBlockBorderWrapper"]:has(.risk-commits-table-marker) {{
    padding: 24px !important;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.risk-executions-table-marker) [data-testid="stButton"] > button,
[data-testid="stVerticalBlockBorderWrapper"]:has(.risk-commits-table-marker) [data-testid="stButton"] > button {{
    min-height: 72px !important;
    height: 72px !important;
    padding: 0 !important;
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    color: var(--text) !important;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace !important;
    font-size: 0.78rem !important;
    font-weight: 600 !important;
    text-align: left !important;
    justify-content: flex-start !important;
}}
[data-testid="stVerticalBlockBorderWrapper"]:has(.risk-executions-table-marker) [data-testid="stButton"] > button:hover,
[data-testid="stVerticalBlockBorderWrapper"]:has(.risk-commits-table-marker) [data-testid="stButton"] > button:hover {{
    background: #F8FAFC !important;
}}
[data-testid="stMain"] [data-testid="stSelectbox"] [data-baseweb="select"] > div {{
    background: var(--panel-bg) !important;
    border-color: var(--border) !important;
    border-radius: 0.75rem !important;
    color: var(--text) !important;
}}
[data-testid="stMain"] [data-testid="stSelectbox"] [data-baseweb="select"] svg {{
    fill: var(--text-muted) !important;
}}
@media (max-width: 47.999rem) {{
    [data-testid="stSidebar"] {{
        min-width: 3.5rem !important;
        max-width: 3.5rem !important;
    }}
    .risk-workspace-header {{
        flex-direction: column;
    }}
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


def format_duration_hm(minutes: object) -> str:
    """Convert minute values to a readable hours/minutes string."""
    try:
        total_minutes = int(float(minutes))
    except (TypeError, ValueError):
        return "—"

    if total_minutes <= 0:
        return "—"

    hours, mins = divmod(total_minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def overview_section_title(text: str) -> None:
    st.markdown(
        f'<p style="font-size:0.82rem;font-weight:650;color:{T["text"]};'
        f'margin:0 0 0.7rem 0">{text}</p>',
        unsafe_allow_html=True,
    )


def overview_kpi_icon(kind: str, color: str) -> str:
    paths = {
        "activity": '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
        "check": '<path d="m5 12 4 4L19 6"/>',
        "warning": '<path d="M12 4 3.8 19h16.4L12 4Z"/><path d="M12 9v4m0 3h.01"/>',
        "close": '<path d="m7 7 10 10M17 7 7 17"/>',
        "analytics": '<path d="M4 19V9m5 10V5m5 14v-7m5 7V3"/>',
    }
    return (
        f'<span style="width:30px;height:30px;border-radius:8px;display:inline-flex;'
        f'align-items:center;justify-content:center;background:{color}12;color:{color}">'
        f'<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{paths[kind]}</svg></span>'
    )


def _ai_analyzing_loader_html(message: str = "") -> str:
    return (
        '<div id="risk-analysis-anchor" class="ai-analyze-loader">'
        '<div class="ai-analyze-loader-wrapper">'
        '<div class="ai-analyze-circle"></div>'
        '<div class="ai-analyze-circle"></div>'
        '<div class="ai-analyze-circle"></div>'
        '<div class="ai-analyze-shadow"></div>'
        '<div class="ai-analyze-shadow"></div>'
        '<div class="ai-analyze-shadow"></div>'
        '</div>'
        '</div>'
    )


@contextmanager
def ai_analyzing_loader(message: str):
    """Show bouncing-dots loader while AI risk analysis runs."""
    _slot = st.empty()
    _slot.markdown(_ai_analyzing_loader_html(message), unsafe_allow_html=True)
    try:
        yield
    finally:
        _slot.empty()


def _scroll_risk_page_bottom_if_needed() -> None:
    """Smooth-scroll the main pane to the bottom after a risk table selection."""
    if not st.session_state.pop("risk_scroll_to_analysis", False):
        return
    import streamlit.components.v1 as components
    components.html(
        """<script>
        (function scrollRiskPageToBottom() {
            const parentDoc = window.parent.document;
            const scrollBottom = () => {
                const main = parentDoc.querySelector('[data-testid="stMain"]');
                if (main) {
                    main.scrollTo({ top: main.scrollHeight, behavior: 'smooth' });
                    return;
                }
                const appView = parentDoc.querySelector('[data-testid="stAppViewContainer"]');
                if (appView) {
                    appView.scrollTo({ top: appView.scrollHeight, behavior: 'smooth' });
                    return;
                }
                window.parent.scrollTo({ top: parentDoc.body.scrollHeight, behavior: 'smooth' });
            };
            scrollBottom();
            let attempts = 0;
            const timer = setInterval(() => {
                scrollBottom();
                attempts += 1;
                if (attempts >= 12) clearInterval(timer);
            }, 200);
        })();
        </script>""",
        height=0,
    )


def _ra_card_icon_svg(kind: str) -> str:
    icons = {
        "check": (
            '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" '
            'stroke-width="2.5" stroke="currentColor">'
            '<path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/>'
            '</svg>'
        ),
        "warn": (
            '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" '
            'stroke-width="2.5" stroke="currentColor">'
            '<path stroke-linecap="round" stroke-linejoin="round" '
            'd="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>'
            '</svg>'
        ),
        "x": (
            '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" '
            'stroke-width="2.5" stroke="currentColor">'
            '<path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>'
            '</svg>'
        ),
        "server": (
            '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" '
            'stroke-width="2" stroke="currentColor">'
            '<path stroke-linecap="round" stroke-linejoin="round" '
            'd="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2"/>'
            '</svg>'
        ),
        "build": (
            '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" '
            'stroke-width="2" stroke="currentColor">'
            '<path stroke-linecap="round" stroke-linejoin="round" '
            'd="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/>'
            '<path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>'
            '</svg>'
        ),
    }
    return icons.get(kind, icons["check"])


def _strip_risk_finding_prefix(text: str) -> str:
    """Remove [LOW]/[MEDIUM]/[HIGH]/[CERTAIN] prefix from finding text for display."""
    import re
    return re.sub(r"^\[(?:LOW|MEDIUM|HIGH|CERTAIN)\]\s*", "", (text or "").strip())


def risk_summary_card_html(
    title: str,
    percent_label: str,
    percent_color: str,
    icon_bg: str,
    headline: str,
    body_html: str = "",
    icon_kind: str = "check",
    compact: bool = False,
) -> str:
    _variant = "ra-ui-card-compact" if compact else "ra-ui-card-hero"
    _percent_html = (
        f'<p class="ra-ui-percent" style="color:{percent_color}">{percent_label}</p>'
        if percent_label else ""
    )
    return (
        f'<div class="ra-ui-card {_variant}">'
        f'<div class="ra-ui-title">'
        f'<span style="background-color:{icon_bg}">'
        f'{_ra_card_icon_svg(icon_kind)}'
        f'</span>'
        f'<p class="ra-ui-title-text">{title}</p>'
        f'{_percent_html}'
        f'</div>'
        f'<div class="ra-ui-data">'
        f'<p class="ra-ui-headline">{headline}</p>'
        f'<div class="ra-ui-details">{body_html}</div>'
        f'</div>'
        f'</div>'
    )


def historical_matches_carousel_html(hits: list, tokens: dict) -> str:
    """Minimal horizontal carousel for historical pipeline matches."""
    if not hits:
        return (
            '<div class="hist-carousel-wrap">'
            '<p class="hist-carousel-label">Historical Matches</p>'
            '<p class="hist-carousel-empty">No similar failures in pipeline memory yet.</p>'
            '</div>'
        )
    import re as _re_car
    slides = []
    for _h in hits[:5]:
        _score = int(_h.get("similarity_score", 0) * 100)
        _step = (_h.get("step") or "unknown").strip()
        _cause = ((_h.get("root_cause") or "").strip())[:120]
        if len((_h.get("root_cause") or "")) > 120:
            _cause += "…"
        _eid = str(_h.get("execution_id", ""))
        _sim_col = tokens["red"] if _score >= 85 else tokens["amber"] if _score >= 70 else tokens["green"]
        _sha_m = _re_car.search(r"risk-([a-f0-9]{6,12})-(\w+)", _eid)
        if _eid.isdigit():
            _src = "#" + _eid[:8]
        elif _sha_m:
            _src = _sha_m.group(1)[:8]
        else:
            _src = _eid[:10] or "—"
        slides.append(
            f'<div class="hist-slide">'
            f'<div class="hist-slide-top">'
            f'<span class="hist-slide-score" style="color:{_sim_col};'
            f'background:{_sim_col}18;border:1px solid {_sim_col}44">{_score}%</span>'
            f'<span class="hist-slide-step">{_step}</span>'
            f'<span class="hist-slide-src">{_src}</span>'
            f'</div>'
            f'<p class="hist-slide-cause">{_cause or "Similar failure pattern"}</p>'
            f'</div>'
        )
    return (
        '<div class="hist-carousel-wrap">'
        '<p class="hist-carousel-label">Historical Matches · past pipeline failures like this change</p>'
        f'<div class="hist-carousel">{"".join(slides)}</div>'
        '</div>'
    )


@contextmanager
def content_card(*, overview: bool = False):
    """Single bordered card — avoids empty white blocks from split <div class='panel'> tags."""
    with st.container(border=True):
        if overview:
            st.markdown('<span class="overview-card-marker"></span>', unsafe_allow_html=True)
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
        f'letter-spacing:0.06em;color:{T["text_muted"]};padding:0.75rem 1rem;'
        f'background:{T["surface2"]};border-bottom:1px solid {T["border"]};'
        f'text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{col_labels.get(c, c)}</th>'
        for c in show_cols
    )
    rows = ""
    for _, row in df.iterrows():
        cells = "".join(
            f'<td style="font-size:0.81rem;color:{T["text"]};padding:0.78rem 1rem;'
            f'border-bottom:1px solid {T["border2"]};white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis;max-width:280px">'
            f'{str(row[c])[:90]}</td>'
            for c in show_cols
        )
        rows += f'<tr class="argus-data-row">{cells}</tr>'
    st.markdown(
        f'<style>'
        f'.argus-data-table .argus-data-row:nth-child(even){{background:{T["surface2"]};}}'
        f'.argus-data-table .argus-data-row:hover{{background:#F1F5F9;}}'
        f'</style>'
        f'<div class="argus-data-table" style="overflow-x:auto;border:1px solid {T["border2"]};'
        f'border-radius:10px;margin-bottom:0.25rem">'
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
        f'border-radius:10px;padding:0.75rem 1rem;margin-bottom:0.5rem">{rows}</div>',
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
        f'padding:0.65rem 0;border-bottom:1px solid {T["border2"]}">'
        f'<span style="font-size:0.8rem;color:{T["text"]};width:130px;'
        f'flex-shrink:0;font-weight:500">{label}</span>'
        f'<div style="flex:1;height:8px;background:{T["border2"]};border-radius:999px">'
        f'<div style="width:{pct}%;height:8px;background:{color};'
        f'border-radius:999px;transition:width 0.3s"></div></div>'
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


# ── Background Splunk refresh ─────────────────────────────────────────────────
import threading as _threading

# Per-program_id locks — HDFC refresh doesn't block IDFC refresh
_splunk_refresh_locks: dict = {}
_splunk_refresh_state = {"running": False, "done": False, "error": None}


def _get_splunk_lock(program_id: str) -> _threading.Lock:
    if program_id not in _splunk_refresh_locks:
        _splunk_refresh_locks[program_id] = _threading.Lock()
    return _splunk_refresh_locks[program_id]


def _run_splunk_refresh_bg():
    """Fetch fresh Splunk data in a background thread, save to cache."""
    global _splunk_refresh_state
    try:
        from analysis.ingest import load_data, _save_cache
        _pid = int(os.getenv("PROGRAM_ID", "19905"))
        pipeline_df, failed_df, failed_steps_df, share_map = load_data(
            program_id=_pid, force_refresh=True,
            skip_share_names=False,  # Failure Pinpoint needs share names to fetch logs
        )
        _save_cache(pipeline_df, failed_df, failed_steps_df, share_map)
        _splunk_refresh_state["done"]  = True
        _splunk_refresh_state["error"] = None
    except Exception as e:
        _splunk_refresh_state["error"] = str(e)[:200]
    finally:
        _splunk_refresh_state["running"] = False


def _maybe_start_bg_refresh(force: bool = False):
    """Start background Splunk refresh if not running and cache is stale/empty."""
    _pid_key = str(os.getenv("PROGRAM_ID", "unknown"))
    with _get_splunk_lock(_pid_key):
        if _splunk_refresh_state["running"]:
            return
        try:
            from analysis.ingest import _cache_is_fresh, _use_splunk_api, CACHE_FILE
            if _use_splunk_api() and (force or not _cache_is_fresh()):
                _splunk_refresh_state["running"] = True
                _splunk_refresh_state["done"]    = False
                t = _threading.Thread(target=_run_splunk_refresh_bg, daemon=True)
                t.start()
        except Exception:
            pass


# ── Shared data ───────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def load_splunk_data(program_id: str = "19905"):
    """
    Returns data instantly — never blocks the UI.
    Priority: disk cache → CSV fallback → empty DataFrame.
    Fresh Splunk fetch always runs in background via _maybe_start_bg_refresh().
    """
    import pandas as pd
    from analysis.ingest import CACHE_DIR, load_csv_data, _cache_is_fresh, _cache_exists, _load_cache

    os.environ["PROGRAM_ID"] = program_id

    pid_int = int(program_id)

    # 1. Fresh per-customer cache — instant
    if _cache_is_fresh(pid_int):
        try:
            pdf, fdf, fsteps, smap = _load_cache(pid_int)
            return pdf, fdf, smap, "cache"
        except Exception:
            pass

    # 2. Stale per-customer cache — still useful, bg refresh will update
    if _cache_exists(pid_int):
        try:
            pdf, fdf, fsteps, smap = _load_cache(pid_int)
            return pdf, fdf, smap, "stale_cache"
        except Exception:
            pass

    # 3. CSV fallback (IDFC only)
    if program_id == "19905":
        try:
            pdf, fdf, fsteps, smap = load_csv_data()
            return pdf, fdf, smap, "csv"
        except Exception:
            pass

    # 4. No data yet — background fetch needed
    empty = pd.DataFrame()
    return empty, empty, {}, "loading"



def get_data_or_stop():
    """
    Load Splunk data for the active customer.
    If data isn't ready yet (first fetch for this customer), shows a loading
    message and stops rendering — never passes an empty DataFrame to pages.
    """
    _pid = _active_customer["program_id"] or "19905"
    pdf, fdf, smap, src = load_splunk_data(_pid)
    if pdf.empty and src == "loading":
        _maybe_start_bg_refresh(force=True)
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px;'
            f'background:#F5F8FF;border:1px solid #C0D2FA;border-radius:6px;'
            f'padding:14px 18px;font-size:13px;color:#1473E6;margin-top:20px">'
            f'<span style="font-size:1.2rem">⟳</span>'
            f'&nbsp;<div><strong>Fetching data for {_active_customer["short"]} from Splunk...</strong><br>'
            f'<span style="font-size:12px;opacity:0.8">This takes ~30 seconds on first load.</span></div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if st.button("↺ Check again", key="_loading_check_again", type="primary"):
            try:
                load_splunk_data.clear()
            except Exception:
                pass
            st.rerun()
        # Auto-rerun every 5 seconds while waiting
        import time as _t_load
        _t_load.sleep(5)
        try:
            load_splunk_data.clear()
        except Exception:
            pass
        st.rerun()

    # Auto-enrich pending predictions with actual outcomes from Splunk
    # Pass program_id so we only resolve predictions for the active customer.
    try:
        from analysis.prediction_store import enrich_from_splunk
        enrich_from_splunk(pdf, fdf, program_id=str(_pid))
    except Exception:
        pass

    return pdf, fdf, smap, src


# ── Sidebar ───────────────────────────────────────────────────────────────────
_PAGE_ICONS = {
    "Argus Home":            ":material/home:",
    "Overview":              ":material/dashboard:",
    "Risk Assessment":       ":material/security:",
    "Post-Failure Diagnosis": ":material/my_location:",
    "Failure Analysis":      ":material/report_problem:",
    "Memory Search":         ":material/search:",
    "Memory Explorer":       ":material/travel_explore:",
    "Repo Settings":         ":material/settings:",
}
_PAGES = [
    page_name
    for page_name in _PAGE_ICONS
    if page_name not in {"Argus Home", "Memory Search", "Static Analysis"}
]

_PAGE_ROUTES = {
    "argus_home": "Argus Home",
    "overview": "Overview",
    "failure_analysis": "Failure Analysis",
    "risk_assessment": "Risk Assessment",
    "failure_pinpoint": "Post-Failure Diagnosis",
    "memory_search": "Memory Search",
    "memory_explorer": "Memory Explorer",
    "static_analysis": "Static Analysis",
    "repository_settings": "Repo Settings",
}
_ROUTE_FOR_PAGE = {page_name: route for route, page_name in _PAGE_ROUTES.items()}


def _navigate_to(page_name: str) -> None:
    """Persist a page selection in both session state and the shareable URL."""
    st.session_state["page"] = page_name
    route = _ROUTE_FOR_PAGE[page_name]
    if st.query_params.get("page") != route:
        st.query_params["page"] = route


_requested_route = st.query_params.get("page")
if _requested_route in _PAGE_ROUTES:
    st.session_state["page"] = _PAGE_ROUTES[_requested_route]
elif _requested_route is not None:
    _navigate_to(_PAGE_ROUTES["argus_home"])
elif "page" not in st.session_state:
    st.session_state["page"] = _PAGE_ROUTES["argus_home"]
if st.session_state.get("page") == "Failure Pinpoint":
    st.session_state["page"] = "Post-Failure Diagnosis"
if "sb_open" not in st.session_state:
    st.session_state["sb_open"] = True


def _render_top_navigation_bar() -> None:
    """Render the Fixed Top Navigation Bar with customer selection and account menu."""
    import json
    import streamlit.components.v1 as components

    # Serialize customer data
    active_customer = st.session_state.get("selected_customer", list(_CUSTOMERS.keys())[0])
    customers_list = list(_CUSTOMERS.keys())
    theme_name = st.session_state.get("ui_theme", "light")
    theme_colors = _THEMES[theme_name]
    _cache_tooltip = "Refresh data"
    _cache_col = theme_colors["green"]

    style_content = f"""
    .custom-top-bar {{
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        width: 100%;
        height: {_TOPBAR_HEIGHT_CSS};
        background-color: {theme_colors["surface2"]} !important;
        border: none !important;
        color: {theme_colors["text"]} !important;
        z-index: 99999;
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0 16px;
        box-sizing: border-box;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        box-shadow: none;
    }}

    .top-bar-left, .top-bar-right {{
        display: flex;
        align-items: center;
        gap: 20px;
    }}

    .hamburger-btn {{
        background: none;
        border: none;
        cursor: pointer;
        width: 36px;
        height: 36px;
        padding: 8px;
        border-radius: 999px;
        color: {theme_colors["text_muted"]} !important;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: background-color 0.15s, color 0.15s;
    }}

    .hamburger-btn:hover {{
        background-color: {theme_colors["border2"]} !important;
        color: {theme_colors["text"]} !important;
    }}

    .hamburger-btn svg {{
        width: 20px;
        height: 20px;
        fill: currentColor;
    }}

    .logo-container {{
        display: flex;
        align-items: center;
        gap: 8px;
        cursor: pointer;
        user-select: none;
        text-decoration: none;
    }}

    .app-logo {{
        width: 24px;
        height: 24px;
        color: {theme_colors["blue"]} !important;
    }}

    .app-name {{
        font-size: 16px;
        font-weight: 700;
        letter-spacing: -0.2px;
        color: {theme_colors["text"]} !important;
    }}

    .customer-selector-container {{
        position: relative;
    }}

    .customer-selector-btn {{
        background: transparent;
        border: none;
        cursor: pointer;
        padding: 6px 12px;
        border-radius: 6px;
        color: {theme_colors["text_sub"]} !important;
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 14px;
        font-weight: 500;
        transition: background-color 200ms ease, color 200ms ease;
    }}

    .customer-selector-btn:hover {{
        background-color: {theme_colors["border2"]} !important;
        color: {theme_colors["text"]} !important;
    }}

    .customer-selector-btn:focus {{
        outline: none;
    }}

    .selected-customer-name {{
        max-width: 150px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }}

    .chevron-icon {{
        width: 14px;
        height: 14px;
        fill: currentColor;
        transition: transform 0.15s ease;
    }}

    .customer-selector-container.open .chevron-icon {{
        transform: rotate(180deg);
    }}

    .customer-dropdown-menu {{
        position: absolute;
        top: calc(100% + 6px);
        right: 0;
        width: 260px;
        background-color: {theme_colors["surface"]} !important;
        border: 1px solid {theme_colors["border"]} !important;
        border-radius: 8px;
        box-shadow: {theme_colors["shadow"]} !important;
        z-index: 1000000;
        display: flex;
        flex-direction: column;
        overflow: hidden;
        transform-origin: top right;
        transition: opacity 0.15s ease, transform 0.15s ease;
        opacity: 0;
        transform: scale(0.95);
        pointer-events: none;
    }}

    .customer-dropdown-menu.show {{
        opacity: 1;
        transform: scale(1);
        pointer-events: auto;
    }}

    .search-container {{
        display: flex;
        align-items: center;
        padding: 8px 12px;
        border-bottom: 1px solid {theme_colors["border2"]} !important;
        gap: 8px;
    }}

    .search-icon {{
        width: 16px;
        height: 16px;
        fill: {theme_colors["text_muted"]} !important;
        flex-shrink: 0;
    }}

    .customer-search-input {{
        border: none;
        outline: none;
        font-size: 13px;
        width: 100%;
        background: transparent;
        color: {theme_colors["text"]} !important;
    }}

    .customer-search-input::placeholder {{
        color: {theme_colors["text_muted"]} !important;
    }}

    .customer-list-container {{
        max-height: 240px;
        overflow-y: auto;
        padding: 4px 0;
    }}

    .customer-list-container::-webkit-scrollbar {{
        width: 6px;
    }}

    .customer-list-container::-webkit-scrollbar-track {{
        background: transparent;
    }}

    .customer-list-container::-webkit-scrollbar-thumb {{
        background-color: {theme_colors["border"]} !important;
        border-radius: 3px;
    }}

    .customer-item {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 16px;
        font-size: 13px;
        font-weight: 400;
        color: {theme_colors["text_sub"]} !important;
        cursor: pointer;
        transition: background-color 150ms ease, color 150ms ease;
        user-select: none;
    }}

    .customer-item:hover, .customer-item.focused {{
        background-color: {theme_colors["border2"]} !important;
        color: {theme_colors["text"]} !important;
    }}

    .customer-item.active {{
        font-weight: 600;
        color: {theme_colors["blue"]} !important;
        background-color: {theme_colors["border2"]} !important;
    }}

    .checkmark-icon {{
        width: 14px;
        height: 14px;
        fill: currentColor;
        opacity: 0;
    }}

    .customer-item.active .checkmark-icon {{
        opacity: 1;
    }}

    .icon-nav-btn {{
        background: {theme_colors["surface2"]};
        border: none;
        cursor: pointer;
        padding: 8px;
        width: 36px;
        height: 36px;
        border-radius: 999px;
        color: {theme_colors["text_muted"]} !important;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: background-color 0.15s, color 0.15s;
    }}

    .icon-nav-btn:hover {{
        background-color: {theme_colors["border"]} !important;
        color: {theme_colors["text"]} !important;
    }}

    .icon-nav-btn svg {{
        width: 20px;
        height: 20px;
        fill: currentColor;
    }}

    .user-profile-avatar {{
        width: 32px;
        height: 32px;
        border-radius: 50%;
        background-color: {theme_colors["blue"]} !important;
        color: #ffffff !important;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
        transition: transform 0.15s;
        user-select: none;
        position: relative;
    }}

    .user-profile-avatar:hover {{
        transform: scale(1.05);
    }}

    /* Account Dropdown */
    .account-dropdown-menu {{
        position: absolute;
        top: calc(100% + 6px);
        right: 0;
        width: 200px;
        background-color: {theme_colors["surface"]} !important;
        border: 1px solid {theme_colors["border"]} !important;
        border-radius: 8px;
        box-shadow: {theme_colors["shadow"]} !important;
        z-index: 1000000;
        display: flex;
        flex-direction: column;
        overflow: hidden;
        transform-origin: top right;
        transition: opacity 0.15s ease, transform 0.15s ease;
        opacity: 0;
        transform: scale(0.95);
        pointer-events: none;
        font-weight: normal;
        color: {theme_colors["text"]} !important;
    }}

    .user-profile-avatar.open .account-dropdown-menu {{
        opacity: 1;
        transform: scale(1);
        pointer-events: auto;
    }}

    .account-info {{
        padding: 12px 16px;
        border-bottom: 1px solid {theme_colors["border2"]} !important;
        font-size: 12px;
        text-align: left;
    }}

    .account-email {{
        color: {theme_colors["text_muted"]} !important;
        font-size: 11px;
        margin-top: 2px;
    }}

    .account-item {{
        padding: 8px 16px;
        font-size: 12px;
        color: {theme_colors["text_sub"]} !important;
        cursor: pointer;
        transition: background-color 150ms ease, color 150ms ease;
        text-align: left;
    }}

    .account-item:hover {{
        background-color: {theme_colors["border2"]} !important;
        color: {theme_colors["text"]} !important;
    }}

    @keyframes tb-spin {{
        from {{ transform: rotate(0deg); }}
        to   {{ transform: rotate(360deg); }}
    }}
    .tb-spinning svg {{
        animation: tb-spin 0.8s linear infinite;
    }}
    """

    html_content = f"""
    <div id="top-bar-left-section" class="top-bar-left">
        <button class="hamburger-btn" id="top-bar-hamburger" title="Toggle Sidebar">
            <svg viewBox="0 0 24 24"><path d="M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z"/></svg>
        </button>
        <a class="logo-container" id="top-bar-logo" href="?page=argus_home">
            <svg class="app-logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
            </svg>
            <span class="app-name">Argus</span>
        </a>
    </div>
    <div id="top-bar-right-section" class="top-bar-right">
        <div class="customer-selector-container" id="customer-selector-container">
            <button class="customer-selector-btn" id="customer-selector-trigger">
                <span class="selected-customer-name">{active_customer}</span>
                <svg class="chevron-icon" viewBox="0 0 24 24"><path d="M7 10l5 5 5-5H7z"/></svg>
            </button>
            <div class="customer-dropdown-menu" id="customer-dropdown">
                <div class="search-container">
                    <svg class="search-icon" viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
                    <input type="text" class="customer-search-input" id="customer-search-input" placeholder="Search customers..." autocomplete="off">
                </div>
                <div class="customer-list-container" id="customer-list-container"></div>
            </div>
        </div>
        <button class="icon-nav-btn" id="topbar-refresh-btn"
                title="{_cache_tooltip}" style="position:relative">
            <span style="position:absolute;top:6px;right:6px;width:6px;height:6px;border-radius:50%;background:{_cache_col}"></span>
            <svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.958 7.958 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
        </button>
    </div>
    """

    js_content = f"""
    <script>
    (function() {{
        const parentDoc = window.parent.document;
        
        // 1. Inject or update CSS
        let styleEl = parentDoc.getElementById('custom-top-bar-styles');
        if (!styleEl) {{
            styleEl = parentDoc.createElement('style');
            styleEl.id = 'custom-top-bar-styles';
            parentDoc.head.appendChild(styleEl);
        }}
        styleEl.textContent = `{style_content}`;
        
        // 2. Inject or update the top bar element
        let topBar = parentDoc.getElementById('custom-top-navigation-bar');
        if (!topBar) {{
            topBar = parentDoc.createElement('div');
            topBar.id = 'custom-top-navigation-bar';
            
            const appView = parentDoc.querySelector('[data-testid="stAppViewContainer"]');
            if (appView) {{
                appView.insertBefore(topBar, appView.firstChild);
            }} else {{
                parentDoc.body.insertBefore(topBar, parentDoc.body.firstChild);
            }}
        }}
        topBar.className = 'custom-top-bar';
        topBar.innerHTML = `{html_content}`;
        
        // Data variables
        const customers = {json.dumps(customers_list)};
        const activeCustomer = {json.dumps(active_customer)};
        
        let focusedIndex = -1;
        
        // Helper to close dropdowns
        function closeDropdown() {{
            const dropdown = parentDoc.getElementById('customer-dropdown');
            const container = parentDoc.getElementById('customer-selector-container');
            if (dropdown && container) {{
                dropdown.classList.remove('show');
                container.classList.remove('open');
            }}
            focusedIndex = -1;
        }}
        
        function closeAccountDropdown() {{
            const avatar = parentDoc.getElementById('user-avatar-container');
            if (avatar) {{
                avatar.classList.remove('open');
            }}
        }}
        
        // Select customer function
        // Use a real parent-page navigation so Streamlit processes the updated
        // query param on rerun. history.replaceState only changes the address
        // bar and leaves the active customer/session context unchanged.
        function selectCustomer(customerName) {{
            closeDropdown();
            if (customerName === activeCustomer) return;

            try {{
                const url = new URL(window.parent.location.href);
                url.searchParams.set('customer', customerName);
                window.parent.location.assign(url.href);
            }} catch(e) {{
                try {{
                    const url = new URL(window.parent.location.href);
                    url.searchParams.set('customer', customerName);
                    window.parent.location.href = url.href;
                }} catch(e2) {{}}
            }}
        }}
        
        // Render customer list helper
        function renderCustomerList(filterText = '') {{
            const listContainer = parentDoc.getElementById('customer-list-container');
            if (!listContainer) return;
            
            listContainer.innerHTML = '';
            const filtered = customers.filter(c => c.toLowerCase().includes(filterText.toLowerCase()));
            
            if (filtered.length === 0) {{
                const noResult = parentDoc.createElement('div');
                noResult.className = 'customer-item';
                noResult.style.color = '#888';
                noResult.style.cursor = 'default';
                noResult.style.backgroundColor = 'transparent';
                noResult.textContent = 'No customers found';
                listContainer.appendChild(noResult);
                return;
            }}
            
            filtered.forEach((customer, index) => {{
                const item = parentDoc.createElement('a');
                item.className = 'customer-item';
                item.href = (() => {{
                    try {{
                        const url = new URL(window.parent.location.href);
                        url.searchParams.set('customer', customer);
                        return url.href;
                    }} catch(e) {{
                        return '?customer=' + encodeURIComponent(customer);
                    }}
                }})();
                if (customer === activeCustomer) {{
                    item.classList.add('active');
                }}
                item.dataset.index = index;
                item.dataset.value = customer;
                item.style.textDecoration = 'none';

                // Use textContent — avoids Python f-string vs JS template-literal conflict
                const nameSpan = parentDoc.createElement('span');
                nameSpan.textContent = customer;
                item.appendChild(nameSpan);
                if (customer === activeCustomer) {{
                    const tick = parentDoc.createElement('span');
                    tick.textContent = '✓';
                    tick.style.cssText = 'color:#1473E6;margin-left:auto;font-size:12px';
                    item.appendChild(tick);
                }}

                listContainer.appendChild(item);
            }});

            // ── Add new customer ──
            const divider = parentDoc.createElement('div');
            divider.style.cssText = 'height:1px;background:rgba(0,0,0,0.1);margin:4px 0';
            listContainer.appendChild(divider);

            const addItem = parentDoc.createElement('div');
            addItem.className = 'customer-item';
            addItem.style.cssText = 'color: #000000; font-weight:500; gap:6px; display:flex; align-items:center';
            addItem.textContent = '+ Add new customer';
            addItem.addEventListener('click', () => {{
                closeDropdown();
                const allBtns = parentDoc.querySelectorAll('button');
                let found = false;
                for (const btn of allBtns) {{
                    const t = (btn.innerText || btn.textContent || '').replace(/\s+/g,' ').trim();
                    if (t.includes('Repo Settings')) {{ btn.click(); found = true; break; }}
                }}
                if (!found) {{
                    try {{
                        const url = new URL(window.parent.location.href);
                        url.searchParams.set('page', 'repository_settings');
                        window.parent.location.href = url.href;
                    }} catch(e) {{}}
                }}
            }});
            listContainer.appendChild(addItem);
        }}
        
        // 3. Set up event listeners
        const trigger = parentDoc.getElementById('customer-selector-trigger');
        const dropdown = parentDoc.getElementById('customer-dropdown');
        const container = parentDoc.getElementById('customer-selector-container');
        const searchInput = parentDoc.getElementById('customer-search-input');
        const listContainer = parentDoc.getElementById('customer-list-container');

        if (listContainer) {{
            listContainer.onclick = (e) => {{
                const item = e.target.closest('.customer-item');
                if (!item || !listContainer.contains(item)) return;
                if (item.tagName === 'A') return;
                const customerName = item.dataset.value;
                if (customerName) {{
                    e.preventDefault();
                    e.stopPropagation();
                    selectCustomer(customerName);
                }}
            }};
        }}
        
        if (trigger && dropdown && container) {{
            trigger.onclick = (e) => {{
                e.stopPropagation();
                closeAccountDropdown();
                const isOpen = dropdown.classList.contains('show');
                if (isOpen) {{
                    closeDropdown();
                }} else {{
                    dropdown.classList.add('show');
                    container.classList.add('open');
                    renderCustomerList('');
                    if (searchInput) {{
                        searchInput.value = '';
                        setTimeout(() => searchInput.focus(), 50);
                    }}
                }}
            }};
        }}
        
        if (searchInput) {{
            searchInput.onclick = (e) => e.stopPropagation();
            searchInput.oninput = (e) => {{
                renderCustomerList(e.target.value);
                focusedIndex = -1;
            }};
            
            searchInput.onkeydown = (e) => {{
                const items = Array.from(parentDoc.querySelectorAll('.customer-item:not([style*="cursor: default"])'));
                if (items.length === 0) return;
                
                if (e.key === 'ArrowDown') {{
                    e.preventDefault();
                    focusedIndex = (focusedIndex + 1) % items.length;
                    updateFocus(items);
                }} else if (e.key === 'ArrowUp') {{
                    e.preventDefault();
                    focusedIndex = (focusedIndex - 1 + items.length) % items.length;
                    updateFocus(items);
                }} else if (e.key === 'Enter') {{
                    e.preventDefault();
                    if (focusedIndex >= 0 && focusedIndex < items.length) {{
                        items[focusedIndex].click();
                    }} else if (items.length > 0) {{
                        items[0].click();
                    }}
                }} else if (e.key === 'Escape') {{
                    e.preventDefault();
                    closeDropdown();
                }}
            }};
        }}
        
        function updateFocus(items) {{
            items.forEach((item, index) => {{
                if (index === focusedIndex) {{
                    item.classList.add('focused');
                    item.scrollIntoView({{ block: 'nearest' }});
                }} else {{
                    item.classList.remove('focused');
                }}
            }});
        }}

        function findSidebarStateToggle() {{
            const bridgeLabels = new Set(['ARGUS_SIDEBAR_TOGGLE_BRIDGE', '__toggle_sidebar__', 'toggle_sidebar']);
            const xpath = "//*[normalize-space(text())='ARGUS_SIDEBAR_TOGGLE_BRIDGE' or normalize-space(text())='__toggle_sidebar__' or normalize-space(text())='toggle_sidebar']";
            const element = parentDoc.evaluate(xpath, parentDoc, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            if (element) {{
                return element.closest('button');
            }}
            const buttons = parentDoc.querySelectorAll('button');
            for (const btn of buttons) {{
                const label = (btn.innerText || btn.textContent || '').replace(/\s+/g, ' ').trim();
                if (bridgeLabels.has(label)) {{
                    return btn;
                }}
            }}
            return null;
        }}

        function clickSidebarStateToggle(btn) {{
            try {{
                btn.dispatchEvent(new MouseEvent('pointerdown', {{ bubbles: true, cancelable: true, view: window.parent }}));
                btn.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true, cancelable: true, view: window.parent }}));
                btn.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true, cancelable: true, view: window.parent }}));
            }} catch(e) {{}}
            btn.click();
        }}
        
        // Hamburger toggle
        const hamburgerBtn = parentDoc.getElementById('top-bar-hamburger');
        if (hamburgerBtn) {{
            hamburgerBtn.onclick = (e) => {{
                e.preventDefault();
                e.stopPropagation();
                closeDropdown();
                closeAccountDropdown();

                const stateToggle = findSidebarStateToggle();
                if (stateToggle) {{
                    clickSidebarStateToggle(stateToggle);
                    return;
                }}

                const nativeToggle = parentDoc.querySelector('button[title="Toggle sidebar"]');
                if (nativeToggle) {{
                    nativeToggle.click();
                }}
            }};
        }}

        // Use a native link for home navigation, preserving the customer query param.
        const topbarLogo = parentDoc.getElementById('top-bar-logo');
        if (topbarLogo) {{
            try {{
                const url = new URL(window.parent.location.href);
                url.searchParams.set('page', 'argus_home');
                topbarLogo.href = url.href;
            }} catch(e) {{}}
        }}
        
        // ── Topbar refresh — click the existing sidebar button directly ──
        function clickSidebarBtn(labelTexts) {{
            const allBtns = parentDoc.querySelectorAll('button');
            for (const btn of allBtns) {{
                const t = (btn.innerText || btn.textContent || '').replace(/\s+/g, ' ').trim();
                if (labelTexts.some(l => t === l || t.includes(l))) {{
                    clickSidebarStateToggle(btn);
                    return true;
                }}
            }}
            return false;
        }}

        const topbarRefreshBtn = parentDoc.getElementById('topbar-refresh-btn');
        if (topbarRefreshBtn) {{
            topbarRefreshBtn.onclick = (e) => {{
                e.preventDefault();
                e.stopPropagation();
                closeDropdown();
                // Find refresh button via its marker span — works open or collapsed
                const marker = parentDoc.getElementById('sidebar-refresh-marker');
                if (marker) {{
                    const container = marker.closest('[data-testid="element-container"]')
                                   || marker.parentElement;
                    const prevContainer = container && container.previousElementSibling;
                    const btn = prevContainer && prevContainer.querySelector('button');
                    if (btn) {{ btn.click(); return; }}
                }}
                // Fallback: search by help text title
                const allBtns = parentDoc.querySelectorAll('button[title="Refresh data"]');
                if (allBtns.length > 0) {{ allBtns[0].click(); return; }}
                // Last fallback: search by text
                clickSidebarBtn(['Refresh']);
            }};
        }}

        // Account avatar dropdown toggle
        const avatar = parentDoc.getElementById('user-avatar-container');
        if (avatar) {{
            avatar.onclick = (e) => {{
                e.stopPropagation();
                closeDropdown();
                avatar.classList.toggle('open');
            }};
        }}
        
        // Close dropdowns on clicking outside
        if (parentDoc.body && !parentDoc.body.dataset.topBarBound) {{
            parentDoc.body.dataset.topBarBound = "true";
            parentDoc.addEventListener('click', (e) => {{
                const dropdown = parentDoc.getElementById('customer-dropdown');
                const trigger = parentDoc.getElementById('customer-selector-trigger');
                if (dropdown && trigger && !dropdown.contains(e.target) && !trigger.contains(e.target)) {{
                    closeDropdown();
                }}
                
                const avatar = parentDoc.getElementById('user-avatar-container');
                if (avatar && !avatar.contains(e.target)) {{
                    closeAccountDropdown();
                }}
            }});
        }}
        
        // 4. Hide hidden sidebar button
        function hideHiddenToggleButton() {{
            const btn = findSidebarStateToggle();
            if (btn) {{
                btn.setAttribute('aria-hidden', 'true');
                btn.tabIndex = -1;
                btn.style.setProperty('display', 'block', 'important');
                btn.style.setProperty('width', '1px', 'important');
                btn.style.setProperty('height', '1px', 'important');
                btn.style.setProperty('min-height', '1px', 'important');
                btn.style.setProperty('padding', '0', 'important');
                btn.style.setProperty('opacity', '0', 'important');
                const container = btn.closest('.element-container') || btn.closest('[data-testid="element-container"]');
                if (container) {{
                    container.style.setProperty('position', 'absolute', 'important');
                    container.style.setProperty('width', '1px', 'important');
                    container.style.setProperty('height', '1px', 'important');
                    container.style.setProperty('min-height', '1px', 'important');
                    container.style.setProperty('margin', '0', 'important');
                    container.style.setProperty('padding', '0', 'important');
                    container.style.setProperty('overflow', 'hidden', 'important');
                    container.style.setProperty('opacity', '0', 'important');
                    container.style.setProperty('pointer-events', 'auto', 'important');
                    container.style.setProperty('transform', 'translateX(-9999px)', 'important');
                }}
            }}
        }}
        hideHiddenToggleButton();
        setTimeout(hideHiddenToggleButton, 200);
        setTimeout(hideHiddenToggleButton, 500);
        setTimeout(hideHiddenToggleButton, 1000);
    }})();
    </script>
    """
    components.html(js_content, height=0)


def _render_customer_context() -> None:
    """Render the Risk Assessment top bar."""
    st.markdown(
        '<div style="margin-bottom:1rem">'
        '<p class="pg-title">Risk Assessment</p>'
        '<p class="pg-sub">Should this Dev-pipeline result be promoted to Production?</p>'
        '</div>',
        unsafe_allow_html=True,
    )


def _toggle_sidebar_state() -> None:
    st.session_state["sb_open"] = not st.session_state.get("sb_open", True)


@st.fragment
def _render_sidebar():
    _open = st.session_state.get("sb_open", True)
    _page = st.session_state.get("page", "Overview")
    _w    = "208px" if _open else "56px"
    _collapsed_css = """
[data-testid="stSidebar"] .stButton > button,
[data-testid="stSidebar"] button[kind="secondary"],
[data-testid="stSidebar"] button[kind="primary"],
[data-testid="stSidebar"] button:not([title="Toggle sidebar"]):not([kind="headerNoPadding"]),
[data-testid="stSidebar"][data-testid="stSidebar"] button[data-testid^="stBaseButton"]:not([kind="headerNoPadding"]) {
    justify-content: center !important;
    text-align: center !important;
    gap: 0 !important;
    padding: 0 !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
    border-left-width: 0 !important;
}
[data-testid="stSidebar"] button:not([kind="headerNoPadding"]) > span,
[data-testid="stSidebar"][data-testid="stSidebar"] button[data-testid^="stBaseButton"]:not([kind="headerNoPadding"]) > span {
    width: 100% !important;
    display: flex !important;
    justify-content: center !important;
    align-items: center !important;
}
[data-testid="stSidebar"] .stButton > button [data-testid="stIconMaterial"],
[data-testid="stSidebar"] button:not([kind="headerNoPadding"]) [data-testid="stIconMaterial"],
[data-testid="stSidebar"][data-testid="stSidebar"] button[data-testid^="stBaseButton"]:not([kind="headerNoPadding"]) [data-testid="stIconMaterial"] {
    margin: 0 auto !important;
    font-size: 1.35rem !important;
    width: 1.35rem !important;
    height: 1.35rem !important;
    line-height: 1 !important;
    text-align: center !important;
    transform: translateX(4px) !important;
}
""" if not _open else ""

    st.markdown(f"""
<style>
[data-testid="stSidebar"] {{
    min-width: {_w} !important;
    max-width: {_w} !important;
    width:     {_w} !important;
}}
[data-testid="stSidebar"] button[title="Toggle sidebar"] {{
    display: none !important;
}}
[data-testid="stSidebar"] > div:first-child,
[data-testid="stSidebar"] > div:first-child > div,
[data-testid="stSidebar"] > div:first-child > div > div {{
    padding-top: 0 !important;
    margin-top: 0 !important;
}}
[data-testid="stSidebar"] .stButton > button,
[data-testid="stSidebar"] button[kind="secondary"],
[data-testid="stSidebar"] button[kind="primary"] {{
    margin-block: 0 !important;
}}
[data-testid="stSidebar"] [data-testid="element-container"]:has(#sidebar-toggle-bridge-marker),
[data-testid="stSidebar"] .element-container:has(#sidebar-toggle-bridge-marker),
[data-testid="stSidebar"] [data-testid="element-container"]:has(#sidebar-toggle-bridge-marker) + [data-testid="element-container"],
[data-testid="stSidebar"] .element-container:has(#sidebar-toggle-bridge-marker) + .element-container {{
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    min-height: 1px !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    opacity: 0 !important;
    pointer-events: auto !important;
    transform: translateX(-9999px) !important;
}}
[data-testid="stSidebar"] [data-testid="element-container"]:has(#sidebar-toggle-bridge-marker) button,
[data-testid="stSidebar"] .element-container:has(#sidebar-toggle-bridge-marker) button,
[data-testid="stSidebar"] [data-testid="element-container"]:has(#sidebar-toggle-bridge-marker) + [data-testid="element-container"] button,
[data-testid="stSidebar"] .element-container:has(#sidebar-toggle-bridge-marker) + .element-container button {{
    display: block !important;
    width: 1px !important;
    height: 1px !important;
    min-height: 1px !important;
    padding: 0 !important;
    opacity: 0 !important;
}}
{_collapsed_css}
</style>
<span id="sidebar-toggle-bridge-marker"></span>
""", unsafe_allow_html=True)

    st.button(
        "ARGUS_SIDEBAR_TOGGLE_BRIDGE",
        key="_topbar_sidebar_toggle",
        help="Toggle sidebar",
        on_click=_toggle_sidebar_state,
    )

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
            _navigate_to(_p)
            st.rerun()

    # ── Theme toggle ─────────────────────────────────────────────────────────
    _theme_label = "Dark mode" if st.session_state["ui_theme"] == "light" else "Light mode"
    if st.button(_theme_label, key="_theme_switch",
                 help=f"Switch to {_theme_label.lower()}",
                 icon=":material/dark_mode:" if st.session_state["ui_theme"] == "light" else ":material/light_mode:",
                 use_container_width=True):
        st.session_state["ui_theme"] = "dark" if st.session_state["ui_theme"] == "light" else "light"
        st.rerun()

    # ── Cache status + refresh ────────────────────────────────────────────────
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
                f'<p class="sidebar-cache-status" style="padding:0 0.75rem;margin:0 0 0.25rem;white-space:nowrap;overflow:hidden">'
                f'<span style="display:inline-block;width:0.375rem;height:0.375rem;border-radius:9999px;'
                f'background:{_sc};margin-right:0.25rem;vertical-align:middle"></span>{_st2}</p>',
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
        # Hidden marker so topbar JS can find refresh button regardless of open/collapsed state
        st.markdown('<span id="sidebar-refresh-marker"></span>', unsafe_allow_html=True)
    except Exception:
        pass


_render_top_navigation_bar()

with st.sidebar:
    _render_sidebar()


page = st.session_state.get("page", _PAGE_ROUTES["argus_home"])

# ═══════════════════════════════════════════════════════════
# PAGE: ARGUS HOME
# ═══════════════════════════════════════════════════════════
if page == "Argus Home":
    render_argus_home()

# ═══════════════════════════════════════════════════════════
# PAGE: OVERVIEW
# ═══════════════════════════════════════════════════════════
elif page == "Overview":
    section_header("Pipeline Health Overview", "Last 30 days &middot; Live from Splunk")

    # Kick off background Splunk refresh (no-op if already fresh or running)
    _maybe_start_bg_refresh()

    # Show background fetch status banner
    if _splunk_refresh_state["running"]:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:6px;'
            f'background:rgba(37,99,235,0.035);border:1px solid rgba(37,99,235,0.14);border-radius:6px;'
            f'padding:5px 10px;margin-bottom:14px;font-size:11px;color:{T["text_sub"]}">'
            f'<span style="color:{T["blue"]}">⟳</span>'
            f'<strong style="font-weight:600">Fetching fresh data from Splunk in background</strong> — '
            f'showing cached data. Click Refresh Data in sidebar when done.'
            f'</div>',
            unsafe_allow_html=True,
        )
    elif _splunk_refresh_state["done"]:
        # Fresh data arrived — clear cache and reload once
        st.cache_resource.clear()
        _splunk_refresh_state["done"] = False
        st.rerun()
    elif _splunk_refresh_state["error"]:
        st.markdown(
            f'<div style="font-size:11px;color:#E79D13;margin-bottom:6px">'
            f'⚠ Background Splunk fetch failed: {_splunk_refresh_state["error"]}'
            f'</div>',
            unsafe_allow_html=True,
        )
        _splunk_refresh_state["error"] = None

    # Load instantly — never blocks
    try:
        pipeline_df, failed_df, share_map, _data_source = get_data_or_stop()
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
            f'<div style="background:rgba(37,99,235,0.035);border:1px solid rgba(37,99,235,0.14);'
            f'border-radius:6px;padding:5px 10px;margin-bottom:14px;'
            f'display:flex;align-items:center;gap:6px">'
            f'<span style="font-size:11px;color:{T["text_sub"]}">'
            f'<b style="font-weight:600;color:{T["text"]}">Stale cache</b> — Splunk API was unreachable. Showing cached data. '
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
    _overview_kpis = (
        ("Total Executions", total, T["text"], "activity"),
        ("Completed", finished, T["green"], "check"),
        ("Failed", failed_n, T["red"], "warning"),
        ("Cancelled", cancelled, T["text_muted"], "close"),
        ("Completion Rate", f"{rate}%", T["blue"], "analytics"),
    )
    _kpi_cards = "".join(
        f'<div style="min-height:112px;padding:24px;background:{"#000000" if _IS_DARK else "#FFFFFF"};'
        f'border:1px solid {T["border"]};border-radius:14px;box-shadow:0 1px 2px rgba(15,23,42,.04);'
        f'display:flex;flex-direction:column;justify-content:space-between">'
        f'<div style="display:flex;align-items:center;justify-content:space-between;gap:8px">'
        f'<p style="margin:0;font-size:0.72rem;font-weight:500;color:{T["text"] if _IS_DARK else T["text_muted"]};'
        f'letter-spacing:0.01em">{label}</p>{overview_kpi_icon(icon, color)}</div>'
        f'<p style="margin:14px 0 0;font-size:2rem;line-height:1;font-weight:750;'
        f'letter-spacing:-0.035em;color:{color}">{value}</p>'
        f'</div>'
        for label, value, color, icon in _overview_kpis
    )
    st.markdown(
        f'<div style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr));'
        f'gap:16px;margin:8px 0 24px">{_kpi_cards}</div>',
        unsafe_allow_html=True,
    )

    # ── Charts row ──
    left, right = st.columns(2, gap="medium")

    with left:
        with content_card(overview=True):
            overview_section_title("Failures by Pipeline Step")

            if not failed_df.empty and "firstFailedStep" in failed_df.columns:
                step_df = failed_df["firstFailedStep"].value_counts().reset_index()
                step_df.columns = ["Step", "Count"]
                max_count = step_df["Count"].max()

                for _, row in step_df.head(6).iterrows():
                    stat_bar(row["Step"], row["Count"], max_count, T["red"])
            else:
                st.info("No failed step data.")

    with right:
        with content_card(overview=True):
            overview_section_title("Status Distribution")
            status_counts = pipeline_df["Status"].value_counts()
            _status_colors = {
                "FINISHED": T["green"],
                "FAILED": T["red"],
                "ERROR": T["red"],
                "CANCELLED": T["gray"],
                "RUNNING": T["blue"],
            }
            fig_pie = go.Figure(go.Pie(
                labels=status_counts.index,
                values=status_counts.values,
                hole=0.72,
                marker=dict(
                    colors=[_status_colors.get(status, T["gray"]) for status in status_counts.index],
                    line=dict(color=T["surface"], width=2),
                ),
                textinfo="none",
                hovertemplate="<b>%{label}</b><br>%{value} executions<br>%{percent}<extra></extra>",
            ))
            theme = chart_theme(205)
            theme.update(
                margin=dict(t=0, b=0, l=0, r=0),
                annotations=[dict(
                    text=f'<b style="font-size:18px">{rate}%</b><br>'
                         f'<span style="font-size:10px;color:{T["text_muted"]}">Complete</span>',
                    x=0.5, y=0.5, font_size=14, showarrow=False,
                    font=dict(color=T["text"]),
                )]
            )
            fig_pie.update_layout(**theme)
            legend_items = list(zip(
                status_counts.index,
                [_status_colors.get(status, T["gray"]) for status in status_counts.index],
                status_counts.values,
            ))
            legend_html = "".join(
                f'<span style="display:inline-flex;align-items:center;gap:5px;'
                f'margin:0 0 9px;font-size:0.72rem;color:{T["text_muted"]}">'
                f'<span style="width:6px;height:6px;border-radius:999px;'
                f'background:{c};flex-shrink:0"></span>{l}'
                f'<strong style="margin-left:auto;color:{T["text"]};font-weight:600">{v}</strong></span>'
                for l, c, v in legend_items
            )
            _donut_col, _legend_col = st.columns([1.1, 0.9], gap="small")
            with _donut_col:
                st.plotly_chart(fig_pie, use_container_width=True, config={"displayModeBar": False})
            with _legend_col:
                st.markdown(
                    f'<div style="padding-top:1.2rem;display:flex;flex-direction:column">{legend_html}</div>',
                    unsafe_allow_html=True,
                )


    # ── Table ──
    with content_card(overview=True):
        overview_section_title("Recent Failed Executions")
        if not failed_df.empty:
            render_failed_executions_table(
                failed_df,
                ["executionId", "pipelineName", "firstFailedStep", "Deploy Start Time", "Duration (Min)"],
                max_rows=20,
            )
        else:
            st.success("No recent failures found.")

# ═══════════════════════════════════════════════════════════
# PAGE 2 — FAILURE ANALYSIS
# ═══════════════════════════════════════════════════════════
elif page == "Failure Analysis":
    section_header("Failure Analysis", "AI-powered root cause analysis across all pipeline executions")

    _fa_pid = _active_customer.get("program_id") or "19905"
    saved = None
    # Primary: load from SQLite
    try:
        from db.report_store import load_failure_report as _load_fa
        _fa_result = _load_fa(_fa_pid)
        if _fa_result:
            saved, _ = _fa_result
    except Exception:
        pass
    # Fallback: legacy file (for environments without DB yet)
    if saved is None:
        report_path = Path(f"reports/latest_report_{_fa_pid}.json")
        if report_path.exists():
            try:
                with open(report_path) as f:
                    saved = json.load(f)
            except Exception:
                saved = None

    if saved is not None:


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

        if Path(f"reports/latest_report_{_active_customer['program_id'] or '19905'}.md").exists():
            with st.expander("View full markdown report"):
                st.markdown(Path(f"reports/latest_report_{_active_customer['program_id'] or '19905'}.md").read_text())
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

                try:
                    from db.report_store import save_failure_report as _save_fa
                    _save_fa(
                        program_id=_active_customer.get("program_id","19905"),
                        report_json=report.model_dump(mode="json"),
                        customer=_active_customer.get("short",""),
                    )
                except Exception:
                    # Fallback to file
                    os.makedirs("reports", exist_ok=True)
                    with open(f"reports/latest_report_{_active_customer['program_id'] or '19905'}.json", "w") as f:
                        json.dump(report.model_dump(mode="json"), f, indent=2)
                st.cache_data.clear()
                st.cache_resource.clear()
                st.rerun()
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
            pipeline_df, failed_df, share_map, _ra_source = get_data_or_stop()
        except Exception as _e:
            st.error(f"Could not load data: {_e}")
            st.stop()

    # ── Dev-pipeline executions table ─────────────────────────────────────────
    with content_card():
        section_label("Recent Pipeline Executions", dark=True)
        st.markdown(
            f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:0 0 10px 0">'
            f'All recent executions — click any row to run a risk assessment.</p>',
            unsafe_allow_html=True,
        )

        # Show ALL pipelines — developers need to assess any recent execution
        _dev_df  = pipeline_df.copy()
        _dev_fdf = failed_df.copy()

        # CSV supplement only for IDFC (only customer with local CSV)
        if _active_customer.get("tenant_id") == "idfc":
            try:
                from connectors.splunk_csv_reader import load_pipeline_list as _load_csv_pl, get_failed_executions as _load_csv_fe, load_failed_steps as _load_csv_fs
                _csv_pdf = _load_csv_pl("data/splunk_exports/pipelines-list.csv")
                _csv_dev = _csv_pdf[_csv_pdf["pipelineId"].astype(str) == str(_dev_pid)].copy() if _dev_pid else _csv_pdf[_csv_pdf["pipelineName"].str.contains("Dev|dev", case=False, na=False)].copy()
                if not _csv_dev.empty:
                    _dev_df = pd.concat([_dev_df, _csv_dev]).drop_duplicates("executionId")
                    _csv_fsteps = _load_csv_fs("data/splunk_exports/first-failed-steps.csv")
                    _csv_fdf = _load_csv_fe(_csv_pdf, _csv_fsteps)
                    _csv_fdf_dev = _csv_fdf[_csv_fdf["pipelineId"].astype(str) == str(_dev_pid)].copy() if _dev_pid else _csv_fdf[_csv_fdf["pipelineName"].str.contains("Dev|dev", case=False, na=False)].copy()
                    _dev_fdf = pd.concat([_dev_fdf, _csv_fdf_dev]).drop_duplicates("executionId")
            except Exception:
                pass

        # Merge step info onto dev executions
        if not _dev_fdf.empty and "firstFailedStep" in _dev_fdf.columns:
            _step_map = _dev_fdf.drop_duplicates("executionId")[["executionId","firstFailedStep"]]
            _dev_df = _dev_df.merge(_step_map, on="executionId", how="left")
        else:
            _dev_df["firstFailedStep"] = ""

        # Show all statuses — developers may want to assess any recent execution
        _dev_df = _dev_df.drop_duplicates("executionId")
        _dev_df = _dev_df.sort_values("Deploy Start Time", ascending=False)

        if _dev_df.empty:
            st.info("No pipeline executions found in the current data export.")
        else:
            # Column headers
            _dh = st.columns([1.2, 1.3, 1.5, 1.8])
            for _col, _lbl in zip(_dh, ["Execution", "Status", "Failed Step", "Started"]):
                _col.markdown(
                    f'<p style="font-size:0.67rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0;padding:4px 0">{_lbl}</p>',
                    unsafe_allow_html=True,
                )
            st.markdown(f'<hr style="margin:0 0 4px 0;border:none;border-top:1px solid {T["border"]}">', unsafe_allow_html=True)

            _sel_dev = str(st.session_state.get("risk_dev_exec", "") or "").strip()
            for _dev_idx, (_dr_idx, _dr) in enumerate(_dev_df.head(25).iterrows()):
                _eid    = str(_dr.get("executionId", "")).strip()
                _status = str(_dr.get("Status", ""))
                _step   = str(_dr.get("firstFailedStep", "") or "")
                _step   = "" if _step == "nan" else _step
                _start  = str(_dr.get("Deploy Start Time", ""))
                try:
                    _start_fmt = pd.to_datetime(_start).strftime("%b %d · %H:%M")
                except Exception:
                    _start_fmt = _start[:16]
                _is_sel = _eid == _sel_dev

                # Status styling
                _sc = {"FINISHED": T["green"], "FAILED": T["red"],
                       "ERROR": T["red"], "CANCELLED": T["text_muted"]}.get(_status, T["text_muted"])
                _si = {"FINISHED": "✓", "FAILED": "✗", "ERROR": "✗", "CANCELLED": "○"}.get(_status, "·")

                _dc1, _dc2, _dc3, _dc4 = st.columns([1.2, 1.3, 1.5, 1.8])
                with _dc1:
                    if st.button(
                        _eid[-8:],
                        key=f"_dev_{_dev_idx}_{_eid}",
                        use_container_width=True,
                        type="primary" if _is_sel else "secondary",
                        help=f"Execution {_eid}",
                    ):
                        st.session_state["risk_dev_exec"] = _eid
                        st.session_state["risk_dev_status"] = _status
                        st.session_state["risk_dev_step"] = _step
                        st.session_state.pop("risk_commit_input", None)
                        st.session_state.pop("risk_report", None)
                        st.session_state["risk_scroll_to_analysis"] = True
                        st.rerun()
                _text_color = T["text"] if _IS_DARK else T["text_muted"]
                _dc2.markdown(f'<p style="font-size:0.8rem;color:{_sc};font-weight:600;margin:6px 0">{_si} {_status}</p>', unsafe_allow_html=True)
                _dc3.markdown(f'<p style="font-size:0.78rem;color:{T["red"] if _step else _text_color};margin:6px 0">{_step or "—"}</p>', unsafe_allow_html=True)
                _dc4.markdown(f'<p style="font-size:0.78rem;color:{_text_color};margin:6px 0">{_start_fmt}</p>', unsafe_allow_html=True)

    # ── Recent git commits ────────────────────────────────────────────────────
    with content_card():
        section_label("Recent Git Commits", dark=True)
        st.markdown(
            f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:0 0 8px 0">'
            f'No dev pipeline run yet? Select a commit for an early code-based risk estimate. '
            f'<strong>Lower confidence</strong> — build outcome unknown.</p>',
            unsafe_allow_html=True,
        )
        try:
            from connectors.git_connector import get_recent_commits, get_sync_status
            _gc_commits = get_recent_commits(branch=_active_customer.get("git_branch",""), n=15)
            _gc_sync = get_sync_status()
            if _gc_sync.get("age_minutes") is not None:
                st.markdown(
                    f'<p style="font-size:0.68rem;color:{T["text_muted"]};margin:0 0 6px 0">'
                    f'Synced {_gc_sync["age_minutes"]} min ago</p>',
                    unsafe_allow_html=True,
                )
            if _gc_commits:
                _sel_sha = st.session_state.get("risk_commit_input", "")
                _gc_hc = st.columns([1.2, 3.5, 1.5, 1.0])
                for _gc_col, _gc_lbl in zip(_gc_hc, ["Commit", "Message", "Author", "When"]):
                    _gc_col.markdown(
                        f'<p style="font-size:0.67rem;font-weight:700;text-transform:uppercase;'
                        f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0;padding:4px 0">{_gc_lbl}</p>',
                        unsafe_allow_html=True,
                    )
                st.markdown(f'<hr style="margin:0 0 4px 0;border:none;border-top:1px solid {T["border"]}">', unsafe_allow_html=True)
                for _gc_idx, _gc in enumerate(_gc_commits):
                    _gc_sha  = _gc.get("sha", "")
                    _gc_sha8 = _gc_sha[:8]
                    _gc_title = _gc.get("title", "")[:65]
                    _gc_author = _gc.get("author", "—")
                    _gc_when = _gc.get("when", "—")
                    _gc_is_sel = _sel_sha.startswith(_gc_sha8) and not st.session_state.get("risk_dev_exec")
                    _gcc1, _gcc2, _gcc3, _gcc4 = st.columns([1.2, 3.5, 1.5, 1.0])
                    with _gcc1:
                        if st.button(
                            _gc_sha8,
                            key=f"_gc_{_gc_idx}_{_gc_sha8}",
                            use_container_width=True,
                            type="primary" if _gc_is_sel else "secondary",
                            help=_gc_sha,
                        ):
                            st.session_state["risk_commit_input"] = _gc_sha
                            st.session_state.pop("risk_dev_exec", None)
                            st.session_state.pop("risk_dev_status", None)
                            st.session_state.pop("risk_dev_step", None)
                            st.session_state.pop("risk_report", None)
                            st.session_state["risk_scroll_to_analysis"] = True
                            st.rerun()
                    _gcc2.markdown(f'<p style="font-size:0.78rem;color:{T["text"]};margin:6px 0">{_gc_title}</p>', unsafe_allow_html=True)
                    _gcc3.markdown(f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:6px 0">{_gc_author[:20]}</p>', unsafe_allow_html=True)
                    _gcc4.markdown(f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:6px 0">{_gc_when}</p>', unsafe_allow_html=True)
            else:
                _gc_branch_cfg = _active_customer.get("git_branch", "")
                st.markdown(
                    f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:4px 0">'
                    f'No recent commits found on branch <code>{_gc_branch_cfg or "HEAD"}</code>. '
                    f'The branch may not be fetched locally yet — Argus will fetch it automatically '
                    f'when you run an assessment.</p>',
                    unsafe_allow_html=True,
                )
        except Exception as _gc_e:
            _gc_err = str(_gc_e)
            if "ambiguous argument" in _gc_err or "unknown revision" in _gc_err:
                _gc_branch_cfg = _active_customer.get("git_branch", "")
                st.markdown(
                    f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:4px 0">'
                    f'Branch <code>{_gc_branch_cfg}</code> not in local repo yet. '
                    f'Run an assessment — Argus will fetch it automatically.</p>',
                    unsafe_allow_html=True,
                )
            else:
                st.caption(f"Git repo unavailable — {_gc_err[:80]}")

    # ── SHA search bar ────────────────────────────────────────────────────────
    st.markdown(
        f'<div style="background:{T["surface2"]};border:1px solid {T["border"]};'
        f'border-radius:8px;padding:12px 16px;margin:8px 0 6px 0">'
        f'<p style="font-size:0.78rem;font-weight:600;color:{T["text"]};margin:0 0 6px 0">'
        f'Paste a commit SHA</p>'
        f'<p style="font-size:0.72rem;color:{T["text_muted"]};margin:0 0 4px 0;line-height:1.7">'
        f'<b>Before triggering a pipeline:</b> run '
        f'<code style="background:{T["surface"]};padding:1px 5px;border-radius:3px">git log --oneline -5</code> '
        f'in your terminal to get the latest commit SHA.</p>'
        f'<p style="font-size:0.72rem;color:{T["text_muted"]};margin:0;line-height:1.7">'
        f'<b>After a pipeline has run:</b> Open Cloud Manager → your program → Pipelines → click any execution → '
        f'look for <code style="background:{T["surface"]};padding:1px 5px;border-radius:3px">COMMIT:</code> '
        f'under the Build &amp; Unit Testing step. Copy the full 40-character SHA shown there.</p>'
        f'</div>',
        unsafe_allow_html=True,
    )
    with st.form("sha_search_form", clear_on_submit=False):
        _search_col, _btn_col = st.columns([5, 1], gap="small")
        with _search_col:
            _sha_input = st.text_input(
                "sha_search",
                placeholder="e.g. 70190dfe144ba4e1d92972a329dea9d4f3f540eb",
                label_visibility="collapsed",
                key="_sha_search_input",
            )
        with _btn_col:
            _sha_search_clicked = st.form_submit_button("Analyse", type="primary", use_container_width=True)

    if _sha_search_clicked and _sha_input.strip():
        _cleaned = _sha_input.strip()

        # Reject execution IDs (pure digits) — SHA only accepted now
        import re as _re_input
        if _re_input.match(r'^\d{5,14}$', _cleaned):
            st.warning("Please paste the commit SHA, not the execution ID. Find it in Cloud Manager → execution page → COMMIT: field under Build & Unit Testing.")
            _cleaned = ""
        elif False:  # placeholder to keep the indentation block below intact
            _resolved_sha = ""
            _eid = _cleaned

            _pid       = _active_customer.get("program_id", "")
            _git_dir   = _active_customer.get("git_local_dir", "")
            _branch    = _active_customer.get("git_branch", "master")

            # Build list of all repos to search for this customer (main + additional)
            _all_git_dirs = [_git_dir] if _git_dir else []
            try:
                import json as _json_rc
                _rc = _json_rc.loads(Path("data/repo_config.json").read_text())
                _cname = st.session_state.get("selected_customer", "")
                for _ar in _rc.get(_cname, {}).get("additional_repos", []):
                    _ald = _ar.get("local_dir", "")
                    if _ald and _ald not in _all_git_dirs and Path(_ald).exists():
                        _all_git_dirs.append(_ald)
            except Exception:
                pass

            # Step 1: Git tag lookup across ALL repos for this customer
            for _git_dir in _all_git_dirs:
                try:
                    import subprocess as _sp_tag
                    _tag_out = _sp_tag.run(
                        ["git", "tag", "--list", f"*{_eid}*"],
                        cwd=_git_dir, capture_output=True, text=True, timeout=10,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    ).stdout.strip()
                    if not _tag_out:
                        # Fetch tags if not found locally
                        _sp_tag.run(
                            ["git", "fetch", "--tags", "--quiet"],
                            cwd=_git_dir, capture_output=True, timeout=30,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                        )
                        _tag_out = _sp_tag.run(
                            ["git", "tag", "--list", f"*{_eid}*"],
                            cwd=_git_dir, capture_output=True, text=True, timeout=10,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                        ).stdout.strip()
                    if _tag_out:
                        _tag_name = _tag_out.splitlines()[0].strip()
                        _sha_out = _sp_tag.run(
                            ["git", "rev-list", "-n", "1", _tag_name],
                            cwd=_git_dir, capture_output=True, text=True, timeout=10,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                        ).stdout.strip()
                        if _sha_out and len(_sha_out) >= 40:
                            _resolved_sha = _sha_out
                            break  # found it — stop searching other repos
                except Exception:
                    pass
                if _resolved_sha:
                    break

            # Restore _git_dir to primary repo for subsequent steps
            _git_dir = _active_customer.get("git_local_dir", "")

            # Step 2: CM API — fallback if git tag not found (requires service account token on server)
            if not _resolved_sha:
                try:
                    from connectors.cm_connector import get_commit_sha_from_execution
                    _ppid = _active_customer.get("pipeline_prod", "")
                    _tid  = _active_customer.get("tenant_id", "")
                    _resolved_sha = get_commit_sha_from_execution(_pid, _ppid, _eid, tenant_id=_tid) or ""
                except Exception:
                    pass

            # Step 2: Splunk timestamp correlation — only if CM API failed
            # MUST use this customer's git repo (pass git_dir explicitly to avoid cross-tenant leak)
            if not _resolved_sha and _all_git_dirs:
                try:
                    _splunk_cache_path = Path(f"data/cache/splunk_cache_{_pid}.pkl")
                    if _splunk_cache_path.exists():
                        import pickle as _pkl
                        with open(_splunk_cache_path, "rb") as _cf:
                            _cdata = _pkl.load(_cf)
                        _cpdf = _cdata.get("pipeline_df")
                        if _cpdf is not None and not _cpdf.empty:
                            import os as _os_eid
                            # Temporarily set correct git dir for this customer's correlation
                            _old_git_dir = _os_eid.environ.get("GIT_LOCAL_DIR", "")
                            _os_eid.environ["GIT_LOCAL_DIR"] = _git_dir
                            try:
                                from connectors.git_connector import correlate_executions_to_commits
                                _crows = _cpdf.drop_duplicates("executionId").to_dict("records")
                                # Try configured branch first, then --all (handles release/Dev-v1 etc.)
                                _cmap = correlate_executions_to_commits(_crows, branch=_branch)
                                if _eid not in _cmap:
                                    # Branch mismatch — try searching all branches
                                    _cmap = correlate_executions_to_commits(_crows, branch="")
                                if _eid in _cmap:
                                    _resolved_sha = _cmap[_eid].get("sha", "")
                            finally:
                                if _old_git_dir:
                                    _os_eid.environ["GIT_LOCAL_DIR"] = _old_git_dir
                except Exception:
                    pass

            # If not resolved, try a fresh Splunk pull once before showing error
            if not _resolved_sha and _pid:
                _auto_refresh_key = f"_eid_refreshed_{_eid}"
                if not st.session_state.get(_auto_refresh_key):
                    st.session_state[_auto_refresh_key] = True
                    with st.spinner(f"Execution {_eid} not in cache — refreshing pipeline data..."):
                        try:
                            from analysis.ingest import load_data as _ld_fresh
                            _pdf_fresh, _fdf_fresh, _, _ = _ld_fresh(program_id=int(_pid), force_refresh=True)
                            # Retry Splunk timing with fresh data
                            if _pdf_fresh is not None and not _pdf_fresh.empty and _all_git_dirs:
                                import os as _os_fresh
                                _old_fresh = _os_fresh.environ.get("GIT_LOCAL_DIR", "")
                                _os_fresh.environ["GIT_LOCAL_DIR"] = _all_git_dirs[0]
                                try:
                                    from connectors.git_connector import correlate_executions_to_commits
                                    _cmap_f = correlate_executions_to_commits(
                                        _pdf_fresh.drop_duplicates("executionId").to_dict("records"), branch=""
                                    )
                                    if _eid in _cmap_f:
                                        _resolved_sha = _cmap_f[_eid].get("sha", "")
                                finally:
                                    if _old_fresh: _os_fresh.environ["GIT_LOCAL_DIR"] = _old_fresh
                        except Exception:
                            pass

            if _resolved_sha:
                st.success(f"Execution ID {_eid} → SHA `{_resolved_sha[:12]}...`")
                _cleaned = _resolved_sha
            else:
                st.markdown(
                    f'<div style="background:{T["surface2"]};border:1px solid {T["border"]};'
                    f'border-left:4px solid {T["amber"]};border-radius:8px;padding:16px 20px;margin:10px 0">'
                    f'<p style="font-size:0.82rem;font-weight:700;color:{T["text"]};margin:0 0 6px 0">'
                    f'Argus could not find this execution</p>'
                    f'<p style="font-size:0.78rem;color:{T["text_muted"]};margin:0 0 10px 0;line-height:1.6">'
                    f'Quick heads up — Argus is a prototype. It does not have every repository for every customer '
                    f'pre-loaded out of the box. Some customers, like this one, use multiple repos in Cloud Manager, '
                    f'and Argus only knows about the ones it has been told about so far. '
                    f'This execution ran against a repo that is not on the server yet — which is why it cannot find the commit.'
                    f'</p>'
                    f'<p style="font-size:0.75rem;color:{T["text_muted"]};margin:0;line-height:1.5">'
                    f'The only way to fix this is to add the repo name below — Argus will clone it automatically and then you are good to go. '
                    f'Pasting the commit SHA directly will not help here either, since Argus still needs the repo cloned locally to read what code actually changed.'
                    f'</p>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                _tried_key = f"_repo_tried_{_eid}"
                _existing_url = _active_customer.get("git_url", "")
                _org = _existing_url.rstrip("/").split("/")[-2] if _existing_url else ""
                _base = f"https://git.cloudmanager.adobe.com/{_org}/" if _org else "https://git.cloudmanager.adobe.com/org/"
                _add_repo_btn = False
                _repo_input = ""

                if st.session_state.get(_tried_key):
                    st.info("Repo already added. Click Refresh in the sidebar to reload pipeline data, then try the execution ID again.")
                    _cleaned = ""
                else:
                    with st.form(f"add_repo_form_{_eid}", clear_on_submit=True):
                        st.markdown(
                            f'<p style="font-size:0.78rem;font-weight:600;color:{T["text"]};margin:0 0 8px 0">'
                            f'Which repository did this execution use?</p>'
                            f'<p style="font-size:0.72rem;color:{T["text_muted"]};margin:0 0 10px 0">'
                            f'Find it in Cloud Manager under your program name → Repositories.</p>',
                            unsafe_allow_html=True,
                        )
                        _repo_input = st.text_input(
                            "Repository name",
                            placeholder="b86-hdfcformsmaster",
                            help=f"Will be cloned from {_base}<repo-name>/"
                        )
                        _add_repo_btn = st.form_submit_button("Clone and retry", type="primary")

                if _add_repo_btn and _repo_input.strip():
                    _repo_name = _repo_input.strip()
                    _extra_url = f"{_base}{_repo_name}/"
                    _repos_base = os.getenv("REPOS_BASE_DIR", str(Path.home() / "projects"))
                    _extra_local = f"{_repos_base}/{_repo_name}"
                    _git_pwd = _active_customer.get("git_password", "")
                    _git_user = _active_customer.get("git_username", "vanssharma-adobe-com")

                    if not _git_pwd:
                        st.error("No git password found for this customer. Add it in Repo Settings first.")
                    else:
                        with st.spinner(f"Cloning {_repo_name}..."):
                            try:
                                import subprocess as _sp_clone
                                from urllib.parse import quote as _q_clone
                                _auth = _extra_url.replace("https://", f"https://{_q_clone(_git_user,safe='')}:{_q_clone(_git_pwd,safe='')}@")
                                Path(_extra_local).mkdir(parents=True, exist_ok=True)
                                _cr = _sp_clone.run(
                                    ["git", "clone", _auth, _extra_local],
                                    capture_output=True, text=True, timeout=300,
                                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                                )
                                if _cr.returncode != 0 and "already exists" not in _cr.stderr:
                                    st.error(f"Clone failed: {_cr.stderr[:200]}")
                                else:
                                    st.success(f"Cloned to {_extra_local}")
                                    # Retry resolution with the new repo
                                    import os as _os_retry
                                    _old = _os_retry.environ.get("GIT_LOCAL_DIR", "")
                                    _os_retry.environ["GIT_LOCAL_DIR"] = _extra_local
                                    try:
                                        from connectors.git_connector import correlate_executions_to_commits
                                        with open(Path(f"data/cache/splunk_cache_{_pid}.pkl"), "rb") as _cf2:
                                            import pickle as _pkl2
                                            _cd2 = _pkl2.load(_cf2)
                                        _cpdf2 = _cd2.get("pipeline_df")
                                        if _cpdf2 is not None:
                                            _cm2 = correlate_executions_to_commits(
                                                _cpdf2.drop_duplicates("executionId").to_dict("records"), branch=""
                                            )
                                            if _eid in _cm2:
                                                _resolved_sha = _cm2[_eid].get("sha", "")
                                    finally:
                                        if _old: _os_retry.environ["GIT_LOCAL_DIR"] = _old

                                    if _resolved_sha:
                                        st.success(f"Resolved → SHA `{_resolved_sha[:12]}...`")
                                        _cleaned = _resolved_sha
                                    else:
                                        # Mark as tried so form doesn't loop
                                        st.session_state[_tried_key] = True
                                        st.info(
                                            "Repo cloned. The execution may be too recent for Splunk. "
                                            "Click **Refresh** in the sidebar, then try the execution ID again. "
                                            "Or paste the commit SHA directly from Cloud Manager."
                                        )
                            except Exception as _ce:
                                st.error(f"Error: {_ce}")
                _cleaned = _resolved_sha if _resolved_sha else ""

        if _cleaned:
            st.session_state["risk_commit_input"] = _cleaned
            st.session_state.pop("risk_dev_exec", None)
            st.session_state.pop("risk_report", None)
            st.session_state["risk_scroll_to_analysis"] = True
            st.rerun()

    _scroll_risk_page_bottom_if_needed()

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

        # RUNNING → try git tag lookup first (CM writes the tag at build start)
        # Only fall back to "paste SHA" message if tag not found yet
        elif _sel_status == "RUNNING":
            _running_sha = ""
            _git_dir_r = _active_customer.get("git_local_dir", "") or os.getenv("GIT_LOCAL_DIR", "")
            if _sel_exec and _git_dir_r:
                try:
                    import subprocess as _sp_run
                    _t = _sp_run.run(
                        ["git", "tag", "--list", f"*{_sel_exec}*"],
                        cwd=_git_dir_r, capture_output=True, text=True, timeout=5,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                    ).stdout.strip()
                    if _t:
                        _sha_r = _sp_run.run(
                            ["git", "rev-list", "-n", "1", _t.splitlines()[0].strip()],
                            cwd=_git_dir_r, capture_output=True, text=True, timeout=5,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                        ).stdout.strip()
                        if _sha_r and len(_sha_r) >= 40:
                            _running_sha = _sha_r
                            st.session_state["risk_commit_input"] = _running_sha
                except Exception:
                    pass

            if _running_sha:
                # SHA found via git tag — run the assessment directly here.
                # Cannot "fall through" to the FINISHED elif (elif already matched RUNNING).
                st.markdown(
                    f'<div style="background:rgba(99,102,241,0.07);border:1px solid rgba(99,102,241,0.3);'
                    f'border-left:4px solid {T["blue"]};border-radius:6px;padding:10px 14px;margin:8px 0">'
                    f'<p style="font-size:0.83rem;color:{T["blue"]};font-weight:600;margin:0">'
                    f'Pipeline is currently running — SHA found from git tag, assessing now.</p>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                with ai_analyzing_loader(f"Analysing build risk for running pipeline ({_running_sha[:8]}…)"):
                    try:
                        _cust_git_dir_r = _active_customer.get("git_local_dir","") or os.getenv("GIT_LOCAL_DIR","")
                        if _cust_git_dir_r:
                            os.environ["GIT_LOCAL_DIR"] = _cust_git_dir_r
                        from analysis.risk_analyzer import run_pre_deploy_risk, save_risk_report
                        from analysis.ingest import build_base_bundle
                        if "risk_base_bundle" not in st.session_state:
                            _rb_r, _, _, _ = build_base_bundle(fetch_logs=False)
                            st.session_state["risk_base_bundle"] = _rb_r
                        _bundle_r = st.session_state["risk_base_bundle"]
                        _bundle_r.__dict__["git_local_dir"] = _cust_git_dir_r
                        _bundle_r.__dict__["customer_name"] = st.session_state.get("selected_customer","")
                        _bundle_r.__dict__["dev_execution_status"] = "RUNNING"
                        _bundle_r.__dict__["dev_execution_id"] = str(_sel_exec or "")
                        _, _rep_r, _md_r = run_pre_deploy_risk(
                            commit_sha=_running_sha, fetch_logs=False, use_llm=True, bundle=_bundle_r
                        )
                        save_risk_report(_rep_r, _md_r, commit_sha=_running_sha,
                                         program_id=_active_customer.get("program_id",""),
                                         customer=_active_customer.get("short",""))
                        _rr = _rep_r.model_dump(mode="json")
                        if hasattr(_rep_r,"__dict__") and _rep_r.__dict__.get("similar_incidents_raw"):
                            _rr["similar_incidents"] = _rep_r.__dict__["similar_incidents_raw"]
                        if hasattr(_rep_r,"__dict__") and _rep_r.__dict__.get("risk_decision"):
                            _dr = _rep_r.__dict__["risk_decision"]
                            _rr["_env_signal"]  = {"status":_dr.env.status,"score":_dr.env.score,"consecutive_failures":_dr.env.consecutive_failures,"dominant_step":_dr.env.dominant_step,"last_success_ago":_dr.env.last_success_ago,"detail":_dr.env.detail,"fix":_dr.env.fix,"failure_probability":_dr.env.failure_probability,"is_persistent_infra":getattr(_dr.env,"is_persistent_infra",False),"is_env_issue":getattr(_dr.env,"is_env_issue",False),"hold_threshold":getattr(_dr.env,"hold_threshold",3),"env_step_failure_count":getattr(_dr.env,"env_step_failure_count",0)}
                            _rr["_code_signal"] = {"level":_dr.code.level,"score":_dr.code.score,"detail":_dr.code.detail,"findings":_dr.code.findings}
                            _rr["_hist_signal"] = {"score":_dr.historical.score,"match_count":_dr.historical.match_count,"dominant_step":_dr.historical.dominant_step,"detail":_dr.historical.detail}
                            _rr["_recommendation"] = _dr.recommendation
                            _rr["_confidence_basis"] = _dr.confidence_basis
                            _rr["_expected_outcome"] = _dr.expected_outcome
                            _rr["_code_recommendation"]   = _rep_r.__dict__.get("_code_recommendation","")
                            _rr["_code_confidence"]       = _rep_r.__dict__.get("_code_confidence",0.0)
                            _rr["_code_confidence_basis"] = _rep_r.__dict__.get("_code_confidence_basis","")
                        if _bundle_r.git_context:
                            st.session_state["risk_git_changed_files"] = _bundle_r.git_context.changed_files or []
                            st.session_state["risk_git_title"]  = _bundle_r.git_context.title or ""
                            st.session_state["risk_git_author"] = _bundle_r.git_context.author or ""
                            st.session_state["risk_git_date"]   = _bundle_r.git_context.commit_date or ""
                            st.session_state["risk_git_diff"]   = (_bundle_r.git_context.diff_excerpt or "")[:5000]
                        st.session_state["risk_report"] = _rr
                    except Exception as _e_r:
                        st.error(f"Assessment failed: {_e_r}")
                st.rerun()
            else:
                st.markdown(
                    f'<div style="background:rgba(99,102,241,0.07);border:1px solid rgba(99,102,241,0.3);'
                    f'border-left:4px solid {T["blue"]};border-radius:6px;padding:14px 18px;margin:8px 0">'
                    f'<p style="font-size:0.88rem;font-weight:700;color:{T["blue"]};margin:0 0 6px 0">'
                    f'Pipeline is currently running</p>'
                    f'<p style="font-size:0.78rem;color:{T["text"]};margin:0 0 8px 0">'
                    f'The git tag is not available yet. Open the execution in Cloud Manager, '
                    f'go to Build &amp; Unit Testing, copy the SHA next to '
                    f'<code style="background:{T["surface"]};padding:1px 5px;border-radius:3px">COMMIT:</code> '
                    f'and paste it into the input field below.</p>'
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

        # FINISHED → LLM-based Production risk assessment (auto-runs immediately)
        elif _sel_status == "FINISHED":
            # Step 1: try to get SHA from session state (already validated)
            _auto_sha = st.session_state.get("risk_commit_input", "")
            import re as _re_sha0
            if _auto_sha and not _re_sha0.match(r"^[0-9a-f]{7,}", _auto_sha.lower()):
                _auto_sha = ""

            # Step 1b: git tag lookup using execution ID — most accurate, no API needed
            # CM writes tag: 2026.630.141604.0008505992 → points to exact SHA built
            if not _auto_sha and _sel_exec:
                try:
                    import subprocess as _sp_tag2
                    _git_dir2 = _active_customer.get("git_local_dir", "") or os.getenv("GIT_LOCAL_DIR", "")
                    if _git_dir2:
                        _tag_out2 = _sp_tag2.run(
                            ["git", "tag", "--list", f"*{_sel_exec}*"],
                            cwd=_git_dir2, capture_output=True, text=True, timeout=5,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                        ).stdout.strip()
                        if _tag_out2:
                            _sha_from_tag = _sp_tag2.run(
                                ["git", "rev-list", "-n", "1", _tag_out2.splitlines()[0].strip()],
                                cwd=_git_dir2, capture_output=True, text=True, timeout=5,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                            ).stdout.strip()
                            if _sha_from_tag and len(_sha_from_tag) >= 40:
                                # Check if tag points to a Jenkins bot commit — if so use parent
                                _bot_t = ("updated pom.xml file as per build parameters",
                                          "tagging version", "bump version")
                                try:
                                    _tag_title = _sp_tag2.run(
                                        ["git", "log", "-1", "--format=%s", _sha_from_tag],
                                        cwd=_git_dir2, capture_output=True, text=True, timeout=5,
                                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                                    ).stdout.strip().lower()
                                    _tag_author = _sp_tag2.run(
                                        ["git", "log", "-1", "--format=%an", _sha_from_tag],
                                        cwd=_git_dir2, capture_output=True, text=True, timeout=5,
                                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                                    ).stdout.strip().lower()
                                    if (any(p in _tag_title for p in _bot_t)
                                            or "jenkins" in _tag_author or "cicd" in _tag_author):
                                        _parent = _sp_tag2.run(
                                            ["git", "rev-list", "-n", "1", f"{_sha_from_tag}^"],
                                            cwd=_git_dir2, capture_output=True, text=True, timeout=5,
                                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                                        ).stdout.strip()
                                        if _parent and len(_parent) >= 40:
                                            _sha_from_tag = _parent
                                except Exception:
                                    pass
                                _auto_sha = _sha_from_tag
                                st.session_state["risk_commit_input"] = _auto_sha
                except Exception:
                    pass

            # Step 2: try Azure build log — contains "git checkout {SHA}"
            if not _auto_sha:
                _share = share_map.get(_sel_exec, "")
                if _share:
                    try:
                        from connectors.azure_connector import get_file_from_share
                        _blog = get_file_from_share(_share, "build.log")
                        _sha_match = re.search(r"git checkout ([0-9a-f]{40})", _blog or "")
                        if _sha_match:
                            _auto_sha = _sha_match.group(1)
                            st.session_state["risk_commit_input"] = _auto_sha
                    except Exception:
                        pass

            # Step 3: pick most recent developer commit from configured branch
            # Use customer's deploy branch (e.g. stage_and_prod for HDFC), not local HEAD
            if not _auto_sha:
                try:
                    import subprocess as _sp3
                    _repo3 = _active_customer.get("git_local_dir", "") or os.getenv("GIT_LOCAL_DIR", "")
                    _branch3 = _active_customer.get("git_branch", "master")
                    _ref3 = _branch3
                    _ref_check = _sp3.run(
                        ["git", "rev-parse", "--verify", _branch3],
                        cwd=_repo3, capture_output=True, timeout=5,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                    )
                    if _ref_check.returncode != 0:
                        _ref3 = f"origin/{_branch3}"
                    _bot_authors3 = {"jenkins cicd", "jenkins", "bot", "automated"}
                    _bot_titles3  = ("updated pom.xml file as per build parameters",
                                     "tagging version", "bump version")
                    _log3 = _sp3.run(
                        ["git", "log", "--format=%H|||%an|||%s", "-n", "50", _ref3],
                        cwd=_repo3, capture_output=True, text=True, timeout=10,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                    ).stdout.strip().splitlines()
                    for _line3 in _log3:
                        _parts3 = _line3.split("|||", 2)
                        if len(_parts3) < 2:
                            continue
                        _sha3, _auth3, _msg3 = _parts3[0], _parts3[1], (_parts3[2] if len(_parts3)>2 else "")
                        if (any(b in _auth3.lower() for b in _bot_authors3)
                                or any(t in _msg3.lower() for t in _bot_titles3)):
                            continue
                        _auto_sha = _sha3
                        st.session_state["risk_commit_input"] = _auto_sha
                        break
                except Exception:
                    pass

            _sha_info = f" (SHA: `{_auto_sha[:12]}...`)" if _auto_sha else " (no SHA — using pipeline history only)"
            st.markdown(
                f'<div style="background:rgba(45,157,92,0.07);border:1px solid rgba(45,157,92,0.3);'
                f'border-left:4px solid {T["green"]};border-radius:6px;padding:10px 14px;margin:8px 0">'
                f'<p style="font-size:0.83rem;color:{T["green"]};font-weight:600;margin:0">'
                f'✓  Dev-pipeline passed — running Production risk assessment{_sha_info}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if "risk_report" not in st.session_state:
                with ai_analyzing_loader(f"Analysing commit {_auto_sha[:8] if _auto_sha else ''}…"):
                        _status_slot = st.empty()
                        def _status(msg: str):
                            _status_slot.markdown(
                                f'<p style="font-size:0.75rem;color:{T["text_muted"]};'
                                f'text-align:center;margin:4px 0">{msg}</p>',
                                unsafe_allow_html=True,
                            )
                        try:
                            # Force correct repo dir for this customer before any git calls
                            _cust_git_dir = _active_customer.get("git_local_dir", "") or os.getenv("GIT_LOCAL_DIR", "")
                            if _cust_git_dir:
                                os.environ["GIT_LOCAL_DIR"] = _cust_git_dir
                            # Also set GIT_BRANCH so clone_or_update fast-forwards the right branch
                            _cust_git_branch = _active_customer.get("git_branch", "")
                            if _cust_git_branch:
                                os.environ["GIT_BRANCH"] = _cust_git_branch
                            from analysis.risk_analyzer import run_pre_deploy_risk, save_risk_report
                            from analysis.ingest import build_base_bundle, _cache_is_fresh
                            _pid_int = int(_active_customer.get("program_id", 0) or 0)
                            if "risk_base_bundle" not in st.session_state:
                                _is_cached = _pid_int and _cache_is_fresh(_pid_int)
                                _status("Fetching pipeline history from Splunk…" if not _is_cached else "Loading pipeline history from cache…")
                                _base_bundle, _, _, _ = build_base_bundle(fetch_logs=False)
                                st.session_state["risk_base_bundle"] = _base_bundle
                            else:
                                _base_bundle = st.session_state["risk_base_bundle"]
                            # Inject customer-specific context
                            _base_bundle.__dict__["git_local_dir"] = _active_customer.get("git_local_dir", "") or os.getenv("GIT_LOCAL_DIR", "")
                            _base_bundle.__dict__["customer_name"] = st.session_state.get("selected_customer", "")
                            _base_bundle.__dict__["dev_execution_status"] = _sel_status if _sel_exec else ""
                            _base_bundle.__dict__["dev_execution_id"] = str(_sel_exec or "")
                            try:
                                from analysis.risk_scorer import infer_java_upgrade_pending
                                _base_bundle.__dict__["java_upgrade_pending"] = infer_java_upgrade_pending(
                                    {}, pipeline_df, str(_sel_exec or ""),
                                )
                            except Exception:
                                _base_bundle.__dict__["java_upgrade_pending"] = False
                            _exec_date = ""
                            if _sel_exec and not pipeline_df.empty:
                                _exec_row = pipeline_df[pipeline_df["executionId"].astype(str) == str(_sel_exec)]
                                if not _exec_row.empty:
                                    _exec_date = str(_exec_row.iloc[0].get("Deploy Start Time", ""))[:10]
                            _base_bundle.__dict__["execution_date"] = _exec_date
                            # Pre-check: does this SHA exist in the local repo?
                            # If not, a git fetch is needed — tell the user explicitly
                            # so they don't stare at a spinner wondering what's happening.
                            try:
                                from connectors.git_connector import _sha_exists, _local_dir
                                _repo_for_check = _cust_git_dir or _local_dir()
                                if _repo_for_check and not _sha_exists(_auto_sha, _repo_for_check):
                                    _status(f"SHA {_auto_sha[:12]} not in local repo — fetching from remote (up to 30s)…")
                            except Exception:
                                pass

                            _status("Analysing diff and running structural checks…")
                            from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TE2
                            import contextvars as _cv
                            _TIMEOUT = int(os.getenv("ANALYSIS_TIMEOUT_SEC", "180"))
                            _report_result = [None, None, None]
                            # Capture isolated context snapshot — each user session gets its
                            # own ContextVar values (GIT_LOCAL_DIR, credentials, etc.)
                            # so 20 concurrent users can't overwrite each other's state.
                            _ctx_snapshot = _cv.copy_context()
                            def _run_analysis():
                                def _inner():
                                    _r2 = run_pre_deploy_risk(
                                        commit_sha=_auto_sha, fetch_logs=False,
                                        use_llm=True, bundle=_base_bundle,
                                    )
                                    _report_result[:] = list(_r2)
                                _ctx_snapshot.run(_inner)
                            with ThreadPoolExecutor(max_workers=1) as _ex2:
                                _fut2 = _ex2.submit(_run_analysis)
                                try:
                                    _fut2.result(timeout=_TIMEOUT)
                                except _TE2:
                                    st.markdown(
                                        f'<div style="background:#FEF3C7;border-left:4px solid {T["amber"]};'
                                        f'border-radius:8px;padding:14px 18px;margin:8px 0">'
                                        f'<p style="font-size:0.88rem;font-weight:700;color:#92400E;margin:0 0 6px">⏱ Analysis timed out</p>'
                                        f'<p style="font-size:0.78rem;color:#78350F;margin:0 0 10px">'
                                        f'Took longer than {_TIMEOUT}s. This happens when the LLM is slow or the diff is very large.</p>'
                                        f'</div>',
                                        unsafe_allow_html=True,
                                    )
                                    if st.button("↺ Retry", type="primary", key=f"retry_timeout_{_auto_sha[:8]}"):
                                        st.session_state.pop("risk_report", None)
                                        st.session_state.pop("risk_base_bundle", None)
                                        st.rerun()
                                    st.stop()
                            _, report, md = _report_result[0], _report_result[1], _report_result[2]
                            if report is None:
                                st.error("Analysis returned no result — try again.")
                                st.stop()
                            _status("Saving results…")
                            save_risk_report(report, md, commit_sha=_auto_sha,
                                             program_id=_active_customer.get("program_id",""),
                                             customer=_active_customer.get("short", st.session_state.get("selected_customer","")))
                            _r = report.model_dump(mode="json")
                            if hasattr(report, '__dict__') and report.__dict__.get("similar_incidents_raw"):
                                _r["similar_incidents"] = report.__dict__["similar_incidents_raw"]
                            # Store pre-computed signals so display uses correct inputs
                            if hasattr(report, '__dict__') and report.__dict__.get("risk_decision"):
                                _d = report.__dict__["risk_decision"]
                                _r["_env_signal"]  = {"status": _d.env.status, "score": _d.env.score, "consecutive_failures": _d.env.consecutive_failures, "dominant_step": _d.env.dominant_step, "last_success_ago": _d.env.last_success_ago, "detail": _d.env.detail, "fix": _d.env.fix, "failure_probability": _d.env.failure_probability, "is_persistent_infra": getattr(_d.env, "is_persistent_infra", False), "is_env_issue": getattr(_d.env, "is_env_issue", False), "hold_threshold": getattr(_d.env, "hold_threshold", 3), "env_step_failure_count": getattr(_d.env, "env_step_failure_count", 0)}
                                _r["_code_signal"] = {"level": _d.code.level, "score": _d.code.score, "detail": _d.code.detail, "findings": _d.code.findings}
                                _r["_hist_signal"] = {"score": _d.historical.score, "match_count": _d.historical.match_count, "dominant_step": _d.historical.dominant_step, "detail": _d.historical.detail}
                                _r["_llm_signal"]   = {"score": _d.llm.score, "commit_risk_step": _d.llm.commit_risk_step, "dominant_step": _d.llm.dominant_step, "detail": _d.llm.detail, "has_analysis": _d.llm.has_analysis}
                                _r["_recommendation"] = _d.recommendation
                                _r["_confidence_basis"] = _d.confidence_basis
                                _r["_expected_outcome"] = _d.expected_outcome
                                # Code-only recommendation — never blended with env
                                _r["_code_recommendation"]   = report.__dict__.get("_code_recommendation", "")
                                _r["_code_confidence"]       = report.__dict__.get("_code_confidence", 0.0)
                                _r["_code_confidence_basis"] = report.__dict__.get("_code_confidence_basis", "")
                            # Structural override path: risk_decision absent but _code_recommendation
                            # may still be set directly. Pick it up here.
                            if not _r.get("_code_recommendation") and hasattr(report, "__dict__"):
                                if report.__dict__.get("_code_recommendation"):
                                    _r["_code_recommendation"]   = report.__dict__["_code_recommendation"]
                                    _r["_code_confidence"]       = report.__dict__.get("_code_confidence", 0.0)
                                    _r["_code_confidence_basis"] = report.__dict__.get("_code_confidence_basis", "")
                            # Structural override: use _code_signal_override (has correct findings)
                            # over the default _code_signal (which shows subtree=LOW regardless)
                            if not _r.get("_code_signal") and hasattr(report, "__dict__"):
                                _cso = report.__dict__.get("_code_signal_override")
                                if _cso:
                                    _r["_code_signal"] = _cso
                            # Structural override: also serialize env signal from raw env dict
                            if not _r.get("_env_signal") and hasattr(report, "__dict__"):
                                _env_raw = report.__dict__.get("_env_signal_raw")
                                if _env_raw:
                                    _r["_env_signal"] = {
                                        "status": _env_raw.get("status","UNKNOWN"),
                                        "score": _env_raw.get("env_step_failure_count",0) / 10.0,
                                        "consecutive_failures": _env_raw.get("consecutive_failures",0),
                                        "dominant_step": _env_raw.get("dominant_step",""),
                                        "last_success_ago": _env_raw.get("last_success_ago","unknown"),
                                        "detail": _env_raw.get("recommendation","")[:80],
                                        "fix": "", "failure_probability": _env_raw.get("env_step_failure_count",0)/10.0,
                                        "is_persistent_infra": False,
                                        "is_env_issue": _env_raw.get("is_env_issue", False),
                                        "hold_threshold": 3,
                                        "env_step_failure_count": _env_raw.get("env_step_failure_count", 0),
                                    }
                            # Store git context for display and code signal inputs
                            if _base_bundle.git_context:
                                st.session_state["risk_git_changed_files"] = _base_bundle.git_context.changed_files or []
                                st.session_state["risk_git_title"]  = _base_bundle.git_context.title or ""
                                st.session_state["risk_git_author"] = _base_bundle.git_context.author or ""
                                st.session_state["risk_git_date"]   = _base_bundle.git_context.commit_date or ""
                                st.session_state["risk_git_diff"]   = (_base_bundle.git_context.diff_excerpt or "")[:5000]
                            st.session_state["risk_report"] = _r
                            # Save prediction as PENDING — scorer-driven values
                            try:
                                from analysis.prediction_store import save_prediction
                                _dec = report.__dict__.get("risk_decision")
                                _top_factors = []
                                if _dec:
                                    _top_factors = [
                                        _dec.confidence_basis,
                                        f"driver={_dec.primary_driver}",
                                        f"env={_dec.env.status}({_dec.env.consecutive_failures}x)",
                                        f"code={_dec.code.level}",
                                        f"hist={int(_dec.historical.score*100)}%",
                                        *(_dec.code.findings[:2]),
                                    ]
                                save_prediction(
                                    commit_sha=_auto_sha,
                                    predicted_risk=getattr(report, "risk_level", ""),
                                    predicted_step=getattr(report, "most_likely_failure_step", ""),
                                    confidence=int(getattr(report, "confidence_score", 0) or 0),
                                    program_id=_active_customer.get("program_id", "19905"),
                                    execution_id=_sel_exec or "",
                                    tenant_id=_active_customer.get("tenant_id", ""),
                                    pipeline_name="Production Pipeline",
                                    modules_at_risk=getattr(report, "modules_at_risk", []) or [],
                                    top_factors=[f for f in _top_factors if f],
                                    env_prediction={
                                        "step": _dec.env.dominant_step if _dec else "",
                                        "status": _dec.env.status if _dec else "",
                                        "p_fail": _dec.env.failure_probability if _dec else 0,
                                    } if _dec else {},
                                    commit_prediction={
                                        "level": _dec.code.level if _dec else "",
                                        "p_fail": _dec.code.score if _dec else 0,
                                    } if _dec else {},
                                    primary_driver=_dec.primary_driver if _dec else "llm",
                                )
                            except Exception:
                                pass
                            st.rerun()
                        except Exception as e:
                            _err_str = str(e)
                            _is_sha_not_found = (
                                "not found in local repo" in _err_str
                                or "not found in" in _err_str
                                or "SHA" in _err_str and "not found" in _err_str
                            )
                            _is_timeout = "timeout" in _err_str.lower() or "timed out" in _err_str.lower()

                            if _is_sha_not_found or _is_timeout:
                                # Clear, actionable message — not a raw Python traceback
                                if _is_timeout:
                                    st.markdown(
                                        f'<div style="background:#FEF3C7;border-left:4px solid {T["amber"]};'
                                        f'border-radius:8px;padding:14px 18px;margin:8px 0">'
                                        f'<p style="font-size:0.88rem;font-weight:700;color:#92400E;margin:0 0 6px 0">'
                                        f'⏱ Fetch timed out</p>'
                                        f'<p style="font-size:0.78rem;color:#78350F;margin:0 0 10px 0">'
                                        f'SHA <code>{_auto_sha[:12]}</code> was not in the local repo and the remote fetch '
                                        f'timed out after 30 seconds. This happens when the remote git server is slow '
                                        f'or unreachable.</p>'
                                        f'<p style="font-size:0.75rem;color:#92400E;margin:0">'
                                        f'Try again — it usually succeeds on retry.</p>'
                                        f'</div>',
                                        unsafe_allow_html=True,
                                    )
                                else:
                                    st.markdown(
                                        f'<div style="background:#FEF3C7;border-left:4px solid {T["amber"]};'
                                        f'border-radius:8px;padding:14px 18px;margin:8px 0">'
                                        f'<p style="font-size:0.88rem;font-weight:700;color:#92400E;margin:0 0 6px 0">'
                                        f'⚠ Commit not found in local repo</p>'
                                        f'<p style="font-size:0.78rem;color:#78350F;margin:0 0 10px 0">'
                                        f'SHA <code>{_auto_sha[:12]}</code> is not in the local clone. '
                                        f'It may be on a branch that hasn\'t been fetched yet.</p>'
                                        f'</div>',
                                        unsafe_allow_html=True,
                                    )

                                col_retry, col_clear = st.columns([1, 3])
                                with col_retry:
                                    if st.button("↺ Retry", type="primary", key=f"retry_sha_{_auto_sha[:8]}"):
                                        st.session_state.pop("risk_report", None)
                                        st.session_state.pop("risk_base_bundle", None)
                                        st.rerun()
                            else:
                                st.error(f"Assessment failed: {_err_str}")

                            if (_is_sha_not_found and not _is_timeout) or (not _is_sha_not_found and not _is_timeout):
                                _eu = _active_customer.get("git_url","")
                                _eo = _eu.rstrip("/").split("/")[-2] if _eu else ""
                                _eb = f"https://git.cloudmanager.adobe.com/{_eo}/" if _eo else ""
                                st.markdown(
                                    f'<div style="background:{T["surface2"]};border:1px solid {T["amber"]}44;'
                                    f'border-left:4px solid {T["amber"]};border-radius:8px;padding:12px 16px;margin:8px 0">'
                                    f'<p style="font-size:0.78rem;font-weight:600;color:{T["text"]};margin:0 0 4px 0">'
                                    f'This commit is from a different repository</p>'
                                    f'<p style="font-size:0.72rem;color:{T["text_muted"]};margin:0">'
                                    f'Enter the repo name below and Argus will clone it automatically.</p></div>',
                                    unsafe_allow_html=True,
                                )
                                with st.form(f"add_repo_sha_err_{_auto_sha[:8]}", clear_on_submit=True):
                                    _ri_e = st.text_input("Repository name", placeholder="b86-hdfcformsmaster",
                                                          help=f"Will be cloned from {_eb}<name>/")
                                    _ae = st.form_submit_button("Clone and retry", type="primary")
                                if _ae and _ri_e.strip():
                                    _rb_e = os.getenv("REPOS_BASE_DIR", str(Path.home() / "projects"))
                                    _xl_e = f"{_rb_e}/{_ri_e.strip()}"
                                    _xurl_e = f"{_eb}{_ri_e.strip()}/".replace("https://", f"https://{_active_customer.get('git_username','')}:{_active_customer.get('git_password','')}@")
                                    with st.spinner(f"Cloning {_ri_e.strip()}..."):
                                        import subprocess as _spe2
                                        Path(_xl_e).mkdir(parents=True, exist_ok=True)
                                        _cr_e = _spe2.run(["git","clone",_xurl_e,_xl_e], capture_output=True, text=True, timeout=300, env={**os.environ,"GIT_TERMINAL_PROMPT":"0"})
                                        if _cr_e.returncode == 0 or "already exists" in _cr_e.stderr:
                                            os.environ["GIT_LOCAL_DIR"] = _xl_e
                                            st.session_state.pop("risk_report", None)
                                            st.rerun()
                                        else:
                                            st.error(f"Clone failed: {_cr_e.stderr[:200]}")

    # ── Git commit flow (no dev exec selected, commit clicked from table or pasted) ──
    _auto_sha = st.session_state.get("risk_commit_input", "")
    import re as _re_sha2
    if _auto_sha and not _re_sha2.match(r"^[0-9a-f]{7,}", _auto_sha.lower()):
        _auto_sha = ""
    if _auto_sha and not _sel_exec and "risk_report" not in st.session_state:
        with ai_analyzing_loader(
            f"AI is analyzing commit {_auto_sha[:8]}… (may take 20–40s for submodule analysis)"
        ):
            try:
                # Force correct repo dir for this customer before any git calls
                _git_dir = _active_customer.get("git_local_dir", "") or os.getenv("GIT_LOCAL_DIR", "")
                if _git_dir:
                    os.environ["GIT_LOCAL_DIR"] = _git_dir
                # Verify the SHA exists — check main repo AND all additional repos
                from connectors.git_connector import get_commit_diff as _gcd
                _pre_diff = None
                _found_git_dir = _git_dir

                # Build list of all repos to try
                _all_dirs_to_try = [_git_dir] if _git_dir else []
                try:
                    _rc2 = json.loads(Path("data/repo_config.json").read_text())
                    _cname2 = st.session_state.get("selected_customer", "")
                    for _ar2 in _rc2.get(_cname2, {}).get("additional_repos", []):
                        _ald2 = _ar2.get("local_dir", "")
                        if _ald2 and _ald2 not in _all_dirs_to_try and Path(_ald2).exists():
                            _all_dirs_to_try.append(_ald2)
                except Exception:
                    pass

                for _try_dir in _all_dirs_to_try:
                    try:
                        _attempt = _gcd(_try_dir, _auto_sha)
                        if _attempt.get("changed_files"):
                            _pre_diff = _attempt
                            _found_git_dir = _try_dir
                            if _try_dir != _git_dir:
                                os.environ["GIT_LOCAL_DIR"] = _try_dir
                            break
                    except Exception:
                        continue

                if not _pre_diff or not _pre_diff.get("changed_files"):
                    st.stop()
                from analysis.risk_analyzer import run_pre_deploy_risk, save_risk_report
                from analysis.ingest import build_base_bundle
                if "risk_base_bundle" not in st.session_state:
                    _base_bundle, _, _, _ = build_base_bundle(fetch_logs=False)
                    st.session_state["risk_base_bundle"] = _base_bundle
                else:
                    _base_bundle = st.session_state["risk_base_bundle"]
                # Inject customer-specific context — use whichever repo the SHA was found in
                _base_bundle.__dict__["git_local_dir"] = _found_git_dir
                _base_bundle.__dict__["customer_name"] = st.session_state.get("selected_customer", "")
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TE
                _ANALYSIS_TIMEOUT = int(os.getenv("ANALYSIS_TIMEOUT_SEC", "180"))
                with ThreadPoolExecutor(max_workers=1) as _ex:
                    _fut = _ex.submit(run_pre_deploy_risk,
                                      commit_sha=_auto_sha, fetch_logs=False,
                                      use_llm=True, bundle=_base_bundle)
                    try:
                        _, report, md = _fut.result(timeout=_ANALYSIS_TIMEOUT)
                    except _TE:
                        st.error(f"Analysis timed out after {_ANALYSIS_TIMEOUT}s. "
                                 f"Try again — submodule repos may need fetching.")
                        st.stop()
                save_risk_report(report, md, commit_sha=_auto_sha,
                                 program_id=_active_customer.get("program_id",""),
                                 customer=_active_customer.get("short", st.session_state.get("selected_customer","")))
                _r3 = report.model_dump(mode="json")
                if hasattr(report, '__dict__') and report.__dict__.get("similar_incidents_raw"):
                    _r3["similar_incidents"] = report.__dict__["similar_incidents_raw"]
                if hasattr(report, '__dict__') and report.__dict__.get("risk_decision"):
                    _d3 = report.__dict__["risk_decision"]
                    _r3["_env_signal"]       = {"status": _d3.env.status, "score": _d3.env.score, "consecutive_failures": _d3.env.consecutive_failures, "dominant_step": _d3.env.dominant_step, "last_success_ago": _d3.env.last_success_ago, "detail": _d3.env.detail, "fix": _d3.env.fix, "failure_probability": _d3.env.failure_probability, "is_persistent_infra": getattr(_d3.env, "is_persistent_infra", False), "is_env_issue": getattr(_d3.env, "is_env_issue", False), "hold_threshold": getattr(_d3.env, "hold_threshold", 3), "env_step_failure_count": getattr(_d3.env, "env_step_failure_count", 0)}
                    _r3["_code_signal"]      = {"level": _d3.code.level, "score": _d3.code.score, "detail": _d3.code.detail, "findings": _d3.code.findings}
                    _r3["_hist_signal"]      = {"score": _d3.historical.score, "match_count": _d3.historical.match_count, "dominant_step": _d3.historical.dominant_step, "detail": _d3.historical.detail}
                    _r3["_llm_signal"]       = {"score": _d3.llm.score, "commit_risk_step": _d3.llm.commit_risk_step, "dominant_step": _d3.llm.dominant_step, "detail": _d3.llm.detail, "has_analysis": _d3.llm.has_analysis}
                    _r3["_recommendation"]   = _d3.recommendation
                    _r3["_confidence_basis"] = _d3.confidence_basis
                    _r3["_expected_outcome"] = _d3.expected_outcome
                    _r3["_code_recommendation"]   = report.__dict__.get("_code_recommendation", "")
                    _r3["_code_confidence"]       = report.__dict__.get("_code_confidence", 0.0)
                    _r3["_code_confidence_basis"] = report.__dict__.get("_code_confidence_basis", "")
                if not _r3.get("_code_recommendation") and hasattr(report, "__dict__"):
                    if report.__dict__.get("_code_recommendation"):
                        _r3["_code_recommendation"]   = report.__dict__["_code_recommendation"]
                        _r3["_code_confidence"]       = report.__dict__.get("_code_confidence", 0.0)
                        _r3["_code_confidence_basis"] = report.__dict__.get("_code_confidence_basis", "")
                if not _r3.get("_code_signal") and hasattr(report, "__dict__"):
                    _cso3 = report.__dict__.get("_code_signal_override")
                    if _cso3:
                        _r3["_code_signal"] = _cso3
                if not _r3.get("_env_signal") and hasattr(report, "__dict__"):
                    _env_raw3 = report.__dict__.get("_env_signal_raw")
                    if _env_raw3:
                        _r3["_env_signal"] = {
                            "status": _env_raw3.get("status","UNKNOWN"),
                            "score": _env_raw3.get("env_step_failure_count",0) / 10.0,
                            "consecutive_failures": _env_raw3.get("consecutive_failures",0),
                            "dominant_step": _env_raw3.get("dominant_step",""),
                            "last_success_ago": _env_raw3.get("last_success_ago","unknown"),
                            "detail": _env_raw3.get("recommendation","")[:80],
                            "fix": "", "failure_probability": _env_raw3.get("env_step_failure_count",0)/10.0,
                            "is_persistent_infra": False,
                            "is_env_issue": _env_raw3.get("is_env_issue", False),
                            "hold_threshold": 3,
                            "env_step_failure_count": _env_raw3.get("env_step_failure_count", 0),
                        }
                if _base_bundle.git_context:
                    st.session_state["risk_git_changed_files"] = _base_bundle.git_context.changed_files or []
                    st.session_state["risk_git_title"]  = _base_bundle.git_context.title or ""
                    st.session_state["risk_git_author"] = _base_bundle.git_context.author or ""
                    st.session_state["risk_git_date"]   = _base_bundle.git_context.commit_date or ""
                    st.session_state["risk_git_diff"]   = (_base_bundle.git_context.diff_excerpt or "")[:5000]
                st.session_state["risk_report"] = _r3
                try:
                    from analysis.prediction_store import save_prediction
                    _dec2 = report.__dict__.get("risk_decision")
                    _top2 = []
                    if _dec2:
                        _top2 = [
                            _dec2.confidence_basis,
                            f"driver={_dec2.primary_driver}",
                            f"env={_dec2.env.status}({_dec2.env.consecutive_failures}x)",
                            f"code={_dec2.code.level}",
                            f"hist={int(_dec2.historical.score*100)}%",
                            *(_dec2.code.findings[:2]),
                        ]
                    save_prediction(
                        commit_sha=_auto_sha,
                        predicted_risk=getattr(report, "risk_level", ""),
                        predicted_step=getattr(report, "most_likely_failure_step", ""),
                        confidence=int(getattr(report, "confidence_score", 0) or 0),
                        program_id=_active_customer.get("program_id", "19905"),
                        execution_id="",
                        tenant_id=_active_customer.get("tenant_id", ""),
                        pipeline_name="",
                        modules_at_risk=getattr(report, "modules_at_risk", []) or [],
                        top_factors=[f for f in _top2 if f],
                        env_prediction={
                            "step": _dec2.env.dominant_step if _dec2 else "",
                            "status": _dec2.env.status if _dec2 else "",
                            "p_fail": _dec2.env.failure_probability if _dec2 else 0,
                        } if _dec2 else {},
                        commit_prediction={
                            "level": _dec2.code.level if _dec2 else "",
                            "p_fail": _dec2.code.score if _dec2 else 0,
                        } if _dec2 else {},
                        primary_driver=_dec2.primary_driver if _dec2 else "llm",
                    )
                except Exception:
                    pass
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
        _meta = None
        try:
            from connectors.git_connector import get_recent_commits
            _all_commits = get_recent_commits(branch=_active_customer.get("git_branch",""), n=30)
            _meta = next((c for c in _all_commits if c["sha"].startswith(_sha[:8])), None)
        except Exception:
            pass
        # Fallback: use git context stored during analysis (works for any SHA)
        if not _meta:
            _stored_title  = st.session_state.get("risk_git_title", "")
            _stored_author = st.session_state.get("risk_git_author", "")
            _stored_date   = st.session_state.get("risk_git_date", "")
            if _stored_title or _stored_author:
                _meta = {
                    "sha":    _sha,
                    "title":  _stored_title or "—",
                    "author": _stored_author or "—",
                    "when":   _stored_date or "—",
                }

        # Get execution start time for the dev exec if selected
        _exec_start_time = ""
        if _dev_eid and not _dev_df.empty:
            _dev_row = _dev_df[_dev_df["executionId"] == _dev_eid]
            if not _dev_row.empty:
                _exec_start_time = str(_dev_row.iloc[0].get("Deploy Start Time", ""))
                try:
                    _exec_start_time = pd.to_datetime(
                        _exec_start_time.replace(" PDT","").replace(" PST",""),
                        errors="coerce"
                    ).strftime("%b %d, %Y · %H:%M")
                except Exception:
                    pass

        if _sha or _meta:
            _title_text  = _meta["title"] if _meta else "—"
            _author_text = _meta["author"] if _meta else "—"
            _commit_when = _meta["when"] if _meta else "—"
            st.markdown(
                f'<div style="background:{T["surface2"]};border:1px solid {T["border"]};'
                f'border-radius:10px;padding:0.7rem 1rem;margin-bottom:0.65rem;'
                f'display:flex;align-items:center;gap:1.5rem;flex-wrap:wrap">'
                f'<div><p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Commit SHA</p>'
                f'{id_chip(_sha, max_len=len(_sha))}</div>'
                f'<div style="flex:1;min-width:160px"><p style="font-size:0.62rem;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Commit Message</p>'
                f'<p style="font-size:0.83rem;color:{T["text"]};margin:0;font-weight:500">{_title_text[:90]}</p></div>'
                f'<div><p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Author</p>'
                f'<p style="font-size:0.8rem;color:{T["text_sub"]};margin:0">{_author_text}</p></div>'
                f'<div><p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Commit Date</p>'
                f'<p style="font-size:0.8rem;color:{T["text_sub"]};margin:0">{_commit_when}</p></div>'
                + (
                    f'<div><p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Pipeline Run</p>'
                    f'<p style="font-size:0.8rem;color:{T["text"]};margin:0;font-weight:600">{_exec_start_time}</p></div>'
                    if _exec_start_time else ""
                )
                + f'<div><p style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0 0 0.2rem 0">Environment</p>'
                f'<span style="background:{T["blue"]}18;color:{T["blue"]};border:1px solid {T["blue"]}44;'
                f'padding:2px 8px;border-radius:4px;font-size:0.72rem;font-weight:600">AEM Cloud Manager</span></div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Disclaimer and duplicate standing env banner removed —
        # replaced by signal transparency matrix and unified env advisory section below.

        # ── Resolve signals (pre-computed or fallback recompute) ─────────────────
        _cs_pre = r.get("_code_signal") or {}
        if (_cs_pre.get("level") in ("HIGH","MEDIUM") and
            "subtree import" in (_cs_pre.get("detail","") or "").lower()):
            r.pop("_code_signal", None)

        if r.get("_env_signal") and r.get("_code_signal") and r.get("_hist_signal"):
            from analysis.risk_scorer import EnvSignal, CodeSignal, HistoricalSignal
            _es = r["_env_signal"]
            _cs = r["_code_signal"]
            _hs = r["_hist_signal"]
            _env_sig   = EnvSignal(**_es)
            _cs_level  = _cs["level"]
            _cs_score  = _cs["score"]
            _cs_detail = _cs["detail"]
            if "subtree import" in _cs_detail.lower() or "subtree" in _cs_detail.lower():
                _cs_level = "LOW"
                _cs_score = 0.15
            _code_sig  = CodeSignal(level=_cs_level, score=_cs_score, detail=_cs_detail,
                                    findings=_cs.get("findings",[]),
                                    is_submodule_only=_cs_level=="LOW", has_real_code=_cs_level!="LOW")
            _hist_sig  = HistoricalSignal(score=_hs["score"], match_count=_hs.get("match_count",0),
                                          dominant_step=_hs.get("dominant_step",""), fail_rate=_hs["score"],
                                          detail=_hs["detail"], examples=[])
            _rec       = r.get("_recommendation", "CAUTION")
            _conf_f    = (r.get("confidence_score", 50) or 50) / 100
            _basis     = r.get("_confidence_basis", "scorer analysis")
        else:
            try:
                from analysis.risk_scorer import (
                    compute_env_signal, compute_code_signal,
                    compute_historical_signal, make_decision
                )
                from analysis.ingest import load_data as _ld2
                _pdf2, _fdf2, _, _ = _ld2()
                _env_sig  = compute_env_signal(_pdf2, _fdf2)
                _code_sig = compute_code_signal(
                    diff_text     = st.session_state.get("risk_git_diff", "") or "",
                    changed_files = st.session_state.get("risk_git_changed_files", []) or [],
                    commit_title  = st.session_state.get("risk_git_title", "") or "",
                )
                _hist_sig = compute_historical_signal(r)
                _rec, _outcome, _conf_f, _basis, _driver = make_decision(_env_sig, _code_sig, _hist_sig)
            except Exception:
                _rec    = {"High":"HOLD","Critical":"HOLD","Medium":"CAUTION","Low":"GO"}.get(r.get("risk_level",""), "CAUTION")
                _conf_f = (r.get("confidence_score", 50) or 50) / 100
                _basis  = "LLM analysis"
                _step_risks_raw = r.get("step_risks") or []
                _env_step  = next((s for s in _step_risks_raw if s.get("step") in ("securityTest","deploy","loadTest")), None)
                _code_step = next((s for s in _step_risks_raw if s.get("step") in ("build","codeQuality")), None)
                try:
                    from analysis.risk_scorer import EnvSignal, CodeSignal, HistoricalSignal
                    _env_lvl  = (_env_step.get("level","Low") if _env_step else "Low")
                    _env_sig  = EnvSignal(
                        status="READY" if _env_lvl == "Low" else "CAUTION",
                        score={"Low":0.1,"Medium":0.4,"High":0.7}.get(_env_lvl,0.1),
                        consecutive_failures=0,
                        dominant_step=_env_step.get("step","") if _env_step else "",
                        last_success_ago="unknown",
                        detail=(_env_step.get("rationale","") or "Based on LLM analysis")[:80],
                        fix="", failure_probability={"Low":0.1,"Medium":0.4,"High":0.7}.get(_env_lvl,0.1),
                    )
                    _code_lvl = (_code_step.get("level","Low") if _code_step else "Low")
                    _code_sig = CodeSignal(
                        level=_code_lvl,
                        score={"Low":0.15,"Medium":0.5,"High":0.75}.get(_code_lvl,0.15),
                        detail=(_code_step.get("rationale","") or r.get("narrative","Based on LLM analysis"))[:80],
                        findings=[], is_submodule_only=False, has_real_code=True,
                    )
                    _hist_sig = None
                except Exception:
                    _env_sig = _code_sig = _hist_sig = None

        # ── Derived display values ────────────────────────────────────────────
        _code_lvl_cap = {"LOW": 0.25, "MEDIUM": 0.60, "HIGH": 0.85, "CERTAIN": 0.92}
        _build_score  = _code_sig.score if _code_sig else 0.0
        _build_score  = min(_build_score, _code_lvl_cap.get(_code_sig.level if _code_sig else "LOW", 0.25))
        _build_level  = (_code_sig.level if _code_sig else "LOW")
        _build_detail = (_code_sig.detail if _code_sig else "No code analysis available") or "—"
        _build_findings = (_code_sig.findings if _code_sig else []) or []

        _basis_label = {
            "code":         "structural diff analysis",
            "code+history": "structural diff + historical pattern",
            "llm":          "LLM diff analysis",
            "llm+code":     "LLM + structural analysis",
            "none":         "no failure patterns detected",
        }.get(r.get("primary_driver",""), _basis or "scorer analysis")

        # ── Build + environment summary cards (code-only vs live env check) ───

        # ── Hero verdict: best available build signal ────────────────────────
        # Precedence (highest → lowest):
        #   1. Structural code findings (HIGH/CERTAIN → HOLD, MEDIUM → CAUTION)
        #   2. LLM build step risk from step_risks (most comprehensive: rule_scores
        #      + historical ChromaDB + full diff — this is why Step Risk Summary
        #      is more accurate than a code-only verdict)
        #   3. Scorer blended recommendation when driven by code/history
        #   4. Code-only recommendation (fallback)
        # Exception: when scorer verdict is env-only driven, hero always shows
        # code-only verdict — env advisory handles env risk separately.
        _primary_driver = r.get("primary_driver", "") or r.get("_primary_driver", "")
        _env_only_drivers = {"environment", "environment_ops", "environment+history"}
        _code_rec_fallback = (
            "HOLD"    if _build_level in ("HIGH","CERTAIN") else
            "CAUTION" if _build_level == "MEDIUM" else "GO"
        )

        # LLM build step risk — the most comprehensive build signal
        _llm_step_risks = r.get("step_risks") or []
        _llm_build_sr   = next((s for s in _llm_step_risks if s.get("step") == "build"), None)
        _llm_build_lvl  = (_llm_build_sr.get("level", "Low") if _llm_build_sr else "Low")
        _llm_build_rec  = {
            "High": "HOLD", "Critical": "HOLD",
            "Medium": "CAUTION", "Low": "GO",
        }.get(_llm_build_lvl, "GO")
        _llm_build_basis = (_llm_build_sr.get("rationale", "") or "")[:100] if _llm_build_sr else ""

        # Level ordering for max()
        _lvl_order = {"GO": 0, "CAUTION": 1, "HOLD": 2}

        # ── Structural findings pre-computed here so ALL hero paths can use them ──
        # Previously these were computed inside the hero findings block (too late).
        # Structural analysis is deterministic — it catches compile errors the LLM
        # misses when it applies heuristics (e.g. "subtree import = low risk").
        _all_findings_low_pre = all(f.startswith("[LOW]") for f in _build_findings) if _build_findings else True
        _has_high_structural = any(f.startswith("[HIGH]") or f.startswith("[CERTAIN]") for f in _build_findings)
        # Compilation errors: missing symbols, syntax errors — certain build failures
        _has_certain_compile_error = any(
            any(kw in f.lower() for kw in ("cannot find symbol", "class not found", "import not found",
                                            "compilation error", "syntax error", "missing symbol"))
            for f in _build_findings
        )

        if _primary_driver in _env_only_drivers:
            # Env-driven — hero stays code-only, env advisory shows env risk.
            # BUT structural HIGH findings always override GO — the structural checker
            # catches compile errors the LLM misses (e.g. subtree with missing imports).
            _hero_rec   = r.get("_code_recommendation") or _code_rec_fallback
            _hero_conf  = r.get("_code_confidence") or _build_score
            _hero_basis = r.get("_code_confidence_basis") or _basis_label
            if _hero_rec == "GO" and _has_high_structural:
                _hero_rec   = "HOLD" if _has_certain_compile_error else "CAUTION"
                _hero_conf  = max(_hero_conf, 0.70) if _has_certain_compile_error else max(_hero_conf, 0.55)
                _hero_basis = next(
                    (f.split("] ", 1)[-1].split(" —")[0][:120] for f in _build_findings if f.startswith(("[HIGH]", "[CERTAIN]"))),
                    "structural analysis — HIGH findings detected"
                )
        else:
            _is_submodule_only_commit = _code_sig and _code_sig.is_submodule_only

            # LLM is primary — it synthesizes rule_scores + historical ChromaDB + diff.
            if _llm_build_sr and _llm_build_rec != "GO":
                _structural_confirms_high = _build_level in ("HIGH", "CERTAIN")
                _high_confidence = (_conf_f or 0) >= 0.65

                if _is_submodule_only_commit:
                    # Submodule-only parent: LLM can add value (historical patterns,
                    # cross-submodule API breaks) BUT systematically over-rates because
                    # it sees deleted tests and @Reference as build risks — they aren't.
                    # Rule: LLM can say CAUTION (useful signal), but never HOLD
                    # (too many false positives for routine pointer bumps).
                    _hero_rec = "CAUTION" if _llm_build_rec in ("HOLD", "CAUTION") else "GO"
                elif _llm_build_rec == "HOLD" and not _structural_confirms_high and not _high_confidence:
                    # Real code commit: HOLD needs corroboration
                    _hero_rec = "CAUTION"
                else:
                    _hero_rec = _llm_build_rec

                _hero_conf  = _conf_f or _build_score
                _hero_basis = _llm_build_basis or _basis_label
            elif _llm_build_sr and _llm_build_rec == "GO":
                # LLM says build is safe. But structural checker may have caught a
                # compile error the LLM missed (e.g. git subtree with missing imports —
                # LLM applies "subtree = low risk" heuristic; structural found it anyway).
                # Structural HIGH with a compile error always overrides LLM GO.
                if _has_high_structural:
                    _hero_rec   = "HOLD" if _has_certain_compile_error else "CAUTION"
                    _hero_conf  = max((_conf_f or _build_score), 0.70) if _has_certain_compile_error else max((_conf_f or _build_score), 0.55)
                    _hero_basis = next(
                        (f.split("] ", 1)[-1].split(" —")[0][:120] for f in _build_findings if f.startswith(("[HIGH]", "[CERTAIN]"))),
                        "structural diff analysis — HIGH findings override LLM GO"
                    )
                else:
                    _hero_rec   = "GO"
                    _hero_conf  = _conf_f or _build_score
                    _hero_basis = _llm_build_basis or "LLM analysis — no build risk detected"
            else:
                # No LLM build step data — fall back to structural code signal
                _hero_rec   = r.get("_code_recommendation") or _code_rec_fallback
                _hero_conf  = r.get("_code_confidence") or _build_score
                _hero_basis = r.get("_code_confidence_basis") or _basis_label

        # ── Reasoning-derived final pass ─────────────────────────────────────────
        # The LLM's free-form reasoning is written before the structured step_risks,
        # so it contains the real analysis without hedging. Parse it here to:
        #   1. Upgrade _hero_rec if reasoning says something stronger than the struct
        #   2. Extract the key sentence to use as _hero_sub (replaces the generic text)
        #
        # Deterministic structural findings (compile errors) are never downgraded by this —
        # they override reasoning. But reasoning can UPGRADE a too-low struct verdict.
        import re as _re_rsn
        _full_rsn = r.get("reasoning", "") or r.get("narrative", "") or ""
        _rsn_hero_rec  = None   # verdict parsed from reasoning
        _rsn_key_sent  = ""     # sentence to show on hero card

        if _full_rsn:
            _rsn_sents = [s.strip() for s in _re_rsn.split(r'(?<=[.!?])\s+', _full_rsn) if len(s.strip()) > 15]

            # Patterns that map to HOLD/HIGH — deterministic failure language
            _hold_kw = (
                "will fail", "compilation error", "compile error", "cannot find symbol",
                "class not found", "does not exist", "not found in diff", "not found in repo",
                "definite", "certain failure", "certain build failure", "loginexception",
                "nosuchmethoderror", "classnotfoundexception", "module not found",
                "webpack will fail", "syntax error", "missing import", "import not found",
                "build will fail", "this will fail", "fail at build", "fail the build",
            )
            # Patterns that map to CAUTION/MEDIUM
            _caution_kw = (
                "may fail", "might fail", "could fail", "potential", "risk", "concern",
                "possible failure", "worth checking", "should verify", "could break",
                "might break", "raises risk", "elevates risk",
            )
            # Patterns that mean GO/LOW — LLM explicitly calling it safe
            _go_kw = (
                "low risk", "no build risk", "safe to deploy", "unlikely to fail",
                "no compile", "no structural", "no critical", "passes build",
            )

            _build_kw = ("build", "compile", "maven", "mvn", "java", "pom", "import",
                         "symbol", "module", "webpack", "npm", "syntax", "class", "package")

            # Score each sentence: find highest verdict among build-relevant sentences
            _rsn_best_level = 0  # 0=nothing, 1=GO, 2=CAUTION, 3=HOLD
            for _s in _rsn_sents[:12]:
                _sl = _s.lower()
                if not any(k in _sl for k in _build_kw):
                    continue
                if any(k in _sl for k in _hold_kw):
                    if _rsn_best_level < 3:
                        _rsn_best_level = 3
                        _rsn_key_sent = _s
                elif any(k in _sl for k in _caution_kw) and _rsn_best_level < 2:
                    _rsn_best_level = 2
                    _rsn_key_sent = _s
                elif any(k in _sl for k in _go_kw) and _rsn_best_level < 1:
                    _rsn_best_level = 1
                    _rsn_key_sent = _s

            _rsn_hero_rec = {3: "HOLD", 2: "CAUTION", 1: "GO"}.get(_rsn_best_level)

            # If no build sentence found, take the first sentence as the key sentence
            if not _rsn_key_sent and _rsn_sents:
                _rsn_key_sent = _rsn_sents[0]

        # Apply reasoning verdict: upgrade _hero_rec if reasoning says higher risk.
        # Never downgrade a structural compile-error HOLD — deterministic beats free-form.
        if _rsn_hero_rec:
            _rsn_order = {"GO": 0, "CAUTION": 1, "HOLD": 2}
            if _rsn_order.get(_rsn_hero_rec, 0) > _rsn_order.get(_hero_rec, 0):
                # Reasoning is stronger than current verdict — upgrade
                _hero_rec   = _rsn_hero_rec
                _hero_conf  = max(_hero_conf or _build_score,
                                  0.75 if _rsn_hero_rec == "HOLD" else 0.55)
                _hero_basis = _rsn_key_sent[:150] if _rsn_key_sent else _hero_basis

        # Map internal GO/CAUTION/HOLD → display labels LOW/MEDIUM/HIGH
        _hero_display = {"GO": "LOW", "CAUTION": "MEDIUM", "HOLD": "HIGH"}.get(_hero_rec, _hero_rec)
        _hero_col  = {"GO": T["green"], "CAUTION": T["amber"], "HOLD": T["red"]}.get(_hero_rec, T["gray"])
        _hero_icon = {"GO": "✓", "CAUTION": "⚠", "HOLD": "✕"}.get(_hero_rec, "?")
        # Triage framing: tell the developer what to DO, not just a verdict.
        # Derive the recommended action from the top finding type.
        _top_finding_check = ""
        if _build_findings:
            import re as _re_fc
            _fc_match = _re_fc.search(r'\[(?:HIGH|MEDIUM|LOW|CERTAIN)\]\s+(.+?)(?:\s+—|\s+\()', _build_findings[0])
            _top_finding_check = _fc_match.group(1).lower() if _fc_match else ""

        _action_hint = (
            "verify Java 21 compatibility — check for removed APIs and update compiler settings"
            if any(k in _top_finding_check for k in ("java version", "java-version", "cloudmanager", "lts", "java 21", "java21"))
            else "run `mvn -pl <module> test` locally before promoting"
            if any(k in _top_finding_check for k in ("changed", "test", "mock", "inject", "assert", "verify", "npe", "null"))
            else "run a local build to verify before promoting"
            if any(k in _top_finding_check for k in ("pom", "module", "reactor", "submodule", "syntax", "exception", "plugin", "version"))
            else "verify dispatcher config and run a local build before promoting"
            if any(k in _top_finding_check for k in ("dispatcher", "vhost", ".any", ".farm"))
            else "verify the affected module builds cleanly before promoting"
        )

        # Detect migration commits for special handling
        _commit_title_lower = (st.session_state.get("risk_git_title","") or r.get("commit_sha","") or "").lower()
        _is_migration = any(k in _commit_title_lower for k in
                            ("migration", "migrate", "upgrade", "lts", "java21", "java 21", "refactor"))

        # For migration commits: filter LOW service findings from hero (move to footnote)
        # They're advisory noise — 5 × "tests may fail (30%)" obscures the real signals
        _hero_findings = _build_findings
        _migration_footnote = ""
        if _is_migration:
            _real_findings = [f for f in _build_findings if not f.startswith("[LOW]") or
                              not any(k in f.lower() for k in ("changed —", "may fail at runtime"))]
            _low_service = [f for f in _build_findings if f not in _real_findings]
            if _low_service:
                _hero_findings = _real_findings
                _migration_footnote = (
                    f'<p class="ra-ui-footnote">'
                    f'Advisory ({len(_low_service)} service changes with no same-commit test update — '
                    f'normal for coordinated migration PRs, existing tests should still pass).</p>'
                )

        # Unify confidence: structural score caps the LLM confidence.
        # LLM pattern-matches broadly and returns 80-95% for any large migration.
        # Structural analysis is more precise — use it to bound the displayed %.
        # Rule: displayed confidence = min(LLM confidence, structural-based ceiling)
        # Note: _has_high_structural and _all_findings_low_pre are already computed above
        # (before hero decision block). Alias them here for the confidence section.
        _all_findings_low = _all_findings_low_pre

        # Confidence ceiling based on structural evidence:
        # No HIGH findings → max 65% (LLM is speculating beyond structural evidence)
        # All LOW findings → max 55%
        # HIGH findings confirmed → allow up to LLM confidence
        if _hero_conf and _llm_build_sr:
            if _all_findings_low and _hero_rec == "GO":
                # Only cap when hero agrees it's low risk — don't cap structural overrides
                _hero_conf = min(_hero_conf, 0.55)
            elif not _has_high_structural and _is_migration:
                # Migration commit with only MEDIUM structural findings — LLM over-patterns
                _hero_conf = min(_hero_conf, 0.65)

        # ── Single source of truth: _approx_pct always reflects _hero_conf ──────
        # Previously: _approx_pct = int(_build_score * 100) — the raw structural scorer.
        # Problem: when _hero_rec is overridden (e.g. GO→HOLD by compile errors),
        # _hero_conf updates but _approx_pct stays at the original low score.
        # Result: hero badge = HIGH, hero subtext = "Minor signal" (15%) → contradiction.
        # Fix: _approx_pct derives from _hero_conf, which IS updated on override.
        _approx_pct = int((_hero_conf or _build_score) * 100)

        # ── Hero subtext: use reasoning sentence when available ──────────────────
        # _rsn_key_sent is the actual sentence from the LLM's free-form analysis.
        # It's more informative than a generic "Minor signal" label.
        # Fallback to generic text only when reasoning produced nothing useful.
        if _rsn_key_sent and _hero_rec != "GO":
            # Trim to a readable length and strip trailing incomplete words
            _rsn_display = _rsn_key_sent[:200].rsplit(" ", 1)[0] if len(_rsn_key_sent) > 200 else _rsn_key_sent
            _hero_sub = _rsn_display
        elif _hero_rec == "GO":
            _hero_sub = (
                "No code issues found"
                if _primary_driver not in ("history", "code+history")
                else f"Code looks clean — but similar past commits failed. {_action_hint}."
            )
        elif _hero_rec == "HOLD" and _has_certain_compile_error:
            _hero_sub = f"Compilation error — this commit will fail the build"
        elif _hero_rec == "HOLD":
            _hero_sub = f"Code changes likely to cause issues — {_action_hint}"
        elif _approx_pct <= 35:
            _hero_sub = f"Minor signal — {_action_hint}"
        elif _approx_pct <= 60:
            _hero_sub = f"Code changes may cause issues — {_action_hint}"
        else:
            _hero_sub = f"Code changes likely to cause issues — {_action_hint}"

        # ── Summary cards — Build Test + Environment Readiness ────────────────
        if _hero_findings:
            _build_findings_html = (
                '<p class="ra-ui-section-label">Findings in this diff</p>'
                + "".join(
                    f'<p class="ra-ui-finding">› {_strip_risk_finding_prefix(f)}</p>'
                    for f in _hero_findings[:5]
                )
                + _migration_footnote
            )
        elif _build_findings and _migration_footnote:
            # All findings were LOW service changes — show just the footnote
            _build_findings_html = _migration_footnote
        else:
            _changed_types = []
            if _code_sig:
                _diff_text_check = st.session_state.get("risk_git_diff", "") or ""
                if "java" in _build_detail.lower() or ".java" in _diff_text_check.lower():
                    _changed_types.append("Java")
                if "pom" in _build_detail.lower():
                    _changed_types.append("pom.xml")
                if _code_sig.is_submodule_only:
                    _changed_types.append("submodule pointers")
            _checked_str = ", ".join(_changed_types) or "diff"
            _build_findings_html = (
                f'<p>✓ No critical patterns found — checked {_checked_str} for OSGi issues, '
                f'missing @Reference/@Service, reactor changes, and dependency conflicts.</p>'
            )

        # _build_fail_pct must be consistent with _hero_rec.
        # Since _approx_pct now derives from _hero_conf, use the same source here
        # so the displayed percentage always matches the badge level.
        _build_fail_pct = _approx_pct
        _build_icon_kind = {"GO": "check", "CAUTION": "warn", "HOLD": "x"}.get(_hero_rec, "build")

        # When HIGH/HOLD and basis contains the specific reason (e.g. compilation error),
        # show it prominently as the main body — not buried in tiny footnote font.
        _basis_is_specific = (
            _hero_rec in ("HOLD", "CAUTION")
            and any(k in (_hero_basis or "").lower() for k in (
                "exception", "loginexception", "compilation", "uncaught",
                "missing", "error", "conflict", "fails"
            ))
        )
        _basis_display = (
            f'<p class="ra-ui-action" style="color:{_hero_col}">{_hero_basis}</p>'
            if _basis_is_specific
            else f'<p class="ra-ui-footnote">Based on: {_hero_basis}</p>'
        )

        # Show reasoning as small text when not HOLD, not as huge headline
        _hero_sub_body = (
            f'<p style="font-size:0.82rem;color:{T["text_sub"]};margin:0 0 8px 0">{_hero_sub}</p>'
            if _hero_rec != "HOLD" and _hero_sub else ""
        )
        _build_card_body = (
            f'{_hero_sub_body}'
            f'{_build_findings_html}'
        )

        _env_card_verdict = "—"
        _env_card_col     = T["gray"]
        _env_card_icon_kind = "server"
        _env_fail_pct     = 0
        _env_card_sub     = "Based on recent pipeline history"
        _env_card_body    = (
            '<p>No environment data available. '
            'Run assessment with pipeline history to check env readiness.</p>'
        )
        if _env_sig:
            _env_status = _env_sig.status
            _env_col = {"READY": T["green"], "CAUTION": T["amber"], "NOT_READY": T["red"]}.get(
                _env_status, T["gray"]
            )
            _env_icon = {"READY": "✓", "CAUTION": "⚠", "NOT_READY": "✕"}.get(_env_status, "?")
            _consec = _env_sig.consecutive_failures
            _dom = _env_sig.dominant_step or "unknown"
            _last_ok = _env_sig.last_success_ago or "unknown"
            _window_fail_count = (
                r.get("_env_signal", {}).get("env_step_failure_count", 0)
                or getattr(_env_sig, "env_step_failure_count", 0)
                or 0
            )

            if _env_status == "READY":
                _env_headline = "Environment is healthy"
                _last_ok_str = f"Last success: {_last_ok}." if _last_ok and _last_ok != "unknown" else "No recent pipeline data available."
                _env_body = f"No recent failures detected. {_last_ok_str}"
                _env_action = ""
            elif _env_status == "CAUTION" and _consec == 0 and _window_fail_count > 0:
                _env_headline = (
                    f"{_window_fail_count} failures at {_dom} in recent window — last run passed"
                )
                _env_body = (
                    f"Last success: {_last_ok}. The pipeline has had {_window_fail_count} "
                    f"{_dom} failures recently. The last run passed, but the pattern may recur. "
                    f"This is not caused by this commit — it is an Adobe-managed environment issue."
                )
                _env_action = f"This step may fail. It is an infrastructure issue unrelated to your code."
            elif _env_status == "CAUTION" and _consec == 0:
                _env_headline = "Environment recently stable"
                _env_body = f"Last success: {_last_ok}. No recent failures in current window."
                _env_action = ""
            elif _env_status == "CAUTION":
                _env_headline = f"{_consec} recent failure{'s' if _consec != 1 else ''} at {_dom}"
                _env_body = (
                    f"Last success: {_last_ok}. Recent {_dom} failures are infrastructure-level — "
                    f"not caused by this commit. This is an Adobe-managed environment issue."
                )
                _env_action = f"This step may fail again. It is not related to your code changes."
            else:
                # Guard: NOT_READY with 0 consecutive failures or unknown step is
                # inconsistent data (serialization error or stale cache). Show as healthy.
                if _consec == 0 or _dom == "unknown":
                    _env_headline = "Environment is healthy"
                    _env_body = f"No recent failures detected. Last success: {_last_ok}."
                    _env_action = ""
                    _env_status = "READY"
                    _env_col  = T["green"]
                    _env_bg   = "#EDFAF3"
                    _env_icon = "✓"
                else:
                    _env_headline = f"{_consec} consecutive failures at {_dom}"
                    _env_body = (
                        f"Last success: {_last_ok}. Every recent pipeline has failed at {_dom}. "
                        f"This is an Adobe-managed infrastructure issue — "
                        f"not caused by any code change in this commit. "
                        f"Your code risk is assessed separately in Build Test above."
                    )
                    _env_action = f"This step will likely fail again. Raise with Adobe Support if this has been ongoing for more than a few days."

            if _env_status == "CAUTION" and _consec == 0 and _window_fail_count > 0:
                _data_note_count = (
                    f"{_window_fail_count} {_dom} failures in recent window · last run passed"
                )
            elif _consec > 0:
                _data_note_count = f"{_consec} consecutive failure{'s' if _consec != 1 else ''}"
            else:
                _data_note_count = "recent pipeline history"

            _env_risk_lvl = (
                "HIGH"
                if (_env_status == "NOT_READY" or _consec >= 3 or _window_fail_count >= 3)
                else "MEDIUM"
                if (_env_status == "CAUTION" and (_consec > 0 or _window_fail_count > 0))
                else "LOW"
            )
            _env_risk_label = {
                "HIGH":   "HIGH",
                "MEDIUM": "MEDIUM",
                "LOW":    "LOW",
            }.get(_env_risk_lvl, "")
            # Compute failure probability from window data when consecutive=0.
            # failure_probability=0.05 (the default for consecutive=0) is wrong when
            # the window shows many failures — 10/10 runs failed = 60% probability not 5%.
            if _consec == 0 and _window_fail_count > 0:
                _window_rate = _window_fail_count / 10.0
                _env_fail_pct = int(max(0.15, min(_window_rate * 0.6, 0.80)) * 100)
            else:
                _env_fail_pct = int((_env_sig.failure_probability or 0) * 100)
            if _env_risk_lvl == "HIGH":
                _env_card_icon_kind = "x"
            elif _env_risk_lvl == "MEDIUM":
                _env_card_icon_kind = "warn"
            else:
                _env_card_icon_kind = "check"

            _env_card_verdict = _env_headline   # headline = descriptive text, not HIGH/MEDIUM/LOW
            _env_card_col = _env_col
            _env_card_body = (
                f'<p class="ra-ui-sub">{_env_card_sub}</p>'
                f'<p>{_env_body}</p>'
                + (
                    f'<p class="ra-ui-action" style="color:{_env_col}">→ {_env_action}</p>'
                    if _env_action and _env_risk_lvl != "LOW"
                    else ""
                )
                + f'<p class="ra-ui-footnote">{_data_note_count} · Splunk pipeline window</p>'
            )

        # ── Historical matches (carousel rendered at end of page) ────────────
        _hist_hits = []
        try:
            from vector_store.store import find_similar_failures
            import re as _re_hist
            _mods = r.get("modules_at_risk", [])
            _q_signal = " ".join(_mods) + " " + r.get("change_intent", "")
            _target_pipeline = pipeline_df["pipelineName"].mode()[0] if not pipeline_df.empty else ""
            _env_sig_raw = r.get("_env_signal") or {}
            _changed_files_hist = st.session_state.get("risk_git_changed_files", []) or []
            _has_disp_hist = any(
                k in " ".join(_changed_files_hist).lower()
                for k in ("dispatcher", ".any", ".vhost", ".farm", "ui.config", "security",
                          "auth", "acl", "oauth", "crxde")
            )
            _skip_sec_hist = not _has_disp_hist
            _query_step = r.get("most_likely_failure_step", "build")
            if _skip_sec_hist and _query_step == "securityTest":
                _query_step = "build"
            _hist_tenant = _active_customer.get("tenant_id", "") or _active_customer.get("program_id", "")
            _hist_hits = find_similar_failures(
                error_type=_query_step,
                error_message=_q_signal,
                key_lines=_mods,
                step="" if _skip_sec_hist else _query_step,
                top_k=6,
                pipeline=_target_pipeline,
                tenant_id=_hist_tenant,
            )
            if _skip_sec_hist:
                _hist_hits = [h for h in _hist_hits if h.get("step", "") != "securityTest"]
        except Exception:
            pass

        if _hist_hits:
            from difflib import SequenceMatcher
            import re as _re_dedup

            def _text_sim(a: str, b: str) -> float:
                return SequenceMatcher(None, a.lower()[:300], b.lower()[:300]).ratio()

            def _fingerprint(h: dict) -> str:
                text = (h.get("root_cause") or "") + " " + (h.get("step") or "")
                words = _re_dedup.findall(r'\b[a-z]{5,}\b', text.lower())
                top = sorted(set(words), key=words.count, reverse=True)[:6]
                return " ".join(sorted(top))

            _deduped = []
            for _h in sorted(_hist_hits, key=lambda x: x.get("similarity_score", 0), reverse=True):
                _rc = (_h.get("root_cause") or "").strip()
                _stp = (_h.get("step") or "").strip()
                _fp = _fingerprint(_h)
                _dup = any(
                    (_stp == (d.get("step") or "").strip()) and (
                        _text_sim(_rc, d.get("root_cause") or "") > 0.55 or
                        _text_sim(_fp, _fingerprint(d)) > 0.75
                    )
                    for d in _deduped
                )
                if not _dup:
                    _deduped.append(_h)
            _hist_hits = _deduped[:5]

            if _hist_hits and (_hist_sig is None or _hist_sig.score == 0.0):
                try:
                    from analysis.risk_scorer import HistoricalSignal as _HS
                    _scores = [h.get("similarity_score", 0) for h in _hist_hits]
                    _avg_s = sum(_scores) / len(_scores) if _scores else 0
                    _dom = max(
                        {h.get("step", ""): 0 for h in _hist_hits},
                        key=lambda s: sum(1 for h in _hist_hits if h.get("step") == s),
                        default="",
                    )
                    _hist_sig = _HS(
                        score=_avg_s, match_count=len(_hist_hits),
                        dominant_step=_dom,
                        fail_rate=_avg_s * 0.9,
                        detail=f"{len(_hist_hits)} past incidents matched — avg {int(_avg_s*100)}% similarity",
                        examples=_hist_hits[:3],
                    )
                except Exception:
                    pass

        # _env_fill_pct and _env_risk_lvl are set inside `if _env_sig:` — ensure defaults
        try:
            _env_fill_pct
        except NameError:
            _env_fill_pct = 0
        try:
            _env_risk_lvl
        except NameError:
            _env_risk_lvl = "LOW"

        # ── Pipeline Risk banner — overall verdict before step-specific cards ──
        # This is the most prominent statement. It answers: "will this pipeline fail?"
        # Step-specific cards below answer: "where and why?"
        # Keeping the top verdict pipeline-level means it stays correct even when
        # Argus predicts the wrong step — direction accuracy is ~70%, step ~35%.
        _overall_risk_pct = max(_build_fail_pct, _env_fill_pct)
        _overall_risk_lvl  = (
            "HIGH"   if _hero_rec in ("HOLD",) or _env_risk_lvl == "HIGH"
            else "MEDIUM" if _hero_rec == "CAUTION" or _env_risk_lvl == "MEDIUM"
            else "LOW"
        )
        _overall_col  = {"HIGH": T["red"], "MEDIUM": T["amber"], "LOW": T["green"]}.get(_overall_risk_lvl, T["gray"])
        _overall_icon = {"HIGH": "✕", "MEDIUM": "⚠", "LOW": "✓"}.get(_overall_risk_lvl, "?")
        _overall_text = {
            "HIGH":   "This commit has a HIGH chance of causing a pipeline failure",
            "MEDIUM": "This commit may cause a pipeline failure — verify before promoting",
            "LOW":    "This commit looks safe to promote",
        }.get(_overall_risk_lvl, "")

        # ── NEW: Top Verdict Banner ────────────────────────────────────────────
        # Determine banner state: env-only issue takes precedence for special framing
        _is_env_only_issue = (
            _primary_driver in _env_only_drivers
            and _hero_rec == "GO"
            and _env_sig is not None
            and getattr(_env_sig, "status", "READY") not in ("READY",)
        )
        if _is_env_only_issue:
            _verdict_icon  = "🔁"
            _verdict_title = "PIPELINE UNSTABLE — NOT YOUR CODE"
            _verdict_color = "#4A6FA5"
            _verdict_bg    = "#EEF3FE"
            _verdict_border = "#C0D2FA"
        elif _hero_rec == "HOLD":
            _verdict_icon  = "🔴"
            _verdict_title = "DO NOT PROMOTE"
            _verdict_color = T["red"]
            _verdict_bg    = "#FEF0F0"
            _verdict_border = "#FBCECE"
        elif _hero_rec == "CAUTION":
            _verdict_icon  = "⚠️"
            _verdict_title = "REVIEW BEFORE PROMOTING"
            _verdict_color = T["amber"]
            _verdict_bg    = "#FEFAE8"
            _verdict_border = "#F5E0A0"
        else:
            _verdict_icon  = "✅"
            _verdict_title = "SAFE TO PROMOTE"
            _verdict_color = T["green"]
            _verdict_bg    = "#EDFAF3"
            _verdict_border = "#BDECD3"

        st.markdown(
            f'<div class="risk-verdict-banner" style="border:2px solid {_verdict_border};border-radius:10px;'
            f'padding:12px 18px;margin-bottom:14px;background:{_verdict_bg}">'
            f'<p style="font-size:14px;font-weight:700;color:{_verdict_color};margin:0">'
            f'{_verdict_icon}&nbsp;&nbsp;{_verdict_title}</p>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── Existing hero cards (now secondary detail) ────────────────────────
        _env_card_title = (
            "Pipeline History (not caused by this commit)"
            if _primary_driver in _env_only_drivers
            else "Pipeline History"
        )
        # Rewrite env card body wording: replace "environment broken" language
        # and add disclaimer about data source
        if _env_sig:
            _env_disclaimer = (
                '<p class="ra-ui-footnote" style="margin-top:8px;font-style:italic">'
                'Argus reads Splunk pipeline run history — not server metrics or infrastructure state.</p>'
            )
            _consec_display = getattr(_env_sig, "consecutive_failures", 0)
            _last_ok_display = getattr(_env_sig, "last_success_ago", "unknown")
            _dom_display = getattr(_env_sig, "dominant_step", "unknown")
            _pipeline_history_extra = (
                f'<p class="ra-ui-sub" style="margin-top:6px">'
                f'Consecutive failures: <strong>{_consec_display}</strong> · '
                f'Last success: <strong>{_last_ok_display}</strong> · '
                f'Dominant step: <strong>{_dom_display}</strong>'
                f'</p>'
            )
            # Replace "Environment broken" language in body
            _env_card_body_display = _env_card_body.replace(
                "Environment broken", "Pipeline showing repeated failures"
            ).replace(
                "environment broken", "pipeline showing repeated failures"
            )
            _env_card_body_display = _env_card_body_display + _pipeline_history_extra + _env_disclaimer
        else:
            _env_card_body_display = _env_card_body

        st.markdown(
            '<div class="ra-hero-stack">'
            + risk_summary_card_html(
                title="Code Analysis",
                percent_label=f"{_build_fail_pct}% confidence",
                percent_color="#9CA3AF",
                icon_bg=_hero_col,
                headline=_hero_sub if _hero_rec == "HOLD" else "",
                body_html=_build_card_body,
                icon_kind=_build_icon_kind,
                compact=False,
            )
            + risk_summary_card_html(
                title=_env_card_title,
                percent_label=f"{_env_fail_pct}%",
                percent_color=_env_card_col,
                icon_bg=_env_card_col,
                headline=_env_card_verdict,
                body_html=_env_card_body_display,
                icon_kind=_env_card_icon_kind,
                compact=True,
            )
            + "</div>",
            unsafe_allow_html=True,
        )

        # ── NEW: What Will Fail (HOLD only) ───────────────────────────────────
        if _hero_rec == "HOLD":
            _high_findings = [
                f for f in _build_findings
                if f.startswith("[HIGH]") or f.startswith("[CERTAIN]")
            ]
            _fail_step_display = r.get("most_likely_failure_step", "build")
            _first_cause = ""
            if _high_findings:
                import re as _re_wf
                _wf_m = _re_wf.sub(r'^\[(?:HIGH|CERTAIN)\]\s*', '', _high_findings[0])
                _first_cause = _wf_m.strip()
            _rec_actions_wf = r.get("recommended_actions", [])
            _fix_text = _rec_actions_wf[0] if _rec_actions_wf else ""

            _findings_bullets = "".join(
                f'<li style="font-size:0.83rem;color:{T["text"]};line-height:1.55;margin-bottom:4px">'
                f'{_strip_risk_finding_prefix(f)}</li>'
                for f in _high_findings
            )
            _fix_html = (
                f'<p style="font-size:0.83rem;color:#333;margin:8px 0 0 0">'
                f'<strong>Fix:</strong> {_fix_text}</p>'
                if _fix_text else ""
            )
            st.markdown(
                f'<div style="border:1px solid #FBCECE;border-radius:8px;'
                f'padding:14px 18px;margin-bottom:12px;background:#FEF0F0">'
                f'<p style="font-size:13px;font-weight:700;color:{T["red"]};margin:0 0 8px 0">'
                f'🔴 WHAT WILL FAIL</p>'
                f'<p style="font-size:0.83rem;color:#333;margin:0 0 4px 0">'
                f'<strong>Step:</strong> {_fail_step_display}</p>'
                + (f'<p style="font-size:0.83rem;color:#333;margin:0 0 8px 0">'
                   f'<strong>Cause:</strong> {_first_cause}</p>' if _first_cause else "")
                + (f'<p style="font-size:0.78rem;font-weight:700;color:{T["red"]};'
                   f'margin:8px 0 4px 0">Findings:</p>'
                   f'<ul style="margin:0;padding-left:1.25rem">{_findings_bullets}</ul>'
                   if _findings_bullets else "")
                + _fix_html
                + f'</div>',
                unsafe_allow_html=True,
            )

        # ── NEW: What to Watch (CAUTION or HOLD) ─────────────────────────────
        if _hero_rec in ("CAUTION", "HOLD"):
            _step_risks_all = r.get("step_risks", [])
            _fail_step_skip = r.get("most_likely_failure_step", "") if _hero_rec == "HOLD" else ""
            _watch_steps = [
                s for s in _step_risks_all
                if s.get("level", "Low").upper() in ("MEDIUM", "LOW", "HIGH")
                and s.get("step", "") != _fail_step_skip
                and s.get("step", "") != "build"  # build is shown in What Will Fail
            ]
            # Sort: Medium first, then Low
            _watch_steps.sort(key=lambda s: {"High": 0, "Medium": 1, "Low": 2}.get(s.get("level", "Low"), 2))
            if _watch_steps:
                _watch_rows = "".join(
                    f'<div style="padding:6px 0;border-bottom:1px solid #F5E0A0">'
                    f'<span style="font-size:0.83rem;font-weight:600;color:#333">'
                    f'{s.get("step","?")} — {s.get("level","?")}</span>'
                    + (f'<br><span style="font-size:0.78rem;color:#555;line-height:1.4">'
                       f'{s.get("rationale","")}</span>' if s.get("rationale") else "")
                    + f'</div>'
                    for s in _watch_steps
                )
                st.markdown(
                    f'<div style="border:1px solid #F5E0A0;border-radius:8px;'
                    f'padding:14px 18px;margin-bottom:12px;background:#FEFAE8">'
                    f'<p style="font-size:13px;font-weight:700;color:{T["amber"]};margin:0 0 8px 0">'
                    f'⚠️ WHAT TO WATCH</p>'
                    + _watch_rows
                    + f'</div>',
                    unsafe_allow_html=True,
                )

        # Keep for downstream sections (blast radius, affected modules etc.)
        _fail_step  = r.get("most_likely_failure_step", "—")
        _br_scope   = (r.get("blast_radius_analysis") or {}).get("deployment_scope", "—")
        _scope_col  = {"isolated": T["green"], "service-wide": T["amber"], "platform-wide": T["red"]}.get(_br_scope, T["gray"])
        _scope_bg   = {"isolated": "#EDFAF3",  "service-wide": "#FEFAE8",  "platform-wide": "#FEF0F0"}.get(_br_scope, "#F4F4F8")

        # ─────────────────────────────────────────────────────────────────────
        # DEEP DIVE — AI reasoning, modules, step risks, hypotheses
        # Lower confidence than build hero above — LLM-generated content.
        # ─────────────────────────────────────────────────────────────────────
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px;margin:16px 0 8px 0">'
            f'<div style="flex:1;height:1px;background:{T["border"]}"></div>'
            f'<span style="font-size:0.62rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:0.12em;color:{T["text_muted"]};white-space:nowrap">'
            f'TECHNICAL DETAILS &amp; AI REASONING · Lower confidence</span>'
            f'<div style="flex:1;height:1px;background:{T["border"]}"></div>'
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

        # ── Recommended Actions ───────────────────────────────────────────────
        _rec_actions = r.get("recommended_actions", [])
        if _rec_actions:
            with content_card():
                section_label("Recommended Actions")
                action_list(_rec_actions)

        # ── Technical Details ─────────────────────────────────────────────────
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        section_label("Why Argus thinks this · Details", dark=True)

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

                # Derive whether commit has security-relevant files (for override below)
                _sr_files = st.session_state.get("risk_git_changed_files", []) or []
                _sr_has_security = any(
                    k in " ".join(_sr_files).lower()
                    for k in ("dispatcher", ".any", ".vhost", ".farm", "ui.config",
                              "security", "auth", "acl", "oauth", "crxde")
                )
                # Env status for the step risk override
                _sr_env_sig   = r.get("_env_signal") or {}
                _sr_env_ready = _sr_env_sig.get("status", "UNKNOWN") == "READY"
                _sr_env_dom   = _sr_env_sig.get("dominant_step", "")
                _sr_env_consec = _sr_env_sig.get("consecutive_failures", 0)
                _sr_env_broken = _sr_env_sig.get("status", "UNKNOWN") in ("NOT_READY", "CAUTION")

                # Map hero verdict to max allowed step risk level
                # If hero says CAUTION, no step should show Higher than Medium
                # This prevents hero=CAUTION + step_summary=High contradictions
                _hero_max_level = {
                    "GO":      "Low",
                    "CAUTION": "Medium",
                    "HOLD":    "High",
                }.get(_hero_rec, "High")
                _level_order = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}

                for _sr in _step_risks:
                    _step_name = _sr.get("step", "")
                    _slv = _sr.get("level", "Low")
                    _sr_rationale = _sr.get("rationale", "")

                    # Cap build step at hero verdict — prevents 12%/CAUTION hero + High step summary
                    _sr_override_note = ""
                    if _step_name == "build" and _level_order.get(_slv, 0) > _level_order.get(_hero_max_level, 2):
                        _slv = _hero_max_level
                        _sr_override_note = f" [capped to match hero verdict: {_hero_rec}]"

                    # Cap securityTest at Medium when env is READY and no security-relevant
                    # files changed — LLM may have used miscalibrated rule_scores to set HIGH
                    if _step_name == "securityTest" and _slv == "High" and _sr_env_ready and not _sr_has_security:
                        _slv = "Low"
                        _sr_override_note = " [env healthy + no dispatcher/security changes — override to Low]"

                    # Cap loadTest HIGH → MEDIUM unless structural evidence exists.
                    # LLM pattern-matches "Java 21 + large diff" → loadTest High (~40% accuracy).
                    # Only allow High when scorer found dao_migration_perf_risk or java_upgrade_pending.
                    # These are the structural signals that actually indicate loadTest risk.
                    if _step_name in ("loadTest", "reportPerformanceTest") and _slv == "High":
                        _has_perf_structural = any(
                            k in " ".join(_sr_files).lower() or
                            k in (_sr.get("rationale","") or "").lower()
                            for k in ("dao_migration_perf_risk", "java_upgrade_perf_risk",
                                      "java upgrade pending", "lts migration", "dao migration")
                        )
                        # Also check if code_caused_perf flag was set by scorer
                        _code_caused_perf = (r.get("_code_signal") or {}).get("code_caused_perf", False)
                        if not _has_perf_structural and not _code_caused_perf:
                            _slv = "Medium"
                            _sr_override_note = " [capped: no structural DAO/perf finding — LLM loadTest High requires evidence]"

                    # Require ChromaDB similarity ≥0.65 for loadTest MEDIUM from history alone.
                    # Lower similarity matches are too noisy for performance step predictions.
                    if _step_name in ("loadTest",) and _slv == "Medium":
                        _hist_dom = (_hist_sig.dominant_step if _hist_sig else "")
                        _hist_score = (_hist_sig.score if _hist_sig else 0.0)
                        if _hist_dom == "loadTest" and _hist_score < 0.65 and not _code_caused_perf:
                            _slv = "Low"
                            _sr_override_note = " [history similarity <65% — insufficient evidence for loadTest Medium]"

                    # When env is broken at this step, add a clear note so developer
                    # understands "Low" means "commit didn't cause it" not "step will pass"
                    _env_broken_note = ""
                    if (
                        _step_name == _sr_env_dom
                        and _sr_env_broken
                        and _sr_env_consec >= 1
                        and _slv in ("Low", "Medium")
                    ):
                        _env_broken_note = (
                            f' ⚠ Note: environment currently broken at {_step_name} '
                            f'({_sr_env_consec} consecutive failures — see Environment Health above). '
                            f'Pipeline will fail here regardless of this commit.'
                        )

                    _scc = _slv_col.get(_slv, T["gray"])
                    _scb = _slv_bg.get(_slv, "#F4F4F8")
                    _scbr = _slv_brd.get(_slv, "#D8D8E8")
                    st.markdown(
                        f'<div style="display:flex;align-items:flex-start;gap:0.75rem;'
                        f'padding:0.5rem 0;border-bottom:1px solid {T["border2"]}">'
                        f'<span style="font-size:0.78rem;font-weight:700;color:{T["text_sub"]};'
                        f'width:110px;flex-shrink:0;padding-top:2px">{_step_name}</span>'
                        f'<span style="background:{_scb};color:{_scc};border:1px solid {_scbr};'
                        f'padding:2px 8px;border-radius:4px;font-size:0.68rem;font-weight:700;'
                        f'flex-shrink:0;white-space:nowrap">{_slv}</span>'
                        f'<span style="font-size:0.8rem;color:{T["text_sub"]};line-height:1.5">'
                        f'{_sr_rationale}{_sr_override_note}'
                        + (f'<span style="color:{T["amber"]};font-weight:600">{_env_broken_note}</span>' if _env_broken_note else "")
                        + f'</span>'
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
            with content_card():
                section_label("Technical Rationale")
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
        st.markdown(historical_matches_carousel_html(_hist_hits, T), unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════
# PAGE 4 — FAILURE PINPOINT
# ═══════════════════════════════════════════════════════════
elif page == "Post-Failure Diagnosis":
    section_header("Post-Failure Diagnosis", "Identify the exact file and line responsible for a pipeline failure")

    with st.spinner("Loading execution data..."):
        try:
            pipeline_df, failed_df, share_map, _data_source = get_data_or_stop()
        except Exception as e:
            st.error(f"Could not load data: {e}")
            st.stop()

    # Share names are needed to fetch logs — if cache was built without them, fetch now
    if not share_map:
        try:
            from connectors.splunk_connector import fetch_share_names
            _pid_fp = _active_customer.get("program_id", "")
            if _pid_fp:
                with st.spinner("Fetching Azure log locations…"):
                    _sn = fetch_share_names(int(_pid_fp))
                    share_map = {str(k): str(v) for k, v in _sn.items()}
        except Exception:
            pass

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
            _pinpoint_df = failed_df.drop_duplicates("executionId").sort_values(
                "Deploy Start Time", ascending=False
            )
            for _pp_idx, (_, _row) in enumerate(_pinpoint_df.head(15).iterrows()):
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
                        key=f"_pp_{_pp_idx}_{_eid}",
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

    _auto_eid = st.session_state.get("pinpoint_exec_input", "")

    # Auto-run pinpoint when an execution is selected
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
                # Auto-trigger log analysis immediately after pinpoint
                st.session_state["post_failure_eid"]  = _auto_eid
                st.session_state.pop("post_failure_md", None)
                st.session_state.pop("post_failure_report", None)
                st.rerun()
            except Exception as e:
                st.error(f"Pinpoint failed: {e}")

    # Auto-run log analysis when execution is selected and pinpoint is done
    _pf_eid = st.session_state.get("post_failure_eid", "")
    if _pf_eid and _pf_eid == _auto_eid and "post_failure_report" not in st.session_state:
        with st.spinner(f"Fetching build log for {_pf_eid}..."):
            try:
                from analysis.post_failure_assessor import assess_failed_execution
                _pf_report, _pf_md = assess_failed_execution(
                    _pf_eid,
                    use_llm=True,
                    use_reranker=False,
                    pipeline_df=pipeline_df,
                    failed_df=failed_df,
                    share_map=share_map,
                )
                _pf_dict = (
                    _pf_report.model_dump()
                    if hasattr(_pf_report, "model_dump")
                    else _pf_report
                )
                # Check if log was unavailable — show error instead of storing fake analysis
                if isinstance(_pf_dict, dict) and _pf_dict.get("log_unavailable"):
                    st.markdown(
                        f'<div style="border:1px solid #F59E0B;border-left:4px solid #F59E0B;'
                        f'border-radius:8px;padding:12px 16px;margin:8px 0;background:#FFFBEB">'
                        f'<p style="font-weight:700;color:#92400E;margin:0 0 4px 0">⚠ Log Unavailable</p>'
                        f'<p style="font-size:13px;color:#78350F;margin:0">'
                        f'{_pf_dict.get("log_error","Log could not be fetched.")}</p>'
                        f'<p style="font-size:12px;color:#92400E;margin:6px 0 0 0">'
                        f'Check Cloud Manager → Pipelines → execution {_pf_eid} → View Log directly.</p>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.session_state["post_failure_md"] = _pf_md
                    st.session_state["post_failure_report"] = _pf_dict
                    st.rerun()
            except Exception as _pf_err:
                st.markdown(
                    f'<div style="border:1px solid #F59E0B;border-left:4px solid #F59E0B;'
                    f'border-radius:8px;padding:12px 16px;margin:8px 0;background:#FFFBEB">'
                    f'<p style="font-weight:700;color:#92400E;margin:0 0 4px 0">⚠ Log Analysis Failed</p>'
                    f'<p style="font-size:13px;color:#78350F;margin:0">{str(_pf_err)[:200]}</p>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

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

        # ── Log analysis summary (shown automatically after pinpoint) ──────────
        if "post_failure_report" in st.session_state:
            _pfr = st.session_state["post_failure_report"]
            _rl = _pfr.get("risk_level", "")
            _retry = _pfr.get("retry_recommendation", "")
            _summary = _pfr.get("root_cause_summary", "")
            _fix_steps = _pfr.get("fix_steps", [])
            _rl_col = {"Critical": T["red"], "High": T["red"], "Medium": T["amber"], "Low": T["green"]}.get(_rl, T["blue"])

            if _summary or _fix_steps:
                st.markdown(
                    f'<div style="background:{T["surface2"]};border-left:3px solid {_rl_col};'
                    f'border-radius:4px;padding:12px 16px;margin-top:12px">'
                    f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">'
                    f'<p style="font-size:0.68rem;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:{T["text_muted"]};margin:0">Log Analysis</p>'
                    + (f'<span style="font-size:0.7rem;font-weight:700;color:{_rl_col};'
                       f'background:{_rl_col}15;padding:1px 8px;border-radius:10px">{_rl}</span>'
                       if _rl else "")
                    + (f'<span style="font-size:0.7rem;color:{T["text_muted"]}">'
                       f'Retry: <strong>{_retry}</strong></span>'
                       if _retry else "")
                    + f'</div>'
                    + (f'<p style="font-size:0.82rem;color:{T["text"]};line-height:1.55;margin:0 0 8px 0">'
                       f'{_summary}</p>' if _summary else "")
                    + ("".join(
                        f'<p style="font-size:0.8rem;color:{T["text_sub"]};margin:2px 0">'
                        f'{i}. {s}</p>'
                        for i, s in enumerate(_fix_steps[:4], 1)
                    ) if _fix_steps else "")
                    + f'</div>',
                    unsafe_allow_html=True,
                )


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
            df["doc_preview"] = [d for d in docs]  # full text shown in expander

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

                _is_dark_mode = st.session_state.get("ui_theme", "light") == "dark"

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
                        hoverlabel=dict(
                            bgcolor="#1E1E1E" if _is_dark_mode else "#FFFFFF",
                            bordercolor="#444" if _is_dark_mode else "#E5E7EB",
                            font=dict(color="#FFFFFF" if _is_dark_mode else "#111827", size=11),
                        ),
                    ))

                # Hover tooltip: black text on white in light mode, white on dark in dark mode
                fig_scatter.update_layout(hoverlabel=dict(
                    bgcolor="#1E1E1E" if _is_dark_mode else "#FFFFFF",
                    font_color="#FFFFFF" if _is_dark_mode else "#111827",
                    bordercolor="#444" if _is_dark_mode else "#E5E7EB",
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
                        # ── Security signals ──
                        _sec_flags = [f for f in ("crxde_active","davex_active","webdav_active",
                                       "dispatcher_config","referrer_filter","replication_admin")
                                      if str(row.get(f,"")) == "1"]
                        if _sec_flags:
                            st.markdown(
                                f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                                f'letter-spacing:0.07em;color:{T["red"]};margin:0 0 0.2rem 0">'
                                f'Security Failures</p>'
                                f'<p style="font-size:0.82rem;color:{T["text"]};margin:0 0 0.75rem 0">'
                                f'{" · ".join(_sec_flags)}</p>',
                                unsafe_allow_html=True,
                            )

                        # ── Build error ──
                        if row.get("build_error_type"):
                            st.markdown(
                                f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                                f'letter-spacing:0.07em;color:{T["text_muted"]};margin:0 0 0.2rem 0">'
                                f'Build Error</p>'
                                f'<p style="font-size:0.82rem;color:{T["text"]};margin:0 0 0.75rem 0">'
                                f'{row.get("build_error_type","")} '
                                f'{"· module: " + str(row["failing_module"]) if row.get("failing_module") and str(row.get("failing_module","")) not in ("","nan") else ""}</p>',
                                unsafe_allow_html=True,
                            )

                        # ── Error message ──
                        if row.get("error_message"):
                            st.markdown(
                                f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                                f'letter-spacing:0.07em;color:{T["text_muted"]};margin:0 0 0.2rem 0">'
                                f'Error Message</p>'
                                f'<p style="font-size:0.82rem;color:{T["text_sub"]};margin:0 0 0.75rem 0">'
                                f'{row.get("error_message","")}</p>',
                                unsafe_allow_html=True,
                            )

                        # ── Git context ──
                        _git_parts = []
                        if row.get("modules_touched"): _git_parts.append(f"modules: {row['modules_touched']}")
                        if str(row.get("has_pom_change","")) == "1": _git_parts.append("pom.xml changed")
                        if str(row.get("has_dispatcher","")) == "1": _git_parts.append("dispatcher changed")
                        if str(row.get("has_config_change","")) == "1": _git_parts.append("config changed")
                        if row.get("commit_sha"): _git_parts.append(f"SHA: {row['commit_sha']}")
                        if _git_parts:
                            st.markdown(
                                f'<p style="font-size:0.72rem;font-weight:700;text-transform:uppercase;'
                                f'letter-spacing:0.07em;color:{T["text_muted"]};margin:0 0 0.2rem 0">'
                                f'Git Context</p>'
                                f'<p style="font-size:0.82rem;color:{T["text_sub"]};margin:0 0 0.75rem 0">'
                                f'{" · ".join(_git_parts)}</p>',
                                unsafe_allow_html=True,
                            )

                        # ── Source + full embedded text ──
                        _src = row.get("source","unknown")
                        _src_col = T["green"] if _src == "live_log" else T["amber"]
                        st.markdown(
                            f'<span style="font-size:0.68rem;background:{_src_col}18;color:{_src_col};'
                            f'border:1px solid {_src_col}44;padding:1px 7px;border-radius:10px">'
                            f'source: {_src}</span>',
                            unsafe_allow_html=True,
                        )
                        with st.expander("Full embedded text (what the model sees)"):
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


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: Customer Onboarding
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "Repo Settings":
    section_header("Customer Onboarding", "Add or configure a customer — all settings saved securely on the server")

    if _IS_DARK:
        st.markdown(
            "<style>"
            "[data-testid='stTextInput'] label,"
            "[data-testid='stTextInput'] input,"
            "[data-testid='stTextInput'] [data-baseweb='input'],"
            "[data-testid='stNumberInput'] input,"
            "[data-testid='stTextArea'] textarea {"
            "color:#0F172A !important;"
            "-webkit-text-fill-color:#0F172A !important;"
            "}"
            "[data-testid='stSelectbox'] label,"
            "[data-testid='stSelectbox'] > div > div,"
            "[data-testid='stSelectbox'] [data-baseweb='select'],"
            "[data-testid='stSelectbox'] [data-baseweb='select'] span,"
            "[data-testid='stSelectbox'] [data-baseweb='select'] div[role='button'] {"
            "color:#FFFFFF !important;"
            "-webkit-text-fill-color:#FFFFFF !important;"
            "}"
            "[data-testid='stTextInput'] input::placeholder,"
            "[data-testid='stTextInput'] [data-baseweb='input']::placeholder,"
            "[data-testid='stTextArea'] textarea::placeholder {"
            "color:#000000 !important;"
            "-webkit-text-fill-color:#000000 !important;"
            "opacity:1 !important;"
            "}"
            "</style>",
            unsafe_allow_html=True,
        )

    st.caption(
        "Passwords are stored in `data/.secrets.json` (gitignored, server-only). "
        "Customer config saved to `data/customer_config.json`. "
        "After saving, the customer appears in the sidebar selector immediately."
    )

    # ── Tab: Add New / Edit Existing ─────────────────────────────────────────
    _existing_names = list(_load_customers().keys())
    _ob_tab, _list_tab = st.tabs(["Add / Edit Customer", "All Customers"])

    with _ob_tab:
        # Pre-fill from existing customer if editing
        _edit_mode = st.selectbox(
            "Edit existing or add new",
            ["— Add new customer —"] + _existing_names,
            key="onboard_edit_sel"
        )
        _prefill = {}
        _prefill_pwd = ""
        if _edit_mode != "— Add new customer —":
            _prefill = _load_customers().get(_edit_mode, {})
            _secrets_data = json.loads(_SECRETS_PATH.read_text()) if _SECRETS_PATH.exists() else {}
            _prefill_pwd = _secrets_data.get(_edit_mode, {}).get("git_password", "")

        with st.form("customer_onboard_form", clear_on_submit=False):
            st.markdown("#### Customer Info")
            _cust_name    = st.text_input("Customer name", value=_edit_mode if _edit_mode != "— Add new customer —" else "", placeholder="e.g. Bajaj Allianz")
            _cust_short   = _prefill.get("short", "")
            _oc3, _oc4    = st.columns(2)
            _prog_id      = _oc3.text_input("Adobe CM Program ID", value=_prefill.get("program_id",""), placeholder="e.g. 19905")
            _org_id       = _oc4.text_input("Adobe Org ID", value=_prefill.get("org_id",""), placeholder="e.g. XXXXXX@AdobeOrg")
            _oc5, _oc6    = st.columns(2)
            _pip_prod     = _oc5.text_input("Production Pipeline ID", value=_prefill.get("pipeline_prod",""), placeholder="e.g. 2357452")
            _pip_dev      = _oc6.text_input("Dev Pipeline ID", value=_prefill.get("pipeline_dev",""), placeholder="e.g. 47202398")

            st.markdown("#### Git Repository")
            st.caption("Find these in Adobe Cloud Manager → Your Program → Repositories")
            _gc1, _gc2    = st.columns(2)
            _git_url      = _gc1.text_input("Git URL", value=_prefill.get("git_url",""), placeholder="https://git.cloudmanager.adobe.com/org/repo/")
            _git_branch   = _gc2.text_input("Deploy Branch", value=_prefill.get("git_branch","master"), placeholder="master or stage_and_prod")
            _gc3, _gc4    = st.columns(2)
            _git_user     = _gc3.text_input("Git Username", value=_prefill.get("git_username",""), placeholder="vanssharma-adobe-com")
            _git_pwd      = _gc4.text_input("Git Password", value=_prefill_pwd, type="password",
                                             placeholder="Generate in CM → Repositories → Generate password",
                                             help="Stored in data/.secrets.json — never committed to git")
            # Repo path — optional: leave blank to auto-generate, or point to existing clone
            _default_repo_name = _prefill.get("git_url","").rstrip("/").split("/")[-1] if _prefill.get("git_url") else ""
            _default_path = _prefill.get("git_local_dir","") or (
                f"{os.getenv('REPOS_BASE_DIR', str(Path.home() / 'projects'))}/{_default_repo_name}"
                if _default_repo_name else ""
            )
            _repo_already_exists = bool(_default_path and (Path(_default_path) / ".git").exists())
            _git_local = st.text_input(
                "Local repo path (on this server)",
                value=_default_path,
                placeholder=f"{os.getenv('REPOS_BASE_DIR', '/opt/repos')}/repo-name",
                help=(
                    "Path where the repo will be cloned ON THIS SERVER (not your laptop). "
                    "Leave as-is — Argus auto-generates it from REPOS_BASE_DIR. "
                    "Only change if the repo is already cloned at a different path on this server."
                )
            )
            if _repo_already_exists and _git_local == _default_path:
                st.success(f"Repo already cloned at `{_git_local}` — will fetch updates only, no duplicate clone.")
            elif _git_local and _git_local != _default_path and not Path(_git_local).exists():
                st.warning(f"Path `{_git_local}` does not exist on this server. Argus will clone there automatically.")

            # Always use discovered submodules — auto-detected from .gitmodules after clone
            _rc_cfg = json.loads(_repo_config_path().read_text()) if _repo_config_path().exists() else {}
            _new_sms = _rc_cfg.get(_edit_mode if _edit_mode != "— Add new customer —" else "", {}).get("submodules", [])

            st.markdown("")
            _save_btn = st.form_submit_button("💾 Save & Fetch Repository", type="primary", use_container_width=True)

            if _save_btn:
                if not _cust_name or not _prog_id or not _git_url:
                    st.error("Customer name, Program ID, and Git URL are required.")
                else:
                    _tenant_id = _cust_short.lower() if _cust_short else _cust_name.lower().replace(" ", "")[:6]
                    # Auto-generate local path from git URL if not provided
                    if not _git_local and _git_url:
                        _repo_name = _git_url.rstrip("/").split("/")[-1]
                        _repos_base2 = os.getenv("REPOS_BASE_DIR", str(Path.home() / "projects"))
                        _git_local = f"{_repos_base2}/{_repo_name}"
                    _new_cfg = {
                        "program_id":    _prog_id,
                        "pipeline_prod": _pip_prod,
                        "pipeline_dev":  _pip_dev,
                        "org_id":        _org_id,
                        "tenant_id":     _tenant_id,
                        "short":         _cust_short.upper() if _cust_short else _cust_name[:4].upper(),
                        "git_url":       _git_url,
                        "git_username":  _git_user,
                        "git_local_dir": _git_local,
                        "git_branch":    _git_branch,
                        "splunk_index":  "ams_linux-os",
                    }
                    _save_customer(_cust_name, _new_cfg, _git_pwd)

                    # Update repo_config.json with submodules
                    if _new_sms or _edit_mode != "— Add new customer —":
                        _rc = json.loads(_repo_config_path().read_text()) if _repo_config_path().exists() else {}
                        _rc[_cust_name] = {**_new_cfg, "submodules": _new_sms}
                        _repo_config_path().write_text(json.dumps(_rc, indent=2))

                    st.success(f"✓ Saved **{_cust_name}**")

                    # Auto-clone/fetch the repo
                    if _git_url and _git_local and _git_user and _git_pwd:
                        with st.spinner(f"Fetching repository for {_cust_name}..."):
                            try:
                                import subprocess as _sp
                                from urllib.parse import quote as _q
                                _auth_url = _git_url.replace("https://", f"https://{_q(_git_user,safe='')}:{_q(_git_pwd,safe='')}@")
                                _lp = Path(_git_local)
                                if not (_lp / ".git").exists():
                                    _lp.mkdir(parents=True, exist_ok=True)
                                    _r = _sp.run(["git", "clone", _auth_url, str(_lp)], capture_output=True, text=True, timeout=300,
                                                 env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
                                    if _r.returncode == 0:
                                        st.success(f"✓ Repository cloned to {_git_local}")
                                        # Auto-discover submodules from .gitmodules
                                        try:
                                            from connectors.submodule_connector import auto_populate_submodules_in_config
                                            _sm_added = auto_populate_submodules_in_config(
                                                _cust_name, _git_local,
                                                base_local_dir=str(Path(_git_local).parent / "submodules")
                                            )
                                            if _sm_added:
                                                st.info(f"Auto-discovered {_sm_added} submodule(s) from .gitmodules")
                                        except Exception:
                                            pass
                                    else:
                                        st.warning(f"Clone failed: {_r.stderr[:200]}")
                                else:
                                    _r = _sp.run(["git", "fetch", "--all", "--prune"],
                                                 capture_output=True, text=True, timeout=120, cwd=str(_lp),
                                                 env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"})
                                    st.success(f"✓ Repository fetched")
                                    # Auto-discover submodules on fetch too
                                    try:
                                        from connectors.submodule_connector import auto_populate_submodules_in_config
                                        _sm_added = auto_populate_submodules_in_config(
                                            _cust_name, _git_local,
                                            base_local_dir=str(Path(_git_local).parent / "submodules")
                                        )
                                        if _sm_added:
                                            st.info(f"Auto-discovered {_sm_added} submodule(s) from .gitmodules")
                                    except Exception:
                                        pass
                            except Exception as _ge:
                                st.warning(f"Git operation: {_ge}. You can fetch manually later.")

                    st.success(f"**{_cust_name}** has been added successfully.")

                    # Auto-run historical initialization in background — no button needed.
                    # Runs once per customer after first save. Populates ChromaDB from
                    # last 30 days of Splunk data so predictions have historical context
                    # from day one. Skipped if ChromaDB already has data for this customer.
                    def _auto_init_bg(_cfg=_new_cfg, _name=_cust_name):
                        import threading as _thr
                        def _run():
                            try:
                                import os as _os
                                from connectors.splunk_connector import fetch_pipeline_list, fetch_failed_steps
                                from analysis.cold_start import initialize_customer
                                _pid = _cfg["program_id"]
                                _os.environ["PROGRAM_ID"]   = _pid
                                _os.environ["GIT_LOCAL_DIR"] = _cfg.get("git_local_dir", "")
                                _cs_pdf = fetch_pipeline_list(int(_pid))
                                _cs_fdf = fetch_failed_steps(int(_pid))
                                initialize_customer(
                                    program_id    = _pid,
                                    tenant_id     = _cfg.get("tenant_id", ""),
                                    pipeline_df   = _cs_pdf,
                                    failed_df     = _cs_fdf,
                                    git_local_dir = _cfg.get("git_local_dir", ""),
                                    git_branch    = _cfg.get("git_branch", "master"),
                                    git_url       = _cfg.get("git_url", ""),
                                    git_username  = _cfg.get("git_username", ""),
                                    git_password  = _git_pwd,
                                )
                                print(f"  [onboard] Historical init complete for {_name}")
                            except Exception as _e:
                                print(f"  [onboard] Historical init failed for {_name}: {_e}")
                        _thr.Thread(target=_run, daemon=True,
                                    name=f"init-{_name}").start()

                    _auto_init_bg()
                    st.info(
                        f"⟳ **Building historical context for {_cust_name} in the background** — "
                        f"fetching last 30 days of Splunk data and ingesting failure patterns into ChromaDB. "
                        f"This takes 2-5 minutes and runs automatically. "
                        f"Predictions work immediately using code analysis; historical matching activates once complete."
                    )

        # ── Cold Start Initialization ─────────────────────────────────────────
        if _edit_mode != "— Add new customer —":
            st.markdown("---")
            st.markdown("#### Initialize Historical Data")
            st.caption(
                "Runs once after setup. Pulls last 30 days of Splunk data, "
                "creates resolved predictions for calibration, and ingests "
                "historical failure patterns into ChromaDB. Takes 2-5 minutes."
            )
            _cust_data = _load_customers().get(_edit_mode, {})
            _init_ready = bool(
                _cust_data.get("program_id") and
                _cust_data.get("git_local_dir") and
                Path(_cust_data["git_local_dir"]).exists()
            )
            if not _init_ready:
                st.warning("Save the customer and ensure the repository is cloned before initializing.")
            elif st.button("Initialize Historical Data", type="primary", key="cold_start_btn"):
                _progress_msgs = []
                def _on_progress(msg):
                    _progress_msgs.append(msg)

                with st.spinner("Initializing — fetching Splunk data and processing history..."):
                    try:
                        # Load Splunk data for this customer
                        _old_pid = os.environ.get("PROGRAM_ID", "")
                        os.environ["PROGRAM_ID"] = _cust_data["program_id"]
                        os.environ["GIT_LOCAL_DIR"] = _cust_data["git_local_dir"]

                        from connectors.splunk_connector import fetch_pipeline_list, fetch_failed_steps
                        import pandas as _pd_cs
                        _cs_pdf = fetch_pipeline_list(int(_cust_data["program_id"]))
                        _cs_fdf = fetch_failed_steps(int(_cust_data["program_id"]))

                        from analysis.cold_start import initialize_customer
                        _cs_results = initialize_customer(
                            program_id    = _cust_data["program_id"],
                            tenant_id     = _cust_data.get("tenant_id", ""),
                            pipeline_df   = _cs_pdf,
                            failed_df     = _cs_fdf,
                            git_local_dir = _cust_data["git_local_dir"],
                            git_branch    = _cust_data.get("git_branch", "master"),
                            on_progress   = _on_progress,
                            git_url       = _cust_data.get("git_url", ""),
                            git_username  = _cust_data.get("git_username", ""),
                            git_password  = _cust_data.get("git_password", ""),
                        )

                        if _old_pid:
                            os.environ["PROGRAM_ID"] = _old_pid

                        _retro = _cs_results.get("retroactive", {})
                        _chroma = _cs_results.get("chromadb", {})
                        st.success(
                            f"Initialization complete for **{_edit_mode}**\n\n"
                            f"- **{_retro.get('created', 0)}** historical prediction records created\n"
                            f"- **{_chroma.get('ingested', 0)}** failure patterns ingested into ChromaDB\n\n"
                            f"Historical Match signal is now active. Confidence scores will be higher."
                        )
                    except Exception as _cse:
                        st.error(f"Initialization failed: {_cse}")
                        if _progress_msgs:
                            st.code("\n".join(_progress_msgs[-10:]))

            # Delete customer
            st.markdown("")
            _rm_key1 = f"rm_cust_a_{_edit_mode}"
            if st.button(f"Remove {_edit_mode}", type="secondary", key=_rm_key1):
                _delete_customer(_edit_mode)
                st.success(f"Removed {_edit_mode}")
                st.rerun()

        # Delete customer
        if _edit_mode != "— Add new customer —":
            st.markdown("---")
            _rm_key2 = f"rm_cust_b_{_edit_mode}"
            if st.button(f"Remove {_edit_mode}", type="secondary", key=_rm_key2):
                _delete_customer(_edit_mode)
                st.success(f"Removed {_edit_mode}")
                st.rerun()

    with _list_tab:
        _all = _load_customers()
        if not _all:
            st.info("No customers configured yet.")
        for _cn, _cc in _all.items():
            _has_git = bool(_cc.get("git_local_dir") and Path(_cc["git_local_dir"]).exists())
            _status  = "Repo cloned" if _has_git else "Repo not found locally"
            with st.expander(f"{_cc.get('short','?')} — {_cn}  |  Program {_cc.get('program_id','?')}  |  {_status}"):
                c1, c2, c3 = st.columns(3)
                c1.metric("Program ID", _cc.get("program_id","—"))
                c2.metric("Prod Pipeline", _cc.get("pipeline_prod","—"))
                c3.metric("Dev Pipeline", _cc.get("pipeline_dev","—"))
                st.code(_cc.get("git_url","—"), language=None)
                st.caption(f"Branch: {_cc.get('git_branch','master')} | Local: {_cc.get('git_local_dir','—')}")
