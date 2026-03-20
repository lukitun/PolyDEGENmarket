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
    ledger["funds"] += amount
    ledger["initial_deposit"] += amount
    _save(ledger)
    print(f"Deposited ${amount:.2f}. Total funds: ${ledger['funds']:.2f}")


def get_funds():
    """Get current available funds."""
    ledger = _load()
    return ledger["funds"]


def get_max_bet():
    """Get maximum allowed bet size (20% of funds)."""
    return get_funds() * 0.20


def record_buy(market, side, price, size, token_id="", notes="",
               stop_loss=None, take_profit_1=None, take_profit_2=None,
               tp1_pct=0.50, tick_size="0.01", neg_risk=False):
    """Record opening a position. Optionally set monitor rules."""
    ledger = _load()
    cost = price * size

    if cost > ledger["funds"] * 0.20:
        print(f"WARNING: Bet ${cost:.2f} exceeds 20% limit (${ledger['funds'] * 0.20:.2f})")
        print("Proceeding anyway — but this violates risk rules.")

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

    ledger["funds"] -= cost
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

    revenue = sell_price * size
    cost = bet["price"] * size
    pnl = revenue - cost

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

    ledger["funds"] += revenue
    ledger["pnl_total"] += pnl
    ledger["trades"].append(sell_record)

    # Close the bet (or reduce size for partial sells)
    remaining = bet["size"] - size
    if remaining <= 0:
        ledger["open_bets"] = [b for b in ledger["open_bets"] if b["id"] != bet_id]
        bet["status"] = "CLOSED"
        ledger["closed_bets"].append(bet)
    else:
        bet["size"] = remaining

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
        ledger["funds"] += payout
    else:
        pnl = -bet["cost"]

    ledger["pnl_total"] += pnl

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


def status():
    """Print full portfolio status."""
    ledger = _load()

    print("=" * 60)
    print("PORTFOLIO STATUS")
    print("=" * 60)
    print(f"  Initial Deposit: ${ledger['initial_deposit']:.2f}")
    print(f"  Current Funds:   ${ledger['funds']:.2f}")
    print(f"  Total PnL:       ${ledger['pnl_total']:+.2f}")
    print(f"  Max Single Bet:  ${ledger['funds'] * 0.20:.2f} (20%)")

    # Open positions value
    open_cost = sum(b["cost"] for b in ledger["open_bets"])
    print(f"  In Open Bets:    ${open_cost:.2f}")
    print(f"  Total Value:     ${ledger['funds'] + open_cost:.2f}")

    if ledger["open_bets"]:
        print(f"\n  OPEN BETS ({len(ledger['open_bets'])}):")
        for b in ledger["open_bets"]:
            print(f"    #{b['id']} {b['market'][:50]}")
            print(f"       {b['side']} @ {b['price']} x {b['size']} = ${b['cost']:.2f}  [{b['timestamp'][:10]}]")
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
                    print(f"       Rules: {' | '.join(parts)}")

    if ledger["closed_bets"]:
        print(f"\n  CLOSED BETS ({len(ledger['closed_bets'])}):")
        for b in ledger["closed_bets"][-10:]:  # Last 10
            pnl = b.get("pnl", 0)
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
        init_funds(float(sys.argv[2]))
    elif cmd == "deposit":
        deposit(float(sys.argv[2]))
    elif cmd == "status":
        status()
    elif cmd == "history":
        history()
    elif cmd == "max-bet":
        print(f"Max single bet: ${get_max_bet():.2f} (20% of ${get_funds():.2f})")
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
