# Multi-Timeframe Analysis & Structure

A daily setup alone is a coin toss. A daily setup confirmed by weekly trend and structure is a high-probability trade.

## Three-Timeframe Rule

| Timeframe | Purpose | What to Check |
|-----------|---------|---------------|
| Weekly | Trend direction | Is the stock in a weekly uptrend? EMA20 > EMA50 on weekly? |
| Daily | Setup identification | Pattern (inside candle, NR7, etc.) + filters |
| 4H or Hourly | Entry timing (optional) | Precise entry within the daily setup zone |

**Minimum requirement:** weekly + daily alignment. The hourly is a bonus for tighter entries.

## Weekly Trend Check

Run scanner.py analyze with `--timeframe weekly`:
```bash
python3 <skill_dir>/scripts/scanner.py analyze --symbol RELIANCE --timeframe weekly
```

**Weekly bullish (proceed with daily setup):**
- Weekly close > weekly EMA20
- Weekly EMA20 > weekly EMA50
- Weekly RSI > 50
- Bonus: weekly making higher highs and higher lows

**Weekly neutral (proceed with caution):**
- Weekly close near EMA20 (within 2%)
- Weekly RSI between 45–55
- Reduce position size by 25%

**Weekly bearish (skip the trade):**
- Weekly close < weekly EMA50
- Weekly RSI < 45
- Even the best daily pattern will likely fail against a weekly downtrend

## Structure Analysis

Structure = support and resistance levels derived from price action, not just indicators.

### Support Identification

Check these levels (any match = valid support):

1. **Pivot levels:** Pivot.M.Classic.S1, S2 (monthly pivots). Price bouncing off these is strong.
2. **Moving averages:** EMA50, EMA200 on daily. EMA20 on weekly. These act as dynamic support.
3. **Bollinger lower band:** BB.lower. Price touching this in an uptrend = mean reversion opportunity.
4. **Prior swing low:** check low[1] through low[6] for recent swing lows. Price revisiting a prior low = double bottom potential.
5. **Round numbers:** ₹100, ₹200, ₹500, ₹1000 etc. Psychological support.

### Resistance Identification

1. **Pivot levels:** Pivot.M.Classic.R1, R2 (targets for the trade)
2. **Prior swing high:** high[1] through high[6] — if price stalled here before, it may stall again
3. **Bollinger upper band:** BB.upper — dynamic resistance in ranging markets
4. **52-week high:** if close is within 5% of the 52-week high, expect resistance

### How Structure Improves Setups

**Best case:** inside candle forms right at a support level (say EMA50 AND Pivot S1 both nearby). This gives you:
- Tight SL (below the support level)
- Clear target (next resistance level)
- Higher probability (support = institutional buying zone)

**Worst case:** inside candle forms in the middle of a range with no support/resistance context. SL is arbitrary, target is unclear. Skip.

## Momentum Confirmation

Beyond RSI 40–65, these momentum signals add conviction:

### RSI Divergence
- **Bullish divergence:** price makes lower low, RSI makes higher low → trend reversal coming
- Best when: divergence appears at a support level with an engulfing candle
- Check: compare current RSI and RSI[1] vs low and low[1]

### MACD
- **MACD histogram turning positive from below zero** → momentum shifting bullish
- Not an entry signal alone, but confirms other patterns
- Check: MACD.macd > MACD.signal AND MACD.macd approaching zero from below

### ADX Trend Strength
- **ADX > 25:** strong trend. Swing setups ride the momentum.
- **ADX < 20:** weak/ranging. More SL hits, less follow-through.
- **ADX rising + DI+ > DI-:** bullish trend strengthening. Green light.
- **ADX falling:** trend exhaustion. Be cautious, tighten targets.

### Stochastic
- **Stoch K crossing above D from below 20:** oversold reversal signal
- Best when combined with price at support + pattern (engulfing, inside candle)
- Not reliable alone — always pair with price structure

## Sector Analysis

Before committing to a stock, check its sector health:

### Sector Indices to Monitor
Run scanner.py analyze on these (exchange: NSE, screener: india):
- NIFTY_BANK
- NIFTY_IT
- NIFTY_PHARMA
- NIFTY_AUTO
- NIFTY_METAL
- NIFTY_REALTY
- NIFTY_FMCG
- NIFTY_ENERGY

### Sector Health Grading
- **Leading:** EMA20 > EMA50, RSI > 55, ADX > 20. Actively scan stocks here.
- **Neutral:** EMAs mixed, RSI 45–55. Scan only for the strongest patterns.
- **Lagging:** EMA20 < EMA50, RSI < 45. Skip stocks in this sector.

### Sector Rotation Signal
When a sector transitions from lagging to neutral (EMA20 crossing above EMA50), early entries in that sector can catch a rotation. But require stronger individual stock setups (multiple filter confirmations).

## Conviction Scoring

After analysis, score the setup 1–5:

| Score | Criteria |
|-------|----------|
| 5 | Weekly aligned + daily pattern + at support + volume confirmed + sector leading |
| 4 | Weekly aligned + daily pattern + volume confirmed + sector neutral |
| 3 | Weekly neutral + daily pattern + filters pass |
| 2 | Daily pattern only, weekly unclear |
| 1 | Pattern exists but filters failing |

**Trade only scores 3+.** Score 5 setups get full position size. Score 3 setups get 75% position size.
