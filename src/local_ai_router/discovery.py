"""Refresh only configured inventories; never confuse a model catalog with deployments."""
import asyncio, os, time, json
from urllib.parse import quote
from .schema import Model

class DiscoveryService:
    def __init__(self, engine):
        self.engine = engine
        self.last = 0.0
        self.lock = asyncio.Lock()
        self.status = {}

    async def refresh(self, force=False):
        s, policy = self.engine.settings, self.engine.settings.discovery
        if not policy.enabled or s.mock:
            return {"status": "disabled"}
        async with self.lock:
            if not force and time.monotonic()-self.last < policy.interval_seconds:
                return self.status
            self.last = time.monotonic()
            inventories = {}
            status = {}
            for name in policy.inventory_providers:
                provider = s.providers.get(name)
                if provider is None:
                    continue
                result = await self.engine.providers.health(provider)
                status[name] = result["status"]
                if result["status"] == "ok":
                    inventories[name] = [{"id": x, "capabilities": {}} for x in result.get("models", []) if x]
            if policy.azure_subscription and "foundry" in s.providers:
                token = os.getenv(policy.management_token_env, "")
                if not token and (s.state / "azure-auth-record.json").exists():
                    try:
                        from .azure_auth import get_token
                        token = await asyncio.to_thread(get_token, s, "https://management.azure.com/.default")
                    except Exception:
                        status["foundry"] = "management_login_expired"
                if policy.token_command:
                    try:
                        proc = await asyncio.create_subprocess_exec(*policy.token_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                        try:
                            out, _ = await asyncio.wait_for(proc.communicate(), 10)
                        except BaseException:
                            proc.kill(); await proc.wait(); raise
                        if proc.returncode == 0:
                            token = out.decode().strip()
                    except Exception:
                        status["foundry"] = "management_auth_unavailable"
                if token:
                    try:
                        base = "https://management.azure.com/subscriptions/"+quote(policy.azure_subscription, safe="")+"/resourceGroups/"+quote(policy.azure_resource_group, safe="")+"/providers/Microsoft.CognitiveServices/accounts/"+quote(policy.azure_account, safe="")+"/deployments"
                        url = base + "?api-version=2024-10-01"
                        deployments = []
                        for _ in range(20):
                            r = await self.engine.providers.client.get(url, headers={"Authorization": "Bearer "+token}, timeout=5)
                            r.raise_for_status()
                            data = r.json()
                            deployments.extend(data.get("value", []))
                            url = data.get("nextLink")
                            if not url:
                                break
                            # Never forward an ARM bearer token to an arbitrary nextLink.
                            if not url.startswith(base+"?"):
                                raise ValueError("Unexpected deployment pagination endpoint")
                        else:
                            raise ValueError("Inventory pagination limit reached")
                        inventories["foundry"] = [{"id": d["name"], "capabilities": d.get("properties", {}).get("capabilities", {}),
                                                    "model": d.get("properties", {}).get("model", {})} for d in deployments
                                                   if d.get("properties", {}).get("provisioningState") == "Succeeded"]
                        status["foundry"] = "ok"
                    except Exception as exc:
                        status["foundry"] = "inventory_unavailable:"+type(exc).__name__
                else:
                    status["foundry"] = "management_login_required"
            existing = {m.id: m for m in s.models}
            previously_discovered = self.engine.store.metadata("discovered-model-ids") or []
            discovered_ids = set(previously_discovered)
            added = []
            for provider, inventory in inventories.items():
                active = set()
                for item in inventory:
                    alias = item["id"]
                    model_id = provider + ":" + alias
                    active.add(model_id)
                    if any(m.provider == provider and m.deployment_name == alias and m.id not in discovered_ids for m in s.models):
                        continue
                    config = {**policy.model_defaults, "id": model_id, "provider": provider, "deployment_name": alias}
                    caps = item.get("capabilities", {})
                    mapping = {"chatCompletion": "supports_chat_completions", "responses": "supports_responses_api", "toolCalling": "supports_tools", "jsonObjectResponse": "supports_structured_output"}
                    for key, value in mapping.items():
                        if key in caps:
                            config[value] = str(caps[key]).lower() == "true"
                    if s.providers[provider].local:
                        config.update(tier=1, input_price=0, output_price=0)
                    config.update(policy.overrides.get(alias, {}))
                    config.update(policy.overrides.get(model_id, {}))
                    model = Model.model_validate(config)
                    if model_id in existing:
                        s.models[s.models.index(existing[model_id])] = model
                    else:
                        s.models.append(model)
                        added.append(model_id)
                    self.engine.providers.semaphores.setdefault(model_id, asyncio.Semaphore(model.concurrency_limit))
                    from collections import deque
                    self.engine.providers.windows.setdefault(model_id, deque())
                    discovered_ids.add(model_id)
                # Remove only automatically discovered aliases after a successful complete inventory.
                for model in s.models:
                    if model.id in discovered_ids and model.provider == provider and model.id not in active:
                        model.enabled = False
            self.engine.store.metadata("discovered-model-ids", sorted(discovered_ids))
            self.engine.store.metadata("discovered-models", [m.model_dump() for m in s.models if m.id in discovered_ids])
            self.status = {"providers": status, "added": added, "pricing_policy": "Unknown prices are ineligible; configure discovery.overrides or model defaults"}
            self.engine.store.put("discovery-status", self.status)
            return self.status

    async def watch(self):
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = {"error": type(exc).__name__}
            await asyncio.sleep(self.engine.settings.discovery.interval_seconds)
