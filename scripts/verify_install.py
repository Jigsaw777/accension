"""No-cost installed-system evidence. Writes metadata only to docs/verification.json."""

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

from local_ai_router.config import Repository, load
from local_ai_router.doctor import doctor
from local_ai_router.engine import Engine
from local_ai_router.schema import Request

ROOT = Path(__file__).resolve().parents[1]


async def main():
    real = Engine(load(ROOT))
    try:
        checks = await doctor(real)
        checks["discovery"] = await real.discovery.refresh(True)
    finally:
        await real.close()
    settings = load(ROOT, mock=True)
    repo = ROOT / "examples/demo-repo"
    repo.mkdir(parents=True, exist_ok=True)
    settings.repositories = [
        Repository(path=str(repo), validation={"tests": [sys.executable, "-m", "unittest", "discover", "-v"]})
    ]
    engine = Engine(settings)
    try:
        demo = await engine.run(
            Request(
                task="Add a greeting feature with named and blank input tests and usage documentation",
                repo_path=str(repo),
            )
        )
        trace = engine.store.traces(demo["request_id"])
        await engine.route("Rename this variable.")
        timings = []
        for _ in range(100):
            start = time.perf_counter()
            await engine.route("Rename this variable.")
            timings.append((time.perf_counter() - start) * 1000)
    finally:
        await engine.close()
    autostart = False
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                value, _ = winreg.QueryValueEx(key, "LocalAIRouter")
                autostart = str(ROOT) in value
        except OSError:
            pass
    evidence = {
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "doctor": checks,
        "autostart_matches_installation": autostart,
        "mock_e2e": demo,
        "example_trace": trace,
        "cached_trivial_route_ms": {
            "samples": 100,
            "median": round(statistics.median(timings), 3),
            "max": round(max(timings), 3),
        },
        "limitations": [
            "Mock inference fixture; Python checks execute locally",
            "No live cloud/Qwen inference",
            "Desktop UI invocation requires client reload",
        ],
    }
    (ROOT / "docs/verification.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "evidence": "docs/verification.json",
                "gateway": checks["gateway"],
                "demo": demo["status"],
                "request_id": demo["request_id"],
                "cost": demo["costs"]["estimated_usd"],
                "autostart": autostart,
                "median_route_ms": evidence["cached_trivial_route_ms"]["median"],
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
