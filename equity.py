"""Equity curve tracker — record portfolio value over time.

Tracks total portfolio value (funds + open bet costs + unrealized PnL)
so we can see if we're actually making money.

Usage:
    python3 equity.py                # Show equity curve summary
    python3 equity.py snapshot       # Record current portfolio value
    python3 equity.py history        # Show full history
    python3 equity.py chart          # ASCII chart of equity over time
"""
import json
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EQUITY_FILE = os.path.join(BASE_DIR, "equity_history.json")
LEDGER_FILE = os.path.join(BASE_DIR, "ledger.json")


def _load_equity():
    if os.path.exists(EQUITY_FILE):
        with open(EQUITY_FILE) as f:
            return json.load(f)
    return {"snapshots": []}


def _save_equity(data):
    with open(EQUITY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _load_ledger():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return None


def take_snapshot(live_prices=None):
    """Record current portfolio value.

    live_prices: optional dict of {token_id: current_price} for mark-to-market.
    If not provided, uses cost basis (book value).
    """
    ledger = _load_ledger()
    if not ledger:
        print("No ledger.json found.")
        return None

    funds = ledger.get("funds", 0)
    initial = ledger.get("initial_deposit", 0)
    pnl_realized = ledger.get("pnl_total", 0)
    open_bets = ledger.get("open_bets", [])

    # Calculate open position value
    open_cost = sum(b.get("cost", 0) for b in open_bets)

    # Mark-to-market if live prices provided
    unrealized_pnl = 0
    market_value = open_cost  # default to cost basis
    if live_prices:
        market_value = 0
        for bet in open_bets:
            token_id = bet.get("token_id", "")
            if token_id in live_prices and live_prices[token_id] is not None:
                current_price = live_prices[token_id]
                position_value = current_price * bet.get("size", 0)
                market_value += position_value
                unrealized_pnl += position_value - bet.get("cost", 0)
            else:
                market_value += bet.get("cost", 0)  # fallback to cost

    total_value = funds + market_value
    total_return = total_value - initial if initial > 0 else 0
    return_pct = (total_return / initial * 100) if initial > 0 else 0

    now = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "timestamp": now,
        "funds": round(funds, 4),
        "open_cost": round(open_cost, 4),
        "market_value": round(market_value, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "realized_pnl": round(pnl_realized, 4),
        "total_value": round(total_value, 4),
        "total_return": round(total_return, 4),
        "return_pct": round(return_pct, 2),
        "num_positions": len(open_bets),
        "has_live_prices": live_prices is not None,
    }

    data = _load_equity()
    data["snapshots"].append(snapshot)

    # Keep last 1000 snapshots
    if len(data["snapshots"]) > 1000:
        data["snapshots"] = data["snapshots"][-1000:]

    _save_equity(data)

    print(f"Equity snapshot recorded at {now[:19]}")
    print(f"  Funds:           ${funds:.2f}")
    print(f"  Open positions:  ${market_value:.2f} ({len(open_bets)} bets)")
    print(f"  Total value:     ${total_value:.2f}")
    print(f"  Total return:    ${total_return:+.2f} ({return_pct:+.1f}%)")
    return snapshot


def take_live_snapshot():
    """Take a snapshot using live prices from the CLOB API."""
    ledger = _load_ledger()
    if not ledger:
        print("No ledger.json found.")
        return

    open_bets = ledger.get("open_bets", [])
    if not open_bets:
        # No open positions, just record book value
        return take_snapshot()

    # Fetch live prices
    try:
        from proxy_client import get_client
        client = get_client(with_auth=False)

        live_prices = {}
        for bet in open_bets:
            token_id = bet.get("token_id", "")
            if not token_id:
                continue
            try:
                mid = client.get_midpoint(token_id)
                price = float(mid) if not isinstance(mid, dict) else float(mid.get("mid", 0))
                live_prices[token_id] = price
            except Exception as e:
                print(f"  Could not get price for {bet.get('market', '')[:30]}: {e}")

        return take_snapshot(live_prices=live_prices)
    except ImportError:
        print("  Warning: proxy_client not available, using book value")
        return take_snapshot()


def show_summary():
    """Show equity curve summary."""
    data = _load_equity()
    snapshots = data.get("snapshots", [])

    if not snapshots:
        print("No equity history. Run 'equity.py snapshot' to start tracking.")
        return

    print("=" * 60)
    print("EQUITY CURVE SUMMARY")
    print("=" * 60)

    latest = snapshots[-1]
    first = snapshots[0]

    print(f"  First recorded:  {first['timestamp'][:19]}")
    print(f"  Latest:          {latest['timestamp'][:19]}")
    print(f"  Total snapshots: {len(snapshots)}")
    print()
    print(f"  Starting value:  ${first['total_value']:.2f}")
    print(f"  Current value:   ${latest['total_value']:.2f}")
    print(f"  Total return:    ${latest['total_return']:+.2f} ({latest['return_pct']:+.1f}%)")
    print(f"  Realized PnL:    ${latest['realized_pnl']:+.2f}")
    print(f"  Unrealized PnL:  ${latest['unrealized_pnl']:+.2f}")
    print(f"  Positions:       {latest['num_positions']}")

    # High/low watermarks
    values = [s["total_value"] for s in snapshots]
    high = max(values)
    low = min(values)
    current = latest["total_value"]
    drawdown = ((high - current) / high * 100) if high > 0 else 0

    print()
    print(f"  High watermark:  ${high:.2f}")
    print(f"  Low watermark:   ${low:.2f}")
    print(f"  Current drawdown: {drawdown:.1f}%")

    print("=" * 60)


def show_history():
    """Show full equity history."""
    data = _load_equity()
    snapshots = data.get("snapshots", [])

    if not snapshots:
        print("No equity history.")
        return

    print(f"{'Date':>19}  {'Value':>10}  {'Return':>10}  {'Ret%':>7}  {'Positions':>5}")
    print("-" * 60)

    for s in snapshots:
        date = s["timestamp"][:19]
        value = s["total_value"]
        ret = s["total_return"]
        pct = s["return_pct"]
        pos = s["num_positions"]
        print(f"{date}  ${value:>9.2f}  ${ret:>+9.2f}  {pct:>+6.1f}%  {pos:>5}")


def show_chart():
    """Show ASCII chart of equity over time."""
    data = _load_equity()
    snapshots = data.get("snapshots", [])

    if len(snapshots) < 2:
        print("Need at least 2 snapshots for a chart. Run 'equity.py snapshot' periodically.")
        return

    values = [s["total_value"] for s in snapshots]
    dates = [s["timestamp"][:10] for s in snapshots]

    # Downsample if too many points
    max_points = 50
    if len(values) > max_points:
        step = len(values) // max_points
        values = values[::step]
        dates = dates[::step]

    min_val = min(values)
    max_val = max(values)
    val_range = max_val - min_val
    if val_range < 0.01:
        val_range = 1  # Avoid division by zero

    chart_height = 15
    chart_width = len(values)

    print(f"\nEquity Curve (${min_val:.2f} - ${max_val:.2f})")
    print("-" * (chart_width + 12))

    for row in range(chart_height, -1, -1):
        threshold = min_val + (row / chart_height) * val_range
        label = f"${threshold:>7.2f} |"
        line = label
        for v in values:
            normalized = (v - min_val) / val_range * chart_height
            if normalized >= row:
                line += "#"
            else:
                line += " "
        print(line)

    # X-axis
    print(" " * 10 + "+" + "-" * chart_width)
    if len(dates) <= 10:
        print(" " * 10 + " " + "  ".join(d[5:] for d in dates[:10]))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_summary()
    elif sys.argv[1] == "snapshot":
        take_snapshot()
    elif sys.argv[1] == "live":
        take_live_snapshot()
    elif sys.argv[1] == "history":
        show_history()
    elif sys.argv[1] == "chart":
        show_chart()
    else:
        print("Usage:")
        print("  python3 equity.py                # Show summary")
        print("  python3 equity.py snapshot        # Record book value")
        print("  python3 equity.py live            # Record with live prices")
        print("  python3 equity.py history          # Full history")
        print("  python3 equity.py chart            # ASCII chart")
