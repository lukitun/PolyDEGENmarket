"""Intelligence gathering — real-world data feeds for trading signals."""
import json
import sys
import time
import httpx
import feedparser
from datetime import datetime, timezone

# ============================================================
# 1. MILITARY FLIGHT TRACKING (ADS-B)
# ============================================================

def get_military_flights_taiwan():
    """Check for military aircraft near Taiwan Strait (21-27°N, 117-123°E)."""
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
    except Exception as e:
        return {"error": str(e)}


def get_military_flights_hormuz():
    """Check for military aircraft near Strait of Hormuz (24-28°N, 54-60°E)."""
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
    except Exception as e:
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
    except:
        pass

    # Fallback: scrape summary data
    try:
        resp = httpx.get(
            "https://www.marinetraffic.org/api/v1/vessels",
            params={"area": "hormuz"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except:
        pass

    return {"status": "No free AIS API available — use aisstream.io websocket for live data"}


# ============================================================
# 3. EARTHQUAKES (USGS)
# ============================================================

def get_earthquakes(min_magnitude=5.0, days=7):
    """Get recent significant earthquakes worldwide."""
    try:
        end = datetime.now(timezone.utc)
        start = datetime(end.year, end.month, max(1, end.day - days), tzinfo=timezone.utc)
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
    except Exception as e:
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
                if published:
                    entry_time = time.mktime(published)
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
                            "age_hours": round(age_hours, 1) if published else None,
                        })
        except Exception as e:
            pass

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
    """Get current WTI crude oil price."""
    try:
        resp = httpx.get(
            "https://api.twelvedata.com/price?symbol=CL&apikey=demo",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except:
        pass

    # Fallback: scrape from a free source
    try:
        resp = httpx.get(
            "https://api.commodities-api.com/api/latest?access_key=demo&base=USD&symbols=WTI",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except:
        pass

    return {"status": "Use web search for current oil price"}


# ============================================================
# 6. GOLD PRICE
# ============================================================

def get_gold_price():
    """Get current gold price."""
    try:
        resp = httpx.get(
            "https://api.twelvedata.com/price?symbol=GC&apikey=demo",
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except:
        pass
    return {"status": "Use web search for current gold price"}


# ============================================================
# FULL INTEL REPORT
# ============================================================

def full_report():
    """Run all intelligence gathering and print a report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"INTELLIGENCE REPORT — {now}")
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
                print(f"  M{q['magnitude']} — {q['place']} ({q['time']})")
        else:
            print("  No significant earthquakes.")
    else:
        print(f"  Error: {quakes}")

    # Flight tracking
    print("\n\n[FLIGHT ACTIVITY]")
    print("-" * 40)
    taiwan = get_military_flights_taiwan()
    if isinstance(taiwan, list):
        print(f"  Taiwan Strait area: {len(taiwan)} aircraft detected")
        for ac in taiwan[:5]:
            print(f"    {ac['type'] or 'Unknown'} @ {ac['alt']}ft ({ac['lat']:.2f}, {ac['lon']:.2f})")
    else:
        print(f"  Taiwan: {taiwan}")

    hormuz = get_military_flights_hormuz()
    if isinstance(hormuz, list):
        print(f"  Hormuz area: {len(hormuz)} aircraft detected")
        for ac in hormuz[:5]:
            print(f"    {ac['type'] or 'Unknown'} @ {ac['alt']}ft ({ac['lat']:.2f}, {ac['lon']:.2f})")
    else:
        print(f"  Hormuz: {hormuz}")

    # Summary
    print("\n\n[SIGNAL SUMMARY]")
    print("-" * 40)
    china_alerts = sum(1 for a in alerts if a["category"] == "china_taiwan")
    iran_alerts = sum(1 for a in alerts if a["category"] == "iran_hormuz")
    oil_alerts = sum(1 for a in alerts if a["category"] == "oil")
    uk_alerts = sum(1 for a in alerts if a["category"] == "uk_politics")

    print(f"  China/Taiwan:  {china_alerts} news alerts" + (" ⚡ ELEVATED" if china_alerts >= 3 else ""))
    print(f"  Iran/Hormuz:   {iran_alerts} news alerts" + (" ⚡ ELEVATED" if iran_alerts >= 3 else ""))
    print(f"  Oil:           {oil_alerts} news alerts" + (" ⚡ ELEVATED" if oil_alerts >= 3 else ""))
    print(f"  UK Politics:   {uk_alerts} news alerts" + (" ⚡ ELEVATED" if uk_alerts >= 2 else ""))

    quake_count = len(quakes) if isinstance(quakes, list) else 0
    if quake_count >= 3:
        print(f"  Earthquakes:   {quake_count} significant events — CHECK POLYMARKET EARTHQUAKE MARKETS")


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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "full"
    if cmd == "full":
        full_report()
    elif cmd == "news":
        news_only()
    elif cmd == "quakes":
        quakes = get_earthquakes(min_magnitude=4.5, days=7)
        if isinstance(quakes, list):
            for q in quakes:
                print(f"M{q['magnitude']} — {q['place']} ({q['time']})")
    elif cmd == "flights":
        print("Taiwan Strait:")
        print(json.dumps(get_military_flights_taiwan(), indent=2))
        print("\nHormuz:")
        print(json.dumps(get_military_flights_hormuz(), indent=2))
    else:
        print("Usage: python3 intel.py [full|news|quakes|flights]")
