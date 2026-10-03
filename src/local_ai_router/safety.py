"""Repository containment, hash-checked edits and trusted validation commands."""
from __future__ import annotations
import asyncio, hashlib, json, os, re, subprocess, sys
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

SECRET = re.compile(r"(?i)(?:bearer\s+[\w.-]{12,}|(?:api[_-]?key|password|secret|access_token)\s*[:=]\s*[\"']?[^\s\"',}]{8,}|sk-[A-Za-z0-9_-]{16,}|-----BEGIN .*PRIVATE KEY-----)")
SENSITIVE = re.compile(r"(?i)(^|[/\\])(?:\.env(?:\..*)?|credentials|id_rsa|id_ed25519|.*\.(?:pem|key|pfx|p12))$")
FORBIDDEN_PARTS = {".git", ".router", ".codex", ".agents", ".aws", ".ssh", ".venv", "node_modules"}

def redact(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"(?i)(secret|password|api_key|authorization|access_token)", k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x) for x in value]
    if isinstance(value, str):
        return SECRET.sub("[REDACTED]", value)
    return value

def digest(path: Path):
    if not path.exists():
        return None
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()

def safe_path(root: Path, relative: str, internal: bool = False) -> Path:
    root = root.resolve()
    win = PureWindowsPath(relative)
    parts = relative.replace("\\", "/").split("/")
    if not relative or win.drive or win.root or Path(relative).is_absolute() or any(x in ("", ".", "..") or ":" in x or x.endswith((" ", ".")) for x in parts):
        raise ValueError("Unsafe relative file path")
    checked_parts = parts[1:] if internal and parts[0] == ".router" else parts
    if any(p.lower() in FORBIDDEN_PARTS for p in checked_parts) or SENSITIVE.search(relative):
        raise ValueError("Protected path")
    path = root.joinpath(*parts)
    current = root
    for part in parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Linked paths are forbidden")
        if current.exists() and current.is_file() and current.stat().st_nlink > 1:
            raise ValueError("Hard-linked files are forbidden")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Path escapes repository")
    if win.is_reserved():
        raise ValueError("Reserved Windows path")
    return path

@contextmanager
def repo_lock(root: Path):
    state = safe_path(root, ".router", internal=True)
    state.mkdir(exist_ok=True)
    file = safe_path(root, ".router/execution.lock", internal=True).open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if file.tell() == 0:
                file.write(b"0"); file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        raise ValueError("Another router run owns this repository") from None
    try:
        yield
    finally:
        file.close()

class Edits:
    def __init__(self, root: Path, run: str):
        self.root = root.resolve()
        self.backup = safe_path(root, ".router/backups/" + run, internal=True)
        self.backup.mkdir(parents=True, exist_ok=True)
        self.originals: dict[str, bytes | None] = {}
        self.written: dict[str, str] = {}
        self.identities: dict[str, str] = {}

    def apply(self, changes, allowed: list[str]):
        seen = set()
        prepared = []
        for change in changes:
            key = change.path.replace("\\", "/").casefold()
            if key in seen or key not in {x.replace("\\", "/").casefold() for x in allowed}:
                raise ValueError("Duplicate or undeclared edit")
            seen.add(key)
            path = safe_path(self.root, change.path)
            # Resolve one canonical identity for allowed aliases and all rollback history.
            canonical = os.path.normcase(str(path.relative_to(self.root))).replace("\\", "/")
            display = self.identities.setdefault(canonical, path.relative_to(self.root).as_posix())
            change = change.model_copy(update={"path": display})
            if digest(path) != change.original_sha256:
                raise ValueError("File changed since context was built")
            if SECRET.search(change.content):
                raise ValueError("Secret-like content in generated file")
            prepared.append((change, path))
        for change, path in prepared:
            if change.path not in self.originals:
                original = path.read_bytes() if path.exists() else None
                self.originals[change.path] = original
                backup = safe_path(self.root, (self.backup / (hashlib.sha256(change.path.encode()).hexdigest()+".bak")).relative_to(self.root).as_posix(), internal=True)
                if original is not None:
                    backup.write_bytes(original)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".router-tmp")
            # Exclusive file creation avoids overwriting an unrelated temporary file.
            with temp.open("xb") as f:
                f.write(change.content.encode("utf-8"))
            try:
                if digest(path) != change.original_sha256:
                    raise ValueError("Concurrent edit detected before replacement")
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
            self.written[change.path] = digest(path)
            self._journal()

    def _journal(self):
        journal = safe_path(self.root, (self.backup / "journal.json").relative_to(self.root).as_posix(), internal=True)
        journal.write_text(json.dumps({p: {"original_sha256": hashlib.sha256(v).hexdigest() if v is not None else None, "written_sha256": self.written.get(p), "backup": hashlib.sha256(p.encode()).hexdigest()+".bak"} for p,v in self.originals.items()}, indent=2))

    def rollback(self):
        conflicts = []
        for name, original in self.originals.items():
            path = safe_path(self.root, name)
            if digest(path) != self.written.get(name):
                conflicts.append(name)
                continue
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        return conflicts

def validation_argv(commands: dict[str, list[str]], name: str):
    if name not in commands:
        raise ValueError(f"Unregistered validation check: {name}")
    argv = list(commands[name])
    if not argv:
        raise ValueError("Empty validation command")
    if argv[0] == "{python}":
        argv[0] = sys.executable
    exe = Path(argv[0]).name.lower().removesuffix(".exe")
    # These are operator-configured commands, never arbitrary planner shell strings.
    if exe in {"cmd", "powershell", "pwsh", "bash", "sh", "curl", "wget", "rm", "del", "format"}:
        raise ValueError("Shell/destructive validation entry is forbidden")
    if exe == "git" and any(a in {"push", "reset", "clean", "checkout"} for a in argv[1:]):
        raise ValueError("Mutating Git validation entry is forbidden")
    if exe in {"python", "python3", "python3.12"} and any(a in {"-c", "-m"} for a in argv[1:]):
        if "-c" in argv or ("-m" in argv and argv[argv.index("-m")+1] not in {"pytest", "unittest", "compileall", "ruff", "mypy"}):
            raise ValueError("Only registered Python test/lint modules are allowed")
    return argv

async def validate(root: Path, commands: dict[str, list[str]], names: list[str], timeout: float):
    results = []
    env = {k: v for k,v in os.environ.items() if not re.search(r"(?i)(key|token|secret|password|credential)", k)}
    env["PYTHONNOUSERSITE"] = "1"
    for name in dict.fromkeys(names):
        argv = validation_argv(commands, name)
        # Output to a file prevents unbounded pipe buffering. Only a bounded tail is returned.
        log = safe_path(root, ".router/validation.log", internal=True)
        log.parent.mkdir(exist_ok=True)
        with log.open("wb") as output:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                                        stdout=output, stderr=subprocess.STDOUT,
                                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                                        start_new_session=os.name != "nt")
            timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except (TimeoutError, asyncio.CancelledError) as exc:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    await killer.wait()
                else:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                timed_out = True
        with log.open("rb") as f:
            f.seek(max(0, log.stat().st_size-6000))
            tail = redact(f.read().decode("utf-8", errors="replace"))
        log.write_text(tail, encoding="utf-8")
        results.append({"check": name, "passed": proc.returncode == 0 and not timed_out, "exit_code": proc.returncode, "timeout": timed_out, "output": tail})
    return results
