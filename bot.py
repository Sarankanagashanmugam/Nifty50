import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime, timezone, timedelta
from dhanhq import DhanContext, dhanhq

# ─────────────────────────────────────────
# CONFIG — UPDATE THESE
# ─────────────────────────────────────────
BOT_TOKEN  = "8429138467:AAFi-Ee71HHLEh_dupW0gms0baS91VGDufY"
CHAT_ID    = "-1003872921226"

DHAN_CLIENT_ID    = "1112186743"
DHAN_ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzgyOTEwODY5LCJpYXQiOjE3ODI4MjQ0NjksInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTEyMTg2NzQzIn0.UgiKMTRIMIu2rhvMhbBCtf-KMTxnV_HB8YK878Fsyj-W0LCvSUK-BNh3Qbh28lDToXqgRpiKbKsKC-wuiaO5iA"

# Nifty 50 Index on Dhan
NIFTY_SECURITY_ID = "13"
NIFTY_EXCHANGE_SEG = "IDX_I"
NIFTY_INSTRUMENT   = "INDEX"

SCAN_EVERY = 60
SWING_LEN  = 5
SL_BUFFER  = 15
RR_TP1     = 1.5
RR_TP2     = 3.0

last_signal          = {"type": None, "bar": 0}
market_opened_today  = None
ob_history            = {"BULL": [], "BEAR": []}   # tracks OBs for mitigation check

IST = timezone(timedelta(hours=5, minutes=30))

dhan_context = DhanContext(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
dhan = dhanhq(dhan_context)

# ─────────────────────────────────────────
# EXPIRY / DAY BASED OPTION SETTINGS
# ─────────────────────────────────────────
def get_option_settings():
    day = datetime.now(IST).weekday()
    if day == 3:
        return {"offset": 100, "tp1": 10, "tp2": 20, "sl": 10, "label": "OTM (Expiry Day)"}
    elif day == 2:
        return {"offset": 0, "tp1": 25, "tp2": 45, "sl": 20, "label": "ATM"}
    else:
        return {"offset": 0, "tp1": 30, "tp2": 60, "sl": 25, "label": "ATM"}

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
        "📊 Live Data via Dhan API\n"
        "⏱️ 15m + 5m + 3m Multi Timeframe\n"
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
# DHAN LIVE DATA FETCH
# ─────────────────────────────────────────
def get_candles_dhan(interval="5"):
    """
    interval: '1', '5', '15', '25', '60' (minutes) — as per Dhan API
    Returns a clean OHLCV dataframe with lowercase columns.
    """
    try:
        now = datetime.now(IST)
        from_date = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        to_date   = now.strftime("%Y-%m-%d %H:%M:%S")

        response = dhan.intraday_minute_data(
            security_id=NIFTY_SECURITY_ID,
            exchange_segment=NIFTY_EXCHANGE_SEG,
            instrument_type=NIFTY_INSTRUMENT,
            from_date=from_date,
            to_date=to_date,
            interval=interval
        )

        if not response or "data" not in response:
            print(f"Dhan API: no data for interval {interval}")
            return None

        data = response["data"]
        df = pd.DataFrame({
            "timestamp": data.get("timestamp", []),
            "open":      data.get("open", []),
            "high":      data.get("high", []),
            "low":       data.get("low", []),
            "close":     data.get("close", []),
            "volume":    data.get("volume", []),
        })

        if df.empty:
            return None

        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s") + timedelta(hours=5, minutes=30)
        df.dropna(inplace=True)
        return df.reset_index(drop=True)

    except Exception as e:
        print(f"Dhan data error ({interval}m):", e)
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
    if "volume" not in df.columns or df["volume"].sum() == 0:
        return True  # index data often has no volume — don't block on it
    avg_vol = df["volume"].rolling(20).mean().iloc[-1]
    cur_vol = df["volume"].iloc[-1]
    if pd.isna(avg_vol) or avg_vol == 0:
        return True
    return cur_vol > avg_vol * 0.8

# ─────────────────────────────────────────
# SMC — SWINGS, OB (WITH MITIGATION), FVG (WITH FILL CHECK)
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
    """Find the OB candle right before the BOS break."""
    for i in range(bos_i - 1, max(0, bos_i - 10), -1):
        if direction == "BULL" and df["close"].iloc[i] < df["open"].iloc[i]:
            return {"top": df["high"].iloc[i], "bot": df["low"].iloc[i], "i": i}
        if direction == "BEAR" and df["close"].iloc[i] > df["open"].iloc[i]:
            return {"top": df["high"].iloc[i], "bot": df["low"].iloc[i], "i": i}
    return None

def is_ob_valid(df, ob, direction):
    """
    ✅ OB MITIGATION / INVALIDATION CHECK
    An OB is invalid if price has already CLOSED beyond it after it formed
    (fully mitigated), meaning the zone has been consumed.
    """
    if ob is None:
        return False

    candles_after = df.iloc[ob["i"]+1:]
    if candles_after.empty:
        return True  # too fresh, nothing has happened yet — still valid

    if direction == "BULL":
        # Invalid if any candle CLOSED below the OB bottom (fully broken through)
        if (candles_after["close"] < ob["bot"]).any():
            return False
    else:
        # Invalid if any candle CLOSED above the OB top (fully broken through)
        if (candles_after["close"] > ob["top"]).any():
            return False

    return True

def find_fvg(df, direction, min_gap_points=5):
    """
    ✅ FVG WITH GAP SIZE + FILL CHECK
    Returns True only if there is a FRESH (unfilled) FVG of meaningful size.
    """
    n = len(df)
    for i in range(n - 1, max(2, n - 15), -1):
        if direction == "BULL":
            gap_bottom = df["high"].iloc[i-2]
            gap_top    = df["low"].iloc[i]
            gap_size   = gap_top - gap_bottom
            if gap_size >= min_gap_points:
                # Check if filled: any candle AFTER the gap closed back into/through it
                later = df.iloc[i+1:]
                filled = (later["low"] <= gap_bottom).any() if not later.empty else False
                if not filled:
                    return True
        else:
            gap_top    = df["low"].iloc[i-2]
            gap_bottom = df["high"].iloc[i]
            gap_size   = gap_top - gap_bottom
            if gap_size >= min_gap_points:
                later = df.iloc[i+1:]
                filled = (later["high"] >= gap_top).any() if not later.empty else False
                if not filled:
                    return True
    return False

def detect_choch(df, highs, lows, direction):
    if direction == "BULL" and len(lows) >= 2:
        if df["close"].iloc[-1] < lows[-2]["price"]:
            return True
    if direction == "BEAR" and len(highs) >= 2:
        if df["close"].iloc[-1] > highs[-2]["price"]:
            return True
    return False

def get_htf_trend(df15):
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
    fvg_txt    = "✅ Fresh" if fvg_ok else "❌ No"

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
        f"🌀 <b>FVG (fresh)     :</b> {fvg_txt}\n"
        f"💪 <b>Signal Strength :</b> {strength_stars} ({strength_score}/5)\n"
        f"📡 <b>Data Source     :</b> Dhan Live API\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💡 Book 50% at TP1, move SL to entry!\n"
        f"🕐 {now_ist}\n\n"
        f"⚠️ <i>Educational only. Trade at your own risk.</i>"
    )

# ─────────────────────────────────────────
# MAIN SCAN — MULTI TIMEFRAME + LIVE DATA + OB/FVG FIXES
# ─────────────────────────────────────────
def scan():
    global last_signal

    df15 = get_candles_dhan("15")
    df5  = get_candles_dhan("5")
    df3  = get_candles_dhan("3") if False else get_candles_dhan("1")  # Dhan supports 1/5/15/25/60 — using 1m as fast-confirm TF

    if df5 is None or len(df5) < 30:
        print("Not enough 5m data yet")
        return

    htf_trend = get_htf_trend(df15)
    if htf_trend == "NEUTRAL":
        print("HTF Neutral — skipping")
        return

    highs, lows = find_swings(df5)
    if not highs or not lows:
        return

    rsi    = calc_rsi(df5["close"], 14)
    vol_ok = calc_volume_ok(df5)

    trend = "NEUTRAL"
    ob    = None

    for i in range(5, len(df5)):
        sh = [h for h in highs if h["i"] < i]
        sl_ = [l for l in lows if l["i"] < i]
        if not sh or not sl_:
            continue
        if df5["close"].iloc[i] > sh[-1]["price"]:
            candidate_ob = find_ob(df5, i, "BULL")
            if candidate_ob and is_ob_valid(df5, candidate_ob, "BULL"):
                ob    = candidate_ob
                trend = "BULLISH"
        if df5["close"].iloc[i] < sl_[-1]["price"]:
            candidate_ob = find_ob(df5, i, "BEAR")
            if candidate_ob and is_ob_valid(df5, candidate_ob, "BEAR"):
                ob    = candidate_ob
                trend = "BEARISH"

    if ob is None:
        print("No valid (unmitigated) OB found")
        return

    curr   = df5.iloc[-1]
    curr_i = len(df5) - 1
    nifty_ltp = round(float(curr["close"]))
    rsi_val   = rsi.iloc[-1]

    conf_fast = False
    if df3 is not None and len(df3) >= 3:
        last_fast = df3.iloc[-1]
        if trend == "BULLISH" and last_fast["close"] > last_fast["open"]:
            conf_fast = True
        if trend == "BEARISH" and last_fast["close"] < last_fast["open"]:
            conf_fast = True

    opt = get_option_settings()

    if trend == "BULLISH" and htf_trend == "BULLISH":
        fvg_ok = find_fvg(df5, "BULL")
        rsi_ok = rsi_val > 50
        ob_ok  = curr["low"] <= ob["top"] and curr["low"] >= ob["bot"] - 10
        choch  = detect_choch(df5, highs, lows, "BULL")

        score, stars = calc_strength(True, ob_ok, fvg_ok, rsi_ok, vol_ok)

        if ob_ok and conf_fast and not choch and score >= 3:
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

    if trend == "BEARISH" and htf_trend == "BEARISH":
        fvg_ok = find_fvg(df5, "BEAR")
        rsi_ok = rsi_val < 50
        ob_ok  = curr["high"] >= ob["bot"] and curr["high"] <= ob["top"] + 10
        choch  = detect_choch(df5, highs, lows, "BEAR")

        score, stars = calc_strength(True, ob_ok, fvg_ok, rsi_ok, vol_ok)

        if ob_ok and conf_fast and not choch and score >= 3:
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
print("SMC Bot Started (Dhan Live API)!")
send_telegram(
    "🤖 <b>SMC Bot Started!</b>\n"
    "📡 Live Data via Dhan API\n"
    "📊 Multi Timeframe: 15m + 5m + 1m\n"
    "✅ EMA + RSI + OB(mitigation) + FVG(fill-check) + CHOCH\n"
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
