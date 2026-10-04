"""Incremental AST/text graph, bounded capsules and lazy skill retrieval."""
from __future__ import annotations
import ast, json, re, subprocess, tempfile, os
from pathlib import Path
from .store import cache_key
from .safety import safe_path, digest, SECRET
from .providers import token_estimate

EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".kts", ".go", ".rs", ".cs", ".md", ".json", ".toml", ".yaml", ".yml"}

def file_names(root: Path):
    # Isolate Git from MCP's stdio handles; file-backed output also bounds timeout cleanup.
    with tempfile.TemporaryFile() as output:
        try:
            # A registered folder must not inherit an unrelated ancestor's ignore rules.
            if not (root / ".git").exists():
                raise FileNotFoundError("No Git metadata at the registered root")
            result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                    cwd=root, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=15, close_fds=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode == 0:
                output.seek(0)
                return sorted(set(output.read().decode(errors="replace").split("\0")) - {""})
        except (OSError, subprocess.TimeoutExpired):
            pass
    # ponytail: non-Git fallback walks the repository; use Git for very large trees.
    names = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {".git", ".router", ".venv", "node_modules", "__pycache__", "graphify-out"} and not (Path(directory)/d).is_symlink()]
        names.extend((Path(directory)/f).relative_to(root).as_posix() for f in files)
    return sorted(names)

class ContextGraph:
    def __init__(self, root, store, repo):
        self.root, self.store, self.repo = Path(root).resolve(), store, repo
        self.files = {}
        self.edges = []

    def build(self):
        self.files, self.edges = {}, []
        self.cache_usage = {"hits": 0, "misses": 0}
        revision = {}
        for name in file_names(self.root):
            if name.endswith(".axir.json"):
                continue  # Portable compiler artifacts are not source preconditions.
            try:
                path = safe_path(self.root, name)
                if not path.is_file():
                    continue
                stat = path.stat()
                # Hash preconditions must survive equal-size writes within a filesystem
                # timestamp tick. Cache parsing by content, never trust stat as identity.
                revision[name] = digest(path)
                from .privacy import protected_path
                if protected_path(name, self.repo):
                    continue
                if path.suffix not in EXTENSIONS or stat.st_size > self.repo.max_file_bytes:
                    continue
                memo = cache_key("file-v2", self.root, name, revision[name])
                item = self.store.get(memo)
                self.cache_usage["hits" if item is not None else "misses"] += 1
                if item is None:
                    text = path.read_text(encoding="utf-8")
                    if SECRET.search(text):
                        continue
                    item = {"sha256": digest(path), "symbols": [], "imports": [], "relations": []}
                    if path.suffix == ".py":
                        try:
                            tree = ast.parse(text)
                            for node in ast.walk(tree):
                                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                    item["symbols"].append({"name": node.name, "start": node.lineno, "end": node.end_lineno})
                                    if isinstance(node, ast.ClassDef):
                                        item["relations"].extend({"relation": "extends", "source": node.name, "target": ast.unparse(b)} for b in node.bases)
                                elif isinstance(node, ast.Import):
                                    item["imports"].extend(n.name for n in node.names)
                                elif isinstance(node, ast.ImportFrom):
                                    item["imports"].append(node.module or "")
                                elif isinstance(node, ast.Call):
                                    item["relations"].append({"relation": "calls", "target": ast.unparse(node.func), "line": node.lineno})
                        except SyntaxError:
                            item["parse_error"] = True
                    else:
                        item["imports"] = re.findall(r"(?:import|from|require\(|implementation\(project\()[\s'\"]+([\w./:-]+)", text)
                        item["symbols"] = [{"name": m[1], "start": text[:m.start()].count("\n")+1, "end": text[:m.start()].count("\n")+1} for m in re.finditer(r"(?:class|interface|fun|function|def)\s+(\w+)", text)]
                    self.store.put(memo, item)
                self.files[name] = item
            except (ValueError, UnicodeError, OSError):
                continue
        modules = {str(Path(n).with_suffix("")).replace("\\", "/").replace("/", "."): n for n in self.files}
        for name, data in self.files.items():
            for imported in data["imports"]:
                matches = [p for m,p in modules.items() if m == imported or m.endswith("."+imported)]
                for target in matches:
                    self.edges.append({"source": name, "target": target, "relation": "imports", "confidence": "EXTRACTED"})
            if Path(name).name.startswith("test_"):
                for target in self.files:
                    if Path(target).stem == Path(name).stem.removeprefix("test_"):
                        self.edges.append({"source": name, "target": target, "relation": "covers", "confidence": "INFERRED"})
        self.portable_fingerprint = cache_key("repository-content-v1", revision)
        self.fingerprint = cache_key("repo-v2", str(self.root), revision)
        result = {"fingerprint": self.fingerprint, "files": self.files, "edges": self.edges}
        self.store.put(cache_key("graph", str(self.root)), result)
        return result

    def capsule(self, task, dependencies, constraints, token_limit):
        relevant = list(dict.fromkeys(task.relevant_files + task.expected_artifacts))
        neighbors = [e["target"] for e in self.edges if e["source"] in relevant]
        names = list(dict.fromkeys(relevant + neighbors))[:self.repo.max_context_files]
        protected = {"task": task.model_dump(), "constraints": constraints, "dependencies": dependencies,
                     "contracts": task.required_context, "allowed_tools": ["propose_file_changes"], "validation_checks": task.validation_commands}
        base = json.dumps(protected, ensure_ascii=False)
        if token_estimate(base) > token_limit:
            raise ValueError("Protected constraints exceed token ceiling; split task")
        files = []
        raw = token_estimate(base)
        used = raw
        for name in names:
            from .privacy import protected_path
            from .contracts import current_contract
            if name in current_contract().get("blocked_files", ()):
                if name in task.expected_artifacts:
                    raise ValueError("Cloud node cannot read an artifact from a local-only node")
                continue
            if protected_path(name, self.repo):
                if name in task.expected_artifacts:
                    raise ValueError("Edit target excluded by repository privacy policy")
                continue
            path = safe_path(self.root, name)
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            if SECRET.search(content):
                raise ValueError("Secret-like source excluded from model context")
            raw += token_estimate(content)
            # Full expected artifacts must fit for safe whole-file replacements.
            if name in task.expected_artifacts and (used + token_estimate(content) > token_limit):
                raise ValueError("Edit target exceeds context ceiling; split task")
            if used + token_estimate(content) > token_limit:
                continue
            used += token_estimate(content)
            files.append({"path": name, "original_sha256": digest(path), "content": content})
        if not set(task.expected_artifacts) <= {f["path"] for f in files}:
            raise ValueError("All edit targets must be in context")
        return {**protected, "files": files}, {"raw_estimated_tokens": raw, "optimized_estimated_tokens": used, "tokens_saved": raw-used, "percent_saved": round(100*(raw-used)/max(1,raw),2)}

def optimize_messages(messages, ceiling):
    # Only exact duplicate system instructions are removed. User turns/tool results stay intact.
    result, seen = [], set()
    for message in messages:
        if message.get("role") == "system":
            key = json.dumps(message, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
        result.append(message)
    raw, optimized = token_estimate(json.dumps(messages)), token_estimate(json.dumps(result))
    if optimized > ceiling:
        raise ValueError("Conversation exceeds token ceiling; explicit compaction required")
    return result, {"raw_estimated_tokens": raw, "optimized_estimated_tokens": optimized, "tokens_saved": raw-optimized}

def load_skills(settings, names, maximum=2000):
    skills = {}
    for name in names:
        if name not in settings.skills:
            raise ValueError("Unknown requested skill")
        text = Path(settings.skills[name]).read_text(encoding="utf-8")
        if token_estimate(text) > maximum or SECRET.search(text):
            raise ValueError("Skill exceeds context budget or contains secret-like text")
        skills[name] = text
    return skills
