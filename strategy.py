#!/usr/bin/env python3
"""
Strategy Framework

Provides infrastructure for running trading strategies as composable,
config-driven routines:

  - TradingContext: Dependency injection container for all services
  - EntryCondition / ExitCondition factory functions
  - StrategyConfig: Declarative strategy definition
  - StrategyRunner: Executes one strategy — checks entries, creates trades

Architecture:
  A strategy is NOT a class to subclass.  It is a StrategyConfig that
  declares *what* to trade, *when* to enter, *when* to exit, and *how*
  to execute.  The StrategyRunner handles the mechanics.

Usage:
    from strategy import build_context, StrategyConfig, StrategyRunner
    from strategy import time_window, weekday_filter, min_available_margin_pct
    from strategy import profit_target, max_loss, max_hold_hours
    from option_selection import LegSpec

    ctx = build_context()

    config = StrategyConfig(
        name="short_strangle_daily",
        legs=[...],
        entry_conditions=[
            time_window(8, 20),
            weekday_filter(["mon", "tue", "wed", "thu"]),
            min_available_margin_pct(50),
        ],
        exit_conditions=[
            profit_target(50),
            max_loss(100),
            max_hold_hours(24),
        ],
        max_concurrent_trades=1,
        cooldown_seconds=3600,
    )

    runner = StrategyRunner(config, ctx)
    ctx.position_monitor.on_update(runner.tick)
    ctx.position_monitor.start()
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from account_manager import AccountSnapshot, PositionMonitor
from exchanges import build_exchange
from execution.currency import Currency
from execution.profiles import ExecutionProfile, get_profile, load_profiles
from option_selection import LegSpec, resolve_legs
from lifecycle_engine import LifecycleEngine
from trade_lifecycle import (
    ExitCondition,
    RFQParams,
    TradeLifecycle,
    TradeState,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Dependency Injection
# =============================================================================

@dataclass
class TradingContext:
    """
    Container holding one instance of every service.

    Strategies and the orchestration loop receive this instead of
    importing module-level globals.  For tests, individual services
    can be replaced with mocks.
    """
    auth: Any                          # ExchangeAuth adapter
    market_data: Any                    # ExchangeMarketData adapter
    executor: Any                       # ExchangeExecutor adapter
    rfq_executor: Any                   # ExchangeRFQExecutor adapter
    account_manager: Any                # ExchangeAccountManager adapter
    position_monitor: PositionMonitor
    lifecycle_manager: LifecycleEngine
    persistence: Optional[Any] = None   # TradeStatePersistence (optional)
    profiles: Dict[str, ExecutionProfile] = field(default_factory=dict)


def build_context(
    poll_interval: int = 10,
    rfq_notional_threshold: float = 50000.0,
) -> TradingContext:
    """
    Construct a fully-wired TradingContext from config.py settings.

    The PositionMonitor is created but NOT started — caller must invoke
    ctx.position_monitor.start() when ready.
    """
    components = build_exchange()
    auth = components['auth']
    market_data_svc = components['market_data']
    executor = components['executor']
    rfq_executor = components['rfq_executor']
    account_mgr = components['account_manager']
    state_map = components['state_map']
    monitor = PositionMonitor(account_manager=account_mgr, poll_interval=poll_interval, auth=auth)
    lifecycle_mgr = LifecycleEngine(
        rfq_notional_threshold=rfq_notional_threshold,
        account_manager=account_mgr,
        executor=executor,
        rfq_executor=rfq_executor,
        market_data=market_data_svc,
        exchange_state_map=state_map,
        expected_denomination=Currency.BTC,
    )

    # Wire lifecycle ticks to position monitor
    monitor.on_update(lifecycle_mgr.tick)

    # Load execution profiles from TOML
    try:
        profiles = load_profiles()
    except FileNotFoundError:
        logger.warning("execution_profiles.toml not found — no profiles loaded")
        profiles = {}

    return TradingContext(
        auth=auth,
        market_data=market_data_svc,
        executor=executor,
        rfq_executor=rfq_executor,
        account_manager=account_mgr,
        position_monitor=monitor,
        lifecycle_manager=lifecycle_mgr,
        profiles=profiles,
    )


# =============================================================================
# Condition Type Aliases
# =============================================================================

# EntryCondition: callable checked before opening a trade.
# Takes only an AccountSnapshot (no trade reference yet).
EntryCondition = Callable[[AccountSnapshot], bool]

# ExitCondition is imported from trade_lifecycle (canonical definition):
#   Callable[[AccountSnapshot, TradeLifecycle], bool]


def min_available_margin_pct(pct: float) -> EntryCondition:
    """Block entry when available margin is less than pct% of equity."""
    def _check(account: AccountSnapshot) -> bool:
        if account.equity <= 0:
            return False
        margin_pct = (account.available_margin / account.equity) * 100
        ok = margin_pct >= pct
        if not ok:
            logger.debug(f"min_available_margin_pct({pct}%): margin={margin_pct:.1f}% — blocked")
        return ok
    _check.__name__ = f"min_available_margin_pct({pct}%)"
    return _check


def time_window(start_hour: int, end_hour: int, tz: str = "UTC") -> EntryCondition:
    """
    Only allow entry between start_hour and end_hour (inclusive start,
    exclusive end).  Supports wrapping past midnight (e.g., 22 -> 06).
    Times are in UTC by default.
    """
    def _check(account: AccountSnapshot) -> bool:
        hour = datetime.now(timezone.utc).hour
        if start_hour <= end_hour:
            ok = start_hour <= hour < end_hour
        else:
            ok = hour >= start_hour or hour < end_hour
        if not ok:
            logger.debug(f"time_window({start_hour}-{end_hour}): hour={hour} — blocked")
        return ok
    _check.__name__ = f"time_window({start_hour}-{end_hour} {tz})"
    return _check


def weekday_filter(days: List[str]) -> EntryCondition:
    """
    Only allow entry on specified weekdays.

    Args:
        days: List of day abbreviations, e.g. ["mon", "tue", "wed"]
    """
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    allowed = set()
    for d in days:
        abbr = d.lower()[:3]
        if abbr in day_names:
            allowed.add(day_names.index(abbr))
        else:
            raise ValueError(f"Unknown weekday: {d}")

    def _check(account: AccountSnapshot) -> bool:
        today = datetime.now(timezone.utc).weekday()
        ok = today in allowed
        if not ok:
            logger.debug(f"weekday_filter({days}): today={day_names[today]} — blocked")
        return ok
    _check.__name__ = f"weekday_filter({days})"
    return _check


def min_equity(amount: float) -> EntryCondition:
    """Block entry when account equity is below a minimum USD amount."""
    def _check(account: AccountSnapshot) -> bool:
        ok = account.equity >= amount
        if not ok:
            logger.debug(f"min_equity(${amount}): equity=${account.equity:.2f} — blocked")
        return ok
    _check.__name__ = f"min_equity(${amount})"
    return _check


def max_account_delta(threshold: float) -> EntryCondition:
    """Block entry when absolute account delta exceeds threshold."""
    def _check(account: AccountSnapshot) -> bool:
        ok = abs(account.net_delta) <= threshold
        if not ok:
            logger.debug(
                f"max_account_delta({threshold}): delta={account.net_delta:+.4f} — blocked"
            )
        return ok
    _check.__name__ = f"max_account_delta({threshold})"
    return _check


def max_margin_utilization(pct: float) -> EntryCondition:
    """Block entry when margin utilisation exceeds pct%."""
    def _check(account: AccountSnapshot) -> bool:
        ok = account.margin_utilization <= pct
        if not ok:
            logger.debug(
                f"max_margin_utilization({pct}%): util={account.margin_utilization:.1f}% — blocked"
            )
        return ok
    _check.__name__ = f"max_margin_utilization({pct}%)"
    return _check


def no_existing_position_in(symbols: List[str]) -> EntryCondition:
    """Block entry if account already holds a position in any of the given symbols."""
    def _check(account: AccountSnapshot) -> bool:
        for sym in symbols:
            if account.get_position(sym) is not None:
                logger.debug(f"no_existing_position_in: have {sym} — blocked")
                return False
        return True
    _check.__name__ = f"no_existing_position_in({symbols})"
    return _check


# =============================================================================
# Exit Condition Factories
# =============================================================================

def profit_target(pct: float, pnl_mode: str = "mark") -> ExitCondition:
    """
    Close when structure PnL exceeds +pct% of entry cost.

    Args:
        pct: Profit target percentage of entry cost.
        pnl_mode: How to evaluate PnL.
            "mark"       — use exchange mark/mid prices (default, fast).
            "executable" — use live orderbook best bid/ask to estimate
                           what closing would actually yield.  Safer for
                           illiquid or wide-spread options (e.g. short DTE).
                           If any orderbook is unavailable, the condition
                           silently skips that tick (no false triggers).

    Example: profit_target(50, pnl_mode="executable")
    """
    label = f"profit_target({pct}%,{pnl_mode})"

    def _check(account: AccountSnapshot, trade: "TradeLifecycle") -> bool:
        entry = trade.total_entry_cost()
        if entry == 0:
            return False

        if pnl_mode == "executable":
            pnl = trade.executable_pnl()
            if pnl is None:
                return False  # orderbook unavailable — skip this tick
        else:
            pnl = trade.structure_pnl(account)

        ratio = (pnl / abs(entry)) * 100
        triggered = ratio >= pct
        if triggered:
            logger.info(
                f"[{trade.id}] {label} triggered: PnL ratio={ratio:.1f}% "
                f"(pnl=${pnl:.4f}, entry=${entry:.4f})"
            )
        return triggered

    _check.__name__ = label
    return _check


def max_loss(pct: float, pnl_mode: str = "mark") -> ExitCondition:
    """
    Close when structure loss exceeds pct% of entry cost.

    Args:
        pct: Loss threshold percentage of entry cost.
        pnl_mode: How to evaluate PnL.
            "mark"       — use exchange mark/mid prices (default, fast).
            "executable" — use live orderbook best bid/ask to estimate
                           what closing would actually yield.  Safer for
                           illiquid or wide-spread options (e.g. short DTE).
                           If any orderbook is unavailable, the condition
                           silently skips that tick (no false triggers).

    Example: max_loss(100, pnl_mode="executable")
    """
    label = f"max_loss({pct}%,{pnl_mode})"

    def _check(account: AccountSnapshot, trade: "TradeLifecycle") -> bool:
        entry = trade.total_entry_cost()
        if entry == 0:
            return False

        if pnl_mode == "executable":
            pnl = trade.executable_pnl()
            if pnl is None:
                return False  # orderbook unavailable — skip this tick
        else:
            pnl = trade.structure_pnl(account)

        ratio = (pnl / abs(entry)) * 100
        triggered = ratio <= -pct
        if triggered:
            logger.info(
                f"[{trade.id}] {label} triggered: PnL ratio={ratio:.1f}% "
                f"(pnl=${pnl:.4f}, entry=${entry:.4f})"
            )
        return triggered

    _check.__name__ = label
    return _check


def max_hold_hours(hours: float) -> ExitCondition:
    """Close when position has been open longer than N hours."""
    def _check(account: AccountSnapshot, trade: "TradeLifecycle") -> bool:
        hold = trade.hold_seconds
        if hold is None:
            return False
        triggered = hold >= hours * 3600
        if triggered:
            logger.info(f"[{trade.id}] max_hold_hours({hours}h) triggered: held {hold/3600:.1f}h")
        return triggered
    _check.__name__ = f"max_hold_hours({hours}h)"
    return _check


def time_exit(hour: int, minute: int = 0) -> ExitCondition:
    """
    Close the trade at or after a specific UTC wall-clock time.

    Args:
        hour: UTC hour (0-23).
        minute: UTC minute (0-59), default 0.

    Example:
        time_exit(19, 0)  → close at or after 19:00 UTC
    """
    def _check(account: AccountSnapshot, trade: "TradeLifecycle") -> bool:
        now = datetime.now(timezone.utc)
        cutoff = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        triggered = now >= cutoff
        if triggered:
            logger.info(
                f"[{trade.id}] time_exit({hour:02d}:{minute:02d} UTC) triggered: "
                f"now={now.strftime('%H:%M:%S')}"
            )
        return triggered
    _check.__name__ = f"time_exit({hour:02d}:{minute:02d} UTC)"
    return _check


def utc_time_window(
    start: "datetime",
    end: "datetime",
) -> EntryCondition:
    """
    Only allow entry when UTC time is within [start, end).

    Unlike time_window() which uses hour-of-day, this uses full datetimes
    so it works for minute-precision scheduled entries.

    Args:
        start: Earliest UTC datetime to allow entry.
        end: Latest UTC datetime (exclusive) to allow entry.
    """
    def _check(account: AccountSnapshot) -> bool:
        now = datetime.now(timezone.utc)
        ok = start <= now < end
        if not ok:
            logger.debug(
                f"utc_time_window({start.strftime('%H:%M')}-{end.strftime('%H:%M')}): "
                f"now={now.strftime('%H:%M:%S')} — blocked"
            )
        return ok
    _check.__name__ = (
        f"utc_time_window({start.strftime('%H:%M')}-{end.strftime('%H:%M')})"
    )
    return _check


def utc_datetime_exit(dt: "datetime") -> ExitCondition:
    """
    Close the trade at or after a specific UTC datetime.

    Unlike time_exit() which uses hour-of-day, this uses a full datetime
    so it is unambiguous across midnight boundaries and supports
    minute-precision scheduling.

    Args:
        dt: UTC datetime at which to trigger exit.
    """
    def _check(account: AccountSnapshot, trade: "TradeLifecycle") -> bool:
        now = datetime.now(timezone.utc)
        triggered = now >= dt
        if triggered:
            logger.info(
                f"[{trade.id}] utc_datetime_exit({dt.strftime('%H:%M')}) triggered: "
                f"now={now.strftime('%H:%M:%S')}"
            )
        return triggered
    _check.__name__ = f"utc_datetime_exit({dt.strftime('%Y-%m-%d %H:%M')})"
    return _check


def account_delta_limit(threshold: float) -> ExitCondition:
    """Close when account-wide absolute delta exceeds threshold."""
    def _check(account: AccountSnapshot, trade: "TradeLifecycle") -> bool:
        triggered = abs(account.net_delta) > threshold
        if triggered:
            logger.info(
                f"[{trade.id}] account_delta_limit({threshold}) triggered: "
                f"account delta={account.net_delta:+.4f}"
            )
        return triggered
    _check.__name__ = f"account_delta_limit({threshold})"
    return _check


def structure_delta_limit(threshold: float) -> ExitCondition:
    """Close when this trade's absolute delta exceeds threshold."""
    def _check(account: AccountSnapshot, trade: "TradeLifecycle") -> bool:
        d = trade.structure_delta(account)
        triggered = abs(d) > threshold
        if triggered:
            logger.info(
                f"[{trade.id}] structure_delta_limit({threshold}) triggered: "
                f"structure delta={d:+.4f}"
            )
        return triggered
    _check.__name__ = f"structure_delta_limit({threshold})"
    return _check


def leg_greek_limit(leg_index: int, greek: str, op: str, value: float) -> ExitCondition:
    """
    Close when a specific leg's Greek crosses a threshold.

    Args:
        leg_index: Index into open_legs (0 = first leg)
        greek: "delta", "gamma", "theta", or "vega"
        op: ">" or "<"
        value: Threshold value

    Example: leg_greek_limit(0, "theta", "<", -5.0)
             → close when first leg's theta drops below -5
    """
    def _check(account: AccountSnapshot, trade: "TradeLifecycle") -> bool:
        if leg_index >= len(trade.open_legs):
            return False
        leg = trade.open_legs[leg_index]
        pos = account.get_position(leg.symbol)
        if pos is None:
            return False
        actual = getattr(pos, greek, 0.0)
        if op == ">":
            triggered = actual > value
        elif op == "<":
            triggered = actual < value
        else:
            return False
        if triggered:
            logger.info(
                f"[{trade.id}] leg_greek_limit(leg[{leg_index}].{greek} {op} {value}) "
                f"triggered: actual={actual:+.6f}"
            )
        return triggered
    _check.__name__ = f"leg[{leg_index}].{greek}{op}{value}"
    return _check


# =============================================================================
# Strategy Configuration
# =============================================================================

@dataclass
class StrategyConfig:
    """
    Declarative strategy definition.

    Combines *what* to trade (legs), *when* to enter (entry_conditions),
    *when* to exit (exit_conditions), *how* to execute (execution_mode),
    and operational limits (max_concurrent_trades, cooldown).

    Attributes:
        name: Unique strategy identifier (used as strategy_id on trades)
        legs: LegSpec templates — resolved to concrete symbols at trade time
        entry_conditions: Callables that must ALL return True to open a trade
        exit_conditions: Callables — ANY returning True triggers a close
        execution_mode: "limit", "rfq", or "auto" (notional-based routing)
        execution_params: Optional ExecutionParams for "limit" mode (phased pricing, timeouts)
        rfq_params: Optional RFQParams for "rfq" mode (timeout, improvement, fallback)
        rfq_action: "buy" or "sell" — what to do on open (close is the reverse)
        max_concurrent_trades: Maximum active trades for this strategy
        cooldown_seconds: Minimum seconds between trade opens
        check_interval_seconds: How often to evaluate entry conditions
        metadata: Arbitrary context passed to each trade
    """
    name: str
    legs: List[LegSpec]
    entry_conditions: List[EntryCondition] = field(default_factory=list)
    exit_conditions: List[ExitCondition] = field(default_factory=list)
    execution_mode: str = "auto"
    execution_params: Optional[Any] = None  # legacy ExecutionParams (unused by new profiles)
    execution_profile: Optional[str] = None
    rfq_params: Optional[RFQParams] = None
    rfq_action: str = "buy"
    max_concurrent_trades: int = 1
    max_trades_per_day: int = 0          # 0 = unlimited
    cooldown_seconds: float = 0.0
    check_interval_seconds: float = 60.0
    on_trade_closed: Optional[Callable] = None   # (TradeLifecycle, AccountSnapshot) -> None
    on_trade_opened: Optional[Callable] = None   # (TradeLifecycle, AccountSnapshot) -> None
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Strategy Runner
# =============================================================================

class StrategyRunner:
    """
    Executes a single strategy: evaluates entry conditions, resolves legs,
    creates trades, and lets the LifecycleEngine handle execution and exits.

    Register with the PositionMonitor to receive periodic ticks:

        runner = StrategyRunner(config, ctx)
        ctx.position_monitor.on_update(runner.tick)

    The runner does NOT run its own thread or event loop — it piggybacks
    on the PositionMonitor's polling cycle via the tick() callback.
    """

    def __init__(self, config: StrategyConfig, ctx: TradingContext):
        self.config = config
        self.ctx = ctx
        self._strategy_id: str = config.name
        self._last_check_time: float = 0.0
        self._enabled: bool = True
        self._known_closed_ids: set = set()   # tracks already-handled closed trades
        self._known_open_ids: set = set()     # tracks already-handled opened trades

        # Apply per-slot execution profile override from env
        env_profile = os.environ.get("EXECUTION_PROFILE")
        if env_profile:
            self.config.execution_profile = env_profile

        # Collect per-slot execution overrides from EXECUTION_OVERRIDE_* env vars
        env_overrides = {}
        prefix = "EXECUTION_OVERRIDE_"
        for key, val in os.environ.items():
            if key.startswith(prefix):
                override_key = key[len(prefix):]
                try:
                    env_overrides[override_key] = float(val)
                except ValueError:
                    env_overrides[override_key] = val
        if env_overrides:
            existing = self.config.metadata.get("execution_overrides", {})
            existing.update(env_overrides)
            self.config.metadata["execution_overrides"] = existing

        logger.info(
            f"StrategyRunner '{config.name}' initialised "
            f"(max_trades={config.max_concurrent_trades}, "
            f"max_per_day={config.max_trades_per_day}, "
            f"cooldown={config.cooldown_seconds}s, "
            f"check_interval={config.check_interval_seconds}s)"
        )

    # -- Properties -----------------------------------------------------------

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @property
    def active_trades(self) -> List[TradeLifecycle]:
        """Active trades belonging to this strategy."""
        return self.ctx.lifecycle_manager.active_trades_for_strategy(self._strategy_id)

    @property
    def all_trades(self) -> List[TradeLifecycle]:
        """All trades (any state) belonging to this strategy."""
        return self.ctx.lifecycle_manager.get_trades_for_strategy(self._strategy_id)

    @property
    def is_done(self) -> bool:
        """True when this runner has exhausted its daily quota and has no active trades."""
        if self.active_trades:
            return False
        if self.config.max_trades_per_day <= 0:
            return False  # unlimited — never "done"
        today = datetime.now(timezone.utc).date()
        today_count = sum(
            1 for t in self.all_trades
            if datetime.fromtimestamp(t.created_at, tz=timezone.utc).date() == today
        )
        return today_count >= self.config.max_trades_per_day

    # -- Tick -----------------------------------------------------------------

    def tick(self, account: AccountSnapshot) -> None:
        """
        Called on every PositionMonitor update.

        Throttled by check_interval_seconds.  Evaluates entry conditions
        and opens a new trade if all gates pass.  Also fires
        on_trade_closed for newly completed trades.
        """
        # Always process trade open/close events, even when entry is paused
        self._check_opened_trades(account)
        self._check_closed_trades(account)

        if not self._enabled:
            return

        # Skip new entries when exchange is unreachable (exits still managed
        # by LifecycleEngine on its own tick, which also runs from callbacks).
        if hasattr(self.ctx, 'auth') and hasattr(self.ctx.auth, 'reachable'):
            if not self.ctx.auth.reachable:
                logger.debug(f"[{self._strategy_id}] exchange unreachable — skipping entry")
                return

        if not self._enabled:
            return

        now = time.time()
        if now - self._last_check_time < self.config.check_interval_seconds:
            return
        self._last_check_time = now

        # Log active-trade PnL summary (brief, once per check interval)
        for trade in self.active_trades:
            if trade.state == TradeState.OPEN:
                pnl = trade.structure_pnl(account)
                hold = trade.hold_seconds or 0
                logger.debug(
                    f"[{self._strategy_id}] trade {trade.id} OPEN "
                    f"hold={hold:.0f}s PnL={pnl:+.4f}"
                )

        if self._should_open(account):
            self._open_trade()

    # -- Entry Evaluation -----------------------------------------------------

    def _should_open(self, account: AccountSnapshot) -> bool:
        """Evaluate all entry gates.  All must pass to open."""
        logger.debug(f"[{self._strategy_id}] evaluating entry conditions...")

        # Gate 1: max concurrent trades
        active = self.active_trades
        if len(active) >= self.config.max_concurrent_trades:
            logger.debug(
                f"[{self._strategy_id}] max_concurrent_trades "
                f"({len(active)}/{self.config.max_concurrent_trades}) — skip"
            )
            return False

        # Gate 2: cooldown since last trade
        if self.config.cooldown_seconds > 0:
            all_trades = self.all_trades
            if all_trades:
                last_created = max(t.created_at for t in all_trades)
                elapsed = time.time() - last_created
                if elapsed < self.config.cooldown_seconds:
                    logger.debug(
                        f"[{self._strategy_id}] cooldown "
                        f"({elapsed:.0f}/{self.config.cooldown_seconds:.0f}s) — skip"
                    )
                    return False

        # Gate 3: max trades per calendar day (UTC)
        if self.config.max_trades_per_day > 0:
            today = datetime.now(timezone.utc).date()
            today_count = sum(
                1 for t in self.all_trades
                if datetime.fromtimestamp(t.created_at, tz=timezone.utc).date() == today
            )
            if today_count >= self.config.max_trades_per_day:
                logger.debug(
                    f"[{self._strategy_id}] max_trades_per_day "
                    f"({today_count}/{self.config.max_trades_per_day}) — skip"
                )
                return False

        # Gate 4: user-defined entry conditions (all must pass)
        for cond in self.config.entry_conditions:
            try:
                if not cond(account):
                    cond_name = getattr(cond, "__name__", repr(cond))
                    logger.debug(f"[{self._strategy_id}] entry blocked by {cond_name}")
                    return False
            except Exception as e:
                logger.error(f"[{self._strategy_id}] entry condition error: {e}")
                return False  # Fail-safe: don't trade on error

        logger.info(f"[{self._strategy_id}] ✓ all entry gates passed — opening trade")
        return True

    # -- Trade Creation -------------------------------------------------------

    def _open_trade(self) -> None:
        """Resolve leg specs to concrete symbols and open a trade."""
        try:
            legs = resolve_legs(self.config.legs, self.ctx.market_data)
            logger.info(
                f"[{self._strategy_id}] resolved {len(legs)} legs: "
                f"{[l.symbol for l in legs]}"
            )

            exec_mode = (
                None if self.config.execution_mode == "auto"
                else self.config.execution_mode
            )

            trade = self.ctx.lifecycle_manager.create(
                legs=legs,
                exit_conditions=list(self.config.exit_conditions),
                execution_mode=exec_mode,
                rfq_action=self.config.rfq_action,
                execution_params=self.config.execution_params,
                rfq_params=self.config.rfq_params,
                strategy_id=self._strategy_id,
                metadata={"strategy": self._strategy_id, **self.config.metadata},
            )

            # Resolve named execution profile → stash on trade for Router
            if self.config.execution_profile:
                profile = get_profile(
                    self.config.execution_profile, self.ctx.profiles,
                )
                overrides = self.config.metadata.get("execution_overrides")
                if overrides:
                    profile = profile.apply_overrides(overrides)
                trade.metadata["_execution_profile"] = profile

            logger.info(f"[{self._strategy_id}] opening trade {trade.id}")
            self.ctx.lifecycle_manager.open(trade.id)

        except Exception as e:
            logger.error(f"[{self._strategy_id}] failed to open trade: {e}")

    # -- Controls -------------------------------------------------------------

    def enable(self) -> None:
        """Resume entry evaluation."""
        self._enabled = True
        logger.info(f"[{self._strategy_id}] enabled")

    def disable(self) -> None:
        """Pause entry evaluation (existing trades still managed by lifecycle)."""
        self._enabled = False
        logger.info(f"[{self._strategy_id}] disabled")

    def stop(self) -> None:
        """Disable and force-close all active trades."""
        self._enabled = False
        active = self.active_trades
        for trade in active:
            self.ctx.lifecycle_manager.force_close(trade.id)
        logger.info(
            f"[{self._strategy_id}] stopped — "
            f"force-closed {len(active)} active trade(s)"
        )

    # -- Status ---------------------------------------------------------------

    def status(self, account: Optional[AccountSnapshot] = None) -> str:
        """Human-readable status report for this strategy."""
        active = self.active_trades
        all_t = self.all_trades
        lines = [
            f"Strategy: {self._strategy_id}",
            f"  Enabled: {self._enabled}",
            f"  Active trades: {len(active)}/{self.config.max_concurrent_trades}",
            f"  Total trades: {len(all_t)}",
        ]
        for trade in active:
            lines.append(f"  {trade.summary(account)}")
        return "\n".join(lines)

    # -- Trade Open Tracking --------------------------------------------------

    def _check_opened_trades(self, account: AccountSnapshot) -> None:
        """
        Detect trades that transitioned to OPEN since the last tick.
        Fire the on_trade_opened callback for each newly-opened trade.
        """
        if not self.config.on_trade_opened:
            return
        for trade in self.all_trades:
            if trade.state == TradeState.OPEN and trade.id not in self._known_open_ids:
                self._known_open_ids.add(trade.id)
                try:
                    self.config.on_trade_opened(trade, account)
                except Exception as e:
                    logger.error(
                        f"[{self._strategy_id}] on_trade_opened callback error: {e}"
                    )

    # -- Trade Close Tracking -------------------------------------------------

    def _check_closed_trades(self, account: AccountSnapshot) -> None:
        """
        Detect trades that transitioned to CLOSED or FAILED since the last tick.
        Fire the on_trade_closed callback for each newly-closed trade.
        Persist completed trades to history log if persistence is available.
        """
        for trade in self.all_trades:
            if trade.state in (TradeState.CLOSED, TradeState.FAILED):
                if trade.id not in self._known_closed_ids:
                    self._known_closed_ids.add(trade.id)
                    pnl = trade.realized_pnl if trade.realized_pnl is not None else 0.0
                    logger.info(
                        f"[{self._strategy_id}] trade {trade.id} → {trade.state.value} "
                        f"(PnL={pnl:+.4f})"
                    )
                    # Persist to trade history log
                    if self.ctx.persistence and trade.state == TradeState.CLOSED:
                        try:
                            self.ctx.persistence.save_completed_trade(trade)
                        except Exception as e:
                            logger.error(
                                f"[{self._strategy_id}] failed to persist trade {trade.id}: {e}"
                            )
                    if self.config.on_trade_closed:
                        try:
                            self.config.on_trade_closed(trade, account)
                        except Exception as e:
                            logger.error(
                                f"[{self._strategy_id}] on_trade_closed callback error: {e}"
                            )

    # -- Stats ----------------------------------------------------------------

    @property
    def stats(self) -> Dict[str, Any]:
        """
        Aggregate win/loss statistics for this strategy.

        Returns a dict with keys: total, wins, losses, win_rate,
        total_pnl, avg_hold_seconds, today_trades, today_pnl.
        """
        closed = [
            t for t in self.all_trades if t.state == TradeState.CLOSED
        ]
        if not closed:
            return {
                "total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "avg_hold_seconds": 0.0,
                "today_trades": 0, "today_pnl": 0.0,
            }

        total_hold = 0.0
        total_pnl = 0.0
        wins = 0
        losses = 0
        today = datetime.now(timezone.utc).date()
        today_count = 0
        today_pnl = 0.0

        for t in closed:
            pnl = t.realized_pnl if t.realized_pnl is not None else 0.0
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            if t.hold_seconds is not None:
                total_hold += t.hold_seconds
            if datetime.fromtimestamp(t.created_at, tz=timezone.utc).date() == today:
                today_count += 1
                today_pnl += pnl

        return {
            "total": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(closed) if closed else 0.0,
            "total_pnl": total_pnl,
            "avg_hold_seconds": total_hold / len(closed) if closed else 0.0,
            "today_trades": today_count,
            "today_pnl": today_pnl,
        }
