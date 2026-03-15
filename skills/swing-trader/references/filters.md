# Filter Criteria

Apply ALL of these before entering any trade. If any filter fails, skip the setup.

## 1. Trend Filter

**Primary:** close > EMA20 on daily (short-term uptrend)
**Secondary:** close > EMA50 (medium-term intact)
**Hard stop:** never swing trade a stock below 200 DMA. That's a broken stock.

**EMA alignment check:**
```
Strong uptrend: EMA20 > EMA50 > EMA200 (stacked bullish)
Weakening:      EMA20 < EMA50 but both > EMA200 (pullback, may be okay for engulfing)
Broken:         close < EMA200 (skip entirely)
```

## 2. RSI Filter

RSI(14) between **40–65** at time of pattern formation.

- Below 40: stock is weak, momentum gone. Skip.
- Above 65: stock is extended, limited upside before overbought territory. Skip.
- Exception: if stock just broke out of a 4+ week base, RSI can be 65–70. The breakout energy allows higher RSI.

**RSI divergence bonus (not a filter, but a conviction booster):**
If RSI is making higher lows while price makes equal or lower lows → bullish divergence. Higher probability setup.

## 3. Volume Filter

**On the pattern candle (compression days):** volume should be declining or below average. Quiet = coiled spring.

**On the breakout candle:** volume > 1.5× the 20-day average volume. If volume is weak on breakout, the move is less reliable.

VWMA relationship: price above VWMA = net buying pressure from institutions. Price below VWMA = selling pressure.

## 4. Relative Strength

The stock should be outperforming its relevant benchmark. Do NOT hardcode Nifty 50 for all stocks.

**Matching benchmark:**
- Nifty 50 constituent → compare vs Nifty 50
- Midcap stock → compare vs Nifty Midcap 150
- Smallcap stock → compare vs Nifty Smallcap 250
- Or compare vs sectoral index (Nifty IT, Nifty Pharma, Nifty Bank, etc.)

**Simplest practical check (use this by default):**
```
Stock EMAs stacked bullish (EMA20 > EMA50 > EMA200)
AND stock making higher lows on daily
```
If you can't determine the index, this is enough. A stock with all EMAs stacked bullish and making higher lows IS strong, regardless of what any index is doing.

**For sector scan:** use scanner.py to check sector indices (NIFTY_BANK, NIFTY_IT, etc.) for their EMA status. Prefer stocks in sectors with bullish EMA stacks.

## 5. Earnings Filter

Do NOT enter if quarterly results are due within **5 trading days**. Earnings gaps destroy swing setups — a 10% gap down blows through any SL.

The scanner does not check this automatically. The LLM should:
1. Check the company's earnings date (search web if needed)
2. If within 5 days, skip the trade and tell the user why

## 6. F&O Expiry Filter

For F&O stocks, avoid entering during weekly expiry week (Thursday expiry). Expiry-related unwinding causes whipsaws.

- Prefer entries on Monday/Tuesday of non-expiry weeks
- This is a soft filter — override if the setup is very strong

## 7. Market Regime

Check Nifty 50 trend to calibrate exposure:

**Nifty above 20 EMA (healthy market):**
- Full exposure: 6–8 trades allowed
- Normal position sizing

**Nifty below 20 EMA but above 50 EMA (caution):**
- Reduce to 3–4 trades max
- Prefer defensive sectors (pharma, FMCG, IT)
- Tighten SLs by 1–2%

**Nifty below 50 EMA (bear regime):**
- Maximum 1–2 trades, only the strongest setups
- Or sit in cash entirely — capital preservation > returns
- Only trade stocks showing independent strength (all EMAs bullish while Nifty is not)

**ADX confirmation:**
- ADX > 25: trending market → swing setups work well
- ADX < 20: ranging/choppy market → reduce position count, expect more SL hits
- ADX between 20-25: transitional, proceed with caution

## Sector Rotation Awareness

When scanning, check which sectors are leading:
- Run scanner.py analyze on sector indices: NIFTY_BANK, NIFTY_IT, NIFTY_PHARMA, NIFTY_AUTO, NIFTY_METAL, NIFTY_REALTY, NIFTY_FMCG, NIFTY_ENERGY
- Sectors with EMA20 > EMA50 and RSI > 50 are leading
- Bias your stock scans toward leading sectors
- Avoid stocks in sectors with EMA20 < EMA50 (rotating out)
