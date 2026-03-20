"""Search and browse Polymarket markets."""
import json
import sys
import httpx

GAMMA_API = "https://gamma-api.polymarket.com"


def search_markets(query, limit=10):
    """Search markets by keyword."""
    resp = httpx.get(f"{GAMMA_API}/events", params={
        "limit": limit,
        "active": True,
        "closed": False,
        "title": query,
    })
    resp.raise_for_status()
    events = resp.json()

    for event in events:
        print(f"\n{'='*60}")
        print(f"Event: {event.get('title', 'N/A')}")
        print(f"Slug:  {event.get('slug', 'N/A')}")
        for market in event.get("markets", []):
            question = market.get("question", "N/A")
            outcomes = market.get("outcomes", "N/A")
            prices = market.get("outcomePrices", "N/A")
            volume = market.get("volumeNum", 0)
            token_ids = market.get("clobTokenIds", [])
            print(f"\n  Market: {question}")
            print(f"  Outcomes: {outcomes}")
            print(f"  Prices:   {prices}")
            print(f"  Volume:   ${volume:,.2f}" if volume else "  Volume:   N/A")
            print(f"  Token IDs: {token_ids}")
    return events


def get_market(condition_id):
    """Get a specific market by condition ID."""
    resp = httpx.get(f"{GAMMA_API}/markets/{condition_id}")
    resp.raise_for_status()
    return resp.json()


def list_trending(limit=10):
    """List trending markets by volume."""
    resp = httpx.get(f"{GAMMA_API}/events", params={
        "limit": limit,
        "active": True,
        "closed": False,
        "order": "volume",
        "ascending": False,
    })
    resp.raise_for_status()
    events = resp.json()

    for event in events:
        title = event.get("title", "N/A")
        volume = event.get("volume", 0)
        print(f"\n{'='*60}")
        print(f"Event:  {title}")
        print(f"Volume: ${float(volume):,.2f}" if volume else "Volume: N/A")
        for market in event.get("markets", []):
            question = market.get("question", "")
            prices = market.get("outcomePrices", "")
            print(f"  -> {question}  |  Prices: {prices}")
    return events


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python markets.py search <query>")
        print("  python markets.py trending")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        search_markets(query)
    elif cmd == "trending":
        list_trending()
    else:
        print(f"Unknown command: {cmd}")
