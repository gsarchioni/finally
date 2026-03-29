# Market Data Backend — Detailed Design

This document provides a comprehensive, implementation-level design for FinAlly's market data subsystem. It covers the unified interface, GBM simulator, Massive API client, shared price cache, SSE streaming, and app-level integration. Every module includes code snippets drawn from the actual implementation.

---

## 1. Design Principles

| Principle | How It's Applied |
|-----------|-----------------|
| **Producer-Consumer decoupling** | Data sources write to `PriceCache`; consumers (SSE, portfolio, trades) read from it. Neither side knows about the other. |
| **Strategy pattern** | `MarketDataSource` ABC defines the contract. A factory selects the concrete implementation at startup based on an environment variable. |
| **Lazy initialization** | Data sources are created unstarted. The caller controls the lifecycle (`start` / `stop`). |
| **Thread safety** | `PriceCache` uses a `threading.Lock` so it's safe for concurrent reads from SSE and writes from the data source. |
| **Resilience** | Both data sources catch and log errors in their loops without crashing. The next tick/poll retries automatically. |

---

## 2. Architecture

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

### File Map

All files live under `backend/app/market/`:

| File | Purpose |
|------|---------|
| `models.py` | `PriceUpdate` frozen dataclass |
| `cache.py` | Thread-safe `PriceCache` |
| `interface.py` | `MarketDataSource` abstract base class |
| `seed_prices.py` | Default tickers, seed prices, GBM parameters, correlation groups |
| `simulator.py` | `GBMSimulator` (math) + `SimulatorDataSource` (async wrapper) |
| `massive_client.py` | `MassiveDataSource` (REST polling wrapper) |
| `factory.py` | `create_market_data_source()` — env-driven source selection |
| `stream.py` | SSE streaming endpoint (`/api/stream/prices`) |
| `__init__.py` | Public API re-exports |

---

## 3. Data Model — `PriceUpdate`

**File:** `backend/app/market/models.py`

Every price observation flowing through the system is an immutable `PriceUpdate`. Immutability means it's safe to pass between threads without copying.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

### Design notes

- `frozen=True` makes it hashable and prevents accidental mutation.
- `slots=True` reduces memory per instance (no `__dict__`).
- `direction` is computed, not stored — derived from `price` vs `previous_price`.
- `to_dict()` is the canonical serialization for SSE JSON payloads. It includes all computed properties so the frontend doesn't need to recalculate.

---

## 4. Price Cache

**File:** `backend/app/market/cache.py`

The cache is the central decoupling point. Data sources write; consumers read. There is exactly one `PriceCache` instance per application.

```python
class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price  # First update is "flat"

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        return self._version
```

### Key behaviors

| Behavior | Detail |
|----------|--------|
| **Automatic previous_price** | On `update()`, the cache looks up the last stored price. Data sources only provide `ticker` + `price`. |
| **First update is flat** | If no previous price exists, `previous_price = price`, giving `direction = "flat"`. This avoids a spurious change on the initial seed. |
| **Price rounding** | All prices are rounded to 2 decimal places on write. |
| **Monotonic version** | `_version` increments on every `update()`. The SSE endpoint uses this to avoid sending duplicate data — it only serializes when `version` has changed since the last push. |
| **Thread safety** | `threading.Lock` guards all reads and writes. This is sufficient because the critical sections are tiny (dict lookups/inserts). |

---

## 5. Abstract Interface — `MarketDataSource`

**File:** `backend/app/market/interface.py`

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

### Lifecycle contract

```
create (via factory)  →  start(tickers)  →  add/remove  →  stop()
                         ↑                                    ↑
                     called once                         safe to call multiple times
```

- `start()` must be called exactly once. It creates the background loop.
- `stop()` is idempotent — safe to call multiple times.
- `add_ticker()` / `remove_ticker()` can be called at any time after `start()`.

---

## 6. Factory — Source Selection

**File:** `backend/app/market/factory.py`

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

The factory reads `MASSIVE_API_KEY` at call time. Empty string and whitespace-only are treated as "not set". Returns an **unstarted** source — the caller owns the lifecycle.

---

## 7. Seed Prices & GBM Parameters

**File:** `backend/app/market/seed_prices.py`

This module centralizes all configurable constants for the simulator.

### Seed Prices

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,  "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
    "TSLA": 250.00,  "NVDA": 800.00,  "META": 500.00, "JPM": 195.00,
    "V": 280.00,     "NFLX": 600.00,
}
```

### Per-Ticker Volatility & Drift

```python
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},   # Moderate, steady
    "GOOGL": {"sigma": 0.25, "mu": 0.05},  # Moderate
    "MSFT": {"sigma": 0.20, "mu": 0.05},   # Low volatility, steady
    "AMZN": {"sigma": 0.28, "mu": 0.05},   # Above-average volatility
    "TSLA": {"sigma": 0.50, "mu": 0.03},   # Highest volatility, low drift
    "NVDA": {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},   # Above-average
    "JPM": {"sigma": 0.18, "mu": 0.04},    # Low volatility (bank)
    "V": {"sigma": 0.17, "mu": 0.04},      # Lowest volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},   # High volatility
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}
```

Dynamically added tickers (not in `SEED_PRICES`) get a random seed price between $50-$300 and use `DEFAULT_PARAMS`.

### Correlation Groups

```python
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6     # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3    # Between sectors / unknown tickers
TSLA_CORR = 0.3           # TSLA does its own thing
```

---

## 8. GBM Simulator — Implementation Detail

**File:** `backend/app/market/simulator.py`

The simulator is split into two classes for separation of concerns:

- **`GBMSimulator`** — Pure math engine. No async, no cache, no I/O. Easy to unit test.
- **`SimulatorDataSource`** — Async lifecycle wrapper. Adapts the simulator to the `MarketDataSource` contract.

### 8.1 GBMSimulator — The Math Engine

#### GBM Formula

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` = current price
- `mu` = annualized drift (expected return)
- `sigma` = annualized volatility
- `dt` = time step as fraction of a trading year
- `Z` = correlated standard normal random variable

#### Time Step

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8
```

This tiny `dt` produces sub-cent moves per tick, which accumulate naturally over time.

#### Core Step Method

```python
def step(self) -> dict[str, float]:
    n = len(self._tickers)
    if n == 0:
        return {}

    # Generate n independent standard normal draws
    z_independent = np.random.standard_normal(n)

    # Apply Cholesky to get correlated draws
    if self._cholesky is not None:
        z_correlated = self._cholesky @ z_independent
    else:
        z_correlated = z_independent

    result: dict[str, float] = {}
    for i, ticker in enumerate(self._tickers):
        params = self._params[ticker]
        mu = params["mu"]
        sigma = params["sigma"]

        # GBM formula
        drift = (mu - 0.5 * sigma**2) * self._dt
        diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
        self._prices[ticker] *= math.exp(drift + diffusion)

        # Random event: ~0.1% chance per tick per ticker
        if random.random() < self._event_prob:
            shock_magnitude = random.uniform(0.02, 0.05)
            shock_sign = random.choice([-1, 1])
            self._prices[ticker] *= 1 + shock_magnitude * shock_sign

        result[ticker] = round(self._prices[ticker], 2)

    return result
```

#### Cholesky Correlation

The Cholesky decomposition transforms independent random variables into correlated ones. The matrix is rebuilt only when tickers change (add/remove), not on every tick.

```python
def _rebuild_cholesky(self) -> None:
    n = len(self._tickers)
    if n <= 1:
        self._cholesky = None  # No correlation needed for 0-1 tickers
        return

    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
            corr[i, j] = rho
            corr[j, i] = rho

    self._cholesky = np.linalg.cholesky(corr)
```

Pairwise correlation lookup:

```python
@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR              # 0.3
    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR        # 0.6
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR     # 0.5
    return CROSS_GROUP_CORR           # 0.3
```

#### Random Events

To create visual drama in the sparklines:

- **Probability:** 0.1% per tick per ticker
- **Magnitude:** 2-5% shock (uniform random)
- **Direction:** equally likely up or down
- **Expected frequency:** With 10 tickers at 2 ticks/sec, expect a shock roughly every 50 seconds

#### Dynamic Ticker Management

```python
def add_ticker(self, ticker: str) -> None:
    if ticker in self._prices:
        return  # No-op for duplicates
    self._add_ticker_internal(ticker)
    self._rebuild_cholesky()

def remove_ticker(self, ticker: str) -> None:
    if ticker not in self._prices:
        return  # No-op for non-existent
    self._tickers.remove(ticker)
    del self._prices[ticker]
    del self._params[ticker]
    self._rebuild_cholesky()
```

`_add_ticker_internal` is the batch-safe version (no Cholesky rebuild), used during `__init__` to add all starting tickers before a single rebuild.

### 8.2 SimulatorDataSource — Async Wrapper

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache,
                 update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache immediately so SSE has data before the first tick
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

### Key implementation details

- **Immediate seeding:** `start()` writes seed prices to the cache before the loop begins. This ensures the SSE endpoint has data the instant a client connects.
- **Task naming:** `asyncio.create_task(..., name="simulator-loop")` makes debugging easier.
- **Exception resilience:** The loop catches all exceptions, logs them, and continues. A single bad tick never kills the simulator.
- **Clean cancellation:** `stop()` cancels the task and awaits it, catching `CancelledError`.

---

## 9. Massive API Client — Implementation Detail

**File:** `backend/app/market/massive_client.py`

### 9.1 MassiveDataSource

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache,
                 poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None
```

### 9.2 Start with Immediate First Poll

```python
async def start(self, tickers: list[str]) -> None:
    self._client = RESTClient(api_key=self._api_key)
    self._tickers = list(tickers)

    # Immediate first poll — cache has data before the loop starts
    await self._poll_once()

    self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
```

### 9.3 Polling Loop

```python
async def _poll_loop(self) -> None:
    while True:
        await asyncio.sleep(self._interval)
        await self._poll_once()
```

The sleep is *before* the poll (not after) because `start()` already did the first poll.

### 9.4 Single Poll Cycle

```python
async def _poll_once(self) -> None:
    if not self._tickers or not self._client:
        return

    try:
        # Sync client called via thread to avoid blocking the event loop
        snapshots = await asyncio.to_thread(self._fetch_snapshots)
        for snap in snapshots:
            try:
                price = snap.last_trade.price
                timestamp = snap.last_trade.timestamp / 1000.0  # ms → seconds
                self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
            except (AttributeError, TypeError) as e:
                logger.warning("Skipping snapshot for %s: %s",
                               getattr(snap, "ticker", "???"), e)
    except Exception as e:
        logger.error("Massive poll failed: %s", e)
```

### 9.5 Snapshot Fetching

```python
def _fetch_snapshots(self) -> list:
    return self._client.get_snapshot_all(
        market_type=SnapshotMarketType.STOCKS,
        tickers=self._tickers,
    )
```

This uses the Massive Python SDK (`massive` package, previously `polygon-api-client`). The `get_snapshot_all` call hits the batch endpoint:

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...
```

One API call for all tickers — efficient for rate limits (free tier: 5 req/min).

### 9.6 Ticker Management

```python
async def add_ticker(self, ticker: str) -> None:
    ticker = ticker.upper().strip()
    if ticker not in self._tickers:
        self._tickers.append(ticker)

async def remove_ticker(self, ticker: str) -> None:
    ticker = ticker.upper().strip()
    self._tickers = [t for t in self._tickers if t != ticker]
    self._cache.remove(ticker)
```

Note: tickers are normalized to uppercase and stripped. Added tickers appear on the *next* poll cycle (no immediate fetch).

### 9.7 Error Handling Strategy

| Error | Behavior |
|-------|----------|
| 401 (bad API key) | Logged, retried next interval |
| 429 (rate limit) | Logged, retried next interval |
| Network error | Logged, retried next interval |
| Malformed snapshot | Skipped (per-ticker), others processed |
| Empty ticker list | Poll skipped entirely |

The design philosophy: **never crash the loop**. If the API is down, the cache retains stale data. When the API recovers, fresh data flows in.

---

## 10. SSE Streaming Endpoint

**File:** `backend/app/market/stream.py`

### Router Factory

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    return router
```

### Event Generator

```python
async def _generate_events(
    price_cache: PriceCache, request: Request, interval: float = 0.5
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"  # Auto-reconnect after 1s

    last_version = -1
    try:
        while True:
            if await request.is_disconnected():
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
```

### SSE Protocol Details

| Detail | Value |
|--------|-------|
| **Endpoint** | `GET /api/stream/prices` |
| **Content-Type** | `text/event-stream` |
| **Push interval** | ~500ms |
| **Retry directive** | `retry: 1000` (browser reconnects after 1s) |
| **Change detection** | Compares `price_cache.version` — skips push if nothing changed |
| **Disconnect detection** | `request.is_disconnected()` checked each cycle |
| **Proxy compatibility** | `X-Accel-Buffering: no` prevents nginx from buffering |

### SSE Event Format

Each event is a single `data:` line containing a JSON object keyed by ticker:

```
data: {"AAPL": {"ticker": "AAPL", "price": 190.50, "previous_price": 190.00, "change": 0.50, "change_percent": 0.26, "direction": "up", "timestamp": 1711771234.5}, "GOOGL": {...}}
```

The frontend uses the browser's native `EventSource` API:

```javascript
const source = new EventSource('/api/stream/prices');
source.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // Update each ticker in the UI
};
```

---

## 11. App-Level Integration

### Startup Sequence

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

# 1. Create shared cache (single instance for the app)
price_cache = PriceCache()

# 2. Factory picks Simulator or Massive based on MASSIVE_API_KEY env var
source = create_market_data_source(price_cache)

# 3. Start with default watchlist (loaded from database)
default_tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]
await source.start(default_tickers)

# 4. Wire up SSE endpoint
stream_router = create_stream_router(price_cache)
app.include_router(stream_router)
```

### Dynamic Ticker Management (Watchlist Changes)

When the user adds/removes a ticker from their watchlist (via REST API or AI chat):

```python
# User adds PYPL to watchlist
await source.add_ticker("PYPL")
# Simulator: immediately seeds cache with price; Massive: appears on next poll

# User removes NFLX from watchlist
await source.remove_ticker("NFLX")
# Both: removed from active set AND from cache
```

### Reading Prices (Portfolio, Trades)

Other backend components read from the cache, never from the data source:

```python
# Get current price for trade execution
price = price_cache.get_price("AAPL")  # Returns float or None

# Get full update for portfolio display
update = price_cache.get("AAPL")  # Returns PriceUpdate or None

# Get all prices for portfolio valuation
all_prices = price_cache.get_all()  # Returns dict[str, PriceUpdate]
```

### Shutdown

```python
await source.stop()
```

---

## 12. Testing Strategy

### Test Files

| File | Scope | Type |
|------|-------|------|
| `test_models.py` | `PriceUpdate` dataclass: creation, computed properties, serialization, immutability | Unit |
| `test_cache.py` | `PriceCache`: update/get, direction, version, remove, rounding, thread-safety | Unit |
| `test_factory.py` | Factory: env var handling (empty, whitespace, set), correct type returned | Unit |
| `test_simulator.py` | `GBMSimulator`: step output, positive prices, seed prices, add/remove, Cholesky, correlation | Unit |
| `test_simulator_source.py` | `SimulatorDataSource`: start/stop lifecycle, cache integration, dynamic tickers, resilience | Integration (async) |
| `test_massive.py` | `MassiveDataSource`: poll updates cache, malformed snapshot handling, API errors, timestamp conversion, ticker management | Unit (mocked API) |

### Running Tests

```bash
cd backend
uv run --extra dev pytest tests/market/ -v
```

### Key Testing Patterns

**Simulator tests** run the actual GBM math — no mocks needed:

```python
def test_prices_are_positive(self):
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0  # exp() guarantees this
```

**Massive tests** mock the REST client since we don't want real API calls:

```python
async def test_poll_updates_cache(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL"]
    source._client = MagicMock()

    mock_snapshots = [_make_snapshot("AAPL", 190.50, 1707580800000)]
    with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
```

**Async tests** use `pytest-asyncio` with `asyncio_mode = "auto"`:

```python
@pytest.mark.asyncio
class TestSimulatorDataSource:
    async def test_start_populates_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        assert cache.get("AAPL") is not None
        await source.stop()
```

---

## 13. Dependencies

| Package | Used By | Purpose |
|---------|---------|---------|
| `numpy>=2.0.0` | `GBMSimulator` | Cholesky decomposition, vectorized random draws |
| `massive>=1.0.0` | `MassiveDataSource` | Massive (Polygon.io) Python SDK |
| `fastapi>=0.115.0` | `stream.py` | SSE endpoint router |
| `rich>=13.0.0` | `market_data_demo.py` | Terminal demo dashboard (dev only) |

Standard library: `math` (exp, sqrt), `random` (events), `threading` (Lock), `asyncio`, `json`, `time`, `dataclasses`.

---

## 14. Configuration Reference

| Setting | Location | Default | Notes |
|---------|----------|---------|-------|
| `MASSIVE_API_KEY` | `.env` / environment | empty (simulator) | Set to enable real market data |
| Simulator tick interval | `SimulatorDataSource.__init__` | 0.5s | |
| Massive poll interval | `MassiveDataSource.__init__` | 15.0s | Free tier safe. Reduce for paid tiers. |
| Event probability | `GBMSimulator.__init__` | 0.001 (0.1%) | Per tick per ticker |
| SSE push interval | `_generate_events()` | 0.5s | |
| SSE retry directive | `_generate_events()` | 1000ms | Browser auto-reconnect delay |

---

## 15. Data Flow Summary

```
Simulator (500ms)  ─or─  Massive API (15s)
        │                        │
        └─── write ──────────────┘
                     │
                     ▼
              ┌──────────────┐
              │  PriceCache  │ (single instance, thread-safe)
              │  version: N  │
              └──────┬───────┘
                     │ read
         ┌───────────┼────────────────┐
         ▼           ▼                ▼
    SSE Stream   GET /api/portfolio  POST /api/portfolio/trade
    (500ms push)  (on-demand)        (on-demand)
         │
         ▼
    EventSource (browser)
    → price flash animation
    → sparkline accumulation
    → watchlist update
```
