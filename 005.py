"""
Grid Strategy + Signal Filter — Live Engine (Vantage MT5 Direct)
=================================================================

SETUP:
1. Vantage MT5 installed and logged in
2. pip install MetaTrader5 pandas numpy schedule
3. Fill in CONFIG below
4. Enable Algo Trading in MT5 (green triangle button)
5. Run: python grid_strategy_live.py

# ═══════════════════════════════════════════════════════════════
# PARAMETERS
# ═══════════════════════════════════════════════════════════════
#
#   FIXED_LOT            = 0.05
#   GRID_LOT_MULTIPLIERS = [2, 4]
#       Entry=0.05  Grid1=0.10  Grid2=0.20  Max=0.35 lots
#
#   SINGLE_TRADE_TP_PTS  = 10   (min pts for no-grid exit)
#   TARGET_PROFIT        = 50   (min $ for Grid 1 exit)
#   TARGET_PROFIT_G2     = 20   (min $ for Grid 2 exit)
#   DAILY_SL_USD         = 3000 (fixed $ daily stop loss)
#   COOLDOWN_BARS        = 2    (wait 30 mins after exit)
#   GRID_STEP            = 15   (grid spacing in pts)
#   MIN_CONFIDENCE       = 45   (minimum signal score % to enter)
#
# ═══════════════════════════════════════════════════════════════
# SIGNAL FILTER — same as backtest
# ═══════════════════════════════════════════════════════════════
#
#   Scores each candle 0-95% across 5 layers:
#     1. EMA trend alignment (EMA50 / EMA200)
#     2. Candlestick patterns (Engulfing, Pin Bar, Stars, etc.)
#     3. RSI overbought / oversold
#     4. Support & Resistance proximity
#     5. MACD momentum
#
#   Entry only fires if:
#     confidence >= MIN_CONFIDENCE
#     AND candle direction matches signal direction
#
# ═══════════════════════════════════════════════════════════════
# EXIT LOGIC
# ═══════════════════════════════════════════════════════════════
#
#   TP exits  → wait for candle CLOSE, take whatever close gives
#   SL exit   → fires IMMEDIATELY intra-candle
#
#   check_grids_realtime() — every 0.3s:
#       Grid triggers + Stop Loss + Manual close detection
#
#   on_new_candle() — every 15-min candle close:
#       TP (no grid)  → high/low confirmed TP pts, exit at close
#       G1 exit       → profit at close >= TARGET_PROFIT
#       G2 exit       → profit at close >= TARGET_PROFIT_G2
#       Break-even    → profit at close > 0 (Grid 3+)
#       New entry     → signal scored, confidence checked
#
# ═══════════════════════════════════════════════════════════════
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta

import schedule
import MetaTrader5 as mt5
import pandas as pd
import numpy as np


# ════════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════════

MT5_LOGIN    = 24367452
MT5_PASSWORD = "UY61&jYZ"
MT5_SERVER   = "VantageInternational-Demo"
SYMBOL       = "XAUUSD"
PAPER_TRADE  = True


# ════════════════════════════════════════════════════════════════════
#  STRATEGY PARAMETERS
# ════════════════════════════════════════════════════════════════════

TIMEFRAME            = mt5.TIMEFRAME_M15
GRID_STEP            = 15
SINGLE_TRADE_TP_PTS  = 10
TARGET_PROFIT        = 50.0
TARGET_PROFIT_G2     = 20.0
DAILY_SL_USD         = 3000.0
COOLDOWN_BARS        = 2
GRID_LOT_MULTIPLIERS = [2, 4]
FIXED_LOT            = 0.05
INITIAL_CAPITAL      = 10000
STATE_FILE           = "grid_state.json"
WARMUP_BARS          = 250
MAGIC_NUMBER         = 20250101
ENTRY_GUARD_SECS     = 5

# Signal filter parameters — must match backtest CONFIG
MIN_CONFIDENCE   = 45
EMA_FAST         = 50
EMA_SLOW         = 200
RSI_PERIOD       = 14
ATR_PERIOD       = 14
MACD_FAST        = 12
MACD_SLOW        = 26
MACD_SIGNAL_PERIOD = 9
SR_LOOKBACK      = 100
SR_ZONE_ATR_MULT = 0.3
SR_MIN_TOUCHES   = 2
SR_NEAR_ATR_MULT = 0.5


# ════════════════════════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("grid_live.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
#  HELPER — MT5 SERVER TIME
# ════════════════════════════════════════════════════════════════════

def get_server_time():
    try:
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick:
            return datetime.utcfromtimestamp(tick.time) + timedelta(hours=3)
    except Exception:
        pass
    return datetime.now()


# ════════════════════════════════════════════════════════════════════
#  INCREMENTAL INDICATORS  (EMA200 + ATR for warmup tracking)
# ════════════════════════════════════════════════════════════════════

class IncrementalEMA:
    def __init__(self, period):
        self.period = period
        self.k      = 2 / (period + 1)
        self.value  = None
        self.buffer = []

    def update(self, price):
        if self.value is None:
            self.buffer.append(price)
            if len(self.buffer) >= self.period:
                self.value = sum(self.buffer) / len(self.buffer)
        else:
            self.value = price * self.k + self.value * (1 - self.k)
        return self.value

    def is_ready(self):
        return self.value is not None


class IncrementalATR:
    def __init__(self, period):
        self.period     = period
        self.value      = None
        self.prev_close = None
        self.tr_buffer  = []

    def update(self, high, low, close):
        if self.prev_close is None:
            self.prev_close = close
            return None
        tr = max(high - low,
                 abs(high - self.prev_close),
                 abs(low  - self.prev_close))
        self.prev_close = close
        if self.value is None:
            self.tr_buffer.append(tr)
            if len(self.tr_buffer) >= self.period:
                self.value = sum(self.tr_buffer) / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value

    def is_ready(self):
        return self.value is not None


# ════════════════════════════════════════════════════════════════════
#  SIGNAL FILTER ENGINE
#  Exact replication of backtest Cell 4 — no changes
# ════════════════════════════════════════════════════════════════════

def _calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def _calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    ag    = gain.ewm(alpha=1/period, adjust=False).mean()
    al    = loss.ewm(alpha=1/period, adjust=False).mean()
    rs    = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _calc_macd(series, fast=12, slow=26, signal=9):
    ml = _calc_ema(series, fast) - _calc_ema(series, slow)
    sl = _calc_ema(ml, signal)
    return ml, sl, ml - sl

def _calc_atr(df, period=14):
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def _add_signal_indicators(df):
    df = df.copy()
    df["ema50"]       = _calc_ema(df["close"], EMA_FAST)
    df["ema200"]      = _calc_ema(df["close"], EMA_SLOW)
    df["rsi"]         = _calc_rsi(df["close"], RSI_PERIOD)
    df["atr"]         = _calc_atr(df, ATR_PERIOD)
    ml, sl, hist      = _calc_macd(df["close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL_PERIOD)
    df["macd"]        = ml
    df["macd_signal"] = sl
    df["macd_hist"]   = hist
    df["body"]        = (df["close"] - df["open"]).abs()
    df["upper_wick"]  = df["high"] - df[["open","close"]].max(axis=1)
    df["lower_wick"]  = df[["open","close"]].min(axis=1) - df["low"]
    df["range"]       = df["high"] - df["low"]
    return df

def _find_sr_levels(df_slice, atr):
    highs, lows = [], []
    data = df_slice.reset_index(drop=True)
    for i in range(2, len(data) - 2):
        h = data["high"].iloc[i]; l = data["low"].iloc[i]
        if h == data["high"].iloc[i-2:i+3].max(): highs.append(h)
        if l == data["low"].iloc[i-2:i+3].min():  lows.append(l)

    def cluster(prices):
        if not prices: return []
        thr    = atr * SR_ZONE_ATR_MULT
        levels = []; used = [False]*len(prices)
        for i, p in enumerate(prices):
            if used[i]: continue
            cl = [p]; used[i] = True
            for j in range(i+1, len(prices)):
                if not used[j] and abs(prices[j]-p) <= thr:
                    cl.append(prices[j]); used[j] = True
            if len(cl) >= SR_MIN_TOUCHES:
                levels.append((sum(cl)/len(cl), len(cl)))
        return sorted(levels)

    return cluster(highs + lows)

def _detect_trend_score(row, prev_row, df20):
    price, e50, e200 = row["close"], row["ema50"], row["ema200"]
    if   price > e50 > e200:  ema_score = 2
    elif price < e50 < e200:  ema_score = -2
    elif price > e200:        ema_score = 1
    elif price < e200:        ema_score = -1
    else:                     ema_score = 0

    mh, pmh = row["macd_hist"], prev_row["macd_hist"]
    if   mh > 0 and mh > pmh:  macd_score = 1
    elif mh < 0 and mh < pmh:  macd_score = -1
    else:                       macd_score = 0

    hs = df20["high"].values; ls = df20["low"].values
    hh = hs[-1]>hs[-5]>hs[-10]; hl = ls[-1]>ls[-5]>ls[-10]
    lh = hs[-1]<hs[-5]<hs[-10]; ll = ls[-1]<ls[-5]<ls[-10]
    if   hh and hl:  swing = 2
    elif lh and ll:  swing = -2
    elif hh or hl:   swing = 1
    elif lh or ll:   swing = -1
    else:            swing = 0

    return ema_score + macd_score + swing

def _detect_patterns(c0, c1, c2, atr):
    patterns = []
    b0, b1   = c0["body"], c1["body"]
    bull0 = c0["close"]>c0["open"]; bear0 = c0["close"]<c0["open"]
    bull1 = c1["close"]>c1["open"]; bear1 = c1["close"]<c1["open"]
    bull2 = c2["close"]>c2["open"]; bear2 = c2["close"]<c2["open"]

    if bear1 and bull0 and c0["close"]>c1["open"] and c0["open"]<c1["close"] and b0>b1*1.1:
        patterns.append(("Bullish Engulfing","BUY",3))
    if bull1 and bear0 and c0["close"]<c1["open"] and c0["open"]>c1["close"] and b0>b1*1.1:
        patterns.append(("Bearish Engulfing","SELL",3))

    lwr = c0["lower_wick"]/c0["range"] if c0["range"]>0 else 0
    if lwr>=0.6 and c0["upper_wick"]<b0*0.5 and b0>=atr*0.1:
        patterns.append(("Bullish Pin Bar","BUY",2))
    uwr = c0["upper_wick"]/c0["range"] if c0["range"]>0 else 0
    if uwr>=0.6 and c0["lower_wick"]<b0*0.5 and b0>=atr*0.1:
        patterns.append(("Bearish Pin Bar","SELL",2))

    if bear2 and c1["body"]<atr*0.3 and bull0 and c0["close"]>(c2["open"]+c2["close"])/2:
        patterns.append(("Morning Star","BUY",3))
    if bull2 and c1["body"]<atr*0.3 and bear0 and c0["close"]<(c2["open"]+c2["close"])/2:
        patterns.append(("Evening Star","SELL",3))

    if c1["high"]<c2["high"] and c1["low"]>c2["low"] and bull0 and c0["close"]>c2["high"]:
        patterns.append(("Inside Bar Breakout","BUY",2))
    if c1["high"]<c2["high"] and c1["low"]>c2["low"] and bear0 and c0["close"]<c2["low"]:
        patterns.append(("Inside Bar Breakdown","SELL",2))

    if bull0 and bull1 and bull2 and c0["close"]>c1["close"]>c2["close"] and b0>=atr*0.3:
        patterns.append(("3 White Soldiers","BUY",3))
    if bear0 and bear1 and bear2 and c0["close"]<c1["close"]<c2["close"] and b0>=atr*0.3:
        patterns.append(("3 Black Crows","SELL",3))

    return patterns

def score_signal(df_window):
    """
    Scores the latest candle in df_window.
    Returns (direction, confidence_pct, pattern_name)
             or (None, 0, None) if no qualified signal.
    Identical logic to backtest Cell 4.
    """
    if len(df_window) < 25:
        return None, 0, None

    df_ind = _add_signal_indicators(df_window)
    c0   = df_ind.iloc[-1]
    c1   = df_ind.iloc[-2]
    c2   = df_ind.iloc[-3]
    prev = df_ind.iloc[-2]

    atr_val = c0["atr"]; rsi = c0["rsi"]

    trend_score = _detect_trend_score(c0, prev, df_ind.tail(20))
    patterns    = _detect_patterns(c0, c1, c2, atr_val)
    sr_levels   = _find_sr_levels(df_window.tail(SR_LOOKBACK), atr_val)
    price       = c0["close"]

    near_sup = any(abs(price-lvl)<=atr_val*SR_NEAR_ATR_MULT for lvl,_ in sr_levels if lvl<price)
    near_res = any(abs(price-lvl)<=atr_val*SR_NEAR_ATR_MULT for lvl,_ in sr_levels if lvl>price)

    bull_score = bear_score = 0
    best_bull = best_bear = None

    if   trend_score > 0:  bull_score += min(trend_score, 4)
    elif trend_score < 0:  bear_score += min(abs(trend_score), 4)

    for name, direction, score in patterns:
        if direction == "BUY":
            bull_score += score
            if not best_bull or score > best_bull[1]: best_bull = (name, score)
        elif direction == "SELL":
            bear_score += score
            if not best_bear or score > best_bear[1]: best_bear = (name, score)

    if rsi <= 35:    bull_score += 2
    elif rsi >= 65:  bear_score += 2
    if near_sup:  bull_score += 2
    if near_res:  bear_score += 2

    mh, pmh = c0["macd_hist"], prev["macd_hist"]
    if   mh > 0 and mh > pmh:  bull_score += 1
    elif mh < 0 and mh < pmh:  bear_score += 1

    MAX = 14.0
    if bull_score > bear_score and best_bull:
        return "BUY",  min(int(bull_score/MAX*100), 95), best_bull[0]
    elif bear_score > bull_score and best_bear:
        return "SELL", min(int(bear_score/MAX*100), 95), best_bear[0]
    return None, 0, None


# ════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ════════════════════════════════════════════════════════════════════

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
        log.info(f"Resumed state  Capital:{state['capital']:.2f}")
        return state
    log.info("Fresh start — no previous state found")
    return {
        "capital"             : INITIAL_CAPITAL,
        "basket_active"       : False,
        "direction"           : None,
        "entry_price"         : None,
        "entry_time"          : None,
        "entry_candle_time"   : None,
        "trades"              : [],
        "triggered_grids"     : [],
        "cooldown_counter"    : 0,
        "daily_sl_hit"        : False,
        "daily_sl_date"       : None,
        "capital_at_day_open" : INITIAL_CAPITAL,
        "current_day"         : None,
        "total_profit"        : 0.0,
        "total_loss"          : 0.0,
        "win_trades"          : 0,
        "loss_trades"         : 0,
        "trade_log"           : [],
        "open_tickets"        : [],
        "effective_grid_step" : GRID_STEP,
        "trade_lot"           : FIXED_LOT,
        "trend_tag"           : "",
        "grid_lots"           : [],
        "session_trades"      : 0,
        "session_profit"      : 0.0,
        "last_exit_reason"    : "",
        "last_profit"         : 0.0,
        "float_pnl"           : 0.0,
        "grids_hit"           : 0,
        "signal_pattern"      : "",
        "signal_confidence"   : 0,
    }


# ════════════════════════════════════════════════════════════════════
#  MT5 BROKER
# ════════════════════════════════════════════════════════════════════

class MT5Broker:

    def connect(self):
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if not mt5.login(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
        info = mt5.account_info()
        log.info(f"Connected  Account:{info.login}  Name:{info.name}  Balance:{info.balance}  Leverage:1:{info.leverage}")
        return True

    def disconnect(self):
        mt5.shutdown()
        log.info("MT5 disconnected")

    def get_balance(self):
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"Cannot get balance: {mt5.last_error()}")
        return info.balance

    def get_latest_candles(self, symbol, timeframe, count):
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count + 2)
        if rates is None:
            raise RuntimeError(f"Cannot get candles: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return [{"open": r["open"], "high": r["high"],
                 "low": r["low"], "close": r["close"], "time": r["time"]}
                for _, r in df.iterrows()]

    def get_symbol_info(self, symbol):
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"Symbol {symbol} not found: {mt5.last_error()}")
        return info

    def place_order(self, direction, symbol, lot, comment=""):
        order_type  = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        symbol_info = self.get_symbol_info(symbol)
        price       = symbol_info.ask if direction == "BUY" else symbol_info.bid
        request = {
            "action"      : mt5.TRADE_ACTION_DEAL,
            "symbol"      : symbol,
            "volume"      : float(lot),
            "type"        : order_type,
            "price"       : price,
            "deviation"   : 20,
            "magic"       : MAGIC_NUMBER,
            "comment"     : comment,
            "type_time"   : mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"Order failed  retcode:{result.retcode if result else 'None'}  error:{mt5.last_error()}")
        log.info(f"  {direction} placed  lot:{lot}  ticket:{result.order}  fill:{result.price}")
        return result.order, result.price

    def close_position(self, ticket):
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            log.warning(f"  Position {ticket} not found — may already be closed")
            return
        position    = positions[0]
        close_type  = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        symbol_info = self.get_symbol_info(position.symbol)
        price       = symbol_info.bid if position.type == mt5.ORDER_TYPE_BUY else symbol_info.ask
        request = {
            "action"      : mt5.TRADE_ACTION_DEAL,
            "symbol"      : position.symbol,
            "volume"      : position.volume,
            "type"        : close_type,
            "position"    : ticket,
            "price"       : price,
            "deviation"   : 20,
            "magic"       : MAGIC_NUMBER,
            "comment"     : "close",
            "type_time"   : mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error(f"  Close failed ticket:{ticket}  retcode:{result.retcode if result else 'None'}")
        else:
            log.info(f"  Closed ticket:{ticket}")

    def close_all_positions(self, tickets):
        for ticket in tickets:
            try:
                self.close_position(int(ticket))
            except Exception as e:
                log.error(f"  Failed to close {ticket}: {e}")


# ════════════════════════════════════════════════════════════════════
#  GLOBAL OBJECTS
# ════════════════════════════════════════════════════════════════════

ema            = IncrementalEMA(EMA_SLOW)   # EMA200 for warmup tracking
atr_inc        = IncrementalATR(ATR_PERIOD)
state          = load_state()
broker         = MT5Broker()
bot_start_time = datetime.now()

# Rolling candle buffer for signal scoring — kept at SR_LOOKBACK + 50
candle_buffer: list = []

mt5_status = {
    "connected"      : False,
    "last_ping"      : None,
    "account_name"   : "",
    "account_number" : "",
    "server"         : "",
    "balance"        : 0.0,
    "equity"         : 0.0,
    "margin_free"    : 0.0,
    "ping_ms"        : None,
    "algo_trading"   : False,
    "error"          : "",
    "open_positions" : 0,
}


# ════════════════════════════════════════════════════════════════════
#  MT5 CONNECTION CHECKER
# ════════════════════════════════════════════════════════════════════

def check_mt5_connection():
    global mt5_status
    try:
        t0   = time.time()
        tick = mt5.symbol_info_tick(SYMBOL)
        ping = round((time.time() - t0) * 1000, 1)
        if tick is None:
            mt5_status["connected"] = False
            mt5_status["error"]     = str(mt5.last_error())
            return
        info = mt5.account_info()
        if info is None:
            mt5_status["connected"] = False
            mt5_status["error"]     = str(mt5.last_error())
            return
        terminal = mt5.terminal_info()
        mt5_status.update({
            "connected"      : True,
            "last_ping"      : datetime.now().strftime("%H:%M:%S"),
            "account_name"   : info.name,
            "account_number" : str(info.login),
            "server"         : info.server,
            "balance"        : info.balance,
            "equity"         : info.equity,
            "margin_free"    : info.margin_free,
            "ping_ms"        : ping,
            "algo_trading"   : terminal.trade_allowed if terminal else False,
            "error"          : "",
        })
        positions = mt5.positions_get()
        mt5_status["open_positions"] = len(positions) if positions else 0
    except Exception as e:
        mt5_status["connected"] = False
        mt5_status["error"]     = str(e)


# ════════════════════════════════════════════════════════════════════
#  TP EXIT — checked at candle close only
# ════════════════════════════════════════════════════════════════════

def get_tp_exit(profit, trades, direction, entry_price, h, l):
    """
    All TP exits — candle close only.
    Profit is close-price profit, not capped at minimum target.
    """
    if len(trades) == 1:
        if direction == "BUY"  and h >= entry_price + SINGLE_TRADE_TP_PTS:
            return True, "TP (no grid)", profit
        if direction == "SELL" and l <= entry_price - SINGLE_TRADE_TP_PTS:
            return True, "TP (no grid)", profit
    elif len(trades) == 2 and profit >= TARGET_PROFIT:
        return True, "TARGET PROFIT G1", profit
    elif len(trades) == 3 and profit >= TARGET_PROFIT_G2:
        return True, "TARGET PROFIT G2", profit
    elif len(trades) >= 4 and profit > 0:
        return True, "BREAK-EVEN RECOVERY", profit
    return False, "", profit


# ════════════════════════════════════════════════════════════════════
#  SL EXIT — fires immediately intra-candle
# ════════════════════════════════════════════════════════════════════

def get_sl_exit(profit):
    if profit <= -DAILY_SL_USD:
        return True, "STOP LOSS", profit
    return False, "", profit


# ════════════════════════════════════════════════════════════════════
#  ON NEW CANDLE — signal scoring + TP exits + new entries
# ════════════════════════════════════════════════════════════════════

def on_new_candle(candle):
    global state, candle_buffer

    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    c = float(candle["close"])
    t = candle["time"]

    if isinstance(t, str):
        t = datetime.fromisoformat(t)

    bar_date_str = str(t.date())
    log.info(f"Candle {t}  O:{o}  H:{h}  L:{l}  C:{c}")

    # Update incremental indicators
    ema.update(c)
    atr_inc.update(h, l, c)

    # Maintain rolling candle buffer for signal scoring
    candle_buffer.append({"open":o,"high":h,"low":l,"close":c,"time":t,"volume":1})
    if len(candle_buffer) > SR_LOOKBACK + 50:
        candle_buffer.pop(0)

    if not ema.is_ready():
        log.info("EMA warming up — skip")
        return

    # Daily reset
    if bar_date_str != state["current_day"]:
        state["current_day"]         = bar_date_str
        state["capital_at_day_open"] = state["capital"]
        state["daily_sl_hit"]        = False
        state["session_trades"]      = 0
        state["session_profit"]      = 0.0
        log.info(f"NEW DAY {bar_date_str}  Capital:{state['capital']:.2f}  DailySL:${DAILY_SL_USD}")

    if state["daily_sl_hit"]:
        if state["cooldown_counter"] > 0:
            state["cooldown_counter"] -= 1
        save_state(state)
        return

    if state["cooldown_counter"] > 0:
        state["cooldown_counter"] -= 1
        if state["cooldown_counter"] > 0:
            save_state(state)
            return

    # ── Manage open basket — TP exits at candle close ────────────
    if state["basket_active"]:

        direction   = state["direction"]
        entry_price = state["entry_price"]
        trades      = [tuple(x) for x in state["trades"]]
        triggered   = set(state["triggered_grids"])
        grid_lots   = state["grid_lots"]
        eff_step    = state["effective_grid_step"]

        for idx, lot in enumerate(grid_lots):
            if idx in triggered:
                continue
            level      = (idx + 1) * eff_step
            grid_price = entry_price - level if direction == "BUY" else entry_price + level
            hit = (direction == "BUY" and l <= grid_price) or \
                  (direction == "SELL" and h >= grid_price)
            if hit:
                ticket, fill_price = broker.place_order(direction, SYMBOL, lot,
                                                        comment=f"Grid#{idx+1}")
                trades.append((fill_price, lot))
                triggered.add(idx)
                state["open_tickets"].append(ticket)
                log.info(f"Grid #{idx+1} @ fill:{fill_price}  Lot:{lot}")

        # Profit at candle close
        if direction == "BUY":
            profit = sum((c - p) * lot * 100 for p, lot in trades)
        else:
            profit = sum((p - c) * lot * 100 for p, lot in trades)

        state["float_pnl"] = round(profit, 2)
        state["grids_hit"] = len(triggered)
        log.info(f"Close P&L:{profit:.2f}  Grids:{len(triggered)}  Account:{state['capital']+profit:.2f}")

        exit_trade, exit_reason, profit = get_tp_exit(
            profit, trades, direction, entry_price, h, l
        )

        if exit_trade:
            broker.close_all_positions(state["open_tickets"])
            state["capital"]          += profit
            state["session_trades"]   += 1
            state["session_profit"]   += profit
            state["last_exit_reason"]  = exit_reason
            state["last_profit"]       = round(profit, 2)
            state["float_pnl"]         = 0.0
            state["grids_hit"]         = 0
            log.info(f"CANDLE-CLOSE EXIT [{exit_reason}]  P&L:{profit:.2f}  Capital:{state['capital']:.2f}")

            if profit > 0:
                state["total_profit"] += profit
                state["win_trades"]   += 1
            else:
                state["total_loss"]   += abs(profit)
                state["loss_trades"]  += 1

            state["trade_log"].append({
                "time"       : str(t),
                "direction"  : direction,
                "profit"     : round(profit, 2),
                "capital"    : round(state["capital"], 2),
                "exit_reason": exit_reason,
                "grids_hit"  : len(triggered),
                "pattern"    : state.get("signal_pattern", ""),
                "confidence" : state.get("signal_confidence", 0),
            })

            state["basket_active"]     = False
            state["trades"]            = []
            state["triggered_grids"]   = []
            state["open_tickets"]      = []
            state["entry_candle_time"] = None
            state["cooldown_counter"]  = COOLDOWN_BARS
            state["signal_pattern"]    = ""
            state["signal_confidence"] = 0
        else:
            state["trades"]          = [list(x) for x in trades]
            state["triggered_grids"] = list(triggered)

        save_state(state)
        return

    # ── Open new basket — signal filter gate ────────────────────
    is_bull = c > o
    is_bear = c < o

    if not is_bull and not is_bear:
        log.info("Doji — skip")
        save_state(state)
        return

    try:
        state["capital"] = broker.get_balance()
    except Exception as e:
        log.warning(f"Balance sync failed: {e}")

    # Score signal — same logic as backtest
    if len(candle_buffer) >= 25:
        df_window = pd.DataFrame(candle_buffer)
        sig_dir, confidence, pattern_name = score_signal(df_window)
    else:
        sig_dir, confidence, pattern_name = None, 0, None

    direction_from_candle = "BUY" if is_bull else "SELL"

    if sig_dir != direction_from_candle or confidence < MIN_CONFIDENCE:
        log.info(f"Signal filtered  sig:{sig_dir}  conf:{confidence}%  candle:{direction_from_candle}  min:{MIN_CONFIDENCE}%")
        save_state(state)
        return

    # Signal approved — open basket
    trade_lot  = FIXED_LOT
    grid_lots  = [trade_lot * m for m in GRID_LOT_MULTIPLIERS]
    above_ema  = o >= ema.value
    with_trend = (is_bull and above_ema) or (is_bear and not above_ema)
    trend_tag  = "WITH-TREND" if with_trend else "COUNTER-TREND"

    log.info(f"{direction_from_candle} [{trend_tag}] APPROVED  conf:{confidence}%  pattern:{pattern_name}  EMA:{ema.value:.2f}")

    ticket, fill_price = broker.place_order(direction_from_candle, SYMBOL, trade_lot,
                                            comment=f"Entry {trend_tag} {confidence}%")

    state["basket_active"]       = True
    state["direction"]           = direction_from_candle
    state["entry_price"]         = fill_price
    state["entry_time"]          = str(datetime.now())
    state["entry_candle_time"]   = str(t)
    state["trades"]              = [[fill_price, trade_lot]]
    state["triggered_grids"]     = []
    state["open_tickets"]        = [ticket]
    state["effective_grid_step"] = GRID_STEP
    state["trade_lot"]           = trade_lot
    state["trend_tag"]           = trend_tag
    state["grid_lots"]           = grid_lots
    state["float_pnl"]           = 0.0
    state["grids_hit"]           = 0
    state["signal_pattern"]      = pattern_name or ""
    state["signal_confidence"]   = confidence

    save_state(state)


# ════════════════════════════════════════════════════════════════════
#  WARMUP — primes EMA200, ATR and candle buffer
# ════════════════════════════════════════════════════════════════════

def warmup():
    global candle_buffer
    log.info(f"Warming up on {WARMUP_BARS} historical bars...")
    candles = broker.get_latest_candles(SYMBOL, TIMEFRAME, WARMUP_BARS)
    for c in candles[:-1]:
        ema.update(c["close"])
        atr_inc.update(c["high"], c["low"], c["close"])
        candle_buffer.append(c)
    if len(candle_buffer) > SR_LOOKBACK + 50:
        candle_buffer = candle_buffer[-(SR_LOOKBACK + 50):]
    log.info(f"Warmup done  EMA200:{ema.value:.2f}  ATR:{atr_inc.value:.4f}  Buffer:{len(candle_buffer)} bars")


# ════════════════════════════════════════════════════════════════════
#  TICK
# ════════════════════════════════════════════════════════════════════

def tick():
    try:
        time.sleep(2)
        candles = broker.get_latest_candles(SYMBOL, TIMEFRAME, 3)
        closed  = candles[-2]
        on_new_candle(closed)
    except Exception as e:
        log.error(f"Tick error: {e}", exc_info=True)


# ════════════════════════════════════════════════════════════════════
#  REAL-TIME GRID CHECK
#  Grid triggers + Stop Loss only — NO TP exits here
# ════════════════════════════════════════════════════════════════════

def check_grids_realtime():
    global state

    if not state.get("basket_active"):
        return

    try:
        # Manual close detector
        open_tickets = state.get("open_tickets", [])
        if open_tickets:
            still_open = [t for t in open_tickets if mt5.positions_get(ticket=int(t))]

            if len(still_open) == 0:
                log.warning(f"MANUAL CLOSE DETECTED — all {len(open_tickets)} position(s) closed outside bot")
                state.update({
                    "basket_active"    : False,
                    "trades"           : [],
                    "triggered_grids"  : [],
                    "open_tickets"     : [],
                    "entry_candle_time": None,
                    "float_pnl"        : 0.0,
                    "grids_hit"        : 0,
                    "cooldown_counter" : COOLDOWN_BARS,
                    "last_exit_reason" : "MANUAL CLOSE",
                    "last_profit"      : 0.0,
                    "signal_pattern"   : "",
                    "signal_confidence": 0,
                })
                save_state(state)
                log.warning("State reset — bot will wait cooldown then resume")
                return

            elif len(still_open) < len(open_tickets):
                log.warning(f"PARTIAL MANUAL CLOSE — {len(open_tickets)-len(still_open)} position(s) closed outside bot")
                state["open_tickets"] = still_open
                save_state(state)

        # Entry guard
        entry_time = state.get("entry_time")
        if entry_time:
            secs_since_entry = (datetime.now() - datetime.fromisoformat(str(entry_time))).total_seconds()
            if secs_since_entry < ENTRY_GUARD_SECS:
                return

        symbol_info = broker.get_symbol_info(SYMBOL)
        direction   = state["direction"]
        live_price  = symbol_info.bid if direction == "BUY" else symbol_info.ask

        entry_price  = state["entry_price"]
        trades       = [tuple(x) for x in state["trades"]]
        triggered    = set(state["triggered_grids"])
        grid_lots    = state["grid_lots"]
        eff_step     = state["effective_grid_step"]
        new_grid_hit = False

        # Grid triggers
        for idx, lot in enumerate(grid_lots):
            if idx in triggered:
                continue
            level      = (idx + 1) * eff_step
            grid_price = entry_price - level if direction == "BUY" else entry_price + level
            hit = (direction == "BUY"  and live_price <= grid_price) or \
                  (direction == "SELL" and live_price >= grid_price)
            if hit:
                ticket, fill_price = broker.place_order(direction, SYMBOL, lot,
                                                        comment=f"Grid#{idx+1}RT")
                trades.append((fill_price, lot))
                triggered.add(idx)
                state["open_tickets"].append(ticket)
                state["grids_hit"] = len(triggered)
                new_grid_hit       = True
                log.info(f"REALTIME Grid #{idx+1} @ live:{live_price}  fill:{fill_price}  Lot:{lot}")

        # Update float P&L for dashboard
        if direction == "BUY":
            profit = sum((live_price - p) * l * 100 for p, l in trades)
        else:
            profit = sum((p - live_price) * l * 100 for p, l in trades)

        state["float_pnl"] = round(profit, 2)

        if new_grid_hit:
            state["trades"]          = [list(x) for x in trades]
            state["triggered_grids"] = list(triggered)
            save_state(state)

        # Stop loss — fires immediately
        exit_trade, exit_reason, profit = get_sl_exit(profit)

        if exit_trade:
            broker.close_all_positions(state["open_tickets"])
            state["capital"]          += profit
            state["session_trades"]   += 1
            state["session_profit"]   += profit
            state["last_exit_reason"]  = exit_reason
            state["last_profit"]       = round(profit, 2)
            state["float_pnl"]         = 0.0
            state["grids_hit"]         = 0
            state["basket_active"]     = False
            state["trades"]            = []
            state["triggered_grids"]   = []
            state["open_tickets"]      = []
            state["entry_candle_time"] = None
            state["cooldown_counter"]  = COOLDOWN_BARS
            state["daily_sl_hit"]      = True
            state["signal_pattern"]    = ""
            state["signal_confidence"] = 0
            state["total_loss"]       += abs(profit)
            state["loss_trades"]      += 1
            state["trade_log"].append({
                "time"       : str(datetime.now()),
                "direction"  : direction,
                "profit"     : round(profit, 2),
                "capital"    : round(state["capital"], 2),
                "exit_reason": exit_reason,
                "grids_hit"  : len(triggered),
            })
            log.info(f"IMMEDIATE SL EXIT  P&L:{profit:.2f}  Capital:{state['capital']:.2f}")
            save_state(state)

    except Exception as e:
        log.error(f"Realtime grid check error: {e}")


# ════════════════════════════════════════════════════════════════════
#  DASHBOARD HELPERS
# ════════════════════════════════════════════════════════════════════

def get_market_session(server_time):
    h    = server_time.hour + server_time.minute / 60.0
    wday = server_time.weekday()
    asian    = 2.0  <= h < 10.0
    london   = 10.0 <= h < 18.0
    new_york = 15.0 <= h < 23.0
    weekend  = (wday == 4 and h >= 23.0) or (wday == 5) or (wday == 6 and h < 2.0)

    if weekend:
        days_left = 2 if wday == 4 else (1 if wday == 5 else 0)
        target = server_time.replace(hour=2, minute=0, second=0, microsecond=0)
        if days_left > 0: target += timedelta(days=days_left)
        elif server_time >= target: target += timedelta(days=7)
        diff = target - server_time; tot = int(diff.total_seconds())
        return "CLOSED", "WEEKEND", f"{tot//3600}h {(tot%3600)//60:02d}m"

    if not (asian or london or new_york):
        target = server_time.replace(hour=2, minute=0, second=0, microsecond=0)
        if server_time >= target: target += timedelta(days=1)
        diff = target - server_time; tot = int(diff.total_seconds())
        return "CLOSED", "DAILY CLOSE", f"{tot//3600}h {(tot%3600)//60:02d}m"

    if london and new_york: session = "LONDON + NEW YORK [OVERLAP]"
    elif london:            session = "LONDON"
    elif new_york:          session = "NEW YORK"
    elif asian:             session = "ASIAN"
    else:                   session = "UNKNOWN"
    return "OPEN", session, ""


def get_uptime():
    delta = datetime.now() - bot_start_time
    total = int(delta.total_seconds())
    h = total//3600; m = (total%3600)//60; s = total%60
    return f"{h}h {m:02d}m {s:02d}s" if h > 0 else (f"{m}m {s:02d}s" if m > 0 else f"{s}s")


def get_time_in_trade():
    entry_time = state.get("entry_time")
    if not entry_time or not state.get("basket_active"): return None
    try:
        delta = datetime.now() - datetime.fromisoformat(str(entry_time))
        total = int(delta.total_seconds())
        h = total//3600; m = (total%3600)//60; s = total%60
        return f"{h}h {m:02d}m {s:02d}s" if h > 0 else f"{m}m {s:02d}s"
    except Exception: return None


def get_daily_sl_bar(daily_loss, width=20):
    pct    = min(abs(daily_loss) / DAILY_SL_USD, 1.0)
    filled = int(pct * width)
    bar    = "█" * filled + "░" * (width - filled)
    return f"[{bar}]  {pct*100:.0f}%  (${abs(daily_loss):.0f} of ${DAILY_SL_USD:.0f} limit)"


# ════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ════════════════════════════════════════════════════════════════════

def print_dashboard(next_mins, next_secs):
    os.system('cls' if os.name == 'nt' else 'clear')

    now       = get_server_time()
    total     = state.get("win_trades", 0) + state.get("loss_trades", 0)
    win_rate  = (state.get("win_trades", 0) / total * 100) if total > 0 else 0.0
    net_pnl   = state.get("capital", INITIAL_CAPITAL) - INITIAL_CAPITAL
    daily_pnl = state.get("capital", INITIAL_CAPITAL) - state.get("capital_at_day_open", INITIAL_CAPITAL)

    if state.get("basket_active"):
        grids      = state.get("grids_hit", 0)
        pattern    = state.get("signal_pattern", "")
        conf       = state.get("signal_confidence", 0)
        sig_str    = f"  [{pattern}  {conf}%]" if pattern else ""
        basket_str = f"{state.get('direction')} | Grids:{grids}/{len(GRID_LOT_MULTIPLIERS)} | Float:${state.get('float_pnl',0.0):+.2f}{sig_str}"
        entry_str  = f"Entry @ {state.get('entry_price','?')}  |  TP exits at candle close"
        tit_str    = f"Time in trade  : {get_time_in_trade()}" if get_time_in_trade() else ""
    else:
        last_reason = state.get("last_exit_reason", "")
        last_profit = state.get("last_profit", 0.0)
        last        = f"${last_profit:+.2f} [{last_reason}]" if last_reason else "None yet"
        basket_str  = f"WAITING  |  Last trade: {last}"
        entry_str   = f"Cooldown: {state.get('cooldown_counter', 0)} bars  |  Min confidence: {MIN_CONFIDENCE}%"
        tit_str     = ""

    if mt5_status["connected"]:
        conn_str  = "CONNECTED"
        algo_str  = "ON" if mt5_status["algo_trading"] else "OFF  <-- ENABLE IN MT5!"
        ping_str  = f"{mt5_status['ping_ms']} ms"
        acct_str  = f"{mt5_status['account_name']}  ({mt5_status['account_number']})"
        bal_str   = f"${mt5_status['balance']:.2f}  |  Equity:${mt5_status['equity']:.2f}  |  Free:${mt5_status['margin_free']:.2f}"
        last_ping = mt5_status["last_ping"] or "?"
        pos_str   = str(mt5_status["open_positions"])
    else:
        conn_str  = "DISCONNECTED  <-- MT5 NOT RUNNING OR NOT LOGGED IN"
        algo_str  = ping_str = acct_str = bal_str = last_ping = pos_str = "N/A"

    ema_str = f"{ema.value:.2f}" if ema.is_ready() else "Warming up..."
    atr_str = f"{atr_inc.value:.4f}" if atr_inc.is_ready() else "Warming up..."
    mkt_status, mkt_session, mkt_opens_in = get_market_session(now)
    mkt_str = f"OPEN  |  {mkt_session}" if mkt_status == "OPEN" \
              else f"CLOSED  ({mkt_session})  |  Opens in: {mkt_opens_in}"
    sl_bar  = get_daily_sl_bar(daily_pnl) if daily_pnl < 0 \
              else f"[{'░'*20}]  0%  ($0 of ${DAILY_SL_USD:.0f} limit)"

    print("=" * 66)
    print(f"  GRID + SIGNAL BOT  |  {now.strftime('%Y-%m-%d  %H:%M:%S')}  [UTC+3]")
    print("=" * 66)
    print(f"  MT5            : {conn_str}")
    print(f"  Account        : {acct_str}")
    print(f"  Server         : {mt5_status['server'] or MT5_SERVER}")
    print(f"  Algo Trading   : {algo_str}")
    print(f"  Ping           : {ping_str}  |  Last: {last_ping}")
    if not mt5_status["connected"] and mt5_status["error"]:
        print(f"  Error          : {mt5_status['error']}")
    print(f"  Balance        : {bal_str}")
    print(f"  Open Positions : {pos_str}")
    print("-" * 66)
    print(f"  Market         : {mkt_str}")
    print(f"  Uptime         : {get_uptime()}")
    print(f"  Next candle in : {next_mins:02d}:{next_secs:02d}")
    print(f"  EMA(200)       : {ema_str}  |  ATR(14): {atr_str}  |  Buffer: {len(candle_buffer)} bars")
    print("-" * 66)
    print(f"  Capital        : ${state.get('capital', INITIAL_CAPITAL):.2f}")
    print(f"  Net P&L        : ${net_pnl:+.2f}")
    print(f"  Today P&L      : ${daily_pnl:+.2f}  {'  DAILY SL HIT' if state.get('daily_sl_hit') else ''}")
    print(f"  Daily SL       : {sl_bar}")
    print(f"  Session        : {state.get('session_trades',0)} trades  |  ${state.get('session_profit',0.0):+.2f}")
    print("-" * 66)
    print(f"  Total trades   : {total}  |  Win rate: {win_rate:.1f}%")
    print(f"  Total profit   : ${state.get('total_profit',0.0):.2f}")
    print(f"  Total loss     : ${state.get('total_loss',0.0):.2f}")
    print("-" * 66)
    print(f"  Signal filter  : Min confidence {MIN_CONFIDENCE}%")
    print(f"  TP (min)       : No grid:{SINGLE_TRADE_TP_PTS}pts | G1:${TARGET_PROFIT} | G2:${TARGET_PROFIT_G2} | G3:B/E")
    print(f"  Basket         : {basket_str}")
    print(f"                   {entry_str}")
    if tit_str:
        print(f"                   {tit_str}")
    print("=" * 66)


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════

async def main():
    log.info("=" * 60)
    log.info("Grid Strategy + Signal Filter — Live")
    log.info(f"Account  : {MT5_LOGIN}  Server: {MT5_SERVER}")
    log.info(f"Symbol   : {SYMBOL}  Lot: {FIXED_LOT}  Grid: {GRID_STEP}pts")
    log.info(f"Daily SL : ${DAILY_SL_USD}  (fires immediately)")
    log.info(f"TP min   : No-grid:{SINGLE_TRADE_TP_PTS}pts  G1:${TARGET_PROFIT}  G2:${TARGET_PROFIT_G2}")
    log.info(f"TP mode  : Candle close — profits ride the full candle")
    log.info(f"Signal   : Min confidence {MIN_CONFIDENCE}%")
    log.info("=" * 60)

    broker.connect()
    warmup()

    schedule.every().hour.at(":00").do(tick)
    schedule.every().hour.at(":15").do(tick)
    schedule.every().hour.at(":30").do(tick)
    schedule.every().hour.at(":45").do(tick)

    while True:
        schedule.run_pending()
        check_mt5_connection()
        check_grids_realtime()

        now        = get_server_time()
        total_secs = now.minute * 60 + now.second
        next_secs  = None
        for t in [0, 15, 30, 45]:
            if t * 60 > total_secs:
                next_secs = t * 60 - total_secs
                break
        if next_secs is None:
            next_secs = (60 - now.minute) * 60 - now.second
        next_secs = max(0, next_secs)

        print_dashboard(next_secs // 60, next_secs % 60)
        await asyncio.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(main())
