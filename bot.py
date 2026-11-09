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

TIMEFRAME  = "15m"
LIMIT_BARS = 1000

# Exact TradingView Finalized Inputs
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
        self.wfile.write(b"Trend Liquidity Sniper Bot 24/7 LIVE")

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
    headers = {"User-Agent": "Mozilla/5.0"}

    # 1. Delta Exchange
    res_code = "1D" if timeframe == "1d" else "15"
    sec_per_bar = 86400 if timeframe == "1d" else 900
    safe_delta_limit = min(limit, 500)
    from_time = now - (safe_delta_limit * sec_per_bar)

    delta_url = f"https://api.delta.exchange/v2/chart/history?symbol={delta_sym}&resolution={res_code}&from={from_time}&to={now}"
    try:
        r = requests.get(delta_url, headers=headers, timeout=6)
        if r.status_code == 200:
            data = r.json()
            if data.get("success") and data.get("result") and len(data["result"]) >= 50:
                df = pd.DataFrame(data["result"])
                df = df.sort_values("time").reset_index(drop=True)
                for col in ['open', 'high', 'low', 'close']:
                    df[col] = df[col].astype(float)
                return df
    except Exception:
        pass

    # 2. Binance Vision Fallback
    binance_interval = "1d" if timeframe == "1d" else "15m"
    mirrors = [
        f"https://data-api.binance.vision/api/v3/klines?symbol={binance_sym}&interval={binance_interval}&limit={min(limit, 1000)}",
        f"https://api.binance.com/api/v3/klines?symbol={binance_sym}&interval={binance_interval}&limit={min(limit, 1000)}"
    ]
    for m_url in mirrors:
        try:
            r = requests.get(m_url, headers=headers, timeout=5)
            if r.status_code == 200:
                candles = r.json()
                if isinstance(candles, list) and len(candles) >= 50:
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

    print(">>> Finalized Trend Liquidity Sniper Engine Running 24/7 <<<", flush=True)

    while True:
        for sym_dict in SYMBOLS:
            sym = sym_dict["delta"]
            try:
                # 1. Fetch 15m Candles
                df = fetch_klines(sym_dict, timeframe="15m", limit=LIMIT_BARS)
                if df.empty or len(df) < 150:
                    continue

                closed_candle = df.iloc[-2]
                closed_time = closed_candle['time']

                is_new_candle = (last_processed_candle_time[sym] != closed_time)
                if not is_new_candle and test_alert_done[sym]:
                    continue

                cdf = df.iloc[:-1].copy().reset_index(drop=True)
                n = len(cdf)

                # 2. Daily PDH / PDL
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
                cdf['is_expanded'] = cdf['atr'] >= (cdf['atr_ma'] * 0.95)

                c_range = cdf['high'] - cdf['low']
                c_body = (cdf['close'] - cdf['open']).abs()
                cdf['solid_candle'] = np.where(c_range > 0, (c_body / c_range) >= MIN_BODY, False)
                cdf['ema_base'] = cdf['close'].ewm(span=EMA_LEN, adjust=False).mean()

                closes = cdf['close'].values
                highs  = cdf['high'].values
                lows   = cdf['low'].values
                opens  = cdf['open'].values
                atrs   = cdf['atr'].values

                # 4. Major Swings (20 Bars)
                major_high = np.nan
                major_low = np.nan
                for idx in range(SWING_LEN, n - SWING_LEN):
                    if highs[idx] == np.max(highs[idx - SWING_LEN : idx + SWING_LEN + 1]):
                        major_high = highs[idx]
                    if lows[idx] == np.min(lows[idx - SWING_LEN : idx + SWING_LEN + 1]):
                        major_low = lows[idx]

                # 5. Dynamic Trailing Range Filter (ATR_MULT = 2.4)
                up_trend = np.zeros(n)
                dn_trend = np.zeros(n)
                trend_state = np.zeros(n, dtype=int)

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

                trend_flipped_bull = (trend_state[-1] == 1 and trend_state[-2] == -1)
                trend_flipped_bear = (trend_state[-1] == -1 and trend_state[-2] == 1)

                # 6. Sweep Validation (Last 20 Bars)
                swept_low = any((lows[j] < pdl or (not np.isnan(major_low) and lows[j] < major_low)) for j in range(max(0, n-20), n))
                swept_high = any((highs[j] > pdh or (not np.isnan(major_high) and highs[j] > major_high)) for j in range(max(0, n-20), n))

                # 7. Reversals (For $81.5k Peak & $74k Bottom)
                peak_sell = (highs[-1] > pdh or (not np.isnan(major_high) and highs[-1] > major_high)) and (closes[-1] < opens[-1]) and bool(cdf['solid_candle'].iloc[-1]) and bool(cdf['is_expanded'].iloc[-1])
                trough_buy = (lows[-1] < pdl or (not np.isnan(major_low) and lows[-1] < major_low)) and (closes[-1] > opens[-1]) and bool(cdf['solid_candle'].iloc[-1]) and bool(cdf['is_expanded'].iloc[-1])

                curr_close = closes[-1]
                curr_ema = cdf['ema_base'].iloc[-1]
                is_solid = bool(cdf['solid_candle'].iloc[-1])
                is_exp = bool(cdf['is_expanded'].iloc[-1])

                raw_buy  = (trend_flipped_bull and (swept_low or curr_close > curr_ema) and is_exp and is_solid) or trough_buy
                raw_sell = (trend_flipped_bear and (swept_high or curr_close < curr_ema) and is_exp and is_solid) or peak_sell

                trade = active_trades[sym]

                # Initial Status Ping on Deployment
                if not test_alert_done[sym]:
                    test_alert_done[sym] = True
                    last_processed_candle_time[sym] = closed_time
                    state_str = "BULLISH 🟢" if trend_state[-1] == 1 else "BEARISH 🔴"
                    send_telegram(
                        f"⚡ *TREND LIQUIDITY SNIPER (1:1 TV SYNCED)*\n\n"
                        f"Asset: `#{sym}`\n"
                        f"Price: `${curr_close:.2f}`\n"
                        f"Trend: `{state_str}`\n"
                        f"50 EMA: `${curr_ema:.2f}`\n\n"
                        f"🎯 Parameters: `20, 14, 2.4, 50, 0.60`"
                    )

                # --- EXIT LOGIC (Opposite Flip or Breakout) ---
                if trade["side"] == "BUY":
                    if raw_sell or curr_close < curr_ema:
                        pnl_pts = curr_close - trade["entry_price"]
                        pnl_pct = (pnl_pts / trade["entry_price"]) * 100
                        badge = "🟢 PROFIT" if pnl_pts > 0 else "🔴 LOSS"
                        send_telegram(
                            f"🏁 *TRADE CLOSED (BUY EXIT)*\n\n"
                            f"Asset: `#{sym}`\n"
                            f"Exit: `${curr_close:.2f}`\n"
                            f"Entry: `${trade['entry_price']:.2f}`\n"
                            f"Result: *{badge}*\n"
                            f"PnL: `{pnl_pts:+.2f} USDT` (`{pnl_pct:+.2f}%`)\n"
                            f"Trigger: `Opposite Signal / EMA Filter`"
                        )
                        trade["side"] = None

                elif trade["side"] == "SELL":
                    if raw_buy or curr_close > curr_ema:
                        pnl_pts = trade["entry_price"] - curr_close
                        pnl_pct = (pnl_pts / trade["entry_price"]) * 100
                        badge = "🟢 PROFIT" if pnl_pts > 0 else "🔴 LOSS"
                        send_telegram(
                            f"🏁 *TRADE CLOSED (SELL EXIT)*\n\n"
                            f"Asset: `#{sym}`\n"
                            f"Exit: `${curr_close:.2f}`\n"
                            f"Entry: `${trade['entry_price']:.2f}`\n"
                            f"Result: *{badge}*\n"
                            f"PnL: `{pnl_pts:+.2f} USDT` (`{pnl_pct:+.2f}%`)\n"
                            f"Trigger: `Opposite Signal / EMA Filter`"
                        )
                        trade["side"] = None

                # --- ENTRY LOGIC (Strict Single Direction Lock) ---
                if raw_buy and trade["side"] != "BUY":
                    sl_level = lows[-1] - (atrs[-1] * 0.5)
                    trade["side"] = "BUY"
                    trade["entry_price"] = curr_close
                    trade["entry_time"] = closed_time
                    trade["trailing_sl"] = sl_level

                    send_telegram(
                        f"🟢 *SNIPER BUY ALERT*\n\n"
                        f"Asset: `#{sym}`\n"
                        f"Entry Price: `${curr_close:.2f}`\n"
                        f"Suggested SL: `${sl_level:.2f}`\n"
                        f"Target (1:2): `${curr_close + (curr_close - sl_level) * 2:.2f}`\n"
                        f"Rule: `2.4 ATR Trend Flip / Trough Sweep`"
                    )

                elif raw_sell and trade["side"] != "SELL":
                    sl_level = highs[-1] + (atrs[-1] * 0.5)
                    trade["side"] = "SELL"
                    trade["entry_price"] = curr_close
                    trade["entry_time"] = closed_time
                    trade["trailing_sl"] = sl_level

                    send_telegram(
                        f"🔴 *SNIPER SELL ALERT*\n\n"
                        f"Asset: `#{sym}`\n"
                        f"Entry Price: `${curr_close:.2f}`\n"
                        f"Suggested SL: `${sl_level:.2f}`\n"
                        f"Target (1:2): `${curr_close - (sl_level - curr_close) * 2:.2f}`\n"
                        f"Rule: `2.4 ATR Trend Flip / Peak Sweep`"
                    )

                last_processed_candle_time[sym] = closed_time

            except Exception as err:
                print(f"Error in {sym} loop: {err}", flush=True)

            time.sleep(1)

        time.sleep(10)

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()
    run_strategy()
