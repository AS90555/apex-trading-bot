#!/usr/bin/env python3
"""
VAA Live Bot — Volume Absorption Anomaly, SHORT-Only, 1H.

Läuft jede volle Stunde via Cron:
  0 * * * * cd /root/apex-trading-bot && venv/bin/python scripts/vaa_bot.py

Flow:
  1. Pending Sell-Stops prüfen → bei Preis-Hit: Market-Short + SL + TP
  2. Neue VAA-Setups scannen  → bei Signal: Pending speichern
  3. Telegram-Report
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from config.bot_config import (
    VAA_ENABLED, VAA_DRY_RUN, VAA_ASSETS, VAA_BLACKLIST,
    VAA_VOL_MULT, VAA_BODY_MULT, VAA_ATR_EXPAND, VAA_TP_R,
    VAA_ENTRY_WINDOW, VAA_MAX_RISK_PCT, VAA_CANDLE_LIMIT,
    VAA_VOL_SMA_PERIOD, VAA_BODY_SMA_PERIOD, VAA_EMA_PERIOD,
    VAA_ATR_PERIOD, LEVERAGE, DATA_DIR,
)
from scripts.bitget_client import BitgetClient
from scripts.telegram_sender import send_telegram_message, format_event_tag

PENDING_FILE = os.path.join(DATA_DIR, "vaa_pending.json")
TRADES_FILE  = os.path.join(DATA_DIR, "vaa_trades.json")
LOG_PREFIX   = "[VAA]"


# ─── Indikatoren (pure Python) ────────────────────────────────────────────────

def _sma(values: list, period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def _ema(values: list, period: int) -> float:
    if len(values) < period:
        return 0.0
    k    = 2 / (period + 1)
    val  = sum(values[:period]) / period
    for v in values[period:]:
        val = v * k + val * (1 - k)
    return val


def _atr(candles: list, period: int) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder's Smoothing auf den letzten `period` TRs
    window = trs[-period*2:] if len(trs) >= period * 2 else trs
    if not window:
        return 0.0
    atr = sum(window[:period]) / period
    for tr in window[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_indicators(candles: list) -> dict:
    """Berechnet alle VAA-Indikatoren auf den letzten N 1H-Candles."""
    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]
    bodies  = [abs(c["open"] - c["close"]) for c in candles]

    vol_sma  = _sma(volumes, VAA_VOL_SMA_PERIOD)
    body_sma = _sma(bodies,  VAA_BODY_SMA_PERIOD)
    ema20    = _ema(closes,  VAA_EMA_PERIOD)
    atr14    = _atr(candles, VAA_ATR_PERIOD)

    # ATR_SMA(20): EMA der letzten ATR-Werte — berechne ATR-Serie für SMA
    atr_vals = []
    for i in range(VAA_ATR_PERIOD + 1, len(candles) + 1):
        atr_vals.append(_atr(candles[:i], VAA_ATR_PERIOD))
    atr_sma20 = _sma(atr_vals, 20) if len(atr_vals) >= 20 else atr14

    return {
        "vol_sma":  vol_sma,
        "body_sma": body_sma,
        "ema20":    ema20,
        "atr14":    atr14,
        "atr_sma20": atr_sma20,
    }


# ─── Signal-Erkennung ─────────────────────────────────────────────────────────

def check_vaa_signal(candle: dict, ind: dict) -> bool:
    """
    Prüft ob die letzte abgeschlossene 1H-Kerze ein VAA-SHORT-Setup erfüllt.
    Gibt True zurück wenn alle Bedingungen erfüllt sind.
    """
    if ind["vol_sma"] <= 0 or ind["body_sma"] <= 0 or ind["ema20"] <= 0:
        return False

    vol_ratio  = candle["volume"] / ind["vol_sma"]
    body_ratio = abs(candle["open"] - candle["close"]) / ind["body_sma"]
    atr_ratio  = ind["atr14"] / ind["atr_sma20"] if ind["atr_sma20"] > 0 else 0

    # Alle Bedingungen
    big_vol    = vol_ratio  > VAA_VOL_MULT
    small_body = body_ratio < VAA_BODY_MULT
    trend_up   = candle["close"] > ind["ema20"]   # kurzfristiger Aufwärtstrend
    atr_expand = atr_ratio > VAA_ATR_EXPAND        # F-06: ATR-Expansion

    return big_vol and small_body and trend_up and atr_expand


# ─── Pending-Signale ──────────────────────────────────────────────────────────

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

def calc_size(client: BitgetClient, asset: str,
              entry: float, sl: float) -> tuple[float, float]:
    """
    Gibt (size, risk_usd) zurück.
    size = (balance × VAA_MAX_RISK_PCT) / sl_distance, gerundet auf Bitget-Decimals.
    """
    balance  = client.get_balance()
    risk_usd = balance * VAA_MAX_RISK_PCT
    sl_dist  = abs(entry - sl)
    if sl_dist <= 0:
        return 0.0, 0.0

    raw_size = risk_usd / sl_dist
    # Leverage-Margin-Check: Positionswert ≤ 90% Balance × Leverage
    max_size = (balance * LEVERAGE * 0.90) / entry
    size     = min(raw_size, max_size)

    # Runden auf Asset-Dezimalstellen
    from config.bot_config import SIZE_DECIMALS
    dec  = SIZE_DECIMALS.get(asset, 1)
    size = math.floor(size * 10**dec) / 10**dec

    return size, risk_usd


# ─── Order-Ausführung ─────────────────────────────────────────────────────────

def execute_short(client: BitgetClient, signal: dict, current_price: float) -> bool:
    """
    Platziert Market-Short + SL + TP für ein VAA-Signal.
    Gibt True bei Erfolg zurück.
    """
    asset  = signal["asset"]
    sl     = signal["sl"]        # Anomalie-Candle-High
    entry  = current_price
    risk   = sl - entry
    if risk <= 0:
        print(f"{LOG_PREFIX} {asset}: risk <= 0 (entry={entry:.4f}, sl={sl:.4f}) — skip")
        return False

    tp = entry - VAA_TP_R * risk

    size, risk_usd = calc_size(client, asset, entry, sl)
    if size <= 0:
        print(f"{LOG_PREFIX} {asset}: size=0 — skip")
        return False

    print(f"{LOG_PREFIX} {asset} SHORT: entry≈{entry:.4f}  SL={sl:.4f}  TP={tp:.4f}  "
          f"size={size}  risk=${risk_usd:.2f}")

    # Market-Short
    result = client.place_market_order(
        coin=asset, is_buy=False, size=size,
        stop_loss=sl, take_profit=tp,
    )
    if not result.success:
        print(f"{LOG_PREFIX} {asset}: Order FAILED — {result}")
        send_telegram_message(f"⚠️ VAA {asset}: Order fehlgeschlagen\n{result}")
        return False

    trade = {
        "asset":       asset,
        "direction":   "short",
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "signal_time": signal["signal_time"],
        "entry_price": entry,
        "sl":          sl,
        "tp":          tp,
        "size":        size,
        "risk_usd":    round(risk_usd, 4),
        "order_id":    result.order_id,
        "vol_ratio":   signal["vol_ratio"],
        "body_ratio":  signal["body_ratio"],
        "atr_ratio":   signal["atr_ratio"],
        "dry_run":     VAA_DRY_RUN,
    }
    save_trade(trade)

    msg = (
        f"{format_event_tag('VAA', 'ENTRY', asset, VAA_DRY_RUN)}\n"
        f"VAA SHORT: #{asset}\n"
        f"Entry : ${entry:,.4f}\n"
        f"SL    : ${sl:,.4f}  (+{(sl/entry-1)*100:.2f}%)\n"
        f"TP    : ${tp:,.4f}  (−{(1-tp/entry)*100:.2f}%)\n"
        f"Risiko: ${risk_usd:.2f} ({VAA_MAX_RISK_PCT*100:.0f}%)\n"
        f"Vol   : {signal['vol_ratio']:.1f}×SMA  Body: {signal['body_ratio']:.3f}×SMA  "
        f"ATR: {signal['atr_ratio']:.2f}×SMA"
    )
    send_telegram_message(msg)
    return True


# ─── Haupt-Loop ───────────────────────────────────────────────────────────────

def main():
    now     = datetime.now(timezone.utc)
    now_ts  = int(now.timestamp() * 1000)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    print(f"{LOG_PREFIX} Start {now_str}")

    if not VAA_ENABLED:
        print(f"{LOG_PREFIX} VAA_ENABLED=False — exit")
        return

    client = BitgetClient(dry_run=VAA_DRY_RUN)

    # Sicherheits-Check: kein Trade wenn offene VAA-Position existiert
    positions = client.get_positions()
    active_assets = {p.coin for p in positions if p.size > 0}

    # ── Schritt 1: Pending Sell-Stops prüfen ─────────────────────────────────
    pending     = load_pending()
    still_valid = []
    executed    = set()

    for sig in pending:
        asset   = sig["asset"]
        expiry  = sig["expiry_ts"]

        # Abgelaufen?
        if now_ts > expiry:
            print(f"{LOG_PREFIX} {asset}: Signal abgelaufen ({sig['signal_time']})")
            continue

        # Asset bereits in Position?
        if asset in active_assets:
            still_valid.append(sig)
            continue

        # Preis abfragen und prüfen ob Low durchbrochen
        try:
            price = client.get_price(asset)
        except Exception as e:
            print(f"{LOG_PREFIX} {asset}: Preis-Fehler — {e}")
            still_valid.append(sig)
            continue

        if price <= sig["stop_price"]:
            # Sell-Stop getriggert → Market-Short
            print(f"{LOG_PREFIX} {asset}: Sell-Stop getriggert bei {price:.4f} ≤ {sig['stop_price']:.4f}")
            ok = execute_short(client, sig, price)
            if ok:
                executed.add(asset)
                active_assets.add(asset)
            # Signal nach Trigger nicht mehr pending (egal ob ok oder nicht)
        else:
            still_valid.append(sig)

    save_pending(still_valid)

    # ── Schritt 2: Neue VAA-Setups scannen ───────────────────────────────────
    new_signals = []

    for asset in VAA_ASSETS:
        if asset in VAA_BLACKLIST:
            continue
        if asset in active_assets:
            continue
        # Kein doppeltes Pending pro Asset
        if any(p["asset"] == asset for p in still_valid):
            continue

        try:
            candles = client.get_candles(asset, interval="1h", limit=VAA_CANDLE_LIMIT)
        except Exception as e:
            print(f"{LOG_PREFIX} {asset}: Candle-Fehler — {e}")
            continue

        if len(candles) < VAA_VOL_SMA_PERIOD + 5:
            print(f"{LOG_PREFIX} {asset}: zu wenig Candles ({len(candles)})")
            continue

        # Letzte abgeschlossene Kerze (vorletzt in der Liste, da aktuelle noch läuft)
        trigger = candles[-2]
        ind     = compute_indicators(candles[:-1])  # ohne laufende Kerze

        if not check_vaa_signal(trigger, ind):
            continue

        # VAA-Setup gefunden
        vol_ratio  = trigger["volume"] / ind["vol_sma"]
        body_ratio = abs(trigger["open"] - trigger["close"]) / ind["body_sma"]
        atr_ratio  = ind["atr14"] / ind["atr_sma20"] if ind["atr_sma20"] > 0 else 0

        stop_price  = trigger["low"]    # Sell-Stop
        sl          = trigger["high"]   # Stop-Loss
        expiry_ts   = now_ts + VAA_ENTRY_WINDOW * 3600 * 1000

        approx_risk = sl - stop_price
        if approx_risk <= 0 or approx_risk / stop_price >= 0.25:
            print(f"{LOG_PREFIX} {asset}: Risk zu groß/klein — skip")
            continue

        sig = {
            "asset":       asset,
            "signal_time": now_str,
            "stop_price":  round(stop_price, 6),   # Einstieg wenn Preis ≤ Low
            "sl":          round(sl, 6),
            "expiry_ts":   expiry_ts,
            "vol_ratio":   round(vol_ratio, 2),
            "body_ratio":  round(body_ratio, 3),
            "atr_ratio":   round(atr_ratio, 3),
            "trigger_high": round(trigger["high"], 6),
            "trigger_low":  round(trigger["low"], 6),
            "trigger_time": trigger["time"],
        }
        new_signals.append(sig)

        print(f"{LOG_PREFIX} {asset}: 🎯 VAA-Signal!  "
              f"Vol={vol_ratio:.1f}x  Body={body_ratio:.3f}x  ATR={atr_ratio:.2f}x  "
              f"Stop@{stop_price:.4f}  SL={sl:.4f}  Expiry in {VAA_ENTRY_WINDOW}h")

        alert = (
            f"{format_event_tag('VAA', 'SIGNAL', asset, VAA_DRY_RUN)}\n"
            f"VAA Signal: #{asset}\n"
            f"Anomalie-Kerze: Vol={vol_ratio:.1f}×SMA  Body={body_ratio:.3f}×SMA  "
            f"ATR={atr_ratio:.2f}×SMA\n"
            f"Sell-Stop : ${stop_price:,.4f} (Candle-Low)\n"
            f"SL        : ${sl:,.4f} (Candle-High)\n"
            f"Gültig    : {VAA_ENTRY_WINDOW}h  |  TP bei Signal-Execution: {VAA_TP_R}R"
        )
        send_telegram_message(alert)

    # Neue Signale zu Pending hinzufügen
    if new_signals:
        updated = still_valid + new_signals
        save_pending(updated)
        print(f"{LOG_PREFIX} {len(new_signals)} neue Signal(e) gespeichert")

    # Status-Log
    total_pending = len(still_valid) + len(new_signals)
    print(f"{LOG_PREFIX} Fertig. Pending: {total_pending}  Neu ausgeführt: {len(executed)}")

    if not new_signals and not executed:
        print(f"{LOG_PREFIX} Kein Signal, keine Execution — ruhige Stunde.")


if __name__ == "__main__":
    main()
