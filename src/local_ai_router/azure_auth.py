"""Official Azure Identity device login with OS-encrypted persistent cache."""
import json, time
from pathlib import Path

def credential(settings, interactive=False):
    from azure.identity import DeviceCodeCredential, TokenCachePersistenceOptions, AuthenticationRecord
    record_path = settings.state / "azure-auth-record.json"
    record = AuthenticationRecord.deserialize(record_path.read_text()) if record_path.exists() else None
    def handoff(uri, user_code, expires_on):
        (settings.state / "azure-login-handoff.json").write_text(json.dumps({"url": uri, "code": user_code, "expires": str(expires_on)}))
        print("Azure device login ready. See .router/azure-login-handoff.json for sign-in URL and code.", flush=True)
    return DeviceCodeCredential(tenant_id=settings.discovery.azure_tenant, authentication_record=record,
        cache_persistence_options=TokenCachePersistenceOptions(name="local-ai-router", allow_unencrypted_storage=False),
        disable_automatic_authentication=not interactive, prompt_callback=handoff, timeout=600)

def login(settings):
    cred = credential(settings, True)
    try:
        record = cred.authenticate(scopes=["https://management.azure.com/.default"])
        (settings.state / "azure-auth-record.json").write_text(record.serialize())
        return {"status": "authenticated", "cache": "OS-encrypted", "scope": "Azure resource discovery"}
    finally:
        cred.close()
        (settings.state / "azure-login-handoff.json").unlink(missing_ok=True)

def get_token(settings, scope):
    if not (settings.state / "azure-auth-record.json").exists():
        return ""
    cred = credential(settings)
    try:
        return cred.get_token(scope).token
    finally:
        cred.close()
