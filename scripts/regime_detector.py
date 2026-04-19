#!/usr/bin/env python3
"""
Regime Detector — Phase B.1

Beantwortet: In welchem Markt-Regime handelt der Bot gerade — und soll er
sein Risiko anpassen oder pausieren?

Inputs:
  - BTC 30d-Trend (close heute vs. close 30d zurück in %)
  - BTC 7d-Trend (für crash-Check)
  - BTC ATR(14) auf 4h-Candles als % des aktuellen Preises (Vol-Proxy)
  - Fear & Greed Index (alternative.me, Tageswert)

Klassifikation:
  Regime       BTC 30d        ATR%   F&G      Risk-Mod
  bull_quiet   > +5%          <2%    >60      1.00
  bull_vol     > +5%          >=2%   >50      0.75
  sideways     ±5%            *      40-60    0.50
  bear_quiet   < -5%          <2%    <40      0.50
  bear_vol     < -5%          >=2%   <30      0.25
  crash        BTC 7d < -15%  *      <20      0.00 (NO-TRADE)

Bei widersprüchlichen Signalen wird die konservativere Klasse gewählt.

Verwendung: python3 regime_detector.py [--json]
Integration: /ASS Schritt 0.5 (Snapshot), autonomous_trade.py (Risk-Mod).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bitget_client import BitgetClient  # type: ignore  # noqa: E402

CACHE_FILE = Path("/root/apex-trading-bot/data/regime_state.json")
CACHE_TTL_SEC = 3600  # 1h — Regime ändert sich langsam


def _fetch_fear_and_greed() -> dict:
    """F&G Index, kostenlos, kein API-Key. Fallback: leer."""
    try:
        url = "https://api.alternative.me/fng/?limit=1&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "APEX-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        item = (data.get("data") or [{}])[0]
        return {"value": int(item["value"]), "label": item.get("value_classification", "")}
    except Exception:
        return {}


def _calc_atr_pct(candles: list[dict], period: int = 14) -> float:
    """ATR über period, ausgedrückt als % des letzten Close."""
    if len(candles) < period + 1:
        return float("nan")
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = mean(trs[-period:])
    last_close = candles[-1]["close"]
    return (atr / last_close) * 100 if last_close else float("nan")


def _pct_change(old: float, new: float) -> float:
    return ((new - old) / old) * 100 if old else 0.0


def _load_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        age = __import__("time").time() - data.get("_ts", 0)
        if age < CACHE_TTL_SEC:
            return data
    except Exception:
        pass
    return None


def _save_cache(result: dict) -> None:
    result["_ts"] = __import__("time").time()
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2))
    os.replace(tmp, CACHE_FILE)


def classify(btc_30d: float, btc_7d: float, atr_pct: float, fg: int | None) -> tuple[str, float]:
    """Gibt (regime, risk_modifier) zurück.

    Regel: Trend (BTC 30d) UND Sentiment (F&G) müssen zusammenpassen damit ein
    starkes Regime greift. Widerspruch → `sideways` (konservativer Default).
    """
    # Crash-Check hat Vorrang
    if btc_7d < -15 or (fg is not None and fg < 20):
        return ("crash", 0.0)
    # Signale
    if btc_30d > 5:
        trend = "bull"
    elif btc_30d < -5:
        trend = "bear"
    else:
        trend = "side"
    if fg is None:
        sent = "neutral"
    elif fg > 60:
        sent = "greed"
    elif fg < 40:
        sent = "fear"
    else:
        sent = "neutral"
    vol_high = atr_pct >= 2.0 if atr_pct == atr_pct else False
    # Bull-Regime nur wenn Sentiment nicht aktiv dagegen spricht
    if trend == "bull" and sent in ("greed", "neutral"):
        return ("bull_vol", 0.75) if vol_high else ("bull_quiet", 1.0)
    if trend == "bear" and sent in ("fear", "neutral"):
        return ("bear_vol", 0.25) if vol_high else ("bear_quiet", 0.5)
    # Alles andere: widersprüchliche Signale oder echtes sideways
    return ("sideways", 0.5)


def detect(use_cache: bool = True) -> dict:
    if use_cache:
        cached = _load_cache()
        if cached:
            return cached
    client = BitgetClient(dry_run=True)  # nur public endpoints nötig
    # 4h-Candles für BTC: 30d = 180 × 4h; 7d = 42 × 4h. Hole 210 für Puffer.
    candles_4h = client.get_candles("BTC", interval="4h", limit=210)
    if len(candles_4h) < 180:
        return {"regime": "unknown", "risk_modifier": 0.5, "go": True,
                "reason": f"zu wenig BTC-Candles ({len(candles_4h)})", "error": True}
    now_close = candles_4h[-1]["close"]
    close_30d = candles_4h[-180]["close"]
    close_7d = candles_4h[-42]["close"]
    btc_30d = _pct_change(close_30d, now_close)
    btc_7d = _pct_change(close_7d, now_close)
    atr_pct = _calc_atr_pct(candles_4h, period=14)
    fg_data = _fetch_fear_and_greed()
    fg_val = fg_data.get("value") if fg_data else None
    regime, risk_mod = classify(btc_30d, btc_7d, atr_pct, fg_val)
    go = risk_mod > 0
    reason_parts = [f"BTC 30d {btc_30d:+.1f}%", f"7d {btc_7d:+.1f}%",
                    f"ATR% {atr_pct:.2f}"]
    if fg_val is not None:
        reason_parts.append(f"F&G {fg_val} ({fg_data.get('label', '?')})")
    result = {
        "regime": regime,
        "risk_modifier": risk_mod,
        "go": go,
        "btc_30d_pct": round(btc_30d, 2),
        "btc_7d_pct": round(btc_7d, 2),
        "atr_pct": round(atr_pct, 3),
        "fear_greed": fg_val,
        "fear_greed_label": fg_data.get("label") if fg_data else None,
        "reason": " | ".join(reason_parts),
    }
    _save_cache(result)
    return result


def render(r: dict) -> str:
    go_flag = "✅ GO" if r["go"] else "🛑 NO-TRADE"
    lines = [
        f"Regime: {r['regime']:<12} Risk-Mod: {r['risk_modifier']:.2f}  {go_flag}",
        f"  {r['reason']}",
    ]
    return "\n".join(lines)


def main() -> int:
    no_cache = "--no-cache" in sys.argv
    r = detect(use_cache=not no_cache)
    if "--json" in sys.argv:
        print(json.dumps(r, indent=2))
    else:
        print(render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
