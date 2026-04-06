#!/usr/bin/env python3
"""
APEX - Bitget Client
====================
Ersetzt hyperliquid_client.py und place_order.py.
USDT-Perpetual Futures via Bitget REST API v2.

DRY_RUN=True  → kein echtes Geld, nur Simulation
DRY_RUN=False → Live-Trading
"""

import os
import sys
import json
import time
import hmac
import hashlib
import base64
import requests
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass

# ========================
# CONSTANTS
# ========================

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN = "USDT"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"))
try:
    from bot_config import MARGIN_MODE, PRICE_DECIMALS
except ImportError:
    MARGIN_MODE = "isolated"
    PRICE_DECIMALS = {"BTC": 1, "ETH": 2, "SOL": 3, "AVAX": 3, "XRP": 4}

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_DIR, "config")
DATA_DIR = os.path.join(PROJECT_DIR, "data")

# Bitget Interval-Format
INTERVAL_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D"
}


# ========================
# DATACLASSES
# ========================

@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    filled_size: float = 0.0
    avg_price: float = 0.0
    error: Optional[str] = None


@dataclass
class Position:
    coin: str
    size: float          # positiv = long, negativ = short
    entry_price: float
    unrealized_pnl: float
    leverage: float
    liquidation_price: float


# ========================
# CLIENT
# ========================

class BitgetClient:
    """
    Bitget USDT-Perpetual Futures Client

    Features:
    - HMAC-SHA256 Authentifizierung
    - Marktdaten: Preis, Candles, Orderbook
    - Orders: Market, Stop-Loss, Take-Profit
    - Account: Balance, Positionen, Trade-History
    - DRY_RUN Mode: alle Order-Calls werden simuliert
    """

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.api_key = None
        self.secret_key = None
        self.passphrase = None
        self._load_credentials()

        mode = "DRY RUN" if dry_run else "LIVE"
        print(f"✅ BitgetClient initialisiert [{mode}]")
        if dry_run:
            print("   ⚠️  DRY RUN aktiv - keine echten Orders werden platziert!")

    # ─── Credentials ──────────────────────────────────────────────────────────

    def _load_credentials(self):
        """Lade API-Credentials aus config/.env.bitget"""
        env_file = os.path.join(CONFIG_DIR, ".env.bitget")
        if not os.path.exists(env_file):
            print(f"⚠️  Keine Credentials: {env_file}")
            print("   → Erstelle config/.env.bitget mit deinen API-Keys")
            return

        with open(env_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key == "BITGET_API_KEY":
                    self.api_key = value.strip()
                elif key == "BITGET_SECRET_KEY":
                    self.secret_key = value.strip()
                elif key == "BITGET_PASSPHRASE":
                    self.passphrase = value.strip()

        if self.api_key:
            print(f"✅ Credentials geladen: {self.api_key[:8]}...")

    @property
    def is_ready(self) -> bool:
        return all([self.api_key, self.secret_key, self.passphrase])

    # ─── Auth Helpers ─────────────────────────────────────────────────────────

    def _symbol(self, coin: str) -> str:
        return f"{coin.upper()}USDT"

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        prehash = timestamp + method.upper() + path + body
        return base64.b64encode(
            hmac.new(
                self.secret_key.encode("utf-8"),
                prehash.encode("utf-8"),
                hashlib.sha256
            ).digest()
        ).decode("utf-8")

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict:
        ts = str(int(time.time() * 1000))
        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "locale": "en-US",
        }
        if method.upper() == "POST":
            headers["Content-Type"] = "application/json"
        return headers

    # ─── HTTP Helpers ─────────────────────────────────────────────────────────

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """HTTP Request mit Exponential Backoff bei 429 (max 3 Versuche)"""
        delays = [5, 15, 30]
        for attempt, delay in enumerate(delays, 1):
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429:
                print(f"⚠️  Bitget 429 – Rate Limit. Warte {delay}s (Versuch {attempt}/3)...")
                time.sleep(delay)
                continue
            return resp
        # Letzter Versuch ohne Abfangen
        return requests.request(method, url, **kwargs)

    def _get(self, path: str, params: Dict = None, auth: bool = False) -> any:
        """GET Request – gibt data-Feld zurück"""
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())

        full_path = path + query
        headers = self._auth_headers("GET", full_path) if auth else {"locale": "en-US"}

        resp = self._request_with_retry("GET", BASE_URL + full_path, headers=headers, timeout=10)
        if not resp.ok:
            raise Exception(f"{resp.status_code} Client Error: {resp.reason} for url: {resp.url} | Body: {resp.text[:500]}")
        data = resp.json()

        if data.get("code") != "00000":
            raise Exception(f"Bitget Error [{data.get('code')}]: {data.get('msg')}")

        return data.get("data")

    def _post(self, path: str, body: Dict) -> any:
        """POST Request (immer authentifiziert) – gibt data-Feld zurück"""
        if not self.is_ready:
            raise Exception("API-Credentials nicht konfiguriert")

        body_str = json.dumps(body)
        headers = self._auth_headers("POST", path, body_str)

        resp = self._request_with_retry("POST", BASE_URL + path, data=body_str, headers=headers, timeout=10)
        if not resp.ok:
            raise Exception(f"{resp.status_code} Client Error: {resp.reason} for url: {resp.url} | Body: {resp.text[:500]}")
        data = resp.json()

        if data.get("code") != "00000":
            raise Exception(f"Bitget Error [{data.get('code')}]: {data.get('msg')}")

        return data.get("data")

    # ========================
    # PUBLIC MARKET DATA
    # ========================

    def get_price(self, coin: str) -> float:
        """Aktueller Mark-Preis"""
        data = self._get("/api/v2/mix/market/ticker", {
            "symbol": self._symbol(coin),
            "productType": PRODUCT_TYPE,
        })
        items = data if isinstance(data, list) else [data]
        if not items:
            return 0.0
        item = items[0]
        return float(item.get("markPrice") or item.get("lastPr") or 0)

    def get_candles(self, coin: str, interval: str = "15m", limit: int = 100) -> List[Dict]:
        """
        OHLCV Kerzen – sortiert älteste zuerst.
        Intervals: 1m, 5m, 15m, 1h, 4h, 1d
        """
        bg_interval = INTERVAL_MAP.get(interval, interval)
        data = self._get("/api/v2/mix/market/candles", {
            "symbol": self._symbol(coin),
            "productType": PRODUCT_TYPE,
            "granularity": bg_interval,
            "limit": str(limit),
        })

        candles = []
        for c in (data if isinstance(data, list) else []):
            # Bitget Format: [timestamp, open, high, low, close, volume, quoteVolume]
            candles.append({
                "time": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })

        candles.sort(key=lambda x: x["time"])
        return candles

    def get_orderbook(self, coin: str) -> Dict:
        """Orderbook mit Spread-Berechnung"""
        data = self._get("/api/v2/mix/market/orderbook", {
            "symbol": self._symbol(coin),
            "productType": PRODUCT_TYPE,
            "limit": "5",
        })

        bids = data.get("bids", []) if isinstance(data, dict) else []
        asks = data.get("asks", []) if isinstance(data, dict) else []

        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
        spread_pct = (best_ask - best_bid) / mid * 100 if mid > 0 else 0.0

        return {
            "bid": best_bid,
            "ask": best_ask,
            "mid": mid,
            "spread_pct": spread_pct,
        }

    # ========================
    # ACCOUNT (AUTH)
    # ========================

    def get_balance(self) -> float:
        """Verfügbares USDT-Guthaben im Futures-Konto"""
        if not self.is_ready:
            return 0.0
        try:
            data = self._get("/api/v2/mix/account/accounts", {
                "productType": PRODUCT_TYPE,
            }, auth=True)
            accounts = data if isinstance(data, list) else []
            for acc in accounts:
                if acc.get("marginCoin") == "USDT":
                    # equity = gesamtes Kapital inkl. offener Positionen (nicht nur available)
                    return float(acc.get("equity") or acc.get("usdtEquity") or acc.get("available", 0))
        except Exception as e:
            print(f"⚠️  Balance-Fehler: {e}")
        return 0.0

    def get_equity(self) -> float:
        """Gesamte Equity (Available + Margin + Unrealized PnL)"""
        if not self.is_ready:
            return 0.0
        try:
            data = self._get("/api/v2/mix/account/accounts", {
                "productType": PRODUCT_TYPE,
            }, auth=True)
            accounts = data if isinstance(data, list) else []
            for acc in accounts:
                if acc.get("marginCoin") == "USDT":
                    return float(acc.get("equity", acc.get("usdtEquity", acc.get("available", 0))))
        except Exception as e:
            print(f"⚠️  Equity-Fehler: {e}")
        return 0.0

    def get_positions(self) -> List[Position]:
        """Alle offenen Positionen"""
        if not self.is_ready:
            return []
        try:
            data = self._get("/api/v2/mix/position/all-position", {
                "productType": PRODUCT_TYPE,
                "marginCoin": MARGIN_COIN,
            }, auth=True)

            positions = []
            for pos in (data if isinstance(data, list) else []):
                size = float(pos.get("total", 0))
                if size == 0:
                    continue

                hold_side = pos.get("holdSide", "long")
                signed_size = size if hold_side == "long" else -size

                symbol = pos.get("symbol", "")
                coin = symbol.replace("USDT", "")

                positions.append(Position(
                    coin=coin,
                    size=signed_size,
                    entry_price=float(pos.get("openPriceAvg", 0)),
                    unrealized_pnl=float(pos.get("unrealizedPL", 0)),
                    leverage=float(pos.get("leverage", 1)),
                    liquidation_price=float(pos.get("liquidationPrice") or 0),
                ))
            return positions
        except Exception as e:
            print(f"⚠️  Positions-Fehler: {e}")
            return []

    def get_tpsl_orders(self, coin: str) -> List[Dict]:
        """Gibt aktive Plan-Orders (SL/TP) für ein Asset zurück"""
        if self.dry_run:
            return []
        try:
            data = self._get("/api/v2/mix/order/orders-plan-pending", {
                "productType": PRODUCT_TYPE,
                "symbol": self._symbol(coin),
                "planType": "profit_loss",
            }, auth=True)
            if isinstance(data, dict):
                return data.get("entrustedList") or []
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"⚠️  TPSL-Order Check Fehler: {e}")
            return []

    def get_recent_fills(self, coin: str = None, limit: int = 20) -> List[Dict]:
        """Letzte abgeschlossene Trades (Fill-History)"""
        if not self.is_ready:
            return []
        try:
            params = {"productType": PRODUCT_TYPE, "limit": str(limit)}
            if coin:
                params["symbol"] = self._symbol(coin)
            data = self._get("/api/v2/mix/order/fill-history", params, auth=True)
            return data.get("fillList", []) if isinstance(data, dict) else []
        except Exception as e:
            print(f"⚠️  Fill-History Fehler: {e}")
            return []

    # ========================
    # LEVERAGE
    # ========================

    def set_leverage(self, coin: str, leverage: int) -> bool:
        """Setze Leverage für Long und Short"""
        if self.dry_run:
            print(f"[DRY RUN] Leverage {leverage}x für {coin} gesetzt")
            return True
        try:
            for side in ["long", "short"]:
                self._post("/api/v2/mix/account/set-leverage", {
                    "symbol": self._symbol(coin),
                    "productType": PRODUCT_TYPE,
                    "marginCoin": MARGIN_COIN,
                    "leverage": str(leverage),
                    "holdSide": side,
                })
            print(f"✅ Leverage {leverage}x gesetzt für {coin}")
            return True
        except Exception as e:
            print(f"⚠️  Leverage-Fehler: {e}")
            return False

    # ========================
    # TRADING
    # ========================

    def place_market_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        reduce_only: bool = False,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """
        Platziere eine Market Order.

        is_buy=True  + reduce_only=False → Long öffnen
        is_buy=False + reduce_only=False → Short öffnen
        is_buy=False + reduce_only=True  → Long schließen
        is_buy=True  + reduce_only=True  → Short schließen

        Optionale SL/TP werden als Preset direkt mitgegeben.
        """
        side = "buy" if is_buy else "sell"
        trade_side = "close" if reduce_only else "open"

        if self.dry_run:
            current_price = self.get_price(coin)
            direction = "LONG" if is_buy else "SHORT"
            action = "CLOSE" if reduce_only else "OPEN"
            print(f"\n[DRY RUN] Market Order: {coin} {direction} {action}")
            print(f"   Size:  {size}")
            print(f"   Preis: ${current_price:,.4f}")
            if stop_loss:
                print(f"   SL:    ${stop_loss:,.4f}")
            if take_profit:
                print(f"   TP:    ${take_profit:,.4f}")
            return OrderResult(
                success=True,
                order_id=f"DRY-{int(time.time())}",
                filled_size=size,
                avg_price=current_price,
            )

        body = {
            "symbol": self._symbol(coin),
            "productType": PRODUCT_TYPE,
            "marginMode": MARGIN_MODE,
            "marginCoin": MARGIN_COIN,
            "size": str(size),
            "side": side,
            "tradeSide": trade_side,
            "orderType": "market",
            "force": "ioc",
        }
        p_dec = PRICE_DECIMALS.get(coin, 4)
        if stop_loss:
            body["presetStopLossPrice"] = str(round(stop_loss, p_dec))
            body["presetStopLossTriggerType"] = "mark_price"
        if take_profit:
            body["presetStopSurplusPrice"] = str(round(take_profit, p_dec))
            body["presetStopSurplusTriggerType"] = "mark_price"

        try:
            result = self._post("/api/v2/mix/order/place-order", body)
            order_id = result.get("orderId", "") if isinstance(result, dict) else ""
            time.sleep(0.5)  # kurz warten für Fill
            fill_price = self.get_price(coin)
            return OrderResult(
                success=True,
                order_id=order_id,
                filled_size=size,
                avg_price=fill_price,
            )
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def place_stop_loss(self, coin: str, trigger_price: float, size: float) -> OrderResult:
        """Setze einen Stop-Loss für eine offene Position"""
        if self.dry_run:
            print(f"[DRY RUN] Stop-Loss: {coin} @ ${trigger_price:,.4f}")
            return OrderResult(success=True, avg_price=trigger_price)

        positions = self.get_positions()
        pos = next((p for p in positions if p.coin == coin), None)
        if not pos:
            return OrderResult(success=False, error=f"Keine offene Position für {coin}")

        hold_side = "long" if pos.size > 0 else "short"
        p_dec = PRICE_DECIMALS.get(coin, 4)
        try:
            self._post("/api/v2/mix/order/place-tpsl-order", {
                "symbol": self._symbol(coin),
                "productType": PRODUCT_TYPE,
                "marginCoin": MARGIN_COIN,
                "planType": "loss_plan",
                "triggerPrice": str(round(trigger_price, p_dec)),
                "triggerType": "mark_price",
                "executePrice": "0",
                "holdSide": hold_side,
                "size": str(size),
            })
            return OrderResult(success=True, avg_price=trigger_price)
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def place_take_profit(self, coin: str, trigger_price: float, size: float) -> OrderResult:
        """Setze einen Take-Profit für eine offene Position"""
        if self.dry_run:
            print(f"[DRY RUN] Take-Profit: {coin} @ ${trigger_price:,.4f}")
            return OrderResult(success=True, avg_price=trigger_price)

        positions = self.get_positions()
        pos = next((p for p in positions if p.coin == coin), None)
        if not pos:
            return OrderResult(success=False, error=f"Keine offene Position für {coin}")

        hold_side = "long" if pos.size > 0 else "short"
        p_dec = PRICE_DECIMALS.get(coin, 4)
        try:
            self._post("/api/v2/mix/order/place-tpsl-order", {
                "symbol": self._symbol(coin),
                "productType": PRODUCT_TYPE,
                "marginCoin": MARGIN_COIN,
                "planType": "profit_plan",
                "triggerPrice": str(round(trigger_price, p_dec)),
                "triggerType": "mark_price",
                "executePrice": "0",
                "holdSide": hold_side,
                "size": str(size),
            })
            return OrderResult(success=True, avg_price=trigger_price)
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def cancel_tpsl_orders(self, coin: str) -> bool:
        """Storniere alle Plan-Orders (SL/TP) für ein Asset"""
        if self.dry_run:
            print(f"[DRY RUN] Cancel TP/SL für {coin}")
            return True
        try:
            # Erst alle offenen SL/TP Plan-Orders holen
            data = self._get("/api/v2/mix/order/orders-plan-pending", {
                "productType": PRODUCT_TYPE,
                "symbol": self._symbol(coin),
                "planType": "profit_loss",
            }, auth=True)

            if isinstance(data, dict):
                orders = data.get("entrustedList", [])
            elif isinstance(data, list):
                orders = data
            else:
                orders = []

            if not orders:
                return True  # Keine Orders vorhanden – OK

            order_id_list = [
                {"orderId": o["orderId"], "clientOid": o.get("clientOid", "")}
                for o in orders if o.get("orderId")
            ]

            self._post("/api/v2/mix/order/cancel-plan-order", {
                "symbol": self._symbol(coin),
                "productType": PRODUCT_TYPE,
                "marginCoin": MARGIN_COIN,
                "orderIdList": order_id_list,
            })
            print(f"   🧹 {len(order_id_list)} Plan-Orders storniert ({coin})")
            return True
        except Exception as e:
            print(f"⚠️  Cancel TP/SL Fehler: {e}")
            return False

    # ========================
    # UTILITY
    # ========================

    def calculate_position_size(
        self,
        risk_amount: float,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """
        Berechne Position Size (in Coins) basierend auf Risk.

        risk_amount: Max Verlust in USD
        entry_price: Geplanter Entry-Preis
        stop_loss:   Stop-Loss Preis

        Beispiel: risk=$1, entry=$1900, sl=$1870 → 0.033 ETH
        """
        risk_per_unit = abs(entry_price - stop_loss)
        if risk_per_unit == 0:
            return 0.0
        return risk_amount / risk_per_unit

    def format_status(self) -> str:
        """Formatierter Account-Status für Logs/Telegram"""
        if not self.is_ready:
            return "❌ API-Credentials nicht konfiguriert"

        balance = self.get_balance()
        positions = self.get_positions()
        mode = "[DRY RUN] " if self.dry_run else ""

        lines = [
            f"{mode}💰 Balance: ${balance:,.2f} USDT",
            f"📊 Positionen: {len(positions)}",
        ]
        for pos in positions:
            pnl_emoji = "🟢" if pos.unrealized_pnl >= 0 else "🔴"
            direction = "LONG" if pos.size > 0 else "SHORT"
            lines.append(
                f"   {pos.coin} {direction}: {abs(pos.size):.4f} "
                f"@ ${pos.entry_price:,.4f} "
                f"{pnl_emoji} ${pos.unrealized_pnl:+,.2f}"
            )
        return "\n".join(lines)


# ========================
# STANDALONE TEST
# ========================

if __name__ == "__main__":
    print("=" * 55)
    print("APEX - Bitget Client Test")
    print("=" * 55)

    client = BitgetClient(dry_run=True)

    print(f"\n🔑 Credentials bereit: {client.is_ready}")

    print("\n📊 Marktdaten (ETH, SOL, AVAX):")
    for coin in ["ETH", "SOL", "AVAX"]:
        try:
            price = client.get_price(coin)
            book = client.get_orderbook(coin)
            print(f"   {coin}: ${price:,.4f} | Spread: {book['spread_pct']:.4f}%")
        except Exception as e:
            print(f"   {coin}: Fehler – {e}")

    print("\n📈 ETH 15m Candles (letzte 3):")
    try:
        candles = client.get_candles("ETH", "15m", limit=3)
        for c in candles:
            dt = datetime.utcfromtimestamp(c["time"] / 1000).strftime("%H:%M")
            print(f"   {dt} | O:{c['open']:.2f} H:{c['high']:.2f} "
                  f"L:{c['low']:.2f} C:{c['close']:.2f}")
    except Exception as e:
        print(f"   Fehler: {e}")

    print("\n📐 Position Size Rechner (50 USDT Konto, 2% Risk, 5x Hebel):")
    examples = [
        ("ETH",  1900.0, 1865.0),
        ("SOL",  140.0,  137.0),
        ("AVAX", 22.0,   21.3),
    ]
    for coin, entry, sl in examples:
        size = client.calculate_position_size(
            risk_amount=50 * 0.02,  # $1
            entry_price=entry,
            stop_loss=sl,
        )
        margin = (size * entry) / 5  # 5x leverage
        print(f"   {coin}: size={size:.4f} | Margin: ${margin:.2f}")

    print("\n[DRY RUN] Simuliere Market Order ETH Long:")
    result = client.place_market_order("ETH", is_buy=True, size=0.033,
                                       stop_loss=1865.0, take_profit=1970.0)
    print(f"   Result: success={result.success}, price=${result.avg_price:,.2f}")

    print("\n✅ Test abgeschlossen.")
