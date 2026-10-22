import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import pandas as pd
import numpy as np

# ---------------- CONFIGURATION ----------------
TELEGRAM_BOT_TOKEN = "7891331261:AAGk2XL2_4LqIpBwSHwA-5dn2RTrzUbvAnA"
TELEGRAM_CHAT_ID   = "6470999168"

SYMBOLS    = ["BTCUSDT", "ETHUSDT"]
TIMEFRAME  = "15m"

SWING_LEN  = 20
ATR_PERIOD = 14
ATR_MULT   = 2.4
EMA_LEN    = 50
MIN_BODY   = 0.60

def log(msg):
    print(msg, flush=True)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Trend Liquidity Sniper Engine 24/7 LIVE")

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
        log(f"Telegram sent status: {r.status_code}")
    except Exception as e:
        log(f"Telegram Send Error: {e}")

def fetch_klines(symbol, interval, limit=250):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    res = requests.get(url, timeout=10).json()
    df = pd.DataFrame(res, columns=[
        'time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'
    ])
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col].astype(float)
    return df

def rma(series, length):
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def run_strategy():
    positions = {sym: 0 for sym in SYMBOLS}
    last_processed_candle_time = {sym: 0 for sym in SYMBOLS}
    initial_check_sent = False

    log("Sniper Bot Engine Starting...")
    send_telegram(
        f"🎯 *Trend Liquidity Sniper Engine Active!*\n\n"
        f"Pairs: `BTCUSDT, ETHUSDT` (15m)\n"
        f"Logging: `Live Stream Active`\n"
        f"Status: Next candle par instant test alert aayega."
    )

    while True:
        for sym in SYMBOLS:
            try:
                df = fetch_klines(sym, TIMEFRAME, limit=250)
                closed_time = int(df.iloc[-2]['time'])

                # Daily PDH / PDL
                daily_df = fetch_klines(sym, "1d", limit=5)
                pdh = float(daily_df.iloc[-2]['high'])
                pdl = float(daily_df.iloc[-2]['low'])

                cdf = df.iloc[:-1].copy().reset_index(drop=True)
                n = len(cdf)

                # ATR RMA
                tr1 = cdf['high'] - cdf['low']
                tr2 = (cdf['high'] - cdf['close'].shift(1)).abs()
                tr3 = (cdf['low'] - cdf['close'].shift(1)).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

                cdf['atr'] = rma(tr, ATR_PERIOD)
                cdf['atr_ma'] = cdf['atr'].rolling(20).mean()
                cdf['is_expanded'] = cdf['atr'] >= (cdf['atr_ma'] * 0.95)

                # Solid Candle Check
                c_range = cdf['high'] - cdf['low']
                c_body = (cdf['close'] - cdf['open']).abs()
                cdf['solid_candle'] = np.where(c_range > 0, (c_body / c_range) >= MIN_BODY, False)

                # EMA 50
                cdf['ema_base'] = cdf['close'].ewm(span=EMA_LEN, adjust=False).mean()

                # Pivot High / Low
                major_high = np.full(n, np.nan)
                major_low = np.full(n, np.nan)
                curr_maj_high = np.nan
                curr_maj_low = np.nan

                highs = cdf['high'].values
                lows = cdf['low'].values
                closes = cdf['close'].values

                for i in range(SWING_LEN * 2, n):
                    center_idx = i - SWING_LEN
                    if all(highs[center_idx] > highs[j] for j in range(center_idx - SWING_LEN, center_idx + SWING_LEN + 1) if j != center_idx):
                        curr_maj_high = highs[center_idx]
                    if all(lows[center_idx] < lows[j] for j in range(center_idx - SWING_LEN, center_idx + SWING_LEN + 1) if j != center_idx):
                        curr_maj_low = lows[center_idx]
                    major_high[i] = curr_maj_high
                    major_low[i] = curr_maj_low

                # Dynamic Trailing Trend Engine
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

                # Sweep Validation
                swept_low = False
                swept_high = False
                lookback_start = max(0, n - 21)
                for k in range(n - 1, lookback_start - 1, -1):
                    if lows[k] < pdl or (not np.isnan(major_low[k]) and lows[k] < major_low[k]):
                        swept_low = True
                        break

                for k in range(n - 1, lookback_start - 1, -1):
                    if highs[k] > pdh or (not np.isnan(major_high[k]) and highs[k] > major_high[k]):
                        swept_high = True
                        break

                curr_close = closes[-1]
                curr_ema = cdf['ema_base'].iloc[-1]
                is_exp = bool(cdf['is_expanded'].iloc[-1])
                is_solid = bool(cdf['solid_candle'].iloc[-1])
                trend_val = trend_state[-1]

                # First Run Instant Diagnostic Message
                if not initial_check_sent:
                    initial_check_sent = True
                    diag_msg = (
                        f"⚡ *SYSTEM HEALTH CHECK: OK*\n\n"
                        f"Pair: `#{sym}`\n"
                        f"Price: `${curr_close:.2f}`\n"
                        f"Trend: `{'BULL 🟢' if trend_val == 1 else 'BEAR 🔴'}`\n"
                        f"EMA50: `${curr_ema:.2f}`\n"
                        f"ATR Expanded: `{is_exp}`\n"
                        f"Solid Candle: `{is_solid}`\n\n"
                        f"Bot active hai aur calculation loop 100% chal raha hai!"
                    )
                    send_telegram(diag_msg)
                    log(f"Initial diagnostic sent for {sym}")

                # Check if this candle is already processed
                if last_processed_candle_time[sym] == closed_time:
                    continue

                trend_flipped_bull = (trend_state[-1] == 1 and trend_state[-2] == -1)
                trend_flipped_bear = (trend_state[-1] == -1 and trend_state[-2] == 1)

                raw_buy = trend_flipped_bull and (swept_low or curr_close > curr_ema) and is_exp and is_solid
                raw_sell = trend_flipped_bear and (swept_high or curr_close < curr_ema) and is_exp and is_solid

                buy_condition = (positions[sym] != 1) and raw_buy
                sell_condition = (positions[sym] != -1) and raw_sell

                if buy_condition:
                    positions[sym] = 1
                    msg = (
                        f"🟢 *SNIPER BUY ALERT!*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Price: `${curr_close:.2f}`\n"
                        f"Timeframe: `15m`\n"
                        f"Status: Trend Flip + Validated"
                    )
                    send_telegram(msg)
                    log(f"BUY Signal sent for {sym}")

                elif sell_condition:
                    positions[sym] = -1
                    msg = (
                        f"🔴 *SNIPER SELL ALERT!*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Price: `${curr_close:.2f}`\n"
                        f"Timeframe: `15m`\n"
                        f"Status: Trend Flip + Validated"
                    )
                    send_telegram(msg)
                    log(f"SELL Signal sent for {sym}")

                last_processed_candle_time[sym] = closed_time
                log(f"Processed candle {closed_time} for {sym}")

            except Exception as err:
                log(f"Loop Exception for {sym}: {err}")

            time.sleep(1)

        time.sleep(15)

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()
    run_strategy()
