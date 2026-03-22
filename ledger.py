"""Track trades, bets, and funds. Persists to ledger.json.

v2 data model: events[] (immutable log) + positions{} (derived, keyed by token_id).
Positions consolidate multiple buys on the same token into one entry.
Can always be rebuilt from events via rebuild().
"""
import json
import os
import sys
from datetime import datetime, timezone

LEDGER_FILE = os.path.join(os.path.dirname(__file__), "ledger.json")


def _load():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            data = json.load(f)
        # v1 ledger has "trades" + "open_bets" + "closed_bets"
        # v2 ledger has "events" + "positions"
        if "version" not in data:
            return data  # v1 format, caller should migrate
        return data
    return _new_ledger()


def _new_ledger():
    return {
        "version": 2,
        "initial_deposit": 0,
        "funds": 0,
        "events": [],
        "positions": {},
        "next_pos_id": 1,
        "pnl_total": 0,
    }


def _save(ledger):
    """Save ledger atomically -- write to temp file then rename."""
    tmp_file = LEDGER_FILE + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(ledger, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_file, LEDGER_FILE)


def _next_event_id(ledger):
    if not ledger["events"]:
        return 1
    return max(e["id"] for e in ledger["events"]) + 1


# ── Position helpers ──────────────────────────────────────────────

def _get_position(ledger, token_id):
    """Get position by token_id, or None."""
    return ledger["positions"].get(token_id)


def _get_position_by_id(ledger, pos_id):
    """Get position by pos_id (the human-friendly integer)."""
    for pos in ledger["positions"].values():
        if pos["pos_id"] == pos_id:
            return pos
    return None


def _find_position(ledger, identifier):
    """Find a position by pos_id (int) or token_id (string).
    Accepts the old bet_id format too -- searches pos_id and legacy entry IDs.
    """
    # Try as pos_id first
    if isinstance(identifier, int):
        pos = _get_position_by_id(ledger, identifier)
        if pos:
            return pos
        # Fallback: search for legacy bet_id in entries
        for p in ledger["positions"].values():
            for entry in p.get("entries", []):
                if entry.get("event_id") == identifier:
                    return p
        return None
    # String: treat as token_id
    return ledger["positions"].get(str(identifier))


# ── Backward compatibility ────────────────────────────────────────

def get_open_bets(ledger=None):
    """Return open positions formatted like the old open_bets[] array.
    This is the main backward-compat bridge for monitor.py, execute.py, etc.
    Each position maps to one 'bet' dict with the familiar fields.
    """
    if ledger is None:
        ledger = _load()
    bets = []
    for pos in ledger["positions"].values():
        if pos["status"] != "OPEN":
            continue
        bets.append(_pos_to_bet(pos))
    return bets


def _pos_to_bet(pos):
    """Convert a position to a bet-like dict for backward compat."""
    return {
        "id": pos["pos_id"],
        "market": pos["market"],
        "side": pos["side"],
        "price": pos["avg_price"],
        "size": pos["total_shares"],
        "cost": pos["total_cost"],
        "token_id": pos["token_id"],
        "status": pos["status"],
        "timestamp": pos.get("first_entry", ""),
        "rules": pos.get("rules"),
        "notes": "",
    }


# ── Core operations ───────────────────────────────────────────────

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
    """Sync ledger funds with on-chain USDC balance.

    Only adjusts funds, NOT initial_deposit. The initial_deposit should only
    change via explicit deposit() calls. Adjusting it here would corrupt PnL
    tracking (e.g., when resolved positions pay out USDC on-chain).
    """
    from positions import get_balance
    on_chain = get_balance()
    ledger = _load()
    old = ledger["funds"]
    diff = round(on_chain - old, 6)
    if abs(diff) < 0.01:
        print(f"Ledger in sync (${on_chain:.2f})")
        return
    ledger["funds"] = round(on_chain, 6)
    # Record the adjustment as an event for auditability
    event = {
        "id": _next_event_id(ledger),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "SYNC",
        "old_funds": old,
        "new_funds": round(on_chain, 6),
        "diff": diff,
        "notes": "Synced with on-chain USDC balance",
    }
    ledger["events"].append(event)
    _save(ledger)
    print(f"Synced: ${old:.2f} -> ${on_chain:.2f} (diff: ${diff:+.2f})")
    if diff > 0:
        print(f"  NOTE: +${diff:.2f} extra on-chain. Likely from resolved positions or external deposit.")
        print(f"  If this was an actual deposit, run: python3 ledger.py deposit {diff:.2f}")


def get_funds():
    """Get current available funds."""
    return _load()["funds"]


def get_max_bet():
    """Get maximum allowed bet size (20% of total portfolio value)."""
    ledger = _load()
    funds = ledger["funds"]
    open_cost = sum(
        p["total_cost"] for p in ledger["positions"].values()
        if p["status"] == "OPEN"
    )
    total_value = funds + open_cost
    return total_value * 0.20


def record_buy(market, side, price, size, token_id="", notes="",
               stop_loss=None, take_profit_1=None, take_profit_2=None,
               tp1_pct=0.50, tick_size="0.01", neg_risk=False):
    """Record opening a position. Merges into existing position if same token_id."""
    ledger = _load()
    cost = round(price * size, 6)

    # Risk checks
    open_cost = sum(p["total_cost"] for p in ledger["positions"].values() if p["status"] == "OPEN")
    total_value = ledger["funds"] + open_cost
    max_bet = total_value * 0.20

    if cost > max_bet:
        print(f"WARNING: Bet ${cost:.2f} exceeds 20% limit (${max_bet:.2f} of ${total_value:.2f} total)")
        print("Proceeding anyway -- but this violates risk rules.")

    # Check combined exposure on same token
    existing_pos = _get_position(ledger, token_id) if token_id else None
    if existing_pos and existing_pos["status"] == "OPEN":
        combined = existing_pos["total_cost"] + cost
        pct = (combined / total_value * 100) if total_value > 0 else 0
        print(f"NOTE: Existing position on this token: ${existing_pos['total_cost']:.2f}")
        print(f"  Combined exposure after this buy: ${combined:.2f} ({pct:.1f}% of portfolio)")
        if pct > 20:
            print(f"  WARNING: Combined position exceeds 20% limit!")

    # Create event
    event_id = _next_event_id(ledger)
    event = {
        "id": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "BUY",
        "market": market,
        "side": side,
        "price": price,
        "size": size,
        "cost": cost,
        "token_id": token_id,
        "notes": notes,
    }
    ledger["events"].append(event)
    ledger["funds"] = round(ledger["funds"] - cost, 6)

    # Update or create position
    if existing_pos and existing_pos["status"] == "OPEN":
        pos = existing_pos
        old_shares = pos["total_shares"]
        old_cost = pos["total_cost"]
        pos["total_shares"] = round(old_shares + size, 6)
        pos["total_cost"] = round(old_cost + cost, 6)
        pos["avg_price"] = round(pos["total_cost"] / pos["total_shares"], 6) if pos["total_shares"] > 0 else 0
        pos["entries"].append({"event_id": event_id, "price": price, "size": size, "timestamp": event["timestamp"]})
        # Update rules if provided (new rules override old)
        if stop_loss is not None or take_profit_1 is not None:
            pos["rules"] = {
                "stop_loss": stop_loss,
                "take_profit_1": take_profit_1,
                "take_profit_2": take_profit_2,
                "tp1_pct": tp1_pct,
                "tp1_hit": pos.get("rules", {}).get("tp1_hit", False),
                "tick_size": tick_size,
                "neg_risk": neg_risk,
            }
    else:
        pos_id = ledger["next_pos_id"]
        ledger["next_pos_id"] = pos_id + 1
        pos = {
            "pos_id": pos_id,
            "market": market,
            "side": side,
            "token_id": token_id,
            "status": "OPEN",
            "total_shares": size,
            "total_cost": cost,
            "avg_price": price,
            "realized_pnl": 0,
            "first_entry": event["timestamp"],
            "entries": [{"event_id": event_id, "price": price, "size": size, "timestamp": event["timestamp"]}],
        }
        if stop_loss is not None or take_profit_1 is not None:
            pos["rules"] = {
                "stop_loss": stop_loss,
                "take_profit_1": take_profit_1,
                "take_profit_2": take_profit_2,
                "tp1_pct": tp1_pct,
                "tp1_hit": False,
                "tick_size": tick_size,
                "neg_risk": neg_risk,
            }
        if token_id:
            ledger["positions"][token_id] = pos
        else:
            ledger["positions"][f"_pos_{pos_id}"] = pos

    _save(ledger)

    print(f"BUY recorded: {market} ({side}) @ {price} x {size} = ${cost:.2f}")
    print(f"Remaining funds: ${ledger['funds']:.2f}")
    if pos.get("rules"):
        r = pos["rules"]
        print(f"Monitor rules: stop={r['stop_loss']}  TP1={r['take_profit_1']}  TP2={r['take_profit_2']}")

    # Return bet-like dict for backward compat
    result = _pos_to_bet(pos)
    result["id"] = event_id  # execute.py expects the event id for "Ledger updated: bet #X"
    return result


def set_rules(pos_id, stop_loss=None, take_profit_1=None, take_profit_2=None,
              tp1_pct=0.50, tick_size="0.01", neg_risk=False):
    """Set or update monitor rules for a position."""
    ledger = _load()
    pos = _find_position(ledger, pos_id)

    if not pos:
        print(f"No position with ID {pos_id}")
        return None

    pos["rules"] = {
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "tp1_pct": tp1_pct,
        "tp1_hit": pos.get("rules", {}).get("tp1_hit", False),
        "tick_size": tick_size,
        "neg_risk": neg_risk,
    }

    _save(ledger)
    print(f"Rules set for position #{pos['pos_id']} ({pos['market'][:50]}):")
    print(f"  Stop Loss:    {stop_loss}")
    print(f"  Take Profit 1: {take_profit_1} (sell {tp1_pct:.0%})")
    print(f"  Take Profit 2: {take_profit_2}")
    return _pos_to_bet(pos)


def record_sell(pos_id, sell_price, size=None, notes=""):
    """Record closing a position (partial or full).
    pos_id can be a pos_id (int) or token_id (string).
    """
    ledger = _load()
    pos = _find_position(ledger, pos_id)

    if not pos:
        print(f"No open position with ID {pos_id}")
        return None

    if pos["status"] != "OPEN":
        print(f"Position #{pos['pos_id']} is {pos['status']}, not OPEN")
        return None

    if size is None:
        size = pos["total_shares"]

    if size > pos["total_shares"]:
        print(f"WARNING: Sell size {size} exceeds position size {pos['total_shares']}. Clamping.")
        size = pos["total_shares"]

    revenue = round(sell_price * size, 6)
    cost_portion = round(pos["avg_price"] * size, 6)
    pnl = round(revenue - cost_portion, 6)

    event = {
        "id": _next_event_id(ledger),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "SELL",
        "market": pos["market"],
        "side": pos["side"],
        "sell_price": sell_price,
        "buy_price": pos["avg_price"],
        "size": size,
        "revenue": revenue,
        "pnl": pnl,
        "token_id": pos["token_id"],
        "notes": notes,
        "pos_id": pos["pos_id"],
    }
    ledger["events"].append(event)
    ledger["funds"] = round(ledger["funds"] + revenue, 6)
    ledger["pnl_total"] = round(ledger["pnl_total"] + pnl, 6)

    # Update position
    remaining = round(pos["total_shares"] - size, 6)
    pos["realized_pnl"] = round(pos.get("realized_pnl", 0) + pnl, 6)
    if remaining <= 0:
        pos["total_shares"] = 0
        pos["total_cost"] = 0
        pos["status"] = "CLOSED"
    else:
        cost_remaining = round(pos["avg_price"] * remaining, 6)
        pos["total_shares"] = remaining
        pos["total_cost"] = cost_remaining

    _save(ledger)

    emoji = "+" if pnl >= 0 else ""
    print(f"SELL recorded: {pos['market']} @ {sell_price} x {size}")
    print(f"PnL: {emoji}${pnl:.2f}  |  Funds: ${ledger['funds']:.2f}")
    return event


def record_resolution(pos_id, won, notes=""):
    """Record a position resolving (contract expired)."""
    ledger = _load()
    pos = _find_position(ledger, pos_id)

    if not pos:
        print(f"No position with ID {pos_id}")
        return None

    if pos["status"] != "OPEN":
        print(f"Position #{pos['pos_id']} is {pos['status']}, not OPEN")
        return None

    if won:
        payout = pos["total_shares"]
        pnl = round(payout - pos["total_cost"], 6)
        ledger["funds"] = round(ledger["funds"] + payout, 6)
    else:
        pnl = round(-pos["total_cost"], 6)

    ledger["pnl_total"] = round(ledger["pnl_total"] + pnl, 6)

    event = {
        "id": _next_event_id(ledger),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "RESOLUTION",
        "market": pos["market"],
        "result": "WON" if won else "LOST",
        "pnl": pnl,
        "notes": notes,
        "pos_id": pos["pos_id"],
    }
    ledger["events"].append(event)

    pos["realized_pnl"] = round(pos.get("realized_pnl", 0) + pnl, 6)
    pos["total_shares"] = 0
    pos["total_cost"] = 0
    pos["status"] = "WON" if won else "LOST"

    _save(ledger)
    status = "WON" if won else "LOST"
    print(f"RESOLVED: {pos['market']} -> {status} (PnL: ${pnl:+.2f})")
    print(f"Funds: ${ledger['funds']:.2f}")
    return event


# ── Rebuild & Reconcile ───────────────────────────────────────────

def rebuild():
    """Rebuild positions from event log. Fixes any position drift."""
    ledger = _load()
    events = ledger["events"]

    # Rebuild positions from scratch
    positions = {}
    next_pos_id = 1

    for e in events:
        etype = e.get("type", e.get("action"))  # "action" for v1 compat
        token_id = e.get("token_id", "")

        if etype == "BUY":
            key = token_id or f"_event_{e['id']}"
            if key in positions and positions[key]["status"] == "OPEN":
                pos = positions[key]
                pos["total_shares"] = round(pos["total_shares"] + e["size"], 6)
                pos["total_cost"] = round(pos["total_cost"] + e["cost"], 6)
                pos["avg_price"] = round(pos["total_cost"] / pos["total_shares"], 6) if pos["total_shares"] > 0 else 0
                pos["entries"].append({"event_id": e["id"], "price": e["price"], "size": e["size"], "timestamp": e["timestamp"]})
            else:
                pos = {
                    "pos_id": next_pos_id,
                    "market": e["market"],
                    "side": e["side"],
                    "token_id": token_id,
                    "status": "OPEN",
                    "total_shares": e["size"],
                    "total_cost": round(e["price"] * e["size"], 6),
                    "avg_price": e["price"],
                    "realized_pnl": 0,
                    "first_entry": e["timestamp"],
                    "entries": [{"event_id": e["id"], "price": e["price"], "size": e["size"], "timestamp": e["timestamp"]}],
                }
                next_pos_id += 1
                positions[key] = pos

        elif etype == "SELL":
            key = token_id or f"_event_{e.get('pos_id', e.get('original_bet_id', '?'))}"
            pos = positions.get(key)
            # Fallback: find by original_bet_id matching an entry event_id
            if not pos or pos["status"] != "OPEN":
                obid = e.get("original_bet_id") or e.get("pos_id")
                if obid:
                    for p in positions.values():
                        if p["status"] == "OPEN" and any(
                            entry.get("event_id") == obid for entry in p.get("entries", [])
                        ):
                            pos = p
                            break
            if pos and pos["status"] == "OPEN":
                sell_size = e["size"]
                pnl = e.get("pnl", 0)
                remaining = round(pos["total_shares"] - sell_size, 6)
                pos["realized_pnl"] = round(pos.get("realized_pnl", 0) + pnl, 6)
                if remaining <= 0:
                    pos["total_shares"] = 0
                    pos["total_cost"] = 0
                    pos["status"] = "CLOSED"
                else:
                    pos["total_shares"] = remaining
                    pos["total_cost"] = round(pos["avg_price"] * remaining, 6)

        elif etype == "RESOLUTION":
            # Find position by pos_id, original_bet_id, or entry event_id
            pid = e.get("pos_id", e.get("original_bet_id"))
            found = False
            for p in positions.values():
                if p["status"] != "OPEN":
                    continue
                # Match by pos_id
                if p["pos_id"] == pid:
                    found = True
                # Match by entry event_id (for v1 migrated data where original_bet_id = event_id)
                elif any(entry.get("event_id") == pid for entry in p.get("entries", [])):
                    found = True
                if found:
                    pnl = e.get("pnl", 0)
                    p["realized_pnl"] = round(p.get("realized_pnl", 0) + pnl, 6)
                    p["total_shares"] = 0
                    p["total_cost"] = 0
                    p["status"] = "WON" if e.get("result") == "WON" else "LOST"
                    break

        elif etype == "WITHDRAW":
            key = token_id or f"_event_{e.get('pos_id', '?')}"
            pos = positions.get(key)
            if pos and pos["status"] == "OPEN":
                pos["total_shares"] = 0
                pos["total_cost"] = 0
                pos["status"] = "WITHDRAWN"

    # Copy rules from existing positions (rules aren't in events)
    for key, old_pos in ledger["positions"].items():
        if key in positions and old_pos.get("rules"):
            positions[key]["rules"] = old_pos["rules"]

    # Report differences
    old_open = {k: p for k, p in ledger["positions"].items() if p["status"] == "OPEN"}
    new_open = {k: p for k, p in positions.items() if p["status"] == "OPEN"}
    diffs = 0

    for key in set(list(old_open.keys()) + list(new_open.keys())):
        old = old_open.get(key)
        new = new_open.get(key)
        if old and not new:
            print(f"  DRIFT: {old['market'][:50]} -- was OPEN, should be CLOSED")
            diffs += 1
        elif new and not old:
            print(f"  DRIFT: {new['market'][:50]} -- was missing, should be OPEN ({new['total_shares']} shares)")
            diffs += 1
        elif old and new:
            if abs(old["total_shares"] - new["total_shares"]) > 0.01:
                print(f"  DRIFT: {old['market'][:50]} -- shares {old['total_shares']} -> {new['total_shares']}")
                diffs += 1

    if diffs == 0:
        print("  Positions match event log. No drift detected.")
    else:
        print(f"\n  Fixed {diffs} drift(s).")

    ledger["positions"] = positions
    ledger["next_pos_id"] = next_pos_id
    _save(ledger)
    return positions


def reconcile():
    """Cross-reference ledger positions against on-chain data."""
    import httpx
    from positions import get_address, DATA_API
    ledger = _load()

    # Fetch on-chain positions
    address = get_address()
    resp = httpx.get(f"{DATA_API}/positions", params={"user": address}, timeout=15)
    resp.raise_for_status()
    on_chain = resp.json()

    # Build on-chain map: token_id -> total shares
    chain_map = {}
    for p in on_chain:
        tid = p.get("asset", "")
        size = float(p.get("size", 0))
        if tid and size > 0.01:
            chain_map[tid] = chain_map.get(tid, 0) + size

    issues = 0

    # Check each open ledger position
    for key, pos in ledger["positions"].items():
        if pos["status"] != "OPEN":
            continue
        tid = pos["token_id"]
        ledger_shares = pos["total_shares"]
        chain_shares = chain_map.pop(tid, 0)

        if abs(ledger_shares - chain_shares) > 0.01:
            print(f"  MISMATCH: {pos['market'][:50]}")
            print(f"    Ledger: {ledger_shares} shares  |  On-chain: {chain_shares} shares")
            issues += 1

    # Check for on-chain positions not in ledger
    for tid, size in chain_map.items():
        if size > 0.01:
            print(f"  UNTRACKED: token {tid[:20]}... has {size} shares on-chain but not in ledger")
            issues += 1

    if issues == 0:
        print("  Ledger matches on-chain positions. All good.")
    else:
        print(f"\n  {issues} discrepancy(ies) found.")
    return issues


# ── Migration from v1 ─────────────────────────────────────────────

def migrate():
    """Migrate v1 ledger (trades/open_bets/closed_bets) to v2 (events/positions)."""
    if not os.path.exists(LEDGER_FILE):
        print("No ledger.json found.")
        return

    with open(LEDGER_FILE) as f:
        v1 = json.load(f)

    if v1.get("version") == 2:
        print("Already v2 format.")
        return

    # Backup
    backup = LEDGER_FILE + ".v1.backup"
    with open(backup, "w") as f:
        json.dump(v1, f, indent=2)
    print(f"Backed up v1 to {backup}")

    # Convert trades[] to events[]
    events = []
    for t in v1.get("trades", []):
        action = t.get("action", "")
        event = {"id": t["id"], "timestamp": t["timestamp"]}

        if action == "BUY":
            event.update({
                "type": "BUY",
                "market": t["market"],
                "side": t["side"],
                "price": t["price"],
                "size": t["size"],
                "cost": t.get("cost", round(t["price"] * t["size"], 6)),
                "token_id": t.get("token_id", ""),
                "notes": t.get("notes", ""),
            })
        elif action == "SELL":
            event.update({
                "type": "SELL",
                "market": t["market"],
                "side": t.get("side", ""),
                "sell_price": t["sell_price"],
                "buy_price": t.get("buy_price", 0),
                "size": t["size"],
                "revenue": t.get("revenue", 0),
                "pnl": t.get("pnl", 0),
                "token_id": t.get("token_id", ""),
                "notes": t.get("notes", ""),
                "original_bet_id": t.get("original_bet_id"),
            })
        elif action == "RESOLUTION":
            event.update({
                "type": "RESOLUTION",
                "market": t["market"],
                "result": t.get("result", ""),
                "pnl": t.get("pnl", 0),
                "notes": t.get("notes", ""),
                "original_bet_id": t.get("original_bet_id"),
            })
        else:
            event.update({"type": action})
            event.update({k: v for k, v in t.items() if k not in ("id", "timestamp", "action")})

        events.append(event)

    # Build positions from events + open_bets/closed_bets for rules
    positions = {}
    next_pos_id = 1

    # Group BUY events by token_id
    token_buys = {}
    for e in events:
        if e.get("type") == "BUY":
            tid = e.get("token_id", "")
            key = tid or f"_event_{e['id']}"
            token_buys.setdefault(key, []).append(e)

    # Build a map of sell/resolution events by original_bet_id
    sell_events = {}
    for e in events:
        if e.get("type") in ("SELL", "RESOLUTION"):
            bid = e.get("original_bet_id") or e.get("pos_id")
            if bid:
                sell_events.setdefault(bid, []).append(e)

    # Map from old bet_id to token_id for linking sells to positions
    bet_to_token = {}
    for e in events:
        if e.get("type") == "BUY" and e.get("token_id"):
            bet_to_token[e["id"]] = e["token_id"]

    # Build positions from grouped buys
    for key, buys in token_buys.items():
        total_shares = sum(b["size"] for b in buys)
        total_cost = sum(b.get("cost", round(b["price"] * b["size"], 6)) for b in buys)
        avg_price = round(total_cost / total_shares, 6) if total_shares > 0 else 0

        # Apply sells and resolutions
        realized_pnl = 0
        status = "OPEN"
        for buy in buys:
            for se in sell_events.get(buy["id"], []):
                if se["type"] == "SELL":
                    total_shares = round(total_shares - se["size"], 6)
                    realized_pnl = round(realized_pnl + se.get("pnl", 0), 6)
                elif se["type"] == "RESOLUTION":
                    realized_pnl = round(realized_pnl + se.get("pnl", 0), 6)
                    if se.get("result") == "WON":
                        status = "WON"
                    else:
                        status = "LOST"
                    total_shares = 0

        if total_shares <= 0 and status == "OPEN":
            status = "CLOSED"

        # Get rules from open_bets or closed_bets
        rules = None
        for source in [v1.get("open_bets", []), v1.get("closed_bets", [])]:
            for b in source:
                if b.get("token_id") == key or (not b.get("token_id") and b["id"] == buys[0]["id"]):
                    if b.get("rules"):
                        rules = b["rules"]
                        break
            if rules:
                break

        # Check if any bet for this token was WITHDRAWN
        for source in [v1.get("closed_bets", [])]:
            for b in source:
                if b.get("token_id") == key and b.get("status") == "WITHDRAWN":
                    # This specific bet was withdrawn -- reduce shares
                    total_shares = round(total_shares - b.get("size", 0), 6)
                    if total_shares <= 0:
                        status = "WITHDRAWN"

        pos = {
            "pos_id": next_pos_id,
            "market": buys[0]["market"],
            "side": buys[0]["side"],
            "token_id": buys[0].get("token_id", ""),
            "status": status,
            "total_shares": max(0, total_shares),
            "total_cost": round(avg_price * max(0, total_shares), 6) if status == "OPEN" else 0,
            "avg_price": avg_price,
            "realized_pnl": realized_pnl,
            "first_entry": buys[0]["timestamp"],
            "entries": [
                {"event_id": b["id"], "price": b["price"], "size": b["size"], "timestamp": b["timestamp"]}
                for b in buys
            ],
        }
        if rules:
            pos["rules"] = rules
        positions[key] = pos
        next_pos_id += 1

    # Handle withdrawal events (mark events for positions that were withdrawn)
    for b in v1.get("closed_bets", []):
        if b.get("status") == "WITHDRAWN":
            tid = b.get("token_id", "")
            if tid in positions:
                # Add a synthetic WITHDRAW event
                events.append({
                    "id": _next_event_id_from_list(events),
                    "timestamp": b.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    "type": "WITHDRAW",
                    "market": b.get("market", ""),
                    "token_id": tid,
                    "size": b.get("size", 0),
                    "notes": "Migrated from v1 WITHDRAWN status",
                })

    v2 = {
        "version": 2,
        "initial_deposit": v1.get("initial_deposit", 0),
        "funds": v1.get("funds", 0),
        "events": events,
        "positions": positions,
        "next_pos_id": next_pos_id,
        "pnl_total": v1.get("pnl_total", 0),
    }

    with open(LEDGER_FILE, "w") as f:
        json.dump(v2, f, indent=2)

    open_count = sum(1 for p in positions.values() if p["status"] == "OPEN")
    closed_count = sum(1 for p in positions.values() if p["status"] != "OPEN")
    print(f"Migrated to v2: {len(events)} events, {open_count} open + {closed_count} closed positions")
    return v2


def _next_event_id_from_list(events):
    if not events:
        return 1
    return max(e["id"] for e in events) + 1


# ── Display ───────────────────────────────────────────────────────

def _fetch_live_prices(positions):
    """Fetch live midpoint prices for all open positions."""
    live_prices = {}
    if not positions:
        return live_prices
    try:
        from monitor import get_midpoint
        token_ids = set(
            p["token_id"] for p in positions.values()
            if p["status"] == "OPEN" and p.get("token_id")
        )
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

    positions = ledger.get("positions", {})
    live_prices = _fetch_live_prices(positions)

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

    # Max bet using total portfolio value (not just funds)
    open_cost = sum(p["total_cost"] for p in positions.values() if p["status"] == "OPEN")
    funds = on_chain if on_chain is not None else ledger['funds']
    total_portfolio = funds + open_cost
    print(f"  Max Single Bet:  ${total_portfolio * 0.20:.2f} (20% of ${total_portfolio:.2f})")

    # Open positions
    open_positions = {k: p for k, p in positions.items() if p["status"] == "OPEN"}
    total_unrealized = 0
    market_value_total = 0
    has_prices = bool(live_prices)

    if has_prices:
        for p in open_positions.values():
            tid = p["token_id"]
            if tid in live_prices:
                mv = live_prices[tid] * p["total_shares"]
                market_value_total += mv
                total_unrealized += mv - p["total_cost"]
            else:
                market_value_total += p["total_cost"]
    else:
        market_value_total = open_cost

    print(f"  In Open Bets:    ${open_cost:.2f} (cost basis)")
    if has_prices:
        print(f"  Market Value:    ${market_value_total:.2f} (live)")
        print(f"  Unrealized PnL:  ${total_unrealized:+.2f}")
        total_live = funds + market_value_total
        print(f"  Total Value:     ${total_live:.2f} (live)")
    else:
        print(f"  Total Value:     ${funds + open_cost:.2f} (book)")

    if open_positions:
        print(f"\n  OPEN POSITIONS ({len(open_positions)}):")
        for p in sorted(open_positions.values(), key=lambda x: -x["total_cost"]):
            tid = p["token_id"]
            mid = live_prices.get(tid)
            pct_portfolio = (p["total_cost"] / total_portfolio * 100) if total_portfolio > 0 else 0
            over = " *** OVER 20% ***" if pct_portfolio > 20 else ""
            print(f"    #{p['pos_id']} {p['market'][:50]}")
            price_line = f"       {p['side']} @ {p['avg_price']:.4f} x {p['total_shares']} = ${p['total_cost']:.2f} ({pct_portfolio:.1f}%){over}"
            if mid is not None:
                mv = mid * p["total_shares"]
                upnl = mv - p["total_cost"]
                pct_pnl = (upnl / p["total_cost"] * 100) if p["total_cost"] > 0 else 0
                price_line += f"  |  NOW {mid:.3f} = ${mv:.2f} ({upnl:+.2f} / {pct_pnl:+.1f}%)"
            print(price_line)
            if len(p.get("entries", [])) > 1:
                print(f"       Entries: {len(p['entries'])} buys merged")
            if p.get("rules"):
                r = p["rules"]
                parts = []
                if r.get("stop_loss") is not None:
                    parts.append(f"Stop: {r['stop_loss']}")
                if r.get("take_profit_1") is not None:
                    parts.append(f"TP1: {r['take_profit_1']}")
                if r.get("take_profit_2") is not None:
                    parts.append(f"TP2: {r['take_profit_2']}")
                if parts:
                    rule_line = f"       Rules: {' | '.join(parts)}"
                    if mid is not None:
                        sl = r.get("stop_loss")
                        tp1 = r.get("take_profit_1")
                        if sl is not None and mid <= sl * 1.15:
                            rule_line += "  ** NEAR STOP **"
                        elif tp1 is not None and mid >= tp1 * 0.95:
                            rule_line += "  ** NEAR TP1 **"
                    print(rule_line)

    # Closed positions
    closed = [p for p in positions.values() if p["status"] != "OPEN"]
    if closed:
        print(f"\n  CLOSED POSITIONS ({len(closed)}):")
        for p in closed[-10:]:
            pnl = p.get("realized_pnl", 0)
            print(f"    #{p['pos_id']} {p['market'][:50]}  ->  {p['status']}  (${pnl:+.2f})")

    # Win rate
    wins = sum(1 for p in closed if p.get("realized_pnl", 0) > 0)
    total = len(closed)
    if total > 0:
        print(f"\n  Win Rate: {wins}/{total} ({wins/total*100:.0f}%)")

    print("=" * 60)
    return ledger


def history():
    """Print event history."""
    ledger = _load()
    events = ledger.get("events", ledger.get("trades", []))
    print(f"{'ID':>4}  {'Date':>10}  {'Type':>10}  {'Market':<40}  {'PnL':>10}")
    print("-" * 80)
    for e in events:
        date = e["timestamp"][:10]
        etype = e.get("type", e.get("action", "?"))
        market = e.get("market", "?")[:40]
        pnl = e.get("pnl", -e.get("cost", 0))
        print(f"{e['id']:>4}  {date}  {etype:>10}  {market:<40}  ${pnl:>+9.2f}")


def analytics():
    """Print win rate, PnL, and hold time analytics broken down by strategy."""
    ledger = _load()
    positions = ledger.get("positions", {})
    events = ledger.get("events", [])

    # Only analyze closed positions (CLOSED, WON, LOST -- skip WITHDRAWN)
    closed = [p for p in positions.values() if p["status"] in ("CLOSED", "WON", "LOST")]
    open_pos = [p for p in positions.values() if p["status"] == "OPEN"]

    if not closed and not open_pos:
        print("No positions to analyze.")
        return

    # Build event lookup for computing hold times
    event_map = {e["id"]: e for e in events}

    # Classify each position by strategy based on notes and market name
    def classify(pos):
        """Infer strategy from notes and market characteristics."""
        notes = ""
        # Gather all notes from buy events for this position
        for entry in pos.get("entries", []):
            eid = entry.get("event_id")
            if eid and eid in event_map:
                n = event_map[eid].get("notes", "")
                if n:
                    notes += " " + n.lower()
        market = pos.get("market", "").lower()

        if "bond" in notes or "near-certain" in notes or "sure thing" in notes:
            return "Bond Play"
        if "arbitrage" in notes or "arb " in notes:
            return "Arbitrage"
        if "lottery" in notes or "tail risk" in notes:
            return "Lottery/Tail"
        if "mispriced" in notes or "bookmaker" in notes or "edge" in notes:
            return "Mispriced Prob"
        if "stale" in notes or "second-order" in notes:
            return "Stale Price"
        if "stop loss" in notes or "swing" in notes or "volatility" in notes:
            return "Swing/Vol"
        if any(kw in market for kw in ("btc", "eth", "bitcoin", "crypto")):
            return "Crypto"
        if any(kw in market for kw in ("oil", "cl ", "cl_", "wti", "brent")):
            return "Commodities"
        if any(kw in market for kw in ("iran", "china", "taiwan", "starmer", "ceasefire")):
            return "Geopolitical"
        return "Other"

    def compute_hold_days(pos):
        """Compute hold time in days from first entry to last sell/resolution event."""
        first = pos.get("first_entry", "")
        if not first:
            return None
        # Find the last event for this position
        last_ts = None
        for e in events:
            if e.get("type") in ("SELL", "RESOLUTION") and e.get("pos_id") == pos.get("pos_id"):
                last_ts = e.get("timestamp")
        if not last_ts:
            return None
        try:
            from datetime import datetime, timezone
            t0 = datetime.fromisoformat(first.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            return max(0, (t1 - t0).total_seconds() / 86400)
        except (ValueError, TypeError):
            return None

    # --- Overall stats ---
    print("=" * 60)
    print("TRADING ANALYTICS")
    print("=" * 60)

    total_pnl = sum(p.get("realized_pnl", 0) for p in closed)
    wins = [p for p in closed if p.get("realized_pnl", 0) > 0]
    losses = [p for p in closed if p.get("realized_pnl", 0) < 0]
    breakeven = [p for p in closed if p.get("realized_pnl", 0) == 0]

    print(f"\n  OVERALL ({len(closed)} closed trades)")
    print(f"    Wins:       {len(wins)}")
    print(f"    Losses:     {len(losses)}")
    print(f"    Breakeven:  {len(breakeven)}")
    if closed:
        wr = len(wins) / len(closed) * 100
        print(f"    Win Rate:   {wr:.0f}%")
    print(f"    Total PnL:  ${total_pnl:+.2f}")
    if wins:
        avg_win = sum(p["realized_pnl"] for p in wins) / len(wins)
        print(f"    Avg Win:    ${avg_win:+.2f}")
    if losses:
        avg_loss = sum(p["realized_pnl"] for p in losses) / len(losses)
        print(f"    Avg Loss:   ${avg_loss:+.2f}")
    if wins and losses:
        avg_w = sum(p["realized_pnl"] for p in wins) / len(wins)
        avg_l = abs(sum(p["realized_pnl"] for p in losses) / len(losses))
        if avg_l > 0:
            print(f"    Win/Loss:   {avg_w/avg_l:.2f}x")

    # Hold times
    hold_days = [compute_hold_days(p) for p in closed]
    hold_days = [d for d in hold_days if d is not None]
    if hold_days:
        avg_hold = sum(hold_days) / len(hold_days)
        print(f"    Avg Hold:   {avg_hold:.1f} days")

    # --- By strategy ---
    strategy_groups = {}
    for p in closed:
        strat = classify(p)
        strategy_groups.setdefault(strat, []).append(p)

    if strategy_groups:
        print(f"\n  BY STRATEGY")
        print(f"  {'Strategy':<18} {'W-L':>5} {'WR%':>5} {'PnL':>9} {'Avg':>8} {'Hold':>6}")
        print(f"  {'-'*18} {'-'*5} {'-'*5} {'-'*9} {'-'*8} {'-'*6}")

        for strat in sorted(strategy_groups.keys()):
            positions_in = strategy_groups[strat]
            w = sum(1 for p in positions_in if p.get("realized_pnl", 0) > 0)
            l = sum(1 for p in positions_in if p.get("realized_pnl", 0) < 0)
            n = len(positions_in)
            wr = (w / n * 100) if n > 0 else 0
            pnl = sum(p.get("realized_pnl", 0) for p in positions_in)
            avg_pnl = pnl / n if n > 0 else 0
            holds = [compute_hold_days(p) for p in positions_in]
            holds = [d for d in holds if d is not None]
            avg_h = sum(holds) / len(holds) if holds else 0
            print(f"  {strat:<18} {w}-{l:>2} {wr:>4.0f}% ${pnl:>+8.2f} ${avg_pnl:>+7.2f} {avg_h:>5.1f}d")

    # --- Open positions summary ---
    if open_pos:
        open_groups = {}
        for p in open_pos:
            strat = classify(p)
            open_groups.setdefault(strat, []).append(p)

        print(f"\n  OPEN POSITIONS BY STRATEGY ({len(open_pos)} total)")
        for strat in sorted(open_groups.keys()):
            positions_in = open_groups[strat]
            cost = sum(p["total_cost"] for p in positions_in)
            names = [f"#{p['pos_id']}" for p in positions_in]
            print(f"    {strat:<18} {len(positions_in)} pos  ${cost:.2f} cost  ({', '.join(names)})")

    # --- Biggest winners and losers ---
    if len(closed) >= 3:
        sorted_by_pnl = sorted(closed, key=lambda p: p.get("realized_pnl", 0))
        print(f"\n  BIGGEST LOSERS")
        for p in sorted_by_pnl[:3]:
            pnl = p.get("realized_pnl", 0)
            if pnl < 0:
                print(f"    #{p['pos_id']} {p['market'][:45]}  ${pnl:+.2f}")
        print(f"\n  BIGGEST WINNERS")
        for p in reversed(sorted_by_pnl[-3:]):
            pnl = p.get("realized_pnl", 0)
            if pnl > 0:
                print(f"    #{p['pos_id']} {p['market'][:45]}  ${pnl:+.2f}")

    print("=" * 60)


# ── CLI ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 ledger.py status               # Portfolio overview")
        print("  python3 ledger.py history               # Event history")
        print("  python3 ledger.py max-bet               # Show max bet size")
        print("  python3 ledger.py set-rules <pos_id> <stop> <tp1> <tp2>")
        print("  python3 ledger.py sync                  # Sync with on-chain USDC")
        print("  python3 ledger.py rebuild               # Rebuild positions from events")
        print("  python3 ledger.py analytics             # Win rate, PnL, hold time by strategy")
        print("  python3 ledger.py reconcile             # Verify vs on-chain positions")
        print("  python3 ledger.py migrate               # Migrate v1 -> v2 format")
        print("  python3 ledger.py init <amount>         # Set initial bankroll")
        print("  python3 ledger.py deposit <amount>      # Add funds")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "init":
        if len(sys.argv) < 3:
            print("Usage: python3 ledger.py init <amount>"); sys.exit(1)
        init_funds(float(sys.argv[2]))
    elif cmd == "deposit":
        if len(sys.argv) < 3:
            print("Usage: python3 ledger.py deposit <amount>"); sys.exit(1)
        deposit(float(sys.argv[2]))
    elif cmd == "status":
        status()
    elif cmd == "history":
        history()
    elif cmd == "sync":
        sync()
    elif cmd == "max-bet":
        ledger = _load()
        positions = ledger.get("positions", {})
        funds = ledger["funds"]
        open_cost = sum(p["total_cost"] for p in positions.values() if p["status"] == "OPEN")
        total = funds + open_cost
        print(f"Max single bet: ${get_max_bet():.2f} (20% of ${total:.2f} total value)")
        print(f"  Available funds: ${funds:.2f}  |  In open positions: ${open_cost:.2f}")
    elif cmd == "set-rules":
        if len(sys.argv) < 6:
            print("Usage: python3 ledger.py set-rules <pos_id> <stop_loss> <tp1> <tp2> [tp1_pct]")
            sys.exit(1)
        pid = int(sys.argv[2])
        stop = float(sys.argv[3])
        tp1 = float(sys.argv[4])
        tp2 = float(sys.argv[5])
        tp1_pct = float(sys.argv[6]) if len(sys.argv) > 6 else 0.50
        set_rules(pid, stop_loss=stop, take_profit_1=tp1, take_profit_2=tp2, tp1_pct=tp1_pct)
    elif cmd == "analytics":
        analytics()
    elif cmd == "rebuild":
        rebuild()
    elif cmd == "reconcile":
        reconcile()
    elif cmd == "migrate":
        migrate()
    else:
        print(f"Unknown command: {cmd}")
