import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import pandas as pd
import numpy as np

# ---------------- CONFIGURATION ----------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "7891331261:AAGk2XL2_4LqIpBwSHwA-5dn2RTrzUbvAnA")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "6470999168")

# Exact TradingView Watchlist Mapping:
# 1. BTCUSD.P  -> Delta Exchange India (BTCUSD)
# 2. ETHUSDT.P -> Binance Futures (ETHUSDT)
SYMBOLS = [
    {"tv_name": "BTCUSD.P",  "symbol": "BTCUSD",  "exchange": "delta"},
    {"tv_name": "ETHUSDT.P", "symbol": "ETHUSDT", "exchange": "binance"}
]

TIMEFRAME  = "15m"
LIMIT_BARS = 1000

# Exact Locked Pine Script Inputs: Institutional SMC Engine [60-Bar Clean Pro]
# Params: (12, 5, 60, 12, 1.25, 0.50)
SWING_LEN     = 12
IDM_LEN       = 5
ZONE_LIFE     = 60
COOLDOWN      = 12
VOL_MULT      = 1.25
MIN_BODY      = 0.50
GIANT_BAR_PCT = 0.015   # >1.5% single 15m candle -> 50% Pullback Limit Alert

# Smart Risk & Profit Protection Alert Thresholds
MAX_SL_PCT      = 0.012 # 1.2% Max Stop Loss
BREAKEVEN_TRIG  = 0.012 # +1.2% Move -> Breakeven Alert
TRAIL_START_PCT = 0.022 # +2.2% Move -> Trailing Profit Lock Active
TRAIL_GAP_PCT   = 0.008 # 0.8% Trailing Gap from Peak

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Institutional SMC Engine [60-Bar Clean Pro] LIVE")

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
                    if 'volume' in df.columns:
                        df['volume'] = df['volume'].astype(float)
                    else:
                        df['volume'] = 0.0
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
                    for col in ['open', 'high', 'low', 'close', 'volume']:
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

def run_strategy():
    active_trades = {
        s["tv_name"]: {
            "side": None,
            "entry_price": 0.0,
            "sl_level": 0.0,
            "peak_price": 0.0,
            "be_alerted": False,
            "trail_active": False
        } for s in SYMBOLS
    }

    last_processed_candle_time = {s["tv_name"]: None for s in SYMBOLS}
    initialized = {s["tv_name"]: False for s in SYMBOLS}

    print(">>> Institutional SMC Engine [60-Bar Clean Pro] (BTCUSD.P + ETHUSDT.P) LIVE <<<", flush=True)

    while True:
        for sym_cfg in SYMBOLS:
            tv_name = sym_cfg["tv_name"]
            try:
                df = fetch_klines(sym_cfg, timeframe="15m", limit=LIMIT_BARS)
                if df.empty or len(df) < 250:
                    continue

                live_price    = float(df.iloc[-1]['close'])
                closed_candle = df.iloc[-2]
                closed_time   = closed_candle['time']
                trade         = active_trades[tv_name]

                # --------------------------------------------------------------
                # A) LIVE SL / BREAKEVEN / TRAILING PROFIT MONITOR (EVERY 10s)
                # --------------------------------------------------------------
                if initialized[tv_name] and trade["side"] in ("BUY", "SELL"):
                    if trade["side"] == "BUY":
                        if live_price > trade["peak_price"]:
                            trade["peak_price"] = live_price
                        move_pct = (trade["peak_price"] - trade["entry_price"]) / trade["entry_price"]

                        if move_pct >= BREAKEVEN_TRIG and not trade["be_alerted"]:
                            trade["sl_level"] = trade["entry_price"] * 1.001
                            trade["be_alerted"] = True
                            send_telegram(
                                f"🛡️ BREAKEVEN LOCKED (#{tv_name} BUY)\n\n"
                                f"Price moved +1.2%! Move your SL to Entry:\n"
                                f"New Safe SL: ${trade['sl_level']:.2f}"
                            )

                        if move_pct >= TRAIL_START_PCT:
                            trade["trail_active"] = True
                            new_trail = trade["peak_price"] * (1 - TRAIL_GAP_PCT)
                            if new_trail > trade["sl_level"]:
                                trade["sl_level"] = new_trail

                        if live_price <= trade["sl_level"]:
                            pnl_pts = live_price - trade["entry_price"]
                            pnl_pct = (pnl_pts / trade["entry_price"]) * 100
                            reason  = "TRAILING PROFIT LOCK HIT 💰" if trade["trail_active"] else ("BREAKEVEN EXIT 🛡️" if trade["be_alerted"] else "MAX SL HIT 🛑")
                            send_telegram(
                                f"🏁 BUY TRADE CLOSED ({reason})\n\n"
                                f"Asset: #{tv_name}\n"
                                f"Exit: ${live_price:.2f} | Entry: ${trade['entry_price']:.2f}\n"
                                f"Result: {pnl_pts:+.2f} USDT ({pnl_pct:+.2f}%)"
                            )
                            trade["side"] = None

                    elif trade["side"] == "SELL":
                        if live_price < trade["peak_price"]:
                            trade["peak_price"] = live_price
                        move_pct = (trade["entry_price"] - trade["peak_price"]) / trade["entry_price"]

                        if move_pct >= BREAKEVEN_TRIG and not trade["be_alerted"]:
                            trade["sl_level"] = trade["entry_price"] * 0.999
                            trade["be_alerted"] = True
                            send_telegram(
                                f"🛡️ BREAKEVEN LOCKED (#{tv_name} SELL)\n\n"
                                f"Price moved +1.2%! Move your SL to Entry:\n"
                                f"New Safe SL: ${trade['sl_level']:.2f}"
                            )

                        if move_pct >= TRAIL_START_PCT:
                            trade["trail_active"] = True
                            new_trail = trade["peak_price"] * (1 + TRAIL_GAP_PCT)
                            if new_trail < trade["sl_level"]:
                                trade["sl_level"] = new_trail

                        if live_price >= trade["sl_level"]:
                            pnl_pts = trade["entry_price"] - live_price
                            pnl_pct = (pnl_pts / trade["entry_price"]) * 100
                            reason  = "TRAILING PROFIT LOCK HIT 💰" if trade["trail_active"] else ("BREAKEVEN EXIT 🛡️" if trade["be_alerted"] else "MAX SL HIT 🛑")
                            send_telegram(
                                f"🏁 SELL TRADE CLOSED ({reason})\n\n"
                                f"Asset: #{tv_name}\n"
                                f"Exit: ${live_price:.2f} | Entry: ${trade['entry_price']:.2f}\n"
                                f"Result: {pnl_pts:+.2f} USDT ({pnl_pct:+.2f}%)"
                            )
                            trade["side"] = None

                # Skip heavy calculation if no new 15m candle has closed yet
                if initialized[tv_name] and last_processed_candle_time[tv_name] == closed_time:
                    continue

                # --------------------------------------------------------------
                # B) EXACT 1:1 PINE SCRIPT ENGINE [60-BAR CLEAN PRO]
                # --------------------------------------------------------------
                cdf = df.iloc[:-1].copy().reset_index(drop=True)
                n   = len(cdf)

                cdf['macro_ema'] = cdf['close'].ewm(span=200, adjust=False).mean()
                cdf['vol_sma']   = cdf['volume'].rolling(20).mean()

                c_range = cdf['high'] - cdf['low']
                c_body  = (cdf['close'] - cdf['open']).abs()
                cdf['solid_bar']    = np.where(c_range > 0, (c_body / c_range) >= MIN_BODY, False)
                cdf['is_giant_bar'] = np.where(cdf['open'] > 0, (c_range / cdf['open']) >= GIANT_BAR_PCT, False)

                closes    = cdf['close'].values
                highs     = cdf['high'].values
                lows      = cdf['low'].values
                opens     = cdf['open'].values
                volumes   = cdf['volume'].values
                macro_ema = cdf['macro_ema'].values
                vol_sma   = cdf['vol_sma'].values
                solids    = cdf['solid_bar'].values
                giants    = cdf['is_giant_bar'].values

                bsl_level      = np.nan
                ssl_level      = np.nan
                last_idm_high  = np.nan
                last_idm_low   = np.nan
                bull_idm_swept = False
                bear_idm_swept = False

                bull_ob_top = np.nan
                bull_ob_bot = np.nan
                bull_ob_bar = -999

                bear_ob_top = np.nan
                bear_ob_bot = np.nan
                bear_ob_bar = -999

                tv_position         = 0
                last_sig_bar        = -999
                last_sig_price      = np.nan
                prev_sig_price      = np.nan
                buy_triggered_last  = False
                sell_triggered_last = False

                for i in range(2, n):
                    # Major Swing Pivots (swing_len = 12)
                    if i >= SWING_LEN * 2:
                        piv_idx = i - SWING_LEN
                        if highs[piv_idx] == np.max(highs[piv_idx - SWING_LEN : piv_idx + SWING_LEN + 1]):
                            bsl_level = highs[piv_idx]
                        if lows[piv_idx] == np.min(lows[piv_idx - SWING_LEN : piv_idx + SWING_LEN + 1]):
                            ssl_level = lows[piv_idx]

                    # Internal Inducement Pivots (idm_len = 5)
                    if i >= IDM_LEN * 2:
                        idm_idx = i - IDM_LEN
                        if highs[idm_idx] == np.max(highs[idm_idx - IDM_LEN : idm_idx + IDM_LEN + 1]):
                            last_idm_high  = highs[idm_idx]
                            bear_idm_swept = False
                        if lows[idm_idx] == np.min(lows[idm_idx - IDM_LEN : idm_idx + IDM_LEN + 1]):
                            last_idm_low   = lows[idm_idx]
                            bull_idm_swept = False

                    if not np.isnan(last_idm_low) and lows[i] < last_idm_low:
                        bull_idm_swept = True
                    if not np.isnan(last_idm_high) and highs[i] > last_idm_high:
                        bear_idm_swept = True

                    # 60-Bar Dealing Range (Premium / Discount)
                    win_start   = max(0, i - ZONE_LIFE + 1)
                    fib_high    = np.max(highs[win_start : i + 1])
                    fib_low     = np.min(lows[win_start : i + 1])
                    fib_mid     = (fib_high + fib_low) * 0.5
                    in_discount = closes[i] <= fib_mid
                    in_premium  = closes[i] >= fib_mid
                    valid_range = ((fib_high - fib_low) / fib_low >= 0.012) if fib_low > 0 else False

                    # Volume Spike Check
                    has_vol_data = (not np.isnan(vol_sma[i])) and (vol_sma[i] > 0)
                    high_vol     = (volumes[i] >= vol_sma[i] * VOL_MULT or volumes[i - 1] >= vol_sma[i] * VOL_MULT) if has_vol_data else True

                    # Unmitigated Order Block & FVG Engine (60-Bar Memory)
                    bull_fvg = (lows[i] > highs[i - 2]) and (closes[i - 1] > opens[i - 1])
                    bear_fvg = (highs[i] < lows[i - 2]) and (closes[i - 1] < opens[i - 1])

                    if bull_fvg:
                        bull_ob_top = max(highs[i - 2], opens[i - 2])
                        bull_ob_bot = min(lows[i - 2], lows[i - 1])
                        bull_ob_bar = i

                    if bear_fvg:
                        bear_ob_top = max(highs[i - 2], highs[i - 1])
                        bear_ob_bot = min(lows[i - 2], opens[i - 2])
                        bear_ob_bar = i

                    bull_rfvg = (not np.isnan(bear_ob_top)) and ((i - bear_ob_bar) <= ZONE_LIFE) and (closes[i] > bear_ob_top) and (closes[i - 1] <= bear_ob_top)
                    bear_rfvg = (not np.isnan(bull_ob_bot)) and ((i - bull_ob_bar) <= ZONE_LIFE) and (closes[i] < bull_ob_bot) and (closes[i - 1] >= bull_ob_bot)

                    bull_ob_active = (not np.isnan(bull_ob_top)) and (2 <= (i - bull_ob_bar) <= ZONE_LIFE)
                    bear_ob_active = (not np.isnan(bear_ob_bot)) and (2 <= (i - bear_ob_bar) <= ZONE_LIFE)

                    tapped_bull_ob = bull_ob_active and (lows[i] <= bull_ob_top) and (closes[i] >= bull_ob_bot)
                    tapped_bear_ob = bear_ob_active and (highs[i] >= bear_ob_bot) and (closes[i] <= bear_ob_top)

                    ssl_sweep_buy  = (not np.isnan(ssl_level)) and (lows[i] < ssl_level) and (closes[i] > ssl_level) and (closes[i] > opens[i])
                    bsl_sweep_sell = (not np.isnan(bsl_level)) and (highs[i] > bsl_level) and (closes[i] < bsl_level) and (closes[i] < opens[i])

                    # Clean Strict Alternating Triggers
                    can_fire      = ((i - last_sig_bar) >= COOLDOWN) and valid_range
                    ob_buy_valid  = (bull_idm_swept or in_discount) if closes[i] > macro_ema[i] else (bull_idm_swept and high_vol)
                    ob_sell_valid = (bear_idm_swept or in_premium)  if closes[i] < macro_ema[i] else (bear_idm_swept and high_vol)

                    buy_setup_ob   = tapped_bull_ob and ob_buy_valid and (closes[i] > opens[i]) and solids[i]
                    buy_setup_liq  = ssl_sweep_buy and solids[i] and high_vol
                    buy_setup_rfvg = bull_rfvg and high_vol and solids[i]

                    sell_setup_ob   = tapped_bear_ob and ob_sell_valid and (closes[i] < opens[i]) and solids[i]
                    sell_setup_liq  = bsl_sweep_sell and solids[i] and high_vol
                    sell_setup_rfvg = bear_rfvg and high_vol and solids[i]

                    b_cond = (tv_position != 1)  and can_fire and (buy_setup_ob or buy_setup_liq or buy_setup_rfvg)
                    s_cond = (tv_position != -1) and can_fire and (sell_setup_ob or sell_setup_liq or sell_setup_rfvg)

                    if b_cond:
                        if i == n - 1:
                            prev_sig_price = last_sig_price
                        tv_position    = 1
                        last_sig_bar   = i
                        last_sig_price = closes[i]
                        bull_idm_swept = False

                    if s_cond:
                        if i == n - 1:
                            prev_sig_price = last_sig_price
                        tv_position    = -1
                        last_sig_bar   = i
                        last_sig_price = closes[i]
                        bear_idm_swept = False

                    if i == n - 1:
                        buy_triggered_last  = b_cond
                        sell_triggered_last = s_cond

                curr_close = closes[-1]
                is_giant   = bool(giants[-1])

                # Startup Sync
                if not initialized[tv_name]:
                    initialized[tv_name] = True
                    last_processed_candle_time[tv_name] = closed_time
                    if tv_position == 1:
                        trade["side"] = "BUY"
                        trade["entry_price"] = last_sig_price
                        trade["peak_price"]  = last_sig_price
                        trade["sl_level"]    = last_sig_price * (1 - MAX_SL_PCT)
                    elif tv_position == -1:
                        trade["side"] = "SELL"
                        trade["entry_price"] = last_sig_price
                        trade["peak_price"]  = last_sig_price
                        trade["sl_level"]    = last_sig_price * (1 + MAX_SL_PCT)

                    state_str = "BUY (LONG) 🟢" if tv_position == 1 else ("SELL (SHORT) 🔴" if tv_position == -1 else "NEUTRAL")
                    sig_p_str = f"${last_sig_price:.2f}" if not np.isnan(last_sig_price) else "N/A"
                    send_telegram(
                        f"⚡ INSTITUTIONAL SMC ENGINE [60-BAR CLEAN PRO] SYNCED\n\n"
                        f"Asset: #{tv_name} ({sym_cfg['exchange'].upper()})\n"
                        f"Price: ${curr_close:.2f}\n"
                        f"Active TV Position: {state_str}\n"
                        f"Last Signal Price: {sig_p_str}\n"
                        f"Params: 12, 5, 60, 12, 1.25, 0.50"
                    )
                    if not (buy_triggered_last or sell_triggered_last):
                        continue

                # --------------------------------------------------------------
                # C) NEW CONFIRMED SIGNAL ALERTS (EXACT TRADINGVIEW ARROWS)
                # --------------------------------------------------------------
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

                    mid_pullback = (highs[-1] + lows[-1]) * 0.5
                    entry_rec    = mid_pullback if is_giant else curr_close
                    sl_level     = entry_rec * (1 - MAX_SL_PCT)
                    tp_level     = entry_rec * (1 + TRAIL_START_PCT)

                    trade["side"]         = "BUY"
                    trade["entry_price"]  = entry_rec
                    trade["peak_price"]   = curr_close
                    trade["sl_level"]     = sl_level
                    trade["be_alerted"]   = False
                    trade["trail_active"] = False

                    entry_mode = f"BUY (50% PULLBACK LIMIT @ ${mid_pullback:.2f})" if is_giant else f"BUY (MARKET @ ${curr_close:.2f})"
                    send_telegram(
                        f"🟢 INSTITUTIONAL SMC BUY ALERT\n\n"
                        f"Asset: #{tv_name}\n"
                        f"Action: {entry_mode}\n"
                        f"Max SL (1.2%): ${sl_level:.2f}\n"
                        f"Breakeven Shift (+1.2%): ${entry_rec * (1 + BREAKEVEN_TRIG):.2f}\n"
                        f"Trail Lock Start (+2.2%): ${tp_level:.2f}"
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

                    mid_pullback = (highs[-1] + lows[-1]) * 0.5
                    entry_rec    = mid_pullback if is_giant else curr_close
                    sl_level     = entry_rec * (1 + MAX_SL_PCT)
                    tp_level     = entry_rec * (1 - TRAIL_START_PCT)

                    trade["side"]         = "SELL"
                    trade["entry_price"]  = entry_rec
                    trade["peak_price"]   = curr_close
                    trade["sl_level"]     = sl_level
                    trade["be_alerted"]   = False
                    trade["trail_active"] = False

                    entry_mode = f"SELL (50% PULLBACK LIMIT @ ${mid_pullback:.2f})" if is_giant else f"SELL (MARKET @ ${curr_close:.2f})"
                    send_telegram(
                        f"🔴 INSTITUTIONAL SMC SELL ALERT\n\n"
                        f"Asset: #{tv_name}\n"
                        f"Action: {entry_mode}\n"
                        f"Max SL (1.2%): ${sl_level:.2f}\n"
                        f"Breakeven Shift (+1.2%): ${entry_rec * (1 - BREAKEVEN_TRIG):.2f}\n"
                        f"Trail Lock Start (+2.2%): ${tp_level:.2f}"
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
