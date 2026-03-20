# Polymarkt — AI-Powered Polymarket Trading Desk

A Python CLI trading toolkit for [Polymarket](https://polymarket.com) prediction markets. Built to be used with Claude Code as the analysis brain — or standalone as a set of trading scripts.

**What it does:**
- Scans 9,000+ active markets for arbitrage opportunities
- Detects high-volatility markets for swing trading
- Monitors positions with automatic stop-loss and take-profit
- Gathers real-world intelligence (news, earthquakes, flight tracking) for trading signals
- Uses Kelly criterion for position sizing
- Tracks all trades with a local ledger

## Quick Start

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/polymarkt.git
cd polymarkt

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
cp ledger.example.json ledger.json
# Edit .env with your Polymarket wallet private key
```

### Wallet Setup

You need a funded Polygon wallet:

1. Create a new wallet (MetaMask, or generate with `python3 -c "from eth_account import Account; a = Account.create(); print(a.key.hex())"`)
2. Fund it with USDC on Polygon network
3. Paste the private key in `.env`

> **Note:** If Polymarket is geo-blocked in your region, set `SOCKS_PROXY` in `.env` to a SOCKS5 proxy address.

## Usage

### Initialize your bankroll
```bash
python3 ledger.py init 100        # Start with $100 USDC
python3 ledger.py status          # View portfolio
```

### Find opportunities
```bash
python3 arbitrage.py              # Scan all events for arbitrage
python3 volatility.py             # Find high-volatility markets
python3 markets.py search "oil"   # Search markets by keyword
python3 markets.py trending       # Browse by volume
```

### Trade
```bash
python3 trade.py price <token_id>              # Check price + order book
python3 trade.py buy <token_id> <price> <size>  # Place a buy order
python3 trade.py sell <token_id> <price> <size>  # Place a sell order
```

### Track positions
```bash
python3 ledger.py status                          # Portfolio overview
python3 ledger.py history                         # Trade history
python3 ledger.py max-bet                         # Max allowed bet (20% rule)
python3 ledger.py set-rules 1 0.18 0.60 0.75      # Set stop/TP on bet #1
```

### Monitor (auto stop-loss / take-profit)
```bash
python3 monitor.py check          # Check all positions once
python3 monitor.py loop 120       # Monitor every 120 seconds
```

### Intelligence
```bash
python3 intel.py full             # Full intel report (news, earthquakes, flights)
python3 intel.py news             # Quick news scan only
python3 intel.py quakes           # Recent earthquakes
```

### Other tools
```bash
python3 kelly.py 0.75 0.40        # Kelly criterion sizing (true_prob, market_price)
python3 positions.py              # On-chain wallet positions
python3 balance.py                # Wallet balances (POL, USDC)
python3 gdrive.py                 # Sync portfolio to Google Drive (optional)
```

## Strategy

The bot prioritizes opportunities in this order:

1. **Arbitrage** — risk-free profit from mispriced markets (outcome sums != 1.0, interval ordering violations, multi-outcome mispricing)
2. **Volatility trading** — buy dips, sell rips on high-swing markets
3. **Mispriced probability** — when real odds differ from market price, take the underpriced side
4. **Stale price hunting** — find markets where price hasn't caught up to real-world events

See [STRATEGY.md](STRATEGY.md) for full details.

## Risk Rules

- Never bet more than 20% of bankroll on a single position
- Always check liquidity before entering
- Every position should have a stop-loss
- Prefer exiting before resolution — trade the swings, not the outcomes

## Using with Claude Code

This project is designed to work with [Claude Code](https://claude.ai/claude-code) as an AI co-pilot. Claude analyzes markets, proposes trades with full reasoning, and you confirm before execution. The `CLAUDE.md` file contains session rules that guide Claude's behavior.

```bash
# Start a Claude Code session in the project directory
cd polymarkt
claude
```

## Project Structure

```
polymarkt/
├── trade.py           # Buy/sell orders via CLOB API
├── arbitrage.py       # Arbitrage scanner (9000+ events)
├── volatility.py      # Volatility scanner
├── monitor.py         # Auto stop-loss / take-profit loop
├── ledger.py          # Portfolio tracker + trade log
├── kelly.py           # Kelly criterion position sizing
├── markets.py         # Market search and browse
├── intel.py           # News, earthquakes, flight tracking
├── positions.py       # On-chain position checker
├── balance.py         # Wallet balance checker
├── proxy_client.py    # Optional SOCKS5 proxy for geo-blocked regions
├── client.py          # Base Polymarket CLOB client
├── gdrive.py          # Google Drive sync (optional)
├── plays/             # Trade plan documents
├── STRATEGY.md        # Trading strategy details
├── STRATEGIES.md      # Strategy evolution notes
└── CLAUDE.md          # AI session rules
```

## APIs Used

- **Polymarket CLOB API** — order placement, order books, pricing
- **Polymarket Gamma API** — market search, event metadata
- **Polymarket Data API** — on-chain positions
- **USGS Earthquake API** — seismic activity
- **RSS feeds** (Reuters, BBC, Al Jazeera, CNBC) — news monitoring
- **ADS-B Exchange** — military flight tracking (requires API key)

## Disclaimer

This is a trading tool that interacts with real prediction markets using real money. Use at your own risk. No guarantees of profit. Always do your own research before trading.
