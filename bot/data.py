"""Pengambil data harga emas.

Sumber utama: Binance PAXG/USDT (PAX Gold, 1 token = 1 troy oz emas) —
proxy harga spot XAU/USD yang datanya gratis, real-time, dan punya
riwayat panjang. data-api.binance.vision dicoba lebih dulu karena
api.binance.com memblokir IP US (runner GitHub Actions -> HTTP 451).
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from .strategy import TF_MS

HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]
SYMBOL = "PAXGUSDT"
UA = {"User-Agent": "XauBotSniper/1.0 (+github)"}


def _get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_klines(interval, start_ms=None, end_ms=None, limit=1000, symbol=SYMBOL):
    """Satu halaman kline Binance -> list dict {t,o,h,l,c,v} (bar terakhir bisa belum close)."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    qs = urllib.parse.urlencode(params)
    last_err = None
    for host in HOSTS:
        try:
            raw = _get_json(f"{host}/api/v3/klines?{qs}")
            return [
                {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                 "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
                for k in raw
            ]
        except Exception as e:  # noqa: BLE001 — coba host berikutnya
            last_err = e
    raise RuntimeError(f"Gagal mengambil kline {interval}: {last_err}")


def fetch_history(interval, days, symbol=SYMBOL):
    """Riwayat `days` hari penuh dengan paginasi. Hanya bar yang sudah close."""
    step = TF_MS[interval]
    now = int(time.time() * 1000)
    start = now - days * 86_400_000
    out = []
    cursor = start
    while cursor < now:
        page = fetch_klines(interval, start_ms=cursor, limit=1000, symbol=symbol)
        if not page:
            break
        out.extend(page)
        nxt = page[-1]["t"] + step
        if nxt <= cursor:
            break
        cursor = nxt
        if len(page) < 1000:
            break
        time.sleep(0.15)  # sopan ke API publik
    # buang duplikat & bar yang belum close
    seen, dedup = set(), []
    for c in out:
        if c["t"] in seen:
            continue
        seen.add(c["t"])
        if c["t"] + step <= now:
            dedup.append(c)
    dedup.sort(key=lambda c: c["t"])
    return dedup


def fetch_recent_closed(interval, limit=500, symbol=SYMBOL):
    """`limit` bar terakhir yang sudah close."""
    page = fetch_klines(interval, limit=min(limit + 1, 1000), symbol=symbol)
    now = int(time.time() * 1000)
    step = TF_MS[interval]
    closed = [c for c in page if c["t"] + step <= now]
    return closed[-limit:]
