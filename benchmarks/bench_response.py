#!/usr/bin/env python3
"""Benchmarks for the Arachnidium performance work (task item 6).

Modes:
  response --tag before|after   Drive addon.response() over representative
                                fixtures; writes results/<tag>.json
  micro    --tag after          Micro-benchmarks: blocklist lookup old-vs-new,
                                speculative-cache eviction old-vs-new, and the
                                image size-threshold validation.
  compare  a.json b.json        Print a markdown comparison table.

Run with the project venv python, e.g.:
  .venv/bin/python benchmarks/bench_response.py --mode response --tag after
"""

import argparse
import asyncio
import gzip
import io
import json
import os
import random
import statistics
import sys
import time
import types

# --- import addon from the repo this script lives in (works in worktrees) ---
_tk = types.ModuleType("tkinter")
sys.modules.setdefault("tkinter", _tk)
sys.modules.setdefault("tkinter.ttk", types.ModuleType("tkinter.ttk"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

import mitmproxy.addonmanager  # noqa: E402,F401 -- resolves annotations
import mitmproxy.dns  # noqa: E402,F401 -- resolves annotations
import mitmproxy.http  # noqa: E402,F401 -- resolves annotations
import addon  # noqa: E402
from aiohttp import web  # noqa: E402
from mitmproxy import http  # noqa: E402
from mitmproxy.test import tflow  # noqa: E402
from PIL import Image  # noqa: E402

addon.ENABLE_GUI = False
RESULTS_DIR = os.path.join(REPO_ROOT, "benchmarks", "results")


# ---------------------------------------------------------------- fixtures
def noisy_image(size=(800, 600), seed=7):
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    return img


def make_html(n_paragraphs=1200):
    parts = [
        "<!DOCTYPE html><html><head><title>Bench</title>"
        "<style>" + "body { color: #333; } /* pad */" * 200 + "</style></head><body>"
    ]
    for i in range(n_paragraphs):
        parts.append(
            f"<p id='p{i}' class='para'>Lorem ipsum dolor sit amet, consectetur "
            f"adipiscing elit {i}.</p>"
        )
    parts.append("</body></html>")
    return "".join(parts).encode()


def make_css(n_rules=1500):
    return (
        "".join(
            f".cls{i} {{ color: #{i % 256:02x}0000; margin: {i % 12}px; }}\n"
            for i in range(n_rules)
        )
    ).encode()


def make_js(n_funcs=800):
    return (
        "".join(
            f"function fn{i}(a, b) {{ var x = a + {i}; return x * b; }}\n"
            for i in range(n_funcs)
        )
    ).encode()


FIXTURES = {}


def _register_fixtures():
    html = make_html()
    css = make_css()
    js = make_js()
    FIXTURES["html-200k"] = ("text/html", None, html)
    FIXTURES["css-50k"] = ("text/css", None, css)
    FIXTURES["js-identity-60k"] = ("application/javascript", None, js)
    FIXTURES["js-gzip-60k"] = ("application/javascript", "gzip", gzip.compress(js))
    FIXTURES["jpeg-photo"] = (
        "image/jpeg",
        None,
        _jpeg_bytes(noisy_image()),
    )
    FIXTURES["png-noisy"] = ("image/png", None, _png_bytes(noisy_image((512, 512))))
    FIXTURES["webp-input"] = ("image/webp", None, _webp_bytes(noisy_image((512, 512))))
    FIXTURES["png-small"] = ("image/png", None, _png_bytes(noisy_image((32, 32))))


def _jpeg_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _webp_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=80)
    return buf.getvalue()


def make_flow(content_type, content_encoding, body, url="http://example.com/asset"):
    flow = tflow.tflow(resp=True)
    flow.request = http.Request.make(
        "GET", url, headers=http.Headers([(b"accept-encoding", b"gzip, deflate, br")])
    )
    res_headers = http.Headers([(b"content-type", content_type.encode())])
    if content_encoding:
        res_headers["content-encoding"] = content_encoding
    # set raw_content directly: Response.make would (re-)encode `body`.
    flow.response = http.Response.make(200, b"", res_headers)
    flow.response.raw_content = body
    return flow


# ---------------------------------------------------------------- Bun stub
async def start_bun_stub():
    async def css(request):
        body = await request.text()
        # crude but real minification work
        return web.Response(text=" ".join(body.split()))

    async def svg(request):
        return web.Response(text=(await request.text()).strip())

    async def html(request):
        data = await request.json()
        return web.json_response({"code": data["code"], "links": []})

    app = web.Application()
    app.router.add_post("/api/minify/css", css)
    app.router.add_post("/api/minify/svg", svg)
    app.router.add_post("/api/minify/html", html)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 3000)
    await site.start()
    return runner


# ---------------------------------------------------------------- part A
def percentile(data, p):
    data = sorted(data)
    k = (len(data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(data) - 1)
    return data[f] + (data[c] - data[f]) * (k - f)


async def bench_response(tag):
    _register_fixtures()
    runner = await start_bun_stub()
    results = {"tag": tag, "fixtures": {}}
    try:
        for name, (ctype, cenc, body) in FIXTURES.items():
            n = 8 if ctype.startswith("image/") else 30
            # warmup
            for _ in range(2):
                await addon.response(make_flow(ctype, cenc, body))
            walls = []
            sizes_in, sizes_out = [], []
            cpu0 = time.process_time()
            t0 = time.perf_counter()
            for _ in range(n):
                flow = make_flow(ctype, cenc, body)
                sizes_in.append(len(flow.response.raw_content))
                s = time.perf_counter()
                await addon.response(flow)
                walls.append((time.perf_counter() - s) * 1000)
                sizes_out.append(len(flow.response.raw_content))
            total_s = time.perf_counter() - t0
            cpu_ms = (time.process_time() - cpu0) * 1000
            results["fixtures"][name] = {
                "n": n,
                "wall_ms_p50": round(percentile(walls, 50), 2),
                "wall_ms_p95": round(percentile(walls, 95), 2),
                "cpu_ms_per_resp": round(cpu_ms / n, 2),
                "throughput_rps": round(n / total_s, 1),
                "bytes_in": sizes_in[0],
                "bytes_out_mean": round(statistics.mean(sizes_out)),
            }
            print(f"  {name}: p50={results['fixtures'][name]['wall_ms_p50']}ms "
                  f"p95={results['fixtures'][name]['wall_ms_p95']}ms "
                  f"in={sizes_in[0]} out~{results['fixtures'][name]['bytes_out_mean']}")
    finally:
        await runner.cleanup()
        if hasattr(addon, "close_session"):
            await addon.close_session()
    return results


# ---------------------------------------------------------------- part B/C/D
def bench_blocklist():
    entries = {f"blocked{i}.example.com" for i in range(50000)}
    entries |= {"ads.example.com", "tracker.net"}
    addon.DNS_BLOCKLIST = set(entries)

    def old_check(host):
        return any((host == s or host.endswith("." + s)) for s in entries)

    hosts = (
        ["ads.example.com", "a.ads.example.com", "x.tracker.net"]
        + [f"host{i}.other.org" for i in range(197)]
    )
    # new
    t0 = time.perf_counter()
    for h in hosts:
        addon.check_dns_blocklist(h)
    new_s = time.perf_counter() - t0
    # old
    t0 = time.perf_counter()
    for h in hosts:
        old_check(h.lower())
    old_s = time.perf_counter() - t0
    return {
        "entries": len(entries),
        "lookups": len(hosts),
        "old_ms": round(old_s * 1000, 1),
        "new_ms": round(new_s * 1000, 1),
        "speedup": round(old_s / new_s, 1),
    }


def bench_cache_evict():
    max_entries = 1000
    n_puts = 3000

    # old: dict + full min-scan
    old_cache = {}

    def old_put(url, ts):
        if len(old_cache) >= max_entries:
            oldest = min(old_cache, key=lambda k: old_cache[k][1])
            del old_cache[oldest]
        old_cache[url] = (None, ts)

    t0 = time.perf_counter()
    for i in range(n_puts):
        old_put(f"u{i}", float(i))
    old_s = time.perf_counter() - t0

    # new: LRU
    fake_task = types.SimpleNamespace(done=lambda: True, cancel=lambda: None)
    new_cache = addon.SpeculativeCache(max_entries=max_entries)
    t0 = time.perf_counter()
    for i in range(n_puts):
        new_cache.put(f"u{i}", fake_task)
    new_s = time.perf_counter() - t0
    assert len(new_cache) == max_entries
    return {
        "max_entries": max_entries,
        "puts": n_puts,
        "old_ms": round(old_s * 1000, 1),
        "new_ms": round(new_s * 1000, 1),
        "speedup": round(old_s / new_s, 1),
    }


def bench_image_threshold():
    """At what input size does WebP re-encode stop paying off?"""
    rows = []
    for px in (16, 32, 64, 128, 256, 512):
        img = noisy_image((px, px), seed=px)
        png = _png_bytes(img)
        webp = addon.convert_webp(png)
        rows.append(
            {"px": px, "png_bytes": len(png), "webp_bytes": len(webp),
             "shrinks": len(webp) < len(png)}
        )
    return rows


async def bench_micro(tag):
    return {
        "tag": tag,
        "blocklist": bench_blocklist(),
        "cache_evict": bench_cache_evict(),
        "image_threshold": bench_image_threshold(),
    }


# ---------------------------------------------------------------- compare
def cmd_compare(a_path, b_path):
    a = json.load(open(a_path))["fixtures"]
    b = json.load(open(b_path))["fixtures"]
    print("\n| fixture | p50 ms (before → after) | p95 ms (before → after) | "
          "cpu ms/resp (before → after) | bytes out (before → after) |")
    print("|---|---|---|---|---|")
    for name in a:
        fa, fb = a[name], b[name]
        print(f"| {name} | {fa['wall_ms_p50']} → {fb['wall_ms_p50']} | "
              f"{fa['wall_ms_p95']} → {fb['wall_ms_p95']} | "
              f"{fa['cpu_ms_per_resp']} → {fb['cpu_ms_per_resp']} | "
              f"{fa['bytes_out_mean']} → {fb['bytes_out_mean']} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["response", "micro", "compare"], required=True)
    ap.add_argument("--tag", default="after")
    ap.add_argument("--out", default=RESULTS_DIR)
    ap.add_argument("files", nargs="*")
    args = ap.parse_args()

    if args.mode == "compare":
        cmd_compare(args.files[0], args.files[1])
        return
    os.makedirs(args.out, exist_ok=True)
    if args.mode == "response":
        results = asyncio.run(bench_response(args.tag))
    else:
        results = asyncio.run(bench_micro(args.tag))
    path = os.path.join(args.out, f"{args.mode}-{args.tag}.json")
    json.dump(results, open(path, "w"), indent=2)
    print(f"wrote {path}")
    if args.mode == "micro":
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
