#!/usr/bin/env python3
"""
Telegram Notification Module

Sends high-level trading alerts to a Telegram chat via the Bot API.
Designed to be fire-and-forget: a Telegram failure never crashes the bot.

Notifications sent:
  - System startup / shutdown
  - Trade opened (strategy, legs, entry cost)
  - Trade closed (PnL, ROI, hold time)
  - Daily account summary (equity, UPnL, positions, delta)
  - Critical errors (consecutive failures in main loop)

Setup:
  1. Message @BotFather on Telegram → /newbot → get your bot token
  2. Message your new bot, then visit:
     https://api.telegram.org/bot<TOKEN>/getUpdates
     to find your chat_id
  3. Add to .env:
     TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
     TELEGRAM_CHAT_ID=123456789

If TELEGRAM_BOT_TOKEN is not set, the notifier silently no-ops.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Rate limit: minimum seconds between messages
_MIN_INTERVAL = 1.0


class TelegramNotifier:
    """
    Sends messages to a Telegram chat via the Bot API.

    Thread-safe, fire-and-forget.  If credentials are missing the
    instance is created but all send() calls are silent no-ops.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        self._token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self._enabled = bool(self._token and self._chat_id)
        self._lock = threading.Lock()
        self._last_send: float = 0.0
        # Daily summary: track the last UTC date we sent on
        self._last_daily_date: Optional[str] = None
        self._daily_hour = 7  # Send daily summary at 07:00 UTC

        if self._enabled:
            self._url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            logger.info("TelegramNotifier enabled")
        else:
            self._url = ""
            logger.info("TelegramNotifier disabled (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Core send ────────────────────────────────────────────────────────

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """
        Send a message to the configured Telegram chat.

        Returns True if the message was sent successfully, False otherwise.
        Never raises — all errors are caught and logged locally.
        """
        if not self._enabled:
            return False

        with self._lock:
            # Simple rate limiting
            now = time.time()
            elapsed = now - self._last_send
            if elapsed < _MIN_INTERVAL:
                time.sleep(_MIN_INTERVAL - elapsed)
            self._last_send = time.time()

        try:
            resp = requests.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": message,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Telegram send failed (HTTP {resp.status_code}): {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")
            return False

    # ── High-level notification helpers ──────────────────────────────────

    def notify_startup(self, environment: str) -> None:
        """Send a system startup notification."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        self.send(
            f"🟢 <b>CoincallTrader started</b>\n"
            f"Environment: {environment}\n"
            f"Time: {ts}"
        )

    def notify_shutdown(self) -> None:
        """Send a system shutdown notification."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        self.send(f"🔴 <b>CoincallTrader stopped</b>\nTime: {ts}")

    def notify_trade_opened(
        self,
        strategy_name: str,
        trade_id: str,
        legs: list,
        entry_cost: float,
    ) -> None:
        """Notify when a new trade is opened."""
        legs_text = "\n".join(
            f"  {'BUY' if leg.side == 1 else 'SELL'} {leg.qty}× {leg.symbol}"
            for leg in legs
        )
        self.send(
            f"📈 <b>Trade Opened</b>\n"
            f"Strategy: {strategy_name}\n"
            f"ID: {trade_id}\n"
            f"{legs_text}\n"
            f"Entry cost: ${entry_cost:.2f}"
        )

    def notify_trade_closed(
        self,
        strategy_name: str,
        trade_id: str,
        pnl: float,
        roi: float,
        hold_minutes: float,
        entry_cost: float,
    ) -> None:
        """Notify when a trade is closed with PnL details."""
        emoji = "✅" if pnl >= 0 else "❌"
        self.send(
            f"{emoji} <b>Trade Closed</b>\n"
            f"Strategy: {strategy_name}\n"
            f"ID: {trade_id}\n"
            f"PnL: <b>${pnl:+.2f}</b> ({roi:+.1f}%)\n"
            f"Hold: {hold_minutes:.1f} min\n"
            f"Entry cost: ${entry_cost:.2f}"
        )

    def maybe_send_daily_summary(
        self,
        equity: float,
        unrealized_pnl: float,
        net_delta: float,
        positions: tuple = (),
    ) -> None:
        """
        Send a daily account summary at a fixed wall-clock time (07:00 UTC).

        Called every 10 s from the main event loop.  Internally gated
        by date string — sends at most once per calendar day, and only
        after self._daily_hour UTC.  Immune to process restarts (uses date,
        not elapsed time).
        """
        now_utc = datetime.now(timezone.utc)

        # Only send after the configured hour
        if now_utc.hour < self._daily_hour:
            return

        today_str = now_utc.strftime("%Y-%m-%d")
        if self._last_daily_date == today_str:
            return  # Already sent today

        self._last_daily_date = today_str

        # Build positions list
        pos_lines = []
        for p in positions:
            side = getattr(p, "side", "?")
            symbol = getattr(p, "symbol", "?")
            qty = getattr(p, "qty", 0)
            pos_lines.append(f"  • {symbol} {qty} {side}")

        ts = now_utc.strftime("%Y-%m-%d %H:%M UTC")
        msg = (
            f"📊 <b>Daily Summary</b> — {ts}\n"
            f"Equity: ${equity:,.2f}\n"
            f"Unrealized PnL: ${unrealized_pnl:+,.2f}\n"
            f"Net delta: {net_delta:+.4f}\n"
            f"Open positions: {len(positions)}"
        )
        if pos_lines:
            msg += "\n" + "\n".join(pos_lines)

        self.send(msg)

    def notify_error(self, message: str) -> None:
        """Send a critical error alert."""
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        self.send(f"🚨 <b>Error</b> ({ts})\n{message}")

    # ── Dashboard control notifications ──────────────────────────────────

    def notify_strategy_paused(self, strategy_name: str) -> None:
        """Notify when a strategy is paused via dashboard."""
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        self.send(f"\u23f8 <b>Strategy paused</b>: {strategy_name}\nTime: {ts}")

    def notify_strategy_resumed(self, strategy_name: str) -> None:
        """Notify when a strategy is resumed via dashboard."""
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        self.send(f"\u25b6 <b>Strategy resumed</b>: {strategy_name}\nTime: {ts}")

    def notify_strategy_stopped(self, strategy_name: str) -> None:
        """Notify when a strategy is stopped via dashboard."""
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        self.send(f"\u23f9 <b>Strategy stopped</b>: {strategy_name}\nTime: {ts}")