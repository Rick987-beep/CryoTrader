# Coincall Exchange API Reference

**Official Documentation:** https://docs.coincall.com/  
**Last Updated:** March 3, 2026

Reference for the Coincall exchange REST and WebSocket APIs.
For CoincallTrader internal module documentation, see [MODULE_REFERENCE.md](MODULE_REFERENCE.md).

---

## Authentication

All private endpoints require these headers:

| Header | Description |
|--------|-------------|
| `X-CC-APIKEY` | Your API key |
| `sign` | HMAC-SHA256 signature |
| `ts` | Current timestamp (milliseconds) |
| `X-REQ-TS-DIFF` | Request timestamp tolerance (optional) |

### Signature Algorithm
```
sign = HMAC-SHA256(apiSecret, method + uri + "?" + sortedQueryParams)
```

For POST with JSON body, include body params in query string for signing.

---

## Base URLs

| Environment | URL |
|-------------|-----|
| Production | `https://api.coincall.com` |
| Testnet | `https://betaapi.coincall.com` |

---

## Options Trading

### Get Option Instruments
```
GET /open/option/getInstruments/{baseCurrency}
```
Returns all available options for a currency (BTC, ETH, etc.)

**Response fields:**
| Field | Type | Description |
|-------|------|-------------|
| `symbolName` | `string` | Full option name (e.g., `BTCUSD-14SEP23-22500-C`) |
| `strike` | `number` | Strike price |
| `expirationTimestamp` | `number` | Expiry time in milliseconds |
| `isActive` | `boolean` | Whether tradeable |
| `minQty` | `number` | Minimum order quantity |
| `tickSize` | `number` | Tick size |

### Get Option Chain
```
GET /open/option/get/v1/{index}?endTime={endTime}
```
Returns full option chain with Greeks, IV, orderbook summary.

### Get Option Details
```
GET /open/option/detail/v1/{symbol}
```
Returns single option details including Greeks.

### Get Option OrderBook
```
GET /open/option/order/orderbook/v1/{symbol}
```
Returns 100-depth orderbook.

### Place Option Order
```
POST /open/option/order/create/v1
```
**Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | `string` | Yes | Option symbol |
| `tradeSide` | `number` | Yes | 1=BUY, 2=SELL |
| `tradeType` | `number` | Yes | 1=LIMIT, 3=POST_ONLY |
| `qty` | `number` | Yes | Quantity |
| `price` | `number` | Limit only | Price |
| `timeInForce` | `string` | No | IOC, GTC, FOK |
| `reduceOnly` | `number` | No | 1=reduce only |
| `mmp` | `boolean` | No | Market maker protection |

**Rate Limit:** 60/s

### Batch Create Orders
```
POST /open/option/order/batchCreate/v1
```
Up to 40 orders per request.

### Cancel Order
```
POST /open/option/order/cancel/v1
```
By `orderId` or `clientOrderId`.

**Important:** `orderId` must be sent as an integer, not a string.

### Get Order Status
```
GET /open/option/order/singleQuery/v1?orderId={orderId}
```

**Important:** The path-based endpoint (`/open/option/order/{id}/v1`) returns 404 — use the query-parameter version above.

**Response:**
| Field | Type | Description |
|-------|------|-------------|
| `orderId` | `number` | Order ID |
| `symbol` | `string` | Option symbol |
| `qty` | `number` | Ordered quantity |
| `fillQty` | `number` | Filled quantity (not `executedQty`) |
| `remainQty` | `number` | Remaining quantity |
| `price` | `number` | Order price |
| `avgPrice` | `number` | Average fill price |
| `state` | `number` | Order state (see below) |

### Order States
| State | Meaning |
|-------|---------|
| 0 | NEW |
| 1 | FILLED |
| 2 | PARTIALLY_FILLED |
| 3 | CANCELED |
| 4 | PRE_CANCEL |
| 5 | CANCELING |
| 6 | INVALID |
| 10 | CANCEL_BY_EXERCISE |

### Get Positions
```
GET /open/option/position/get/v1
```
Returns all open option positions with Greeks and P&L.

**Response (array of positions):**
| Field | Type | Description |
|-------|------|-------------|
| `positionId` | `string` | Unique position ID |
| `symbol` | `string` | Option symbol (e.g., `BTCUSD-13FEB26-80000-C`) |
| `displayName` | `string` | Human-readable name |
| `qty` | `number` | Position size |
| `avgPrice` | `number` | Average entry price |
| `markPrice` | `number` | Current mark price |
| `upnl` | `number` | Unrealised P&L (USD, based on last trade price) |
| `upnlByMarkPrice` | `number` | Unrealised P&L (USD, based on mark price — more accurate for options) |
| `roi` | `number` | Return on investment (ratio, based on last trade) |
| `roiByMarkPrice` | `number` | Return on investment (ratio, based on mark price) |
| `tradeSide` | `number` | 1=BUY (long), 2=SELL (short) |
| `delta` | `number` | Position delta |
| `gamma` | `number` | Position gamma |
| `vega` | `number` | Position vega |
| `theta` | `number` | Position theta |

---

## RFQ (Block Trades)

**Requirements:**
- Minimum notional: $50,000 (sum of strike values × quantity)
- Accept and Cancel endpoints require `application/x-www-form-urlencoded` content type

### Create RFQ Request (Taker)
```
POST /open/option/blocktrade/request/create/v1
Content-Type: application/json
```
**Body:**
```json
{
  "legs": [
    {"instrumentName": "BTCUSD-29OCT25-109000-C", "side": "BUY", "qty": "1"},
    {"instrumentName": "BTCUSD-29OCT25-90000-P", "side": "BUY", "qty": "1"}
  ]
}
```

**Leg fields:**
- `instrumentName` — Full option symbol
- `side` — `"BUY"` or `"SELL"` (your intended direction)
- `qty` — Quantity as string

**Response:**
```json
{
  "data": {
    "requestId": "1983060031318396928",
    "expiryTime": 1761636929597,
    "state": "ACTIVE"
  }
}
```

### Get Quotes Received
```
GET /open/option/blocktrade/request/getQuotesReceived/v1?requestId={id}
```

Returns list of quotes from market makers. Each quote contains:
- `quoteId` — Unique quote identifier
- `legs` — Array with each leg's `side`, `price`, `quantity`, `instrumentName`
- `state` — Quote state (OPEN, CANCELLED, FILLED)

**Quote direction convention:**
- Leg `side: "SELL"` = market maker sells to us = **we buy** = we pay
- Leg `side: "BUY"` = market maker buys from us = **we sell** = we receive

### Accept Quote
```
POST /open/option/blocktrade/request/accept/v1
Content-Type: application/x-www-form-urlencoded
```
**Parameters (form-urlencoded):**
- `requestId` — RFQ request ID
- `quoteId` — Quote ID to accept

### Cancel RFQ
```
POST /open/option/blocktrade/request/cancel/v1
Content-Type: application/x-www-form-urlencoded
```
**Parameters (form-urlencoded):**
- `requestId` — RFQ request ID to cancel

### Get RFQ List
```
GET /open/option/blocktrade/rfqList/v1
```
Query your RFQ history with filters.

### RFQ States
| State | Meaning |
|-------|---------|
| `ACTIVE` | Waiting for quotes |
| `CANCELLED` | Cancelled by user |
| `FILLED` | Quote accepted and executed |
| `EXPIRED` | Timed out |
| `TRADED_AWAY` | Another quote was accepted |

---

## Account

### Get Account Summary
```
GET /open/account/summary/v1
```
**Response:**
| Field | Type | Description |
|-------|------|-------------|
| `equity` | `number` | Total account equity (USD) |
| `availableMargin` | `number` | Margin available for new trades |
| `imAmount` | `number` | Initial margin used |
| `mmAmount` | `number` | Maintenance margin required |
| `unrealizedPnL` | `number` | Total unrealised P&L |
| `imRatio` | `number` | Initial margin ratio |
| `mmRatio` | `number` | Maintenance margin ratio |
| `totalDollarValue` | `number` | Total account value in USD |

### Get Wallet
```
GET /open/account/wallet/v1
```
Returns holdings per asset.

### Query API Info
```
GET /open/auth/user/query-api
```
Returns API key permissions and readOnly status.

---

## Futures Trading

### Get Futures Instruments
```
GET /open/futures/market/instruments/v1
```

### Get Futures Symbol Info
```
GET /open/futures/market/symbol/v1
```

### Get Futures OrderBook
```
GET /open/futures/order/orderbook/v1/{symbol}
```

### Set Leverage
```
POST /open/futures/leverage/set/v1
```
**Parameters:** `symbol`, `leverage`

### Place Futures Order
```
POST /open/futures/order/create/v1
```
**Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | `string` | Yes | BTCUSD, ETHUSD, etc. |
| `tradeSide` | `number` | Yes | 1=BUY, 2=SELL |
| `tradeType` | `number` | Yes | 1=LIMIT, 2=MARKET, 3=POST_ONLY, 4=STOP_LIMIT, 5=STOP_MARKET |
| `qty` | `number` | Yes | Quantity |
| `price` | `number` | Limit only | Price |
| `triggerPrice` | `number` | Stop only | Trigger price |
| `reduceOnly` | `number` | No | 1=reduce only |

### Get Futures Positions
```
GET /open/futures/position/get/v1
```

---

## Spot Trading

### Get Spot Instruments
```
GET /open/spot/market/instruments
```

### Get Spot OrderBook
```
GET /open/spot/market/orderbook?symbol={symbol}
```

### Place Spot Order
```
POST /open/spot/trade/order/v1
```
**Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | `string` | Yes | TRXUSDT, etc. |
| `tradeSide` | `string` | Yes | 1=BUY, 2=SELL |
| `tradeType` | `string` | Yes | 1=LIMIT, 2=MARKET, 3=POST_ONLY |
| `qty` | `string` | Yes | Quantity |
| `price` | `string` | Limit only | Price |

**Note:** CALL token cannot be traded via API.

---

## WebSocket Connections

### Options WebSocket
```
wss://ws.coincall.com/options?code=10&uuid={uuid}&ts={ts}&sign={sign}&apiKey={apiKey}
```

### Futures WebSocket
```
wss://ws.coincall.com/futures?code=10&uuid={uuid}&ts={ts}&sign={sign}&apiKey={apiKey}
```

### Spot WebSocket (Public)
```
wss://ws.coincall.com/spot/ws
```

### Spot WebSocket (Private)
```
wss://ws.coincall.com/spot/ws/private?ts={ts}&sign={sign}&apiKey={apiKey}
```

### Subscribe Format
```json
{"action": "subscribe", "dataType": "order"}
{"action": "subscribe", "dataType": "position"}
{"action": "subscribe", "dataType": "orderBook", "payload": {"symbol": "BTCUSD"}}
```

### RFQ WebSocket Channels
| Channel | Data Type | Description |
|---------|-----------|-------------|
| `rfqMaker` | 28 | RFQ requests for market makers |
| `rfqTaker` | 129 | RFQ status updates for takers |
| `rfqQuote` | 130 | Quote updates for makers |
| `quoteReceived` | 131 | Incoming quotes for takers |
| `blockTradeDetail` | 22 | Private trade confirmations |
| `blockTradePublic` | 23 | Public trade feed |

### Heartbeat
Send any message within 30 seconds to keep connection alive.

---

## Error Codes

| Code | Message | Description |
|------|---------|-------------|
| 0 | Success | OK |
| 10534 | order.size.exceeds.the.maximum.limit.per.order | Order too large |
| 10540 | Order has expired | Order expired |
| 10558 | less.than.min.amount | Below minimum quantity |

---

## WebSocket Example (Python)

```python
import hashlib
import hmac
import websocket
import json
import time

api_key = "YOUR_API_KEY"
api_sec = "YOUR_API_SECRET"

def get_signed_header(ts):
    verb = 'GET'
    uri = '/users/self/verify'
    auth = verb + uri + '?apiKey=' + api_key + '&ts=' + str(ts)
    signature = hmac.new(
        api_sec.encode('utf-8'),
        auth.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature.upper()

def on_open(ws):
    ws.send(json.dumps({
        "action": "subscribe",
        "dataType": "order"
    }))

def on_message(ws, message):
    data = json.loads(message)
    print(data)

ts = int(time.time() * 1000)
sign = get_signed_header(ts)
url = f"wss://ws.coincall.com/options?code=10&ts={ts}&sign={sign}&apiKey={api_key}"

ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
ws.run_forever()
```

---

*For complete documentation, see https://docs.coincall.com/*
