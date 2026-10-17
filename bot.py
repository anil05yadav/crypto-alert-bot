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
    "BTCUSDT", 
    "ETHUSDT", 
    "SOLUSDT", 
    "BNBUSDT", 
    "XRPUSDT", 
    "DOGEUSDT", 
    "ADAUSDT", 
    "AVAXUSDT"
]

TIMEFRAME          = "15m"
SWING_LEN          = 20
ATR_PERIOD         = 14
ATR_MULT           = 2.4
EMA_LEN            = 50
MIN_BODY_RATIO     = 0.60

# --- RENDER PORT BINDING (Fixes "No open ports detected" Error) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Crypto Sniper Bot is running 24/7!")

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ---------------- TELEGRAM NOTIFIER ----------------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print("Telegram error:", e)
        return False

# ---------------- BINANCE FETCHER ----------------
def fetch_binance_klines(symbol, interval, limit=200):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    res = requests.get(url, timeout=10).json()
    df = pd.DataFrame(res, columns=[
        'time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'
    ])
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    return df

# ---------------- STRATEGY SCANNER ----------------
def run_strategy():
    positions = {sym: 0 for sym in SYMBOLS}
    print("Multi-Coin Sniper Bot Initializing...")

    symbols_text = ", ".join(SYMBOLS)
    send_telegram(f"🔥 *Sniper Bot LIVE on Render!*\n\nTracking ({len(SYMBOLS)} Pairs):\n`{symbols_text}`\nTimeframe: `{TIMEFRAME}`\n_Monitoring 24/7 on Cloud..._")

    while True:
        for sym in SYMBOLS:
            try:
                df = fetch_binance_klines(sym, TIMEFRAME, limit=150)
                df = df.iloc[:-1].copy()

                # 1. ATR & Squeeze
                tr1 = df['high'] - df['low']
                tr2 = (df['high'] - df['close'].shift(1)).abs()
                tr3 = (df['low'] - df['close'].shift(1)).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                df['atr'] = tr.rolling(ATR_PERIOD).mean()
                df['atr_ma'] = df['atr'].rolling(20).mean()
                df['is_expanded'] = df['atr'] >= (df['atr_ma'] * 0.95)

                # 2. Solid Candle Check
                candle_range = df['high'] - df['low']
                candle_body  = (df['close'] - df['open']).abs()
                df['solid_candle'] = candle_range > 0 and ((candle_body / candle_range) >= MIN_BODY_RATIO)

                # 3. EMA Trend Base
                df['ema_base'] = df['close'].ewm(span=EMA_LEN, adjust=False).mean()

                # 4. Trailing Dynamic State
                up_trend = np.zeros(len(df))
                dn_trend = np.zeros(len(df))
                trend_state = np.zeros(len(df))

                for i in range(1, len(df)):
                    src_up = df['close'].iloc[i] - (ATR_MULT * df['atr'].iloc[i])
                    src_dn = df['close'].iloc[i] + (ATR_MULT * df['atr'].iloc[i])

                    up_trend[i] = max(src_up, up_trend[i-1]) if df['close'].iloc[i-1] > up_trend[i-1] else src_up
                    dn_trend[i] = min(src_dn, dn_trend[i-1]) if df['close'].iloc[i-1] < dn_trend[i-1] else src_dn

                    if df['close'].iloc[i] > dn_trend[i-1]:
                        trend_state[i] = 1
                    elif df['close'].iloc[i] < up_trend[i-1]:
                        trend_state[i] = -1
                    else:
                        trend_state[i] = trend_state[i-1]

                df['trend_state'] = trend_state

                curr = df.iloc[-1]
                prev = df.iloc[-2]

                trend_bull = (curr['trend_state'] == 1 and prev['trend_state'] == -1)
                trend_bear = (curr['trend_state'] == -1 and prev['trend_state'] == 1)

                buy_signal  = trend_bull and curr['is_expanded'] and curr['solid_candle'] and (curr['close'] > curr['ema_base'])
                sell_signal = trend_bear and curr['is_expanded'] and curr['solid_candle'] and (curr['close'] < curr['ema_base'])

                if buy_signal and positions[sym] != 1:
                    positions[sym] = 1
                    msg = f"🟢 *SNIPER BUY SIGNAL!*\n\nCoin: `#{sym}`\nPrice: `${curr['close']}`\nTimeframe: `{TIMEFRAME}`\nTarget: 1:3 RR"
                    print(msg)
                    send_telegram(msg)

                elif sell_signal and positions[sym] != -1:
                    positions[sym] = -1
                    msg = f"🔴 *SNIPER SELL SIGNAL!*\n\nCoin: `#{sym}`\nPrice: `${curr['close']}`\nTimeframe: `{TIMEFRAME}`\nTarget: 1:3 RR"
                    print(msg)
                    send_telegram(msg)

            except Exception as err:
                print(f"Error scanning {sym}:", err)

            time.sleep(1)

        time.sleep(45)

if __name__ == "__main__":
    # Web server ko background thread me chalayenge taaki Render port pass ho jaye
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()

    # Main Strategy Runner
    run_strategy()
