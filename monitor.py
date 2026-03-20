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
        if resp.get("success"):
            record_sell(bet["id"], bid, shares, notes=reason)
            print(f"  SOLD: {resp}")
            return True
        else:
            print(f"  SELL FAILED: {resp}")
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
        total_shares = bet["size"]

        if total_shares <= 0:
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
            print(f"    Price: {mid:.4f}  |  Shares: {total_shares}")
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
            success = execute_sell(bet, total_shares, f"Stop loss @ {mid}")
            if success:
                actions_taken.append(f"STOP LOSS: {bet['market'][:40]} sold {total_shares} shares @ ~{mid}")
            continue

        # TAKE PROFIT 1
        if not tp1_hit and tp1 is not None and mid >= tp1:
            sell_shares = int(total_shares * tp1_pct)
            if sell_shares > 0:
                print(f"  *** TAKE PROFIT 1 TRIGGERED @ {mid} ***")
                success = execute_sell(bet, sell_shares, f"Take profit 1 @ {mid}")
                if success:
                    state[f"bet_{bet['id']}_tp1_hit"] = True
                    save_state(state)
                    actions_taken.append(f"TP1: {bet['market'][:40]} sold {sell_shares} shares @ ~{mid}")
            continue

        # TAKE PROFIT 2
        if tp1_hit and tp2 is not None and mid >= tp2:
            print(f"  *** TAKE PROFIT 2 TRIGGERED @ {mid} ***")
            success = execute_sell(bet, total_shares, f"Take profit 2 @ {mid}")
            if success:
                actions_taken.append(f"TP2: {bet['market'][:40]} sold {total_shares} shares @ ~{mid}")
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

                # Optional: sync to Google Drive after trades
                try:
                    from gdrive import sync_all
                    print("\n  Syncing to Google Drive...")
                    sync_all()
                except Exception as ge:
                    print(f"  GDrive sync skipped: {ge}")
        except Exception as e:
            print(f"  Monitor error: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 120
        run_loop(interval)
    elif len(sys.argv) > 1 and sys.argv[1] == "check":
        check_rules(verbose=True)
    else:
        print("Usage:")
        print("  python3 monitor.py check          # Check once")
        print("  python3 monitor.py loop [seconds]  # Continuous monitoring")
