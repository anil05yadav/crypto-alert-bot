import time
import requests
import pandas as pd
import numpy as np

# ---------------- CONFIGURATION ----------------
TELEGRAM_BOT_TOKEN = "7891331261:AAGk2XL2_4LqIpBwSHwA-5dn2RTrzUbvAnA"
TELEGRAM_CHAT_ID   = "6470999168"
SYMBOL             = "BTCUSDT"   # Coin symbol (e.g. BTCUSDT, ETHUSDT, SOLUSDT)
TIMEFRAME          = "15m"       # 15 Minute timeframe

SWING_LEN          = 20
ATR_PERIOD         = 14
ATR_MULT           = 2.4
EMA_LEN            = 50
MIN_BODY_RATIO     = 0.60

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

def fetch_binance_klines(symbol, interval, limit=250):
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

def run_strategy():
    position = 0 # 1 for Long, -1 for Short
    print("Sniper Bot Started... Monitoring Binance 24/7.")
    send_telegram(f"🚀 *Crypto Sniper Alert Bot Active!*\nTracking: `{SYMBOL}` on `{TIMEFRAME}`")

    while True:
        try:
            df = fetch_binance_klines(SYMBOL, TIMEFRAME, limit=150)
            
            # Running candle ko chhodkar sirf closed candle consider karenge (Non-repainting)
            df = df.iloc[:-1].copy()

            # 1. ATR & Squeeze
            tr1 = df['high'] - df['low']
            tr2 = (df['high'] - df['close'].shift(1)).abs()
            tr3 = (df['low'] - df['close'].shift(1)).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            df['atr'] = tr.rolling(ATR_PERIOD).mean()
            df['atr_ma'] = df['atr'].rolling(20).mean()
            df['is_expanded'] = df['atr'] >= (df['atr_ma'] * 0.95)

            # 2. Candle Conviction
            candle_range = df['high'] - df['low']
            candle_body  = (df['close'] - df['open']).abs()
            df['solid_candle'] = (candle_body / candle_range) >= MIN_BODY_RATIO

            # 3. EMA Trend Base
            df['ema_base'] = df['close'].ewm(span=EMA_LEN, adjust=False).mean()

            # 4. Dynamic Trailing Trend State
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

            # Signals on the latest closed candle
            curr = df.iloc[-1]
            prev = df.iloc[-2]

            trend_bull = (curr['trend_state'] == 1 and prev['trend_state'] == -1)
            trend_bear = (curr['trend_state'] == -1 and prev['trend_state'] == 1)

            buy_signal  = trend_bull and curr['is_expanded'] and curr['solid_candle'] and (curr['close'] > curr['ema_base'])
            sell_signal = trend_bear and curr['is_expanded'] and curr['solid_candle'] and (curr['close'] < curr['ema_base'])

            # Signal Trigger & Telegram Alert
            if buy_signal and position != 1:
                position = 1
                msg = f"🟢 *SNIPER BUY SIGNAL!*\n\nPair: `{SYMBOL}`\nPrice: `{curr['close']}`\nTimeframe: `{TIMEFRAME}`\nSL: Swing Low\nTarget: 1:3 RR"
                print(msg)
                send_telegram(msg)

            elif sell_signal and position != -1:
                position = -1
                msg = f"🔴 *SNIPER SELL SIGNAL!*\n\nPair: `{SYMBOL}`\nPrice: `{curr['close']}`\nTimeframe: `{TIMEFRAME}`\nSL: Swing High\nTarget: 1:3 RR"
                print(msg)
                send_telegram(msg)

        except Exception as err:
            print("Loop Error:", err)

        # Har 1 minute me check karega
        time.sleep(60)

if __name__ == "__main__":
    run_strategy()
