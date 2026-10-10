"""Tests for the async Bun minifier path: success, timeout/failure fallback,
session reuse and shutdown."""

import asyncio

import aiohttp
import pytest
from aiohttp import web

import addon


@pytest.fixture()
async def bun_stub(monkeypatch):
    """A stub of the Bun minify API on an ephemeral port.

    Yields (base_url, state); set state["css_delay"] / state["css_status"]
    to simulate slowness or failures.
    """
    state = {"css_delay": 0, "css_status": 200}

    async def css(request):
        if state["css_delay"]:
            await asyncio.sleep(state["css_delay"])
        body = await request.text()
        return web.Response(status=state["css_status"], text="minified:" + body)

    async def svg(request):
        body = await request.text()
        return web.Response(text="minsvg:" + body)

    async def html(request):
        data = await request.json()
        return web.json_response(
            {"code": "minhtml:" + data["code"], "links": ["http://x/y.css"]}
        )

    app = web.Application()
    app.router.add_post("/api/minify/css", css)
    app.router.add_post("/api/minify/svg", svg)
    app.router.add_post("/api/minify/html", html)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(addon, "BUN_MINIFY_BASE_URL", base_url)
    yield base_url, state
    await runner.cleanup()


@pytest.fixture(autouse=True)
async def clean_session():
    yield
    await addon.close_session()


async def test_do_minify_css_success(bun_stub):
    assert await addon.do_minify_css("a { color: red; }") == "minified:a { color: red; }"


async def test_do_minify_svg_success(bun_stub):
    assert await addon.do_minify_svg("<svg/>") == "minsvg:<svg/>"


async def test_process_html_success(bun_stub):
    code, links = await addon.process_html("<html>", "http://x/")
    assert code == "minhtml:<html>"
    assert links == ["http://x/y.css"]


async def test_minify_timeout_falls_back_to_original(bun_stub, monkeypatch):
    _, state = bun_stub
    state["css_delay"] = 5
    monkeypatch.setattr(addon, "MINIFY_TIMEOUT", aiohttp.ClientTimeout(total=0.2))
    assert await addon.do_minify_css("a{}") == "a{}"


async def test_minify_http_error_falls_back_to_original(bun_stub):
    _, state = bun_stub
    state["css_status"] = 500
    assert await addon.do_minify_css("a{}") == "a{}"


async def test_minify_connection_refused_falls_back_to_original(monkeypatch):
    # nothing listens here
    monkeypatch.setattr(addon, "BUN_MINIFY_BASE_URL", "http://127.0.0.1:1")
    assert await addon.do_minify_css("a{}") == "a{}"
    assert await addon.do_minify_svg("<svg/>") == "<svg/>"
    code, links = await addon.process_html("<html>", "http://x/")
    assert (code, links) == ("<html>", [])


async def test_session_is_reused():
    assert addon.get_session() is addon.get_session()


async def test_close_session_idempotent():
    await addon.close_session()
    await addon.close_session()
    assert not addon.get_session().closed
