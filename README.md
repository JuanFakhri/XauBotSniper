# 🎯 XauBotSniper

Bot sinyal **emas (XAU/USD)** berbasis strategi *naked chart multi-timeframe*
(sniper & handgun) yang berjalan **live di GitHub Pages** — tanpa server,
tanpa biaya. Lengkap dengan **backtester** otomatis via GitHub Actions.

> Data harga memakai **PAXG/USDT** (PAX Gold, 1 token = 1 troy oz emas) sebagai
> proxy XAU/USD karena gratis, real-time, dan riwayatnya panjang.

## Strategi yang diotomatiskan

Tiga langkah (dari teknik scalping naked chart Franz / TCI):

| Langkah | Timeframe | Yang dilakukan bot |
|---|---|---|
| 1. Arah market | 1H, 4H, Daily | Baca tren dari harga vs EMA50 + kemiringannya. Daily = bos, minimal 1 TF lain konfirmasi. Jangan melawan arus. |
| 2. Area potensial | 15m, 30m | Cluster swing high/low → zona support/resistance. Entry hanya di dekat zona. |
| 3. Eksekusi entry | 1m, 5m | Tunggu pola **kocokan** (box konsolidasi rapat), lalu **dorongan terakhir** (false break ambil liquidity) yang gagal + **candle jarum** menolak level → entry berlawanan tusukan. |

Prinsip yang dikodekan:

- **Harga tidak bergerak dengan mudah** — wajib ada kocokan dulu sebelum meledak.
- **Waspadai candle jarum** — bila ada jarum menonjol yang belum ditembus, bot
  *tidak* buru-buru entry (level itu biasanya disapu dulu).
- **Market itu fraktal** — aturan yang sama dipakai di 1m dan 5m.
- **Sniper vs Handgun** — trending → entry searah tren; ranging → fade tepi range 15m.
- **Risiko terukur** — SL di balik titik false break + buffer ATR; setup dengan SL
  terlalu jauh dilewati.
- **Disiplin TP** — target di zona S/R terdekat berikutnya (tidak serakah), timeout ±8 jam.

## Cara mengaktifkan (sekali saja)

1. **GitHub Pages**: Settings → Pages → *Deploy from a branch* → pilih branch ini
   → folder **/docs** → Save. Dashboard live muncul di
   `https://<username>.github.io/XauBotSniper/`.
2. **Backtest**: tab Actions → workflow **"Backtest strategi sniper"** → *Run
   workflow* (default 365 hari). Hasil otomatis muncul di tab 📊 Backtest dashboard.
   Backtest juga refresh otomatis tiap minggu.
3. **Snapshot sinyal**: workflow **"Scan sinyal berkala"** menulis
   `docs/data/live.json` tiap jam sebagai fallback bila API market tidak bisa
   diakses dari browser.

Dashboard menghitung sinyal **live di browser** (refresh tiap 20 detik) langsung
dari API publik Binance — GitHub Pages hanya menyajikan file statis, jadi bot
tetap "hidup" selama halaman terbuka. Aktifkan ✔ Notifikasi untuk push
notification browser saat sinyal muncul.

## Struktur

```
bot/strategy.py    # engine strategi (pure Python, tanpa dependensi)
bot/backtest.py    # backtester anti-lookahead → docs/data/backtest.json
bot/live_scan.py   # snapshot sinyal → docs/data/live.json
docs/              # dashboard GitHub Pages (engine yang sama, port JS)
.github/workflows/ # backtest mingguan + scan per jam
```

## Riset varian (anti-overfit)

`bot/research.py` menguji 16 varian (BE-stop, filter sesi, sell-only, TP tetap)
dengan validasi in-sample/out-of-sample. Temuan di 730 hari data: sisi BUY
mekanis rugi di kedua paruh data, jadi default bot **sell-only** (`sides` di
`bot/strategy.py`). Workflow **"Riset varian strategi"** bisa dijalankan ulang
kapan saja dari tab Actions.

Backtest: entry di close candle trigger, SL & TP di bar yang sama dihitung SL
(konservatif), biaya 0.01%/sisi, risiko 1% ekuitas per trade, satu posisi pada
satu waktu.

## ⚠️ Disclaimer

Proyek edukasi/analisis — **bukan saran keuangan**. Sinyal tidak menjamin
profit; hasil backtest tidak menjamin hasil di masa depan. Trading
emas/leverage berisiko tinggi. Selalu pakai manajemen risiko.
