# Polymarkt Trading Strategy

## Priority Order

### 1. Arbitrage (Highest Priority)
Find risk-free or near-risk-free profit opportunities:
- **Intra-event interval arbitrage (PRIORITY)**: Within the same Polymarket event, there are often multiple markets at different thresholds/intervals (e.g., "Will oil hit $90?", "$95?", "$100?"). These prices should be monotonically ordered (hitting $100 implies hitting $90), but they frequently get out of sync. Buy the cheap implied-Yes on the easier threshold, sell the expensive one on the harder threshold, or exploit any logical inconsistency between intervals.
- **Outcome mispricing**: Multi-outcome markets where probabilities sum to more or less than 100% — buy the underpriced side or sell the overpriced side
- **Cross-market arbitrage**: Same event priced differently across correlated markets (e.g., "Will X happen by March?" vs "Will X happen by April?" where March Yes implies April Yes)
- **Correlated event arbitrage**: Two markets that are logically linked but priced inconsistently (e.g., "Will oil hit $90?" at 95% while "Will oil hit $95?" at 90%)
- **Cross-platform arbitrage**: Same event on Polymarket vs Kalshi or other prediction markets with different odds

### 2. Volatility Trading (Second Priority)
Find high-volatility markets and trade the swings — never hold to resolution:
- Identify markets with large daily price swings (>5% moves)
- Buy dips, sell rips — profit from oscillation, not outcome
- Focus on event-driven markets where news causes sharp moves (politics, commodities, geopolitics)
- Set target entry/exit spreads (e.g., buy at 0.40, sell at 0.50)
- Use order book depth to gauge liquidity before entering
- Cut losses fast — if thesis breaks, exit immediately

### 3. Mispriced Probability Plays (Third Priority)
Find markets where the price doesn't match real-world probability:
- Look for markets where our assessed probability differs significantly from market price
- The bigger the gap between perceived odds and market price, the better
- Example: If real odds are ~50/50 but market prices it 30/70, take the 30 side
- Use news, data, and logic to form independent probability estimates
- Focus on markets where we have an information or analytical edge
- Avoid markets driven purely by sentiment/hype with no analytical anchor

### 4. Stale Price Hunting (Strategy #0)
Scan for markets where real-world probability has shifted due to second-order effects
but the market price hasn't caught up yet:
- Run `python3 intel.py news` to find breaking developments
- Cross-reference with related Polymarket markets
- Example: Iran closes Hormuz → oil markets reprice fast, but "Iran ceasefire" markets lag
- First-mover advantage: act before the market digests the news

## Risk Management
- Never bet more than 20% of bankroll on a single position (per CLAUDE.md rules)
- Always check liquidity (order book depth) before entering
- Prefer markets with high volume (easier to exit)
- Set hard stop-losses on every trade (documented in play files)
- Take profits — don't get greedy waiting for max payout
- **Keep 10-15% cash reserve** for averaging down or new opportunities
- Correlated positions count together for risk sizing (e.g., CL $100 + CL $105 = one oil bet)
- **Multiple buys on the same market count as one position** — check total exposure before adding
- Check liquidity (bid depth) BEFORE entering illiquid markets — if best bid is <0.03, max 1% of bankroll

## Current Portfolio Priorities (2026-03-19)

### Immediate (check daily)
1. **CL $100** — HOLD, thesis playing out. Watch for $100 settlement.
2. **CL $105** — HIGHEST RISK. Exit by March 24 if CL hasn't broken $100.
3. **Iran ceasefire Apr 30 NO** — Near TP1. Sell half at 0.71+ to free cash.

### Hold (check weekly)
4. **Iran ceasefire Mar 31 NO** — Bond play, hold to resolution March 31.
5. **Starmer June 30** — Highest conviction. Wait for May elections catalyst.
6. **China/Taiwan blockade** — Lottery ticket, hold until signal or expiry.

### Key Risk: Zero Cash Reserve
We are 99.9% deployed. Priority is freeing cash via:
- Partial profit-taking on Iran Apr 30 NO (~$1.40)
- Exiting CL $105 if thesis weakens (~$8-10)
This would restore ability to act on new opportunities.
