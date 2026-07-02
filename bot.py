import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime, timezone, timedelta
from dhanhq import DhanContext, dhanhq

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
BOT_TOKEN  = "8429138467:AAEV3QF6VPqFys1jINIXB0Fs3hA_-Xhxnhk"
CHAT_ID    = "-1003872921226"

DHAN_CLIENT_ID    = "1112186743"
DHAN_ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzgyOTk2OTQ4LCJpYXQiOjE3ODI5MTA1NDgsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTEyMTg2NzQzIn0.pa3IK_c4W6JC1XJyuD_R2BvZxfWZn0MZ66FWY9HUo9I7C_ie2EPd6gfVFXJ2cAJiEeYr0VSzy4_TBdoOIT-oqw"

NIFTY_SECURITY_ID  = "13"
NIFTY_EXCHANGE_SEG = "IDX_I"
NIFTY_INSTRUMENT   = "INDEX"

SCAN_EVERY   = 60
SWING_LEN    = 5
SL_BUFFER    = 15
RR_TP1       = 1.5
RR_TP2       = 3.0
COOLDOWN_MIN = 15
MAX_SIGNALS  = 6
PREMIUM_MIN  = 100
PREMIUM_MAX  = 200

IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
last_signal         = {"type": None, "bar": 0, "time": None}
market_opened_today = None
daily_signals       = 0
daily_date          = None
active_trade        = None
expiry_cache        = {"expiry": None, "fetched": None}

dhan_context = DhanContext(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
dhan         = dhanhq(dhan_context)

# ─────────────────────────────────────────
# MARKET OPEN
# ─────────────────────────────────────────
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t

# ─────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────
def send_telegram(message):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("Telegram sent:", r.status_code)
    except Exception as e:
        print("Telegram Error:", e)

def send_market_open_message():
    now_ist = datetime.now(IST).strftime("%d %b %Y")
    day     = datetime.now(IST).weekday()
    expiry  = "⚠️ <b>EXPIRY DAY</b> — Extra caution!\n" if day == 3 else ""
    send_telegram(
        "🔔 <b>Market is Now Open!</b>\n\n"
        f"📅 {now_ist}\n"
        f"{expiry}"
        "📡 Live Data: Dhan API\n"
        "⏱ Multi TF: 15m + 5m + 1m\n"
        "💰 Premium Filter: ₹100 - ₹200\n"
        f"📊 Max Signals Today: {MAX_SIGNALS}\n"
        "🕐 9:15 AM - 3:30 PM IST\n\n"
        "⚠️ <i>Educational only. Trade at your own risk.</i>"
    )
    print("Market Open message sent!")

def send_ready_alert(direction):
    msg = (
        "⚡️ <b>SIGNAL LOADING...</b>\n\n"
        f"{'🟢 Get Ready for <b>CE</b> 📈' if direction == 'BUY' else '🔴 Get Ready for <b>PE</b> 📉'}\n"
        "⏳ Candle closed — signal incoming!"
    )
    send_telegram(msg)

# ─────────────────────────────────────────
# DHAN CANDLE DATA — Direct REST API call
# ✅ Bypasses dhanhq library parameter issues
# ✅ Uses correct Dhan v2 endpoint directly
# ─────────────────────────────────────────
def get_candles_dhan(interval=5):
    try:
        now       = datetime.now(IST)
        from_date = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        to_date   = now.strftime("%Y-%m-%d %H:%M:%S")

        url     = "https://api.dhan.co/v2/charts/intraday"
        headers = {
            "Content-Type":  "application/json",
            "access-token":  DHAN_ACCESS_TOKEN,
            "client-id":     DHAN_CLIENT_ID
        }
        payload = {
            "securityId":      NIFTY_SECURITY_ID,
            "exchangeSegment": NIFTY_EXCHANGE_SEG,
            "instrument":      NIFTY_INSTRUMENT,
            "interval":        str(interval),   # ✅ Dhan v2 expects string
            "oi":              False,
            "fromDate":        from_date,
            "toDate":          to_date
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=10)

        if resp.status_code != 200:
            print(f"Dhan ({interval}m): HTTP {resp.status_code} → {resp.text[:120]}")
            return None

        data = resp.json()

        if not isinstance(data, dict):
            print(f"Dhan ({interval}m): response not dict → {str(data)[:120]}")
            return None

        timestamps = data.get("timestamp", [])
        if not timestamps:
            print(f"Dhan ({interval}m): empty data → {str(data)[:120]}")
            return None

        df = pd.DataFrame({
            "timestamp": timestamps,
            "open":      data.get("open",   []),
            "high":      data.get("high",   []),
            "low":       data.get("low",    []),
            "close":     data.get("close",  []),
            "volume":    data.get("volume", []),
        })

        if df.empty:
            print(f"Dhan ({interval}m): empty dataframe")
            return None

        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s") \
                          + timedelta(hours=5, minutes=30)
        df.dropna(inplace=True)
        df = df.reset_index(drop=True)
        print(f"Dhan ({interval}m): {len(df)} candles ✅")
        return df

    except Exception as e:
        print(f"Dhan data error ({interval}m):", e)
        return None

# ─────────────────────────────────────────
# CANDLE CLOSE CONFIRMATION
# BUG FIX 2: timezone-aware comparison fixed
# ─────────────────────────────────────────
def is_candle_closed(df, interval_min=5):
    if df is None or len(df) < 2:
        return False
    last_ts = df["timestamp"].iloc[-1]
    # ✅ Make both timezone-naive for safe comparison
    if hasattr(last_ts, 'tzinfo') and last_ts.tzinfo is not None:
        last_ts = last_ts.replace(tzinfo=None)
    now_naive = datetime.now(IST).replace(tzinfo=None)
    elapsed = (now_naive - last_ts).total_seconds() / 60
    return elapsed >= interval_min

# ─────────────────────────────────────────
# DHAN OPTION CHAIN — real premium
# BUG FIX 3: isinstance check on expiry resp
# ─────────────────────────────────────────
def get_nearest_expiry():
    now = datetime.now(IST)
    if expiry_cache["expiry"] and expiry_cache["fetched"] and \
       (now - expiry_cache["fetched"]).total_seconds() < 3600:
        return expiry_cache["expiry"]
    try:
        resp = dhan.expiry_list(
            under_security_id      = int(NIFTY_SECURITY_ID),
            under_exchange_segment = NIFTY_EXCHANGE_SEG
        )
        if not isinstance(resp, dict):
            print("Expiry list: invalid response →", str(resp)[:100])
            return None
        expiries = resp.get("data", [])
        if expiries:
            expiry_cache["expiry"]  = expiries[0]
            expiry_cache["fetched"] = now
            print(f"Nearest expiry: {expiries[0]}")
            return expiries[0]
        print("Expiry list: empty data")
    except Exception as e:
        print("Expiry list error:", e)
    return None

def fetch_option_chain(expiry):
    try:
        resp = dhan.option_chain(
            under_security_id      = int(NIFTY_SECURITY_ID),
            under_exchange_segment = NIFTY_EXCHANGE_SEG,
            expiry                 = expiry
        )
        if not isinstance(resp, dict):
            print("Option chain: invalid response →", str(resp)[:100])
            return {}
        return resp.get("data", {}).get("oc", {})
    except Exception as e:
        print("Option chain fetch error:", e)
        return {}

def find_best_strike(nifty_price, signal_type):
    expiry = get_nearest_expiry()
    if not expiry:
        return None, None

    oc = fetch_option_chain(expiry)
    if not oc:
        print("Empty option chain")
        return None, None

    time.sleep(3)  # Dhan OC rate limit

    atm        = round(nifty_price / 50) * 50
    leg        = "ce" if signal_type == "BUY" else "pe"
    candidates = []

    for offset in [0, -50, 50, -100, 100, -150, 150, -200, 200]:
        strike     = atm + offset
        strike_key = None
        for k in oc.keys():
            try:
                if abs(float(k) - strike) < 1:
                    strike_key = k
                    break
            except ValueError:
                continue
        if not strike_key:
            continue
        leg_data = oc[strike_key].get(leg, {})
        # ✅ BUG FIX 4: leg_data could be None — added None check
        if not leg_data or not isinstance(leg_data, dict):
            continue
        ltp = leg_data.get("last_price")
        if ltp and PREMIUM_MIN <= float(ltp) <= PREMIUM_MAX:
            candidates.append((strike, round(float(ltp), 2)))

    if not candidates:
        print(f"No strike in ₹{PREMIUM_MIN}-₹{PREMIUM_MAX} range")
        return None, None

    best = min(candidates, key=lambda x: abs(x[0] - atm))
    print(f"Best strike: {best[0]} {leg.upper()} @ ₹{best[1]}")
    return best

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
        return True
    avg = df["volume"].rolling(20).mean().iloc[-1]
    cur = df["volume"].iloc[-1]
    return True if pd.isna(avg) or avg == 0 else bool(cur > avg * 0.8)

# ─────────────────────────────────────────
# SMC
# ─────────────────────────────────────────
def find_swings(df):
    highs, lows = [], []
    for i in range(SWING_LEN, len(df) - SWING_LEN):
        if all(df["high"].iloc[i] >= df["high"].iloc[i-j] for j in range(1, SWING_LEN+1)) and \
           all(df["high"].iloc[i] >= df["high"].iloc[i+j] for j in range(1, SWING_LEN+1)):
            highs.append({"i": i, "price": float(df["high"].iloc[i])})
        if all(df["low"].iloc[i] <= df["low"].iloc[i-j] for j in range(1, SWING_LEN+1)) and \
           all(df["low"].iloc[i] <= df["low"].iloc[i+j] for j in range(1, SWING_LEN+1)):
            lows.append({"i": i, "price": float(df["low"].iloc[i])})
    return highs, lows

def find_ob(df, bos_i, direction):
    for i in range(bos_i - 1, max(0, bos_i - 10), -1):
        if direction == "BULL" and df["close"].iloc[i] < df["open"].iloc[i]:
            return {"top": float(df["high"].iloc[i]), "bot": float(df["low"].iloc[i]), "i": i}
        if direction == "BEAR" and df["close"].iloc[i] > df["open"].iloc[i]:
            return {"top": float(df["high"].iloc[i]), "bot": float(df["low"].iloc[i]), "i": i}
    return None

def is_ob_valid(df, ob, direction):
    if ob is None:
        return False
    after = df.iloc[ob["i"]+1:]
    if after.empty:
        return True
    if direction == "BULL" and (after["close"] < ob["bot"]).any():
        return False
    if direction == "BEAR" and (after["close"] > ob["top"]).any():
        return False
    return True

def find_fvg(df, direction, min_gap=5):
    n = len(df)
    for i in range(n - 1, max(2, n - 15), -1):
        if direction == "BULL":
            gap = float(df["low"].iloc[i]) - float(df["high"].iloc[i-2])
            if gap >= min_gap:
                later = df.iloc[i+1:]
                if later.empty or not (later["low"] <= df["high"].iloc[i-2]).any():
                    return True
        else:
            gap = float(df["low"].iloc[i-2]) - float(df["high"].iloc[i])
            if gap >= min_gap:
                later = df.iloc[i+1:]
                if later.empty or not (later["high"] >= df["low"].iloc[i-2]).any():
                    return True
    return False

def detect_choch(df, highs, lows, direction):
    if direction == "BULL" and len(lows) >= 2:
        return float(df["close"].iloc[-1]) < lows[-2]["price"]
    if direction == "BEAR" and len(highs) >= 2:
        return float(df["close"].iloc[-1]) > highs[-2]["price"]
    return False

def get_htf_trend(df15):
    if df15 is None or len(df15) < 55:
        return "NEUTRAL"
    ema50 = calc_ema(df15["close"], 50)
    lc = float(df15["close"].iloc[-1])
    le = float(ema50.iloc[-1])
    if lc > le: return "BULLISH"
    if lc < le: return "BEARISH"
    return "NEUTRAL"

def calc_strength(htf_ok, ob_ok, fvg_ok, rsi_ok, vol_ok):
    score = sum([htf_ok, ob_ok, fvg_ok, rsi_ok, vol_ok])
    return score, "⭐" * score + "☆" * (5 - score)

# ─────────────────────────────────────────
# OPTION TARGETS — delta based
# ─────────────────────────────────────────
def calc_option_targets(premium, nifty_entry, nifty_sl, nifty_tp1, nifty_tp2, signal):
    delta = 0.5
    if signal == "BUY":
        tp1_pts = nifty_tp1 - nifty_entry
        tp2_pts = nifty_tp2 - nifty_entry
        sl_pts  = nifty_entry - nifty_sl
    else:
        tp1_pts = nifty_entry - nifty_tp1
        tp2_pts = nifty_entry - nifty_tp2
        sl_pts  = nifty_sl - nifty_entry
    opt_tp1 = round(premium + tp1_pts * delta)
    opt_tp2 = round(premium + tp2_pts * delta)
    opt_sl  = round(max(5, premium - sl_pts * delta))
    return opt_tp1, opt_tp2, opt_sl

# ─────────────────────────────────────────
# FORMAT SIGNAL
# ─────────────────────────────────────────
def format_signal(signal, entry, sl, tp1, tp2, strike, premium,
                  opt_tp1, opt_tp2, opt_sl, score, stars,
                  htf_trend, fvg_ok, signals_today):
    now_ist  = datetime.now(IST).strftime("%d %b %Y  %H:%M IST")
    emoji    = "🟢" if signal == "BUY" else "🔴"
    arrow    = "▲ BUY | 5m | BULLISH" if signal == "BUY" else "▼ SELL | 5m | BEARISH"
    opt_type = "CE 📈" if signal == "BUY" else "PE 📉"
    fvg_txt  = "✅ Fresh" if fvg_ok else "❌ No"

    return (
        f"{emoji}<b>NIFTY 50</b>\n"
        f"{arrow}\n\n"
        f"📍 Entry : <code>{round(entry)}</code>\n"
        f"🛑 SL    : <code>{round(sl)}</code>\n"
        f"🎯 TP1   : <code>{round(tp1)}</code>\n"
        f"🏆 TP2   : <code>{round(tp2)}</code>\n\n"
        f"💰 <b>OPTION</b> (Real Premium)\n"
        f"📌 Strike  : <code>{strike} {opt_type}</code>\n"
        f"💵 Premium : <code>₹{premium}</code>\n"
        f"🎯 TP1     : <code>₹{opt_tp1}</code>\n"
        f"🏆 TP2     : <code>₹{opt_tp2}</code>\n"
        f"🛑 SL      : <code>₹{opt_sl}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📊 HTF (15m)  : {htf_trend}\n"
        f"🌀 FVG        : {fvg_txt}\n"
        f"💪 Strength   : {stars} ({score}/5)\n"
        f"📈 Signal #{signals_today} of {MAX_SIGNALS} today\n"
        f"📡 Data       : Dhan Live API\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💡 Book 50% at TP1 → move SL to entry!\n"
        f"🕐 {now_ist}\n\n"
        f"⚠️ <i>Educational only. Trade at your own risk.</i>"
    )

# ─────────────────────────────────────────
# TRADE MONITOR — TP/SL/Breakeven alerts
# ─────────────────────────────────────────
def monitor_active_trade(df5):
    global active_trade
    if not active_trade:
        return
    curr    = df5.iloc[-1]
    price   = float(curr["close"])
    signal  = active_trade["signal"]
    entry   = active_trade["entry"]
    sl      = active_trade["sl"]
    tp1     = active_trade["tp1"]
    tp2     = active_trade["tp2"]
    tp1_hit = active_trade.get("tp1_hit", False)
    be_done = active_trade.get("be_moved", False)

    if signal == "BUY":
        if not tp1_hit and price >= tp1:
            active_trade["tp1_hit"] = True
            send_telegram(f"🎯 <b>TP1 HIT!</b>\n\n✅ Nifty: <code>{round(tp1)}</code>\n💡 Book 50% now!\n🔄 Move SL to entry <code>{round(entry)}</code>")
        elif tp1_hit and not be_done:
            active_trade["be_moved"] = True
            send_telegram(f"🔄 <b>BREAKEVEN</b>\n\nMove SL → <code>{round(entry)}</code>\n🏆 Target TP2: <code>{round(tp2)}</code>")
        if tp1_hit and price >= tp2:
            send_telegram(f"🏆 <b>TP2 HIT! FULL TARGET!</b>\n\n✅ Nifty: <code>{round(tp2)}</code>\n🎉 Exit full position!")
            active_trade = None
        elif not tp1_hit and price <= sl:
            send_telegram(f"🛑 <b>SL HIT</b>\n\n❌ Nifty: <code>{round(sl)}</code>\n📉 Exit. Wait for next signal.")
            active_trade = None
    else:  # SELL
        if not tp1_hit and price <= tp1:
            active_trade["tp1_hit"] = True
            send_telegram(f"🎯 <b>TP1 HIT!</b>\n\n✅ Nifty: <code>{round(tp1)}</code>\n💡 Book 50% now!\n🔄 Move SL to entry <code>{round(entry)}</code>")
        elif tp1_hit and not be_done:
            active_trade["be_moved"] = True
            send_telegram(f"🔄 <b>BREAKEVEN</b>\n\nMove SL → <code>{round(entry)}</code>\n🏆 Target TP2: <code>{round(tp2)}</code>")
        if tp1_hit and price <= tp2:
            send_telegram(f"🏆 <b>TP2 HIT! FULL TARGET!</b>\n\n✅ Nifty: <code>{round(tp2)}</code>\n🎉 Exit full position!")
            active_trade = None
        elif not tp1_hit and price >= sl:
            send_telegram(f"🛑 <b>SL HIT</b>\n\n❌ Nifty: <code>{round(sl)}</code>\n📉 Exit. Wait for next signal.")
            active_trade = None

# ─────────────────────────────────────────
# RISK CONTROLS
# ─────────────────────────────────────────
def reset_daily_if_needed():
    global daily_signals, daily_date, active_trade
    today = datetime.now(IST).date()
    if daily_date != today:
        daily_signals = 0
        daily_date    = today
        active_trade  = None
        print(f"Daily reset: {today}")

def cooldown_ok():
    if not last_signal["time"]:
        return True
    elapsed = (datetime.now(IST) - last_signal["time"]).total_seconds() / 60
    if elapsed < COOLDOWN_MIN:
        print(f"Cooldown: {round(COOLDOWN_MIN - elapsed)} min left")
        return False
    return True

def signals_ok():
    if daily_signals >= MAX_SIGNALS:
        print(f"Max {MAX_SIGNALS} signals reached today")
        return False
    return True

# ─────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────
def scan():
    global last_signal, daily_signals, active_trade

    reset_daily_if_needed()

    df15 = get_candles_dhan(15)
    df5  = get_candles_dhan(5)
    df1  = get_candles_dhan(1)

    if df5 is not None and active_trade:
        monitor_active_trade(df5)

    if df5 is None or len(df5) < 30:
        print("Not enough 5m data")
        return

    if not is_candle_closed(df5, interval_min=5):
        print("5m candle not closed yet — waiting")
        return

    if not signals_ok() or not cooldown_ok():
        return

    htf_trend = get_htf_trend(df15)
    if htf_trend == "NEUTRAL":
        print("HTF Neutral — skipping")
        return

    highs, lows = find_swings(df5)
    if not highs or not lows:
        print("No swings found")
        return

    rsi    = calc_rsi(df5["close"], 14)
    vol_ok = calc_volume_ok(df5)
    trend  = "NEUTRAL"
    ob     = None

    for i in range(5, len(df5)):
        sh  = [h for h in highs if h["i"] < i]
        sl_ = [l for l in lows  if l["i"] < i]
        if not sh or not sl_:
            continue
        if float(df5["close"].iloc[i]) > sh[-1]["price"]:
            cob = find_ob(df5, i, "BULL")
            if cob and is_ob_valid(df5, cob, "BULL"):
                ob, trend = cob, "BULLISH"
        if float(df5["close"].iloc[i]) < sl_[-1]["price"]:
            cob = find_ob(df5, i, "BEAR")
            if cob and is_ob_valid(df5, cob, "BEAR"):
                ob, trend = cob, "BEARISH"

    if ob is None:
        print("No valid OB found")
        return

    curr      = df5.iloc[-1]
    curr_i    = len(df5) - 1
    nifty_ltp = round(float(curr["close"]))
    rsi_val   = float(rsi.iloc[-1])

    conf_fast = False
    if df1 is not None and len(df1) >= 3:
        lf = df1.iloc[-1]
        if trend == "BULLISH" and float(lf["close"]) > float(lf["open"]): conf_fast = True
        if trend == "BEARISH" and float(lf["close"]) < float(lf["open"]): conf_fast = True

    if trend == "BULLISH" and htf_trend == "BULLISH":
        fvg_ok = find_fvg(df5, "BULL")
        rsi_ok = rsi_val > 50
        ob_ok  = float(curr["low"]) <= ob["top"] and float(curr["low"]) >= ob["bot"] - 10
        choch  = detect_choch(df5, highs, lows, "BULL")
        score, stars = calc_strength(True, ob_ok, fvg_ok, rsi_ok, vol_ok)

        if ob_ok and conf_fast and not choch and score >= 3:
            if last_signal["type"] != "BUY" or curr_i != last_signal["bar"]:
                entry  = float(curr["close"])
                sl     = ob["bot"] - SL_BUFFER
                tp1    = entry + (entry - sl) * RR_TP1
                tp2    = entry + (entry - sl) * RR_TP2
                strike, premium = find_best_strike(nifty_ltp, "BUY")
                if not strike:
                    print("No valid strike ₹100-₹200 — skipping")
                    return
                opt_tp1, opt_tp2, opt_sl = calc_option_targets(premium, entry, sl, tp1, tp2, "BUY")
                send_ready_alert("BUY")
                time.sleep(5)
                daily_signals += 1
                send_telegram(format_signal("BUY", entry, sl, tp1, tp2, strike, premium, opt_tp1, opt_tp2, opt_sl, score, stars, htf_trend, fvg_ok, daily_signals))
                last_signal  = {"type": "BUY", "bar": curr_i, "time": datetime.now(IST)}
                active_trade = {"signal": "BUY", "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp1_hit": False, "be_moved": False}
                print(f"BUY #{daily_signals} | Strike={strike} | ₹{premium} | Strength={score}/5")

    if trend == "BEARISH" and htf_trend == "BEARISH":
        fvg_ok = find_fvg(df5, "BEAR")
        rsi_ok = rsi_val < 50
        ob_ok  = float(curr["high"]) >= ob["bot"] and float(curr["high"]) <= ob["top"] + 10
        choch  = detect_choch(df5, highs, lows, "BEAR")
        score, stars = calc_strength(True, ob_ok, fvg_ok, rsi_ok, vol_ok)

        if ob_ok and conf_fast and not choch and score >= 3:
            if last_signal["type"] != "SELL" or curr_i != last_signal["bar"]:
                entry  = float(curr["close"])
                sl     = ob["top"] + SL_BUFFER
                tp1    = entry - (sl - entry) * RR_TP1
                tp2    = entry - (sl - entry) * RR_TP2
                strike, premium = find_best_strike(nifty_ltp, "SELL")
                if not strike:
                    print("No valid strike ₹100-₹200 — skipping")
                    return
                opt_tp1, opt_tp2, opt_sl = calc_option_targets(premium, entry, sl, tp1, tp2, "SELL")
                send_ready_alert("SELL")
                time.sleep(5)
                daily_signals += 1
                send_telegram(format_signal("SELL", entry, sl, tp1, tp2, strike, premium, opt_tp1, opt_tp2, opt_sl, score, stars, htf_trend, fvg_ok, daily_signals))
                last_signal  = {"type": "SELL", "bar": curr_i, "time": datetime.now(IST)}
                active_trade = {"signal": "SELL", "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp1_hit": False, "be_moved": False}
                print(f"SELL #{daily_signals} | Strike={strike} | ₹{premium} | Strength={score}/5")

# ─────────────────────────────────────────
# START
# ─────────────────────────────────────────
print("SMC Bot v3 Started!")
send_telegram(
    "🤖 <b>SMC Bot v3 Started!</b>\n"
    "📡 Live Data: Dhan API\n"
    "📊 Multi TF: 15m + 5m + 1m\n"
    "✅ Real Option Chain Premium\n"
    "✅ Candle Close Confirmation\n"
    "✅ TP1/TP2/SL/Breakeven Alerts\n"
    f"✅ Max {MAX_SIGNALS} Signals/Day | {COOLDOWN_MIN}min Cooldown\n"
    "💰 Premium Filter: ₹100 - ₹200\n"
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
            print(f"Scanning {now_ist.strftime('%H:%M:%S')} IST | Signals: {daily_signals}/{MAX_SIGNALS}")
            scan()
        else:
            print(f"Market closed {now_ist.strftime('%H:%M:%S')} IST")

    except Exception as e:
        print("Loop Error:", e)

    time.sleep(SCAN_EVERY)
