"""Compact atomic audit projections; immutable journal/attempts remain authority."""

import json
from typing import Annotated

from pydantic import Field, ValidationError

from ..safety import redact
from ..schema import Strict
from .policy import authorize
from .schema import UUID, ActorType, Identifier, Name
from .store import WorkbenchError


class ActivityMetadata(Strict):
    entity_type: Identifier | None = None
    title: Name | None = None
    status: Identifier | None = None
    previous_status: Identifier | None = None
    agent_name: Name | None = None
    agent_revision: Annotated[int, Field(strict=True, ge=1)] | None = None
    model_id: Name | None = None
    stage: Identifier | None = None
    summary: Annotated[str, Field(strict=True, max_length=2000)] | None = None
    changed_fields: Annotated[list[Identifier], Field(max_length=30)] = []
    legacy: bool = False


class ActivityEntry(Strict):
    source_key: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    workspace_id: UUID
    project_id: UUID | None = None
    item_id: UUID | None = None
    agent_definition_id: UUID | None = None
    attempt_id: UUID | None = None
    occurred_at: Annotated[int, Field(strict=True, ge=0)]
    actor_type: ActorType
    actor_id: Annotated[str, Field(strict=True, max_length=256)]
    event_type: Identifier
    metadata: ActivityMetadata = Field(default_factory=ActivityMetadata)
    source_event_id: UUID | None = None


def append_activity(store, **fields):
    if not store.db.in_transaction:
        raise RuntimeError("Activity requires the owning transaction")
    entry = ActivityEntry.model_validate(fields)
    data = entry.model_dump(exclude={"metadata"})
    data["metadata_json"] = json.dumps(redact(entry.metadata.model_dump(exclude_none=True)), separators=(",", ":"))
    if len(data["metadata_json"].encode()) > 8192:
        raise WorkbenchError("INVALID_ACTIVITY", "Activity metadata exceeds its bound", 422)
    inserted = store.db.execute(
        "INSERT OR IGNORE INTO wb_activity_entries(" + ",".join(data) + ") VALUES(" + ",".join("?" for _ in data) + ")",
        tuple(data.values()),
    )
    return inserted.rowcount == 1


def event_activity(store, row, payload, *, legacy=False):
    kind = row["entity_type"]
    if kind == "agent" and row["event_type"] == "agent_removed":
        previous = store.db.execute(
            "SELECT snapshot_json FROM wb_entity_versions WHERE workspace_id=? AND entity_type='agent' AND entity_id=? AND revision<? ORDER BY revision DESC LIMIT 1",
            (row["workspace_id"], row["entity_id"], payload["revision"]),
        ).fetchone()
        payload = json.loads(previous[0]) if previous else {}
    item_id = row["entity_id"] if kind == "work_item" else payload.get("item_id") if kind == "comment" else None
    agent_id = row["entity_id"] if kind == "agent" else payload.get("assigned_agent_id")
    metadata = {"legacy": legacy, "entity_type": kind}
    for key in ("title", "status"):
        if payload.get(key) is not None:
            metadata[key] = payload[key]
    if agent_id and not legacy:
        agent = store.db.execute("SELECT name,revision FROM wb_agent_definitions WHERE id=?", (agent_id,)).fetchone()
        if agent:
            metadata.update(agent_name=agent["name"], agent_revision=agent["revision"])
        else:
            agent_id = None
    elif legacy:
        agent_id = None
    if kind == "agent":
        metadata.update(agent_name=payload.get("name"), agent_revision=payload.get("revision"))
        agent_id = (
            row["entity_id"]
            if store.db.execute("SELECT 1 FROM wb_agent_definitions WHERE id=?", (row["entity_id"],)).fetchone()
            else None
        )
    attempt_id = payload.get("active_attempt_id") if kind == "work_item" else None
    if (
        attempt_id
        and not store.db.execute(
            "SELECT 1 FROM wb_attempts WHERE id=? AND workspace_id=?", (attempt_id, row["workspace_id"])
        ).fetchone()
    ):
        attempt_id = None
    append_activity(
        store,
        source_key="event:" + row["event_id"],
        workspace_id=row["workspace_id"],
        project_id=payload.get("project_id"),
        item_id=item_id,
        agent_definition_id=agent_id,
        attempt_id=attempt_id,
        occurred_at=row["timestamp"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        event_type=row["event_type"],
        metadata=metadata,
        source_event_id=row["event_id"],
    )


def backfill_activity(store, *, limit=200):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Backfill limit must be 1..200")
    with store.router.transaction():
        saved = store.db.execute("SELECT value FROM metadata WHERE key='wb_activity_backfill'").fetchone()
        after = json.loads(saved[0]) if saved else 0
        rows = store.db.execute(
            "SELECT * FROM wb_events WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit)
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                event_activity(store, row, payload, legacy=True)
            except (ValueError, TypeError, ValidationError) as exc:
                raise WorkbenchError("CORRUPT_DATA", "Invalid activity source event", 500) from exc
        if rows:
            store.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('wb_activity_backfill',?)", (json.dumps(rows[-1]["sequence"]),)
            )
        return len(rows)


def timeline(
    runtime,
    workspace_id,
    principal,
    *,
    item_id=None,
    agent_id=None,
    project_id=None,
    cursor=None,
    limit=50,
    start=None,
    end=None,
):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise WorkbenchError("INVALID_PAGE", "Timeline limit must be 1..100", 422)
    if (
        (start is None) != (end is None)
        or start is not None
        and (type(start) is not int or type(end) is not int or not 0 <= start < end)
    ):
        raise WorkbenchError("INVALID_WINDOW", "Timeline window requires from/to epoch milliseconds", 422)
    authorize(principal, workspace_id, "workspace.read")
    store = runtime.service.store
    if item_id:
        store.get("work_item", workspace_id, item_id)
    scope = {
        "workspace_id": workspace_id,
        "item_id": item_id,
        "agent_id": agent_id,
        "project_id": project_id,
        "principal_id": principal.id,
        "kind": "activity",
        "version": 1,
        "from": start,
        "to": end,
    }
    clauses, values = ["workspace_id=?"], [workspace_id]
    for column, value in (("item_id", item_id), ("agent_definition_id", agent_id), ("project_id", project_id)):
        if value:
            clauses.append(column + "=?")
            values.append(value)
    if start is not None:
        clauses.append("occurred_at>=? AND occurred_at<?")
        values.extend((start, end))
    with store.router.lock:
        if cursor:
            position = runtime.journal.decode(cursor)
            if (
                not isinstance(position, dict)
                or any(position.get(key) != value for key, value in scope.items())
                or type(position.get("before")) is not int
                or type(position.get("high")) is not int
            ):
                raise WorkbenchError("CURSOR_QUERY_MISMATCH", "Timeline scope changed", 422)
            high, before = position["high"], position["before"]
        else:
            high = store.db.execute("SELECT COALESCE(MAX(id),0) FROM wb_activity_entries").fetchone()[0]
            before = high + 1
        rows = store.db.execute(
            "SELECT * FROM wb_activity_entries WHERE "
            + " AND ".join(clauses)
            + " AND id<=? AND id<? ORDER BY id DESC LIMIT ?",
            [*values, high, before, limit + 1],
        ).fetchall()
        result = []
        for row in rows[:limit]:
            data = dict(row)
            entry_id = data.pop("id")
            try:
                data["metadata"] = json.loads(data.pop("metadata_json"))
                result.append({"id": entry_id, **ActivityEntry.model_validate(data).model_dump(mode="json")})
            except (ValueError, TypeError) as exc:
                raise WorkbenchError("CORRUPT_DATA", "Invalid activity entry", 500) from exc
        more = len(rows) > limit
        return {
            "items": result,
            "high_water": high,
            "has_more": more,
            "next_cursor": runtime.journal.token({**scope, "high": high, "before": result[-1]["id"]}) if more else None,
        }
