"""Privacy gates shared by routing, transports, discovery and repository context."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from fnmatch import fnmatchcase
from urllib.parse import urlparse

from .errors import PrivacyViolation
from .safety import SECRET, redact

_repository_policy = ContextVar("accension_repository_privacy", default=None)


@contextmanager
def repository_scope(repository):
    token = _repository_policy.set(repository)
    try:
        yield
    finally:
        _repository_policy.reset(token)


def fully_local(settings):
    return settings.control_plane.fully_local or settings.routing.preset == "fully-local"


def is_local(provider):
    if not provider.local:
        return False
    if provider.kind in {"bedrock", "vertex", "gemini"} or provider.auth in {"azure_identity", "aws_chain", "google_adc", "command"} or provider.azure_identity or provider.token_command:
        return False
    if provider.endpoint:
        return urlparse(provider.endpoint).hostname in {"localhost", "127.0.0.1", "::1"}
    # Explicitly configured processes are trusted software, not network sandboxes.
    return provider.kind in {"mcp", "mock"}


def allowed_provider(settings, provider, role="executor", allow_cloud=True):
    from .contracts import current_contract
    local = is_local(provider)
    repo = _repository_policy.get()
    if not local and (fully_local(settings) or not allow_cloud or current_contract().get("mode") == "LOCAL_ONLY" or (repo is not None and not repo.cloud_allowed)):
        return False
    if not local and role in {"classifier", "arbiter"} and settings.control_plane.routing_location == "local-only":
        return False
    policy = settings.roles.get("repairer" if role == "repair" else role)
    if policy and (policy.strategy == "disabled" or (policy.locality == "local-only" and not local)):
        return False
    return True


def guard_provider(settings, provider, role="executor"):
    if not allowed_provider(settings, provider, role):
        raise PrivacyViolation("Privacy policy prohibits this provider for " + role)


def protected_path(name, repository):
    path = name.replace("\\", "/").casefold()
    patterns = [".env", ".env.*", "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/credentials*", "secrets/**"]
    if repository.privacy:
        patterns += [p.replace("\\", "/").casefold() for p in repository.privacy.never_send]
    return any(fnmatchcase(path, pattern) or path == pattern.removesuffix("/**") for pattern in patterns)


def preflight(messages, provider):
    """Protect secrets in every cloud prompt, including dependency summaries."""
    if is_local(provider):
        return messages
    sanitized = redact(messages)
    if sanitized != messages:
        repo = _repository_policy.get()
        from .contracts import current_contract, strictest
        if strictest(repo.privacy_mode if repo else None, current_contract().get("mode")) == "CLOUD_REDACTED":
            return sanitized
        raise PrivacyViolation("Secret-sensitive content cannot be sent to a cloud provider")
    return messages
