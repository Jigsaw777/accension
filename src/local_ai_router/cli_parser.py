"""Argparse command surface; no optional CLI framework or shell-completion dependency."""

import argparse
import os

from . import __version__

COMMANDS = (
    "init",
    "doctor",
    "ui",
    "start",
    "stop",
    "restart",
    "status",
    "serve",
    "provider",
    "model",
    "role",
    "route",
    "plan",
    "run",
    "execute",
    "inspect",
    "reroute",
    "resume",
    "rollback",
    "receipt",
    "trace",
    "costs",
    "cache",
    "calibrate",
    "plugin",
    "integrate",
    "launch",
    "mode",
    "lab",
    "savings",
    "config",
    "repo",
    "completion",
    "version",
    "mcp",
)
PRIVACY = ["LOCAL_ONLY", "CLOUD_REDACTED", "CLOUD_ALLOWED"]
COMMANDS += ("skill", "preset", "logs")


def common(parser, child=False):
    default = argparse.SUPPRESS if child else None
    parser.add_argument("--home", default=default if child else os.getenv("ROUTER_HOME"), help="Configuration home")
    for name, help in [
        ("mock", "Deterministic fixture models only"),
        ("json", "Machine-readable JSON output"),
        ("no-color", "Plain output (also the default)"),
        ("debug", "Show diagnostic traceback on failure"),
    ]:
        parser.add_argument("--" + name, action="store_true", default=argparse.SUPPRESS if child else False, help=help)


def parser():
    root = argparse.ArgumentParser(prog="accs", description="Accension — local AI execution compiler and control plane")
    root.add_argument("--version", action="version", version="Accension " + __version__)
    common(root)
    sub = root.add_subparsers(dest="command", metavar="COMMAND")
    root.set_defaults(command="ui", port=None, host=None, no_browser=False)

    def command(name, help=""):
        child = sub.add_parser(name, help=help)
        common(child, True)
        return child

    for name in ("serve", "ui", "start", "stop", "restart", "status"):
        child = command(
            name,
            {
                "ui": "Open the local UI",
                "serve": "Run the local hub in this terminal",
                "start": "Start a user-level background hub",
            }.get(name, "Manage the local hub"),
        )
        child.add_argument("--port", type=int)
        child.add_argument("--host")
        if name == "ui":
            child.add_argument("--no-browser", action="store_true")
        if name in {"start", "restart"}:
            child.add_argument("--background", action="store_true", help="Background is the default")
        if name == "serve":
            child.add_argument(
                "--remote", action="store_true", help="Reserved; remote service mode is currently refused"
            )
            child.add_argument("--auth-token-env")
    for name in ("doctor", "costs", "mcp", "enroll", "discover", "azure-login", "version"):
        child = command(name)
        if name == "doctor":
            child.add_argument("--bundle", metavar="FILE.zip")
    child = command("skill", "Find and manage reusable AI skills")
    child.add_argument(
        "action",
        choices=[
            "list",
            "search",
            "info",
            "scan",
            "add",
            "remove",
            "enable",
            "disable",
            "trust",
            "untrust",
            "validate",
            "suggest",
            "compose",
            "recipes",
            "recipe",
        ],
    )
    child.add_argument("value", nargs="?")
    child.add_argument("target", nargs="?")
    child.add_argument("--id")
    child.add_argument("--trust", action="store_true", help="Trust the inspected file being added")
    child.add_argument("--directory", action="append")
    child.add_argument("--task-family")
    child.add_argument("--learned", action="store_true")
    child.add_argument("--offset", type=int, default=0)
    child.add_argument("--limit", type=int, default=50)
    child = command("preset", "Save groups of skills for later")
    child.add_argument(
        "action", choices=["list", "create", "show", "use", "clone", "update", "rename", "delete", "export", "import"]
    )
    child.add_argument("name", nargs="?")
    child.add_argument("target", nargs="?")
    child.add_argument("--skill", action="append")
    for flag in ("description", "repo", "session", "output"):
        child.add_argument("--" + flag)
    child.add_argument("--token-budget", type=int)
    child.add_argument("--offset", type=int, default=0)
    child.add_argument("--limit", type=int, default=50)
    child = command("logs", "View local troubleshooting logs")
    child.add_argument("action", nargs="?", choices=["tail", "show", "path", "export"], default="tail")
    child.add_argument("value", nargs="?")
    child.add_argument("--follow", action="store_true")
    child.add_argument("--level", choices=["ERROR", "WARNING", "INFO", "DEBUG"])
    for flag in ("component", "run", "since", "output"):
        child.add_argument("--" + flag)
    child.add_argument("--limit", type=int, default=100)
    child = command("init", "Terminal setup; no browser required")
    child.add_argument("--non-interactive", action="store_true")
    child.add_argument("--mode", choices=["companion", "sovereign"], default="companion")
    child.add_argument("--preset", default="balanced")
    child.add_argument("--repo")
    child.add_argument("--validation", choices=["python-unittest", "python-pytest", "npm-test", "none"])
    child.add_argument("--privacy", choices=PRIVACY, default="LOCAL_ONLY")
    for name in ("provider", "name", "endpoint", "credential-env", "region", "project"):
        child.add_argument("--" + name)
    for name in ("route", "run", "plan"):
        child = command(
            name,
            {
                "route": "Simulate a route without inference or edits",
                "plan": "Compile a task into a portable execution graph",
                "run": "Compile, execute, verify and record a receipt",
            }[name],
        )
        child.add_argument("task")
        child.add_argument("--repo")
        child.add_argument("--budget", type=float)
        child.add_argument("--constraint", action="append", default=[])
        child.add_argument("--session", default="default")
        child.add_argument("--preset")
        child.add_argument("--skill", action="append", default=[])
        child.add_argument("--skill-mode", choices=["manual", "auto", "off"], default="manual")
        child.add_argument("--privacy", choices=PRIVACY)
        child.add_argument("--local-only", action="store_true")
        child.add_argument("--quality", type=float)
        child.add_argument("--max-cloud-context", type=int)
        child.add_argument("--mode", choices=["safe-auto", "plan-only"], default="safe-auto")
        child.add_argument("--dry-run", action="store_true")
        child.add_argument("--explain", action="store_true")
        if name == "plan":
            child.add_argument("files", nargs="*", help="OLD NEW for plan diff")
            child.add_argument("--export", metavar="FILE.axir.json")
    child = command("execute", "Execute a stored plan or portable AXIR file")
    child.add_argument("plan_id")
    child.add_argument("--repo")
    for name in ("inspect", "reroute"):
        command(name).add_argument("file")
    for name in ("resume", "rollback"):
        command(name).add_argument("run_id")
    command("trace").add_argument("request_id")
    child = command("receipt", "Read execution evidence")
    child.add_argument("run_id")
    child.add_argument("--output")
    child.add_argument("--markdown", action="store_true")
    child = command("provider", "Connect any number of provider instances")
    child.add_argument("action", choices=["list", "add", "test", "refresh", "remove"])
    child.add_argument("name", nargs="?", help="Provider kind when adding, instance ID otherwise")
    child.add_argument("--name", dest="instance_name")
    for name in ("kind", "endpoint", "protocol", "credential-env", "auth", "region", "profile", "project"):
        child.add_argument("--" + name)
    for name in ("field", "group", "include-model", "exclude-model"):
        child.add_argument("--" + name, action="append", default=[])
    for name in ("api-key", "non-interactive", "local", "inference", "allow-paid"):
        child.add_argument("--" + name, action="store_true")
    child.add_argument("--budget", type=float, default=0.01)
    for name in ("model", "models"):
        child = command(name, "Browse, probe and inspect Model DNA" if name == "model" else "Legacy model-list alias")
        if name == "model":
            child.add_argument(
                "action", choices=["list", "search", "info", "probe", "enable", "disable", "refresh", "dna"]
            )
            child.add_argument("name", nargs="?")
            child.add_argument("--reset", action="store_true")
            child.add_argument("--capability", default="text")
            child.add_argument("--budget", type=float, default=0.01)
            child.add_argument("--allow-paid", action="store_true")
        child.add_argument("--provider")
        child.add_argument("--search")
        child.add_argument("--offset", type=int, default=0)
        child.add_argument("--limit", type=int, default=50)
    child = command("role", "Explain or configure capability-based role assignments")
    child.add_argument("action", choices=["list", "explain", "set", "auto", "reset"])
    child.add_argument("name", nargs="?")
    child.add_argument("model_id", nargs="?")
    child.add_argument("--strategy", choices=["auto", "pinned", "preferred", "disabled"])
    child.add_argument("--model")
    child.add_argument("--locality", choices=["local-only", "local-preferred", "any"])
    child.add_argument("--minimum-quality", type=float)
    child = command("calibrate", "Preview or run bounded objective fixtures")
    child.add_argument("action", choices=["preview", "run"], nargs="?", default="preview")
    child.add_argument("--budget", type=float)
    child.add_argument("--model", action="append")
    child.add_argument("--quote-id")
    for name in ("quick", "local-only", "allow-paid"):
        child.add_argument("--" + name, action="store_true")
    child = command("plugin", "Inspect built-in and explicitly trusted plugins")
    child.add_argument("action", choices=["list", "info"])
    child.add_argument("name", nargs="?")
    child = command("integrate", "Preview, apply or undo a client integration")
    child.add_argument("client", choices=["list", "undo", "codex", "claude-code", "claude-desktop"])
    child.add_argument("target", nargs="?")
    child.add_argument("--mode", choices=["companion", "sovereign"], default="companion")
    child.add_argument("--apply", action="store_true", help="Apply the displayed, backed-up change")
    child = command("launch", "Launch a client through the hub, or use --direct")
    child.add_argument("client", choices=["codex", "claude", "claude-code"])
    child.add_argument("--direct", action="store_true")
    child.add_argument("--dry-run", action="store_true")
    child.add_argument("arguments", nargs="*")
    command("mode").add_argument("value", choices=["companion", "sovereign"], nargs="?")
    child = command("lab", "Compare historical routing policies without shadow calls")
    child.add_argument("action", choices=["compare"])
    child.add_argument("run_id")
    child = command("savings", "Read local estimated savings and usage")
    ranges = child.add_mutually_exclusive_group()
    for flag, value in (("today", "today"), ("7d", "7d"), ("30d", "30d"), ("all", "all")):
        ranges.add_argument("--" + flag, dest="period", action="store_const", const=value)
    child.add_argument("--run")
    child.add_argument("--session")
    child.add_argument("--reprice", action="store_true")
    child = command("cache")
    child.add_argument("action", choices=["stats", "clear"])
    child = command("config")
    child.add_argument("action", choices=["validate", "migrate", "export", "import"])
    child.add_argument("--file")
    child = command("repo")
    child.add_argument("action", choices=["add", "list"])
    child.add_argument("path", nargs="?")
    child.add_argument("--check", action="append", default=[], help="Trusted NAME=JSON_ARGV")
    child.add_argument("--allow-cloud", action="store_true")
    child.add_argument("--privacy", choices=PRIVACY)
    child.add_argument("--never-send", action="append", default=[])
    command("completion").add_argument("shell", choices=["bash", "zsh", "fish", "powershell"])
    child = command("eval")
    child.add_argument("--budget", type=float, default=0)
    child.add_argument("--model")
    command("profiles").add_argument("action", choices=["reset"])
    child = command("integration")
    child.add_argument("action", choices=["preview", "install"])
    child.add_argument("client", choices=["codex", "claude-code", "claude-desktop"])
    child = command("recovery")
    child.add_argument("action", choices=["list", "inspect", "resume", "rollback", "discard"])
    child.add_argument("plan_id", nargs="?")
    command("demo").add_argument("--repo")
    return root


def completion(shell):
    words = " ".join(COMMANDS)
    if shell == "bash":
        return "complete -W '" + words + "' accs accension router\n"
    if shell == "zsh":
        return "#compdef accs accension router\n_arguments '1:command:(" + words + ")' '*:file:_files'\n"
    if shell == "fish":
        return "\n".join(f"complete -c accs -n '__fish_use_subcommand' -a {word}" for word in COMMANDS) + "\n"
    return (
        "Register-ArgumentCompleter -Native -CommandName accs,accension,router -ScriptBlock { param($wordToComplete,$commandAst,$cursorPosition) '"
        + words
        + "'.Split(' ') | Where-Object { $_ -like ($wordToComplete + '*') } | ForEach-Object { [System.Management.Automation.CompletionResult]::new($_,$_, 'ParameterValue',$_) } }\n"
    )
