"""Tests for the mitmproxy response hook: skip rules, fast paths, minify."""

import asyncio
import gzip
import io

import pytest
from aiohttp import web
from mitmproxy import http
from mitmproxy.test import tflow
from PIL import Image

import addon


@pytest.fixture(autouse=True)
def no_gui(monkeypatch):
    monkeypatch.setattr(addon, "ENABLE_GUI", False)
    yield
    addon.speculative_cache.clear()


@pytest.fixture(autouse=True)
async def clean_session():
    yield
    await addon.close_session()


def make_flow(
    body: bytes,
    content_type="text/html",
    content_encoding=None,
    method="GET",
    url="http://example.com/page",
    accept_encoding="gzip, deflate, br",
    status=200,
):
    flow = tflow.tflow(resp=True)
    req_headers = http.Headers(
        [(b"accept-encoding", accept_encoding.encode())]
    )
    flow.request = http.Request.make(method, url, headers=req_headers)
    res_headers = http.Headers([(b"content-type", content_type.encode())])
    if content_encoding:
        res_headers["content-encoding"] = content_encoding
    # NB: pass b"" as content and set raw_content directly: Response.make
    # would otherwise (re-)encode `body` per the content-encoding header.
    flow.response = http.Response.make(status, b"", res_headers)
    flow.response.raw_content = body
    return flow


def png_bytes(size=(64, 64)):
    img = Image.new("RGB", size, color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def noisy_bytes(size=(256, 256)):
    import random
    random.seed(7)
    img = Image.new("RGB", size)
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = (random.randrange(256), random.randrange(256), random.randrange(256))
    return img


def webp_bytes(size=(256, 256)):
    buf = io.BytesIO()
    noisy_bytes(size).save(buf, format="WEBP", quality=80)
    return buf.getvalue()


def png_bytes_noisy(size=(256, 256)):
    buf = io.BytesIO()
    noisy_bytes(size).save(buf, format="PNG")
    return buf.getvalue()


async def test_skips_post():
    flow = make_flow(b"<html>" * 100, method="POST")
    before = flow.response.raw_content
    await addon.response(flow)
    assert flow.response.raw_content == before


async def test_skips_xhr():
    flow = make_flow(b"<html>" * 100)
    flow.request.headers["X-Requested-With"] = "XMLHttpRequest"
    before = flow.response.raw_content
    await addon.response(flow)
    assert flow.response.raw_content == before


async def test_skips_206():
    flow = make_flow(b"x" * 1000, status=206)
    before = flow.response.raw_content
    await addon.response(flow)
    assert flow.response.raw_content == before


async def test_skips_already_compressed_types():
    flow = make_flow(b"x" * 1000, content_type="application/zip")
    before = flow.response.raw_content
    await addon.response(flow)
    assert flow.response.raw_content == before


async def test_webp_passthrough_no_reencode():
    """image/webp input must not be re-encoded (needless CPU + quality loss)."""
    body = webp_bytes((256, 256))
    assert len(body) > 2048, len(body)
    flow = make_flow(body, content_type="image/webp")
    await addon.response(flow)
    assert flow.response.raw_content == body


async def test_small_image_passthrough():
    body = png_bytes((8, 8))
    assert len(body) < addon.MIN_IMAGE_PROCESSING_SIZE
    flow = make_flow(body, content_type="image/png")
    await addon.response(flow)
    assert flow.response.raw_content == body


async def test_png_gets_converted():
    body = png_bytes_noisy((256, 256))
    assert len(body) > addon.MIN_IMAGE_PROCESSING_SIZE, len(body)
    flow = make_flow(body, content_type="image/png")
    await addon.response(flow)
    assert flow.response.headers["content-type"] == "image/webp"
    assert flow.response.raw_content != body


async def test_gzipped_js_passthrough_fast_path():
    """Minify no-op + acceptable wire encoding -> body untouched."""
    raw_js = b"function hello() { return 42; }" * 200
    gz = gzip.compress(raw_js)
    flow = make_flow(
        gz, content_type="application/javascript", content_encoding="gzip"
    )
    await addon.response(flow)
    assert flow.response.raw_content == gz
    assert flow.response.headers["content-encoding"] == "gzip"


async def test_identity_js_still_gets_compressed():
    """Uncompressed bodies keep the original compress-on-the-fly behavior."""
    raw_js = b"function hello() { return 42; }" * 200
    flow = make_flow(raw_js, content_type="application/javascript")
    await addon.response(flow)
    assert flow.response.headers["content-encoding"] == "gzip"
    assert gzip.decompress(flow.response.raw_content) == raw_js


async def test_redirect_body_cleared():
    flow = make_flow(b"<html>redirect</html>" * 20, status=302)
    await addon.response(flow)
    assert flow.response.raw_content == b""
    assert flow.response.headers["content-length"] == "0"


@pytest.fixture()
async def bun_stub(monkeypatch):
    async def css(request):
        body = await request.text()
        return web.Response(text="/*min*/" + body)

    app = web.Application()
    app.router.add_post("/api/minify/css", css)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(addon, "BUN_MINIFY_BASE_URL", f"http://127.0.0.1:{port}")
    yield
    await runner.cleanup()


async def test_css_minified_through_stub(bun_stub):
    css = b"body { color: red; } /* comment */" * 50
    flow = make_flow(css, content_type="text/css")
    await addon.response(flow)
    assert b"/*min*/" in gzip.decompress(flow.response.raw_content)


async def test_speculative_cache_hit_forges_response():
    flow = tflow.tflow(resp=True)
    flow.request = http.Request.make(
        "GET",
        "http://example.com/app.js",
        headers=http.Headers([(b"accept-encoding", b"gzip")]),
    )
    flow.response = http.Response.make(404, b"not found", http.Headers())

    async def fake_fetch():
        return (
            b"console.log(1);" * 50,
            {"content-type": "application/javascript"},
        )

    task = asyncio.ensure_future(fake_fetch())
    addon.speculative_cache.put("http://example.com/app.js", task)
    await addon.request(flow)
    assert flow.response.status_code == 200
    assert b"console.log(1);" in gzip.decompress(flow.response.raw_content)
    addon.speculative_cache.clear()


async def test_speculative_miss_leaves_flow_alone():
    flow = tflow.tflow(resp=True)
    flow.request = http.Request.make("GET", "http://example.com/nope")
    flow.response = http.Response.make(200, b"orig", http.Headers())
    await addon.request(flow)
    assert flow.response.raw_content == b"orig"
