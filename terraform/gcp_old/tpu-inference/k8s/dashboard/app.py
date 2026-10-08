#!/usr/bin/env python3
"""Health of the kube TPU CI fleet, as two pages.

Buildkite's queue page cannot show the kube fleet by topology. Every kube step
sits on the one `kube` queue, and its topology is chosen later, by the
launcher, as a Kueue queue. These pages are the view an on-call, a maintainer
and an owner each need:

- Live (/): health checks from Buildkite to a TPU pod, quota and borrowing per
  queue, the workloads and builds each queue holds and is waiting on and why,
  cluster events, and kube steps Buildkite has not handed over yet.
- History (/history): for any range, chips admitted against chips busy,
  outcomes split into test and infrastructure failures, wait, startup and run
  percentiles, and per-queue usage and backlog.

fleet.py reads Kueue and Kubernetes through Connect Gateway, Buildkite, Cloud
Monitoring and BigQuery; views.py turns that into the pages, charts.py draws.

Nothing polls in the background of an idle instance. A live view is served the
last snapshot and a stale one is refreshed behind it, so a dashboard nobody is
looking at makes no API calls - which matters for Buildkite, whose rate limit
the whole org shares. History is fetched when asked for and cached.

Run locally against the live fleet, with your own credentials:

    ./app.py --local     # gcloud for Google APIs, `bk api` for Buildkite
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fleet
import views

STATIC = Path(__file__).parent / "static"
CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
MAX_HISTORY_SECONDS = 90 * 86400


class Source:
    """One upstream, its last good result, and why the latest fetch failed."""

    def __init__(self, name: str, fetch, ttl: float) -> None:
        self.name = name
        self.fetch = fetch
        self.ttl = ttl
        self.data: dict | None = None
        self.at = 0.0
        self.error = ""
        self._lock = threading.Lock()

    def refresh_if_stale(self) -> None:
        with self._lock:
            if self.data is not None and time.time() - self.at < self.ttl:
                return
            try:
                self.data, self.at, self.error = self.fetch(), time.time(), ""
            except Exception as e:  # noqa: BLE001 - shown on the page, not raised
                self.error = f"{type(e).__name__}: {e}"[:300]
                # The page has one line; the log has the whole story.
                print(f"{self.name} fetch failed:", file=sys.stderr)
                traceback.print_exc()

    def status(self) -> dict:
        return {"name": self.name, "at": self.at, "error": self.error}


class Live:
    def __init__(self, cfg: fleet.Config) -> None:
        self.cfg = cfg
        pipelines = fleet.Cached(
            lambda: {
                p["slug"]
                for p in fleet.buildkite_pages(cfg, "/pipelines")
                if p.get("cluster_id") == cfg.cluster_id
            },
            3600,
        )
        ttl = cfg.cache_seconds
        self.sources = {
            "kueue": Source("Kueue", lambda: fleet.fetch_kueue(cfg), ttl),
            "buildkite": Source(
                "Buildkite", lambda: fleet.fetch_buildkite(cfg, pipelines), ttl
            ),
            "events": Source("Events", lambda: fleet.fetch_events(cfg), ttl),
            "health": Source("Metrics", lambda: fleet.fetch_health(cfg), ttl),
            # Node pools change when Terraform does; an hour is soon enough.
            "pools": Source("GKE", lambda: fleet.fetch_node_pools(cfg), 3600),
            # The health checks' 24-hour failure counts; BigQuery need not be
            # asked every minute.
            "stats24": Source(
                "BigQuery",
                lambda: fleet.fetch_stats(
                    cfg, int(time.time()) - 86400, int(time.time())
                ),
                300,
            ),
        }
        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self._at = 0.0
        self._refreshing = False

    def _refresh(self) -> None:
        try:
            with concurrent.futures.ThreadPoolExecutor(len(self.sources)) as pool:
                list(pool.map(Source.refresh_if_stale, self.sources.values()))
            snapshot = views.build_live(
                self.cfg,
                {k: s.data for k, s in self.sources.items()},
                {k: s.status() for k, s in self.sources.items()},
            )
            with self._lock:
                self._snapshot, self._at = snapshot, time.time()
        finally:
            with self._lock:
                self._refreshing = False

    def snapshot(self) -> dict:
        # A refresh takes seconds - most of it Buildkite - so a view is served
        # the last snapshot and a stale one is refreshed behind it. Only the
        # first view after a cold start waits. The page states its own age.
        with self._lock:
            snapshot = self._snapshot
            stale = snapshot is None or time.time() - self._at > self.cfg.cache_seconds
            start = stale and not self._refreshing
            if start:
                self._refreshing = True
        if snapshot is None:
            if start:
                self._refresh()
            while self._snapshot is None and self._refreshing:
                time.sleep(0.2)
            return self._snapshot
        if start:
            threading.Thread(target=self._refresh, daemon=True).start()
        return snapshot


class History:
    """History views by range, cached: a range ending now for a minute, a range
    wholly in the past for an hour."""

    def __init__(self, cfg: fleet.Config, live: Live) -> None:
        self.cfg = cfg
        self.live = live
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[dict, dict, float, float]] = {}

    def get(self, span: dict) -> tuple[dict, dict]:
        key = (span["start"], span["end"], span["step"])
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[2] < hit[3]:
                return hit[0], hit[1]
        # The queue list, cohort membership and node pools are as they are now.
        kueue, pools = self.live.sources["kueue"], self.live.sources["pools"]
        kueue.refresh_if_stale()
        pools.refresh_if_stale()
        sources = {"kueue": kueue.status(), "pools": pools.status()}
        calls = {
            "history": (
                "Metrics",
                lambda: fleet.fetch_history(
                    self.cfg, span["start"], span["end"], span["step"]
                ),
            ),
            "stats": (
                "BigQuery",
                lambda: fleet.fetch_stats(self.cfg, span["start"], span["end"]),
            ),
        }
        with concurrent.futures.ThreadPoolExecutor(len(calls)) as pool:
            futures = {k: pool.submit(fetch) for k, (_, fetch) in calls.items()}
        results = {}
        for k, future in futures.items():
            try:
                results[k], error = future.result(), ""
            except Exception as e:  # noqa: BLE001 - shown on the page, not raised
                results[k], error = {}, f"{type(e).__name__}: {e}"[:300]
                print(f"{calls[k][0]} history fetch failed:", file=sys.stderr)
                traceback.print_exception(type(e), e, e.__traceback__)
            sources[k] = {"name": calls[k][0], "at": time.time(), "error": error}
        view = views.build_history(
            self.cfg,
            (kueue.data or {}).get("queues", []),
            pools.data or [],
            results["history"],
            results["stats"],
            span,
        )
        ttl = (
            self.cfg.cache_seconds
            if time.time() - span["end"] < 2 * span["step"]
            else 3600
        )
        with self._lock:
            if len(self._cache) > 16:
                self._cache.clear()
            self._cache[key] = (view, sources, time.time(), ttl)
        return view, sources


def parse_span(params: dict, now: float) -> dict:
    """A preset (?preset=7d) or explicit epoch seconds (?start=..&end=..), the
    latter capped at now and at 90 days, both aligned to the chart step."""
    preset = None
    try:
        start, end = int(params["start"][0]), int(params["end"][0])
        end = min(end, int(now))
        start = max(min(start, end - 3600), end - MAX_HISTORY_SECONDS)
    except (KeyError, ValueError, IndexError):
        preset = params.get("preset", ["24h"])[0]
        if preset not in views.PRESETS:
            preset = "24h"
        end = int(now)
        start = end - views.PRESETS[preset]
    step = views.choose_step(end - start)
    return {
        "start": start - start % step,
        "end": end - end % step,
        "step": step,
        "preset": preset,
    }


def serve(live: Live, history: History, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            url = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(url.query)
            if url.path == "/healthz":
                return self.reply(200, "text/plain", b"ok")
            # /overview as well, for links made before it became the front page.
            if url.path in ("/", "/overview"):
                page = views.render_overview(live.snapshot())
                return self.reply(200, "text/html; charset=utf-8", page.encode())
            if url.path == "/baseline":
                page = views.render_baseline()
                return self.reply(200, "text/html; charset=utf-8", page.encode())
            if url.path == "/live":
                page = views.render_live(live.snapshot())
                return self.reply(200, "text/html; charset=utf-8", page.encode())
            if url.path == "/api/live":
                body = json.dumps(live.snapshot(), indent=1)
                return self.reply(200, "application/json", body.encode())
            if url.path in ("/history", "/api/history"):
                view, sources = history.get(parse_span(params, time.time()))
                if url.path == "/api/history":
                    return self.reply(
                        200, "application/json", json.dumps(view, indent=1).encode()
                    )
                page = views.render_history(
                    view, sources, f"?{url.query}" if url.query else ""
                )
                return self.reply(200, "text/html; charset=utf-8", page.encode())
            if url.path.startswith("/static/"):
                path = (STATIC / url.path.removeprefix("/static/")).resolve()
                if (
                    path.parent == STATIC.resolve()
                    and path.suffix in CONTENT_TYPES
                    and path.is_file()
                ):
                    return self.reply(
                        200, CONTENT_TYPES[path.suffix], path.read_bytes(), cache=300
                    )
            return self.reply(404, "text/plain", b"not found")

        def reply(self, code: int, kind: str, body: bytes, cache: int = 0) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Cache-Control", f"max-age={cache}" if cache else "no-store"
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            # One line per request on stderr, which Cloud Run collects.
            print(f"{self.command} {self.path} {fmt % args}", flush=True)

    ThreadingHTTPServer(("", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--local",
        action="store_true",
        help="use gcloud and the bk CLI for credentials instead of the metadata server",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    fleet.LOCAL = args.local
    cfg = fleet.Config()
    live = Live(cfg)
    serve(live, History(cfg, live), args.port)


if __name__ == "__main__":
    main()
