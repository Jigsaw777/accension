"""Build the wheel and run CLI smoke checks in a fresh environment outside source."""

import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from local_ai_router import __version__

ROOT = Path(__file__).resolve().parents[1]


def run(argv, cwd, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError("Wheel check failed: " + Path(argv[0]).name + " " + " ".join(argv[1:3]))
    return result.stdout


def main():
    run([sys.executable, "-m", "build", "--wheel"], ROOT)
    wheels = sorted((ROOT / "dist").glob("accension-*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        raise RuntimeError("Build produced no wheel")
    with tempfile.TemporaryDirectory(prefix="accension-wheel-") as folder:
        base = Path(folder)
        venv.EnvBuilder(with_pip=True).create(base / "env")
        scripts = base / "env" / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        cli = scripts / ("accs.exe" if os.name == "nt" else "accs")
        env = {**os.environ, "ROUTER_HOME": str(base / "home"), "PYTHONPATH": "", "ROUTER_API_TOKEN": ""}
        run([str(python), "-m", "pip", "install", str(wheels[-1])], base, env)
        run([str(cli), "--help"], base, env)
        version = json.loads(run([str(cli), "version", "--json"], base, env))
        if version["version"] != __version__:
            raise RuntimeError("Wheel reports an unexpected public version")
        doctor = json.loads(run([str(cli), "doctor", "--mock", "--json"], base, env))
        if doctor["database"] != "ok" or not doctor["logging"]["writable"]:
            raise RuntimeError("Installed wheel doctor failed")
        for family in ("skill", "preset"):
            run([str(cli), family, "list", "--mock", "--json"], base, env)
        run([str(cli), "logs", "--json"], base, env)
        run(
            [
                str(python),
                "-c",
                "from importlib.resources import files; from local_ai_router.workbench.templates import builtins; r=files('local_ai_router'); assert all(r.joinpath('static',n).read_bytes() for n in ('workbench.html','workbench.js','workbench.css','workbench-lan.html','workbench-lan.js')); assert len(builtins())==2",
            ],
            base,
            env,
        )
        smoke = """
import asyncio
from pathlib import Path
import httpx
from local_ai_router.app import create_app
from local_ai_router.config import load
from local_ai_router.engine import Engine
from local_ai_router.cli_parser import parser
from local_ai_router.db_migrations import SUPPORTED_VERSION
from local_ai_router.workbench.cli import dispatch
from local_ai_router.workbench.client import WorkbenchClient
async def main():
    engine = Engine(load(Path('home'), mock=True))
    app = create_app(engine.settings, engine)
    try:
        client = WorkbenchClient(engine.settings, transport=httpx.ASGITransport(app=app))
        result = await dispatch(parser().parse_args(['workspace','list','--json']), engine.settings, client=client)
        assert result['items'] and engine.store.db.execute('PRAGMA user_version').fetchone()[0] == SUPPORTED_VERSION
        assert engine.store.costs()['calls'] == 0
    finally:
        await app.state.workbench.close()
        await engine.close()
asyncio.run(main())
"""
        run([str(python), "-c", smoke], base, env)
    print(
        "PASS: wheel build, clean installation, help, version, mock doctor, skills, presets, logs and Workbench resources"
    )


if __name__ == "__main__":
    main()
