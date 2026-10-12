"""Build an offline, per-user Windows installer with the normal CPython runtime."""

import argparse
import ast
import hashlib
import importlib.metadata
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.13.16"
PYTHON_ARCHIVE = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ARCHIVE}"
PYTHON_SHA256 = "97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297"


def download_runtime(cache):
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / PYTHON_ARCHIVE
    if not archive.exists():
        response = httpx.get(PYTHON_URL, timeout=90)
        response.raise_for_status()
        data = response.content
        if hashlib.sha256(data).hexdigest() != PYTHON_SHA256:
            raise RuntimeError("Python runtime checksum mismatch")
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != PYTHON_SHA256:
        raise RuntimeError("Cached Python runtime checksum mismatch")
    return archive


def version():
    tree = ast.parse((ROOT / "src/local_ai_router/__init__.py").read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
    raise RuntimeError("Package version missing")


def run(argv, cwd=ROOT):
    subprocess.run([str(arg) for arg in argv], cwd=cwd, check=True, timeout=600)


def asset_hashes(package):
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((package / "static").iterdir())
        if path.is_file()
    }


def build_icon(destination):
    from playwright.sync_api import sync_playwright

    svg = (ROOT / "src/local_ai_router/static/logo.svg").read_text(encoding="utf-8")
    sizes = (16, 32, 48, 64, 256)
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<style>body{margin:0}svg{display:block;width:100vw;height:100vh}</style>" + svg)
        images = []
        for size in sizes:
            page.set_viewport_size({"width": size, "height": size})
            images.append(page.screenshot(omit_background=True))
        browser.close()
    offset = 6 + 16 * len(sizes)
    entries = []
    for size, image in zip(sizes, images, strict=True):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    destination.write_bytes(struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries + images))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path, help="Path to Inno Setup 6 ISCC.exe")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Build on 64-bit Windows")
    compiler = args.compiler or shutil.which("ISCC.exe")
    if not compiler or not Path(compiler).is_file():
        parser.error("Install Inno Setup 6 and pass --compiler PATH\\ISCC.exe")
    compiler = Path(compiler).resolve()
    workspace = ROOT / "build/windows"
    workspace.mkdir(parents=True, exist_ok=True)
    output = ROOT / "dist/windows"
    output.mkdir(parents=True, exist_ok=True)
    archive = download_runtime(workspace / "downloads")
    app_version = version()
    with tempfile.TemporaryDirectory(prefix="payload-", dir=workspace) as temporary:
        payload = Path(temporary)
        runtime = payload / "python"
        runtime.mkdir()
        with zipfile.ZipFile(archive) as embedded:
            embedded.extractall(runtime)
        (runtime / "python313._pth").write_text("python313.zip\n.\nLib/site-packages\nimport site\n", encoding="utf-8")
        source = payload / "source"
        shutil.copytree(
            ROOT / "src/local_ai_router",
            source / "src/local_ai_router",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE"):
            shutil.copy2(ROOT / name, source / name)
        wheel_dir = payload / "wheel"
        run([sys.executable, "-m", "build", "--wheel", "--outdir", wheel_dir], cwd=source)
        wheel = next(wheel_dir.glob("accension-*.whl"))
        packages = runtime / "Lib/site-packages"
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--only-binary=:all:",
                "--no-compile",
                "--ignore-installed",
                "--platform",
                "win_amd64",
                "--python-version",
                "3.13",
                "--implementation",
                "cp",
                "--abi",
                "cp313",
                "--target",
                packages,
                str(wheel) + "[azure,aws,google,lan]",
            ]
        )
        assets = asset_hashes(ROOT / "src/local_ai_router")
        if asset_hashes(packages / "local_ai_router") != assets:
            raise RuntimeError("Bundled UI assets do not match current source")
        # Build intermediates never ship; each build uses fresh package sources.
        shutil.rmtree(source)
        shutil.rmtree(wheel_dir)
        shutil.copy2(ROOT / "installer/windows/desktop.py", payload / "desktop.py")
        build_icon(payload / "accension.ico")
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
            shutil.copy2(ROOT / name, payload / name)
        shutil.copy2(ROOT / "installer/windows/README.md", payload / "README.md")
        manifest = {
            "version": app_version,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10
            ).strip(),
            "ui_assets": assets,
            "python": {"version": PYTHON_VERSION, "url": PYTHON_URL, "sha256": PYTHON_SHA256},
            "packages": sorted(
                [
                    {"name": item.metadata["Name"], "version": item.version}
                    for item in importlib.metadata.distributions(path=[str(packages)])
                ],
                key=lambda item: item["name"].lower(),
            ),
        }
        (payload / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        run(
            [
                runtime / "python.exe",
                "-B",
                "-c",
                "from importlib.resources import files; from local_ai_router.app import create_app; "
                "from local_ai_router.workbench.templates import builtins; "
                "import azure.identity, boto3, google.auth, cryptography; "
                "root=files('local_ai_router'); "
                "assert all(root.joinpath('static',name).read_bytes() for name in "
                "('index.html','app.js','style.css','workbench.html','workbench.js','workbench.css')); "
                "assert len(builtins())==2",
            ],
            cwd=payload,
        )
        run(
            [
                compiler,
                "/Qp",
                f"/DAppVersion={app_version}",
                f"/DPayloadDir={payload}",
                f"/DOutputDir={output}",
                ROOT / "installer/windows/accension.iss",
            ]
        )
    executable = output / f"Accension-{app_version}-windows-x64.exe"
    checksum = hashlib.sha256(executable.read_bytes()).hexdigest()
    executable.with_suffix(".exe.sha256").write_text(f"{checksum}  {executable.name}\n", encoding="utf-8")
    executable.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    latest = output / "Accension-latest-windows-x64.exe"
    shutil.copy2(executable, latest)
    latest.with_suffix(".exe.sha256").write_text(f"{checksum}  {latest.name}\n", encoding="utf-8")
    latest.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: Windows installer {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
