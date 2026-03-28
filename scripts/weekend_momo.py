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

Cron Schedule (auf ClawdBot):
  Freitag  23:00 Berlin:  python weekend_momo.py --check
  Samstag  00:05 UTC:     python weekend_momo.py --entry
  Sonntag  21:00 Berlin:  python weekend_momo.py --exit

Max 1 Trade pro Wochenende, 2% Kontorisiko.
"""

import os
import sys
import json
from datetime import datetime, timedelta

# Imports aus dem Projekt
from place_order import place_market_order, place_stop_loss, place_take_profit, load_credentials
from telegram_sender import send_telegram_message

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.hyperliquid_client import HyperliquidClient

# === Config ===
ASSET = "AVAX"
MOMENTUM_THRESHOLD = 0.03  # 3%
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0    # Ergibt 2:1 R:R
MAX_RISK_PCT = 0.02         # 2% vom Konto

# Datenpfade (lokal und auf Server)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
# Fallback fuer Server-Pfad
if os.path.exists("/data/.openclaw/workspace/projects/apex-trading/data"):
    DATA_DIR = "/data/.openclaw/workspace/projects/apex-trading/data"

WEEKEND_STATE_FILE = os.path.join(DATA_DIR, "weekend_momo_state.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")


def load_state():
    """Lade Weekend-Momo State"""
    if os.path.exists(WEEKEND_STATE_FILE):
        with open(WEEKEND_STATE_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_state(state):
    """Speichere Weekend-Momo State"""
    os.makedirs(os.path.dirname(WEEKEND_STATE_FILE), exist_ok=True)
    with open(WEEKEND_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def log_trade(trade_data):
    """Logge Trade in zentrale trades.json"""
    os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)

    trades = []
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, 'r') as f:
            trades = json.load(f)

    trades.append({
        **trade_data,
        "timestamp": datetime.utcnow().isoformat(),
        "session": "weekend_momo",
        "strategy": "WeekendMomo"
    })

    with open(TRADES_FILE, 'w') as f:
        json.dump(trades, f, indent=2)


def get_3day_momentum(client):
    """
    Berechne 3-Tage-Momentum: M = Freitag-Close / Dienstag-Close - 1

    Nutzt Daily-Candles von der Hyperliquid API.
    """
    candles = client.get_candles(ASSET, interval="1d", limit=7)

    if not candles or len(candles) < 5:
        return None, None, None

    # Sortiere nach Zeit (aelteste zuerst)
    candles.sort(key=lambda c: c.get("t", c.get("time", 0)))

    # Finde Dienstag und Freitag in den letzten 7 Tagen
    tuesday_close = None
    friday_close = None

    for candle in candles:
        ts = candle.get("t", candle.get("time", 0))
        if isinstance(ts, (int, float)):
            dt = datetime.utcfromtimestamp(ts / 1000 if ts > 1e12 else ts)
        else:
            dt = datetime.fromisoformat(str(ts))

        close = float(candle.get("c", candle.get("close", 0)))

        if dt.weekday() == 1:  # Dienstag
            tuesday_close = close
        elif dt.weekday() == 4:  # Freitag
            friday_close = close

    if not tuesday_close or not friday_close:
        return None, None, None

    momentum = (friday_close / tuesday_close) - 1
    return momentum, tuesday_close, friday_close


def get_atr_4h(client, periods=14):
    """
    Berechne ATR(14) auf 4h-Chart

    ATR = Durchschnitt der True Range ueber N Perioden
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    """
    candles = client.get_candles(ASSET, interval="4h", limit=periods + 1)

    if not candles or len(candles) < periods + 1:
        return None

    candles.sort(key=lambda c: c.get("t", c.get("time", 0)))

    true_ranges = []
    for i in range(1, len(candles)):
        high = float(candles[i].get("h", candles[i].get("high", 0)))
        low = float(candles[i].get("l", candles[i].get("low", 0)))
        prev_close = float(candles[i - 1].get("c", candles[i - 1].get("close", 0)))

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        true_ranges.append(tr)

    atr = sum(true_ranges[-periods:]) / min(periods, len(true_ranges))
    return atr


def calculate_position_size(balance, risk_pct, entry_price, stop_loss_price):
    """Berechne Position Size basierend auf Risk"""
    risk_usd = balance * risk_pct
    risk_per_unit = abs(entry_price - stop_loss_price)

    if risk_per_unit == 0:
        return 0, 0

    size = risk_usd / risk_per_unit
    return round(size, 4), risk_usd


# === PHASE 1: Freitag-Check ===

def check_momentum():
    """
    Freitag 23:00: Pruefe ob Momentum-Signal vorliegt.
    Speichert das Signal im State fuer Samstag-Entry.
    """
    print("=" * 60)
    print("APEX WeekendMomo - Freitag Momentum-Check")
    print("=" * 60)

    client = HyperliquidClient()

    # Momentum berechnen
    momentum, tue_close, fri_close = get_3day_momentum(client)

    if momentum is None:
        msg = "⚠️ WeekendMomo: Konnte Momentum nicht berechnen (fehlende Daten)"
        print(msg)
        send_telegram_message(msg)
        return

    momentum_pct = momentum * 100
    print(f"\n📊 {ASSET} 3-Tage-Momentum:")
    print(f"   Dienstag-Close: ${tue_close:,.4f}")
    print(f"   Freitag-Close:  ${fri_close:,.4f}")
    print(f"   Momentum:       {momentum_pct:+.2f}%")
    print(f"   Threshold:      ±{MOMENTUM_THRESHOLD * 100:.0f}%")

    # ATR berechnen
    atr = get_atr_4h(client)
    if atr is None:
        msg = "⚠️ WeekendMomo: Konnte ATR nicht berechnen"
        print(msg)
        send_telegram_message(msg)
        return

    print(f"   ATR(14, 4h):    ${atr:,.4f}")

    # Signal pruefen
    if abs(momentum) >= MOMENTUM_THRESHOLD:
        direction = "long" if momentum > 0 else "short"
        direction_emoji = "🟢" if direction == "long" else "🔴"

        # State speichern fuer Samstag-Entry
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
            "traded": False
        }
        save_state(state)

        msg = (
            f"📊 *WeekendMomo Signal!*\n\n"
            f"{direction_emoji} *{ASSET} {direction.upper()}*\n\n"
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
        # Kein Signal - State zuruecksetzen
        state = {
            "signal": False,
            "momentum": momentum,
            "momentum_pct": momentum_pct,
            "checked_at": datetime.utcnow().isoformat(),
            "weekend_of": datetime.utcnow().strftime("%Y-%m-%d"),
            "traded": False
        }
        save_state(state)

        msg = (
            f"📊 *WeekendMomo - Kein Signal*\n\n"
            f"{ASSET} Momentum: {momentum_pct:+.2f}%\n"
            f"Threshold: ±{MOMENTUM_THRESHOLD * 100:.0f}%\n\n"
            f"⏸️ Kein Trade dieses Wochenende"
        )
        print(f"\n⏸️ KEIN SIGNAL ({momentum_pct:+.2f}% < ±{MOMENTUM_THRESHOLD * 100:.0f}%)")
        send_telegram_message(msg)

    print("NO_REPLY")


# === PHASE 2: Samstag-Entry ===

def execute_entry():
    """
    Samstag 00:05 UTC: Fuehre Trade aus falls Signal vorhanden.
    """
    print("=" * 60)
    print("APEX WeekendMomo - Samstag Entry")
    print("=" * 60)

    # State laden
    state = load_state()

    if not state.get("signal"):
        print("⏸️ Kein Signal vorhanden - kein Trade")
        print("NO_REPLY")
        return

    if state.get("traded"):
        print("✅ Bereits getradet dieses Wochenende")
        print("NO_REPLY")
        return

    direction = state["direction"]
    atr = state["atr"]

    client = HyperliquidClient()

    # Aktuellen Preis holen
    current_price = client.get_price(ASSET)
    if not current_price:
        msg = f"❌ WeekendMomo: Konnte {ASSET}-Preis nicht abrufen"
        print(msg)
        send_telegram_message(msg)
        return

    # Balance holen
    balance = client.get_balance()
    if not balance or balance <= 0:
        msg = "❌ WeekendMomo: Konnte Balance nicht abrufen"
        print(msg)
        send_telegram_message(msg)
        return

    # Offene Positionen pruefen
    positions = client.get_positions()
    avax_positions = [p for p in positions if p.coin == ASSET]
    if avax_positions:
        msg = f"⏭️ WeekendMomo: {ASSET} Position bereits offen - kein neuer Trade"
        print(msg)
        send_telegram_message(msg)
        return

    # SL und TP berechnen
    sl_distance = atr * ATR_SL_MULTIPLIER
    tp_distance = atr * ATR_TP_MULTIPLIER

    if direction == "long":
        stop_loss = current_price - sl_distance
        take_profit = current_price + tp_distance
    else:
        stop_loss = current_price + sl_distance
        take_profit = current_price - tp_distance

    # Position Size berechnen
    size, risk_usd = calculate_position_size(balance, MAX_RISK_PCT, current_price, stop_loss)

    if size <= 0:
        msg = "❌ WeekendMomo: Position Size zu klein"
        print(msg)
        send_telegram_message(msg)
        return

    print(f"\n🎯 Trade Setup:")
    print(f"   Asset:       {ASSET}")
    print(f"   Direction:   {direction.upper()}")
    print(f"   Entry:       ${current_price:,.4f}")
    print(f"   Size:        {size}")
    print(f"   Stop-Loss:   ${stop_loss:,.4f} (${sl_distance:,.4f} = {ATR_SL_MULTIPLIER}x ATR)")
    print(f"   Take-Profit: ${take_profit:,.4f} (${tp_distance:,.4f} = {ATR_TP_MULTIPLIER}x ATR)")
    print(f"   Risk:        ${risk_usd:,.2f} ({MAX_RISK_PCT * 100:.0f}%)")
    print(f"   R:R:         2:1")
    print(f"   Balance:     ${balance:,.2f}")

    # Order platzieren (Size auf 2 Dezimalstellen runden für AVAX szDecimals=2)
    size = round(size, 2)
    is_buy = (direction == "long")
    order_result = place_market_order(ASSET, is_buy, size, reduce_only=False)

    if not order_result["success"]:
        msg = f"❌ WeekendMomo: Order fehlgeschlagen - {order_result.get('error')}"
        print(msg)
        send_telegram_message(msg)
        return

    actual_entry = order_result["avg_price"]

    # SL und TP neu berechnen mit tatsaechlichem Entry
    if direction == "long":
        stop_loss = actual_entry - sl_distance
        take_profit = actual_entry + tp_distance
    else:
        stop_loss = actual_entry + sl_distance
        take_profit = actual_entry - tp_distance

    # SL platzieren
    sl_result = place_stop_loss(ASSET, stop_loss, size)

    # TP platzieren
    tp_result = place_take_profit(ASSET, take_profit, size)

    # Trade loggen
    log_trade({
        "asset": ASSET,
        "direction": direction,
        "entry_price": actual_entry,
        "size": size,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_usd": risk_usd,
        "reward_usd": risk_usd * 2,
        "ratio": "2:1",
        "atr": atr,
        "momentum": state["momentum"],
        "momentum_pct": state["momentum_pct"],
        "order_result": order_result,
        "sl_result": sl_result,
        "tp_result": tp_result
    })

    # State aktualisieren
    state["traded"] = True
    state["entry_price"] = actual_entry
    state["size"] = size
    state["stop_loss"] = stop_loss
    state["take_profit"] = take_profit
    state["entry_time"] = datetime.utcnow().isoformat()
    save_state(state)

    # Telegram Notification
    direction_emoji = "🟢" if direction == "long" else "🔴"
    msg = (
        f"🚀 *WeekendMomo TRADE!*\n\n"
        f"{direction_emoji} *{ASSET} {direction.upper()}*\n\n"
        f"📈 Entry: ${actual_entry:,.4f}\n"
        f"📦 Size: {size} {ASSET}\n"
        f"🛑 Stop-Loss: ${stop_loss:,.4f} ({ATR_SL_MULTIPLIER}x ATR)\n"
        f"🎯 Take-Profit: ${take_profit:,.4f} ({ATR_TP_MULTIPLIER}x ATR)\n"
        f"💰 Risk: ${risk_usd:,.2f} ({MAX_RISK_PCT * 100:.0f}%)\n"
        f"📐 R:R: 2:1\n\n"
        f"📊 Momentum: {state['momentum_pct']:+.2f}%\n"
        f"SL: {'✅' if sl_result['success'] else '❌'} | "
        f"TP: {'✅' if tp_result['success'] else '❌'}\n\n"
        f"⏰ Exit: Sonntag 21:00 Berlin (falls SL/TP nicht getroffen)"
    )
    print(f"\n✅ TRADE EXECUTED!")
    send_telegram_message(msg)
    print("NO_REPLY")


# === PHASE 3: Sonntag-Exit ===

def execute_exit():
    """
    Sonntag 21:00 Berlin: Schliesse Position falls noch offen.
    SL/TP koennten bereits getroffen haben.
    """
    print("=" * 60)
    print("APEX WeekendMomo - Sonntag Exit")
    print("=" * 60)

    state = load_state()

    if not state.get("traded"):
        print("⏸️ Kein WeekendMomo-Trade offen")
        print("NO_REPLY")
        return

    client = HyperliquidClient()

    # Pruefen ob Position noch offen
    positions = client.get_positions()
    avax_pos = None
    for p in positions:
        if p.coin == ASSET:
            avax_pos = p
            break

    if not avax_pos:
        # Position wurde bereits durch SL oder TP geschlossen
        msg = (
            f"📊 *WeekendMomo Sonntag-Check*\n\n"
            f"Position bereits geschlossen (SL/TP getroffen).\n"
            f"Details im Position Monitor."
        )
        print("✅ Position bereits geschlossen")
        send_telegram_message(msg)

        # State zuruecksetzen
        state["closed"] = True
        state["close_reason"] = "sl_or_tp"
        save_state(state)
        print("NO_REPLY")
        return

    # Position noch offen - Market Close
    current_price = client.get_price(ASSET)
    entry_price = state.get("entry_price", 0)
    direction = state.get("direction", "unknown")
    size = abs(avax_pos.size)

    # PnL berechnen
    if direction == "long":
        pnl = (current_price - entry_price) * size
    else:
        pnl = (entry_price - current_price) * size

    pnl_pct = (pnl / (entry_price * size)) * 100 if entry_price > 0 else 0

    print(f"\n📊 Offene Position:")
    print(f"   {ASSET} {direction.upper()}")
    print(f"   Entry: ${entry_price:,.4f}")
    print(f"   Current: ${current_price:,.4f}")
    print(f"   Unrealized P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%)")
    print(f"\n🔄 Schliesse Position via Market Order...")

    # Close: Gegenteilige Market Order
    is_buy_close = (direction == "short")  # Short schliessen = Buy
    close_result = place_market_order(ASSET, is_buy_close, size, reduce_only=True)

    if close_result["success"]:
        close_price = close_result["avg_price"]

        # Finales PnL
        if direction == "long":
            final_pnl = (close_price - entry_price) * size
        else:
            final_pnl = (entry_price - close_price) * size

        final_pnl_pct = (final_pnl / (entry_price * size)) * 100 if entry_price > 0 else 0

        result_emoji = "✅" if final_pnl > 0 else "❌"

        msg = (
            f"🏁 *WeekendMomo CLOSE*\n\n"
            f"{result_emoji} *{ASSET} {direction.upper()}*\n\n"
            f"📈 Entry: ${entry_price:,.4f}\n"
            f"📉 Exit: ${close_price:,.4f}\n"
            f"💰 P&L: ${final_pnl:,.2f} ({final_pnl_pct:+.2f}%)\n"
            f"📊 Momentum war: {state.get('momentum_pct', 0):+.2f}%\n\n"
            f"📋 Grund: Sonntag-Abend Timeout\n"
            f"{'🎉 Gewinn!' if final_pnl > 0 else '😤 Verlust - Weiter gehts!'}"
        )
        print(f"\n{result_emoji} Position geschlossen: ${final_pnl:,.2f} ({final_pnl_pct:+.2f}%)")
        send_telegram_message(msg)

        # Trade loggen
        log_trade({
            "asset": ASSET,
            "direction": f"close_{direction}",
            "entry_price": entry_price,
            "exit_price": close_price,
            "size": size,
            "pnl": final_pnl,
            "pnl_pct": final_pnl_pct,
            "close_reason": "sunday_timeout"
        })

    else:
        msg = f"❌ WeekendMomo: Close fehlgeschlagen - {close_result.get('error')}"
        print(msg)
        send_telegram_message(msg)

    # State zuruecksetzen
    state["closed"] = True
    state["close_reason"] = "sunday_timeout"
    save_state(state)
    print("NO_REPLY")


# === CLI Interface ===

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python weekend_momo.py --check    # Freitag: Momentum pruefen")
        print("  python weekend_momo.py --entry    # Samstag: Trade ausfuehren")
        print("  python weekend_momo.py --exit     # Sonntag: Position schliessen")
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
        if state:
            print(json.dumps(state, indent=2))
        else:
            print("Kein State vorhanden")
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
