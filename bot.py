import yfinance as yf
import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime, timezone, timedelta

BOT_TOKEN  = "8429138467:AAEV3QF6VPqFys1jINIXB0Fs3hA_-Xhxnhk"
CHAT_ID    = "-1003872921226"
SYMBOL     = "^NSEI"
SCAN_EVERY = 60
SWING_LEN  = 5
SL_BUFFER  = 15

# RR Settings
RR_TP1 = 1.5
RR_TP2 = 3.0

last_signal         = {"type": None, "bar": 0}
market_opened_today = None

IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────
# EXPIRY / DAY BASED OPTION SETTINGS
# ─────────────────────────────────────────
def get_option_settings():
    day = datetime.now(IST).weekday()  # 0=Mon, 3=Thu
    if day == 3:  # Thursday expiry
        return {"offset": 100, "tp1": 10, "tp2": 20, "sl": 10, "label": "OTM (Expiry Day)"}
    elif day == 2:  # Wednesday
        return {"offset": 0,   "tp1": 25, "tp2": 45, "sl": 20, "label": "ATM"}
    else:  # Mon, Tue, Fri
        return {"offset": 0,   "tp1": 30, "tp2": 60, "sl": 25, "label": "ATM"}

# ─────────────────────────────────────────
# MARKET OPEN CHECK
# ─────────────────────────────────────────
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=15, second=0) <= now <= now.replace(hour=15, minute=30, second=0)

# ─────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("Sent:", r.status_code)
    except Exception as e:
        print("Telegram Error:", e)

def send_market_open_message():
    now_ist = datetime.now(IST).strftime("%d %b %Y")
    day     = datetime.now(IST).weekday()
    expiry  = "⚠️ <b>EXPIRY DAY</b> — OTM Signals Active!\n" if day == 3 else ""
    send_telegram(
        "🔔 <b>Market is Now Open!</b>\n\n"
        f"📅 {now_ist}\n"
        f"{expiry}"
        "📊 Scanning NIFTY 50 — Multi Timeframe\n"
        "⏱ 15m + 5m + 3m Analysis\n"
        "🕐 9:15 AM - 3:30 PM IST\n"
        "✅ Bot is Active!\n\n"
        "⚠️ <i>Educational only. Trade at your own risk.</i>"
    )
    print("Market Open message sent!")

def send_ready_alert(direction):
    if direction == "BUY":
        msg = "⚡️ <b>SIGNAL LOADING...</b>\n\n🟢 Get Ready for <b>CE</b> 📈\n⏳ Confirming entry... wait for signal!"
    else:
        msg = "⚡️ <b>SIGNAL LOADING...</b>\n\n🔴 Get Ready for <b>PE</b> 📉\n⏳ Confirming entry... wait for signal!"
    send_telegram(msg)

# ─────────────────────────────────────────
# DATA FETCH — MULTI TIMEFRAME
# ─────────────────────────────────────────
def get_candles(interval, period="5d"):
    try:
        df = yf.download(SYMBOL, period=period, interval=interval, progress=False)
        df.dropna(inplace=True)
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        return df.reset_index()
    except Exception as e:
        print(f"Data error ({interval}):", e)
        return None

# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))

def calc_volume_ok(df):
    avg_vol = df["volume"].rolling(20).mean().iloc[-1]
    cur_vol = df["volume"].iloc[-1]
    return cur_vol > avg_vol * 0.8  # volume at least 80% of avg

# ─────────────────────────────────────────
# SMC FUNCTIONS
# ─────────────────────────────────────────
def find_swings(df):
    highs, lows = [], []
    for i in range(SWING_LEN, len(df) - SWING_LEN):
        if all(df["high"].iloc[i] >= df["high"].iloc[i-j] for j in range(1, SWING_LEN+1)) and \
           all(df["high"].iloc[i] >= df["high"].iloc[i+j] for j in range(1, SWING_LEN+1)):
            highs.append({"i": i, "price": df["high"].iloc[i]})
        if all(df["low"].iloc[i] <= df["low"].iloc[i-j] for j in range(1, SWING_LEN+1)) and \
           all(df["low"].iloc[i] <= df["low"].iloc[i+j] for j in range(1, SWING_LEN+1)):
            lows.append({"i": i, "price": df["low"].iloc[i]})
    return highs, lows

def find_ob(df, bos_i, direction):
    for i in range(bos_i - 1, max(0, bos_i - 10), -1):
        if direction == "BULL" and df["close"].iloc[i] < df["open"].iloc[i]:
            return {"top": df["high"].iloc[i], "bot": df["low"].iloc[i], "i": i}
        if direction == "BEAR" and df["close"].iloc[i] > df["open"].iloc[i]:
            return {"top": df["high"].iloc[i], "bot": df["low"].iloc[i], "i": i}
    return None

def find_fvg(df, direction):
    # FVG = gap between candle[i-2] high/low and candle[i] low/high
    for i in range(len(df)-1, max(2, len(df)-15), -1):
        if direction == "BULL":
            if df["low"].iloc[i] > df["high"].iloc[i-2]:
                return True
        if direction == "BEAR":
            if df["high"].iloc[i] < df["low"].iloc[i-2]:
                return True
    return False

def detect_choch(df, highs, lows, direction):
    # CHOCH = price breaks last swing in opposite direction after BOS
    if direction == "BULL" and len(lows) >= 2:
        if df["close"].iloc[-1] < lows[-2]["price"]:
            return True  # CHOCH detected — trend might reverse
    if direction == "BEAR" and len(highs) >= 2:
        if df["close"].iloc[-1] > highs[-2]["price"]:
            return True
    return False

def get_htf_trend(df15):
    # 15m trend using EMA 50
    if df15 is None or len(df15) < 55:
        return "NEUTRAL"
    ema50 = calc_ema(df15["close"], 50)
    last_close = df15["close"].iloc[-1]
    last_ema   = ema50.iloc[-1]
    if last_close > last_ema:
        return "BULLISH"
    elif last_close < last_ema:
        return "BEARISH"
    return "NEUTRAL"

# ─────────────────────────────────────────
# ATM / PREMIUM
# ─────────────────────────────────────────
def get_atm_strike(price, offset=0):
    base = round(price / 50) * 50
    return base + offset if offset > 0 else base

def get_option_premium(nifty_price, strike):
    diff = abs(nifty_price - strike)
    base = max(10, 150 - diff * 0.5)
    return round(base)

# ─────────────────────────────────────────
# SIGNAL STRENGTH
# ─────────────────────────────────────────
def calc_strength(htf_ok, ob_ok, fvg_ok, rsi_ok, vol_ok):
    score = sum([htf_ok, ob_ok, fvg_ok, rsi_ok, vol_ok])
    stars = "⭐" * score + "☆" * (5 - score)
    return score, stars

# ─────────────────────────────────────────
# FORMAT SIGNAL MESSAGE
# ─────────────────────────────────────────
def format_signal(signal, entry, sl, tp1, tp2, nifty_price, strength_score, strength_stars, htf_trend, fvg_ok, opt):
    now_ist    = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")
    emoji      = "🟢" if signal == "BUY" else "🔴"
    arrow      = "▲ BUY | 5m | BULLISH" if signal == "BUY" else "▼ SELL | 5m | BEARISH"
    atm_strike = get_atm_strike(nifty_price, opt["offset"])
    opt_type   = "CE 📈" if signal == "BUY" else "PE 📉"
    premium    = get_option_premium(nifty_price, atm_strike)
    opt_tp1    = premium + opt["tp1"]
    opt_tp2    = premium + opt["tp2"]
    opt_sl     = max(5, premium - opt["sl"])
    fvg_txt    = "✅ Yes" if fvg_ok else "❌ No"

    return (
        f"{emoji}<b>NIFTY--50</b>\n"
        f"{arrow}\n\n"
        f"📍 Entry : <code>{round(entry)}</code>\n"
        f"🛑 SL    : <code>{round(sl)}</code>\n"
        f"🎯 TP1   : <code>{round(tp1)}</code>\n"
        f"🏆 TP2   : <code>{round(tp2)}</code>\n\n"
        f"🎯 <b>ATM OPTION</b> ({opt['label']})\n"
        f"📌 Strike : <code>{atm_strike} {opt_type}</code>\n"
        f"💰 Premium: <code>₹{premium}</code>\n"
        f"🎯 TP1    : <code>₹{opt_tp1}</code>\n"
        f"🏆 TP2    : <code>₹{opt_tp2}</code>\n"
        f"🛑 SL     : <code>₹{opt_sl}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>HTF Trend (15m) :</b> {htf_trend}\n"
        f"🌀 <b>FVG Detected    :</b> {fvg_txt}\n"
        f"💪 <b>Signal Strength :</b> {strength_stars} ({strength_score}/5)\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💡 Book 50% at TP1, move SL to entry!\n"
        f"🕐 {now_ist}\n\n"
        f"⚠️ <i>Educational only. Trade at your own risk.</i>"
    )

# ─────────────────────────────────────────
# MAIN SCAN — MULTI TIMEFRAME
# ─────────────────────────────────────────
def scan():
    global last_signal

    # Fetch all timeframes
    df15 = get_candles("15m", "5d")
    df5  = get_candles("5m",  "2d")
    df3  = get_candles("3m",  "1d")

    if df5 is None or len(df5) < 30:
        return

    # ── HTF Trend (15m) ──
    htf_trend = get_htf_trend(df15)
    if htf_trend == "NEUTRAL":
        print("HTF Neutral — skipping")
        return

    # ── 5m Analysis ──
    highs, lows = find_swings(df5)
    if not highs or not lows:
        return

    ema20  = calc_ema(df5["close"], 20)
    ema50  = calc_ema(df5["close"], 50)
    rsi    = calc_rsi(df5["close"], 14)
    vol_ok = calc_volume_ok(df5)

    trend = "NEUTRAL"
    ob    = None
    bos_i = None

    for i in range(5, len(df5)):
        sh = [h for h in highs if h["i"] < i]
        sl = [l for l in lows  if l["i"] < i]
        if not sh or not sl:
            continue
        if df5["close"].iloc[i] > sh[-1]["price"]:
            ob    = find_ob(df5, i, "BULL")
            trend = "BULLISH"
            bos_i = i
        if df5["close"].iloc[i] < sl[-1]["price"]:
            ob    = find_ob(df5, i, "BEAR")
            trend = "BEARISH"
            bos_i = i

    curr   = df5.iloc[-1]
    curr_i = len(df5) - 1
    nifty_ltp = round(float(curr["close"]))

    rsi_val  = rsi.iloc[-1]
    ema50_val = ema50.iloc[-1]

    # ── 3m Candle Confirmation ──
    conf_3m = False
    if df3 is not None and len(df3) >= 3:
        last3 = df3.iloc[-1]
        if trend == "BULLISH" and last3["close"] > last3["open"]:
            conf_3m = True
        if trend == "BEARISH" and last3["close"] < last3["open"]:
            conf_3m = True

    opt = get_option_settings()

    # ── BUY SIGNAL ──
    if trend == "BULLISH" and htf_trend == "BULLISH" and ob:
        fvg_ok  = find_fvg(df5, "BULL")
        rsi_ok  = rsi_val > 50
        htf_ok  = True
        ob_ok   = curr["low"] <= ob["top"] and curr["low"] >= ob["bot"] - 10
        choch   = detect_choch(df5, highs, lows, "BULL")

        score, stars = calc_strength(htf_ok, ob_ok, fvg_ok, rsi_ok, vol_ok)

        if ob_ok and conf_3m and not choch and score >= 3:
            if last_signal["type"] != "BUY" or curr_i != last_signal["bar"]:
                entry = float(curr["close"])
                sl    = ob["bot"] - SL_BUFFER
                tp1   = entry + (entry - sl) * RR_TP1
                tp2   = entry + (entry - sl) * RR_TP2
                send_ready_alert("BUY")
                time.sleep(5)
                send_telegram(format_signal("BUY", entry, sl, tp1, tp2, nifty_ltp, score, stars, htf_trend, fvg_ok, opt))
                last_signal = {"type": "BUY", "bar": curr_i}
                print(f"BUY sent! Strength={score}/5")

    # ── SELL SIGNAL ──
    if trend == "BEARISH" and htf_trend == "BEARISH" and ob:
        fvg_ok  = find_fvg(df5, "BEAR")
        rsi_ok  = rsi_val < 50
        htf_ok  = True
        ob_ok   = curr["high"] >= ob["bot"] and curr["high"] <= ob["top"] + 10
        choch   = detect_choch(df5, highs, lows, "BEAR")

        score, stars = calc_strength(htf_ok, ob_ok, fvg_ok, rsi_ok, vol_ok)

        if ob_ok and conf_3m and not choch and score >= 3:
            if last_signal["type"] != "SELL" or curr_i != last_signal["bar"]:
                entry = float(curr["close"])
                sl    = ob["top"] + SL_BUFFER
                tp1   = entry - (sl - entry) * RR_TP1
                tp2   = entry - (sl - entry) * RR_TP2
                send_ready_alert("SELL")
                time.sleep(5)
                send_telegram(format_signal("SELL", entry, sl, tp1, tp2, nifty_ltp, score, stars, htf_trend, fvg_ok, opt))
                last_signal = {"type": "SELL", "bar": curr_i}
                print(f"SELL sent! Strength={score}/5")

# ─────────────────────────────────────────
# START
# ─────────────────────────────────────────
print("SMC Bot Started!")
send_telegram(
    "🤖 <b>SMC Bot Started!</b>\n"
    "📊 Multi Timeframe: 15m + 5m + 3m\n"
    "✅ EMA + RSI + OB + FVG + CHOCH + Volume\n"
    "📅 Smart Expiry Day Logic Active\n"
    "🕐 Market: 9:15 AM - 3:30 PM IST"
)

while True:
    try:
        now_ist = datetime.now(IST)
        today   = now_ist.date()

        if is_market_open():
            if market_opened_today != today:
                market_opened_today = today
                send_market_open_message()

            print(f"Scanning {now_ist.strftime('%H:%M:%S')} IST...")
            scan()
        else:
            print(f"Market closed... {now_ist.strftime('%H:%M:%S')} IST")

    except Exception as e:
        print("Error:", e)

    time.sleep(SCAN_EVERY)
        
