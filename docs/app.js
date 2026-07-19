/* XauBotSniper — bot live di browser.
 * Port JavaScript dari bot/strategy.py (aturan & parameter identik).
 * Data: Binance PAXG/USDT (proxy XAU/USD), fallback snapshot GitHub Actions.
 */
"use strict";

/* ================================================================ params */
const P = {
  atr_period: 14,
  box_lookback: 10,
  box_max_atr: 2.0,
  poke_min_atr: 0.05,
  wick_frac: 0.35,
  sweep_lookback: 36,
  wick_prominence_atr: 0.5,
  ema_period: 50,
  ema_slope_bars: 5,
  zone_k: 3,
  zone_lookback: 240,
  zone_tol_pct: 0.0012,
  zone_near_atr: 1.2,
  range_min_atr: 2.0,
  sl_buf_atr: 0.25,
  min_risk_atr: 0.5,
  max_risk_atr: 2.5,
  min_rr: 1.3,
  max_rr: 4.0,
  fallback_rr: 1.5,
  max_cost_frac: 0.2,
  cost_pct_side: 0.0001,
  // hasil riset IS/OOS 730 hari: sisi BUY rugi konsisten -> default sell-only
  sides: "sell",
  session_utc: null,
};

/* Jam market XAU/USD: ~Minggu 22:00 UTC s/d Jumat 21:00 UTC. */
function isXauMarketOpen(tMs) {
  const t = Math.floor(tMs / 1000);
  const dow = (Math.floor(t / 86400) + 4) % 7;
  const hod = Math.floor((t % 86400) / 3600);
  if (dow === 6) return false;
  if (dow === 5 && hod >= 21) return false;
  if (dow === 0 && hod < 22) return false;
  return true;
}

const HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"];
const SYMBOL = "PAXGUSDT";
const TF_MS = { "1m": 6e4, "5m": 3e5, "15m": 9e5, "30m": 18e5, "1h": 36e5, "4h": 144e5, "1d": 864e5 };
const FETCH_PLAN = { "1m": 300, "5m": 300, "15m": 240, "30m": 240, "1h": 200, "4h": 200, "1d": 200 };

/* ================================================================ engine */
function emaSeries(values, period) {
  if (values.length < period) return [];
  const k = 2 / (period + 1);
  const out = new Array(period - 1).fill(null);
  let prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
  out.push(prev);
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out.push(prev);
  }
  return out;
}

function atr(candles, period) {
  const n = candles.length;
  if (n < period + 1) return null;
  let s = 0;
  for (let i = n - period; i < n; i++) {
    const c = candles[i], p = candles[i - 1];
    s += Math.max(c.h - c.l, Math.abs(c.h - p.c), Math.abs(c.l - p.c));
  }
  return s / period;
}

function tfDirection(candles) {
  const closes = candles.map((c) => c.c);
  const e = emaSeries(closes, P.ema_period);
  const sb = P.ema_slope_bars;
  if (!e.length || e.length < P.ema_period + sb) return 0;
  const close = closes[closes.length - 1];
  const now = e[e.length - 1], then = e[e.length - 1 - sb];
  if (now == null || then == null) return 0;
  if (close > now && now > then) return 1;
  if (close < now && now < then) return -1;
  return 0;
}

function htfBias(d1, h4, h1) {
  const dd = tfDirection(d1), d4 = tfDirection(h4), dh = tfDirection(h1);
  const detail = { d1: dd, h4: d4, h1: dh };
  if (dd !== 0 && (d4 === dd || dh === dd)) {
    return { dir: dd, mode: "sniper", strength: 1 + (d4 === dd) + (dh === dd), detail };
  }
  if (dd === 0 && d4 !== 0 && d4 === dh) {
    return { dir: d4, mode: "sniper", strength: 2, detail };
  }
  return { dir: 0, mode: "handgun", strength: 0, detail };
}

function pivotPoints(candles, k) {
  const highs = [], lows = [];
  for (let i = k; i < candles.length - k; i++) {
    let isH = true, isL = true;
    for (let j = i - k; j <= i + k; j++) {
      if (j === i) continue;
      if (candles[j].h > candles[i].h) isH = false;
      if (candles[j].l < candles[i].l) isL = false;
    }
    if (isH) highs.push(candles[i].h);
    if (isL) lows.push(candles[i].l);
  }
  return [highs, lows];
}

function clusterZones(prices, tolPct) {
  if (!prices.length) return [];
  prices = prices.slice().sort((a, b) => a - b);
  const groups = [];
  let cur = [prices[0]];
  for (let i = 1; i < prices.length; i++) {
    if (prices[i] - cur[cur.length - 1] <= cur[0] * tolPct * 2) cur.push(prices[i]);
    else { groups.push(cur); cur = [prices[i]]; }
  }
  groups.push(cur);
  return groups.map((g) => ({
    lo: Math.min(...g), hi: Math.max(...g),
    mid: (Math.min(...g) + Math.max(...g)) / 2, touches: g.length,
  }));
}

function buildZones(m15, m30) {
  const lb = P.zone_lookback;
  const highs = [], lows = [];
  for (const series of [m15.slice(-lb), m30.slice(-lb)]) {
    const [h, l] = pivotPoints(series, P.zone_k);
    highs.push(...h);
    lows.push(...l);
  }
  return { res: clusterZones(highs, P.zone_tol_pct), sup: clusterZones(lows, P.zone_tol_pct) };
}

function nearestZone(zones, price, side, maxDist) {
  let best = null;
  for (const z of zones) {
    let d = side === "above" ? z.lo - price : price - z.hi;
    if (d < 0) {
      if (z.lo <= price && price <= z.hi) d = 0;
      else continue;
    }
    if (maxDist != null && d > maxDist) continue;
    if (!best || d < best[0]) best = [d, z];
  }
  return best;
}

/* Jarum menonjol di dekat tepi box (<= 1.5 ATR) yang belum disapu. */
function prominentWickLevel(candles, side, atrVal, ref) {
  const lb = P.sweep_lookback;
  const thr = P.wick_prominence_atr * atrVal;
  let best = null;
  for (const c of candles.slice(-lb)) {
    const rng = c.h - c.l;
    if (rng <= 0) continue;
    if (side === "sell") {
      if (c.h - Math.max(c.o, c.c) >= thr && ref < c.h && c.h <= ref + 1.5 * atrVal)
        best = best == null ? c.h : Math.max(best, c.h);
    } else {
      if (Math.min(c.o, c.c) - c.l >= thr && ref - 1.5 * atrVal <= c.l && c.l < ref)
        best = best == null ? c.l : Math.min(best, c.l);
    }
  }
  return best;
}

/* Evaluasi candle terakhir yang sudah close. diag=true -> kembalikan status
 * parsial untuk checklist UI walau belum ada sinyal penuh. */
function detectSignal(entry, zones, bias, atr15, diag) {
  const need = Math.max(P.box_lookback + 3, P.atr_period + 2, P.sweep_lookback + 2);
  const D = { boxOk: false, poked: false, closedBack: false, wickOk: false,
              sweepOk: false, nearZone: false, biasOk: bias.dir !== 0,
              boxHi: null, boxLo: null };
  if (entry.length < need) return { signal: null, diag: D };
  const a = atr(entry, P.atr_period);
  if (!a || a <= 0 || !atr15) return { signal: null, diag: D };

  const c0 = entry[entry.length - 1];
  if (!isXauMarketOpen(c0.t)) return { signal: null, diag: D };
  if (P.session_utc) {
    const hod = Math.floor(c0.t / 36e5) % 24;
    if (!(P.session_utc[0] <= hod && hod < P.session_utc[1])) return { signal: null, diag: D };
  }
  const fakeWin = entry.slice(-3);
  const box = entry.slice(-(P.box_lookback + 3), -3);
  const boxHi = Math.max(...box.map((c) => c.h));
  const boxLo = Math.min(...box.map((c) => c.l));
  D.boxHi = boxHi; D.boxLo = boxLo;
  D.boxOk = boxHi - boxLo <= P.box_max_atr * a;

  for (const side of ["sell", "buy"]) {
    if (P.sides !== "both" && side !== P.sides) continue;
    if (bias.dir === 1 && side === "sell") continue;
    if (bias.dir === -1 && side === "buy") continue;
    const mode = bias.dir !== 0 ? "sniper" : "handgun";
    const poke = P.poke_min_atr * a;
    let fakeExt, poked, closedBack, wickOk;
    if (side === "sell") {
      fakeExt = Math.max(...fakeWin.map((c) => c.h));
      poked = fakeExt > boxHi + poke;
      closedBack = c0.c < boxHi && c0.c < c0.o;
      const pc = fakeWin.reduce((a, b) => (b.h >= a.h ? b : a));
      const rng = pc.h - pc.l;
      wickOk = rng > 0 && pc.h - Math.max(pc.o, pc.c) >= P.wick_frac * rng;
    } else {
      fakeExt = Math.min(...fakeWin.map((c) => c.l));
      poked = fakeExt < boxLo - poke;
      closedBack = c0.c > boxLo && c0.c > c0.o;
      const pc = fakeWin.reduce((a, b) => (b.l <= a.l ? b : a));
      const rng = pc.h - pc.l;
      wickOk = rng > 0 && Math.min(pc.o, pc.c) - pc.l >= P.wick_frac * rng;
    }
    if (diag) {
      D.poked = D.poked || poked;
      D.closedBack = D.closedBack || closedBack;
      D.wickOk = D.wickOk || wickOk;
    }
    if (!(D.boxOk && poked && closedBack && wickOk)) continue;

    const wickLvl = prominentWickLevel(entry.slice(0, -3), side, a,
                                       side === "sell" ? boxHi : boxLo);
    let sweepOk = true;
    if (wickLvl != null) {
      if (side === "sell" && fakeExt < wickLvl - 0.02 * a) sweepOk = false;
      if (side === "buy" && fakeExt > wickLvl + 0.02 * a) sweepOk = false;
    }
    D.sweepOk = sweepOk;
    if (!sweepOk) continue;

    const near = P.zone_near_atr * atr15;
    const inZone = (zs) => zs.some((z) => z.lo - near <= fakeExt && fakeExt <= z.hi + near);
    let zoneOk;
    if (side === "sell") {
      zoneOk = nearestZone(zones.res, fakeExt, "above", near) != null || inZone(zones.res);
    } else {
      zoneOk = nearestZone(zones.sup, fakeExt, "below", near) != null || inZone(zones.sup);
    }
    D.nearZone = zoneOk;
    if (!zoneOk) continue;

    if (mode === "handgun") {
      const opp = nearestZone(side === "sell" ? zones.sup : zones.res, c0.c,
                              side === "sell" ? "below" : "above", null);
      if (!opp || opp[0] < P.range_min_atr * atr15) continue;
      D.biasOk = true; // handgun sah saat ranging
    }

    const entryPx = c0.c;
    let sl, risk;
    if (side === "sell") { sl = fakeExt + P.sl_buf_atr * a; risk = sl - entryPx; }
    else { sl = fakeExt - P.sl_buf_atr * a; risk = entryPx - sl; }
    if (risk < P.min_risk_atr * a || risk > P.max_risk_atr * a) continue;
    if (entryPx * P.cost_pct_side * 2 > P.max_cost_frac * risk) continue;

    let tp = null;
    const tgtZones = side === "sell" ? zones.sup : zones.res;
    const cands = [];
    for (const z of tgtZones) {
      const d = side === "sell" ? entryPx - z.hi : z.lo - entryPx;
      if (d > 0) cands.push([d, side === "sell" ? z.hi : z.lo]);
    }
    cands.sort((x, y) => x[0] - y[0]);
    for (const [d, lvl] of cands) {
      const rr = d / risk;
      if (rr >= P.min_rr && rr <= P.max_rr) { tp = lvl; break; }
    }
    if (tp == null) tp = side === "sell" ? entryPx - P.fallback_rr * risk : entryPx + P.fallback_rr * risk;
    const rr = side === "sell" ? (entryPx - tp) / risk : (tp - entryPx) / risk;

    return {
      signal: { side, mode, time: c0.t, entry: entryPx, sl, tp,
                rr: Math.round(rr * 100) / 100, fakeExt, boxHi, boxLo, atr: a, bias },
      diag: { ...D, sweepOk: true, nearZone: true, biasOk: true },
    };
  }
  return { signal: null, diag: D };
}

/* ============================================================== data feed */
const state = {
  candles: {},          // per TF, hanya bar yang sudah close
  price: null,
  prevPrice: null,
  bias: null,
  zones: null,
  atr15: null,
  signal: null,         // sinyal aktif terakhir {sig, tf, shownAt}
  diag: null,
  chartTf: "5m",
  source: "—",
  lastUpdate: null,
  usingSnapshot: false,
  history: [],
};

async function fetchJson(path) {
  let lastErr;
  for (const host of HOSTS) {
    try {
      const r = await fetch(host + path, { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      state.source = host.includes("vision") ? "binance.vision" : "binance.com";
      return await r.json();
    } catch (e) { lastErr = e; }
  }
  throw lastErr;
}

async function fetchKlines(tf, limit) {
  const raw = await fetchJson(`/api/v3/klines?symbol=${SYMBOL}&interval=${tf}&limit=${limit}`);
  const now = Date.now();
  return raw
    .map((k) => ({ t: k[0], o: +k[1], h: +k[2], l: +k[3], c: +k[4], v: +k[5] }))
    .filter((c) => c.t + TF_MS[tf] <= now);
}

async function refreshAll() {
  try {
    const tfs = Object.keys(FETCH_PLAN);
    const results = await Promise.all(tfs.map((tf) => fetchKlines(tf, FETCH_PLAN[tf])));
    tfs.forEach((tf, i) => { state.candles[tf] = results[i]; });
    const t = await fetchJson(`/api/v3/ticker/price?symbol=${SYMBOL}`);
    state.prevPrice = state.price;
    state.price = +t.price;
    state.lastUpdate = Date.now();
    state.usingSnapshot = false;
    compute();
    hideWarning();
  } catch (e) {
    await loadSnapshotFallback(e);
  }
  render();
}

async function loadSnapshotFallback(err) {
  try {
    const r = await fetch("data/live.json", { cache: "no-store" });
    if (!r.ok) throw new Error("no snapshot");
    const snap = await r.json();
    state.price = snap.price;
    state.bias = snap.bias;
    state.zones = snap.zones;
    state.atr15 = snap.atr15;
    state.usingSnapshot = true;
    state.source = "snapshot GitHub Actions";
    state.lastUpdate = snap.generated_at;
    if (snap.signal_5m) state.signal = { sig: snap.signal_5m, tf: "5m", shownAt: Date.now() };
    showWarning("⚠️ API market tidak bisa diakses dari browser ini (" + err.message +
      "). Menampilkan snapshot terakhir dari GitHub Actions — data mungkin tertunda ±1 jam.");
  } catch {
    showWarning("⚠️ Tidak bisa memuat data market: " + err.message +
      ". Coba muat ulang, atau cek koneksi/adblocker.");
  }
}

function compute() {
  const { candles } = state;
  if (!candles["1d"] || !candles["15m"]) return;
  state.bias = htfBias(candles["1d"], candles["4h"], candles["1h"]);
  state.zones = buildZones(candles["15m"], candles["30m"]);
  state.atr15 = atr(candles["15m"], P.atr_period);

  // evaluasi entry di 5m dan 1m
  let found = null, diag = null;
  for (const tf of ["5m", "1m"]) {
    const r = detectSignal(candles[tf], state.zones, state.bias, state.atr15, tf === "5m");
    if (tf === "5m") diag = r.diag;
    if (r.signal && !found) found = { sig: r.signal, tf };
  }
  state.diag = diag;
  if (found) {
    const prev = state.signal;
    const isNew = !prev || prev.sig.time !== found.sig.time || prev.sig.side !== found.sig.side;
    state.signal = { ...found, shownAt: Date.now() };
    if (isNew) onNewSignal(found.sig, found.tf);
  } else if (state.signal && Date.now() - state.signal.shownAt > 30 * 60e3) {
    state.signal = null; // sinyal kedaluwarsa setelah 30 menit
  }
}

function onNewSignal(sig, tf) {
  state.history.unshift({ ...sig, tf });
  state.history = state.history.slice(0, 50);
  try { localStorage.setItem("xbs_hist", JSON.stringify(state.history)); } catch {}
  if (Notification && Notification.permission === "granted") {
    const arah = sig.side === "sell" ? "SELL 🔻" : "BUY 🔼";
    new Notification(`XauBotSniper: ${arah} @ ${fmt(sig.entry)}`, {
      body: `Mode ${sig.mode} (TF ${tf}) · SL ${fmt(sig.sl)} · TP ${fmt(sig.tp)} · RR ${sig.rr}`,
    });
  }
}

/* ==================================================================== UI */
const $ = (id) => document.getElementById(id);
const fmt = (x) => x == null ? "—" : Number(x).toLocaleString("id-ID", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const wib = (ms) => new Date(ms).toLocaleString("id-ID", { timeZone: "Asia/Jakarta", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const ARROW = { 1: ["↑ NAIK", "up"], "-1": ["↓ TURUN", "down"], 0: ["− NETRAL", "flat"] };

function showWarning(msg) { const w = $("conn-warning"); w.textContent = msg; w.classList.remove("hidden"); }
function hideWarning() { $("conn-warning").classList.add("hidden"); }

function render() {
  // harga
  const p = $("price");
  p.textContent = state.price ? fmt(state.price) : "—";
  p.className = "price " + (state.prevPrice == null ? "" : state.price >= state.prevPrice ? "up" : "down");
  $("price-meta").textContent = state.lastUpdate
    ? `${SYMBOL} · ${state.source} · ${wib(state.lastUpdate)} WIB` : "memuat data…";

  // bias
  if (state.bias) {
    for (const [key, el] of [["d1", "bias-d1"], ["h4", "bias-h4"], ["h1", "bias-h1"]]) {
      const [txt, cls] = ARROW[state.bias.detail[key]];
      $(el).textContent = txt;
      $(el).className = cls;
    }
    const b = state.bias;
    let btxt = b.dir === 0
      ? "Market RANGING (TF besar tidak selaras) → mode HANDGUN: fade tepi range 15m."
      : `Tren besar ${b.dir === 1 ? "NAIK" : "TURUN — fokus cari SELL"} ` +
        `(${b.strength}/3 TF selaras) → mode SNIPER.`;
    if (P.sides === "sell" && b.dir === 1)
      btxt += " Bot default hanya SELL (riset 730 hari: BUY mekanis rugi) → menunggu.";
    $("bias-summary").textContent = btxt;
  }

  // zona
  if (state.zones && state.price) {
    const res = nearestZone(state.zones.res, state.price, "above", null);
    const sup = nearestZone(state.zones.sup, state.price, "below", null);
    $("zone-res").textContent = res ? `${fmt(res[1].lo)} – ${fmt(res[1].hi)} (+${fmt(res[0])})` : "—";
    $("zone-sup").textContent = sup ? `${fmt(sup[1].lo)} – ${fmt(sup[1].hi)} (−${fmt(sup[0])})` : "—";
  }

  // checklist
  if (state.diag) {
    const D = state.diag;
    const set = (id, ok, extra) => {
      const li = $(id);
      li.classList.toggle("ok", !!ok);
      li.innerHTML = (ok ? "✅ " : "⬜ ") + li.innerHTML.replace(/^([✅⬜]\s*)/, "").replace(/^([✅⬜])/, "");
      if (extra) li.innerHTML = li.innerHTML.replace(/ <i class="dim">.*<\/i>$/, "") + ` <i class="dim">${extra}</i>`;
    };
    set("ck-box", D.boxOk, D.boxHi ? `${fmt(D.boxLo)}–${fmt(D.boxHi)}` : "");
    set("ck-poke", D.poked);
    set("ck-wick", D.wickOk);
    set("ck-sweep", D.sweepOk);
    set("ck-zone", D.nearZone);
    set("ck-bias", D.biasOk);
  }

  // sinyal
  const card = $("signal-card"), st = $("signal-status");
  if (state.signal) {
    const { sig, tf } = state.signal;
    st.textContent = sig.side.toUpperCase() + (sig.side === "sell" ? " 🔻" : " 🔼");
    st.className = "signal-status " + sig.side;
    card.className = "card signal-card " + sig.side;
    $("signal-detail").textContent =
      `Mode ${sig.mode.toUpperCase()} di TF ${tf} — ${wib(sig.time)} WIB. ` +
      `False break ${fmt(sig.fakeExt)} dari box ${fmt(sig.boxLo)}–${fmt(sig.boxHi)}.`;
    $("signal-levels").classList.remove("hidden");
    $("lv-entry").textContent = fmt(sig.entry);
    $("lv-sl").textContent = fmt(sig.sl);
    $("lv-tp").textContent = fmt(sig.tp);
    $("lv-rr").textContent = "1 : " + sig.rr;
  } else {
    st.textContent = "WAIT";
    st.className = "signal-status wait";
    card.className = "card signal-card";
    $("signal-detail").textContent = "Menunggu setup ideal… bot hanya entry saat pola " +
      "kocokan + dorongan terakhir muncul di zona S/R searah tren besar.";
    $("signal-levels").classList.add("hidden");
  }

  renderHistory();
  drawChart();
  $("foot-status").textContent = state.usingSnapshot
    ? "mode fallback (snapshot)" : "live · refresh tiap 20 detik";
}

function renderHistory() {
  const tb = $("hist-body");
  if (!state.history.length) {
    tb.innerHTML = '<tr><td colspan="8" class="dim">Belum ada sinyal.</td></tr>';
    return;
  }
  tb.innerHTML = state.history.map((h) => `<tr>
    <td>${wib(h.time)}</td><td>${h.tf}</td>
    <td class="${h.side}">${h.side.toUpperCase()}</td><td>${h.mode}</td>
    <td>${fmt(h.entry)}</td><td>${fmt(h.sl)}</td><td>${fmt(h.tp)}</td><td>${h.rr}</td>
  </tr>`).join("");
}

/* ---------------------------------------------------------------- chart */
function drawChart() {
  const cv = $("chart");
  const tf = state.chartTf;
  const candles = (state.candles[tf] || []).slice(-120);
  const ctx = cv.getContext("2d");
  const W = (cv.width = cv.clientWidth * devicePixelRatio);
  const H = (cv.height = 380 * devicePixelRatio);
  ctx.clearRect(0, 0, W, H);
  if (candles.length < 5) {
    ctx.fillStyle = "#8b949e";
    ctx.font = `${13 * devicePixelRatio}px sans-serif`;
    ctx.fillText("Menunggu data…", 20, 40);
    return;
  }
  const padR = 64 * devicePixelRatio, padT = 12 * devicePixelRatio, padB = 22 * devicePixelRatio;
  let lo = Math.min(...candles.map((c) => c.l));
  let hi = Math.max(...candles.map((c) => c.h));
  const sig = state.signal && state.signal.sig;
  if (sig) { lo = Math.min(lo, sig.tp, sig.sl); hi = Math.max(hi, sig.tp, sig.sl); }
  const span = (hi - lo) || 1;
  lo -= span * 0.05; hi += span * 0.05;
  const X = (i) => ((i + 0.5) / candles.length) * (W - padR);
  const Y = (p) => padT + (1 - (p - lo) / (hi - lo)) * (H - padT - padB);
  const cw = Math.max(2, ((W - padR) / candles.length) * 0.65);

  // zona S/R
  if (state.zones) {
    for (const [zs, color] of [[state.zones.res, "rgba(248,81,73,0.10)"], [state.zones.sup, "rgba(63,185,80,0.10)"]]) {
      for (const z of zs) {
        if (z.hi < lo || z.lo > hi) continue;
        ctx.fillStyle = color;
        ctx.fillRect(0, Y(z.hi), W - padR, Math.max(2, Y(z.lo) - Y(z.hi)));
      }
    }
  }

  // box kocokan (di TF entry saja)
  if (state.diag && state.diag.boxHi && (tf === "5m" || tf === "1m")) {
    ctx.strokeStyle = "rgba(88,166,255,0.8)";
    ctx.setLineDash([5, 4]);
    ctx.lineWidth = devicePixelRatio;
    const x0 = X(Math.max(0, candles.length - P.box_lookback - 3));
    ctx.strokeRect(x0, Y(state.diag.boxHi), X(candles.length - 1) - x0, Y(state.diag.boxLo) - Y(state.diag.boxHi));
    ctx.setLineDash([]);
  }

  // candlestick
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    const up = c.c >= c.o;
    ctx.strokeStyle = ctx.fillStyle = up ? "#3fb950" : "#f85149";
    ctx.lineWidth = devicePixelRatio;
    ctx.beginPath();
    ctx.moveTo(X(i), Y(c.h));
    ctx.lineTo(X(i), Y(c.l));
    ctx.stroke();
    const yO = Y(c.o), yC = Y(c.c);
    ctx.fillRect(X(i) - cw / 2, Math.min(yO, yC), cw, Math.max(1, Math.abs(yC - yO)));
  }

  // level sinyal
  if (sig) {
    const lines = [[sig.entry, "#58a6ff", "Entry"], [sig.sl, "#f85149", "SL"], [sig.tp, "#3fb950", "TP"]];
    ctx.font = `${11 * devicePixelRatio}px sans-serif`;
    for (const [pv, color, label] of lines) {
      ctx.strokeStyle = color;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(0, Y(pv));
      ctx.lineTo(W - padR, Y(pv));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.fillText(`${label} ${fmt(pv)}`, W - padR + 4, Y(pv) + 4);
    }
  }

  // sumbu harga & harga terakhir
  ctx.fillStyle = "#8b949e";
  ctx.font = `${11 * devicePixelRatio}px sans-serif`;
  for (let i = 0; i <= 4; i++) {
    const pv = lo + ((hi - lo) * i) / 4;
    ctx.fillText(fmt(pv), W - padR + 4, Y(pv) + 4);
  }
  const last = candles[candles.length - 1];
  ctx.fillStyle = last.c >= last.o ? "#3fb950" : "#f85149";
  ctx.fillText("● " + fmt(last.c), W - padR + 4, Y(last.c) - 8 * devicePixelRatio);
  // label waktu
  ctx.fillStyle = "#8b949e";
  const t0 = wib(candles[0].t), t1 = wib(last.t);
  ctx.fillText(t0, 4, H - 6 * devicePixelRatio);
  ctx.fillText(t1, W - padR - ctx.measureText(t1).width - 6, H - 6 * devicePixelRatio);
}

/* ------------------------------------------------------------- backtest */
async function loadBacktest() {
  try {
    const r = await fetch("data/backtest.json", { cache: "no-store" });
    if (!r.ok) throw new Error();
    const bt = await r.json();
    $("bt-missing").classList.add("hidden");
    $("bt-content").classList.remove("hidden");
    renderBacktest(bt);
  } catch {
    $("bt-missing").classList.remove("hidden");
    $("bt-content").classList.add("hidden");
  }
}

function statCard(k, v, cls = "") {
  return `<div class="stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
}

function statsRowHtml(name, s) {
  return `<tr><td>${name}</td><td>${s.trades}</td><td>${s.win_rate}%</td>
    <td>${s.profit_factor ?? "—"}</td><td class="${s.total_r >= 0 ? "pos" : "neg"}">${s.total_r}R</td></tr>`;
}

function renderBacktest(bt) {
  const s = bt.stats;
  $("bt-stats").innerHTML =
    statCard("TRADE", s.trades) +
    statCard("WIN RATE", s.win_rate + "%") +
    statCard("PROFIT FACTOR", s.profit_factor ?? "—") +
    statCard("TOTAL R", s.total_r + "R", s.total_r >= 0 ? "pos" : "neg") +
    statCard("RETURN", s.return_pct + "%", s.return_pct >= 0 ? "pos" : "neg") +
    statCard("MAX DRAWDOWN", s.max_drawdown_pct + "%", "neg");

  const head = "<thead><tr><th></th><th>Trade</th><th>WR</th><th>PF</th><th>Total R</th></tr></thead>";
  $("bt-mode").innerHTML = head + "<tbody>" +
    statsRowHtml("Sniper (trending)", bt.by_mode.sniper) +
    statsRowHtml("Handgun (ranging)", bt.by_mode.handgun) + "</tbody>";
  $("bt-side").innerHTML = head + "<tbody>" +
    statsRowHtml("BUY", bt.by_side.buy) + statsRowHtml("SELL", bt.by_side.sell) + "</tbody>";

  $("bt-monthly").innerHTML =
    "<thead><tr><th>Bulan</th><th>Trade</th><th>Win</th><th>Total R</th></tr></thead><tbody>" +
    bt.monthly.map((m) => `<tr><td>${m.month}</td><td>${m.trades}</td><td>${m.wins}</td>
      <td class="${m.r >= 0 ? "pos" : "neg"}">${m.r}R</td></tr>`).join("") + "</tbody>";

  $("bt-trades").innerHTML =
    "<thead><tr><th>Masuk (WIB)</th><th>Arah</th><th>Mode</th><th>Entry</th><th>SL</th><th>TP</th><th>Exit</th><th>Hasil</th><th>R</th></tr></thead><tbody>" +
    bt.trades.slice().reverse().slice(0, 60).map((t) => `<tr>
      <td>${wib(t.time)}</td><td class="${t.side}">${t.side.toUpperCase()}</td><td>${t.mode}</td>
      <td>${fmt(t.entry)}</td><td>${fmt(t.sl)}</td><td>${fmt(t.tp)}</td><td>${fmt(t.exit)}</td>
      <td>${t.result}</td><td class="${t.r_net >= 0 ? "pos" : "neg"}">${t.r_net}</td></tr>`).join("") + "</tbody>";

  $("bt-meta").textContent =
    `Backtest ${bt.days} hari · entry TF ${bt.entry_tf} · ${bt.symbol} (${bt.proxy_note}) · ` +
    `biaya ${bt.assumptions.cost_pct_side * 100}%/sisi · SL+TP satu bar dihitung SL (konservatif) · ` +
    `dibuat ${wib(bt.generated_at)} WIB`;

  drawEquity(bt.equity_curve);
}

function drawEquity(curve) {
  const cv = $("equity-chart");
  const ctx = cv.getContext("2d");
  const W = (cv.width = cv.clientWidth * devicePixelRatio);
  const H = (cv.height = 260 * devicePixelRatio);
  ctx.clearRect(0, 0, W, H);
  if (!curve || curve.length < 2) return;
  const padR = 70 * devicePixelRatio, pad = 12 * devicePixelRatio;
  const eqs = curve.map((p) => p.eq);
  const lo = Math.min(...eqs, 1000), hi = Math.max(...eqs, 1000);
  const X = (i) => (i / (curve.length - 1)) * (W - padR);
  const Y = (v) => pad + (1 - (v - lo) / (hi - lo || 1)) * (H - 2 * pad);
  ctx.strokeStyle = "#2d333b";
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(0, Y(1000)); ctx.lineTo(W - padR, Y(1000)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = eqs[eqs.length - 1] >= 1000 ? "#3fb950" : "#f85149";
  ctx.lineWidth = 2 * devicePixelRatio;
  ctx.beginPath();
  curve.forEach((p, i) => (i ? ctx.lineTo(X(i), Y(p.eq)) : ctx.moveTo(X(i), Y(p.eq))));
  ctx.stroke();
  ctx.fillStyle = "#8b949e";
  ctx.font = `${11 * devicePixelRatio}px sans-serif`;
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    ctx.fillText("$" + fmt(v), W - padR + 4, Y(v) + 4);
  }
  ctx.fillText(wib(curve[0].t), 4, H - 4);
  const tEnd = wib(curve[curve.length - 1].t);
  ctx.fillText(tEnd, W - padR - ctx.measureText(tEnd).width - 8, H - 4);
}

/* ---------------------------------------------------------------- wiring */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-page").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "backtest") loadBacktest();
  });
});

document.querySelectorAll("#tf-btns button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tf-btns button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.chartTf = btn.dataset.tf;
    drawChart();
  });
});

$("notif-toggle").addEventListener("change", (e) => {
  if (e.target.checked && Notification.permission !== "granted") {
    Notification.requestPermission().then((p) => {
      if (p !== "granted") e.target.checked = false;
    });
  }
});

try { state.history = JSON.parse(localStorage.getItem("xbs_hist") || "[]"); } catch {}
window.addEventListener("resize", () => { drawChart(); });

refreshAll();
setInterval(refreshAll, 20_000);
