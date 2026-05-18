#!/usr/bin/env python3
import html as htmllib
import json
import os
import re
import smtplib
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from playwright_stealth import Stealth

# ── Config — fill these in before running ────────────────────────────────────
STUBHUB_URL = (
    "https://www.stubhub.com/noah-kahan-st-louis-tickets-8-2-2026/event/160403259/"
    "?quantity=4&sort=price%2Casc"
)
QUANTITY = 4

EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

CHECK_INTERVAL_MINUTES = 15
ALERT_COOLDOWN_HOURS = 2    # minimum hours between alert emails

# Thresholds — set either or both. None = disabled.
PRICE_BELOW = 200           # alert when any listing drops below $200/ticket
PRICE_DROP_PCT = None       # e.g. 15 → alert when price drops 15% from first reading

STATE_FILE = Path("state.json")
# ─────────────────────────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"baseline": None, "last_alerted": None, "last_digest": None, "hourly_low": None, "history": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _parse_price(text: str) -> float | None:
    m = re.search(r"[\d,]+(?:\.\d{2})?", text.replace(",", ""))
    if m:
        val = float(m.group().replace(",", ""))
        return val if 10 < val < 10_000 else None
    return None


def scrape_lowest_listing() -> dict | None:
    """Returns details of the cheapest listing: price, section, row, quantity."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        ).new_page()
        Stealth().apply_stealth_sync(page)

        try:
            page.goto(STUBHUB_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(5_000)
            page.evaluate("window.scrollTo(0, 500)")
            page.wait_for_timeout(3_000)
        except PWTimeoutError:
            browser.close()
            return None

        # Each listing has a data-listing-id attribute with data-price and
        # section/row in the first lines of innerText.
        raw = page.evaluate("""() =>
            [...document.querySelectorAll('[data-listing-id]')].map(el => ({
                price: el.getAttribute('data-price'),
                text: el.innerText,
            }))
        """)

        browser.close()

        listings = []
        for item in raw:
            lines = [l.strip() for l in item["text"].splitlines() if l.strip()]

            # Prefer the all-in price (line before "incl. fees") over data-price
            # which is the pre-fee base price.
            price = None
            for i, line in enumerate(lines):
                if "incl. fees" in line.lower() and i > 0:
                    price = _parse_price(lines[i - 1])
                    break
            if not price:
                price = _parse_price(item["price"] or "")
            if not price:
                continue

            section = lines[0] if lines else "—"
            row = lines[1] if len(lines) > 1 and lines[1].lower().startswith("row") else "—"
            qty_line = next((l for l in lines if "ticket" in l.lower()), "—")
            listings.append({"price": price, "section": section, "row": row, "qty": qty_line})

        return min(listings, key=lambda x: x["price"]) if listings else None


def in_cooldown(state: dict) -> bool:
    if not state["last_alerted"]:
        return False
    elapsed_hours = (datetime.now() - datetime.fromisoformat(state["last_alerted"])).total_seconds() / 3600
    return elapsed_hours < ALERT_COOLDOWN_HOURS


def triggered_reasons(listing: dict, state: dict) -> list[str]:
    price = listing["price"]
    reasons = []
    if PRICE_BELOW is not None and price < PRICE_BELOW:
        reasons.append(f"price ${price:.2f} dropped below your threshold of ${PRICE_BELOW}")
    if PRICE_DROP_PCT is not None and state["baseline"] is not None:
        drop = (state["baseline"] - price) / state["baseline"] * 100
        if drop >= PRICE_DROP_PCT:
            reasons.append(
                f"price dropped {drop:.1f}% from baseline ${state['baseline']:.2f}"
            )
    return reasons


def compute_trends(history: list, current_price: float) -> dict:
    """For each window, find the oldest entry within that lookback and compute delta."""
    now = datetime.now()
    windows = {
        "1h":  timedelta(hours=1),
        "1d":  timedelta(days=1),
        "7d":  timedelta(days=7),
        "14d": timedelta(days=14),
        "30d": timedelta(days=30),
    }
    trends = {}
    for label, delta in windows.items():
        cutoff = now - delta
        within = [e for e in history if datetime.fromisoformat(e["ts"]) >= cutoff]
        if within:
            ref = within[0]["price"]   # oldest entry in the window
            change = current_price - ref
            trends[label] = {"ref": ref, "change": change, "pct": change / ref * 100}
        else:
            trends[label] = None
    # Always include baseline comparison
    if history:
        ref = history[0]["price"]
        change = current_price - ref
        trends["start"] = {"ref": ref, "change": change, "pct": change / ref * 100,
                           "ts": history[0]["ts"]}
    else:
        trends["start"] = None
    return trends


STUBHUB_URL_HTML = htmllib.escape(STUBHUB_URL)


def _send(subject: str, html: str) -> None:
    recipients = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
        smtp.sendmail(EMAIL_FROM, recipients, msg.as_string())


def _listing_html(listing: dict) -> str:
    return f"""
    <table style="border-collapse:collapse;font-family:sans-serif;font-size:15px;">
      <tr><td style="padding:4px 12px 4px 0;color:#888;">Price</td>
          <td style="padding:4px 0;"><strong>~${listing['price']:.2f} per ticket</strong> <span style="color:#888;font-size:13px;">(may vary slightly on site)</span></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#888;">Section</td>
          <td style="padding:4px 0;"><strong>{listing['section']}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#888;">Row</td>
          <td style="padding:4px 0;"><strong>{listing['row']}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#888;">Quantity</td>
          <td style="padding:4px 0;">{listing['qty']}</td></tr>
    </table>"""


def _trend_row(label: str, data: dict | None) -> str:
    if data is None:
        return f'<tr><td style="padding:3px 12px 3px 0;color:#888;">{label}</td><td style="padding:3px 0;color:#aaa;">not enough data yet</td></tr>'
    change = data["change"]
    pct = data["pct"]
    arrow = "▼" if change < 0 else "▲"
    color = "#1e8c45" if change < 0 else "#d93025"  # green=down (good), red=up (bad)
    sign = "" if change < 0 else "+"
    return (
        f'<tr><td style="padding:3px 12px 3px 0;color:#888;">{label}</td>'
        f'<td style="padding:3px 0;color:{color};font-weight:bold;">'
        f'{arrow} ${abs(change):.2f} ({sign}{pct:.1f}%) '
        f'<span style="color:#aaa;font-weight:normal;font-size:13px;">vs ~${data["ref"]:.0f}</span>'
        f'</td></tr>'
    )


def send_digest(listing: dict, trends: dict) -> None:
    price = listing["price"]
    now_ct = datetime.now(timezone.utc).astimezone(ZoneInfo('America/Chicago'))

    window_labels = {"1h": "Last hour", "1d": "Last 24h", "7d": "Last 7 days",
                     "14d": "Last 14 days", "30d": "Last 30 days"}
    trend_rows = "".join(_trend_row(lbl, trends.get(key)) for key, lbl in window_labels.items())

    start = trends.get("start")
    if start:
        start_date = datetime.fromisoformat(start["ts"]).strftime("%b %d")
        trend_rows += _trend_row(f"Since {start_date}", start)

    html = f"""
    <div style="font-family:sans-serif;max-width:520px;">
      <h2 style="margin-bottom:4px;">🎵 Noah Kahan — St. Louis</h2>
      <p style="margin-top:0;color:#888;">Aug 2, 2026 &nbsp;·&nbsp; as of {now_ct.strftime('%I:%M %p CT')}</p>
      <h3 style="margin-bottom:8px;">Cheapest listing this period</h3>
      {_listing_html(listing)}
      <h3 style="margin-bottom:4px;margin-top:20px;">Lowest price trends</h3>
      <p style="margin-top:0;margin-bottom:8px;color:#888;font-size:13px;">How the cheapest ticket available has changed over time.</p>
      <table style="border-collapse:collapse;font-family:sans-serif;font-size:14px;">
        {trend_rows}
      </table>
      <p style="color:#888;font-size:12px;margin-top:6px;">Green ▼ = cheapest ticket got cheaper (good!). Red ▲ = cheapest ticket got more expensive.</p>
      <br>
      <a href="{STUBHUB_URL_HTML}" style="display:inline-block;padding:10px 20px;background:#1a73e8;color:white;text-decoration:none;border-radius:6px;font-weight:bold;">
        View Tickets on StubHub
      </a>
      <p style="color:#aaa;font-size:12px;margin-top:16px;">Set quantity to {QUANTITY} once the page loads.</p>
    </div>"""
    _send(f"StubHub Update — Noah Kahan STL ~${price:.0f}/ticket", html)


def send_alert(listing: dict, reasons: list[str]) -> None:
    price = listing["price"]
    reasons_html = "".join(f"<li>{r}</li>" for r in reasons)
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;">
      <h2 style="margin-bottom:4px;color:#d93025;">🚨 Price Alert — Noah Kahan STL</h2>
      <p style="margin-top:0;color:#888;">Aug 2, 2026</p>
      <ul style="font-size:15px;padding-left:20px;">{reasons_html}</ul>
      <h3 style="margin-bottom:8px;">Listing details</h3>
      {_listing_html(listing)}
      <br>
      <a href="{STUBHUB_URL_HTML}" style="display:inline-block;padding:10px 20px;background:#d93025;color:white;text-decoration:none;border-radius:6px;font-weight:bold;">
        View Tickets on StubHub
      </a>
      <p style="color:#aaa;font-size:12px;margin-top:16px;">Set quantity to {QUANTITY} once the page loads.</p>
    </div>"""
    _send(f"🚨 StubHub Alert — Noah Kahan STL ~${price:.0f}/ticket", html)


def run_once() -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    state = load_state()

    print(f"[{ts}] Checking...", end=" ", flush=True)
    listing = scrape_lowest_listing()

    if listing is None:
        print("FAILED — could not read price (bot block or DOM change)", flush=True)
        return

    price = listing["price"]
    print(f"${price:.2f}/ticket  section={listing['section']} row={listing['row']}", flush=True)

    if state["baseline"] is None:
        state["baseline"] = price
        print(f"[{ts}] Baseline set: ${price:.2f}", flush=True)

    state["history"] = (state["history"] + [{"ts": datetime.now().isoformat(), "price": price}])[-10_000:]

    # Track hourly low
    hourly_low = state.get("hourly_low")
    if hourly_low is None or price < hourly_low["price"]:
        state["hourly_low"] = listing

    # Send digest at the top of each even-numbered hour (every 2 hours)
    last_digest = state.get("last_digest")
    new_hour = (
        not last_digest or
        datetime.fromisoformat(last_digest).hour // 2 != datetime.now().hour // 2
    )
    if new_hour and state["hourly_low"]:
        trends = compute_trends(state["history"], state["hourly_low"]["price"])
        send_digest(state["hourly_low"], trends)
        state["last_digest"] = datetime.now().isoformat()
        state["hourly_low"] = None
        print(f"[{ts}] Digest sent.", flush=True)

    reasons = triggered_reasons(listing, state)
    if reasons and not in_cooldown(state):
        send_alert(listing, reasons)
        state["last_alerted"] = datetime.now().isoformat()
        print(f"[{ts}] Alert sent.", flush=True)

    save_state(state)


def main() -> None:
    print(f"Watching: Noah Kahan STL (qty {QUANTITY})")
    print(f"Interval: {CHECK_INTERVAL_MINUTES}min | Cooldown: {ALERT_COOLDOWN_HOURS}hr")
    print(f"Thresholds: PRICE_BELOW={PRICE_BELOW}, PRICE_DROP_PCT={PRICE_DROP_PCT}%")
    print()
    while True:
        run_once()
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS"):
        run_once()
    else:
        main()
