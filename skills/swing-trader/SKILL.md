# Indian Equity Swing Trader

By AD–Ritesh (TKM Framework + Technical Setups)

Disciplined swing trading for Indian equities on the daily timeframe. Scan for high-probability setups, size positions mechanically, enter with defined risk, trail stops, exit without emotion.

## When to Use

- User asks to scan for swing trades or setups
- User asks to analyze a stock technically
- User asks about position sizing or capital allocation
- User asks to place or manage a swing trade
- User asks for trade journal or performance review

## Tools

- **scanner.py** (via shell_exec): batch scan NSE stocks for patterns + indicators using tradingview_ta. No API key.
- **Kite MCP**: kite_get_holdings, kite_get_positions, kite_get_margins, kite_place_gtt, kite_cancel_order
- **memory_write**: store trade journal entries

The scanner lives at the same directory as this skill file. Derive the scripts path from the skill path shown above (replace `SKILL.md` with `scripts/scanner.py`).

## Quick Reference

```bash
# Scan all patterns across Nifty 50
python3 <skill_dir>/scripts/scanner.py scan --pattern all

# Scan specific pattern
python3 <skill_dir>/scripts/scanner.py scan --pattern inside_candle
python3 <skill_dir>/scripts/scanner.py scan --pattern nr7
python3 <skill_dir>/scripts/scanner.py scan --pattern volume_dryup
python3 <skill_dir>/scripts/scanner.py scan --pattern engulfing

# Scan a different watchlist
python3 <skill_dir>/scripts/scanner.py scan --pattern all --watchlist nifty_next50

# Analyze a single stock (all indicators + pattern check)
python3 <skill_dir>/scripts/scanner.py analyze --symbol RELIANCE

# Multi-timeframe: add --timeframe weekly
python3 <skill_dir>/scripts/scanner.py analyze --symbol TATAMOTORS --timeframe weekly

# Position sizing calculator
python3 <skill_dir>/scripts/scanner.py size --symbol RELIANCE --entry 1275 --sl 1255 --capital 100000
```

All commands output JSON. Parse and present using the format in the Response Format section.

## Workflow

### 1. Scan → 2. Validate → 3. Size → 4. Present → 5. Execute

**Step 1 — Scan.** Run scanner.py with `--pattern all`. It checks inside candle, NR7, volume dry-up, bullish engulfing. Trend, RSI, and volume filters are applied automatically.

**Step 2 — Validate.** For each hit, run `analyze --symbol X` to get the full indicator picture. For higher conviction, also run with `--timeframe weekly` to confirm the weekly trend aligns. For the full MTF checklist and structure analysis, read `references/multi-timeframe.md`.

**Step 3 — Size.** Quick version: divide capital into 6–8 equal parts, max 1–2% risk per trade, minimum 1:2 R:R. Use `scanner.py size` to calculate. For full TKM rules, read `references/tkm-rules.md`.

**Step 4 — Present.** Show the setup to the user using the response format below.

**Step 5 — Execute.** Only on explicit user approval. Place GTT via Kite MCP. Log to memory.

## Entry Patterns (Summary)

Four patterns, daily timeframe only. Full formulas and edge cases in `references/patterns.md`.

| Pattern | Signal | SL | Best When |
|---------|--------|----|-----------|
| Inside Candle | high < prev high, low > prev low | Inside candle low | Forms at 20 EMA pullback in uptrend |
| NR7 | Narrowest range in 7 days | NR7 candle low | NR7 + inside candle combo |
| Volume Dry-Up | 3+ days declining volume, then breakout | Consolidation low | At support with volume spike on breakout |
| Bullish Engulfing | Green candle engulfs prior red candle body | Engulfing candle low | At a known support level, above 50 EMA |

## Filters (Apply to Every Setup)

1. **Trend**: price above 20 EMA on daily. Never swing trade below 200 DMA.
2. **RSI**: RSI(14) between 40–65. Not overbought, not oversold.
3. **Volume**: breakout candle volume > 1.5× 20-day average.
4. **Relative strength**: stock's EMAs stacked bullish (20 > 50 > 200). Compare vs own sector, not hardcoded Nifty 50. Details in `references/filters.md`.
5. **No earnings within 5 trading days.**
6. **Market regime**: if Nifty below 20 EMA, reduce to 3–4 trades max.

## Position Sizing (TKM Quick Rules)

- Capital ÷ 6–8 = per-trade allocation
- Max risk per trade: 1–2% of total swing capital
- If SL is wide, reduce quantity: `qty = (capital × risk_pct) / SL_distance`
- Minimum R:R = 1:2. Skip if chart doesn't offer it.
- Full rules and examples in `references/tkm-rules.md`

## Exit Rules (Summary)

- SL is set at entry. Non-negotiable. Never move it down.
- At 1:1 R:R → move SL to breakeven
- At 1.5:1 → trail with 20 EMA daily
- At 2:1 → book 50%, trail rest with 20 EMA
- At 3:1+ → switch to 9 EMA trail
- If no movement in 10 days → exit (dead money)
- 3 consecutive SLs hit → stop for the rest of the week
- Full trailing system and edge cases in `references/exits.md`

## Response Format

When presenting a trade setup:

```
📊 SWING SETUP: [SYMBOL]
Pattern: [Inside Candle / NR7 / Volume Dry-up / Engulfing]
Timeframe: Daily

Entry: ₹[price] (trigger above [level])
SL: ₹[price] ([X]% below entry)
T1: ₹[price] (R:R 1:[X]) — book 50%
T2: ₹[price] (R:R 1:[X]) — trail with 20 EMA

Qty: [N] shares (₹*** / [X]% of swing capital)
Risk: ₹*** ([X]% of total capital)

Filters: ✅ Above 20 EMA | ✅ RSI [value] | ✅ Volume OK | ✅ No earnings soon
Weekly: [Aligned / Not aligned]
```

Mask all rupee amounts with ₹***. Show percentages and quantities.

## Safety Rules

- All analysis is advisory. Never guarantee returns.
- GTT placement always requires explicit user approval.
- Mask monetary amounts in holdings/P&L.
- If setup doesn't meet all filters, skip it and say why.
- Never recommend margin/leverage for swing trading.
- If capital < ₹1 lakh, advise building capital first.

## Detailed References

Read these on demand, not upfront:

- `references/patterns.md` — full pattern detection formulas, examples, edge cases
- `references/tkm-rules.md` — complete TKM position management framework
- `references/filters.md` — all filter criteria, sector rotation, market regime
- `references/exits.md` — trailing SL system, partial booking, time exits
- `references/multi-timeframe.md` — MTF analysis, structure, momentum confirmation
