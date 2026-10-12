"""Declarative template trust boundaries and immutable PRD revision behavior."""

from dataclasses import replace
from importlib.resources import files

import pytest
import yaml

from local_ai_router.store import Store
from local_ai_router.workbench.policy import CAPABILITIES, Principal
from local_ai_router.workbench.schema import ActorType, PRDSpec
from local_ai_router.workbench.service import WorkbenchService
from local_ai_router.workbench.store import WorkbenchError, identity
from local_ai_router.workbench.templates import MAX_TEMPLATE_BYTES, builtins, get, load_template


def complete():
    return {
        "problem_statement": "Need a verified board",
        "objectives": ["Track work"],
        "scope": ["Local board"],
        "functional_requirements": [{"id": "FR-1", "text": "Persist tickets"}],
        "acceptance_criteria": [{"id": "AC-1", "text": "Reopen retains tickets"}],
    }


@pytest.fixture
def service(settings):
    router = Store(settings)
    yield WorkbenchService(router)
    router.close()


@pytest.fixture
def human():
    return Principal("human", ActorType.HUMAN, CAPABILITIES, frozenset({"*"}), True)


def entities(service, human):
    ws = service.create("workspace", {"command_id": identity(), "name": "Test"}, human).body
    project = service.create(
        "project", {"command_id": identity(), "workspace_id": ws["id"], "name": "Test", "key": "WB"}, human
    ).body
    return ws, project


@pytest.mark.parametrize(
    "raw",
    [
        "!!python/object/apply:os.system ['do-not-run']",
        "schema_version: 1\nschema_version: 2",
        "{{ expression }}",
        "x: &alias [1]\ny: *alias",
        "x: &loop [*loop]",
        "x: !include secret",
        "[" * 18 + "1" + "]" * 18,
        "x: " + "a" * MAX_TEMPLATE_BYTES,
        "x: ${ENV}",
        b"\xff",
        "",
        "1",
        "x: 2026-10-12",
    ],
    ids=[
        "constructor",
        "duplicate",
        "expression",
        "alias",
        "recursive",
        "include",
        "depth",
        "size",
        "placeholder",
        "encoding",
        "empty",
        "scalar",
        "timestamp",
    ],
)
def test_template_safe_yaml_and_no_execution(raw):
    with pytest.raises(WorkbenchError) as caught:
        load_template(raw)
    assert caught.value.code == "INVALID_TEMPLATE"


@pytest.mark.parametrize(
    "field,value",
    [
        ("defaults", {"status": "DONE"}),
        ("defaults", {"allowed_paths": ["secret"]}),
        ("defaults", {"privacy_policy": "CLOUD_ALLOWED"}),
        ("defaults", {"validation_checks": ["shell command"]}),
        ("defaults", {"actor": "human"}),
        ("sections", [{"field": "command", "label": "Execute"}]),
        ("validators", ["eval"]),
        ("constraints", {"regex": "(.+)+"}),
    ],
)
def test_template_cannot_weaken_contract(field, value):
    data = builtins()[1].model_dump(mode="json")
    data[field] = value
    with pytest.raises(WorkbenchError):
        load_template(yaml.safe_dump(data))
    data = builtins()[1].model_dump(mode="json")
    data["required_fields"] = []
    template = load_template(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="Required template field"):
        template.document({}, complete=True)


def test_prd_revision_preserves_items(service, human, settings):
    settings.providers = {}
    settings.models = []
    ws, project = entities(service, human)
    prd = service.create(
        "prd",
        {
            "command_id": identity(),
            "workspace_id": ws["id"],
            "project_id": project["id"],
            "title": "PRD",
            "spec": complete(),
        },
        human,
    ).body
    assert prd["template_id"] == "prd-default" and prd["template_version"] == 1
    item = service.create(
        "work_item",
        {
            "command_id": identity(),
            "workspace_id": ws["id"],
            "project_id": project["id"],
            "prd_id": prd["id"],
            "title": "Linked",
            "execution_kind": "MANUAL",
        },
        human,
    ).body
    review = service.prd_command(
        ws["id"], prd["id"], {"command_id": identity(), "expected_revision": 1, "command": "submit_review"}, human
    ).body
    assert review["status"] == "IN_REVIEW"
    machine = replace(human, actor_type=ActorType.MACHINE)
    with pytest.raises(WorkbenchError, match="human"):
        service.prd_command(
            ws["id"], prd["id"], {"command_id": identity(), "expected_revision": 2, "command": "approve"}, machine
        )
    approved = service.prd_command(
        ws["id"], prd["id"], {"command_id": identity(), "expected_revision": 2, "command": "approve"}, human
    ).body
    assert approved["status"] == "APPROVED"
    changed = service.update(
        "prd", ws["id"], prd["id"], {"command_id": identity(), "expected_revision": 3, "title": "Edited"}, human
    ).body
    assert changed["status"] == "DRAFT"
    assert service.get("work_item", ws["id"], item["id"], human).model_dump(mode="json") == item
    revisions = service.revisions("prd", ws["id"], prd["id"], human)
    assert [r.status for r in revisions] == ["DRAFT", "APPROVED", "IN_REVIEW", "DRAFT"]
    assert {r.template_hash for r in revisions} == {prd["template_hash"]}


def test_template_version_hash_roundtrip(service, human):
    template = builtins()[0]
    template = service.import_template(yaml.safe_dump(template.model_dump(mode="json")), human)
    assert service.import_template(yaml.safe_dump(template.model_dump(mode="json")), human) == template
    assert get(service.store.db, template.id, template.version).content_hash == template.content_hash
    changed = template.model_dump(mode="json")
    changed["display_name"] = "New display"
    with pytest.raises(WorkbenchError, match="immutable"):
        service.import_template(yaml.safe_dump(changed), human)
    changed["version"] = 2
    updated = service.import_template(yaml.safe_dump(changed), human)
    assert updated.content_hash != template.content_hash
    assert get(service.store.db, template.id, 1).content_hash == template.content_hash
    with pytest.raises(WorkbenchError, match="not found"):
        get(service.store.db, template.id, 3)
    service.store.db.execute("UPDATE wb_templates SET content_hash=? WHERE id=? AND version=2", ("0" * 64, template.id))
    with pytest.raises(WorkbenchError, match="Invalid stored"):
        get(service.store.db, template.id, 2)


def test_bundled_template_package_resources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    templates = builtins()
    assert [t.id for t in templates] == ["prd-default", "work-item-default"]
    assert files("local_ai_router").joinpath("data", "templates", "prd-default.yaml").is_file()
    assert templates[0].document(complete(), complete=True) == PRDSpec.model_validate(complete())


def test_preview_and_export_literal_text_and_constraints():
    template = builtins()[0]
    spec = complete()
    spec["background"] = '<script>alert("x")</script> [click](https://example.invalid)'
    preview = template.preview(spec)
    assert next(s["value"] for s in preview if s["field"] == "background") == spec["background"]
    markdown = template.markdown(spec)
    assert "<script>" not in markdown and "[click]" not in markdown
    strengthened = template.model_dump(mode="json")
    strengthened["required_fields"] += ["background"]
    strengthened["constraints"] = {
        "max_lengths": {"background": 5},
        "min_counts": {"objectives": 2},
        "allowed_values": {"background": ["hello"]},
    }
    template = load_template(yaml.safe_dump(strengthened))
    with pytest.raises(ValueError, match="maximum"):
        template.document(spec, complete=True)
    spec["background"] = "hello"
    with pytest.raises(ValueError, match="minimum"):
        template.document(spec, complete=True)
    spec["objectives"].append("Second")
    assert template.document(spec, complete=True).background == "hello"
    spec["background"] = "no"
    with pytest.raises(ValueError, match="allowed subset"):
        template.document(spec, complete=True)


def test_empty_draft_cannot_be_reviewed_and_archives_without_deletion(service, human):
    ws, project = entities(service, human)
    draft = service.create(
        "prd",
        {"command_id": identity(), "workspace_id": ws["id"], "project_id": project["id"], "title": "Draft"},
        human,
    ).body
    with pytest.raises(ValueError, match="PRD requires"):
        service.prd_command(
            ws["id"], draft["id"], {"command_id": identity(), "expected_revision": 1, "command": "submit_review"}, human
        )
    archived = service.prd_command(
        ws["id"], draft["id"], {"command_id": identity(), "expected_revision": 1, "command": "archive"}, human
    ).body
    assert archived["status"] == "ARCHIVED"
    assert service.list("prd", ws["id"], human) == []
    assert service.get("prd", ws["id"], draft["id"], human).status == "ARCHIVED"
    with pytest.raises(WorkbenchError, match="unavailable"):
        service.prd_command(
            ws["id"], draft["id"], {"command_id": identity(), "expected_revision": 2, "command": "approve"}, human
        )
