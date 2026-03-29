# Massive API Reference (formerly Polygon.io)

Reference documentation for the Massive REST API as used by FinAlly for real market data. Polygon.io rebranded to Massive in October 2025. The old `api.polygon.io` base URL still works; the new base URL is `api.massive.com`.

## Authentication

All requests require an API key, obtained from https://massive.com/dashboard/api-keys.

**Header (recommended):**
```
Authorization: Bearer YOUR_API_KEY
```

**Query parameter:**
```
?apiKey=YOUR_API_KEY
```

## Rate Limits

| Plan | Cost | Rate Limit | Data Freshness | Notes |
|------|------|-----------|----------------|-------|
| Basic (Free) | $0/mo | 5 req/min | End-of-day only | No intraday or real-time |
| Starter | $29/mo | Unlimited | 15-min delayed | |
| Developer | $79/mo | Unlimited | Real-time | |
| Advanced | $199/mo | Unlimited | Real-time | |

Free tier returns HTTP 429 when the limit is exceeded. Paid "unlimited" tiers have a soft cap around 100 req/s.

## Endpoints Used by FinAlly

### 1. Snapshot — Multiple Tickers (Primary Endpoint)

This is the main endpoint FinAlly uses. It returns the latest price, daily bar, previous close, and trade data for a batch of tickers in a single API call.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers
```

**Query Parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `tickers` | No | Comma-separated list (e.g., `AAPL,GOOGL,MSFT`). Omit for all tickers. |
| `include_otc` | No | Include OTC securities. Default: `false`. |

**Example Request:**
```bash
curl "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Example Response:**
```json
{
  "count": 3,
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "day": {
        "o": 189.50,
        "h": 191.20,
        "l": 188.90,
        "c": 190.85,
        "v": 28727868,
        "vw": 190.12
      },
      "prevDay": {
        "o": 188.00,
        "h": 190.10,
        "l": 187.50,
        "c": 189.50,
        "v": 31504200,
        "vw": 189.05
      },
      "lastTrade": {
        "p": 190.85,
        "s": 100,
        "t": 1711771234306274000
      },
      "lastQuote": {
        "p": 190.84,
        "P": 190.86,
        "s": 3,
        "S": 1,
        "t": 1711771234306274000
      },
      "todaysChange": 1.35,
      "todaysChangePerc": 0.71,
      "updated": 1711771234306274000
    }
  ]
}
```

**Field Reference:**

Bar fields (`day`, `prevDay`, `min`):
- `o` open, `h` high, `l` low, `c` close, `v` volume, `vw` VWAP

`lastTrade`:
- `p` price, `s` size (shares), `t` SIP timestamp (nanoseconds)

`lastQuote`:
- `p` bid price, `P` ask price, `s` bid size, `S` ask size

Data resets at 3:30 AM EST daily; starts updating around 4:00 AM EST.

### 2. Snapshot — Single Ticker

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
```

**Example:**
```bash
curl "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/AAPL" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Response is the same shape, but wrapped in a singular `"ticker"` key instead of a `"tickers"` array.

### 3. Previous Close

Returns the previous trading day's OHLCV bar. Useful for calculating daily change when the snapshot hasn't updated yet.

```
GET /v2/aggs/ticker/{ticker}/prev
```

**Query Parameters:**

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `adjusted` | No | `true` | Adjust for splits |

**Example:**
```bash
curl "https://api.massive.com/v2/aggs/ticker/AAPL/prev" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response:**
```json
{
  "adjusted": true,
  "results": [
    {
      "T": "AAPL",
      "o": 188.00,
      "h": 190.10,
      "l": 187.50,
      "c": 189.50,
      "v": 31504200,
      "vw": 189.05,
      "t": 1711670400000,
      "n": 524891
    }
  ],
  "status": "OK"
}
```

Results fields: `T` ticker, `t` timestamp (Unix milliseconds), `n` number of transactions.

## Python Client: `massive`

### Installation

```bash
uv add massive
```

Requires Python 3.9+. Previously published as `polygon-api-client`.

### Initialization

```python
from massive import RESTClient

# Explicit API key
client = RESTClient(api_key="YOUR_API_KEY")

# Or reads from MASSIVE_API_KEY environment variable
client = RESTClient()
```

### Fetching Snapshots (Multiple Tickers)

This is what FinAlly uses — a single call to get all watched tickers:

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="YOUR_API_KEY")

# Fetch snapshots for specific tickers
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Day range: {snap.day.low} - {snap.day.high}")
    print(f"  Change: {snap.todays_change} ({snap.todays_change_perc}%)")
```

### Fetching a Single Ticker Snapshot

```python
snapshot = client.get_snapshot_ticker(ticker="AAPL")

price = snapshot.last_trade.price
timestamp_ms = snapshot.last_trade.timestamp  # Unix milliseconds
bid = snapshot.last_quote.bid_price
ask = snapshot.last_quote.ask_price
```

### Previous Close

```python
prev = client.get_previous_close_agg(ticker="AAPL")
for bar in prev:
    print(f"Previous close: ${bar.close}, Volume: {bar.volume}")
```

### Last Trade

```python
trade = client.get_last_trade(ticker="AAPL")
print(f"Last trade: ${trade.price}, size: {trade.size}")
```

### Aggregate Bars (Historical OHLCV)

```python
aggs = list(client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",       # "minute", "hour", "day", "week", etc.
    from_="2024-01-01",
    to="2024-12-31",
    limit=50000,
))

for bar in aggs:
    print(f"{bar.timestamp}: O={bar.open} H={bar.high} L={bar.low} C={bar.close}")
```

## How FinAlly Uses the API

FinAlly polls the multi-ticker snapshot endpoint on a fixed interval:

1. **Single API call** fetches all watched tickers at once (efficient for rate limits)
2. Extracts `last_trade.price` and `last_trade.timestamp` from each snapshot
3. Converts timestamps from milliseconds to seconds
4. Writes each price to the shared `PriceCache`
5. Sleeps for the configured interval, then repeats

**Polling interval by tier:**
- Free (5 req/min): poll every 15 seconds (default)
- Paid tiers: poll every 2-5 seconds (configurable)

The synchronous `RESTClient` is called via `asyncio.to_thread()` to avoid blocking the async event loop.

**Error handling:** Failed polls are logged but don't crash the polling loop. Common failures (401 bad key, 429 rate limit, network errors) are caught and retried on the next interval.

See `backend/app/market/massive_client.py` for the full implementation.
