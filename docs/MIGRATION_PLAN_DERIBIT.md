# Migration Plan: Coincall → Deribit

**Author:** Architecture Review  
**Date:** 15 March 2026  
**Status:** IN PROGRESS — Phase 1 Complete (v1.3.0-wip)  
**Last Updated:** 16 March 2026  
**Scope:** Full exchange migration from Coincall to Deribit, with optional dual-exchange support

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Why Deribit](#2-why-deribit)
3. [Current Architecture & Coupling Assessment](#3-current-architecture--coupling-assessment)
4. [Key Differences: Coincall vs Deribit](#4-key-differences-coincall-vs-deribit)
5. [Proposed Abstraction Layer](#5-proposed-abstraction-layer)
6. [Module-by-Module Migration Plan](#6-module-by-module-migration-plan)
7. [Contract & Symbol Translation](#7-contract--symbol-translation)
8. [Execution Model Changes](#8-execution-model-changes)
9. [Testing Strategy](#9-testing-strategy)
10. [Migration Phases & Sequencing](#10-migration-phases--sequencing)
11. [Risk Register](#11-risk-register)
12. [Open Questions & Decisions Required](#12-open-questions--decisions-required)
13. [Deribit API Field Reference (Test Findings)](#13-deribit-api-field-reference-test-findings)

---

## 1. Executive Summary

This document proposes a plan to migrate the CoincallTrader bot from the Coincall exchange to Deribit, the most liquid crypto options venue in the world. The migration touches **8 of 19 Python modules** at a critical level, **4 at a moderate level**, and leaves **7 modules entirely untouched**. The key architectural move is the introduction of an **Exchange Abstraction Layer** — a set of interfaces that isolate exchange-specific logic (authentication, market data, order management, account queries) behind stable contracts. This lets the strategy layer, lifecycle engine, and all higher-order logic remain unchanged.

The migration is proposed in **four phases**, designed so that each phase is independently testable and the system is never in a broken state for more than one module at a time.

---

## 2. Why Deribit

For context, the motivations for this migration likely include:

| Factor | Coincall | Deribit |
|--------|----------|---------|
| **Liquidity** | Thin books; spreads widen fast above $10k notional | Deepest crypto options liquidity globally; tight spreads even at size |
| **Instrument Range** | BTC + ETH options; limited expiries | BTC + ETH options; daily, weekly, monthly, quarterly expiries; perpetuals |
| **Margin Model** | Cross-margin with basic portfolio margining | Full portfolio margining (PM) with significant capital efficiency |
| **Market Makers** | Few; RFQ often the only way to trade size | Dense orderbook; professional market makers on every strike |
| **API Maturity** | Functional but young; occasional quirks | Battle-tested; WebSocket-first; FIX protocol available |
| **Institutional Adoption** | Niche | Standard venue for institutional crypto options |

The core trading strategies (delta-targeted put selling, straddles, strangles) are **directly transferable** — the products are the same (European-style BTC/ETH options settled in USD). The migration is purely an infrastructure concern, not a strategy concern.

---

## 3. Current Architecture & Coupling Assessment

The codebase follows a **tick-driven, composition-over-inheritance** design:

```
PositionMonitor (10s poll)
  → StrategyRunner.tick()
    → LifecycleEngine (state machine)
      → ExecutionRouter
        → TradeExecutor / RFQExecutor / SmartOrderbookExecutor
          → Coincall REST API (via auth.py)
```

### Coupling Map

Every module was assessed for how tightly it depends on Coincall-specific APIs, data formats, and conventions:

#### CRITICAL — Must Refactor (8 modules)
| Module | Coupling Point |
|--------|---------------|
| `config.py` | Hardcoded Coincall URLs (`api.coincall.com`, `ws.coincall.com`), env var names (`COINCALL_API_KEY_*`) |
| `auth.py` | Coincall-specific HMAC-SHA256 signing: custom prehash format, `X-CC-APIKEY` / `sign` / `ts` / `X-REQ-TS-DIFF` headers |
| `market_data.py` | All endpoints: `/open/option/getInstruments`, `/open/futures/ticker`, option detail with Greeks; response field names (`symbolName`, `tradeSide`, `markPrice`) |
| `trade_execution.py` | Order endpoints (`/open/option/order/create/v1`, `cancel/v1`, `singleQuery/v1`); side encoding (`1`=buy, `2`=sell); order status codes (`0`=NEW … `10`=CANCEL_BY_EXERCISE) |
| `order_manager.py` | Exchange status code → internal status mapping (`_EXCHANGE_STATE_MAP`); side encoding |
| `account_manager.py` | Account summary endpoint (`/open/account/summary/v1`); position endpoint (`/open/option/position/get/v1`); field extraction (`equity`, `imAmount`, `mmAmount`, `upnlByMarkPrice`) |
| `rfq.py` | Entire RFQ lifecycle — Deribit has **no equivalent RFQ system** |
| `execution_router.py` | RFQ routing decision; notional threshold logic |

#### MODERATE — Requires Adaptation (4 modules)
| Module | Coupling Point |
|--------|---------------|
| `option_selection.py` | Contract naming format (`BTCUSD-28MAR26-100000-C`) |
| `trade_lifecycle.py` | `TradeLeg.side` uses `1`/`2` encoding; `TradeLeg.symbol` is Coincall format |
| `strategy.py` | `TradingContext` references concrete Coincall types (`CoincallAuth`, `MarketData`, `TradeExecutor`) |
| `strategies/daily_put_sell.py` | RFQ phased execution metadata; strike selection via Coincall instruments |

#### LOW / NONE — No Changes Needed (7 modules)
| Module | Reason |
|--------|--------|
| `lifecycle_engine.py` | Generic state machine; works once ExecutionRouter is abstracted |
| `position_closer.py` | Generic close phases; delegates to executor/account_manager |
| `multileg_orderbook.py` | Generic chunking algorithm; delegates to executor |
| `persistence.py` | Pure file I/O |
| `dashboard.py` | Displays generic `AccountSnapshot` data |
| `ema_filter.py` | Uses Binance public API, not Coincall |
| `telegram_notifier.py` | Exchange-agnostic notifications |
| `retry.py` | Generic utility |
| `health_check.py` | Generic logging |
| `main.py` | Orchestration; works once `build_context()` is parameterized |

**Bottom line:** ~40% of the codebase needs refactoring. ~60% is already exchange-agnostic.

---

## 4. Key Differences: Coincall vs Deribit

### 4.1 Authentication

| Aspect | Coincall | Deribit |
|--------|----------|---------|
| **Method** | HMAC-SHA256 with custom prehash string; signature in `sign` header | Client ID + Client Secret; OAuth2-style `/public/auth` call returns access token (JWT) |
| **Headers** | `X-CC-APIKEY`, `sign`, `ts`, `X-REQ-TS-DIFF` | `Authorization: Bearer <access_token>` |
| **Token Refresh** | N/A (sign every request) | Access tokens expire (~900s); must refresh via `/public/auth` with `refresh_token` grant |
| **WebSocket Auth** | Send signed message after connect | Send `public/auth` message after connect; receive session token |

**Implication:** Deribit auth is *simpler per-request* (just a bearer token) but requires a token lifecycle manager that refreshes before expiry. Coincall's sign-every-request model has no state to manage.

### 4.2 Market Data

| Aspect | Coincall | Deribit |
|--------|----------|---------|
| **Instrument List** | REST: `/open/option/getInstruments/{underlying}` | REST: `/public/get_instruments?currency=BTC&kind=option` |
| **Ticker / Mark Price** | REST: `/open/futures/ticker/BTCUSDT` | REST: `/public/ticker?instrument_name=BTC-28MAR26-100000-C`; or subscribe via WebSocket |
| **Option Greeks** | Per-instrument REST call returns delta/gamma/theta/vega | Included in `ticker` response (`greeks` object): `delta`, `gamma`, `theta`, `vega`, `rho` |
| **Orderbook** | REST per instrument | REST: `/public/get_order_book?instrument_name=...&depth=N` |
| **Index Price** | Embedded in futures ticker | Dedicated: `/public/get_index_price?index_name=btc_usd` |
| **WebSocket** | Separate URLs for options/futures/spot; text frames | Single URL (`wss://www.deribit.com/ws/api/v2`); JSON-RPC 2.0 over WebSocket |
| **Rate Limits** | Undocumented / generous | Documented: 100 req/s REST; WebSocket more generous |

**Implication:** Deribit is more efficient — Greeks come bundled with ticker data (no extra call per option). WebSocket is strongly preferred for real-time data; the current 30s-TTL polling approach works but should eventually migrate to WebSocket subscriptions for lower latency.

### 4.3 Order Management

| Aspect | Coincall | Deribit |
|--------|----------|---------|
| **Place Order** | POST `/open/option/order/create/v1` with `symbol`, `qty`, `tradeSide` (1/2), `tradeType`, `price` | POST (or WS) `/private/buy` or `/private/sell` with `instrument_name`, `amount`, `type` (limit/market), `price` |
| **Side Encoding** | Numeric: `1`=buy, `2`=sell | Separate endpoints: `/private/buy` vs `/private/sell` |
| **Cancel** | POST `/open/option/order/cancel/v1` `{orderId}` | `/private/cancel` `{order_id}` |
| **Order Status** | GET `/open/option/order/singleQuery/v1?orderId=X` | `/private/get_order_state` `{order_id}` |
| **Status Codes** | Numeric: `0`=NEW, `1`=FILLED, `2`=PARTIAL, `3`=CANCELED … | String: `"open"`, `"filled"`, `"cancelled"`, `"rejected"`, `"untriggered"` |
| **Client Order ID** | `clientOrderId` field | `label` field (max 64 chars) |
| **Reduce Only** | `reduceOnly` boolean | `reduce_only` boolean |

**Implication:** The buy/sell split into separate endpoints is a minor structural difference. String-based status codes are actually cleaner than Coincall's numeric codes. The `label` field replaces `clientOrderId` for idempotency tracking.

### 4.4 Account & Positions

| Aspect | Coincall | Deribit |
|--------|----------|---------|
| **Account Summary** | `/open/account/summary/v1` → `equity`, `availableMargin`, `imAmount`, `mmAmount` | `/private/get_account_summary?currency=BTC` → `equity`, `available_funds`, `initial_margin`, `maintenance_margin` |
| **Positions** | `/open/option/position/get/v1` → list with `positionId`, `symbol`, `qty`, `tradeSide`, `upnlByMarkPrice`, Greeks | `/private/get_positions?currency=BTC&kind=option` → list with `instrument_name`, `size`, `direction` ("buy"/"sell"), `floating_profit_loss`, `delta`, `gamma`, `vega`, `theta` |
| **Margin Model** | Cross-margin; basic portfolio margining | Full portfolio margin (PM); significantly more capital-efficient |

**Implication:** Fields are similar in concept but different in name and structure. The portfolio margining difference is a *strategic advantage* — same strategies will require less capital on Deribit.

### 4.5 Contract Naming Conventions

| Exchange | Format | Example |
|----------|--------|---------|
| **Coincall** | `{UNDERLYING}{QUOTE}-{DDMMMYY}-{STRIKE}-{TYPE}` | `BTCUSD-28MAR26-100000-C` |
| **Deribit** | `{UNDERLYING}-{DMMMYY}-{STRIKE}-{TYPE}` | `BTC-28MAR26-100000-C` |

Key differences:
- Coincall uses `BTCUSD`; Deribit uses `BTC`
- Coincall date format: `28MAR26`; Deribit date format: `28MAR26` (same!)
- Strike and type encoding: identical
- Deribit also has `BTC-PERPETUAL` for the perp (no Coincall equivalent used)

**Implication:** Very close. A simple prefix transformation handles this.

### 4.6 RFQ / Block Trades

Deribit has a full-featured **Block RFQ** system — actually *more sophisticated* than Coincall's. Both exchanges follow the same fundamental model (taker creates RFQ → market makers respond with quotes → taker crosses the best quote), but the details differ significantly.

| Aspect | Coincall | Deribit Block RFQ |
|--------|----------|-------------------|
| **RFQ System** | Full lifecycle: create → poll quotes → accept/reject | Full lifecycle: `create_block_rfq` → MMs quote → `accept_block_rfq` |
| **API Protocol** | REST (HTTP POST/GET) | JSON-RPC 2.0 (over REST or WebSocket) |
| **Leg Format** | `{instrumentName, side: "BUY"/"SELL", qty}` | `{instrument_name, direction: "buy"/"sell", amount}` |
| **Direction Model** | Each leg has `side`; separate `action` param at acceptance | Each leg has `direction`; crossing order specifies `direction` + `price` |
| **Minimum Size** | $50k notional (sum of strike × qty) | **25 BTC** or **250 ETH** option contracts (significantly larger) |
| **Expiry** | Configurable timeout | Fixed **5 minutes** from creation |
| **Quote Visibility** | Taker sees individual MM quotes with quote IDs | **Blind auction**: taker sees aggregated best bid/ask after 5s delay |
| **Multi-Maker** | Single MM per accepted quote | **Multi-maker matching**: multiple MMs can fill one RFQ at the last matched price |
| **Pricing Model** | Individual leg prices per quote; taker accepts by `quoteId` | **Ratio-based pricing**: GCD-reduced leg ratios; structure priced as a single unit |
| **Crossing** | `accept_quote(requestId, quoteId)` | `accept_block_rfq(block_rfq_id, direction, price, amount, legs)` — taker specifies a price they're willing to trade at |
| **Trigger/Limit Orders** | Not supported — taker must manually accept | Supported: `time_in_force: "good_til_cancelled"` keeps order open until matched |
| **Hedge Legs** | Not supported | Supported: add a perpetual/future leg for delta hedging (price fixed within 1% of mark) |
| **Anonymity** | Basic | Sophisticated: anonymous (min 5 targeted MMs) or disclosed identity; taker rating system (OTV ratio) |
| **Pre-Allocation** | Not supported | Supported: split trade across sub-accounts or broker-linked clients |
| **Real-Time Updates** | REST polling only | WebSocket subscriptions: `block_rfq.taker.{currency}`, `block_rfq.maker.{currency}` |
| **API Scope** | Uses general trading API keys | Requires dedicated `block_rfq:read` and `block_rfq_id:read_write` scopes |
| **Block Trade Result** | Trade details in accept response | Trades reported as standard block trades with `block_trade_id` + `block_rfq_id` linkage |
| **Form Encoding** | Accept/cancel use `application/x-www-form-urlencoded` | All calls use JSON-RPC 2.0 (consistent with rest of Deribit API) |
| **RFQ States** | PENDING, ACTIVE, FILLED, CANCELLED, EXPIRED, TRADED_AWAY | created, open, filled, cancelled, expired |
| **Combo Recognition** | Not applicable | Returns `combo_id` for recognized strategies (e.g., `BTC-CS-14FEB25-100000_110000` for a call spread) |
| **Leg Pricing Helper** | Not available | `private/get_leg_prices` decomposes a structure price into valid per-leg prices |

#### Deribit Block RFQ Lifecycle (Taker Perspective)

```
1. Create:    private/create_block_rfq  →  {legs: [{instrument_name, amount, direction}], makers?: [...]}
2. Monitor:   Subscribe to block_rfq.taker.{currency}  (or poll via private/get_block_rfqs)
              → receive real-time updates with aggregated bids[] and asks[]
3. Cross:     private/accept_block_rfq   →  {block_rfq_id, direction, price, amount, legs: [{..., ratio}]}
              → if matching liquidity exists at price, trade executes immediately
              → optionally use time_in_force: "good_til_cancelled" to keep order open
4. Cancel:    private/cancel_block_rfq   →  {block_rfq_id}
5. Result:    Trades appear as block trades in position/trade history with block_rfq_id linkage
```

#### Deribit Block RFQ — Key Design Differences from Coincall

**Ratio-based pricing:** Deribit normalizes multi-leg structures into their smallest integer ratio. For example, a 100-lot call spread (buy 100 of strike A, sell 100 of strike B) has ratio `[1, -1]` and amount `100`. Prices are quoted *per unit of the structure*, not per individual leg. This is a cleaner model but requires our abstraction to handle the translation.

**Multi-maker matching:** Unlike Coincall where you pick one MM's quote, Deribit aggregates multiple MMs' liquidity. If you request 100 BTC and two MMs each offer 60, both get filled (60 + 40 or proportionally). All trades print at the *last matched price* — MMs who quoted tighter are not penalized, which incentivizes competitive pricing.

**Blind auction with 5s delay:** The taker cannot see individual MM quotes. After 5 seconds, the taker sees the best aggregated bid and ask. This prevents information leakage and gaming.

**Trigger orders:** The taker can place a limit-like crossing order with `good_til_cancelled` that sits passively until a maker's quote matches or improves the taker's price. This is powerful for phased execution — our current `rfq_phased` strategy metadata can map directly to this.

**Minimum size reality check:** At current BTC prices (~$84k), 25 BTC of options ≈ **$2.1M notional**. This is substantially larger than Coincall's $50k minimum. For our typical position sizes ($10k–$200k), Deribit Block RFQ may be **too large**. We may need to use regular limit orders for most trades and only use Block RFQ for very large positions. This is an important sizing decision.

**Implication:** The RFQ abstraction is both possible and necessary. The two systems share the same conceptual lifecycle (create → receive quotes → cross/cancel) but differ in API shape, pricing model, quote visibility, and minimum sizes. A well-designed `ExchangeRFQExecutor` interface can cover both.

### 4.7 WebSocket vs REST

Coincall: REST-only approach (current implementation polls every 10-30s).  
Deribit: WebSocket-first design. All private operations can be performed over a single WebSocket connection using JSON-RPC 2.0, including order placement, account queries, and market data subscriptions.

**Implication:** Phase 1 can use REST (Deribit REST API is fully functional). Phase 2+ should migrate to WebSocket for latency and rate-limit efficiency. This is an *improvement opportunity*, not a blocker.

---

## 5. Proposed Abstraction Layer

The core architectural change is the introduction of an **Exchange Provider** abstraction. This is a set of Python protocols (or abstract base classes) that define the contract between the strategy/lifecycle layer and the exchange-specific implementation.

### 5.1 Design Principle: Ports and Adapters

```
┌─────────────────────────────────────────────────────────┐
│  Strategy Layer (unchanged)                             │
│  StrategyRunner, StrategyConfig, entry/exit conditions  │
└──────────────────────┬──────────────────────────────────┘
                       │ uses
┌──────────────────────▼──────────────────────────────────┐
│  Core Domain (unchanged)                                │
│  LifecycleEngine, ExecutionRouter, OrderManager,        │
│  TradeLifecycle, TradeLeg, PositionMonitor              │
└──────────────────────┬──────────────────────────────────┘
                       │ depends on (via interfaces)
┌──────────────────────▼──────────────────────────────────┐
│  Exchange Abstraction Layer (NEW)                       │
│  ExchangeAuth, ExchangeMarketData, ExchangeExecutor,   │
│  ExchangeAccountManager                                 │
└──────┬───────────────────────────────────┬──────────────┘
       │                                   │
┌──────▼──────────┐              ┌─────────▼─────────┐
│  Coincall Impl  │              │  Deribit Impl     │
│  (existing code │              │  (new code)       │
│   reorganized)  │              │                   │
└─────────────────┘              └───────────────────┘
```

### 5.2 Proposed Interfaces

#### `ExchangeAuth`
Responsible for authenticating API requests. Hides the signing/token mechanism.

```
Methods:
  - authenticated_request(method, endpoint, params, body) → response
  - get_ws_auth_message() → dict  (for future WebSocket auth)
```

The key insight: the rest of the system should never construct HTTP requests directly. It should call `auth.request(...)` and get back parsed JSON. This is already how `auth.py` works today — the abstraction is natural.

#### `ExchangeMarketData`
Responsible for all read-only market queries.

```
Methods:
  - get_index_price(underlying: str) → float
  - get_option_instruments(underlying: str) → List[Instrument]
  - get_option_ticker(symbol: str) → OptionTicker  (includes Greeks, mark price, bid/ask)
  - get_orderbook(symbol: str, depth: int) → Orderbook
  - get_expiry_dates(underlying: str) → List[date]
```

Where `Instrument`, `OptionTicker`, and `Orderbook` are **normalized dataclasses** shared across exchanges. The exchange implementation is responsible for mapping exchange-specific field names into these normalized structures.

#### `ExchangeExecutor`
Responsible for order lifecycle operations.

```
Methods:
  - place_order(symbol, side, qty, price, order_type, client_id, reduce_only) → OrderResult
  - cancel_order(order_id) → bool
  - get_order_status(order_id) → OrderStatus
  - get_open_orders() → List[Order]
```

Where `side` is normalized to `"buy"` / `"sell"` (string, not `1`/`2`), and `OrderStatus` is the internal enum already defined in `order_manager.py`.

#### `ExchangeAccountManager`
Responsible for account and position queries.

```
Methods:
  - get_account_summary() → AccountSummary
  - get_positions() → List[Position]
```

Where `AccountSummary` and `Position` are normalized dataclasses with standard field names (`equity`, `available_margin`, `initial_margin`, `maintenance_margin`, `unrealized_pnl`, etc.).

#### `ExchangeRFQExecutor`
Responsible for the RFQ/block trade lifecycle. This is the key new abstraction that enables RFQ support on both exchanges.

```
Methods:
  - create_rfq(legs: List[RFQLeg]) → RFQHandle
  - get_quotes(rfq_handle: RFQHandle) → RFQQuoteSnapshot
  - accept_rfq(rfq_handle: RFQHandle, direction: str, price: float) → RFQTradeResult
  - cancel_rfq(rfq_handle: RFQHandle) → bool
  - get_rfq_status(rfq_handle: RFQHandle) → RFQState
  - get_orderbook_cost(legs: List[RFQLeg], direction: str) → Optional[float]
```

This interface abstracts over the fundamental differences between Coincall and Deribit RFQ systems while preserving the shared lifecycle:

```
create → poll/subscribe for quotes → evaluate → accept or cancel
```

The critical design decisions in this abstraction:

**1. `RFQLeg` — Normalized leg representation:**
```python
@dataclass
class RFQLeg:
    symbol: str           # Exchange-native symbol
    direction: str        # "buy" or "sell" (normalized)
    qty: float            # Quantity in base currency
```
Each exchange adapter translates to its own format:
- Coincall: `{instrumentName, side: "BUY"/"SELL", qty}` 
- Deribit: `{instrument_name, direction: "buy"/"sell", amount}`

**2. `RFQHandle` — Opaque identifier returned by `create_rfq`:**
```python
@dataclass
class RFQHandle:
    rfq_id: str                    # Exchange-specific ID (requestId / block_rfq_id)
    legs: List[RFQLeg]             # Original legs
    ratios: Optional[List[int]]    # Deribit: GCD-reduced ratios; Coincall: None
    total_amount: Optional[float]  # Deribit: total amount per ratio unit; Coincall: None
    combo_id: Optional[str]        # Deribit: recognized strategy combo; Coincall: None
    expiry_time: int               # Unix ms when RFQ expires
    state: RFQState
```
This handle carries enough context for the taker to cross the RFQ later. On Deribit, the ratios and combo_id are returned by the exchange at creation and must be echoed back at acceptance. On Coincall, these fields are simply None.

**3. `RFQQuoteSnapshot` — What the taker sees:**
```python
@dataclass
class RFQQuoteSnapshot:
    best_bid: Optional[RFQQuoteLevel]   # Best price if we want to sell
    best_ask: Optional[RFQQuoteLevel]   # Best price if we want to buy
    raw_quotes: List[Any]               # Exchange-specific quote details for logging
    mark_price: Optional[float]         # Structure mark price (if available)
```

@dataclass
class RFQQuoteLevel:
    price: float          # Per-unit structure price
    amount: float         # Available quantity
    quote_id: Optional[str]  # Coincall: specific quoteId to accept; Deribit: None (crossing by price)
```
This normalizes the critical difference: Coincall accepts by `quoteId`, Deribit crosses by `price`+`direction`. The `accept_rfq` method on each implementation knows which field to use.

**4. `RFQTradeResult` — Execution outcome:**
```python
@dataclass
class RFQTradeResult:
    success: bool
    rfq_id: str
    block_trade_id: Optional[str]       # Deribit: BLOCK-XXXXXX; Coincall: None
    direction: str                       # "buy" or "sell"
    price: float                         # Executed structure price
    amount: float                        # Executed quantity
    legs: List[RFQTradeLeg]             # Per-leg fill details
    total_cost: float                    # Net cost (positive=debit, negative=credit)
    orderbook_cost: Optional[float]      # Comparison baseline
    improvement_pct: float               # vs orderbook
    message: str
```

**5. High-level `execute()` remains in the shared layer:**

The current `RFQExecutor.execute()` method — which orchestrates the create → poll → evaluate → accept/cancel loop — is exchange-agnostic in its logic. It becomes a shared function that works with the `ExchangeRFQExecutor` interface:

```
def execute_rfq(
    rfq_executor: ExchangeRFQExecutor,
    legs: List[RFQLeg],
    direction: str,
    timeout_seconds: int,
    min_improvement_pct: float,
    poll_interval_seconds: int,
) → RFQTradeResult:
    # 1. Get orderbook baseline
    # 2. Create RFQ
    # 3. Poll for quotes (or listen via callback)
    # 4. Evaluate best quote vs baseline
    # 5. Accept or cancel
```

The phased execution logic (initial wait → mark floor → relax) also lives in the shared layer, parameterized by strategy metadata.

### 5.2.1 RFQ Abstraction — Exchange-Specific Adapters

#### `CoincallRFQExecutor` (wraps current `rfq.py`)

```
create_rfq(legs):
    # Translate RFQLeg → {instrumentName, side, qty}
    # POST /open/option/blocktrade/request/create/v1
    # Return RFQHandle with requestId

get_quotes(handle):
    # GET /open/option/blocktrade/request/getQuotesReceived/v1?requestId=X
    # Parse individual MM quotes, compute costs
    # Return RFQQuoteSnapshot with best_bid/ask and individual quote_ids

accept_rfq(handle, direction, price):
    # Find the quote matching direction + best price
    # POST /open/option/blocktrade/request/accept/v1 {requestId, quoteId}
    # (Coincall requires specific quoteId)

cancel_rfq(handle):
    # POST /open/option/blocktrade/request/cancel/v1 {requestId}
```

#### `DeribitRFQExecutor` (new implementation)

```
create_rfq(legs):
    # Translate RFQLeg → {instrument_name, amount, direction}
    # JSON-RPC: private/create_block_rfq {legs: [...], makers?: [...]}
    # Return RFQHandle with block_rfq_id, ratios, combo_id, total_amount

get_quotes(handle):
    # JSON-RPC: private/get_block_rfqs {block_rfq_id}
    # (or receive via WebSocket subscription block_rfq.taker.{currency})
    # Parse aggregated bids[]/asks[] into RFQQuoteSnapshot
    # Note: individual MMs are not visible — only aggregated best quote

accept_rfq(handle, direction, price):
    # JSON-RPC: private/accept_block_rfq {
    #   block_rfq_id, direction, price, amount,
    #   legs: [{instrument_name, ratio, direction}]  ← echo ratios from creation
    # }
    # Can use time_in_force: "good_til_cancelled" for trigger orders
    # (Deribit crosses by price, not by specific quoteId)

cancel_rfq(handle):
    # JSON-RPC: private/cancel_block_rfq {block_rfq_id}
```

### 5.2.2 RFQ Abstraction — Design Rationale

**Why not just skip RFQ on Deribit?** Deribit's orderbook is deep enough for most trades. However:
- For very large positions (25+ BTC options), Block RFQ provides better execution than legging in
- Block RFQ aggregates liquidity from MMs who may not be quoting on-screen
- The phased pricing model (multi-maker, last-matched-price) actively incentivizes competitive quotes
- Block trade fees may differ from on-screen fees
- Hedge legs allow atomic delta-hedging with a futures position

**Why abstract rather than have two different RFQ modules?** The high-level orchestration (create → poll → evaluate → accept) is identical. Duplicating this logic means duplicating the phased execution strategy, improvement calculation, orderbook comparison, logging, and timeout management. The exchange-specific part is purely the API translation in 4-5 methods.

**Minimum size implications:** Coincall's $50k minimum is easily met by our strategies. Deribit's 25 BTC minimum (≈$2.1M at current prices) is much larger. The `ExecutionRouter` must be aware of this threshold and only route to RFQ when the trade qualifies. For sub-threshold trades, limit orders or the smart orderbook executor are used instead.

### 5.3 Normalized Data Models

A critical part of the abstraction is **normalized data models** that the rest of the system works with:

| Model | Key Fields | Notes |
|-------|-----------|-------|
| `Instrument` | `symbol` (exchange-native), `underlying`, `expiry`, `strike`, `option_type`, `is_active` | Parsed from exchange-specific naming |
| `OptionTicker` | `symbol`, `mark_price`, `bid`, `ask`, `last_price`, `delta`, `gamma`, `theta`, `vega`, `open_interest`, `volume` | Unified Greeks |
| `Orderbook` | `bids: List[(price, qty)]`, `asks: List[(price, qty)]`, `mark_price` | Standard L2 book |
| `OrderResult` | `order_id`, `client_id`, `status`, `filled_qty`, `avg_price` | Returned from place/query |
| `AccountSummary` | `equity`, `available_margin`, `initial_margin`, `maintenance_margin`, `unrealized_pnl`, `currency` | Normalized across exchanges |
| `Position` | `symbol`, `qty`, `side` ("buy"/"sell"), `avg_price`, `mark_price`, `unrealized_pnl`, `delta`, `gamma`, `vega`, `theta` | Normalized side encoding |

### 5.4 Side Encoding Migration

Today: `1` = buy, `2` = sell (Coincall convention, baked into `TradeLeg`, `order_manager`, and `trade_execution`).

Proposal: Normalize to `"buy"` / `"sell"` strings at the abstraction boundary. The exchange implementations translate:
- Coincall: `"buy"` → `1`, `"sell"` → `2`
- Deribit: `"buy"` → calls `/private/buy`, `"sell"` → calls `/private/sell`

The internal data model (`TradeLeg.side`) should migrate to strings. This is a sweeping but mechanical change.

### 5.5 Exchange Factory

A single factory function selects the exchange implementation based on configuration:

```
EXCHANGE=deribit  (in .env)

build_exchange(exchange_name) → {auth, market_data, executor, account_manager}
```

This replaces the current `build_context()` wiring in `strategy.py`, making the exchange selection a configuration choice rather than a code change.

---

## 6. Module-by-Module Migration Plan

### 6.1 `config.py` — Exchange-Aware Configuration

**Change:** Add `EXCHANGE` env var. Load exchange-specific URLs and credentials based on its value.

```
EXCHANGE=deribit

# Deribit credentials
DERIBIT_CLIENT_ID=...
DERIBIT_CLIENT_SECRET=...

# Or Coincall credentials (for backward compat)
COINCALL_API_KEY_PROD=...
COINCALL_API_SECRET_PROD=...
```

The module exports `EXCHANGE`, `BASE_URL`, `WS_URL`, `API_KEY`, `API_SECRET` — resolved from the selected exchange. Downstream modules don't change their imports.

### 6.2 `auth.py` → `exchanges/coincall/auth.py`

**Change:** Move current auth logic into a Coincall-specific module. Create `exchanges/deribit/auth.py` implementing the same interface with Deribit's OAuth2 token flow.

Deribit auth flow:
1. On startup: POST `/public/auth` with `client_id` + `client_secret` + grant_type `client_credentials`
2. Receive `access_token` (JWT, ~900s TTL) + `refresh_token`
3. Before expiry: POST `/public/auth` with `refresh_token` grant to get new access token
4. All private requests: `Authorization: Bearer <access_token>` header

The token refresh can run on a background timer or be checked before each request (lazy refresh).

### 6.3 `market_data.py` → `exchanges/{exchange}/market_data.py`

**Change:** Extract the current Coincall-specific endpoint calls into `exchanges/coincall/market_data.py`. Create `exchanges/deribit/market_data.py` implementing the same `ExchangeMarketData` interface.

The existing **caching layer** (TTL cache, 30s) stays in a shared base class or wrapper — caching is exchange-agnostic.

Key mapping:

| Operation | Coincall | Deribit |
|-----------|----------|---------|
| Instruments | `/open/option/getInstruments/BTCUSD` | `/public/get_instruments?currency=BTC&kind=option` |
| Option ticker | Per-option detail call | `/public/ticker?instrument_name=BTC-28MAR26-100000-C` |
| Greeks | Separate detail call | Included in ticker (`greeks` field) |
| Index price | `/open/futures/ticker/BTCUSDT` | `/public/get_index_price?index_name=btc_usd` |
| Orderbook | Orderbook depth endpoint | `/public/get_order_book?instrument_name=...` |

### 6.4 `trade_execution.py` → `exchanges/{exchange}/executor.py`

**Change:** Extract Coincall order placement into `exchanges/coincall/executor.py`. Create `exchanges/deribit/executor.py`.

Key differences to handle:
- Deribit uses separate `/private/buy` and `/private/sell` endpoints (no `tradeSide` field)
- Order status is string-based (`"open"`, `"filled"`, `"cancelled"`) not numeric
- `label` replaces `clientOrderId`
- `amount` replaces `qty`
- `reduce_only` (underscore) replaces `reduceOnly` (camelCase)

The `LimitFillManager` and `ExecutionPhase` logic stays in the shared layer — they work with the abstract `ExchangeExecutor` interface.

### 6.5 `order_manager.py` — Parameterized Status Mapping

**Change:** Replace the hardcoded `_EXCHANGE_STATE_MAP` with an exchange-provided mapping. The `OrderManager` itself is exchange-agnostic — it just needs to know how to translate exchange status codes into its internal `OrderStatus` enum.

```
coincall_status_map = {0: LIVE, 1: FILLED, 2: PARTIAL, 3: CANCELLED, ...}
deribit_status_map  = {"open": LIVE, "filled": FILLED, "cancelled": CANCELLED, ...}
```

The exchange implementation provides this map at construction time.

### 6.6 `account_manager.py` → `exchanges/{exchange}/account.py`

**Change:** Extract Coincall account/position API calls. The `PositionMonitor` (background polling thread) and `AccountSnapshot` dataclass stay in the shared layer — they're exchange-agnostic. Only the data-fetching methods move.

Field mapping:

| Concept | Coincall field | Deribit field |
|---------|---------------|--------------|
| Equity | `equity` | `equity` |
| Available margin | `availableMargin` | `available_funds` |
| Initial margin | `imAmount` | `initial_margin` |
| Maintenance margin | `mmAmount` | `maintenance_margin` |
| Unrealized PnL | `upnlByMarkPrice` | `floating_profit_loss` |
| Position side | `tradeSide` (1/2) | `direction` ("buy"/"sell") |
| Position symbol | `symbol` | `instrument_name` |

### 6.7 `rfq.py` → Exchange-Abstracted RFQ

**Change:** The current monolithic `rfq.py` is split into:

1. **Shared orchestration** (`rfq_orchestrator.py` or integrated into `execution_router.py`) — the create → poll → evaluate → accept loop, phased execution timing, orderbook comparison, improvement calculation. This is exchange-agnostic.

2. **`exchanges/coincall/rfq.py`** — the Coincall-specific API adapter implementing `ExchangeRFQExecutor`. This is essentially the current `create_rfq()`, `get_quotes()`, `accept_quote()`, `cancel_rfq()` methods, translated to use the normalized data models.

3. **`exchanges/deribit/rfq.py`** — the Deribit Block RFQ adapter implementing `ExchangeRFQExecutor`. New implementation using:
  - `private/create_block_rfq` for creation
  - `private/get_block_rfqs` for polling (or WebSocket subscription for Phase 4)
  - `private/accept_block_rfq` for crossing
  - `private/cancel_block_rfq` for cancellation
  - `private/get_leg_prices` for decomposing structure prices into per-leg prices

The `RFQResult` and `RFQState` data classes migrate to the shared `exchanges/base.py` as part of the normalized model.

Key translation work for the Deribit adapter:
- Convert `RFQLeg` (symbol, direction, qty) → Deribit format (instrument_name, direction, amount)
- Handle ratio-based pricing: Deribit returns ratios at creation that must be echoed at acceptance
- Parse aggregated bids/asks into `RFQQuoteSnapshot` (no individual quote IDs on Deribit)
- Map Deribit RFQ states (`created`, `open`, `filled`, `cancelled`, `expired`) to internal `RFQState` enum
- Handle Deribit's `combo_id` for recognized strategies (informational, not required for execution)

### 6.8 `execution_router.py` — Exchange-Aware Routing with RFQ

**Change:** The routing decision tree becomes exchange-aware, with RFQ supported on *both* exchanges but with different thresholds:

- **Coincall:** Single leg → limit; multi-leg ≥ $50k notional → RFQ; multi-leg < $50k → smart orderbook
- **Deribit:** Single leg → limit; multi-leg ≥ 25 BTC → Block RFQ; multi-leg < 25 BTC → smart orderbook / limit

The router receives the RFQ minimum threshold from the exchange configuration:

```python
class ExchangeConfig:
    rfq_min_btc_contracts: Optional[float]   # Deribit: 25.0
    rfq_min_notional_usd: Optional[float]    # Coincall: 50_000
    rfq_available: bool                      # True on both
    combo_orders_available: bool             # Deribit: True (Phase 4)
```

The router calculates whether the trade meets the exchange's RFQ minimum and routes accordingly. For most of our current strategies on Deribit (position sizes well under 25 BTC), trades will route through limit orders. RFQ becomes available for larger positions or when explicitly requested via strategy metadata (`execution_mode: "rfq"`).

**Fallback chain on Deribit:**
1. If `execution_mode: "rfq"` in metadata AND trade meets minimum → Block RFQ
2. If multi-leg and combo instrument exists → combo order (Phase 4)
3. If multi-leg → smart orderbook executor (leg individually)
4. Single leg → limit order

### 6.9 `option_selection.py` — Parameterized Symbol Parsing

**Change:** Extract the symbol parsing and construction into exchange-specific helpers:

```
coincall: parse("BTCUSD-28MAR26-100000-C") → Instrument(underlying="BTC", expiry=..., strike=100000, type="C")
deribit:  parse("BTC-28MAR26-100000-C")    → Instrument(underlying="BTC", expiry=..., strike=100000, type="C")
```

The selection algorithms (`find_option`, `resolve_legs`, delta-based selection) work on the normalized `Instrument` model and are completely exchange-agnostic.

### 6.10 `trade_lifecycle.py` — Normalize Side Encoding

**Change:** Migrate `TradeLeg.side` from `int` (1/2) to `str` ("buy"/"sell"). This is a mechanical find-and-replace across the codebase, with exchange implementations handling the translation at the boundary.

### 6.11 `strategy.py` — Generic TradingContext

**Change:** Update `TradingContext` to reference abstract types instead of concrete Coincall types:

```
Before: auth: CoincallAuth, market_data: MarketData, executor: TradeExecutor
After:  auth: ExchangeAuth, market_data: ExchangeMarketData, executor: ExchangeExecutor
```

The `build_context()` function reads `EXCHANGE` from config and constructs the appropriate implementation.

### 6.12 Strategies — Minimal Changes

The strategies (`daily_put_sell`, `atm_straddle`, `blueprint_strangle`) need only:
1. Remove RFQ-specific metadata (or make it conditional on exchange)
2. Ensure strike selection uses the normalized `ExchangeMarketData` interface

The entry/exit conditions, position sizing, and timing logic are all exchange-agnostic already.

---

## 7. Contract & Symbol Translation

### 7.1 Symbol Mapping

| Component | Coincall | Deribit |
|-----------|----------|---------|
| Underlying | `BTCUSD` | `BTC` |
| Separator | `-` | `-` |
| Date format | `28MAR26` | `28MAR26` |
| Strike | `100000` | `100000` |
| Type | `C` / `P` | `C` / `P` |
| Full symbol | `BTCUSD-28MAR26-100000-C` | `BTC-28MAR26-100000-C` |

A `SymbolTranslator` utility handles parsing and construction per exchange. Internally, the system works with structured `Instrument` objects; symbols are only needed when making API calls.

### 7.2 Underlying Mapping

| Asset | Coincall | Deribit |
|-------|----------|---------|
| Bitcoin options | `BTCUSD` | `BTC` |
| Ethereum options | `ETHUSD` | `ETH` |
| Index | via futures ticker | `btc_usd` / `eth_usd` |

### 7.3 Expiry Representation

Both exchanges use the same `DDMMMYY` format. Deribit expiries settle at **08:00 UTC**; Coincall at **08:00 UTC** as well. No timezone conversion needed.

---

## 8. Execution Model Changes

### 8.1 RFQ on Both Exchanges — Different Thresholds, Same Abstraction

Both Coincall and Deribit support RFQ, but with very different minimum sizes:

| | Coincall | Deribit |
|-|----------|--------|
| **Minimum** | $50k notional | 25 BTC contracts (~$2.1M at $84k) |
| **Practical impact** | Most multi-leg trades qualify | Only very large positions qualify |
| **Default execution** | RFQ for size; limit for small | Limit/smart orderbook for most; Block RFQ for size |

For our current strategy sizes ($10k–$200k notional), the practical reality is:
- **Coincall:** RFQ is the primary execution mode for multi-leg structures (nearly everything meets $50k)
- **Deribit:** Limit orders / smart orderbook for most trades; Block RFQ only when we scale up to 25+ BTC positions

The `ExchangeRFQExecutor` abstraction handles both. The `ExecutionRouter` checks the exchange-specific minimum threshold before routing to RFQ. If a strategy explicitly requests RFQ mode but the trade doesn't meet the minimum, it falls back gracefully to the smart orderbook executor.

### 8.2 Deribit Block RFQ — Operational Considerations

When we do use Deribit Block RFQ, several operational details differ from Coincall:

**API key setup:** Block RFQ requires dedicated scopes (`block_rfq:read`, `block_rfq_id:read_write`) that must be explicitly enabled on the API key. This is a one-time setup step.

**Taker rating:** Deribit tracks an order-to-volume (OTV) ratio for takers. Creating many RFQs without trading on them ("price fishing") increases the OTV ratio, which MMs can use to filter out requests. Our bot should only create RFQs it intends to trade on.

**Anonymous vs disclosed:** By default, RFQs are anonymous but require targeting at least 5 MMs. If we want to target fewer MMs, we must disclose our identity. For most cases, anonymous targeting of all MMs is ideal (maximum competition).

**Multi-maker execution:** Unlike Coincall where we accept one specific MM's quote, Deribit may fill our order across multiple MMs. Each fill is a separate block trade but all share the same `block_rfq_id`. Our trade tracking must handle one RFQ producing multiple block trades.

**Trigger orders for phased execution:** Our current `rfq_phased` strategy metadata (initial wait → mark floor → relax) maps well to Deribit's `good_til_cancelled` trigger orders. Instead of polling and manually deciding when to accept, we can place a crossing order at our desired price and let it sit until a maker meets it, updating the price over time.

### 8.3 Combo Orders (Future Optimization)

Deribit supports **combo instruments** — native multi-leg orders that execute atomically. For example, a straddle can be traded as a single combo instrument with its own orderbook. This would allow:
- Atomic execution of straddle/strangle opens (no leg risk)
- Native combo orderbook with tighter spreads than legging in
- Simpler execution tracking (one order instead of two)
- Deribit Block RFQ already returns `combo_id` for recognized strategies — this links the RFQ to the corresponding combo orderbook

This is a Phase 4 optimization, not a migration requirement.

### 8.4 WebSocket Execution (Future Optimization)

Deribit's WebSocket API supports order placement via JSON-RPC. This reduces latency from ~100ms (REST round-trip) to ~10ms (WebSocket frame). Relevant for:
- Requoting during `LimitFillManager` execution phases
- Real-time fill notifications (subscriptions instead of polling)
- Market data streaming instead of REST polling
- **Block RFQ real-time updates** via `block_rfq.taker.{currency}` subscription (replace REST polling of quotes)

This is a Phase 4 optimization.

---

## 9. Testing Strategy

### 9.0 Philosophy

We don't need hundreds of tests. We need a small set of **rigorous, live-account tests** that prove each layer works against real Deribit infrastructure. The goal is to *learn Deribit's actual behavior* — its timing, data formats, edge cases, and quirks — not to achieve coverage metrics.

**Core principle:** Test early on live accounts (testnet first, then production with small size). Mock-based unit tests are useful for regression, but they cannot catch the real surprises — unexpected field names, inconsistent null handling, rate limit behavior, order state race conditions. The live tests are what actually de-risk the migration.

**Design principle:** Each test below is self-contained and can be implemented and run independently. They're written for an AI agent to pick up one at a time during implementation.

### 9.1 Test Environment Setup

Deribit provides a full **testnet** at `test.deribit.com`:
- Free test funds (request via dashboard)
- Same API surface as production
- Simulated orderbook with synthetic liquidity
- Same contract specifications

#### Credentials (⚠️ rotate before production use)

**Testnet** (`test.deribit.com`):
- Login: `trader8@aureaasfinance.com`
- Password: `Testiger2026#`
- Client ID: `CWlZBUXA`
- Client Secret: `sVrL_Bdz-j8_mtLB-y4EdxPS-YGkqeMtLzh4Wi1sz2E`
- Scopes: `block_rfq:read_write block_trade:read_write trade:read_write custody:read account:read_write wallet:read`
- Balance: ~100 BTC (testnet funds)

**Production** (`deribit.com`, low-balance account):
- Client ID: `TV6tvw6J`
- Client Secret: `NUDhggDLNwL9xj6N2_e-2dqP4jOrKnrBFRMVopK_IAM`
- Scopes: `block_rfq:read_write block_trade:read_write trade:read_write custody:read account:read_write wallet:read`
- Balance: ~0.1 BTC (~$8,400)

**Auth verified:** 16 Mar 2026 — both keys authenticate successfully, token TTL = 900s.

The `.env` file supports `TRADING_ENVIRONMENT=testnet` which maps to Deribit testnet URLs. All tests below should first pass on testnet, then be re-run once on production with minimum size to confirm real-world behavior matches.

**Test file location:** `tests/deribit/` — one file per test area.

### 9.2 Live Integration Tests

These are the tests that matter. Each one hits the real Deribit API and validates that our adapter handles the actual response correctly. Run these sequentially — they're designed to be safe (read-only or using minimum-size orders that get cancelled).

---

#### Test 1: Authentication Lifecycle (`test_deribit_auth.py`)

**What we're proving:** OAuth2 token grant works, tokens are valid, refresh works before expiry.

```
Steps:
  1. Authenticate with client_id + client_secret → receive access_token + refresh_token
  2. Assert: access_token is non-empty, expires_in > 0
  3. Make an authenticated call (e.g., /private/get_account_summary) → assert 200, not auth error
  4. Wait for 80% of TTL (or call refresh immediately for the test)
  5. Refresh token → receive new access_token
  6. Make another authenticated call with new token → assert success
  7. Try a call with the OLD token → observe behavior (does Deribit reject it immediately or after a grace period?)
```

**What to learn:** Token TTL in practice, whether old tokens are invalidated immediately on refresh, error format for expired tokens.

---

#### Test 2: Market Data — Live Price Feed (`test_deribit_market_data.py`)

**What we're proving:** We can read instruments, orderbooks, and tickers, and correctly parse Deribit's BTC-denominated option prices.

**Critical detail:** Deribit's standard BTC options are quoted in **BTC prices, not USD**. A call option might show `mark_price: 0.045` meaning 0.045 BTC, not $0.045. Our normalization layer must handle this correctly — multiply by index price if we need USD values internally.

```
Steps:
  1. GET /public/get_instruments?currency=BTC&kind=option
     → Assert: response is a list; each item has instrument_name, strike, expiration_timestamp,
       option_type, min_trade_amount, tick_size, is_active
     → Log a few examples to see exact format
     → Verify our Instrument parser handles all fields

  2. Pick one active option (e.g., nearest ATM call expiring in ~7 days)
     GET /public/ticker?instrument_name={selected}
     → Assert: response has mark_price, best_bid_price, best_ask_price, greeks (delta, gamma, vega, theta),
       underlying_price, underlying_index
     → CRITICAL CHECK: mark_price is in BTC (should be < 1.0 for most options, not thousands)
     → Verify: underlying_price (the BTC index) is a reasonable number (~$80k-$100k range)
     → Compute: mark_price_usd = mark_price * underlying_price → assert it's a reasonable USD value

  3. GET /public/get_order_book?instrument_name={selected}&depth=10
     → Assert: bids and asks are present; each level has [price, amount]
     → Verify: prices are in BTC (not USD)
     → Verify: amounts are in number of contracts

  4. GET /public/get_index_price?index_name=btc_usd
     → Assert: returns index_price as a float; value is reasonable
```

**What to learn:** Exact field names and types in responses, BTC-denominated pricing behavior, whether Greeks are always present or sometimes null (e.g., for deep OTM), tick_size and min_trade_amount values.

---

#### Test 3: Account Data — Positions, Margin, Wallet (`test_deribit_account.py`)

**What we're proving:** We can read account state and correctly map Deribit's fields to our normalized model.

**Critical detail:** Deribit's margin model and wallet format may differ significantly from Coincall. BTC-settled accounts report equity in BTC, not USD. USDC-margined accounts are different again. We need to understand exactly what we get.

```
Steps:
  1. GET /private/get_account_summary?currency=BTC
     → Log the FULL response (every field) — we need to see what's available
     → Assert: equity, available_funds, initial_margin, maintenance_margin are present
     → CRITICAL CHECK: what currency are these values in? (BTC for BTC-settled accounts)
     → Note: does Deribit report margin_balance, session_upl, session_rpl separately?
     → Note: is there a delta_total or portfolio-level Greeks field?

  2. GET /private/get_account_summary?currency=USDC  (if we plan to use USDC margin)
     → Compare field structure with BTC response — are they identical?
     → Note: currency of values changes

  3. GET /private/get_positions?currency=BTC&kind=option
     → If no positions exist: assert empty list (not an error)
     → If positions exist: log one fully, verify fields:
       instrument_name, size, direction ("buy"/"sell"), average_price,
       mark_price, floating_profit_loss, delta, gamma, vega, theta
     → CRITICAL CHECK: is size signed (negative = short) or unsigned with direction field?
     → CRITICAL CHECK: are position Greeks per-contract or total?

  4. GET /private/get_open_orders_by_currency?currency=BTC
     → Assert: returns a list (empty if no open orders)
     → Verify field structure: order_id, instrument_name, direction, price, amount,
       filled_amount, order_state, label, order_type
```

**What to learn:** Wallet currency denomination, margin field names and units, position representation (signed vs unsigned size), Greek granularity, empty-state behavior.

---

#### Test 4: Order Management — Full Round Trip (`test_deribit_orders.py`)

**What we're proving:** We can place, read, modify, cancel orders, and track an order through its lifecycle. This is the most critical test — if this works, we can trade.

**Run on testnet first. Then re-run on production with minimum size (0.1 BTC options contract).**

```
Test 4a: Place and Cancel (no fill)
  1. Pick a liquid option (ATM, ~7 DTE)
  2. Place a limit BUY order far below the market (e.g., best_bid * 0.5) with a unique label
     POST /private/buy {instrument_name, amount: 0.1, type: "limit", price: <far_below>, label: "test_001"}
     → Assert: response has order.order_id, order.order_state == "open"
     → Save order_id

  3. Read order status
     GET /private/get_order_state {order_id}
     → Assert: matches what we placed — same instrument, price, amount, direction, label
     → Note: is label echoed back exactly?

  4. Find our order in the open orders list
     GET /private/get_open_orders_by_currency {currency: "BTC"}
     → Assert: our order_id appears in the list
     → Verify: we can distinguish it from other orders by label or order_id

  5. Modify the order (change price)
     POST /private/edit {order_id, amount: 0.1, price: <slightly_different>}
     → Assert: success; verify new price in response
     → Note: does order_id change after edit? (on some exchanges it does!)

  6. Cancel the order
     POST /private/cancel {order_id}
     → Assert: order_state == "cancelled"

  7. Verify it's gone from open orders
     GET /private/get_open_orders_by_currency
     → Assert: our order_id no longer in list

Test 4b: Place and Fill — Full Position Lifecycle
  1. Pick a liquid option (ATM call, ~7 DTE)
  2. Place a limit BUY at best_ask (should fill immediately or very quickly)
     POST /private/buy {instrument_name, amount: 0.1, type: "limit", price: best_ask, label: "test_round_trip"}
     → Assert: order_state is "filled" or "open" (may take a moment)

  3. Poll order status until filled (max 30s, poll every 2s)
     → Track: how long did it take? Was there a partial fill stage?
     → Note: what does a partial fill look like? (order_state == "open", filled_amount > 0, filled_amount < amount)
     → Save: average_price from the fill

  4. Verify position exists
     GET /private/get_positions {currency: "BTC", kind: "option"}
     → Assert: our instrument appears with size == 0.1, direction == "buy"
     → Log: floating_profit_loss, delta, mark_price

  5. Close the position: place a SELL at best_bid
     POST /private/sell {instrument_name, amount: 0.1, type: "limit", price: best_bid, label: "test_close", reduce_only: true}
     → Poll until filled

  6. Verify position is gone (or size == 0)
     GET /private/get_positions
     → Assert: position no longer listed (or size == 0)

  7. Check trade history
     GET /private/get_user_trades_by_currency {currency: "BTC", count: 10}
     → Assert: our two trades (buy + sell) appear with correct instrument, amount, price
     → Note: is there a fee field? What's the fee structure?

Test 4c: Edge Cases
  1. Place an order with reduce_only=true when no position exists → expect rejection or specific error
  2. Place an order below minimum size (e.g., amount: 0.01) → expect rejection
  3. Place an order for an expired instrument → expect rejection
  4. Read the error response format for each → log the exact error structure
```

**What to learn:** Order lifecycle timing, partial fill representation, whether `order_id` survives edits, `label` round-tripping, fill price vs limit price, fee structure, `reduce_only` enforcement, error response format, trade history structure.

---

#### Test 5: Symbol Translation — Round Trip (`test_deribit_symbols.py`)

**What we're proving:** Our symbol parser correctly handles Deribit's instrument naming, and we can go from our internal `Instrument` model back to a valid Deribit symbol.

```
Steps:
  1. Fetch all active BTC options from /public/get_instruments
  2. For each instrument, parse instrument_name into Instrument(underlying, expiry, strike, option_type)
  3. Reconstruct the Deribit symbol from the parsed Instrument
  4. Assert: reconstructed == original for every instrument
  5. Edge cases to watch:
     - Strikes with decimals (does Deribit have them?)
     - Very short expiry names (e.g., daily expiries)
     - Perpetual instruments (BTC-PERPETUAL) — should be filtered out of option parsing
```

**What to learn:** Full range of Deribit symbol formats, any edge cases in date or strike formatting.

---

#### Test 6: Rate Limits & Error Handling (`test_deribit_resilience.py`)

**What we're proving:** We know what Deribit's rate limits look like in practice and our retry logic handles them.

```
Steps:
  1. Make 20 rapid /public/get_index_price calls in a tight loop (no delay)
     → Note: at what point (if any) does Deribit throttle? What does the response look like?
     → Is it HTTP 429? A JSON error with a specific code? A empty response?

  2. Make an authenticated call with an invalid/expired token
     → Log the exact error response — status code, error code, error message

  3. Make a call with a valid token but insufficient scope (e.g., call /private/buy without trade scope)
     → Log the exact error response

  4. Make a well-formed call to a non-existent instrument
     → Log the exact error response
```

**What to learn:** Rate limit thresholds and response format, error response structure, scope enforcement behavior.

---

### 9.3 Abstraction Layer Tests (Unit)

These are fast, offline tests that validate the exchange abstraction layer itself —  not Deribit's API. Run them on every code change.

```
tests/test_exchange_abstraction.py:
  - Coincall adapter still produces correct output with fixture data (regression)
  - Normalized data model serialization round-trips
  - Side encoding: "buy"/"sell" ↔ exchange-specific format (1/2 for Coincall, "buy"/"sell" for Deribit)
  - Symbol parser: Instrument ↔ exchange symbol for both exchanges
  - ExchangeConfig: correct RFQ thresholds per exchange
  - ExecutionRouter: routes to RFQ vs limit vs smart-orderbook correctly based on exchange + size
```

These are standard pytest unit tests with fixture data. They exist to catch regressions during refactoring, not to validate Deribit behavior.

### 9.4 Test Sequencing

The tests above map to migration phases:

| Phase | Tests to Run | Environment |
|-------|-------------|-------------|
| **Phase 1** (abstraction) | 9.3 abstraction layer tests + all existing Coincall tests | Offline / Coincall prod |
| **Phase 2** (Deribit impl) | Test 1 (auth) → Test 2 (market data) → Test 3 (account) → Test 5 (symbols) → Test 6 (resilience) → Test 4 (orders) | Deribit testnet |
| **Phase 2b** (validation) | Re-run Tests 1–6 on Deribit **production** with minimum sizes | Deribit production |
| **Phase 3** (cutover) | Test 4b round trip on production at real size; parallel read-only run (see 9.5) | Deribit production |

**Order matters in Phase 2.** Don't attempt order tests until auth, market data, and account tests pass. Each test builds on the confidence from the previous one.

### 9.5 Parallel Running (Confidence Building)

Before going live on Deribit, run the bot in **read-only mode**:
1. Fetch market data from Deribit
2. Run strategy entry conditions
3. Log *what would have been traded* without placing orders
4. Compare option selection and pricing with Coincall

This builds confidence that the Deribit implementation produces equivalent decisions. Run for at least 24–48 hours across different market conditions before enabling live trading.

---

## 10. Migration Phases & Sequencing

### Phase 0: Preparation (No Code Changes to Production)
- [ ] Create Deribit account + API keys (testnet + production)
- [ ] Study Deribit API documentation thoroughly
- [ ] Set up Deribit testnet environment
- [ ] Document all Deribit API endpoints we'll need
- [ ] Identify any Deribit-specific constraints (rate limits, minimum order sizes, tick sizes)

### Phase 1: Exchange Abstraction Layer ✅ COMPLETE (v1.3.0-wip, 16 March 2026)
**Goal:** Introduce interfaces without changing behavior. The system still runs on Coincall.

- [x] Define abstract interfaces: `ExchangeAuth`, `ExchangeMarketData`, `ExchangeExecutor`, `ExchangeAccountManager`, `ExchangeRFQExecutor`
- [ ] Define normalized data models: `Instrument`, `OptionTicker`, `Orderbook`, `OrderResult`, `AccountSummary`, `Position`, `RFQLeg`, `RFQHandle`, `RFQQuoteSnapshot`, `RFQTradeResult` *(deferred — Phase 2, when Deribit adapters need them)*
- [x] Wrap existing Coincall code behind these interfaces (move into `exchanges/coincall/`)
- [ ] Extract RFQ orchestration logic from `rfq.py` into shared layer; move Coincall API calls into `exchanges/coincall/rfq.py` *(deferred — Phase 2, current RFQ adapter wraps existing rfq.py)*
- [x] Migrate `TradeLeg.side` from int to string (`"buy"` / `"sell"` everywhere, adapter converts at API boundary)
- [x] Update `TradingContext` to reference abstract types
- [x] Update `build_context()` to use exchange factory
- [x] Parameterize `_EXCHANGE_STATE_MAP` in `OrderManager`
- [ ] Parameterize RFQ minimum thresholds in `ExecutionRouter` (from exchange config) *(deferred — Phase 2)*
- [x] Run all existing tests — everything passes (379 total: 67+23+85+71+40+34+49+10 across 8 test suites)
- [ ] Deploy to production on Coincall — verify no regressions

**Implementation notes:**
- `exchanges/base.py` — 5 ABCs defining the exchange contract
- `exchanges/__init__.py` — `build_exchange(name)` factory (supports "coincall"; raises for "deribit")
- `exchanges/coincall/` — 5 thin adapter classes wrapping existing modules
- `CoincallExecutorAdapter.place_order()` converts `"buy"→1, "sell"→2` at the API boundary
- `TradeExecutor.place_order(side: int)` is **unchanged** — adapters handle translation
- Backward compat: `TradeLeg.__post_init__` and `OrderRecord.from_dict()` auto-convert legacy int sides from crash-recovery snapshots
- `config.py` — `EXCHANGE = os.getenv('EXCHANGE', 'coincall')` with validation
- All documentation updated (MODULE_REFERENCE.md, .copilot-instructions.md)

**Directory structure after Phase 1:**
```
exchanges/
  __init__.py            # build_exchange(name) factory
  base.py                # 5 Abstract interfaces
  coincall/
    __init__.py          # COINCALL_STATE_MAP + build_coincall()
    auth.py              # CoincallAuthAdapter
    market_data.py       # CoincallMarketDataAdapter
    executor.py          # CoincallExecutorAdapter ("buy"→1, "sell"→2)
    account.py           # CoincallAccountAdapter
    rfq.py               # CoincallRFQAdapter
  deribit/               # (Phase 2)
    __init__.py
```

### Phase 2: Deribit Implementation
**Goal:** Implement all Deribit exchange adapters including Block RFQ. Test on testnet.

- [ ] Implement `DeribitAuth` with OAuth2 token lifecycle
- [ ] Implement `DeribitMarketData` (REST, poll-based — matching current architecture)
- [ ] Implement `DeribitExecutor` (REST order placement)
- [ ] Implement `DeribitAccountManager` (account + position queries)
- [ ] Implement `DeribitRFQExecutor` (Block RFQ via REST JSON-RPC)
  - [ ] `create_block_rfq` with leg translation and ratio handling
  - [ ] `get_block_rfqs` polling for quote snapshots
  - [ ] `accept_block_rfq` with ratio echo and price-based crossing
  - [ ] `cancel_block_rfq`
  - [ ] Handle multi-maker fill results (one RFQ → multiple block trades)
- [ ] Implement Deribit symbol parser
- [ ] Ensure API keys have `block_rfq:read` + `block_rfq_id:read_write` scopes
- [ ] Write Deribit-specific unit tests (mock API responses, including Block RFQ lifecycle)
- [ ] Write Deribit integration tests (hit testnet)
- [ ] Update `ExecutionRouter` with Deribit RFQ minimum threshold (25 BTC)
- [ ] Run in read-only mode on Deribit testnet (strategies evaluate but don't trade)
- [ ] Validate option selection produces sensible results on Deribit instruments
- [ ] Test Block RFQ on testnet (create, receive quotes, cross) — requires testnet MMs

### Phase 3: Production Cutover
**Goal:** Go live on Deribit.

- [ ] Paper trade on Deribit testnet for at least 1 full week
- [ ] Verify: order placement, fills, cancellation, requoting, position closing
- [ ] Verify: account snapshot accuracy (equity, margin, Greeks)
- [ ] Verify: kill switch works (position_closer)
- [ ] Verify: crash recovery works (snapshot reload → reconcile)
- [ ] Switch `.env` to `EXCHANGE=deribit` with production credentials
- [ ] Start with reduced position size (50% of normal)
- [ ] Monitor for 48h; compare execution quality with Coincall logs
- [ ] Scale to full position size

### Phase 4: Optimization (Post-Migration)
**Goal:** Take advantage of Deribit-specific features.

- [ ] WebSocket market data subscriptions (replace REST polling)
- [ ] WebSocket order placement (lower latency requoting)
- [ ] WebSocket Block RFQ updates via `block_rfq.taker.{currency}` subscription (replace REST polling of quotes)
- [ ] Combo instrument support (atomic multi-leg execution)
- [ ] Block RFQ trigger orders (`good_til_cancelled`) for passive phased execution
- [ ] Block RFQ hedge legs (attach perpetual/future delta hedge to option structures)
- [ ] Portfolio margin optimization (strategy adjustments for better capital efficiency)
- [ ] Real-time fill notifications via WebSocket (replace poll-based fill tracking)

---

## 11. Risk Register

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Deribit API downtime during migration** | Medium | Testnet first; keep Coincall path functional throughout |
| **Subtle data format differences cause wrong trades** | High | Extensive unit tests with real API response fixtures; read-only validation period |
| **Token refresh failure causes auth cascade** | Medium | Implement proactive refresh (refresh at 80% TTL); fallback to re-auth from scratch |
| **Rate limiting on Deribit** | Medium | Respect documented limits; add backoff; consider WebSocket early |
| **Different margin calculation leads to unexpected liquidations** | High | Compare margin requirements side-by-side before going live; start with conservative sizing |
| **Greeks calculation differences between exchanges** | Low | Both use Black-Scholes; verify delta/gamma/theta match within tolerance |
| **Orderbook execution quality worse than expected** | Low | Deribit is more liquid; but validate with small trades first |
| **Deribit Block RFQ minimum too large for current strategies** | Medium | 25 BTC minimum (~$2.1M) exceeds typical trade sizes; router falls back to limit orders; plan to scale into RFQ as AUM grows |
| **Block RFQ taker rating degradation** | Low | Only create RFQs we intend to trade on; monitor OTV ratio; avoid price fishing |
| **Multi-maker fill produces multiple block trades** | Medium | Ensure trade tracking handles one RFQ → N block trades; reconcile via `block_rfq_id` |
| **Deribit testnet lacks Block RFQ MMs** | Medium | May need to test with disclosed identity targeting specific test accounts; or test on production with minimum size |
| **Regression in Coincall path during abstraction** | Medium | Phase 1 is a pure refactor — all existing tests must pass before proceeding |

---

## 12. Open Questions & Decisions Required

1. **Dual-exchange support?** Do we want to run on both exchanges simultaneously (e.g., different strategies on different exchanges)? This is architecturally supported by the abstraction layer but adds operational complexity. Recommend: **no** — clean cutover is simpler.

2. **WebSocket timeline?** Should Phase 2 start with WebSocket (more work, better latency) or REST (faster to implement, matches current architecture)? Recommend: **REST first** — get functional parity, then optimize.

3. **Combo instruments?** Should multi-leg strategies use Deribit combo instruments from day one? Recommend: **no** — use individual limit orders first (proven approach), add combo support in Phase 4.

4. **Settlement currency?** Deribit offers BTC-margined and USDC-margined options. Which do we use? BTC-margined is more liquid but introduces BTC exposure on collateral. USDC-margined is simpler from a P&L perspective. Recommend: **decide based on current trading approach**.

5. **Minimum order size?** Deribit minimum is 0.1 BTC option contracts (vs Coincall's minimum which may differ). Verify strategy position sizes meet Deribit minimums.

6. **Archive Coincall code?** After successful migration, do we archive the Coincall adapter or keep it maintained? Recommend: **keep in `exchanges/coincall/` but stop maintaining** — it's useful as a reference and costs nothing to keep.

7. **Block RFQ minimum size vs current strategies?** Deribit requires 25 BTC contracts minimum for Block RFQ. At ~$84k/BTC, this is ~$2.1M notional. Our current strategies typically trade $10k–$200k. Do we (a) scale up position sizes on Deribit to use Block RFQ, (b) accept that Block RFQ is not available at current sizes and use limit orders, or (c) plan a gradual ramp where Block RFQ unlocks at larger AUM? Recommend: **(b) for now, (c) as we grow** — the abstraction is ready when we need it.

8. **Block RFQ anonymous vs disclosed?** Anonymous RFQs require targeting ≥5 MMs. Disclosed RFQs can target 1 MM but reveal our identity. For maximizing competition, anonymous is better. But if we have a preferred MM relationship, disclosed enables targeted quoting. Recommend: **anonymous by default** — maximum competition, and we have no established MM relationships on Deribit yet.

9. **Phased RFQ execution on Deribit — poll or trigger?** Our current `rfq_phased` logic polls quotes and manually decides when to accept. Deribit's `good_til_cancelled` trigger orders automate this — place a crossing order at desired price, update it over time. Should we use triggers from Phase 2 or stick with polling for parity? Recommend: **polling in Phase 2** (simpler, matches Coincall behavior), **trigger orders in Phase 4** (better execution, less API traffic).

---

## 13. Deribit API Field Reference (Test Findings)

> **Purpose:** Concrete, verified field-level reference for the AI agent building the abstraction layer. All data from live tests against Deribit testnet and production (March 2026). Test scripts in `tests/deribit/`.

---

### 13.1 Authentication & Token Lifecycle

**Endpoint:** `POST /api/v2/public/auth` (JSON-RPC 2.0 body)

**Grant types tested:**
- `client_credentials` — initial auth; requires `client_id` + `client_secret`
- `refresh_token` — uses `refresh_token` from a previous auth response

**Auth response fields:**
```
access_token        string    Bearer token for Authorization header
refresh_token       string    Single-use token for refresh grant
expires_in          int       Token TTL in seconds (observed: 900 on both envs)
token_type          string    Always "bearer"
scope               string    Space-separated scopes (e.g., "account:read trade:read_write block_rfq:read_write ...")
```

**Verified behaviors:**
- Token TTL: **900 seconds** (15 minutes) on both testnet and production
- **Refresh invalidates the old token immediately.** After a `refresh_token` grant, the previous `access_token` returns error code `13009` (`unauthorized`, reason: `invalid_token`). This means: refresh MUST be atomic — swap old→new in one step, never let a request use the old token after refresh.
- **Refresh token is also single-use.** A new `refresh_token` is issued with each refresh; the old one is consumed.
- Both `access_token` and `refresh_token` change on every refresh (verified: `Token changed? YES`, `Refresh token changed? YES`).
- Using the API: `Authorization: Bearer <access_token>` header on all private endpoints.

**Scopes confirmed on our keys (both envs):**
`account:read`, `trade:read_write`, `wallet:read`, `block_rfq:read_write`, `block_rfq_id:read_write`, and more. Full scope confirmed via auth response.

---

### 13.2 Error Response Format

All Deribit errors come back as **HTTP 400** with a JSON-RPC error body. There are no 401, 403, or 429 HTTP status codes — you must inspect the JSON error code.

**Standard error shape:**
```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": <int>,
    "message": "<string>",
    "data": {
      "reason": "<string>",
      "param": "<string>"      // optional, present for param-specific errors
    }
  },
  "testnet": true/false
}
```

**Observed error codes:**

| Code | Message | When | `data.reason` |
|------|---------|------|---------------|
| `13009` | `unauthorized` | Invalid/expired/no token | `invalid_token` |
| `-32601` | `Method not found` | Non-existent API method | — |
| `-32602` | `Invalid params` | Bad parameter value | `instrument not found`, `must conform to tick size`, `must be a multiple of the minimum order size` |
| `11030` | `other_reject invalid_reduce_only_order` | `reduce_only=true` with no position | — |

**Key for abstraction:** Error detection must check `"error" in response_json`, NOT HTTP status code. All errors are HTTP 400.

---

### 13.3 Rate Limits

**Observed (from account summary `limits` field):**

| Category | Rate | Burst |
|----------|------|-------|
| Non-matching engine (reads) | 20/s | 100 |
| Trading (orders) | 5/s | 20 |
| Cancel all | 5/s | 20 |
| Block RFQ maker | 10/s | 20 |
| Spot | 5/s | 20 |

**Rapid-fire test results:**
- 25 calls in 1.8s (testnet) and 1.87s (production) — **zero throttling** observed.
- Latency: 60–116ms per call (testnet), 61–86ms (production).
- No HTTP 429 responses; Deribit's throttling mechanism (when it triggers) returns a JSON-RPC error, not an HTTP status code.

**Practical implication:** Our 10s polling interval is very conservative. Even at 1 call/70ms we'd be within limits. But stay well below trading burst (20) for order operations.

---

### 13.4 Index Price

**Official BTC/USD composite index:**
```
GET /api/v2/public/get_index_price?index_name=btc_usd
```

**This is the correct endpoint for the BTC spot index.** It returns Deribit's official composite index price (weighted across multiple spot exchanges), NOT the perpetual futures price. The perpetual (`BTC-PERPETUAL`) trades at a different price due to funding rate premium/discount.

**Response:**
```json
{
  "index_price": 73843.48,
  "estimated_delivery_price": 73843.48
}
```

| Index Name | Asset |
|------------|-------|
| `btc_usd` | Bitcoin |
| `eth_usd` | Ethereum |

**Where index appears elsewhere:**
- Ticker response: `index_price` field
- Position data: `index_price` field
- Trade data: `index_price` field
- Account summary: NOT included (must fetch separately)

**NOTE:** `underlying_price` in ticker is a float (e.g., `73678.09`) — this is the forward price (slightly different from spot index). `underlying_index` in the orderbook returns a *string* like `"BTC-20MAR26"` (the futures delivery name), NOT a number.

---

### 13.5 Market Data — Instruments

**Endpoint:** `GET /api/v2/public/get_instruments?currency=BTC&kind=option&expired=false`

**Instrument count (March 2026):** 1134 testnet, 918 production. 11 unique expiry dates, 95–122 unique strikes.

**Full field list (per instrument):**
```
instrument_name          "BTC-20MAR26-74000-C"
instrument_id            int
strike                   float        (always integer values, no decimals observed)
expiration_timestamp     int (ms)     Unix millis; settlement at 08:00 UTC on expiry date
option_type              "call" | "put"
is_active                bool
kind                     "option"
instrument_type          "reversed"   (BTC-margined options are "reversed" contracts)
contract_size            1.0          (1 option = 1 BTC notional)
min_trade_amount         0.1          (minimum order size in contracts)
tick_size                0.0001       (base tick — but see tick_size_steps below)
tick_size_steps           [{"above_price": 0.005, "tick_size": 0.0005}]
maker_commission         0.0003       (0.03%)
taker_commission         0.0003       (0.03%)
base_currency            "BTC"
counter_currency         "USD"
quote_currency           "BTC"
settlement_currency      "BTC"
settlement_period        "week" | "month" | "quarter"
creation_timestamp       int (ms)
state                    "open"
price_index              "btc_usd"
block_trade_min_trade_amount   25     (25 BTC for block/RFQ trades)
block_trade_tick_size    0.0001
```

**Tick size rules (critical for order placement):**
```
Price < 0.005 BTC  →  tick = 0.0001
Price >= 0.005 BTC →  tick = 0.0005
```
This is encoded in the `tick_size_steps` field. **Orders at invalid tick sizes are rejected** with code `-32602` ("must conform to tick size").

**Symbol format:** `{UNDERLYING}-{D}[D]{MMM}{YY}-{STRIKE}-{C|P}`
- Day can be 1 or 2 digits: `3APR26` or `20MAR26`
- Strike: always integer (no decimal points observed in 2052 instruments across both envs)
- Regex: `^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-([CP])$`
- **Round-trip verified:** parse→reconstruct matches 100% for all 1134 testnet and 918 production instruments.

**Active futures (non-option, for filtering):**
```
BTC-20MAR26, BTC-27MAR26, BTC-24APR26, BTC-29MAY26,
BTC-26JUN26, BTC-25SEP26, BTC-25DEC26, BTC-PERPETUAL
```
Our option parser correctly rejects all of these.

---

### 13.6 Market Data — Ticker

**Endpoint:** `GET /api/v2/public/ticker?instrument_name=BTC-20MAR26-74000-C`

**All prices are in BTC** (e.g., `mark_price: 0.021` = $1,551 USD at $73,861 underlying).

**Full field list:**
```
mark_price               float     Option price in BTC
best_bid_price           float     Best bid in BTC (can be 0 if no bids)
best_ask_price           float     Best ask in BTC (can be 0 if no asks)
best_bid_amount          float     Size at best bid (in contracts)
best_ask_amount          float     Size at best ask (in contracts)
last_price               float     Last traded price in BTC
index_price              float     Current spot index ($)
underlying_price         float     Forward price ($) — slightly different from index
underlying_index         string    Forward reference (e.g., "SYN.BTC-20MAR26")
mark_iv                  float     Mark implied volatility (%)
bid_iv                   float     Bid implied volatility (%)
ask_iv                   float     Ask implied volatility (%)
interest_rate            float     Risk-free interest rate used
open_interest            float     Open interest (in contracts)
volume                   float     24h volume (in contracts)
settlement_price         float     Previous settlement price
estimated_delivery_price float     Est. delivery price at settlement
min_price                float     Minimum allowed order price
max_price                float     Maximum allowed order price
state                    string    "open" | "closed"
timestamp                int (ms)  Server timestamp

greeks:
  delta                  float     Option delta (per contract)
  gamma                  float     Option gamma
  vega                   float     Option vega
  theta                  float     Option theta
  rho                    float     Option rho

stats:
  high                   float     24h high
  low                    float     24h low
  price_change           float     24h price change (%)
  volume                 float     24h volume
  volume_usd             float     24h volume in USD
```

**Greeks are ALWAYS populated** — even for deep OTM options (tested: strike 105000 with delta 0.02142). No null/None values observed.

---

### 13.7 Market Data — Orderbook

**Endpoint:** `GET /api/v2/public/get_order_book?instrument_name=...&depth=10`

**Response includes the full ticker data PLUS:**
```
bids      [[price, amount], [price, amount], ...]     Descending price
asks      [[price, amount], [price, amount], ...]     Ascending price
change_id int                                          Sequence number for WS sync
```

**Note:** `underlying_index` in the orderbook response returns a string like `"BTC-20MAR26"` (the futures reference), not a float. This is different from the ticker's `underlying_price` which is a float.

---

### 13.8 Account Summary

**Endpoint:** `POST /api/v2/private/get_account_summary` with `{currency: "BTC"}` or `{currency: "USDC"}`

**BTC and USDC accounts have identical field structures** (verified: zero fields only in BTC, zero fields only in USDC).

**Full field list (47 fields):**
```
equity                           float     Total equity in account currency
balance                          float     Cash balance (deposits - withdrawals + realized PnL)
available_funds                  float     Funds available for new orders
available_withdrawal_funds       float     Max withdrawable amount
initial_margin                   float     Currently used initial margin
maintenance_margin               float     Currently used maintenance margin
margin_balance                   float     Balance used for margin calculations
projected_initial_margin         float     IM including projected moves
projected_maintenance_margin     float     MM including projected moves
projected_close_out_margin       float     
close_out_margin                 float     
currency                         string    "BTC" or "USDC"
margin_model                     string    "cross_pm" (cross portfolio margin)
portfolio_margining_enabled      bool      true
cross_collateral_enabled         bool      true

session_upl                      float     Unrealized PnL this session
session_rpl                      float     Realized PnL this session
total_pl                         float     Total PnL
options_pl                       float     PnL from options
futures_pl                       float     PnL from futures
futures_session_rpl              float
futures_session_upl              float
options_session_rpl              float
options_session_upl              float

delta_total                      float     Portfolio delta
delta_total_map                  dict
options_delta                    float
options_gamma                    float     Portfolio gamma
options_gamma_map                dict
options_vega                     float     Portfolio vega
options_vega_map                 dict
options_theta                    float     Portfolio theta
options_theta_map                dict
options_value                    float     Total mark-to-market value of options
projected_delta_total            float

spot_reserve                     float
locked_balance                   float
fee_balance                      float
additional_reserve               float
disable_kyc_verification         bool

total_equity_usd                 float     Equity converted to USD
total_initial_margin_usd         float     IM in USD
total_maintenance_margin_usd     float     MM in USD
total_margin_balance_usd         float     Margin balance in USD
total_delta_total_usd            float     Delta in USD terms

limits                           dict      Rate limit info (see 13.3)
change_margin_model_api_limit    dict      {timeframe, rate}
```

**Key comparison with Coincall:**

| Concept | Coincall | Deribit |
|---------|----------|---------|
| Equity | `equity` | `equity` |
| Available margin | `availableMargin` | `available_funds` |
| Initial margin | `imAmount` | `initial_margin` |
| Maintenance margin | `mmAmount` | `maintenance_margin` |
| Unrealized PnL | `upnl` | `session_upl` |
| Realized PnL | `rpnl` | `session_rpl` |
| Currency | Always USD-denominated | Account currency (BTC or USDC) |

**Critical difference:** Deribit reports margin and equity **in account currency** (BTC or USDC), not USD. Use `total_equity_usd` etc. for cross-currency USD comparisons.

---

### 13.9 Positions

**Endpoint:** `POST /api/v2/private/get_positions` with `{currency: "BTC", kind: "option"}`

**Position field list (19 fields):**
```
instrument_name          string     "BTC-20MAR26-74000-C"
kind                     string     "option"
size                     float      Contract count (UNSIGNED — always >= 0)
direction                string     "buy" | "sell" | "zero" (for closed/zero-size positions)
average_price            float      Average entry price in BTC
average_price_usd        float      Average entry price in USD
mark_price               float      Current mark price in BTC
index_price              float      Current BTC index price ($)
settlement_price         float
initial_margin           float      IM for this position (0 for long options)
maintenance_margin       float      MM for this position (0 for long options)
floating_profit_loss     float      Unrealized PnL in BTC
floating_profit_loss_usd float      Unrealized PnL in USD
realized_profit_loss     float      Realized PnL in BTC
total_profit_loss        float      Total PnL in BTC
delta                    float      Position delta (total, not per-contract)
gamma                    float      Position gamma (total)
vega                     float      Position vega (total)
theta                    float      Position theta (total)
```

**CRITICAL: Size is UNSIGNED with separate direction field.**
- Coincall uses signed size (negative = short).
- Deribit uses unsigned `size` + `direction` ("buy"/"sell"/"zero").
- The abstraction layer MUST normalize this. A Deribit `{size: 0.1, direction: "sell"}` = Coincall `{qty: -0.1}`.

**Position Greeks are TOTAL, not per-contract.** A 0.1 contract position at delta 0.4974/contract shows `delta: 0.04974`.

**Long options have 0 margin.** Observed: `initial_margin: 0.0`, `maintenance_margin: 0.0` for a long call. Short option positions would show non-zero margin.

**Closed positions:** Deribit can return positions with `size: 0, direction: "zero"` — these are historical entries, not active. Filter by `size > 0` for active positions.

---

### 13.10 Orders

**Placement:** `POST /api/v2/private/buy` or `/private/sell` (separate endpoints per side)

**Request parameters:**
```json
{
  "instrument_name": "BTC-20MAR26-74000-C",
  "amount": 0.1,
  "type": "limit",
  "price": 0.021,
  "label": "my_strategy_001",
  "reduce_only": false
}
```

**Response shape:** `{order: {...}, trades: [...]}`
- `trades` is populated if the order fills immediately (even partially).
- For a limit order below market, `trades` is empty `[]`.

**Order field list (23 fields):**
```
order_id                 string     "88058021064" (numeric string)
order_state              string     "open" | "filled" | "cancelled" | "rejected" | "untriggered"
order_type               string     "limit" | "market" | "stop_limit" | ...
instrument_name          string
direction                string     "buy" | "sell"
price                    float      Limit price in BTC
amount                   float      Total order size (contracts)
filled_amount            float      How much has been filled so far
contracts                float      Same as amount (echoed)
average_price            float      Avg fill price (0 if unfilled)
label                    string     Round-trips perfectly — visible in order, open-order list, AND trade history
time_in_force            string     "good_til_cancelled" (default)
creation_timestamp       int (ms)
last_update_timestamp    int (ms)
post_only                bool
reduce_only              bool
replaced                 bool       Set to true after an edit (order_id stays the same!)
cancel_reason            string     Present only on cancelled orders. Observed: "user_request"
api                      bool       true if order was placed via API (vs web UI)
web                      bool       true if order was placed via web UI
mmp                      bool       Market maker protection flag
is_liquidation           bool
risk_reducing            bool
user_id                  int
```

**Verified behaviors:**
- **`order_id` does NOT change after edit.** The `replaced` flag goes from `false` to `true`, but the ID stays the same. This simplifies order tracking vs exchanges where edits create new IDs.
- **`label` round-trips through all endpoints:** order placement response, `get_order_state`, `get_open_orders_by_currency`, and `get_user_trades_by_currency`. Max 64 chars.
- **Immediate fill:** Placing a buy at `best_ask` filled immediately — `order_state: "filled"`, `trades: [1 trade]` in the same response.
- **Near-fill latency:** Closing a position at `best_bid` went to `order_state: "open"` initially, then `"filled"` on the first 2s poll.

**Order state transitions (observed):**
```
Place far below market  →  "open"
Edit price              →  "open" (replaced=true)
Cancel                  →  "cancelled" (cancel_reason="user_request")
Place at best_ask       →  "filled" (immediate)
Place at best_bid       →  "open" → poll → "filled"
```

---

### 13.11 Trade History

**Endpoint:** `POST /api/v2/private/get_user_trades_by_currency` with `{currency: "BTC", count: N}`

**Response shape:** `{trades: [...], has_more: bool}` — paginated.

**Trade field list (28 fields):**
```
trade_id                 string     "225388371"
order_id                 string     Links to the originating order
instrument_name          string
direction                string     "buy" | "sell"
price                    float      Execution price in BTC
amount                   float      Filled amount (contracts)
contracts                float      Same as amount
fee                      float      Fee in BTC
fee_currency             string     "BTC"
iv                       float      Implied volatility at execution
mark_price               float      Mark price at time of trade
index_price              float      Index price at time of trade
underlying_price         float      Underlying forward price at time of trade
profit_loss              float      PnL for this trade (0 for opens, nonzero for closes)
state                    string     "filled"
timestamp                int (ms)
trade_seq                int        Sequential trade number
matching_id              string|null
order_type               string     "limit" | "market" | ...
label                    string     From the originating order (confirmed: visible in trade history!)
post_only                bool
reduce_only              bool
risk_reducing            bool
mmp                      bool
self_trade               bool
api                      bool       true if order was placed via API
tick_direction            int        Price movement indicator
liquidity                string     "M" (maker) | "T" (taker)
user_id                  int
```

---

### 13.12 Fee Structure

**Observed fees (our account — both testnet and production):**

| | Rate | Example |
|-|------|---------|
| **Maker** | 0.03% (0.0003 per 1.0 BTC contract) | 0.1 contracts → fee 0.00003 BTC |
| **Taker** | 0.03% (0.0003 per 1.0 BTC contract) | 0.1 contracts → fee 0.00003 BTC |

**Maker and taker fees are identical on our account.** This may vary for other fee tiers.

The `liquidity` field in trade history indicates `"M"` (maker) or `"T"` (taker).

**Fee in instrument metadata:** `maker_commission: 0.0003`, `taker_commission: 0.0003` — these match the observed fees.

**Note:** In Test 4, buying at `best_ask` (crossing the spread) is a **taker** trade, and selling at `best_bid` (crossing the spread) is also a **taker** trade. Both showed fee = 0.00003 BTC (0.03%). The `liquidity` field would show `"T"` for these. The original Test 4 analysis incorrectly labeled these as "maker fee" — corrected here.

---

### 13.13 Advanced Order Pricing Modes

**Not yet tested — documented for future reference.**

Deribit supports three pricing modes when placing orders:

| Mode | `type` suffix | How it works |
|------|---------------|-------------|
| **BTC price** | `"limit"` (default) | Price in BTC. This is what we tested. `price: 0.021` means 0.021 BTC. |
| **USD price** | `"limit"` with `advanced: "usd"` | Price in USD. Deribit dynamically converts to BTC using the current index. Useful for strategies that think in dollar terms. |
| **IV price** | `"limit"` with `advanced: "implv"` | Price in implied volatility. `price: 55.0` means 55% IV. Deribit dynamically computes the BTC price from the IV using its own pricing model. |

**Example (IV order):**
```json
{
  "instrument_name": "BTC-20MAR26-74000-C",
  "amount": 0.1,
  "type": "limit",
  "price": 55.0,
  "advanced": "implv"
}
```

**Why this matters:**
- USD pricing removes the need to manually convert dollar targets to BTC prices. If a strategy says "I want to sell this call for $1,500", we can send `price: 1500, advanced: "usd"` instead of computing `1500 / index_price`.
- IV pricing enables volatility-based strategies — "sell at 60% IV" without computing option prices ourselves. The exchange handles the Black-Scholes conversion in real time.
- Both modes dynamically adjust the effective BTC price over time as spot moves, which is powerful for GTC orders.

**Decision:** Defer testing until a strategy needs it. Current strategies express prices in BTC or dollar terms that we convert ourselves. IV pricing may be valuable for volatility-targeting strategies in Phase 4.

---

### 13.14 Key Differences Summary (Deribit vs Coincall — Quick Reference)

| Aspect | Coincall | Deribit | Abstraction Impact |
|--------|----------|---------|-------------------|
| Auth | HMAC sign per request | Bearer token (900s TTL + refresh) | Need token lifecycle manager |
| Side encoding | `1`=buy, `2`=sell (ints) | Separate `/buy` and `/sell` endpoints | Router normalizes |
| Position size | Signed (negative = short) | Unsigned + `direction` field | **Normalize in adapter** |
| Order status | Numeric (`0`=new, `1`=filled, …) | String (`"open"`, `"filled"`, …) | Map in adapter |
| Client order ID | `clientOrderId` | `label` (max 64 chars) | Rename in adapter |
| Price currency | USD | BTC | **Convert in adapter** |
| Index price | Embedded in futures ticker | Dedicated `get_index_price` | Separate call |
| Error format | HTTP status codes | Always HTTP 400 + JSON error code | Check JSON, not HTTP |
| order_id on edit | May change (unknown) | **Does NOT change** (`replaced=true`) | Simpler tracking |
| Greeks in ticker | Separate call | Bundled in ticker response | Fewer API calls |
| Position Greeks | Per-contract (?) | **Total** (size × per-contract) | Normalize to per-contract if needed |
| Trade fee field | ? | `fee` + `fee_currency` per trade | Direct mapping |
| Tick sizes | Uniform (?) | Variable by price level (`tick_size_steps`) | Must consult before placing |
| Min order size | Varies | 0.1 contracts for all BTC options | Hardcode or read from instrument |
| RFQ minimum | $50k notional | 25 BTC contracts (~$1.8M) | Threshold in exchange config |

---

*End of migration plan. This document should be reviewed, discussed, and refined before any implementation begins.*
