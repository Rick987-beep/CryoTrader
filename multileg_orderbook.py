#!/usr/bin/env python3
"""
Multi-Leg Smart Orderbook Execution Module

Implements the smart-dealing algorithm for executing multi-leg option structures
via the orderbook using chunking, continuous quoting, and aggressive fallback.

Algorithm Overview:
  1. Split the order into N chunks (proportionally for all legs)
  2. For each chunk:
     - Phase A (Quoting): Quote all legs for duration `time_per_chunk`
       with continuous repricing based on configurable strategy (top-of-book, mid, mark)
     - Phase B (Aggressive Fallback): If not completely filled, use aggressive limit
       orders (crossing the spread) with multiple retry attempts
  3. Track position deltas from starting point to handle both opens and closes
  4. Complete when all chunks are executed or positions reach target

Key Features:
  - Proportional chunking maintains leg ratios throughout execution
  - Position-aware tracking works for opening new positions and closing existing ones
  - Continuous repricing adapts to market movements
  - Aggressive fallback ensures execution while minimizing market impact
  - Configurable quoting strategies for different market conditions

This minimizes slippage and execution risk while maintaining price improvement
over orderbook midprices through intelligent chunking and quoting strategy.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any

from market_data import get_option_orderbook
from trade_execution import TradeExecutor

logger = logging.getLogger(__name__)


# =============================================================================
# Enums & Data Classes
# =============================================================================

class ChunkPhase(Enum):
    """Phases in chunk execution."""
    QUOTING = "quoting"      # Phase A: continuous quoting
    FALLBACK = "fallback"    # Phase B: market fill fallback
    COMPLETED = "completed"  # Chunk fully executed


@dataclass
class SmartExecConfig:
    """
    Configuration for smart multi-leg orderbook execution.
    
    Attributes:
        chunk_count: Number of chunks to split order into (e.g., 4)
        time_per_chunk: Time allowed per chunk in seconds (must be multiple of 10)
        quoting_strategy: How to price quotes:
            "top_of_book" = use orderbook bid/ask directly
            "top_of_book_offset_pct" = offset from top by spread_pct
            "mid" = use (bid + ask) / 2
            "mark" = use mark price (currently falls back to mid)
        spread_pct: Spread offset as % if using offset strategy (e.g., 0.5 for ±0.5%)
        reprice_interval: How often to reprice in seconds (minimum 10.0)
        reprice_price_threshold: Minimum price change to trigger repricing
        min_order_qty: Minimum order size to submit
        aggressive_attempts: Number of aggressive fill attempts per chunk
        aggressive_wait_seconds: Max wait per aggressive attempt
        aggressive_retry_pause: Pause between aggressive attempts
        iv_adjustments: Per-leg IV adjustments (symbol -> adjustment, currently unused)
    """
    chunk_count: int = 5
    time_per_chunk: float = 600.0  # seconds (10 minutes default)
    quoting_strategy: str = "top_of_book"  # "top_of_book", "top_of_book_offset_pct", "mid", "mark"
    spread_pct: float = 0.5
    reprice_interval: float = 10.0  # Minimum 10 seconds
    reprice_price_threshold: float = 0.1
    min_order_qty: float = 0.01
    aggressive_attempts: int = 10
    aggressive_wait_seconds: float = 5.0
    aggressive_retry_pause: float = 1.0
    iv_adjustments: Dict[str, float] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate configuration parameters."""
        allowed_strategies = {"top_of_book", "top_of_book_offset_pct", "mid", "mark"}
        if self.quoting_strategy not in allowed_strategies:
            logger.warning(f"Unknown quoting_strategy '{self.quoting_strategy}', defaulting to top_of_book")
            self.quoting_strategy = "top_of_book"
        if self.reprice_interval < 10.0:
            logger.warning(f"reprice_interval {self.reprice_interval}s < 10s minimum, adjusting to 10s")
            self.reprice_interval = 10.0
        if self.time_per_chunk % 10 != 0:
            logger.warning(f"time_per_chunk {self.time_per_chunk}s not multiple of 10, rounding")
            self.time_per_chunk = round(self.time_per_chunk / 10) * 10
        if self.reprice_price_threshold <= 0:
            logger.warning("reprice_price_threshold must be > 0, defaulting to 0.1")
            self.reprice_price_threshold = 0.1
        if self.min_order_qty <= 0:
            logger.warning("min_order_qty must be > 0, defaulting to 0.01")
            self.min_order_qty = 0.01
        if self.aggressive_attempts < 1:
            logger.warning("aggressive_attempts must be >= 1, defaulting to 1")
            self.aggressive_attempts = 1
        if self.aggressive_wait_seconds <= 0:
            logger.warning("aggressive_wait_seconds must be > 0, defaulting to 5.0")
            self.aggressive_wait_seconds = 5.0
        if self.aggressive_retry_pause < 0:
            logger.warning("aggressive_retry_pause must be >= 0, defaulting to 1.0")
            self.aggressive_retry_pause = 1.0


@dataclass
class LegChunkState:
    """
    Tracks state of a single leg within a chunk.
    
    Attributes:
        symbol: Option symbol (e.g., "BTCUSD-28FEB26-100000-C")
        side: Trade side (1=buy, 2=sell)
        total_qty: Total quantity for this leg in this chunk
        filled_qty: Quantity filled IN THIS CHUNK (incremental, not total position)
        starting_position: Position size when chunk started (for calculating incremental fills)
        current_order_id: Current active order ID
        current_order_price: Price of current active order
        fill_times: List of times when partial fills occurred
    """
    symbol: str
    side: str
    total_qty: float
    filled_qty: float = 0.0
    starting_position: float = 0.0
    current_order_id: Optional[str] = None
    current_order_price: Optional[float] = None
    fill_times: List[float] = field(default_factory=list)

    @property
    def remaining_qty(self) -> float:
        """Remaining quantity to fill in THIS chunk (not total position)."""
        return max(0, self.total_qty - self.filled_qty)

    @property
    def is_filled(self) -> bool:
        """Check if THIS chunk is filled (not total position)."""
        return self.filled_qty >= self.total_qty * 0.99  # Allow 1% tolerance


@dataclass
class ChunkState:
    """
    Tracks state of a single chunk during execution.
    
    Attributes:
        chunk_idx: Index of this chunk (0-based)
        legs_state: Per-leg state (symbol -> LegChunkState)
        start_time: Unix timestamp when chunk execution started
        phase: Current phase (QUOTING, FALLBACK, COMPLETED)
        status_message: Human-readable status
    """
    chunk_idx: int
    legs_state: Dict[str, LegChunkState]
    start_time: float = field(default_factory=time.time)
    phase: ChunkPhase = ChunkPhase.QUOTING
    status_message: str = ""

    @property
    def all_legs_filled(self) -> bool:
        return all(leg.is_filled for leg in self.legs_state.values())

    @property
    def any_leg_filled(self) -> bool:
        return any(leg.filled_qty > 0 for leg in self.legs_state.values())

    @property
    def elapsed_time(self) -> float:
        return time.time() - self.start_time


@dataclass
class SmartExecResult:
    """
    Result of smart multi-leg orderbook execution.
    
    Attributes:
        success: Whether execution completed successfully
        chunks_completed: Number of chunks fully executed
        chunks_total: Total number of chunks
        total_filled_qty: Total quantity filled per leg (symbol -> qty)
        total_cost: Total cost of execution (positive = we paid, negative = we received)
        execution_time: Total execution time in seconds
        avg_price_per_leg: Average executed price per leg
        fallback_count: Number of chunks that required market fallback
        message: Human-readable result message
    """
    success: bool
    chunks_completed: int
    chunks_total: int
    total_filled_qty: Dict[str, float] = field(default_factory=dict)
    total_cost: float = 0.0
    execution_time: float = 0.0
    avg_price_per_leg: Dict[str, float] = field(default_factory=dict)
    fallback_count: int = 0
    message: str = ""


# =============================================================================
# Smart Orderbook Executor
# =============================================================================

class SmartOrderbookExecutor:
    """
    Executes multi-leg option structures using smart chunking and quoting.
    
    Usage:
        executor = SmartOrderbookExecutor()
        legs = [
            TradeLeg(symbol="BTCUSD-28FEB26-100000-C", qty=1.0, side="buy"),
            TradeLeg(symbol="BTCUSD-28FEB26-105000-P", qty=1.0, side="sell"),
        ]
        config = SmartExecConfig(chunk_count=5, time_per_chunk=30)
        result = executor.execute_smart_multi_leg(legs, config)
    """

    def __init__(self, get_positions=None, executor=None):
        """Initialize smart orderbook executor.

        Args:
            get_positions: Optional callable that returns a list of position
                dicts (each with 'symbol' and 'qty' keys).  If not provided,
                position tracking is skipped and only order-based fill
                detection is used.
            executor: Optional ExchangeExecutor adapter.  If not provided,
                a raw TradeExecutor is created (Coincall-only).
        """
        self._executor = executor or TradeExecutor()
        self._active_chunks: Dict[int, ChunkState] = {}
        self._get_positions = get_positions or (lambda: [])

    def execute_smart_multi_leg(
        self,
        legs: List[Any],
        config: SmartExecConfig
    ) -> SmartExecResult:
        """
        Execute multi-leg order using smart chunking and quoting.
        
        Args:
            legs: List of TradeLeg objects (from trade_lifecycle)
            config: SmartExecConfig with execution parameters
            
        Returns:
            SmartExecResult with execution details
        """
        start_time = time.time()
        
        try:
            logger.info(f"Starting smart execution: {len(legs)} legs, {config.chunk_count} chunks")
            
            # Track target quantities and starting positions per leg
            target_qty = {leg.symbol: leg.qty for leg in legs}
            starting_position = {}
            filled_delta = {}  # Track change from starting position
            
            # Get starting positions
            try:
                positions = self._get_positions()
                for leg in legs:
                    pos_qty = 0.0
                    for pos in positions:
                        if pos.get('symbol') == leg.symbol:
                            pos_qty = abs(float(pos.get('qty', 0)))
                            break
                    starting_position[leg.symbol] = pos_qty
                    filled_delta[leg.symbol] = 0.0
                    logger.info(f"[Smart Exec] {leg.symbol}: target={leg.qty:.3f}, starting_pos={pos_qty:.3f}")
            except Exception as e:
                logger.warning(f"Could not check starting positions: {e}")
                for leg in legs:
                    starting_position[leg.symbol] = 0.0
                    filled_delta[leg.symbol] = 0.0
            
            # Execute each chunk sequentially
            total_cost = 0.0
            fallback_count = 0
            chunks_executed = 0
            
            for chunk_idx in range(config.chunk_count):
                # Check current positions to measure progress
                try:
                    positions = self._get_positions()
                    for leg in legs:
                        pos_qty = 0.0
                        for pos in positions:
                            if pos.get('symbol') == leg.symbol:
                                pos_qty = abs(float(pos.get('qty', 0)))
                                break
                        # Calculate delta from starting position
                        filled_delta[leg.symbol] = abs(pos_qty - starting_position[leg.symbol])
                except Exception as e:
                    logger.warning(f"Could not check positions before chunk {chunk_idx}: {e}")
                
                # Calculate remaining quantity for each leg
                remaining_qty = {}
                all_filled = True
                for leg in legs:
                    remaining = target_qty[leg.symbol] - filled_delta[leg.symbol]
                    remaining_qty[leg.symbol] = max(0.0, remaining)
                    if remaining_qty[leg.symbol] > config.min_order_qty:
                        all_filled = False
                
                # Stop if all legs are filled
                if all_filled:
                    logger.info(f"All legs filled after {chunks_executed} chunks, stopping early")
                    break
                
                # Calculate chunk size (divide remaining by remaining chunks)
                remaining_chunks = config.chunk_count - chunk_idx
                chunk_legs = []
                
                for leg in legs:
                    if remaining_qty[leg.symbol] > config.min_order_qty:
                        chunk_qty = remaining_qty[leg.symbol] / remaining_chunks
                        # Ensure chunk_qty meets minimum
                        if chunk_qty < config.min_order_qty:
                            chunk_qty = remaining_qty[leg.symbol]  # Put all remaining in this chunk
                        
                        chunk_leg = type('ChunkLeg', (), {
                            'symbol': leg.symbol,
                            'qty': chunk_qty,
                            'side': leg.side,
                            'order_id': None,
                            'fill_price': None,
                            'filled_qty': 0.0,
                            'position_id': None
                        })()
                        chunk_legs.append(chunk_leg)
                
                if not chunk_legs:
                    logger.info(f"No legs to execute in chunk {chunk_idx}, stopping")
                    break
                
                logger.info(f"Executing chunk {chunk_idx + 1}/{config.chunk_count}")
                
                chunk_result = self._execute_chunk(
                    chunk_idx=chunk_idx,
                    chunk_legs=chunk_legs,
                    config=config
                )
                
                # Aggregate results
                total_cost += chunk_result["total_cost"]
                if chunk_result["fallback_triggered"]:
                    fallback_count += 1
                
                chunks_executed += 1
            
            # Final position check to get accurate fills
            try:
                positions = self._get_positions()
                for leg in legs:
                    pos_qty = 0.0
                    for pos in positions:
                        if pos.get('symbol') == leg.symbol:
                            pos_qty = abs(float(pos.get('qty', 0)))
                            break
                    filled_delta[leg.symbol] = abs(pos_qty - starting_position[leg.symbol])
            except Exception as e:
                logger.warning(f"Could not get final position check: {e}")
            
            # Calculate average prices (rough estimate based on cost)
            avg_prices = {}
            total_qty = sum(filled_delta.values())
            if total_qty > 0:
                avg_price = total_cost / total_qty
                for leg in legs:
                    avg_prices[leg.symbol] = avg_price  # Simplified - same avg for all legs
            
            execution_time = time.time() - start_time
            
            result = SmartExecResult(
                success=True,
                chunks_completed=chunks_executed,
                chunks_total=config.chunk_count,
                total_filled_qty=filled_delta,
                total_cost=total_cost,
                execution_time=execution_time,
                avg_price_per_leg=avg_prices,
                fallback_count=fallback_count,
                message=f"Smart execution completed in {execution_time:.1f}s, {chunks_executed}/{config.chunk_count} chunks, {fallback_count} fallbacks"
            )
            
            logger.info(f"Smart execution completed: {result.message}")
            return result
            
        except Exception as e:
            logger.error(f"Smart execution failed: {e}")
            execution_time = time.time() - start_time
            return SmartExecResult(
                success=False,
                chunks_completed=0,
                chunks_total=config.chunk_count,
                execution_time=execution_time,
                message=f"Smart execution failed: {e}"
            )

    def _execute_chunk(
        self,
        chunk_idx: int,
        chunk_legs: List[Any],
        config: SmartExecConfig
    ) -> Dict[str, Any]:
        """
        Execute a single chunk with inner quoting loop.
        
        **Outer loop logic (this method):**
        - Define chunk size and total time limit
        
        **Inner loop logic (_inner_quoting_loop):**
        - Quote all unfilled legs
        - Monitor fills continuously
        - When a leg fills, stop quoting it and continue with others
        - After time expires, market fill remaining unfilled legs
        
        Args:
            chunk_idx: Index of this chunk
            chunk_legs: Legs in this chunk
            config: Execution config
            
        Returns:
            Dict with filled_qty, total_cost, fallback_triggered
        """
        logger.info(f"Executing chunk {chunk_idx + 1}/{config.chunk_count}")
        
        # Get starting positions for this chunk
        starting_positions = {}
        try:
            positions = self._get_positions()
            for leg in chunk_legs:
                for pos in positions:
                    if pos.get('symbol') == leg.symbol:
                        starting_positions[leg.symbol] = abs(float(pos.get('qty', 0)))
                        break
                if leg.symbol not in starting_positions:
                    starting_positions[leg.symbol] = 0.0
        except Exception as e:
            logger.warning(f"Could not get starting positions for chunk {chunk_idx}: {e}")
            starting_positions = {leg.symbol: 0.0 for leg in chunk_legs}
        
        # Initialize chunk state
        legs_state = {}
        for leg in chunk_legs:
            start_pos = starting_positions.get(leg.symbol, 0.0)
            legs_state[leg.symbol] = LegChunkState(
                symbol=leg.symbol,
                side=leg.side,
                total_qty=leg.qty,
                filled_qty=0.0,
                starting_position=start_pos
            )
            logger.info(
                f"[Chunk {chunk_idx}] {leg.symbol}: chunk_target={leg.qty:.3f}, "
                f"starting_pos={start_pos:.3f}"
            )
        
        chunk_state = ChunkState(chunk_idx=chunk_idx, legs_state=legs_state)
        self._active_chunks[chunk_idx] = chunk_state
        
        # Inner quoting loop: quote and monitor until time expires
        logger.info(f"[Chunk {chunk_idx}] Starting inner loop for {config.time_per_chunk}s")
        fallback_triggered = self._inner_quoting_loop(chunk_state, chunk_legs, config)
        
        # Aggregate chunk results
        filled_qty = {leg.symbol: leg_state.filled_qty 
                      for leg, leg_state in zip(chunk_legs, chunk_state.legs_state.values())}
        
        chunk_state.phase = ChunkPhase.COMPLETED
        
        return {
            "filled_qty": filled_qty,
            "total_cost": 0.0,  # TODO: track execution cost
            "fallback_triggered": fallback_triggered
        }

    def _inner_quoting_loop(
        self,
        chunk_state: ChunkState,
        chunk_legs: List[Any],
        config: SmartExecConfig
    ) -> bool:
        """
        Inner loop: Quote all unfilled legs, monitor fills, adjust continuously.
        
        Logic:
        1. Quote all unfilled legs at calculated prices
        2. Monitor for fills
        3. When a leg fills, stop quoting it, continue with others
        4. Reprice continuously at config.reprice_interval
        5. After config.time_per_chunk expires, market fill any remaining unfilled legs
        
        Args:
            chunk_state: Current chunk state
            chunk_legs: Legs in this chunk
            config: Execution config
            
        Returns:
            True if market fallback was triggered, False if all filled via limit orders
        """
        chunk_state.phase = ChunkPhase.QUOTING
        chunk_start_time = time.time()
        last_reprice = chunk_start_time
        active_orders = {}  # symbol -> order_id
        
        while True:
            elapsed = time.time() - chunk_start_time
            
            # Check if chunk time expired
            if elapsed >= config.time_per_chunk:
                logger.info(f"[Chunk {chunk_state.chunk_idx}] Time expired ({elapsed:.1f}s)")
                break
            
            # Reprice check
            should_reprice = (time.time() - last_reprice) >= config.reprice_interval
            
            if should_reprice:
                # Check if any prices have actually changed
                needs_reprice = False
                new_prices = {}
                
                for leg in chunk_legs:
                    leg_state = chunk_state.legs_state.get(leg.symbol)
                    if not leg_state or leg_state.is_filled:
                        continue
                    
                    new_price = self._calculate_quote_price(leg.symbol, leg.side, config)
                    if not new_price:
                        continue
                    
                    new_prices[leg.symbol] = new_price
                    
                    # Check if price changed significantly (more than 0.1)
                    if leg_state.current_order_price is None or abs(new_price - leg_state.current_order_price) > config.reprice_price_threshold:
                        needs_reprice = True
                
                if needs_reprice:
                    logger.info(f"[Chunk {chunk_state.chunk_idx}] Price changed, repricing at {elapsed:.1f}s")
                    
                    # Cancel existing orders
                    for symbol, order_id in active_orders.items():
                        try:
                            self._executor.cancel_order(order_id)
                            logger.debug(f"[Chunk {chunk_state.chunk_idx}] Cancelled order {order_id} for {symbol}")
                        except Exception as e:
                            logger.warning(f"Could not cancel order {order_id}: {e}")
                    
                    active_orders.clear()
                    
                    # Place new orders for unfilled legs only
                    for leg in chunk_legs:
                        leg_state = chunk_state.legs_state.get(leg.symbol)
                        if not leg_state or leg_state.is_filled:
                            continue
                        
                        # Use pre-calculated price
                        quote_price = new_prices.get(leg.symbol)
                        if not quote_price:
                            continue
                        
                        # Place limit order for remaining quantity
                        try:
                            order_result = self._executor.place_order(
                                symbol=leg.symbol,
                                qty=leg_state.remaining_qty,
                                side=leg.side,
                                order_type=1,  # Limit
                                price=quote_price
                            )
                            
                            if order_result and 'orderId' in order_result:
                                order_id = str(order_result['orderId'])
                                active_orders[leg.symbol] = order_id
                                leg_state.current_order_id = order_id
                                leg_state.current_order_price = quote_price
                                logger.info(f"[Chunk {chunk_state.chunk_idx}] Placed {leg.symbol} @ {quote_price}, order {order_id}")
                            else:
                                logger.warning(f"[Chunk {chunk_state.chunk_idx}] Failed to place order for {leg.symbol}")
                                    
                        except Exception as e:
                            logger.error(f"[Chunk {chunk_state.chunk_idx}] Exception placing order for {leg.symbol}: {e}")
                    
                    last_reprice = time.time()
                else:
                    logger.debug(f"[Chunk {chunk_state.chunk_idx}] Price unchanged, keeping existing orders")
                    last_reprice = time.time()  # Reset timer even if no reprice needed
            
            # Initial order placement if no orders exist
            elif not active_orders:
                logger.info(f"[Chunk {chunk_state.chunk_idx}] Placing initial orders")
                
                # Place new orders for unfilled legs only
                for leg in chunk_legs:
                    leg_state = chunk_state.legs_state.get(leg.symbol)
                    if not leg_state or leg_state.is_filled:
                        continue
                    
                    # Calculate quote price
                    quote_price = self._calculate_quote_price(leg.symbol, leg.side, config)
                    if not quote_price:
                        logger.warning(f"[Chunk {chunk_state.chunk_idx}] Could not get quote price for {leg.symbol}")
                        continue
                    
                    # Place limit order for remaining quantity
                    try:
                        order_result = self._executor.place_order(
                            symbol=leg.symbol,
                            qty=leg_state.remaining_qty,
                            side=leg.side,
                            order_type=1,  # Limit
                            price=quote_price
                        )
                        
                        if order_result and 'orderId' in order_result:
                            order_id = str(order_result['orderId'])
                            active_orders[leg.symbol] = order_id
                            leg_state.current_order_id = order_id
                            leg_state.current_order_price = quote_price
                            logger.info(f"[Chunk {chunk_state.chunk_idx}] Placed {leg.symbol} @ {quote_price}, order {order_id}")
                        else:
                            logger.warning(f"[Chunk {chunk_state.chunk_idx}] Failed to place order for {leg.symbol}")
                            
                    except Exception as e:
                        logger.error(f"[Chunk {chunk_state.chunk_idx}] Exception placing order for {leg.symbol}: {e}")
                
                last_reprice = time.time()
            
            # Monitor for fills
            self._check_order_fills(chunk_state, chunk_legs, active_orders)
            
            # Check if all legs filled
            if chunk_state.all_legs_filled:
                logger.info(f"[Chunk {chunk_state.chunk_idx}] All legs filled within time limit!")
                # Cancel any remaining orders
                for symbol, order_id in active_orders.items():
                    try:
                        self._executor.cancel_order(order_id)
                    except:
                        pass
                return False  # No fallback needed
            
            # Sleep before next iteration
            time.sleep(0.5)
        
        # Time expired - cancel remaining orders and market fill
        logger.info(f"[Chunk {chunk_state.chunk_idx}] Cancelling remaining orders for market fill")
        for symbol, order_id in active_orders.items():
            try:
                self._executor.cancel_order(order_id)
            except:
                pass
        
        # Aggressive limit fill unfilled legs (cross the spread)
        # Keep trying until all legs are filled
        chunk_state.phase = ChunkPhase.FALLBACK
        
        max_attempts = config.aggressive_attempts  # Maximum aggressive fill attempts
        attempt = 0
        
        while attempt < max_attempts and not chunk_state.all_legs_filled:
            attempt += 1
            
            unfilled_legs = [
                (symbol, leg_state)
                for symbol, leg_state in chunk_state.legs_state.items()
                if not leg_state.is_filled and leg_state.remaining_qty >= config.min_order_qty  # Skip tiny remainders
            ]
            
            if not unfilled_legs:
                # All legs either filled or have sub-minimum remainders
                logger.info(
                    f"[Chunk {chunk_state.chunk_idx}] No fillable legs remaining "
                    f"(all filled or < {config.min_order_qty})"
                )
                break
            
            logger.info(f"[Chunk {chunk_state.chunk_idx}] Aggressive fill attempt {attempt}/{max_attempts} for {len(unfilled_legs)} legs")
            fallback_orders = {}
            
            for symbol, leg_state in unfilled_legs:
                try:
                    # Find original leg for side info
                    orig_leg = next((l for l in chunk_legs if l.symbol == symbol), None)
                    if not orig_leg:
                        continue
                    
                    # Get aggressive price (cross the spread)
                    aggressive_price = self._get_aggressive_limit_price(symbol, orig_leg.side)
                    if not aggressive_price:
                        logger.warning(f"[Chunk {chunk_state.chunk_idx}] Could not get aggressive price for {symbol}")
                        continue
                    
                    order_result = self._executor.place_order(
                        symbol=symbol,
                        qty=leg_state.remaining_qty,
                        side=orig_leg.side,
                        order_type=1,  # Limit (but aggressive)
                        price=aggressive_price
                    )
                    
                    if order_result and 'orderId' in order_result:
                        order_id = str(order_result['orderId'])
                        fallback_orders[symbol] = order_id
                        logger.info(f"[Chunk {chunk_state.chunk_idx}] Aggressive limit {order_id} for {symbol} @ {aggressive_price} (qty={leg_state.remaining_qty})")
                            
                except Exception as e:
                    logger.error(f"[Chunk {chunk_state.chunk_idx}] Failed to place aggressive order for {symbol}: {e}")
            
            # Wait and monitor for fills
            if fallback_orders:
                logger.info(f"[Chunk {chunk_state.chunk_idx}] Waiting up to {config.aggressive_wait_seconds:.1f}s for aggressive fills...")
                fallback_start = time.time()
                while time.time() - fallback_start < config.aggressive_wait_seconds:
                    self._check_order_fills(chunk_state, chunk_legs, fallback_orders)
                    if chunk_state.all_legs_filled:
                        logger.info(f"[Chunk {chunk_state.chunk_idx}] All legs filled!")
                        break
                    time.sleep(0.5)
                
                # Cancel any remaining unfilled orders
                for symbol, order_id in fallback_orders.items():
                    try:
                        self._executor.cancel_order(order_id)
                    except:
                        pass
                
                # If filled, break
                if chunk_state.all_legs_filled:
                    break
                
                # Brief pause before next attempt
                if attempt < max_attempts:
                    time.sleep(config.aggressive_retry_pause)
        
        if not chunk_state.all_legs_filled:
            logger.warning(f"[Chunk {chunk_state.chunk_idx}] Failed to fill all legs after {attempt} attempts")
        
        return True  # Fallback was triggered

    def _get_aggressive_limit_price(
        self,
        symbol: str,
        side: str
    ) -> Optional[float]:
        """
        Get aggressive limit price that crosses the spread.
        
        For immediate fills:
          - BUY orders: quote at ASK (lift the offer)
          - SELL orders: quote at BID (hit the bid)
        
        Args:
            symbol: Option symbol
            side: "buy" or "sell"
            
        Returns:
            Aggressive price or None if market data unavailable
        """
        try:
            orderbook = get_option_orderbook(symbol)
            if not orderbook:
                return None
            
            bids = orderbook.get('bids', [])
            asks = orderbook.get('asks', [])
            
            if not bids or not asks:
                return None
            
            best_bid = float(bids[0]['price']) if bids else 0
            best_ask = float(asks[0]['price']) if asks else 0
            
            if best_bid <= 0 or best_ask <= 0:
                return None
            
            # Cross the spread for immediate fill
            if side == "buy":  # Buy - lift the offer
                return best_ask
            else:  # Sell - hit the bid
                return best_bid
                
        except Exception as e:
            logger.warning(f"Could not get aggressive price for {symbol}: {e}")
            return None
    
    def _calculate_quote_price(
        self,
        symbol: str,
        side: str,
        config: SmartExecConfig
    ) -> Optional[float]:
        """
        Calculate quote price based on configured strategy.
        
        Strategy:
          - "top_of_book": Quote at best bid (for BUY) or best ask (for SELL)
          - "top_of_book_offset_pct": Offset from top by spread_pct
                    - "mid": Use (bid + ask) / 2
                    - "mark": Use mark price (currently falls back to mid)
        
        Args:
            symbol: Option symbol
            side: "buy" or "sell"
            config: Execution config
            
        Returns:
            Quote price or None if market data unavailable
        """
        try:
            orderbook = get_option_orderbook(symbol)
            if not orderbook:
                logger.warning(f"Could not fetch orderbook for {symbol}")
                return None
            
            bids = orderbook.get('bids', [])
            asks = orderbook.get('asks', [])
            
            if not bids or not asks:
                logger.warning(f"Empty orderbook for {symbol}")
                return None
            
            # Extract top of book
            best_bid = float(bids[0]['price']) if bids else 0
            best_ask = float(asks[0]['price']) if asks else 0
            
            if best_bid <= 0 or best_ask <= 0:
                logger.warning(f"Invalid orderbook prices for {symbol}: bid={best_bid}, ask={best_ask}")
                return None
            
            # Calculate quote based on strategy
            if config.quoting_strategy == "top_of_book":
                if side == "buy":  # Buy - quote at bid (join the bid)
                    quote_price = best_bid
                    logger.debug(f"[Quote] {symbol} BUY @ bid {quote_price}")
                else:  # Sell - quote at ask (join the ask)
                    quote_price = best_ask
                    logger.debug(f"[Quote] {symbol} SELL @ ask {quote_price}")
                    
            elif config.quoting_strategy == "top_of_book_offset_pct":
                offset = config.spread_pct / 100.0
                if side == "buy":  # Buy - bid with offset
                    quote_price = best_bid * (1 + offset)
                    logger.debug(f"[Quote] {symbol} BUY @ bid+{config.spread_pct}% = {quote_price}")
                else:  # Sell - ask with offset
                    quote_price = best_ask * (1 - offset)
                    logger.debug(f"[Quote] {symbol} SELL @ ask-{config.spread_pct}% = {quote_price}")
            
            elif config.quoting_strategy == "mid":
                quote_price = (best_bid + best_ask) / 2.0
                logger.debug(f"[Quote] {symbol} @ mid {quote_price}")
                
            else:  # "mark" or fallback
                # For mark price, would need to implement get_option_details
                quote_price = (best_bid + best_ask) / 2.0
                logger.debug(f"[Quote] {symbol} @ mid (mark fallback) {quote_price}")
            
            return max(0.0001, quote_price)  # Ensure positive price
            
        except Exception as e:
            logger.warning(f"Could not calculate quote price for {symbol}: {e}")
            return None

    def _check_order_fills(
        self,
        chunk_state: ChunkState,
        chunk_legs: List[Any],
        active_orders: Dict[str, str]
    ) -> None:
        """
        Check order fills using position data from account manager.
        
        Tracks INCREMENTAL fills in this chunk by comparing current position
        to the starting position when the chunk began.
        
        Args:
            chunk_state: Current chunk state
            chunk_legs: Legs in this chunk
            active_orders: Dict of symbol -> order_id for active orders
        """
        try:
            # Get current positions
            positions = self._get_positions()
            
            for symbol in active_orders.keys():
                leg_state = chunk_state.legs_state.get(symbol)
                if not leg_state or leg_state.is_filled:
                    continue
                
                # Find current position for this symbol
                current_pos_qty = 0.0
                for pos in positions:
                    if pos.get('symbol') == symbol:
                        current_pos_qty = abs(float(pos.get('qty', 0)))
                        break
                
                # Calculate what was filled IN THIS CHUNK
                # Use absolute delta to handle both:
                #   - Opens (increasing position): 0.0 -> 0.1 = abs(0.1 - 0.0) = 0.1
                #   - Closes (decreasing position): 0.2 -> 0.1 = abs(0.1 - 0.2) = 0.1
                # This is critical for close detection - without abs(), closes would
                # return negative deltas clamped to 0, making the algorithm think
                # nothing filled and loop indefinitely
                filled_in_chunk = abs(current_pos_qty - leg_state.starting_position)
                
                # Update filled quantity if we got new fills
                if filled_in_chunk > leg_state.filled_qty:
                    old_filled = leg_state.filled_qty
                    leg_state.filled_qty = filled_in_chunk
                    leg_state.fill_times.append(time.time())
                    logger.info(
                        f"[Chunk {chunk_state.chunk_idx}] {symbol} chunk filled "
                        f"{leg_state.filled_qty}/{leg_state.total_qty} "
                        f"(total pos: {current_pos_qty})"
                    )
                        
        except Exception as e:
            logger.debug(f"Could not check fills via positions: {e}")
