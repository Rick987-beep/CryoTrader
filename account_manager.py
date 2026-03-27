#!/usr/bin/env python3
"""
Account Manager Module

Handles all account-related operations:
  - Account info (balance, equity, margin)
  - Position retrieval with Greeks and P&L
  - Open order queries
  - Position monitoring via PositionMonitor

The PositionMonitor provides a typed, thread-safe snapshot of all positions
and account state, refreshed on a configurable interval (default 10s).
Strategies can register callbacks via on_update() to react to changes.

Environment-agnostic - works the same for testnet and production.
The environment is controlled via config.py.
"""

import logging
import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Any, Optional, Callable
from config import BASE_URL, API_KEY, API_SECRET
from auth import CoincallAuth

logger = logging.getLogger(__name__)


# =============================================================================
# Data Classes - Typed snapshots for positions and account state
# =============================================================================

@dataclass(frozen=True)
class PositionSnapshot:
    """
    Point-in-time view of a single position.
    
    Immutable so it can be safely shared across threads.
    The 'side' field is normalised to "long" or "short" from the API's
    tradeSide (1=buy/long, 2=sell/short).
    """
    position_id: str
    symbol: str
    qty: float
    side: str                # "long" or "short"
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    roi: float
    # Greeks
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float  = 0.0
    timestamp: float = 0.0


@dataclass(frozen=True)
class AccountSnapshot:
    """
    Point-in-time view of account state plus all open positions.
    
    Provides aggregated Greeks across every position and key margin metrics.
    Immutable for thread-safety.
    """
    # Account fields
    equity: float = 0.0
    available_margin: float = 0.0
    initial_margin: float = 0.0
    maintenance_margin: float = 0.0
    unrealized_pnl: float = 0.0
    margin_utilization: float = 0.0    # % of equity used as margin
    # Positions
    positions: tuple = ()               # tuple of PositionSnapshot (frozen)
    # Aggregated Greeks
    net_delta: float = 0.0
    net_gamma: float = 0.0
    net_theta: float = 0.0
    net_vega: float  = 0.0
    timestamp: float = 0.0
    
    @property
    def position_count(self) -> int:
        return len(self.positions)
    
    def get_position(self, symbol: str) -> Optional[PositionSnapshot]:
        """Find a position by symbol, or None."""
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None
    
    def summary_str(self) -> str:
        """Human-readable one-liner."""
        return (
            f"Equity=${self.equity:.2f}  "
            f"UPnL=${self.unrealized_pnl:.2f}  "
            f"Margin={self.margin_utilization:.1f}%  "
            f"Δ={self.net_delta:.4f}  "
            f"Γ={self.net_gamma:.6f}  "
            f"Θ={self.net_theta:.4f}  "
            f"V={self.net_vega:.4f}  "
            f"Positions={self.position_count}"
        )


class AccountManager:
    """Manages account operations with proper API authentication"""

    def __init__(self):
        """Initialize account manager with authenticated API client"""
        self.auth = CoincallAuth(API_KEY, API_SECRET, BASE_URL)
        self.last_update = None
        
        # Cache for account data
        self._account_info_cache = None
        self._positions_cache = None
        self._orders_cache = None

    def get_account_info(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get account summary information
        
        Args:
            force_refresh: Skip cache and fetch fresh data
            
        Returns:
            Dict with account information or None on error
        """
        # Return cached data if available and not forcing refresh
        if self._account_info_cache and not force_refresh:
            if time.time() - self.last_update < 30:  # 30 second cache
                return self._account_info_cache

        try:
            response = self.auth.get('/open/account/summary/v1')
            
            if self.auth.is_successful(response):
                data = response.get('data', {})
                self._account_info_cache = {
                    'user_id': data.get('userId'),
                    'total_balance_btc': float(data.get('totalBtcValue', 0)),
                    'total_balance_usd': float(data.get('totalDollarValue', 0)),
                    'total_balance_usdt': float(data.get('totalUsdtValue', 0)),
                    'equity': float(data.get('equity', 0)),
                    'available_margin': float(data.get('availableMargin', 0)),
                    'initial_margin': float(data.get('imAmount', 0)),
                    'maintenance_margin': float(data.get('mmAmount', 0)),
                    'unrealized_pnl': float(data.get('unrealizedPnL', 0)),
                    'margin_ratio_initial': float(data.get('imRatio', 0)),
                    'margin_ratio_maintenance': float(data.get('mmRatio', 0)),
                    'timestamp': time.time()
                }
                self.last_update = time.time()
                logger.debug(f"Account info retrieved: ${self._account_info_cache['available_margin']:.2f} available")
                return self._account_info_cache
            else:
                logger.error(f"Failed to get account info: {response.get('msg')}")
                return None
        
        except Exception as e:
            logger.error(f"Exception getting account info: {e}")
            return None

    def get_positions(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Get all open option positions
        
        Args:
            force_refresh: Skip cache and fetch fresh data
            
        Returns:
            List of position dictionaries
        """
        # Return cached data if available
        if self._positions_cache and not force_refresh:
            if time.time() - self.last_update < 30:
                return self._positions_cache

        try:
            response = self.auth.get('/open/option/position/get/v1')
            
            if self.auth.is_successful(response):
                positions_data = response.get('data', [])
                
                if isinstance(positions_data, list):
                    self._positions_cache = []
                    for pos in positions_data:
                        position = {
                            'position_id': pos.get('positionId'),
                            'symbol': pos.get('symbol'),
                            'display_name': pos.get('displayName'),
                            'qty': float(pos.get('qty', 0)),
                            'avg_price': float(pos.get('avgPrice', 0)),
                            'mark_price': float(pos.get('markPrice', 0)),
                            'last_price': float(pos.get('lastPrice', 0)),
                            'index_price': float(pos.get('indexPrice', 0)),
                            'value': float(pos.get('value', 0)),
                            # Two PnL flavours: by last traded price, and by mark price.
                            # Mark-price PnL is the standard for options.
                            'unrealized_pnl': float(pos.get('upnlByMarkPrice', 0)),
                            'roi': float(pos.get('roiByMarkPrice', 0)),
                            'upnl_by_last': float(pos.get('upnl', 0)),
                            'roi_by_last': float(pos.get('roi', 0)),
                            'trade_side': pos.get('tradeSide'),  # 1: buy, 2: sell
                            'delta': float(pos.get('delta', 0)),
                            'gamma': float(pos.get('gamma', 0)),
                            'vega': float(pos.get('vega', 0)),
                            'theta': float(pos.get('theta', 0)),
                        }
                        self._positions_cache.append(position)
                    
                    logger.debug(f"Retrieved {len(self._positions_cache)} open positions")
                    self.last_update = time.time()
                    return self._positions_cache
            
            logger.error(f"Failed to get positions: {response.get('msg')}")
            return []
        
        except Exception as e:
            logger.error(f"Exception getting positions: {e}")
            return []

    def get_open_orders(self, symbol: Optional[str] = None, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Get all open orders, optionally filtered by symbol
        
        Args:
            symbol: Optional symbol to filter by
            force_refresh: Skip cache and fetch fresh data
            
        Returns:
            List of open order dictionaries
        """
        # Return cached data if available
        if self._orders_cache and not force_refresh:
            if time.time() - self.last_update < 30:
                orders = self._orders_cache
                if symbol:
                    return [o for o in orders if o['symbol'] == symbol]
                return orders

        try:
            response = self.auth.get('/open/option/order/pending/v1')
            
            if self.auth.is_successful(response):
                orders_data = response.get('data', {}).get('list', [])
                self._orders_cache = []
                
                for order in orders_data:
                    # Helper function to safely convert to float
                    def safe_float(val, default=0):
                        try:
                            return float(val) if val is not None else default
                        except (ValueError, TypeError):
                            return default
                    
                    order_info = {
                        'order_id': order.get('orderId'),
                        'client_order_id': order.get('clientOrderId'),
                        'symbol': order.get('symbol'),
                        'display_name': order.get('displayName'),
                        'qty': safe_float(order.get('qty')),
                        'remaining_qty': safe_float(order.get('remainQty')),
                        'filled_qty': safe_float(order.get('fillQty')),
                        'price': safe_float(order.get('price')),
                        'avg_price': safe_float(order.get('avgPrice')),
                        'trade_side': order.get('tradeSide'),  # 1: buy, 2: sell
                        'trade_type': order.get('tradeType'),  # 1: limit, 2: market, etc
                        'state': order.get('state'),  # Order status
                        'create_time': order.get('createTime'),
                        'update_time': order.get('updateTime'),
                    }
                    self._orders_cache.append(order_info)
                
                logger.debug(f"Retrieved {len(self._orders_cache)} open orders")
                self.last_update = time.time()
                
                if symbol:
                    return [o for o in self._orders_cache if o['symbol'] == symbol]
                return self._orders_cache
            
            logger.error(f"Failed to get open orders: {response.get('msg')}")
            return []
        
        except Exception as e:
            logger.error(f"Exception getting open orders: {e}")
            return []

    def get_user_info(self) -> Optional[Dict[str, Any]]:
        """
        Get user profile information
        
        Returns:
            Dict with user info or None on error
        """
        try:
            response = self.auth.get('/open/user/info/v1')
            
            if self.auth.is_successful(response):
                data = response.get('data', {})
                return {
                    'user_id': data.get('userId'),
                    'name': data.get('name'),
                    'email': data.get('email'),
                }
            else:
                logger.error(f"Failed to get user info: {response.get('msg')}")
                return None
        
        except Exception as e:
            logger.error(f"Exception getting user info: {e}")
            return None

    def get_account_summary(self) -> Dict[str, Any]:
        """
        Get comprehensive account summary
        
        Returns:
            Dict with all account information
        """
        return {
            'account_info': self.get_account_info(force_refresh=True),
            'positions': self.get_positions(force_refresh=True),
            'open_orders': self.get_open_orders(force_refresh=True),
            'user_info': self.get_user_info(),
        }

    def get_risk_metrics(self) -> Dict[str, Any]:
        """
        Get risk metrics for the account
        
        Returns:
            Dict with risk calculation results
        """
        try:
            account_info = self.get_account_info()
            positions = self.get_positions()
            
            if not account_info or account_info is None:
                logger.error("No account info available for risk metrics")
                return {}
            
            if positions is None:
                logger.error("Positions data is None")
                return {}
            
            total_unrealized_pnl = sum(pos.get('unrealized_pnl', 0) for pos in positions)
            
            equity = account_info.get('equity', 0)
            available_margin = account_info.get('available_margin', 0)
            initial_margin = account_info.get('initial_margin', 0)
            maintenance_margin = account_info.get('maintenance_margin', 0)
            
            return {
                'total_unrealized_pnl': total_unrealized_pnl,
                'total_margin_used': initial_margin,
                'margin_utilization': (initial_margin / equity * 100) if equity > 0 else 0,
                'margin_available': available_margin,
                'open_positions_count': len(positions),
                'account_equity': equity,
                'margin_level': (equity / maintenance_margin) if maintenance_margin > 0 else 0,
            }
        
        except Exception as e:
            logger.error(f"Exception in get_risk_metrics: {e}")
            return {}


# =============================================================================
# Position Monitor - Background polling with typed snapshots
# =============================================================================

class PositionMonitor:
    """
    Monitors positions and account state on a regular interval.
    
    Produces immutable AccountSnapshot objects that can be safely read
    from any thread. Strategies register callbacks via on_update() to
    react to each new snapshot.
    
    Usage:
        monitor = PositionMonitor(poll_interval=10)
        monitor.on_update(lambda snap: print(snap.summary_str()))
        monitor.start()
        
        # Read latest snapshot at any time
        snap = monitor.latest
        if snap:
            print(f"Net delta: {snap.net_delta}")
        
        monitor.stop()
    """
    
    def __init__(self, account_manager=None, poll_interval: int = 10, auth=None):
        """
        Args:
            account_manager: ExchangeAccountManager adapter (injected by build_context).
                             Falls back to Coincall AccountManager if not provided.
            poll_interval: Seconds between each refresh (default 10)
            auth: ExchangeAuth adapter — used to read ``reachable`` flag.
        """
        self._account_mgr = account_manager or AccountManager()
        self._poll_interval = poll_interval
        self._auth = auth
        self._latest: Optional[AccountSnapshot] = None
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[AccountSnapshot], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._was_reachable = True  # for transition detection
    
    # -- Public API -----------------------------------------------------------
    
    @property
    def latest(self) -> Optional[AccountSnapshot]:
        """Most recent snapshot (thread-safe read)."""
        with self._lock:
            return self._latest
    
    def on_update(self, callback: Callable[[AccountSnapshot], None]) -> None:
        """
        Register a callback that fires after each new snapshot.
        
        The callback receives the new AccountSnapshot. It runs on the
        monitor thread, so keep it fast or dispatch to another thread.
        """
        self._callbacks.append(callback)
    
    def snapshot(self) -> AccountSnapshot:
        """
        Fetch fresh data from the exchange and return a typed snapshot.
        
        Can be called manually for a one-off read, independent of the
        background polling loop.
        """
        now = time.time()
        
        # Fetch positions
        raw_positions = self._account_mgr.get_positions(force_refresh=True)
        position_snapshots = []
        net_delta = 0.0
        net_gamma = 0.0
        net_theta = 0.0
        net_vega = 0.0
        
        for pos in raw_positions:
            side_code = pos.get('trade_side')
            side = "long" if side_code == 1 else "short" if side_code == 2 else "unknown"
            
            ps = PositionSnapshot(
                position_id=str(pos.get('position_id', '')),
                symbol=pos.get('symbol', ''),
                qty=pos.get('qty', 0.0),
                side=side,
                entry_price=pos.get('avg_price', 0.0),
                mark_price=pos.get('mark_price', 0.0),
                unrealized_pnl=pos.get('unrealized_pnl', 0.0),
                roi=pos.get('roi', 0.0),
                delta=pos.get('delta', 0.0),
                gamma=pos.get('gamma', 0.0),
                theta=pos.get('theta', 0.0),
                vega=pos.get('vega', 0.0),
                timestamp=now,
            )
            position_snapshots.append(ps)
            
            # Aggregate Greeks
            net_delta += ps.delta
            net_gamma += ps.gamma
            net_theta += ps.theta
            net_vega += ps.vega
        
        # Fetch account info
        acct = self._account_mgr.get_account_info(force_refresh=True) or {}
        
        equity = acct.get('equity', 0.0)
        initial_margin = acct.get('initial_margin', 0.0)
        margin_util = (initial_margin / equity * 100) if equity > 0 else 0.0
        
        snap = AccountSnapshot(
            equity=equity,
            available_margin=acct.get('available_margin', 0.0),
            initial_margin=initial_margin,
            maintenance_margin=acct.get('maintenance_margin', 0.0),
            unrealized_pnl=acct.get('unrealized_pnl', 0.0),
            margin_utilization=margin_util,
            positions=tuple(position_snapshots),
            net_delta=net_delta,
            net_gamma=net_gamma,
            net_theta=net_theta,
            net_vega=net_vega,
            timestamp=now,
        )
        
        # Store as latest
        with self._lock:
            self._latest = snap
        
        return snap
    
    def start(self) -> None:
        """Start background polling thread."""
        if self._running:
            logger.warning("PositionMonitor already running")
            return
        
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="PositionMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"PositionMonitor started (interval={self._poll_interval}s)")
    
    def stop(self) -> None:
        """Stop background polling."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=self._poll_interval + 2)
            self._thread = None
        logger.info("PositionMonitor stopped")
    
    # -- Internal -------------------------------------------------------------
    
    def _poll_loop(self) -> None:
        """Background loop: snapshot → callbacks → sleep.

        When the exchange is unreachable (auth.reachable == False),
        the poll interval ramps up to reduce pressure on a recovering
        exchange and a Telegram alert is sent on the transition.  On
        recovery the interval snaps back and a reconnect alert fires.
        """
        while self._running:
            # ── Check reachability transitions ───────────────────────
            if self._auth is not None:
                now_reachable = self._auth.reachable
                if self._was_reachable and not now_reachable:
                    logger.warning("Exchange marked UNREACHABLE — backing off polls")
                    try:
                        from telegram_notifier import get_notifier
                        get_notifier().send(
                            "⚠️ <b>Exchange unreachable</b>\n"
                            "Consecutive API failures detected. "
                            "Poll interval increased until connection restores."
                        )
                    except Exception:
                        pass
                elif not self._was_reachable and now_reachable:
                    logger.info("Exchange RECONNECTED — resuming normal polls")
                    try:
                        from telegram_notifier import get_notifier
                        get_notifier().send(
                            "✅ <b>Exchange reconnected</b>\n"
                            "API responding normally. Resuming standard polling."
                        )
                    except Exception:
                        pass
                self._was_reachable = now_reachable

            try:
                snap = self.snapshot()
                logger.debug(snap.summary_str())
                
                # Fire callbacks
                for cb in self._callbacks:
                    try:
                        cb(snap)
                    except Exception as e:
                        logger.error(f"Callback error: {e}")
                        
            except Exception as e:
                logger.error(f"PositionMonitor poll error: {e}")

            # ── Adaptive sleep ───────────────────────────────────────
            if self._auth is not None and not self._auth.reachable:
                # Backoff: min(60, poll_interval * 2^n) capped at 60s
                sleep_secs = min(60, self._poll_interval * 3)
            else:
                sleep_secs = self._poll_interval

            for _ in range(int(sleep_secs * 10)):
                if not self._running:
                    return
                time.sleep(0.1)
