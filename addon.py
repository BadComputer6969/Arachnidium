from __future__ import annotations

import mitmproxy

# import mitmproxy_rs
import tkinter as tk
from tkinter import ttk

import subprocess
import threading
import asyncio
import collections
import aiohttp
import time
import io
import os

from PIL import Image, ImageTk
import requests
import json
import qrcode

import gzip
import zlib as deflate
import brotli as br
import zstandard as zstd

# Outbound HTTP (Bun minifier + speculative prefetch) shares one session
# owned by the addon, so connections are reused instead of re-established
# per request. Created lazily on first async use, closed in done().
_session: aiohttp.ClientSession | None = None

MINIFY_TIMEOUT = aiohttp.ClientTimeout(total=3)
SPEC_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=5)
BUN_MINIFY_BASE_URL = "http://localhost:3000"


def _make_session() -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        limit=64,  # total concurrent outbound connections
        limit_per_host=16,
        ttl_dns_cache=300,
    )
    return aiohttp.ClientSession(connector=connector)


def get_session() -> aiohttp.ClientSession:
    """Return the shared outbound session, creating it if needed."""
    global _session
    if _session is None or _session.closed:
        _session = _make_session()
    return _session


async def close_session() -> None:
    """Close the shared session; safe to call when already closed."""
    global _session
    session, _session = _session, None
    if session is not None and not session.closed:
        await session.close()


with open("defaults.json", "r") as defaults_json:
    defaults = json.loads(defaults_json.read())
    ENABLE_GUI = defaults["ENABLE_GUI"]
    FORCE_MAX_COMPRESSION = defaults["FORCE_MAX_COMPRESSION"]
    IMAGE_QUALITY = defaults["IMAGE_QUALITY"]
    USE_AVIF = defaults["USE_AVIF"]
    USE_SPECULATIVE_CACHE = defaults["USE_SPECULATIVE_CACHE"]
    SPECULATIVE_CACHE_MAX_ENTRIES = defaults["SPECULATIVE_CACHE_MAX_ENTRIES"]
    CLEAR_HTTP_ERRORS = defaults["CLEAR_HTTP_ERRORS"]
    BLOCK_ADS = defaults["BLOCK_ADS"]
    ENABLE_DEBUG = defaults["ENABLE_DEBUG"]
    MAX_PROCESSING_SIZE_MB = defaults["MAX_PROCESSING_SIZE_MB"]

MIN_PROCESSING_SIZE = 100
# Images below this size are passed through: re-encoding them rarely
# shrinks them enough to justify the CPU (see benchmarks/).
MIN_IMAGE_PROCESSING_SIZE = 1024
EMPTY_BODY_STATUSES = {301, 302, 304, 307, 308}

ALREADY_COMPRESSED_TYPES = (
    "application/zip",
    "application/x-zip-compressed",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
    "application/x-gzip",
    "application/x-bzip2",
    "application/octet-stream",
    "video/",
    "audio/",
)

IMAGE_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/vnd.microsoft.icon",
)


BLOCKLIST_FILE = "blocklist.txt"
CACHE_TTL_SECONDS = 86400
DNS_BLOCKLIST: set[str] = set()

def start_gui():
    if not ENABLE_GUI:
        return

    global gui_root, frame_root, stats_frame
    gui_root = tk.Tk()
    gui_root.title("Arachnidium web proxy")

    frame_root = ttk.Frame(gui_root)
    frame_root.pack(fill="both", expand=True, padx=100, pady=30)

    gui_root.tk.call("source", "theme/azure.tcl")
    gui_root.tk.call("set_theme", "light")

    stats_frame = ttk.Frame(frame_root)
    stats_frame.pack(pady=(0, 15))

    labels_frame = ttk.Frame(stats_frame)
    labels_frame.pack(side="left", padx=(0, 10))

    global saved_label_widget, used_label_widget
    saved_label_widget = ttk.Label(
        labels_frame, text="Data saved: 0B", font=("Helvetica", 14)
    )
    saved_label_widget.pack(anchor="w")

    used_label_widget = ttk.Label(
        labels_frame, text="Data used: 0B", font=("Helvetica", 14)
    )
    used_label_widget.pack(anchor="w", pady=(5, 0))

    def reset_savings():
        global data_saved, data_used
        data_saved = 0
        data_used = 0
        saved_label_widget.config(text="Data saved: 0B")
        used_label_widget.config(text="Data used: 0B")

    reset_btn = ttk.Button(stats_frame, text="↻", width=3, command=reset_savings)
    reset_btn.pack(side="right", fill="y", pady=2)

    tk_IMAGE_QUALITY = tk.IntVar(value=IMAGE_QUALITY)
    tk_FORCE_MAX_COMPRESSION = tk.BooleanVar(value=FORCE_MAX_COMPRESSION)
    tk_USE_AVIF = tk.BooleanVar(value=USE_AVIF)
    tk_USE_SPECULATIVE_CACHE = tk.BooleanVar(value=USE_SPECULATIVE_CACHE)
    tk_CLEAR_HTTP_ERRORS = tk.BooleanVar(value=CLEAR_HTTP_ERRORS)
    tk_BLOCK_ADS = tk.BooleanVar(value=BLOCK_ADS)
    tk_ENABLE_DEBUG = tk.BooleanVar(value=ENABLE_DEBUG)

    image_quality_label = ttk.Label(
        frame_root, text="Image Quality (" + str(IMAGE_QUALITY) + ")"
    )
    image_quality_label.pack()

    def update_settings(_=0):
        global IMAGE_QUALITY, FORCE_MAX_COMPRESSION, USE_AVIF, USE_SPECULATIVE_CACHE, CLEAR_HTTP_ERRORS, BLOCK_ADS, ENABLE_DEBUG
        IMAGE_QUALITY = tk_IMAGE_QUALITY.get()
        FORCE_MAX_COMPRESSION = tk_FORCE_MAX_COMPRESSION.get()
        USE_AVIF = tk_USE_AVIF.get()
        USE_SPECULATIVE_CACHE = tk_USE_SPECULATIVE_CACHE.get()
        CLEAR_HTTP_ERRORS = tk_CLEAR_HTTP_ERRORS.get()
        BLOCK_ADS = tk_BLOCK_ADS.get()
        ENABLE_DEBUG = tk_ENABLE_DEBUG.get()
        image_quality_label.config(text="Image Quality (" + str(IMAGE_QUALITY) + ")")

    ttk.Scale(
        frame_root,
        from_=0,
        to=100,
        orient="horizontal",
        variable=tk_IMAGE_QUALITY,
        command=update_settings,
    ).pack()
    ttk.Checkbutton(
        frame_root,
        text="Force Max Compression",
        variable=tk_FORCE_MAX_COMPRESSION,
        command=update_settings,
    ).pack()
    ttk.Checkbutton(
        frame_root,
        text="Use AVIF Images",
        variable=tk_USE_AVIF,
        command=update_settings
    ).pack()
    ttk.Checkbutton(
        frame_root,
        text="Speculative Caching",
        variable=tk_USE_SPECULATIVE_CACHE,
        command=update_settings,
    ).pack()
    ttk.Checkbutton(
        frame_root,
        text="Clear HTTP Error Body",
        variable=tk_CLEAR_HTTP_ERRORS,
        command=update_settings,
    ).pack()
    ttk.Checkbutton(
        frame_root, text="Block Ads", variable=tk_BLOCK_ADS, command=update_settings
    ).pack()
    ttk.Checkbutton(
        frame_root, text="Debug Mode", variable=tk_ENABLE_DEBUG, command=update_settings
    ).pack()

    def open_qrcode_window():
        new_window = tk.Toplevel(frame_root)
        new_window.title("WireGuard Configuration QR Code")
        new_window.geometry("300x300")
        with open("wireguard.cfg", "r") as wg_config_file:
            config = wg_config_file.read()
            config_qr = qrcode.make(config)
            config_qr = config_qr.resize((300, 300))
            qr_img = ImageTk.PhotoImage(config_qr)
            panel = tk.Label(new_window, image=qr_img)
            panel.img = qr_img
            panel.pack()

    ttk.Button(
        frame_root, text="Show WireGuard QR Code", command=open_qrcode_window
    ).pack(pady=(10, 0))

    def on_window_close():
        mitmproxy.ctx.master.shutdown()

    gui_root.protocol("WM_DELETE_WINDOW", on_window_close)

    gui_root.mainloop()


# Prefetched responses expire if nobody claims them quickly.
SPECULATIVE_CACHE_TTL_SECONDS = 300


class _SpecEntry:
    __slots__ = ("task", "timestamp")

    def __init__(self, task, timestamp):
        self.task = task
        self.timestamp = timestamp


class SpeculativeCache:
    """Bounded LRU cache for speculative prefetch results.

    Entries are consumed once (pop on hit). Evicting the least-recently-used
    entry is O(1); expired entries are dropped on access and by
    evict_expired(). Pending fetch tasks of discarded entries are cancelled
    so they don't leak.
    """

    def __init__(self, max_entries: int = 1000, ttl: float = SPECULATIVE_CACHE_TTL_SECONDS):
        self.max_entries = max_entries
        self.ttl = ttl
        self._entries: "collections.OrderedDict[str, _SpecEntry]" = (
            collections.OrderedDict()
        )

    def __len__(self) -> int:
        return len(self._entries)

    def _expired(self, entry: _SpecEntry) -> bool:
        return (time.time() - entry.timestamp) > self.ttl

    def _cancel(self, entry: _SpecEntry) -> None:
        if not entry.task.done():
            entry.task.cancel()

    def _discard(self, url: str, entry: _SpecEntry) -> None:
        self._entries.pop(url, None)
        self._cancel(entry)

    def __contains__(self, url: str) -> bool:
        entry = self._entries.get(url)
        if entry is None:
            return False
        if self._expired(entry):
            self._discard(url, entry)
            return False
        return True

    def get(self, url: str):
        """Peek without consuming; refreshes LRU position."""
        entry = self._entries.get(url)
        if entry is None:
            return None
        if self._expired(entry):
            self._discard(url, entry)
            return None
        self._entries.move_to_end(url)
        return entry

    def pop(self, url: str):
        """Consume-once take; returns None on miss or expiry."""
        entry = self._entries.pop(url, None)
        if entry is None:
            return None
        if self._expired(entry):
            self._cancel(entry)
            return None
        return entry

    def put(self, url: str, task) -> None:
        old = self._entries.pop(url, None)
        if old is not None:
            self._cancel(old)
        self._entries[url] = _SpecEntry(task, time.time())
        while len(self._entries) > self.max_entries:
            old_url, old_entry = self._entries.popitem(last=False)
            self._cancel(old_entry)

    def evict_expired(self) -> None:
        for url, entry in list(self._entries.items()):
            if self._expired(entry):
                self._discard(url, entry)

    def clear(self) -> None:
        for url, entry in list(self._entries.items()):
            self._discard(url, entry)


speculative_cache = SpeculativeCache(max_entries=SPECULATIVE_CACHE_MAX_ENTRIES)


# Bound concurrent speculative fetches so one page can't open unbounded
# outbound connections.
_spec_fetch_semaphore = asyncio.Semaphore(8)
# URLs with a fetch currently in flight -> task. Prevents duplicate
# prefetches of the same URL.
_spec_fetch_inflight: dict[str, asyncio.Task] = {}


def _mitm_headers_to_dict(headers: mitmproxy.http.Headers) -> dict[str, str]:
    return {name: value for name, value in headers.items()}


async def _spec_fetch(url: str, headers: dict[str, str]):
    """Single speculative fetch; returns (body, headers) or None."""
    session = get_session()
    async with _spec_fetch_semaphore:
        try:
            async with session.get(
                url, headers=headers, timeout=SPEC_FETCH_TIMEOUT
            ) as resp:
                body = await resp.read()
                return (body, dict(resp.headers))
        except Exception:
            return None


async def do_async_http_request(url: str, headers: mitmproxy.http.Headers):
    """Fetch a URL speculatively, de-duplicating in-flight fetches."""
    task = _spec_fetch_inflight.get(url)
    if task is None or task.done():
        _spec_fetch_inflight.pop(url, None)
        task = asyncio.ensure_future(
            _spec_fetch(url, _mitm_headers_to_dict(headers))
        )
        _spec_fetch_inflight[url] = task

        def _forget(t, _url=url):
            if _spec_fetch_inflight.get(_url) is t:
                _spec_fetch_inflight.pop(_url, None)

        task.add_done_callback(_forget)
    return await task


def check_dns_blocklist(host: str) -> bool:
    """Check host and its parent domains against the blocklist set.

    Matches the previous semantics (an entry matches when it equals the host
    or is a parent domain of it) but walks the host's labels instead of
    scanning the whole set: O(labels) instead of O(len(blocklist)).
    Hostnames are lowercased and stripped of any trailing dot because DNS
    is case-insensitive and blocklist entries are stored normalized.
    """
    if not DNS_BLOCKLIST or not host:
        return False
    host = host.lower().rstrip(".")
    if not host:
        return False
    labels = host.split(".")
    for i in range(len(labels)):
        if ".".join(labels[i:]) in DNS_BLOCKLIST:
            return True
    return False


async def request(flow: mitmproxy.http.HTTPFlow) -> None:
    # Do not process mitmproxy certificate installation page
    if flow.request.pretty_host == "mitm.it":
        return

    # Reject hosts that don't pass the DNS blocklist
    if BLOCK_ADS:
        if check_dns_blocklist(flow.request.pretty_host):
            flow.response = mitmproxy.http.Response.make(status_code=403)
            return

    ae = flow.request.headers.get("accept-encoding", "")
    if "zstd-d" in ae or "dcz" in ae or "dcb" in ae:
        encodings = [
            e.strip()
            for e in ae.split(",")
            if e.strip() not in ("zstd-d", "dcz", "dcb")
        ]
        flow.request.headers["accept-encoding"] = ", ".join(encodings)

    accept_hdr = flow.request.headers.get("accept", "")
    # Page navigations send "text/html,...,image/avif,image/webp,...".
    # Clobbering that to image-only makes strict servers answer 406
    # (and bot protection 403), so only narrow non-document requests.
    if "image" in accept_hdr and "text/html" not in accept_hdr:
        flow.request.headers["accept"] = "image/webp,image/avif,image/*;q=0.8"

    entry = speculative_cache.pop(flow.request.pretty_url)
    if entry is None:
        return
    if ENABLE_DEBUG:
        print("Cache hit:", flow.request.pretty_url)

    # Retrieve the prefetched response, awaiting the in-flight fetch if needed.
    try:
        result = await entry.task
    except (asyncio.CancelledError, Exception):
        return
    if not result:
        return
    body, res_headers = result

    headers = mitmproxy.http.Headers()
    for header in res_headers:
        headers[header] = res_headers[header]

    # Forge a fake flow response to make it compatible with optimization function.
    flow.response = mitmproxy.http.Response.make(
        status_code=200, content=body, headers=headers
    )
    flow.is_replay = "request"

    # Run the forged flow through the response handler to perform optimization.
    await response(flow)


def convert_webp(data: bytes) -> bytes:
    try:
        image = Image.open(io.BytesIO(data))
        if image.mode in ("CMYK", "P"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")

        output = io.BytesIO()
        image.save(
            output,
            format="WEBP",
            lossless=(IMAGE_QUALITY == 100),
            quality=IMAGE_QUALITY,
            alpha_quality=IMAGE_QUALITY,
            method=4,
            save_all=getattr(image, "is_animated", False),
        )
        return output.getvalue()
    except Exception:
        return data


def convert_avif(data: bytes) -> bytes:
  image = Image.open(io.BytesIO(data))
  output = io.BytesIO()
  image.save(
    output,
    format="AVIF",
    # AVIF needs a higher quality value than WebP for similar visual quality,
    # so map IMAGE_QUALITY onto a roughly equivalent AVIF quality (by SSIM).
    quality=round(24 + 0.6 * IMAGE_QUALITY),
    # Speed 8 is usually faster than WebP's method 6 and still produces smaller
    # files. Lower speeds compress a bit better, but are much slower.
    speed=8,
    save_all=True
  )
  return output.getvalue()


async def process_html(code: str, url: str) -> tuple[str, list]:
    try:
        session = get_session()
        async with session.post(
            f"{BUN_MINIFY_BASE_URL}/api/minify/html",
            json={"code": code, "url": url},
            timeout=MINIFY_TIMEOUT,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return (data.get("code", code), data.get("links", []))
    except Exception:
        pass
    return (code, [])


async def do_minify_css(code: str) -> str:
    try:
        session = get_session()
        async with session.post(
            f"{BUN_MINIFY_BASE_URL}/api/minify/css",
            data=code.encode("utf-8"),
            timeout=MINIFY_TIMEOUT,
        ) as resp:
            if resp.status == 200:
                return await resp.text()
    except Exception:
        pass
    return code


def do_minify_js(code: str) -> str:
    return code


async def do_minify_svg(code: str) -> str:
    try:
        session = get_session()
        async with session.post(
            f"{BUN_MINIFY_BASE_URL}/api/minify/svg",
            data=code.encode("utf-8"),
            timeout=MINIFY_TIMEOUT,
        ) as resp:
            if resp.status == 200:
                return await resp.text()
    except Exception:
        pass
    return code


def do_minify_json(code: str) -> str:
    try:
        return json.dumps(json.loads(code), separators=(",", ":"), check_circular=False)
    except:
        return code


def sizeof_fmt(num, suffix="B"):
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"


def count_savings(size_before: int, size_after: int) -> None:
    just_saved = size_before - size_after
    if not "data_saved" in globals():
        global data_saved, data_used
        data_saved = 0
        data_used = 0
    data_saved += just_saved
    data_used += size_before
    data_saved_fmt = sizeof_fmt(data_saved)
    data_used_fmt = sizeof_fmt(data_used)

    if ENABLE_GUI:
        global gui_root, saved_label_widget, used_label_widget
        if "saved_label_widget" in globals() and saved_label_widget:
            gui_root.after(
                0,
                lambda: {
                    saved_label_widget.config(text=("Data saved: " + data_saved_fmt)),
                    used_label_widget.config(text=("Data used: " + data_used_fmt)),
                },
            )

    if ENABLE_DEBUG:
        print("JUST SAVED: ", sizeof_fmt(just_saved))
        print("TOTAL SAVED:", sizeof_fmt(data_saved))


async def response(flow: mitmproxy.http.HTTPFlow) -> None:
    if not (flow.response and flow.response.raw_content):
        return

    if flow.request.method == "POST" or "X-Requested-With" in flow.request.headers:
        return

    status = flow.response.status_code
    if status == 206:
        return

    content_type = flow.response.headers.get("content-type", "application/octet-stream")
    mime_type = content_type.split(";")[0].strip().lower()

    if ENABLE_DEBUG:
        print(content_type)

    if (
        mime_type.startswith(ALREADY_COMPRESSED_TYPES)
        or "x-protobuf" in mime_type
        or "grpc" in mime_type
    ):
        return

    size_before = len(flow.response.raw_content)

    if not (MIN_PROCESSING_SIZE <= size_before <= MAX_PROCESSING_SIZE_MB * 1024 * 1024):
        return

    is_redirect = status in EMPTY_BODY_STATUSES
    is_http_error = CLEAR_HTTP_ERRORS and status >= 400 and status not in (404, 410)

    if is_redirect or is_http_error:
        flow.response.headers["content-length"] = "0"
        flow.response.headers.pop("content-encoding", None)
        flow.response.raw_content = b""
        return count_savings(size_before, 0)

    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=")[1].split(";")[0].strip()

    # Wire encoding before anything is mutated; used by the no-op fast path.
    original_encoding = flow.response.headers.get("content-encoding")

    try:
        decompressed = flow.response.content
    except Exception as e:
        if ENABLE_DEBUG:
            print("Failed to get content:", e)
        return
    output = decompressed

    if mime_type in IMAGE_TYPES or mime_type.startswith(IMAGE_TYPES):
        # Skip the re-encode when the image is already in the target format:
        # it only burns CPU (and at quality < 100 it even degrades the image).
        if mime_type == "image/webp" or (USE_AVIF and mime_type == "image/avif"):
            return count_savings(size_before, size_before)
        # Tiny images rarely shrink enough to justify a re-encode.
        if size_before < MIN_IMAGE_PROCESSING_SIZE:
            return count_savings(size_before, size_before)
        # Prefer AVIF when the client advertises support for it. Lossless output
        # (quality 100) stays WebP, as does anything AVIF fails to encode.
        output = None
        accept = flow.request.headers.get("accept") or ""
        if USE_AVIF and IMAGE_QUALITY < 100 and "image/avif" in accept:
          try:
            output = convert_avif(flow.response.content)
            flow.response.headers["content-type"] = "image/avif"
          except Exception as e:
            if ENABLE_DEBUG: print("AVIF conversion failed, using WebP:", e)
        if output is None:
          output = convert_webp(flow.response.content)
          flow.response.headers["content-type"] = "image/webp"
        flow.response.raw_content = output
        flow.response.headers.pop("content-encoding", None)
        flow.response.headers.pop("transfer-encoding", None)
        flow.response.headers["content-type"] = "image/webp"
        flow.response.headers["content-length"] = str(len(flow.response.raw_content))
        return count_savings(size_before, len(flow.response.raw_content))

    accept_encoding_hdr = flow.request.headers.get("accept-encoding", "")
    accepted_encodings = [
        a.strip() for a in accept_encoding_hdr.split(",") if a.strip()
    ]

    if mime_type.startswith("text/") and not FORCE_MAX_COMPRESSION:
        compression = flow.response.headers.get("content-encoding")
        if compression == "gzip" and not any(
            i in accepted_encodings for i in ["br", "zstd"]
        ):
            if len(flow.response.raw_content) > 8 and (
                flow.response.raw_content[8] & 2
            ):
                return count_savings(size_before, len(flow.response.raw_content))

    use_max_compression = FORCE_MAX_COMPRESSION
    is_binary_data = mime_type.startswith(("application/", "image/"))
    speculated_links = []

    if mime_type.startswith("text/html"):
        use_max_compression = True
    elif mime_type.startswith("text/css"):
        minified_css = await do_minify_css(output.decode(charset, errors="ignore"))
        output = minified_css.encode(charset)
    elif mime_type.startswith(("text/javascript", "application/javascript")):
        pass
    elif mime_type.startswith("application/json"):
        output = do_minify_json(output.decode(charset, errors="ignore")).encode(charset)
        is_binary_data = False
    elif mime_type.startswith("image/svg"):
        minified_svg = await do_minify_svg(output.decode(charset, errors="ignore"))
        output = minified_svg.encode(charset)
        is_binary_data = False

    # Fast path: minification was a no-op and the wire bytes are already in
    # an encoding the client accepts -> leave the body untouched instead of
    # paying for a decompress/recompress round-trip. Identity (uncompressed)
    # bodies still go through compression below, as before.
    if (
        output == decompressed
        and original_encoding is not None
        and original_encoding.lower() in [e.lower() for e in accepted_encodings]
    ):
        return count_savings(size_before, size_before)

    flow.response.headers.pop("transfer-encoding", None)

    if USE_SPECULATIVE_CACHE and not flow.is_replay:
        for link in speculated_links:
            if ENABLE_DEBUG:
                print("Caching", link)
            task = asyncio.ensure_future(
                do_async_http_request(link, flow.request.headers)
            )
            speculative_cache.put(link, task)

    if ENABLE_DEBUG:
        print("Algorithm comparison:")
        orig_len = len(flow.response.raw_content)
        benchmarks = [
            (
                "br",
                lambda d: br.compress(
                    d, br.MODE_GENERIC, 11 if use_max_compression else 4
                ),
            ),
            ("gzip", lambda d: gzip.compress(d, 9 if use_max_compression else 6)),
            ("zstd", lambda d: zstd.compress(d, 22 if use_max_compression else 12)),
            ("deflate", lambda d: deflate.compress(d, 9 if use_max_compression else 6)),
        ]
        for name, comp_fn in benchmarks:
            start = time.time()
            res = comp_fn(output)
            print(
                f"  {name}: {sizeof_fmt(orig_len - len(res))} {time.time() - start:.4f}s"
            )

    raw_output = output
    encoding = "identity"

    if "gzip" in accepted_encodings:
        raw_output = gzip.compress(output, 9 if use_max_compression else 6)
        encoding = "gzip"
    elif "deflate" in accepted_encodings:
        raw_output = deflate.compress(output, 9 if use_max_compression else 6)
        encoding = "deflate"
    elif "br" in accepted_encodings and (
        not is_binary_data or accepted_encodings == ["br"]
    ):
        mode = (
            br.MODE_TEXT
            if mime_type.startswith("text/")
            else (br.MODE_FONT if mime_type.startswith("font/") else br.MODE_GENERIC)
        )
        raw_output = br.compress(output, mode, 4)
        encoding = "br"
    elif "zstd" in accepted_encodings:
        raw_output = zstd.compress(output, 22 if use_max_compression else 12)
        encoding = "zstd"

    if len(raw_output) < len(flow.response.raw_content):
        flow.response.headers["content-length"] = str(len(raw_output))
        flow.response.headers["content-encoding"] = encoding
        flow.response.raw_content = raw_output
    elif ENABLE_DEBUG:
        print(
            "SKIPPING, RECOMPRESSED OUTPUT IS LARGER:",
            len(raw_output),
            ">",
            len(flow.response.raw_content),
        )

    return count_savings(size_before, len(flow.response.raw_content))


def dns_request(flow: mitmproxy.dns.DNSFlow) -> None:
    if not flow.request.question:
        return

    if BLOCK_ADS:
        if check_dns_blocklist(str(flow.request.question)):
            flow.response = flow.request.fail(mitmproxy.dns.response_codes.NXDOMAIN)


def _load_and_update_dns_blocklist():
    global DNS_BLOCKLIST

    if os.path.exists(BLOCKLIST_FILE):
        try:
            with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
                DNS_BLOCKLIST = {
                    line.strip().lower()
                    for line in f
                    if line.strip() and not line.startswith("#")
                }
            if ENABLE_DEBUG:
                print(f"Loaded {len(DNS_BLOCKLIST)} domains from local cache (background)")
        except Exception as e:
            if ENABLE_DEBUG:
                print("Error reading local blocklist:", e)

    file_age = (
        time.time() - os.path.getmtime(BLOCKLIST_FILE)
        if os.path.exists(BLOCKLIST_FILE)
        else float("inf")
    )
    if file_age > CACHE_TTL_SECONDS:
        url = "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/wildcard/pro-onlydomains.txt"
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                with open(BLOCKLIST_FILE, "w", encoding="utf-8") as f:
                    f.write(res.text)

                DNS_BLOCKLIST = {
                    s.strip().lower()
                    for s in res.text.split("\n")
                    if s.strip() and not s.startswith("#")
                }
                if ENABLE_DEBUG:
                    print(f"DNS Blocklist updated from network: {len(DNS_BLOCKLIST)} domains")
        except Exception as e:
            if ENABLE_DEBUG:
                print("Offline or network error, using cached blocklist:", e)


def load_dns_blocklist():
  threading.Thread(target=_load_and_update_dns_blocklist, daemon=True).start()


def load(loader: mitmproxy.addonmanager.Loader):
    if ENABLE_GUI:
        gui_thread = threading.Thread(target=start_gui, daemon=True)
        gui_thread.start()

    bun_binary_name = "bun-api/bun" if os.name == "posix" else "bun-api/bun.exe"
    if os.path.exists(bun_binary_name):
        global bun_api_process
        bun_api_process = subprocess.Popen([bun_binary_name, "run", "bun-api/index.ts"])
    else:
        print("Warning: Could not find Bun API binary - please start it manually.")

    # wan_ip_req = requests.get("https://api.ipify.org")
    # if wan_ip_req.status_code != 200 or not wan_ip_req.text:
    #   wan_ip_req = requests.get("https://api.seeip.org")
    # wan_ip = wan_ip_req.text
    #   with open("wg-keys.json", "r") as keys_file:
    #     wg_keys = json.loads(keys_file.read())
    #     config = f"""\
    # # This file was automatically generated.
    # # To change keys, edit `wg-keys.json` instead.

    # [Interface]
    # PrivateKey = {wg_keys["client_key"]}
    # Address = 10.0.0.1/32
    # DNS = 10.0.0.53

    # [Peer]
    # PublicKey = {mitmproxy_rs.wireguard.pubkey(wg_keys["server_key"])}
    # AllowedIPs = 0.0.0.0/0
    # Endpoint = {wan_ip}:51820"""
    #     # Write config to file
    #     with open("wireguard.cfg", "w") as config_file:
    #       config_file.write(config)

    load_dns_blocklist()


async def done():
    global ENABLE_GUI, gui_root
    if ENABLE_GUI and "gui_root" in globals() and gui_root:
        print("destroying")
        gui_root.after(0, lambda: {gui_root.destroy()})
    global bun_api_process
    if "bun_api_process" in globals():
        bun_api_process.kill()
    # Drop speculative state (cancelling in-flight fetches) and close the
    # shared outbound session. mitmproxy awaits async hooks.
    speculative_cache.clear()
    await close_session()
