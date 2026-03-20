"""Place and manage trades on Polymarket."""
import json
import sys
import httpx
from proxy_client import get_client, buy as _buy, sell as _sell

GAMMA_API = "https://gamma-api.polymarket.com"


def buy(token_id, price, size, tick_size="0.01", neg_risk=False):
    """Buy shares of an outcome token."""
    order = _buy(token_id, price, size, tick_size=tick_size, neg_risk=neg_risk)
    return order


def sell(token_id, price, size, tick_size="0.01", neg_risk=False):
    """Sell shares of an outcome token."""
    order = _sell(token_id, price, size, tick_size=tick_size, neg_risk=neg_risk)
    return order


def get_orders():
    """List open orders."""
    client = get_client(with_auth=True)
    orders = client.get_orders()
    if not orders:
        print("No open orders.")
    else:
        for o in orders:
            print(f"  {o}")
    return orders


def cancel_all():
    """Cancel all open orders."""
    client = get_client(with_auth=True)
    result = client.cancel_all()
    print(f"Cancelled: {result}")
    return result


def get_gamma_price(token_id):
    """Get price from Gamma API as a fallback/cross-reference."""
    try:
        resp = httpx.get(f"{GAMMA_API}/markets", params={
            "clob_token_ids": token_id,
        }, timeout=15)
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            return None, None

        market = markets[0]
        question = market.get("question", "")
        prices_raw = market.get("outcomePrices", "")
        tokens_raw = market.get("clobTokenIds", "")

        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw or []

        if isinstance(tokens_raw, str):
            tokens = json.loads(tokens_raw)
        else:
            tokens = tokens_raw or []

        for i, tid in enumerate(tokens):
            if tid == token_id and i < len(prices):
                return float(prices[i]), question

        if prices:
            return float(prices[0]), question
        return None, question
    except Exception:
        return None, None


def get_price(token_id):
    """Get current price for a token from multiple sources."""
    print(f"Market: {token_id[:16]}...")

    # Source 1: CLOB order book
    book = None
    try:
        client = get_client(with_auth=False)
        book = client.get_order_book(token_id)
        bid = book.bids[0].price if book.bids else 'N/A'
        ask = book.asks[0].price if book.asks else 'N/A'
        mid = client.get_midpoint(token_id)

        # Flag phantom bids
        bid_val = float(bid) if bid != 'N/A' else 0
        bid_note = " (PHANTOM -- CLOB API bug)" if bid_val <= 0.005 and bid != 'N/A' else ""

        print(f"  [CLOB] Best Bid: {bid}{bid_note}")
        print(f"  [CLOB] Best Ask: {ask}")
        print(f"  [CLOB] Midpoint: {mid}")
    except Exception as e:
        print(f"  [CLOB] Error: {e}")

    # Source 2: Gamma API (more reliable for current price)
    gamma_price, question = get_gamma_price(token_id)
    if gamma_price is not None:
        print(f"  [GAMMA] Price:   {gamma_price:.4f}")
        if question:
            print(f"  [GAMMA] Market:  {question}")
    else:
        print(f"  [GAMMA] Price:   unavailable")

    return book


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python trade.py price <token_id>")
        print("  python trade.py buy <token_id> <price> <size>")
        print("  python trade.py sell <token_id> <price> <size>")
        print("  python trade.py orders")
        print("  python trade.py cancel-all")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "price":
        if len(sys.argv) < 3:
            print("Usage: python3 trade.py price <token_id>")
            sys.exit(1)
        get_price(sys.argv[2])
    elif cmd == "buy":
        if len(sys.argv) < 5:
            print("Usage: python3 trade.py buy <token_id> <price> <size>")
            sys.exit(1)
        buy(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))
    elif cmd == "sell":
        if len(sys.argv) < 5:
            print("Usage: python3 trade.py sell <token_id> <price> <size>")
            sys.exit(1)
        sell(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))
    elif cmd == "orders":
        get_orders()
    elif cmd == "cancel-all":
        cancel_all()
    else:
        print(f"Unknown command: {cmd}")
