"""Load test of the dashboard API (Phase 8).

Starts the real ASGI app under uvicorn in this process and drives it with concurrent HTTP
clients (httpx, async) at several concurrency levels for a fixed duration each, over a mix
of read endpoints. Records throughput, latency percentiles and error/throttle counts, plus
the conditions (hardware, Python, backend, data volume). Two backends:

* ``--backend demo``      the isolated synthetic demo (in memory): API + serialisation cost.
* ``--backend database``  the configured database (TREND_AGENT_DATABASE_URL).

The per-client rate limit is raised for the capacity runs (``--rate-limit``); a separate
run at the configured limit verifies that throttling engages (HTTP 429 + Retry-After).
This is a single-host test: it measures this machine, not a production cluster.

    python scripts/load_test.py --backend demo --duration 10 --out docs/benchmarks/phase8_load_demo.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trend_agent.api.app import create_app  # noqa: E402
from trend_agent.config import Settings  # noqa: E402

TOKEN = "load-test-token-0123456789abcdef0123456789"
PATHS = ["/api/overview", "/api/opportunities?page_size=25", "/api/recommendations?page_size=25",
         "/api/trends", "/api/system/mode", "/health/ready"]  # fmt: skip


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def build(backend: str, rate_limit: int) -> tuple[Any, Any, dict[str, Any]]:
    from trend_agent.container import build_container

    settings = Settings(api_token=TOKEN, demo_mode=backend == "demo")  # type: ignore[call-arg]
    container = build_container(settings)
    be = container.dashboard_backend()
    loaded = getattr(be, "loaded", None)
    if loaded is not None:
        api = loaded.config.api.model_copy(update={"rate_limit_per_minute": rate_limit})
        be.loaded = loaded.model_copy(update={"config": loaded.config.model_copy(update={"api": api})})
    info: dict[str, Any] = {"backend": backend}
    if backend == "database":
        import sqlalchemy as sa

        with container.engine.connect() as c:
            info["rows"] = {t: int(c.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one())  # noqa: S608
                            for t in ("ts_observations", "content_opportunities",
                                      "video_recommendations", "surge_events")}  # fmt: skip
    app = create_app(be, settings, ops=None if backend == "demo" else container)
    return app, container, info


def serve(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, t


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))] * 1000, 2)


async def run_level(base: str, concurrency: int, duration: float) -> dict[str, Any]:
    lat: list[float] = []
    codes: dict[int, int] = {}
    errors = 0
    stop = time.perf_counter() + duration
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async def worker(i: int) -> None:
        nonlocal errors
        async with httpx.AsyncClient(base_url=base, timeout=30) as c:
            k = i
            while time.perf_counter() < stop:
                path = PATHS[k % len(PATHS)]
                k += 1
                t0 = time.perf_counter()
                try:
                    r = await c.get(path, headers=headers)
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                    if r.status_code < 400:
                        lat.append(time.perf_counter() - t0)
                except httpx.HTTPError:
                    errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    elapsed = time.perf_counter() - t0
    ok = sum(n for c, n in codes.items() if c < 400)
    return {"concurrency": concurrency, "seconds": round(elapsed, 2), "requests_ok": ok,
            "rps": round(ok / elapsed, 1), "p50_ms": pct(lat, 0.5), "p95_ms": pct(lat, 0.95),
            "p99_ms": pct(lat, 0.99), "status_counts": codes, "client_errors": errors}  # fmt: skip


async def throttle_check(base: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        codes: dict[int, int] = {}
        retry_after = None
        for _ in range(300):
            r = await c.get("/api/system/mode", headers=headers)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
    return {"requests": 300, "status_counts": codes, "retry_after": retry_after}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("demo", "database"), default="demo")
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--levels", default="1,8,32")
    ap.add_argument("--rate-limit", type=int, default=1_000_000)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    report: dict[str, Any] = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "conditions": {"python": platform.python_version(), "platform": platform.platform(),
                       "cpus": os.cpu_count(), "server": "uvicorn, 1 process, in-process client",
                       "duration_per_level_s": args.duration, "paths": PATHS},  # fmt: skip
        "levels": [],
    }
    app, container, info = build(args.backend, args.rate_limit)
    report["conditions"] |= info
    port = free_port()
    server, thread = serve(app, port)
    base = f"http://127.0.0.1:{port}"
    try:
        for level in (int(x) for x in args.levels.split(",")):
            res = asyncio.run(run_level(base, level, args.duration))
            report["levels"].append(res)
            print(json.dumps(res))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    # throttling at the configured per-client limit
    app2, c2, _ = build(args.backend, 240)
    port2 = free_port()
    server2, thread2 = serve(app2, port2)
    try:
        report["throttling_at_240_per_minute"] = asyncio.run(throttle_check(f"http://127.0.0.1:{port2}"))
        print(json.dumps(report["throttling_at_240_per_minute"]))
    finally:
        server2.should_exit = True
        thread2.join(timeout=10)
    for c in (container, c2):
        c.engine.dispose()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
