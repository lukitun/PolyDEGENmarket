"""Shared Polymarket client setup."""
import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "1"))
FUNDER = os.getenv("FUNDER", "")


def get_client(with_auth=False):
    """Get a ClobClient instance. Set with_auth=True for trading."""
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
