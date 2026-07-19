"""Riset varian strategi dengan validasi in-sample / out-of-sample.

Menguji kombinasi 4 "saklar" perbaikan di data 730 hari:
  BE    : geser SL ke breakeven setelah profit mengambang +0.8R
          (meniru disiplin cut-loss manual Franz)
  SESI  : entry hanya sesi London+New York (07-20 UTC) — jam emas bergerak
  SELL  : hanya ambil sinyal sell (fade resistance; temuan backtest awal)
  TPFIX : TP tetap 1.5R alih-alih zona S/R terdekat

Setiap varian dinilai di paruh PERTAMA data (in-sample) dan paruh KEDUA
(out-of-sample). Varian yang hanya bagus di satu paruh = overfit, dibuang.
Hasil -> docs/data/research.json + tabel di log.

Jalankan: python -m bot.research   (env RESEARCH_DAYS, default 730)
"""

from __future__ import annotations

import itertools
import json
import os
import time

from .backtest import WARMUP_DAYS, simulate, stats_for
from .data import fetch_history, SYMBOL
from .strategy import DEFAULT_PARAMS

OUT_PATH = os.path.join("docs", "data", "research.json")

AXES = {
    "BE":    {"be_at_r": 0.8},
    "SESI":  {"session_utc": (7, 20)},
    "SELL":  {"sides": "sell"},
    "TPFIX": {"tp_mode": "fixed"},
}


def run(days=730):
    print(f"Mengambil riwayat 5m {days + WARMUP_DAYS} hari ({SYMBOL})...")
    m5 = fetch_history("5m", days + WARMUP_DAYS)
    print(f"  {len(m5)} bar")

    start_ms = m5[0]["t"] + WARMUP_DAYS * 86_400_000
    mid_ms = (start_ms + m5[-1]["t"]) // 2

    rows = []
    names = list(AXES)
    for flags in itertools.product([0, 1], repeat=len(names)):
        label = "+".join(n for n, f in zip(names, flags) if f) or "BASE"
        p = dict(DEFAULT_PARAMS)
        for n, f in zip(names, flags):
            if f:
                p.update(AXES[n])
        t0 = time.time()
        trades = simulate(m5, p)
        is_tr = [t for t in trades if t["exit_time"] < mid_ms]
        oos_tr = [t for t in trades if t["exit_time"] >= mid_ms]
        row = {
            "variant": label,
            "flags": {n: bool(f) for n, f in zip(names, flags)},
            "all": stats_for(trades),
            "is": stats_for(is_tr),
            "oos": stats_for(oos_tr),
        }
        rows.append(row)
        print(f"{label:22s} all: n={row['all']['trades']:3d} "
              f"pf={row['all']['profit_factor']} r={row['all']['total_r']:6.1f} | "
              f"IS: n={row['is']['trades']:3d} pf={row['is']['profit_factor']} | "
              f"OOS: n={row['oos']['trades']:3d} pf={row['oos']['profit_factor']} "
              f"({time.time()-t0:.0f}s)")

    out = {
        "generated_at": int(time.time() * 1000),
        "symbol": SYMBOL,
        "days": days,
        "split_mid_ms": mid_ms,
        "axes": {k: str(v) for k, v in AXES.items()},
        "results": rows,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"\nJSON: {OUT_PATH}")
    return out


if __name__ == "__main__":
    run(days=int(os.environ.get("RESEARCH_DAYS", "730")))
