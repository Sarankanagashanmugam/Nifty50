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
DHAN_ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzgyOTEwODY5LCJpYXQiOjE3ODI4MjQ0NjksInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTEyMTg2NzQzIn0.UgiKMTRIMIu2rhvMhbBCtf-KMTxnV_HB8YK878Fsyj-W0LCvSUK-BNh3Qbh28lDToXqgRpiKbKsKC-wuiaO5iA"

NIFTY_SECURITY_ID  = "13"
NIFTY_EXCHANGE_SEG = "IDX_I"
NIFTY_INSTRUMENT   = "INDEX"

SCAN_EVERY     = 60       # seconds between scans
SWING_LEN      = 5
SL_BUFFER      = 15
RR_TP1         = 1.5
RR_TP2         = 3.0
COOLDOWN_MIN   = 15       # minutes between signals
MAX_SIGNALS    = 6        # max signals per day
MIN_SIGNALS    = 3        # minimum target per day
PREMIUM_MIN    = 100      # minimum option premium
PREMIUM_MAX    = 200      # maximum option premium

IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
last_signal         = {"type": None, "bar": 0, "time": None}
market_opened_today = None
daily_signals       = 0
daily_date          = None
active_trade        = None   # tracks open trade for TP/SL alerts
expiry_cache        = {"expiry": None, "fetched": None}

dhan_context = DhanContext(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
dhan = dhanhq(dhan_context)

# ─────────────────────────────────────────
# MARKET OPEN
# ─────────────────────────────────────────
def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=15, second=0) <= now <= now.replace(hour=15, minute=30, second=0)

# ─────────────────────────────────────────
# EXPIRY / DAY SETTINGS
# ─────────────────────────────────────────
def get_day_label():
    day = datetime.now(IST).weekday()
    return "expiry" if day == 3 else "normal"

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
    day = datetime.now(IST).weekday()
    expiry = "⚠️ <b>EXPIRY DAY</b> — Extra caution!\n" if day == 3 else ""
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
# DHAN — CANDLE DATA
# ─────────────────────────────────────────
def get_candles_dhan(interval="5"):
    try:
        now       = datetime.now(IST)
        from_date = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        to_date   = now.strftime("%Y-%m-%d %H:%M:%S")
        response  = dhan.intraday_minute_data(
            security_id=NIFTY_SECURITY_ID,
            exchange_segment=NIFTY_EXCHANGE_SEG,
            instrument_type=NIFTY_INSTRUMENT,
            from_date=from_date,
            to_date=to_date,
            interval=interval
        )
        if not response or "data" not in response:
            print(f"Dhan: no data ({interval}m)")
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
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s") \
                          + timedelta(hours=5, minutes=30)
        df.dropna(inplace=True)
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"Dhan data error ({interval}m):", e)
        return None

# ─────────────────────────────────────────
# CANDLE CLOSE CONFIRMATION
# ✅ Only trade on fully closed candles — no repaint
# ─────────────────────────────────────────
def is_candle_closed(df, interval_min=5):
    """Returns True only if the last candle is fully closed (not current forming candle)."""
    if df is None or len(df) < 2:
        return False
    last_ts  = df["timestamp"].iloc[-1]
    now_ist  = datetime.now(IST).replace(tzinfo=None)
    elapsed  = (now_ist - last_ts).total_seconds() / 60
    # candle is "closed" if at least interval_min minutes have passed since it opened
    return elapsed >= interval_min

# ─────────────────────────────────────────
# DHAN — REAL OPTION PREMIUM
# ✅ Fetches real LTP from option chain, scans strikes for ₹100-₹200 range
# ─────────────────────────────────────────
def get_nearest_expiry():
    now = datetime.now(IST)
    if expiry_cache["expiry"] and expiry_cache["fetched"] and \
       (now - expiry_cache["fetched"]).total_seconds() < 3600:
        return expiry_cache["expiry"]
    try:
        resp     = dhan.expiry_list(
            under_security_id=int(NIFTY_SECURITY_ID),
            under_exchange_segment=NIFTY_EXCHANGE_SEG
        )
        expiries = resp.get("data", [])
        if expiries:
            expiry_cache["expiry"]  = expiries[0]
            expiry_cache["fetched"] = now
            print(f"Nearest expiry: {expiries[0]}")
            return expiries[0]
    except Exception as e:
        print("Expiry list error:", e)
    return None

def fetch_option_chain(expiry):
    try:
        resp = dhan.option_chain(
            under_security_id=int(NIFTY_SECURITY_ID),
            under_exchange_segment=NIFTY_EXCHANGE_SEG,
            expiry=expiry
        )
        return resp.get("data", {}).get("oc", {})
    except Exception as e:
        print("Option chain fetch error:", e)
        return {}

def find_best_strike(nifty_price, signal_type):
    """
    ✅ Scans ATM ± 4 strikes and picks the one with premium ₹100-₹200.
    Returns (strike, premium) or (None, None) if none found.
    signal_type: 'BUY' → CE, 'SELL' → PE
    """
    expiry = get_nearest_expiry()
    if not expiry:
        return None, None

    oc = fetch_option_chain(expiry)
    if not oc:
        print("Empty option chain")
        return None, None

    time.sleep(3)  # Dhan OC rate limit: 1 req per 3 sec

    atm = round(nifty_price / 50) * 50
    leg = "ce" if signal_type == "BUY" else "pe"

    # Scan strikes: ATM, ATM-50, ATM+50, ATM-100, ATM+100, ATM-150, ATM+150, ATM-200
    candidates = []
    for offset in [0, -50, 50, -100, 100, -150, 150, -200, 200]:
        strike = atm + offset
        # find matching key in OC (keys are float strings like "24250.0")
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
        ltp = oc[strike_key].get(leg, {}).get("last_price")
        if ltp and PREMIUM_MIN <= ltp <= PREMIUM_MAX:
            candidates.append((strike, round(ltp, 2)))

    if not candidates:
        print(f"No strike found in ₹{PREMIUM_MIN}-₹{PREMIUM_MAX} range")
        return None, None

    # Pick the one closest to ATM within range
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
    return True if pd.isna(avg) or avg == 0 else cur > avg * 0.8

# ─────────────────────────────────────────
# SMC
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
            gap = df["low"].iloc[i] - df["high"].iloc[i-2]
            if gap >= min_gap:
                later = df.iloc[i+1:]
                if later.empty or not (later["low"] <= df["high"].iloc[i-2]).any():
                    return True
        else:
            gap = df["low"].iloc[i-2] - df["high"].iloc[i]
            if gap >= min_gap:
                later = df.iloc[i+1:]
                if later.empty or not (later["high"] >= df["low"].iloc[i-2]).any():
                    return True
    return False

def detect_choch(df, highs, lows, direction):
    if direction == "BULL" and len(lows) >= 2:
        return df["close"].iloc[-1] < lows[-2]["price"]
    if direction == "BEAR" and len(highs) >= 2:
        return df["close"].iloc[-1] > highs[-2]["price"]
    return False

def get_htf_trend(df15):
    if df15 is None or len(df15) < 55:
        return "NEUTRAL"
    ema50 = calc_ema(df15["close"], 50)
    lc, le = df15["close"].iloc[-1], ema50.iloc[-1]
    if lc > le: return "BULLISH"
    if lc < le: return "BEARISH"
    return "NEUTRAL"

def calc_strength(htf_ok, ob_ok, fvg_ok, rsi_ok, vol_ok):
    score = sum([htf_ok, ob_ok, fvg_ok, rsi_ok, vol_ok])
    return score, "⭐" * score + "☆" * (5 - score)

# ─────────────────────────────────────────
# OPTION TARGETS — DELTA BASED (0.5 for near-ATM)
# ─────────────────────────────────────────
def calc_option_targets(premium, nifty_entry, nifty_sl, nifty_tp1, nifty_tp2, signal):
    delta = 0.5
    if signal == "BUY":
        nifty_tp1_pts = nifty_tp1 - nifty_entry
        nifty_tp2_pts = nifty_tp2 - nifty_entry
        nifty_sl_pts  = nifty_entry - nifty_sl
    else:
        nifty_tp1_pts = nifty_entry - nifty_tp1
        nifty_tp2_pts = nifty_entry - nifty_tp2
        nifty_sl_pts  = nifty_sl - nifty_entry

    opt_tp1 = round(premium + nifty_tp1_pts * delta)
    opt_tp2 = round(premium + nifty_tp2_pts * delta)
    opt_sl  = round(max(5, premium - nifty_sl_pts * delta))
    return opt_tp1, opt_tp2, opt_sl

# ─────────────────────────────────────────
# FORMAT SIGNAL
# ─────────────────────────────────────────
def format_signal(signal, entry, sl, tp1, tp2, strike, premium,
                  opt_tp1, opt_tp2, opt_sl, strength_score,
                  strength_stars, htf_trend, fvg_ok, signals_today):
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
        f"📊 HTF Trend (15m) : {htf_trend}\n"
        f"🌀 FVG (fresh)     : {fvg_txt}\n"
        f"💪 Strength        : {strength_stars} ({strength_score}/5)\n"
        f"📈 Signal #{signals_today} today  (Max {MAX_SIGNALS})\n"
        f"📡 Data            : Dhan Live API\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💡 Book 50% at TP1 → move SL to entry!\n"
        f"🕐 {now_ist}\n\n"
        f"⚠️ <i>Educational only. Trade at your own risk.</i>"
    )

# ─────────────────────────────────────────
# TRADE MONITOR — TP/SL/BREAKEVEN ALERTS
# ─────────────────────────────────────────
def monitor_active_trade(df5):
    global active_trade
    if not active_trade:
        return

    curr      = df5.iloc[-1]
    price     = float(curr["close"])
    signal    = active_trade["signal"]
    entry     = active_trade["entry"]
    sl        = active_trade["sl"]
    tp1       = active_trade["tp1"]
    tp2       = active_trade["tp2"]
    tp1_hit   = active_trade.get("tp1_hit", False)
    be_moved  = active_trade.get("be_moved", False)

    if signal == "BUY":
        if not tp1_hit and price >= tp1:
            active_trade["tp1_hit"] = True
            send_telegram(
                f"🎯 <b>TP1 HIT!</b>\n\n"
                f"✅ Nifty reached <code>{round(tp1)}</code>\n"
                f"💡 Book 50% profit now!\n"
                f"🔄 Move SL to entry <code>{round(entry)}</code>"
            )
        if tp1_hit and not be_moved:
            active_trade["be_moved"] = True
            send_telegram(
                f"🔄 <b>BREAKEVEN ALERT</b>\n\n"
                f"Move your SL to entry: <code>{round(entry)}</code>\n"
                f"🏆 Now targeting TP2: <code>{round(tp2)}</code>"
            )
        if tp1_hit and price >= tp2:
            send_telegram(
                f"🏆 <b>TP2 HIT! FULL TARGET!</b>\n\n"
                f"✅ Nifty reached <code>{round(tp2)}</code>\n"
                f"🎉 Excellent trade! Exit full position."
            )
            active_trade = None
        elif price <= sl:
            send_telegram(
                f"🛑 <b>SL HIT</b>\n\n"
                f"❌ Nifty hit SL at <code>{round(sl)}</code>\n"
                f"📉 Exit position. Wait for next signal."
            )
            active_trade = None

    else:  # SELL
        if not tp1_hit and price <= tp1:
            active_trade["tp1_hit"] = True
            send_telegram(
                f"🎯 <b>TP1 HIT!</b>\n\n"
                f"✅ Nifty reached <code>{round(tp1)}</code>\n"
                f"💡 Book 50% profit now!\n"
                f"🔄 Move SL to entry <code>{round(entry)}</code>"
            )
        if tp1_hit and not be_moved:
            active_trade["be_moved"] = True
            send_telegram(
                f"🔄 <b>BREAKEVEN ALERT</b>\n\n"
                f"Move your SL to entry: <code>{round(entry)}</code>\n"
                f"🏆 Now targeting TP2: <code>{round(tp2)}</code>"
            )
        if tp1_hit and price <= tp2:
            send_telegram(
                f"🏆 <b>TP2 HIT! FULL TARGET!</b>\n\n"
                f"✅ Nifty reached <code>{round(tp2)}</code>\n"
                f"🎉 Excellent trade! Exit full position."
            )
            active_trade = None
        elif price >= sl:
            send_telegram(
                f"🛑 <b>SL HIT</b>\n\n"
                f"❌ Nifty hit SL at <code>{round(sl)}</code>\n"
                f"📉 Exit position. Wait for next signal."
            )
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
        print(f"Cooldown: {round(COOLDOWN_MIN - elapsed)} min remaining")
        return False
    return True

def signals_ok():
    if daily_signals >= MAX_SIGNALS:
        print(f"Max signals ({MAX_SIGNALS}) reached today")
        return False
    return True

# ─────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────
def scan():
    global last_signal, daily_signals, active_trade

    reset_daily_if_needed()

    df15 = get_candles_dhan("15")
    df5  = get_candles_dhan("5")
    df1  = get_candles_dhan("1")

    # ── Monitor active trade first ──
    if df5 is not None and active_trade:
        monitor_active_trade(df5)

    if df5 is None or len(df5) < 30:
        print("Not enough 5m data")
        return

    # ── Candle close confirmation ──
    if not is_candle_closed(df5, interval_min=5):
        print("5m candle not closed yet — waiting")
        return

    # ── Risk checks ──
    if not signals_ok() or not cooldown_ok():
        return

    # ── HTF Trend ──
    htf_trend = get_htf_trend(df15)
    if htf_trend == "NEUTRAL":
        print("HTF Neutral — skipping")
        return

    # ── 5m SMC ──
    highs, lows = find_swings(df5)
    if not highs or not lows:
        return

    rsi    = calc_rsi(df5["close"], 14)
    vol_ok = calc_volume_ok(df5)

    trend = "NEUTRAL"
    ob    = None

    for i in range(5, len(df5)):
        sh  = [h for h in highs if h["i"] < i]
        sl_ = [l for l in lows  if l["i"] < i]
        if not sh or not sl_:
            continue
        if df5["close"].iloc[i] > sh[-1]["price"]:
            cob = find_ob(df5, i, "BULL")
            if cob and is_ob_valid(df5, cob, "BULL"):
                ob, trend = cob, "BULLISH"
        if df5["close"].iloc[i] < sl_[-1]["price"]:
            cob = find_ob(df5, i, "BEAR")
            if cob and is_ob_valid(df5, cob, "BEAR"):
                ob, trend = cob, "BEARISH"

    if ob is None:
        print("No valid OB found")
        return

    curr      = df5.iloc[-1]
    curr_i    = len(df5) - 1
    nifty_ltp = round(float(curr["close"]))
    rsi_val   = rsi.iloc[-1]

    # ── 1m fast confirmation ──
    conf_fast = False
    if df1 is not None and len(df1) >= 3:
        lf = df1.iloc[-1]
        if trend == "BULLISH" and lf["close"] > lf["open"]: conf_fast = True
        if trend == "BEARISH" and lf["close"] < lf["open"]: conf_fast = True

    # ── BUY ──
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

                # ── Real option premium ──
                strike, premium = find_best_strike(nifty_ltp, "BUY")
                if not strike:
                    print("No valid strike in ₹100-₹200 — skipping signal")
                    return

                opt_tp1, opt_tp2, opt_sl = calc_option_targets(
                    premium, entry, sl, tp1, tp2, "BUY"
                )

                send_ready_alert("BUY")
                time.sleep(5)
                daily_signals += 1
                send_telegram(format_signal(
                    "BUY", entry, sl, tp1, tp2, strike, premium,
                    opt_tp1, opt_tp2, opt_sl, score, stars,
                    htf_trend, fvg_ok, daily_signals
                ))
                last_signal = {"type": "BUY", "bar": curr_i, "time": datetime.now(IST)}
                active_trade = {
                    "signal": "BUY", "entry": entry, "sl": sl,
                    "tp1": tp1, "tp2": tp2,
                    "tp1_hit": False, "be_moved": False
                }
                print(f"BUY sent! #{daily_signals} Strength={score}/5 Strike={strike} Premium=₹{premium}")

    # ── SELL ──
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

                # ── Real option premium ──
                strike, premium = find_best_strike(nifty_ltp, "SELL")
                if not strike:
                    print("No valid strike in ₹100-₹200 — skipping signal")
                    return

                opt_tp1, opt_tp2, opt_sl = calc_option_targets(
                    premium, entry, sl, tp1, tp2, "SELL"
                )

                send_ready_alert("SELL")
                time.sleep(5)
                daily_signals += 1
                send_telegram(format_signal(
                    "SELL", entry, sl, tp1, tp2, strike, premium,
                    opt_tp1, opt_tp2, opt_sl, score, stars,
                    htf_trend, fvg_ok, daily_signals
                ))
                last_signal = {"type": "SELL", "bar": curr_i, "time": datetime.now(IST)}
                active_trade = {
                    "signal": "SELL", "entry": entry, "sl": sl,
                    "tp1": tp1, "tp2": tp2,
                    "tp1_hit": False, "be_moved": False
                }
                print(f"SELL sent! #{daily_signals} Strength={score}/5 Strike={strike} Premium=₹{premium}")

# ─────────────────────────────────────────
# START
# ─────────────────────────────────────────
print("SMC Bot Started!")
send_telegram(
    "🤖 <b>SMC Bot v3 Started!</b>\n"
    "📡 Live Data: Dhan API\n"
    "📊 Multi TF: 15m + 5m + 1m\n"
    "✅ Real Option Chain Premium\n"
    "✅ Candle Close Confirmation\n"
    "✅ TP1/TP2/SL/Breakeven Alerts\n"
    "✅ Max 6 Signals/Day | 15min Cooldown\n"
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

            print(f"Scanning {now_ist.strftime('%H:%M:%S')} IST... Signals today: {daily_signals}/{MAX_SIGNALS}")
            scan()
        else:
            print(f"Market closed... {now_ist.strftime('%H:%M:%S')} IST")

    except Exception as e:
        print("Loop Error:", e)

    time.sleep(SCAN_EVERY)
