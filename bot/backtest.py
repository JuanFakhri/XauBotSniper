"""Backtest strategi sniper multi-timeframe di data riwayat PAXG/USDT (proxy XAUUSD).

Jalankan:  python -m bot.backtest            (default 365 hari, entry TF 5m)
Env:       BACKTEST_DAYS=730                 (ubah panjang riwayat)

Anti-lookahead: semua keputusan hanya memakai bar yang SUDAH close pada
waktu itu. TF menengah/besar dibangun dengan resample dari data 5m,
sehingga bar 15m/1h/4h/1d baru "terlihat" setelah bucket-nya selesai.

Asumsi eksekusi (konservatif):
  - Entry di harga close candle trigger.
  - Bila SL dan TP tersentuh di bar yang sama -> dihitung SL (loss).
  - Biaya 0.01% per sisi (~spread XAUUSD ritel).
  - Satu posisi pada satu waktu (meniru trader manual).
  - Timeout: posisi ditutup paksa di close setelah max_hold_bars.
"""

from __future__ import annotations

import json
import os
import time

from .data import fetch_history, SYMBOL
from .strategy import (DEFAULT_PARAMS, TF_MS, atr, build_zones, detect_signal,
                       htf_bias, resample)

WARMUP_DAYS = 70          # cukup untuk EMA50 harian + slope
BIAS_WINDOW = 200         # bar per-TF yang dipakai menghitung arah
ENTRY_WINDOW = 80         # bar entry-TF yang dipakai detektor sinyal
OUT_PATH = os.path.join("docs", "data", "backtest.json")


def run(days=365, params=None):
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    entry_tf = p["entry_tf"]
    step = TF_MS[entry_tf]

    print(f"Mengambil riwayat {entry_tf} {days + WARMUP_DAYS} hari ({SYMBOL})...")
    m5 = fetch_history(entry_tf, days + WARMUP_DAYS)
    if len(m5) < 5000:
        raise RuntimeError(f"Data terlalu sedikit: {len(m5)} bar")
    print(f"  {len(m5)} bar {entry_tf} ({time.strftime('%Y-%m-%d', time.gmtime(m5[0]['t']/1000))}"
          f" .. {time.strftime('%Y-%m-%d', time.gmtime(m5[-1]['t']/1000))})")

    # Bangun TF lain dari 5m (anti-lookahead: pakai pointer bar yang sudah close)
    hts = {tf: resample(m5, entry_tf, tf) for tf in ("15m", "30m", "1h", "4h", "1d")}
    ptr = {tf: 0 for tf in hts}

    start_ms = m5[0]["t"] + WARMUP_DAYS * 86_400_000
    zones = None
    atr15 = None
    bias = {"dir": 0, "mode": "handgun", "strength": 0,
            "detail": {"d1": 0, "h4": 0, "h1": 0}}

    equity = 1000.0
    peak = equity
    max_dd = 0.0
    curve = []
    trades = []
    open_tr = None
    signals_seen = 0

    for i, bar in enumerate(m5):
        cur_end = bar["t"] + step

        # majukan pointer bar HTF yang sudah close; recompute saat bar baru
        new15 = False
        for tf, series in hts.items():
            dst = TF_MS[tf]
            k = ptr[tf]
            moved = False
            while k < len(series) and series[k]["t"] + dst <= cur_end:
                k += 1
                moved = True
            if moved:
                ptr[tf] = k
                if tf == "15m":
                    new15 = True
        if new15 and ptr["15m"] >= 60 and ptr["30m"] >= 30:
            m15v = hts["15m"][max(0, ptr["15m"] - p["zone_lookback"]):ptr["15m"]]
            m30v = hts["30m"][max(0, ptr["30m"] - p["zone_lookback"]):ptr["30m"]]
            zones = build_zones(m15v, m30v, p)
            atr15 = atr(m15v, p["atr_period"])
            bias = htf_bias(
                hts["1d"][max(0, ptr["1d"] - BIAS_WINDOW):ptr["1d"]],
                hts["4h"][max(0, ptr["4h"] - BIAS_WINDOW):ptr["4h"]],
                hts["1h"][max(0, ptr["1h"] - BIAS_WINDOW):ptr["1h"]],
                p,
            )

        # kelola posisi terbuka
        if open_tr is not None:
            t = open_tr
            exited = None
            if t["side"] == "sell":
                if bar["h"] >= t["sl"]:
                    exited = (t["sl"], "sl")
                elif bar["l"] <= t["tp"]:
                    exited = (t["tp"], "tp")
            else:
                if bar["l"] <= t["sl"]:
                    exited = (t["sl"], "sl")
                elif bar["h"] >= t["tp"]:
                    exited = (t["tp"], "tp")
            if exited is None and i - t["i"] >= p["max_hold_bars"]:
                exited = (bar["c"], "timeout")
            if exited is not None:
                px, why = exited
                gross = (t["entry"] - px) if t["side"] == "sell" else (px - t["entry"])
                cost = t["entry"] * p["cost_pct_side"] * 2
                r_net = (gross - cost) / t["risk"]
                equity *= 1 + p["risk_pct"] / 100.0 * r_net
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak)
                t.update(exit=px, exit_time=cur_end, result=why,
                         r_net=round(r_net, 3))
                trades.append(t)
                curve.append((cur_end, round(equity, 2)))
                open_tr = None
            continue  # fokus satu posisi; tidak cari sinyal saat floating

        # cari sinyal baru
        if bar["t"] < start_ms or zones is None or atr15 is None:
            continue
        window = m5[max(0, i - ENTRY_WINDOW + 1):i + 1]
        sig = detect_signal(window, zones, bias, atr15, p)
        if sig is None:
            continue
        signals_seen += 1
        open_tr = {
            "i": i, "time": bar["t"] + step, "side": sig["side"],
            "mode": sig["mode"], "entry": sig["entry"], "sl": sig["sl"],
            "tp": sig["tp"], "risk": sig["risk"], "rr": sig["rr"],
        }

    # ------------------------------------------------------------- statistik
    def stats_for(sel):
        n = len(sel)
        wins = [t for t in sel if t["r_net"] > 0]
        losses = [t for t in sel if t["r_net"] <= 0]
        gp = sum(t["r_net"] for t in wins)
        gl = -sum(t["r_net"] for t in losses)
        return {
            "trades": n,
            "win_rate": round(100.0 * len(wins) / n, 1) if n else 0.0,
            "profit_factor": round(gp / gl, 2) if gl > 0 else None,
            "total_r": round(sum(t["r_net"] for t in sel), 1),
            "avg_r": round(sum(t["r_net"] for t in sel) / n, 3) if n else 0.0,
        }

    monthly = {}
    for t in trades:
        key = time.strftime("%Y-%m", time.gmtime(t["exit_time"] / 1000))
        m = monthly.setdefault(key, {"trades": 0, "wins": 0, "r": 0.0})
        m["trades"] += 1
        m["wins"] += t["r_net"] > 0
        m["r"] = round(m["r"] + t["r_net"], 2)

    ds = max(1, len(curve) // 500)
    result = {
        "generated_at": int(time.time() * 1000),
        "symbol": SYMBOL,
        "proxy_note": "PAXG/USDT dipakai sebagai proxy XAU/USD (1 PAXG = 1 oz emas)",
        "entry_tf": entry_tf,
        "days": days,
        "period": {"from": m5[0]["t"] + WARMUP_DAYS * 86_400_000, "to": m5[-1]["t"]},
        "assumptions": {
            "cost_pct_side": p["cost_pct_side"],
            "risk_pct_per_trade": p["risk_pct"],
            "same_bar_sl_tp": "dihitung SL (konservatif)",
            "start_equity": 1000.0,
        },
        "params": {k: v for k, v in p.items()},
        "stats": {
            **stats_for(trades),
            "end_equity": round(equity, 2),
            "return_pct": round((equity / 1000.0 - 1) * 100, 1),
            "max_drawdown_pct": round(max_dd * 100, 1),
            "signals_seen": signals_seen,
        },
        "by_mode": {m: stats_for([t for t in trades if t["mode"] == m])
                    for m in ("sniper", "handgun")},
        "by_side": {s: stats_for([t for t in trades if t["side"] == s])
                    for s in ("buy", "sell")},
        "by_exit": {w: sum(1 for t in trades if t["result"] == w)
                    for w in ("tp", "sl", "timeout")},
        "monthly": [{"month": k, **v} for k, v in sorted(monthly.items())],
        "equity_curve": [{"t": t, "eq": e} for t, e in curve[::ds]],
        "trades": [
            {k: t[k] for k in ("time", "exit_time", "side", "mode", "entry",
                                "sl", "tp", "exit", "result", "rr", "r_net")}
            for t in trades[-300:]
        ],
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, separators=(",", ":"))

    s = result["stats"]
    print("\n=== HASIL BACKTEST ===")
    print(f"Periode      : {days} hari, entry TF {entry_tf}")
    print(f"Jumlah trade : {s['trades']} (win rate {s['win_rate']}%)")
    print(f"Profit factor: {s['profit_factor']}")
    print(f"Total R      : {s['total_r']}R  (avg {s['avg_r']}R/trade)")
    print(f"Ekuitas      : 1000 -> {s['end_equity']} ({s['return_pct']}%), "
          f"maxDD {s['max_drawdown_pct']}%")
    print(f"Exit         : {result['by_exit']}")
    print(f"Mode         : sniper={result['by_mode']['sniper']['trades']} trade, "
          f"handgun={result['by_mode']['handgun']['trades']} trade")
    print(f"JSON         : {OUT_PATH}")
    return result


if __name__ == "__main__":
    run(days=int(os.environ.get("BACKTEST_DAYS", "365")))
