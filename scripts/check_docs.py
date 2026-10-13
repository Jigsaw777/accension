"""Offline checks for relative links, Markdown fences, public wording and CLI examples."""

import contextlib
import hashlib
import io
import json
import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def anchors(text):
    """GitHub-style heading IDs, including repeated headings."""
    found, counts = set(), {}
    fenced = False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        if fenced:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)(?:\s+#+)?$", line)
        if heading:
            title = re.sub(r"!?\[([^]]+)\]\([^)]+\)", r"\1", heading[1])
            slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
            count = counts.get(slug, 0)
            counts[slug] = count + 1
            found.add(slug + (f"-{count}" if count else ""))
    found.update(re.findall(r'\bid=["\']([^"\']+)', text))
    return found


def check(root=ROOT):
    from local_ai_router.cli_parser import parser

    failures = []
    names = (
        subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)
        .decode()
        .split("\0")
    )
    public = set(names) - {""}
    metadata = root / "src/local_ai_router/__init__.py"
    if metadata.is_file():
        version = re.search(r'__version__\s*=\s*"([^"]+)"', metadata.read_text(encoding="utf-8"))
        readme = root / "README.md"
        if version and readme.is_file():
            advertised = re.search(r"\*\*Version ([0-9.]+)", readme.read_text(encoding="utf-8"))
            if advertised and advertised[1] != version[1]:
                failures.append("README.md: product version differs from package metadata")
    manifest = root / "docs/guides/screenshots/capture-manifest.json"
    if manifest.is_file():
        try:
            records = json.loads(manifest.read_text(encoding="utf-8"))
            for row in records["images"]:
                image_path = manifest.parent / row["file"]
                if hashlib.sha256(image_path.read_bytes()).hexdigest() != row["sha256"]:
                    failures.append("screenshot manifest: changed image " + row["file"])
            for asset, expected in records.get("ui_asset_sha256", {}).items():
                data = (root / asset).read_bytes().replace(b"\r\n", b"\n")
                if hashlib.sha256(data).hexdigest() != expected:
                    failures.append("screenshot manifest: UI asset changed; verify captures " + asset)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            failures.append("screenshot manifest: invalid or missing data: " + str(exc))
    for name in names:
        path = root / name
        if path.suffix.lower() != ".md" or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if len(re.findall(r"(?m)^\s*```", text)) % 2:
            failures.append(name + ": unclosed Markdown fence")
        if re.search(r"\bAccension V[23]\b|\bV[23] (?:is|integration|release|role)|MIGRATION_V1_V2", text, re.I):
            failures.append(name + ": obsolete public version wording")
        if re.search(
            r"(?:create|register|sign up for|log in to|sign in to) (?:an? |your )?Accension (?:account|dashboard)",
            text,
            re.I,
        ):
            failures.append(name + ": obsolete Accension account instruction; use the local app")
        if re.search(r"(?:[A-Za-z]:[/\\]Users[/\\](?!<)|\.codex-state/|docs/prompts/\d)", text):
            failures.append(name + ": unexpected private path")
        images = []
        for match in re.finditer(r"(!?)\[([^\]]*)\]\(([^)]+)\)", text):
            image, label, link = match.groups()
            link = link.split(' "')[0].strip("<>")
            if re.match(r"^[a-z][a-z0-9+.-]*:", link, re.I):
                continue
            target, _, anchor = unquote(link).partition("#")
            destination = (path.parent / target).resolve() if target else path.resolve()
            try:
                relative = destination.relative_to(root.resolve()).as_posix()
            except ValueError:
                failures.append(name + ": link outside public checkout " + link)
                continue
            visible = relative in public or any(n.startswith(relative + "/") for n in public)
            if not destination.exists():
                failures.append(name + ": missing relative link " + link)
            elif not visible:
                failures.append(name + ": ignored/untracked public link " + link)
            elif anchor and destination.suffix == ".md":
                if anchor not in anchors(destination.read_text(encoding="utf-8")):
                    failures.append(name + ": missing heading anchor " + link)
            if image:
                if len(label.strip()) < 12 or label.strip().lower() in {"screenshot", "image"}:
                    failures.append(name + ": screenshot needs descriptive alt text " + link)
                if relative in images:
                    failures.append(name + ": duplicate image reference " + link)
                images.append(relative)
        # Parse concrete single-line CLI examples. Placeholders and continuations
        # remain human-reviewed; commands are never executed by this checker.
        fenced = False
        shell = False
        for line in text.splitlines():
            if line.startswith("```"):
                fenced = not fenced
                language = line[3:].strip()
                if fenced and language and not re.fullmatch(r"[A-Za-z0-9_+.-]+", language):
                    failures.append(name + ": invalid fence language " + language)
                shell = fenced and language in {"sh", "bash", "powershell", "shell"}
            if shell and line.startswith("accs ") and not any(x in line for x in ("\\", "...", "|", ">", "<")):
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        parser().parse_args(shlex.split(line.split(" #")[0])[1:])
                except SystemExit as exc:
                    if exc.code:
                        failures.append(name + ": stale CLI example " + line)
                except ValueError:
                    failures.append(name + ": stale CLI example " + line)
    return failures


if __name__ == "__main__":
    failures = check()
    print(
        "\n".join(failures)
        if failures
        else "PASS: public links, anchors, screenshot alt text, fences, names and concrete CLI examples"
    )
    raise SystemExit(bool(failures))
