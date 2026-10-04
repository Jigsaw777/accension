"""Shared typed configuration and administration for local UI, CLI and MCP."""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path

import yaml

from .config import ROLE_NAMES, Privacy, Repository, RolePolicy, Settings
from .privacy import is_local
from .safety import digest, repo_lock, safe_path, validation_argv
from .schema import Model, Provider

PROFILE_ROUTING = {
    "preset",
    "quality_slos",
    "local_first",
    "cost_weight",
    "latency_weight",
    "failure_risk_weight",
    "cache_hit_bonus",
    "local_execution_bonus",
    "historical_success_bonus",
    "allow_below_slo",
    "resource_aware",
    "available_ram_mb",
    "allow_unknown_pricing",
    "unknown_input_price",
    "unknown_output_price",
    "max_cost_confirmation",
}


class Management:
    def __init__(self, engine):
        self.engine = engine
        self.settings = engine.settings
        self.path = safe_path(self.settings.home, "config/local.yaml")
        self.revision = digest(self.path)
        self.lock = asyncio.Lock()

    def save(self, changes):
        """Validate before atomic persistence. Reject stale/concurrent writers."""
        if set(changes) != {"savings"} and (
            self.engine.store.active_calls or self.engine.active_runs or self.engine.active_operations
        ):
            raise ValueError("An inference or repository run is active; retry configuration after it finishes")
        candidate = Settings.model_validate({**self.settings.model_dump(), **changes})
        with repo_lock(self.settings.home):
            if digest(self.path) != self.revision:
                raise ValueError("Configuration changed in another process; restart this client before saving")
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            data = {**(data or {}), **changes, "schema_version": 2}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                backup = self.settings.state / "backups" / ("local-" + str(time.time_ns()) + ".yaml")
                backup.parent.mkdir(exist_ok=True)
                shutil.copy2(self.path, backup)
            temp = self.path.with_name("local.yaml.saving")
            temp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            temp.replace(self.path)
            self.path.chmod(0o600)
            self.revision = digest(self.path)
        for name in changes:
            setattr(self.settings, name, getattr(candidate, name))
        self.engine.router.classifier.policy = self.settings.routing
        self.engine.router.arbitration.routing = self.settings.routing
        self.engine.store.clear_cache()
        return {"saved": True, "schema_version": 2}

    def manifests(self):
        return self.engine.providers.plugins.manifests()

    def providers(self):
        from collections import Counter

        counts = Counter(m.provider for m in self.settings.models)
        return [
            {
                "id": name,
                "kind": p.kind,
                "enabled": p.enabled,
                "local": is_local(p),
                "endpoint": p.endpoint,
                "auth": p.auth,
                "credential_configured": bool(
                    p.credential_ref
                    or p.api_key_env
                    or p.azure_identity
                    or p.auth in {"aws_chain", "google_adc", "azure_identity", "command"}
                ),
                "groups": p.groups,
                "health": self.engine.store.provider_health(name),
                "models": counts[name],
            }
            for name, p in self.settings.providers.items()
        ]

    def connect(self, name, kind, fields, secret=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Provider name must contain only letters, numbers, underscores or dashes")
        manifest = self.engine.providers.plugins.get(kind).manifest()
        allowed = {field.name for field in manifest.fields} | {
            "auth",
            "protocol",
            "enabled",
            "groups",
            "include_models",
            "exclude_models",
        }
        if set(fields) - allowed:
            raise ValueError("Unknown provider fields")
        data = (
            self.settings.providers[name].model_dump()
            if name in self.settings.providers
            else {
                "kind": kind,
                "local": manifest.local,
                "endpoint": manifest.default_endpoint,
                "auth": manifest.authentication[0],
                "protocol": manifest.protocols[0],
            }
        )
        data["kind"] = kind
        for field, value in fields.items():
            if field == "api_key":
                raise ValueError("Send credentials through the separate secret field")
            if field.startswith("options."):
                data.setdefault("options", {})[field.split(".", 1)[1]] = value
            else:
                data[field] = value
        if data["auth"] not in manifest.authentication + ["auto"] or data["protocol"] not in manifest.protocols:
            raise ValueError("Authentication or protocol is unsupported by this provider")
        for field in manifest.fields:
            value = (
                data.get("options", {}).get(field.name.split(".", 1)[1])
                if field.name.startswith("options.")
                else data.get(field.name)
            )
            if field.required and not value:
                raise ValueError("Required field: " + field.label)
        if secret:
            from .schema import uid

            data["credential_ref"] = "provider/" + name + "/" + uid()
        provider = Provider.model_validate(data)
        providers = {k: p.model_dump() for k, p in self.settings.providers.items()}
        providers[name] = provider.model_dump()
        Settings.model_validate({**self.settings.model_dump(), "providers": providers})
        if secret:
            from .credentials import CredentialStore

            CredentialStore(self.settings).save(data["credential_ref"], secret)
        try:
            self.save({"providers": providers})
        except BaseException:
            if secret:
                CredentialStore(self.settings).delete(data["credential_ref"])
            raise
        return {"saved": True, "provider": name, "credential_saved": bool(secret)}

    def remove_provider(self, name):
        if name not in self.settings.providers:
            raise ValueError("Unknown provider instance")
        remaining = [m for m in self.settings.models if m.provider != name]
        self.save(
            {
                "providers": {key: value.model_dump() for key, value in self.settings.providers.items() if key != name},
                "models": [m.model_dump() for m in remaining if m.id in self.engine.configured_model_ids],
            }
        )
        self.settings.models = remaining
        self.engine.configured_model_ids.intersection_update(m.id for m in remaining)
        with self.engine.store.transaction():
            self.engine.store.db.execute("DELETE FROM model_registry WHERE provider=?", (name,))
            detected = self.engine.store.metadata("detected-providers") or {}
            detected.pop(name, None)
            self.engine.store.metadata("detected-providers", detected)
        return {"removed": name, "history_preserved": True}

    def update_model(self, model_id, fields):
        allowed = {
            "enabled",
            "input_price",
            "output_price",
            "cached_input_price",
            "cache_write_price",
            "context_window",
            "max_output",
            "quality_priors",
        }
        allowed |= {name for name in Model.model_fields if name.startswith("supports_")}
        if set(fields) - allowed:
            raise ValueError("Unknown editable model field")
        original = next((m for m in self.settings.models if m.id == model_id), None)
        if original is None:
            raise ValueError("Unknown model")
        data = {**original.model_dump(), **fields}
        if any(key.endswith("price") for key in fields):
            data.update(pricing_status="user_supplied", pricing_source="user_override", pricing_updated_at=time.time())
        evidence = dict(data["capability_evidence"])
        for key, value in fields.items():
            if key.startswith("supports_"):
                evidence[key[9:]] = {"source": "user_override", "supported": value, "timestamp": time.time()}
        data["capability_evidence"] = evidence
        model = Model.model_validate(data)
        configured = [
            m.model_dump()
            for m in self.settings.models
            if m.id in self.engine.configured_model_ids and m.id != model_id
        ] + [model.model_dump()]
        # Declarative overrides are persisted; other discovered records stay in SQLite.
        dynamic = [m for m in self.settings.models if m.id not in self.engine.configured_model_ids and m.id != model_id]
        self.save({"models": configured})
        self.settings.models.extend(dynamic)
        self.engine.configured_model_ids.add(model_id)
        self.engine.store.save_model(model)
        return {"saved": True, "model": model_id}

    def set_role(self, role, data):
        if role not in ROLE_NAMES:
            raise ValueError("Unknown role")
        policy = RolePolicy.model_validate(data)
        return self.save(
            {
                "roles": {
                    **{key: value.model_dump() for key, value in self.settings.roles.items()},
                    role: policy.model_dump(),
                }
            }
        )

    def set_policy(self, data):
        if set(data) - {"preset", "routing", "budgets", "control_plane"}:
            raise ValueError("Unknown policy fields")
        changes = {}
        if "preset" in data:
            from .policy import preset

            routing, control = preset(self.settings, data["preset"])
            changes.update(routing=routing.model_dump(), control_plane=control.model_dump())
        for key in ("routing", "budgets", "control_plane"):
            if key in data:
                if key == "routing" and set(data[key]) - PROFILE_ROUTING:
                    raise ValueError("Use trusted local configuration for executable classifier settings")
                changes[key] = {**changes.get(key, getattr(self.settings, key).model_dump()), **data[key]}
        return self.save(changes)

    def register_repository(self, path, mode="LOCAL_ONLY", validation=None, never_send=None):
        root = Path(path).resolve(strict=True)
        if not root.is_dir() or root == Path.home() or root == Path(root.anchor):
            raise ValueError("Register a specific project directory")
        validation = validation or {}
        for name in validation:
            validation_argv(validation, name)
        repo = Repository(
            path=str(root),
            validation=validation,
            allow_cloud=mode != "LOCAL_ONLY",
            privacy=Privacy(mode=mode, never_send=never_send or []),
        )
        repositories = [r.model_dump() for r in self.settings.repositories if Path(r.path).resolve() != root] + [
            repo.model_dump()
        ]
        return self.save({"repositories": repositories})

    def export_profile(self):
        return {
            "schema_version": 2,
            "routing": {k: v for k, v in self.settings.routing.model_dump().items() if k in PROFILE_ROUTING},
            "budgets": self.settings.budgets.model_dump(),
            "control_plane": self.settings.control_plane.model_dump(),
            "roles": {
                k: {**v.model_dump(), "model": None, "strategy": "disabled" if v.strategy == "disabled" else "auto"}
                for k, v in self.settings.roles.items()
            },
        }

    def import_profile(self, data):
        if (
            not isinstance(data, dict)
            or set(data) - {"schema_version", "routing", "budgets", "control_plane", "roles"}
            or data.get("schema_version") != 2
        ):
            raise ValueError("Not a supported declarative routing profile")
        if set(data.get("routing", {})) - PROFILE_ROUTING:
            raise ValueError("Profile includes unsupported or executable settings")
        roles = {name: RolePolicy.model_validate(value) for name, value in data.get("roles", {}).items()}
        if any(value.model for value in roles.values()):
            raise ValueError("Shared profiles may not reference private model identifiers")
        changes = {
            key: {**getattr(self.settings, key).model_dump(), **data[key]}
            for key in ("routing", "budgets", "control_plane")
            if key in data
        }
        if roles:
            changes["roles"] = {
                **{k: v.model_dump() for k, v in self.settings.roles.items()},
                **{k: v.model_dump() for k, v in roles.items()},
            }
        candidate = Settings.model_validate({**self.settings.model_dump(), **changes})
        from .privacy import fully_local

        if (fully_local(self.settings) and not fully_local(candidate)) or (
            self.settings.control_plane.routing_location == "local-only"
            and candidate.control_plane.routing_location == "hybrid"
        ):
            raise ValueError("Profile would relax privacy; change privacy explicitly before importing")
        for name, old in self.settings.roles.items():
            new = candidate.roles[name]
            if (old.locality == "local-only" and new.locality != "local-only") or (
                old.strategy == "disabled" and new.strategy != "disabled"
            ):
                raise ValueError("Profile would relax role restrictions; change that role explicitly before importing")
        return self.save(changes)

    def dashboard(self):
        store = self.engine.store
        rows = [
            dict(row)
            for row in store.db.execute(
                "SELECT model, count(*) calls, avg(latency) latency, sum(cost) estimated_cost FROM calls GROUP BY model"
            )
        ]
        local_ids = {m.id for m in self.settings.models if is_local(self.settings.providers[m.provider])}
        total = sum(row["calls"] for row in rows)
        recent = [
            dict(row)
            for row in store.db.execute(
                "SELECT request, MAX(stamp) stamp FROM traces GROUP BY request ORDER BY stamp DESC LIMIT 50"
            )
        ]
        return {
            "costs": store.costs(),
            "cache": store.cache_stats(),
            "models": rows,
            "profiles": [dict(row) for row in store.db.execute("SELECT * FROM profiles")],
            "recent": recent,
            "local_execution_percent": round(
                100 * sum(row["calls"] for row in rows if row["model"] in local_ids) / total, 1
            )
            if total
            else 0,
            "savings": store.economics("summary"),
            "savings_note": "Estimated counterfactual; prices and usage coverage appear in receipt economics.",
        }

    async def action(self, name, data):
        async with self.lock:
            if name.startswith("skill-"):
                from .skill_actions import skill_action

                return skill_action(self.engine.skills, name.removeprefix("skill-"), data)
            if name.startswith("preset-"):
                from .skill_actions import preset_action

                return preset_action(self.engine.skills, name.removeprefix("preset-"), data)
            if name == "provider-connect":
                return self.connect(data["name"], data["kind"], data.get("fields", {}), data.get("secret"))
            if name == "provider-remove":
                return self.remove_provider(data["name"])
            if name == "provider-test":
                provider = self.settings.providers[data["name"]]
                result = await self.engine.providers.health(provider)
                self.engine.store.provider_health(
                    data["name"],
                    "HEALTHY"
                    if result["status"] == "ok"
                    else "AUTH_REQUIRED"
                    if result["status"] == "auth_required"
                    else "UNAVAILABLE",
                    30,
                )
                return result
            if name == "integration-preview":
                from .integrations import preview

                return preview(self.engine, data["client"])
            if name == "integration-install":
                from .integrations import install

                return install(self.engine, data["quote_id"])
            if name == "discover":
                return await self.engine.discovery.refresh(force=True, provider_name=data.get("provider"))
            if name == "discover-local":
                return await self.engine.discovery.discover_local()
            if name == "model-update":
                return self.update_model(data["model"], data["fields"])
            if name == "role-set":
                return self.set_role(data["role"], data["policy"])
            if name == "policy-set":
                return self.set_policy(data)
            if name == "savings-set":
                from .config import Savings

                candidate = Savings.model_validate({**self.settings.savings.model_dump(), **data})
                if candidate.baseline_model:
                    model = next((m for m in self.settings.models if m.id == candidate.baseline_model), None)
                    if not model or is_local(self.settings.providers[model.provider]):
                        raise ValueError("Choose a registered cloud model for the baseline")
                result = self.save({"savings": candidate.model_dump()})
                self.engine.store.economics("changed")
                return result
            if name == "repository-set":
                return self.register_repository(**data)
            if name == "profile-import":
                return self.import_profile(data)
            if name == "route":
                return await self.engine.route(data["task"], repo_path=data.get("repo_path") or None)
            if name in {"plan", "run"}:
                from .schema import Request

                request = Request.model_validate(data)
                if name == "run":
                    return await self.engine.run(request)
                plan = await self.engine.plan(request)
                from .axir import export

                return {
                    "plan_id": plan.plan_id,
                    "request_id": plan.request_id,
                    "axir": export(self.engine, plan.plan_id),
                }
            if name == "execute":
                return await self.engine.execute_plan(data["plan_id"], data["repo_path"])
            if name == "dna-reset":
                from .dna import reset

                return reset(self.engine.store, data.get("model"))
            if name == "lab-compare":
                from .lab import compare

                return compare(self.engine, data["run_id"])
            if name == "calibrate-preview":
                from .calibration import preview

                return preview(self.engine, data.get("models"), data.get("budget"))
            if name == "calibrate-run":
                from .calibration import run

                return await run(self.engine, data["quote_id"], data.get("allow_paid", False))
            if name == "probe":
                from .calibration import probe_model

                model = next(m for m in self.settings.models if m.id == data["model"])
                return await probe_model(
                    self.engine.providers, model, data["capability"], data.get("budget", 0), data.get("approved", False)
                )
            if name == "cache-clear":
                self.engine.store.clear_cache()
                return self.engine.store.cache_stats()
            if name == "profiles-reset":
                self.engine.store.reset_profiles()
                for model in self.settings.models:
                    model.quality = {}
                    model.benchmarked_at = None
                    self.engine.store.save_model(model)
                return {"reset": True}
            if name == "doctor":
                from .doctor import doctor

                return await doctor(self.engine)
            if name == "recovery":
                from .recovery import recover

                return await recover(self.engine, data["id"], data["action"])
            raise ValueError("Unknown management action")
