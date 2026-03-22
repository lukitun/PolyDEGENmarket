"""Monitor open positions and execute stop-losses / take-profits automatically."""
import json
import time
import sys
import os
import httpx
from datetime import datetime, timezone

from proxy_client import get_client, sell, ACTIVE_PROXY
from ledger import _load, _save, record_sell, get_open_bets
from alerts import alert as log_alert, CRITICAL, WARNING, INFO, TRADE

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

STATE_FILE = os.path.join(os.path.dirname(__file__), "monitor_state.json")

# Price source preference: Gamma API is more reliable than CLOB order book
# (CLOB order book often shows 0.001 phantom bids)
_GAMMA_PRICE_CACHE = {}  # token_id -> (price, timestamp)
_GAMMA_CACHE_TTL = 60  # seconds

# Track consecutive price fetch failures per token for alerting.
# Loaded from/saved to monitor_state.json so failures accumulate across cron invocations.
_PRICE_FAIL_COUNTS = {}  # token_id -> count (loaded from state in check_rules/check_emergency)
_PRICE_FAIL_ALERT_THRESHOLD = 3  # Alert after this many consecutive failures (= 6 minutes with 2min cron)

# Retry config for failed stop-loss sells
_SELL_RETRY_MAX = 2
_SELL_RETRY_DELAY = 3  # seconds between retries


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    """Save monitor state atomically -- write to temp then rename."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def get_gamma_price(token_id):
    """Get price from Gamma API (more reliable than CLOB order book).
    Returns float price or None. Uses a short cache to avoid hammering the API.
    """
    now = time.time()
    cached = _GAMMA_PRICE_CACHE.get(token_id)
    if cached and (now - cached[1]) < _GAMMA_CACHE_TTL:
        return cached[0]

    try:
        resp = httpx.get(f"{GAMMA_API}/markets", params={
            "clob_token_ids": token_id,
        }, timeout=15)
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            return None

        market = markets[0]
        # Parse prices to find which outcome matches our token
        prices_raw = market.get("outcomePrices", "")
        tokens_raw = market.get("clobTokenIds", "")

        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, TypeError):
                return None
        else:
            prices = prices_raw or []

        if isinstance(tokens_raw, str):
            try:
                tokens = json.loads(tokens_raw)
            except (json.JSONDecodeError, TypeError):
                return None
        else:
            tokens = tokens_raw or []

        for i, tid in enumerate(tokens):
            if tid == token_id and i < len(prices):
                price = float(prices[i])
                _GAMMA_PRICE_CACHE[token_id] = (price, now)
                return price

        # Fallback: if only 2 outcomes, first is YES
        if len(prices) >= 1:
            price = float(prices[0])
            _GAMMA_PRICE_CACHE[token_id] = (price, now)
            return price

        return None
    except (httpx.HTTPError, ValueError, KeyError, OSError) as e:
        return None


def get_midpoint(token_id):
    """Get current midpoint price for a token.
    Tries CLOB midpoint first, falls back to Gamma API if CLOB returns garbage.
    """
    clob_price = None
    try:
        client = get_client(with_auth=False)
        mid = client.get_midpoint(token_id)
        if isinstance(mid, dict):
            clob_price = float(mid.get("mid", 0))
        else:
            clob_price = float(mid)
    except Exception as e:
        pass  # Fall through to Gamma

    # Validate CLOB price -- reject obviously broken values
    if clob_price is not None and clob_price > 0.005:
        return clob_price

    # Fallback: Gamma API
    gamma_price = get_gamma_price(token_id)
    if gamma_price is not None and gamma_price > 0:
        return gamma_price

    # If CLOB returned something small but non-zero, use it as last resort
    if clob_price is not None and clob_price > 0:
        return clob_price

    return None


def get_best_bid(token_id):
    """Get best bid price (what we'd sell at).
    Falls back to Gamma API price if CLOB order book shows phantom 0.001 bids.
    """
    try:
        client = get_client(with_auth=False)
        book = client.get_order_book(token_id)
        if book.bids:
            bid = float(book.bids[0].price)
            # Reject phantom bids (the CLOB API bug shows 0.001 bids)
            if bid > 0.005:
                return bid
    except Exception as e:
        pass  # Fall through to Gamma

    # Fallback: use Gamma API midpoint as a proxy for best bid
    # This is less accurate but better than 0.001
    gamma_price = get_gamma_price(token_id)
    if gamma_price is not None and gamma_price > 0.005:
        # Gamma gives midpoint; real bid is slightly lower, but for monitoring
        # purposes this is good enough to avoid phantom 0.001 triggering stop losses
        return gamma_price

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
    """Execute a sell order for a bet. Retries up to _SELL_RETRY_MAX times on failure."""
    token_id = bet["token_id"]
    rules = bet.get("rules", {})
    tick_size = rules.get("tick_size", "0.01")
    neg_risk = rules.get("neg_risk", False)

    for attempt in range(_SELL_RETRY_MAX + 1):
        bid = get_best_bid(token_id)
        if bid is None or bid <= 0.001:
            if attempt < _SELL_RETRY_MAX:
                print(f"  WARNING: No valid bid for {bet['market'][:50]}, retrying in {_SELL_RETRY_DELAY}s... (attempt {attempt + 1})")
                time.sleep(_SELL_RETRY_DELAY)
                # Clear cache so retry fetches fresh price
                _GAMMA_PRICE_CACHE.pop(token_id, None)
                continue
            print(f"  WARNING: No valid bid for {bet['market'][:50]} after {_SELL_RETRY_MAX + 1} attempts, skipping sell")
            log_alert(
                f"SELL ABANDONED (no bid): {bet['market'][:50]} -- no valid bid after {_SELL_RETRY_MAX + 1} attempts",
                severity=CRITICAL, source="monitor"
            )
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
                    log_alert(
                        f"LEDGER FAILURE: Sell order {order_id} placed but ledger update failed for bet #{bet['id']}. "
                        f"MANUAL FIX NEEDED: sold {shares} shares @ {bid}",
                        severity=CRITICAL, source="monitor"
                    )
                print(f"  SOLD (order {order_id}): {resp}")
                return True
            else:
                if attempt < _SELL_RETRY_MAX:
                    print(f"  SELL FAILED (no order ID): {resp} -- retrying in {_SELL_RETRY_DELAY}s... (attempt {attempt + 1})")
                    time.sleep(_SELL_RETRY_DELAY)
                    continue
                print(f"  SELL FAILED (no order ID) after {_SELL_RETRY_MAX + 1} attempts: {resp}")
                return False
        except Exception as e:
            if attempt < _SELL_RETRY_MAX:
                print(f"  SELL ERROR: {e} -- retrying in {_SELL_RETRY_DELAY}s... (attempt {attempt + 1})")
                time.sleep(_SELL_RETRY_DELAY)
                continue
            print(f"  SELL ERROR after {_SELL_RETRY_MAX + 1} attempts: {e}")
            return False

    return False


def get_monitored_bets():
    """Get all open positions that have monitor rules set."""
    bets = get_open_bets()
    return [b for b in bets if b.get("rules") and b.get("token_id")]


def get_all_open_bets():
    """Get ALL open positions (for emergency stop checking)."""
    return [b for b in get_open_bets() if b.get("token_id")]


def check_rules(verbose=True):
    """Check all positions against their rules."""
    global _PRICE_FAIL_COUNTS
    state = load_state()
    _PRICE_FAIL_COUNTS = state.get("price_fail_counts", {})
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
            _PRICE_FAIL_COUNTS[token_id] = _PRICE_FAIL_COUNTS.get(token_id, 0) + 1
            fail_count = _PRICE_FAIL_COUNTS[token_id]
            if verbose:
                print(f"  {bet['market'][:50]}: Could not get price, skipping (fail #{fail_count})")
            if fail_count == _PRICE_FAIL_ALERT_THRESHOLD:
                log_alert(
                    f"PRICE FETCH FAILING: {bet['market'][:50]} -- {fail_count} consecutive failures, "
                    f"stop loss at {rules.get('stop_loss')} will NOT fire until price API recovers",
                    severity=CRITICAL, source="monitor"
                )
            continue
        # Reset failure counter on success
        _PRICE_FAIL_COUNTS.pop(token_id, None)

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
            log_alert(
                f"STOP LOSS TRIGGERED: {bet['market'][:50]} @ {mid} (limit: {stop_loss}), "
                f"selling {remaining_shares} shares",
                severity=CRITICAL, source="monitor"
            )
            success = execute_sell(bet, remaining_shares, f"Stop loss @ {mid}")
            if success:
                actions_taken.append(f"STOP LOSS: {bet['market'][:40]} sold {remaining_shares} shares @ ~{mid}")
                log_alert(
                    f"STOP LOSS EXECUTED: {bet['market'][:50]} sold {remaining_shares} shares @ ~{mid}",
                    severity=TRADE, source="monitor"
                )
            else:
                log_alert(
                    f"STOP LOSS SELL FAILED: {bet['market'][:50]} -- manual intervention needed",
                    severity=CRITICAL, source="monitor"
                )
            continue

        # TAKE PROFIT 1
        if not tp1_hit and tp1 is not None and mid >= tp1:
            sell_shares = max(1, round(remaining_shares * tp1_pct))
            if sell_shares > remaining_shares:
                sell_shares = remaining_shares
            if sell_shares > 0:
                print(f"  *** TAKE PROFIT 1 TRIGGERED @ {mid} ***")
                log_alert(
                    f"TP1 TRIGGERED: {bet['market'][:50]} @ {mid} (target: {tp1}), selling {sell_shares} shares",
                    severity=TRADE, source="monitor"
                )
                success = execute_sell(bet, sell_shares, f"Take profit 1 @ {mid}")
                if success:
                    state[f"bet_{bet['id']}_tp1_hit"] = True
                    save_state(state)
                    # Also persist tp1_hit in the ledger position rules
                    try:
                        ledger = _load()
                        for pos in ledger.get("positions", {}).values():
                            if pos.get("pos_id") == bet["id"] and pos.get("rules"):
                                pos["rules"]["tp1_hit"] = True
                                break
                        _save(ledger)
                    except Exception as e:
                        print(f"  Warning: Could not persist tp1_hit to ledger: {e}")
                    actions_taken.append(f"TP1: {bet['market'][:40]} sold {sell_shares} shares @ ~{mid}")
            continue

        # TAKE PROFIT 2
        if tp1_hit and tp2 is not None and mid >= tp2:
            print(f"  *** TAKE PROFIT 2 TRIGGERED @ {mid} ***")
            log_alert(
                f"TP2 TRIGGERED: {bet['market'][:50]} @ {mid} (target: {tp2}), selling {remaining_shares} shares",
                severity=TRADE, source="monitor"
            )
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

    # Persist failure counts across cron invocations
    state["price_fail_counts"] = _PRICE_FAIL_COUNTS
    save_state(state)

    return actions_taken


def check_emergency_stops(max_loss_pct=0.40, verbose=True):
    """Check ALL open positions for catastrophic losses, even those without rules.

    Any position down more than max_loss_pct from entry gets auto-sold.
    This is the safety net -- catches positions that have no explicit stop set.
    """
    global _PRICE_FAIL_COUNTS
    state = load_state()
    _PRICE_FAIL_COUNTS = state.get("price_fail_counts", {})
    actions_taken = []
    bets = get_all_open_bets()
    ruled_ids = {b["id"] for b in get_monitored_bets()}

    for bet in bets:
        if bet["id"] in ruled_ids:
            continue  # Already covered by normal stop loss rules

        token_id = bet["token_id"]
        entry_price = bet["price"]
        remaining_shares = bet["size"]

        if remaining_shares <= 0:
            continue

        mid = get_midpoint(token_id)
        if mid is None:
            _PRICE_FAIL_COUNTS[token_id] = _PRICE_FAIL_COUNTS.get(token_id, 0) + 1
            fail_count = _PRICE_FAIL_COUNTS[token_id]
            if verbose:
                print(f"  [EMERGENCY] {bet['market'][:50]}: Could not get price (fail #{fail_count})")
            if fail_count == _PRICE_FAIL_ALERT_THRESHOLD:
                log_alert(
                    f"PRICE FETCH FAILING: {bet['market'][:50]} -- {fail_count} consecutive failures, "
                    f"emergency stop will NOT fire until price API recovers",
                    severity=CRITICAL, source="monitor"
                )
            continue
        _PRICE_FAIL_COUNTS.pop(token_id, None)

        loss_pct = (entry_price - mid) / entry_price if entry_price > 0 else 0

        if verbose:
            status = "OK"
            if loss_pct > max_loss_pct * 0.75:
                status = "WARNING"
            if loss_pct > max_loss_pct:
                status = "EMERGENCY SELL"
            print(f"\n  [NO RULES] {bet['market'][:50]}")
            print(f"    Entry: {entry_price:.4f}  |  Now: {mid:.4f}  |  Loss: {loss_pct:.1%}  |  {status}")

        if loss_pct >= max_loss_pct:
            print(f"  *** EMERGENCY STOP: {bet['market'][:50]} down {loss_pct:.1%} (limit {max_loss_pct:.0%}) ***")
            log_alert(
                f"EMERGENCY STOP: {bet['market'][:50]} down {loss_pct:.1%} from entry "
                f"({entry_price} -> {mid}), selling {remaining_shares} shares",
                severity=CRITICAL, source="monitor"
            )
            success = execute_sell(bet, remaining_shares, f"Emergency stop @ {mid} (down {loss_pct:.1%})")
            if success:
                actions_taken.append(f"EMERGENCY: {bet['market'][:40]} sold {remaining_shares} @ ~{mid}")
                log_alert(
                    f"EMERGENCY STOP EXECUTED: {bet['market'][:50]} sold {remaining_shares} @ ~{mid}",
                    severity=TRADE, source="monitor"
                )
            else:
                log_alert(
                    f"EMERGENCY STOP SELL FAILED: {bet['market'][:50]} -- MANUAL INTERVENTION NEEDED",
                    severity=CRITICAL, source="monitor"
                )

    # Persist failure counts across cron invocations
    state["price_fail_counts"] = _PRICE_FAIL_COUNTS
    save_state(state)

    return actions_taken


def sync_limit_orders(verbose=True):
    """Sync GTC limit orders with exchange to detect fills.
    Returns list of fill descriptions.
    """
    actions = []
    try:
        from limit_orders import sync_with_exchange, cleanup_stale
        if verbose:
            print("[LIMIT ORDER SYNC]")
        cleanup_stale()
        changes = sync_with_exchange()
        for c in changes:
            actions.append(f"LIMIT FILL: {c['market'][:40]} {c['level']} @ {c['price']}")
    except ImportError:
        pass  # limit_orders module not available
    except Exception as e:
        if verbose:
            print(f"  Limit order sync error: {e}")
    return actions


def check_all(verbose=True):
    """Run both rule-based stops, emergency stops, and limit order sync. Returns all actions taken."""
    actions = []
    if verbose:
        print("[RULE-BASED STOPS]")
    actions.extend(check_rules(verbose=verbose))
    if verbose:
        print("\n[EMERGENCY STOPS (positions without rules)]")
    actions.extend(check_emergency_stops(verbose=verbose))
    if verbose:
        print()
    actions.extend(sync_limit_orders(verbose=verbose))
    return actions


def run_loop(interval=60):
    """Continuously monitor positions."""
    print(f"Starting monitor loop (checking every {interval}s)")
    print(f"Proxy: {ACTIVE_PROXY or 'direct (no proxy)'}")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    while True:
        try:
            print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Checking positions...")
            actions = check_all(verbose=True)

            if actions:
                print("\n  ACTIONS TAKEN:")
                for a in actions:
                    print(f"    -> {a}")
        except Exception as e:
            print(f"  Monitor error: {e}")
            try:
                log_alert(f"Monitor loop error: {e}", severity=WARNING, source="monitor")
            except Exception:
                pass  # Don't let alert logging kill the monitor

        time.sleep(interval)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 120
        run_loop(interval)
    elif len(sys.argv) > 1 and sys.argv[1] == "check":
        check_all(verbose=True)
    elif len(sys.argv) > 1 and sys.argv[1] == "stops":
        # Quick stop-loss only check (for frequent cron)
        actions = check_all(verbose=False)
        if actions:
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] ACTIONS TAKEN:")
            for a in actions:
                print(f"  -> {a}")
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
        print("  python3 monitor.py check                        # Full check (verbose)")
        print("  python3 monitor.py stops                        # Quick stop-loss check (for cron)")
        print("  python3 monitor.py loop [seconds]                # Continuous monitoring")
        print("  python3 monitor.py liquidity <token_id> <size>   # Check exit liquidity")
