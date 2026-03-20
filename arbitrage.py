"""Scan Polymarket for arbitrage opportunities across all active events."""
import json
import re
import sys
import time
import httpx
from collections import defaultdict

GAMMA_API = "https://gamma-api.polymarket.com"
PAGE_SIZE = 500


def fetch_all_events(max_pages=20):
    """Fetch all active events using pagination. ~9000+ events across ~19 pages."""
    all_events = []
    offset = 0
    page = 0

    while page < max_pages:
        print(f"  Fetching page {page + 1} (offset={offset})...", end=" ", flush=True)
        resp = httpx.get(f"{GAMMA_API}/events", params={
            "limit": PAGE_SIZE,
            "offset": offset,
            "active": True,
            "closed": False,
        }, timeout=30)
        resp.raise_for_status()
        events = resp.json()
        print(f"{len(events)} events")

        if not events:
            break

        all_events.extend(events)
        if len(events) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        page += 1
        time.sleep(0.2)  # Be nice to the API

    return all_events


def parse_prices(market):
    """Parse outcome prices from a market object. Returns list of floats or None."""
    prices_raw = market.get("outcomePrices", "")
    if not prices_raw:
        return None
    try:
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw
        return [float(p) for p in prices]
    except (json.JSONDecodeError, ValueError):
        return None


def scan_outcome_mispricing(events, min_spread=0.005):
    """
    Find markets where Yes + No prices don't sum to ~1.00.
    If sum < 1.00, buying both sides is guaranteed profit.
    If sum > 1.00, selling both sides is guaranteed profit.
    """
    opportunities = []
    for event in events:
        for market in event.get("markets", []):
            prices = parse_prices(market)
            if not prices or len(prices) != 2:
                continue

            total = sum(prices)
            spread = abs(total - 1.0)

            if spread > min_spread:
                opportunities.append({
                    "event": event.get("title", ""),
                    "market": market.get("question", ""),
                    "prices": prices,
                    "sum": total,
                    "spread_pct": spread * 100,
                    "direction": "BUY BOTH" if total < 1.0 else "SELL BOTH",
                    "token_ids": market.get("clobTokenIds", []),
                    "volume": market.get("volumeNum", 0),
                })

    opportunities.sort(key=lambda x: x["spread_pct"], reverse=True)
    return opportunities


def scan_interval_arbitrage(events):
    """
    Find events with multiple threshold markets (same subject, different price levels)
    where prices are logically inconsistent.
    E.g., "Will oil hit $90?" should always be >= "Will oil hit $95?"
    Only compares markets that share the same template/subject.
    """
    opportunities = []

    for event in events:
        markets = event.get("markets", [])
        if len(markets) < 2:
            continue

        market_data = []
        for m in markets:
            question = m.get("question", "")
            prices = parse_prices(m)
            if not prices:
                continue
            yes_price = prices[0]

            numbers = re.findall(r'\$?([\d,]+(?:\.\d+)?)', question)
            numbers = [float(n.replace(',', '')) for n in numbers if n.replace(',', '').replace('.', '').strip()]

            if not numbers:
                continue

            # Replace only dollar amounts and decimal thresholds (e.g., $100, 39.5)
            # Keep integers like "Game 1", "Season 2026" intact to avoid cross-game false positives
            template = re.sub(r'\$[\d,]+(?:\.\d+)?', '##', question)  # $100, $1,000
            template = re.sub(r'\b\d+\.\d+\b', '##', template)  # 39.5, 100.00
            template = template.lower().strip()

            market_data.append({
                "question": question,
                "template": template,
                "threshold": max(numbers),
                "yes_price": yes_price,
                "token_ids": m.get("clobTokenIds", []),
                "volume": m.get("volumeNum", 0),
            })

        if len(market_data) < 2:
            continue

        groups = defaultdict(list)
        for md in market_data:
            groups[md["template"]].append(md)

        for template, group in groups.items():
            if len(group) < 2:
                continue

            group.sort(key=lambda x: x["threshold"])

            is_hit_above = any(w in template for w in ["hit", "above", "reach", "over", "equal to or above"])
            is_drop_below = any(w in template for w in ["below", "under", "drop", "fall"])

            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    low = group[i]
                    high = group[j]

                    if is_hit_above:
                        if low["yes_price"] < high["yes_price"] - 0.01:
                            profit_pct = (high["yes_price"] - low["yes_price"]) * 100
                            opportunities.append({
                                "event": event.get("title", ""),
                                "buy": f"{low['question']} (Yes @ {low['yes_price']:.4f})",
                                "sell": f"{high['question']} (Yes @ {high['yes_price']:.4f})",
                                "logic": f"Hitting {low['threshold']} is easier than {high['threshold']}, but priced lower",
                                "mispricing_pct": profit_pct,
                                "buy_tokens": low["token_ids"],
                                "sell_tokens": high["token_ids"],
                            })
                    elif is_drop_below:
                        if low["yes_price"] > high["yes_price"] + 0.01:
                            profit_pct = (low["yes_price"] - high["yes_price"]) * 100
                            opportunities.append({
                                "event": event.get("title", ""),
                                "buy": f"{high['question']} (Yes @ {high['yes_price']:.4f})",
                                "sell": f"{low['question']} (Yes @ {low['yes_price']:.4f})",
                                "logic": f"Dropping below {high['threshold']} is easier than {low['threshold']}, but priced lower",
                                "mispricing_pct": profit_pct,
                                "buy_tokens": high["token_ids"],
                                "sell_tokens": low["token_ids"],
                            })

    opportunities.sort(key=lambda x: x["mispricing_pct"], reverse=True)
    return opportunities


def scan_multi_outcome_arbitrage(events):
    """
    Find multi-outcome markets (3+ outcomes) where probabilities
    don't sum to 1.00. Only checks mutually exclusive events
    (winner markets, nominee markets, etc.) — skips top-N, O/U, props.
    """
    # Keywords that suggest mutually exclusive outcomes (only 1 can win)
    EXCLUSIVE_KEYWORDS = ["win", "winner", "nominee", "nomination", "next",
                          "who will", "which", "elected", "champion"]
    # Keywords that suggest non-exclusive outcomes (skip these)
    NON_EXCLUSIVE_KEYWORDS = ["top 5", "top 10", "top 20", "over/under",
                              "o/u", "total kills", "total corners",
                              "spread", "visit", "qualify", "finish in"]

    opportunities = []

    for event in events:
        markets = event.get("markets", [])
        if len(markets) < 3:
            continue

        title = event.get("title", "").lower()

        # Skip events that are clearly non-exclusive
        if any(kw in title for kw in NON_EXCLUSIVE_KEYWORDS):
            continue

        # Only process events that look mutually exclusive
        if not any(kw in title for kw in EXCLUSIVE_KEYWORDS):
            continue

        yes_prices = []
        market_names = []
        for m in markets:
            prices = parse_prices(m)
            if not prices:
                continue
            yes_prices.append(prices[0])
            market_names.append(m.get("question", "")[:60])

        if len(yes_prices) < 3:
            continue

        total = sum(yes_prices)
        if total < 0.01:
            continue

        spread = abs(total - 1.0)
        if spread > 0.03:  # >3% mispricing
            opportunities.append({
                "event": event.get("title", ""),
                "num_outcomes": len(yes_prices),
                "probability_sum": total,
                "spread_pct": spread * 100,
                "direction": "BUY ALL YES" if total < 1.0 else "SELL ALL YES",
                "top_markets": list(zip(market_names[:5], yes_prices[:5])),
            })

    opportunities.sort(key=lambda x: x["spread_pct"], reverse=True)
    return opportunities


def scan_all(max_pages=20):
    """Run all arbitrage scans across all active events."""
    print("Fetching all active events...")
    events = fetch_all_events(max_pages=max_pages)
    total_markets = sum(len(e.get("markets", [])) for e in events)
    print(f"\nTotal: {len(events)} events, {total_markets} markets\n")

    # Scan 1: Outcome mispricing (Yes + No)
    print("=" * 60)
    print("SCAN 1: OUTCOME MISPRICING (Yes + No != 1.00)")
    print("=" * 60)
    mispr = scan_outcome_mispricing(events)
    if mispr:
        for opp in mispr[:20]:
            print(f"\n  Event:  {opp['event']}")
            print(f"  Market: {opp['market']}")
            print(f"  Prices: Yes={opp['prices'][0]:.4f}  No={opp['prices'][1]:.4f}  Sum={opp['sum']:.4f}")
            print(f"  Spread: {opp['spread_pct']:.2f}%  ->  {opp['direction']}")
            vol = opp['volume']
            print(f"  Volume: ${vol:,.2f}" if vol else "  Volume: N/A")
    else:
        print("  No binary mispricing found.")

    # Scan 2: Interval arbitrage
    print(f"\n{'=' * 60}")
    print("SCAN 2: INTERVAL ARBITRAGE (threshold ordering violations)")
    print("=" * 60)
    interval = scan_interval_arbitrage(events)
    if interval:
        for opp in interval[:20]:
            print(f"\n  Event: {opp['event']}")
            print(f"  BUY:   {opp['buy']}")
            print(f"  SELL:  {opp['sell']}")
            print(f"  Logic: {opp['logic']}")
            print(f"  Mispricing: {opp['mispricing_pct']:.2f}%")
    else:
        print("  No interval arbitrage found.")

    # Scan 3: Multi-outcome mispricing
    print(f"\n{'=' * 60}")
    print("SCAN 3: MULTI-OUTCOME MISPRICING (sum of Yes != 1.00)")
    print("=" * 60)
    multi = scan_multi_outcome_arbitrage(events)
    if multi:
        for opp in multi[:20]:
            print(f"\n  Event: {opp['event']}")
            print(f"  Outcomes: {opp['num_outcomes']}  |  Sum: {opp['probability_sum']:.4f}")
            print(f"  Spread: {opp['spread_pct']:.2f}%  ->  {opp['direction']}")
            for name, price in opp["top_markets"]:
                print(f"    {name}... @ {price:.4f}")
    else:
        print("  No multi-outcome mispricing found.")

    return {
        "outcome_mispricing": mispr,
        "interval_arbitrage": interval,
        "multi_outcome": multi,
    }


if __name__ == "__main__":
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    scan_all(max_pages=pages)
