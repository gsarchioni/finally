# Market Simulator — GBM Price Engine

This document describes the Geometric Brownian Motion (GBM) market simulator used by FinAlly when no Massive API key is configured. The simulator produces realistic, correlated stock price movements with occasional dramatic events.

## Mathematical Model

The simulator uses the classic GBM formula from quantitative finance:

```
S(t + dt) = S(t) * exp((mu - sigma^2 / 2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- **S(t)** = current price
- **mu** = annualized drift (expected return, e.g., 0.05 = 5%/year)
- **sigma** = annualized volatility (e.g., 0.25 = 25%/year)
- **dt** = time step as a fraction of a trading year
- **Z** = correlated standard normal random variable

### Time Step Calculation

The simulator ticks every 500ms. Expressed as a fraction of a trading year:

```
Trading seconds per year = 252 days * 6.5 hours/day * 3600 sec/hour = 5,896,800
dt = 0.5 / 5,896,800 = ~8.48e-8
```

This tiny dt produces sub-cent moves per tick that accumulate naturally over time, giving realistic price behavior at any observation frequency.

### Why GBM?

GBM is the foundation of the Black-Scholes model and the standard assumption for stock price modeling:
- Prices are always positive (exponential ensures this)
- Returns are log-normally distributed (matches empirical observations reasonably well)
- The drift term provides realistic long-term trends
- The volatility term creates realistic random fluctuations
- It's computationally trivial — one `exp()` call per ticker per tick

## Correlation Structure

Real stocks don't move independently. Tech stocks tend to move together; a broad market selloff drags everything down. The simulator models this using **Cholesky decomposition** of a correlation matrix.

### Correlation Groups

```python
CORRELATION_GROUPS = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}
```

### Correlation Coefficients

| Pair Type | Correlation | Meaning |
|-----------|-------------|---------|
| Tech-Tech (excluding TSLA) | 0.6 | Strong co-movement |
| Finance-Finance | 0.5 | Moderate co-movement |
| TSLA with anything | 0.3 | TSLA does its own thing |
| Cross-sector / unknown | 0.3 | Weak background correlation |

### How Cholesky Works

1. Build an n x n correlation matrix C from the pairwise correlations above
2. Compute the Cholesky decomposition: L where L * L^T = C
3. On each tick, generate n independent standard normal draws Z
4. Multiply: Z_correlated = L @ Z
5. Use Z_correlated[i] as the random input for ticker i in the GBM formula

This transforms independent random variables into correlated ones matching the desired correlation structure. The matrix is rebuilt only when tickers are added or removed — not on every tick.

## Random Events (Volatility Shocks)

To add drama, the simulator occasionally applies sudden price shocks:

- **Probability:** 0.1% per tick per ticker (~0.001)
- **Magnitude:** 2-5% move (uniformly random)
- **Direction:** Equally likely up or down
- **Expected frequency:** With 10 tickers at 2 ticks/second, expect a shock roughly every 50 seconds

These create the sudden spikes visible in sparklines — more engaging than pure GBM.

## Per-Ticker Parameters

Each default ticker has tuned volatility and drift values:

| Ticker | Seed Price | Volatility (sigma) | Drift (mu) | Character |
|--------|-----------|-------------------|------------|-----------|
| AAPL | $190 | 0.22 | 0.05 | Moderate, steady |
| GOOGL | $175 | 0.25 | 0.05 | Moderate |
| MSFT | $420 | 0.20 | 0.05 | Low volatility, steady |
| AMZN | $185 | 0.28 | 0.05 | Above-average volatility |
| TSLA | $250 | 0.50 | 0.03 | Highest volatility, low drift |
| NVDA | $800 | 0.40 | 0.08 | High volatility, strong drift |
| META | $500 | 0.30 | 0.05 | Above-average |
| JPM | $195 | 0.18 | 0.04 | Low volatility (bank) |
| V | $280 | 0.17 | 0.04 | Lowest volatility (payments) |
| NFLX | $600 | 0.35 | 0.05 | High volatility |

Dynamically added tickers use defaults: sigma=0.25, mu=0.05, and a random seed price between $50-$300.

## Code Structure

### GBMSimulator Class

The core math engine (`backend/app/market/simulator.py`):

```python
class GBMSimulator:
    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None:
        # Initialize per-ticker prices and params from seed_prices.py
        # Build Cholesky decomposition of correlation matrix

    def step(self) -> dict[str, float]:
        # Hot path — called every 500ms
        # 1. Generate n independent standard normal draws
        # 2. Apply Cholesky for correlated draws
        # 3. Apply GBM formula to each ticker
        # 4. Apply random events (0.1% chance per ticker)
        # 5. Return {ticker: new_price}

    def add_ticker(self, ticker: str) -> None:
        # Add ticker with seed/default params, rebuild Cholesky

    def remove_ticker(self, ticker: str) -> None:
        # Remove ticker, rebuild Cholesky
```

### SimulatorDataSource Class

The async wrapper that implements `MarketDataSource`:

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache,
                 update_interval: float = 0.5) -> None:
        # Store cache reference and interval

    async def start(self, tickers: list[str]) -> None:
        # 1. Create GBMSimulator with the initial ticker list
        # 2. Seed the cache with initial prices (so SSE has data immediately)
        # 3. Launch asyncio.create_task for the tick loop

    async def stop(self) -> None:
        # Cancel the background task

    async def add_ticker(self, ticker: str) -> None:
        # Delegate to GBMSimulator.add_ticker
        # Seed cache immediately so ticker has a price right away

    async def remove_ticker(self, ticker: str) -> None:
        # Delegate to GBMSimulator.remove_ticker
        # Remove from cache

    async def _run_loop(self) -> None:
        # while True: step(), write prices to cache, sleep(0.5)
```

### Separation of Concerns

The design splits into two classes:
- **`GBMSimulator`** — Pure math, no async, no cache, no I/O. Easy to unit test.
- **`SimulatorDataSource`** — Async lifecycle, cache integration, background task management. Adapts the simulator to the `MarketDataSource` contract.

## Dependencies

- **numpy** — For Cholesky decomposition (`np.linalg.cholesky`) and vectorized random number generation (`np.random.standard_normal`). Used only in the simulator, not the Massive client.
- **math** — Standard library `exp` and `sqrt` for the GBM formula.
- **random** — Standard library for event probability and shock magnitude.

## Testing

Tests are in `backend/tests/market/`:

- **`test_simulator.py`** — GBM math correctness, correlation structure, random events, ticker add/remove, Cholesky rebuild
- **`test_simulator_source.py`** — Async lifecycle (start/stop), cache integration, dynamic ticker management

Run with:
```bash
cd backend
uv run --extra dev pytest tests/market/test_simulator.py -v
uv run --extra dev pytest tests/market/test_simulator_source.py -v
```
