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
DAILY_PNL_FILE = os.path.join(DATA_DIR, "daily_pnl.json")  # Opt 2: Daily-DD-Breaker
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
            "body_ratio": trade.get("body_ratio"),
            "close_position": trade.get("close_position"),
            "scan_latency_sec": trade.get("scan_latency_sec"),
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

    # KRITISCH: Nur den alten loss_plan canceln – TP1 und TP2 (beide profit_plan)
    # bleiben aktiv. Cancel-All würde die TPs killen und das Upside zerstören.
    cancel_ok = client.cancel_tpsl_orders(pos.coin, plan_types=["loss_plan"])
    if not cancel_ok:
        print(f"   ❌ Cancel SL fehlgeschlagen – BE-SL nicht gesetzt")
        return False

    sl_result = client.place_stop_loss(pos.coin, new_sl, abs(pos.size), hold_side=hold_side)
    if not sl_result.success:
        print(f"   ❌ BE-SL setzen fehlgeschlagen: {sl_result.error}")
        send_telegram_notification(
            f"Kleines Problem beim {pos.coin} {direction_str} — Break-Even SL konnte nicht gesetzt werden. Bitte manuell auf ${new_sl:,.4f} nachziehen."
        )
        return False

    print(f"   ✅ BE-SL gesetzt @ ${new_sl:,.4f}")
    send_telegram_notification(
        f"Break-Even erreicht beim {pos.coin} {direction_str}. SL auf ${new_sl:,.4f} nachgezogen — läuft jetzt risikofrei."
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
                if total_pnl > 0:
                    if pnl_r >= 2.5:
                        base_reason = "TP2_WIN"
                    elif pnl_r >= 0.8:
                        base_reason = "TP1_WIN"
                    else:
                        base_reason = "PARTIAL_WIN"
                    exit_reason = f"BE_{base_reason}" if be_applied else base_reason
                elif be_applied and total_pnl >= 0:
                    exit_reason = "BE_BREAKEVEN"
                else:
                    exit_reason = "LOSS"

                trades[i]["exit_timestamp"] = datetime.now().isoformat()
                trades[i]["exit_price"] = exit_price
                trades[i]["exit_pnl_usd"] = round(total_pnl, 4)
                trades[i]["exit_pnl_r"] = pnl_r
                trades[i]["exit_reason"] = exit_reason
                trades[i]["be_applied"] = be_applied
                trades[i]["funding_paid_usd"] = funding_paid_usd
                updated_trade = trades[i]
                break

        if updated_trade is None:
            print(f"   ⚠️  Kein offener Trade für {coin} in trades.json – Exit nicht geloggt")
            return

        # Atomares Schreiben: tmp-Datei + rename verhindert korrupte JSON bei Absturz
        tmp_file = TRADES_FILE + ".tmp"
        with open(tmp_file, 'w') as f:
            json.dump(trades, f, indent=2)
        os.replace(tmp_file, TRADES_FILE)
        funding_str = f" | Funding ${funding_paid_usd:+.4f}" if funding_paid_usd is not None else ""
        print(f"   📝 Trade-Exit geloggt: {coin} | PnL ${total_pnl:.2f} ({pnl_r}R) | {exit_reason}{funding_str}")

        # Event für Claude-Session-Start: Pending Note für trade_log.md erzeugen
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


def update_daily_pnl(pnl_usd: float, pnl_r: float) -> None:
    """Opt 2 – Tages-PnL-Tracker für Daily-DD-Circuit-Breaker.

    Auto-Reset bei Datumswechsel. Atomar geschrieben.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if os.path.exists(DAILY_PNL_FILE):
            with open(DAILY_PNL_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") != today:
                data = {"date": today, "realized_pnl_usd": 0.0, "realized_r": 0.0,
                        "trades_closed": 0, "kill_alert_sent": False}
        else:
            data = {"date": today, "realized_pnl_usd": 0.0, "realized_r": 0.0,
                    "trades_closed": 0, "kill_alert_sent": False}

        data["realized_pnl_usd"] = round(data.get("realized_pnl_usd", 0.0) + float(pnl_usd), 4)
        data["realized_r"] = round(data.get("realized_r", 0.0) + float(pnl_r), 3)
        data["trades_closed"] = data.get("trades_closed", 0) + 1
        data["last_update"] = datetime.now().isoformat()

        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = DAILY_PNL_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DAILY_PNL_FILE)
        print(f"   📅 Daily-PnL: {data['realized_r']:+.2f}R (${data['realized_pnl_usd']:+.2f}) nach {data['trades_closed']} Trades")
    except Exception as e:
        print(f"⚠️  Daily-PnL Update fehlgeschlagen: {e}")


def main():
    """Main monitoring logic — unterstützt mehrere parallele Positionen."""
    client = BitgetClient(dry_run=DRY_RUN)

    positions = client.get_positions()
    current_count = len(positions)
    state = load_state()

    # --- State-Migration: altes Flat-Format → active_trades-Dict ---
    # Altes Format: tracked_coin / position_opened_at / be_applied auf top-level
    # Neues Format: active_trades: { "AVAX": { tracked_coin, position_opened_at, be_applied }, ... }
    active_trades = state.get("active_trades")
    if active_trades is None:
        active_trades = {}
        if state.get("tracked_coin"):
            coin = state["tracked_coin"]
            active_trades[coin] = {
                "tracked_coin": coin,
                "position_opened_at": state.get("position_opened_at", 0),
                "be_applied": state.get("be_applied", False),
            }
            print(f"   🔄 State-Migration: Flat-Format → active_trades['{coin}']")

    orphan_notified = list(state.get("orphan_notified", []))
    last_position_count = state.get("last_position_count", 0)
    current_coins = {pos.coin for pos in positions}

    if current_count == 0 and last_position_count == 0 and not active_trades:
        print("\n⏸️  Keine Positionen - Monitor idle")
        save_state({
            "last_position_count": 0,
            "last_check": datetime.now().isoformat(),
            "active_trades": {},
            "orphan_notified": orphan_notified,
        })
        return 0

    new_active_trades = {}

    # --- Exit-Detection: Coins die vorher getrackt wurden, jetzt aber weg sind ---
    for coin, trade_state in active_trades.items():
        if coin in current_coins:
            continue  # noch offen → weiter unten verarbeiten

        # Idempotenz-Guard: Falls Exit bereits in einem früheren Run verarbeitet wurde
        # (z.B. nach Exception vor save_state), nicht erneut melden/loggen.
        last_trade_check = load_last_trade(coin)
        if last_trade_check and last_trade_check.get("exit_timestamp"):
            print(f"   ℹ️  {coin} Exit bereits geloggt – State-Cleanup")
            continue

        opened_at_ms = trade_state.get("position_opened_at", 0)
        be_was_applied = trade_state.get("be_applied", False)

        print("\n" + "=" * 60)
        print(f"🎯 POSITION GESCHLOSSEN: {coin}")
        print("=" * 60)

        total_pnl, exit_price, total_size = get_total_trade_pnl(client, coin, opened_at_ms)

        if exit_price:
            print(f"\n💰 FINAL RESULT:")
            print(f"   Asset: {coin}")
            print(f"   Exit:  ${exit_price:,.4f}")
            print(f"   Size:  {total_size:.4f}")
            print(f"   P&L:   ${total_pnl:,.2f}")

            balance = client.get_balance()
            print(f"\nAktuelle Balance: ${balance:,.2f} USDT")

            last_trade = load_last_trade(coin) or {}
            direction = (last_trade.get("direction") or "?").upper()
            entry_price = last_trade.get("entry_price", 0)
            risk_usd = last_trade.get("risk_usd") or 1.0
            pnl_r = round(total_pnl / risk_usd, 2) if risk_usd else 0.0

            sign = "+" if total_pnl >= 0 else ""
            if total_pnl > 0:
                result_line = f"Schöner Trade — {sign}${total_pnl:.2f} ({sign}{pnl_r}R). Book jetzt ${balance:,.2f}."
            elif total_pnl < 0:
                result_line = f"{coin} {direction} hat nicht funktioniert. {sign}${total_pnl:.2f} ({sign}{pnl_r}R). Book ${balance:,.2f}. Nächste Session."
            else:
                result_line = f"{coin} {direction} bei Break-Even raus. Book ${balance:,.2f}."

            message = (
                f"{result_line}\n"
                f"Entry ${entry_price:,.4f} · Exit ${exit_price:,.4f}"
            )
            send_telegram_notification(message)
            update_pnl_tracker(total_pnl)
            update_daily_pnl(total_pnl, pnl_r)  # Opt 2: Daily-DD-Breaker-Input

            funding_paid = client.get_funding_paid(coin, opened_at_ms) if opened_at_ms else None
            update_trade_with_exit(coin, total_pnl, exit_price, be_was_applied, funding_paid)
        else:
            print(f"⚠️  Keine Fill-Daten für {coin} verfügbar")
            send_telegram_notification(f"{coin} wurde geschlossen, aber die Trade-Details sind nicht auffindbar. Bitte kurz manuell prüfen.")

    # --- Aktive Positionen: Tracking + Break-Even Check (alle Coins) ---
    for pos in positions:
        coin = pos.coin
        is_long = pos.size > 0
        print(f"\n✅ Position läuft weiter: {coin} {'LONG' if is_long else 'SHORT'} | P&L: ${pos.unrealized_pnl:.2f}")

        per_coin = active_trades.get(coin, {})

        if not per_coin or "position_opened_at" not in per_coin:
            # Neue Position: Timestamp aus trades.json holen
            last_trade = load_last_trade(coin)
            ts_str = last_trade.get("timestamp") if last_trade else None
            if ts_str:
                # 30s Puffer: Trade-Log-Timestamp kann nach tatsächlichem Fill liegen
                opened_at_ms = int(datetime.fromisoformat(ts_str).timestamp() * 1000) - 30_000
            else:
                opened_at_ms = int(datetime.now().timestamp() * 1000)
            new_per_coin = {
                "tracked_coin": coin,
                "position_opened_at": opened_at_ms,
                "be_applied": False,
            }
        else:
            new_per_coin = {
                "tracked_coin": coin,
                "position_opened_at": per_coin.get("position_opened_at"),
                "be_applied": per_coin.get("be_applied", False),
            }

        # Break-Even Check: SL auf Entry verschieben wenn 1R Gewinn erreicht
        be_applied = check_and_apply_break_even(client, pos, new_per_coin)
        if be_applied:
            new_per_coin["be_applied"] = True

        new_active_trades[coin] = new_per_coin

    # --- Orphan-Detection: nur wenn aktuell keine Positionen offen ---
    if current_count == 0:
        if os.path.exists(TRADES_FILE):
            try:
                with open(TRADES_FILE, 'r') as f:
                    all_trades = json.load(f)
                now = datetime.now()

                def _trade_age_minutes(t):
                    ts = t.get("timestamp", "")[:19]
                    try:
                        return (now - datetime.fromisoformat(ts)).total_seconds() / 60
                    except Exception:
                        return 999

                orphaned = [
                    t for t in all_trades
                    if not t.get("exit_timestamp")
                    and t.get("exit_reason") != "ORPHANED"
                    and _trade_age_minutes(t) > 10  # 10 Min Grace-Period
                ]
                if orphaned:
                    already_notified = set(orphan_notified)
                    for ot in orphaned:
                        asset = ot.get("asset", "?")
                        ts = ot.get("timestamp", "?")[:16]
                        key = f"{asset}_{ts}"
                        print(f"   ⚠️  ORPHANED TRADE: {asset} {ot.get('direction','').upper()} vom {ts[:10]} – kein Bitget-Match")
                        if key not in already_notified:
                            send_telegram_notification(
                                f"Kurz prüfen — {asset} {ot.get('direction','').upper()} vom {ts[:10]} ist im Log, aber keine offene Position auf Bitget gefunden."
                            )
                            orphan_notified.append(key)
                            already_notified.add(key)
                else:
                    print("\n⏸️  Keine offenen Positionen")
            except Exception as e:
                print(f"   ⚠️  Orphaned-Check fehlgeschlagen: {e}")

    save_state({
        "last_position_count": current_count,
        "last_check": datetime.now().isoformat(),
        "active_trades": new_active_trades,
        "orphan_notified": orphan_notified,
    })
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
            send_telegram_notification(f"Bot-Fehler (position_monitor) — {e}")
        except Exception:
            pass
        print("NO_REPLY")
        sys.exit(1)
