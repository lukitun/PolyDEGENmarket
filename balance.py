"""Check on-chain wallet balances on Polygon."""
import os
import sys
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3

load_dotenv()

# Polygon RPC (public)
RPC = "https://polygon-bor-rpc.publicnode.com"
w3 = Web3(Web3.HTTPProvider(RPC))

# USDC.e on Polygon (what Polymarket uses)
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# USDC native on Polygon
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
     "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
]


def get_address():
    key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if not key or key == "0xYOUR_PRIVATE_KEY_HERE" or len(key) < 10:
        print("ERROR: No valid private key configured.")
        print("  Set POLYMARKET_PRIVATE_KEY in your .env file.")
        sys.exit(1)
    return Account.from_key(key).address


def check_balances():
    address = get_address()
    print(f"Wallet: {address}\n")

    # POL (native token, formerly MATIC)
    pol_balance = w3.eth.get_balance(address)
    pol_human = w3.from_wei(pol_balance, 'ether')
    print(f"  POL (native):  {pol_human:.6f}")

    # USDC.e (bridged — Polymarket uses this)
    usdc_e = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    usdc_e_bal = usdc_e.functions.balanceOf(Web3.to_checksum_address(address)).call()
    usdc_e_human = usdc_e_bal / 10**6
    print(f"  USDC.e:        ${usdc_e_human:.6f}")

    # USDC native
    usdc_n = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=ERC20_ABI)
    usdc_n_bal = usdc_n.functions.balanceOf(Web3.to_checksum_address(address)).call()
    usdc_n_human = usdc_n_bal / 10**6
    print(f"  USDC (native): ${usdc_n_human:.6f}")

    total_usdc = usdc_e_human + usdc_n_human
    print(f"\n  Total USDC:    ${total_usdc:.6f}")
    print(f"  Total Value:   ~${total_usdc:.2f} + {pol_human:.4f} POL")

    return {
        "address": address,
        "pol": float(pol_human),
        "usdc_e": usdc_e_human,
        "usdc_native": usdc_n_human,
        "total_usdc": total_usdc,
    }


if __name__ == "__main__":
    check_balances()
