#!/usr/bin/env python3
"""
APEX Dashboard API Server
=========================
Lauscht auf Port 8889.
Gibt Live-Positionen + Trade-Historie + PnL-Tracker als JSON zurück.

Authentifizierung: Bearer Token im Authorization-Header
"""

import os
import sys
import json
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request, abort
from flask_cors import CORS

# ── Pfad-Setup ────────────────────────────────────────────────────────────────

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from config.bot_config import DRY_RUN
from scripts.bitget_client import BitgetClient

# ── Konfiguration ─────────────────────────────────────────────────────────────

PORT = 8889
API_TOKEN = "71fd511c951ae0a8e925a36e831ab4f6487ed88173d06047844e467d9fb07694"
DATA_DIR = os.path.join(PROJECT_DIR, "data")

# ── App + CORS ────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": [
    "https://apex-dashboard-omega.vercel.app",
    "http://localhost:3000",
]}}, supports_credentials=True)

# Client wird lazy initialisiert (vermeidet Crash beim Import ohne Credentials)
_client = None

def get_client() -> BitgetClient:
    global _client
    if _client is None:
        _client = BitgetClient(dry_run=DRY_RUN)
    return _client

# ── Auth Decorator ────────────────────────────────────────────────────────────

def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # OPTIONS-Preflight immer durchlassen (CORS)
        if request.method == "OPTIONS":
            return "", 204
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != API_TOKEN:
            abort(401)
        return f(*args, **kwargs)
    return decorated

# ── JSON-Datei Helfer ─────────────────────────────────────────────────────────

def load_json(filename: str) -> any:
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

# ── Endpunkte ─────────────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET", "OPTIONS"])
def health():
    """Healthcheck – kein Token nötig"""
    return jsonify({
        "status": "ok",
        "dry_run": DRY_RUN,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })


@app.route("/api/dashboard", methods=["GET", "OPTIONS"])
@require_token
def dashboard():
    """
    Hauptendpunkt für das Vercel-Frontend.

    Liefert:
    - history:      Liste aller Trades aus trades.json
    - pnl_tracker:  Inhalt von pnl_tracker.json (oder null)
    - positions:    Live-Positionen angereichert mit SL/TP aus trades.json
    """
    client = get_client()

    # ── JSON-Daten laden ──────────────────────────────────────────────────────
    history = load_json("trades.json") or []
    pnl_tracker = load_json("pnl_tracker.json")

    # ── Live-Positionen ───────────────────────────────────────────────────────
    live_positions = []
    try:
        positions = client.get_positions()

        for pos in positions:
            coin = pos.coin

            # Passendsten Trade aus der Historie suchen (letzter offener Trade)
            matching_trade = None
            for trade in reversed(history):
                if trade.get("asset", "").upper() == coin.upper():
                    matching_trade = trade
                    break

            sl = None
            tp1 = None
            tp2 = None
            if matching_trade:
                sl = matching_trade.get("stop_loss")
                # Manche Trades haben split TP, andere nur einen
                tp1 = matching_trade.get("take_profit_1") or matching_trade.get("take_profit")
                tp2 = matching_trade.get("take_profit_2")

            live_positions.append({
                "coin": coin,
                "direction": "long" if pos.size > 0 else "short",
                "size": abs(pos.size),
                "entry_price": pos.entry_price,
                "unrealized_pnl": pos.unrealized_pnl,
                "leverage": pos.leverage,
                "liquidation_price": pos.liquidation_price,
                "stop_loss": sl,
                "take_profit_1": tp1,
                "take_profit_2": tp2,
                "is_break_even": pos.unrealized_pnl > 0,
            })
    except Exception as e:
        live_positions = [{"error": str(e)}]

    # ── Balance ───────────────────────────────────────────────────────────────
    balance = None
    try:
        balance = client.get_balance()
    except Exception:
        pass

    return jsonify({
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "dry_run": DRY_RUN,
        "balance_usdt": balance,
        "history": history,
        "pnl_tracker": pnl_tracker,
        "positions": live_positions,
    })


@app.route("/api/history", methods=["GET", "OPTIONS"])
@require_token
def history_only():
    """Nur Trade-Historie"""
    return jsonify(load_json("trades.json") or [])


@app.route("/api/pnl", methods=["GET", "OPTIONS"])
@require_token
def pnl_only():
    """Nur PnL-Tracker"""
    tracker = load_json("pnl_tracker.json")
    if tracker is None:
        return jsonify({"error": "pnl_tracker.json nicht gefunden"}), 404
    return jsonify(tracker)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("APEX Dashboard API Server")
    print("=" * 55)
    print(f"Port:     {PORT}")
    print(f"DRY_RUN:  {DRY_RUN}")
    print(f"Token:    {API_TOKEN}")
    print(f"Data-Dir: {DATA_DIR}")
    print("=" * 55)
    app.run(host="127.0.0.1", port=PORT, debug=False)
