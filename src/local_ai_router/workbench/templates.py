"""Bounded declarative templates; no expression language or external resources."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Annotated, Literal

import yaml
from pydantic import Field, model_validator
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from ..schema import Strict
from .schema import Identifier, Name, PRDSpec, Revision, WorkItemSpec
from .service import payload_hash
from .store import WorkbenchError, now

MAX_TEMPLATE_BYTES = 256_000
SPEC_MODELS = {"prd": PRDSpec, "work_item": WorkItemSpec}
REQUIRED = {
    "prd": {"problem_statement", "objectives", "scope", "functional_requirements", "acceptance_criteria"},
    "work_item": {"objective", "acceptance_criteria", "definition_of_done"},
}
HARMLESS_DEFAULTS = {
    "problem_statement",
    "background",
    "objectives",
    "scope",
    "out_of_scope",
    "constraints",
    "assumptions",
    "dependencies",
    "architecture_notes",
    "objective",
    "description",
    "rationale",
    "acceptance_criteria",
    "definition_of_done",
    "verifier_requirements",
    "context_notes",
}


class Section(Strict):
    field: Identifier
    label: Name
    display: Literal["text", "list", "checklist"] = "text"


class ExportSection(Strict):
    field: Identifier
    heading: Name


class Constraints(Strict):
    max_lengths: dict[Identifier, Annotated[int, Field(strict=True, ge=1, le=100_000)]] = {}
    min_counts: dict[Identifier, Annotated[int, Field(strict=True, ge=1, le=100)]] = {}
    allowed_values: dict[Identifier, Annotated[list[Name], Field(min_length=1, max_length=100)]] = {}


class Template(Strict):
    schema_version: Literal[1] = 1
    id: Identifier
    kind: Literal["prd", "work_item"]
    version: Revision
    display_name: Name
    sections: Annotated[list[Section], Field(min_length=1, max_length=100)]
    required_fields: Annotated[list[Identifier], Field(max_length=100)] = []
    defaults: dict = {}
    constraints: Constraints = Field(default_factory=Constraints)
    export_sections: Annotated[list[ExportSection], Field(max_length=100)] = []

    @model_validator(mode="after")
    def vocabulary(self):
        fields = SPEC_MODELS[self.kind].model_fields
        used = [s.field for s in self.sections] + self.required_fields + [s.field for s in self.export_sections]
        used += (
            list(self.constraints.max_lengths)
            + list(self.constraints.min_counts)
            + list(self.constraints.allowed_values)
        )
        if any(field not in fields for field in used):
            raise ValueError("Unknown template field")
        if len({s.field for s in self.sections}) != len(self.sections):
            raise ValueError("Duplicate template section")
        if set(self.defaults) - HARMLESS_DEFAULTS:
            raise ValueError("Template defaults cannot set execution or policy authority")
        SPEC_MODELS[self.kind].model_validate(self.defaults)
        for field in self.constraints.min_counts:
            if fields[field].annotation not in (list[str],) and field not in {
                "objectives",
                "scope",
                "out_of_scope",
                "functional_requirements",
                "nonfunctional_requirements",
                "acceptance_criteria",
                "constraints",
                "assumptions",
                "dependencies",
                "risks",
                "milestones",
                "definition_of_done",
                "relevant_files",
                "allowed_paths",
                "expected_artifacts",
                "required_capabilities",
                "required_tools",
                "required_skills",
                "validation_checks",
                "verifier_requirements",
                "requirement_ids",
            }:
                raise ValueError("Minimum count requires a list field")
        return self

    @property
    def content_hash(self):
        return payload_hash(self.model_dump(mode="json"))

    def document(self, spec, *, complete=False):
        data = {**self.defaults, **spec}
        result = SPEC_MODELS[self.kind].model_validate(data)
        data = result.model_dump(mode="json")
        if complete:
            for field in REQUIRED[self.kind] | set(self.required_fields):
                value = data[field]
                if not value or isinstance(value, str) and not value.strip():
                    raise ValueError(f"Required template field: {field}")
        for field, length in self.constraints.max_lengths.items():
            if len(data[field]) > length:
                raise ValueError(f"Template maximum length exceeded: {field}")
        for field, count in self.constraints.min_counts.items():
            if complete and len(data[field]) < count:
                raise ValueError(f"Template minimum count unmet: {field}")
        for field, choices in self.constraints.allowed_values.items():
            if data[field] and (not isinstance(data[field], str) or data[field] not in choices):
                raise ValueError(f"Template value is outside allowed subset: {field}")
        return result

    def preview(self, spec):
        data = self.document(spec).model_dump(mode="json")
        return [
            {"field": s.field, "label": s.label, "display": s.display, "value": data[s.field]} for s in self.sections
        ]

    def markdown(self, spec):
        data = self.document(spec).model_dump(mode="json")
        sections = self.export_sections or [ExportSection(field=s.field, heading=s.label) for s in self.sections]

        def literal(value):
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "#", "!"):
                text = text.replace(character, "\\" + character)
            return text

        return (
            "\n\n".join(
                "## "
                + literal(section.heading)
                + "\n\n"
                + (
                    "\n".join("- " + literal(entry) for entry in data[section.field])
                    if isinstance(data[section.field], list)
                    else literal(data[section.field])
                )
                for section in sections
            )
            + "\n"
        )


def load_template(raw: str | bytes) -> Template:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if len(raw.encode("utf-8")) > MAX_TEMPLATE_BYTES:
            raise ValueError("Template exceeds size limit")
        if any(marker in raw for marker in ("{{", "{%", "${")):
            raise ValueError("Template expressions/placeholders are unsupported")
        node = yaml.compose(raw, Loader=yaml.SafeLoader)
        seen = set()

        def check(current, depth=0):
            if current is None or depth > 16 or len(seen) >= 4096:
                raise ValueError("Template nesting/collection limit exceeded")
            if id(current) in seen:
                raise ValueError("YAML aliases are unsupported")
            seen.add(id(current))
            if not current.tag.startswith("tag:yaml.org,2002:") or current.tag.rsplit(":", 1)[-1] not in {
                "map",
                "seq",
                "str",
                "int",
                "bool",
                "null",
                "float",
            }:
                raise ValueError("Unsupported YAML tag")
            if isinstance(current, MappingNode):
                keys = set()
                for key, value in current.value:
                    if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in keys:
                        raise ValueError("Template keys must be unique strings")
                    keys.add(key.value)
                    check(key, depth + 1)
                    check(value, depth + 1)
            elif isinstance(current, SequenceNode):
                for value in current.value:
                    check(value, depth + 1)

        check(node)
        return Template.model_validate(yaml.safe_load(raw))
    except (ValueError, TypeError, UnicodeError, RecursionError, yaml.YAMLError) as exc:
        raise WorkbenchError("INVALID_TEMPLATE", "Invalid declarative template: " + str(exc)[:200], 422) from exc


def builtins():
    root = files("local_ai_router").joinpath("data", "templates")
    return [load_template(root.joinpath(name).read_bytes()) for name in ("prd-default.yaml", "work-item-default.yaml")]


def register(db, template: Template, source: str):
    previous = db.execute(
        "SELECT content_hash FROM wb_templates WHERE id=? AND version=?", (template.id, template.version)
    ).fetchone()
    if previous:
        if previous[0] != template.content_hash:
            raise WorkbenchError("TEMPLATE_VERSION_CONFLICT", "Template version contents are immutable")
        return template
    db.execute(
        "INSERT INTO wb_templates VALUES(?,?,?,?,?,?,1,?)",
        (
            template.id,
            template.kind,
            template.version,
            template.content_hash,
            source,
            template.model_dump_json(),
            now(),
        ),
    )
    return template


def seed(db):
    for template in builtins():
        register(db, template, "bundled")


def get(db, template_id, version):
    row = db.execute(
        "SELECT definition_json,content_hash FROM wb_templates WHERE id=? AND version=?", (template_id, version)
    ).fetchone()
    if row is None:
        raise WorkbenchError("TEMPLATE_NOT_FOUND", "Template version not found", 404)
    try:
        template = Template.model_validate_json(row[0])
        if template.content_hash != row[1]:
            raise ValueError("Content hash mismatch")
        return template
    except ValueError as exc:
        raise WorkbenchError("CORRUPT_DATA", "Invalid stored template", 500) from exc
