"""Tests for speculative fetching: in-flight dedupe and concurrency limits."""

import asyncio

import pytest
from aiohttp import web
from mitmproxy import http

import addon


@pytest.fixture()
async def fetch_stub(monkeypatch):
    """Counting stub server with a slow endpoint for concurrency tests."""
    state = {"hits": 0, "in_flight": 0, "max_in_flight": 0}

    async def asset(request):
        state["hits"] += 1
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        try:
            await asyncio.sleep(0.2)
            return web.Response(body=b"x" * 5000)
        finally:
            state["in_flight"] -= 1

    app = web.Application()
    app.router.add_get("/asset", asset)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/asset", state
    await runner.cleanup()


@pytest.fixture(autouse=True)
async def clean_state():
    addon._spec_fetch_inflight.clear()
    yield
    addon._spec_fetch_inflight.clear()
    await addon.close_session()


def _headers():
    return http.Headers([(b"accept", b"*/*")])


async def test_duplicate_in_flight_fetch_hits_server_once(fetch_stub):
    url, state = fetch_stub
    results = await asyncio.gather(
        addon.do_async_http_request(url, _headers()),
        addon.do_async_http_request(url, _headers()),
        addon.do_async_http_request(url, _headers()),
    )
    assert state["hits"] == 1
    assert all(r is not None and r[0] == b"x" * 5000 for r in results)


async def test_sequential_fetches_each_hit_server(fetch_stub):
    """Dedupe only applies while a fetch is in flight."""
    url, state = fetch_stub
    await addon.do_async_http_request(url, _headers())
    await addon.do_async_http_request(url, _headers())
    assert state["hits"] == 2


async def test_concurrency_limit_respected(fetch_stub, monkeypatch):
    url, state = fetch_stub
    monkeypatch.setattr(addon, "_spec_fetch_semaphore", asyncio.Semaphore(2))
    await asyncio.gather(
        *[addon.do_async_http_request(f"{url}?{i}", _headers()) for i in range(6)]
    )
    assert state["max_in_flight"] <= 2
    assert state["hits"] == 6


async def test_failed_fetch_returns_none_and_unblocks():
    await addon.close_session()
    # nothing listens on port 1
    result = await addon.do_async_http_request(
        "http://127.0.0.1:1/nope", _headers()
    )
    assert result is None
    # in-flight entry was cleaned up
    assert "http://127.0.0.1:1/nope" not in addon._spec_fetch_inflight
