#!/usr/bin/env python3
"""
APEX - Weekend Momentum Carry Strategy (WeekendMomo)
=====================================================
Handelt AVAX am Wochenende basierend auf 3-Tage-Momentum.

Strategie:
  1. Freitag: Berechne 3-Tage-Momentum (Freitag-Close / Dienstag-Close - 1)
  2. Wenn |Momentum| >= 3%: Trade in Momentum-Richtung
  3. Entry: Samstag 00:00 UTC
  4. SL: 1.5x ATR(14) auf 4h-Chart
  5. TP: 3x ATR (R:R = 2:1)
  6. Exit: Sonntagabend falls SL/TP nicht getroffen

Cron Schedule:
  Freitag  23:00 Berlin:  python weekend_momo.py --check
  Samstag  00:05 UTC:     python weekend_momo.py --entry
  Sonntag  21:00 Berlin:  python weekend_momo.py --exit
"""

import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.bitget_client import BitgetClient
from telegram_sender import send_telegram_message

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")

sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import (
        DRY_RUN, CAPITAL, MAX_RISK_PCT,
        WEEKEND_ASSET, MOMENTUM_THRESHOLD, ATR_SL_MULTIPLIER, ATR_TP_MULTIPLIER,
        LEVERAGE
    )
except ImportError:
    DRY_RUN = True
    CAPITAL = 50.0
    MAX_RISK_PCT = 0.02
    WEEKEND_ASSET = "AVAX"
    MOMENTUM_THRESHOLD = 0.03
    ATR_SL_MULTIPLIER = 1.5
    ATR_TP_MULTIPLIER = 3.0
    LEVERAGE = 5

WEEKEND_STATE_FILE = os.path.join(DATA_DIR, "weekend_momo_state.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")


def load_state():
    if os.path.exists(WEEKEND_STATE_FILE):
        with open(WEEKEND_STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WEEKEND_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log_trade(trade_data):
    os.makedirs(DATA_DIR, exist_ok=True)
    trades = []
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            trades = json.load(f)

    trades.append({
        **trade_data,
        "timestamp": datetime.utcnow().isoformat(),
        "session": "weekend_momo",
        "strategy": "WeekendMomo",
        "dry_run": DRY_RUN,
    })
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def get_3day_momentum(client):
    """
    Berechne 3-Tage-Momentum: M = Freitag-Close / Dienstag-Close - 1
    """
    candles = client.get_candles(WEEKEND_ASSET, interval="1d", limit=7)
    if not candles or len(candles) < 5:
        return None, None, None

    tuesday_close = None
    friday_close = None

    for candle in candles:
        ts = candle.get("time", 0)
        dt = datetime.utcfromtimestamp(ts / 1000 if ts > 1e10 else ts)
        close = float(candle.get("close", 0))

        if dt.weekday() == 1:   # Dienstag
            tuesday_close = close
        elif dt.weekday() == 4: # Freitag
            friday_close = close

    if not tuesday_close or not friday_close:
        return None, None, None

    momentum = (friday_close / tuesday_close) - 1
    return momentum, tuesday_close, friday_close


def get_atr_4h(client, periods=14):
    """ATR(14) auf 4h-Chart"""
    candles = client.get_candles(WEEKEND_ASSET, interval="4h", limit=periods + 1)
    if not candles or len(candles) < periods + 1:
        return None

    true_ranges = []
    for i in range(1, len(candles)):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        prev_close = float(candles[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    return sum(true_ranges[-periods:]) / min(periods, len(true_ranges))


def calculate_position_size(balance, risk_pct, entry_price, stop_loss_price):
    risk_usd = balance * risk_pct
    risk_per_unit = abs(entry_price - stop_loss_price)
    if risk_per_unit == 0:
        return 0, 0
    return round(risk_usd / risk_per_unit, 2), risk_usd


# === PHASE 1: Freitag-Check ===

def check_momentum():
    print("=" * 60)
    print("APEX WeekendMomo – Freitag Momentum-Check")
    print("=" * 60)

    dry_tag = " [DRY RUN]" if DRY_RUN else ""
    client = BitgetClient(dry_run=DRY_RUN)

    momentum, tue_close, fri_close = get_3day_momentum(client)
    if momentum is None:
        msg = "⚠️ WeekendMomo: Konnte Momentum nicht berechnen (fehlende Daten)"
        print(msg)
        send_telegram_message(msg)
        return

    momentum_pct = momentum * 100
    print(f"\n📊 {WEEKEND_ASSET} 3-Tage-Momentum:")
    print(f"   Dienstag-Close: ${tue_close:,.4f}")
    print(f"   Freitag-Close:  ${fri_close:,.4f}")
    print(f"   Momentum:       {momentum_pct:+.2f}%")
    print(f"   Threshold:      ±{MOMENTUM_THRESHOLD * 100:.0f}%")

    atr = get_atr_4h(client)
    if atr is None:
        msg = "⚠️ WeekendMomo: Konnte ATR nicht berechnen"
        print(msg)
        send_telegram_message(msg)
        return

    print(f"   ATR(14, 4h):    ${atr:,.4f}")

    if abs(momentum) >= MOMENTUM_THRESHOLD:
        direction = "long" if momentum > 0 else "short"
        direction_emoji = "🟢" if direction == "long" else "🔴"

        state = {
            "signal": True,
            "direction": direction,
            "momentum": momentum,
            "momentum_pct": momentum_pct,
            "tuesday_close": tue_close,
            "friday_close": fri_close,
            "atr": atr,
            "checked_at": datetime.utcnow().isoformat(),
            "weekend_of": datetime.utcnow().strftime("%Y-%m-%d"),
            "traded": False,
        }
        save_state(state)

        msg = (
            f"📊 *WeekendMomo Signal!*{dry_tag}\n\n"
            f"{direction_emoji} *{WEEKEND_ASSET} {direction.upper()}*\n\n"
            f"3-Tage-Momentum: *{momentum_pct:+.2f}%*\n"
            f"Dienstag: ${tue_close:,.4f}\n"
            f"Freitag: ${fri_close:,.4f}\n"
            f"ATR(14, 4h): ${atr:,.4f}\n\n"
            f"⏰ Entry geplant: Samstag 00:05 UTC\n"
            f"🎯 SL: {ATR_SL_MULTIPLIER}x ATR = ${atr * ATR_SL_MULTIPLIER:,.4f}\n"
            f"🎯 TP: {ATR_TP_MULTIPLIER}x ATR = ${atr * ATR_TP_MULTIPLIER:,.4f}\n"
            f"📐 R:R = 2:1"
        )
        print(f"\n✅ SIGNAL: {direction.upper()} ({momentum_pct:+.2f}%)")
        send_telegram_message(msg)
    else:
        state = {
            "signal": False,
            "momentum": momentum,
            "momentum_pct": momentum_pct,
            "checked_at": datetime.utcnow().isoformat(),
            "weekend_of": datetime.utcnow().strftime("%Y-%m-%d"),
            "traded": False,
        }
        save_state(state)
        msg = (
            f"📊 *WeekendMomo – Kein Signal*{dry_tag}\n\n"
            f"{WEEKEND_ASSET} Momentum: {momentum_pct:+.2f}%\n"
            f"Threshold: ±{MOMENTUM_THRESHOLD * 100:.0f}%\n\n"
            f"⏸️ Kein Trade dieses Wochenende"
        )
        print(f"\n⏸️ KEIN SIGNAL ({momentum_pct:+.2f}%)")
        send_telegram_message(msg)

    print("NO_REPLY")


# === PHASE 2: Samstag-Entry ===

def execute_entry():
    print("=" * 60)
    print("APEX WeekendMomo – Samstag Entry")
    print("=" * 60)

    dry_tag = " [DRY RUN]" if DRY_RUN else ""
    state = load_state()

    if not state.get("signal"):
        print("⏸️ Kein Signal – kein Trade")
        print("NO_REPLY")
        return

    if state.get("traded"):
        print("✅ Bereits getradet dieses Wochenende")
        print("NO_REPLY")
        return

    direction = state["direction"]
    atr = state["atr"]

    client = BitgetClient(dry_run=DRY_RUN)

    current_price = client.get_price(WEEKEND_ASSET)
    if not current_price:
        msg = f"❌ WeekendMomo: Konnte {WEEKEND_ASSET}-Preis nicht abrufen"
        print(msg)
        send_telegram_message(msg)
        return

    balance = client.get_balance()
    if not balance or balance <= 0:
        # Im DRY RUN: CAPITAL aus Config verwenden
        if DRY_RUN:
            balance = CAPITAL
            print(f"[DRY RUN] Simulierte Balance: ${balance}")
        else:
            msg = "❌ WeekendMomo: Konnte Balance nicht abrufen"
            print(msg)
            send_telegram_message(msg)
            return

    positions = client.get_positions()
    if any(p.coin == WEEKEND_ASSET for p in positions):
        msg = f"⏭️ WeekendMomo: {WEEKEND_ASSET} Position bereits offen"
        print(msg)
        send_telegram_message(msg)
        return

    sl_distance = atr * ATR_SL_MULTIPLIER
    tp_distance = atr * ATR_TP_MULTIPLIER

    if direction == "long":
        stop_loss = current_price - sl_distance
        take_profit = current_price + tp_distance
    else:
        stop_loss = current_price + sl_distance
        take_profit = current_price - tp_distance

    size, risk_usd = calculate_position_size(balance, MAX_RISK_PCT, current_price, stop_loss)

    if size <= 0:
        msg = "❌ WeekendMomo: Position Size zu klein"
        print(msg)
        send_telegram_message(msg)
        return

    print(f"\n🎯 Trade Setup{dry_tag}:")
    print(f"   Asset:       {WEEKEND_ASSET}")
    print(f"   Direction:   {direction.upper()}")
    print(f"   Entry:       ${current_price:,.4f}")
    print(f"   Size:        {size}")
    print(f"   Stop-Loss:   ${stop_loss:,.4f} ({ATR_SL_MULTIPLIER}x ATR)")
    print(f"   Take-Profit: ${take_profit:,.4f} ({ATR_TP_MULTIPLIER}x ATR)")
    print(f"   Risk:        ${risk_usd:,.2f}")
    print(f"   Hebel:       {LEVERAGE}x")

    client.set_leverage(WEEKEND_ASSET, LEVERAGE)

    is_buy = (direction == "long")
    order_result = client.place_market_order(
        coin=WEEKEND_ASSET,
        is_buy=is_buy,
        size=size,
        reduce_only=False,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )

    if not order_result.success:
        msg = f"❌ WeekendMomo: Order fehlgeschlagen – {order_result.error}"
        print(msg)
        send_telegram_message(msg)
        return

    actual_entry = order_result.avg_price

    if direction == "long":
        stop_loss = actual_entry - sl_distance
        take_profit = actual_entry + tp_distance
    else:
        stop_loss = actual_entry + sl_distance
        take_profit = actual_entry - tp_distance

    import time
    time.sleep(5)

    # Prüfen ob Preset-SL/TP bereits aktiv
    existing_tpsl = client.get_tpsl_orders(WEEKEND_ASSET)
    sl_ok = any(o.get("planType") == "loss_plan" for o in existing_tpsl)
    tp_ok = any(o.get("planType") == "profit_plan" for o in existing_tpsl)

    if sl_ok:
        print(f"   ✅ SL aktiv (Preset)")
    else:
        sl_r = client.place_stop_loss(WEEKEND_ASSET, stop_loss, size)
        if not sl_r.success:
            time.sleep(2)
            sl_r = client.place_stop_loss(WEEKEND_ASSET, stop_loss, size)
        sl_ok = sl_r.success
        print(f"   {'✅' if sl_ok else '❌'} SL {'gesetzt' if sl_ok else 'FEHLER'}")

    if tp_ok:
        print(f"   ✅ TP aktiv (Preset)")
    else:
        tp_r = client.place_take_profit(WEEKEND_ASSET, take_profit, size)
        if not tp_r.success:
            time.sleep(2)
            tp_r = client.place_take_profit(WEEKEND_ASSET, take_profit, size)
        tp_ok = tp_r.success
        print(f"   {'✅' if tp_ok else '❌'} TP {'gesetzt' if tp_ok else 'FEHLER'}")

    # KRITISCH: Wenn weder Preset noch separate SL/TP aktiv → Position schließen
    if not sl_ok or not tp_ok:
        print(f"\n🚨 KRITISCH: SL/TP nicht gesetzt (SL={sl_ok}, TP={tp_ok})")
        close_result = client.place_market_order(
            coin=WEEKEND_ASSET,
            is_buy=not is_buy,
            size=size,
            reduce_only=True,
        )
        alert_msg = (
            f"🚨 WeekendMomo NOTFALL{dry_tag}\n\n"
            f"{WEEKEND_ASSET} geöffnet aber SL/TP NICHT gesetzt!\n"
            f"SL={sl_ok} | TP={tp_ok}\n"
            f"Position {'geschlossen ✅' if close_result.success else 'KONNTE NICHT GESCHLOSSEN WERDEN ❌ – MANUELL HANDELN!'}"
        )
        send_telegram_message(alert_msg)
        return

    log_trade({
        "asset": WEEKEND_ASSET,
        "direction": direction,
        "entry_price": actual_entry,
        "size": size,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_usd": risk_usd,
        "reward_usd": risk_usd * 2,
        "ratio": "2:1",
        "atr": atr,
        "leverage": LEVERAGE,
        "momentum": state["momentum"],
        "momentum_pct": state["momentum_pct"],
    })

    state["traded"] = True
    state["entry_price"] = actual_entry
    state["size"] = size
    state["stop_loss"] = stop_loss
    state["take_profit"] = take_profit
    state["entry_time"] = datetime.utcnow().isoformat()
    save_state(state)

    direction_emoji = "🟢" if direction == "long" else "🔴"
    msg = (
        f"🚀 *WeekendMomo TRADE!*{dry_tag}\n\n"
        f"{direction_emoji} *{WEEKEND_ASSET} {direction.upper()}*\n\n"
        f"📈 Entry: ${actual_entry:,.4f}\n"
        f"📦 Size: {size} {WEEKEND_ASSET}\n"
        f"🛑 Stop-Loss: ${stop_loss:,.4f} ({ATR_SL_MULTIPLIER}x ATR)\n"
        f"🎯 Take-Profit: ${take_profit:,.4f} ({ATR_TP_MULTIPLIER}x ATR)\n"
        f"💰 Risk: ${risk_usd:,.2f}\n"
        f"📐 R:R: 2:1 | Hebel: {LEVERAGE}x\n\n"
        f"📊 Momentum: {state['momentum_pct']:+.2f}%\n"
        f"SL: {'✅' if sl_result.success else '❌'} | "
        f"TP: {'✅' if tp_result.success else '❌'}\n\n"
        f"⏰ Exit: Sonntag 21:00 Berlin"
    )
    print(f"\n✅ TRADE AUSGEFÜHRT{dry_tag}")
    send_telegram_message(msg)
    print("NO_REPLY")


# === PHASE 3: Sonntag-Exit ===

def execute_exit():
    print("=" * 60)
    print("APEX WeekendMomo – Sonntag Exit")
    print("=" * 60)

    dry_tag = " [DRY RUN]" if DRY_RUN else ""
    state = load_state()

    if not state.get("traded"):
        print("⏸️ Kein WeekendMomo-Trade offen")
        print("NO_REPLY")
        return

    client = BitgetClient(dry_run=DRY_RUN)
    positions = client.get_positions()
    avax_pos = next((p for p in positions if p.coin == WEEKEND_ASSET), None)

    if not avax_pos:
        msg = (
            f"📊 *WeekendMomo Sonntag-Check*{dry_tag}\n\n"
            f"Position bereits geschlossen (SL/TP getroffen)."
        )
        print("✅ Position bereits geschlossen")
        send_telegram_message(msg)
        state["closed"] = True
        state["close_reason"] = "sl_or_tp"
        save_state(state)
        print("NO_REPLY")
        return

    current_price = client.get_price(WEEKEND_ASSET)
    entry_price = state.get("entry_price", 0)
    direction = state.get("direction", "unknown")
    size = abs(avax_pos.size)

    if direction == "long":
        pnl = (current_price - entry_price) * size
    else:
        pnl = (entry_price - current_price) * size

    print(f"\n📊 Offene Position{dry_tag}:")
    print(f"   {WEEKEND_ASSET} {direction.upper()}")
    print(f"   Entry:   ${entry_price:,.4f}")
    print(f"   Aktuell: ${current_price:,.4f}")
    print(f"   Unreal. P&L: ${pnl:,.2f}")
    print(f"\n🔄 Schließe Position via Market Order...")

    is_buy_close = (direction == "short")
    close_result = client.place_market_order(
        coin=WEEKEND_ASSET,
        is_buy=is_buy_close,
        size=size,
        reduce_only=True,
    )

    if close_result.success:
        close_price = close_result.avg_price
        if direction == "long":
            final_pnl = (close_price - entry_price) * size
        else:
            final_pnl = (entry_price - close_price) * size
        final_pnl_pct = (final_pnl / (entry_price * size)) * 100 if entry_price > 0 else 0

        result_emoji = "✅" if final_pnl > 0 else "❌"
        msg = (
            f"🏁 *WeekendMomo CLOSE*{dry_tag}\n\n"
            f"{result_emoji} *{WEEKEND_ASSET} {direction.upper()}*\n\n"
            f"📈 Entry: ${entry_price:,.4f}\n"
            f"📉 Exit: ${close_price:,.4f}\n"
            f"💰 P&L: ${final_pnl:,.2f} ({final_pnl_pct:+.2f}%)\n"
            f"📊 Momentum war: {state.get('momentum_pct', 0):+.2f}%\n\n"
            f"{'🎉 Gewinn!' if final_pnl > 0 else '😤 Verlust – weiter gehts!'}"
        )
        print(f"\n{result_emoji} ${final_pnl:,.2f} ({final_pnl_pct:+.2f}%)")
        send_telegram_message(msg)

        log_trade({
            "asset": WEEKEND_ASSET,
            "direction": f"close_{direction}",
            "entry_price": entry_price,
            "exit_price": close_price,
            "size": size,
            "pnl": final_pnl,
            "pnl_pct": final_pnl_pct,
            "close_reason": "sunday_timeout",
        })
    else:
        msg = f"❌ WeekendMomo: Close fehlgeschlagen – {close_result.error}"
        print(msg)
        send_telegram_message(msg)

    state["closed"] = True
    state["close_reason"] = "sunday_timeout"
    save_state(state)
    print("NO_REPLY")


# === CLI Interface ===

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python weekend_momo.py --check    # Freitag: Momentum prüfen")
        print("  python weekend_momo.py --entry    # Samstag: Trade ausführen")
        print("  python weekend_momo.py --exit     # Sonntag: Position schließen")
        print("  python weekend_momo.py --status   # Aktuellen State anzeigen")
        sys.exit(1)

    action = sys.argv[1]
    if action == "--check":
        check_momentum()
    elif action == "--entry":
        execute_entry()
    elif action == "--exit":
        execute_exit()
    elif action == "--status":
        state = load_state()
        print(json.dumps(state, indent=2) if state else "Kein State vorhanden")
    else:
        print(f"Unbekannte Aktion: {action}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n💥 ERROR: {e}")
        import traceback
        traceback.print_exc()
        send_telegram_message(f"💥 WeekendMomo ERROR: {e}")
        print("NO_REPLY")
        sys.exit(1)
