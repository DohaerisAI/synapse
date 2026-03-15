# Exit Rules

Entries are optional. Exits are mandatory. Every trade must have a pre-defined exit plan before entry.

## Stop Loss — Non-Negotiable

- Set SL at the time of entry. Write it down. Do not change it.
- Typical SL range: **5–8% below entry** for most setups
- Inside candle / NR7 setups often have tighter SLs (2–5%) because the pattern low is close to entry
- **Never move SL down.** Never hold hoping "it will come back."
- If SL is hit, exit immediately. Book the loss. Move on.

## Trailing Stop Loss System

Once the trade moves in your favor, trail mechanically:

### Stage 1: Breakeven (at 1:1 R:R)
- When unrealized profit equals the risk amount, move SL to entry price
- You now have a free trade — worst case is breakeven
- Example: entry ₹100, SL ₹95 (risk = ₹5). When price hits ₹105, move SL to ₹100

### Stage 2: 20 EMA Trail (at 1.5:1 R:R)
- Switch to trailing with the **20 EMA on daily close**
- Exit rule: if daily candle **closes below** 20 EMA, exit next morning
- Do NOT exit on intraday wicks below 20 EMA — only on closing basis
- Example: entry ₹100, SL ₹95. Price at ₹107.50+ → trail with 20 EMA

### Stage 3: Partial Booking (at 2:1 R:R)
- Book **50% of the position** at 2× the risk
- Let the remaining 50% ride with the 20 EMA trail
- Example: entry ₹100, SL ₹95, risk = ₹5. At ₹110, book half. Trail rest.

### Stage 4: Tight Trail for Runners (at 3:1+ R:R)
- Switch remaining position to **9 EMA trail** for tighter tracking
- Exit rule: daily close below 9 EMA → exit remaining position
- This captures the momentum phase while protecting profits

### Summary Table

| R:R Reached | Action | Trail Method |
|-------------|--------|-------------|
| 1:1 | Move SL to breakeven | Fixed at entry price |
| 1.5:1 | Start trailing | 20 EMA daily close |
| 2:1 | Book 50% | Continue 20 EMA on remaining |
| 3:1+ | Tighten trail | Switch to 9 EMA on remaining |

## Target-Based Exits

In addition to trailing, watch for these resistance-based exits:

- **Pivot resistance:** if price stalls at Pivot.M.Classic.R1 or R2 for 2+ days with declining volume, consider booking
- **Prior swing high:** if price approaches a prior swing high and shows weakness (red candle, volume drop), book partial
- **Fibonacci extension:** if you can identify the impulse leg, 1.618× extension is a natural target zone

## Gap Exits

- If a stock **gaps up > 5%** above previous close at open, consider booking on the gap day
- Gaps of this size often fill partially — book profits while they exist
- Exception: if the gap is on high volume with a bullish catalyst (sector breakout, results), hold with tighter trail

## Time-Based Exit

- If a trade has **not moved meaningfully in 10 trading days**, exit at market
- "Not moved" = price within 2–3% of entry, going sideways
- Dead money is opportunity cost — that capital could be in a live setup
- **Exception:** if the stock is forming another compression pattern (inside candle, NR7) while holding above entry, it may be coiling for a bigger move. Use judgment, but bias toward exiting.

## Event-Based Exits

Exit or tighten SL before these events regardless of R:R status:

- **Quarterly earnings:** exit at least 1 day before results. Earnings gap risk is unmanageable.
- **Union Budget day:** reduce exposure. Budget reactions are unpredictable.
- **RBI monetary policy:** if holding banking/NBFC stocks, tighten SL to breakeven minimum
- **Global events:** Fed decision, geopolitical escalation → tighten across the board

## Distribution Signals (Exit Warning)

Watch for these signs that smart money is exiting:

- **High volume red candle** after a sustained uptrend → distribution. Tighten SL immediately.
- **Price making higher highs but RSI making lower highs** → bearish divergence. Trail aggressively.
- **Volume spike without price progress** → churning. Institutions selling into retail buying.
- **Stock breaks below 20 EMA on high volume** → trend weakening. Don't wait for trailing SL. Exit.

## Trade Journal Entry (on Exit)

After every trade closes (SL or target), log to memory:

```
Trade: [SYMBOL]
Pattern: [which pattern]
Entry: [price] | Exit: [price]
R:R planned: 1:[X] | R:R realized: 1:[X]
Result: [profit/loss] [X]%
Duration: [N] days
Notes: [what worked, what didn't]
```

Use memory_write with scope=user to store. This builds a performance database for review.
