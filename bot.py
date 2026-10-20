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
]

TIMEFRAME  = "15m"
PERIOD     = 20
ATR_LEN    = 14
ATR_MULT   = 2.4

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Sniper Bot Active")

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
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print("Telegram Send Error:", e)

def fetch_klines(symbol):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={TIMEFRAME}&limit=120"
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

def rma(series, length):
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def run_strategy():
    last_signal = {sym: 0 for sym in SYMBOLS}
    last_candle = {sym: 0 for sym in SYMBOLS}

    send_telegram("🚀 *Sniper Bot v3 Synced!*\n\nAll filter bottlenecks removed. Waiting for next 15M candle close.")

    while True:
        for sym in SYMBOLS:
            try:
                df = fetch_klines(sym)
                # Last closed candle
                c_time = df.iloc[-2]['time']
                
                if last_candle[sym] == c_time:
                    continue

                cdf = df.iloc[:-1].copy()

                # ATR RMA
                tr1 = cdf['high'] - cdf['low']
                tr2 = (cdf['high'] - cdf['close'].shift(1)).abs()
                tr3 = (cdf['low'] - cdf['close'].shift(1)).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                cdf['atr'] = rma(tr, ATR_LEN)

                # SuperTrend Core
                up_trend = np.zeros(len(cdf))
                dn_trend = np.zeros(len(cdf))
                trend = np.zeros(len(cdf))

                for i in range(1, len(cdf)):
                    hl2 = (cdf['high'].iloc[i] + cdf['low'].iloc[i]) / 2.0
                    up = hl2 - (ATR_MULT * cdf['atr'].iloc[i])
                    dn = hl2 + (ATR_MULT * cdf['atr'].iloc[i])

                    up_trend[i] = max(up, up_trend[i-1]) if cdf['close'].iloc[i-1] > up_trend[i-1] else up
                    dn_trend[i] = min(dn, dn_trend[i-1]) if cdf['close'].iloc[i-1] < dn_trend[i-1] else dn

                    if cdf['close'].iloc[i] > dn_trend[i-1]:
                        trend[i] = 1
                    elif cdf['close'].iloc[i] < up_trend[i-1]:
                        trend[i] = -1
                    else:
                        trend[i] = trend[i-1]

                cdf['trend'] = trend
                curr = cdf.iloc[-1]
                prev = cdf.iloc[-2]

                # Direct Trend Transition Check
                if curr['trend'] == 1 and prev['trend'] == -1:
                    if last_signal[sym] != 1:
                        last_signal[sym] = 1
                        msg = (
                            f"🟢 *SNIPER BUY SIGNAL*\n\n"
                            f"Pair: `#{sym}`\n"
                            f"Price: `${curr['close']:.2f}`\n"
                            f"Timeframe: `15m`\n"
                            f"Target: Next Swing High"
                        )
                        send_telegram(msg)

                elif curr['trend'] == -1 and prev['trend'] == 1:
                    if last_signal[sym] != -1:
                        last_signal[sym] = -1
                        msg = (
                            f"🔴 *SNIPER SELL SIGNAL*\n\n"
                            f"Pair: `#{sym}`\n"
                            f"Price: `${curr['close']:.2f}`\n"
                            f"Timeframe: `15m`\n"
                            f"Target: Next Swing Low"
                        )
                        send_telegram(msg)

                last_candle[sym] = c_time

            except Exception as e:
                print(f"Error {sym}:", e)

            time.sleep(0.5)

        time.sleep(10)

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()
    run_strategy()
