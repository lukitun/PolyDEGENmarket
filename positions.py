"""Check wallet positions and balance."""
import sys
import httpx
from eth_account import Account
from dotenv import load_dotenv
import os

load_dotenv()

DATA_API = "https://data-api.polymarket.com"


def get_address():
    """Derive wallet address from private key."""
    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    account = Account.from_key(key)
    return account.address


def get_positions():
    """Get current open positions."""
    address = get_address()
    print(f"Wallet: {address}\n")

    resp = httpx.get(f"{DATA_API}/positions", params={"user": address})
    resp.raise_for_status()
    positions = resp.json()

    if not positions:
        print("No open positions.")
        return positions

    for p in positions:
        title = p.get("title", p.get("asset", "Unknown"))
        size = p.get("size", 0)
        value = p.get("currentValue", 0)
        pnl = p.get("pnl", 0)
        print(f"  {title}")
        print(f"    Size: {size}  |  Value: ${value}  |  PnL: ${pnl}")
    return positions


def get_portfolio_value():
    """Get total portfolio value."""
    address = get_address()
    resp = httpx.get(f"{DATA_API}/value", params={"user": address})
    resp.raise_for_status()
    data = resp.json()
    print(f"Wallet: {address}")
    print(f"Portfolio Value: {data}")
    return data


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "positions"
    if cmd == "positions":
        get_positions()
    elif cmd == "value":
        get_portfolio_value()
    else:
        print("Usage: python positions.py [positions|value]")
