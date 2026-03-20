"""
Telegram Notifier for Grid Strategy Bot
========================================

SETUP:
1. pip install requests
2. Make sure this file is in the same folder as grid_strategy_live.py
3. Import and use in grid_strategy_live.py:
       from telegram_notify import TelegramNotifier
       notifier = TelegramNotifier()

USAGE in grid_strategy_live.py:
   notifier.signal_approved(direction, confidence, pattern, entry_price, trend_tag)
   notifier.trade_exit(exit_reason, profit, capital, grids_hit, direction)
   notifier.grid_triggered(grid_num, fill_price, lot, float_pnl, direction)
   notifier.sl_hit(profit, capital)
   notifier.daily_summary(session_trades, session_profit, win_trades, loss_trades, capital)
   notifier.bot_started(symbol, lot, grid_step, min_confidence, daily_sl)
   notifier.bot_error(error_message)
   notifier.connection_lost()
   notifier.connection_restored(balance)
"""

import requests
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════════

BOT_TOKEN = "8386293337:AAE5TJOM3VfrUb0dF313eBsRQxf_Rkt4ylI"
CHAT_ID   = "7858967749"


# ════════════════════════════════════════════════════════════════════
#  TELEGRAM NOTIFIER
# ════════════════════════════════════════════════════════════════════

class TelegramNotifier:

    def __init__(self, bot_token=BOT_TOKEN, chat_id=CHAT_ID):
        self.bot_token = bot_token
        self.chat_id   = chat_id
        self.base_url  = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        self._test_connection()

    def _test_connection(self):
        try:
            url      = f"https://api.telegram.org/bot{self.bot_token}/getMe"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                name = response.json().get("result", {}).get("first_name", "Bot")
                log.info(f"Telegram connected — bot name: {name}")
            else:
                log.warning(f"Telegram connection test failed: {response.status_code}")
        except Exception as e:
            log.warning(f"Telegram connection test error: {e}")

    def _send(self, message: str):
        """Send a message. Fails silently so bot never crashes on Telegram errors."""
        try:
            payload  = {
                "chat_id"    : self.chat_id,
                "text"       : message,
                "parse_mode" : "HTML",
            }
            response = requests.post(self.base_url, json=payload, timeout=5)
            if response.status_code != 200:
                log.warning(f"Telegram send failed: {response.status_code}  {response.text[:100]}")
        except Exception as e:
            log.warning(f"Telegram send error: {e}")

    def _now(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Bot lifecycle ─────────────────────────────────────────────

    def bot_started(self, symbol, lot, grid_step, min_confidence, daily_sl):
        msg = (
            f"🟢 <b>BOT STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time       : {self._now()}\n"
            f"📊 Symbol     : {symbol}\n"
            f"📦 Lot size   : {lot}\n"
            f"📏 Grid step  : {grid_step} pts\n"
            f"🔬 Min conf   : {min_confidence}%\n"
            f"🛡️ Daily SL   : ${daily_sl}\n"
            f"🕯️ TP mode    : Candle close\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Bot is live and watching for signals."
        )
        self._send(msg)

    def bot_stopped(self):
        msg = (
            f"🔴 <b>BOT STOPPED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time : {self._now()}\n"
            f"Bot has been shut down."
        )
        self._send(msg)

    def bot_error(self, error_message):
        msg = (
            f"⚠️ <b>BOT ERROR</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time  : {self._now()}\n"
            f"❌ Error : <code>{error_message[:200]}</code>"
        )
        self._send(msg)

    def connection_lost(self):
        msg = (
            f"📡 <b>MT5 CONNECTION LOST</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time : {self._now()}\n"
            f"⚠️ MT5 is disconnected. Check your terminal."
        )
        self._send(msg)

    def connection_restored(self, balance):
        msg = (
            f"📡 <b>MT5 CONNECTION RESTORED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time    : {self._now()}\n"
            f"✅ Back online\n"
            f"💰 Balance : ${balance:,.2f}"
        )
        self._send(msg)

    # ── Signal & entry ────────────────────────────────────────────

    def signal_approved(self, direction, confidence, pattern, entry_price, trend_tag):
        arrow  = "🟢 BUY  📈" if direction == "BUY" else "🔴 SELL 📉"
        msg = (
            f"{arrow} <b>SIGNAL APPROVED — ENTRY</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time       : {self._now()}\n"
            f"📍 Direction  : {direction}\n"
            f"💲 Entry price: {entry_price}\n"
            f"🔬 Confidence : {confidence}%\n"
            f"🕯️ Pattern    : {pattern}\n"
            f"📊 Trend      : {trend_tag}"
        )
        self._send(msg)

    def signal_filtered(self, sig_dir, confidence, candle_dir, min_conf):
        msg = (
            f"🚫 <b>SIGNAL FILTERED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time       : {self._now()}\n"
            f"📊 Signal     : {sig_dir or 'None'}  ({confidence}%)\n"
            f"🕯️ Candle     : {candle_dir}\n"
            f"🔬 Min conf   : {min_conf}%\n"
            f"⏭️ Skipping this candle."
        )
        self._send(msg)

    # ── Grid triggers ─────────────────────────────────────────────

    def grid_triggered(self, grid_num, fill_price, lot, float_pnl, direction):
        pnl_emoji = "📈" if float_pnl >= 0 else "📉"
        msg = (
            f"⚡ <b>GRID #{grid_num} TRIGGERED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time       : {self._now()}\n"
            f"📍 Direction  : {direction}\n"
            f"💲 Fill price : {fill_price}\n"
            f"📦 Lot        : {lot}\n"
            f"{pnl_emoji} Float P&L  : ${float_pnl:+.2f}"
        )
        self._send(msg)

    # ── Trade exits ───────────────────────────────────────────────

    def trade_exit(self, exit_reason, profit, capital, grids_hit, direction):
        if profit > 0:
            result_emoji = "✅"
            result_label = "WIN"
        else:
            result_emoji = "❌"
            result_label = "LOSS"

        reason_emoji = {
            "TP (no grid)"       : "🎯",
            "TARGET PROFIT G1"   : "🎯",
            "TARGET PROFIT G2"   : "🎯",
            "BREAK-EVEN RECOVERY": "⚖️",
            "STOP LOSS"          : "🛑",
            "MANUAL CLOSE"       : "🤚",
        }.get(exit_reason, "📤")

        msg = (
            f"{result_emoji} <b>TRADE CLOSED — {result_label}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time       : {self._now()}\n"
            f"📍 Direction  : {direction}\n"
            f"{reason_emoji} Reason    : {exit_reason}\n"
            f"💰 P&L        : ${profit:+.2f}\n"
            f"⚡ Grids hit  : {grids_hit}\n"
            f"🏦 Capital    : ${capital:,.2f}"
        )
        self._send(msg)

    def sl_hit(self, profit, capital):
        msg = (
            f"🛑 <b>STOP LOSS HIT — DAILY SL TRIGGERED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time    : {self._now()}\n"
            f"💸 Loss    : ${profit:+.2f}\n"
            f"🏦 Capital : ${capital:,.2f}\n"
            f"⛔ No more trades today."
        )
        self._send(msg)

    # ── Daily summary ─────────────────────────────────────────────

    def daily_summary(self, session_trades, session_profit, win_trades, loss_trades, capital):
        win_rate = (win_trades / session_trades * 100) if session_trades > 0 else 0
        pnl_emoji = "📈" if session_profit >= 0 else "📉"
        msg = (
            f"📋 <b>DAILY SUMMARY</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time       : {self._now()}\n"
            f"📊 Trades     : {session_trades}\n"
            f"✅ Wins       : {win_trades}\n"
            f"❌ Losses     : {loss_trades}\n"
            f"🎯 Win rate   : {win_rate:.1f}%\n"
            f"{pnl_emoji} Session P&L : ${session_profit:+.2f}\n"
            f"🏦 Capital    : ${capital:,.2f}"
        )
        self._send(msg)

    # ── Manual close detected ─────────────────────────────────────

    def manual_close_detected(self, num_positions):
        msg = (
            f"🤚 <b>MANUAL CLOSE DETECTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time       : {self._now()}\n"
            f"📦 Positions  : {num_positions} closed outside bot\n"
            f"⏳ Bot will wait cooldown then resume normally."
        )
        self._send(msg)


# ════════════════════════════════════════════════════════════════════
#  QUICK TEST — run this file directly to test your connection
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)s  %(message)s")

    print("Testing Telegram connection...")
    n = TelegramNotifier()

    print("Sending test messages...")
    n.bot_started("XAUUSD", 0.05, 15, 45, 3000)
    n.signal_approved("BUY", 72, "Bullish Engulfing", 2341.50, "WITH-TREND")
    n.grid_triggered(1, 2326.50, 0.10, -15.50, "BUY")
    n.trade_exit("TARGET PROFIT G1", 87.30, 10087.30, 1, "BUY")
    n.daily_summary(5, 143.20, 4, 1, 10143.20)

    print("Done — check your Telegram!")
