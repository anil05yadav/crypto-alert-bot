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
ATR_PERIOD         = 14
ATR_MULT           = 2.4
EMA_LEN            = 50
MIN_BODY_RATIO     = 0.55  # Realistic threshold taaki clean wick sweep miss na ho

# --- RENDER HEALTH CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Crypto Sniper Bot is running 24/7!")

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
        res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print("Telegram Error:", e)
        return False

# BINANCE FUTURES API (Matches TradingView BTCUSDT.P 100%)
def fetch_binance_futures_klines(symbol, interval, limit=200):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
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

# TradingView Pine Script Exact RMA Function
def rma(series, length):
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def run_strategy():
    positions = {sym: 0 for sym in SYMBOLS}
    last_processed_candle_time = {sym: 0 for sym in SYMBOLS}

    print("Multi-Coin Sniper Bot Initialized (Futures Feed)...")
    symbols_text = ", ".join(SYMBOLS)
    send_telegram(f"⚡ *Futures Sniper Engine Active!*\n\nData Feed: `Binance Futures (fapi)`\nTracking: `{symbols_text}`\nTimeframe: `{TIMEFRAME}`")

    while True:
        for sym in SYMBOLS:
            try:
                df = fetch_binance_futures_klines(sym, TIMEFRAME, limit=150)
                
                # Sirf LAST CLOSED candle check karni hai
                closed_candle_time = df.iloc[-2]['time']
                
                # Agar is candle ko pehle hi check kar chuke hain to dobara evaluate mat karo
                if last_processed_candle_time[sym] == closed_candle_time:
                    continue

                # Closed candles dataframe
                cdf = df.iloc[:-1].copy()

                # 1. Exact Pine Script ATR (using RMA)
                tr1 = cdf['high'] - cdf['low']
                tr2 = (cdf['high'] - cdf['close'].shift(1)).abs()
                tr3 = (cdf['low'] - cdf['close'].shift(1)).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                
                cdf['atr'] = rma(tr, ATR_PERIOD)
                cdf['atr_ma'] = cdf['atr'].rolling(20).mean()
                cdf['is_expanded'] = cdf['atr'] >= (cdf['atr_ma'] * 0.90)

                # 2. Candle Conviction Ratio
                candle_range = cdf['high'] - cdf['low']
                candle_body  = (cdf['close'] - cdf['open']).abs()
                cdf['solid_candle'] = (candle_range > 0) & ((candle_body / candle_range) >= MIN_BODY_RATIO)

                # 3. EMA 50
                cdf['ema_base'] = cdf['close'].ewm(span=EMA_LEN, adjust=False).mean()

                # 4. Supertrend / Dynamic Trailing Logic
                up_trend = np.zeros(len(cdf))
                dn_trend = np.zeros(len(cdf))
                trend_state = np.zeros(len(cdf))

                for i in range(1, len(cdf)):
                    src_up = cdf['close'].iloc[i] - (ATR_MULT * cdf['atr'].iloc[i])
                    src_dn = cdf['close'].iloc[i] + (ATR_MULT * cdf['atr'].iloc[i])

                    up_trend[i] = max(src_up, up_trend[i-1]) if cdf['close'].iloc[i-1] > up_trend[i-1] else src_up
                    dn_trend[i] = min(src_dn, dn_trend[i-1]) if cdf['close'].iloc[i-1] < dn_trend[i-1] else src_dn

                    if cdf['close'].iloc[i] > dn_trend[i-1]:
                        trend_state[i] = 1
                    elif cdf['close'].iloc[i] < up_trend[i-1]:
                        trend_state[i] = -1
                    else:
                        trend_state[i] = trend_state[i-1]

                cdf['trend_state'] = trend_state

                curr = cdf.iloc[-1]
                prev = cdf.iloc[-2]

                trend_bull = (curr['trend_state'] == 1 and prev['trend_state'] == -1)
                trend_bear = (curr['trend_state'] == -1 and prev['trend_state'] == 1)

                buy_signal  = trend_bull and curr['is_expanded'] and (curr['close'] > curr['ema_base'])
                sell_signal = trend_bear and curr['is_expanded'] and (curr['close'] < curr['ema_base'])

                if buy_signal and positions[sym] != 1:
                    positions[sym] = 1
                    msg = (
                        f"🟢 *SNIPER BUY SIGNAL!*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Entry (Close): `${curr['close']:.2f}`\n"
                        f"Timeframe: `{TIMEFRAME}`\n"
                        f"SL: Below Swing Low\n"
                        f"Target: 1:2 to 1:3 RR"
                    )
                    print(msg)
                    send_telegram(msg)

                elif sell_signal and positions[sym] != -1:
                    positions[sym] = -1
                    msg = (
                        f"🔴 *SNIPER SELL SIGNAL!*\n\n"
                        f"Coin: `#{sym}`\n"
                        f"Entry (Close): `${curr['close']:.2f}`\n"
                        f"Timeframe: `{TIMEFRAME}`\n"
                        f"SL: Above Swing High\n"
                        f"Target: 1:2 to 1:3 RR"
                    )
                    print(msg)
                    send_telegram(msg)

                # Mark this closed candle as processed
                last_processed_candle_time[sym] = closed_candle_time

            except Exception as err:
                print(f"Error checking {sym}:", err)

            time.sleep(0.5)

        time.sleep(15)  # Scan every 15 seconds so no closed candle is missed

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()
    run_strategy()
