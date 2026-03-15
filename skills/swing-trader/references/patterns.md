# Pattern Detection Formulas

All patterns use tradingview_ta data via scanner.py. The `[N]` suffix fetches the Nth previous candle's value.

## Inside Candle

An inside candle's entire range fits within the previous candle (the "mother" candle).

**Detection:**
```
high < high[1]  AND  low > low[1]
```

**Volume confirmation:**
```
volume < volume[1]    (compression — less activity on the inside bar)
```

**SL placement:** below the inside candle's low. If very tight (< 2%), use the mother candle's low instead.

**Entry trigger:** price crosses above mother candle's high (high[1]) on the next session.

**Best setups:**
- Inside candle forms right at the 20 EMA after a pullback in an uptrend
- Multiple inside candles in sequence (2-3 inside bars = stronger compression)
- Inside candle with NR7 (combo signal — highest probability)

**Skip when:**
- Inside candle forms mid-range with no EMA or support context
- In a downtrend (below 50 EMA) — inside candles become continuation bearish patterns
- Mother candle is a doji or very small range (no real compression)

## NR7 (Narrow Range 7)

The candle with the narrowest high-low range in the last 7 trading sessions. Signals extreme compression before expansion.

**Detection:**
```
range_today = high - low
range_today < high[1] - low[1]
range_today < high[2] - low[2]
range_today < high[3] - low[3]
range_today < high[4] - low[4]
range_today < high[5] - low[5]
range_today < high[6] - low[6]
```

**SL placement:** below the NR7 candle's low.

**Entry trigger:** price crosses above NR7 candle's high.

**NR7 + Inside Candle combo:** when the NR7 candle is also an inside candle, this is the highest probability setup. Prioritize these always.

## Volume Dry-Up Breakout

3–5 consecutive days of declining volume while price holds at a level. Then a volume spike on the breakout candle.

**Detection (3-day):**
```
volume < volume[1]  AND  volume[1] < volume[2]  AND  volume[2] < volume[3]
```

**Detection (5-day — stronger):**
```
volume < volume[1] < volume[2] < volume[3] < volume[4] < volume[5]
```

Note: the "volume" here refers to the compression days. The breakout day itself should have HIGH volume:
```
breakout_volume > 1.5 × average_volume_20
```

The scanner checks for 3-day declining volume first (more common), then flags 5-day as "strong" if found.

**SL placement:** below the consolidation low (lowest low in the dry-up period).

**Entry trigger:** breakout above the consolidation high with volume confirmation.

## Bullish Engulfing at Support

A green candle whose real body completely engulfs the previous red candle's real body, forming at a support level.

**Detection:**
```
close > open              (today green)
close[1] < open[1]       (yesterday red)
close > open[1]           (today's close > yesterday's open)
open < close[1]           (today's open < yesterday's close)
```

**Support check (any of these):**
- Price near Pivot.M.Classic.S1 or S2 (within 1%)
- Price near BB.lower (within 1%)
- Price near EMA50 or EMA200 (within 1%)

**Additional filter:** volume on the engulfing candle should be above the 20-day average.

**SL placement:** below the engulfing candle's low.

**Entry trigger:** above the engulfing candle's high.

**Skip when:**
- No clear support level nearby — engulfing in mid-air is low probability
- The engulfing candle's range is < 1% (too small to be meaningful)
- Stock is below 200 DMA — engulfing against a major downtrend rarely works

## Pattern Priority

When multiple patterns trigger on the same stock, prioritize:

1. NR7 + Inside Candle combo (highest)
2. Inside Candle at 20 EMA pullback
3. NR7 standalone
4. Volume Dry-Up Breakout
5. Bullish Engulfing at Support

## Indicators Requested per Scan

The scanner requests these from tradingview_ta in one batch call:

```
OHLCV:     open, high, low, close, volume, change
Lookback:  open[1], high[1], low[1], close[1], volume[1]
           high[2..6], low[2..6], volume[2..6]
Trend:     EMA20, EMA50, EMA200, SMA20, SMA50, SMA200
Momentum:  RSI, RSI[1], ADX, ADX+DI, ADX-DI
           MACD.macd, MACD.signal, Stoch.K, Stoch.D
Volatility: BB.upper, BB.lower
Volume:    VWMA
Pivots:    Pivot.M.Classic.S1, S2, R1, R2, Middle
Recs:      Recommend.All, Recommend.MA, Recommend.Other
```

50 indicators per symbol. Batch scan: 50 symbols × 50 indicators = one API call.
