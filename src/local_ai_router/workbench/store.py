"""Scoped Workbench persistence on the router's existing SQLite connection."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from uuid import uuid4

from pydantic import ValidationError

from ..store import Store
from .schema import PRD, Comment, PlanImport, Project, WorkItem, Workspace, budget_microusd, validate_parents

ENTITIES = {
    "workspace": ("wb_workspaces", Workspace),
    "project": ("wb_projects", Project),
    "prd": ("wb_prds", PRD),
    "work_item": ("wb_work_items", WorkItem),
    "comment": ("wb_comments", Comment),
    "plan_import": ("wb_plan_imports", PlanImport),
}


class WorkbenchError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def now() -> int:
    return time.time_ns() // 1_000_000


def identity() -> str:
    return str(uuid4())


class WorkbenchStore:
    def __init__(self, store: Store):
        self.router = store
        self.db = store.db

    def get(self, kind: str, workspace_id: str, entity_id: str):
        table, model = ENTITIES[kind]
        scope = "id" if kind == "workspace" else "workspace_id"
        with self.router.lock:
            row = self.db.execute(
                f"SELECT * FROM {table} WHERE {scope}=? AND id=?", (workspace_id, entity_id)
            ).fetchone()
            if row is None:
                raise WorkbenchError("NOT_FOUND", f"{kind} not found in workspace", 404)
            entity = self._decode(model, row)
            self._references(kind, entity, writable=False)
            return entity

    @staticmethod
    def _decode(model, row):
        data = dict(row)
        try:
            for key in tuple(data):
                if key.endswith("_json"):
                    data[key.removesuffix("_json")] = json.loads(data.pop(key))
            if "budget_limit_microusd" in data:
                data["budget_limit"] = str(Decimal(data.pop("budget_limit_microusd")) / 1_000_000)
            # Validate the persisted contract; malformed rows are never omitted from pages.
            return model.model_validate(data)
        except (ValidationError, ValueError, TypeError) as exc:
            raise WorkbenchError("CORRUPT_DATA", f"Invalid persisted {model.__name__} {row['id']}", 500) from exc

    @staticmethod
    def _encode(entity):
        data = entity.model_dump(mode="json")
        # SQL timestamps remain integers; JSON projections use UTC ISO strings.
        for field in ("created_at", "updated_at", "archived_at", "deleted_at"):
            if field in data:
                data[field] = getattr(entity, field)
        for field in ("spec", "execution_policy", "proposal"):
            if field in data:
                data[field + "_json"] = json.dumps(data.pop(field), sort_keys=True, separators=(",", ":"))
        if "budget_limit" in data:
            data["budget_limit_microusd"] = budget_microusd(data.pop("budget_limit"))
        return data

    def _references(self, kind, entity, *, writable: bool):
        if kind == "workspace":
            return
        ws = self.db.execute("SELECT archived_at FROM wb_workspaces WHERE id=?", (entity.workspace_id,)).fetchone()
        if ws is None:
            raise WorkbenchError("INVALID_REFERENCE", "Workspace does not exist")
        if writable and ws[0] is not None:
            raise WorkbenchError("ARCHIVED", "Workspace is archived")
        if kind in {"prd", "work_item", "plan_import"}:
            project = self.db.execute(
                "SELECT key,archived_at FROM wb_projects WHERE workspace_id=? AND id=?",
                (entity.workspace_id, entity.project_id),
            ).fetchone()
            if project is None:
                raise WorkbenchError("INVALID_REFERENCE", "Project does not belong to workspace")
            if writable and project[1] is not None:
                raise WorkbenchError("ARCHIVED", "Project is archived")
        if (
            kind == "plan_import"
            and not self.db.execute(
                "SELECT 1 FROM wb_prds WHERE workspace_id=? AND project_id=? AND id=?",
                (entity.workspace_id, entity.project_id, entity.prd_id),
            ).fetchone()
        ):
            raise WorkbenchError("INVALID_REFERENCE", "Preview PRD does not belong to project")
        if kind == "work_item":
            if entity.display_key != f"{project[0]}-{entity.number}":
                raise WorkbenchError("CORRUPT_DATA", "Item display key does not match project", 500)
            for field, table in (("prd_id", "wb_prds"), ("parent_item_id", "wb_work_items")):
                value = getattr(entity, field)
                if (
                    value
                    and not self.db.execute(
                        f"SELECT 1 FROM {table} WHERE workspace_id=? AND project_id=? AND id=?",
                        (entity.workspace_id, entity.project_id, value),
                    ).fetchone()
                ):
                    raise WorkbenchError("INVALID_REFERENCE", f"{field} does not belong to project")
            if writable:
                parents = dict(
                    self.db.execute(
                        "SELECT id,parent_item_id FROM wb_work_items WHERE workspace_id=? AND project_id=?",
                        (entity.workspace_id, entity.project_id),
                    )
                )
                parents[entity.id] = entity.parent_item_id
                validate_parents(parents)
        if (
            kind == "comment"
            and not self.db.execute(
                "SELECT 1 FROM wb_work_items WHERE workspace_id=? AND id=?", (entity.workspace_id, entity.item_id)
            ).fetchone()
        ):
            raise WorkbenchError("INVALID_REFERENCE", "Comment item does not belong to workspace")

    def insert(self, kind: str, entity):
        """Internal primitive; caller owns the command transaction."""
        if not self.db.in_transaction:
            raise RuntimeError("Workbench insert requires an owning transaction")
        self._references(kind, entity, writable=True)
        table, _ = ENTITIES[kind]
        data = self._encode(entity)
        self.db.execute(
            f"INSERT INTO {table}({','.join(data)}) VALUES({','.join('?' for _ in data)})", tuple(data.values())
        )
        if kind == "project":
            self.db.execute("INSERT INTO wb_project_counters VALUES(?,1)", (entity.id,))

    def replace(self, kind: str, entity, expected_revision: int):
        if not self.db.in_transaction:
            raise RuntimeError("Workbench update requires an owning transaction")
        workspace_id = entity.id if kind == "workspace" else entity.workspace_id
        previous = self.get(kind, workspace_id, entity.id)
        if previous.revision != expected_revision or entity.revision != expected_revision + 1:
            raise WorkbenchError("REVISION_CONFLICT", "Entity changed; refresh before editing")
        immutable = {"id", "created_at", "created_by", "workspace_id", "project_id", "number", "display_key", "item_id"}
        if any(getattr(previous, field) != getattr(entity, field) for field in immutable if hasattr(entity, field)):
            raise WorkbenchError("IMMUTABLE_IDENTITY", "Entity identity cannot change")
        if kind == "project" and previous.key != entity.key:
            counter = self.db.execute(
                "SELECT next_number FROM wb_project_counters WHERE project_id=?", (entity.id,)
            ).fetchone()
            if counter[0] > 1:
                raise WorkbenchError("IMMUTABLE_KEY", "Project key is frozen after its first item")
        self._references(kind, entity, writable=True)
        data = self._encode(entity)
        table, _ = ENTITIES[kind]
        scope = "id" if kind == "workspace" else "workspace_id"
        changed = self.db.execute(
            f"UPDATE {table} SET {','.join(key + '=?' for key in data)} WHERE {scope}=? AND id=? AND revision=?",
            (*data.values(), workspace_id, entity.id, expected_revision),
        ).rowcount
        if changed != 1:
            raise WorkbenchError("REVISION_CONFLICT", "Entity changed; refresh before editing")

    def allocate_item(self, request, actor: str, *, entity_id: str | None = None):
        if not self.db.in_transaction:
            raise RuntimeError("Ticket allocation requires an owning transaction")
        project = self.get("project", request.workspace_id, request.project_id)
        row = self.db.execute(
            "SELECT next_number FROM wb_project_counters WHERE project_id=?", (project.id,)
        ).fetchone()
        if row is None:
            raise WorkbenchError("CORRUPT_DATA", "Missing project counter", 500)
        number = row[0]
        self.db.execute("UPDATE wb_project_counters SET next_number=next_number+1 WHERE project_id=?", (project.id,))
        stamp = now()
        item = WorkItem.model_validate(
            {
                **request.model_dump(exclude={"command_id"}),
                "id": entity_id or identity(),
                "number": number,
                "display_key": f"{project.key}-{number}",
                "status": "BACKLOG",
                "revision": 1,
                "contract_revision": 1,
                "created_at": stamp,
                "updated_at": stamp,
                "created_by": actor,
                "updated_by": actor,
            }
        )
        self.insert("work_item", item)
        return self.get("work_item", item.workspace_id, item.id)

    def list(
        self,
        kind: str,
        workspace_id: str,
        *,
        project_id: str | None = None,
        item_id: str | None = None,
        priority: int | None = None,
        agent_id: str | None = None,
        item_type: str | None = None,
        status: str | None = None,
        search: str = "",
        limit: int = 50,
        after: tuple[int, str] | str | None = None,
        include_archived: bool = False,
    ):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise WorkbenchError("INVALID_PAGE", "Page size must be between 1 and 200", 422)
        if len(search) > 200:
            raise WorkbenchError("INVALID_SEARCH", "Search is limited to 200 characters", 422)
        if kind not in {"workspace", "project", "prd", "work_item", "comment"}:
            raise WorkbenchError("INVALID_PAGE", "Entity kind is not pageable", 422)
        table, model = ENTITIES[kind]
        clauses, params = ["id=?" if kind == "workspace" else "workspace_id=?"], [workspace_id]
        for column, value, allowed in (
            ("item_id", item_id, "comment"),
            ("priority", priority, "work_item"),
            ("assigned_agent_id", agent_id, "work_item"),
            ("type", item_type, "work_item"),
        ):
            if value is not None:
                if kind != allowed:
                    raise WorkbenchError("INVALID_PAGE", "Filter is unsupported for this entity", 422)
                clauses.append(column + "=?")
                params.append(value)
        if project_id is not None:
            if kind not in {"prd", "work_item"}:
                raise WorkbenchError("INVALID_PAGE", "Project filter is unsupported", 422)
            clauses.append("project_id=?")
            params.append(project_id)
        if status is not None:
            if kind not in {"prd", "work_item"}:
                raise WorkbenchError("INVALID_PAGE", "Status filter is unsupported", 422)
            clauses.append("status=?")
            params.append(status)
        if not include_archived:
            clauses.append(
                "deleted_at IS NULL"
                if kind == "comment"
                else "status!='ARCHIVED'"
                if kind == "prd"
                else "archived_at IS NULL"
            )
        if search:
            column = "name" if kind in {"workspace", "project"} else "body" if kind == "comment" else "title"
            literal = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            if kind == "work_item":
                clauses.append("(title LIKE ? ESCAPE '\\' OR display_key LIKE ? ESCAPE '\\')")
                params.extend([literal, literal])
            else:
                clauses.append(f"{column} LIKE ? ESCAPE '\\'")
                params.append(literal)
        order = "priority,id" if kind == "work_item" else "id"
        if after is not None:
            if kind == "work_item":
                if (
                    not isinstance(after, tuple)
                    or len(after) != 2
                    or type(after[0]) is not int
                    or not isinstance(after[1], str)
                ):
                    raise WorkbenchError("INVALID_PAGE", "Item cursor requires priority and id", 422)
                clauses.append("(priority,id)>(?,?)")
                params.extend(after)
            else:
                if not isinstance(after, str):
                    raise WorkbenchError("INVALID_PAGE", "Cursor requires id", 422)
                clauses.append("id>?")
                params.append(after)
        with self.router.lock:
            rows = self.db.execute(
                f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY {order} LIMIT ?", (*params, limit)
            ).fetchall()
            entities = [self._decode(model, row) for row in rows]
            for entity in entities:
                self._references(kind, entity, writable=False)
            return entities
