"""Place and manage trades on Polymarket."""
import sys
from client import get_client
from py_clob_client.order_builder.constants import BUY, SELL


def buy(token_id, price, size, tick_size="0.01", neg_risk=False):
    """Buy shares of an outcome token."""
    client = get_client(with_auth=True)
    order = client.create_and_post_order({
        "token_id": token_id,
        "price": price,
        "size": size,
        "side": BUY,
    }, options={"tick_size": tick_size, "neg_risk": neg_risk})
    print(f"Order placed: {order}")
    return order


def sell(token_id, price, size, tick_size="0.01", neg_risk=False):
    """Sell shares of an outcome token."""
    client = get_client(with_auth=True)
    order = client.create_and_post_order({
        "token_id": token_id,
        "price": price,
        "size": size,
        "side": SELL,
    }, options={"tick_size": tick_size, "neg_risk": neg_risk})
    print(f"Order placed: {order}")
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


def get_price(token_id):
    """Get current price for a token."""
    client = get_client()
    book = client.get_order_book(token_id)
    print(f"Market: {token_id[:16]}...")
    print(f"  Best Bid: {book.bids[0].price if book.bids else 'N/A'}")
    print(f"  Best Ask: {book.asks[0].price if book.asks else 'N/A'}")
    mid = client.get_midpoint(token_id)
    print(f"  Midpoint: {mid}")
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
        get_price(sys.argv[2])
    elif cmd == "buy":
        buy(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))
    elif cmd == "sell":
        sell(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))
    elif cmd == "orders":
        get_orders()
    elif cmd == "cancel-all":
        cancel_all()
    else:
        print(f"Unknown command: {cmd}")
