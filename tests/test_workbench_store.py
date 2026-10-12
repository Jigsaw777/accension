"""Scoped SQL, stable ticket identity, and transaction ownership regressions."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from local_ai_router.store import Store
from local_ai_router.workbench.schema import PRD, CreateWorkItem, Project, Workspace
from local_ai_router.workbench.service import WorkbenchService
from local_ai_router.workbench.store import WorkbenchError, WorkbenchStore, identity, now


def workspace(store, name="Test"):
    stamp = now()
    entity = Workspace(
        id=identity(), name=name, revision=1, created_at=stamp, updated_at=stamp, created_by="human", updated_by="human"
    )
    with store.router.transaction():
        store.insert("workspace", entity)
    return entity


def project(store, ws, key="WB"):
    stamp = now()
    entity = Project(
        id=identity(),
        workspace_id=ws.id,
        name=key,
        key=key,
        revision=1,
        created_at=stamp,
        updated_at=stamp,
        created_by="human",
        updated_by="human",
    )
    with store.router.transaction():
        store.insert("project", entity)
    return entity


def request(ws, p, **fields):
    return CreateWorkItem(
        command_id=identity(), workspace_id=ws.id, project_id=p.id, title="A task", execution_kind="MANUAL", **fields
    )


@pytest.fixture
def wb(settings):
    router = Store(settings)
    yield WorkbenchStore(router)
    router.close()


def test_item_counter_concurrent_unique_never_reused(wb, settings):
    ws = workspace(wb)
    p = project(wb, ws)
    second = Store(settings)
    stores = [wb, WorkbenchStore(second)]
    barrier = Barrier(2)

    def insert(index):
        store = stores[index]
        barrier.wait(timeout=10)
        ids = []
        for _ in range(25):
            with store.router.transaction():
                ids.append(store.allocate_item(request(ws, p), "human"))
        return ids

    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(insert, range(2)))
        items = [item for batch in results for item in batch]
        assert sorted(item.number for item in items) == list(range(1, 51))
        assert len({item.display_key for item in items}) == 50
        first = items[0]
        with wb.router.transaction():
            wb.replace("work_item", first.model_copy(update={"archived_at": now(), "revision": 2}), 1)
            next_item = wb.allocate_item(request(ws, p), "human")
        assert next_item.number == 51
        with pytest.raises(WorkbenchError, match="frozen"), wb.router.transaction():
            wb.replace("project", p.model_copy(update={"key": "NEW", "revision": 2}), 1)
        assert wb.db.execute("SELECT next_number FROM wb_project_counters").fetchone()[0] == 52
    finally:
        second.close()


def test_cross_workspace_fk_rejected(wb):
    left, right = workspace(wb), workspace(wb)
    lp, rp = project(wb, left), project(wb, right)
    with pytest.raises(WorkbenchError), wb.router.transaction():
        wb.allocate_item(request(left, rp), "human")
    with wb.router.transaction():
        item = wb.allocate_item(request(right, rp), "human")
    with pytest.raises(WorkbenchError, match="parent_item_id"), wb.router.transaction():
        wb.allocate_item(request(left, lp, parent_item_id=item.id), "human")
    # Database constraints independently protect bypasses of service validation.
    data = wb._encode(item)
    data.update(id=identity(), workspace_id=left.id, number=2, display_key="WB-2")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        wb.db.execute(
            f"INSERT INTO wb_work_items({','.join(data)}) VALUES({','.join('?' for _ in data)})", tuple(data.values())
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        wb.db.execute(
            "INSERT INTO wb_dependencies VALUES(?,?,?,'BLOCKS',?,?)", (left.id, item.id, identity(), now(), "human")
        )
    assert wb.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reopen_persists_keys_and_spec(wb, settings):
    ws = workspace(wb)
    p = project(wb, ws)
    with wb.router.transaction():
        item = wb.allocate_item(
            request(
                ws, p, budget_limit="0.1234569", spec={"objective": "Exact contract", "context_notes": "Retain me"}
            ),
            "human",
        )
    assert item.budget_limit == "0.123456"
    reopened = Store(settings)
    try:
        saved = WorkbenchStore(reopened).get("work_item", ws.id, item.id)
        assert saved.model_dump_json() == item.model_dump_json()
        assert saved.display_key == "WB-1"
        assert saved.spec.context_notes == "Retain me"
    finally:
        reopened.close()


def test_board_page_limits_and_parameterized_search(wb):
    ws = workspace(wb)
    p = project(wb, ws)
    with wb.router.transaction():
        for index in range(210):
            req = request(ws, p).model_copy(
                update={"title": "100%_ literal" if index == 0 else f"Task {index}", "priority": index % 5}
            )
            wb.allocate_item(req, "human")
    assert len(wb.list("work_item", ws.id)) == 50
    assert len(wb.list("work_item", ws.id, limit=200)) == 200
    for limit in (0, 201, True):
        with pytest.raises(WorkbenchError, match="Page size"):
            wb.list("work_item", ws.id, limit=limit)
    assert len(wb.list("work_item", ws.id, search="%_")) == 1
    assert wb.list("work_item", ws.id, search="' OR 1=1 --") == []
    first = wb.list("work_item", ws.id, limit=200)
    second = wb.list("work_item", ws.id, after=(first[-1].priority, first[-1].id))
    assert len(second) == 10
    assert not {i.id for i in first} & {i.id for i in second}
    assert wb.list("work_item", identity()) == []


def test_corrupt_json_and_reference_diagnosed(wb):
    ws = workspace(wb)
    p = project(wb, ws)
    with wb.router.transaction():
        item = wb.allocate_item(request(ws, p), "human")
    wb.db.execute("UPDATE wb_work_items SET spec_json='broken' WHERE id=?", (item.id,))
    with pytest.raises(WorkbenchError, match="Invalid persisted"):
        wb.list("work_item", ws.id)
    wb.db.execute("UPDATE wb_work_items SET spec_json='{}' WHERE id=?", (item.id,))
    wb.db.execute("PRAGMA foreign_keys=OFF")
    wb.db.execute("UPDATE wb_work_items SET project_id=? WHERE id=?", (identity(), item.id))
    wb.db.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(WorkbenchError, match="Project does not belong"):
        wb.get("work_item", ws.id, item.id)


def test_failed_insert_rolls_back_counter_and_scoped_prd(wb):
    ws = workspace(wb)
    p = project(wb, ws)
    with pytest.raises(WorkbenchError), wb.router.transaction():
        wb.allocate_item(request(ws, p, prd_id=identity()), "human")
    assert wb.db.execute("SELECT next_number FROM wb_project_counters").fetchone()[0] == 1
    stamp = now()
    prd = PRD(
        id=identity(),
        workspace_id=ws.id,
        project_id=p.id,
        title="Draft",
        status="DRAFT",
        revision=1,
        created_at=stamp,
        updated_at=stamp,
        created_by="human",
        updated_by="human",
    )
    with wb.router.transaction():
        wb.insert("prd", prd)
        item = wb.allocate_item(request(ws, p, prd_id=prd.id), "human")
    assert item.number == 1
    assert wb.list("prd", ws.id, project_id=p.id)[0] == prd
    with wb.router.transaction():
        wb.replace("project", p.model_copy(update={"archived_at": now(), "revision": 2}), 1)
    assert wb.list("project", ws.id) == []
    with pytest.raises(WorkbenchError, match="archived"), wb.router.transaction():
        wb.allocate_item(request(ws, p), "human")


def test_parent_cycle_and_depth_and_transaction_owner(wb):
    ws = workspace(wb)
    p = project(wb, ws)
    with pytest.raises(RuntimeError, match="owning transaction"):
        wb.allocate_item(request(ws, p), "human")
    with wb.router.transaction():
        parent = wb.allocate_item(request(ws, p), "human")
        child = wb.allocate_item(request(ws, p, parent_item_id=parent.id), "human")
    with pytest.raises(ValueError, match="cycle"), wb.router.transaction():
        wb.replace("work_item", parent.model_copy(update={"parent_item_id": child.id, "revision": 2}), 1)
    assert wb.get("work_item", ws.id, parent.id).parent_item_id is None


def test_default_seed_is_idempotent_without_enabling_execution(wb, settings, repo):
    service = WorkbenchService(wb.router)
    first = service.seed_defaults()
    assert service.seed_defaults() == first
    assert not first.execution_policy.auto_dispatch
    assert first.execution_policy.require_execution_approval
    assert wb.db.execute("SELECT COUNT(*) FROM wb_projects").fetchone()[0] == 0
    reference = next(iter(service.repositories()))
    assert service.repository(reference).path == str(repo)
    with pytest.raises(WorkbenchError, match="not registered"):
        service.repository(str(repo))
