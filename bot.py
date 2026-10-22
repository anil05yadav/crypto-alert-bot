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

# Delta Exchange Symbols
SYMBOLS    = ["BTCUSD", "ETHUSD"]
TIMEFRAME  = "15m"  # Delta resolution: 15m, 1d

SWING_LEN  = 20
ATR_PERIOD = 14
ATR_MULT   = 2.4
EMA_LEN    = 50
MIN_BODY   = 0.60

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Delta Sniper Engine 24/7 LIVE")

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

# Delta Exchange Candle Fetcher (100% Free Public API)
def fetch_delta_klines(symbol, resolution="15m", limit=120):
    now = int(time.time())
    # 15m = 900 seconds per candle
    sec_per_bar = 86400 if resolution == "1d" else 900
    start_time = now - (limit * sec_per_bar)
    
    url = f"https://api.delta.exchange/v2/chart/history?symbol={symbol}&resolution={resolution}&from={start_time}&to={now}"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        res = requests.get(url, headers=headers, timeout=8).json()
        if res.get("success") and res.get("result"):
            data = res["result"]
            df = pd.DataFrame(data)
            # Delta returns: time, open, high, low, close, volume
            df = df.sort_values("time").reset_index(drop=True)
            for col in ['open', 'high', 'low', 'close']:
                df[col] = df[col].astype(float)
            return df
    except Exception as e:
        print(f"Delta API Error for {symbol}: {e}", flush=True)
    return pd.DataFrame()

def rma(series, length):
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def run_strategy():
    positions = {sym: 0 for sym in SYMBOLS}
    last_processed_candle_time = {sym: None for sym in SYMBOLS}
    test_alert_done = {sym: False for sym in SYMBOLS}

    print(">>> Delta Exchange Sniper Engine Started <<<", flush=True)
    send_telegram(
        "⚡ *Delta Exchange Sniper Engine Active!*\n\n"
        "DataSource: `Delta Exchange API`\n"
        "Pairs: `BTCUSD, ETHUSD`\n"
        "Status: Connecting to candles..."
    )

    while True:
        for sym in SYMBOLS:
            try:
                # 1. Fetch 15m Candles
                df = fetch_delta_klines(sym, resolution="15m", limit=120)
                if df.empty or len(df) < 50:
                    print(f"[{sym}] Waiting for Delta data...", flush=True)
                    continue

                closed_candle = df.iloc[-2]
                closed_time = closed_candle['time']

                is_new_candle = (last_processed_candle_time[sym] != closed_time)
                if not is_new_candle and test_alert_done[sym]:
                    continue

                cdf = df.iloc[:-1].copy().reset_index(drop=True)
                n = len(cdf)

                # 2. Fetch Daily PDH / PDL
                daily_df = fetch_delta_klines(sym, resolution="1d", limit=4)
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
                cdf['is_expanded'] = cdf['atr'] >= (cdf['atr_ma'] * 0.95)

                # 4. Solid Candle Body Ratio
                c_range = cdf['high'] - cdf['low']
                c_body = (cdf['close'] - cdf['open']).abs()
                cdf['solid_candle'] = np.where(c_range > 0, (c_body / c_range) >= MIN_BODY, False)
                cdf['ema_base'] = cdf['close'].ewm(span=EMA_LEN, adjust=False).mean()

                # 5. Major Swing Pivots
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

                # 6. Dynamic Trailing Trend Engine
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

                print(f"[{sym}] Checked! Close: {curr_close:.2f}, Trend: {trend_state[-1]}", flush=True)

                # Instant Test Alert on Startup
                if not test_alert_done[sym]:
                    test_alert_done[sym] = True
                    last_processed_candle_time[sym] = closed_time
                    state_str = "BULLISH 🟢" if trend_state[-1] == 1 else "BEARISH 🔴"
                    test_msg = (
                        f"🧪 *DELTA EXCHANGE TEST ALERT*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Last Close: `${curr_close:.2f}`\n"
                        f"Trend: `{state_str}`\n"
                        f"EMA 50: `${curr_ema:.2f}`\n\n"
                        f"✅ Delta Exchange connection 100% active!"
                    )
                    send_telegram(test_msg)

                # 8. Real Signals
                raw_buy = trend_flipped_bull and (swept_low or curr_close > curr_ema) and is_exp and is_solid
                raw_sell = trend_flipped_bear and (swept_high or curr_close < curr_ema) and is_exp and is_solid

                buy_condition = (positions[sym] != 1) and raw_buy
                sell_condition = (positions[sym] != -1) and raw_sell

                if buy_condition:
                    positions[sym] = 1
                    send_telegram(
                        f"🟢 *SNIPER BUY ALERT (DELTA)*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Price: `${curr_close:.2f}`\n"
                        f"Timeframe: `15m`"
                    )

                elif sell_condition:
                    positions[sym] = -1
                    send_telegram(
                        f"🔴 *SNIPER SELL ALERT (DELTA)*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Price: `${curr_close:.2f}`\n"
                        f"Timeframe: `15m`"
                    )

                last_processed_candle_time[sym] = closed_time

            except Exception as err:
                print(f"Loop Exception for {sym}: {err}", flush=True)

            time.sleep(1)

        time.sleep(12)

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()
    run_strategy()
