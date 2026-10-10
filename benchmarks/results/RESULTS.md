# Benchmark results — Arachnidium performance work

Environment: Linux x86_64, Python 3.12, mitmproxy 12.2.3, Pillow 12.3.
"Before" = HEAD (461746a) + the pre-existing indentation fix (the file does
not compile without it); "after" = this branch. A stub Bun minify API ran on
localhost:3000 for both. Each fixture: 30 iterations (8 for images) after
warmup; wall = per-`response()` wall time, CPU = `process_time()` per response.

## End-to-end `response()` (before → after)

| fixture | p50 ms | p95 ms | cpu ms/resp | bytes out |
|---|---|---|---|---|
| html-200k | 4.07 → 3.25 | 5.91 → 3.79 | 4.88 → 4.13 | 6812 → 6812 |
| css-50k | 5.13 → 4.13 | 13.23 → 4.55 | 5.86 → 4.98 | 8262 → 8262 |
| js-identity-60k | 0.80 → 0.65 | 1.10 → 0.74 | 1.81 → 1.41 | 4191 → 4191 |
| js-gzip-60k | 0.81 → **0.21** | 1.08 → 0.26 | 1.69 → 1.00 | 4191 → 4201 |
| jpeg-photo | 243 → 193 | 334 → 244 | 204 → 198 | 163698 → 163698 |
| png-noisy | 120 → 114 | 125 → 119 | 118 → 115 | 99972 → 99972 |
| webp-input | 148 → **0.04** | 210 → 0.04 | 138 → 0.63 | 88048 → 180096 |
| png-small (3.1 KB) | 1.06 → 0.97 | 3.12 → 0.98 | 1.88 → 1.70 | 498 → 498 |

Notes:
- **js-gzip-60k**: the no-op fast path skips the decompress→recompress
  round-trip (~4x faster). Output is byte-identical to the wire bytes
  (+10 B vs a fresh gzip-6 encode — negligible).
- **webp-input**: webp→webp re-encode skipped (~3700x faster). The old output
  was *smaller* only because it lossy-re-encoded an 80-quality WebP at
  quality 20 — i.e. it was degrading the image. Passthrough is byte-identical
  and preserves quality.
- Image conversion times (jpeg/png) vary ±50 ms run-to-run on this box;
  small deltas there are noise. Outputs are byte-identical before/after.
- Repeated runs confirmed the js-identity delta is noise (second run:
  0.80 → 0.65 ms in favour of "after").

Raw JSON: `response-before.json`, `response-after.json`.

## Micro-benchmarks (after tree; old algorithm reimplemented inline)

- **Blocklist lookup**, 200 lookups against 50,002 entries:
  old O(n) scan 5461.1 ms → new parent-domain walk **0.5 ms**
  (**~10,800x**). Correctness cross-checked against the old scan on 2000
  random hosts.
- **Speculative-cache eviction**, 3000 puts at 1000 max entries:
  old `min()` full scan 782.7 ms → new LRU `popitem` **15.4 ms** (**~51x**).

Raw JSON: `micro-after.json`.

## Image size-threshold validation (icon-like images, WebP method 4)

| px | png B | webp B | saved B | cpu ms | B/ms |
|---|---|---|---|---|---|
| 16 | 205 | 132 | 73 | 12.43 (first-call warmup) | 6 |
| 24 | 284 | 224 | 60 | 0.74 | 81 |
| 32 | 369 | 220 | 149 | 0.71 | 209 |
| 48 | 626 | 264 | 362 | 1.01 | 360 |
| 64 | 908 | 298 | 610 | 1.41 | 432 |
| 96 | 1721 | 454 | 1267 | 2.50 | 506 |
| 128 | 2789 | 510 | 2279 | 3.64 | 626 |

Bytes-saved-per-ms collapses below ~1 KB (flat ~0.7–1 ms PIL overhead for
tens of bytes saved). `MIN_IMAGE_PROCESSING_SIZE = 1024` keeps everything
with a healthy payoff and passes through the poor-tradeoff zone.
