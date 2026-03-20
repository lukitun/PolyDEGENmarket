"""Search and browse Polymarket markets."""
import json
import re
import sys
import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_BASE = "https://polymarket.com/event/"

# How many events to fetch per page when searching (API max seems ~100)
_SEARCH_PAGE_SIZE = 100
# How many pages to scan for search results
_SEARCH_MAX_PAGES = 5


def _parse_prices(market):
    """Parse outcome prices into a clean dict. Returns {outcome: price_str}."""
    outcomes_raw = market.get("outcomes", "")
    prices_raw = market.get("outcomePrices", "")
    # Both can be JSON strings or already lists
    if isinstance(outcomes_raw, str):
        try:
            outcomes = json.loads(outcomes_raw)
        except (json.JSONDecodeError, TypeError):
            outcomes = []
    else:
        outcomes = outcomes_raw or []

    if isinstance(prices_raw, str):
        try:
            prices = json.loads(prices_raw)
        except (json.JSONDecodeError, TypeError):
            prices = []
    else:
        prices = prices_raw or []

    return dict(zip(outcomes, prices))


def _parse_token_ids(market):
    """Parse clobTokenIds into a list."""
    raw = market.get("clobTokenIds", "")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    return raw or []


def _print_event(event, show_tokens=True):
    """Pretty-print an event with all its markets."""
    print(f"\n{'='*70}")
    title = event.get("title", "N/A")
    slug = event.get("slug", "N/A")
    volume = event.get("volume", 0)
    print(f"Event:  {title}")
    print(f"Slug:   {slug}")
    if volume:
        print(f"Volume: ${float(volume):,.0f}")
    print(f"URL:    {POLYMARKET_BASE}{slug}")

    markets = event.get("markets", [])
    if not markets:
        print("  (no markets)")
        return

    for market in markets:
        question = market.get("question", "N/A")
        price_map = _parse_prices(market)
        token_ids = _parse_token_ids(market)
        vol = market.get("volumeNum", 0)
        liquidity = market.get("liquidityNum", 0)

        print(f"\n  Market: {question}")

        # Format prices nicely
        if price_map:
            parts = []
            for outcome, price in price_map.items():
                try:
                    cents = float(price) * 100
                    parts.append(f"{outcome}: {cents:.1f}c")
                except (ValueError, TypeError):
                    parts.append(f"{outcome}: {price}")
            print(f"  Prices:    {' / '.join(parts)}")

        if vol:
            print(f"  Volume:    ${vol:,.0f}")
        if liquidity:
            print(f"  Liquidity: ${liquidity:,.0f}")

        if show_tokens and token_ids:
            outcomes_raw = market.get("outcomes", "")
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except (json.JSONDecodeError, TypeError):
                    outcomes = []
            else:
                outcomes = outcomes_raw or []
            for i, tid in enumerate(token_ids):
                label = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
                print(f"  Token[{label}]: {tid}")


def search_markets(query, limit=10):
    """Search markets by keyword (client-side filtering since API has no text search).

    Fetches events sorted by volume and filters by query string match
    in event title or market question. Returns up to `limit` matching events.
    """
    if not query:
        print("Error: search query required")
        print("Usage: python markets.py search <query>")
        return []

    query_lower = query.lower()
    # Split query into words for multi-word matching
    query_words = query_lower.split()
    matched = []

    print(f"Searching for '{query}' across active markets...")

    for page in range(_SEARCH_MAX_PAGES):
        try:
            resp = httpx.get(f"{GAMMA_API}/events", params={
                "limit": _SEARCH_PAGE_SIZE,
                "offset": page * _SEARCH_PAGE_SIZE,
                "active": True,
                "closed": False,
                "order": "volume",
                "ascending": False,
            }, timeout=30)
            resp.raise_for_status()
            events = resp.json()
        except httpx.HTTPError as e:
            print(f"  (API error on page {page + 1}: {e})")
            break

        if not events:
            break

        for event in events:
            title = event.get("title", "").lower()
            # Also check market questions within the event
            market_text = " ".join(
                m.get("question", "").lower()
                for m in event.get("markets", [])
            )
            searchable = f"{title} {market_text}"

            # All query words must appear somewhere in the event
            if all(w in searchable for w in query_words):
                matched.append(event)
                if len(matched) >= limit:
                    break

        if len(matched) >= limit:
            break

    if not matched:
        print(f"No markets found matching '{query}'.")
        print("Try a shorter or different keyword.")
        return []

    print(f"Found {len(matched)} matching event(s):\n")
    for event in matched:
        _print_event(event, show_tokens=True)

    return matched


def get_event_by_slug(slug):
    """Fetch a specific event by its URL slug."""
    try:
        resp = httpx.get(f"{GAMMA_API}/events", params={
            "slug": slug,
        }, timeout=30)
        resp.raise_for_status()
        events = resp.json()
    except httpx.HTTPError as e:
        print(f"Error fetching event: {e}")
        return None

    if not events:
        print(f"No event found with slug '{slug}'")
        return None

    event = events[0]
    _print_event(event, show_tokens=True)
    return event


def get_event_by_url(url):
    """Extract slug from a Polymarket URL and fetch the event."""
    # Handle URLs like:
    #   https://polymarket.com/event/iran-military-action-against-israel-on
    #   https://polymarket.com/event/iran-military-action-against-israel-on#RZcYFAM
    #   polymarket.com/event/some-slug
    match = re.search(r'polymarket\.com/event/([a-zA-Z0-9_-]+)', url)
    if not match:
        print(f"Error: could not extract event slug from URL")
        print(f"Expected format: https://polymarket.com/event/<slug>")
        return None

    slug = match.group(1)
    print(f"Extracted slug: {slug}")
    return get_event_by_slug(slug)


def get_market(condition_id):
    """Get a specific market by condition ID."""
    try:
        resp = httpx.get(f"{GAMMA_API}/markets/{condition_id}", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        print(f"Error fetching market {condition_id}: {e}")
        return None


def list_trending(limit=15):
    """List trending markets by volume with full details."""
    try:
        resp = httpx.get(f"{GAMMA_API}/events", params={
            "limit": limit,
            "active": True,
            "closed": False,
            "order": "volume",
            "ascending": False,
        }, timeout=30)
        resp.raise_for_status()
        events = resp.json()
    except httpx.HTTPError as e:
        print(f"Error fetching trending markets: {e}")
        return []

    print(f"Top {len(events)} trending events by volume:")

    for i, event in enumerate(events, 1):
        title = event.get("title", "N/A")
        slug = event.get("slug", "N/A")
        volume = event.get("volume", 0)

        print(f"\n{'='*70}")
        print(f"#{i}  {title}")
        if volume:
            print(f"     Volume: ${float(volume):,.0f}  |  Slug: {slug}")
        else:
            print(f"     Slug: {slug}")

        markets = event.get("markets", [])
        for market in markets:
            question = market.get("question", "")
            price_map = _parse_prices(market)
            vol = market.get("volumeNum", 0)
            token_ids = _parse_token_ids(market)

            # Build compact price string
            price_str = ""
            if price_map:
                parts = []
                for outcome, price in price_map.items():
                    try:
                        cents = float(price) * 100
                        parts.append(f"{outcome}:{cents:.0f}c")
                    except (ValueError, TypeError):
                        parts.append(f"{outcome}:{price}")
                price_str = "  ".join(parts)

            vol_str = f"${vol:,.0f}" if vol else "N/A"
            print(f"     {question}")
            print(f"       {price_str}  |  Vol: {vol_str}")

            # Show token IDs inline
            if token_ids:
                outcomes_raw = market.get("outcomes", "")
                if isinstance(outcomes_raw, str):
                    try:
                        outcomes = json.loads(outcomes_raw)
                    except (json.JSONDecodeError, TypeError):
                        outcomes = []
                else:
                    outcomes = outcomes_raw or []
                for j, tid in enumerate(token_ids):
                    label = outcomes[j] if j < len(outcomes) else f"outcome_{j}"
                    print(f"       Token[{label}]: {tid}")

    return events


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python markets.py search <query>    Search markets by keyword")
        print("  python markets.py trending          Top markets by volume")
        print("  python markets.py event <slug>      Lookup event by slug")
        print("  python markets.py url <url>         Lookup event by Polymarket URL")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        search_markets(query)
    elif cmd == "trending":
        list_trending()
    elif cmd == "event":
        if len(sys.argv) < 3:
            print("Usage: python markets.py event <slug>")
            sys.exit(1)
        get_event_by_slug(sys.argv[2])
    elif cmd == "url":
        if len(sys.argv) < 3:
            print("Usage: python markets.py url <polymarket_url>")
            sys.exit(1)
        get_event_by_url(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        print("Run 'python markets.py' for usage.")
