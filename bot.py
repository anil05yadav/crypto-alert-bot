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

# Exact TradingView Watchlist Mapping:
# 1. BTCUSD.P  -> Delta Exchange India (BTCUSD)
# 2. ETHUSDT.P -> Binance Futures (ETHUSDT)
SYMBOLS = [
    {"tv_name": "BTCUSD.P",  "symbol": "BTCUSD",  "exchange": "delta"},
    {"tv_name": "ETHUSDT.P", "symbol": "ETHUSDT", "exchange": "binance"}
]

TIMEFRAME  = "15m"
LIMIT_BARS = 1000

# Exact Pine Script Inputs: (20, 50, 200, 14, 2.4, 10, 0.55, 12)
SWING_LEN  = 20
FAST_EMA   = 50
EMA_LEN    = 200
ATR_PERIOD = 14
ATR_MULT   = 2.4
MIN_BARS   = 10
MIN_BODY   = 0.55
FVG_LOOK   = 12

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"SMC Pullback & Reversal Pro LIVE")

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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"Telegram status: {r.status_code}", flush=True)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def fetch_delta_klines(symbol, timeframe="15m", limit=1000):
    headers     = {"User-Agent": "Mozilla/5.0"}
    now         = int(time.time())
    res_code    = "1D" if timeframe == "1d" else "15"
    sec_per_bar = 86400 if timeframe == "1d" else 900
    from_time   = now - (limit * sec_per_bar)

    endpoints = [
        f"https://api.india.delta.exchange/v2/chart/history?symbol={symbol}&resolution={res_code}&from={from_time}&to={now}",
        f"https://api.delta.exchange/v2/chart/history?symbol={symbol}&resolution={res_code}&from={from_time}&to={now}"
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if data.get("success") and data.get("result"):
                    df = pd.DataFrame(data["result"]).sort_values("time").drop_duplicates("time").reset_index(drop=True)
                    for col in ['open', 'high', 'low', 'close']:
                        df[col] = df[col].astype(float)
                    # Convert Delta seconds timestamp to milliseconds for consistency
                    if df['time'].iloc[-1] < 1e11:
                        df['time'] = df['time'] * 1000
                    return df
        except Exception:
            continue
    return pd.DataFrame()

def fetch_binance_klines(symbol, timeframe="15m", limit=1000):
    headers          = {"User-Agent": "Mozilla/5.0"}
    binance_interval = "1d" if timeframe == "1d" else "15m"
    mirrors = [
        f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={binance_interval}&limit={min(limit, 1000)}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={binance_interval}&limit={min(limit, 1000)}",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={binance_interval}&limit={min(limit, 1000)}"
    ]
    for m_url in mirrors:
        try:
            r = requests.get(m_url, headers=headers, timeout=8)
            if r.status_code == 200:
                candles = r.json()
                if isinstance(candles, list) and len(candles) >= 5:
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

def fetch_klines(sym_cfg, timeframe="15m", limit=1000):
    if sym_cfg["exchange"] == "delta":
        df = fetch_delta_klines(sym_cfg["symbol"], timeframe, limit)
        if not df.empty:
            return df
        return fetch_binance_klines("BTCUSDT", timeframe, limit)
    else:
        df = fetch_binance_klines(sym_cfg["symbol"], timeframe, limit)
        if not df.empty:
            return df
        return fetch_delta_klines("ETHUSD", timeframe, limit)

def rma(series, length):
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()

def calc_rsi(series, length=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def build_daily_pdh_pdl(cdf, daily_df):
    """Exact replica of Pine Script request.security(..., 'D', [high[1], low[1]], lookahead=on)"""
    cdf['date_str'] = pd.to_datetime(cdf['time'], unit='ms', utc=True).dt.strftime('%Y-%m-%d')
    if not daily_df.empty and len(daily_df) >= 2:
        ddf = daily_df.copy()
        ddf['date_str'] = pd.to_datetime(ddf['time'], unit='ms', utc=True).dt.strftime('%Y-%m-%d')
        ddf['prev_high'] = ddf['high'].shift(1)
        ddf['prev_low']  = ddf['low'].shift(1)
        pdh_map = dict(zip(ddf['date_str'], ddf['prev_high']))
        pdl_map = dict(zip(ddf['date_str'], ddf['prev_low']))
        pdh_arr = cdf['date_str'].map(pdh_map).ffill().bfill().values
        pdl_arr = cdf['date_str'].map(pdl_map).ffill().bfill().values
    else:
        pdh_arr = np.full(len(cdf), cdf['high'].max())
        pdl_arr = np.full(len(cdf), cdf['low'].min())
    return pdh_arr, pdl_arr

def run_strategy():
    active_trades = {
        s["tv_name"]: {
            "side": None,
            "entry_price": 0.0,
            "sl_level": 0.0
        } for s in SYMBOLS
    }

    last_processed_candle_time = {s["tv_name"]: None for s in SYMBOLS}
    initialized = {s["tv_name"]: False for s in SYMBOLS}

    print(">>> SMC Pullback & Reversal Pro (BTCUSD.P Delta + ETHUSDT.P Binance) LIVE <<<", flush=True)

    while True:
        for sym_cfg in SYMBOLS:
            tv_name = sym_cfg["tv_name"]
            try:
                df = fetch_klines(sym_cfg, timeframe="15m", limit=LIMIT_BARS)
                if df.empty or len(df) < 250:
                    continue

                closed_candle = df.iloc[-2]
                closed_time   = closed_candle['time']

                if initialized[tv_name] and last_processed_candle_time[tv_name] == closed_time:
                    continue

                cdf = df.iloc[:-1].copy().reset_index(drop=True)
                n   = len(cdf)

                # Date-aligned PDH / PDL (Exact Pine Script request.security match)
                daily_df = fetch_klines(sym_cfg, timeframe="1d", limit=30)
                pdh_arr, pdl_arr = build_daily_pdh_pdl(cdf, daily_df)

                # Indicators (Exact TradingView Match)
                cdf['macro_ema'] = cdf['close'].ewm(span=EMA_LEN, adjust=False).mean()
                cdf['value_ema'] = cdf['close'].ewm(span=FAST_EMA, adjust=False).mean()
                cdf['rsi']       = calc_rsi(cdf['close'], 14)

                tr1 = cdf['high'] - cdf['low']
                tr2 = (cdf['high'] - cdf['close'].shift(1)).abs()
                tr3 = (cdf['low'] - cdf['close'].shift(1)).abs()
                tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                cdf['atr'] = rma(tr, ATR_PERIOD)

                c_range = cdf['high'] - cdf['low']
                c_body  = (cdf['close'] - cdf['open']).abs()
                cdf['solid_candle'] = np.where(c_range > 0, (c_body / c_range) >= MIN_BODY, False)

                closes    = cdf['close'].values
                highs     = cdf['high'].values
                lows      = cdf['low'].values
                opens     = cdf['open'].values
                macro_ema = cdf['macro_ema'].values
                value_ema = cdf['value_ema'].values
                rsis      = cdf['rsi'].values
                atrs      = cdf['atr'].values
                solids    = cdf['solid_candle'].values

                # Exact Historical Simulation Bar-by-Bar
                up_trend        = np.zeros(n)
                dn_trend        = np.zeros(n)
                trend_state     = np.zeros(n, dtype=int)

                tv_position     = 0
                last_signal_bar = 0
                last_sig_price  = np.nan
                major_high      = np.nan
                major_low       = np.nan

                last_bull_fvg_bot = np.nan
                last_bull_fvg_bar = -999
                last_bear_fvg_top = np.nan
                last_bear_fvg_bar = -999

                prev_sig_price      = np.nan
                buy_triggered_last  = False
                sell_triggered_last = False

                for i in range(2, n):
                    # Lagged Swing Pivots: ta.pivothigh(high, 20, 20)
                    if i >= SWING_LEN * 2:
                        piv_idx = i - SWING_LEN
                        if highs[piv_idx] == np.max(highs[piv_idx - SWING_LEN : piv_idx + SWING_LEN + 1]):
                            major_high = highs[piv_idx]
                        if lows[piv_idx] == np.min(lows[piv_idx - SWING_LEN : piv_idx + SWING_LEN + 1]):
                            major_low = lows[piv_idx]

                    # 3-Candle FVG Detection
                    bull_fvg_new = (lows[i] > highs[i-2]) and (closes[i-1] > opens[i-1])
                    bear_fvg_new = (highs[i] < lows[i-2]) and (closes[i-1] < opens[i-1])

                    if bull_fvg_new:
                        last_bull_fvg_bot = highs[i-2]
                        last_bull_fvg_bar = i
                    if bear_fvg_new:
                        last_bear_fvg_top = lows[i-2]
                        last_bear_fvg_bar = i

                    has_bull_fvg = (i - last_bull_fvg_bar) <= FVG_LOOK
                    has_bear_fvg = (i - last_bear_fvg_bar) <= FVG_LOOK

                    # Inversion FVG (IFVG)
                    bear_ifvg = (not np.isnan(last_bull_fvg_bot)) and ((i - last_bull_fvg_bar) <= FVG_LOOK * 2) and (closes[i] < last_bull_fvg_bot) and (closes[i-1] >= last_bull_fvg_bot)
                    bull_ifvg = (not np.isnan(last_bear_fvg_top)) and ((i - last_bear_fvg_bar) <= FVG_LOOK * 2) and (closes[i] > last_bear_fvg_top) and (closes[i-1] <= last_bear_fvg_top)

                    # Pullback Zone & OB Rejection
                    touched_res_zone = (highs[i] >= value_ema[i] or highs[i] >= macro_ema[i]) and (closes[i] < value_ema[i])
                    touched_sup_zone = (lows[i] <= value_ema[i] or lows[i] <= macro_ema[i]) and (closes[i] > value_ema[i])

                    bear_ob_reject = (highs[i] > pdh_arr[i] or (not np.isnan(major_high) and highs[i] > major_high) or touched_res_zone) and (closes[i] < opens[i]) and solids[i]
                    bull_ob_reject = (lows[i] < pdl_arr[i] or (not np.isnan(major_low) and lows[i] < major_low) or touched_sup_zone) and (closes[i] > opens[i]) and solids[i]

                    # Trailing Stop Engine
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

                    bars_since_last    = i - last_signal_bar
                    price_moved_enough = True if np.isnan(last_sig_price) else (abs(closes[i] - last_sig_price) / last_sig_price) >= 0.015

                    if tv_position == 1 and trend_state[i] == -1 and bars_since_last >= 16 and price_moved_enough:
                        tv_position = 0
                    if tv_position == -1 and trend_state[i] == 1 and bars_since_last >= 16 and price_moved_enough:
                        tv_position = 0

                    macro_bull = closes[i] > (macro_ema[i] * 1.002)
                    macro_bear = closes[i] < (macro_ema[i] * 0.998)

                    trend_sell = macro_bear and bear_ob_reject and (has_bear_fvg or bear_ifvg)
                    trend_buy  = macro_bull and bull_ob_reject and (has_bull_fvg or bull_ifvg)

                    rsi_win           = rsis[max(0, i-14):i+1]
                    recent_oversold   = np.nanmin(rsi_win) < 32 if len(rsi_win) > 0 else False
                    recent_overbought = np.nanmax(rsi_win) > 68 if len(rsi_win) > 0 else False

                    bounce_buy  = macro_bear and recent_oversold   and (closes[i] > value_ema[i]) and (closes[i-1] > value_ema[i-1]) and (has_bull_fvg or bull_ifvg) and solids[i]
                    bounce_sell = macro_bull and recent_overbought and (closes[i] < value_ema[i]) and (closes[i-1] < value_ema[i-1]) and (has_bear_fvg or bear_ifvg) and solids[i]

                    b_cond = (tv_position != 1)  and (trend_buy or bounce_buy)   and (bars_since_last >= MIN_BARS)
                    s_cond = (tv_position != -1) and (trend_sell or bounce_sell) and (bars_since_last >= MIN_BARS)

                    if b_cond:
                        if i == n - 1:
                            prev_sig_price = last_sig_price
                        tv_position     = 1
                        last_signal_bar = i
                        last_sig_price  = closes[i]
                    if s_cond:
                        if i == n - 1:
                            prev_sig_price = last_sig_price
                        tv_position     = -1
                        last_signal_bar = i
                        last_sig_price  = closes[i]

                    if i == n - 1:
                        buy_triggered_last  = b_cond
                        sell_triggered_last = s_cond

                curr_close = closes[-1]
                trade      = active_trades[tv_name]

                # Startup Sync (Never skips if latest candle just triggered a signal!)
                if not initialized[tv_name]:
                    initialized[tv_name] = True
                    last_processed_candle_time[tv_name] = closed_time
                    if tv_position == 1:
                        trade["side"] = "BUY"
                        trade["entry_price"] = last_sig_price
                    elif tv_position == -1:
                        trade["side"] = "SELL"
                        trade["entry_price"] = last_sig_price

                    state_str = "BUY (LONG) 🟢" if tv_position == 1 else ("SELL (SHORT) 🔴" if tv_position == -1 else "NEUTRAL")
                    sig_p_str = f"${last_sig_price:.2f}" if not np.isnan(last_sig_price) else "N/A"
                    send_telegram(
                        f"⚡ SMC PULLBACK & REVERSAL PRO SYNCED\n\n"
                        f"Asset: #{tv_name} ({sym_cfg['exchange'].upper()})\n"
                        f"Price: ${curr_close:.2f}\n"
                        f"Active TV Position: {state_str}\n"
                        f"Last Signal Price: {sig_p_str}\n"
                        f"Params: 20, 50, 200, 14, 2.4, 10, 0.55, 12"
                    )
                    if not (buy_triggered_last or sell_triggered_last):
                        continue

                if buy_triggered_last:
                    prev_entry = trade["entry_price"] if trade["side"] == "SELL" and trade["entry_price"] > 0 else prev_sig_price
                    if not np.isnan(prev_entry) and prev_entry > 0:
                        pnl_pts = prev_entry - curr_close
                        pnl_pct = (pnl_pts / prev_entry) * 100
                        badge = "🟢 PROFIT" if pnl_pts > 0 else "🔴 LOSS"
                        send_telegram(
                            f"🏁 SELL CLOSED ON REVERSAL\n\n"
                            f"Asset: #{tv_name}\n"
                            f"Exit: ${curr_close:.2f} | Entry: ${prev_entry:.2f}\n"
                            f"Result: {badge} ({pnl_pts:+.2f} USDT / {pnl_pct:+.2f}%)"
                        )

                    sl_level = lows[-1] - (atrs[-1] * 0.5)
                    trade["side"] = "BUY"
                    trade["entry_price"] = curr_close
                    trade["sl_level"] = sl_level

                    send_telegram(
                        f"🟢 SMC SNIPER BUY ALERT\n\n"
                        f"Asset: #{tv_name}\n"
                        f"Entry Price: ${curr_close:.2f}\n"
                        f"Suggested SL: ${sl_level:.2f}\n"
                        f"Target (1:2): ${curr_close + (curr_close - sl_level) * 2:.2f}"
                    )

                elif sell_triggered_last:
                    prev_entry = trade["entry_price"] if trade["side"] == "BUY" and trade["entry_price"] > 0 else prev_sig_price
                    if not np.isnan(prev_entry) and prev_entry > 0:
                        pnl_pts = curr_close - prev_entry
                        pnl_pct = (pnl_pts / prev_entry) * 100
                        badge = "🟢 PROFIT" if pnl_pts > 0 else "🔴 LOSS"
                        send_telegram(
                            f"🏁 BUY CLOSED ON REVERSAL\n\n"
                            f"Asset: #{tv_name}\n"
                            f"Exit: ${curr_close:.2f} | Entry: ${prev_entry:.2f}\n"
                            f"Result: {badge} ({pnl_pts:+.2f} USDT / {pnl_pct:+.2f}%)"
                        )

                    sl_level = highs[-1] + (atrs[-1] * 0.5)
                    trade["side"] = "SELL"
                    trade["entry_price"] = curr_close
                    trade["sl_level"] = sl_level

                    send_telegram(
                        f"🔴 SMC SNIPER SELL ALERT\n\n"
                        f"Asset: #{tv_name}\n"
                        f"Entry Price: ${curr_close:.2f}\n"
                        f"Suggested SL: ${sl_level:.2f}\n"
                        f"Target (1:2): ${curr_close - (sl_level - curr_close) * 2:.2f}"
                    )

                last_processed_candle_time[tv_name] = closed_time

            except Exception as err:
                print(f"Error in {tv_name} loop: {err}", flush=True)

            time.sleep(1)

        time.sleep(10)

if __name__ == "__main__":
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()
    run_strategy()
