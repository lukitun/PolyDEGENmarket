# Advanced Trading Strategies

## Strategy 0: "Stale Price" Hunting (HIGHEST PRIORITY)
**How it works:** Find markets where the real-world probability has shifted significantly due to a major event, but the market price hasn't moved. This is the purest form of edge — the market is asleep while reality changed.

**Why it works:** Prediction markets are efficient at pricing well-known information, but they lag on SECOND-ORDER EFFECTS. When Event A happens, everyone reprices the markets directly about A. But markets about B, C, D that are indirectly affected often don't move for days or weeks. That's where the money is.

**The China-Taiwan Example:**
- Iran war started Feb 28, US military resources stretched, Trump delayed Xi meeting
- Direct Iran markets moved instantly (oil, ceasefire, military action)
- But "China blockade Taiwan" barely moved (+1%) even though:
  - US is distracted and overextended
  - No US-China diplomatic guardrails (meeting delayed)
  - China is actively rehearsing blockade drills
  - Historical pattern: adversaries exploit when the US is in a conflict elsewhere
- The REAL probability shifted much more than the price reflected

**How to find these:**
1. When a MAJOR event happens (war, crisis, election shock, natural disaster)
2. Map ALL second-order effects — what else becomes more/less likely?
3. Check those related markets — did the price move proportionally?
4. If price barely moved but reality shifted → BUY
5. These are the highest-edge, lowest-competition trades because most traders only look at direct markets

**Checklist for every major event:**
- Military conflict → What other conflicts become more likely? (distracted superpower)
- Oil shock → Who gets hurt? (recession, energy-dependent economies, elections)
- Political crisis → What policies change? (trade, sanctions, alliances)
- Natural disaster → Supply chains? Elections? Insurance markets?
- Tech breakthrough → Who loses? What becomes obsolete?

**Current stale price candidates to always monitor:**
- China-Taiwan (any time US is in a conflict)
- NATO cohesion (any time US acts unilaterally)
- Recession markets (any time oil spikes)
- Currency markets (any time central banks diverge)
- Election markets (any time a scandal breaks in adjacent politics)

**Rules:**
- The bigger the second-order gap, the bigger the position
- These are often low-liquidity → use limit orders, be patient
- Set wide TPs — the market may take days/weeks to catch up
- Perfect for lottery ticket sizing on tail risks (like our China play)

---

## Strategy 1: "Bond" Plays (Near-Certain Markets)
**How it works:** Buy markets priced 90-98c that are near-certain to resolve YES. Earn 2-10% return in days/weeks. Compound repeatedly.

**Why it works:** A 5% return in 7 days = ~260% annualized. Low risk if probability assessment is correct.

**Rules:**
- Only buy at 90-98c if true probability is 99%+
- Must have >$50k daily volume (liquidity to exit)
- Never hold more than 20% in a single bond play
- Check resolution date — prefer resolving within 30 days
- Use Kelly criterion: bet = (p*b - q) / b where p=true prob, b=net odds, q=1-p

**Current Opportunities (research needed):**
- Sports finals (teams that are massive favorites)
- Political races already decided (e.g., incumbents with no challenger)
- Events that have already happened but market hasn't resolved yet

**Risk:** Black swan events. A 97c "sure thing" can go to $0 if something unexpected happens. Only use with truly near-certain outcomes.

---

## Strategy 2: News-Driven Speed Trading
**How it works:** Claude Code monitors breaking news, assesses impact on Polymarket prices, and trades before the market fully adjusts. Prices lag 30 seconds to several minutes after breaking news.

**Why it works:** Claude can analyze news, assess probability shifts, and place trades faster than most retail traders. Information asymmetry is the core edge.

**Workflow:**
1. Monitor news sources (Reuters, Bloomberg, Al Jazeera for Iran)
2. When breaking news hits, assess which Polymarket markets it affects
3. Calculate new probability vs current market price
4. If gap >5%, trade immediately
5. Exit when price catches up to fair value

**Best markets for this:** Iran war, oil prices, political events, crypto prices

---

## Strategy 3: Weekly Expiry Swing Trading
**How it works:** Trade weekly markets (Mon-Fri expiry) that have high intraday volatility. Buy dips, sell rips within the week.

**Why it works:** Short timeframe means faster capital recycling. Weekly markets on oil, stocks, crypto have big intraday swings driven by the underlying asset.

**Rules:**
- Focus on asset-linked markets (CL oil, BTC, ETH, SPX)
- Enter when underlying moves sharply against the market price (market hasn't caught up)
- Exit same day or next day — never hold to Friday expiry unless deeply ITM
- Use deep scan (`volatility.py deep <token>`) to identify swing ranges

---

## Strategy 4: Correlated Event Chains
**How it works:** When Event A happens, it makes Event B much more likely. Buy Event B before the market reprices it.

**Current chains:**
- Iran escalation → Oil higher → CL $110/$115 markets jump
- Hormuz stays closed → Global recession fears → Fed rate cut markets move
- China-Taiwan escalation → Tech supply chain fears → Market crash bets
- Ceasefire announced → Oil crashes → CL "hit LOW $X" markets spike
- Starmer loses May elections → Starmer out by June 30 → YES price jumps

**Rules:**
- Map out cause-effect chains BEFORE the trigger event
- Pre-identify which Polymarket tokens to buy
- When trigger fires, execute immediately
- This is the highest-edge strategy for Claude Code as the brain

---

## Strategy 5: Market Making (Future — needs more capital)
**How it works:** Place limit orders on both YES and NO sides, capture the bid-ask spread. Polymarket also pays daily liquidity rewards.

**Why wait:** With $52 bankroll, spread capture is pennies. Need $5k+ to make this worthwhile. Build capital with strategies 1-4 first.

**When ready:**
- Use Polymarket's official market maker bot (GitHub: Polymarket/poly-market-maker)
- Target high-volume markets with wide spreads (5c+)
- Quadratic rewards favor orders 1-2c from midpoint

---

## Strategy 6: Cross-Platform Arbitrage (Polymarket vs Kalshi)
**How it works:** Same event priced differently on Polymarket and Kalshi. Buy cheap side on one, sell expensive side on the other.

**Limitations:**
- Need Kalshi account (US-regulated, requires KYC)
- Gaps last only 2-7 seconds — need automation
- Fee difference matters (Polymarket 0.01% vs Kalshi ~1.2%)

**When to pursue:** After building capital and setting up Kalshi API access.

---

## Strategy 7: Kelly Criterion Sizing
**Formula:** fraction = (p × b - q) / b
- p = estimated true probability
- b = net payout per dollar risked (1/price - 1)
- q = 1 - p

**Use half-Kelly** (multiply result by 0.5) to reduce variance.

**Example:** Market at 0.60, we think true prob is 75%
- b = (1/0.60) - 1 = 0.667
- fraction = (0.75 × 0.667 - 0.25) / 0.667 = 0.375
- Half-Kelly: 0.1875 → bet 18.75% of bankroll

**Rule:** Never exceed 20% regardless of Kelly output (hard cap).

---

## Current Geopolitical Opportunity Map

### Iran-Hormuz (ACTIVE CRISIS)
- Strait closed, Iran says no ceasefire
- Oil at $99, analysts say $100+ in March, $200 possible if prolonged
- Goldman: Brent >$100 March average
- SPR release underway but takes months
- **Our positions:** CL $100 YES, CL $105 YES

### China-Taiwan (MONITORING)
- China rehearsing blockade, 130+ aircraft sorties in recent drills
- 2027 is the "ready by" date for invasion capability
- Xi-Trump meeting planned end of March → likely DE-escalation short term
- Market: "China invade Taiwan by 2026" at 10.3% — seems fair
- Market: "China blockade Taiwan by June 30" at 6.5% — could be underpriced if Iran crisis emboldens China
- **Action:** Monitor. If Iran crisis escalates further or US looks distracted, consider buying Taiwan blockade YES at 6.5c

### UK Politics (ACTIVE PLAY)
- Starmer under massive pressure, bookies give 80-87% exit in 2026
- May local elections are the critical trigger
- **Our position:** Starmer out by June 30 YES @ 0.44

---

## Immediate Action Items (Next Sessions)

### Priority 1: Event Chain Pre-Mapping
Map triggers → which tokens to buy → target price. Have orders ready BEFORE news hits:
- **If Hormuz escalates:** Buy CL $110, $115, $120 YES immediately
- **If ceasefire announced:** Sell all CL positions instantly, buy "CL hit LOW $X" markets
- **If China moves on Taiwan:** Buy blockade YES at 6.5c, buy tech crash markets
- **If Starmer scandal deepens:** Add to Starmer YES position

### Priority 2: Bond Play Execution
Deploy $5-10 of reserve into 1-2 near-certain markets:
- Research Man City PL status (are they actually near-certain?)
- Research Detroit Pistons NBA Finals (97c — what's the situation?)
- Look for already-resolved events that haven't been settled yet

### Priority 3: Build News Monitor
Create a script that periodically checks news sources and alerts on:
- Iran/Hormuz keywords
- Oil price movements >$2
- China/Taiwan military keywords
- UK politics / Starmer keywords

### Priority 4: Weekly Expiry Pipeline
Every Monday: scan weekly oil/crypto/stock markets for swing trades
Every Friday: close all weekly positions before expiry
