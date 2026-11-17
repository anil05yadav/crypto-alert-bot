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

# Exact Finalized TradingView Parameters (20, 14, 2.4, 5, 50, 0.60)
SWING_LEN  = 20
ATR_PERIOD = 14
ATR_MULT   = 2.4
MIN_BARS   = 5
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
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def fetch_klines(sym_dict, timeframe="15m", limit=1000):
    binance_sym = sym_dict["binance"]
    delta_sym   = sym_dict["delta"]
    headers     = {"User-Agent": "Mozilla/5.0"}

    # 1. Binance Futures / Spot Mirrors
    binance_interval = "1d" if timeframe == "1d" else "15m"
    mirrors = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol={binance_sym}&interval={binance_interval}&limit={min(limit, 1000)}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={binance_sym}&interval={binance_interval}&limit={min(limit, 1000)}",
        f"https://api.binance.com/api/v3/klines?symbol={binance_sym}&interval={binance_interval}&limit={min(limit, 1000)}"
    ]
    for m_url in mirrors:
        try:
            r = requests.get(m_url, headers=headers, timeout=5)
            if r.status_code == 200:
                candles = r.json()
                if isinstance(candles, list) and (len(candles) >= 150 or (timeframe == "1d" and len(candles) >= 3)):
                    df = pd.DataFrame(candles, columns=[
                        'time', 'open', 'high', 'low', 'close', 'volume',
                        'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'
                    ])
                    for col in ['open', 'high', 'low', 'close']:
                        df[col] = df[col].astype(float)
                    return df
        except Exception:
            continue

    # 2. Delta Exchange Fallback
    now = int(time.time())
    res_code = "1D" if timeframe == "1d" else "15"
    sec_per_bar = 86400 if timeframe == "1d" else 900
    from_time = now - (min(limit, 500) * sec_per_bar)
    delta_url = f"https://api.delta.exchange/v2/chart/history?symbol={delta_sym}&resolution={res_code}&from={from_time}&to={now}"
    try:
        r = requests.get(delta_url, headers=headers, timeout=6)
        if r.status_code == 200:
            data = r.json()
            if data.get("success") and data.get("result"):
                df = pd.DataFrame(data["result"]).sort_values("time").reset_index(drop=True)
                for col in ['open', 'high', 'low', 'close']:
                    df[col] = df[col].astype(float)
                return df
    except Exception:
        pass

    return pd.DataFrame()

def rma(series, length):
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def run_strategy():
    active_trades = {
        s["delta"]: {
            "side": None,
            "entry_price": 0.0,
            "sl_level": 0.0
        } for s in SYMBOLS
    }

    last_processed_candle_time = {s["delta"]: None for s in SYMBOLS}
    initialized = {s["delta"]: False for s in SYMBOLS}

    print(">>> Finalized 1:1 Trend Liquidity Sniper Engine Running <<<", flush=True)

    while True:
        for sym_dict in SYMBOLS:
            sym = sym_dict["delta"]
            try:
                df = fetch_klines(sym_dict, timeframe="15m", limit=LIMIT_BARS)
                if df.empty or len(df) < 150:
                    continue

                closed_candle = df.iloc[-2]
                closed_time   = closed_candle['time']

                if initialized[sym] and last_processed_candle_time[sym] == closed_time:
                    continue

                cdf = df.iloc[:-1].copy().reset_index(drop=True)
                n   = len(cdf)

                # Daily PDH / PDL
                daily_df = fetch_klines(sym_dict, timeframe="1d", limit=5)
                if not daily_df.empty and len(daily_df) >= 2:
                    pdh = daily_df.iloc[-2]['high']
                    pdl = daily_df.iloc[-2]['low']
                else:
                    pdh = cdf['high'].max()
                    pdl = cdf['low'].min()

                # ATR & Volatility Expansion (0.95 Multiplier)
                tr1 = cdf['high'] - cdf['low']
                tr2 = (cdf['high'] - cdf['close'].shift(1)).abs()
                tr3 = (cdf['low'] - cdf['close'].shift(1)).abs()
                tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

                cdf['atr']         = rma(tr, ATR_PERIOD)
                cdf['atr_ma']      = cdf['atr'].rolling(20).mean()
                cdf['is_expanded'] = cdf['atr'] >= (cdf['atr_ma'] * 0.95)

                c_range = cdf['high'] - cdf['low']
                c_body  = (cdf['close'] - cdf['open']).abs()
                cdf['solid_candle'] = np.where(c_range > 0, (c_body / c_range) >= MIN_BODY, False)
                cdf['ema_base']     = cdf['close'].ewm(span=EMA_LEN, adjust=False).mean()

                closes = cdf['close'].values
                highs  = cdf['high'].values
                lows   = cdf['low'].values
                opens  = cdf['open'].values
                atrs   = cdf['atr'].values
                emas   = cdf['ema_base'].values
                solids = cdf['solid_candle'].values
                exps   = cdf['is_expanded'].values

                # Exact Historical Simulation Bar-by-Bar
                up_trend        = np.zeros(n)
                dn_trend        = np.zeros(n)
                trend_state     = np.zeros(n, dtype=int)
                tv_position     = 0
                last_signal_bar = -999
                last_sig_price  = 0.0
                major_high      = np.nan
                major_low       = np.nan

                buy_triggered_last  = False
                sell_triggered_last = False

                for i in range(1, n):
                    if i >= SWING_LEN * 2:
                        piv_idx = i - SWING_LEN
                        if highs[piv_idx] == np.max(highs[piv_idx - SWING_LEN : piv_idx + SWING_LEN + 1]):
                            major_high = highs[piv_idx]
                        if lows[piv_idx] == np.min(lows[piv_idx - SWING_LEN : piv_idx + SWING_LEN + 1]):
                            major_low = lows[piv_idx]

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

                    t_flip_bull = (trend_state[i] == 1 and trend_state[i-1] == -1)
                    t_flip_bear = (trend_state[i] == -1 and trend_state[i-1] == 1)

                    p_sell = (highs[i] > pdh or (not np.isnan(major_high) and highs[i] > major_high)) and (closes[i] < opens[i]) and solids[i] and exps[i]
                    t_buy  = (lows[i] < pdl or (not np.isnan(major_low) and lows[i] < major_low)) and (closes[i] > opens[i]) and solids[i] and exps[i]

                    r_buy  = (t_flip_bull and closes[i] > emas[i] and exps[i]) or t_buy
                    r_sell = (t_flip_bear and closes[i] < emas[i] and exps[i]) or p_sell

                    bars_since_last = i - last_signal_bar

                    b_cond = (tv_position != 1)  and r_buy  and (bars_since_last >= MIN_BARS)
                    s_cond = (tv_position != -1) and r_sell and (bars_since_last >= MIN_BARS)

                    if b_cond:
                        tv_position     = 1
                        last_signal_bar = i
                        last_sig_price  = closes[i]
                    if s_cond:
                        tv_position     = -1
                        last_signal_bar = i
                        last_sig_price  = closes[i]

                    if i == n - 1:
                        buy_triggered_last  = b_cond
                        sell_triggered_last = s_cond

                curr_close = closes[-1]
                curr_ema   = emas[-1]
                trade      = active_trades[sym]

                if not initialized[sym]:
                    initialized[sym] = True
                    last_processed_candle_time[sym] = closed_time
                    if tv_position == 1:
                        trade["side"] = "BUY"
                        trade["entry_price"] = last_sig_price
                    elif tv_position == -1:
                        trade["side"] = "SELL"
                        trade["entry_price"] = last_sig_price

                    state_str = "BUY (LONG) 🟢" if tv_position == 1 else ("SELL (SHORT) 🔴" if tv_position == -1 else "NEUTRAL")
                    send_telegram(
                        f"⚡ *SNIPER ENGINE 1:1 FINAL SYNCED*\n\n"
                        f"Asset: `#{sym}`\n"
                        f"Price: `${curr_close:.2f}`\n"
                        f"Active TV Position: *{state_str}*\n"
                        f"Last Signal Price: `${last_sig_price:.2f}`\n"
                        f"Params: `20, 14, 2.4, 5, 50, 0.60`"
                    )
                    continue

                if buy_triggered_last:
                    if trade["side"] == "SELL" and trade["entry_price"] > 0:
                        pnl_pts = trade["entry_price"] - curr_close
                        pnl_pct = (pnl_pts / trade["entry_price"]) * 100
                        badge = "🟢 PROFIT" if pnl_pts > 0 else "🔴 LOSS"
                        send_telegram(
                            f"🏁 *SELL CLOSED ON REVERSAL*\n\n"
                            f"Asset: `#{sym}`\n"
                            f"Exit: `${curr_close:.2f}` | Entry: `${trade['entry_price']:.2f}`\n"
                            f"Result: *{badge}* (`{pnl_pts:+.2f} USDT` / `{pnl_pct:+.2f}%`)"
                        )

                    sl_level = lows[-1] - (atrs[-1] * 0.5)
                    trade["side"] = "BUY"
                    trade["entry_price"] = curr_close
                    trade["sl_level"] = sl_level

                    send_telegram(
                        f"🟢 *SNIPER BUY ALERT (TV CONFIRMED)*\n\n"
                        f"Asset: `#{sym}`\n"
                        f"Entry Price: `${curr_close:.2f}`\n"
                        f"Suggested SL: `${sl_level:.2f}`\n"
                        f"Target (1:2): `${curr_close + (curr_close - sl_level) * 2:.2f}`"
                    )

                elif sell_triggered_last:
                    if trade["side"] == "BUY" and trade["entry_price"] > 0:
                        pnl_pts = curr_close - trade["entry_price"]
                        pnl_pct = (pnl_pts / trade["entry_price"]) * 100
                        badge = "🟢 PROFIT" if pnl_pts > 0 else "🔴 LOSS"
                        send_telegram(
                            f"🏁 *BUY CLOSED ON REVERSAL*\n\n"
                            f"Asset: `#{sym}`\n"
                            f"Exit: `${curr_close:.2f}` | Entry: `${trade['entry_price']:.2f}`\n"
                            f"Result: *{badge}* (`{pnl_pts:+.2f} USDT` / `{pnl_pct:+.2f}%`)"
                        )

                    sl_level = highs[-1] + (atrs[-1] * 0.5)
                    trade["side"] = "SELL"
                    trade["entry_price"] = curr_close
                    trade["sl_level"] = sl_level

                    send_telegram(
                        f"🔴 *SNIPER SELL ALERT (TV CONFIRMED)*\n\n"
                        f"Asset: `#{sym}`\n"
                        f"Entry Price: `${curr_close:.2f}`\n"
                        f"Suggested SL: `${sl_level:.2f}`\n"
                        f"Target (1:2): `${curr_close - (sl_level - curr_close) * 2:.2f}`"
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
