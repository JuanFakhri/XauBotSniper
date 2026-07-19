"""Snapshot sinyal terkini -> docs/data/live.json (dipakai GitHub Actions).

Dashboard GitHub Pages menghitung sinyal secara live di browser; file ini
adalah cadangan (fallback) bila API market tidak bisa diakses dari browser
pengguna, sekaligus arsip status bot.
"""

from __future__ import annotations

import json
import os
import time

from .data import fetch_recent_closed, SYMBOL
from .strategy import DEFAULT_PARAMS, atr, build_zones, detect_signal, htf_bias

OUT_PATH = os.path.join("docs", "data", "live.json")


def run():
    p = DEFAULT_PARAMS
    m1 = fetch_recent_closed("1m", 300)
    m5 = fetch_recent_closed("5m", 300)
    m15 = fetch_recent_closed("15m", p["zone_lookback"])
    m30 = fetch_recent_closed("30m", p["zone_lookback"])
    h1 = fetch_recent_closed("1h", 200)
    h4 = fetch_recent_closed("4h", 200)
    d1 = fetch_recent_closed("1d", 200)

    bias = htf_bias(d1, h4, h1, p)
    zones = build_zones(m15, m30, p)
    atr15 = atr(m15, p["atr_period"])
    price = m1[-1]["c"]

    sig5 = detect_signal(m5, zones, bias, atr15, p)
    sig1 = detect_signal(m1, zones, bias, atr15, p)

    near = 6 * (atr15 or 1)
    out = {
        "generated_at": int(time.time() * 1000),
        "symbol": SYMBOL,
        "price": price,
        "bias": bias,
        "atr15": atr15,
        "zones": {
            "res": [z for z in zones["res"] if z["lo"] - price < near][:8],
            "sup": [z for z in zones["sup"] if price - z["hi"] < near][:8],
        },
        "signal_5m": sig5,
        "signal_1m": sig1,
        "status": (sig5 or sig1 or {}).get("side", "wait").upper(),
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Status: {out['status']}  harga {price}  bias {bias['detail']}")
    return out


if __name__ == "__main__":
    run()
