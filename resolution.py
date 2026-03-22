"""Resolution tracker -- find markets about to resolve for bond plays.

Scans Polymarket for markets with approaching end dates, high prices (90c+),
and sufficient volume. These are candidates for Strategy 1 (bond plays).

Usage:
    python3 resolution.py                 # Scan for near-resolution markets
    python3 resolution.py bonds           # Bond play candidates (90-98c, high volume)
    python3 resolution.py expiring [days] # Markets expiring within N days (default: 7)
    python3 resolution.py check           # Check our open positions for upcoming resolutions
"""
import json
import sys
import time
import httpx
from datetime import datetime, timezone, timedelta

GAMMA_API = "https://gamma-api.polymarket.com"
PAGE_SIZE = 500


def fetch_events_page(offset=0, limit=500):
    """Fetch a page of active events."""
    resp = httpx.get(f"{GAMMA_API}/events", params={
        "limit": limit,
        "offset": offset,
        "active": True,
        "closed": False,
        "order": "volume",
        "ascending": False,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_end_date(market):
    """Parse the end date from a market. Returns datetime or None."""
    end_str = market.get("endDate") or market.get("end_date_iso", "")
    if not end_str:
        return None
    try:
        # Handle various ISO formats
        end_str = end_str.replace("Z", "+00:00")
        return datetime.fromisoformat(end_str)
    except (ValueError, TypeError):
        return None


def parse_prices(market):
    """Parse outcome prices. Returns list of floats or None."""
    prices_raw = market.get("outcomePrices", "")
    if not prices_raw:
        return None
    try:
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw
        return [float(p) for p in prices]
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def parse_token_ids(market):
    """Parse clobTokenIds into a list."""
    raw = market.get("clobTokenIds", "")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    return raw or []


def scan_expiring(max_days=7, max_pages=5, min_volume=1000):
    """Find markets expiring within max_days."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=max_days)
    expiring = []

    for page in range(max_pages):
        print(f"  Scanning page {page + 1}...", end=" ", flush=True)
        try:
            events = fetch_events_page(offset=page * PAGE_SIZE, limit=PAGE_SIZE)
        except (httpx.HTTPError, ValueError) as e:
            print(f"error: {e}")
            break

        if not events:
            print("done")
            break

        count = 0
        for event in events:
            for market in event.get("markets", []):
                end_date = parse_end_date(market)
                if end_date is None:
                    continue

                if now < end_date <= cutoff:
                    prices = parse_prices(market)
                    volume = market.get("volumeNum", 0) or 0
                    if volume < min_volume:
                        continue

                    token_ids = parse_token_ids(market)
                    days_left = (end_date - now).total_seconds() / 86400

                    entry = {
                        "event": event.get("title", ""),
                        "question": market.get("question", ""),
                        "end_date": end_date.strftime("%Y-%m-%d %H:%M"),
                        "days_left": round(days_left, 1),
                        "prices": prices,
                        "volume": volume,
                        "liquidity": market.get("liquidityNum", 0) or 0,
                        "token_ids": token_ids,
                        "condition_id": market.get("conditionId", ""),
                    }
                    expiring.append(entry)
                    count += 1

        print(f"{count} expiring markets found")
        if len(events) < PAGE_SIZE:
            break
        time.sleep(0.2)

    expiring.sort(key=lambda x: x["days_left"])
    return expiring


def scan_bond_candidates(max_days=30, max_pages=8, min_volume=5000,
                          min_price=0.90, max_price=0.98):
    """Find bond play candidates: high-price markets with upcoming resolution.

    Bond plays are markets priced 90-98c where the outcome is near-certain.
    We buy at 90-98c and collect the remaining 2-10c when it resolves YES.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=max_days)
    candidates = []

    for page in range(max_pages):
        print(f"  Scanning page {page + 1}...", end=" ", flush=True)
        try:
            events = fetch_events_page(offset=page * PAGE_SIZE, limit=PAGE_SIZE)
        except (httpx.HTTPError, ValueError) as e:
            print(f"error: {e}")
            break

        if not events:
            print("done")
            break

        count = 0
        for event in events:
            for market in event.get("markets", []):
                prices = parse_prices(market)
                if not prices or len(prices) < 2:
                    continue

                yes_price = prices[0]
                volume = market.get("volumeNum", 0) or 0

                # Filter: high price, decent volume
                if not (min_price <= yes_price <= max_price and volume >= min_volume):
                    continue

                end_date = parse_end_date(market)
                days_left = None
                if end_date and end_date > now:
                    days_left = (end_date - now).total_seconds() / 86400
                    # Skip if too far out
                    if days_left > max_days:
                        continue

                token_ids = parse_token_ids(market)

                # Calculate potential return
                profit_per_share = 1.0 - yes_price
                return_pct = (profit_per_share / yes_price) * 100
                if days_left and days_left > 0:
                    annualized = return_pct * (365 / days_left)
                else:
                    annualized = None

                entry = {
                    "event": event.get("title", ""),
                    "question": market.get("question", ""),
                    "yes_price": yes_price,
                    "profit_per_share": round(profit_per_share, 4),
                    "return_pct": round(return_pct, 2),
                    "annualized_pct": round(annualized, 1) if annualized else None,
                    "end_date": end_date.strftime("%Y-%m-%d") if end_date else "Unknown",
                    "days_left": round(days_left, 1) if days_left else None,
                    "volume": volume,
                    "liquidity": market.get("liquidityNum", 0) or 0,
                    "token_ids": token_ids,
                }
                candidates.append(entry)
                count += 1

        print(f"{count} candidates found")
        if len(events) < PAGE_SIZE:
            break
        time.sleep(0.2)

    # Sort by return_pct descending (best deals first)
    candidates.sort(key=lambda x: x["return_pct"], reverse=True)
    return candidates


def check_our_positions():
    """Check which of our open bets are near resolution."""
    try:
        from ledger import get_open_bets
    except ImportError:
        print("Cannot load ledger.")
        return

    open_bets = get_open_bets()
    if not open_bets:
        print("No open positions.")
        return

    print("Checking open positions for upcoming resolutions...\n")

    for bet in open_bets:
        token_id = bet.get("token_id", "")
        if not token_id:
            continue

        # Try to find market data
        try:
            resp = httpx.get(f"{GAMMA_API}/markets", params={
                "clob_token_ids": token_id,
            }, timeout=15)
            resp.raise_for_status()
            markets = resp.json()
        except (httpx.HTTPError, ValueError):
            continue

        if not markets:
            continue

        market = markets[0]
        end_date = parse_end_date(market)
        now = datetime.now(timezone.utc)

        print(f"  #{bet['id']} {bet['market'][:50]}")
        if end_date:
            days_left = (end_date - now).total_seconds() / 86400
            if days_left <= 0:
                print(f"    ** PAST END DATE ** ({end_date.strftime('%Y-%m-%d')})")
            elif days_left <= 3:
                print(f"    ** RESOLVING SOON ** ({days_left:.1f} days left)")
            elif days_left <= 7:
                print(f"    Resolves in {days_left:.1f} days ({end_date.strftime('%Y-%m-%d')})")
            else:
                print(f"    Resolves: {end_date.strftime('%Y-%m-%d')} ({days_left:.0f} days)")
        else:
            print(f"    End date: unknown")


def print_expiring(results):
    """Print expiring markets."""
    print("=" * 70)
    print(f"MARKETS EXPIRING SOON ({len(results)} found)")
    print("=" * 70)

    for i, r in enumerate(results[:30], 1):
        prices_str = ""
        if r["prices"]:
            parts = [f"{p:.2f}" for p in r["prices"]]
            prices_str = "/".join(parts)

        print(f"\n  #{i}  {r['question'][:65]}")
        print(f"       Ends: {r['end_date']} ({r['days_left']}d left)")
        print(f"       Prices: {prices_str}  |  Vol: ${r['volume']:,.0f}  |  Liq: ${r['liquidity']:,.0f}")
        if r["token_ids"]:
            print(f"       Token: {r['token_ids'][0][:20]}...")


def scan_quick_bonds(max_days=14, min_volume=10000, min_price=0.90, max_price=0.98,
                      min_liquidity=5000):
    """Focused bond scan: 90c+ markets expiring in 1-2 weeks with good liquidity.

    This is the optimized scanner for the best bond play candidates.
    Focuses on the sweet spot: high price, near expiry, sufficient liquidity.
    Sorted by annualized return (best risk-adjusted deals first).
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=max_days)
    candidates = []

    # Scan more pages since we have strict filters
    for page in range(12):
        print(f"  Scanning page {page + 1}...", end=" ", flush=True)
        try:
            events = fetch_events_page(offset=page * PAGE_SIZE, limit=PAGE_SIZE)
        except (httpx.HTTPError, ValueError) as e:
            print(f"error: {e}")
            break

        if not events:
            print("done")
            break

        count = 0
        for event in events:
            for market in event.get("markets", []):
                prices = parse_prices(market)
                if not prices or len(prices) < 2:
                    continue

                volume = market.get("volumeNum", 0) or 0
                liquidity = market.get("liquidityNum", 0) or 0

                # Strict filters for quality bond plays
                if volume < min_volume or liquidity < min_liquidity:
                    continue

                end_date = parse_end_date(market)
                if not end_date or end_date <= now or end_date > cutoff:
                    continue

                days_left = (end_date - now).total_seconds() / 86400
                token_ids = parse_token_ids(market)
                outcomes = market.get("outcomes", [])
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except (json.JSONDecodeError, TypeError):
                        outcomes = ["Yes", "No"]

                # Check both sides for bond-worthy prices
                for idx, price in enumerate(prices):
                    if not (min_price <= price <= max_price):
                        continue

                    side_name = outcomes[idx] if idx < len(outcomes) else ("Yes" if idx == 0 else "No")
                    token_id = token_ids[idx] if idx < len(token_ids) else ""

                    profit_per_share = 1.0 - price
                    return_pct = (profit_per_share / price) * 100
                    annualized = return_pct * (365 / days_left) if days_left > 0 else 0

                    entry = {
                        "event": event.get("title", ""),
                        "question": market.get("question", ""),
                        "side": side_name,
                        "price": price,
                        "profit_per_share": round(profit_per_share, 4),
                        "return_pct": round(return_pct, 2),
                        "annualized_pct": round(annualized, 1),
                        "end_date": end_date.strftime("%Y-%m-%d"),
                        "days_left": round(days_left, 1),
                        "volume": volume,
                        "liquidity": liquidity,
                        "token_id": token_id,
                        "token_ids": token_ids,
                    }
                    candidates.append(entry)
                    count += 1

        print(f"{count} candidates found")
        if len(events) < PAGE_SIZE:
            break
        time.sleep(0.2)

    # Sort by annualized return (best risk-adjusted deals first)
    candidates.sort(key=lambda x: x["annualized_pct"] or 0, reverse=True)
    return candidates


def print_quick_bonds(results):
    """Print quick bond scan results with detailed info."""
    print("=" * 70)
    print(f"QUICK BOND CANDIDATES ({len(results)} found)")
    print(f"  90-98c markets expiring within 14 days, min $10k vol, min $5k liq")
    print("=" * 70)

    for i, r in enumerate(results[:25], 1):
        ann_str = f"{r['annualized_pct']:.0f}% ann." if r["annualized_pct"] else "?"
        print(f"\n  #{i}  {r['question'][:55]} [{r['side'].upper()}]")
        print(f"       Price: {r['price']:.2f}  |  Return: {r['return_pct']:.1f}% ({ann_str})")
        print(f"       Ends: {r['end_date']} ({r['days_left']:.0f}d)  |  Vol: ${r['volume']:,.0f}  |  Liq: ${r['liquidity']:,.0f}")
        if r.get("token_id"):
            print(f"       Token: {r['token_id'][:20]}...")

    if not results:
        print("\n  No candidates found matching criteria.")
        print("  Try: python3 resolution.py bonds  (wider search)")


def print_bonds(results):
    """Print bond play candidates."""
    print("=" * 70)
    print(f"BOND PLAY CANDIDATES ({len(results)} found)")
    print("=" * 70)
    print(f"  (Markets at 90-98c with decent volume -- research before buying)\n")

    for i, r in enumerate(results[:30], 1):
        ann_str = f"  ({r['annualized_pct']:.0f}% annualized)" if r["annualized_pct"] else ""
        days_str = f"{r['days_left']:.0f}d" if r["days_left"] else "?"

        print(f"  #{i}  {r['question'][:60]}")
        print(f"       Price: {r['yes_price']:.2f}  |  Return: {r['return_pct']:.1f}%{ann_str}")
        print(f"       Ends: {r['end_date']} ({days_str})  |  Vol: ${r['volume']:,.0f}")
        if r["token_ids"]:
            print(f"       Token[YES]: {r['token_ids'][0][:20]}...")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "bonds"

    if cmd == "quick":
        # Optimized quick scan: 90c+ within 14 days, good liquidity
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 14
        results = scan_quick_bonds(max_days=days)
        print_quick_bonds(results)

    elif cmd == "bonds":
        results = scan_bond_candidates()
        print_bonds(results)

    elif cmd == "expiring":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        results = scan_expiring(max_days=days)
        print_expiring(results)

    elif cmd == "check":
        check_our_positions()

    else:
        print("Usage:")
        print("  python3 resolution.py                 # Bond play candidates (wide)")
        print("  python3 resolution.py quick [days]     # Quick bonds: 90c+, 14 days, high liq")
        print("  python3 resolution.py bonds           # Bond play candidates (wide)")
        print("  python3 resolution.py expiring [days]  # Markets expiring soon")
        print("  python3 resolution.py check            # Check our positions")
