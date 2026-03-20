"""Search and browse Polymarket markets.

Commands:
    python3 markets.py search <query>       Search markets by keyword
    python3 markets.py trending             Top markets by volume
    python3 markets.py event <slug>         Lookup event by slug
    python3 markets.py url <url>            Lookup event by Polymarket URL
    python3 markets.py rules <token_id>     Show full resolution rules for a market
    python3 markets.py explore <category>   Browse markets by category
    python3 markets.py hot                  High-volume markets we don't hold
    python3 markets.py expiring [days]      Markets expiring soon, sorted by volume
"""
import json
import os
import re
import sys
import textwrap
import time
import httpx
from datetime import datetime, timezone, timedelta

GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_BASE = "https://polymarket.com/event/"

# How many events to fetch per page when searching (API max seems ~100)
_SEARCH_PAGE_SIZE = 100
# How many pages to scan for search results
_SEARCH_MAX_PAGES = 5

# Categories for explore command — maps user-friendly names to tag slugs
CATEGORIES = {
    "crypto":     "crypto",
    "politics":   "politics",
    "sports":     "sports",
    "ai":         "ai",
    "world":      "geopolitics",
    "geopolitics":"geopolitics",
    "economy":    "economy",
    "finance":    "finance",
    "fed":        "fed",
    "oil":        "commodities",
    "commodities":"commodities",
    "china":      "china",
    "elections":  "global-elections",
    "soccer":     "soccer",
    "basketball": "basketball",
    "esports":    "esports",
    "culture":    "pop-culture",
    "climate":    "climate-science",
    "science":    "climate-science",
    "bitcoin":    "bitcoin",
    "ethereum":   "ethereum",
    "epl":        "EPL",
    "nba":        "nba",
    "f1":         "formula1",
    "gaza":       "gaza",
    "breaking":   "breaking-news",
}


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


def _parse_end_date(market):
    """Parse end date from a market. Returns datetime or None."""
    end_str = market.get("endDate") or market.get("endDateIso") or market.get("end_date_iso", "")
    if not end_str:
        return None
    try:
        end_str = end_str.replace("Z", "+00:00")
        return datetime.fromisoformat(end_str)
    except (ValueError, TypeError):
        return None


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

        # End date
        end_date = _parse_end_date(market)
        if end_date:
            now = datetime.now(timezone.utc)
            days_left = (end_date - now).total_seconds() / 86400
            if days_left <= 0:
                print(f"  Ends:      PAST ({end_date.strftime('%Y-%m-%d')})")
            elif days_left <= 7:
                print(f"  Ends:      {end_date.strftime('%Y-%m-%d')} ({days_left:.1f} days)")
            else:
                print(f"  Ends:      {end_date.strftime('%Y-%m-%d')} ({days_left:.0f} days)")

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


def get_market_by_token(token_id):
    """Look up a market by its CLOB token ID. Returns market dict or None."""
    try:
        resp = httpx.get(f"{GAMMA_API}/markets", params={
            "clob_token_ids": token_id,
        }, timeout=30)
        resp.raise_for_status()
        markets = resp.json()
        if markets:
            return markets[0]
    except (httpx.HTTPError, ValueError, IndexError) as e:
        print(f"Error looking up token: {e}")
    return None


def show_rules(token_id):
    """Fetch and display FULL resolution criteria for a market by token ID.

    This is a MANDATORY pre-trade step. Shows:
    - Market question
    - Full description (resolution rules)
    - Resolution source
    - End date
    - Current prices
    - Edge cases and fine print
    """
    print(f"Looking up market for token: {token_id[:40]}...")

    market = get_market_by_token(token_id)
    if not market:
        print(f"No market found for token ID: {token_id}")
        print("Try using the full token ID (77+ chars) from markets.py search output.")
        return None

    question = market.get("question", "N/A")
    description = market.get("description", "")
    resolution_source = market.get("resolutionSource", "")
    end_date_str = market.get("endDate") or market.get("endDateIso") or ""
    condition_id = market.get("conditionId", "")

    price_map = _parse_prices(market)
    token_ids = _parse_token_ids(market)

    print(f"\n{'='*70}")
    print(f"MARKET RESOLUTION RULES")
    print(f"{'='*70}")
    print(f"\nQuestion: {question}")

    # Prices
    if price_map:
        parts = []
        for outcome, price in price_map.items():
            try:
                cents = float(price) * 100
                parts.append(f"{outcome}: {cents:.1f}c")
            except (ValueError, TypeError):
                parts.append(f"{outcome}: {price}")
        print(f"Prices:   {' / '.join(parts)}")

    # End date
    end_date = _parse_end_date(market)
    if end_date:
        now = datetime.now(timezone.utc)
        days_left = (end_date - now).total_seconds() / 86400
        print(f"Ends:     {end_date.strftime('%Y-%m-%d %H:%M UTC')} ({days_left:.1f} days)")
    elif end_date_str:
        print(f"Ends:     {end_date_str}")

    # Condition ID
    if condition_id:
        print(f"Cond. ID: {condition_id}")

    # Volume and liquidity
    vol = market.get("volumeNum", 0)
    liq = market.get("liquidityNum", 0)
    if vol:
        print(f"Volume:   ${vol:,.0f}")
    if liq:
        print(f"Liquidity: ${liq:,.0f}")

    # Resolution source
    print(f"\n--- Resolution Source ---")
    if resolution_source:
        print(f"  {resolution_source}")
    else:
        print(f"  (none specified at event level)")

    # Full description / rules
    print(f"\n--- Resolution Rules ---")
    if description:
        # Wrap long lines for readability
        for para in description.split("\n"):
            para = para.strip()
            if para:
                wrapped = textwrap.fill(para, width=70, initial_indent="  ", subsequent_indent="  ")
                print(wrapped)
            else:
                print()
    else:
        print("  WARNING: No resolution rules found for this market!")
        print("  Check the Polymarket website directly before trading.")

    # Token IDs
    if token_ids:
        outcomes_raw = market.get("outcomes", "")
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except (json.JSONDecodeError, TypeError):
                outcomes = []
        else:
            outcomes = outcomes_raw or []
        print(f"\n--- Token IDs ---")
        for i, tid in enumerate(token_ids):
            label = outcomes[i] if i < len(outcomes) else f"outcome_{i}"
            print(f"  {label}: {tid}")

    # Check for edge cases in description
    print(f"\n--- Edge Case Flags ---")
    desc_lower = description.lower()
    flags = []
    if "50-50" in desc_lower or "50/50" in desc_lower:
        flags.append("CAUTION: Market may resolve 50-50 under certain conditions")
    if "cancel" in desc_lower:
        flags.append("NOTE: Market has cancellation provisions")
    if "delay" in desc_lower:
        flags.append("NOTE: Market has delay/postponement provisions")
    if "forfeit" in desc_lower or "walkover" in desc_lower:
        flags.append("NOTE: Forfeit/walkover rules apply")
    if "uma" in desc_lower or "oracle" in desc_lower:
        flags.append("NOTE: Resolution involves UMA oracle / dispute process")
    if not description:
        flags.append("WARNING: No description found -- check website manually")

    if flags:
        for f in flags:
            print(f"  {f}")
    else:
        print(f"  No special edge cases detected in rules text.")

    print(f"\n{'='*70}")
    return market


def explore_category(category, limit=15):
    """Browse markets by category tag.

    Categories: crypto, politics, sports, ai, world, economy, finance,
    fed, oil, commodities, china, elections, soccer, basketball, esports,
    culture, climate, science, bitcoin, ethereum, epl, nba, f1, gaza, breaking
    """
    tag_slug = CATEGORIES.get(category.lower())
    if not tag_slug:
        print(f"Unknown category: '{category}'")
        print(f"\nAvailable categories:")
        for name in sorted(CATEGORIES.keys()):
            print(f"  {name}")
        return []

    print(f"Browsing '{category}' markets (tag: {tag_slug})...\n")

    try:
        resp = httpx.get(f"{GAMMA_API}/events", params={
            "limit": limit,
            "active": True,
            "closed": False,
            "order": "volume",
            "ascending": False,
            "tag_slug": tag_slug,
        }, timeout=30)
        resp.raise_for_status()
        events = resp.json()
    except httpx.HTTPError as e:
        print(f"Error fetching category: {e}")
        return []

    if not events:
        print(f"No active markets found in category '{category}'.")
        return []

    print(f"Top {len(events)} '{category}' events by volume:")

    for i, event in enumerate(events, 1):
        title = event.get("title", "N/A")
        slug = event.get("slug", "N/A")
        volume = event.get("volume", 0)

        print(f"\n{'='*70}")
        print(f"#{i}  {title}")
        vol_str = f"${float(volume):,.0f}" if volume else "N/A"
        print(f"     Volume: {vol_str}  |  Slug: {slug}")

        markets = event.get("markets", [])
        # Show up to 5 markets per event to keep output manageable
        for market in markets[:5]:
            question = market.get("question", "")
            price_map = _parse_prices(market)
            vol = market.get("volumeNum", 0)
            token_ids = _parse_token_ids(market)

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

        if len(markets) > 5:
            print(f"     ... and {len(markets) - 5} more markets")

    return events


def list_hot(limit=20):
    """List high-volume markets we DON'T already hold positions in.

    Compares trending markets against our ledger to filter out existing positions.
    Shows fresh opportunities only.
    """
    # Load our current token IDs from ledger
    held_tokens = set()
    ledger_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.json")
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path) as f:
                ledger = json.load(f)
            for bet in ledger.get("open_bets", []):
                tid = bet.get("token_id", "")
                if tid:
                    held_tokens.add(tid)
        except (json.JSONDecodeError, OSError):
            pass

    print(f"Fetching hot markets (filtering out {len(held_tokens)} held positions)...\n")

    try:
        resp = httpx.get(f"{GAMMA_API}/events", params={
            "limit": limit + 10,  # fetch extra in case some are filtered
            "active": True,
            "closed": False,
            "order": "volume",
            "ascending": False,
        }, timeout=30)
        resp.raise_for_status()
        events = resp.json()
    except httpx.HTTPError as e:
        print(f"Error fetching markets: {e}")
        return []

    # Filter out events where we hold ALL markets
    hot_events = []
    for event in events:
        markets = event.get("markets", [])
        has_unheld = False
        for market in markets:
            token_ids = _parse_token_ids(market)
            if not any(tid in held_tokens for tid in token_ids):
                has_unheld = True
                break
        if has_unheld:
            hot_events.append(event)
        if len(hot_events) >= limit:
            break

    if not hot_events:
        print("All top markets are already in your portfolio!")
        return []

    print(f"HOT MARKETS (not in portfolio) -- {len(hot_events)} events")
    print(f"{'='*70}")

    for i, event in enumerate(hot_events, 1):
        title = event.get("title", "N/A")
        volume = event.get("volume", 0)
        vol_str = f"${float(volume):,.0f}" if volume else "N/A"

        print(f"\n#{i}  {title}")
        print(f"     Volume: {vol_str}")

        markets = event.get("markets", [])
        for market in markets[:3]:
            question = market.get("question", "")
            price_map = _parse_prices(market)
            vol = market.get("volumeNum", 0)
            token_ids = _parse_token_ids(market)

            # Mark if we hold this specific market
            held_marker = ""
            if any(tid in held_tokens for tid in token_ids):
                held_marker = " [HELD]"

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

            vol_str = f"${vol:,.0f}" if vol else ""
            print(f"     {question}{held_marker}")
            print(f"       {price_str}  |  Vol: {vol_str}")

            if token_ids and not held_marker:
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

        if len(markets) > 3:
            print(f"     ... and {len(markets) - 3} more markets")

    return hot_events


def list_expiring(max_days=7, limit=30, min_volume=5000):
    """List markets expiring within N days, sorted by volume.

    Good for finding bond plays (high-price, near-resolution markets)
    and for avoiding surprise resolutions on existing positions.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=max_days)
    expiring = []

    print(f"Scanning for markets expiring within {max_days} days (min volume ${min_volume:,})...\n")

    for page in range(10):
        try:
            resp = httpx.get(f"{GAMMA_API}/events", params={
                "limit": 500,
                "offset": page * 500,
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
            for market in event.get("markets", []):
                end_date = _parse_end_date(market)
                if end_date is None:
                    continue
                if not (now < end_date <= cutoff):
                    continue

                volume = market.get("volumeNum", 0) or 0
                if volume < min_volume:
                    continue

                prices = _parse_prices(market)
                token_ids = _parse_token_ids(market)
                days_left = (end_date - now).total_seconds() / 86400

                expiring.append({
                    "event": event.get("title", ""),
                    "question": market.get("question", ""),
                    "end_date": end_date,
                    "days_left": round(days_left, 1),
                    "prices": prices,
                    "volume": volume,
                    "liquidity": market.get("liquidityNum", 0) or 0,
                    "token_ids": token_ids,
                    "slug": event.get("slug", ""),
                })

        if len(events) < 500:
            break
        time.sleep(0.2)

    # Sort by volume descending
    expiring.sort(key=lambda x: x["volume"], reverse=True)

    if not expiring:
        print(f"No markets expiring within {max_days} days with volume >= ${min_volume:,}.")
        return []

    print(f"{'='*70}")
    print(f"MARKETS EXPIRING WITHIN {max_days} DAYS ({len(expiring)} found)")
    print(f"{'='*70}")

    for i, r in enumerate(expiring[:limit], 1):
        price_str = ""
        if r["prices"]:
            parts = []
            for outcome, price in r["prices"].items():
                try:
                    cents = float(price) * 100
                    parts.append(f"{outcome}:{cents:.0f}c")
                except (ValueError, TypeError):
                    parts.append(f"{outcome}:{price}")
            price_str = "  ".join(parts)

        print(f"\n  #{i}  {r['question'][:65]}")
        print(f"       Ends: {r['end_date'].strftime('%Y-%m-%d %H:%M')} ({r['days_left']}d left)")
        print(f"       {price_str}  |  Vol: ${r['volume']:,.0f}  |  Liq: ${r['liquidity']:,.0f}")
        if r["token_ids"]:
            print(f"       Token[0]: {r['token_ids'][0][:40]}...")

    return expiring


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
        print("  python3 markets.py search <query>       Search markets by keyword")
        print("  python3 markets.py trending              Top markets by volume")
        print("  python3 markets.py event <slug>          Lookup event by slug")
        print("  python3 markets.py url <url>             Lookup event by Polymarket URL")
        print("  python3 markets.py rules <token_id>      Show resolution rules for a market")
        print("  python3 markets.py explore <category>    Browse markets by category")
        print("  python3 markets.py hot                   High-volume markets not in portfolio")
        print("  python3 markets.py expiring [days]       Markets expiring within N days")
        print()
        print("Categories for explore: " + ", ".join(sorted(CATEGORIES.keys())))
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "search":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        search_markets(query)
    elif cmd == "trending":
        list_trending()
    elif cmd == "event":
        if len(sys.argv) < 3:
            print("Usage: python3 markets.py event <slug>")
            sys.exit(1)
        get_event_by_slug(sys.argv[2])
    elif cmd == "url":
        if len(sys.argv) < 3:
            print("Usage: python3 markets.py url <polymarket_url>")
            sys.exit(1)
        get_event_by_url(sys.argv[2])
    elif cmd == "rules":
        if len(sys.argv) < 3:
            print("Usage: python3 markets.py rules <token_id>")
            print("  Get the token_id from 'markets.py search' or 'markets.py trending' output.")
            sys.exit(1)
        show_rules(sys.argv[2])
    elif cmd == "explore":
        if len(sys.argv) < 3:
            print("Usage: python3 markets.py explore <category>")
            print(f"\nCategories: {', '.join(sorted(CATEGORIES.keys()))}")
            sys.exit(1)
        explore_category(sys.argv[2])
    elif cmd == "hot":
        list_hot()
    elif cmd == "expiring":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        list_expiring(max_days=days)
    else:
        print(f"Unknown command: {cmd}")
        print("Run 'python3 markets.py' for usage.")
