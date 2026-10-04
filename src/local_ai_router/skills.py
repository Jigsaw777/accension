"""Local text skills, deterministic composition, presets and observed recipe outcomes.

Skills are data, never executable plugins. This module cannot grant permissions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import yaml
from pydantic import Field

from .providers import token_estimate
from .safety import SECRET
from .schema import SkillBinding, SkillContract, Strict, uid
from .store import cache_key


class SkillDescriptor(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    name: str
    description: str = ""
    source: str = "local"
    path: str
    content_hash: str
    enabled: bool = True
    trusted: bool = False
    tags: list[str] = []
    roles: list[str] = []
    task_families: list[str] = []
    requires: list[str] = []
    conflicts_with: list[str] = []
    before: list[str] = []
    after: list[str] = []
    token_size: int = 0
    last_seen: float = 0
    metadata_source: str = "filename"


class SkillPreset(Strict):
    id: str = Field(default_factory=uid)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    ordered_skills: list[str] = []
    enabled: bool = True
    task_tags: list[str] = []
    preferred_roles: list[str] = []
    auto_suggest: bool = False
    token_budget: int | None = Field(default=None, gt=0)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


def read_text(path):
    path = Path(path)
    # Bound the actual read too, including files replaced after stat().
    with path.open("rb") as stream:
        raw = stream.read(250001)
    if len(raw) > 250000:
        raise ValueError("Skill exceeds 250 KB; split it into smaller skills")
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    if SECRET.search(text):
        raise ValueError("Skill contains secret-like text; remove it before use")
    return text, hashlib.sha256(raw).hexdigest()


class SkillRegistry:
    def __init__(self, store):
        self.store, self.settings, self.db = store, store.settings, store.db
        with store.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY, path TEXT UNIQUE, data TEXT, words TEXT);
            CREATE TABLE IF NOT EXISTS skill_presets(id TEXT PRIMARY KEY, name TEXT UNIQUE, data TEXT);
            CREATE TABLE IF NOT EXISTS skill_defaults(scope TEXT PRIMARY KEY, preset TEXT);
            CREATE TABLE IF NOT EXISTS skill_recipe_runs(run TEXT PRIMARY KEY, recipe TEXT, family TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS skill_removed_legacy(id TEXT PRIMARY KEY);
            CREATE INDEX IF NOT EXISTS skill_recipe_family ON skill_recipe_runs(family,recipe);
            """)
        # Existing explicit name:path entries retain their prior trust semantics.
        # Invalid legacy entries remain visible in doctor, without breaking startup.
        for name, path in self.settings.skills.items():
            if self.db.execute("SELECT 1 FROM skill_removed_legacy WHERE id=?", (name,)).fetchone():
                continue
            if not self.db.execute("SELECT 1 FROM skills WHERE id=?", (name,)).fetchone():
                try:
                    self.add(path, skill_id=name, trusted=True, source="legacy-config")
                except (OSError, ValueError):
                    pass

    def get(self, skill_id):
        row = self.db.execute("SELECT data FROM skills WHERE id=?", (skill_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown skill {skill_id}; use accs skill list")
        return SkillDescriptor.model_validate_json(row[0])

    def add(self, path, *, skill_id=None, trusted=False, source="local"):
        path = Path(path).expanduser().resolve(strict=True)
        if path.is_dir():
            path /= "SKILL.md"
        text, digest = read_text(path)
        metadata = {}
        if text.startswith("---\n"):
            parts = text.split("\n---", 1)
            if len(parts) == 2:
                metadata = yaml.safe_load(parts[0][4:]) or {}
                if not isinstance(metadata, dict):
                    raise ValueError("Skill metadata must be a YAML mapping")
        old = self.db.execute("SELECT data FROM skills WHERE path=?", (str(path),)).fetchone()
        previous = SkillDescriptor.model_validate_json(old[0]) if old else None
        name = str(
            metadata.get("name") or path.parent.name if path.name == "SKILL.md" else metadata.get("name") or path.stem
        )
        skill_id = skill_id or (
            previous.id if previous else re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-.")[:100] or "skill"
        )
        collision = self.db.execute("SELECT path FROM skills WHERE id=?", (skill_id,)).fetchone()
        if collision and collision[0] != str(path):
            raise ValueError("Skill ID already belongs to another file; supply a unique --id")
        fields = {
            key: metadata[key]
            for key in (
                "description",
                "tags",
                "roles",
                "task_families",
                "requires",
                "conflicts_with",
                "before",
                "after",
            )
            if key in metadata
        }
        skill = SkillDescriptor(
            id=skill_id,
            name=name,
            path=str(path),
            source=source,
            content_hash=digest,
            trusted=trusted or bool(previous and previous.trusted and previous.content_hash == digest),
            enabled=previous.enabled if previous else True,
            token_size=token_estimate(text),
            last_seen=time.time(),
            metadata_source="frontmatter" if metadata else "filename",
            **fields,
        )
        words = " ".join(sorted(set(re.findall(r"[\w-]+", text.lower()))))
        with self.store.lock:
            self.db.execute("DELETE FROM skill_removed_legacy WHERE id=?", (skill.id,))
            self.db.execute(
                "INSERT OR REPLACE INTO skills VALUES(?,?,?,?)", (skill.id, str(path), skill.model_dump_json(), words)
            )
        return skill.model_dump(exclude={"path"})

    def list(self, offset=0, limit=50):
        rows = self.db.execute(
            "SELECT data FROM skills ORDER BY id LIMIT ? OFFSET ?", (max(1, min(limit, 200)), max(0, offset))
        )
        return {
            "total": self.db.execute("SELECT count(*) FROM skills").fetchone()[0],
            "items": [SkillDescriptor.model_validate_json(r[0]).model_dump(exclude={"path"}) for r in rows],
        }

    def change(self, skill_id, **changes):
        if not set(changes) <= {"enabled", "trusted"}:
            raise ValueError("Only enabled and trusted can be changed here")
        skill = self.get(skill_id)
        if changes.get("trusted"):
            _, digest = read_text(skill.path)
            if digest != skill.content_hash:
                raise ValueError("Skill changed; add it again and inspect it before trusting")
        skill = SkillDescriptor.model_validate({**skill.model_dump(), **changes})
        with self.store.lock:
            self.db.execute("UPDATE skills SET data=? WHERE id=?", (skill.model_dump_json(), skill.id))
        return skill.model_dump(exclude={"path"})

    def remove(self, skill_id):
        self.get(skill_id)
        with self.store.lock:
            if skill_id in self.settings.skills:
                self.db.execute("INSERT OR IGNORE INTO skill_removed_legacy VALUES(?)", (skill_id,))
            self.db.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        return {"removed": skill_id, "file_deleted": False}

    def validate(self, skill_id):
        skill = self.get(skill_id)
        if not skill.enabled or not skill.trusted:
            raise ValueError(f"Skill {skill_id} is disabled or untrusted; inspect it before enabling and trusting")
        text, digest = read_text(skill.path)
        if digest != skill.content_hash:
            raise ValueError(f"Required skill {skill_id} has changed; add it again, inspect it and recompile the plan")
        return skill, text

    def scan(self, roots=None):
        roots = (
            roots
            or self.settings.skill_directories
            or [str(Path.home() / p) for p in (".codex/skills", ".agents/skills", ".claude/skills")]
        )
        added, errors, visited = [], [], 0
        for root in roots:
            root = Path(root).expanduser().resolve()
            if not root.is_dir():
                continue
            for directory, dirs, files in os.walk(root, followlinks=False):
                visited += 1
                depth = len(Path(directory).relative_to(root).parts)
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if depth < 6
                    and not d.startswith(".")
                    and not (Path(directory) / d).is_symlink()
                    and not (hasattr(Path, "is_junction") and (Path(directory) / d).is_junction())
                )
                if visited > 10000:
                    raise ValueError("Skill scan reached 10,000 directories; choose a narrower directory")
                if "SKILL.md" in files and not (Path(directory) / "SKILL.md").is_symlink():
                    try:
                        added.append(self.add(Path(directory) / "SKILL.md", source="scan"))
                    except (OSError, ValueError) as exc:
                        errors.append({"name": Path(directory).name, "error_type": type(exc).__name__})
        return {"items": added, "errors": errors, "note": "New or changed skills need explicit trust before use"}

    def search(self, query, family=None, limit=20, offset=0):
        terms = set(re.findall(r"[\w-]+", query.lower()))
        result = []
        for row in self.db.execute("SELECT data,words FROM skills"):
            skill = SkillDescriptor.model_validate_json(row[0])
            labels = " ".join([skill.name, skill.description, *skill.tags, *skill.task_families]).lower()
            strong = terms & set(re.findall(r"[\w-]+", labels))
            weak = terms & set(row[1].split())
            score = len(strong) * 3 + len(weak) + (5 if family and family in skill.task_families else 0)
            if score or not query and not family:
                result.append(
                    {
                        **skill.model_dump(exclude={"path"}),
                        "score": score,
                        "why": sorted(strong or weak)
                        + (["task family: " + family] if family in skill.task_families else []),
                    }
                )
        offset = max(0, offset)
        return sorted(result, key=lambda s: (-s["score"], s["id"]))[offset : offset + max(1, min(limit, 200))]

    def compose_ids(self, names, budget=None):
        selected = {}
        pending = list(dict.fromkeys(names))
        while pending:
            name = pending.pop(0)
            if name in selected:
                continue
            skill, _ = self.validate(name)
            selected[name] = skill
            pending.extend(skill.requires)
        edges = {name: set(s.requires) | (set(s.after) & selected.keys()) for name, s in selected.items()}
        for name, skill in selected.items():
            conflict = set(skill.conflicts_with) & selected.keys()
            if conflict:
                raise ValueError(f"Skill {name} conflicts with {', '.join(sorted(conflict))}")
            for later in set(skill.before) & selected.keys():
                edges[later].add(name)
        ordered = []
        while edges:
            ready = [name for name, deps in edges.items() if not deps]
            if not ready:
                raise ValueError("Skill ordering contains a cycle; check requires/before/after metadata")
            for name in ready:
                ordered.append(selected[name])
                del edges[name]
            for deps in edges.values():
                deps.difference_update(ready)
        ceiling = min(self.settings.active_skill_token_budget, budget or self.settings.active_skill_token_budget)
        if sum(s.token_size for s in ordered) > ceiling:
            sizes = ", ".join(f"{s.id}: {s.token_size}" for s in ordered)
            raise ValueError(f"Skills exceed {ceiling} active tokens ({sizes}); choose fewer skills")
        return ordered

    def freeze(self, names, preset_id=None, source="explicit", budget=None):
        return SkillContract(
            skills=[SkillBinding(id=s.id, content_hash=s.content_hash) for s in self.compose_ids(names, budget)],
            preset_id=preset_id,
            suggestion_source=source,
            token_budget=min(
                self.settings.active_skill_token_budget, budget or self.settings.active_skill_token_budget
            ),
        )

    def load(self, names, contract=None):
        ordered = self.compose_ids(names, contract.token_budget if contract else None)
        if contract is not None:
            expected = {s.id: s.content_hash for s in contract.skills}
            if len(expected) != len(contract.skills) or any(expected.get(s.id) != s.content_hash for s in ordered):
                raise ValueError("Required skill has changed or is absent from the plan contract; recompile")
            if [s.id for s in ordered] != [s.id for s in contract.skills if s.id in {o.id for o in ordered}]:
                raise ValueError("Skill order differs from the frozen plan; recompile")
        # Re-read immediately before use; validate detects modifications after planning.
        return {s.id: self.validate(s.id)[1] for s in ordered}

    def preset(self, name):
        row = self.db.execute("SELECT data FROM skill_presets WHERE id=? OR name=?", (name, name)).fetchone()
        if not row:
            raise ValueError("Unknown preset; use accs preset list")
        return SkillPreset.model_validate_json(row[0])

    def presets(self, offset=0, limit=50):
        return {
            "total": self.db.execute("SELECT count(*) FROM skill_presets").fetchone()[0],
            "items": [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT data FROM skill_presets ORDER BY name LIMIT ? OFFSET ?",
                    (max(1, min(limit, 200)), max(0, offset)),
                )
            ],
        }

    def save_preset(self, name, skills, *, previous=None, **fields):
        self.compose_ids(skills, fields.get("token_budget"))
        preset = SkillPreset.model_validate(
            {
                **(previous.model_dump() if previous else {}),
                **fields,
                "name": name,
                "ordered_skills": skills,
                "updated_at": time.time(),
            }
        )
        if SECRET.search(preset.model_dump_json()):
            raise ValueError("Preset metadata contains secret-like text")
        conflict = self.db.execute("SELECT id FROM skill_presets WHERE name=? OR id=?", (name, name)).fetchone()
        if conflict and conflict[0] != (previous.id if previous else None):
            raise ValueError("Preset name already exists")
        with self.store.lock:
            self.db.execute(
                "INSERT INTO skill_presets VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,data=excluded.data",
                (preset.id, preset.name, preset.model_dump_json()),
            )
        return preset.model_dump()

    def use(self, name, repo=None, session=None):
        preset = self.preset(name)
        if not preset.enabled:
            raise ValueError("Preset is disabled")
        self.compose_ids(preset.ordered_skills, preset.token_budget)
        scope = (
            "repo:" + cache_key(str(Path(repo).resolve())) if repo else "session:" + session if session else "global"
        )
        with self.store.lock:
            self.db.execute("INSERT OR REPLACE INTO skill_defaults VALUES(?,?)", (scope, preset.id))
        return {"preset": preset.name, "scope": "repository" if repo else "session" if session else "global"}

    def delete_preset(self, name):
        preset = self.preset(name)
        with self.store.transaction():
            self.db.execute("DELETE FROM skill_defaults WHERE preset=?", (preset.id,))
            self.db.execute("DELETE FROM skill_presets WHERE id=?", (preset.id,))
        return {"deleted": preset.name}

    def resolve(self, request, family):
        if request.skill_mode == "off":
            return SkillContract()
        name = request.preset
        if name is None:
            for scope in (
                "repo:" + cache_key(str(Path(request.repo_path).resolve())),
                "session:" + request.session_id,
                "global",
            ):
                row = self.db.execute("SELECT preset FROM skill_defaults WHERE scope=?", (scope,)).fetchone()
                if row:
                    name = row[0]
                    break
        preset = self.preset(name) if name else None
        if preset and not preset.enabled:
            raise ValueError("Selected preset is disabled; choose another preset")
        names = (preset.ordered_skills if preset else []) + request.skills
        source = "preset" if preset else "explicit"
        if request.skill_mode == "auto" and not names:
            recommendation = self.compose(request.task, family=family, learned=True)
            names, source = recommendation["skills"], recommendation["source"]
        return self.freeze(names, preset.id if preset else None, source, preset.token_budget if preset else None)

    def compose(self, task, family=None, learned=False):
        if family is None:
            from .routing import deterministic

            family = deterministic(task).task_family
        matches = [s for s in self.search(task, family) if s["enabled"] and s["trusted"]]
        if learned:
            for recipe in sorted(self.recipes(family), key=lambda r: (-r["observed_success"], -r["samples"], r["id"])):
                if (
                    recipe["samples"] >= 5
                    and recipe["observed_success"] >= 0.7
                    and set(recipe["skills"]) & {s["id"] for s in matches}
                ):
                    try:
                        contract = self.freeze(recipe["skills"])
                        if cache_key(family, [s.model_dump() for s in contract.skills]) != recipe["id"]:
                            continue
                        return {
                            "skills": [s.id for s in contract.skills],
                            "source": "local-observations",
                            "evidence": recipe,
                            "note": "Observed correlation, not proof that skills caused success",
                        }
                    except (ValueError, OSError):
                        continue
        names, skipped = [], []
        for match in matches:
            try:
                names = [s.id for s in self.compose_ids(names + [match["id"]])]
            except (ValueError, OSError):
                skipped.append(match["id"])
        return {
            "skills": names,
            "source": "local-lexical",
            "matches": [{"id": s["id"], "why": s["why"]} for s in matches if s["id"] in names],
            "tokens": sum(self.get(n).token_size for n in names),
            "skipped": skipped,
            "note": "New recommendation — limited evidence",
        }

    def record(self, run, family, contract, *, passed, verified, repairs=0, escalations=0):
        if not contract.skills:
            return
        recipe = cache_key(family, [s.model_dump() for s in contract.skills])
        data = {
            "skills": [s.id for s in contract.skills],
            "passed": bool(passed and verified),
            "verified": bool(verified),
            "repairs": repairs,
            "escalations": escalations,
        }
        with self.store.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO skill_recipe_runs VALUES(?,?,?,?)", (run, recipe, family, json.dumps(data))
            )

    def recipes(self, family=None):
        result = {}
        for row in self.db.execute("SELECT * FROM skill_recipe_runs WHERE (? IS NULL OR family=?)", (family, family)):
            data = json.loads(row["data"])
            item = result.setdefault(
                row["recipe"],
                {
                    "id": row["recipe"],
                    "family": row["family"],
                    "skills": data["skills"],
                    "samples": 0,
                    "verified_runs": 0,
                    "passed": 0,
                    "failed": 0,
                    "repairs": 0,
                    "escalations": 0,
                    "passed_without_escalation": 0,
                },
            )
            item["samples"] += 1
            item["verified_runs"] += data["verified"]
            item["passed"] += data["passed"]
            item["failed"] += not data["passed"]
            item["repairs"] += bool(data["repairs"])
            item["escalations"] += bool(data["escalations"])
            item["passed_without_escalation"] += data["passed"] and not data["escalations"]
        for item in result.values():
            item["observed_success"] = item["passed"] / item["samples"]
            item["evidence"] = "limited" if item["samples"] < 5 else "local observations"
        return list(result.values())

    def reset_recipes(self):
        with self.store.lock:
            self.db.execute("DELETE FROM skill_recipe_runs")
        return {"reset": True}
