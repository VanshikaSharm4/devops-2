from pathlib import Path


def test_sidebar_css_is_full_height_and_top_aligned():
    app_path = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
    css = app_path.read_text(encoding="utf-8")

    assert "[data-testid=\"stSidebarContent\"]" in css
    assert "justify-content: flex-start !important;" in css
    assert "top: {_TOPBAR_HEIGHT_CSS} !important;" in css
    assert "height: calc(100vh - {_TOPBAR_HEIGHT_CSS}) !important;" in css
    assert "overflow-y: hidden !important;" in css
    assert "align-items: stretch !important;" in css
    assert "[data-testid=\"stSidebarHeader\"]" in css
    assert "padding-top: 0 !important;" in css
    assert "margin-block: 0 !important;" in css
    assert "transform: translateX(-9999px) !important;" in css
    assert "pointer-events: auto !important;" in css


def test_sidebar_collapsed_icons_keep_size_and_are_centered():
    app_path = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
    css = app_path.read_text(encoding="utf-8")

    assert "font-size: 1.35rem !important;" in css
    assert "_collapsed_css" in css
    assert "justify-content: center !important;" in css
    assert "padding: 0 !important;" in css
    assert "padding-left: 0 !important;" in css
    assert "padding-right: 0 !important;" in css
    assert "border-left-width: 0 !important;" in css
    assert 'button[data-testid^="stBaseButton"]:not([kind="headerNoPadding"])' in css
    assert "align-items: center !important;" in css
    assert "margin: 0 auto !important;" in css
    assert "width: 1.35rem !important;" in css
    assert "height: 1.35rem !important;" in css
    assert "text-align: center !important;" in css
    assert "font-size: 1.45rem !important;" not in css
    assert "transform: translateX(4px) !important;" in css


def test_top_bar_hamburger_uses_sidebar_state_bridge():
    app_path = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
    source = app_path.read_text(encoding="utf-8")

    assert "def _toggle_sidebar_state() -> None:" in source
    assert "st.session_state[\"sb_open\"] = not st.session_state.get(\"sb_open\", True)" in source
    assert "ARGUS_SIDEBAR_TOGGLE_BRIDGE" in source
    assert "function findSidebarStateToggle()" in source
    assert "bridgeLabels.has(label)" in source
    assert "function clickSidebarStateToggle(btn)" in source
    assert "const stateToggle = findSidebarStateToggle();" in source
    assert "clickSidebarStateToggle(stateToggle);" in source


def test_top_bar_customer_selection_navigates_to_query_param():
    app_path = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
    source = app_path.read_text(encoding="utf-8")

    assert "url.searchParams.set('customer', customerName);" in source
    assert "window.parent.location.assign(url.href);" in source
    assert "const listContainer = parentDoc.getElementById('customer-list-container');" in source
    assert "listContainer.onclick = (e) =>" in source
    assert "const customerName = item.dataset.value;" in source
    assert "const item = parentDoc.createElement('a');" in source
    assert "item.href = (() =>" in source
    assert "if (item.tagName === 'A') return;" in source
    assert "window.parent.history.replaceState" not in source


def test_risk_assessment_extra_pipeline_header_removed():
    app_path = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
    source = app_path.read_text(encoding="utf-8")

    assert "Risk Assessment &rsaquo; Pipeline Intelligence" not in source
    assert '<h1 class="cm-page-title">Pipeline Executions</h1>' not in source


def test_risk_assessment_uses_clean_html_tables():
    app_path = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
    source = app_path.read_text(encoding="utf-8")

    assert ".ra-table-wrap" in source
    assert ".ra-table th" in source
    assert "ra-chip-finished" in source
    assert '<table class="ra-table">' in source
    assert "Select execution for assessment" in source
    assert "Select commit for early assessment" in source
    assert "All recent executions — select one below" in source
    assert "Click any execution row above" not in source
