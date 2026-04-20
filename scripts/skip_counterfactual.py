#!/usr/bin/env python3
"""
Skip-Counterfactual: Simuliert Trade-Outcome für gefilterte Signale.

Für jeden Skip mit vollständigen Box-/Entry-Daten wird mit historischen 5m-Candles
rekonstruiert welches R-Multiple der Trade erzielt hätte (SL an Box-Boundary,
TP1 @ 1R / TP2 @ 3R, Split 50/50, BE bei TP1).

Output: data/skip_counterfactual_log.jsonl (append, idempotent via skip-ID)
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from bitget_client import BitgetClient

DATA_DIR           = os.path.join(os.path.dirname(SCRIPT_DIR), "data")
SKIP_LOG           = os.path.join(DATA_DIR, "skip_log.jsonl")
COUNTERFACTUAL_LOG = os.path.join(DATA_DIR, "skip_counterfactual_log.jsonl")

RELEVANT_REASONS = {
    "late_entry", "weak_candle", "low_volume",
    "ema200_misaligned", "ema200_h4_misaligned",
}

SIM_HOURS    = 4           # Simulations-Fenster pro Skip
CANDLE_COUNT = SIM_HOURS * 12  # 5m-Candles (12 pro Stunde)


def skip_id(skip: dict) -> str:
    return f"{skip.get('ts','')}|{skip.get('asset','')}|{skip.get('reason','')}"


def load_existing_ids() -> set:
    if not os.path.exists(COUNTERFACTUAL_LOG):
        return set()
    ids = set()
    with open(COUNTERFACTUAL_LOG) as f:
        for line in f:
            try:
                e = json.loads(line)
                ids.add(e["skip_id"])
            except Exception:
                continue
    return ids


def qualifies(skip: dict) -> bool:
    if skip.get("reason") not in RELEVANT_REASONS:
        return False
    ctx = skip.get("context") or {}
    return all(k in ctx and ctx[k] is not None for k in ("box_high", "box_low", "entry_price", "direction"))


def simulate(skip: dict, candles: list) -> dict:
    """
    Rekonstruiert Trade-Verlauf auf 5m-Candles.
    Returns dict mit r_outcome, exit_reason, bars_to_exit.
    """
    ctx = skip["context"]
    direction  = ctx["direction"]
    entry      = float(ctx["entry_price"])
    box_high   = float(ctx["box_high"])
    box_low    = float(ctx["box_low"])
    box_range  = box_high - box_low
    sl_buffer  = max(box_range * 0.1, entry * 0.001)

    if direction == "long":
        sl = box_low - sl_buffer
        r_size = entry - sl
        tp1 = entry + r_size * 1.0
        tp2 = entry + r_size * 3.0
    else:
        sl = box_high + sl_buffer
        r_size = sl - entry
        tp1 = entry - r_size * 1.0
        tp2 = entry - r_size * 3.0

    if r_size <= 0:
        return {"r_outcome": None, "exit_reason": "invalid_setup", "bars_to_exit": 0}

    tp1_hit = False
    # Nach TP1: SL auf Entry (BE)
    active_sl = sl

    for i, c in enumerate(candles):
        high = c["high"]
        low  = c["low"]

        if direction == "long":
            # Konservativ: SL-first wenn beides in derselben Candle
            if low <= active_sl:
                if tp1_hit:
                    # Halbe Position stoppt bei BE, andere Hälfte schon bei TP1 ausgestiegen
                    return {"r_outcome": 0.5 * 1.0 + 0.5 * 0.0, "exit_reason": "be_after_tp1", "bars_to_exit": i + 1}
                return {"r_outcome": -1.0, "exit_reason": "sl", "bars_to_exit": i + 1}
            if not tp1_hit and high >= tp1:
                tp1_hit = True
                active_sl = entry
                if high >= tp2:
                    return {"r_outcome": 0.5 * 1.0 + 0.5 * 3.0, "exit_reason": "tp2", "bars_to_exit": i + 1}
            elif tp1_hit and high >= tp2:
                return {"r_outcome": 0.5 * 1.0 + 0.5 * 3.0, "exit_reason": "tp2", "bars_to_exit": i + 1}
        else:  # short
            if high >= active_sl:
                if tp1_hit:
                    return {"r_outcome": 0.5 * 1.0 + 0.5 * 0.0, "exit_reason": "be_after_tp1", "bars_to_exit": i + 1}
                return {"r_outcome": -1.0, "exit_reason": "sl", "bars_to_exit": i + 1}
            if not tp1_hit and low <= tp1:
                tp1_hit = True
                active_sl = entry
                if low <= tp2:
                    return {"r_outcome": 0.5 * 1.0 + 0.5 * 3.0, "exit_reason": "tp2", "bars_to_exit": i + 1}
            elif tp1_hit and low <= tp2:
                return {"r_outcome": 0.5 * 1.0 + 0.5 * 3.0, "exit_reason": "tp2", "bars_to_exit": i + 1}

    # Timeout: Mark-to-Market auf letztem Close
    if not candles:
        return {"r_outcome": None, "exit_reason": "no_data", "bars_to_exit": 0}

    last_close = candles[-1]["close"]
    if direction == "long":
        mtm_r = (last_close - entry) / r_size
    else:
        mtm_r = (entry - last_close) / r_size
    # Halbe Position ggf. schon bei TP1 raus
    if tp1_hit:
        final_r = 0.5 * 1.0 + 0.5 * mtm_r
        reason = "timeout_after_tp1"
    else:
        final_r = mtm_r
        reason = "timeout"
    return {"r_outcome": round(final_r, 3), "exit_reason": reason, "bars_to_exit": len(candles)}


def main():
    if not os.path.exists(SKIP_LOG):
        print("⚠️  skip_log.jsonl nicht gefunden")
        return

    existing_ids = load_existing_ids()
    client = BitgetClient(dry_run=True)  # nur Public-Endpoints

    processed = 0
    skipped_existing = 0
    skipped_qualify = 0
    appended = 0

    with open(SKIP_LOG) as f:
        skips = [json.loads(line) for line in f if line.strip()]

    todo = []
    for s in skips:
        if not qualifies(s):
            skipped_qualify += 1
            continue
        sid = skip_id(s)
        if sid in existing_ids:
            skipped_existing += 1
            continue
        todo.append(s)

    print(f"📊 Skip-Counterfactual: {len(skips)} Skips gesamt | {len(todo)} qualifizieren (neu)")
    if skipped_existing:
        print(f"   (übersprungen: {skipped_existing} bereits simuliert, {skipped_qualify} unqualifiziert)")

    for s in todo:
        try:
            ts = datetime.fromisoformat(s["ts"])
            start_ms = int(ts.timestamp() * 1000)
            end_ms = start_ms + SIM_HOURS * 3600 * 1000
            asset = s.get("asset")
            candles = client.get_candles(asset, interval="5m", limit=CANDLE_COUNT,
                                         start_time=start_ms, end_time=end_ms)
            # Bitget liefert nur Closed-Candles — filter auf unseren Zeitraum
            candles = [c for c in candles if start_ms <= c["time"] <= end_ms]
            result = simulate(s, candles)

            entry = {
                "skip_id": skip_id(s),
                "simulated_at": datetime.now().isoformat(),
                "ts": s["ts"],
                "asset": asset,
                "session": s.get("session"),
                "reason": s["reason"],
                "direction": s["context"]["direction"],
                "entry_price": s["context"]["entry_price"],
                "box_high": s["context"]["box_high"],
                "box_low": s["context"]["box_low"],
                "candles_found": len(candles),
                **result,
            }
            with open(COUNTERFACTUAL_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
            appended += 1
            processed += 1

            if processed % 10 == 0:
                print(f"   ... {processed}/{len(todo)} simuliert")
            time.sleep(0.15)  # Rate-Limit-Puffer

        except Exception as e:
            print(f"   ⚠️  Skip {skip_id(s)}: {e}")
            continue

    print(f"\n✅ Neu simuliert: {appended} | Log: {COUNTERFACTUAL_LOG}")

    # Kurze Aggregat-Zusammenfassung
    if os.path.exists(COUNTERFACTUAL_LOG):
        rows = []
        with open(COUNTERFACTUAL_LOG) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        if rows:
            by_reason = {}
            for r in rows:
                if r.get("r_outcome") is None:
                    continue
                by_reason.setdefault(r["reason"], []).append(r["r_outcome"])
            print(f"\n📈 Aggregat (n={sum(len(v) for v in by_reason.values())}):")
            print(f"   {'Reason':<22} {'n':>4} {'AvgR':>7}")
            for reason, vals in sorted(by_reason.items()):
                avg = sum(vals) / len(vals)
                print(f"   {reason:<22} {len(vals):>4} {avg:>+7.2f}R")


if __name__ == "__main__":
    main()
