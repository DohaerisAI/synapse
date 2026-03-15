#!/usr/bin/env python3
"""Swing trade pattern scanner using tradingview_ta.

Usage:
    scanner.py scan --pattern all [--watchlist nifty50]
    scanner.py scan --pattern inside_candle [--watchlist nifty_next50]
    scanner.py analyze --symbol RELIANCE [--timeframe weekly]
    scanner.py size --symbol RELIANCE --entry 1275 --sl 1255 --capital 100000
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from tradingview_ta import TA_Handler, Interval
    from tradingview_ta.main import TradingView
    import requests
except ImportError:
    print(json.dumps({"error": "tradingview_ta not installed. Run: pip install tradingview_ta"}))
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent.parent
WATCHLIST_DIR = SCRIPT_DIR / "watchlists"

INTERVALS = {
    "1m": Interval.INTERVAL_1_MINUTE,
    "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR,
    "4h": Interval.INTERVAL_4_HOURS,
    "daily": Interval.INTERVAL_1_DAY,
    "weekly": Interval.INTERVAL_1_WEEK,
    "monthly": Interval.INTERVAL_1_MONTH,
}

# Indicators requested for pattern detection + filtering
SCAN_INDICATORS = [
    # Today OHLCV
    "open", "high", "low", "close", "volume", "change",
    # Yesterday
    "open[1]", "high[1]", "low[1]", "close[1]", "volume[1]",
    # Lookback for NR7 and volume dry-up
    "high[2]", "low[2]", "volume[2]",
    "high[3]", "low[3]", "volume[3]",
    "high[4]", "low[4]", "volume[4]",
    "high[5]", "low[5]", "volume[5]",
    "high[6]", "low[6]", "volume[6]",
    # Trend
    "EMA20", "EMA50", "EMA200",
    # Momentum
    "RSI", "RSI[1]", "ADX", "ADX+DI", "ADX-DI",
    "MACD.macd", "MACD.signal",
    # Volatility
    "BB.upper", "BB.lower",
    # Volume
    "VWMA",
    # Pivots
    "Pivot.M.Classic.S1", "Pivot.M.Classic.S2",
    "Pivot.M.Classic.R1", "Pivot.M.Classic.R2",
    "Pivot.M.Classic.Middle",
    # Recommendations
    "Recommend.All", "Recommend.MA", "Recommend.Other",
]

# Full analysis adds more indicators
ANALYZE_INDICATORS = SCAN_INDICATORS + [
    "SMA20", "SMA50", "SMA200",
    "EMA5", "EMA10", "SMA5", "SMA10",
    "Stoch.K", "Stoch.D", "CCI20", "W.R", "Mom",
    "AO", "UO", "P.SAR", "Ichimoku.BLine", "HullMA9",
    "Pivot.M.Fibonacci.S1", "Pivot.M.Fibonacci.R1",
    "Pivot.M.Camarilla.S1", "Pivot.M.Camarilla.R1",
]


def load_watchlist(name: str) -> list[str]:
    """Load symbols from a watchlist file."""
    path = WATCHLIST_DIR / f"{name}.txt"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def fetch_batch(symbols: list[str], indicators: list[str], interval: str = "daily") -> dict:
    """Fetch indicators for multiple symbols in one API call."""
    tickers = [f"NSE:{s}" for s in symbols]
    tv_interval = INTERVALS.get(interval, Interval.INTERVAL_1_DAY)
    data = TradingView.data(tickers, tv_interval, indicators)
    scan_url = f"{TradingView.scan_url}india/scan"
    headers = {"User-Agent": "tradingview_ta/3.3.0"}
    try:
        resp = requests.post(scan_url, json=data, headers=headers, timeout=30)
    except Exception as e:
        return {"error": f"API request failed: {e}"}
    if resp.status_code != 200:
        return {"error": f"API returned {resp.status_code}"}
    result = resp.json().get("data", [])
    out = {}
    for row in result:
        symbol = row.get("s", "").replace("NSE:", "")
        values = row.get("d", [])
        if symbol and len(values) == len(indicators):
            out[symbol] = dict(zip(indicators, values))
    return out


def detect_inside_candle(d: dict) -> dict | None:
    """Detect inside candle pattern."""
    try:
        if d["high"] < d["high[1]"] and d["low"] > d["low[1]"]:
            vol_compression = d["volume"] < d["volume[1]"]
            return {
                "pattern": "inside_candle",
                "mother_high": d["high[1]"],
                "mother_low": d["low[1]"],
                "inside_high": d["high"],
                "inside_low": d["low"],
                "entry_trigger": d["high[1]"],
                "sl": d["low"],
                "volume_compression": vol_compression,
            }
    except (KeyError, TypeError):
        pass
    return None


def detect_nr7(d: dict) -> dict | None:
    """Detect NR7 (narrowest range in 7 days)."""
    try:
        today_range = d["high"] - d["low"]
        for i in range(1, 7):
            prev_range = d[f"high[{i}]"] - d[f"low[{i}]"]
            if today_range >= prev_range:
                return None
        return {
            "pattern": "nr7",
            "range": round(today_range, 2),
            "entry_trigger": d["high"],
            "sl": d["low"],
        }
    except (KeyError, TypeError):
        pass
    return None


def detect_volume_dryup(d: dict) -> dict | None:
    """Detect 3+ days of declining volume into the most recent day.

    We look for volume[3] > volume[2] > volume[1] (declining towards today),
    with an optional breakout spike today (volume > 1.3 * volume[1]).
    """
    try:
        vols = [d[f"volume[{i}]"] if i > 0 else d["volume"] for i in range(4)]
        # Declining volume into the most recent completed day (1)
        if vols[3] > vols[2] > vols[1]:
            breakout_vol_spike = vols[0] > vols[1] * 1.3
            lows = [d[f"low[{i}]"] for i in range(1, 4)]
            consol_low = min(lows)
            return {
                "pattern": "volume_dryup",
                "days_declining": 3,
                "breakout_volume_spike": breakout_vol_spike,
                "consolidation_low": consol_low,
                "entry_trigger": d["high"],
                "sl": consol_low,
            }
    except (KeyError, TypeError):
        pass
    return None


def detect_engulfing(d: dict) -> dict | None:
    """Detect bullish engulfing at support."""
    try:
        today_green = d["close"] > d["open"]
        yest_red = d["close[1]"] < d["open[1]"]
        engulfs = d["close"] > d["open[1]"] and d["open"] < d["close[1]"]
        if today_green and yest_red and engulfs:
            # Check if near a support level
            near_support = False
            support_level = None
            for key in ["Pivot.M.Classic.S1", "BB.lower", "EMA50", "EMA200"]:
                val = d.get(key)
                if val and abs(d["low"] - val) / val < 0.015:  # within 1.5%
                    near_support = True
                    support_level = key
                    break
            return {
                "pattern": "bullish_engulfing",
                "near_support": near_support,
                "support_level": support_level,
                "entry_trigger": d["high"],
                "sl": d["low"],
            }
    except (KeyError, TypeError):
        pass
    return None


def apply_filters(d: dict) -> dict:
    """Apply trend, RSI, volume filters. Return filter status."""
    filters = {}
    try:
        filters["above_ema20"] = d["close"] > d["EMA20"] if d.get("EMA20") else None
        filters["above_ema50"] = d["close"] > d["EMA50"] if d.get("EMA50") else None
        filters["above_ema200"] = d["close"] > d["EMA200"] if d.get("EMA200") else None
        filters["emas_stacked"] = (
            d["EMA20"] > d["EMA50"] > d["EMA200"]
            if all(d.get(k) for k in ["EMA20", "EMA50", "EMA200"])
            else None
        )
        filters["rsi"] = round(d["RSI"], 1) if d.get("RSI") else None
        filters["rsi_ok"] = 40 <= d["RSI"] <= 65 if d.get("RSI") else None
        filters["adx"] = round(d["ADX"], 1) if d.get("ADX") else None
        filters["adx_trending"] = d["ADX"] > 20 if d.get("ADX") else None
        filters["above_vwma"] = d["close"] > d["VWMA"] if d.get("VWMA") else None
        filters["recommend"] = round(d["Recommend.All"], 2) if d.get("Recommend.All") is not None else None
    except (KeyError, TypeError):
        pass
    # Overall pass: must be above 20 EMA and RSI in range
    filters["pass"] = bool(filters.get("above_ema20") and filters.get("rsi_ok"))
    return filters


def calc_rr(entry: float, sl: float, d: dict) -> dict:
    """Calculate risk-reward using pivot targets + fixed R-multiples."""
    risk = abs(entry - sl)
    if risk == 0:
        return {"risk": 0, "targets": [], "best_rr": 0}
    targets = []
    for key in ["Pivot.M.Classic.R1", "Pivot.M.Classic.R2"]:
        val = d.get(key)
        if val and val > entry:
            reward = val - entry
            targets.append({
                "level": key.split(".")[-1],
                "price": round(val, 2),
                "rr": round(reward / risk, 1),
            })
    targets.append({"level": "2R", "price": round(entry + 2 * risk, 2), "rr": 2.0})
    targets.append({"level": "3R", "price": round(entry + 3 * risk, 2), "rr": 3.0})
    best_rr = max((t.get("rr", 0) or 0) for t in targets) if targets else 0
    return {
        "risk_per_share": round(risk, 2),
        "risk_pct": round(risk / entry * 100, 1),
        "targets": targets,
        "best_rr": float(best_rr),
    }


def _score_setup(hit: dict, filters: dict, rr: dict) -> dict:
    """Compute a simple score to rank watch candidates."""
    score = 0.0
    reasons: list[str] = []

    # Trend alignment
    if filters.get("above_ema20"):
        score += 1.0
    else:
        reasons.append("below_ema20")
    if filters.get("above_ema50"):
        score += 0.8
    if filters.get("above_ema200"):
        score += 0.8
    if filters.get("emas_stacked"):
        score += 1.0

    # Momentum
    rsi = filters.get("rsi")
    if isinstance(rsi, (int, float)):
        if 45 <= rsi <= 65:
            score += 1.0
        elif 40 <= rsi < 45:
            score += 0.5
        else:
            reasons.append("rsi_out_of_band")

    # Pattern quality
    pat = hit.get("pattern")
    if pat == "inside_candle":
        if hit.get("volume_compression"):
            score += 0.7
    if pat == "volume_dryup":
        if hit.get("breakout_volume_spike"):
            score += 0.7

    # Reward
    best_rr = rr.get("best_rr", 0)
    try:
        best_rr_f = float(best_rr)
    except Exception:
        best_rr_f = 0.0
    score += min(2.0, max(0.0, best_rr_f) / 2.0)  # cap contribution

    return {"score": round(score, 2), "score_notes": reasons or None}


def scan(pattern: str, watchlist: str = "nifty50", *, mode: str = "trade_ready", top: int | None = None) -> dict:
    """Scan watchlist for swing setups.

    mode:
      - trade_ready: only setups passing strict filters
      - near_setups: include pattern hits even if filters fail, with reasons
    """
    symbols = load_watchlist(watchlist)
    if not symbols:
        return {"error": f"Watchlist '{watchlist}' not found or empty"}

    data = fetch_batch(symbols, SCAN_INDICATORS)
    if "error" in data:
        return data

    detectors = {
        "inside_candle": detect_inside_candle,
        "nr7": detect_nr7,
        "volume_dryup": detect_volume_dryup,
        "engulfing": detect_engulfing,
    }
    if pattern == "all":
        active_detectors = detectors
    elif pattern in detectors:
        active_detectors = {pattern: detectors[pattern]}
    else:
        return {"error": f"Unknown pattern: {pattern}. Options: all, inside_candle, nr7, volume_dryup, engulfing"}

    mode = mode or "trade_ready"
    results = []
    for symbol, d in data.items():
        for pat_name, detector in active_detectors.items():
            hit = detector(d)
            if hit is None:
                continue
            filters = apply_filters(d)
            trade_ready = bool(filters.get("pass"))
            if mode == "trade_ready" and not trade_ready:
                continue

            rr = calc_rr(hit["entry_trigger"], hit["sl"], d)
            # Skip if best R:R < 1.5 in trade_ready mode
            if mode == "trade_ready" and rr.get("best_rr", 0) < 1.5:
                continue

            # Check for combo patterns
            combo = []
            if pat_name == "inside_candle" and detect_nr7(d):
                combo.append("nr7")
            if pat_name == "nr7" and detect_inside_candle(d):
                combo.append("inside_candle")

            score = _score_setup(hit, filters, rr)

            results.append({
                "symbol": symbol,
                "close": d.get("close"),
                "change_pct": round(d.get("change", 0), 2),
                **hit,
                "combo": combo if combo else None,
                "filters": filters,
                "trade_ready": trade_ready,
                "risk_reward": rr,
                **score,
            })

    # Sort: trade_ready first, then combos, then score
    results.sort(key=lambda x: (
        0 if x.get("trade_ready") else 1,
        -len(x.get("combo") or []),
        -(x.get("score") or 0),
    ))

    if isinstance(top, int) and top > 0:
        results = results[:top]

    return {
        "watchlist": watchlist,
        "pattern": pattern,
        "mode": mode,
        "top": top,
        "scanned": len(data),
        "setups_found": len(results),
        "setups": results,
    }


def analyze(symbol: str, timeframe: str = "daily") -> dict:
    """Full technical analysis of a single stock."""
    interval = INTERVALS.get(timeframe, Interval.INTERVAL_1_DAY)
    try:
        handler = TA_Handler(
            symbol=symbol,
            screener="india",
            exchange="NSE",
            interval=interval,
        )
        analysis = handler.get_analysis()
    except Exception as e:
        return {"error": f"Failed to analyze {symbol}: {e}"}

    d = analysis.indicators

    # Pattern detection
    patterns = []
    ic = detect_inside_candle(d)
    if ic:
        patterns.append(ic)
    nr = detect_nr7(d)
    if nr:
        patterns.append(nr)
    vd = detect_volume_dryup(d)
    if vd:
        patterns.append(vd)
    eng = detect_engulfing(d)
    if eng:
        patterns.append(eng)

    filters = apply_filters(d)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "summary": analysis.summary,
        "oscillators": analysis.oscillators,
        "moving_averages": analysis.moving_averages,
        "ohlcv": {
            "open": d.get("open"),
            "high": d.get("high"),
            "low": d.get("low"),
            "close": d.get("close"),
            "volume": d.get("volume"),
            "change_pct": round(d.get("change", 0), 2),
        },
        "indicators": {
            "rsi": round(d.get("RSI", 0), 1),
            "adx": round(d.get("ADX", 0), 1),
            "macd": round(d.get("MACD.macd", 0), 4),
            "macd_signal": round(d.get("MACD.signal", 0), 4),
            "ema20": round(d.get("EMA20", 0), 2),
            "ema50": round(d.get("EMA50", 0), 2),
            "ema200": round(d.get("EMA200", 0), 2),
            "bb_upper": round(d.get("BB.upper", 0), 2),
            "bb_lower": round(d.get("BB.lower", 0), 2),
            "vwma": round(d.get("VWMA", 0), 2),
            "pivot_s1": d.get("Pivot.M.Classic.S1"),
            "pivot_r1": d.get("Pivot.M.Classic.R1"),
        },
        "patterns_detected": patterns,
        "filters": filters,
    }


def size(symbol: str, entry: float, sl: float, capital: float, parts: int = 7, risk_pct: float = 0.015) -> dict:
    """Calculate position size per TKM rules."""
    per_trade = capital / parts
    risk_distance = abs(entry - sl)
    if risk_distance == 0:
        return {"error": "Entry and SL are the same price"}

    risk_amount = capital * risk_pct
    qty_by_risk = int(risk_amount / risk_distance)
    qty_by_alloc = int(per_trade / entry)
    qty = min(qty_by_risk, qty_by_alloc)
    investment = qty * entry
    actual_risk = qty * risk_distance

    target_2r = entry + 2 * risk_distance
    target_3r = entry + 3 * risk_distance

    return {
        "symbol": symbol,
        "entry": entry,
        "sl": sl,
        "sl_pct": round(risk_distance / entry * 100, 1),
        "capital": capital,
        "parts": parts,
        "per_trade_alloc": round(per_trade, 0),
        "max_risk_pct": risk_pct * 100,
        "max_risk_amount": round(risk_amount, 0),
        "qty_by_risk": qty_by_risk,
        "qty_by_alloc": qty_by_alloc,
        "recommended_qty": qty,
        "investment": round(investment, 0),
        "actual_risk": round(actual_risk, 0),
        "target_2r": round(target_2r, 2),
        "target_3r": round(target_3r, 2),
        "rr_at_2r": "1:2",
        "rr_at_3r": "1:3",
    }


def main():
    parser = argparse.ArgumentParser(description="Swing trade scanner")
    sub = parser.add_subparsers(dest="command")

    scan_p = sub.add_parser("scan", help="Scan watchlist for patterns")
    scan_p.add_argument("--pattern", required=True, choices=["all", "inside_candle", "nr7", "volume_dryup", "engulfing"])
    scan_p.add_argument("--watchlist", default="nifty50")
    scan_p.add_argument("--mode", default="trade_ready", choices=["trade_ready", "near_setups"], help="Filter strictness")
    scan_p.add_argument("--top", type=int, default=0, help="Return only top N results (0 = all)")

    analyze_p = sub.add_parser("analyze", help="Analyze a single stock")
    analyze_p.add_argument("--symbol", required=True)
    analyze_p.add_argument("--timeframe", default="daily", choices=list(INTERVALS.keys()))

    size_p = sub.add_parser("size", help="Calculate position size")
    size_p.add_argument("--symbol", required=True)
    size_p.add_argument("--entry", required=True, type=float)
    size_p.add_argument("--sl", required=True, type=float)
    size_p.add_argument("--capital", required=True, type=float)
    size_p.add_argument("--parts", default=7, type=int)
    size_p.add_argument("--risk-pct", default=1.5, type=float)

    args = parser.parse_args()

    if args.command == "scan":
        top = args.top if args.top and args.top > 0 else None
        result = scan(args.pattern, args.watchlist, mode=args.mode, top=top)
    elif args.command == "analyze":
        result = analyze(args.symbol, args.timeframe)
    elif args.command == "size":
        result = size(args.symbol, args.entry, args.sl, args.capital, args.parts, args.risk_pct / 100)
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
