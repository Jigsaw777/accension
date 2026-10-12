"""Explicit local purge retires replay, preserving contract and audit evidence."""

import hashlib
import json

from pydantic import Field

from ..schema import Strict
from .policy import authorize
from .schema import UUID, Hash
from .service import CommandResult, payload_hash
from .store import WorkbenchError, now


class Purge(Strict):
    workspace_id: UUID
    command_id: UUID
    before_sequence: int = Field(strict=True, ge=0)
    preview_hash: Hash


class Retention:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.service.store

    def candidates(self, before_sequence):
        if type(before_sequence) is not int or not 0 <= before_sequence <= self.runtime.journal.sequence():
            raise WorkbenchError("INVALID_CURSOR", "Retention boundary must be a committed sequence", 422)
        oldest = self.store.db.execute(
            "SELECT MIN(sequence) FROM wb_snapshot_leases WHERE expires_at>?", (now(),)
        ).fetchone()[0]
        boundary = min(before_sequence, oldest) if oldest is not None else before_sequence
        # Full PRD/item contracts, all journal audit rows and receipts remain immutable.
        rows = self.store.db.execute(
            "SELECT v.rowid,v.snapshot_json FROM wb_entity_versions v WHERE v.sequence<? AND v.entity_type IN ('workspace','project','comment','agent','device') "
            "AND EXISTS(SELECT 1 FROM wb_entity_versions n WHERE n.workspace_id=v.workspace_id AND n.entity_type=v.entity_type AND n.entity_id=v.entity_id AND n.sequence<=? AND n.sequence>v.sequence) ORDER BY v.rowid LIMIT 10000",
            (boundary, boundary),
        ).fetchall()
        days = self.runtime.engine.settings.workbench_diagnostic_days
        logs = []
        if days is not None:
            root = self.runtime.engine.settings.home / "logs"
            for number in range(1, self.runtime.engine.settings.log_backups + 1):
                path = root / ("accension.jsonl." + str(number))
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.stat().st_size <= self.runtime.engine.settings.log_max_bytes
                    and path.stat().st_mtime_ns // 1_000_000 < now() - days * 86400000
                ):
                    data = path.read_bytes()
                    logs.append({"name": path.name, "size": len(data), "hash": hashlib.sha256(data).hexdigest()})
        return {
            "boundary": boundary,
            "requested_boundary": before_sequence,
            "version_rows": [row[0] for row in rows],
            "version_hashes": [hashlib.sha256(row[1].encode()).hexdigest() for row in rows],
            "logs": logs,
        }

    def preview(self, principal, before_sequence):
        authorize(principal, "local", "policy.admin", local_only=True)
        with self.store.router.lock:
            candidates = self.candidates(before_sequence)
            return {
                "preview_hash": payload_hash(candidates),
                "boundary": candidates["boundary"],
                "versions": len(candidates["version_rows"]),
                "diagnostic_files": candidates["logs"],
                "preserved": "all receipts, attempts, approvals, PRD/item versions and journal audit rows",
            }

    def apply(self, request, principal):
        body = Purge.model_validate(request)
        authorize(principal, "local", "policy.admin", local_only=True)

        def mutate():
            candidates = self.candidates(body.before_sequence)
            if payload_hash(candidates) != body.preview_hash:
                raise WorkbenchError("RETENTION_CHANGED", "Retention candidates changed; preview again")
            for row in candidates["version_rows"]:
                self.store.db.execute("DELETE FROM wb_entity_versions WHERE rowid=?", (row,))
            previous = self.store.db.execute("SELECT value FROM metadata WHERE key='wb_events_floor'").fetchone()
            floor = max(int(previous[0]) if previous else 0, candidates["boundary"])
            self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES('wb_events_floor',?)", (str(floor),))
            operation = self.runtime.service.operation_intent(
                body.workspace_id, "retention", None, None, body.preview_hash
            ).body["id"]
            result = {
                "id": operation,
                "state": "COMPLETED",
                "boundary": floor,
                "versions_pruned": len(candidates["version_rows"]),
                "diagnostic_files": candidates["logs"],
            }
            self.store.db.execute(
                "UPDATE wb_operations SET state='COMPLETED',result_json=? WHERE id=?",
                (json.dumps(result), operation),
            )
            self.runtime.planning.event(operation, principal)
            return CommandResult(200, result)

        result = self.runtime.service.command(
            principal,
            body.workspace_id,
            "policy.admin",
            body.command_id,
            "retention_apply",
            body.model_dump(mode="json"),
            mutate,
            local_only=True,
        )
        if not result.replayed:
            root = self.runtime.engine.settings.home / "logs"
            for candidate in result.body["diagnostic_files"]:
                path = root / candidate["name"]
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and hashlib.sha256(path.read_bytes()).hexdigest() == candidate["hash"]
                ):
                    path.unlink()
        return result
