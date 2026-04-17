#!/usr/bin/env python3
"""
APEX Selftest – Smoke-Test vor jedem Deploy.

Prüft kritische Logik ohne echte API-Calls. Läuft in <10s.
Regel: Vor jeder Code-Änderung/Deploy `python3 scripts/selftest.py` → muss grün sein.

Aktuell abgedeckt:
 1. Idempotenz-Guard im position_monitor (Ghost-Loop-Schutz)
 2. State-Migration Flat-Format → active_trades
 3. update_trade_with_exit: idempotent, atomar
 4. get_total_trade_pnl: Filter auf opened_at_ms
 5. Telegram-Sender: kein Markdown-Default (sonst 400 bei $-Zeichen)
 6. Syntax-Check aller Scripts
 7. Pre-Trade Sanity Check (Opt 1)
 8. Daily-DD-Tracker: Reset bei Datumswechsel + Accumulation (Opt 2)
 9. Daily-DD-Breaker: Kill bei ≤ DAILY_DD_KILL_R (Opt 2)
10. Weekly-Audit: aggregate + render produziert validen Report (Opt 4)
"""
import datetime
import json
import os
import py_compile
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))
sys.path.insert(0, str(PROJECT_DIR / "config"))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))


# ---------------------------------------------------------------------------
# Test 1: Idempotenz-Guard (Ghost-Loop-Schutz)
# ---------------------------------------------------------------------------
def test_idempotency_guard():
    """Simuliert: Exit schon geloggt → load_last_trade liefert exit_timestamp → Skip."""
    from scripts import position_monitor as pm

    with tempfile.TemporaryDirectory() as tmp:
        trades_file = Path(tmp) / "trades.json"
        trades = [{
            "asset": "ETH",
            "direction": "short",
            "entry_price": 2336.72,
            "exit_timestamp": "2026-04-16T21:00:00",
            "exit_price": 2335.42,
            "exit_pnl_usd": 0.70,
        }]
        trades_file.write_text(json.dumps(trades))

        orig = pm.TRADES_FILE
        pm.TRADES_FILE = str(trades_file)
        try:
            last = pm.load_last_trade("ETH")
            has_exit = bool(last.get("exit_timestamp"))
            record("idempotency_guard", has_exit,
                   "load_last_trade erkennt exit_timestamp korrekt")
        finally:
            pm.TRADES_FILE = orig


# ---------------------------------------------------------------------------
# Test 2: State-Migration Flat-Format → active_trades
# ---------------------------------------------------------------------------
def test_state_migration():
    from scripts import position_monitor as pm

    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "monitor_state.json"
        flat_state = {
            "last_position_count": 1,
            "tracked_coin": "AVAX",
            "position_opened_at": 1776000000000,
            "be_applied": True,
        }
        state_file.write_text(json.dumps(flat_state))

        orig = pm.STATE_FILE
        pm.STATE_FILE = str(state_file)
        try:
            state = pm.load_state()
            active = state.get("active_trades")
            if active is None and state.get("tracked_coin"):
                coin = state["tracked_coin"]
                active = {coin: {
                    "tracked_coin": coin,
                    "position_opened_at": state.get("position_opened_at", 0),
                    "be_applied": state.get("be_applied", False),
                }}
            ok = (active is not None and "AVAX" in active
                  and active["AVAX"]["be_applied"] is True)
            record("state_migration", ok,
                   "Flat-State → active_trades-Dict mit BE-Flag preserved")
        finally:
            pm.STATE_FILE = orig


# ---------------------------------------------------------------------------
# Test 3: update_trade_with_exit idempotent + atomar
# ---------------------------------------------------------------------------
def test_update_trade_with_exit():
    from scripts import position_monitor as pm

    with tempfile.TemporaryDirectory() as tmp:
        trades_file = Path(tmp) / "trades.json"
        pending_file = Path(tmp) / "pending_notes.jsonl"
        trades = [{
            "asset": "XRP",
            "direction": "long",
            "entry_price": 1.44,
            "risk_usd": 1.19,
            "timestamp": "2026-04-17T09:40:00",
        }]
        trades_file.write_text(json.dumps(trades))

        orig_trades = pm.TRADES_FILE
        orig_pending = pm.PENDING_NOTES_FILE
        orig_pnl = pm.PNL_TRACKER_FILE
        pm.TRADES_FILE = str(trades_file)
        pm.PENDING_NOTES_FILE = str(pending_file)
        pm.PNL_TRACKER_FILE = str(Path(tmp) / "pnl_tracker.json")
        try:
            # 1. Call: logged Exit
            pm.update_trade_with_exit("XRP", 0.66, 1.4531, be_applied=True, funding_paid_usd=None)
            after_first = json.loads(trades_file.read_text())
            first_ok = (after_first[0].get("exit_timestamp") is not None
                        and after_first[0].get("exit_pnl_usd") == 0.66
                        and "BE_" in after_first[0].get("exit_reason", ""))

            # 2. Call: idempotent – findet keinen offenen Trade
            pm.update_trade_with_exit("XRP", 99.0, 9.99, be_applied=False, funding_paid_usd=None)
            after_second = json.loads(trades_file.read_text())
            second_ok = after_second[0]["exit_pnl_usd"] == 0.66  # unverändert

            record("update_trade_with_exit", first_ok and second_ok,
                   "1. Call schreibt, 2. Call ist No-Op")
        finally:
            pm.TRADES_FILE = orig_trades
            pm.PENDING_NOTES_FILE = orig_pending
            pm.PNL_TRACKER_FILE = orig_pnl


# ---------------------------------------------------------------------------
# Test 4: get_total_trade_pnl Filter auf opened_at_ms
# ---------------------------------------------------------------------------
def test_pnl_filter():
    from scripts import position_monitor as pm

    class FakeClient:
        def __init__(self, fills):
            self._fills = fills

        def get_recent_fills(self, coin=None, limit=20):
            return self._fills

    opened_at = 1_776_000_000_000
    # Newest first: 2 nach opened, 1 davor (muss ausgefiltert werden)
    fills = [
        {"cTime": opened_at + 200_000, "profit": "0.30", "baseVolume": "72", "price": "1.4466"},
        {"cTime": opened_at + 100_000, "profit": "0.36", "baseVolume": "71", "price": "1.4531"},
        {"cTime": opened_at - 500_000, "profit": "99.0", "baseVolume": "1",  "price": "9.99"},
    ]
    total, exit_price, size = pm.get_total_trade_pnl(FakeClient(fills), "XRP", opened_at)

    ok = abs(total - 0.66) < 1e-6 and exit_price == 1.4466 and size == 143.0
    record("pnl_filter_opened_at_ms", ok,
           f"PnL={total:.2f}, Exit={exit_price}, Size={size} (erwartet 0.66 / 1.4466 / 143)")


# ---------------------------------------------------------------------------
# Test 5: Telegram-Sender – kein Markdown-Default
# ---------------------------------------------------------------------------
def test_telegram_no_markdown_default():
    import inspect
    from scripts import telegram_sender

    sig = inspect.signature(telegram_sender.send_telegram_message)
    parse_mode_default = sig.parameters["parse_mode"].default
    ok = parse_mode_default is None
    record("telegram_plain_text_default", ok,
           f"parse_mode default = {parse_mode_default!r} (erwartet None)")


# ---------------------------------------------------------------------------
# Test 6: Syntax aller Scripts
# ---------------------------------------------------------------------------
def test_syntax_all_scripts():
    scripts_dir = PROJECT_DIR / "scripts"
    broken = []
    for f in scripts_dir.glob("*.py"):
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            broken.append(f"{f.name}: {e.msg}")
    record("syntax_all_scripts", not broken,
           "alle scripts kompilieren" if not broken else f"Fehler: {broken}")


# ---------------------------------------------------------------------------
# Test 7: Pre-Trade Sanity Check (Opt 1)
# ---------------------------------------------------------------------------
def test_pre_trade_sanity_check():
    from scripts import autonomous_trade as at

    healthy = {
        "asset": "XRP", "direction": "long",
        "current_price": 1.44, "box_high": 1.4356, "box_low": 1.4316,
    }
    ok, reason, _ = at.pre_trade_sanity_check(healthy, risk_usd=1.19, balance=59.33)
    healthy_ok = ok and reason == "ok"

    low_balance = at.pre_trade_sanity_check(healthy, risk_usd=1.19, balance=5.0)
    low_balance_blocked = (not low_balance[0]) and low_balance[1] == "balance_too_low"

    broken_box = at.pre_trade_sanity_check(
        {**healthy, "box_high": 1.0, "box_low": 2.0}, risk_usd=1.19, balance=59.33)
    broken_box_blocked = (not broken_box[0]) and broken_box[1] == "invalid_box"

    zero_price = at.pre_trade_sanity_check({**healthy, "current_price": 0}, 1.19, 59.33)
    zero_price_blocked = (not zero_price[0]) and zero_price[1] == "invalid_entry_price"

    # SL > 10% vom Entry – z.B. ETH mit Box die 15% über Entry liegt
    wide_sl = at.pre_trade_sanity_check(
        {"asset": "ETH", "direction": "long",
         "current_price": 2000.0, "box_low": 1700.0, "box_high": 1750.0},
        risk_usd=1.19, balance=59.33)
    wide_sl_blocked = (not wide_sl[0]) and wide_sl[1] == "sl_distance_too_wide"

    ok = healthy_ok and low_balance_blocked and broken_box_blocked and zero_price_blocked and wide_sl_blocked
    record("pre_trade_sanity_check", ok,
           f"healthy=pass, low_bal/broken_box/zero_price/wide_sl=blocked")


# ---------------------------------------------------------------------------
# Test 8: Daily-PnL Tracker – Reset + Accumulation (Opt 2)
# ---------------------------------------------------------------------------
def test_daily_pnl_tracker():
    from scripts import position_monitor as pm

    with tempfile.TemporaryDirectory() as tmp:
        daily_file = Path(tmp) / "daily_pnl.json"
        orig = pm.DAILY_PNL_FILE
        orig_data_dir = pm.DATA_DIR
        pm.DAILY_PNL_FILE = str(daily_file)
        pm.DATA_DIR = tmp
        try:
            # 1. Call: neuer Tag, 1. Trade
            pm.update_daily_pnl(0.66, 0.56)
            d1 = json.loads(daily_file.read_text())
            step1 = (d1["trades_closed"] == 1 and abs(d1["realized_r"] - 0.56) < 1e-6
                     and abs(d1["realized_pnl_usd"] - 0.66) < 1e-6)

            # 2. Call: gleicher Tag, 2. Trade – Akkumulation
            pm.update_daily_pnl(-1.20, -1.0)
            d2 = json.loads(daily_file.read_text())
            step2 = (d2["trades_closed"] == 2 and abs(d2["realized_r"] - (-0.44)) < 1e-6)

            # 3. Reset bei Datumswechsel (simuliert durch Manipulation)
            d2["date"] = "2020-01-01"
            daily_file.write_text(json.dumps(d2))
            pm.update_daily_pnl(0.5, 0.5)
            d3 = json.loads(daily_file.read_text())
            step3 = (d3["trades_closed"] == 1 and abs(d3["realized_r"] - 0.5) < 1e-6
                     and d3["date"] == datetime.datetime.now().strftime("%Y-%m-%d"))

            record("daily_pnl_tracker", step1 and step2 and step3,
                   f"step1/2/3 = {step1}/{step2}/{step3}")
        finally:
            pm.DAILY_PNL_FILE = orig
            pm.DATA_DIR = orig_data_dir


# ---------------------------------------------------------------------------
# Test 9: Daily-DD-Breaker – blockiert bei ≤ Kill-Threshold (Opt 2)
# ---------------------------------------------------------------------------
def test_daily_dd_breaker():
    from scripts import autonomous_trade as at

    with tempfile.TemporaryDirectory() as tmp:
        orig = at.DAILY_PNL_FILE
        at.DAILY_PNL_FILE = str(Path(tmp) / "daily_pnl.json")
        try:
            # Fall 1: kein File → ok, daily_r = 0
            ok, reason, _ = at.check_daily_dd_breaker()
            case1 = ok and reason == "ok"

            # Fall 2: Tages-R bei -1.5 (noch ok, > -2.0)
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            Path(at.DAILY_PNL_FILE).write_text(json.dumps({
                "date": today, "realized_pnl_usd": -1.8, "realized_r": -1.5,
                "trades_closed": 2, "kill_alert_sent": False,
            }))
            ok, reason, _ = at.check_daily_dd_breaker()
            case2 = ok and reason == "ok"

            # Fall 3: Tages-R bei -2.5 (≤ -2.0 → KILL)
            Path(at.DAILY_PNL_FILE).write_text(json.dumps({
                "date": today, "realized_pnl_usd": -3.0, "realized_r": -2.5,
                "trades_closed": 3, "kill_alert_sent": False,
            }))
            ok, reason, ctx = at.check_daily_dd_breaker()
            case3 = (not ok) and reason == "daily_dd_kill" and ctx["daily_r"] == -2.5

            record("daily_dd_breaker", case1 and case2 and case3,
                   f"empty/ok(-1.5R)/kill(-2.5R) = {case1}/{case2}/{case3}")
        finally:
            at.DAILY_PNL_FILE = orig


# ---------------------------------------------------------------------------
# Test 10: Weekly-Audit Report-Rendering (Opt 4)
# ---------------------------------------------------------------------------
def test_weekly_audit_render():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "weekly_audit", str(PROJECT_DIR / "scripts" / "weekly_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import datetime as dt
    since = dt.datetime(2026, 4, 10)
    until = dt.datetime(2026, 4, 17)
    fake_entries = [
        {"reason": "already_traded", "session": "eu", "asset": None},
        {"reason": "already_traded", "session": "eu", "asset": None},
        {"reason": "no_breakout", "session": "us", "asset": "XRP"},
        {"reason": "box_too_old", "session": "tokyo", "asset": "ETH"},
    ]
    fake_trades = [
        {"exit_timestamp": "2026-04-12T10:00:00", "exit_pnl_usd": 0.66, "exit_pnl_r": 0.56, "exit_reason": "BE_WIN"},
        {"exit_timestamp": "2026-04-13T10:00:00", "exit_pnl_usd": -1.29, "exit_pnl_r": -1.0, "exit_reason": "LOSS"},
    ]
    agg = mod.aggregate(fake_entries)
    ts = mod.summarize_trades(fake_trades)
    report = mod.render_report(since, until, agg, ts)

    ok = (
        agg["total"] == 4
        and agg["by_reason"]["already_traded"] == 2
        and ts["winrate"] == 50.0
        and "Skip-Audit" in report
        and "Total Skips" in report
    )
    record("weekly_audit_render", ok, f"agg={agg['total']}, winrate={ts['winrate']}%")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main() -> int:
    tests = [
        test_syntax_all_scripts,
        test_idempotency_guard,
        test_state_migration,
        test_update_trade_with_exit,
        test_pnl_filter,
        test_telegram_no_markdown_default,
        test_pre_trade_sanity_check,
        test_daily_pnl_tracker,
        test_daily_dd_breaker,
        test_weekly_audit_render,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            record(t.__name__, False, f"Exception: {e!r}")

    print("=" * 60)
    print("APEX SELFTEST")
    print("=" * 60)
    for name, ok, detail in RESULTS:
        mark = "✅" if ok else "❌"
        print(f"{mark} {name}")
        if detail:
            print(f"   └─ {detail}")

    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print("=" * 60)
    if failed:
        print(f"❌ {failed}/{len(RESULTS)} Tests fehlgeschlagen – NICHT deployen")
        return 1
    print(f"✅ Alle {len(RESULTS)} Tests grün – Deploy freigegeben")
    return 0


if __name__ == "__main__":
    sys.exit(main())
