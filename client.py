"""Shared Polymarket client setup."""
import os
import sys
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
FUNDER = os.getenv("FUNDER", "")


def _check_key():
    """Verify the private key is configured."""
    if not KEY or KEY == "0xYOUR_PRIVATE_KEY_HERE" or len(KEY) < 10:
        print("ERROR: No valid private key configured.")
        print("  1. Copy .env.example to .env")
        print("  2. Set POLYMARKET_PRIVATE_KEY to your Polygon wallet private key")
        print("  See README.md for wallet setup instructions.")
        sys.exit(1)


def get_client(with_auth=False):
    """Get a ClobClient instance. Set with_auth=True for trading."""
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
