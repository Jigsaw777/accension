"""Compact board correctness, scope-bound pagination and query budgets."""

import json
from uuid import UUID

import httpx
import pytest
from test_workbench_agents import HUMAN

from local_ai_router.app import create_app
from local_ai_router.workbench.board import BoardQueries
from local_ai_router.workbench.policy import Principal
from local_ai_router.workbench.runtime import WorkbenchRuntime
from local_ai_router.workbench.schema import ActorType
from local_ai_router.workbench.store import WorkbenchError, identity


@pytest.fixture
async def board(engine):
    runtime = WorkbenchRuntime(engine)
    ws = runtime.service.seed_defaults()
    project = runtime.service.create(
        "project", {"command_id": identity(), "workspace_id": ws.id, "name": "Board", "key": "BD"}, HUMAN
    ).body
    for index in range(5):
        runtime.service.create(
            "work_item",
            {
                "command_id": identity(),
                "workspace_id": ws.id,
                "project_id": project["id"],
                "title": "Literal_% " + str(index),
                "execution_kind": "MANUAL",
            },
            HUMAN,
        )
    yield runtime, ws, project, BoardQueries(runtime)
    await runtime.close()


async def test_compact_board_counts_and_cursor_binding(board):
    runtime, ws, project, queries = board
    query = {"workspace_id": ws.id, "project_id": project["id"], "limit": 2}
    page = queries.page(query, HUMAN)
    assert len(page["columns"]) == 11
    backlog = page["columns"][0]
    assert backlog["count"] == 5 and len(backlog["items"]) == 2 and backlog["has_more"]
    assert "spec" not in json.dumps(page) and "logs" not in json.dumps(page)
    seen = [card["id"] for card in backlog["items"]]
    while backlog["has_more"]:
        backlog = queries.page(query, HUMAN, state="BACKLOG", cursor=backlog["next_cursor"])["columns"][0]
        seen.extend(card["id"] for card in backlog["items"])
    assert len(seen) == len(set(seen)) == 5
    token = page["columns"][0]["next_cursor"]
    with pytest.raises(WorkbenchError, match="current filters"):
        queries.page({**query, "query": "changed"}, HUMAN, state="BACKLOG", cursor=token)
    with pytest.raises(WorkbenchError, match="invalid"):
        queries.page(query, HUMAN, state="BACKLOG", cursor=token + "x")
    other = Principal("other", ActorType.MACHINE, frozenset({"workspace.read"}), frozenset({ws.id}), True)
    with pytest.raises(WorkbenchError, match="current filters"):
        queries.page(query, other, state="BACKLOG", cursor=token)
    assert runtime.engine.store.costs()["calls"] == 0


async def test_literal_search_empty_and_scope(board):
    _, ws, _, queries = board
    result = queries.page({"workspace_id": ws.id, "query": "_%"}, HUMAN)
    assert result["columns"][0]["count"] == 5
    assert queries.page({"workspace_id": ws.id, "query": "absent"}, HUMAN)["columns"][0]["items"] == []
    with pytest.raises(WorkbenchError, match="not found"):
        queries.page({"workspace_id": identity()}, HUMAN)
    restricted = Principal(
        "restricted", ActorType.MACHINE, frozenset({"workspace.read"}), frozenset({identity()}), True
    )
    with pytest.raises(WorkbenchError):
        queries.page({"workspace_id": ws.id}, restricted)


async def test_corrupt_orphan_not_dropped(board):
    runtime, ws, _, queries = board
    db = runtime.service.store.db
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("UPDATE wb_work_items SET project_id=?", (identity(),))
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(WorkbenchError, match="invalid persisted references"):
        queries.page({"workspace_id": ws.id}, HUMAN)


async def test_board_reads_remain_scoped_and_track_writes(board):
    runtime, ws, project, queries = board
    query = {"workspace_id": ws.id}
    initial = queries.page(query, HUMAN)
    item = initial["columns"][0]["items"][0]
    initial["columns"][0]["items"].clear()
    assert len(queries.page(query, HUMAN)["columns"][0]["items"]) == 5
    db = runtime.service.store.db
    runtime.service.update(
        "work_item",
        ws.id,
        item["id"],
        {"command_id": identity(), "expected_revision": item["revision"], "title": "Changed"},
        HUMAN,
    )
    assert queries.page(query, HUMAN)["columns"][0]["items"][0]["title"] == "Changed"
    restricted = Principal("human", ActorType.HUMAN, frozenset(), frozenset({ws.id}), True)
    with pytest.raises(WorkbenchError):
        queries.page(query, restricted)
    db.execute("UPDATE wb_projects SET key='BROKEN' WHERE id=?", (project["id"],))
    with pytest.raises(WorkbenchError, match="invalid persisted references"):
        queries.page(query, HUMAN)


async def test_board_10k_query_and_payload_budgets(board, tmp_path):
    runtime, ws, _, queries = board
    db = runtime.service.store.db
    sample = dict(db.execute("SELECT * FROM wb_work_items LIMIT 1").fetchone())
    columns = tuple(sample)
    with runtime.engine.store.transaction():
        db.executemany(
            "INSERT INTO wb_work_items(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
            [
                tuple(
                    {
                        **sample,
                        "id": str(UUID(int=index, version=4)),
                        "number": index + 100,
                        "display_key": "BD-" + str(index + 100),
                        "status": "READY" if index % 2 else "BACKLOG",
                    }[key]
                    for key in columns
                )
                for index in range(1, 10001)
            ],
        )
    report = {}
    for name, state, budget in (("first", None, 30), ("column", "BACKLOG", 6)):
        statements = []
        db.set_trace_callback(statements.append)
        try:
            result = queries.page({"workspace_id": ws.id}, HUMAN, state=state)
        finally:
            db.set_trace_callback(None)
        size = len(json.dumps(result).encode())
        assert len(statements) <= budget, statements
        assert size <= 400 * 1024
        report[name] = {"sql": len(statements), "bytes": size}
    (tmp_path / "board-budget.json").write_text(json.dumps(report), encoding="utf-8")


async def test_board_http_compatibility(board):
    runtime, ws, _, _ = board
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime.engine.settings, runtime.engine)),
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer " + runtime.engine.settings.token},
    ) as client:
        for path in ("/board", "/board/columns/BACKLOG", "/work-items"):
            response = await client.get("/accs/v1/workbench" + path, params={"workspace_id": ws.id})
            assert response.status_code == 200, response.text
        response = await client.get("/accs/v1/workbench/board", params={"workspace_id": ws.id, "limit": 51})
        assert response.status_code == 400
