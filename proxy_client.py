"""Polymarket client with optional SOCKS proxy to bypass geoblock.

If SOCKS_PROXY is set in .env, all requests go through it.
Otherwise, connects directly (works fine in non-blocked regions like the US).
"""
import os
import httpx as _httpx
from dotenv import load_dotenv

load_dotenv()

# Read proxy from .env — leave blank if you don't need one
ACTIVE_PROXY = os.getenv("SOCKS_PROXY", "").strip() or None

# Monkey-patch httpx to use SOCKS proxy if configured
if ACTIVE_PROXY:
    _original_client = _httpx.Client

    class ProxiedClient(_httpx.Client):
        def __init__(self, *args, **kwargs):
            if 'proxy' not in kwargs:
                kwargs['proxy'] = ACTIVE_PROXY
            super().__init__(*args, **kwargs)

    _httpx.Client = ProxiedClient

# Now import the CLOB client (it will use patched httpx if proxy is set)
from py_clob_client.client import ClobClient
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.clob_types import OrderArgs, CreateOrderOptions

HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
FUNDER = os.getenv("FUNDER", "")


def get_client(with_auth=True):
    """Get a ClobClient instance. Uses proxy if configured."""
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


def buy(token_id, price, size, tick_size="0.01", neg_risk=False):
    """Buy shares through proxy."""
    client = get_client()
    resp = client.create_and_post_order(
        OrderArgs(token_id=token_id, price=price, size=size, side=BUY),
        CreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
    )
    print(f"BUY: {resp}")
    return resp


def sell(token_id, price, size, tick_size="0.01", neg_risk=False):
    """Sell shares through proxy."""
    client = get_client()
    resp = client.create_and_post_order(
        OrderArgs(token_id=token_id, price=price, size=size, side=SELL),
        CreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
    )
    print(f"SELL: {resp}")
    return resp
