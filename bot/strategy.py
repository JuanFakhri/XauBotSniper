"""XauBotSniper — engine strategi scalping multi-timeframe "naked chart".

Implementasi aturan dari strategi (Franz / TCI "Close The Door"):

  Langkah 1 — Arah market (TF besar: 1H, 4H, Daily)
      Tren ditentukan dari struktur harga vs EMA50 + kemiringan EMA.
      Daily adalah bos; minimal satu TF konfirmasi (4H/1H) harus searah.
      Jangan melawan arus: bias SELL -> hanya cari sell, bias BUY -> hanya buy.

  Langkah 2 — Area potensial (TF menengah: 15m & 30m)
      Zona support/resistance dibentuk dari cluster pivot (swing high/low).
      Entry hanya boleh terjadi di dekat zona (harga "bereaksi" di area itu).

  Langkah 3 — Eksekusi entry (TF kecil: 1m/5m)
      Pola "kocokan": box konsolidasi rapat (range < X * ATR).
      Dorongan terakhir: false break keluar box (ambil liquidity) lalu
      close balik ke dalam box dengan candle jarum (wick panjang).
      Aturan jarum: kalau ada jarum menonjol sebelumnya yang BELUM disapu,
      jangan buru-buru entry — tunggu level itu ditembus dulu.

  Mode:
      SNIPER  = market trending (TF besar selaras) -> entry searah tren.
      HANDGUN = market ranging (TF besar netral)  -> fade di tepi range 15m.

  Risk management:
      SL  = di atas/bawah titik false break + buffer ATR (risiko terukur).
      TP  = zona S/R terdekat berikutnya (disiplin ambil profit, tidak serakah);
            minimal RR 1.0, fallback 1.5R bila tidak ada zona.

Semua fungsi pure-Python tanpa dependensi eksternal, dipakai oleh
backtest.py dan live_scan.py. Port JavaScript (docs/app.js) memakai
aturan & parameter yang sama persis.
"""

from __future__ import annotations

DEFAULT_PARAMS = {
    "entry_tf": "5m",
    "atr_period": 14,
    "box_lookback": 10,       # jumlah candle pembentuk box kocokan
    "box_max_atr": 1.6,       # tinggi box maksimal (x ATR entry-TF)
    "poke_min_atr": 0.05,     # minimal tusukan keluar box (x ATR)
    "wick_frac": 0.45,        # jarum: wick >= 45% dari range candle
    "sweep_lookback": 36,     # cari jarum menonjol sebelumnya (candle)
    "wick_prominence_atr": 0.5,
    "ema_period": 50,
    "ema_slope_bars": 5,
    "zone_k": 3,              # fractal pivot: k kiri & kanan
    "zone_lookback": 240,     # bar 15m/30m untuk membangun zona
    "zone_tol_pct": 0.0012,   # lebar cluster zona (0.12%)
    "zone_near_atr": 0.9,     # jarak maksimal trigger ke zona (x ATR15)
    "range_min_atr": 2.5,     # handgun: lebar range minimal (x ATR15)
    "sl_buf_atr": 0.25,
    "min_risk_atr": 0.3,      # skip bila SL terlalu rapat (spread makan profit)
    "max_risk_atr": 2.0,      # skip bila jarak SL > 2 ATR (risiko tak terukur)
    "min_rr": 1.0,
    "max_rr": 4.0,
    "fallback_rr": 1.5,
    "max_hold_bars": 96,      # keluar paksa setelah 8 jam (bar 5m)
    "risk_pct": 1.0,          # % ekuitas dirisikokan per trade (backtest)
    "cost_pct_side": 0.0001,  # biaya 0.01% per sisi (~spread XAUUSD)
}


# ---------------------------------------------------------------- indikator

def ema_series(values, period):
    """EMA sederhana; mengembalikan list sepanjang input (awal = SMA seed)."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def atr(candles, period=14):
    """ATR (RMA) dari list candle dict {o,h,l,c}. None bila data kurang."""
    n = len(candles)
    if n < period + 1:
        return None
    trs = []
    for i in range(n - period, n):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    return sum(trs) / period


# ------------------------------------------------------- langkah 1: arah TF besar

def tf_direction(candles, params=DEFAULT_PARAMS):
    """+1 naik, -1 turun, 0 netral — dari posisi close vs EMA50 + slope EMA."""
    period = params["ema_period"]
    closes = [c["c"] for c in candles]
    e = ema_series(closes, period)
    sb = params["ema_slope_bars"]
    if not e or len(e) < period + sb or e[-1] is None or e[-1 - sb] is None:
        return 0
    close, ema_now, ema_then = closes[-1], e[-1], e[-1 - sb]
    if close > ema_now and ema_now > ema_then:
        return 1
    if close < ema_now and ema_now < ema_then:
        return -1
    return 0


def htf_bias(d1, h4, h1, params=DEFAULT_PARAMS):
    """Gabungkan arah Daily/4H/1H. Daily menentukan; 4H/1H konfirmasi."""
    dd, d4, dh = (tf_direction(x, params) for x in (d1, h4, h1))
    detail = {"d1": dd, "h4": d4, "h1": dh}
    if dd != 0 and (d4 == dd or dh == dd):
        strength = 1 + (d4 == dd) + (dh == dd)
        return {"dir": dd, "mode": "sniper", "strength": strength, "detail": detail}
    if dd == 0 and d4 != 0 and d4 == dh:
        return {"dir": d4, "mode": "sniper", "strength": 2, "detail": detail}
    return {"dir": 0, "mode": "handgun", "strength": 0, "detail": detail}


# ------------------------------------------------- langkah 2: zona S/R 15m & 30m

def pivot_points(candles, k):
    """Fractal pivot: high/low yang lebih ekstrem dari k candle kiri & kanan."""
    highs, lows = [], []
    for i in range(k, len(candles) - k):
        h, l = candles[i]["h"], candles[i]["l"]
        if all(candles[j]["h"] <= h for j in range(i - k, i + k + 1) if j != i):
            highs.append(h)
        if all(candles[j]["l"] >= l for j in range(i - k, i + k + 1) if j != i):
            lows.append(l)
    return highs, lows


def cluster_zones(prices, tol_pct):
    """Kelompokkan harga pivot yang berdekatan jadi zona {lo, hi, mid, touches}."""
    if not prices:
        return []
    prices = sorted(prices)
    zones, cur = [], [prices[0]]
    for p in prices[1:]:
        if p - cur[-1] <= cur[0] * tol_pct * 2:
            cur.append(p)
        else:
            zones.append(cur)
            cur = [p]
    zones.append(cur)
    out = []
    for grp in zones:
        lo, hi = min(grp), max(grp)
        out.append({"lo": lo, "hi": hi, "mid": (lo + hi) / 2, "touches": len(grp)})
    return out


def build_zones(m15, m30, params=DEFAULT_PARAMS):
    """Zona S/R gabungan dari pivot 15m + 30m (lookback dibatasi)."""
    lb, k = params["zone_lookback"], params["zone_k"]
    highs, lows = [], []
    for series in (m15[-lb:], m30[-lb:]):
        h, l = pivot_points(series, k)
        highs.extend(h)
        lows.extend(l)
    return {
        "res": cluster_zones(highs, params["zone_tol_pct"]),
        "sup": cluster_zones(lows, params["zone_tol_pct"]),
    }


def nearest_zone(zones, price, side, max_dist=None):
    """Zona terdekat di atas (side='above') / bawah (side='below') harga."""
    best = None
    for z in zones:
        d = (z["lo"] - price) if side == "above" else (price - z["hi"])
        if d < 0:
            # harga di dalam zona -> jarak 0
            if z["lo"] <= price <= z["hi"]:
                d = 0.0
            else:
                continue
        if max_dist is not None and d > max_dist:
            continue
        if best is None or d < best[0]:
            best = (d, z)
    return best  # (jarak, zona) atau None


# --------------------------------------------------- langkah 3: trigger 1m/5m

def _prominent_wick_level(candles, side, atr_val, params, ref):
    """Level jarum menonjol di dekat `ref` (tepi box) yang belum disapu.

    Aturan 'jangan buru-buru': hanya jarum yang menonjol keluar box dan masih
    dekat (<= 1.5 ATR dari tepi box) yang relevan sebagai liquidity — jarum
    lama yang sudah jauh tidak memblokir entry.
    """
    lb = params["sweep_lookback"]
    thr = params["wick_prominence_atr"] * atr_val
    best = None
    for c in candles[-lb:]:
        rng = c["h"] - c["l"]
        if rng <= 0:
            continue
        if side == "sell":
            wick = c["h"] - max(c["o"], c["c"])
            if wick >= thr and ref < c["h"] <= ref + 1.5 * atr_val:
                best = max(best, c["h"]) if best is not None else c["h"]
        else:
            wick = min(c["o"], c["c"]) - c["l"]
            if wick >= thr and ref - 1.5 * atr_val <= c["l"] < ref:
                best = min(best, c["l"]) if best is not None else c["l"]
    return best


def detect_signal(entry_candles, zones, bias, atr15, params=DEFAULT_PARAMS,
                  diag=None):
    """Evaluasi candle entry-TF terakhir yang SUDAH close.

    Mengembalikan dict sinyal (side, mode, entry, sl, tp, rr, checklist)
    atau None bila tidak ada setup. Bila `diag` (dict) diberikan, counter
    per-kondisi ditambahkan ke dalamnya (untuk tuning/diagnostik backtest).
    """
    def bump(key):
        if diag is not None:
            diag[key] = diag.get(key, 0) + 1

    need = max(params["box_lookback"] + 3, params["atr_period"] + 2,
               params["sweep_lookback"] + 2)
    if len(entry_candles) < need:
        return None
    a = atr(entry_candles, params["atr_period"])
    if not a or a <= 0 or not atr15:
        return None
    bump("bars")

    c0 = entry_candles[-1]              # candle trigger (baru close)
    c1 = entry_candles[-2]
    box = entry_candles[-(params["box_lookback"] + 2):-2]
    box_hi = max(c["h"] for c in box)
    box_lo = min(c["l"] for c in box)
    box_ok = (box_hi - box_lo) <= params["box_max_atr"] * a

    for side in ("sell", "buy"):
        # --- arah harus sesuai bias (sniper) atau netral (handgun)
        if bias["dir"] == 1 and side == "sell":
            continue
        if bias["dir"] == -1 and side == "buy":
            continue
        mode = "sniper" if bias["dir"] != 0 else "handgun"

        poke = params["poke_min_atr"] * a
        if side == "sell":
            fake_ext = max(c0["h"], c1["h"])
            poked = fake_ext > box_hi + poke
            closed_back = c0["c"] < box_hi and c0["c"] < c0["o"]
            pc = c0 if c0["h"] >= c1["h"] else c1
            rng = pc["h"] - pc["l"]
            wick_ok = rng > 0 and (pc["h"] - max(pc["o"], pc["c"])) >= params["wick_frac"] * rng
        else:
            fake_ext = min(c0["l"], c1["l"])
            poked = fake_ext < box_lo - poke
            closed_back = c0["c"] > box_lo and c0["c"] > c0["o"]
            pc = c0 if c0["l"] <= c1["l"] else c1
            rng = pc["h"] - pc["l"]
            wick_ok = rng > 0 and (min(pc["o"], pc["c"]) - pc["l"]) >= params["wick_frac"] * rng

        if box_ok:
            bump("box")
            if poked:
                bump("poke")
                if closed_back:
                    bump("closed_back")
                    if wick_ok:
                        bump("wick")
        trigger_ok = box_ok and poked and closed_back and wick_ok
        if not trigger_ok:
            continue

        # --- aturan jarum: liquidity lama harus sudah disapu
        wick_lvl = _prominent_wick_level(entry_candles[:-2], side, a, params,
                                         box_hi if side == "sell" else box_lo)
        if wick_lvl is not None:
            if side == "sell" and fake_ext < wick_lvl - 0.02 * a:
                continue  # masih ada jarum di atas yang belum ditembus
            if side == "buy" and fake_ext > wick_lvl + 0.02 * a:
                continue
        bump("sweep")

        # --- harus terjadi di dekat zona S/R 15m/30m
        near = params["zone_near_atr"] * atr15
        if side == "sell":
            zres = nearest_zone(zones["res"], fake_ext, "above", near)
            if zres is None and not any(z["lo"] - near <= fake_ext <= z["hi"] + near
                                        for z in zones["res"]):
                continue
        else:
            zsup = nearest_zone(zones["sup"], fake_ext, "below", near)
            if zsup is None and not any(z["lo"] - near <= fake_ext <= z["hi"] + near
                                        for z in zones["sup"]):
                continue
        bump("zone")

        # --- handgun: hanya fade di tepi range yang cukup lebar
        if mode == "handgun":
            opp = nearest_zone(zones["sup" if side == "sell" else "res"], c0["c"],
                               "below" if side == "sell" else "above")
            if opp is None or opp[0] < params["range_min_atr"] * atr15:
                continue
        bump("mode_ok")

        # --- level entry / SL / TP (risiko terukur)
        entry = c0["c"]
        if side == "sell":
            sl = fake_ext + params["sl_buf_atr"] * a
            risk = sl - entry
        else:
            sl = fake_ext - params["sl_buf_atr"] * a
            risk = entry - sl
        if risk < params["min_risk_atr"] * a or risk > params["max_risk_atr"] * a:
            continue
        bump("risk_ok")

        tp = None
        tgt_side = "below" if side == "sell" else "above"
        tgt_zones = zones["sup"] if side == "sell" else zones["res"]
        cands = []
        for z in tgt_zones:
            d = (entry - z["hi"]) if side == "sell" else (z["lo"] - entry)
            if d > 0:
                cands.append((d, z["hi" if side == "sell" else "lo"]))
        cands.sort()
        for d, lvl in cands:
            rr = d / risk
            if params["min_rr"] <= rr <= params["max_rr"]:
                tp = lvl
                break
        if tp is None:
            rr = params["fallback_rr"]
            tp = entry - rr * risk if side == "sell" else entry + rr * risk
        rr = (entry - tp) / risk if side == "sell" else (tp - entry) / risk

        return {
            "side": side,
            "mode": mode,
            "time": c0["t"],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": round(rr, 2),
            "risk": risk,
            "fake_ext": fake_ext,
            "box_hi": box_hi,
            "box_lo": box_lo,
            "atr": a,
            "bias": bias,
            "checklist": {
                "arah_tf_besar": bias["detail"],
                "mode": mode,
                "box_kocokan": True,
                "false_break": True,
                "candle_jarum": True,
                "dekat_zona": True,
            },
        }
    return None


# ------------------------------------------------------------------ resample

TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def resample(candles, src_tf, dst_tf):
    """Gabungkan candle src_tf menjadi dst_tf (timestamp = awal bucket, UTC)."""
    src, dst = TF_MS[src_tf], TF_MS[dst_tf]
    assert dst % src == 0
    out = []
    for c in candles:
        bucket = (c["t"] // dst) * dst
        if out and out[-1]["t"] == bucket:
            b = out[-1]
            b["h"] = max(b["h"], c["h"])
            b["l"] = min(b["l"], c["l"])
            b["c"] = c["c"]
            b["v"] += c.get("v", 0.0)
        else:
            out.append({"t": bucket, "o": c["o"], "h": c["h"], "l": c["l"],
                        "c": c["c"], "v": c.get("v", 0.0)})
    return out
