"""Manage GTC limit sell orders on Polymarket for take-profit levels.

Places resting sell orders at TP1/TP2 prices so exits happen automatically,
even when no session is active. Stop losses CANNOT use limit orders (a sell
limit below market would fill immediately), so they remain handled by
monitor.py polling.

State is tracked in limit_orders.json to avoid double-placing orders.

Usage:
    python3 limit_orders.py place              # Place TP orders for all open positions
    python3 limit_orders.py place <bet_id>     # Place TP orders for one position
    python3 limit_orders.py status             # Show all tracked limit orders + exchange status
    python3 limit_orders.py sync               # Sync local state with exchange (detect fills)
    python3 limit_orders.py cancel             # Cancel all our managed limit orders
    python3 limit_orders.py cancel <bet_id>    # Cancel limit orders for one position
    python3 limit_orders.py replace <bet_id>   # Cancel + re-place orders (after level change)
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

from proxy_client import get_client, ACTIVE_PROXY
from py_clob_client.clob_types import OrderArgs, CreateOrderOptions, OrderType, OpenOrderParams
from ledger import _load, _save, get_open_bets
from alerts import alert as log_alert, TRADE, WARNING, INFO

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "limit_orders.json")


def _load_state():
    """Load limit order tracking state."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"orders": [], "last_sync": None}


def _save_state(state):
    """Save limit order tracking state atomically."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def _round_price(price, tick_size="0.01"):
    """Round price to valid tick size."""
    tick = float(tick_size)
    return round(round(price / tick) * tick, 4)


def _find_tracked_order(state, bet_id, level):
    """Find an existing tracked order for a bet+level combo."""
    for o in state["orders"]:
        if o["bet_id"] == bet_id and o["level"] == level and o["status"] == "LIVE":
            return o
    return None


def place_sell_order(token_id, price, size, tick_size="0.01", neg_risk=False):
    """Place a GTC limit sell order on the exchange.

    Returns the order ID on success, None on failure.
    """
    from py_clob_client.order_builder.constants import SELL

    price = _round_price(price, tick_size)

    # Validate price range
    tick = float(tick_size)
    if price < tick or price > 1 - tick:
        print(f"  ERROR: Price {price} outside valid range ({tick}, {1 - tick})")
        return None

    try:
        client = get_client(with_auth=True)
        order = client.create_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=SELL),
            CreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
        )
        resp = client.post_order(order, orderType=OrderType.GTC)

        # Extract order ID
        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        else:
            order_id = (
                getattr(resp, "orderID", None)
                or getattr(resp, "order_id", None)
                or getattr(resp, "id", None)
            )

        if order_id:
            from proxy_client import record_proxy_success
            record_proxy_success()
            return order_id
        else:
            print(f"  ORDER REJECTED (no order ID): {resp}")
            return None

    except Exception as e:
        print(f"  ORDER ERROR: {e}")
        from proxy_client import record_proxy_failure
        record_proxy_failure()
        return None


def cancel_order(order_id):
    """Cancel a single order on the exchange. Returns True on success."""
    try:
        client = get_client(with_auth=True)
        resp = client.cancel(order_id)
        return True
    except Exception as e:
        print(f"  CANCEL ERROR for {order_id}: {e}")
        return False


def get_exchange_orders():
    """Fetch all open orders from the exchange.

    Returns list of order dicts, or empty list on failure.
    """
    try:
        client = get_client(with_auth=True)
        orders = client.get_orders()
        return orders if orders else []
    except Exception as e:
        print(f"  ERROR fetching exchange orders: {e}")
        return []


def get_order_status(order_id):
    """Look up a specific order on the exchange to check if it was filled.

    Returns a dict with at least 'status' key, or None on failure.
    Possible statuses from Polymarket CLOB: 'live', 'matched', 'cancelled', 'delayed'.
    """
    try:
        client = get_client(with_auth=True)
        order = client.get_order(order_id)
        if isinstance(order, dict):
            return order
        # Object response -- extract what we can
        return {
            "status": getattr(order, "status", None) or getattr(order, "order_status", None),
            "size_matched": getattr(order, "size_matched", None),
        }
    except Exception as e:
        # API may return 404 for very old orders
        return None


def place_tp_orders_for_bet(bet, dry_run=False):
    """Place TP1 and TP2 limit sell orders for a single bet.

    Returns list of placed order records.
    """
    state = _load_state()
    rules = bet.get("rules", {})
    token_id = bet.get("token_id")
    bet_id = bet["id"]
    remaining = bet["size"]

    if not token_id:
        print(f"  #{bet_id}: No token_id, skipping")
        return []

    if not rules:
        print(f"  #{bet_id}: No rules set, skipping")
        return []

    tp1 = rules.get("take_profit_1")
    tp2 = rules.get("take_profit_2")
    tp1_pct = rules.get("tp1_pct", 0.50)
    tp1_hit = rules.get("tp1_hit", False)
    tick_size = rules.get("tick_size", "0.01")
    neg_risk = rules.get("neg_risk", False)

    placed = []

    # TP1: sell tp1_pct of position at TP1 price (skip if already hit)
    if tp1 is not None and not tp1_hit:
        tp1_price = _round_price(tp1, tick_size)
        tick_val = float(tick_size)
        if tp1_price > 1 - tick_val:
            print(f"  #{bet_id} TP1: Price {tp1} rounds to {tp1_price} (>= max {1 - tick_val}), hold to resolution")
        else:
            existing = _find_tracked_order(state, bet_id, "TP1")
            if existing:
                print(f"  #{bet_id} TP1: Already placed (order {existing['order_id'][:16]}...)")
            else:
                tp1_shares = max(1, round(remaining * tp1_pct))
                if tp1_shares > remaining:
                    tp1_shares = remaining

                print(f"  #{bet_id} TP1: SELL {tp1_shares} shares @ {tp1_price}")
                print(f"         Market: {bet['market'][:50]}")

                if dry_run:
                    print(f"         [DRY RUN -- no order placed]")
                else:
                    order_id = place_sell_order(token_id, tp1_price, tp1_shares,
                                               tick_size=tick_size, neg_risk=neg_risk)
                    if order_id:
                        record = {
                            "bet_id": bet_id,
                            "level": "TP1",
                            "order_id": order_id,
                            "token_id": token_id,
                            "price": tp1_price,
                            "size": tp1_shares,
                            "market": bet["market"],
                            "status": "LIVE",
                            "placed_at": datetime.now(timezone.utc).isoformat(),
                        }
                        state["orders"].append(record)
                        _save_state(state)
                        placed.append(record)
                        print(f"         ORDER PLACED: {order_id[:24]}...")
                        log_alert(
                            f"TP1 limit sell placed: {bet['market'][:40]} "
                            f"@ {tp1_price} x {tp1_shares} [order {order_id[:16]}]",
                            severity=TRADE, source="limit_orders"
                        )
                    else:
                        print(f"         FAILED to place order")

    # TP2: sell remaining shares at TP2 price
    if tp2 is not None:
        tp2_price = _round_price(tp2, tick_size)
        tick_val = float(tick_size)
        if tp2_price > 1 - tick_val:
            print(f"  #{bet_id} TP2: Price {tp2} rounds to {tp2_price} (>= max {1 - tick_val}), hold to resolution")
        elif tp2 == tp1:
            pass  # Same price as TP1, skip (TP1 covers it)
        else:
            existing = _find_tracked_order(state, bet_id, "TP2")
            if existing:
                print(f"  #{bet_id} TP2: Already placed (order {existing['order_id'][:16]}...)")
            else:
                # Calculate TP2 size: shares remaining after TP1 would fill
                if tp1 is not None and not tp1_hit:
                    tp1_shares_calc = max(1, round(remaining * tp1_pct))
                    tp2_shares = remaining - tp1_shares_calc
                else:
                    tp2_shares = remaining

                if tp2_shares <= 0:
                    print(f"  #{bet_id} TP2: No shares remaining after TP1, skipping")
                else:
                    print(f"  #{bet_id} TP2: SELL {tp2_shares} shares @ {tp2_price}")
                    print(f"         Market: {bet['market'][:50]}")

                    if dry_run:
                        print(f"         [DRY RUN -- no order placed]")
                    else:
                        order_id = place_sell_order(token_id, tp2_price, tp2_shares,
                                                   tick_size=tick_size, neg_risk=neg_risk)
                        if order_id:
                            record = {
                                "bet_id": bet_id,
                                "level": "TP2",
                                "order_id": order_id,
                                "token_id": token_id,
                                "price": tp2_price,
                                "size": tp2_shares,
                                "market": bet["market"],
                                "status": "LIVE",
                                "placed_at": datetime.now(timezone.utc).isoformat(),
                            }
                            state["orders"].append(record)
                            _save_state(state)
                            placed.append(record)
                            print(f"         ORDER PLACED: {order_id[:24]}...")
                            log_alert(
                                f"TP2 limit sell placed: {bet['market'][:40]} "
                                f"@ {tp2_price} x {tp2_shares} [order {order_id[:16]}]",
                                severity=TRADE, source="limit_orders"
                            )
                        else:
                            print(f"         FAILED to place order")

    return placed


def place_all(bet_id_filter=None, dry_run=False):
    """Place TP limit orders for all open positions (or a specific one).

    Args:
        bet_id_filter: If set, only place for this bet ID.
        dry_run: If True, show what would be placed without placing.
    """
    bets = get_open_bets()

    if bet_id_filter is not None:
        bets = [b for b in bets if b["id"] == bet_id_filter]
        if not bets:
            print(f"No open position with ID {bet_id_filter}")
            return []

    eligible = [b for b in bets if b.get("rules") and b.get("token_id")]

    if not eligible:
        print("No eligible positions (need rules + token_id)")
        return []

    print(f"{'[DRY RUN] ' if dry_run else ''}Placing TP limit orders for {len(eligible)} position(s)...")
    print(f"Proxy: {ACTIVE_PROXY or 'direct'}")
    print()

    # NOTE about stop losses
    print("NOTE: Stop losses are NOT placed as limit orders.")
    print("      A sell limit below market would fill immediately.")
    print("      Stop losses remain handled by monitor.py polling.")
    print()

    all_placed = []
    for bet in eligible:
        placed = place_tp_orders_for_bet(bet, dry_run=dry_run)
        all_placed.extend(placed)
        print()

    if all_placed:
        print(f"Placed {len(all_placed)} order(s) on the exchange.")
    elif not dry_run:
        print("No new orders needed (all TPs already placed or no TP levels set).")

    return all_placed


def show_status():
    """Show all tracked limit orders and their status."""
    state = _load_state()

    if not state["orders"]:
        print("No tracked limit orders.")
        print("Run 'python3 limit_orders.py place' to place TP orders.")
        return

    live = [o for o in state["orders"] if o["status"] == "LIVE"]
    filled = [o for o in state["orders"] if o["status"] == "FILLED"]
    cancelled = [o for o in state["orders"] if o["status"] == "CANCELLED"]

    print("=" * 65)
    print("LIMIT ORDER STATUS")
    print("=" * 65)

    if live:
        print(f"\n  LIVE ORDERS ({len(live)}):")
        for o in live:
            print(f"    Bet #{o['bet_id']} {o['level']}: SELL {o['size']} @ {o['price']}")
            print(f"      Market: {o['market'][:50]}")
            print(f"      Order:  {o['order_id'][:30]}...")
            print(f"      Placed: {o['placed_at'][:19]}")

    if filled:
        print(f"\n  FILLED ORDERS ({len(filled)}):")
        for o in filled:
            print(f"    Bet #{o['bet_id']} {o['level']}: SELL {o['size']} @ {o['price']}  [FILLED]")
            if o.get("filled_at"):
                print(f"      Filled: {o['filled_at'][:19]}")

    if cancelled:
        print(f"\n  CANCELLED ORDERS ({len(cancelled)}):")
        for o in cancelled[-5:]:  # Last 5
            print(f"    Bet #{o['bet_id']} {o['level']}: {o['size']} @ {o['price']}")

    print("=" * 65)

    if state.get("last_sync"):
        print(f"Last sync: {state['last_sync'][:19]}")


def sync_with_exchange():
    """Sync local state with exchange orders.

    Detects filled/cancelled orders and updates ledger accordingly.
    Returns list of state changes.
    """
    state = _load_state()
    live_orders = [o for o in state["orders"] if o["status"] == "LIVE"]

    if not live_orders:
        print("No live orders to sync.")
        state["last_sync"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        return []

    print(f"Syncing {len(live_orders)} live order(s) with exchange...")

    # Fetch all open orders from exchange
    exchange_orders = get_exchange_orders()
    exchange_ids = set()
    for eo in exchange_orders:
        if isinstance(eo, dict):
            eid = eo.get("id") or eo.get("orderID") or eo.get("order_id")
        else:
            eid = getattr(eo, "id", None) or getattr(eo, "orderID", None)
        if eid:
            exchange_ids.add(eid)

    changes = []

    for order in live_orders:
        oid = order["order_id"]
        if oid not in exchange_ids:
            # Order is no longer in open orders -- verify status via single-order lookup
            # before recording a fill in the ledger (could be cancelled/expired)
            order_detail = get_order_status(oid)
            actual_status = None
            if order_detail and isinstance(order_detail, dict):
                actual_status = (order_detail.get("status") or "").lower()

            if actual_status == "matched":
                order["status"] = "FILLED"
                order["filled_at"] = datetime.now(timezone.utc).isoformat()
                print(f"  Bet #{order['bet_id']} {order['level']}: FILLED (confirmed by exchange)")
            elif actual_status == "cancelled":
                order["status"] = "CANCELLED"
                order["cancelled_at"] = datetime.now(timezone.utc).isoformat()
                print(f"  Bet #{order['bet_id']} {order['level']}: CANCELLED by exchange (NOT filled)")
                log_alert(
                    f"{order['level']} order CANCELLED by exchange (not filled): "
                    f"{order['market'][:40]} -- order may need re-placing",
                    severity=WARNING, source="limit_orders"
                )
                continue  # Do NOT update ledger for cancelled orders
            elif actual_status:
                # Unknown status -- do not assume filled
                print(f"  Bet #{order['bet_id']} {order['level']}: status='{actual_status}' (unknown, skipping)")
                log_alert(
                    f"{order['level']} order has unexpected status '{actual_status}': "
                    f"{order['market'][:40]} -- manual check needed",
                    severity=WARNING, source="limit_orders"
                )
                continue
            else:
                # Could not look up order (API error or 404) -- do NOT assume filled.
                # Recording a phantom fill would corrupt the ledger. Skip and retry next sync.
                print(f"  Bet #{order['bet_id']} {order['level']}: UNKNOWN (API error, will retry next sync)")
                log_alert(
                    f"{order['level']} order status unknown (API error): "
                    f"{order['market'][:40]} -- will retry next sync. Check manually if persists.",
                    severity=WARNING, source="limit_orders"
                )
                continue  # Do NOT update ledger -- try again next sync

            # Update ledger -- record the sell
            try:
                from ledger import record_sell
                sell_rec = record_sell(
                    order["bet_id"],
                    order["price"],
                    order["size"],
                    notes=f"{order['level']} limit order filled"
                )
                if sell_rec:
                    print(f"    Ledger updated: sold {order['size']} @ {order['price']}")
                    log_alert(
                        f"{order['level']} FILLED: {order['market'][:40]} "
                        f"@ {order['price']} x {order['size']}",
                        severity=TRADE, source="limit_orders"
                    )

                    # If TP1 filled, mark tp1_hit in the ledger position rules
                    if order["level"] == "TP1":
                        ledger = _load()
                        for pos in ledger.get("positions", {}).values():
                            if pos.get("pos_id") == order["bet_id"] and pos.get("rules"):
                                pos["rules"]["tp1_hit"] = True
                                break
                        _save(ledger)

            except Exception as e:
                print(f"    WARNING: Ledger update failed: {e}")
                print(f"    MANUAL FIX NEEDED: bet #{order['bet_id']} sold {order['size']} @ {order['price']}")
                log_alert(
                    f"LEDGER FAILURE: {order['level']} fill for bet #{order['bet_id']} "
                    f"not recorded. MANUAL FIX: sold {order['size']} @ {order['price']}",
                    severity="CRITICAL", source="limit_orders"
                )

            changes.append(order)

    state["last_sync"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    if not changes:
        print("  All orders still live on exchange.")
    else:
        print(f"\n  {len(changes)} order(s) filled since last sync.")

    return changes


def cancel_orders(bet_id_filter=None):
    """Cancel managed limit orders on the exchange.

    Args:
        bet_id_filter: If set, only cancel orders for this bet ID.
    """
    state = _load_state()
    live = [o for o in state["orders"] if o["status"] == "LIVE"]

    if bet_id_filter is not None:
        live = [o for o in live if o["bet_id"] == bet_id_filter]

    if not live:
        target = f" for bet #{bet_id_filter}" if bet_id_filter else ""
        print(f"No live orders to cancel{target}.")
        return

    print(f"Cancelling {len(live)} order(s)...")
    cancelled = 0

    for order in live:
        print(f"  Bet #{order['bet_id']} {order['level']}: {order['order_id'][:24]}...", end="")
        ok = cancel_order(order["order_id"])
        if ok:
            order["status"] = "CANCELLED"
            order["cancelled_at"] = datetime.now(timezone.utc).isoformat()
            cancelled += 1
            print(" CANCELLED")
        else:
            # May already be filled/cancelled on exchange
            print(" FAILED (may already be filled)")
            order["status"] = "UNKNOWN"

    _save_state(state)
    print(f"\nCancelled {cancelled}/{len(live)} orders.")


def replace_orders(bet_id):
    """Cancel existing orders for a bet and re-place with current rules.

    Use this after changing TP levels with ledger.py set-rules.
    """
    print(f"Replacing orders for bet #{bet_id}...")
    print()

    # Cancel existing
    cancel_orders(bet_id_filter=bet_id)
    print()

    # Remove old CANCELLED/UNKNOWN entries for this bet to allow fresh placement
    state = _load_state()
    state["orders"] = [
        o for o in state["orders"]
        if not (o["bet_id"] == bet_id and o["status"] in ("CANCELLED", "UNKNOWN"))
    ]
    _save_state(state)

    # Re-place
    place_all(bet_id_filter=bet_id)


def cleanup_stale():
    """Remove tracked orders for bets that are no longer open."""
    state = _load_state()
    open_ids = {b["id"] for b in get_open_bets()}

    stale = [o for o in state["orders"] if o["bet_id"] not in open_ids and o["status"] == "LIVE"]

    if not stale:
        return

    print(f"Found {len(stale)} orders for closed positions, cancelling...")
    for order in stale:
        cancel_order(order["order_id"])
        order["status"] = "STALE_CANCELLED"
        order["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        print(f"  Cancelled stale order for closed bet #{order['bet_id']}")

    _save_state(state)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 limit_orders.py place [bet_id]   # Place TP limit orders")
        print("  python3 limit_orders.py dry [bet_id]     # Dry run (show what would be placed)")
        print("  python3 limit_orders.py status            # Show tracked limit orders")
        print("  python3 limit_orders.py sync              # Detect filled orders, update ledger")
        print("  python3 limit_orders.py cancel [bet_id]   # Cancel managed limit orders")
        print("  python3 limit_orders.py replace <bet_id>  # Cancel + re-place after level change")
        print()
        print("NOTE: Only take-profit orders are placed as GTC limits.")
        print("      Stop losses cannot use limit orders (would fill immediately).")
        print("      Stop losses are handled by 'python3 monitor.py loop'.")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "place":
        bet_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
        place_all(bet_id_filter=bet_id)

    elif cmd == "dry":
        bet_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
        place_all(bet_id_filter=bet_id, dry_run=True)

    elif cmd == "status":
        show_status()

    elif cmd == "sync":
        cleanup_stale()
        sync_with_exchange()

    elif cmd == "cancel":
        bet_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
        cancel_orders(bet_id_filter=bet_id)

    elif cmd == "replace":
        if len(sys.argv) < 3:
            print("Usage: python3 limit_orders.py replace <bet_id>")
            sys.exit(1)
        replace_orders(int(sys.argv[2]))

    else:
        print(f"Unknown command: {cmd}")
        print("Run 'python3 limit_orders.py' for usage.")
        sys.exit(1)
