#!/usr/bin/env python3
"""
VAA Asset Scanner — Phase-3-Extension für neue Assets.

Testet beliebige Assets gegen die validierten VAA-Parameter:
  Vol>2.5x / Body<0.6x / ATR-Expansion / TP=3R / SHORT-only / 1H

Klassifikation pro Asset:
  KEEP    → n≥30, AvgR>0, PF>1.3, OOS positiv, ≥3/4 Quartale positiv
  TOXIC   → AvgR < -0.1R oder OOS < -0.2R oder alle Quartale negativ
  NEUTRAL → alles andere

Verwendung:
  python3 scripts/backtest/vaa_asset_scan.py
  python3 scripts/backtest/vaa_asset_scan.py --download  # lädt fehlende Daten
"""
import math
import os
import sys
import time

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_DIR)

from scripts.backtest.pdhl_backtest import load_csv, TAKER_FEE, SLIPPAGE
from scripts.backtest.squeeze_scout  import aggregate_1h
from scripts.backtest.vaa_scout      import build_indicators
from scripts.backtest.vaa_phase3     import build_extended_indicators
from datetime import datetime, timezone

DATA_DIR = os.path.join(PROJECT_DIR, "data", "historical")

IS_START  = "2025-04-21"
IS_END    = "2026-02-10"
OOS_START = "2026-02-11"
OOS_END   = "2026-04-19"

VOL_MULT   = 2.5
BODY_MULT  = 0.6
TP_R       = 3.0
ENTRY_WIN  = 3    # Stunden Sell-Stop gültig
WARMUP     = 75   # max(vol_sma=50, body_sma=50, ema20=20, atr_sma20=34) + buffer

# Assets bereits validiert (Phase 3) — nicht nochmal testen
ALREADY_KEEP    = ["SOL", "AVAX", "DOGE", "ADA", "SUI", "AAVE"]
ALREADY_TOXIC   = ["ETH", "LINK"]

# Neue Kandidaten — in Prioritätsreihenfolge (Liquidität, Volatilität)
SCAN_CANDIDATES = [
    # Bereits Daten vorhanden:
    "BTC", "XRP",
    # Download nötig (liquide Bitget USDT-Perps mit guter Vol):
    "BNB", "OP", "ARB", "INJ", "NEAR", "APT", "TIA",
    "WIF", "BONK", "PEPE", "JUP", "SEI", "LDO",
]


# ─── Backtest-Kern ────────────────────────────────────────────────────────────

def run_vaa_asset(candles_1h: list, start: str, end: str) -> list[float]:
    """
    VAA SHORT-only Backtest mit F-06 ATR-Expansion Filter.
    Gibt Liste von R-Ergebnissen zurück.
    """
    inds     = build_extended_indicators(candles_1h)
    pending  = []
    in_trade = False
    trade    = {}
    results  = []

    for i, c in enumerate(candles_1h):
        dt  = datetime.fromtimestamp(c["time"] / 1000, tz=timezone.utc)
        day = dt.strftime("%Y-%m-%d")

        # ── Offene Position managen ───────────────────────────────────────
        if in_trade:
            ae, sl, tp, risk = trade["ae"], trade["sl"], trade["tp"], trade["risk"]
            hit_sl = c["high"] >= sl
            hit_tp = c["low"]  <= tp

            if hit_sl and not hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                results.append(-1.0 - fee_r)
                in_trade = False
                continue

            if hit_tp:
                fee_r = (2 * ae * TAKER_FEE) / risk
                results.append(TP_R - fee_r)
                in_trade = False
                continue
            continue

        # ── Pending Sell-Stops prüfen ─────────────────────────────────────
        if pending:
            triggered = []
            for p in pending:
                if i > p["expiry"]:
                    continue
                if day < start or day > end:
                    continue
                if c["low"] <= p["stop"]:
                    ae   = p["stop"] * (1 - SLIPPAGE)
                    sl   = p["sl"]
                    risk = sl - ae
                    if risk <= 0 or risk / ae < 0.001 or risk / ae > 0.25:
                        continue
                    tp = ae - TP_R * risk
                    in_trade = True
                    trade = {"ae": ae, "sl": sl, "tp": tp, "risk": risk}
                    triggered.append(p)
                    break
            pending = [p for p in pending
                       if p not in triggered and i <= p["expiry"]]

        if in_trade:
            continue

        # ── Neue Anomalie suchen ──────────────────────────────────────────
        if day < start or day > end or i < WARMUP:
            continue

        ind = inds[i]
        if ind["vol_sma"] <= 0 or ind["body_sma"] <= 0 or ind["ema20"] <= 0:
            continue

        vol_ratio  = c["volume"] / ind["vol_sma"]
        body_ratio = ind["body"] / ind["body_sma"]

        if not (vol_ratio > VOL_MULT and body_ratio < BODY_MULT):
            continue

        # F-06: ATR-Expansion
        if not (ind.get("atr_sma20", 0) > 0 and
                ind.get("atr14", 0) > 1.2 * ind.get("atr_sma20", 1)):
            continue

        # SHORT-only: Close über EMA20
        if c["close"] > ind["ema20"]:
            sl = c["high"]
            approx_risk = sl - c["low"]
            if approx_risk > 0 and approx_risk / c["low"] < 0.25:
                pending.append({
                    "stop":   c["low"],
                    "sl":     round(sl, 6),
                    "expiry": i + ENTRY_WIN,
                })

    return results


def kpis(r_list: list) -> dict:
    n = len(r_list)
    if n == 0:
        return {"n":0,"avg_r":0.0,"wr":0.0,"total_r":0.0,
                "pf":0.0,"max_dd":0.0,"p":1.0}
    wins  = [r for r in r_list if r > 0]
    total = sum(r_list)
    gw    = sum(wins)
    gl    = abs(sum(r for r in r_list if r < 0))
    mean  = total / n
    sd    = math.sqrt(sum((r-mean)**2 for r in r_list)/(n-1)) if n > 1 else 0
    peak = cum = dd = 0.0
    for r in r_list:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    def erfc(x):
        t_ = 1/(1+0.3275911*abs(x))
        p  = t_*(0.254829592+t_*(-0.284496736+t_*(1.421413741+
              t_*(-1.453152027+t_*1.061405429))))
        return p * math.exp(-x*x)
    p = erfc(abs(t)/math.sqrt(2)) if t != 0 else 1.0
    return {"n":n,"avg_r":mean,"wr":len(wins)/n,"total_r":total,
            "pf":gw/gl if gl>0 else float("inf"),
            "max_dd":dd,"p":p}


def quarterly_breakdown(candles_1h: list) -> list[float]:
    """Avg R pro Quartal (Q1-Q4 2025/2026)."""
    quarters = [
        ("2025-04-21", "2025-07-20"),
        ("2025-07-21", "2025-10-20"),
        ("2025-10-21", "2026-01-20"),
        ("2026-01-21", "2026-04-19"),
    ]
    results = []
    for qs, qe in quarters:
        r = run_vaa_asset(candles_1h, qs, qe)
        results.append(sum(r) if r else 0.0)
    return results


def classify(asset: str, is_kpis: dict, oos_kpis: dict,
             quarterly: list) -> str:
    """KEEP / TOXIC / NEUTRAL Klassifikation."""
    n          = is_kpis["n"]
    avg_r      = is_kpis["avg_r"]
    pf         = is_kpis["pf"]
    oos_avg_r  = oos_kpis["avg_r"]
    oos_n      = oos_kpis["n"]
    q_positive = sum(1 for q in quarterly if q > 0)

    # TOXIC-Kriterien (mind. eines reicht)
    if avg_r < -0.1 or oos_avg_r < -0.2 or q_positive == 0:
        return "TOXIC"

    # KEEP-Kriterien (alle müssen erfüllt sein)
    if (n >= 30 and avg_r > 0 and pf > 1.3
            and oos_avg_r > 0 and oos_n >= 3
            and q_positive >= 3):
        return "KEEP"

    return "NEUTRAL"


# ─── Download ─────────────────────────────────────────────────────────────────

def download_asset(asset: str) -> bool:
    """Lädt 1H-Daten via candle_downloader wenn 15m-Daten fehlen."""
    path_15m = os.path.join(DATA_DIR, f"{asset}_15m.csv")
    if os.path.exists(path_15m):
        return True

    print(f"  → Downloading {asset} ...")
    try:
        import subprocess
        result = subprocess.run(
            [os.path.join(PROJECT_DIR, "venv", "bin", "python3"),
             os.path.join(SCRIPT_DIR, "candle_downloader.py"),
             "--assets", asset, "--intervals", "15m"],
            capture_output=True, text=True, cwd=PROJECT_DIR, timeout=180
        )
        if result.returncode == 0 and os.path.exists(path_15m):
            print(f"  ✅ {asset} heruntergeladen")
            return True
        else:
            print(f"  ❌ {asset}: {result.stderr[:100]}")
            return False
    except Exception as e:
        print(f"  ❌ {asset}: {e}")
        return False


# ─── Haupt-Scanner ────────────────────────────────────────────────────────────

def scan_asset(asset: str) -> dict | None:
    """Scannt einen Asset und gibt Ergebnis-Dict zurück."""
    path_15m = os.path.join(DATA_DIR, f"{asset}_15m.csv")
    if not os.path.exists(path_15m):
        return None

    try:
        raw      = load_csv(asset, "15m")
        candles  = aggregate_1h(raw)
    except Exception as e:
        print(f"  ⚠️  {asset}: Ladefehler — {e}")
        return None

    if len(candles) < 200:
        print(f"  ⚠️  {asset}: Zu wenig Candles ({len(candles)})")
        return None

    # IS-Backtest
    is_r   = run_vaa_asset(candles, IS_START, IS_END)
    is_k   = kpis(is_r)

    # OOS-Backtest
    oos_r  = run_vaa_asset(candles, OOS_START, OOS_END)
    oos_k  = kpis(oos_r)

    # Quartals-Breakdown
    quarterly = quarterly_breakdown(candles)

    # Klassifikation
    verdict = classify(asset, is_k, oos_k, quarterly)

    return {
        "asset":      asset,
        "verdict":    verdict,
        "is_n":       is_k["n"],
        "is_avg_r":   round(is_k["avg_r"], 3),
        "is_wr":      round(is_k["wr"] * 100, 1),
        "is_pf":      round(is_k["pf"], 2),
        "is_max_dd":  round(is_k["max_dd"], 2),
        "oos_n":      oos_k["n"],
        "oos_avg_r":  round(oos_k["avg_r"], 3),
        "quarterly":  [round(q, 2) for q in quarterly],
        "q_positive": sum(1 for q in quarterly if q > 0),
    }


def print_result(r: dict):
    icons = {"KEEP": "✅", "TOXIC": "❌", "NEUTRAL": "🟡"}
    q_str = " ".join(f"{q:+.1f}" for q in r["quarterly"])
    print(
        f"  {icons[r['verdict']]} {r['asset']:<6}  "
        f"IS: n={r['is_n']:>3}  AvgR={r['is_avg_r']:>+.3f}  "
        f"WR={r['is_wr']:>4.1f}%  PF={r['is_pf']:>4.1f}  DD={r['is_max_dd']:.1f}R  "
        f"| OOS: n={r['oos_n']:>2}  AvgR={r['oos_avg_r']:>+.3f}  "
        f"| Q: [{q_str}]  {r['q_positive']}/4"
    )


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true",
                        help="Fehlende Assets herunterladen")
    parser.add_argument("--assets", nargs="+", default=None,
                        help="Nur diese Assets scannen")
    args = parser.parse_args()

    candidates = args.assets if args.assets else SCAN_CANDIDATES

    print(f"\n{'═'*90}")
    print(f"  VAA Asset Scanner — Validierte Parameter: "
          f"Vol>{VOL_MULT}x / Body<{BODY_MULT}x / TP={TP_R}R / F-06 ATR-Expansion")
    print(f"  IS: {IS_START}→{IS_END}  |  OOS: {OOS_START}→{OOS_END}")
    print(f"{'═'*90}")
    print(f"\n  Bereits validiert — KEEP:  {ALREADY_KEEP}")
    print(f"  Bereits validiert — TOXIC: {ALREADY_TOXIC}")
    print(f"\n  Scanne {len(candidates)} Kandidaten ...\n")

    keep    = []
    toxic   = []
    neutral = []
    missing = []

    for asset in candidates:
        if asset in ALREADY_KEEP:
            print(f"  ⏭️  {asset}: bereits KEEP — skip")
            continue
        if asset in ALREADY_TOXIC:
            print(f"  ⏭️  {asset}: bereits TOXIC — skip")
            continue

        path = os.path.join(DATA_DIR, f"{asset}_15m.csv")
        if not os.path.exists(path):
            if args.download:
                ok = download_asset(asset)
                if not ok:
                    missing.append(asset)
                    continue
            else:
                print(f"  ⬇️  {asset}: keine Daten (--download zum Herunterladen)")
                missing.append(asset)
                continue

        print(f"  Scanne {asset} ...", end="", flush=True)
        result = scan_asset(asset)
        if result is None:
            print(" ⚠️  Fehler")
            continue

        print()
        print_result(result)

        if result["verdict"] == "KEEP":
            keep.append(result)
        elif result["verdict"] == "TOXIC":
            toxic.append(result)
        else:
            neutral.append(result)

    # ── Zusammenfassung ───────────────────────────────────────────────────────
    print(f"\n{'═'*90}")
    print("  ERGEBNIS\n")

    print(f"  ✅ KEEP ({len(keep)}):")
    if keep:
        for r in sorted(keep, key=lambda x: -x["is_avg_r"]):
            print(f"     {r['asset']:<6}  AvgR={r['is_avg_r']:>+.3f}  "
                  f"OOS={r['oos_avg_r']:>+.3f}  Q: {r['q_positive']}/4")
    else:
        print("     —")

    print(f"\n  🟡 NEUTRAL ({len(neutral)}):")
    if neutral:
        for r in sorted(neutral, key=lambda x: -x["is_avg_r"]):
            print(f"     {r['asset']:<6}  AvgR={r['is_avg_r']:>+.3f}  "
                  f"OOS={r['oos_avg_r']:>+.3f}  Q: {r['q_positive']}/4")
    else:
        print("     —")

    print(f"\n  ❌ TOXIC ({len(toxic)}):")
    if toxic:
        for r in sorted(toxic, key=lambda x: x["is_avg_r"]):
            print(f"     {r['asset']:<6}  AvgR={r['is_avg_r']:>+.3f}  "
                  f"OOS={r['oos_avg_r']:>+.3f}  Q: {r['q_positive']}/4")
    else:
        print("     —")

    if missing:
        print(f"\n  ⬇️  Daten fehlen ({len(missing)}): {missing}")
        print(f"     → python3 scripts/backtest/vaa_asset_scan.py --download")

    # ── Empfehlung ────────────────────────────────────────────────────────────
    all_keep = ALREADY_KEEP + [r["asset"] for r in keep]
    all_toxic = ALREADY_TOXIC + [r["asset"] for r in toxic]
    calls_per_min_peak = len(all_keep) * 2 + 4
    api_ok = "✅" if calls_per_min_peak < 44 else "⚠️ "

    print(f"\n{'═'*90}")
    print(f"  EMPFEHLUNG\n")
    print(f"  Finales VAA-Universum ({len(all_keep)} Assets): {all_keep}")
    print(f"  Blacklist ({len(all_toxic)} Assets):             {all_toxic}")
    print(f"  API-Last: ~{calls_per_min_peak} Calls/min peak  {api_ok}")
    print(f"\n  Config-Update für bot_config.py:")
    print(f"  VAA_ASSETS    = {all_keep}")
    print(f"  VAA_BLACKLIST = {all_toxic}")
    print(f"{'═'*90}\n")


if __name__ == "__main__":
    main()
