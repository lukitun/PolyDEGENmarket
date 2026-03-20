# Polymarkt Trading Desk -- Claude Session Rules

## Mission
Maximize profits on Polymarket through active trading. Claude Code is the brain -- analyzing markets, assessing probabilities, and making trade recommendations.

## Agents

Four specialized agents handle different aspects of the operation:

### `@trader` -- Trading Agent
Finds profitable opportunities, monitors positions, recommends trades.
- Runs steps 1-8 of the session workflow
- Analyzes intel, checks prices, evaluates positions, hunts for edges
- Presents trade recommendations with full reasoning
- NEVER executes trades without user confirmation
- Defined in `.claude/agents/trader.md`

### `@dev` -- Lead Developer Agent
Builds new features, improves tools, fixes bugs, and hardens the codebase.
- Fixes bugs that lose money or leak secrets (always first priority)
- Builds new intel sources (bookmaker odds, sentiment, whale tracking, etc.)
- Improves scanners, trade execution, monitoring
- Checks STRATEGIES.md "Immediate Action Items" for the build queue
- Updates `improve.py` with new automated checks
- Defined in `.claude/agents/dev.md`

### `@researcher` -- Deep Research Agent
Investigates topics, events, and markets to produce actionable intelligence reports.
- Web searches from multiple angles (news, experts, bookmakers, contrarian views)
- Cross-references Polymarket prices against bookmaker odds and forecaster estimates
- Produces structured research reports with probability assessments and edge analysis
- Read-only agent -- researches and recommends, never trades or writes code
- Uses sonnet model for fast, thorough research
- Defined in `.claude/agents/researcher.md`

### `@supervisor` -- Strategy & Planning Agent
Oversees the entire operation. Reviews performance, plans ahead, coordinates priorities.
- Assesses portfolio health, concentration risk, and performance trends
- Reviews whether existing positions still have valid theses
- Plans upcoming catalysts and opportunity pipeline
- Prioritizes trader directives and dev backlog
- Catches portfolio drift, strategy decay, and missed connections
- Defined in `.claude/agents/supervisor.md`

**Typical session:** spawn `@supervisor` first (it plans and prioritizes), then spawn `@trader` (it executes the plan), use `@researcher` for deep dives on specific markets before trading, then `@dev` in the background (it builds what the supervisor prioritized).

## Risk Rules (MANDATORY -- NEVER BREAK THESE)
1. **Never bet more than 20% of total funds on a single position**
   - Check `python3 ledger.py max-bet` before every trade
   - If a trade would exceed 20%, reduce size or skip it
2. **Always check liquidity before entering** -- no point buying if you can't sell
3. **Prefer exiting positions before resolution** -- we trade the swings, not the outcomes
4. **Cut losses fast** -- if thesis breaks, exit immediately
5. **Record every trade in the ledger** -- no untracked positions
6. **Every play must have a written plan** in `plays/` before execution -- including:
   - Thesis (why this is a good bet)
   - Key events to monitor
   - Exit strategy (profit targets + stop losses in a table)
   - Risk assessment (max loss, max gain, EV calculation)
   - Hard stop loss level

## Strategy Priority (see STRATEGY.md for details)
1. **Arbitrage** -- risk-free profit first (intra-event interval mispricing, outcome sums != 1.0, cross-market)
2. **Volatility trading** -- find high-swing markets, buy dips, sell rips
3. **Mispriced probability** -- when real odds differ significantly from market price, take the underpriced side

## Session Workflow
Every session should follow this order:
1. Run `python3 ledger.py status` -- know our current position and funds
2. Run `python3 intel.py full` -- check real-world intelligence (news, earthquakes, flights, crypto)
3. Run `python3 monitor.py check` -- check live prices, stops, TPs
4. Review open bets -- decide hold/sell based on intel + prices
5. Run `python3 intel.py news` -- deep scan for stale price opportunities (Strategy #0)
6. Run scanners (`python3 arbitrage.py`, `python3 volatility.py`) if looking for new plays
7. Research top candidates -- spawn `@researcher` for deep dives on promising markets
8. BEFORE any trade: run `python3 markets.py rules <token_id>` to read resolution rules
9. Execute trades if edge is found (only after research confirms the edge)
9. Run `python3 equity.py snapshot` -- record portfolio value
10. Run `@dev` agent -- audits codebase, fixes bugs, improves reliability
11. Run `python3 gdrive.py` -- sync everything to Google Drive

## Tools Available
```
python3 ledger.py status          # Portfolio overview (includes on-chain balance check)
python3 ledger.py sync            # Sync ledger with on-chain USDC balance
python3 ledger.py history         # Trade history
python3 ledger.py max-bet         # Max allowed bet
python3 arbitrage.py [pages]      # Arbitrage scanner (default: all ~9000 events)
python3 volatility.py [pages]     # Volatility scanner
python3 volatility.py deep <id>   # Deep vol analysis on a token
python3 markets.py search <q>     # Search markets by keyword (scans top events)
python3 markets.py trending       # Top markets by volume with prices + token IDs
python3 markets.py event <slug>   # Lookup event by slug (from URL)
python3 markets.py url <url>      # Lookup event by full Polymarket URL
python3 markets.py rules <token>  # MANDATORY: show full resolution rules before trading
python3 markets.py explore <cat>  # Browse by category (crypto, politics, sports, ai, etc.)
python3 markets.py hot            # High-volume markets not in portfolio
python3 markets.py expiring [days] # Markets expiring within N days (bond play hunting)
python3 trade.py price <token>    # Check price
python3 trade.py buy <token> <price> <size>
python3 trade.py sell <token> <price> <size>
python3 positions.py              # On-chain positions
python3 positions.py balance      # On-chain USDC balance
python3 intel.py full             # Full intel report (news, quakes, flights, crypto, oil, gold, VIX)
python3 intel.py crypto           # BTC/ETH prices
python3 intel.py oil              # WTI crude oil price
python3 intel.py gold             # Gold price
python3 intel.py vix              # CBOE VIX fear index
python3 intel.py commodities      # All commodity + crypto + VIX prices
python3 intel.py research <topic> # Aggressive news search across 13+ feeds
python3 news_monitor.py           # Breaking news keyword alerts
python3 news_monitor.py loop [m]  # Continuous news monitor (default: 15 min)
python3 news_monitor.py history   # Recent alert history
python3 monitor.py check          # Check positions against rules
python3 monitor.py liquidity <token> <size>  # Check exit liquidity
python3 watchlist.py              # View watchlist with prices
python3 watchlist.py add <token> <name> [entry_below] [entry_above]
python3 watchlist.py snapshot     # Record watchlist prices
python3 watchlist.py alerts       # Show entry zone alerts
python3 equity.py                 # Equity curve summary
python3 equity.py snapshot        # Record portfolio value (book value)
python3 equity.py live            # Record with live market prices
python3 equity.py chart           # ASCII equity chart
python3 kelly.py <prob> <price>   # Kelly criterion sizing
python3 execute.py buy <token> <price> <size> <name> <side> [--stop X --tp1 X --tp2 X]
python3 execute.py sell <bet_id> <price> [size]  # Unified sell (order + ledger)
python3 execute.py adjust <bet_id> <actual_size> # Fix partial fill (reopen unfilled shares)
python3 execute.py dry-buy ...    # Dry run (no order placed, just checks)
python3 execute.py resolve <bet_id> <won|lost>   # Record resolution
python3 resolution.py             # Bond play candidates (90-98c markets)
python3 resolution.py expiring [days]  # Markets expiring within N days
python3 resolution.py check       # Check our positions for upcoming resolutions
python3 improve.py                # Codebase audit (security, integrity, code quality)
python3 improve.py quick          # Quick safety check only
python3 improve.py reconcile      # Ledger vs on-chain check
python3 proxy_client.py status    # Show proxy config and active proxy
python3 proxy_client.py check     # Test all proxies (health + latency)
python3 proxy_client.py test      # Test CLOB API through active proxy
python3 proxy_client.py scan      # Find working free SOCKS5 proxies
python3 alerts.py                 # Show recent alerts (stop losses, trades, errors)
python3 alerts.py tail [N]        # Show last N alerts
python3 alerts.py clear           # Clear alert log
python3 intel.py stocks           # S&P 500, NASDAQ, Dow Jones
python3 intel.py forex            # DXY, EUR/USD, GBP/USD, USD/JPY
```

## Strategy Evolution
- Claude should actively suggest new strategies when patterns or opportunities are discovered
- If a new approach shows promise (e.g., event-driven plays, sentiment analysis, correlation trades), propose it with reasoning
- Track which strategies are working and which aren't -- adapt over time
- Think outside the box -- creative edges win in prediction markets

## Key Reminders
- Wallet PK is in .env -- NEVER display it
- User prefers active short-term trading over holding to resolution
- Always present trade ideas with clear reasoning before executing
- Get user confirmation before placing any trade
