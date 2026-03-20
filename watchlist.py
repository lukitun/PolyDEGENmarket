"""Watchlist — track markets we're interested in but haven't entered yet.

Records prices over time so we can spot entry points and trends.

Usage:
    python3 watchlist.py                          # Show watchlist with current prices
    python3 watchlist.py add <token_id> <name>    # Add a market to watchlist
    python3 watchlist.py remove <token_id>        # Remove from watchlist
    python3 watchlist.py snapshot                  # Record current prices (run periodically)
    python3 watchlist.py history <token_id>        # Show price history for a market
    python3 watchlist.py alerts                    # Show markets near entry zones
"""
import json
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")


def _load():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    return {"markets": [], "snapshots": []}


def _save(data):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)


def add_market(token_id, name, entry_below=None, entry_above=None, notes=""):
    """Add a market to the watchlist.

    entry_below: alert if price drops below this (buy-the-dip entry)
    entry_above: alert if price rises above this (momentum entry)
    """
    data = _load()

    # Check for duplicate
    for m in data["markets"]:
        if m["token_id"] == token_id:
            print(f"Already watching: {m['name']}")
            return m

    market = {
        "token_id": token_id,
        "name": name,
        "added": datetime.now(timezone.utc).isoformat(),
        "entry_below": entry_below,
        "entry_above": entry_above,
        "notes": notes,
    }
    data["markets"].append(market)
    _save(data)
    print(f"Added to watchlist: {name}")
    if entry_below:
        print(f"  Alert if price drops below: {entry_below}")
    if entry_above:
        print(f"  Alert if price rises above: {entry_above}")
    return market


def remove_market(token_id):
    """Remove a market from the watchlist."""
    data = _load()
    before = len(data["markets"])
    data["markets"] = [m for m in data["markets"] if m["token_id"] != token_id]
    after = len(data["markets"])
    if before == after:
        print(f"Token not found in watchlist: {token_id[:20]}...")
    else:
        _save(data)
        print(f"Removed from watchlist.")


def take_snapshot():
    """Record current prices for all watched markets."""
    data = _load()
    if not data["markets"]:
        print("Watchlist is empty. Add markets first.")
        return

    try:
        from proxy_client import get_client
    except ImportError:
        print("Error: proxy_client not available")
        return

    client = get_client(with_auth=False)
    now = datetime.now(timezone.utc).isoformat()
    snapshot = {"timestamp": now, "prices": {}}

    for market in data["markets"]:
        token_id = market["token_id"]
        try:
            mid = client.get_midpoint(token_id)
            price = float(mid) if not isinstance(mid, dict) else float(mid.get("mid", 0))
            snapshot["prices"][token_id] = price
            print(f"  {market['name'][:50]}: {price:.4f}")
        except Exception as e:
            print(f"  {market['name'][:50]}: ERROR ({e})")
            snapshot["prices"][token_id] = None

    data["snapshots"].append(snapshot)

    # Keep last 500 snapshots to avoid unbounded growth
    if len(data["snapshots"]) > 500:
        data["snapshots"] = data["snapshots"][-500:]

    _save(data)
    print(f"\nSnapshot recorded at {now}")


def show_watchlist():
    """Display the watchlist with latest prices and trends."""
    data = _load()
    if not data["markets"]:
        print("Watchlist is empty. Use 'watchlist.py add <token_id> <name>' to add markets.")
        return

    print("=" * 70)
    print("WATCHLIST")
    print("=" * 70)

    # Get latest snapshot prices
    latest_prices = {}
    prev_prices = {}
    if data["snapshots"]:
        latest = data["snapshots"][-1]
        latest_prices = latest.get("prices", {})
        if len(data["snapshots"]) > 1:
            prev = data["snapshots"][-2]
            prev_prices = prev.get("prices", {})

    for i, market in enumerate(data["markets"], 1):
        token_id = market["token_id"]
        price = latest_prices.get(token_id)
        prev_price = prev_prices.get(token_id)

        print(f"\n  #{i}  {market['name']}")
        print(f"       Token: {token_id[:20]}...")

        if price is not None:
            trend = ""
            if prev_price is not None and prev_price > 0:
                change = price - prev_price
                change_pct = (change / prev_price) * 100
                if change > 0:
                    trend = f"  (UP {change_pct:+.1f}%)"
                elif change < 0:
                    trend = f"  (DOWN {change_pct:+.1f}%)"
            print(f"       Price: {price:.4f}{trend}")
        else:
            print(f"       Price: no data (run 'watchlist.py snapshot')")

        # Check entry zones
        alerts = []
        if market.get("entry_below") and price is not None and price <= market["entry_below"]:
            alerts.append(f"BELOW ENTRY ZONE ({market['entry_below']})")
        if market.get("entry_above") and price is not None and price >= market["entry_above"]:
            alerts.append(f"ABOVE ENTRY ZONE ({market['entry_above']})")

        if alerts:
            for alert in alerts:
                print(f"       *** {alert} ***")
        else:
            parts = []
            if market.get("entry_below"):
                parts.append(f"Buy below: {market['entry_below']}")
            if market.get("entry_above"):
                parts.append(f"Buy above: {market['entry_above']}")
            if parts:
                print(f"       Entry: {' | '.join(parts)}")

        if market.get("notes"):
            print(f"       Notes: {market['notes']}")

    if data["snapshots"]:
        print(f"\n  Last snapshot: {data['snapshots'][-1]['timestamp'][:19]}")
        print(f"  Total snapshots: {len(data['snapshots'])}")

    print("=" * 70)


def show_history(token_id):
    """Show price history for a specific market."""
    data = _load()

    # Find the market name
    name = token_id[:20]
    for m in data["markets"]:
        if m["token_id"] == token_id:
            name = m["name"]
            break

    print(f"Price History: {name}")
    print("-" * 50)

    prices = []
    for snap in data["snapshots"]:
        price = snap["prices"].get(token_id)
        if price is not None:
            ts = snap["timestamp"][:19]
            prices.append((ts, price))
            print(f"  {ts}  {price:.4f}")

    if not prices:
        print("  No price history available.")
        return

    # Summary
    price_vals = [p for _, p in prices]
    print(f"\n  Min: {min(price_vals):.4f}  |  Max: {max(price_vals):.4f}  |  Current: {price_vals[-1]:.4f}")
    print(f"  Data points: {len(prices)}")


def show_alerts():
    """Show markets that are near or in entry zones."""
    data = _load()
    if not data["markets"] or not data["snapshots"]:
        print("No watchlist data. Add markets and take snapshots first.")
        return

    latest = data["snapshots"][-1]
    latest_prices = latest.get("prices", {})
    alerts_found = False

    print("WATCHLIST ALERTS")
    print("=" * 50)

    for market in data["markets"]:
        token_id = market["token_id"]
        price = latest_prices.get(token_id)
        if price is None:
            continue

        triggered = []
        near = []

        if market.get("entry_below"):
            target = market["entry_below"]
            if price <= target:
                triggered.append(f"Price {price:.4f} <= entry zone {target}")
            elif price <= target * 1.1:  # Within 10%
                near.append(f"Price {price:.4f} approaching entry zone {target} (within 10%)")

        if market.get("entry_above"):
            target = market["entry_above"]
            if price >= target:
                triggered.append(f"Price {price:.4f} >= entry zone {target}")
            elif price >= target * 0.9:  # Within 10%
                near.append(f"Price {price:.4f} approaching entry zone {target} (within 10%)")

        if triggered or near:
            alerts_found = True
            print(f"\n  {market['name']}")
            for t in triggered:
                print(f"    *** TRIGGERED: {t}")
            for n in near:
                print(f"    NEAR: {n}")

    if not alerts_found:
        print("\n  No alerts. All watched markets are outside entry zones.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_watchlist()
    elif sys.argv[1] == "add":
        if len(sys.argv) < 4:
            print("Usage: python3 watchlist.py add <token_id> <name> [entry_below] [entry_above] [notes]")
            sys.exit(1)
        token_id = sys.argv[2]
        name = sys.argv[3]
        entry_below = float(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] != "-" else None
        entry_above = float(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] != "-" else None
        notes = " ".join(sys.argv[6:]) if len(sys.argv) > 6 else ""
        add_market(token_id, name, entry_below, entry_above, notes)
    elif sys.argv[1] == "remove":
        if len(sys.argv) < 3:
            print("Usage: python3 watchlist.py remove <token_id>")
            sys.exit(1)
        remove_market(sys.argv[2])
    elif sys.argv[1] == "snapshot":
        take_snapshot()
    elif sys.argv[1] == "history":
        if len(sys.argv) < 3:
            print("Usage: python3 watchlist.py history <token_id>")
            sys.exit(1)
        show_history(sys.argv[2])
    elif sys.argv[1] == "alerts":
        show_alerts()
    else:
        print("Usage:")
        print("  python3 watchlist.py                          # Show watchlist")
        print("  python3 watchlist.py add <token_id> <name>    # Add market")
        print("  python3 watchlist.py remove <token_id>        # Remove market")
        print("  python3 watchlist.py snapshot                  # Record prices")
        print("  python3 watchlist.py history <token_id>        # Price history")
        print("  python3 watchlist.py alerts                    # Entry zone alerts")
