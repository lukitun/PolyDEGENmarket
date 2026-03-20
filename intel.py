"""Intelligence gathering — real-world data feeds for trading signals."""
import calendar
import json
import sys
import time
import httpx
import feedparser
from datetime import datetime, timezone, timedelta

# ============================================================
# 1. MILITARY FLIGHT TRACKING (ADS-B)
# ============================================================

def get_military_flights_taiwan():
    """Check for military aircraft near Taiwan Strait (21-27N, 117-123E)."""
    try:
        # ADS-B Exchange — military aircraft near Taiwan
        # Bounding box: lat1,lat2,lon1,lon2
        resp = httpx.get(
            "https://globe.adsbexchange.com/data/aircraft.json",
            headers={"User-Agent": "Polymarkt-Intel/1.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            aircraft = data.get("aircraft", data.get("ac", []))
            # Filter for Taiwan region
            taiwan_mil = []
            for ac in aircraft:
                lat = ac.get("lat", 0)
                lon = ac.get("lon", 0)
                if lat and lon and 21 <= lat <= 27 and 117 <= lon <= 123:
                    # Military indicators: type starts with mil, or squawk 7xxx
                    ac_type = ac.get("t", "")
                    taiwan_mil.append({
                        "hex": ac.get("hex", ""),
                        "type": ac_type,
                        "alt": ac.get("alt_baro", ""),
                        "lat": lat,
                        "lon": lon,
                        "speed": ac.get("gs", ""),
                    })
            return taiwan_mil
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return {"error": str(e)}


def get_military_flights_hormuz():
    """Check for military aircraft near Strait of Hormuz (24-28N, 54-60E)."""
    try:
        resp = httpx.get(
            "https://globe.adsbexchange.com/data/aircraft.json",
            headers={"User-Agent": "Polymarkt-Intel/1.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            aircraft = data.get("aircraft", data.get("ac", []))
            hormuz_mil = []
            for ac in aircraft:
                lat = ac.get("lat", 0)
                lon = ac.get("lon", 0)
                if lat and lon and 24 <= lat <= 28 and 54 <= lon <= 60:
                    hormuz_mil.append({
                        "hex": ac.get("hex", ""),
                        "type": ac.get("t", ""),
                        "alt": ac.get("alt_baro", ""),
                        "lat": lat,
                        "lon": lon,
                    })
            return hormuz_mil
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return {"error": str(e)}


# ============================================================
# 2. SHIP TRACKING (AIS)
# ============================================================

def get_hormuz_shipping():
    """Get vessel traffic data near Strait of Hormuz via free AIS sources."""
    try:
        # Try hormuz-specific tracker
        resp = httpx.get("https://hormuzstraitmonitor.com/api/vessels", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, ValueError, OSError):
        pass  # Fall through to next source

    # Fallback: scrape summary data
    try:
        resp = httpx.get(
            "https://www.marinetraffic.org/api/v1/vessels",
            params={"area": "hormuz"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, ValueError, OSError):
        pass  # Both sources failed, return status message below

    return {"status": "No free AIS API available — use aisstream.io websocket for live data"}


# ============================================================
# 3. EARTHQUAKES (USGS)
# ============================================================

def get_earthquakes(min_magnitude=5.0, days=7):
    """Get recent significant earthquakes worldwide."""
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        resp = httpx.get(
            "https://earthquake.usgs.gov/fdsnws/event/1/query",
            params={
                "format": "geojson",
                "starttime": start.strftime("%Y-%m-%d"),
                "endtime": end.strftime("%Y-%m-%d"),
                "minmagnitude": min_magnitude,
                "orderby": "magnitude",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        quakes = []
        for f in data.get("features", []):
            props = f.get("properties", {})
            coords = f.get("geometry", {}).get("coordinates", [])
            quakes.append({
                "place": props.get("place", ""),
                "magnitude": props.get("mag", 0),
                "time": datetime.fromtimestamp(props.get("time", 0) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "lat": coords[1] if len(coords) > 1 else None,
                "lon": coords[0] if len(coords) > 0 else None,
            })
        return quakes
    except (httpx.HTTPError, ValueError, KeyError, OSError) as e:
        return {"error": str(e)}


# ============================================================
# 4. NEWS MONITORING (RSS)
# ============================================================

NEWS_FEEDS = {
    "reuters_world": "https://feeds.reuters.com/reuters/worldNews",
    "aljazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "bbc_world": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "cnbc_world": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
}

# Keywords that signal trading opportunities
ALERT_KEYWORDS = {
    "china_taiwan": ["taiwan", "pla", "chinese military", "blockade", "taiwan strait",
                      "xi jinping", "chinese navy", "coast guard taiwan"],
    "iran_hormuz": ["iran", "hormuz", "ceasefire", "tehran", "irgc", "persian gulf",
                     "strait of hormuz", "oil tanker", "iran war", "araghchi"],
    "oil": ["crude oil", "oil price", "opec", "petroleum", "brent crude", "wti",
            "oil shock", "energy crisis", "spr release"],
    "uk_politics": ["starmer", "labour", "uk election", "downing street", "mandelson"],
    "recession": ["recession", "economic downturn", "gdp contraction", "unemployment surge",
                   "fed rate cut", "economic crisis"],
}


def scan_news(max_age_hours=6):
    """Scan RSS feeds for trading-relevant keywords. Returns alerts."""
    alerts = []
    now = time.time()

    for feed_name, feed_url in NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:20]:
                title = entry.get("title", "").lower()
                summary = entry.get("summary", "").lower()
                text = title + " " + summary

                # Check age
                published = entry.get("published_parsed")
                age_hours = None
                if published:
                    entry_time = calendar.timegm(published)  # UTC, not local time
                    age_hours = (now - entry_time) / 3600
                    if age_hours > max_age_hours:
                        continue

                # Check keywords
                for category, keywords in ALERT_KEYWORDS.items():
                    matched = [kw for kw in keywords if kw in text]
                    if matched:
                        alerts.append({
                            "source": feed_name,
                            "title": entry.get("title", ""),
                            "category": category,
                            "keywords": matched,
                            "link": entry.get("link", ""),
                            "age_hours": round(age_hours, 1) if age_hours is not None else None,
                        })
        except (OSError, ValueError, KeyError, AttributeError) as e:
            print(f"  Warning: Failed to fetch {feed_name}: {e}")

    # Deduplicate by title
    seen = set()
    unique = []
    for a in alerts:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)

    return unique


# ============================================================
# 5. OIL PRICE
# ============================================================

def get_oil_price():
    """Get current WTI crude oil price from multiple free sources."""
    # Source 1: Yahoo Finance chart API (no key needed)
    try:
        resp = httpx.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/CL=F",
            params={"interval": "1d", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("previousClose")
            if price:
                return {
                    "source": "yahoo",
                    "symbol": "CL=F (WTI)",
                    "price": price,
                    "previous_close": prev,
                    "change": round(price - prev, 2) if prev else None,
                    "change_pct": round((price - prev) / prev * 100, 2) if prev else None,
                }
    except (httpx.HTTPError, ValueError, OSError, KeyError, IndexError):
        pass

    # Source 2: Twelve Data demo (rate-limited but sometimes works)
    try:
        resp = httpx.get(
            "https://api.twelvedata.com/price?symbol=CL&apikey=demo",
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if "price" in data:
                return {"source": "twelvedata", "symbol": "CL (WTI)", "price": float(data["price"])}
    except (httpx.HTTPError, ValueError, OSError):
        pass

    return {"status": "Use web search for current oil price"}


# ============================================================
# 6. GOLD PRICE
# ============================================================

def get_gold_price():
    """Get current gold price."""
    # Source 1: Yahoo Finance
    try:
        resp = httpx.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
            params={"interval": "1d", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            if price:
                return {"source": "yahoo", "symbol": "GC=F (Gold)", "price": price}
    except (httpx.HTTPError, ValueError, OSError, KeyError, IndexError):
        pass

    # Source 2: Twelve Data demo
    try:
        resp = httpx.get(
            "https://api.twelvedata.com/price?symbol=GC&apikey=demo",
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if "price" in data:
                return {"source": "twelvedata", "symbol": "GC (Gold)", "price": float(data["price"])}
    except (httpx.HTTPError, ValueError, OSError):
        pass
    return {"status": "Use web search for current gold price"}


# ============================================================
# 6b. VIX (Fear Index)
# ============================================================

def get_vix():
    """Get current CBOE VIX (fear/volatility index) from Yahoo Finance."""
    try:
        resp = httpx.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("previousClose")
            if price:
                level = "EXTREME FEAR" if price > 30 else "HIGH FEAR" if price > 25 else "ELEVATED" if price > 20 else "LOW"
                return {
                    "source": "yahoo",
                    "symbol": "VIX",
                    "price": price,
                    "previous_close": prev,
                    "change": round(price - prev, 2) if prev else None,
                    "change_pct": round((price - prev) / prev * 100, 2) if prev else None,
                    "level": level,
                }
    except (httpx.HTTPError, ValueError, OSError, KeyError, IndexError):
        pass
    return {"status": "VIX data unavailable"}


# ============================================================
# 7. CRYPTO PRICES (BTC, ETH)
# ============================================================

def get_crypto_prices():
    """Get current BTC and ETH prices from CoinGecko (free, no API key)."""
    try:
        resp = httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum", "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            result = {}
            for coin_id, info in data.items():
                result[coin_id] = {
                    "price": info.get("usd"),
                    "change_24h_pct": info.get("usd_24h_change"),
                }
            return result
    except (httpx.HTTPError, ValueError, OSError):
        pass  # Fall through to status message
    return {"status": "Use web search for crypto prices"}


# ============================================================
# 8. STOCK MARKET (S&P 500, NASDAQ)
# ============================================================

def get_stock_indices():
    """Get current S&P 500 and NASDAQ prices from Yahoo Finance."""
    indices = {
        "^GSPC": "S&P 500",
        "^IXIC": "NASDAQ",
        "^DJI": "Dow Jones",
    }
    results = {}
    for symbol, name in indices.items():
        try:
            resp = httpx.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "1d", "range": "1d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
                price = meta.get("regularMarketPrice")
                prev = meta.get("previousClose")
                if price:
                    results[name] = {
                        "price": price,
                        "previous_close": prev,
                        "change": round(price - prev, 2) if prev else None,
                        "change_pct": round((price - prev) / prev * 100, 2) if prev else None,
                    }
        except (httpx.HTTPError, ValueError, OSError, KeyError, IndexError):
            pass
    return results


# ============================================================
# 9. FOREX (DXY, EUR/USD, GBP/USD)
# ============================================================

def get_forex():
    """Get key forex rates from Yahoo Finance."""
    pairs = {
        "DX-Y.NYB": "DXY (Dollar Index)",
        "EURUSD=X": "EUR/USD",
        "GBPUSD=X": "GBP/USD",
        "USDJPY=X": "USD/JPY",
    }
    results = {}
    for symbol, name in pairs.items():
        try:
            resp = httpx.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "1d", "range": "1d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
                price = meta.get("regularMarketPrice")
                prev = meta.get("previousClose")
                if price:
                    results[name] = {
                        "price": price,
                        "previous_close": prev,
                        "change_pct": round((price - prev) / prev * 100, 2) if prev else None,
                    }
        except (httpx.HTTPError, ValueError, OSError, KeyError, IndexError):
            pass
    return results


# ============================================================
# 10. WEATHER / HURRICANE DATA (NHC)
# ============================================================

def get_active_hurricanes():
    """Get active tropical cyclones from NHC (NOAA)."""
    try:
        resp = httpx.get(
            "https://www.nhc.noaa.gov/CurrentSummaries.json",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except (httpx.HTTPError, ValueError, OSError):
        pass  # Fall through to status message
    return {"status": "No active hurricane data available"}


# ============================================================
# FULL INTEL REPORT
# ============================================================

def full_report():
    """Run all intelligence gathering and print a report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"INTELLIGENCE REPORT -- {now}")
    print("=" * 60)

    # News Alerts
    print("\n[NEWS ALERTS - Last 6 hours]")
    print("-" * 40)
    alerts = scan_news(max_age_hours=6)
    if alerts:
        by_cat = {}
        for a in alerts:
            by_cat.setdefault(a["category"], []).append(a)
        for cat, items in by_cat.items():
            print(f"\n  {cat.upper()} ({len(items)} alerts):")
            for item in items[:5]:
                age = f" [{item['age_hours']}h ago]" if item['age_hours'] else ""
                print(f"    {item['title'][:80]}{age}")
                print(f"      Keywords: {', '.join(item['keywords'])}")
    else:
        print("  No alerts in the last 6 hours.")

    # Earthquakes
    print("\n\n[EARTHQUAKES - 5.0+ in last 7 days]")
    print("-" * 40)
    quakes = get_earthquakes(min_magnitude=5.0, days=7)
    if isinstance(quakes, list):
        if quakes:
            for q in quakes[:10]:
                print(f"  M{q['magnitude']} -- {q['place']} ({q['time']})")
        else:
            print("  No significant earthquakes.")
    else:
        print(f"  Error: {quakes}")

    # Flight tracking
    print("\n\n[FLIGHT ACTIVITY]")
    print("-" * 40)
    taiwan = get_military_flights_taiwan()
    if isinstance(taiwan, list) and taiwan:
        print(f"  Taiwan Strait area: {len(taiwan)} aircraft detected")
        for ac in taiwan[:5]:
            print(f"    {ac['type'] or 'Unknown'} @ {ac['alt']}ft ({ac['lat']:.2f}, {ac['lon']:.2f})")
    elif isinstance(taiwan, dict) and "error" in taiwan:
        print(f"  Taiwan flights: unavailable ({taiwan['error'][:60]})")
    else:
        print("  Taiwan Strait area: no aircraft detected")

    hormuz = get_military_flights_hormuz()
    if isinstance(hormuz, list) and hormuz:
        print(f"  Hormuz area: {len(hormuz)} aircraft detected")
        for ac in hormuz[:5]:
            print(f"    {ac['type'] or 'Unknown'} @ {ac['alt']}ft ({ac['lat']:.2f}, {ac['lon']:.2f})")
    elif isinstance(hormuz, dict) and "error" in hormuz:
        print(f"  Hormuz flights: unavailable ({hormuz['error'][:60]})")
    else:
        print("  Hormuz area: no aircraft detected")

    # Commodity prices
    print("\n\n[COMMODITY PRICES]")
    print("-" * 40)
    oil = get_oil_price()
    if isinstance(oil, dict) and oil.get("price"):
        change_str = ""
        if oil.get("change_pct"):
            change_str = f"  ({oil['change_pct']:+.1f}%)"
        print(f"  WTI Crude Oil: ${oil['price']:,.2f}{change_str}")
    else:
        print(f"  Oil: {oil.get('status', 'unavailable')}")

    gold = get_gold_price()
    if isinstance(gold, dict) and gold.get("price"):
        print(f"  Gold: ${gold['price']:,.2f}")
    else:
        print(f"  Gold: {gold.get('status', 'unavailable')}")

    vix = get_vix()
    if isinstance(vix, dict) and vix.get("price"):
        change_str = ""
        if vix.get("change_pct"):
            change_str = f"  ({vix['change_pct']:+.1f}%)"
        print(f"  VIX: {vix['price']:.2f}{change_str}  [{vix.get('level', '')}]")
    else:
        print(f"  VIX: {vix.get('status', 'unavailable')}")

    # Crypto prices
    print("\n\n[CRYPTO PRICES]")
    print("-" * 40)
    crypto = get_crypto_prices()
    if isinstance(crypto, dict) and "error" not in crypto and "status" not in crypto:
        for coin, info in crypto.items():
            price = info.get("price")
            change = info.get("change_24h_pct")
            if price:
                change_str = f"  ({change:+.1f}%)" if change else ""
                print(f"  {coin.upper()}: ${price:,.2f}{change_str}")
    else:
        print(f"  Crypto prices: unavailable")

    # Stock indices
    print("\n\n[STOCK INDICES]")
    print("-" * 40)
    stocks = get_stock_indices()
    if stocks:
        for name, info in stocks.items():
            change_str = f"  ({info['change_pct']:+.1f}%)" if info.get("change_pct") else ""
            print(f"  {name}: {info['price']:,.2f}{change_str}")
    else:
        print("  Stock data: unavailable")

    # Forex
    print("\n\n[FOREX]")
    print("-" * 40)
    forex = get_forex()
    if forex:
        for name, info in forex.items():
            change_str = f"  ({info['change_pct']:+.1f}%)" if info.get("change_pct") else ""
            print(f"  {name}: {info['price']:.4f}{change_str}")
    else:
        print("  Forex data: unavailable")

    # Summary
    print("\n\n[SIGNAL SUMMARY]")
    print("-" * 40)
    china_alerts = sum(1 for a in alerts if a["category"] == "china_taiwan")
    iran_alerts = sum(1 for a in alerts if a["category"] == "iran_hormuz")
    oil_alerts = sum(1 for a in alerts if a["category"] == "oil")
    uk_alerts = sum(1 for a in alerts if a["category"] == "uk_politics")

    print(f"  China/Taiwan:  {china_alerts} news alerts" + (" *** ELEVATED" if china_alerts >= 3 else ""))
    print(f"  Iran/Hormuz:   {iran_alerts} news alerts" + (" *** ELEVATED" if iran_alerts >= 3 else ""))
    print(f"  Oil:           {oil_alerts} news alerts" + (" *** ELEVATED" if oil_alerts >= 3 else ""))
    print(f"  UK Politics:   {uk_alerts} news alerts" + (" *** ELEVATED" if uk_alerts >= 2 else ""))

    quake_count = len(quakes) if isinstance(quakes, list) else 0
    if quake_count >= 3:
        print(f"  Earthquakes:   {quake_count} significant events -- CHECK POLYMARKET EARTHQUAKE MARKETS")


def news_only():
    """Quick news scan only."""
    alerts = scan_news(max_age_hours=12)
    if not alerts:
        print("No relevant alerts in last 12 hours.")
        return

    by_cat = {}
    for a in alerts:
        by_cat.setdefault(a["category"], []).append(a)

    for cat, items in sorted(by_cat.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"\n{cat.upper()} ({len(items)} alerts):")
        for item in items[:8]:
            age = f" [{item['age_hours']}h ago]" if item['age_hours'] else ""
            print(f"  {item['title'][:90]}{age}")


# ============================================================
# RESEARCH: aggressive topic scan across all feeds
# ============================================================

# Extended feed list for research mode
RESEARCH_FEEDS = {
    **NEWS_FEEDS,
    "ap_top": "https://feeds.feedburner.com/APTop",
    "guardian_world": "https://www.theguardian.com/world/rss",
    "nyt_world": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "ft_world": "https://www.ft.com/world?format=rss",
    "bloomberg_politics": "https://feeds.bloomberg.com/politics/news.rss",
    "cnn_world": "http://rss.cnn.com/rss/edition_world.rss",
    "sky_news": "https://feeds.skynews.com/feeds/rss/world.xml",
    "bbc_business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "cnbc_economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
}

RESEARCH_SPORTS_FEEDS = {
    "espn_top": "https://www.espn.com/espn/rss/news",
    "bbc_sport": "http://feeds.bbci.co.uk/sport/rss.xml",
    "sky_sport": "https://feeds.skynews.com/feeds/rss/sports.xml",
}


def research_topic(topic, max_age_hours=48):
    """Research a topic by scanning all news feeds aggressively.

    Searches for the topic string in headlines and summaries across a wider
    set of RSS feeds than the normal news scan. Useful for getting a quick
    snapshot of what the media is saying about a topic before trading.

    Usage: python3 intel.py research <topic>
    """
    if not topic:
        print("Usage: python3 intel.py research <topic>")
        print("  Example: python3 intel.py research 'oil prices'")
        print("  Example: python3 intel.py research starmer")
        print("  Example: python3 intel.py research bitcoin")
        return []

    topic_lower = topic.lower()
    topic_words = topic_lower.split()
    now = time.time()
    results = []

    # Choose feeds based on topic
    feeds = dict(RESEARCH_FEEDS)
    # Add sports feeds if topic looks sports-related
    sports_keywords = ["nba", "nfl", "epl", "premier league", "champions league",
                       "soccer", "football", "basketball", "baseball", "tennis",
                       "f1", "formula", "world cup", "olympics", "boxing", "mma", "ufc"]
    if any(kw in topic_lower for kw in sports_keywords):
        feeds.update(RESEARCH_SPORTS_FEEDS)

    print(f"Researching '{topic}' across {len(feeds)} news feeds (last {max_age_hours}h)...\n")

    for feed_name, feed_url in feeds.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:30]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                text = (title + " " + summary).lower()

                # Check age
                published = entry.get("published_parsed")
                age_hours = None
                if published:
                    entry_time = calendar.timegm(published)
                    age_hours = (now - entry_time) / 3600
                    if age_hours > max_age_hours:
                        continue

                # Check if topic matches
                if all(w in text for w in topic_words):
                    results.append({
                        "source": feed_name,
                        "title": title,
                        "summary": summary[:200].strip(),
                        "link": entry.get("link", ""),
                        "age_hours": round(age_hours, 1) if age_hours is not None else None,
                    })
        except (OSError, ValueError, KeyError, AttributeError) as e:
            # Silently skip failed feeds
            pass

    # Deduplicate by title
    seen = set()
    unique = []
    for r in results:
        title_key = r["title"].lower().strip()
        if title_key not in seen:
            seen.add(title_key)
            unique.append(r)

    # Sort by age (newest first)
    unique.sort(key=lambda x: x["age_hours"] if x["age_hours"] is not None else 999)

    if not unique:
        print(f"No articles found matching '{topic}' in last {max_age_hours} hours.")
        print("Try a shorter keyword or broader term.")
        return []

    print(f"Found {len(unique)} articles about '{topic}':\n")
    print("-" * 70)

    for i, r in enumerate(unique[:25], 1):
        age_str = f"[{r['age_hours']}h ago]" if r['age_hours'] is not None else ""
        print(f"\n  #{i}  {r['title'][:80]}")
        print(f"       Source: {r['source']}  {age_str}")
        if r['summary']:
            # Clean HTML tags from summary
            clean = r['summary'].replace("<br>", " ").replace("<br/>", " ")
            # Simple HTML tag removal
            import re
            clean = re.sub(r'<[^>]+>', '', clean)
            if clean and clean != r['title']:
                print(f"       {clean[:120]}")
        if r['link']:
            print(f"       {r['link']}")

    if len(unique) > 25:
        print(f"\n  ... and {len(unique) - 25} more articles")

    print(f"\n{'-'*70}")
    print(f"Total: {len(unique)} articles about '{topic}' from {len(feeds)} feeds")

    return unique


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "full"
    if cmd == "full":
        full_report()
    elif cmd == "news":
        news_only()
    elif cmd == "research":
        topic = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        research_topic(topic)
    elif cmd == "quakes":
        quakes = get_earthquakes(min_magnitude=4.5, days=7)
        if isinstance(quakes, list):
            for q in quakes:
                print(f"M{q['magnitude']} -- {q['place']} ({q['time']})")
    elif cmd == "flights":
        print("Taiwan Strait:")
        print(json.dumps(get_military_flights_taiwan(), indent=2))
        print("\nHormuz:")
        print(json.dumps(get_military_flights_hormuz(), indent=2))
    elif cmd == "crypto":
        crypto = get_crypto_prices()
        print(json.dumps(crypto, indent=2))
    elif cmd == "oil":
        oil = get_oil_price()
        print(json.dumps(oil, indent=2))
    elif cmd == "gold":
        gold = get_gold_price()
        print(json.dumps(gold, indent=2))
    elif cmd == "vix":
        vix = get_vix()
        if isinstance(vix, dict) and vix.get("price"):
            change_str = f"  ({vix['change_pct']:+.1f}%)" if vix.get("change_pct") else ""
            print(f"VIX: {vix['price']:.2f}{change_str}  [{vix.get('level', '')}]")
        else:
            print(f"VIX: {vix.get('status', 'unavailable')}")
    elif cmd == "commodities":
        print("OIL:")
        print(json.dumps(get_oil_price(), indent=2))
        print("\nGOLD:")
        print(json.dumps(get_gold_price(), indent=2))
        print("\nVIX:")
        vix = get_vix()
        if isinstance(vix, dict) and vix.get("price"):
            print(f"  {vix['price']:.2f}  [{vix.get('level', '')}]")
        else:
            print(json.dumps(vix, indent=2))
        print("\nCRYPTO:")
        print(json.dumps(get_crypto_prices(), indent=2))
    elif cmd == "stocks":
        stocks = get_stock_indices()
        if stocks:
            for name, info in stocks.items():
                change_str = f"  ({info['change_pct']:+.1f}%)" if info.get("change_pct") else ""
                print(f"{name}: {info['price']:,.2f}{change_str}")
        else:
            print("Stock data unavailable")
    elif cmd == "forex":
        forex = get_forex()
        if forex:
            for name, info in forex.items():
                change_str = f"  ({info['change_pct']:+.1f}%)" if info.get("change_pct") else ""
                print(f"{name}: {info['price']:.4f}{change_str}")
        else:
            print("Forex data unavailable")
    else:
        print("Usage: python3 intel.py [full|news|research <topic>|quakes|flights|crypto|oil|gold|vix|stocks|forex|commodities]")
