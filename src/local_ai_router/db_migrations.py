"""Version-safe bootstrap for the router's SQLite database."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

SUPPORTED_VERSION = 9
MIGRATION_LOCK_TIMEOUT = 10

SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS traces(seq INTEGER PRIMARY KEY, request TEXT, stage TEXT, stamp REAL, data TEXT)",
    "CREATE INDEX IF NOT EXISTS traces_request ON traces(request)",
    "CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, request TEXT, session TEXT, day TEXT, role TEXT, model TEXT, frontier INTEGER, cost REAL, state TEXT, usage TEXT, latency REAL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS task_calls(call_id TEXT PRIMARY KEY, task_id TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS task_calls_task ON task_calls(task_id)",
    "CREATE TABLE IF NOT EXISTS profiles(model TEXT, family TEXT, n INTEGER, success REAL, latency REAL, PRIMARY KEY(model,family))",
    "CREATE TABLE IF NOT EXISTS health(model TEXT PRIMARY KEY, failures INTEGER, until REAL)",
    "CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, repo TEXT, fingerprint TEXT, status TEXT, data TEXT)",
    "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS model_registry(id TEXT PRIMARY KEY, provider TEXT, data TEXT NOT NULL, updated REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS provider_health(provider TEXT PRIMARY KEY, status TEXT NOT NULL, until REAL, error_code TEXT, stamp REAL)",
    "CREATE TABLE IF NOT EXISTS benchmarks(model TEXT, category TEXT, stamp REAL, passed INTEGER, evidence TEXT, latency REAL, PRIMARY KEY(model,category))",
    "CREATE TABLE IF NOT EXISTS outcomes(seq INTEGER PRIMARY KEY, model TEXT, family TEXT, stamp REAL, data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS calls_request ON calls(request)",
    "CREATE INDEX IF NOT EXISTS calls_session ON calls(session)",
    "CREATE INDEX IF NOT EXISTS registry_provider_id ON model_registry(provider,id)",
    "CREATE TABLE IF NOT EXISTS dna(model TEXT, dimension TEXT, specialization TEXT, samples INTEGER, value REAL, sources TEXT, stamp REAL, PRIMARY KEY(model,dimension,specialization))",
    "CREATE TABLE IF NOT EXISTS egress(call TEXT PRIMARY KEY, request TEXT, node TEXT, provider TEXT, model TEXT, role TEXT, tokens INTEGER, files TEXT, state TEXT, stamp REAL, measurement TEXT)",
    "CREATE INDEX IF NOT EXISTS egress_request_node ON egress(request,node)",
    "CREATE TABLE IF NOT EXISTS receipts(run TEXT PRIMARY KEY, request TEXT, stamp REAL, data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS receipt_request ON receipts(request)",
    "CREATE TABLE IF NOT EXISTS savings_runs(request TEXT PRIMARY KEY, run TEXT, session TEXT, day TEXT, status TEXT, kind TEXT, stamp REAL, updated REAL, baseline TEXT, data TEXT)",
    "CREATE INDEX IF NOT EXISTS savings_status_updated ON savings_runs(status,updated)",
    "CREATE INDEX IF NOT EXISTS savings_updated ON savings_runs(updated)",
    "CREATE TABLE IF NOT EXISTS usage_records(call TEXT PRIMARY KEY, request TEXT, task TEXT, role TEXT, stamp REAL, data TEXT)",
    "CREATE INDEX IF NOT EXISTS usage_request ON usage_records(request,stamp)",
    "CREATE TABLE IF NOT EXISTS savings_gaps(call TEXT PRIMARY KEY, session TEXT, day TEXT, stamp REAL)",
    "CREATE INDEX IF NOT EXISTS savings_gaps_day ON savings_gaps(day)",
    "CREATE INDEX IF NOT EXISTS savings_gaps_session ON savings_gaps(session)",
    "CREATE INDEX IF NOT EXISTS savings_gaps_stamp ON savings_gaps(stamp)",
    "CREATE TABLE IF NOT EXISTS savings_factors(request TEXT, kind TEXT, stage TEXT, data TEXT, PRIMARY KEY(request,kind,stage))",
    "CREATE TABLE IF NOT EXISTS savings_totals(bucket TEXT, key TEXT, data TEXT, PRIMARY KEY(bucket,key))",
    "CREATE TABLE IF NOT EXISTS savings_revision(id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER)",
    "INSERT OR IGNORE INTO savings_revision VALUES(1,0)",
)

TABLE_COLUMNS = {
    "cache": {"key", "value", "expires"},
    "traces": {"seq", "request", "stage", "stamp", "data"},
    "calls": {"id", "request", "session", "day", "role", "model", "frontier", "cost", "state", "usage", "latency"},
    "task_calls": {"call_id", "task_id"},
    "profiles": {"model", "family", "n", "success", "latency"},
    "health": {"model", "failures", "until"},
    "runs": {"id", "repo", "fingerprint", "status", "data"},
    "metadata": {"key", "value"},
    "model_registry": {"id", "provider", "data", "updated"},
    "provider_health": {"provider", "status", "until", "error_code", "stamp"},
    "benchmarks": {"model", "category", "stamp", "passed", "evidence", "latency"},
    "outcomes": {"seq", "model", "family", "stamp", "data"},
    "dna": {"model", "dimension", "specialization", "samples", "value", "sources", "stamp"},
    "egress": {
        "call",
        "request",
        "node",
        "provider",
        "model",
        "role",
        "tokens",
        "files",
        "state",
        "stamp",
        "measurement",
    },
    "receipts": {"run", "request", "stamp", "data"},
    "savings_runs": {"request", "run", "session", "day", "status", "kind", "stamp", "updated", "baseline", "data"},
    "usage_records": {"call", "request", "task", "role", "stamp", "data"},
    "savings_gaps": {"call", "session", "day", "stamp"},
    "savings_factors": {"request", "kind", "stage", "data"},
    "savings_totals": {"bucket", "key", "data"},
    "savings_revision": {"id", "value"},
    "skills": {"id", "path", "data", "words"},
    "skill_presets": {"id", "name", "data"},
    "skill_defaults": {"scope", "preset"},
    "skill_recipe_runs": {"run", "recipe", "family", "data"},
    "skill_removed_legacy": {"id"},
}
CORE_TABLES = frozenset(TABLE_COLUMNS) - {
    "skills",
    "skill_presets",
    "skill_defaults",
    "skill_recipe_runs",
    "skill_removed_legacy",
}

# Column declarations also define the layout checked on every reopen.
WORKBENCH_STATEMENTS: list[str] = []
WORKBENCH_COLUMNS: dict[str, set[str]] = {}


def _table(name: str, columns: str, constraints: str = "") -> None:
    declarations = columns.split(";")
    WORKBENCH_COLUMNS[name] = {column.strip().split()[0] for column in declarations}
    body = ",".join(declarations) + ("," + constraints if constraints else "")
    WORKBENCH_STATEMENTS.append(f"CREATE TABLE {name}({body})")


_AUDIT = "revision INTEGER NOT NULL CHECK(revision>0);created_at INTEGER NOT NULL;updated_at INTEGER NOT NULL;created_by TEXT NOT NULL;updated_by TEXT NOT NULL"
_SCOPE = "id TEXT PRIMARY KEY;workspace_id TEXT NOT NULL REFERENCES wb_workspaces(id)"
_ITEM_SCOPE = _SCOPE + ";item_id TEXT NOT NULL"
_ITEM_FK = "FOREIGN KEY(workspace_id,item_id) REFERENCES wb_work_items(workspace_id,id)"

_table(
    "wb_workspaces",
    "id TEXT PRIMARY KEY;name TEXT NOT NULL;description TEXT NOT NULL;privacy_mode TEXT NOT NULL;inference_policy TEXT NOT NULL;execution_policy_json TEXT NOT NULL;review_policy TEXT NOT NULL;"
    + _AUDIT
    + ";archived_at INTEGER",
)
_table(
    "wb_projects",
    _SCOPE
    + ";key TEXT NOT NULL;name TEXT NOT NULL;description TEXT NOT NULL;repository_ref TEXT;default_privacy TEXT NOT NULL;default_priority INTEGER NOT NULL CHECK(default_priority BETWEEN 0 AND 4);max_active_runs INTEGER NOT NULL CHECK(max_active_runs>0);"
    + _AUDIT
    + ";archived_at INTEGER",
    "UNIQUE(workspace_id,key),UNIQUE(workspace_id,id)",
)
_table(
    "wb_project_counters",
    "project_id TEXT PRIMARY KEY REFERENCES wb_projects(id);next_number INTEGER NOT NULL CHECK(next_number>0)",
)
_table(
    "wb_prds",
    _SCOPE
    + ";project_id TEXT NOT NULL;title TEXT NOT NULL;status TEXT NOT NULL;template_id TEXT;template_version INTEGER;template_hash TEXT;spec_json TEXT NOT NULL;"
    + _AUDIT,
    "UNIQUE(workspace_id,project_id,id),FOREIGN KEY(workspace_id,project_id) REFERENCES wb_projects(workspace_id,id)",
)
_table(
    "wb_work_items",
    _SCOPE
    + ";project_id TEXT NOT NULL;number INTEGER NOT NULL CHECK(number>0);display_key TEXT NOT NULL;prd_id TEXT;parent_item_id TEXT;type TEXT NOT NULL;status TEXT NOT NULL;priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 4);risk INTEGER NOT NULL CHECK(risk BETWEEN 0 AND 100);complexity INTEGER NOT NULL CHECK(complexity BETWEEN 0 AND 100);title TEXT NOT NULL;spec_json TEXT NOT NULL;assigned_agent_id TEXT;preferred_agent_role TEXT;preferred_model_id TEXT;execution_kind TEXT NOT NULL CHECK(execution_kind IN ('ENGINE','MANUAL'));privacy_policy TEXT NOT NULL;budget_limit_microusd INTEGER NOT NULL CHECK(budget_limit_microusd>=0);max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 5);engine_max_attempts INTEGER NOT NULL CHECK(engine_max_attempts BETWEEN 1 AND 5);quality_target REAL;review_policy TEXT NOT NULL;reason_code TEXT;resume_state TEXT;active_attempt_id TEXT;contract_revision INTEGER NOT NULL CHECK(contract_revision>0);"
    + _AUDIT
    + ";archived_at INTEGER",
    "UNIQUE(project_id,number),UNIQUE(workspace_id,display_key),UNIQUE(workspace_id,id),UNIQUE(workspace_id,project_id,id),CHECK(parent_item_id IS NULL OR parent_item_id!=id),FOREIGN KEY(workspace_id,project_id) REFERENCES wb_projects(workspace_id,id),FOREIGN KEY(workspace_id,project_id,prd_id) REFERENCES wb_prds(workspace_id,project_id,id),FOREIGN KEY(workspace_id,project_id,parent_item_id) REFERENCES wb_work_items(workspace_id,project_id,id)",
)
_table(
    "wb_dependencies",
    "workspace_id TEXT NOT NULL;blocker_id TEXT NOT NULL;dependent_id TEXT NOT NULL;kind TEXT NOT NULL DEFAULT 'BLOCKS' CHECK(kind='BLOCKS');created_at INTEGER NOT NULL;created_by TEXT NOT NULL",
    "PRIMARY KEY(blocker_id,dependent_id,kind),CHECK(blocker_id!=dependent_id),FOREIGN KEY(workspace_id,blocker_id) REFERENCES wb_work_items(workspace_id,id),FOREIGN KEY(workspace_id,dependent_id) REFERENCES wb_work_items(workspace_id,id)",
)
_table(
    "wb_operations",
    _SCOPE
    + ";kind TEXT NOT NULL;subject_id TEXT;state TEXT NOT NULL;source_revision INTEGER;request_hash TEXT NOT NULL;result_json TEXT;error_code TEXT;created_at INTEGER NOT NULL;updated_at INTEGER NOT NULL",
)
_table(
    "wb_plan_imports",
    _SCOPE
    + ";project_id TEXT NOT NULL;prd_id TEXT;prd_revision INTEGER;source_hash TEXT NOT NULL;operation_id TEXT REFERENCES wb_operations(id);status TEXT NOT NULL;proposal_json TEXT NOT NULL;revision INTEGER NOT NULL;created_at INTEGER NOT NULL",
    "FOREIGN KEY(workspace_id,project_id) REFERENCES wb_projects(workspace_id,id),FOREIGN KEY(workspace_id,project_id,prd_id) REFERENCES wb_prds(workspace_id,project_id,id)",
)
_table(
    "wb_plan_bindings",
    "import_id TEXT NOT NULL REFERENCES wb_plan_imports(id);source_node_id TEXT NOT NULL;work_item_id TEXT NOT NULL REFERENCES wb_work_items(id);source_batch_id TEXT;source_node_hash TEXT NOT NULL",
    "PRIMARY KEY(import_id,source_node_id)",
)
_table(
    "wb_events",
    "sequence INTEGER PRIMARY KEY AUTOINCREMENT;event_id TEXT NOT NULL UNIQUE;workspace_id TEXT NOT NULL REFERENCES wb_workspaces(id);entity_type TEXT NOT NULL;entity_id TEXT NOT NULL;entity_revision INTEGER NOT NULL;event_type TEXT NOT NULL;actor_type TEXT NOT NULL;actor_id TEXT NOT NULL;timestamp INTEGER NOT NULL;schema_version INTEGER NOT NULL DEFAULT 1;payload_json TEXT NOT NULL",
)
_table(
    "wb_entity_versions",
    "workspace_id TEXT NOT NULL REFERENCES wb_workspaces(id);entity_type TEXT NOT NULL;entity_id TEXT NOT NULL;revision INTEGER NOT NULL;sequence INTEGER NOT NULL REFERENCES wb_events(sequence);snapshot_json TEXT NOT NULL;tombstone INTEGER NOT NULL CHECK(tombstone IN (0,1))",
    "PRIMARY KEY(entity_type,entity_id,revision)",
)
_table(
    "wb_commands",
    "workspace_id TEXT NOT NULL;principal_id TEXT NOT NULL;command_id TEXT NOT NULL;kind TEXT NOT NULL;payload_hash TEXT NOT NULL;state TEXT NOT NULL;response_status INTEGER;response_json TEXT;entity_id TEXT;created_at INTEGER NOT NULL",
    "PRIMARY KEY(workspace_id,principal_id,command_id)",
)
_table(
    "wb_snapshot_leases",
    _SCOPE
    + ";principal_id TEXT NOT NULL;sequence INTEGER NOT NULL;filter_hash TEXT NOT NULL;expires_at INTEGER NOT NULL",
)
_table(
    "wb_agent_definitions",
    "id TEXT PRIMARY KEY;name TEXT NOT NULL;role TEXT NOT NULL;adapter_kind TEXT NOT NULL CHECK(adapter_kind='engine');enabled INTEGER NOT NULL CHECK(enabled IN (0,1));capabilities_json TEXT NOT NULL;tools_json TEXT NOT NULL;skills_json TEXT NOT NULL;repository_permissions_json TEXT NOT NULL;allowed_workspaces_json TEXT NOT NULL;allowed_projects_json TEXT NOT NULL;supported_model_roles_json TEXT NOT NULL;privacy_policy TEXT NOT NULL;concurrency_limit INTEGER NOT NULL CHECK(concurrency_limit>0);"
    + _AUDIT,
)
_table(
    "wb_agent_instances",
    "id TEXT PRIMARY KEY;definition_id TEXT NOT NULL REFERENCES wb_agent_definitions(id);owner_generation TEXT NOT NULL;host_instance_id TEXT NOT NULL;state TEXT NOT NULL;started_at INTEGER NOT NULL;last_heartbeat INTEGER NOT NULL;expires_at INTEGER NOT NULL",
)
_table(
    "wb_assignments",
    _ITEM_SCOPE
    + ";item_contract_revision INTEGER NOT NULL;agent_definition_id TEXT NOT NULL REFERENCES wb_agent_definitions(id);instance_id TEXT REFERENCES wb_agent_instances(id);status TEXT NOT NULL CHECK(status IN ('PROPOSED','RESERVED','ACTIVE','RELEASED'));candidate_model_id TEXT;decision_json TEXT NOT NULL;created_at INTEGER NOT NULL;ended_at INTEGER",
    _ITEM_FK,
)
_table(
    "wb_attempts",
    _ITEM_SCOPE
    + ";ordinal INTEGER NOT NULL CHECK(ordinal>0);assignment_id TEXT NOT NULL REFERENCES wb_assignments(id);contract_revision INTEGER NOT NULL;contract_hash TEXT NOT NULL;contract_json TEXT NOT NULL;state TEXT NOT NULL CHECK(state IN ('QUEUED','CLAIMED','RUNNING','VERIFYING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED','RECONCILING'));run_id TEXT UNIQUE REFERENCES runs(id);request_id TEXT;owner_generation TEXT;fence INTEGER NOT NULL;lease_until INTEGER;cancel_requested_at INTEGER;started_at INTEGER;finished_at INTEGER;error_code TEXT",
    _ITEM_FK + ",UNIQUE(item_id,ordinal),UNIQUE(workspace_id,item_id,id)",
)
_table(
    "wb_execution_grants",
    _ITEM_SCOPE
    + ";contract_revision INTEGER NOT NULL;contract_hash TEXT NOT NULL;principal_id TEXT NOT NULL;policy_revision_hash TEXT NOT NULL;expires_at INTEGER NOT NULL;consumed_attempt_id TEXT REFERENCES wb_attempts(id);revoked_at INTEGER",
    _ITEM_FK,
)
_table(
    "wb_leases",
    "resource_key TEXT PRIMARY KEY;owner_generation TEXT NOT NULL;fence INTEGER NOT NULL CHECK(fence>0);expires_at INTEGER NOT NULL;updated_at INTEGER NOT NULL",
)
_table(
    "wb_budget_reservations",
    "attempt_id TEXT PRIMARY KEY REFERENCES wb_attempts(id);item_id TEXT NOT NULL REFERENCES wb_work_items(id);cap_microusd INTEGER NOT NULL CHECK(cap_microusd>=0);state TEXT NOT NULL;created_at INTEGER NOT NULL;settled_at INTEGER",
)
_table(
    "wb_verifications",
    _ITEM_SCOPE
    + ";attempt_id TEXT;kind TEXT NOT NULL CHECK(kind IN ('ENGINE_VALIDATION','MANUAL_VALIDATION','HUMAN_ATTESTATION','AI_REVIEW'));status TEXT NOT NULL;checks_json TEXT NOT NULL;artifacts_hash TEXT NOT NULL;evidence_hash TEXT NOT NULL;source_run_id TEXT REFERENCES runs(id);created_at INTEGER NOT NULL;actor TEXT NOT NULL",
    _ITEM_FK + ",FOREIGN KEY(workspace_id,item_id,attempt_id) REFERENCES wb_attempts(workspace_id,item_id,id)",
)
_table(
    "wb_artifacts",
    _ITEM_SCOPE
    + ";attempt_id TEXT;kind TEXT NOT NULL;relative_path_or_external_reference TEXT NOT NULL;content_hash TEXT NOT NULL;media_type TEXT NOT NULL;size_bytes INTEGER NOT NULL CHECK(size_bytes>=0);created_at INTEGER NOT NULL",
    _ITEM_FK
    + ",FOREIGN KEY(workspace_id,item_id,attempt_id) REFERENCES wb_attempts(workspace_id,item_id,id),UNIQUE(attempt_id,kind,relative_path_or_external_reference)",
)
_table(
    "wb_receipt_snapshots",
    "attempt_id TEXT PRIMARY KEY REFERENCES wb_attempts(id);source_run_id TEXT NOT NULL REFERENCES runs(id);receipt_hash TEXT NOT NULL;receipt_json TEXT NOT NULL;created_at INTEGER NOT NULL",
)
_table(
    "wb_approvals",
    _ITEM_SCOPE
    + ";attempt_id TEXT;subject_kind TEXT NOT NULL;contract_revision INTEGER NOT NULL;evidence_hash TEXT NOT NULL;decision TEXT NOT NULL CHECK(decision IN ('APPROVE','REJECT'));actor_type TEXT NOT NULL;actor_id TEXT NOT NULL;reason TEXT NOT NULL;created_at INTEGER NOT NULL",
    _ITEM_FK + ",FOREIGN KEY(workspace_id,item_id,attempt_id) REFERENCES wb_attempts(workspace_id,item_id,id)",
)
_table("wb_comments", _ITEM_SCOPE + ";body TEXT NOT NULL;" + _AUDIT + ";deleted_at INTEGER", _ITEM_FK)
_table(
    "wb_templates",
    "id TEXT NOT NULL;kind TEXT NOT NULL;version INTEGER NOT NULL CHECK(version>0);content_hash TEXT NOT NULL;source_reference TEXT NOT NULL;definition_json TEXT NOT NULL;enabled INTEGER NOT NULL CHECK(enabled IN (0,1));created_at INTEGER NOT NULL",
    "PRIMARY KEY(id,version)",
)
WORKBENCH_STATEMENTS += [
    "CREATE INDEX wb_board ON wb_work_items(workspace_id,project_id,status,priority,id)",
    "CREATE INDEX wb_item_page ON wb_work_items(workspace_id,priority,id)",
    "CREATE INDEX wb_project_page ON wb_projects(workspace_id,id)",
    "CREATE INDEX wb_prd_page ON wb_prds(workspace_id,project_id,id)",
    "CREATE INDEX wb_dependency_reverse ON wb_dependencies(workspace_id,dependent_id,blocker_id)",
    "CREATE INDEX wb_events_workspace ON wb_events(workspace_id,sequence)",
    "CREATE INDEX wb_events_entity ON wb_events(workspace_id,entity_type,entity_id,sequence)",
    "CREATE INDEX wb_versions_entity ON wb_entity_versions(workspace_id,entity_type,entity_id,sequence DESC)",
    "CREATE INDEX wb_operations_state ON wb_operations(workspace_id,state,updated_at)",
    "CREATE UNIQUE INDEX wb_assignment_active ON wb_assignments(item_id) WHERE status IN ('RESERVED','ACTIVE')",
    "CREATE UNIQUE INDEX wb_attempt_active ON wb_attempts(item_id) WHERE state IN ('QUEUED','CLAIMED','RUNNING','VERIFYING','RECONCILING')",
    "CREATE INDEX wb_attempt_history ON wb_attempts(workspace_id,item_id,ordinal DESC)",
    "CREATE INDEX wb_comment_page ON wb_comments(workspace_id,item_id,id)",
    "CREATE INDEX wb_verification_history ON wb_verifications(workspace_id,item_id,created_at,id)",
    "CREATE INDEX wb_snapshot_expiry ON wb_snapshot_leases(expires_at)",
]
TABLE_COLUMNS.update(WORKBENCH_COLUMNS)
WORKBENCH_COLUMNS["wb_projects"].add("review_policy")

DEVICE_COLUMNS = {
    "wb_devices": "id TEXT PRIMARY KEY;name TEXT NOT NULL;state TEXT NOT NULL;grants_json TEXT NOT NULL;revision INTEGER NOT NULL;created_at INTEGER NOT NULL;revoked_at INTEGER",
    "wb_device_credentials": "id TEXT PRIMARY KEY;device_id TEXT NOT NULL REFERENCES wb_devices(id);secret_digest TEXT NOT NULL;issued_at INTEGER NOT NULL;expires_at INTEGER NOT NULL;revoked_at INTEGER",
    "wb_device_sessions": "id TEXT PRIMARY KEY;device_id TEXT NOT NULL REFERENCES wb_devices(id);credential_id TEXT REFERENCES wb_device_credentials(id);secret_digest TEXT NOT NULL;csrf_digest TEXT NOT NULL;expires_at INTEGER NOT NULL;last_seen_at INTEGER NOT NULL;revoked_at INTEGER",
    "wb_pairings": "id TEXT PRIMARY KEY;workspace_id TEXT NOT NULL REFERENCES wb_workspaces(id);code_digest TEXT NOT NULL UNIQUE;challenge_digest TEXT;client_name TEXT;phrase TEXT;state TEXT NOT NULL;grants_json TEXT NOT NULL;expires_at INTEGER NOT NULL;attempts INTEGER NOT NULL;approved_by TEXT;approved_at INTEGER;consumed_at INTEGER;revision INTEGER NOT NULL",
}
DEVICE_STATEMENTS = [
    f"CREATE TABLE {name}({declarations.replace(';', ',')})" for name, declarations in DEVICE_COLUMNS.items()
]
DEVICE_STATEMENTS += [
    "CREATE INDEX wb_pairings_expiry ON wb_pairings(state,expires_at)",
    "CREATE INDEX wb_device_sessions_expiry ON wb_device_sessions(device_id,expires_at)",
    "CREATE INDEX wb_device_credentials_device ON wb_device_credentials(device_id,expires_at)",
]
for name, columns in DEVICE_COLUMNS.items():
    TABLE_COLUMNS[name] = {column.split()[0] for column in columns.split(";")}


FEATURE_COLUMNS = {
    "wb_agent_definitions": {"model_bindings_json"},
    "wb_attempts": {"queued_at"},
    "calls": {
        "occurred_at",
        "provider_id",
        "model_snapshot_json",
        "usage_source",
        "cost_source",
        "reported_usage_fields_json",
    },
}
FEATURE_STATEMENTS = [
    'ALTER TABLE wb_agent_definitions ADD COLUMN model_bindings_json TEXT NOT NULL DEFAULT \'{"executor":{"mode":"auto","primary_model_id":null,"fallback_model_ids":[]},"repairer":{"mode":"auto","primary_model_id":null,"fallback_model_ids":[]},"reviewer":{"mode":"auto","primary_model_id":null,"fallback_model_ids":[]}}\'',
    "ALTER TABLE wb_attempts ADD COLUMN queued_at INTEGER",
    "ALTER TABLE calls ADD COLUMN occurred_at INTEGER",
    "ALTER TABLE calls ADD COLUMN provider_id TEXT",
    "ALTER TABLE calls ADD COLUMN model_snapshot_json TEXT",
    "ALTER TABLE calls ADD COLUMN usage_source TEXT NOT NULL DEFAULT 'legacy_unknown'",
    "ALTER TABLE calls ADD COLUMN cost_source TEXT NOT NULL DEFAULT 'legacy_unknown'",
    "ALTER TABLE calls ADD COLUMN reported_usage_fields_json TEXT",
    "CREATE TABLE wb_agent_workspaces(agent_id TEXT NOT NULL REFERENCES wb_agent_definitions(id),workspace_id TEXT NOT NULL REFERENCES wb_workspaces(id),PRIMARY KEY(agent_id,workspace_id))",
    "CREATE INDEX wb_agent_workspace ON wb_agent_workspaces(workspace_id,agent_id)",
    "CREATE TABLE wb_activity_entries(id INTEGER PRIMARY KEY,source_key TEXT UNIQUE NOT NULL,workspace_id TEXT NOT NULL REFERENCES wb_workspaces(id),project_id TEXT,item_id TEXT,agent_definition_id TEXT REFERENCES wb_agent_definitions(id),attempt_id TEXT REFERENCES wb_attempts(id),occurred_at INTEGER NOT NULL,actor_type TEXT NOT NULL,actor_id TEXT NOT NULL,event_type TEXT NOT NULL,metadata_json TEXT NOT NULL CHECK(length(CAST(metadata_json AS BLOB))<=8192),source_event_id TEXT REFERENCES wb_events(event_id),FOREIGN KEY(workspace_id,project_id) REFERENCES wb_projects(workspace_id,id),FOREIGN KEY(workspace_id,item_id) REFERENCES wb_work_items(workspace_id,id))",
    "CREATE INDEX wb_activity_item ON wb_activity_entries(workspace_id,item_id,id DESC)",
    "CREATE INDEX wb_activity_agent ON wb_activity_entries(workspace_id,agent_definition_id,id DESC)",
    "CREATE INDEX wb_activity_project ON wb_activity_entries(workspace_id,project_id,id DESC)",
    "CREATE TABLE wb_attempt_facts(attempt_id TEXT PRIMARY KEY REFERENCES wb_attempts(id),assignment_id TEXT NOT NULL REFERENCES wb_assignments(id),agent_definition_id TEXT NOT NULL REFERENCES wb_agent_definitions(id),agent_revision INTEGER NOT NULL,agent_name TEXT NOT NULL,model_bindings_json TEXT NOT NULL,workspace_id TEXT NOT NULL REFERENCES wb_workspaces(id),project_id TEXT NOT NULL,item_id TEXT NOT NULL,adapter TEXT NOT NULL,terminal_state TEXT,enqueued_at INTEGER,started_at INTEGER,finished_at INTEGER,priced_usage_cost_microusd INTEGER,budget_estimate_microusd INTEGER,connector_reported_cost_microusd INTEGER,unknown_call_count INTEGER NOT NULL DEFAULT 0,uncertain_call_count INTEGER NOT NULL DEFAULT 0,verified_outcome INTEGER,metric_version INTEGER NOT NULL DEFAULT 1,updated_at INTEGER NOT NULL,FOREIGN KEY(workspace_id,project_id) REFERENCES wb_projects(workspace_id,id),FOREIGN KEY(workspace_id,item_id) REFERENCES wb_work_items(workspace_id,id))",
    "CREATE INDEX wb_fact_agent ON wb_attempt_facts(workspace_id,agent_definition_id,finished_at)",
    "CREATE INDEX wb_fact_project ON wb_attempt_facts(workspace_id,project_id,finished_at)",
    "CREATE TABLE wb_attempt_call_facts(attempt_id TEXT NOT NULL REFERENCES wb_attempts(id),call_id TEXT NOT NULL REFERENCES calls(id),model_id TEXT NOT NULL,provider_id TEXT,role TEXT NOT NULL,usage_source TEXT NOT NULL,cost_source TEXT NOT NULL,reported_usage_json TEXT,priced_usage_cost_microusd INTEGER,budget_estimate_microusd INTEGER,connector_reported_cost_microusd INTEGER,PRIMARY KEY(attempt_id,call_id))",
    "CREATE INDEX wb_board_active ON wb_work_items(workspace_id,status,priority,id) WHERE archived_at IS NULL",
]
FEATURE_COLUMNS.update(
    {
        "wb_agent_workspaces": set("agent_id workspace_id".split()),
        "wb_activity_entries": set(
            "id source_key workspace_id project_id item_id agent_definition_id attempt_id occurred_at actor_type actor_id event_type metadata_json source_event_id".split()
        ),
        "wb_attempt_facts": set(
            "attempt_id assignment_id agent_definition_id agent_revision agent_name model_bindings_json workspace_id project_id item_id adapter terminal_state enqueued_at started_at finished_at priced_usage_cost_microusd budget_estimate_microusd connector_reported_cost_microusd unknown_call_count uncertain_call_count verified_outcome metric_version updated_at".split()
        ),
        "wb_attempt_call_facts": set(
            "attempt_id call_id model_id provider_id role usage_source cost_source reported_usage_json priced_usage_cost_microusd budget_estimate_microusd connector_reported_cost_microusd".split()
        ),
    }
)
for name, columns in FEATURE_COLUMNS.items():
    TABLE_COLUMNS.setdefault(name, set()).update(columns)

DELIVERY_STATEMENTS = [
    "CREATE TABLE wb_delivery_batches(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES wb_workspaces(id),project_id TEXT NOT NULL,prd_id TEXT NOT NULL,prd_revision INTEGER NOT NULL,import_id TEXT NOT NULL REFERENCES wb_plan_imports(id),import_hash TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,approved_by TEXT NOT NULL,actor_type TEXT NOT NULL CHECK(actor_type='HUMAN'),approved_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,selection_hash TEXT NOT NULL,policy_hash TEXT NOT NULL,max_cost_microusd INTEGER NOT NULL CHECK(max_cost_microusd>=0),created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,cancellation_reason TEXT,FOREIGN KEY(workspace_id,project_id) REFERENCES wb_projects(workspace_id,id))",
    "CREATE TABLE wb_delivery_members(batch_id TEXT NOT NULL REFERENCES wb_delivery_batches(id),item_id TEXT NOT NULL REFERENCES wb_work_items(id),approved_contract_revision INTEGER NOT NULL,approved_contract_hash TEXT NOT NULL,allowed_agent_revisions_json TEXT NOT NULL,model_policy_hash TEXT NOT NULL,member_state TEXT NOT NULL,last_attempt_id TEXT REFERENCES wb_attempts(id),active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),blocked_reason TEXT,PRIMARY KEY(batch_id,item_id))",
    "CREATE UNIQUE INDEX wb_delivery_active_item ON wb_delivery_members(item_id) WHERE active=1",
    "CREATE INDEX wb_delivery_due ON wb_delivery_batches(state,id)",
    "CREATE TABLE wb_delivery_grants(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES wb_delivery_batches(id),principal_id TEXT NOT NULL,device_credential_id TEXT REFERENCES wb_device_credentials(id),scope_hash TEXT NOT NULL,expires_at INTEGER NOT NULL,revoked_at INTEGER)",
    "CREATE TABLE wb_delivery_reservations(attempt_id TEXT PRIMARY KEY REFERENCES wb_attempts(id),batch_id TEXT NOT NULL REFERENCES wb_delivery_batches(id),reserved_microusd INTEGER NOT NULL CHECK(reserved_microusd>=0),settled_microusd INTEGER,state TEXT NOT NULL)",
    "ALTER TABLE wb_attempts ADD COLUMN batch_id TEXT REFERENCES wb_delivery_batches(id)",
]
DELIVERY_COLUMNS = {
    "wb_delivery_batches": set(
        "id workspace_id project_id prd_id prd_revision import_id import_hash state revision approved_by actor_type approved_at expires_at selection_hash policy_hash max_cost_microusd created_at updated_at cancellation_reason".split()
    ),
    "wb_delivery_members": set(
        "batch_id item_id approved_contract_revision approved_contract_hash allowed_agent_revisions_json model_policy_hash member_state last_attempt_id active blocked_reason".split()
    ),
    "wb_delivery_grants": set("id batch_id principal_id device_credential_id scope_hash expires_at revoked_at".split()),
    "wb_delivery_reservations": set("attempt_id batch_id reserved_microusd settled_microusd state".split()),
    "wb_attempts": {"batch_id"},
}
for name, columns in DELIVERY_COLUMNS.items():
    TABLE_COLUMNS.setdefault(name, set()).update(columns)


class UnsupportedDatabaseVersion(RuntimeError):
    """The database was written by a newer Accension version."""


class DatabaseMigrationError(RuntimeError):
    """The database could not be safely initialized or migrated."""


def initialize_database(db: sqlite3.Connection, path: Path) -> None:
    db.execute("PRAGMA busy_timeout=10000")
    version = _user_version(db)
    _check_version(version)
    with _migration_lock(path):
        version = _user_version(db)
        _check_version(version)
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DatabaseMigrationError("SQLite foreign-key enforcement could not be enabled")

        tables = _table_names(db)
        _validate_layout(tables, db, complete=version >= 3, workbench=version >= 4, version=version)
        _verify_database(db)
        if version == SUPPORTED_VERSION:
            _ensure_wal(db)
            return

        if tables:
            _create_verified_backup(db, path.parent / "backups", version)

        try:
            _ensure_wal(db)
            db.execute("BEGIN IMMEDIATE")
            if version < 3:
                for statement in SCHEMA_STATEMENTS:
                    db.execute(statement)
                if "latency" not in _columns(db, "calls"):
                    db.execute("ALTER TABLE calls ADD COLUMN latency REAL DEFAULT 0")
            if version < 4:
                for statement in WORKBENCH_STATEMENTS:
                    db.execute(statement)
            if version < 5:
                db.execute("ALTER TABLE wb_projects ADD COLUMN review_policy TEXT NOT NULL DEFAULT 'AUTO_VERIFY'")
            if version < 6:
                for statement in DEVICE_STATEMENTS:
                    db.execute(statement)
            if version < 7:
                db.execute("ALTER TABLE wb_entity_versions RENAME TO wb_entity_versions_old")
                db.execute(
                    "CREATE TABLE wb_entity_versions(workspace_id TEXT NOT NULL REFERENCES wb_workspaces(id),"
                    "entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,revision INTEGER NOT NULL,"
                    "sequence INTEGER NOT NULL REFERENCES wb_events(sequence),snapshot_json TEXT NOT NULL,"
                    "tombstone INTEGER NOT NULL CHECK(tombstone IN (0,1)),PRIMARY KEY(workspace_id,entity_type,entity_id,revision))"
                )
                db.execute("INSERT INTO wb_entity_versions SELECT * FROM wb_entity_versions_old")
                db.execute("DROP TABLE wb_entity_versions_old")
                db.execute(
                    "CREATE INDEX wb_versions_entity ON wb_entity_versions(workspace_id,entity_type,entity_id,sequence DESC)"
                )
            if version < 8:
                for statement in FEATURE_STATEMENTS:
                    db.execute(statement)
                from .workbench.schema import AgentDefinition

                columns = [column[1] for column in db.execute("PRAGMA table_info(wb_agent_definitions)")]
                for row in db.execute("SELECT * FROM wb_agent_definitions"):
                    data = dict(zip(columns, row, strict=True))
                    data["enabled"] = bool(data["enabled"])
                    for key in tuple(data):
                        if key.endswith("_json"):
                            data[key.removesuffix("_json")] = json.loads(data.pop(key))
                    definition = AgentDefinition.model_validate(data)
                    db.executemany(
                        "INSERT INTO wb_agent_workspaces VALUES(?,?)",
                        [(definition.id, ws) for ws in set(definition.allowed_workspaces)],
                    )
            if version < 9:
                for statement in DELIVERY_STATEMENTS:
                    db.execute(statement)
            _validate_layout(_table_names(db), db, complete=True, workbench=True)
            _verify_database(db)
            db.execute(f"PRAGMA user_version={SUPPORTED_VERSION}")
            db.execute("COMMIT")
        except BaseException as exc:
            if db.in_transaction:
                db.execute("ROLLBACK")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise DatabaseMigrationError(
                f"Database migration from schema version {version} failed; no partial schema upgrade was committed: {exc}"
            ) from exc
        _verify_database(db)


def _user_version(db: sqlite3.Connection) -> int:
    try:
        return db.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise DatabaseMigrationError(f"Cannot read Accension database version: {exc}") from exc


def _check_version(version: int) -> None:
    if version < 0:
        raise DatabaseMigrationError(f"Invalid negative database schema version {version}")
    if version > SUPPORTED_VERSION:
        raise UnsupportedDatabaseVersion(
            f"UNSUPPORTED_DATABASE_VERSION: database schema version {version} is newer than this build supports ({SUPPORTED_VERSION}); use a compatible newer Accension build"
        )


def _table_names(db: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def _validate_layout(
    tables: set[str], db: sqlite3.Connection, *, complete: bool, workbench: bool = False, version=SUPPORTED_VERSION
) -> None:
    unknown = tables - TABLE_COLUMNS.keys()
    if unknown:
        raise DatabaseMigrationError(f"Unrecognized tables in Accension database: {', '.join(sorted(unknown))}")
    required = CORE_TABLES | (WORKBENCH_COLUMNS.keys() if workbench else set())
    if workbench and version >= 9:
        required = required | DELIVERY_COLUMNS.keys()
    if version < 8:
        required = required - (FEATURE_COLUMNS.keys() - {"calls", "wb_attempts", "wb_agent_definitions"})
    elif workbench:
        required = required | FEATURE_COLUMNS.keys()
    if workbench and version >= 6:
        required = required | DEVICE_COLUMNS.keys()
    missing_tables = required - tables
    if complete and missing_tables:
        raise DatabaseMigrationError(
            f"Incomplete Accension database; missing tables: {', '.join(sorted(missing_tables))}"
        )
    for table in tables:
        expected = TABLE_COLUMNS[table]
        if version < 8:
            expected = expected - FEATURE_COLUMNS.get(table, set())
        if version < 9:
            expected = expected - DELIVERY_COLUMNS.get(table, set())
        if table == "wb_projects" and version < 5:
            expected = expected - {"review_policy"}
        if table == "calls" and not complete:
            expected = expected - {"latency"}
        missing = expected - _columns(db, table)
        if missing:
            raise DatabaseMigrationError(
                f"Malformed Accension table {table}; missing columns: {', '.join(sorted(missing))}"
            )


def _verify_database(db: sqlite3.Connection) -> None:
    result = db.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise DatabaseMigrationError(f"Accension database integrity check failed: {result}")
    violation = db.execute("PRAGMA foreign_key_check").fetchone()
    if violation:
        raise DatabaseMigrationError(f"Accension database has a foreign-key violation in table {violation[0]}")


def _ensure_wal(db: sqlite3.Connection) -> None:
    mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() != "wal":
        result = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if result.lower() != "wal":
            raise DatabaseMigrationError(f"Could not enable SQLite WAL mode; SQLite selected {result}")


def _create_verified_backup(db: sqlite3.Connection, directory: Path, version: int) -> Path:
    path = directory / f"state-v{version}-{time.time_ns()}.sqlite3"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        db.execute("BEGIN")
        source_tables = _table_names(db)
        source_counts = {table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in source_tables}
        with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as target:
            target.execute("PRAGMA foreign_keys=ON")
            if target.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise DatabaseMigrationError("Could not enable foreign keys while creating the migration backup")
            db.backup(target)
            result = target.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise DatabaseMigrationError(f"Migration backup integrity check failed: {result}")
            target_tables = _table_names(target)
            target_counts = {
                table: target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in target_tables
            }
            if (
                target.execute("PRAGMA user_version").fetchone()[0] != version
                or target_tables != source_tables
                or target_counts != source_counts
            ):
                raise DatabaseMigrationError("Migration backup did not preserve the source schema and rows")
    except BaseException as exc:
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass  # Cleanup must not mask the failure that prevented migration.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise DatabaseMigrationError(
            f"Could not create a verified pre-migration backup; no schema changes were made: {exc}"
        ) from exc
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
    return path


@contextmanager
def _migration_lock(database_path: Path):
    lock_path = database_path.with_name(database_path.name + ".migration.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        deadline = time.monotonic() + MIGRATION_LOCK_TIMEOUT
        if os.name == "nt":
            import msvcrt

            # Windows permits locking a byte beyond EOF; no shared file write is needed.
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise DatabaseMigrationError(
                            "Timed out waiting for the database migration lock; close other Accension processes and retry"
                        ) from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise DatabaseMigrationError(
                            "Timed out waiting for the database migration lock; close other Accension processes and retry"
                        ) from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
