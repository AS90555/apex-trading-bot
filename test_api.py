#!/usr/bin/env python3
import sys, requests, time, hmac, hashlib, base64
sys.path.insert(0, '/root/apex-trading-bot')
from scripts.bitget_client import BitgetClient

c = BitgetClient(dry_run=True)
print(f"API Key: {c.api_key[:12]}...")
print(f"Passphrase gesetzt: {bool(c.passphrase)}")

ts = str(int(time.time() * 1000))
path = '/api/v2/mix/account/accounts?productType=USDT-FUTURES'
pre = ts + 'GET' + path
sig = base64.b64encode(
    hmac.new(c.secret_key.encode(), pre.encode(), hashlib.sha256).digest()
).decode()

headers = {
    'ACCESS-KEY': c.api_key,
    'ACCESS-SIGN': sig,
    'ACCESS-TIMESTAMP': ts,
    'ACCESS-PASSPHRASE': c.passphrase,
    'locale': 'en-US'
}

r = requests.get('https://api.bitget.com' + path, headers=headers)
print(f"Status: {r.status_code}")
print(f"Response: {r.text}")
