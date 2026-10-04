"""Shared local skill operations for CLI, authenticated UI and MCP."""

import json
from pathlib import Path

from .skills import SkillPreset


def skill_action(registry, action, data):
    name = data.get("name")
    if action == "list":
        return registry.list(data.get("offset", 0), data.get("limit", 50))
    if action == "scan":
        return registry.scan(data.get("directories"))
    if action == "add":
        return registry.add(data["path"], skill_id=data.get("id"), trusted=data.get("trusted", False))
    if action in {"enable", "disable", "trust", "untrust"}:
        return registry.change(
            name, **{"enabled" if action in {"enable", "disable"} else "trusted": action in {"enable", "trust"}}
        )
    if action == "remove":
        return registry.remove(name)
    if action == "info":
        from .skills import read_text

        skill = registry.get(name)
        return {**skill.model_dump(exclude={"path"}), "text": read_text(skill.path)[0]}
    if action == "validate":
        skill, _ = registry.validate(name)
        return {"valid": True, "id": skill.id, "content_hash": skill.content_hash}
    if action == "search":
        return {
            "items": registry.search(
                data.get("query", ""), data.get("task_family"), data.get("limit", 20), data.get("offset", 0)
            )
        }
    if action in {"suggest", "compose"}:
        return registry.compose(data["query"], data.get("task_family"), data.get("learned", False))
    if action == "recipes":
        return {"items": registry.recipes(data.get("task_family"))}
    if action == "recipe":
        if data.get("operation") == "reset":
            return registry.reset_recipes()
        return next((r for r in registry.recipes() if r["id"] == name), {"error": "Unknown recipe"})
    raise ValueError("Unknown skill action")


def preset_action(registry, action, data):
    name = data.get("name")
    if action == "list":
        return registry.presets(data.get("offset", 0), data.get("limit", 50))
    if action == "create":
        return registry.save_preset(
            name,
            data.get("skills") or [],
            **{
                k: data[k]
                for k in ("description", "enabled", "task_tags", "preferred_roles", "auto_suggest", "token_budget")
                if data.get(k) is not None
            },
        )
    if action in {"show", "export"}:
        return registry.preset(name).model_dump()
    if action == "use":
        return registry.use(name, data.get("repo"), data.get("session"))
    if action == "delete":
        return registry.delete_preset(name)
    if action in {"clone", "update", "rename"}:
        old = registry.preset(name)
        fields = old.model_dump(exclude={"id", "name", "ordered_skills", "created_at", "updated_at"})
        fields.update({k: data[k] for k in fields if data.get(k) is not None})
        return registry.save_preset(
            data.get("target") or name,
            data.get("skills") if data.get("skills") is not None else old.ordered_skills,
            previous=None if action == "clone" else old,
            **fields,
        )
    if action == "import":
        preset = SkillPreset.model_validate(data["preset"])
        return registry.save_preset(
            preset.name,
            preset.ordered_skills,
            **preset.model_dump(exclude={"id", "name", "ordered_skills", "created_at", "updated_at"}),
        )
    raise ValueError("Unknown preset action")


def cli_action(args, engine):
    if args.command == "skill":
        data = {
            "name": args.value,
            "path": args.value,
            "id": args.id,
            "query": args.value or "",
            "trusted": args.trust,
            "directories": args.directory,
            "offset": args.offset,
            "limit": args.limit,
            "task_family": args.task_family,
            "learned": args.learned,
        }
        if args.action == "recipe":
            if args.value not in {"show", "reset"}:
                raise ValueError("Use accs skill recipe show ID or accs skill recipe reset")
            data.update(operation=args.value, name=args.target)
        return skill_action(engine.skills, args.action, data)
    data = {
        "name": args.name,
        "target": args.target,
        "skills": args.skill,
        "description": args.description,
        "token_budget": args.token_budget,
        "repo": args.repo,
        "session": args.session,
        "offset": args.offset,
        "limit": args.limit,
    }
    if args.action == "import":
        with Path(args.name).open(encoding="utf-8") as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("Preset file exceeds 1 MB")
        data["preset"] = json.loads(raw)
    result = preset_action(engine.skills, args.action, data)
    if args.action == "export" and args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return {"exported": str(Path(args.output).resolve())}
    return result
