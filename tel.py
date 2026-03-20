"""
Telegram Notifier + Command Listener for Grid Strategy Bot
===========================================================

SETUP:
1. pip install requests
2. Put this file in the same folder as grid_strategy_live.py
3. Already imported and used in grid_strategy_live.py

TELEGRAM COMMANDS (send these to your bot):
   /status  → replies with full live bot status
   /help    → lists available commands

HOW IT WORKS:
   A background thread polls Telegram for incoming messages every 2 seconds.
   When you send /status, the bot replies instantly with current state.
   Does not block or slow down the trading loop.
"""

import requests
import logging
import threading
import time
from datetime import datetime

log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════════

BOT_TOKEN = "8386293337:AAE5TJOM3VfrUb0dF313eBsRQxf_Rkt4ylI"
CHAT_ID   = "7858967749"


# ════════════════════════════════════════════════════════════════════
#  TELEGRAM NOTIFIER + COMMAND LISTENER
# ════════════════════════════════════════════════════════════════════

class TelegramNotifier:

    def __init__(self, bot_token=BOT_TOKEN, chat_id=CHAT_ID):
        self.bot_token   = bot_token
        self.chat_id     = chat_id
        self.base_url    = f"https://api.telegram.org/bot{self.bot_token}"
        self._last_update_id = None

        # Reference to live state — set by live bot after init
        # notifier.state_ref    = state
        # notifier.mt5_ref      = mt5_status
        # notifier.bot_start_ref = bot_start_time
        self.state_ref     = None
        self.mt5_ref       = None
        self.bot_start_ref = None

        self._test_connection()
        self._start_command_listener()

    def _test_connection(self):
        try:
            r = requests.get(f"{self.base_url}/getMe", timeout=5)
            if r.status_code == 200:
                name = r.json().get("result", {}).get("first_name", "Bot")
                log.info(f"Telegram connected — bot: {name}")
            else:
                log.warning(f"Telegram connection test failed: {r.status_code}")
        except Exception as e:
            log.warning(f"Telegram connection test error: {e}")

    def _send(self, message: str, chat_id=None):
        """Send a message. Fails silently so bot never crashes."""
        try:
            payload = {
                "chat_id"   : chat_id or self.chat_id,
                "text"      : message,
                "parse_mode": "HTML",
            }
            r = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=5)
            if r.status_code != 200:
                log.warning(f"Telegram send failed: {r.status_code}  {r.text[:100]}")
        except Exception as e:
            log.warning(f"Telegram send error: {e}")

    def _now(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ════════════════════════════════════════════════════════════════
    #  COMMAND LISTENER — runs in background thread
    # ════════════════════════════════════════════════════════════════

    def _start_command_listener(self):
        """Start background thread that polls for Telegram commands every 2s."""
        t = threading.Thread(target=self._poll_commands, daemon=True)
        t.start()
        log.info("Telegram command listener started — send /status to check bot")

    def _poll_commands(self):
        """Background polling loop — checks for new messages every 2 seconds."""
        while True:
            try:
                params = {"timeout": 1, "allowed_updates": ["message"]}
                if self._last_update_id is not None:
                    params["offset"] = self._last_update_id + 1

                r = requests.get(f"{self.base_url}/getUpdates",
                                 params=params, timeout=10)
                if r.status_code == 200:
                    updates = r.json().get("result", [])
                    for update in updates:
                        self._last_update_id = update["update_id"]
                        self._handle_update(update)
            except Exception as e:
                log.debug(f"Telegram poll error: {e}")
            time.sleep(2)

    def _handle_update(self, update):
        """Process an incoming message."""
        try:
            message = update.get("message", {})
            text    = message.get("text", "").strip().lower()
            from_id = str(message.get("chat", {}).get("id", ""))

            # Only respond to your own chat ID for security
            if from_id != str(self.chat_id):
                log.warning(f"Ignored message from unknown chat_id: {from_id}")
                return

            if text in ["/status", "/status@" + self._get_bot_username()]:
                self._reply_status(from_id)
            elif text in ["/help", "/help@" + self._get_bot_username()]:
                self._reply_help(from_id)
            else:
                self._send(
                    f"❓ Unknown command: <code>{text}</code>\n"
                    f"Send /help to see available commands.",
                    chat_id=from_id
                )
        except Exception as e:
            log.debug(f"Telegram handle update error: {e}")

    def _get_bot_username(self):
        """Get bot username for command matching (cached)."""
        try:
            if not hasattr(self, "_bot_username"):
                r = requests.get(f"{self.base_url}/getMe", timeout=5)
                self._bot_username = r.json().get("result", {}).get("username", "")
            return self._bot_username
        except Exception:
            return ""

    # ════════════════════════════════════════════════════════════════
    #  /status REPLY
    # ════════════════════════════════════════════════════════════════

    def _reply_status(self, chat_id):
        """Build and send a full live status reply."""
        try:
            s   = self.state_ref
            mt5 = self.mt5_ref

            # If state not linked yet, send basic alive message
            if s is None:
                self._send(
                    f"✅ <b>BOT IS ALIVE</b>\n"
                    f"🕐 {self._now()}\n"
                    f"⚠️ State not linked yet — bot may still be warming up.",
                    chat_id=chat_id
                )
                return

            # Uptime
            if self.bot_start_ref:
                delta   = datetime.now() - self.bot_start_ref
                total   = int(delta.total_seconds())
                h = total//3600; m = (total%3600)//60; sc = total%60
                uptime  = f"{h}h {m:02d}m {sc:02d}s" if h > 0 else f"{m}m {sc:02d}s"
            else:
                uptime = "Unknown"

            # Capital stats
            initial  = s.get("capital", 10000)
            capital  = s.get("capital", 10000)
            net_pnl  = capital - s.get("capital", capital)
            daily_pnl = capital - s.get("capital_at_day_open", capital)

            # Win rate
            wins   = s.get("win_trades", 0)
            losses = s.get("loss_trades", 0)
            total_t = wins + losses
            wr      = f"{wins/total_t*100:.1f}%" if total_t > 0 else "N/A"

            # MT5 connection
            if mt5 and mt5.get("connected"):
                conn_str = f"✅ CONNECTED  ({mt5.get('ping_ms','?')} ms)"
                bal_str  = f"${mt5.get('balance',0):,.2f}"
            else:
                conn_str = "❌ DISCONNECTED"
                bal_str  = "N/A"

            # Basket status
            if s.get("basket_active"):
                grids       = s.get("grids_hit", 0)
                direction   = s.get("direction", "?")
                entry_price = s.get("entry_price", "?")
                float_pnl   = s.get("float_pnl", 0.0)
                pattern     = s.get("signal_pattern", "")
                conf        = s.get("signal_confidence", 0)
                pnl_emoji   = "📈" if float_pnl >= 0 else "📉"
                sig_str     = f"\n🔬 Signal      : {pattern} ({conf}%)" if pattern else ""
                basket_str  = (
                    f"🟡 <b>TRADE OPEN</b>\n"
                    f"📍 Direction   : {direction}\n"
                    f"💲 Entry price : {entry_price}\n"
                    f"⚡ Grids hit   : {grids}\n"
                    f"{pnl_emoji} Float P&L   : ${float_pnl:+.2f}"
                    f"{sig_str}"
                )
            else:
                cooldown   = s.get("cooldown_counter", 0)
                last_exit  = s.get("last_exit_reason", "")
                last_profit= s.get("last_profit", 0.0)
                sl_hit     = s.get("daily_sl_hit", False)

                if sl_hit:
                    basket_str = "⛔ <b>DAILY SL HIT — no more trades today</b>"
                elif cooldown > 0:
                    basket_str = f"⏳ <b>WAITING</b> — cooldown {cooldown} bars remaining"
                else:
                    last_str = f"${last_profit:+.2f} [{last_exit}]" if last_exit else "None yet"
                    basket_str = f"👀 <b>WATCHING FOR SIGNAL</b>\nLast trade: {last_str}"

            msg = (
                f"📊 <b>BOT STATUS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🕐 Time        : {self._now()}\n"
                f"⏱️ Uptime       : {uptime}\n"
                f"📡 MT5          : {conn_str}\n"
                f"💰 Balance      : {bal_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏦 Bot Capital  : ${capital:,.2f}\n"
                f"📅 Today P&L    : ${daily_pnl:+.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📈 Total trades : {total_t}  |  WR: {wr}\n"
                f"✅ Wins         : {wins}\n"
                f"❌ Losses       : {losses}\n"
                f"💵 Session P&L  : ${s.get('session_profit',0.0):+.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{basket_str}"
            )
            self._send(msg, chat_id=chat_id)

        except Exception as e:
            self._send(f"✅ Bot is alive but status error: {e}", chat_id=chat_id)
            log.warning(f"Status reply error: {e}")

    def _reply_help(self, chat_id):
        msg = (
            f"🤖 <b>GRID BOT COMMANDS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"/status — Full live bot status\n"
            f"          Shows MT5 connection, capital,\n"
            f"          open trade, grids, float P&L,\n"
            f"          win rate and daily stats\n\n"
            f"/help   — Show this message\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📲 Automatic alerts sent for:\n"
            f"  • Signal approved + entry\n"
            f"  • Grid triggered\n"
            f"  • Trade closed (win/loss)\n"
            f"  • Stop loss hit\n"
            f"  • MT5 disconnect / reconnect\n"
            f"  • Daily summary\n"
            f"  • Manual close detected"
        )
        self._send(msg, chat_id=chat_id)

    # ════════════════════════════════════════════════════════════════
    #  NOTIFICATION METHODS
    # ════════════════════════════════════════════════════════════════

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
            f"Send /status anytime to check bot."
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

    def signal_approved(self, direction, confidence, pattern, entry_price, trend_tag):
        arrow = "🟢 BUY  📈" if direction == "BUY" else "🔴 SELL 📉"
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

    def trade_exit(self, exit_reason, profit, capital, grids_hit, direction):
        if profit > 0:
            result_emoji = "✅"; result_label = "WIN"
        else:
            result_emoji = "❌"; result_label = "LOSS"

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
            f"🛑 <b>STOP LOSS — DAILY SL TRIGGERED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time    : {self._now()}\n"
            f"💸 Loss    : ${profit:+.2f}\n"
            f"🏦 Capital : ${capital:,.2f}\n"
            f"⛔ No more trades today."
        )
        self._send(msg)

    def daily_summary(self, session_trades, session_profit, win_trades, loss_trades, capital):
        win_rate  = (win_trades / session_trades * 100) if session_trades > 0 else 0
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
#  QUICK TEST — run this file directly to test your Telegram
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

    print("\nCommand listener is running — send /status or /help to your bot now")
    print("Press Ctrl+C to stop\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped")
