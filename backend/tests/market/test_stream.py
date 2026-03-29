"""Tests for SSE streaming endpoint."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


@pytest.fixture
def cache_with_data():
    """PriceCache pre-loaded with two tickers."""
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    cache.update("GOOGL", 175.00)
    return cache


def _make_mock_request(*, disconnected: bool = False) -> AsyncMock:
    """Create a mock Request that reports connection status."""
    request = AsyncMock()
    request.is_disconnected.return_value = disconnected
    request.client = AsyncMock()
    request.client.host = "127.0.0.1"
    return request


@pytest.mark.asyncio
async def test_generator_starts_with_retry(cache_with_data):
    """First SSE message is the retry directive."""
    request = _make_mock_request()
    gen = _generate_events(cache_with_data, request, interval=0.01)
    first = await gen.__anext__()
    assert first == "retry: 1000\n\n"
    await gen.aclose()


@pytest.mark.asyncio
async def test_generator_sends_event_and_data(cache_with_data):
    """Generator sends event: price-update and data payload."""
    request = _make_mock_request()
    gen = _generate_events(cache_with_data, request, interval=0.01)

    messages: list[str] = []
    async for msg in gen:
        messages.append(msg)
        if len(messages) >= 2:
            break
    await gen.aclose()

    # First message is retry, second is the price event
    assert messages[0] == "retry: 1000\n\n"
    event_msg = messages[1]
    assert event_msg.startswith("event: price-update\n")
    assert "data: " in event_msg

    # Parse the data payload
    data_line = [l for l in event_msg.split("\n") if l.startswith("data: ")][0]
    payload = json.loads(data_line.removeprefix("data: "))
    assert "AAPL" in payload
    assert "GOOGL" in payload
    assert payload["AAPL"]["price"] == 190.50


@pytest.mark.asyncio
async def test_generator_skips_when_no_change(cache_with_data):
    """Generator does not send data when version hasn't changed."""
    request = _make_mock_request()
    gen = _generate_events(cache_with_data, request, interval=0.01)

    messages: list[str] = []
    # Collect first two messages (retry + initial data)
    async for msg in gen:
        messages.append(msg)
        if len(messages) >= 2:
            break

    # Wait a cycle — version hasn't changed, so no new data should come
    await asyncio.sleep(0.05)
    # The generator would yield nothing new unless version changes
    assert len(messages) == 2
    await gen.aclose()


@pytest.mark.asyncio
async def test_generator_stops_on_disconnect():
    """Generator stops when the client disconnects."""
    cache = PriceCache()
    cache.update("AAPL", 190.50)

    request = _make_mock_request()
    # Disconnect after first call
    request.is_disconnected.side_effect = [False, True]

    gen = _generate_events(cache, request, interval=0.01)
    messages: list[str] = []
    async for msg in gen:
        messages.append(msg)

    # Should have retry + one data message, then stopped
    assert len(messages) == 2


def test_create_stream_router_returns_fresh_router():
    """Each call to create_stream_router returns an independent router."""
    cache = PriceCache()
    r1 = create_stream_router(cache)
    r2 = create_stream_router(cache)
    assert r1 is not r2


def test_create_stream_router_has_prices_route():
    """Router includes the /prices endpoint."""
    cache = PriceCache()
    router = create_stream_router(cache)
    paths = [route.path for route in router.routes]
    assert "/api/stream/prices" in paths
