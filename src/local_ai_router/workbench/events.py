"""Atomic revision journal and post-commit hints; SQLite remains authoritative."""

import asyncio
import base64
import hashlib
import hmac
import json
import threading
import time

from ..schema import Strict
from .policy import authorize
from .schema import AgentProjection, DeviceProjection, WorkbenchEvent
from .store import ENTITIES, WorkbenchError, identity, now


class EventWake:
    def __init__(self):
        self.changed = threading.Event()

    def notify(self):
        self.changed.set()


def append_event(store, kind, entity, principal, event_type, *, tombstone=False):
    if not store.db.in_transaction:
        raise RuntimeError("Event journal requires an owning transaction")
    workspace_id = entity.id if kind == "workspace" else entity.workspace_id
    payload = entity.model_dump_json()
    event_id, stamp = identity(), now()
    sequence = store.db.execute(
        "INSERT INTO wb_events(event_id,workspace_id,entity_type,entity_id,entity_revision,event_type,actor_type,actor_id,timestamp,schema_version,payload_json) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
        (
            event_id,
            workspace_id,
            kind,
            entity.id,
            entity.revision,
            event_type,
            principal.actor_type,
            principal.id,
            stamp,
            payload,
        ),
    ).lastrowid
    event = WorkbenchEvent(
        sequence=sequence,
        event_id=event_id,
        workspace_id=workspace_id,
        entity_type=kind,
        entity_id=entity.id,
        entity_revision=entity.revision,
        event_type=event_type,
        actor_type=principal.actor_type,
        actor_id=principal.id,
        timestamp=stamp,
        payload=entity,
    )
    store.db.execute(
        "INSERT INTO wb_entity_versions VALUES(?,?,?,?,?,?,?)",
        (workspace_id, kind, entity.id, entity.revision, sequence, payload, int(tombstone)),
    )
    return event


def projection(kind, entity):
    """Board synchronization carries metadata; details remain scoped on-demand reads."""
    fields = {
        "id",
        "workspace_id",
        "project_id",
        "revision",
        "updated_at",
        "archived_at",
        "deleted_at",
        "name",
        "title",
        "key",
        "status",
        "state",
        "type",
        "priority",
        "number",
        "display_key",
        "execution_kind",
        "assigned_agent_id",
        "reason_code",
        "active_attempt_id",
        "contract_revision",
    }
    return {key: value for key, value in entity.model_dump(mode="json").items() if key in fields}


class SnapshotFilter(Strict):
    kinds: list[str] = ["project", "prd", "work_item"]
    project_id: str | None = None
    status: str | None = None


class Journal:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.key = service.settings.token.encode()

    def sequence(self):
        row = self.store.db.execute("SELECT seq FROM sqlite_sequence WHERE name='wb_events'").fetchone()
        return row[0] if row else 0

    def token(self, data):
        raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode() + "." + hmac.new(self.key, raw, hashlib.sha256).hexdigest()

    def decode(self, token):
        try:
            if not isinstance(token, str) or len(token) > 4000:
                raise ValueError()
            encoded, signature = token.split(".")
            raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
            if not hmac.compare_digest(signature, hmac.new(self.key, raw, hashlib.sha256).hexdigest()):
                raise ValueError()
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise WorkbenchError("INVALID_SNAPSHOT", "Snapshot token is invalid", 422) from exc

    def start_snapshot(self, workspace_id, principal, filters=None, *, limit=50):
        authorize(principal, workspace_id, "workspace.read")
        filters = SnapshotFilter.model_validate(filters or {})
        if (
            not filters.kinds
            or len(filters.kinds) > 7
            or len(set(filters.kinds)) != len(filters.kinds)
            or set(filters.kinds) - (ENTITIES.keys() | {"agent", "device"})
        ):
            raise WorkbenchError("INVALID_SNAPSHOT", "Unknown or duplicate snapshot entity kinds", 422)
        with self.store.router.transaction():
            authorize(principal, workspace_id, "workspace.read")
            self.store.db.execute("DELETE FROM wb_snapshot_leases WHERE expires_at<=?", (now(),))
            count = self.store.db.execute(
                "SELECT COUNT(*) FROM wb_snapshot_leases WHERE principal_id=?", (principal.id,)
            ).fetchone()[0]
            if count >= 32:
                raise WorkbenchError("SNAPSHOT_LIMIT", "Too many active snapshots", 429)
            lease, sequence = identity(), self.sequence()
            raw_filter = filters.model_dump(mode="json")
            digest = hashlib.sha256(json.dumps(raw_filter, sort_keys=True).encode()).hexdigest()
            self.store.db.execute(
                "INSERT INTO wb_snapshot_leases VALUES(?,?,?,?,?,?)",
                (lease, workspace_id, principal.id, sequence, digest, now() + 15 * 60_000),
            )
            counts = dict(
                self.store.db.execute(
                    "SELECT status,COUNT(*) FROM wb_work_items WHERE workspace_id=? AND archived_at IS NULL GROUP BY status",
                    (workspace_id,),
                )
            )
            agent_service = getattr(self.service, "agents", None)
            agents = (
                [
                    {"id": agent["id"], "name": agent["name"], "revision": agent["revision"], "state": agent["state"]}
                    for agent in agent_service.list(workspace_id, principal)
                ]
                if agent_service
                else []
            )
        token = self.token(
            {
                "lease": lease,
                "workspace": workspace_id,
                "principal": principal.id,
                "sequence": sequence,
                "filter": raw_filter,
                "filter_hash": digest,
                "after": ["", ""],
            }
        )
        page = self.snapshot_page(token, principal, limit=limit)
        return {**page, "counts": counts, "agents": agents}

    def snapshot_page(self, token, principal, *, limit=50):
        data = self.decode(token)
        authorize(principal, data["workspace"], "workspace.read")
        if principal.id != data["principal"]:
            raise WorkbenchError("NOT_FOUND", "Snapshot not found", 404)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise WorkbenchError("INVALID_PAGE", "Page size must be between 1 and 200", 422)
        filters = SnapshotFilter.model_validate(data["filter"])
        with self.store.router.lock:
            authorize(principal, data["workspace"], "workspace.read")
            lease = self.store.db.execute(
                "SELECT * FROM wb_snapshot_leases WHERE id=? AND workspace_id=? AND principal_id=?",
                (data["lease"], data["workspace"], principal.id),
            ).fetchone()
            if lease is None or lease["expires_at"] <= now():
                raise WorkbenchError("SNAPSHOT_EXPIRED", "Snapshot expired; start a fresh snapshot", 410)
            if lease["sequence"] != data["sequence"] or lease["filter_hash"] != data["filter_hash"]:
                raise WorkbenchError("INVALID_SNAPSHOT", "Snapshot filter mismatch", 422)
            clauses = [
                "v.workspace_id=?",
                "v.sequence<=?",
                "(v.entity_type,v.entity_id)>(?,?)",
                "v.entity_type IN (" + ",".join("?" for _ in filters.kinds) + ")",
                "v.tombstone=0",
                "NOT EXISTS (SELECT 1 FROM wb_entity_versions n WHERE n.workspace_id=v.workspace_id AND n.entity_type=v.entity_type AND n.entity_id=v.entity_id AND n.sequence<=? AND n.sequence>v.sequence)",
            ]
            params = [data["workspace"], data["sequence"], *data["after"], *filters.kinds, data["sequence"]]
            for field in ("project_id", "status"):
                value = getattr(filters, field)
                if value is not None:
                    clauses.append(f"json_extract(v.snapshot_json,'$.{field}')=?")
                    params.append(value)
            rows = self.store.db.execute(
                "SELECT v.entity_type,v.entity_id,v.snapshot_json FROM wb_entity_versions v WHERE "
                + " AND ".join(clauses)
                + " ORDER BY v.entity_type,v.entity_id LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            items = []
            try:
                for row in rows[:limit]:
                    model = (
                        AgentProjection
                        if row[0] == "agent"
                        else DeviceProjection
                        if row[0] == "device"
                        else ENTITIES[row[0]][1]
                    )
                    entity = model.model_validate_json(row[2])
                    payload = projection(row[0], entity)
                    if row[0] == "work_item":
                        current = self.store.get("work_item", data["workspace"], entity.id)
                        payload["allowed_actions"] = (
                            self.service.allowed_actions(current, principal)
                            if current.revision == entity.revision
                            else []
                        )
                    items.append({"entity_type": row[0], "payload": payload})
            except ValueError as exc:
                raise WorkbenchError("CORRUPT_DATA", "Invalid snapshot revision", 500) from exc
        more = len(rows) > limit
        if more:
            data["after"] = [rows[limit - 1][0], rows[limit - 1][1]]
        return {
            "snapshot_id": data["lease"],
            "sequence": data["sequence"],
            "items": items,
            "has_more": more,
            "next_cursor": self.token(data) if more else None,
        }

    def events(self, workspace_id, principal, *, after=0, limit=200):
        authorize(principal, workspace_id, "workspace.read")
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise WorkbenchError("INVALID_CURSOR", "Event cursor/page is invalid", 422)
        with self.store.router.lock:
            authorize(principal, workspace_id, "workspace.read")
            maximum = self.sequence()
            if after > maximum:
                raise WorkbenchError("FUTURE_CURSOR", "Event cursor is ahead of journal", 422)
            floor = self.store.db.execute("SELECT value FROM metadata WHERE key='wb_events_floor'").fetchone()
            if floor and after < int(floor[0]):
                raise WorkbenchError("RESYNC_REQUIRED", "Event cursor retired; start a fresh snapshot", 410)
            rows = self.store.db.execute(
                "SELECT * FROM wb_events WHERE workspace_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (workspace_id, after, limit),
            ).fetchall()
            result = []
            for row in rows:
                if row["schema_version"] != 1:
                    raise WorkbenchError("RESYNC_REQUIRED", "Unknown event schema; refresh client and snapshot", 410)
                data = dict(row)
                try:
                    data["payload"] = json.loads(data.pop("payload_json"))
                    event = WorkbenchEvent.model_validate(data)
                except ValueError as exc:
                    raise WorkbenchError("CORRUPT_DATA", "Invalid persisted event", 500) from exc
                data = event.model_dump(mode="json")
                data["payload"] = projection(event.entity_type, event.payload)
                result.append(data)
            return result

    async def stream(
        self, workspace_id, principal, after=0, *, disconnected=None, heartbeat=15, poll=1, byte_budget=2_000_000
    ):
        spent, stamp = 0, time.monotonic()
        while True:
            if disconnected and await disconnected():
                return
            try:
                batch = self.events(workspace_id, principal, after=after)
            except WorkbenchError as exc:
                yield "event: resync\ndata: " + json.dumps({"code": exc.code}) + "\n\n"
                return
            for event in batch:
                output = (
                    f"id: {event['sequence']}\nevent: workbench\ndata: "
                    + json.dumps(event, separators=(",", ":"))
                    + "\n\n"
                )
                spent += len(output.encode())
                if spent > byte_budget:
                    yield 'event: resync\ndata: {"code":"DELIVERY_LIMIT"}\n\n'
                    return
                yield output
                after, stamp = event["sequence"], time.monotonic()
            if batch:
                continue
            if time.monotonic() - stamp >= heartbeat:
                yield ": heartbeat\n\n"
                stamp = time.monotonic()
            self.service.wake.changed.clear()
            # No SQL transaction or per-subscriber event queue survives this wait.
            await asyncio.sleep(poll)
