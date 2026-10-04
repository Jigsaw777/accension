"""Build the wheel and run CLI smoke checks in a fresh environment outside source."""

import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

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
        if version["version"] != "1.0.0":
            raise RuntimeError("Wheel reports an unexpected public version")
        doctor = json.loads(run([str(cli), "doctor", "--mock", "--json"], base, env))
        if doctor["database"] != "ok" or not doctor["logging"]["writable"]:
            raise RuntimeError("Installed wheel doctor failed")
        for family in ("skill", "preset"):
            run([str(cli), family, "list", "--mock", "--json"], base, env)
        run([str(cli), "logs", "--json"], base, env)
    print("PASS: wheel build, clean installation, help, version, mock doctor, skills, presets and logs")


if __name__ == "__main__":
    main()
