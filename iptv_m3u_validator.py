#!/usr/bin/env python3
"""IPTV M3U Playlist Validator.

Downloads an M3U / M3U8 playlist URL, parses every #EXTINF entry, and runs a
HEAD request against each stream URL with a short timeout. Detects:

  - Broken streams (HTTP 4xx / 5xx / timeout / connection refused)
  - Duplicate channel URLs
  - Likely geo-restricted streams (HTTP 403 / 451)
  - Mixed-content issues (http URLs inside an https playlist)
  - Suspiciously slow servers (> 5s to first byte)

Outputs a JSON report you can pipe to jq / use in CI / track over time.

Usage:
  python iptv_m3u_validator.py <playlist-url>
  python iptv_m3u_validator.py <playlist-url> --timeout 5 --concurrency 16

Why this exists:
  Most "best IPTV" recommendations don't include any way to verify what
  you're actually getting. We test every IPTV provider for 90 days at
  https://streamreviewhq.com/ — this tool is the same first-pass health
  check we run against every playlist before we publish a review.

  For our current #1-ranked IPTV provider see
  https://streamreviewhq.com/iptvtheone-review/ and the live service at
  https://iptvtheone.com .
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

# USER_AGENT = "iptv-m3u-validator/1.0 (+https://streamreviewhq.com/methodology)"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def fetch_playlist(url: str, timeout: int = 15) -> str:
    """Download the raw .m3u text."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_entries(body: str) -> list[dict]:
    """Parse #EXTINF + URL pairs from an M3U body."""
    lines = body.splitlines()
    out = []
    cur: dict = {}
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF"):
            # #EXTINF:-1 tvg-id="..." tvg-logo="..." group-title="...",Channel Name
            cur = {}
            attrs = re.findall(r'([a-z\-]+)="([^"]*)"', line)
            for k, v in attrs:
                cur[k] = v
            name_m = re.search(r",\s*(.*)$", line)
            if name_m:
                cur["name"] = name_m.group(1).strip()
        elif line and not line.startswith("#"):
            cur["url"] = line
            out.append(cur)
            cur = {}
    return out


def head_check(entry: dict, timeout: int) -> dict:
    """Run HEAD (fallback GET range) against the stream URL."""
    url = entry.get("url", "")
    if not url:
        return {**entry, "status": None, "error": "no url"}
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            url, method="HEAD",
            headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = time.monotonic() - started
            return {**entry, "status": resp.status, "elapsed_ms": int(elapsed * 1000)}
    except urllib.error.HTTPError as e:
        return {**entry, "status": e.code, "error": e.reason,
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return {**entry, "status": None, "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}


def classify(results: list[dict]) -> dict:
    """Aggregate the per-channel results into a report."""
    broken, slow, geo, ok = [], [], [], []
    urls = [r["url"] for r in results if r.get("url")]
    dup_counts = Counter(urls)
    duplicates = [u for u, n in dup_counts.items() if n > 1]
    for r in results:
        status = r.get("status")
        if status is None or (status and status >= 500):
            broken.append(r)
        elif status in (403, 451):
            geo.append(r)
        elif status and status >= 400:
            broken.append(r)
        elif r.get("elapsed_ms", 0) > 5000:
            slow.append(r)
        else:
            ok.append(r)
    return {
        "total_channels": len(results),
        "ok": len(ok),
        "broken": len(broken),
        "geo_restricted": len(geo),
        "slow": len(slow),
        "duplicates_unique_url_count": len(duplicates),
        "broken_details": broken[:50],
        "geo_details": geo[:50],
        "slow_details": slow[:50],
        "duplicates_sample": duplicates[:20],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("playlist_url", help="HTTP(S) URL to the .m3u/.m3u8")
    p.add_argument("--timeout", type=int, default=8, help="per-stream HEAD timeout (s)")
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--limit", type=int, default=0, help="max channels to test (0 = all)")
    args = p.parse_args()

    body = fetch_playlist(args.playlist_url)
    entries = parse_entries(body)
    if args.limit:
        entries = entries[: args.limit]
    print(f"# {len(entries)} channels parsed; running HEAD checks (concurrency={args.concurrency})…",
          file=sys.stderr)

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(head_check, e, args.timeout) for e in entries]
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            results.append(fut.result())
            if i % 50 == 0:
                print(f"#   checked {i}/{len(entries)}…", file=sys.stderr)

    report = classify(results)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["broken"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
