"""News monitor -- periodic keyword alerting for trading signals.

Checks RSS feeds and flags breaking news matching our positions and watchlist.
Designed to be run periodically (e.g., every 15 minutes via cron or manual).

Usage:
    python3 news_monitor.py              # Check once, print alerts
    python3 news_monitor.py loop [mins]  # Check every N minutes (default: 15)
    python3 news_monitor.py history      # Show recent alert history
"""
import json
import os
import sys
import time
import calendar
import feedparser
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALERT_LOG = os.path.join(BASE_DIR, "news_alerts.json")

# RSS feeds to monitor
FEEDS = {
    "reuters_world": "https://feeds.reuters.com/reuters/worldNews",
    "aljazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "bbc_world": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "cnbc_world": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
    "reuters_energy": "https://feeds.reuters.com/reuters/UKBankingFinancial",
    "bbc_politics": "http://feeds.bbci.co.uk/news/politics/rss.xml",
}

# Alert categories with keywords and urgency levels
ALERT_RULES = {
    "iran_hormuz": {
        "keywords": [
            "iran", "hormuz", "ceasefire", "tehran", "irgc", "persian gulf",
            "strait of hormuz", "oil tanker", "iran war", "araghchi",
            "iran nuclear", "iran sanctions", "iran deal",
        ],
        "urgency": "HIGH",
        "markets": ["CL Oil", "Iran ceasefire"],
    },
    "china_taiwan": {
        "keywords": [
            "taiwan", "pla", "chinese military", "blockade", "taiwan strait",
            "xi jinping", "chinese navy", "coast guard taiwan", "china invade",
            "china military drill", "taiwan defense",
        ],
        "urgency": "HIGH",
        "markets": ["China blockade Taiwan"],
    },
    "oil_price": {
        "keywords": [
            "crude oil", "oil price", "opec", "brent crude", "wti",
            "oil shock", "energy crisis", "spr release", "oil supply",
            "oil demand", "petroleum reserve", "opec cut",
        ],
        "urgency": "HIGH",
        "markets": ["CL Oil hits $100", "CL Oil hits $105"],
    },
    "uk_politics": {
        "keywords": [
            "starmer", "labour party", "uk election", "downing street",
            "mandelson", "labour poll", "starmer resign", "labour leader",
        ],
        "urgency": "MEDIUM",
        "markets": ["Starmer out by June 30"],
    },
    "recession": {
        "keywords": [
            "recession", "economic downturn", "gdp contraction",
            "unemployment surge", "fed rate cut", "economic crisis",
            "market crash", "stock crash",
        ],
        "urgency": "MEDIUM",
        "markets": ["Recession markets"],
    },
    "crypto": {
        "keywords": [
            "bitcoin crash", "bitcoin surge", "btc price", "ethereum",
            "crypto regulation", "sec crypto", "bitcoin etf",
        ],
        "urgency": "LOW",
        "markets": ["BTC/ETH price markets"],
    },
}


def _load_alert_log():
    if os.path.exists(ALERT_LOG):
        with open(ALERT_LOG) as f:
            return json.load(f)
    return {"alerts": [], "last_check": None}


def _save_alert_log(data):
    with open(ALERT_LOG, "w") as f:
        json.dump(data, f, indent=2)


def scan_feeds(max_age_hours=2):
    """Scan all feeds for keyword matches. Returns list of alerts."""
    now = time.time()
    alerts = []

    for feed_name, feed_url in FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:25]:
                title = entry.get("title", "").lower()
                summary = entry.get("summary", "").lower()
                text = title + " " + summary

                # Check age
                published = entry.get("published_parsed")
                age_hours = None
                if published:
                    entry_time = calendar.timegm(published)
                    age_hours = (now - entry_time) / 3600
                    if age_hours > max_age_hours:
                        continue

                # Check all rule categories
                for category, rule in ALERT_RULES.items():
                    matched = [kw for kw in rule["keywords"] if kw in text]
                    if matched:
                        alerts.append({
                            "source": feed_name,
                            "title": entry.get("title", ""),
                            "category": category,
                            "urgency": rule["urgency"],
                            "keywords": matched,
                            "link": entry.get("link", ""),
                            "age_hours": round(age_hours, 1) if age_hours is not None else None,
                            "affected_markets": rule["markets"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
        except (OSError, ValueError, KeyError, AttributeError) as e:
            print(f"  Warning: Failed to fetch {feed_name}: {e}")

    # Deduplicate by title
    seen = set()
    unique = []
    for a in alerts:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)

    # Sort by urgency then age
    urgency_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    unique.sort(key=lambda x: (urgency_order.get(x["urgency"], 3), x.get("age_hours") or 999))

    return unique


OIL_THRESHOLDS = [99, 100, 101, 105]
_OIL_ALERT_STATE_FILE = os.path.join(BASE_DIR, "oil_alert_state.json")


def _load_oil_state():
    if os.path.exists(_OIL_ALERT_STATE_FILE):
        with open(_OIL_ALERT_STATE_FILE) as f:
            return json.load(f)
    return {"last_alerted": {}}


def _save_oil_state(state):
    with open(_OIL_ALERT_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_oil_thresholds():
    """Check if WTI oil price has crossed key threshold levels.
    Returns list of threshold alerts. Only alerts once per threshold crossing.
    """
    try:
        from intel import get_oil_price
        from alerts import alert as log_alert, CRITICAL
    except ImportError:
        return []

    oil = get_oil_price()
    if not isinstance(oil, dict) or not oil.get("price"):
        return []

    price = oil["price"]
    state = _load_oil_state()
    last_alerted = state.get("last_alerted", {})
    alerts = []

    for level in OIL_THRESHOLDS:
        level_key = str(level)
        prev_alert = last_alerted.get(level_key)

        if price >= level and prev_alert != "above":
            msg = f"OIL THRESHOLD: WTI crossed above ${level} (now ${price:.2f})"
            alerts.append({"level": level, "direction": "above", "price": price, "message": msg})
            log_alert(msg, severity=CRITICAL, source="oil_monitor")
            last_alerted[level_key] = "above"
        elif price < level and prev_alert == "above":
            msg = f"OIL THRESHOLD: WTI dropped below ${level} (now ${price:.2f})"
            alerts.append({"level": level, "direction": "below", "price": price, "message": msg})
            log_alert(msg, severity=CRITICAL, source="oil_monitor")
            last_alerted[level_key] = "below"

    if alerts:
        state["last_alerted"] = last_alerted
        state["last_price"] = price
        state["last_check"] = datetime.now(timezone.utc).isoformat()
        _save_oil_state(state)

    return alerts


def check_and_alert(max_age_hours=2):
    """Run a check and print formatted alerts. Returns alerts list."""
    now = datetime.now(timezone.utc)
    print(f"NEWS MONITOR -- {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Scanning {len(FEEDS)} feeds for {len(ALERT_RULES)} keyword categories...")
    print("=" * 60)

    # Check oil price thresholds
    oil_alerts = check_oil_thresholds()
    if oil_alerts:
        print(f"\n*** OIL PRICE ALERTS ***")
        for oa in oil_alerts:
            print(f"  {oa['message']}")
        print()

    alerts = scan_feeds(max_age_hours=max_age_hours)

    if not alerts:
        print("\nNo alerts in the last {:.0f} hours. Markets quiet.".format(max_age_hours))
        # Log the check
        log = _load_alert_log()
        log["last_check"] = now.isoformat()
        _save_alert_log(log)
        return []

    # Group by urgency
    high = [a for a in alerts if a["urgency"] == "HIGH"]
    medium = [a for a in alerts if a["urgency"] == "MEDIUM"]
    low = [a for a in alerts if a["urgency"] == "LOW"]

    if high:
        print(f"\n*** HIGH URGENCY ({len(high)} alerts) ***")
        print("-" * 40)
        for a in high:
            age_str = f" [{a['age_hours']}h ago]" if a["age_hours"] is not None else ""
            print(f"  [{a['category'].upper()}] {a['title'][:80]}{age_str}")
            print(f"    Keywords: {', '.join(a['keywords'][:5])}")
            print(f"    Markets:  {', '.join(a['affected_markets'])}")
            print(f"    Source:   {a['source']}  |  {a['link'][:60]}")

    if medium:
        print(f"\n  MEDIUM URGENCY ({len(medium)} alerts)")
        print("-" * 40)
        for a in medium:
            age_str = f" [{a['age_hours']}h ago]" if a["age_hours"] is not None else ""
            print(f"  [{a['category'].upper()}] {a['title'][:80]}{age_str}")
            print(f"    Keywords: {', '.join(a['keywords'][:5])}")

    if low:
        print(f"\n  Low urgency ({len(low)} alerts)")
        print("-" * 40)
        for a in low[:5]:
            age_str = f" [{a['age_hours']}h ago]" if a["age_hours"] is not None else ""
            print(f"  [{a['category'].upper()}] {a['title'][:80]}{age_str}")

    # Trading signals summary
    print(f"\n{'=' * 60}")
    print("TRADING SIGNALS:")
    categories_triggered = set(a["category"] for a in alerts)
    for cat in categories_triggered:
        rule = ALERT_RULES[cat]
        count = sum(1 for a in alerts if a["category"] == cat)
        urgency = rule["urgency"]
        print(f"  {urgency} | {cat}: {count} alerts -> Check: {', '.join(rule['markets'])}")

    # Save to log
    log = _load_alert_log()
    log["last_check"] = now.isoformat()
    for a in alerts:
        log["alerts"].append(a)
    # Keep last 500 alerts
    if len(log["alerts"]) > 500:
        log["alerts"] = log["alerts"][-500:]
    _save_alert_log(log)

    return alerts


def show_history(limit=20):
    """Show recent alert history."""
    log = _load_alert_log()
    alerts = log.get("alerts", [])
    last_check = log.get("last_check", "never")

    print(f"Last check: {last_check}")
    print(f"Total alerts logged: {len(alerts)}")
    print("=" * 60)

    if not alerts:
        print("No alerts in history.")
        return

    for a in alerts[-limit:]:
        ts = a.get("timestamp", "")[:19]
        age_str = f" [{a.get('age_hours', '?')}h old at check time]"
        print(f"\n  [{ts}] [{a['urgency']}] {a['category'].upper()}")
        print(f"    {a['title'][:80]}{age_str}")
        print(f"    Keywords: {', '.join(a.get('keywords', [])[:5])}")


def run_loop(interval_minutes=15):
    """Run news monitor in a loop."""
    print(f"Starting news monitor loop (checking every {interval_minutes} minutes)")
    print(f"Press Ctrl+C to stop.\n")

    while True:
        try:
            alerts = check_and_alert(max_age_hours=max(interval_minutes / 60 * 2, 1))
            if alerts:
                high_count = sum(1 for a in alerts if a["urgency"] == "HIGH")
                if high_count > 0:
                    print(f"\n*** {high_count} HIGH URGENCY ALERTS -- CHECK POSITIONS ***\n")
        except Exception as e:
            print(f"  Monitor error: {e}")
            try:
                from alerts import alert as log_alert, WARNING
                log_alert(f"News monitor error: {e}", severity=WARNING, source="news_monitor")
            except Exception:
                pass  # Don't let alert logging kill the monitor

        print(f"\nNext check in {interval_minutes} minutes...")
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        check_and_alert()
    elif sys.argv[1] == "loop":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 15
        run_loop(interval)
    elif sys.argv[1] == "history":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        show_history(limit)
    else:
        print("Usage:")
        print("  python3 news_monitor.py              # Check once")
        print("  python3 news_monitor.py loop [mins]   # Continuous monitoring (default: 15 min)")
        print("  python3 news_monitor.py history [n]    # Recent alert history")
