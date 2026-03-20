"""Polymarket client with optional SOCKS proxy to bypass geoblock.

If SOCKS_PROXY is set in .env, all requests go through it.
Supports proxy rotation: set SOCKS_PROXY_LIST for fallback proxies.
Otherwise, connects directly (works fine in non-blocked regions like the US).
"""
import os
import sys
import time
import httpx as _httpx
from dotenv import load_dotenv

load_dotenv()

# Read proxy from .env — leave blank if you don't need one
_primary_proxy = os.getenv("SOCKS_PROXY", "").strip() or None

# Optional fallback proxies (comma-separated in .env)
# Example: SOCKS_PROXY_LIST=socks5://a:1080,socks5://b:1080
_proxy_list_str = os.getenv("SOCKS_PROXY_LIST", "").strip()
_proxy_list = [p.strip() for p in _proxy_list_str.split(",") if p.strip()] if _proxy_list_str else []

# Build ordered proxy list: primary first, then fallbacks
PROXY_LIST = []
if _primary_proxy:
    PROXY_LIST.append(_primary_proxy)
for p in _proxy_list:
    if p not in PROXY_LIST:
        PROXY_LIST.append(p)

# Track which proxy is currently active and healthy
ACTIVE_PROXY = PROXY_LIST[0] if PROXY_LIST else None
_proxy_failures = {}  # proxy_url -> (fail_count, last_fail_time)
_FAILURE_COOLDOWN = 300  # seconds before retrying a failed proxy
_MAX_FAILURES = 3  # failures before temporarily skipping a proxy


def _get_healthy_proxy():
    """Return the best available proxy, skipping recently failed ones."""
    global ACTIVE_PROXY
    if not PROXY_LIST:
        return None

    now = time.time()
    for proxy in PROXY_LIST:
        fails, last_fail = _proxy_failures.get(proxy, (0, 0))
        # If proxy has too many failures but cooldown hasn't passed, skip it
        if fails >= _MAX_FAILURES and (now - last_fail) < _FAILURE_COOLDOWN:
            continue
        # If cooldown has passed, reset the counter
        if fails >= _MAX_FAILURES and (now - last_fail) >= _FAILURE_COOLDOWN:
            _proxy_failures[proxy] = (0, 0)
        ACTIVE_PROXY = proxy
        return proxy

    # All proxies are in cooldown -- try the primary anyway as last resort
    ACTIVE_PROXY = PROXY_LIST[0]
    return PROXY_LIST[0]


def record_proxy_failure(proxy_url=None):
    """Record a failure for a proxy. Called when a request fails."""
    url = proxy_url or ACTIVE_PROXY
    if not url:
        return
    fails, _ = _proxy_failures.get(url, (0, 0))
    new_fails = fails + 1
    _proxy_failures[url] = (new_fails, time.time())
    # Alert on repeated failures
    if new_fails == _MAX_FAILURES:
        try:
            from alerts import alert as log_alert, CRITICAL
            log_alert(
                f"Proxy {url} has failed {new_fails} times, switching to fallback",
                severity=CRITICAL, source="proxy"
            )
        except ImportError:
            pass


def record_proxy_success(proxy_url=None):
    """Record a success for a proxy. Resets failure counter."""
    url = proxy_url or ACTIVE_PROXY
    if not url:
        return
    if url in _proxy_failures:
        _proxy_failures[url] = (0, 0)


def check_proxy_health(proxy_url=None, timeout=10):
    """Test if a proxy is working by hitting a simple endpoint.
    Returns (ok, latency_ms) tuple.
    """
    url = proxy_url or ACTIVE_PROXY
    if not url:
        return True, 0  # No proxy needed

    start = time.time()
    try:
        client = _httpx.Client(proxy=url, timeout=timeout)
        resp = client.get("https://clob.polymarket.com/time")
        client.close()
        latency = (time.time() - start) * 1000
        if resp.status_code == 200:
            record_proxy_success(url)
            return True, latency
        else:
            record_proxy_failure(url)
            return False, latency
    except Exception:
        record_proxy_failure(url)
        latency = (time.time() - start) * 1000
        return False, latency


def get_proxy_status():
    """Return status of all configured proxies."""
    if not PROXY_LIST:
        return {"proxies": [], "active": None, "mode": "direct"}

    status = {
        "proxies": [],
        "active": ACTIVE_PROXY,
        "mode": "proxy",
    }
    for proxy in PROXY_LIST:
        fails, last_fail = _proxy_failures.get(proxy, (0, 0))
        status["proxies"].append({
            "url": proxy,
            "failures": fails,
            "last_failure": last_fail,
            "is_active": proxy == ACTIVE_PROXY,
        })
    return status


# Monkey-patch httpx to use SOCKS proxy if configured
if PROXY_LIST:
    _original_client = _httpx.Client

    class ProxiedClient(_httpx.Client):
        def __init__(self, *args, **kwargs):
            if 'proxy' not in kwargs:
                kwargs['proxy'] = _get_healthy_proxy()
            super().__init__(*args, **kwargs)

    _httpx.Client = ProxiedClient

# Now import the CLOB client (it will use patched httpx if proxy is set)
from py_clob_client.client import ClobClient

# Also replace the already-instantiated _http_client inside py_clob_client
# (it was created at module load before our monkey-patch)
if PROXY_LIST:
    from py_clob_client.http_helpers import helpers as _helpers
    _helpers._http_client.close()
    _helpers._http_client = ProxiedClient()
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.clob_types import OrderArgs, CreateOrderOptions

HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
FUNDER = os.getenv("FUNDER", "")


def _check_key():
    if not KEY or KEY == "0xYOUR_PRIVATE_KEY_HERE" or len(KEY) < 10:
        print("ERROR: No valid private key configured.")
        print("  Set POLYMARKET_PRIVATE_KEY in your .env file.")
        sys.exit(1)


def get_client(with_auth=True):
    """Get a ClobClient instance. Uses proxy if configured."""
    _check_key()
    client = ClobClient(
        HOST,
        key=KEY,
        chain_id=CHAIN_ID,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER,
    )
    if with_auth:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    return client


def _execute_order_with_retry(token_id, price, size, side, tick_size="0.01", neg_risk=False, max_retries=2):
    """Execute an order with proxy failover. Tries next proxy on connection failures."""
    label = "BUY" if side == BUY else "SELL"
    last_error = None

    for attempt in range(max_retries + 1):
        proxy_used = ACTIVE_PROXY or "direct"
        try:
            client = get_client()
            resp = client.create_and_post_order(
                OrderArgs(token_id=token_id, price=price, size=size, side=side),
                CreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            )
            record_proxy_success()
            print(f"{label}: {resp}")
            return resp
        except (ConnectionError, OSError, TimeoutError) as e:
            # Connection-level failure -- likely proxy is dead
            record_proxy_failure()
            last_error = e
            if attempt < max_retries and len(PROXY_LIST) > 1:
                new_proxy = _get_healthy_proxy()
                print(f"  Proxy {proxy_used} failed ({e}), switching to {new_proxy} (attempt {attempt + 2}/{max_retries + 1})")
                # Re-patch the CLOB helpers with new proxy
                if PROXY_LIST:
                    _helpers._http_client.close()
                    _helpers._http_client = ProxiedClient()
                continue
            raise
        except Exception as e:
            # Non-connection error (API rejection, auth error, etc.) -- don't retry
            print(f"{label} ERROR: {e}")
            raise

    raise last_error


def buy(token_id, price, size, tick_size="0.01", neg_risk=False):
    """Buy shares through proxy (with failover)."""
    return _execute_order_with_retry(token_id, price, size, BUY, tick_size=tick_size, neg_risk=neg_risk)


def sell(token_id, price, size, tick_size="0.01", neg_risk=False):
    """Sell shares through proxy (with failover)."""
    return _execute_order_with_retry(token_id, price, size, SELL, tick_size=tick_size, neg_risk=neg_risk)


def scan_free_proxies(max_test=30, timeout=8):
    """Fetch free SOCKS5 proxy lists and test them against Polymarket.

    Sources:
    - GitHub free-proxy-list repos (TheSpeedX, hookzof, etc.)
    - Public SOCKS5 proxy aggregators

    Returns list of (proxy_url, latency_ms) tuples sorted by latency, only those
    that successfully reach Polymarket's CLOB API.
    """
    sources = [
        # TheSpeedX/PROXY-List (one of the most maintained)
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        # hookzof/socks5_list
        "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
        # monosans/proxy-list
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    ]

    raw_proxies = set()
    for url in sources:
        try:
            resp = _original_client(timeout=10).get(url) if PROXY_LIST else _httpx.Client(timeout=10).get(url)
            if resp.status_code == 200:
                for line in resp.text.strip().split("\n"):
                    line = line.strip()
                    if line and ":" in line and not line.startswith("#"):
                        raw_proxies.add(f"socks5://{line}")
        except Exception as e:
            print(f"  Could not fetch {url.split('/')[-1]}: {e}")

    if not raw_proxies:
        print("  No proxy lists available.")
        return []

    print(f"  Found {len(raw_proxies)} SOCKS5 proxies from {len(sources)} sources.")
    print(f"  Testing top {min(max_test, len(raw_proxies))} against Polymarket CLOB API...\n")

    # Test proxies (limit to max_test to avoid taking forever)
    test_list = list(raw_proxies)[:max_test]
    working = []

    for proxy_url in test_list:
        try:
            start = time.time()
            # Use the raw httpx client (not our monkey-patched one)
            client_cls = _original_client if PROXY_LIST else _httpx.Client
            client = client_cls(proxy=proxy_url, timeout=timeout)
            resp = client.get("https://clob.polymarket.com/time")
            client.close()
            latency = (time.time() - start) * 1000
            if resp.status_code == 200:
                working.append((proxy_url, latency))
                print(f"  OK: {proxy_url} ({latency:.0f}ms)")
        except Exception:
            pass  # Skip failed proxies silently

    # Sort by latency
    working.sort(key=lambda x: x[1])
    return working


if __name__ == "__main__":
    """CLI for proxy management and health checks."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        status = get_proxy_status()
        print(f"Mode: {status['mode']}")
        print(f"Active: {status['active'] or 'none (direct)'}")
        if status["proxies"]:
            print(f"\nConfigured proxies ({len(status['proxies'])}):")
            for p in status["proxies"]:
                active = " [ACTIVE]" if p["is_active"] else ""
                fails = f" (failures: {p['failures']})" if p["failures"] > 0 else ""
                print(f"  {p['url']}{active}{fails}")
        else:
            print("No proxies configured. Set SOCKS_PROXY in .env")

    elif cmd == "check":
        print("Checking proxy health...\n")
        if not PROXY_LIST:
            print("No proxies configured. Running in direct mode.")
        else:
            for proxy in PROXY_LIST:
                ok, latency = check_proxy_health(proxy)
                status_str = f"OK ({latency:.0f}ms)" if ok else f"FAILED ({latency:.0f}ms)"
                active = " [ACTIVE]" if proxy == ACTIVE_PROXY else ""
                print(f"  {proxy}: {status_str}{active}")
            print(f"\nBest proxy: {_get_healthy_proxy() or 'none'}")

    elif cmd == "test":
        print("Testing CLOB API through proxy...\n")
        try:
            client = get_client(with_auth=False)
            # Just get server time
            import httpx
            resp = httpx.get(f"{HOST}/time", timeout=15)
            print(f"  CLOB /time: {resp.status_code} ({resp.text[:50]})")
            print(f"  Proxy: {ACTIVE_PROXY or 'direct'}")
            print("\nProxy is working.")
        except Exception as e:
            print(f"  FAILED: {e}")
            print(f"  Proxy: {ACTIVE_PROXY or 'direct'}")

    elif cmd == "scan":
        print("Scanning for working SOCKS5 proxies...\n")
        working = scan_free_proxies(max_test=30, timeout=8)
        if working:
            print(f"\nFound {len(working)} working proxies:")
            for url, lat in working:
                print(f"  {url}  ({lat:.0f}ms)")
            best = working[0][0]
            print(f"\nTo use the best one, add to .env:")
            print(f"  SOCKS_PROXY={best}")
            if len(working) > 1:
                fallbacks = ",".join(url for url, _ in working[1:4])
                print(f"  SOCKS_PROXY_LIST={fallbacks}")
        else:
            print("No working proxies found. Try again later or use a paid proxy.")

    else:
        print("Usage:")
        print("  python3 proxy_client.py status   # Show proxy config and status")
        print("  python3 proxy_client.py check    # Test all proxies")
        print("  python3 proxy_client.py test     # Test CLOB API through active proxy")
        print("  python3 proxy_client.py scan     # Find working free SOCKS5 proxies")
