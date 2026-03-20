"""Track trades, bets, and funds. Persists to ledger.json."""
import json
import os
import sys
from datetime import datetime, timezone

LEDGER_FILE = os.path.join(os.path.dirname(__file__), "ledger.json")


def _load():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {
        "initial_deposit": 0,
        "funds": 0,
        "trades": [],
        "open_bets": [],
        "closed_bets": [],
        "pnl_total": 0,
    }


def _save(ledger):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def init_funds(amount):
    """Set initial bankroll."""
    ledger = _load()
    ledger["initial_deposit"] = amount
    ledger["funds"] = amount
    _save(ledger)
    print(f"Bankroll initialized: ${amount:.2f}")


def deposit(amount):
    """Add funds."""
    ledger = _load()
    ledger["funds"] = round(ledger["funds"] + amount, 6)
    ledger["initial_deposit"] = round(ledger["initial_deposit"] + amount, 6)
    _save(ledger)
    print(f"Deposited ${amount:.2f}. Total funds: ${ledger['funds']:.2f}")


def sync():
    """Sync ledger funds with on-chain USDC balance."""
    from positions import get_balance
    on_chain = get_balance()
    ledger = _load()
    old = ledger["funds"]
    diff = round(on_chain - old, 6)
    if abs(diff) < 0.01:
        print(f"Ledger in sync (${on_chain:.2f})")
        return
    ledger["funds"] = round(on_chain, 6)
    if diff > 0:
        ledger["initial_deposit"] = round(ledger["initial_deposit"] + diff, 6)
    _save(ledger)
    print(f"Synced: ${old:.2f} -> ${on_chain:.2f} (diff: ${diff:+.2f})")


def get_funds():
    """Get current available funds."""
    ledger = _load()
    return ledger["funds"]


def get_max_bet():
    """Get maximum allowed bet size (20% of total portfolio value).

    Total portfolio = available funds + cost of all open positions.
    This matches the 20% risk rule from STRATEGIES.md.
    """
    ledger = _load()
    funds = ledger["funds"]
    open_cost = sum(b.get("cost", 0) for b in ledger.get("open_bets", []))
    total_value = funds + open_cost
    return total_value * 0.20


def record_buy(market, side, price, size, token_id="", notes="",
               stop_loss=None, take_profit_1=None, take_profit_2=None,
               tp1_pct=0.50, tick_size="0.01", neg_risk=False):
    """Record opening a position. Optionally set monitor rules."""
    ledger = _load()
    cost = round(price * size, 6)

    if cost > ledger["funds"] * 0.20:
        print(f"WARNING: Bet ${cost:.2f} exceeds 20% limit (${ledger['funds'] * 0.20:.2f})")
        print("Proceeding anyway — but this violates risk rules.")

    # Check combined exposure on same token (multiple buys on same market)
    if token_id:
        existing_cost = sum(
            b.get("cost", 0) for b in ledger["open_bets"]
            if b.get("token_id") == token_id
        )
        if existing_cost > 0:
            combined = existing_cost + cost
            total_value = ledger["funds"] + sum(b.get("cost", 0) for b in ledger["open_bets"])
            pct = (combined / total_value * 100) if total_value > 0 else 0
            print(f"NOTE: Existing position on this token: ${existing_cost:.2f}")
            print(f"  Combined exposure after this buy: ${combined:.2f} ({pct:.1f}% of portfolio)")
            if pct > 20:
                print(f"  WARNING: Combined position exceeds 20% limit!")

    bet = {
        "id": len(ledger["trades"]) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "side": side,
        "action": "BUY",
        "price": price,
        "size": size,
        "cost": cost,
        "token_id": token_id,
        "notes": notes,
        "status": "OPEN",
    }

    # Store monitor rules if provided
    if stop_loss is not None or take_profit_1 is not None:
        bet["rules"] = {
            "stop_loss": stop_loss,
            "take_profit_1": take_profit_1,
            "take_profit_2": take_profit_2,
            "tp1_pct": tp1_pct,
            "tp1_hit": False,
            "tick_size": tick_size,
            "neg_risk": neg_risk,
        }

    ledger["funds"] = round(ledger["funds"] - cost, 6)
    ledger["trades"].append(bet)
    ledger["open_bets"].append(bet)
    _save(ledger)

    print(f"BUY recorded: {market} ({side}) @ {price} x {size} = ${cost:.2f}")
    print(f"Remaining funds: ${ledger['funds']:.2f}")
    if bet.get("rules"):
        r = bet["rules"]
        print(f"Monitor rules: stop={r['stop_loss']}  TP1={r['take_profit_1']}  TP2={r['take_profit_2']}")
    return bet


def set_rules(bet_id, stop_loss=None, take_profit_1=None, take_profit_2=None,
              tp1_pct=0.50, tick_size="0.01", neg_risk=False):
    """Set or update monitor rules for an open bet."""
    ledger = _load()

    bet = None
    for b in ledger["open_bets"]:
        if b["id"] == bet_id:
            bet = b
            break

    if not bet:
        print(f"No open bet with ID {bet_id}")
        return None

    bet["rules"] = {
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "tp1_pct": tp1_pct,
        "tp1_hit": bet.get("rules", {}).get("tp1_hit", False),
        "tick_size": tick_size,
        "neg_risk": neg_risk,
    }

    _save(ledger)
    print(f"Rules set for bet #{bet_id} ({bet['market'][:50]}):")
    print(f"  Stop Loss:    {stop_loss}")
    print(f"  Take Profit 1: {take_profit_1} (sell {tp1_pct:.0%})")
    print(f"  Take Profit 2: {take_profit_2}")
    return bet


def record_sell(bet_id, sell_price, size=None, notes=""):
    """Record closing a position (partial or full)."""
    ledger = _load()

    # Find the open bet
    bet = None
    for b in ledger["open_bets"]:
        if b["id"] == bet_id:
            bet = b
            break

    if not bet:
        print(f"No open bet with ID {bet_id}")
        return None

    if size is None:
        size = bet["size"]

    if size > bet["size"]:
        print(f"WARNING: Sell size {size} exceeds position size {bet['size']}. Clamping to {bet['size']}.")
        size = bet["size"]

    revenue = round(sell_price * size, 6)
    cost = round(bet["price"] * size, 6)
    pnl = round(revenue - cost, 6)

    sell_record = {
        "id": len(ledger["trades"]) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": bet["market"],
        "side": bet["side"],
        "action": "SELL",
        "buy_price": bet["price"],
        "sell_price": sell_price,
        "size": size,
        "revenue": revenue,
        "pnl": pnl,
        "token_id": bet.get("token_id", ""),
        "notes": notes,
        "original_bet_id": bet_id,
    }

    ledger["funds"] = round(ledger["funds"] + revenue, 6)
    ledger["pnl_total"] = round(ledger["pnl_total"] + pnl, 6)
    ledger["trades"].append(sell_record)

    # Close the bet (or reduce size for partial sells)
    remaining = round(bet["size"] - size, 6)
    if remaining <= 0:
        ledger["open_bets"] = [b for b in ledger["open_bets"] if b["id"] != bet_id]
        bet["status"] = "CLOSED"
        # Accumulate PnL on the bet record so status display can show it
        bet["pnl"] = round(bet.get("pnl", 0) + pnl, 6)
        ledger["closed_bets"].append(bet)
    else:
        # Partial sell -- accumulate realized PnL on the bet for tracking
        bet["pnl"] = round(bet.get("pnl", 0) + pnl, 6)
        # Reduce cost proportionally so future PnL calculations are correct
        cost_per_share = bet["cost"] / bet["size"]
        bet["size"] = remaining
        bet["cost"] = round(cost_per_share * remaining, 6)

    _save(ledger)

    emoji = "+" if pnl >= 0 else ""
    print(f"SELL recorded: {bet['market']} @ {sell_price} x {size}")
    print(f"PnL: {emoji}${pnl:.2f}  |  Funds: ${ledger['funds']:.2f}")
    return sell_record


def record_resolution(bet_id, won, notes=""):
    """Record a bet resolving (contract expired)."""
    ledger = _load()

    bet = None
    for b in ledger["open_bets"]:
        if b["id"] == bet_id:
            bet = b
            break

    if not bet:
        print(f"No open bet with ID {bet_id}")
        return None

    if won:
        payout = bet["size"]  # Each share pays $1 if won
        pnl = payout - bet["cost"]
        ledger["funds"] = round(ledger["funds"] + payout, 6)
    else:
        pnl = -bet["cost"]

    ledger["pnl_total"] = round(ledger["pnl_total"] + pnl, 6)

    bet["status"] = "WON" if won else "LOST"
    bet["pnl"] = pnl
    ledger["open_bets"] = [b for b in ledger["open_bets"] if b["id"] != bet_id]
    ledger["closed_bets"].append(bet)

    resolution = {
        "id": len(ledger["trades"]) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": bet["market"],
        "action": "RESOLUTION",
        "result": "WON" if won else "LOST",
        "pnl": pnl,
        "notes": notes,
        "original_bet_id": bet_id,
    }
    ledger["trades"].append(resolution)
    _save(ledger)

    status = "WON" if won else "LOST"
    print(f"RESOLVED: {bet['market']} -> {status} (PnL: ${pnl:+.2f})")
    print(f"Funds: ${ledger['funds']:.2f}")
    return resolution


def _fetch_live_prices(open_bets):
    """Fetch live midpoint prices for all open positions.
    Returns dict of {token_id: price} or empty dict on failure.
    """
    live_prices = {}
    if not open_bets:
        return live_prices
    try:
        from monitor import get_midpoint
        # Deduplicate tokens
        token_ids = set(b.get("token_id", "") for b in open_bets if b.get("token_id"))
        for tid in token_ids:
            try:
                mid = get_midpoint(tid)
                if mid is not None and mid > 0:
                    live_prices[tid] = mid
            except Exception:
                pass
    except ImportError:
        pass
    return live_prices


def status():
    """Print full portfolio status."""
    ledger = _load()

    # Check real on-chain balance
    on_chain = None
    try:
        from positions import get_balance
        on_chain = get_balance()
    except Exception:
        pass

    # Fetch live prices for unrealized P&L
    live_prices = _fetch_live_prices(ledger.get("open_bets", []))

    print("=" * 60)
    print("PORTFOLIO STATUS")
    print("=" * 60)
    print(f"  Initial Deposit: ${ledger['initial_deposit']:.2f}")
    print(f"  Ledger Funds:    ${ledger['funds']:.2f}")
    if on_chain is not None:
        print(f"  On-Chain USDC:   ${on_chain:.2f}")
        diff = round(on_chain - ledger['funds'], 2)
        if abs(diff) > 0.05:
            print(f"  ** MISMATCH: ${diff:+.2f} (run 'ledger.py sync' to fix) **")
    print(f"  Total PnL:       ${ledger['pnl_total']:+.2f}")
    funds = on_chain if on_chain is not None else ledger['funds']
    print(f"  Max Single Bet:  ${funds * 0.20:.2f} (20%)")

    # Open positions value -- both book and live
    open_cost = sum(b["cost"] for b in ledger["open_bets"])
    total_unrealized = 0
    market_value_total = 0
    has_prices = bool(live_prices)

    if has_prices:
        for b in ledger["open_bets"]:
            tid = b.get("token_id", "")
            if tid in live_prices:
                mv = live_prices[tid] * b["size"]
                market_value_total += mv
                total_unrealized += mv - b["cost"]
            else:
                market_value_total += b["cost"]  # fallback to cost
    else:
        market_value_total = open_cost

    print(f"  In Open Bets:    ${open_cost:.2f} (cost basis)")
    if has_prices:
        print(f"  Market Value:    ${market_value_total:.2f} (live)")
        print(f"  Unrealized PnL:  ${total_unrealized:+.2f}")
        total_live = ledger['funds'] + market_value_total
        print(f"  Total Value:     ${total_live:.2f} (live)")
    else:
        print(f"  Total Value:     ${ledger['funds'] + open_cost:.2f} (book)")

    if ledger["open_bets"]:
        print(f"\n  OPEN BETS ({len(ledger['open_bets'])}):")
        for b in ledger["open_bets"]:
            tid = b.get("token_id", "")
            mid = live_prices.get(tid)
            print(f"    #{b['id']} {b['market'][:50]}")
            price_line = f"       {b['side']} @ {b['price']} x {b['size']} = ${b['cost']:.2f}  [{b['timestamp'][:10]}]"
            if mid is not None:
                mv = mid * b["size"]
                upnl = mv - b["cost"]
                pct = (upnl / b["cost"] * 100) if b["cost"] > 0 else 0
                price_line += f"  |  NOW {mid:.3f} = ${mv:.2f} ({upnl:+.2f} / {pct:+.1f}%)"
            print(price_line)
            if b.get("rules"):
                r = b["rules"]
                parts = []
                if r.get("stop_loss") is not None:
                    parts.append(f"Stop: {r['stop_loss']}")
                if r.get("take_profit_1") is not None:
                    parts.append(f"TP1: {r['take_profit_1']}")
                if r.get("take_profit_2") is not None:
                    parts.append(f"TP2: {r['take_profit_2']}")
                if parts:
                    rule_line = f"       Rules: {' | '.join(parts)}"
                    # Flag if near stop or TP
                    if mid is not None:
                        sl = r.get("stop_loss")
                        tp1 = r.get("take_profit_1")
                        if sl is not None and mid <= sl * 1.15:
                            rule_line += "  ** NEAR STOP **"
                        elif tp1 is not None and mid >= tp1 * 0.95:
                            rule_line += "  ** NEAR TP1 **"
                    print(rule_line)

    # Show combined exposure when multiple bets on same token
    if ledger["open_bets"]:
        token_groups = {}
        for b in ledger["open_bets"]:
            tid = b.get("token_id", "")
            if tid:
                token_groups.setdefault(tid, []).append(b)
        multi = {tid: bets for tid, bets in token_groups.items() if len(bets) > 1}
        if multi:
            total_value = ledger['funds'] + open_cost
            print(f"\n  COMBINED POSITIONS:")
            for tid, bets in multi.items():
                combined_cost = sum(b["cost"] for b in bets)
                combined_shares = sum(b["size"] for b in bets)
                avg_price = combined_cost / combined_shares if combined_shares > 0 else 0
                pct = (combined_cost / total_value * 100) if total_value > 0 else 0
                ids = [b["id"] for b in bets]
                name = bets[0]["market"][:50]
                flag = " *** OVER 20% ***" if pct > 20 else ""
                print(f"    {name}")
                print(f"       IDs: {ids}  |  {combined_shares} shares @ avg {avg_price:.4f}  |  ${combined_cost:.2f} ({pct:.1f}%){flag}")

    if ledger["closed_bets"]:
        # Build a lookup of PnL from sell/resolution trades for bets missing pnl field
        _sell_pnl = {}
        for t in ledger.get("trades", []):
            if t.get("action") in ("SELL", "RESOLUTION") and "original_bet_id" in t:
                bid = t["original_bet_id"]
                _sell_pnl[bid] = _sell_pnl.get(bid, 0) + t.get("pnl", 0)

        print(f"\n  CLOSED BETS ({len(ledger['closed_bets'])}):")
        for b in ledger["closed_bets"][-10:]:  # Last 10
            pnl = b.get("pnl", None)
            if pnl is None or pnl == 0:
                # Reconstruct from trade history
                pnl = _sell_pnl.get(b["id"], 0)
            print(f"    #{b['id']} {b['market'][:50]}  ->  {b['status']}  (${pnl:+.2f})")

    # Win rate
    wins = sum(1 for b in ledger["closed_bets"] if b.get("pnl", 0) > 0)
    total = len(ledger["closed_bets"])
    if total > 0:
        print(f"\n  Win Rate: {wins}/{total} ({wins/total*100:.0f}%)")

    print("=" * 60)
    return ledger


def history():
    """Print trade history."""
    ledger = _load()
    print(f"{'ID':>4}  {'Date':>10}  {'Action':>10}  {'Market':<40}  {'PnL':>10}")
    print("-" * 80)
    for t in ledger["trades"]:
        date = t["timestamp"][:10]
        action = t["action"]
        market = t["market"][:40]
        pnl = t.get("pnl", -t.get("cost", 0))
        print(f"{t['id']:>4}  {date}  {action:>10}  {market:<40}  ${pnl:>+9.2f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 ledger.py init <amount>        # Set initial bankroll")
        print("  python3 ledger.py deposit <amount>     # Add funds")
        print("  python3 ledger.py status               # Portfolio overview")
        print("  python3 ledger.py history              # Trade history")
        print("  python3 ledger.py max-bet              # Show max bet size")
        print("  python3 ledger.py set-rules <bet_id> <stop> <tp1> <tp2>  # Set monitor rules")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "init":
        if len(sys.argv) < 3:
            print("Usage: python3 ledger.py init <amount>")
            sys.exit(1)
        init_funds(float(sys.argv[2]))
    elif cmd == "deposit":
        if len(sys.argv) < 3:
            print("Usage: python3 ledger.py deposit <amount>")
            sys.exit(1)
        deposit(float(sys.argv[2]))
    elif cmd == "status":
        status()
    elif cmd == "history":
        history()
    elif cmd == "sync":
        sync()
    elif cmd == "max-bet":
        ledger = _load()
        funds = ledger["funds"]
        open_cost = sum(b.get("cost", 0) for b in ledger.get("open_bets", []))
        total = funds + open_cost
        print(f"Max single bet: ${get_max_bet():.2f} (20% of ${total:.2f} total value)")
        print(f"  Available funds: ${funds:.2f}  |  In open bets: ${open_cost:.2f}")
    elif cmd == "set-rules":
        if len(sys.argv) < 6:
            print("Usage: python3 ledger.py set-rules <bet_id> <stop_loss> <tp1> <tp2> [tp1_pct]")
            print("Example: python3 ledger.py set-rules 1 0.18 0.60 0.75 0.50")
            sys.exit(1)
        bid = int(sys.argv[2])
        stop = float(sys.argv[3])
        tp1 = float(sys.argv[4])
        tp2 = float(sys.argv[5])
        tp1_pct = float(sys.argv[6]) if len(sys.argv) > 6 else 0.50
        set_rules(bid, stop_loss=stop, take_profit_1=tp1, take_profit_2=tp2, tp1_pct=tp1_pct)
    else:
        print(f"Unknown command: {cmd}")
