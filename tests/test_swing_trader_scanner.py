from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_scanner_module():
    repo_root = Path(__file__).resolve().parents[1]
    scanner_path = repo_root / "skills" / "swing-trader" / "scripts" / "scanner.py"
    spec = importlib.util.spec_from_file_location("swing_trader_scanner", scanner_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_detect_volume_dryup_declining_into_recent_day():
    m = _load_scanner_module()
    d = {
        "volume": 150,
        "volume[1]": 100,
        "volume[2]": 120,
        "volume[3]": 140,
        "high": 110,
        "low[1]": 90,
        "low[2]": 92,
        "low[3]": 94,
    }
    hit = m.detect_volume_dryup(d)
    assert hit is not None
    assert hit["pattern"] == "volume_dryup"


def test_calc_rr_includes_best_rr():
    m = _load_scanner_module()
    d = {"Pivot.M.Classic.R1": 110, "Pivot.M.Classic.R2": 120}
    rr = m.calc_rr(100, 95, d)
    assert "best_rr" in rr
    assert rr["best_rr"] >= 2.0
