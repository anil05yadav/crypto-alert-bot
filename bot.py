import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import pandas as pd
import numpy as np

# ---------------- CONFIGURATION ----------------
TELEGRAM_BOT_TOKEN = "7891331261:AAGk2XL2_4LqIpBwSHwA-5dn2RTrzUbvAnA"
TELEGRAM_CHAT_ID   = "6470999168"

SYMBOLS = [
    {"delta": "BTCUSD", "binance": "BTCUSDT"},
    {"delta": "ETHUSD", "binance": "ETHUSDT"}
]

# Timeframe & Deep Historical Buffer
TIMEFRAME  = "15m"
LIMIT_BARS = 1000

# Strategy Parameters
SWING_LEN  = 20
ATR_PERIOD = 14
ATR_MULT   = 2.0       # Optimized from 2.4 for faster reversal capture
EMA_LEN    = 50
MIN_BODY   = 0.55

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Crypto Sniper 24/7 LIVE")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=8)
        print(f"Telegram status: {r.status_code}", flush=True)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def fetch_klines(sym_dict, timeframe="15m", limit=1000):
    delta_sym = sym_dict["delta"]
    binance_sym = sym_dict["binance"]
    now = int(time.time())
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # 1. Delta Exchange API (Max safe resolution batch is 500 bars)
    res_code = "1D" if timeframe == "1d" else "15"
    sec_per_bar = 86400 if timeframe == "1d" else 900
    safe_delta_limit = min(limit, 500)
    from_time = now - (safe_delta_limit * sec_per_bar)
    
    delta_url = f"https://api.delta.exchange/v2/chart/history?symbol={delta_sym}&resolution={res_code}&from={from_time}&to={now}"
    try:
        r = requests.get(delta_url, headers=headers, timeout=6)
        if r.status_code == 200:
            data = r.json()
            if data.get("success") and data.get("result") and len(data["result"]) >= 100:
                df = pd.DataFrame(data["result"])
                df = df.sort_values("time").reset_index(drop=True)
                for col in ['open', 'high', 'low', 'close']:
                    df[col] = df[col].astype(float)
                return df
    except Exception:
        pass

    # 2. Resilient Binance Mirrors (1000 Bars Full Buffer)
    mirrors = [
        f"https://data-api.binance.vision/api/v3/klines?symbol={binance_sym}&interval={timeframe}&limit={min(limit, 1000)}",
        f"https://api.binance.com/api/v3/klines?symbol={binance_sym}&interval={timeframe}&limit={min(limit, 1000)}",
        f"https://api1.binance.com/api/v3/klines?symbol={binance_sym}&interval={timeframe}&limit={min(limit, 1000)}"
    ]
    for m_url in mirrors:
        try:
            r = requests.get(m_url, headers=headers, timeout=5)
            if r.status_code == 200:
                candles = r.json()
                if isinstance(candles, list) and len(candles) >= 100:
                    df = pd.DataFrame(candles, columns=[
                        'time', 'open', 'high', 'low', 'close', 'volume',
                        'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'
                    ])
                    for col in ['open', 'high', 'low', 'close']:
                        df[col] = df[col].astype(float)
                    return df
        except Exception:
            continue

    return pd.DataFrame()

def rma(series, length):
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def run_strategy():
    active_trades = {
        s["delta"]: {
            "side": None,      # 'BUY' or 'SELL'
            "entry_price": 0.0,
            "entry_time": 0,
            "trailing_sl": 0.0
        } for s in SYMBOLS
    }

    last_processed_candle_time = {s["delta"]: None for s in SYMBOLS}
    test_alert_done = {s["delta"]: False for s in SYMBOLS}

    print(">>> Strategy Engine Started (1000 Bars + CHoCH + Trailing SL) <<<", flush=True)

    while True:
        for sym_dict in SYMBOLS:
            sym = sym_dict["delta"]
            try:
                # 1. Fetch Deep Candle History
                df = fetch_klines(sym_dict, timeframe="15m", limit=LIMIT_BARS)
                if df.empty or len(df) < 150:
                    print(f"[{sym}] Fetching feed retry...", flush=True)
                    continue

                closed_candle = df.iloc[-2]
                closed_time = closed_candle['time']

                is_new_candle = (last_processed_candle_time[sym] != closed_time)
                if not is_new_candle and test_alert_done[sym]:
                    continue

                cdf = df.iloc[:-1].copy().reset_index(drop=True)
                n = len(cdf)

                # 2. Fetch Daily PDH / PDL
                daily_df = fetch_klines(sym_dict, timeframe="1d", limit=5)
                if not daily_df.empty and len(daily_df) >= 2:
                    pdh = daily_df.iloc[-2]['high']
                    pdl = daily_df.iloc[-2]['low']
                else:
                    pdh = cdf['high'].max()
                    pdl = cdf['low'].min()

                # 3. ATR & Volatility Expansion
                tr1 = cdf['high'] - cdf['low']
                tr2 = (cdf['high'] - cdf['close'].shift(1)).abs()
                tr3 = (cdf['low'] - cdf['close'].shift(1)).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

                cdf['atr'] = rma(tr, ATR_PERIOD)
                cdf['atr_ma'] = cdf['atr'].rolling(20).mean()
                cdf['is_expanded'] = cdf['atr'] >= (cdf['atr_ma'] * 0.92)

                # 4. Solid Candle Body Ratio
                c_range = cdf['high'] - cdf['low']
                c_body = (cdf['close'] - cdf['open']).abs()
                cdf['solid_candle'] = np.where(c_range > 0, (c_body / c_range) >= MIN_BODY, False)
                cdf['ema_base'] = cdf['close'].ewm(span=EMA_LEN, adjust=False).mean()

                # 5. Major Swing Pivots & Genuine CHoCH / MSS Detection
                highs = cdf['high'].values
                lows = cdf['low'].values
                closes = cdf['close'].values

                major_high = np.full(n, np.nan)
                major_low = np.full(n, np.nan)
                curr_maj_high = np.nan
                curr_maj_low = np.nan

                for i in range(SWING_LEN * 2, n):
                    center_idx = i - SWING_LEN
                    if all(highs[center_idx] >= highs[j] for j in range(center_idx - SWING_LEN, center_idx + SWING_LEN + 1) if j != center_idx):
                        curr_maj_high = highs[center_idx]
                    if all(lows[center_idx] <= lows[j] for j in range(center_idx - SWING_LEN, center_idx + SWING_LEN + 1) if j != center_idx):
                        curr_maj_low = lows[center_idx]
                    major_high[i] = curr_maj_high
                    major_low[i] = curr_maj_low

                # Extract last confirmed valid swing high and low
                valid_highs = major_high[~np.isnan(major_high)]
                valid_lows  = major_low[~np.isnan(major_low)]
                last_swing_high = valid_highs[-1] if len(valid_highs) > 0 else highs[-2]
                last_swing_low  = valid_lows[-1] if len(valid_lows) > 0 else lows[-2]

                choch_bull = (closes[-1] > last_swing_high)
                choch_bear = (closes[-1] < last_swing_low)

                # 6. Dynamic Trailing Trend Engine (ATR Multiplier 2.0)
                up_trend = np.zeros(n)
                dn_trend = np.zeros(n)
                trend_state = np.zeros(n, dtype=int)
                atrs = cdf['atr'].values

                for i in range(1, n):
                    src_up = closes[i] - (ATR_MULT * atrs[i])
                    src_dn = closes[i] + (ATR_MULT * atrs[i])

                    up_trend[i] = max(src_up, up_trend[i-1]) if closes[i-1] > up_trend[i-1] else src_up
                    dn_trend[i] = min(src_dn, dn_trend[i-1]) if closes[i-1] < dn_trend[i-1] else src_dn

                    if closes[i] > dn_trend[i-1]:
                        trend_state[i] = 1
                    elif closes[i] < up_trend[i-1]:
                        trend_state[i] = -1
                    else:
                        trend_state[i] = trend_state[i-1]

                # 7. Sweep Validation
                lookback_start = max(0, n - 21)
                swept_low = any(lows[k] < pdl or (not np.isnan(major_low[k]) and lows[k] < major_low[k]) for k in range(n - 1, lookback_start - 1, -1))
                swept_high = any(highs[k] > pdh or (not np.isnan(major_high[k]) and highs[k] > major_high[k]) for k in range(n - 1, lookback_start - 1, -1))

                trend_flipped_bull = (trend_state[-1] == 1 and trend_state[-2] == -1)
                trend_flipped_bear = (trend_state[-1] == -1 and trend_state[-2] == 1)

                curr_close = closes[-1]
                curr_ema = cdf['ema_base'].iloc[-1]
                is_exp = bool(cdf['is_expanded'].iloc[-1])
                is_solid = bool(cdf['solid_candle'].iloc[-1])

                print(f"[{sym}] Processed! Close: {curr_close:.2f}, Trend: {trend_state[-1]}, EMA: {curr_ema:.2f}", flush=True)

                # Instant Test Alert on Startup
                if not test_alert_done[sym]:
                    test_alert_done[sym] = True
                    last_processed_candle_time[sym] = closed_time
                    state_str = "BULLISH 🟢" if trend_state[-1] == 1 else "BEARISH 🔴"
                    test_msg = (
                        f"🧪 *PAPER ENGINE LIVE (VERIFIED)*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Last Close: `${curr_close:.2f}`\n"
                        f"Trend: `{state_str}`\n"
                        f"EMA 50: `${curr_ema:.2f}`\n\n"
                        f"✅ CHoCH/MSS + 50 EMA Trailing Active!"
                    )
                    send_telegram(test_msg)

                # 8. Signals (Pine Parity + CHoCH/MSS Sensitivity)
                raw_buy = (trend_flipped_bull or choch_bull) and (swept_low or curr_close > curr_ema) and is_exp and is_solid
                raw_sell = (trend_flipped_bear or choch_bear) and (swept_high or curr_close < curr_ema) and is_exp and is_solid

                trade = active_trades[sym]

                # --- TRAILING SL & EXIT CHECK (RIDE BIG MOVES) ---
                if trade["side"] == "BUY":
                    trail_level = max(curr_ema, up_trend[-1])
                    if trail_level > trade["trailing_sl"]:
                        trade["trailing_sl"] = trail_level

                    if curr_close < curr_ema or raw_sell:
                        pnl_pts = curr_close - trade["entry_price"]
                        pnl_pct = (pnl_pts / trade["entry_price"]) * 100
                        result_icon = "🟢 PROFIT" if pnl_pts > 0 else "🔴 LOSS"
                        send_telegram(
                            f"🏁 *PAPER EXIT (BUY POSITION CLOSED)*\n\n"
                            f"Coin: `#{sym}`\n"
                            f"Exit: `${curr_close:.2f}`\n"
                            f"Entry: `${trade['entry_price']:.2f}`\n"
                            f"Outcome: *{result_icon}*\n"
                            f"PnL: `{pnl_pts:+.2f} USDT` (`{pnl_pct:+.2f}%`)\n"
                            f"Reason: `Opposite Signal / EMA Trail Broken`"
                        )
                        trade["side"] = None

                elif trade["side"] == "SELL":
                    trail_level = min(curr_ema, dn_trend[-1])
                    if trade["trailing_sl"] == 0 or trail_level < trade["trailing_sl"]:
                        trade["trailing_sl"] = trail_level

                    if curr_close > curr_ema or raw_buy:
                        pnl_pts = trade["entry_price"] - curr_close
                        pnl_pct = (pnl_pts / trade["entry_price"]) * 100
                        result_icon = "🟢 PROFIT" if pnl_pts > 0 else "🔴 LOSS"
                        send_telegram(
                            f"🏁 *PAPER EXIT (SELL POSITION CLOSED)*\n\n"
                            f"Coin: `#{sym}`\n"
                            f"Exit: `${curr_close:.2f}`\n"
                            f"Entry: `${trade['entry_price']:.2f}`\n"
                            f"Outcome: *{result_icon}*\n"
                            f"PnL: `{pnl_pts:+.2f} USDT` (`{pnl_pct:+.2f}%`)\n"
                            f"Reason: `Opposite Signal / EMA Trail Broken`"
                        )
                        trade["side"] = None

                # --- NEW TRADE ENTRIES ---
                if raw_buy and trade["side"] != "BUY":
                    initial_sl = lows[-1] - (atrs[-1] * 0.5)
                    trade["side"] = "BUY"
                    trade["entry_price"] = curr_close
                    trade["entry_time"] = closed_time
                    trade["trailing_sl"] = initial_sl

                    send_telegram(
                        f"🟢 *SNIPER BUY ALERT (PAPER TRADE)*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Entry Price: `${curr_close:.2f}`\n"
                        f"Initial SL: `${initial_sl:.2f}`\n"
                        f"Trailing Ref: `50 EMA (${curr_ema:.2f})`\n"
                        f"Rule: *Holding position until opposite flip / EMA break!*"
                    )

                elif raw_sell and trade["side"] != "SELL":
                    initial_sl = highs[-1] + (atrs[-1] * 0.5)
                    trade["side"] = "SELL"
                    trade["entry_price"] = curr_close
                    trade["entry_time"] = closed_time
                    trade["trailing_sl"] = initial_sl

                    send_telegram(
                        f"🔴 *SNIPER SELL ALERT (PAPER TRADE)*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Entry Price: `${curr_close:.2f}`\n"
                        f"Initial SL: `${initial_sl:.2f}`\n"
                        f"Trailing Ref: `50 EMA (${curr_ema:.2f})`\n"
                        f"Rule: *Holding position until opposite flip / EMA break!*"
                    )

                last_processed_candle_time[sym] = closed_time

            except Exception as err:
                print(f"Loop Exception for {sym}: {err}", flush=True)

            time.sleep(1)

        time.sleep(10)

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()
    run_strategy()
            
