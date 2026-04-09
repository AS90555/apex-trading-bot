#!/usr/bin/env python3
"""
APEX - Position Monitor
=======================
Checkt ob Positionen geschlossen wurden und meldet Ergebnisse.
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.bitget_client import BitgetClient

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "monitor_state.json")
PNL_TRACKER_FILE = os.path.join(DATA_DIR, "pnl_tracker.json")
PENDING_NOTES_FILE = os.path.join(DATA_DIR, "pending_notes.jsonl")
DEEP_REVIEW_FLAG_FILE = os.path.join(DATA_DIR, "deep_review_pending.flag")
DEEP_REVIEW_THRESHOLD = 10  # Alle 10 Trades Deep Review triggern

sys.path.insert(0, os.path.join(PROJECT_DIR, "config"))
try:
    from bot_config import DRY_RUN
except ImportError:
    DRY_RUN = True


TRADES_FILE = os.path.join(DATA_DIR, "trades.json")


def load_state():
    """Load last known state (resilient gegen korrupte JSON)"""
    if not os.path.exists(STATE_FILE):
        return {"last_position_count": 0, "last_check": None}
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  state.json unlesbar ({e}) – starte mit leerem State")
        return {"last_position_count": 0, "last_check": None}


def save_state(state):
    """Save current state (atomar via tmp+rename)"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp_file = STATE_FILE + ".tmp"
    with open(tmp_file, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_file, STATE_FILE)


def get_total_trade_pnl(client, coin: str, opened_at_ms: int):
    """Summiert P&L aller Fills seit Position-Eröffnung (TP1 + TP2/SL).
    Returns: (total_pnl, exit_price, total_size)
    """
    fills = client.get_recent_fills(coin=coin, limit=20) if coin else client.get_recent_fills(limit=10)
    total_pnl = 0.0
    total_size = 0.0
    exit_price = 0.0

    for fill in fills:
        fill_time = int(fill.get("cTime", 0))
        # Fills sind newest-first: Timestamp fehlt (0) oder älter als Trade-Start → Stop
        if not fill_time or (opened_at_ms and fill_time < opened_at_ms):
            break
        total_pnl += float(fill.get("profit", 0))
        size = float(fill.get("baseVolume", fill.get("size", fill.get("fillSz", 0))))
        total_size += size
        if not exit_price:
            exit_price = float(fill.get("price", 0))

    return total_pnl, exit_price, total_size


def send_telegram_notification(message):
    """Send notification via telegram_sender module"""
    try:
        from telegram_sender import send_telegram_message
        send_telegram_message(message)
    except Exception as e:
        print(f"⚠️  Telegram notification error: {e}")


def append_pending_note(trade: dict):
    """Schreibt einen Roh-Event für einen geschlossenen Trade nach data/pending_notes.jsonl.

    Claude verarbeitet diese Einträge beim nächsten Session-Start und transformiert
    sie in memory/trade_log.md Kurz-Notizen. Append-only JSONL.
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        note = {
            "ts": trade.get("exit_timestamp") or datetime.now().isoformat(),
            "asset": trade.get("asset"),
            "session": trade.get("session"),
            "direction": trade.get("direction"),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "pnl_usd": trade.get("exit_pnl_usd"),
            "pnl_r": trade.get("exit_pnl_r"),
            "exit_reason": trade.get("exit_reason"),
            "be_applied": trade.get("be_applied", False),
            "box_range": trade.get("box_range"),
            "box_age_min": trade.get("box_age_min"),
            "breakout_distance": trade.get("breakout_distance"),
            "volume_ratio": trade.get("volume_ratio"),
            "slippage_usd": trade.get("slippage_usd"),
            "funding_paid_usd": trade.get("funding_paid_usd"),
        }
        with open(PENDING_NOTES_FILE, "a") as f:
            f.write(json.dumps(note) + "\n")
        print(f"   📨 Pending-Note geschrieben für {note['asset']}")
    except Exception as e:
        print(f"⚠️  Pending-Note Schreibfehler: {e}")


def load_last_trade(coin: str) -> dict:
    """Lade den letzten Trade für ein Asset aus trades.json"""
    if not os.path.exists(TRADES_FILE):
        return {}
    try:
        with open(TRADES_FILE, 'r') as f:
            trades = json.load(f)
        for t in reversed(trades):
            if t.get("asset") == coin:
                return t
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def check_and_apply_break_even(client, pos, state: dict) -> bool:
    """
    Prüft ob Break-Even-Bedingung erreicht ist (1R Gewinn) und verschiebt SL.

    Long:  neuer SL = entry + fee_buffer (SL geht hoch)
    Short: neuer SL = entry - fee_buffer (SL geht runter)

    Returns True wenn BE angewendet wurde, sonst False.
    """
    if state.get("be_applied"):
        return False  # Bereits angewendet

    last_trade = load_last_trade(pos.coin)
    if not last_trade:
        return False

    entry_price = float(last_trade.get("entry_price", 0))
    original_sl = float(last_trade.get("stop_loss", 0))
    if not entry_price or not original_sl:
        return False

    risk_per_unit = abs(entry_price - original_sl)  # 1R in Preis-Einheiten
    if risk_per_unit == 0:
        return False

    is_long = pos.size > 0

    # Preis: API bevorzugt, Fallback auf unrealized_pnl-Berechnung
    try:
        current_price = client.get_price(pos.coin)
    except Exception as e:
        print(f"   ⚠️  Preisabfrage fehlgeschlagen ({e}) – Fallback auf Position-Daten")
        current_price = pos.entry_price + (pos.unrealized_pnl / abs(pos.size)) if abs(pos.size) > 0 else entry_price

    # 1R-Bedingung prüfen
    if is_long:
        be_triggered = current_price >= entry_price + risk_per_unit
    else:
        be_triggered = current_price <= entry_price - risk_per_unit

    if not be_triggered:
        return False

    # Break-Even SL berechnen (Entry + kleiner Fee-Buffer)
    fee_buffer = risk_per_unit * 0.05  # 5% des initialen Risikoabstands
    if is_long:
        new_sl = entry_price + fee_buffer
        # Nur sinnvoll wenn neuer SL über altem SL liegt
        if new_sl <= original_sl:
            return False
    else:
        new_sl = entry_price - fee_buffer
        if new_sl >= original_sl:
            return False

    hold_side = "long" if is_long else "short"
    direction_str = "LONG" if is_long else "SHORT"
    print(f"\n🛡️  Break-Even Trigger: {pos.coin} {direction_str}")
    print(f"   1R erreicht: Preis ${current_price:,.4f} vs. 1R-Level ${entry_price + (risk_per_unit if is_long else -risk_per_unit):,.4f}")
    print(f"   SL-Verschiebung: ${original_sl:,.4f} → ${new_sl:,.4f} (Entry + Fee-Buffer)")

    # KRITISCH: Nur den alten loss_plan canceln – TP1 (profit_plan) und Trailing (moving_plan)
    # bleiben aktiv. Cancel-All würde den Trailing Stop killen und das gesamte Upside zerstören.
    cancel_ok = client.cancel_tpsl_orders(pos.coin, plan_types=["loss_plan"])
    if not cancel_ok:
        print(f"   ❌ Cancel SL fehlgeschlagen – BE-SL nicht gesetzt")
        return False

    sl_result = client.place_stop_loss(pos.coin, new_sl, abs(pos.size), hold_side=hold_side)
    if not sl_result.success:
        print(f"   ❌ BE-SL setzen fehlgeschlagen: {sl_result.error}")
        send_telegram_notification(
            f"⚠️ APEX: Break-Even SL FEHLER\n{pos.coin} {direction_str}\n"
            f"Cancel OK, aber neuer SL @ ${new_sl:,.4f} konnte nicht gesetzt werden!\n"
            f"Manuell handeln!"
        )
        return False

    # Sicherheitsnetz: Falls das Trailing aus irgendeinem Grund nicht mehr aktiv ist
    # (z.B. manuell entfernt oder durch früheren Cancel-Bug verloren), neu setzen.
    # Wichtig: GET muss erfolgreich sein – sonst Duplikat-Risiko bei API-Hiccup.
    trailing_restored = False
    try:
        active_orders = client.get_tpsl_orders(pos.coin)
        # Plausibilität: unser frisch platzierter BE-SL muss sichtbar sein,
        # sonst hat der GET vermutlich gefailed → kein Re-Place wagen.
        be_sl_visible = any(o.get("planType") == "loss_plan" for o in active_orders)
        has_trailing = any(o.get("planType") == "moving_plan" for o in active_orders)
        if be_sl_visible and not has_trailing:
            trail_pct = float(last_trade.get("trail_pct", 0) or 0)
            trailing_activation = float(last_trade.get("trailing_activation", 0) or 0)
            size_tp2 = float(last_trade.get("size_tp2", 0) or 0)
            if not size_tp2:
                # Fallback: verbleibende Position (TP1 könnte schon gefüllt sein)
                size_tp2 = abs(pos.size)
            if trail_pct > 0 and trailing_activation > 0 and size_tp2 > 0:
                tp2_r = client.place_trailing_stop(
                    pos.coin, trail_pct, trailing_activation, size_tp2, hold_side=hold_side
                )
                trailing_restored = tp2_r.success
                if trailing_restored:
                    print(f"   🔄 Trailing Stop neu gesetzt @ ${trailing_activation:,.4f} ({trail_pct*100:.2f}%)")
                else:
                    print(f"   ⚠️  Trailing-Replace fehlgeschlagen: {tp2_r.error}")
                    send_telegram_notification(
                        f"⚠️ APEX: Trailing-Restore FEHLER\n\n"
                        f"{pos.coin} {direction_str}\n"
                        f"BE-SL aktiv, aber Trailing konnte nicht neu gesetzt werden:\n"
                        f"{tp2_r.error}\n"
                        f"Trade läuft mit BE-SL bis manueller Eingriff."
                    )
    except Exception as e:
        print(f"   ⚠️  Trailing-Check fehlgeschlagen: {e}")
        send_telegram_notification(
            f"⚠️ APEX: Trailing-Check FEHLER\n\n"
            f"{pos.coin} {direction_str}\n"
            f"BE wurde gesetzt, aber Trailing-Plausibilität fehlgeschlagen:\n"
            f"{e}\n"
            f"Trade läuft mit BE-SL bis manueller Eingriff."
        )

    print(f"   ✅ BE-SL gesetzt @ ${new_sl:,.4f}")
    extra = " | Trailing wiederhergestellt" if trailing_restored else ""
    send_telegram_notification(
        f"🛡️ APEX: Break-Even aktiv\n\n"
        f"{pos.coin} {direction_str}\n"
        f"1R erreicht – SL auf ${new_sl:,.4f} nachgezogen{extra}\n"
        f"(Entry: ${entry_price:,.4f} | Buffer: ${fee_buffer:,.4f})"
    )
    return True


def update_trade_with_exit(coin: str, total_pnl: float, exit_price: float, be_applied: bool, funding_paid_usd=None):
    """Schreibt Exit-Daten zurück in das passende Trade-Entry in trades.json.

    funding_paid_usd: Optional float – Summe der gezahlten Funding-Kosten über die Haltedauer.
                      None wenn Bitget-API-Abfrage fehlschlug oder nicht versucht.
    """
    if not os.path.exists(TRADES_FILE):
        return
    try:
        with open(TRADES_FILE, 'r') as f:
            trades = json.load(f)

        updated_trade = None
        for i in range(len(trades) - 1, -1, -1):
            t = trades[i]
            if t.get("asset") == coin and not t.get("exit_timestamp"):
                risk_usd = t.get("risk_usd") or 1.0
                pnl_r = round(total_pnl / risk_usd, 2) if risk_usd else 0.0
                exit_reason = "WIN" if total_pnl > 0 else "LOSS"
                if be_applied and total_pnl >= 0:
                    exit_reason = "BE_WIN" if total_pnl > 0 else "BE_BREAKEVEN"

                trades[i]["exit_timestamp"] = datetime.now().isoformat()
                trades[i]["exit_price"] = exit_price
                trades[i]["exit_pnl_usd"] = round(total_pnl, 4)
                trades[i]["exit_pnl_r"] = pnl_r
                trades[i]["exit_reason"] = exit_reason
                trades[i]["be_applied"] = be_applied
                trades[i]["funding_paid_usd"] = funding_paid_usd
                updated_trade = trades[i]
                break

        # Atomares Schreiben: tmp-Datei + rename verhindert korrupte JSON bei Absturz
        tmp_file = TRADES_FILE + ".tmp"
        with open(tmp_file, 'w') as f:
            json.dump(trades, f, indent=2)
        os.replace(tmp_file, TRADES_FILE)
        funding_str = f" | Funding ${funding_paid_usd:+.4f}" if funding_paid_usd is not None else ""
        print(f"   📝 Trade-Exit geloggt: {coin} | PnL ${total_pnl:.2f} ({pnl_r}R) | {exit_reason}{funding_str}")

        # Event für Claude-Session-Start: Pending Note für trade_log.md erzeugen
        if updated_trade is not None:
            append_pending_note(updated_trade)
    except Exception as e:
        print(f"⚠️  Trade-Exit Logging Fehler: {e}")


def update_pnl_tracker(pnl):
    """Update P&L tracker with realized profit"""
    if not os.path.exists(PNL_TRACKER_FILE):
        return

    try:
        with open(PNL_TRACKER_FILE, 'r') as f:
            tracker = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    # Update realized P&L
    tracker["realized_pnl"] = tracker.get("realized_pnl", 0) + pnl
    tracker["total_pnl"] = tracker["realized_pnl"] + tracker.get("unrealized_pnl", 0)

    # Update trade counts
    if pnl > 0:
        tracker["winning_trades"] = tracker.get("winning_trades", 0) + 1
    else:
        tracker["losing_trades"] = tracker.get("losing_trades", 0) + 1

    tracker["total_trades"] = tracker.get("total_trades", 0) + 1
    tracker["last_updated"] = datetime.now().isoformat()

    # Deep-Review Counter: alle DEEP_REVIEW_THRESHOLD Trades Flag setzen
    tracker["trades_since_last_review"] = tracker.get("trades_since_last_review", 0) + 1
    if tracker["trades_since_last_review"] >= DEEP_REVIEW_THRESHOLD:
        try:
            with open(DEEP_REVIEW_FLAG_FILE, "w") as f:
                f.write(datetime.now().isoformat() + "\n")
            print(f"\n🧪 Deep Review fällig – Flag gesetzt ({tracker['trades_since_last_review']} Trades seit letztem Review)")
        except Exception as e:
            print(f"⚠️  Deep-Review Flag konnte nicht gesetzt werden: {e}")

    # Check milestones
    for milestone_name, milestone in tracker.get("milestones", {}).items():
        if not milestone.get("reached", False):
            if tracker["total_pnl"] >= milestone["target"]:
                milestone["reached"] = True
                print(f"\n🎉 MILESTONE REACHED: +${milestone['target']} → Bonus: +${milestone['bonus']} USDC!")

    # Atomar: tmp + replace verhindert korrupte JSON bei Crash mid-write
    tmp_file = PNL_TRACKER_FILE + ".tmp"
    with open(tmp_file, 'w') as f:
        json.dump(tracker, f, indent=2)
    os.replace(tmp_file, PNL_TRACKER_FILE)


def main():
    """Main monitoring logic"""
    client = BitgetClient(dry_run=DRY_RUN)

    positions = client.get_positions()
    current_count = len(positions)
    state = load_state()
    last_count = state.get("last_position_count", 0)

    if current_count == 0 and last_count == 0:
        print("\n⏸️  Keine Positionen - Monitor idle")
        return current_count

    new_state = {"last_position_count": current_count, "last_check": datetime.now().isoformat()}

    if last_count > 0 and current_count == 0:
        # Position geschlossen — alle Fills seit Eröffnung summieren
        tracked_coin = state.get("tracked_coin")
        opened_at_ms = state.get("position_opened_at", 0)

        print("\n" + "=" * 60)
        print("🎯 POSITION GESCHLOSSEN!")
        print("=" * 60)

        total_pnl, exit_price, total_size = get_total_trade_pnl(client, tracked_coin, opened_at_ms)
        coin = tracked_coin or "?"

        if exit_price:
            print(f"\n💰 FINAL RESULT:")
            print(f"   Asset: {coin}")
            print(f"   Exit:  ${exit_price:,.4f}")
            print(f"   Size:  {total_size:.4f}")
            print(f"   P&L:   ${total_pnl:,.2f}")

            balance = client.get_balance()
            print(f"\nAktuelle Balance: ${balance:,.2f} USDT")

            emoji = "✅" if total_pnl > 0 else "❌"
            result_text = f"GEWINN: +${total_pnl:.2f}" if total_pnl > 0 else f"VERLUST: ${total_pnl:.2f}"

            message = (
                f"🎯 APEX TRADE GESCHLOSSEN!\n\n"
                f"{emoji} {result_text}\n\n"
                f"Asset: {coin}\n"
                f"Exit: ${exit_price:,.4f}\n"
                f"Size: {total_size:.4f}\n\n"
                f"💰 Neue Balance: ${balance:,.2f} USDT"
            )
            print(f"\n{emoji} {result_text}")
            send_telegram_notification(message)
            update_pnl_tracker(total_pnl)

            # Funding-Kosten über die Haltedauer aggregieren (None bei API-Fehler)
            funding_paid = client.get_funding_paid(coin, opened_at_ms) if opened_at_ms else None

            # Exit-Daten zurück in trades.json schreiben
            be_was_applied = state.get("be_applied", False)
            update_trade_with_exit(coin, total_pnl, exit_price, be_was_applied, funding_paid)
        else:
            print("⚠️  Keine Fill-Daten verfügbar")
            send_telegram_notification("🎯 APEX: Position geschlossen, aber keine Trade-Details gefunden.")

    elif current_count > 0:
        pos = positions[0]
        print(f"\n✅ Position läuft weiter:")
        print(f"   {pos.coin} {'LONG' if pos.size > 0 else 'SHORT'}")
        print(f"   P&L: ${pos.unrealized_pnl:.2f}")

        # Position-Tracking: opened_at und coin merken für späteren P&L
        if last_count == 0 or "position_opened_at" not in state:
            new_state["position_opened_at"] = int(datetime.now().timestamp() * 1000)
            new_state["tracked_coin"] = pos.coin
            new_state["be_applied"] = False  # Reset BE-Flag bei neuer Position
        else:
            new_state["position_opened_at"] = state.get("position_opened_at")
            new_state["tracked_coin"] = state.get("tracked_coin", pos.coin)
            new_state["be_applied"] = state.get("be_applied", False)

        # Break-Even Check: SL auf Entry verschieben wenn 1R Gewinn erreicht
        be_applied = check_and_apply_break_even(client, pos, new_state)
        if be_applied:
            new_state["be_applied"] = True
    else:
        print("\n⏸️  Keine offenen Positionen")

    save_state(new_state)
    return current_count


if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging()
    try:
        count = main()
        print("NO_REPLY")
        sys.exit(0)
    except Exception as e:
        print(f"\n💥 ERROR: {e}")
        import traceback
        traceback.print_exc()
        try:
            send_telegram_notification(f"💥 APEX position_monitor.py ERROR: {e}")
        except Exception:
            pass
        print("NO_REPLY")
        sys.exit(1)
