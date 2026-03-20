"""Monitor open positions and execute stop-losses / take-profits automatically."""
import json
import time
import sys
import os
import httpx
from datetime import datetime, timezone

from proxy_client import get_client, sell, ACTIVE_PROXY
from ledger import _load, _save, record_sell

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

STATE_FILE = os.path.join(os.path.dirname(__file__), "monitor_state.json")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_midpoint(token_id):
    """Get current midpoint price for a token."""
    try:
        client = get_client(with_auth=False)
        mid = client.get_midpoint(token_id)
        if isinstance(mid, dict):
            return float(mid.get("mid", 0))
        return float(mid)
    except Exception as e:
        print(f"  Error getting midpoint: {e}")
        return None


def get_best_bid(token_id):
    """Get best bid price (what we'd sell at)."""
    try:
        client = get_client(with_auth=False)
        book = client.get_order_book(token_id)
        if book.bids:
            return float(book.bids[0].price)
        return None
    except Exception as e:
        print(f"  Error getting book: {e}")
        return None


def get_book_depth(token_id, side="bids"):
    """Get order book depth on a given side. Returns list of (price, size) tuples."""
    try:
        client = get_client(with_auth=False)
        book = client.get_order_book(token_id)
        orders = book.bids if side == "bids" else book.asks
        if not orders:
            return []
        return [(float(o.price), float(o.size)) for o in orders]
    except Exception as e:
        print(f"  Error getting book depth: {e}")
        return []


def check_liquidity(token_id, size, side="bids"):
    """Check if there's enough liquidity to exit a position.
    Returns (executable, avg_price, total_depth) tuple.
    - executable: True if enough depth to fill 'size' shares
    - avg_price: weighted average fill price
    - total_depth: total shares available on this side
    """
    depth = get_book_depth(token_id, side)
    if not depth:
        return False, 0, 0

    total_depth = sum(d[1] for d in depth)
    filled = 0
    cost = 0
    for price, available in depth:
        take = min(available, size - filled)
        cost += take * price
        filled += take
        if filled >= size:
            break

    executable = filled >= size
    avg_price = cost / filled if filled > 0 else 0
    return executable, avg_price, total_depth


def execute_sell(bet, shares, reason):
    """Execute a sell order for a bet."""
    token_id = bet["token_id"]
    rules = bet.get("rules", {})
    tick_size = rules.get("tick_size", "0.01")
    neg_risk = rules.get("neg_risk", False)

    bid = get_best_bid(token_id)
    if bid is None or bid <= 0.001:
        print(f"  WARNING: No valid bid for {bet['market'][:50]}, skipping sell")
        return False

    print(f"  EXECUTING SELL: {bet['market'][:50]} x {shares} @ market (~{bid})")
    print(f"  Reason: {reason}")

    try:
        resp = sell(
            token_id=token_id,
            price=bid,
            size=shares,
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        # OrderResponse may be a dict or object -- check for order ID as success signal
        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        else:
            order_id = getattr(resp, "orderID", None) or getattr(resp, "order_id", None) or getattr(resp, "id", None)

        if order_id:
            try:
                record_sell(bet["id"], bid, shares, notes=reason)
            except Exception as le:
                print(f"  CRITICAL: Sell order {order_id} succeeded but ledger update failed: {le}")
                print(f"  MANUAL FIX NEEDED: bet #{bet['id']}, sold {shares} shares @ {bid}")
            print(f"  SOLD (order {order_id}): {resp}")
            return True
        else:
            print(f"  SELL FAILED (no order ID): {resp}")
            return False
    except Exception as e:
        print(f"  SELL ERROR: {e}")
        return False


def get_monitored_bets():
    """Get all open bets that have monitor rules set."""
    ledger = _load()
    monitored = []
    for bet in ledger["open_bets"]:
        if bet.get("rules") and bet.get("token_id"):
            monitored.append(bet)
    return monitored


def check_rules(verbose=True):
    """Check all positions against their rules."""
    state = load_state()
    actions_taken = []
    bets = get_monitored_bets()

    if not bets and verbose:
        print("  No monitored positions. Use 'ledger.py set-rules' to add rules.")
        return actions_taken

    for bet in bets:
        rules = bet["rules"]
        token_id = bet["token_id"]
        remaining_shares = bet["size"]  # Current size (may be reduced after partial sells)

        if remaining_shares <= 0:
            continue

        # Get current price
        mid = get_midpoint(token_id)
        if mid is None:
            if verbose:
                print(f"  {bet['market'][:50]}: Could not get price, skipping")
            continue

        stop_loss = rules.get("stop_loss")
        tp1 = rules.get("take_profit_1")
        tp2 = rules.get("take_profit_2")
        tp1_pct = rules.get("tp1_pct", 0.50)
        tp1_hit = state.get(f"bet_{bet['id']}_tp1_hit", rules.get("tp1_hit", False))

        if verbose:
            print(f"\n  {bet['market'][:50]}")
            print(f"    Price: {mid:.4f}  |  Shares: {remaining_shares}")
            parts = []
            if stop_loss is not None:
                parts.append(f"Stop: {stop_loss}")
            if tp1 is not None:
                parts.append(f"TP1: {tp1}")
            if tp2 is not None:
                parts.append(f"TP2: {tp2}")
            print(f"    {' | '.join(parts)}")

        # STOP LOSS
        if stop_loss is not None and mid <= stop_loss:
            print(f"  *** STOP LOSS TRIGGERED @ {mid} (limit: {stop_loss}) ***")
            success = execute_sell(bet, remaining_shares, f"Stop loss @ {mid}")
            if success:
                actions_taken.append(f"STOP LOSS: {bet['market'][:40]} sold {remaining_shares} shares @ ~{mid}")
            continue

        # TAKE PROFIT 1
        if not tp1_hit and tp1 is not None and mid >= tp1:
            sell_shares = max(1, round(remaining_shares * tp1_pct))
            if sell_shares > remaining_shares:
                sell_shares = remaining_shares
            if sell_shares > 0:
                print(f"  *** TAKE PROFIT 1 TRIGGERED @ {mid} ***")
                success = execute_sell(bet, sell_shares, f"Take profit 1 @ {mid}")
                if success:
                    state[f"bet_{bet['id']}_tp1_hit"] = True
                    save_state(state)
                    # Also persist tp1_hit in the ledger rules as backup
                    try:
                        ledger = _load()
                        for b in ledger["open_bets"]:
                            if b["id"] == bet["id"] and b.get("rules"):
                                b["rules"]["tp1_hit"] = True
                                break
                        _save(ledger)
                    except Exception as e:
                        print(f"  Warning: Could not persist tp1_hit to ledger: {e}")
                    actions_taken.append(f"TP1: {bet['market'][:40]} sold {sell_shares} shares @ ~{mid}")
            continue

        # TAKE PROFIT 2
        if tp1_hit and tp2 is not None and mid >= tp2:
            print(f"  *** TAKE PROFIT 2 TRIGGERED @ {mid} ***")
            success = execute_sell(bet, remaining_shares, f"Take profit 2 @ {mid}")
            if success:
                actions_taken.append(f"TP2: {bet['market'][:40]} sold {remaining_shares} shares @ ~{mid}")
            continue

        if verbose:
            status = "OK"
            if stop_loss and mid < stop_loss * 1.2:
                status = "NEAR STOP"
            elif tp1 and mid > tp1 * 0.9:
                status = "NEAR TP1"
            print(f"    Status: {status}")

    return actions_taken


def run_loop(interval=60):
    """Continuously monitor positions."""
    print(f"Starting monitor loop (checking every {interval}s)")
    print(f"Proxy: {ACTIVE_PROXY or 'direct (no proxy)'}")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    while True:
        try:
            print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Checking positions...")
            actions = check_rules(verbose=True)

            if actions:
                print("\n  ACTIONS TAKEN:")
                for a in actions:
                    print(f"    -> {a}")
        except Exception as e:
            print(f"  Monitor error: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 120
        run_loop(interval)
    elif len(sys.argv) > 1 and sys.argv[1] == "check":
        check_rules(verbose=True)
    elif len(sys.argv) > 1 and sys.argv[1] == "liquidity":
        if len(sys.argv) < 4:
            print("Usage: python3 monitor.py liquidity <token_id> <size>")
            sys.exit(1)
        token_id = sys.argv[2]
        size = float(sys.argv[3])
        executable, avg_price, total_depth = check_liquidity(token_id, size)
        print(f"Token: {token_id[:16]}...")
        print(f"  Requested: {size} shares")
        print(f"  Executable: {'YES' if executable else 'NO'}")
        print(f"  Avg fill price: {avg_price:.4f}")
        print(f"  Total bid depth: {total_depth:.1f} shares")
    else:
        print("Usage:")
        print("  python3 monitor.py check                        # Check once")
        print("  python3 monitor.py loop [seconds]                # Continuous monitoring")
        print("  python3 monitor.py liquidity <token_id> <size>   # Check exit liquidity")
