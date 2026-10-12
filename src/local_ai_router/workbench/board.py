"""Bounded metadata queries; domain state and the full ticket remain with the service."""

import hashlib
import json

from pydantic import Field, ValidationError

from ..schema import Strict
from .policy import authorize
from .schema import UUID, BoardColumn, BoardPage, BoardQuery, CardProjection, WorkItemStatus
from .store import WorkbenchError, now


class BoardCursor(Strict):
    version: int = Field(strict=True, ge=1, le=1)
    kind: str = Field(pattern="^board$")
    query_hash: str = Field(pattern="^[a-f0-9]{64}$")
    principal_id: str
    state: WorkItemStatus
    sequence: int = Field(strict=True, ge=0)
    priority: int = Field(strict=True, ge=0, le=4)
    id: UUID
    expires_at: int = Field(strict=True, ge=0)


class BoardQueries:
    def __init__(self, runtime):
        self.store, self.journal = runtime.service.store, runtime.journal

    def page(self, query, principal, *, state=None, cursor=None):
        query = BoardQuery.model_validate(query)
        authorize(principal, query.workspace_id, "workspace.read")
        states = [WorkItemStatus(state)] if state is not None else query.states
        normalized = query.model_dump(mode="json", exclude={"limit", "states"})
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        position = None
        if cursor:
            try:
                position = BoardCursor.model_validate(self.journal.decode(cursor))
            except (ValidationError, WorkbenchError) as exc:
                raise WorkbenchError("INVALID_CURSOR", "Board cursor is invalid", 422) from exc
            if (
                len(states) != 1
                or position.query_hash != digest
                or position.state != states[0]
                or position.principal_id != principal.id
                or position.expires_at <= now()
            ):
                raise WorkbenchError("CURSOR_QUERY_MISMATCH", "Refresh this column with its current filters", 422)
        clauses, params = ["w.workspace_id=?", "w.archived_at IS NULL"], [query.workspace_id]
        for column, value in (
            ("project_id", query.project_id),
            ("assigned_agent_id", query.agent_id),
            ("priority", query.priority),
            ("type", query.type),
        ):
            if value is not None:
                clauses.append("w." + column + "=?")
                params.append(value)
        if query.query:
            search = "%" + query.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(w.title LIKE ? ESCAPE '\\' OR w.display_key LIKE ? ESCAPE '\\')")
            params.extend((search, search))
        where = " AND ".join(clauses)
        db = self.store.db
        with self.store.router.lock:
            if db.in_transaction:
                raise RuntimeError("Board reads require an independent read transaction")
            db.execute("BEGIN")
            try:
                if not db.execute("SELECT 1 FROM wb_workspaces WHERE id=?", (query.workspace_id,)).fetchone():
                    raise WorkbenchError("NOT_FOUND", "Workspace not found", 404)
                sequence = self.journal.sequence()
                if position and position.sequence != sequence:
                    raise WorkbenchError("CURSOR_QUERY_MISMATCH", "Board changed; refresh the column", 422)
                counts = dict(
                    db.execute(
                        "SELECT w.status,COUNT(*) FROM wb_work_items w WHERE " + where + " GROUP BY w.status", params
                    )
                )
                columns = [
                    self.column(
                        query, principal, column, where, params, counts.get(column, 0), sequence, digest, position
                    )
                    for column in states
                ]
                result = BoardPage(
                    workspace_id=query.workspace_id,
                    project_id=query.project_id,
                    query_hash=digest,
                    sequence=sequence,
                    columns=columns,
                )
                db.execute("COMMIT")
                return result.model_dump(mode="json")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def column(self, query, principal, state, where, params, count, sequence, digest, position):
        after = " AND (w.priority>? OR (w.priority=? AND w.id>?))" if position else ""
        values = [*params, state]
        if position:
            values.extend((position.priority, position.priority, position.id))
        values.append(query.limit + 1)
        rows = self.store.db.execute(
            "SELECT w.id,w.display_key,w.title,w.status,w.revision,w.contract_revision,w.type,w.priority,"
            "w.assigned_agent_id,w.active_attempt_id,w.reason_code,w.number,w.project_id,w.prd_id,w.parent_item_id,"
            "p.key AS project_key,prd.id AS valid_prd,parent.id AS valid_parent,a.id AS agent_ref,"
            "CASE WHEN membership.agent_id IS NOT NULL THEN a.name END AS assigned_agent_name,"
            "assignment.id AS assignment_id,attempt.id AS attempt_ref,"
            "(SELECT COUNT(*) FROM wb_dependencies d WHERE d.workspace_id=w.workspace_id AND d.dependent_id=w.id) AS dependency_count,"
            "(SELECT COUNT(*) FROM wb_dependencies d LEFT JOIN wb_work_items blocker ON blocker.workspace_id=d.workspace_id AND blocker.id=d.blocker_id WHERE d.workspace_id=w.workspace_id AND d.dependent_id=w.id AND (blocker.id IS NULL OR blocker.status!='DONE')) AS blocker_count "
            "FROM wb_work_items w LEFT JOIN wb_projects p ON p.workspace_id=w.workspace_id AND p.id=w.project_id "
            "LEFT JOIN wb_prds prd ON prd.workspace_id=w.workspace_id AND prd.project_id=w.project_id AND prd.id=w.prd_id "
            "LEFT JOIN wb_work_items parent ON parent.workspace_id=w.workspace_id AND parent.project_id=w.project_id AND parent.id=w.parent_item_id "
            "LEFT JOIN wb_agent_definitions a ON a.id=w.assigned_agent_id "
            "LEFT JOIN wb_agent_workspaces membership ON membership.agent_id=a.id AND membership.workspace_id=w.workspace_id "
            "LEFT JOIN wb_assignments assignment ON assignment.workspace_id=w.workspace_id AND assignment.item_id=w.id AND assignment.status IN ('RESERVED','ACTIVE') "
            "LEFT JOIN wb_attempts attempt ON attempt.workspace_id=w.workspace_id AND attempt.item_id=w.id AND attempt.id=w.active_attempt_id "
            "WHERE " + where + " AND w.status=?" + after + " ORDER BY w.priority,w.id LIMIT ?",
            values,
        ).fetchall()
        cards = []
        for row in rows:
            if (
                row["project_key"] is None
                or row["display_key"] != f"{row['project_key']}-{row['number']}"
                or (row["prd_id"] and not row["valid_prd"])
                or (row["parent_item_id"] and not row["valid_parent"])
                or (row["assigned_agent_id"] and not row["agent_ref"])
                or (row["active_attempt_id"] and not row["attempt_ref"])
            ):
                raise WorkbenchError("CORRUPT_DATA", "Board item has invalid persisted references", 500)
            data = {key: value for key, value in dict(row).items() if key in CardProjection.model_fields}
            data["review_needed"] = row["status"] in {"REVIEW", "WAITING_FOR_HUMAN"}
            try:
                cards.append(CardProjection.model_validate(data))
            except ValidationError as exc:
                raise WorkbenchError("CORRUPT_DATA", "Invalid board metadata", 500) from exc
        more = len(cards) > query.limit
        cards = cards[: query.limit]
        token = (
            self.journal.token(
                BoardCursor(
                    version=1,
                    kind="board",
                    query_hash=digest,
                    principal_id=principal.id,
                    state=state,
                    sequence=sequence,
                    priority=cards[-1].priority,
                    id=cards[-1].id,
                    expires_at=now() + 15 * 60_000,
                ).model_dump()
            )
            if more
            else None
        )
        return BoardColumn(state=state, count=count, items=cards, has_more=more, next_cursor=token)
