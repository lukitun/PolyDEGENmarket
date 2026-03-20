"""Scan Polymarket for high-volatility markets to swing trade."""
import json
import math
import sys
import time
import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
PAGE_SIZE = 500


def fetch_markets(limit=500, offset=0):
    """Fetch active markets from Gamma API."""
    resp = httpx.get(f"{GAMMA_API}/markets", params={
        "limit": limit,
        "offset": offset,
        "active": True,
        "closed": False,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_all_markets(max_pages=10):
    """Paginate through all active markets."""
    all_markets = []
    offset = 0
    page = 0

    while page < max_pages:
        print(f"  Fetching page {page + 1} (offset={offset})...", end=" ", flush=True)
        markets = fetch_markets(limit=PAGE_SIZE, offset=offset)
        print(f"{len(markets)} markets")

        if not markets:
            break
        all_markets.extend(markets)
        if len(markets) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        page += 1
        time.sleep(0.2)

    return all_markets


def fetch_price_history(token_id, interval="1w", fidelity=30):
    """Fetch price history for a token from CLOB API."""
    resp = httpx.get(f"{CLOB_API}/prices-history", params={
        "market": token_id,
        "interval": interval,
        "fidelity": fidelity,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("history", [])


def compute_volatility(history):
    """Compute realized volatility from price history."""
    if len(history) < 5:
        return None

    prices = [h["p"] for h in history if h["p"] > 0]
    if len(prices) < 5:
        return None

    # Log returns
    returns = []
    for i in range(1, len(prices)):
        if prices[i] > 0 and prices[i - 1] > 0:
            returns.append(math.log(prices[i] / prices[i - 1]))

    if len(returns) < 3:
        return None

    # Standard deviation of returns
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    stdev = math.sqrt(variance)

    # Price range
    price_min = min(prices)
    price_max = max(prices)
    price_range = price_max - price_min
    current = prices[-1]

    return {
        "stdev": stdev,
        "price_min": price_min,
        "price_max": price_max,
        "price_range": price_range,
        "current": current,
        "range_pct": (price_range / current * 100) if current > 0 else 0,
        "num_points": len(prices),
    }


def quick_scan(max_pages=5, min_volume=5000, top_n=30):
    """
    Fast scan using Gamma API fields (no price history calls).
    Screens by 1-day and 1-week price changes + volume.
    """
    print("Fetching markets for quick volatility scan...")
    all_markets = fetch_all_markets(max_pages=max_pages)
    print(f"\nTotal: {len(all_markets)} markets\n")

    scored = []
    for m in all_markets:
        volume_24h = m.get("volume24hr", 0) or 0
        volume_1w = m.get("volume1wk", 0) or 0
        if volume_24h < min_volume and volume_1w < min_volume:
            continue

        day_change = abs(float(m.get("oneDayPriceChange", 0) or 0))
        week_change = abs(float(m.get("oneWeekPriceChange", 0) or 0))
        spread = float(m.get("spread", 0) or 0)
        best_bid = float(m.get("bestBid", 0) or 0)
        best_ask = float(m.get("bestAsk", 0) or 0)

        prices_raw = m.get("outcomePrices", "")
        try:
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw
            yes_price = float(prices[0])
        except (json.JSONDecodeError, ValueError, IndexError, TypeError):
            continue

        # Skip near-resolved markets (price >0.95 or <0.05)
        if yes_price > 0.95 or yes_price < 0.05:
            continue

        # Volatility score: weight daily moves more, reward volume
        vol_score = (day_change * 3 + week_change) * math.log10(max(volume_24h, 1) + 1)

        if vol_score > 0:
            token_ids = m.get("clobTokenIds", "")
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except (json.JSONDecodeError, ValueError):
                    token_ids = []

            scored.append({
                "question": m.get("question", ""),
                "yes_price": yes_price,
                "day_change": day_change,
                "week_change": week_change,
                "volume_24h": volume_24h,
                "volume_1w": volume_1w,
                "spread": spread,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "vol_score": vol_score,
                "token_ids": token_ids,
                "condition_id": m.get("conditionId", ""),
            })

    scored.sort(key=lambda x: x["vol_score"], reverse=True)
    return scored[:top_n]


def deep_scan(token_id, interval="1w"):
    """Get detailed volatility analysis for a specific token."""
    print(f"Fetching price history for {token_id[:16]}...")
    history = fetch_price_history(token_id, interval=interval, fidelity=10)
    if not history:
        print("  No price history available.")
        return None

    vol = compute_volatility(history)
    if not vol:
        print("  Not enough data to compute volatility.")
        return None

    print(f"  Data points: {vol['num_points']}")
    print(f"  Current:     {vol['current']:.4f}")
    print(f"  Range:       {vol['price_min']:.4f} - {vol['price_max']:.4f} ({vol['range_pct']:.1f}%)")
    print(f"  Stdev:       {vol['stdev']:.6f}")

    # Suggest entry/exit
    if vol['price_range'] > 0.05:  # >5 cent range
        buy_zone = vol['price_min'] + vol['price_range'] * 0.2
        sell_zone = vol['price_max'] - vol['price_range'] * 0.2
        print(f"\n  Suggested swing trade:")
        print(f"    Buy zone:  <= {buy_zone:.4f}")
        print(f"    Sell zone: >= {sell_zone:.4f}")
        print(f"    Potential: {(sell_zone - buy_zone) * 100:.1f} cents per share")

    return vol


def run_scan(max_pages=5, min_volume=5000, top_n=30):
    """Run the quick volatility scan and print results."""
    results = quick_scan(max_pages=max_pages, min_volume=min_volume, top_n=top_n)

    print("=" * 70)
    print(f"TOP {len(results)} VOLATILE MARKETS (by score)")
    print("=" * 70)

    for i, r in enumerate(results, 1):
        print(f"\n  #{i}  {r['question'][:70]}")
        print(f"       Price: {r['yes_price']:.4f}  |  1d: {r['day_change']:+.4f}  |  1w: {r['week_change']:+.4f}")
        print(f"       Vol24h: ${r['volume_24h']:,.0f}  |  Bid/Ask: {r['best_bid']:.2f}/{r['best_ask']:.2f}  |  Spread: {r['spread']:.4f}")
        print(f"       Score: {r['vol_score']:.2f}  |  Token: {r['token_ids'][0][:16] if r['token_ids'] else 'N/A'}...")

    return results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "deep":
        if len(sys.argv) < 3:
            print("Usage: python3 volatility.py deep <token_id> [interval]")
            sys.exit(1)
        interval = sys.argv[3] if len(sys.argv) > 3 else "1w"
        deep_scan(sys.argv[2], interval=interval)
    else:
        pages = int(sys.argv[1]) if len(sys.argv) > 1 else 5
        run_scan(max_pages=pages)
