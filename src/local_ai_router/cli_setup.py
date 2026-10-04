"""Terminal setup with non-echo credentials and explicit automation equivalents."""

import getpass
import json
import sys
from pathlib import Path

ALIASES = {"aws": "bedrock", "azure": "foundry", "custom": "openai-compatible"}
PROTOCOLS = {"openai": "openai_chat", "responses": "openai_responses", "anthropic": "anthropic_messages"}


def prompt(label, default=""):
    value = input(label + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def provider_add(manager, args):
    kind = args.kind or args.name
    kind = ALIASES.get(kind, kind)
    if not kind:
        raise ValueError("Choose a provider kind: accs provider add ollama")
    name = args.instance_name or (args.name if args.kind else kind)
    manifest = manager.engine.providers.plugins.get(kind).manifest()
    fields = {}
    for item in args.field:
        if "=" not in item:
            raise ValueError("Provider fields use NAME=VALUE")
        key, value = item.split("=", 1)
        try:
            fields[key] = json.loads(value)
        except ValueError:
            fields[key] = value
    for key, value in {
        "endpoint": args.endpoint,
        "api_key_env": args.credential_env,
        "region": args.region,
        "profile": args.profile,
        "project": args.project,
        "auth": args.auth,
        "protocol": PROTOCOLS.get(args.protocol, args.protocol),
    }.items():
        if value is not None:
            fields[key] = value
    if args.group:
        fields["groups"] = args.group
    if args.include_model:
        fields["include_models"] = args.include_model
    if args.exclude_model:
        fields["exclude_models"] = args.exclude_model
    if args.local:
        fields["local"] = True
        fields.setdefault("auth", "none")
    elif kind in {"openai-compatible", "anthropic-compatible"} and args.endpoint:
        from urllib.parse import urlparse

        if urlparse(args.endpoint).hostname in {"127.0.0.1", "localhost", "::1"}:
            fields["local"] = True
            fields.setdefault("auth", "none")
    if kind == "openai-compatible" and args.protocol == "anthropic":
        kind = "anthropic-compatible"
        manifest = manager.engine.providers.plugins.get(kind).manifest()
    interactive = not args.non_interactive and sys.stdin.isatty()
    if interactive:
        if not args.instance_name and not args.kind:
            name = prompt("Provider instance name", name)
        for field in manifest.fields:
            if field.name in fields or field.type == "password" or field.name in {"api_key_env", "model_ids"}:
                continue
            if field.required or field.name == "endpoint":
                value = prompt(field.label, str(field.default or ""))
                if value:
                    fields[field.name] = value
    secret = None
    auth = fields.get("auth", manifest.authentication[0])
    credential_configured = fields.get("api_key_env") or (
        name in manager.settings.providers and manager.settings.providers[name].credential_ref
    )
    if args.api_key or (interactive and auth == "api_key" and not credential_configured):
        if not sys.stdin.isatty():
            raise ValueError("API key prompts require a terminal; use --credential-env NAME in automation")
        secret = getpass.getpass("Provider API key (stored in OS credential vault): ").strip() or None
    if auth == "api_key" and not credential_configured and not secret:
        raise ValueError("Supply --credential-env NAME, or run this command in a terminal to enter a key privately")
    return manager.connect(name, kind, fields, secret)


def initialize(manager, args):
    interactive = not args.non_interactive and sys.stdin.isatty()
    mode, preset, repo, check = args.mode, args.preset, args.repo, args.validation
    if interactive:
        print("Accension terminal setup — no account required.")
        mode = prompt("Mode: companion or sovereign", mode)
        preset = prompt("Policy: balanced, maximum-savings, quality-first, fully-local", preset)
        repo = repo or prompt("Project folder (blank skips registration)")
        if repo and not check:
            check = prompt("Validation: python-unittest, python-pytest, npm-test, or none", "python-unittest")
    from .config import ControlPlane

    ControlPlane(mode=mode)
    manager.set_policy({"preset": preset, "control_plane": {"mode": mode}})
    registered = None
    if repo:
        checks = {
            "python-unittest": {"tests": ["{python}", "-m", "unittest", "discover", "-v"]},
            "python-pytest": {"tests": ["{python}", "-m", "pytest", "-q"]},
            "npm-test": {"tests": ["npm", "test"]},
            "none": {},
        }
        if check not in checks:
            raise ValueError("Select --validation python-unittest, python-pytest, npm-test, or none")
        manager.register_repository(repo, args.privacy, checks[check])
        registered = str(Path(repo).resolve())
    provider = None
    kind = args.provider or (
        prompt("Provider kind (ollama/openrouter/aws/vertex/azure; blank skips)") if interactive else None
    )
    if kind:
        from argparse import Namespace

        provider = provider_add(
            manager,
            Namespace(
                name=kind,
                kind=None,
                instance_name=args.name,
                field=[],
                endpoint=args.endpoint,
                credential_env=args.credential_env,
                region=args.region,
                profile=None,
                project=args.project,
                auth=None,
                protocol=None,
                group=[],
                include_model=[],
                exclude_model=[],
                local=False,
                non_interactive=not interactive,
                api_key=False,
            ),
        )
    return {
        "status": "initialized",
        "home": str(manager.settings.home),
        "mode": mode,
        "preset": preset,
        "repository": registered,
        "provider": provider,
        "next": "accs doctor; accs provider add ollama; accs model refresh",
    }
