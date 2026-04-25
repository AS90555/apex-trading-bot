#!/usr/bin/env python3
"""
Candle-Downloader — lädt historische OHLCV-Daten von Bitget und speichert sie lokal.

Persistiert in data/historical/{ASSET}_{INTERVAL}.csv (idempotent, lückenlos).
Unterstützt: 1m, 5m, 15m, 4h (alle für APEX-Backtest relevanten Intervalle)

Verwendung:
  python3 scripts/backtest/candle_downloader.py
  python3 scripts/backtest/candle_downloader.py --assets ETH,SOL --intervals 15m,5m --from 2025-10-01 --to 2026-04-20
  python3 scripts/backtest/candle_downloader.py --assets BTC --intervals 4h --from 2025-04-20 --to 2026-04-20
"""
import csv
import os
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(SCRIPT_DIR)))

from scripts.bitget_client import BitgetClient

DATA_DIR     = os.path.join(os.path.dirname(os.path.dirname(SCRIPT_DIR)), "data")
HIST_DIR     = os.path.join(DATA_DIR, "historical")
CHUNK_LIMIT  = 200    # history-candles max 200 pro Request
RATE_SLEEP   = 0.25   # Sekunden zwischen Calls (konservativ, ~4 req/s)

# Intervall → erwartete Dauer in Minuten
INTERVAL_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}

HEADER = ["time_ms", "open", "high", "low", "close", "volume", "quote_vol"]


def csv_path(asset: str, interval: str) -> str:
    os.makedirs(HIST_DIR, exist_ok=True)
    return os.path.join(HIST_DIR, f"{asset}_{interval}.csv")


def load_existing_timestamps(path: str) -> set:
    if not os.path.exists(path):
        return set()
    ts = set()
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts.add(int(row["time_ms"]))
    return ts


def load_latest_timestamp(path: str) -> int | None:
    """Gibt den höchsten bereits gespeicherten Timestamp zurück."""
    if not os.path.exists(path):
        return None
    latest = None
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["time_ms"])
            if latest is None or ts > latest:
                latest = ts
    return latest


def expected_candle_count(start_ms: int, end_ms: int, interval: str) -> int:
    interval_ms = INTERVAL_MINUTES[interval] * 60 * 1000
    return max(1, (end_ms - start_ms) // interval_ms)


def download_chunk(client: BitgetClient, asset: str, interval: str,
                   start_ms: int, end_ms: int) -> list[dict]:
    """
    Lädt einen Chunk historischer Candles via history-candles Endpunkt.
    Fallback auf normalen candles-Endpunkt wenn history-candles leer.
    """
    import requests as _req
    symbol = f"{asset}USDT"
    params = {
        "symbol":      symbol,
        "granularity": interval,
        "startTime":   str(start_ms),
        "endTime":     str(end_ms),
        "limit":       str(CHUNK_LIMIT),
        "productType": "USDT-FUTURES",
    }
    try:
        r = _req.get("https://api.bitget.com/api/v2/mix/market/history-candles",
                     params=params, timeout=15)
        data = r.json().get("data", [])
        if data:
            candles = []
            for row in data:
                candles.append({
                    "time":        int(row[0]),
                    "open":        float(row[1]),
                    "high":        float(row[2]),
                    "low":         float(row[3]),
                    "close":       float(row[4]),
                    "volume":      float(row[5]),
                    "quoteVolume": float(row[6]) if len(row) > 6 else 0.0,
                })
            candles.sort(key=lambda x: x["time"])
            return candles
    except Exception as e:
        print(f"   ⚠️  history-candles Fehler {asset} {interval}: {e}")

    # Fallback: normaler Endpunkt (nur ~30 Tage)
    try:
        return client.get_candles(asset, interval=interval, limit=CHUNK_LIMIT,
                                  start_time=start_ms, end_time=end_ms)
    except Exception as e:
        print(f"   ⚠️  API-Fehler bei {asset} {interval} [{start_ms}]: {e}")
        return []


def download_asset(client: BitgetClient, asset: str, interval: str,
                   start_ms: int, end_ms: int, verbose: bool = True) -> int:
    """
    Lädt alle Candles für ein Asset+Interval im Zeitraum start_ms..end_ms.
    Anhänge-Modus: lädt nur neue Candles (nach letztem vorhandenen Timestamp).
    Returns: Anzahl neu gespeicherter Candles.
    """
    path = csv_path(asset, interval)
    interval_ms = INTERVAL_MINUTES[interval] * 60 * 1000

    # Bestimme effektiven Startpunkt (überspringe bereits vorhandene)
    latest = load_latest_timestamp(path)
    effective_start = start_ms
    if latest is not None and latest >= start_ms:
        effective_start = latest + interval_ms  # Nächste Candle nach letztem Download

    if effective_start >= end_ms:
        if verbose:
            print(f"   {asset} {interval}: bereits aktuell ({path})")
        return 0

    expected = expected_candle_count(effective_start, end_ms, interval)
    if verbose:
        start_dt = datetime.fromtimestamp(effective_start / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"   {asset} {interval}: {start_dt} → {end_dt} (~{expected} Candles erwartet)")

    file_exists = os.path.exists(path)
    new_rows = 0
    current_start = effective_start

    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(HEADER)

        while current_start < end_ms:
            chunk_end = min(current_start + CHUNK_LIMIT * interval_ms, end_ms)
            candles = download_chunk(client, asset, interval, current_start, chunk_end)

            if not candles:
                # Lücke oder keine Daten — weitermachen mit nächstem Chunk
                current_start = chunk_end + interval_ms
                time.sleep(RATE_SLEEP)
                continue

            for c in candles:
                if c["time"] < effective_start:
                    continue  # Überspringe Candles vor unserem Startpunkt
                writer.writerow([
                    c["time"], c["open"], c["high"], c["low"],
                    c["close"], c["volume"], c.get("quoteVolume", "")
                ])
                new_rows += 1

            # Nächster Chunk beginnt nach letzter Candle dieses Chunks
            if candles:
                current_start = candles[-1]["time"] + interval_ms
            else:
                current_start = chunk_end + interval_ms

            time.sleep(RATE_SLEEP)

    if verbose and new_rows > 0:
        coverage = round(new_rows / expected * 100, 1) if expected > 0 else 0
        print(f"   → {new_rows} neue Candles gespeichert ({coverage}% Coverage)")
    elif verbose and new_rows == 0:
        print(f"   → Keine neuen Candles (Lücke oder API leer)")

    return new_rows


def main():
    parser = argparse.ArgumentParser(description="APEX Candle-Downloader")
    parser.add_argument("--assets", default="ETH,SOL,AVAX,XRP,BTC",
                        help="Komma-getrennte Asset-Liste (default: ETH,SOL,AVAX,XRP,BTC)")
    parser.add_argument("--intervals", default="15m,5m",
                        help="Komma-getrennte Intervalle (default: 15m,5m)")
    parser.add_argument("--from", dest="start", default=None,
                        help="Startdatum YYYY-MM-DD (default: 12 Monate zurück)")
    parser.add_argument("--to", dest="end", default=None,
                        help="Enddatum YYYY-MM-DD (default: heute)")
    parser.add_argument("--quiet", action="store_true", help="Weniger Output")
    args = parser.parse_args()

    # Zeitraum bestimmen
    now = datetime.now(tz=timezone.utc)
    end_dt   = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else now
    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.start else (now - timedelta(days=365))

    end_ms   = int(end_dt.timestamp() * 1000)
    start_ms = int(start_dt.timestamp() * 1000)

    assets    = [a.strip().upper() for a in args.assets.split(",")]
    intervals = [i.strip().lower() for i in args.intervals.split(",")]
    verbose   = not args.quiet

    # Validierung
    for iv in intervals:
        if iv not in INTERVAL_MINUTES:
            print(f"⚠️  Unbekanntes Intervall: {iv}. Gültig: {', '.join(INTERVAL_MINUTES)}")
            sys.exit(1)

    client = BitgetClient(dry_run=True)

    start_label = start_dt.strftime("%Y-%m-%d")
    end_label   = end_dt.strftime("%Y-%m-%d")
    print(f"📥 Candle-Download: {', '.join(assets)} | {', '.join(intervals)} | {start_label} → {end_label}")
    print(f"   Zielordner: {HIST_DIR}")
    print()

    total_new = 0
    t0 = time.time()

    for asset in assets:
        for interval in intervals:
            new = download_asset(client, asset, interval, start_ms, end_ms, verbose=verbose)
            total_new += new

    elapsed = round(time.time() - t0, 1)
    print(f"\n✅ Fertig: {total_new} neue Candles in {elapsed}s")
    print(f"   Dateien in: {HIST_DIR}")


if __name__ == "__main__":
    main()
