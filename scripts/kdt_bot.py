#!/usr/bin/env python3
"""
KDT Live Bot — Kinetic Deceleration Trap, ETH SHORT-Only, 1H.

Edge: 3 aufeinanderfolgende grüne Kerzen mit schrumpfendem Body + Volumen
über EMA(50) → kinetische Erschöpfung → SHORT wenn Preis das Low bricht.

Läuft jede volle Stunde via Cron:
  0 * * * *   cd /root/apex-trading-bot && venv/bin/python scripts/kdt_bot.py >> logs/kdt.log 2>&1

Validierungsstand (2026-04-24):
  IS  : n=17  AvgR=+0.450R  WR=41%  PF=1.64  (2025-04-21→2026-02-10)
  OOS : n=4   AvgR=+0.824R  WR=50%  PF=2.48  (2026-02-11→2026-04-19)
  Phase 4 Gates: 4/6 (DSR + Bootstrap offen wegen n=17)
  → Forward-Testing bis n≥30 Live-Signale für finale Validierung

Flow:
  1. Pending Sell-Stop prüfen → bei ETH-Low-Unterschreitung: Market-Short
  2. Neue KDT-Setups auf ETH scannen → bei Signal: Pending speichern
  3. Telegram-Report
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from config.bot_config import (
    KDT_ENABLED, KDT_DRY_RUN, KDT_ASSET,
    KDT_EMA_PERIOD, KDT_ENTRY_WINDOW, KDT_TP_R, KDT_MAX_RISK_PCT,
    KDT_CANDLE_LIMIT, KDT_SL_ATR_MULT,
    LEVERAGE, SIZE_DECIMALS, DATA_DIR,
)
from scripts.bitget_client import BitgetClient
from scripts.telegram_sender import send_telegram_message

PENDING_FILE = os.path.join(DATA_DIR, "kdt_pending.json")
TRADES_FILE  = os.path.join(DATA_DIR, "kdt_trades.json")
LOG_PREFIX   = "[KDT]"


# ─── Indikatoren ──────────────────────────────────────────────────────────────

def _ema(values: list, period: int) -> float:
    if len(values) < period:
        return 0.0
    k   = 2 / (period + 1)
    val = sum(values[:period]) / period
    for v in values[period:]:
        val = v * k + val * (1 - k)
    return val


def _atr_wilder(candles: list, period: int = 14) -> float:
    """Wilder's ATR auf den letzten Candles."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-(period * 2):]
    if not window:
        return 0.0
    atr = sum(window[:period]) / period
    for tr in window[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_indicators(candles: list) -> dict:
    closes = [c["close"] for c in candles]
    ema50  = _ema(closes, KDT_EMA_PERIOD)
    atr14  = _atr_wilder(candles, 14)
    return {"ema50": ema50, "atr14": atr14}


# ─── Signal-Erkennung ─────────────────────────────────────────────────────────

def check_kdt_signal(candles: list, ind: dict) -> dict | None:
    """
    Prüft die letzten 3 abgeschlossenen Kerzen auf KDT-SHORT-Setup.
    Gibt Signal-Dict zurück oder None.

    Bedingungen:
      1. Close > EMA(50)           — kurzfristiger Aufwärtstrend
      2. Alle 3 Kerzen grün        — Close > Open
      3. Schrumpfende Bodies       — Body[0] < Body[1] < Body[2]
      4. Schrumpfendes Volumen     — Vol[0] < Vol[1] < Vol[2]
      5. F-04 Tight-SL             — SL-Distanz < 1.0 × ATR(14)
    """
    if len(candles) < 3:
        return None

    c0, c1, c2 = candles[-1], candles[-2], candles[-3]
    e    = ind["ema50"]
    atr  = ind["atr14"]

    if e <= 0 or atr <= 0:
        return None

    body0 = abs(c0["close"] - c0["open"])
    body1 = abs(c1["close"] - c1["open"])
    body2 = abs(c2["close"] - c2["open"])

    if body0 <= 0:
        return None

    # Alle 3 grün
    if not (c0["close"] > c0["open"] and
            c1["close"] > c1["open"] and
            c2["close"] > c2["open"]):
        return None

    # Schrumpfende Bodies + Volumen
    if not (body0 < body1 < body2):
        return None
    if not (c0["volume"] < c1["volume"] < c2["volume"]):
        return None

    # Trend-Kontext: Close über EMA(50)
    if c0["close"] <= e:
        return None

    # SL und Stop definieren
    sl_price  = c0["high"]
    stop      = c0["low"]
    sl_dist   = sl_price - stop

    # F-04: Tight-SL
    if sl_dist >= KDT_SL_ATR_MULT * atr:
        return None

    # Plausibilitäts-Check
    if sl_dist <= 0 or sl_dist / stop < 0.0005 or sl_dist / stop > 0.15:
        return None

    return {
        "stop_price":   round(stop, 4),
        "sl":           round(sl_price, 4),
        "sl_dist":      round(sl_dist, 4),
        "atr14":        round(atr, 4),
        "ema50":        round(e, 4),
        "body0":        round(body0, 4),
        "body_ratio":   round(body0 / body1, 3),
        "vol_ratio":    round(c0["volume"] / c1["volume"], 3),
        "candle_time":  c0["time"],
    }


# ─── Persistent Storage ───────────────────────────────────────────────────────

def load_pending() -> list:
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_pending(pending: list):
    tmp = PENDING_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pending, f, indent=2)
    os.replace(tmp, PENDING_FILE)


def load_trades() -> list:
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_trade(trade: dict):
    trades = load_trades()
    trades.append(trade)
    tmp = TRADES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trades, f, indent=2)
    os.replace(tmp, TRADES_FILE)


# ─── Position-Sizing ──────────────────────────────────────────────────────────

def calc_size(client: BitgetClient, entry: float, sl: float) -> tuple[float, float]:
    balance  = client.get_balance()
    risk_usd = balance * KDT_MAX_RISK_PCT
    sl_dist  = abs(entry - sl)
    if sl_dist <= 0:
        return 0.0, 0.0

    raw_size = risk_usd / sl_dist
    max_size = (balance * LEVERAGE * 0.90) / entry
    size     = min(raw_size, max_size)

    dec  = SIZE_DECIMALS.get(KDT_ASSET, 2)
    size = math.floor(size * 10**dec) / 10**dec
    return size, round(risk_usd, 4)


# ─── Order-Ausführung ─────────────────────────────────────────────────────────

def execute_short(client: BitgetClient, signal: dict, current_price: float) -> bool:
    entry = current_price
    sl    = signal["sl"]
    risk  = sl - entry

    if risk <= 0:
        print(f"{LOG_PREFIX} Risk ≤ 0 (entry={entry:.4f}, sl={sl:.4f}) — skip")
        return False

    tp   = entry - KDT_TP_R * risk
    size, risk_usd = calc_size(client, entry, sl)

    if size <= 0:
        print(f"{LOG_PREFIX} size=0 — skip (Balance zu niedrig?)")
        return False

    print(f"{LOG_PREFIX} ETH SHORT: entry≈{entry:.4f}  SL={sl:.4f}  TP={tp:.4f}  "
          f"size={size}  risk=${risk_usd:.2f}")

    result = client.place_market_order(
        coin=KDT_ASSET, is_buy=False, size=size,
        stop_loss=sl, take_profit=tp,
    )

    if not result.success:
        msg = f"⚠️ KDT ETH: Order fehlgeschlagen\n{result}"
        print(f"{LOG_PREFIX} {msg}")
        send_telegram_message(msg)
        return False

    trade = {
        "asset":        KDT_ASSET,
        "direction":    "short",
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "signal_time":  signal["signal_time"],
        "entry_price":  entry,
        "sl":           sl,
        "tp":           round(tp, 4),
        "size":         size,
        "risk_usd":     risk_usd,
        "order_id":     result.order_id,
        "sl_dist":      signal["sl_dist"],
        "atr14":        signal["atr14"],
        "body_ratio":   signal["body_ratio"],
        "vol_ratio":    signal["vol_ratio"],
        "dry_run":      KDT_DRY_RUN,
    }
    save_trade(trade)

    msg = (
        f"{'🔴 [DRY RUN] ' if KDT_DRY_RUN else '🔴 '}"
        f"KDT SHORT: #ETH\n"
        f"Entry : ${entry:,.2f}\n"
        f"SL    : ${sl:,.2f}  (+{(sl/entry-1)*100:.2f}%)\n"
        f"TP    : ${tp:,.2f}  (−{(1-tp/entry)*100:.2f}%)\n"
        f"Risiko: ${risk_usd:.2f} ({KDT_MAX_RISK_PCT*100:.0f}%)\n"
        f"Body  : {signal['body_ratio']:.3f}×  Vol: {signal['vol_ratio']:.3f}×  "
        f"ATR: {signal['atr14']:.2f}  SL-Dist: {signal['sl_dist']:.2f}"
    )
    send_telegram_message(msg)
    return True


# ─── Haupt-Loop ───────────────────────────────────────────────────────────────

def main():
    now     = datetime.now(timezone.utc)
    now_ts  = int(now.timestamp() * 1000)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    print(f"{LOG_PREFIX} Start {now_str}  DRY_RUN={KDT_DRY_RUN}")

    if not KDT_ENABLED:
        print(f"{LOG_PREFIX} KDT_ENABLED=False — exit")
        return

    client = BitgetClient(dry_run=KDT_DRY_RUN)

    # Offene ETH-Position prüfen
    positions    = client.get_positions()
    eth_in_trade = any(p.coin == KDT_ASSET and p.size > 0 for p in positions)

    # ── Schritt 1: Pending Sell-Stop prüfen ──────────────────────────────────
    pending    = load_pending()
    still_valid = []
    executed   = False

    for sig in pending:
        if now_ts > sig["expiry_ts"]:
            print(f"{LOG_PREFIX} Signal abgelaufen ({sig['signal_time']}) — verworfen")
            continue

        if eth_in_trade:
            still_valid.append(sig)
            continue

        try:
            price = client.get_price(KDT_ASSET)
        except Exception as e:
            print(f"{LOG_PREFIX} Preis-Fehler: {e}")
            still_valid.append(sig)
            continue

        if price <= sig["stop_price"]:
            print(f"{LOG_PREFIX} Sell-Stop getriggert: {price:.4f} ≤ {sig['stop_price']:.4f}")
            ok = execute_short(client, sig, price)
            if ok:
                executed = True
                eth_in_trade = True
        else:
            still_valid.append(sig)

    save_pending(still_valid)

    # ── Schritt 2: Neues KDT-Setup auf ETH scannen ───────────────────────────
    new_signal = None

    if not eth_in_trade and not any(True for _ in still_valid):
        try:
            candles = client.get_candles(KDT_ASSET, interval="1h",
                                         limit=KDT_CANDLE_LIMIT)
        except Exception as e:
            print(f"{LOG_PREFIX} Candle-Fehler: {e}")
            candles = []

        if len(candles) >= KDT_EMA_PERIOD + 5:
            # Letzte abgeschlossene Kerze = candles[-2] (aktuelle läuft noch)
            closed = candles[:-1]
            ind    = compute_indicators(closed)
            sig    = check_kdt_signal(closed, ind)

            if sig:
                expiry_ts = now_ts + KDT_ENTRY_WINDOW * 3600 * 1000
                new_signal = {
                    **sig,
                    "asset":       KDT_ASSET,
                    "signal_time": now_str,
                    "expiry_ts":   expiry_ts,
                }

                print(f"{LOG_PREFIX} 🎯 KDT-Signal!  "
                      f"Stop@{sig['stop_price']:.4f}  SL={sig['sl']:.4f}  "
                      f"Body={sig['body_ratio']:.3f}×  Vol={sig['vol_ratio']:.3f}×  "
                      f"ATR={sig['atr14']:.2f}  Gültig {KDT_ENTRY_WINDOW}h")

                alert = (
                    f"{'🔔 [DRY RUN] ' if KDT_DRY_RUN else '🔔 '}"
                    f"KDT Signal: #ETH SHORT\n"
                    f"3 grüne Kerzen erschöpft — kinetische Bremsung\n"
                    f"Sell-Stop : ${sig['stop_price']:,.2f} (Candle-Low)\n"
                    f"SL        : ${sig['sl']:,.2f} (Candle-High)\n"
                    f"SL-Distanz: ${sig['sl_dist']:.2f} ({sig['sl_dist']/sig['stop_price']*100:.2f}%)\n"
                    f"ATR(14)   : ${sig['atr14']:.2f}\n"
                    f"Body-Ratio: {sig['body_ratio']:.3f}×  Vol-Ratio: {sig['vol_ratio']:.3f}×\n"
                    f"Gültig    : {KDT_ENTRY_WINDOW}h  |  TP: {KDT_TP_R}R bei Execution"
                )
                send_telegram_message(alert)

                save_pending(still_valid + [new_signal])
            else:
                print(f"{LOG_PREFIX} Kein KDT-Signal auf ETH.")
        else:
            print(f"{LOG_PREFIX} Zu wenig Candles ({len(candles)})")

    # Status
    total_pending = len(still_valid) + (1 if new_signal else 0)
    print(f"{LOG_PREFIX} Fertig. Pending={total_pending}  "
          f"Executed={executed}  ETH-in-Trade={eth_in_trade}")


if __name__ == "__main__":
    main()
