# dashboard/argus_home.py

from __future__ import annotations

import html
import textwrap
from urllib.parse import urlencode

import streamlit as st
import streamlit.components.v1 as components


def _escape(s: str) -> str:
    return html.escape(s, quote=True)


def _link_card(title: str, description: str, href: str, icon_svg: str, accent_class: str) -> str:
    return textwrap.dedent(f"""\
    <a class="argus-card-link" href="{_escape(href)}" target="_top">
      <div class="link-card">
        <div class="link-top">
          <div class="link-main">
            <div class="icon-circle {accent_class}" aria-hidden="true">
              {icon_svg}
            </div>
            <div class="link-copy">
              <div class="link-title">{_escape(title)}</div>
              <div class="link-desc">{_escape(description)}</div>
            </div>
          </div>
          <div class="external-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="M14 5H19V10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M10 14L19 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M19 13V19H5V5H11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
        </div>
      </div>
    </a>
    """).strip()


def _route_href(page: str) -> str:
    """Build an in-app route while retaining the selected customer."""
    params = {"page": page}
    customer = st.query_params.get("customer")
    if customer:
        params["customer"] = customer
    return f"?{urlencode(params)}"


def render_argus_home() -> None:
    st.markdown(
        """
        <style>
          [data-testid="stMainBlockContainer"]:has(.argus-home-marker) {
            padding-top: 4px !important;
          }
          [data-testid="stMain"] [data-testid="element-container"]:has(.argus-home-marker) {
            display: none !important;
          }
        </style>
        <span class="argus-home-marker"></span>
        """,
        unsafe_allow_html=True,
    )
    styles = textwrap.dedent("""
        <style>
          .stApp {
            background: #ffffff;
          }

          .argus-page {
            font-family: "Segoe UI", Inter, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
            color: #111827;
          }

          .argus-shell {
            max-width: 1600px;
            margin: 0 auto;
            padding: 0 28px 40px 28px;
          }

          .hero-card {
            border: 1px solid #e5e7eb;
            border-radius: 14px;
            background: #ffffff;
            box-shadow: 0 1px 0 rgba(17, 24, 39, 0.02);
            padding: 36px 40px 32px 40px;
            min-height: 430px;
            display: flex;
            align-items: center;
          }

          .hero-grid {
            display: grid;
            grid-template-columns: 1.05fr 0.95fr;
            gap: 24px;
            align-items: center;
            width: 100%;
          }

          .hero-kicker {
            color: #16a34a;
            font-size: 20px;
            font-weight: 700;
            margin: 0 0 10px 0;
            letter-spacing: 0.1px;
          }

          .hero-title {
            font-size: 48px;
            line-height: 1.05;
            font-weight: 800;
            margin: 0 0 18px 0;
            color: #111827;
            letter-spacing: -1.1px;
          }

          .hero-subtitle {
            font-size: 28px;
            line-height: 1.25;
            font-weight: 400;
            margin: 0 0 22px 0;
            color: #111827;
            letter-spacing: -0.4px;
          }

          .hero-copy {
            font-size: 18px;
            line-height: 1.75;
            color: #64748b;
            max-width: 700px;
            margin: 0;
          }

          .hero-art-wrap {
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 300px;
          }

          .section-title {
            font-size: 25px;
            font-weight: 800;
            margin: 34px 0 18px 0;
            color: #111827;
            letter-spacing: -0.4px;
          }

          .links-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 22px;
          }

          .argus-card-link {
            text-decoration: none !important;
            color: inherit !important;
            display: block;
          }

          .link-card {
            border: 1px solid #e5e7eb;
            border-radius: 10px;
            background: #ffffff;
            padding: 34px 28px 26px 28px;
            min-height: 260px;
            box-shadow: 0 1px 0 rgba(17, 24, 39, 0.02);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            transition: transform 0.15s ease, box-shadow 0.15s ease;
          }

          .argus-card-link:hover .link-card {
            transform: translateY(-1px);
            box-shadow: 0 6px 20px rgba(15, 23, 42, 0.05);
          }

          .link-top {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 16px;
          }

          .link-main {
            display: flex;
            align-items: flex-start;
            gap: 18px;
            min-width: 0;
          }

          .icon-circle {
            width: 70px;
            height: 70px;
            border-radius: 999px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex: 0 0 auto;
          }

          .icon-circle.green {
            background: #dff5d2;
            color: #20a34a;
          }

          .icon-circle.teal {
            background: #d8f1f5;
            color: #1fa3b8;
          }

          .icon-circle.orange {
            background: #fae4c9;
            color: #f08a1a;
          }

          .link-copy {
            min-width: 0;
          }

          .link-title {
            font-size: 24px;
            line-height: 1.2;
            font-weight: 800;
            color: #111827;
            margin: 2px 0 14px 0;
            letter-spacing: -0.35px;
          }

          .link-desc {
            font-size: 17px;
            line-height: 1.7;
            color: #64748b;
            max-width: 430px;
          }

          .external-icon {
            width: 24px;
            height: 24px;
            color: #111827;
            margin-top: 6px;
            flex: 0 0 auto;
          }

          .external-icon svg {
            width: 24px;
            height: 24px;
          }

          @media (max-width: 1100px) {
            .hero-grid {
              grid-template-columns: 1fr;
            }

            .hero-title {
              font-size: 44px;
            }

            .hero-subtitle {
              font-size: 24px;
            }

            .links-grid {
              grid-template-columns: 1fr;
            }
          }

          @media (max-width: 700px) {
            .argus-shell {
              padding: 14px;
            }

            .hero-card {
              padding: 26px 20px;
              min-height: auto;
            }

            .hero-title {
              font-size: 36px;
            }

            .hero-subtitle {
              font-size: 21px;
            }

            .hero-copy {
              font-size: 16px;
            }

            .link-card {
              padding: 24px 18px 20px 18px;
              min-height: auto;
            }
          }
        </style>
    """)

    hero_svg = """
    <svg width="640" height="360" viewBox="0 0 640 360" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="g1" x1="160" y1="70" x2="460" y2="300" gradientUnits="userSpaceOnUse">
          <stop stop-color="#EAF7FF"/>
          <stop offset="1" stop-color="#D7F6EE"/>
        </linearGradient>
        <linearGradient id="g2" x1="260" y1="160" x2="470" y2="160" gradientUnits="userSpaceOnUse">
          <stop stop-color="#7FD949"/>
          <stop offset="1" stop-color="#37B5C4"/>
        </linearGradient>
        <linearGradient id="g3" x1="400" y1="230" x2="560" y2="320" gradientUnits="userSpaceOnUse">
          <stop stop-color="#D8F1F5"/>
          <stop offset="1" stop-color="#C6E8FF"/>
        </linearGradient>
      </defs>

      <ellipse cx="376" cy="178" rx="168" ry="132" fill="url(#g1)"/>
      <rect x="262" y="82" width="220" height="148" rx="10" fill="#ffffff" stroke="#3D6175" stroke-width="6"/>
      <rect x="262" y="82" width="220" height="24" rx="10" fill="#3D6175"/>
      <circle cx="282" cy="94" r="5" fill="#FFFFFF"/>
      <circle cx="298" cy="94" r="5" fill="#FFFFFF"/>
      <circle cx="314" cy="94" r="5" fill="#FFFFFF"/>

      <circle cx="352" cy="164" r="50" fill="#D7F6EE"/>
      <path d="M352 118L399 164L352 210L352 118Z" fill="#37B5C4"/>

      <circle cx="476" cy="96" r="58" fill="#D8F1F5"/>
      <path d="M447 96C447 80 460 67 476 67C492 67 505 80 505 96C505 112 492 125 476 125C460 125 447 112 447 96Z" fill="#35AFC1"/>
      <path d="M476 78C488 78 497 87 497 99C497 112 488 121 476 121C464 121 455 112 455 99C455 87 464 78 476 78Z" fill="#CFE8F4"/>

      <path d="M382 236H489C507 236 522 250 522 268C522 285 508 299 491 299H396C375 299 358 283 358 262C358 245 371 236 382 236Z" fill="url(#g3)"/>
      <circle cx="508" cy="170" r="38" fill="#D4F0D1"/>
      <path d="M508 154V186" stroke="#7FD949" stroke-width="6" stroke-linecap="round"/>
      <path d="M492 170H524" stroke="#7FD949" stroke-width="6" stroke-linecap="round"/>

      <rect x="390" y="124" width="118" height="18" rx="9" fill="#7FD949"/>
      <rect x="390" y="151" width="92" height="8" rx="4" fill="#A0E46C"/>
      <rect x="390" y="169" width="92" height="8" rx="4" fill="#A0E46C"/>
      <rect x="390" y="187" width="92" height="8" rx="4" fill="#A0E46C"/>

      <circle cx="305" cy="235" r="46" fill="#D8F1F5"/>
      <path d="M288 235L320 235" stroke="#37B5C4" stroke-width="8" stroke-linecap="round"/>
      <path d="M305 218L305 252" stroke="#37B5C4" stroke-width="8" stroke-linecap="round"/>

      <circle cx="252" cy="266" r="16" fill="#7FD949"/>
      <text x="248" y="273" font-family="Segoe UI, Arial, sans-serif" font-size="20" font-weight="700" fill="#ffffff">1</text>

      <circle cx="368" cy="266" r="16" fill="#7FD949"/>
      <text x="364" y="273" font-family="Segoe UI, Arial, sans-serif" font-size="20" font-weight="700" fill="#ffffff">2</text>

      <circle cx="472" cy="266" r="16" fill="#ffffff" stroke="#37B5C4" stroke-width="5"/>
      <path d="M463 266L470 273L481 259" stroke="#37B5C4" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>

      <circle cx="154" cy="230" r="7" fill="#BDE7F2"/>
      <circle cx="174" cy="230" r="7" fill="#BDE7F2"/>
      <circle cx="194" cy="230" r="7" fill="#BDE7F2"/>
      <circle cx="154" cy="248" r="7" fill="#BDE7F2"/>
      <circle cx="174" cy="248" r="7" fill="#BDE7F2"/>
      <circle cx="194" cy="248" r="7" fill="#BDE7F2"/>
      <circle cx="154" cy="266" r="7" fill="#BDE7F2"/>
      <circle cx="174" cy="266" r="7" fill="#BDE7F2"/>
      <circle cx="194" cy="266" r="7" fill="#BDE7F2"/>

      <path d="M546 220H566" stroke="#B6E9F4" stroke-width="4" stroke-linecap="round"/>
      <path d="M556 210V230" stroke="#B6E9F4" stroke-width="4" stroke-linecap="round"/>
    </svg>
    """

    risk_svg = """
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
      <path d="M4 7H20V17H4V7Z" stroke="currentColor" stroke-width="1.9" />
      <path d="M4 9L12 14L20 9" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
    """

    failure_svg = """
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="5.5" stroke="currentColor" stroke-width="1.9"/>
      <path d="M12 2.8V5.2" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>
      <path d="M12 18.8V21.2" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>
      <path d="M2.8 12H5.2" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>
      <path d="M18.8 12H21.2" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>
    </svg>
    """

    repo_svg = """
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
      <path d="M7 7H17" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>
      <path d="M7 12H17" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>
      <path d="M7 17H12" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>
      <path d="M5 4H19V20H5V4Z" stroke="currentColor" stroke-width="1.9"/>
    </svg>
    """

    # Use query params as a lightweight route target that another AI can wire up
    # in the main app router. These links are intentionally simple and stable.
    risk_href = _route_href("risk_assessment")
    failure_href = _route_href("failure_pinpoint")
    repo_href = _route_href("repository_settings")

    cards_html = "\n".join((
        _link_card(
            "Risk Assessment",
            "Identify potential risks across your infrastructure, deployments, and environments.",
            risk_href,
            risk_svg,
            "teal",
        ),
        _link_card(
            "Post-Failure Diagnosis",
            "Quickly detect, analyze, and pinpoint the root cause of failures across your infrastructure.",
            failure_href,
            failure_svg,
            "orange",
        ),
        _link_card(
            "Initialize Repo",
            "Set up a new repository and jump directly to repository settings.",
            repo_href,
            repo_svg,
            "green",
        ),
    ))
    html_block = f"""
    <div class="argus-page">
      <div class="argus-shell">
        <div class="hero-card">
          <div class="hero-grid">
            <div>
              <div class="hero-title">Welcome to Argus</div>
              <div class="hero-subtitle">Your Intelligent DevOps &amp; Reliability Assistant</div>
              <p class="hero-copy">
                Argus helps your team proactively assess risks, pinpoint failures, and improve
                reliability across your cloud systems with clear, actionable insights.
              </p>
            </div>

            <div class="hero-art-wrap">
              {hero_svg}
            </div>
          </div>
        </div>

        <div class="section-title">Quick Links</div>

        <div class="links-grid">
{cards_html}
        </div>
      </div>
    </div>
    """
    navigation_script = """
    <script>
      document.querySelectorAll(".argus-card-link").forEach((link) => {
        link.addEventListener("click", (event) => {
          event.preventDefault();
          const url = new URL(link.getAttribute("href"), window.parent.location.href);
          const parentLink = window.parent.document.createElement("a");
          parentLink.href = url.href;
          parentLink.style.display = "none";
          window.parent.document.body.appendChild(parentLink);
          parentLink.click();
          parentLink.remove();
        });
      });
    </script>
    """

    components.html(
        f"{styles}{textwrap.dedent(html_block)}{navigation_script}",
        height=920,
        scrolling=False,
    )


if __name__ == "__main__":
    st.set_page_config(page_title="Argus", layout="wide", initial_sidebar_state="collapsed")
    render_argus_home()