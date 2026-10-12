"""Repeatable 10k-item/100k-event query, payload, wake and resource measurements."""

import asyncio
import json
import os
import platform
import time
import tracemalloc
from uuid import UUID

from test_workbench_agents import HUMAN

from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.store import identity


def process_memory_bytes():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak",
                    "working_set",
                    "quota_peak_paged",
                    "quota_paged",
                    "quota_peak_nonpaged",
                    "quota_nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.working_set
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if platform.system() == "Darwin" else value * 1024


async def test_large_board_pages_indexed_bounded_and_no_inference(engine, monkeypatch, tmp_path):
    runtime = WorkbenchRuntime(engine)
    ws = runtime.service.seed_defaults()
    project = runtime.service.create(
        "project", {"command_id": identity(), "workspace_id": ws.id, "name": "Scale", "key": "SC"}, HUMAN
    ).body
    sample = runtime.service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "project_id": project["id"],
            "title": "Scale 0",
            "execution_kind": "MANUAL",
        },
        HUMAN,
    ).body
    db = runtime.service.store.db
    columns = [row[1] for row in db.execute("PRAGMA table_info(wb_work_items)")]
    original = dict(db.execute("SELECT * FROM wb_work_items WHERE id=?", (sample["id"],)).fetchone())
    rows, events = [], []
    stamp = time.perf_counter()
    for number in range(1, 10000):
        item_id = str(UUID(int=number, version=4))
        row = {
            **original,
            "id": item_id,
            "number": number + 1,
            "display_key": "SC-" + str(number + 1),
            "title": "Scale " + str(number),
            "priority": number % 5,
            "status": ("BACKLOG", "READY", "BLOCKED", "REVIEW", "DONE")[number % 5],
            "archived_at": original["updated_at"] if number % 101 == 0 else None,
        }
        rows.append(tuple(row[column] for column in columns))
    event = dict(db.execute("SELECT * FROM wb_events WHERE entity_id=?", (sample["id"],)).fetchone())
    for number in range(100000):
        events.append(
            (
                str(UUID(int=number + 20000, version=4)),
                ws.id,
                "work_item",
                sample["id"],
                1,
                "scale_fixture",
                "SYSTEM",
                "scale",
                event["timestamp"],
                1,
                event["payload_json"],
            )
        )
    with engine.store.transaction():
        db.executemany(
            "INSERT INTO wb_work_items(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", rows
        )
        db.executemany(
            "INSERT INTO wb_events(event_id,workspace_id,entity_type,entity_id,entity_revision,event_type,actor_type,actor_id,timestamp,schema_version,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            events,
        )
    seed_seconds = time.perf_counter() - stamp
    db.execute("UPDATE wb_project_counters SET next_number=10001 WHERE project_id=?", (project["id"],))
    del rows, events

    def forbidden(*args, **kwargs):
        raise AssertionError("Refresh called model/context scan")

    monkeypatch.setattr(engine, "plan", forbidden)
    monkeypatch.setattr(engine.providers, "generate", forbidden)
    from local_ai_router.context import ContextGraph

    monkeypatch.setattr(ContextGraph, "build", forbidden)
    queries = []
    db.set_trace_callback(queries.append)
    timings, size, count = [], 0, 0
    for _ in range(25):
        queries.clear()
        start = time.perf_counter()
        page = runtime.service.list("work_item", ws.id, HUMAN, limit=200)
        timings.append((time.perf_counter() - start) * 1000)
        size = len(json.dumps([item.model_dump(mode="json") for item in page]).encode())
        count = len(queries)
    db.set_trace_callback(None)
    plan = [
        row[3]
        for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM wb_work_items WHERE workspace_id=? AND archived_at IS NULL ORDER BY priority,id LIMIT 200",
            (ws.id,),
        )
    ]
    assert any("wb_item_page" in row for row in plan)
    event_plan = [
        row[3]
        for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM wb_events WHERE workspace_id=? AND sequence>? ORDER BY sequence LIMIT 200",
            (ws.id, 90000),
        )
    ]
    assert any("wb_events_workspace" in row for row in event_plan)
    assert len(page) == 200 and size <= 1_000_000 and count <= 801
    p95 = sorted(timings)[23]
    assert p95 <= 250, timings
    import httpx

    from local_ai_router.app import create_app
    from local_ai_router.workbench.board import BoardQueries
    from local_ai_router.workbench.schema import BoardQuery

    compact_times, compact_counts, compact_sizes = [], [], []
    boards = BoardQueries(runtime)
    for search in ("", "Scale", "Scale 9999"):
        for _ in range(10):
            queries.clear()
            db.set_trace_callback(queries.append)
            begin = time.perf_counter()
            compact = boards.page(BoardQuery(workspace_id=ws.id, query=search), HUMAN)
            compact_times.append((time.perf_counter() - begin) * 1000)
            compact_counts.append(len([q for q in queries if q.lstrip().upper().startswith(("SELECT", "WITH"))]))
            compact_sizes.append(len(json.dumps(compact).encode()))
            db.set_trace_callback(None)
    compact_p95 = sorted(compact_times)[28]
    assert max(compact_counts) <= 30 and max(compact_sizes) <= 400 * 1024
    assert compact_p95 <= 250, compact_times
    db.set_trace_callback(queries.append)
    queries.clear()
    column = boards.page(BoardQuery(workspace_id=ws.id), HUMAN, state="BACKLOG")
    db.set_trace_callback(None)
    assert len([q for q in queries if q.lstrip().upper().startswith(("SELECT", "WITH"))]) <= 6
    assert len(column["columns"][0]["items"]) <= 20
    app = create_app(engine.settings, engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer " + engine.settings.token},
    ) as client:

        async def read():
            begin = time.perf_counter()
            response = await client.get("/accs/v1/workbench/board", params={"workspace_id": ws.id})
            duration = (time.perf_counter() - begin) * 1000
            assert response.status_code == 200, response.text
            assert len(response.content) <= 400 * 1024
            return duration

        http_times = await asyncio.gather(*(read() for _ in range(10)))
    http_p95 = sorted(http_times)[9]
    assert http_p95 <= 400, http_times
    tracemalloc.start()

    async def disconnected():
        return False

    slow = [part async for part in runtime.journal.stream(ws.id, HUMAN, byte_budget=100)]
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(slow) == 1 and "DELIVERY_LIMIT" in slow[0] and peak < 5_000_000
    await runtime.start(dispatcher=True)
    cpu_start, real_start = time.process_time(), time.perf_counter()
    await asyncio.sleep(2.1)
    cpu_ms = (time.process_time() - cpu_start) * 1000
    elapsed = time.perf_counter() - real_start
    assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    cursor = runtime.journal.sequence()
    stream = runtime.journal.stream(ws.id, HUMAN, after=cursor)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    event_start = time.perf_counter()
    runtime.service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws.id,
            "project_id": project["id"],
            "title": "Visible event",
            "execution_kind": "MANUAL",
        },
        HUMAN,
    )
    assert "Visible event" in await asyncio.wait_for(pending, 2)
    event_ms = (time.perf_counter() - event_start) * 1000
    await stream.aclose()
    report = {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "items": 10000,
        "events": 100000,
        "seed_seconds": seed_seconds,
        "samples": 25,
        "warmup": "first sample included",
        "board_p95_ms": p95,
        "compact_board_p95_ms": compact_p95,
        "compact_sql_counts": compact_counts,
        "compact_payload_bytes": compact_sizes,
        "concurrent_http_clients": 10,
        "http_p95_ms": http_p95,
        "http_latency_ms": http_times,
        "percentile_method": "nearest rank, cold first sample included; compact 30 samples, HTTP 10 simultaneous requests",
        "page_bytes": size,
        "sql_statements_per_page": count,
        "board_query_plan": plan,
        "event_query_plan": event_plan,
        "slow_stream_peak_python_bytes": peak,
        "idle_interval_seconds": elapsed,
        "idle_process_cpu_ms": cpu_ms,
        "process_memory_bytes": process_memory_bytes(),
        "memory_measurement": "current working set" if os.name == "nt" else "process peak RSS",
        "event_visibility_ms": event_ms,
        "service_poll_seconds": 1,
        "provider_calls": 0,
    }
    (tmp_path / "performance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    await runtime.close()
