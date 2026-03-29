# Market Data Interface — Unified Design

This document describes the unified Python interface for retrieving stock prices in FinAlly. The system uses the Massive API when `MASSIVE_API_KEY` is set, otherwise falls back to a GBM simulator. All downstream code is agnostic to the data source.

## Architecture

```
┌──────────────────┐     ┌──────────────────┐
│ SimulatorDataSrc  │     │ MassiveDataSource │
│ (GBM, 500ms tick) │     │ (REST poll, 15s)  │
└────────┬─────────┘     └────────┬─────────┘
         │   implements           │   implements
         ▼                        ▼
    ┌─────────────────────────────────┐
    │     MarketDataSource (ABC)      │
    │  start / stop / add / remove    │
    └────────────────┬────────────────┘
                     │ writes to
                     ▼
            ┌─────────────────┐
            │   PriceCache    │
            │ (thread-safe)   │
            └────────┬────────┘
                     │ read by
          ┌──────────┼──────────────┐
          ▼          ▼              ▼
      SSE Stream  Portfolio     Trade Exec
```

The key design principle: **data sources write, consumers read, the cache decouples them.**

## Source Selection: Factory Pattern

The factory reads `MASSIVE_API_KEY` from the environment at startup:

```python
# backend/app/market/factory.py

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

Returns an **unstarted** source. The caller must `await source.start(tickers)`.

## Abstract Interface

All data sources implement `MarketDataSource` (`backend/app/market/interface.py`):

```python
class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set and cache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

**Lifecycle contract:**
1. Create via factory: `source = create_market_data_source(cache)`
2. Start with initial tickers: `await source.start(["AAPL", "GOOGL", ...])`
3. Dynamically manage tickers: `await source.add_ticker("TSLA")`
4. Shut down cleanly: `await source.stop()`

## Shared Price Cache

Both implementations write to the same `PriceCache` (`backend/app/market/cache.py`):

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate
    def get(self, ticker: str) -> PriceUpdate | None
    def get_price(self, ticker: str) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]
    def remove(self, ticker: str) -> None
    @property
    def version(self) -> int   # Monotonic counter, bumped on every update
```

Thread-safe via `threading.Lock`. The `version` property enables efficient SSE change detection — only push to clients when the cache has actually changed.

## Price Data Model

Every price update is an immutable `PriceUpdate` (`backend/app/market/models.py`):

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float           # Unix seconds

    @property
    def change(self) -> float            # Absolute price change
    @property
    def change_percent(self) -> float    # Percentage change
    @property
    def direction(self) -> str           # "up", "down", or "flat"

    def to_dict(self) -> dict            # JSON serialization
```

The cache automatically computes `previous_price` from the last stored value, so data sources only need to provide `ticker`, `price`, and optionally `timestamp`.

## Implementation: Simulator

`SimulatorDataSource` (`backend/app/market/simulator.py`) wraps `GBMSimulator`:

- **Update cadence:** Every 500ms via `asyncio.sleep`
- **Runs in-process:** No external dependencies
- **Dynamic tickers:** Adding/removing rebuilds the correlation matrix
- **Initial seeding:** Writes seed prices to cache immediately on `start()` so SSE has data before the first tick

See [SIMULATOR.md](SIMULATOR.md) for the mathematical model and implementation details.

## Implementation: Massive API

`MassiveDataSource` (`backend/app/market/massive_client.py`):

- **Update cadence:** Every 15s (free tier) or 2-5s (paid), configurable via `poll_interval`
- **Batch fetching:** Single API call for all tickers via snapshot endpoint
- **Async integration:** Sync `RESTClient` called via `asyncio.to_thread()`
- **Immediate first poll:** `start()` does one poll before entering the loop, so the cache has data right away
- **Resilient:** Failed polls are logged, not raised — the loop retries next interval

See [MASSIVE_API.md](MASSIVE_API.md) for the API reference and response formats.

## SSE Streaming (Consumer)

The SSE endpoint (`backend/app/market/stream.py`) reads from `PriceCache` every 500ms:

```
GET /api/stream/prices → text/event-stream
```

- Compares `price_cache.version` to avoid sending duplicate data
- Serializes all prices via `PriceUpdate.to_dict()` into a single JSON payload
- Sends `retry: 1000` so `EventSource` auto-reconnects after 1 second
- Sets `X-Accel-Buffering: no` to prevent nginx from buffering the stream

**Event format:**
```
data: {"AAPL": {"ticker": "AAPL", "price": 190.50, "previous_price": 190.00, "change": 0.50, "change_percent": 0.26, "direction": "up", "timestamp": 1711771234.5}, ...}
```

## Module Exports

All public types are exported from `backend/app/market/__init__.py`:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```

## File Map

| File | Purpose |
|------|---------|
| `interface.py` | Abstract `MarketDataSource` contract |
| `models.py` | `PriceUpdate` immutable dataclass |
| `cache.py` | Thread-safe `PriceCache` |
| `factory.py` | Environment-driven source selection |
| `simulator.py` | GBM simulator + `SimulatorDataSource` |
| `massive_client.py` | Massive API polling + `MassiveDataSource` |
| `stream.py` | SSE streaming endpoint |
| `seed_prices.py` | Default tickers, seed prices, GBM parameters |
| `__init__.py` | Public API exports |

## Usage Example (App Startup)

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

# Create shared cache
price_cache = PriceCache()

# Factory picks Massive or Simulator based on env
source = create_market_data_source(price_cache)

# Start with default watchlist
default_tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]
await source.start(default_tickers)

# Wire up SSE endpoint
stream_router = create_stream_router(price_cache)
app.include_router(stream_router)

# Later: dynamic ticker management
await source.add_ticker("PYPL")
await source.remove_ticker("NFLX")

# Shutdown
await source.stop()
```
