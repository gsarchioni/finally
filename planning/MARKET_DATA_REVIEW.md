# Market Data Implementation — Critical Review

**Date:** 2026-03-29
**Scope:** `backend/app/market/` (9 source files, 7 test files, 73 tests)
**Status:** All 73 tests passing

---

## Overall Assessment

The market data subsystem is well-architected and cleanly implemented. It correctly follows the Strategy pattern with a shared cache, and the GBM math is sound. The code is concise, well-documented, and easy to follow.

That said, there are several issues worth addressing — ranging from concurrency subtleties that could bite during integration, to missing behavior that the rest of the platform will need.

---

## Issues

### 1. `PriceCache.version` Read Is Not Thread-Safe

**Severity:** Medium
**File:** `cache.py:64-67`

The `version` property reads `self._version` without acquiring the lock:

```python
@property
def version(self) -> int:
    return self._version
```

While CPython's GIL makes this safe for single integer reads *today*, it violates the module's own thread-safety contract. The SSE stream reads `version` from an async context while the data source writes to the cache from `asyncio.to_thread` (Massive) or the event loop (Simulator). If this code ever runs on a non-CPython interpreter (PyPy, GraalPy) or if the lock is replaced with a more granular mechanism, this becomes a data race.

**Fix:** Wrap the read in `with self._lock:`.

### 2. `PriceCache.version` Not Incremented on `remove()`

**Severity:** Medium
**File:** `cache.py:59-63`

When a ticker is removed, `version` is not bumped. This means the SSE stream won't detect the removal and will continue broadcasting stale data for the removed ticker until another ticker's price changes.

**Fix:** Increment `self._version` inside `remove()`.

### 3. SSE Sends All Tickers, Not Just Changed Ones

**Severity:** Low (performance, not correctness)
**File:** `stream.py:76-83`

Every time `version` changes (i.e., any single ticker updates), the SSE endpoint serializes and sends the entire price map. With 10 tickers updating every 500ms, each SSE frame is ~2KB. This is fine now, but becomes wasteful if the watchlist grows or if multiple clients connect.

The `version` counter is a blunt instrument — it tells you *something* changed but not *what*. A per-ticker version or a "dirty set" would enable delta-only SSE pushes.

**Recommendation:** Not blocking, but worth noting for the frontend integration phase. The current approach is simpler and correct.

### 4. Simulator Does Not Normalize Ticker Case

**Severity:** Medium
**File:** `simulator.py:120-125`, `simulator.py:146-152`

`MassiveDataSource` normalizes tickers to uppercase (`ticker.upper().strip()`), but `SimulatorDataSource` and `GBMSimulator` do not. If a caller passes `"aapl"` to the simulator, it will create a separate ticker from `"AAPL"`. This asymmetry between the two implementations violates the "one interface, same behavior" contract.

**Fix:** Normalize tickers in `GBMSimulator.add_ticker()` and `SimulatorDataSource.add_ticker()`, or centralize normalization in the `MarketDataSource` interface (e.g., a base class method).

### 5. No Way to Query "Which Tickers Are in the Cache?"

**Severity:** Low
**File:** `cache.py`

`PriceCache` has `get_all()` which returns all prices, but there's no `tickers()` method that returns just the set of tracked ticker symbols. Other parts of the system (watchlist sync, portfolio valuation) will likely need this.

`get_all()` works as a fallback (`set(cache.get_all().keys())`), but a dedicated method would be cleaner.

### 6. `stream.py` Uses Module-Level Router

**Severity:** Low (design smell)
**File:** `stream.py:17`

The router is created at module level (`router = APIRouter(...)`) but routes are registered inside `create_stream_router()`. This means:

- Importing the module has the side effect of creating a router.
- Calling `create_stream_router()` multiple times registers duplicate routes on the same router object.

This is unlikely to cause bugs in practice (one call at startup), but it breaks the factory pattern's intent.

**Fix:** Move `router = APIRouter(...)` inside `create_stream_router()`.

### 7. No SSE `event:` Field

**Severity:** Low
**File:** `stream.py:83`

SSE events are sent as `data: ...` without an `event:` type field. The SSE spec defaults the event type to `"message"`, which works, but adding `event: price-update` would let the frontend use `EventSource.addEventListener("price-update", ...)` instead of the generic `onmessage`. This is a minor API hygiene point.

### 8. `_fetch_snapshots` Return Type Is `list` (Untyped)

**Severity:** Low
**File:** `massive_client.py:123`

```python
def _fetch_snapshots(self) -> list:
```

The return type is bare `list` rather than `list[TickerSnapshot]` or similar. This is the only untyped return in the module. It reduces IDE support for callers.

### 9. GBM `dt` Does Not Adapt to Actual Elapsed Time

**Severity:** Low (correctness nuance)
**File:** `simulator.py:47-48`

The time step `dt` is hardcoded to exactly 500ms of trading time. But `asyncio.sleep(0.5)` does not guarantee exactly 500ms — event loop load, GC pauses, or system scheduling can cause drift. Over long sessions, the simulated time diverges from wall-clock time.

For a demo/educational simulator this is acceptable. For more realism, `step()` could accept the actual elapsed seconds since the last call.

### 10. Documentation References Non-Existent Files

**Severity:** Low (hygiene)
**File:** `CLAUDE.md` (project root)

The project-root CLAUDE.md references:
- `planning/MARKET_DATA_SUMMARY.md` — does not exist
- `planning/archive` folder — does not exist

These should either be created or the references removed.

---

## Test Coverage Assessment

### Strengths

- **73 tests, all passing** across 7 test files.
- Good coverage of edge cases: empty tickers, duplicate add, nonexistent remove, malformed API snapshots, zero previous price.
- Async integration tests for `SimulatorDataSource` verify real cache population and background task behavior.
- `MassiveDataSource` tests mock the API boundary correctly without over-mocking internal logic.

### Gaps

| Gap | Severity | Notes |
|-----|----------|-------|
| No SSE endpoint test | Medium | `stream.py` has zero test coverage. The async generator, version-based change detection, and disconnect handling are untested at the unit level. |
| No concurrent write test for `PriceCache` | Low | Thread-safety is correct by inspection, but no test spawns multiple threads writing simultaneously. |
| `test_prices_rounded_to_two_decimals` is fragile | Low | Checks string representation rather than `round(price, 2) == price`. Floating-point formatting quirks could cause false passes/failures. |
| No test for `remove()` → SSE behavior | Low | Related to issue #2. No test verifies that removing a ticker is reflected in the SSE stream. |
| No factory test for `create_stream_router` | Low | The router factory is untested — no test verifies it returns a valid FastAPI router with the expected route. |

---

## Spec Compliance (vs. PLAN.md Section 6)

| Requirement | Status | Notes |
|-------------|--------|-------|
| Two implementations, one interface | Pass | `SimulatorDataSource` and `MassiveDataSource` both implement `MarketDataSource` |
| Environment-variable driven selection | Pass | Factory checks `MASSIVE_API_KEY` |
| GBM with configurable drift/volatility | Pass | Per-ticker `mu` and `sigma` in `seed_prices.py` |
| ~500ms update interval | Pass | Default `update_interval=0.5` |
| Correlated ticker moves | Pass | Cholesky decomposition with sector groupings |
| Random 2-5% events | Pass | 0.1% probability, `uniform(0.02, 0.05)` magnitude |
| Realistic seed prices | Pass | 10 tickers with approximate real-world prices |
| Massive REST polling (not WebSocket) | Pass | Uses `RESTClient.get_snapshot_all()` |
| Free tier: poll every 15s | Pass | Default `poll_interval=15.0` |
| Shared in-memory price cache | Pass | `PriceCache` with `threading.Lock` |
| SSE endpoint at `/api/stream/prices` | Pass | Via `create_stream_router()` |
| SSE contains ticker, price, previous, timestamp, direction | Pass | `PriceUpdate.to_dict()` includes all fields |
| SSE auto-reconnection | Pass | `retry: 1000` directive sent on connect |

---

## Recommendations (Priority Order)

1. **Fix `PriceCache.version` thread safety** — acquire lock on read (Issue #1)
2. **Bump version on `remove()`** — so SSE detects ticker removal (Issue #2)
3. **Normalize ticker case in Simulator** — match Massive behavior (Issue #4)
4. **Add SSE endpoint tests** — this is the only untested source file (Test Gap)
5. **Move router inside factory** — clean up module-level side effect (Issue #6)
6. **Fix stale doc references** — remove or create `MARKET_DATA_SUMMARY.md` and `planning/archive` (Issue #10)
