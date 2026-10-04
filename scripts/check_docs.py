"""Offline checks for relative links, Markdown fences, public wording and CLI examples."""

import contextlib
import io
import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    from local_ai_router.cli_parser import parser

    failures = []
    names = (
        subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)
        .decode()
        .split("\0")
    )
    for name in names:
        path = root / name
        if path.suffix.lower() != ".md" or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if len(re.findall(r"(?m)^\s*```", text)) % 2:
            failures.append(name + ": unclosed Markdown fence")
        if re.search(r"\bAccension V[23]\b|\bV[23] (?:is|integration|release|role)|MIGRATION_V1_V2", text, re.I):
            failures.append(name + ": obsolete public version wording")
        for link in re.findall(r"\]\(([^)]+)\)", text):
            target = unquote(link.split("#")[0].split(' "')[0].strip("<>"))
            if target and not re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) and not (path.parent / target).exists():
                failures.append(name + ": missing relative link " + target)
        # Parse concrete single-line CLI examples. Placeholders and continuations
        # remain human-reviewed; commands are never executed by this checker.
        fenced = False
        shell = False
        for line in text.splitlines():
            if line.startswith("```"):
                fenced = not fenced
                shell = fenced and line[3:].strip() in {"sh", "bash", "powershell", "shell"}
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
        "\n".join(failures) if failures else "PASS: documentation links, fences, public names and concrete CLI examples"
    )
    raise SystemExit(bool(failures))
