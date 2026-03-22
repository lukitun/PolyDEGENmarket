"""Detect large price swings without obvious news catalysts.

Finds markets where price moved significantly but no breaking news explains
the move. These are potential insider trading or smart money positioning
signals -- worth investigating before the news breaks.

Usage:
    python3 swing_scanner.py              # Scan top markets for unexplained swings
    python3 swing_scanner.py deep         # Deep scan (more markets, slower)
    python3 swing_scanner.py <keyword>    # Scan specific category/keyword
"""
import json
import re
import sys
import time
import calendar
import httpx
import feedparser

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
PAGE_SIZE = 500

# Thresholds for what counts as a "big move"
MIN_DAY_CHANGE = 0.08    # 8% in 24h
MIN_WEEK_CHANGE = 0.15   # 15% in 7d
MIN_VOLUME_24H = 3000    # Minimum 24h volume to care about

# News feeds for cross-referencing (lighter set for speed)
_NEWS_FEEDS = {
    "reuters": "https://feeds.reuters.com/reuters/worldNews",
    "bbc": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "cnbc": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
    "ap": "https://feeds.feedburner.com/APTop",
    "guardian": "https://www.theguardian.com/world/rss",
    "bbc_biz": "http://feeds.bbci.co.uk/news/business/rss.xml",
}

# Cache for news articles so we only fetch once per run
_news_cache = None
_news_cache_time = 0


def _fetch_news_articles(max_age_hours=48):
    """Fetch recent news articles from RSS feeds. Caches results."""
    global _news_cache, _news_cache_time
    if _news_cache is not None and (time.time() - _news_cache_time) < 300:
        return _news_cache

    articles = []
    now = time.time()

    for feed_name, feed_url in _NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:25]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                published = entry.get("published_parsed")
                age_hours = None
                if published:
                    entry_time = calendar.timegm(published)
                    age_hours = (now - entry_time) / 3600
                    if age_hours > max_age_hours:
                        continue
                # Strip HTML from summary
                clean_summary = re.sub(r'<[^>]+>', '', summary)
                articles.append({
                    "source": feed_name,
                    "title": title,
                    "summary": clean_summary[:300],
                    "text": (title + " " + clean_summary).lower(),
                    "age_hours": round(age_hours, 1) if age_hours is not None else None,
                })
        except (OSError, ValueError, KeyError, AttributeError):
            pass

    _news_cache = articles
    _news_cache_time = time.time()
    return articles


def _extract_keywords(question):
    """Extract meaningful keywords from a market question for news matching."""
    # Remove common filler words
    stop_words = {
        "will", "the", "be", "in", "on", "at", "to", "of", "a", "an", "and",
        "or", "for", "by", "this", "that", "is", "it", "do", "does", "did",
        "has", "have", "had", "are", "was", "were", "been", "being", "before",
        "after", "above", "below", "between", "during", "with", "from", "than",
        "more", "most", "less", "least", "yes", "no", "not", "what", "when",
        "where", "who", "which", "how", "any", "all", "each", "every", "some",
        "into", "over", "under", "about", "up", "down", "out", "off",
        "again", "further", "then", "once", "there", "here", "through",
    }
    # Also remove date/number patterns
    question_clean = re.sub(r'\b\d+[.,]?\d*[%$]?\b', '', question)
    question_clean = re.sub(r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\b', '', question_clean.lower())
    question_clean = re.sub(r'\b(2024|2025|2026|2027)\b', '', question_clean)

    words = re.findall(r'[a-z]+', question_clean.lower())
    keywords = [w for w in words if w not in stop_words and len(w) > 2]
    return keywords


def _check_news_for_market(question, articles):
    """Check if any recent news articles match this market's topic.

    Returns (has_news, matching_articles) where has_news is True if we found
    relevant coverage, and matching_articles is a list of matches.
    """
    keywords = _extract_keywords(question)
    if not keywords:
        return False, []

    # We need at least 2 keywords to match to avoid false positives
    # For short questions (few keywords), require proportionally more matches
    min_matches = max(2, len(keywords) // 3)

    matches = []
    for article in articles:
        text = article["text"]
        matched_kw = [kw for kw in keywords if kw in text]
        if len(matched_kw) >= min_matches:
            matches.append({
                "title": article["title"],
                "source": article["source"],
                "age_hours": article["age_hours"],
                "matched_keywords": matched_kw,
            })

    # Sort by number of keyword matches (best match first)
    matches.sort(key=lambda x: len(x["matched_keywords"]), reverse=True)
    return len(matches) > 0, matches[:3]


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


def fetch_all_markets(max_pages=5):
    """Paginate through active markets."""
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


def scan_swings(max_pages=3, min_volume=MIN_VOLUME_24H, top_n=30, keyword=None):
    """Scan markets for large price swings and cross-reference with news.

    Returns list of swing candidates sorted by suspiciousness (no-news movers first).
    """
    print("=" * 70)
    print("SWING SCANNER -- Detecting unexplained price moves")
    print("=" * 70)

    # Step 1: Fetch markets
    print("\n[1/3] Fetching active markets...")
    all_markets = fetch_all_markets(max_pages=max_pages)
    print(f"  Total: {len(all_markets)} markets\n")

    # Step 2: Filter for big movers
    print("[2/3] Filtering for big movers...")
    movers = []
    for m in all_markets:
        volume_24h = float(m.get("volume24hr", 0) or 0)
        if volume_24h < min_volume:
            continue

        day_change = float(m.get("oneDayPriceChange", 0) or 0)
        week_change = float(m.get("oneWeekPriceChange", 0) or 0)
        question = m.get("question", "")

        # Keyword filter
        if keyword and keyword.lower() not in question.lower():
            continue

        # Parse current price
        prices_raw = m.get("outcomePrices", "")
        try:
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw
            yes_price = float(prices[0])
        except (json.JSONDecodeError, ValueError, IndexError, TypeError):
            continue

        # Skip near-resolved markets
        if yes_price > 0.95 or yes_price < 0.05:
            continue

        abs_day = abs(day_change)
        abs_week = abs(week_change)

        # Must meet at least one threshold
        is_big_day = abs_day >= MIN_DAY_CHANGE
        is_big_week = abs_week >= MIN_WEEK_CHANGE

        if not is_big_day and not is_big_week:
            continue

        token_ids = m.get("clobTokenIds", "")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except (json.JSONDecodeError, ValueError):
                token_ids = []

        volume_1w = float(m.get("volume1wk", 0) or 0)

        # Swing magnitude score: bigger moves + more volume = more interesting
        import math
        swing_score = (abs_day * 5 + abs_week * 2) * math.log10(max(volume_24h, 10))

        movers.append({
            "question": question,
            "yes_price": yes_price,
            "day_change": day_change,
            "week_change": week_change,
            "volume_24h": volume_24h,
            "volume_1w": volume_1w,
            "swing_score": swing_score,
            "token_ids": token_ids or [],
            "condition_id": m.get("conditionId", ""),
            "best_bid": float(m.get("bestBid", 0) or 0),
            "best_ask": float(m.get("bestAsk", 0) or 0),
        })

    print(f"  Found {len(movers)} markets with significant price moves\n")

    if not movers:
        print("No significant price swings detected.")
        return []

    # Step 3: Cross-reference with news
    print("[3/3] Cross-referencing with news feeds...")
    articles = _fetch_news_articles(max_age_hours=48)
    print(f"  Loaded {len(articles)} recent articles from {len(_NEWS_FEEDS)} feeds\n")

    results = []
    for mover in movers:
        has_news, news_matches = _check_news_for_market(mover["question"], articles)
        mover["has_news"] = has_news
        mover["news_matches"] = news_matches
        mover["verdict"] = "EXPLAINED" if has_news else "SUSPICIOUS"
        results.append(mover)

    # Sort: SUSPICIOUS first, then by swing score
    results.sort(key=lambda x: (x["has_news"], -x["swing_score"]))

    return results[:top_n]


def print_results(results):
    """Pretty-print swing scan results."""
    if not results:
        return

    suspicious = [r for r in results if r["verdict"] == "SUSPICIOUS"]
    explained = [r for r in results if r["verdict"] == "EXPLAINED"]

    # Print suspicious movers first (the interesting ones)
    if suspicious:
        print("=" * 70)
        print(f"SUSPICIOUS MOVERS -- No obvious news catalyst ({len(suspicious)} found)")
        print("=" * 70)
        for i, r in enumerate(suspicious, 1):
            _print_mover(i, r)

    if explained:
        print(f"\n{'=' * 70}")
        print(f"EXPLAINED MOVERS -- News catalyst found ({len(explained)} found)")
        print("=" * 70)
        for i, r in enumerate(explained, 1):
            _print_mover(i, r)

    # Summary
    print(f"\n{'=' * 70}")
    print(f"SUMMARY: {len(suspicious)} suspicious / {len(explained)} explained / {len(results)} total")
    if suspicious:
        print(f"\nTop suspicious movers to investigate:")
        for i, r in enumerate(suspicious[:5], 1):
            direction = "UP" if r["day_change"] > 0 else "DOWN"
            print(f"  {i}. {r['question'][:60]} ({direction} {abs(r['day_change'])*100:.1f}% today)")
    print("=" * 70)


def _print_mover(idx, r):
    """Print a single mover entry."""
    direction_day = "+" if r["day_change"] >= 0 else ""
    direction_week = "+" if r["week_change"] >= 0 else ""

    print(f"\n  #{idx}  {r['question'][:70]}")
    print(f"       Price: {r['yes_price']:.2f}  |  24h: {direction_day}{r['day_change']*100:.1f}%  |  7d: {direction_week}{r['week_change']*100:.1f}%")
    print(f"       Vol24h: ${r['volume_24h']:,.0f}  |  Bid/Ask: {r['best_bid']:.2f}/{r['best_ask']:.2f}")

    if r["token_ids"]:
        print(f"       Token: {r['token_ids'][0][:50]}...")

    verdict_label = r["verdict"]
    if r["has_news"]:
        print(f"       News: YES  |  Verdict: {verdict_label}")
        for nm in r["news_matches"][:2]:
            age_str = f"[{nm['age_hours']}h ago]" if nm["age_hours"] is not None else ""
            print(f"         -> {nm['title'][:65]} {age_str}")
    else:
        print(f"       News: NONE FOUND  |  Verdict: *** {verdict_label} ***")


def scan_quick():
    """Quick scan: fewer pages, top movers only."""
    results = scan_swings(max_pages=3, top_n=20)
    print_results(results)
    return results


def scan_deep():
    """Deep scan: more pages, lower thresholds."""
    global MIN_DAY_CHANGE, MIN_WEEK_CHANGE, MIN_VOLUME_24H
    # Temporarily lower thresholds for deep scan
    old_day, old_week, old_vol = MIN_DAY_CHANGE, MIN_WEEK_CHANGE, MIN_VOLUME_24H
    MIN_DAY_CHANGE = 0.05   # 5% daily
    MIN_WEEK_CHANGE = 0.10  # 10% weekly
    MIN_VOLUME_24H = 1000

    results = scan_swings(max_pages=8, top_n=40)
    print_results(results)

    # Restore
    MIN_DAY_CHANGE, MIN_WEEK_CHANGE, MIN_VOLUME_24H = old_day, old_week, old_vol
    return results


def scan_keyword(keyword):
    """Scan markets matching a keyword for unexplained swings."""
    results = scan_swings(max_pages=5, top_n=30, keyword=keyword)
    print_results(results)
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        scan_quick()
    elif sys.argv[1] == "deep":
        scan_deep()
    elif sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
    else:
        keyword = " ".join(sys.argv[1:])
        scan_keyword(keyword)
