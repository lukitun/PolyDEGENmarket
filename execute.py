"""Unified trade execution -- places order and records to ledger atomically.

Combines trade.py (order placement) and ledger.py (record keeping) into a
single flow that ensures every executed trade is tracked. Prevents the
scenario where a trade succeeds but the ledger update is forgotten.

Usage:
    python3 execute.py buy <token_id> <price> <size> <market_name> <side> [options]
    python3 execute.py sell <bet_id> <price> [size] [notes]
    python3 execute.py resolve <bet_id> <won|lost> [notes]
    python3 execute.py dry-buy <token_id> <price> <size>  # Dry run (no order placed)

Options for buy:
    --stop <price>      Set stop-loss price
    --tp1 <price>       Set take-profit-1 price
    --tp2 <price>       Set take-profit-2 price
    --tp1-pct <float>   Fraction to sell at TP1 (default: 0.50)
    --tick <size>        Tick size (default: 0.01)
    --neg-risk           Enable neg-risk mode
    --notes <text>       Trade notes
"""
import sys
import os
import json
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger import _load, _save, get_funds, get_max_bet, record_buy, record_sell, record_resolution
from monitor import check_liquidity


def preflight_buy(token_id, price, size, market_name="", side="YES"):
    """Run pre-trade checks. Returns (ok, messages) tuple."""
    messages = []
    ok = True

    cost = round(price * size, 6)
    funds = get_funds()
    max_bet = get_max_bet()

    # Check funds
    if cost > funds:
        messages.append(f"BLOCKED: Cost ${cost:.2f} exceeds available funds ${funds:.2f}")
        ok = False

    # Check 20% rule
    if cost > max_bet:
        messages.append(f"WARNING: Cost ${cost:.2f} exceeds 20% limit ${max_bet:.2f}")
        # Allow but warn -- ledger.py also warns

    # Check combined exposure on same token
    ledger = _load()
    existing_cost = sum(
        b.get("cost", 0) for b in ledger["open_bets"]
        if b.get("token_id") == token_id
    )
    if existing_cost > 0:
        combined = existing_cost + cost
        total_value = funds + sum(b.get("cost", 0) for b in ledger["open_bets"])
        pct = (combined / total_value * 100) if total_value > 0 else 0
        messages.append(f"NOTE: Existing position ${existing_cost:.2f}, combined ${combined:.2f} ({pct:.1f}%)")
        if pct > 20:
            messages.append(f"WARNING: Combined position exceeds 20% limit!")

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
                dry_run=False):
    """Execute a buy order and record it in the ledger atomically.

    Returns the ledger bet record on success, None on failure.
    """
    cost = round(price * size, 6)

    # Preflight checks
    ok, messages = preflight_buy(token_id, price, size, market_name, side)
    for msg in messages:
        print(f"  {msg}")
    if not ok:
        print("\nTrade BLOCKED by preflight checks.")
        return None

    if dry_run:
        print(f"\n  DRY RUN -- would buy:")
        print(f"    Market:  {market_name}")
        print(f"    Side:    {side}")
        print(f"    Price:   {price}")
        print(f"    Size:    {size}")
        print(f"    Cost:    ${cost:.2f}")
        print(f"    Token:   {token_id[:20]}...")
        if stop_loss:
            print(f"    Stop:    {stop_loss}")
        if take_profit_1:
            print(f"    TP1:     {take_profit_1}")
        if take_profit_2:
            print(f"    TP2:     {take_profit_2}")
        print(f"\n  No order placed (dry run).")
        return {"dry_run": True, "cost": cost}

    # Place the order
    print(f"\n  Placing BUY order: {market_name} ({side}) @ {price} x {size} = ${cost:.2f}")
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
        return bet
    except Exception as e:
        print(f"  CRITICAL: Order {order_id} was placed but ledger update FAILED: {e}")
        print(f"  MANUAL FIX NEEDED: record buy for {market_name} @ {price} x {size}")
        print(f"  Token: {token_id}")
        return None


def execute_sell(bet_id, price, size=None, notes="", check_liq=True):
    """Execute a sell order for an existing position and update the ledger.

    Returns the sell record on success, None on failure.
    """
    ledger = _load()

    # Find the bet
    bet = None
    for b in ledger["open_bets"]:
        if b["id"] == bet_id:
            bet = b
            break

    if not bet:
        print(f"  No open bet with ID {bet_id}")
        return None

    token_id = bet["token_id"]
    if not token_id:
        print(f"  Bet #{bet_id} has no token_id -- cannot sell")
        return None

    sell_size = size if size is not None else bet["size"]
    if sell_size > bet["size"]:
        print(f"  WARNING: Sell size {sell_size} exceeds position {bet['size']}, clamping.")
        sell_size = bet["size"]

    rules = bet.get("rules", {})
    tick_size = rules.get("tick_size", "0.01")
    neg_risk = rules.get("neg_risk", False)

    # Liquidity check
    if check_liq:
        executable, avg_price, total_depth = check_liquidity(token_id, sell_size)
        if not executable:
            print(f"  WARNING: Insufficient liquidity. Depth: {total_depth:.1f} shares, need {sell_size}")
            print(f"  Proceeding anyway -- order may partially fill.")

    # Place the sell order
    print(f"\n  Placing SELL order: {bet['market'][:50]} @ {price} x {sell_size}")
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

    # Record in ledger
    try:
        sell_rec = record_sell(bet_id, price, sell_size, notes=notes)
        print(f"  Ledger updated.")
        return sell_rec
    except Exception as e:
        print(f"  CRITICAL: Sell order {order_id} succeeded but ledger update FAILED: {e}")
        print(f"  MANUAL FIX NEEDED: record sell for bet #{bet_id} @ {price} x {sell_size}")
        return None


def _parse_buy_args(args):
    """Parse CLI arguments for a buy command."""
    if len(args) < 6:
        print("Usage: python3 execute.py buy <token_id> <price> <size> <market_name> <side> [options]")
        print("\nOptions:")
        print("  --stop <price>      Stop-loss price")
        print("  --tp1 <price>       Take-profit-1 price")
        print("  --tp2 <price>       Take-profit-2 price")
        print("  --tp1-pct <float>   Fraction to sell at TP1 (default: 0.50)")
        print("  --tick <size>       Tick size (default: 0.01)")
        print("  --neg-risk          Enable neg-risk mode")
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
        print("  python3 execute.py resolve <bet_id> <won|lost> [notes]")
        print("  python3 execute.py dry-buy <token_id> <price> <size> <market_name> <side>")
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

    elif cmd == "resolve":
        if len(sys.argv) < 4:
            print("Usage: python3 execute.py resolve <bet_id> <won|lost> [notes]")
            sys.exit(1)
        bet_id = int(sys.argv[2])
        won = sys.argv[3].lower() in ("won", "yes", "true", "1")
        notes = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""
        record_resolution(bet_id, won, notes)

    else:
        print(f"Unknown command: {cmd}")
        print("Run 'python3 execute.py' for usage.")
        sys.exit(1)
