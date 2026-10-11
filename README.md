## Arachnidium — Fast Fork

**An AI-generated fast fork of [Arachnidium](https://github.com/p2r3/Arachnidium)** — the data-optimizing HTTP(S) proxy, powered by [mitmproxy](https://www.mitmproxy.org/).

Everything below works as in upstream. The difference is speed: this fork reworks the hot paths so pages load fast on real hardware.

### What got faster

- **No more event-loop stalls**: image re-encoding (WebP/AVIF) and recompression (gzip/deflate/brotli/zstd) run on worker threads. This fixed 100% CPU on image-heavy pages. Max-mode levels tuned for speed: gzip/deflate 7, zstd 8, brotli 4.
- **Smarter skips**: webp→webp and avif→avif re-encodes skipped, images under 1 KB pass through untouched, and responses that gain nothing from recompression keep their original wire encoding.
- **Shared async HTTP**: one long-lived session (with connection limits) serves the Bun minifier and speculative prefetching; speculative fetches are capped at 8 concurrent with per-URL de-duplication.
- **Faster lookups**: ad-blocklist checks walk parent domains against the set (~10,800× faster at 50k entries); the speculative cache is a bounded LRU with O(1) eviction (~51× faster).
- **Hardened**: 42 automated tests, before/after benchmarks in `benchmarks/`, plus two correctness fixes the code needed (it didn't compile; the module couldn't be imported standalone).

Full numbers: [`benchmarks/results/RESULTS.md`](benchmarks/results/RESULTS.md). The work lives on the `perf-improvements` branch.

## Setup

0. Find a home server - this can be any internet-connected computer, like an old laptop. Currently, pre-built releases exist for x64 Windows and Linux. If you need ARM or Mac support, clone the repository and run it from source.
1. On your home server, download the portable archive for your system in [Releases](https://github.com/p2r3/Arachnidium/releases/tag/latest).
2. Extract the archive and run `Arachnidium.exe` (or `Arachnidium` on Linux).
3. Install the WireGuard client on your device: [Android](https://play.google.com/store/apps/details?id=com.wireguard.android&pli=1) | [iOS](https://apps.apple.com/us/app/wireguard/id1441195209)
4. Add the VPN preset using the QR code displayed in Arachnidium. Alternatively, use the automatically generated `wireguard.cfg` file.
5. This is where things get a little hairy - if you want to actually use this outside of your home WiFi, you'll have to configure your network to listen for incoming VPN connections. To do this, you will have to forward **UDP** port **51820** from your home server to your router. The exact process for this is vastly different for every network and router, and it would be impossible to give a universal guide in this document, so please research this yourself. Searching for "port forward udp \<router model\>" should be enough to get you on the right track. Alternatively, call your internet service provider and ask if they can help.
6. Connect to the VPN and visit `mitm.it` on your phone's browser. Follow the instructions there to set up the certificate authority. ***Make sure to read all of the instructions.***

If you run into issues, please make sure you've followed these steps carefully. Do your own troubleshooting first to make sure the issue isn't on your end (e.g. misconfigured network).

## Configuration

There are two ways to configure Arachnidium: via the graphical interface, or by editing `defaults.json`. Changes made in the GUI are not saved between restarts - for that, use the JSON file. If you want to run Arachnidium without the GUI, set `ENABLE_GUI` to `false` in `defaults.json`.

## Running from source

To run this project from source code without building it (for development purposes, or to run on unsupported platforms):

1. Install Python and [uv](https://docs.astral.sh/uv/).
2. Install [Bun](https://bun.sh/).
3. Clone this repository with submodules (using `--recursive`).
4. Run `uv run main.py`
5. In another terminal, change directory to `bun-api` and run `bun i`, then `bun run index.ts`

## Acknowledgements

- https://www.mitmproxy.org/
- https://github.com/rdbende/Azure-ttk-theme
- Upstream project: https://github.com/p2r3/Arachnidium

The name "Arachnidium" is a homage to nature's earliest web developers. The arachnidium is, generally, [the part of a spider's body responsible for producing webs](https://www.merriam-webster.com/dictionary/arachnidium). It also sounds really cool.
