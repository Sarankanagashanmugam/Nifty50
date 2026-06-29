import yfinance as yf
import pandas as pd
import requests
import time
from datetime import datetime, timezone, timedelta

BOT_TOKEN  = "8429138467:AAEV3QF6VPqFys1jINIXB0Fs3hA_-Xhxnhk"
CHAT_ID    = "-1003872921226"
SYMBOL     = "^NSEI"
INTERVAL   = "5m"
SCAN_EVERY = 60
SWING_LEN  = 5
RR_TP1     = 1.5
RR_TP2     = 3.0
SL_BUFFER  = 15

# Option Settings
OPT_TP1 = 25
OPT_TP2 = 50
OPT_SL  = 25

last_signal        = {"type": None, "bar": 0}
market_opened_today = None  # ✅ Tracks if 9:15 AM message sent today

# IST Timezone
IST = timezone(timedelta(hours=5, minutes=30))

def is_market_open():
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:
        return False
    market_open  = now_ist.replace(hour=9,  minute=15, second=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0)
    return market_open <= now_ist <= market_close

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("Sent:", r.status_code)
    except Exception as e:
        print("Error:", e)

# ✅ Daily 9:15 AM Market Open Message
def send_market_open_message():
    now_ist = datetime.now(IST).strftime("%d %b %Y")
    send_telegram(
        "🔔 <b>Market is Now Open!</b>\n\n"
        f"📅 {now_ist}\n"
        "📊 Scanning NIFTY 50 for SMC Signals\n"
        "⏱️ Timeframe: 5 Minutes\n"
        "🕐 9:15 AM - 3:30 PM IST\n"
        "✅ Bot is Active — Signals will fire soon!\n\n"
        "⚠️ <i>Educational only. Trade at your own risk.</i>"
    )
    print("Market Open message sent!")

# ✅ Ready Alert (sent before main signal)
def send_ready_alert(direction):
    if direction == "BUY":
        msg = (
            "⚡️ <b>SIGNAL LOADING...</b>\n\n"
            "🟢 Get Ready for <b>CE</b> 📈\n"
            "⏳ Confirming entry... wait for signal!"
        )
    else:
        msg = (
            "⚡️ <b>SIGNAL LOADING...</b>\n\n"
            "🔴 Get Ready for <b>PE</b> 📉\n"
            "⏳ Confirming entry... wait for signal!"
        )
    send_telegram(msg)

def get_candles():
    try:
        df = yf.download(SYMBOL, period="2d", interval=INTERVAL, progress=False)
        df.dropna(inplace=True)
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        return df.reset_index()
    except Exception as e:
        print("Data error:", e)
        return None

def get_atm_strike(price):
    return round(price / 50) * 50

def get_option_premium(nifty_price, strike):
    diff = abs(nifty_price - strike)
    base_premium = max(30, 150 - diff * 0.5)
    return round(base_premium)

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
    for i in range(bos_i - 1, max(0, bos_i - 8), -1):
        if direction == "BULL" and df["close"].iloc[i] < df["open"].iloc[i]:
            return {"top": df["high"].iloc[i], "bot": df["low"].iloc[i]}
        if direction == "BEAR" and df["close"].iloc[i] > df["open"].iloc[i]:
            return {"top": df["high"].iloc[i], "bot": df["low"].iloc[i]}
    return None

def format_signal(signal, entry, sl, tp1, tp2, nifty_price):
    now_ist    = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")
    emoji      = "🟢" if signal == "BUY" else "🔴"
    arrow      = "▲ BUY | 5m | BULLISH" if signal == "BUY" else "▼ SELL | 5m | BEARISH"
    atm_strike = get_atm_strike(nifty_price)
    opt_type   = "CE 📈" if signal == "BUY" else "PE 📉"
    premium    = get_option_premium(nifty_price, atm_strike)
    opt_tp1    = premium + OPT_TP1
    opt_tp2    = premium + OPT_TP2
    opt_sl     = premium - OPT_SL

    return (
        f"{emoji}<b>NIFTY--50</b>\n"
        f"{arrow}\n\n"
        f"📍 Entry : <code>{round(entry)}</code>\n"
        f"🛑 SL    : <code>{round(sl)}</code>\n"
        f"🎯 TP1   : <code>{round(tp1)}</code>\n"
        f"🏆 TP2   : <code>{round(tp2)}</code>\n\n"
        f"🎯 <b>ATM OPTION</b>\n"
        f"📌 Strike : <code>{atm_strike} {opt_type}</code>\n"
        f"💰 Premium: <code>₹{premium}</code>\n"
        f"🎯 TP1    : <code>₹{opt_tp1}</code>\n"
        f"🏆 TP2    : <code>₹{opt_tp2}</code>\n"
        f"🛑 SL     : <code>₹{opt_sl}</code>\n\n"
        f"💡 Book 50% at TP1, move SL to entry!\n"
        f"🕐 {now_ist}\n\n"
        f"⚠️ <i>Educational only. Trade at your own risk.</i>"
    )

def scan():
    global last_signal
    df = get_candles()
    if df is None or len(df) < 20:
        return

    highs, lows = find_swings(df)
    if not highs or not lows:
        return

    trend = "NEUTRAL"
    ob    = None

    for i in range(5, len(df)):
        sh = [h for h in highs if h["i"] < i]
        sl = [l for l in lows  if l["i"] < i]
        if not sh or not sl:
            continue
        if df["close"].iloc[i] > sh[-1]["price"]:
            ob    = find_ob(df, i, "BULL")
            trend = "BULLISH"
        if df["close"].iloc[i] < sl[-1]["price"]:
            ob    = find_ob(df, i, "BEAR")
            trend = "BEARISH"

    curr      = df.iloc[-1]
    curr_i    = len(df) - 1
    nifty_ltp = round(float(curr["close"]))

    if trend == "BULLISH" and ob:
        if curr["low"] <= ob["top"] and curr["low"] >= ob["bot"] - 10:
            if last_signal["type"] != "BUY" or curr_i != last_signal["bar"]:
                entry = curr["close"]
                sl    = ob["bot"] - SL_BUFFER
                tp1   = entry + (entry - sl) * RR_TP1
                tp2   = entry + (entry - sl) * RR_TP2
                send_ready_alert("BUY")
                time.sleep(5)
                send_telegram(format_signal("BUY", entry, sl, tp1, tp2, nifty_ltp))
                last_signal = {"type": "BUY", "bar": curr_i}
                print("BUY sent!")

    if trend == "BEARISH" and ob:
        if curr["high"] >= ob["bot"] and curr["high"] <= ob["top"] + 10:
            if last_signal["type"] != "SELL" or curr_i != last_signal["bar"]:
                entry = curr["close"]
                sl    = ob["top"] + SL_BUFFER
                tp1   = entry - (sl - entry) * RR_TP1
                tp2   = entry - (sl - entry) * RR_TP2
                send_ready_alert("SELL")
                time.sleep(5)
                send_telegram(format_signal("SELL", entry, sl, tp1, tp2, nifty_ltp))
                last_signal = {"type": "SELL", "bar": curr_i}
                print("SELL sent!")

print("SMC Bot Started!")
send_telegram(
    "🤖 <b>SMC Bot Started!</b>\n"
    "📊 Scanning NIFTY 50 every 60 seconds\n"
    "⏱️ Timeframe: 5 Minutes\n"
    "✅ ATM Options activated!\n"
    "🕐 Market: 9:15 AM - 3:30 PM IST"
)

while True:
    try:
        now_ist = datetime.now(IST)
        today   = now_ist.date()

        if is_market_open():
            # ✅ Send 9:15 AM message once per day
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
