"""Unified trade execution -- places order and records to ledger atomically.

Combines trade.py (order placement) and ledger.py (record keeping) into a
single flow that ensures every executed trade is tracked. Prevents the
scenario where a trade succeeds but the ledger update is forgotten.

Usage:
    python3 execute.py buy <token_id> <price> <size> <market_name> <side> [options]
    python3 execute.py sell <bet_id> <price> [size] [notes]
    python3 execute.py adjust <bet_id> <actual_filled_size> [notes]  # Fix partial fill
    python3 execute.py resolve <bet_id> <won|lost> [notes]
    python3 execute.py dry-buy <token_id> <price> <size>  # Dry run (no order placed)
    python3 execute.py place-limits [bet_id]              # Place GTC TP sell orders
    python3 execute.py limit-status                       # Show active limit orders
    python3 execute.py limit-sync                         # Detect filled TP orders
    python3 execute.py limit-cancel [bet_id]              # Cancel TP limit orders
    python3 execute.py limit-replace <bet_id>             # Re-place after level change

Options for buy:
    --stop <price>      Set stop-loss price
    --tp1 <price>       Set take-profit-1 price
    --tp2 <price>       Set take-profit-2 price
    --tp1-pct <float>   Fraction to sell at TP1 (default: 0.50)
    --tick <size>        Tick size (default: 0.01)
    --neg-risk           Enable neg-risk mode
    --notes <text>       Trade notes
    --force              Override 20% concentration block
"""
import sys
import os
import json
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger import _load, _save, get_funds, get_max_bet, record_buy, record_sell, record_resolution, get_open_bets, _find_position
from monitor import check_liquidity
from alerts import alert as log_alert, CRITICAL, TRADE


def preflight_buy(token_id, price, size, market_name="", side="YES", force=False):
    """Run pre-trade checks. Returns (ok, messages) tuple.

    When force=False (default), the trade is BLOCKED if combined exposure
    on the same token would exceed 20% of portfolio. Use --force to override.
    """
    messages = []
    ok = True

    cost = round(price * size, 6)
    funds = get_funds()
    max_bet = get_max_bet()

    # Check funds
    if cost > funds:
        messages.append(f"BLOCKED: Cost ${cost:.2f} exceeds available funds ${funds:.2f}")
        ok = False

    # Check 20% rule for this trade alone
    if cost > max_bet:
        messages.append(f"WARNING: Cost ${cost:.2f} exceeds 20% limit ${max_bet:.2f}")

    # Check combined exposure on same token (hard block unless --force)
    open_bets = get_open_bets()
    existing_cost = sum(
        b.get("cost", 0) for b in open_bets
        if b.get("token_id") == token_id
    )
    total_value = funds + sum(b.get("cost", 0) for b in open_bets)

    # Check total new-trade concentration (even without existing position)
    new_pct = (cost / total_value * 100) if total_value > 0 else 0
    if new_pct > 20 and existing_cost == 0:
        if force:
            messages.append(f"WARNING: Trade is {new_pct:.1f}% of portfolio (20% limit) -- FORCED through")
        else:
            messages.append(f"BLOCKED: Trade is {new_pct:.1f}% of portfolio (20% limit). Use --force to override.")
            ok = False

    if existing_cost > 0:
        combined = existing_cost + cost
        pct = (combined / total_value * 100) if total_value > 0 else 0
        messages.append(f"NOTE: Existing position ${existing_cost:.2f}, combined ${combined:.2f} ({pct:.1f}%)")
        if pct > 20:
            if force:
                messages.append(f"WARNING: Combined position {pct:.1f}% exceeds 20% limit -- FORCED through")
            else:
                messages.append(f"BLOCKED: Combined position {pct:.1f}% exceeds 20% limit. Use --force to override.")
                ok = False

    # Price sanity check
    if price <= 0 or price >= 1:
        messages.append(f"BLOCKED: Price {price} is outside valid range (0, 1)")
        ok = False

    if size <= 0:
        messages.append(f"BLOCKED: Size must be positive, got {size}")
        ok = False

    return ok, messages


def execute_buy(token_id, price, size, market_name, side="YES",
                stop_loss=None, take_profit_1=None, take_profit_2=None,
                tp1_pct=0.50, tick_size="0.01", neg_risk=False, notes="",
                dry_run=False, force=False):
    """Execute a buy order and record it in the ledger atomically.

    Returns the ledger bet record on success, None on failure.
    If force=True, bypasses the 20% concentration block.
    """
    cost = round(price * size, 6)

    # Preflight checks
    ok, messages = preflight_buy(token_id, price, size, market_name, side, force=force)

    # Print the pre-trade checklist
    _print_pretrade_checklist(
        token_id, price, size, market_name, side,
        stop_loss, take_profit_1, ok, messages
    )

    if not ok:
        print("\nTrade BLOCKED by preflight checks.")
        return None

    if dry_run:
        print(f"\n  DRY RUN -- no order placed.")
        return {"dry_run": True, "cost": cost}

    # Place the order (GTC limit order -- sits on book until filled)
    print(f"\n  Placing BUY order (GTC limit): {market_name} ({side}) @ {price} x {size} = ${cost:.2f}")
    try:
        from proxy_client import buy as proxy_buy
        resp = proxy_buy(
            token_id=token_id,
            price=price,
            size=size,
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
    except Exception as e:
        print(f"  ORDER FAILED: {e}")
        print(f"  Ledger NOT updated (no order was placed).")
        return None

    # Check if order was accepted
    order_id = None
    if isinstance(resp, dict):
        order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
    else:
        order_id = getattr(resp, "orderID", None) or getattr(resp, "order_id", None) or getattr(resp, "id", None)

    if not order_id:
        print(f"  ORDER REJECTED (no order ID): {resp}")
        print(f"  Ledger NOT updated.")
        return None

    print(f"  ORDER ACCEPTED: {order_id}")
    print(f"  Order type: GTC (limit) -- will sit on book until filled or cancelled")

    # Record in ledger
    try:
        bet = record_buy(
            market=market_name,
            side=side,
            price=price,
            size=size,
            token_id=token_id,
            notes=notes,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            tp1_pct=tp1_pct,
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        print(f"  Ledger updated: bet #{bet['id']}")
        print(f"  NOTE: GTC order -- verify fill on Polymarket. If unfilled, cancel with:")
        print(f"    python3 -c \"from proxy_client import get_client; get_client(with_auth=True).cancel('{order_id}')\"")
        log_alert(
            f"BUY (GTC): {market_name} ({side}) @ {price} x {size} = ${cost:.2f} [bet #{bet['id']}] order={order_id[:16]}",
            severity=TRADE, source="execute"
        )
        return bet
    except Exception as e:
        print(f"  CRITICAL: Order {order_id} was placed but ledger update FAILED: {e}")
        print(f"  MANUAL FIX NEEDED: record buy for {market_name} @ {price} x {size}")
        print(f"  Token: {token_id}")
        log_alert(
            f"LEDGER FAILURE: Buy order {order_id} placed but not recorded. "
            f"{market_name} @ {price} x {size}. MANUAL FIX NEEDED.",
            severity=CRITICAL, source="execute"
        )
        return None


def execute_sell(bet_id, price, size=None, notes="", check_liq=True):
    """Execute a sell order for an existing position and update the ledger.

    Returns the sell record on success, None on failure.
    """
    ledger = _load()

    # Find the position
    pos = _find_position(ledger, bet_id)
    if not pos:
        print(f"  No open position with ID {bet_id}")
        return None

    if pos["status"] != "OPEN":
        print(f"  Position #{pos['pos_id']} is {pos['status']}, not OPEN")
        return None

    token_id = pos["token_id"]
    if not token_id:
        print(f"  Position #{pos['pos_id']} has no token_id -- cannot sell")
        return None

    bet = {"id": pos["pos_id"], "market": pos["market"], "token_id": token_id,
           "size": pos["total_shares"], "rules": pos.get("rules", {})}

    sell_size = size if size is not None else pos["total_shares"]
    if sell_size > pos["total_shares"]:
        print(f"  WARNING: Sell size {sell_size} exceeds position {pos['total_shares']}, clamping.")
        sell_size = pos["total_shares"]

    rules = pos.get("rules", {})
    tick_size = rules.get("tick_size", "0.01")
    neg_risk = rules.get("neg_risk", False)

    # Liquidity check
    if check_liq:
        executable, avg_price, total_depth = check_liquidity(token_id, sell_size)
        if not executable:
            print(f"  WARNING: Insufficient liquidity. Depth: {total_depth:.1f} shares, need {sell_size}")
            print(f"  Proceeding anyway -- order may partially fill.")

    # Place the sell order (GTC limit order -- sits on book until filled)
    print(f"\n  Placing SELL order (GTC limit): {bet['market'][:50]} @ {price} x {sell_size}")
    try:
        from proxy_client import sell as proxy_sell
        resp = proxy_sell(
            token_id=token_id,
            price=price,
            size=sell_size,
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
    except Exception as e:
        print(f"  ORDER FAILED: {e}")
        print(f"  Ledger NOT updated.")
        return None

    # Check if order was accepted
    order_id = None
    if isinstance(resp, dict):
        order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
    else:
        order_id = getattr(resp, "orderID", None) or getattr(resp, "order_id", None) or getattr(resp, "id", None)

    if not order_id:
        print(f"  ORDER REJECTED (no order ID): {resp}")
        print(f"  Ledger NOT updated.")
        return None

    print(f"  ORDER ACCEPTED: {order_id}")
    print(f"  Order type: GTC (limit) -- will sit on book until filled or cancelled")

    # Record in ledger
    try:
        sell_rec = record_sell(bet_id, price, sell_size, notes=notes)
        print(f"  Ledger updated.")
        print(f"  NOTE: GTC order -- verify fill on Polymarket. If unfilled, cancel with:")
        print(f"    python3 -c \"from proxy_client import get_client; get_client(with_auth=True).cancel('{order_id}')\"")
        log_alert(
            f"SELL (GTC): {bet['market'][:50]} @ {price} x {sell_size} [bet #{bet_id}] order={order_id[:16]}",
            severity=TRADE, source="execute"
        )
        return sell_rec
    except Exception as e:
        print(f"  CRITICAL: Sell order {order_id} succeeded but ledger update FAILED: {e}")
        print(f"  MANUAL FIX NEEDED: record sell for bet #{bet_id} @ {price} x {sell_size}")
        log_alert(
            f"LEDGER FAILURE: Sell order {order_id} placed but not recorded. "
            f"Bet #{bet_id} @ {price} x {sell_size}. MANUAL FIX NEEDED.",
            severity=CRITICAL, source="execute"
        )
        return None


def adjust_partial_fill(bet_id, actual_filled_size, notes=""):
    """Adjust ledger after a partial fill.

    When a sell order was recorded as fully filled but only partially filled
    on-chain, this function corrects the ledger:
    1. Finds the sell trade record for the bet
    2. Adjusts the sell record to reflect the actual filled size
    3. Reopens the unfilled portion as an open bet
    4. Corrects funds and PnL accordingly

    Args:
        bet_id: The original bet ID (the one that was sold)
        actual_filled_size: How many shares actually filled on-chain
        notes: Explanation note
    """
    ledger = _load()

    # Find the sell event for this position
    sell_event = None
    for e in ledger["events"]:
        if e.get("type") == "SELL" and (e.get("pos_id") == bet_id or e.get("original_bet_id") == bet_id):
            sell_event = e  # Use last sell for this position

    if not sell_event:
        print(f"  No sell event found for position #{bet_id}")
        return None

    original_sell_size = sell_event["size"]
    if actual_filled_size >= original_sell_size:
        print(f"  Actual fill ({actual_filled_size}) >= recorded sell ({original_sell_size}). No adjustment needed.")
        return None

    unfilled = round(original_sell_size - actual_filled_size, 6)
    sell_price = sell_event["sell_price"]
    buy_price = sell_event["buy_price"]

    # Correct the sell event
    old_revenue = sell_event["revenue"]
    old_pnl = sell_event["pnl"]
    new_revenue = round(sell_price * actual_filled_size, 6)
    new_pnl = round(new_revenue - buy_price * actual_filled_size, 6)

    sell_event["size"] = actual_filled_size
    sell_event["revenue"] = new_revenue
    sell_event["pnl"] = new_pnl
    sell_event["notes"] = (sell_event.get("notes", "") + f" [ADJUSTED: partial fill, {unfilled} shares unfilled]").strip()

    # Correct funds
    revenue_diff = round(old_revenue - new_revenue, 6)
    ledger["funds"] = round(ledger["funds"] - revenue_diff, 6)

    # Correct PnL
    pnl_diff = round(old_pnl - new_pnl, 6)
    ledger["pnl_total"] = round(ledger["pnl_total"] - pnl_diff, 6)

    # Reopen the position with unfilled shares
    pos = _find_position(ledger, bet_id)
    if pos:
        pos["status"] = "OPEN"
        pos["total_shares"] = round(pos["total_shares"] + unfilled, 6)
        pos["total_cost"] = round(pos["avg_price"] * pos["total_shares"], 6)
        pos["realized_pnl"] = round(pos.get("realized_pnl", 0) - pnl_diff, 6)

    _save(ledger)

    print(f"  PARTIAL FILL ADJUSTMENT for bet #{bet_id}:")
    print(f"    Recorded sell: {original_sell_size} shares")
    print(f"    Actual fill:   {actual_filled_size} shares")
    print(f"    Unfilled:      {unfilled} shares (reopened)")
    print(f"    Revenue adj:   ${revenue_diff:+.2f}")
    print(f"    PnL adj:       ${pnl_diff:+.2f}")
    print(f"    Notes:         {notes}")
    return {"adjusted": True, "unfilled": unfilled}


def _check_play_file(market_name):
    """Check if a play file exists for this market. Returns (exists, path)."""
    plays_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plays")
    if not os.path.isdir(plays_dir):
        return False, None

    # Normalize market name to a likely filename
    normalized = market_name.lower().strip()
    # Check all play files for a match
    for fname in os.listdir(plays_dir):
        if fname == "example_play.md":
            continue
        if fname.endswith(".md"):
            fpath = os.path.join(plays_dir, fname)
            # Check filename match
            fname_base = fname.replace(".md", "").replace("_", " ").replace("-", " ").lower()
            if any(word in fname_base for word in normalized.split()[:2] if len(word) > 3):
                return True, fpath
            # Also check file content for the market name
            try:
                with open(fpath) as f:
                    content = f.read(500).lower()
                if any(word in content for word in normalized.split()[:3] if len(word) > 3):
                    return True, fpath
            except OSError:
                pass

    return False, None


def _print_pretrade_checklist(token_id, price, size, market_name, side,
                               stop_loss, take_profit_1, preflight_ok, preflight_msgs):
    """Print a visible pre-trade checklist before executing a buy."""
    cost = round(price * size, 6)

    print()
    print("=" * 50)
    print("  PRE-TRADE CHECKLIST")
    print("=" * 50)

    # 1. Market rules
    # Check if we can look up rules (don't fetch, just note)
    print(f"\n  Market:  {market_name} ({side})")
    print(f"  Price:   {price}  x  {size} shares  =  ${cost:.2f}")
    print(f"  Token:   {token_id[:40]}...")
    print()

    # Check play file
    play_exists, play_path = _check_play_file(market_name)
    if play_exists:
        print(f"  [OK]  Play file exists: {os.path.basename(play_path)}")
    else:
        print(f"  [!!]  NO play file found for '{market_name}'")
        print(f"        Create one at plays/<name>.md before trading!")

    # Preflight results
    for msg in preflight_msgs:
        if msg.startswith("BLOCKED"):
            print(f"  [XX]  {msg}")
        elif msg.startswith("WARNING"):
            print(f"  [!!]  {msg}")
        else:
            print(f"  [--]  {msg}")

    if preflight_ok:
        print(f"  [OK]  Preflight checks passed")
    else:
        print(f"  [XX]  Preflight checks FAILED")

    # Stop loss / TP
    if stop_loss:
        print(f"  [OK]  Stop loss set: {stop_loss}")
    else:
        print(f"  [!!]  No stop loss set -- consider adding --stop <price>")

    if take_profit_1:
        print(f"  [OK]  Take profit set: {take_profit_1}")
    else:
        print(f"  [--]  No take profit set")

    # Reminder about rules
    print(f"\n  TIP: Run 'python3 markets.py rules {token_id[:20]}...' to check resolution rules")
    print("=" * 50)


def _parse_buy_args(args):
    """Parse CLI arguments for a buy command."""
    if len(args) < 5:
        print("Usage: python3 execute.py buy <token_id> <price> <size> <market_name> <side> [options]")
        print("\nOptions:")
        print("  --stop <price>      Stop-loss price")
        print("  --tp1 <price>       Take-profit-1 price")
        print("  --tp2 <price>       Take-profit-2 price")
        print("  --tp1-pct <float>   Fraction to sell at TP1 (default: 0.50)")
        print("  --tick <size>       Tick size (default: 0.01)")
        print("  --neg-risk          Enable neg-risk mode")
        print("  --force             Override 20% concentration block")
        print("  --notes <text>      Trade notes")
        return None

    result = {
        "token_id": args[0],
        "price": float(args[1]),
        "size": float(args[2]),
        "market_name": args[3],
        "side": args[4] if len(args) > 4 else "YES",
        "stop_loss": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "tp1_pct": 0.50,
        "tick_size": "0.01",
        "neg_risk": False,
        "notes": "",
        "force": False,
    }

    i = 5
    while i < len(args):
        if args[i] == "--stop" and i + 1 < len(args):
            result["stop_loss"] = float(args[i + 1])
            i += 2
        elif args[i] == "--tp1" and i + 1 < len(args):
            result["take_profit_1"] = float(args[i + 1])
            i += 2
        elif args[i] == "--tp2" and i + 1 < len(args):
            result["take_profit_2"] = float(args[i + 1])
            i += 2
        elif args[i] == "--tp1-pct" and i + 1 < len(args):
            result["tp1_pct"] = float(args[i + 1])
            i += 2
        elif args[i] == "--tick" and i + 1 < len(args):
            result["tick_size"] = args[i + 1]
            i += 2
        elif args[i] == "--neg-risk":
            result["neg_risk"] = True
            i += 1
        elif args[i] == "--force":
            result["force"] = True
            i += 1
        elif args[i] == "--notes" and i + 1 < len(args):
            result["notes"] = args[i + 1]
            i += 2
        else:
            i += 1

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 execute.py buy <token_id> <price> <size> <market_name> <side> [options]")
        print("  python3 execute.py sell <bet_id> <price> [size] [notes]")
        print("  python3 execute.py adjust <bet_id> <actual_filled_size> [notes]  # Fix partial fill")
        print("  python3 execute.py resolve <bet_id> <won|lost> [notes]")
        print("  python3 execute.py dry-buy <token_id> <price> <size> <market_name> <side>")
        print("  python3 execute.py place-limits [bet_id]    # Place GTC TP sell orders")
        print("  python3 execute.py limit-status              # Show active limit orders")
        print("  python3 execute.py limit-sync                # Detect filled TP orders")
        print("  python3 execute.py limit-cancel [bet_id]     # Cancel TP limit orders")
        print("  python3 execute.py limit-replace <bet_id>    # Re-place after level change")
        print("\nRun 'python3 execute.py buy' for full buy options.")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "buy":
        parsed = _parse_buy_args(sys.argv[2:])
        if parsed:
            execute_buy(**parsed)

    elif cmd == "dry-buy":
        parsed = _parse_buy_args(sys.argv[2:])
        if parsed:
            execute_buy(**parsed, dry_run=True)

    elif cmd == "sell":
        if len(sys.argv) < 4:
            print("Usage: python3 execute.py sell <bet_id> <price> [size] [notes]")
            sys.exit(1)
        bet_id = int(sys.argv[2])
        price = float(sys.argv[3])
        size = float(sys.argv[4]) if len(sys.argv) > 4 else None
        notes = sys.argv[5] if len(sys.argv) > 5 else ""
        execute_sell(bet_id, price, size, notes)

    elif cmd == "adjust":
        # Adjust a bet's recorded size after a partial fill on-chain
        if len(sys.argv) < 4:
            print("Usage: python3 execute.py adjust <bet_id> <actual_filled_size> [notes]")
            print("  Use when a sell was recorded as full but only partially filled on-chain.")
            print("  This reopens the remaining shares as an open bet.")
            sys.exit(1)
        bet_id = int(sys.argv[2])
        actual_size = float(sys.argv[3])
        notes = sys.argv[4] if len(sys.argv) > 4 else "Partial fill adjustment"
        adjust_partial_fill(bet_id, actual_size, notes)

    elif cmd == "resolve":
        if len(sys.argv) < 4:
            print("Usage: python3 execute.py resolve <bet_id> <won|lost> [notes]")
            sys.exit(1)
        bet_id = int(sys.argv[2])
        won = sys.argv[3].lower() in ("won", "yes", "true", "1")
        notes = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""
        record_resolution(bet_id, won, notes)

    elif cmd == "place-limits":
        from limit_orders import place_all
        bet_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
        place_all(bet_id_filter=bet_id)

    elif cmd == "limit-status":
        from limit_orders import show_status
        show_status()

    elif cmd == "limit-sync":
        from limit_orders import sync_with_exchange, cleanup_stale
        cleanup_stale()
        sync_with_exchange()

    elif cmd == "limit-cancel":
        from limit_orders import cancel_orders
        bet_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
        cancel_orders(bet_id_filter=bet_id)

    elif cmd == "limit-replace":
        if len(sys.argv) < 3:
            print("Usage: python3 execute.py limit-replace <bet_id>")
            sys.exit(1)
        from limit_orders import replace_orders
        replace_orders(int(sys.argv[2]))

    else:
        print(f"Unknown command: {cmd}")
        print("Run 'python3 execute.py' for usage.")
        sys.exit(1)
