"""Run contributor checks. Install .[test,dev] first; use --full for online audits/builds."""

import argparse
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    commands = [
        ["-m", "ruff", "check", "src", "tests", "scripts", "examples/provider-plugin"],
        ["-m", "ruff", "format", "--check", "src", "tests", "scripts", "examples/provider-plugin"],
        ["scripts/scan_public.py"],
        ["scripts/security_policy.py"],
        ["scripts/check_docs.py"],
        ["-m", "bandit", "-r", "src", "scripts", "-ll", "-iii"],
        ["-m", "pytest", "-q", "--cov", "--cov-report=term:skip-covered"],
    ]
    if args.full:
        commands += [
            ["scripts/install_security_tools.py"],
            ["scripts/secret_audit.py"],
            ["-m", "pip_audit", "--skip-editable"],
            ["scripts/check_wheel.py"],
        ]
    for command in commands:
        result = subprocess.run([sys.executable, *command], check=False)
        if result.returncode:
            raise SystemExit(result.returncode)
    zizmor = shutil.which("zizmor")
    if not zizmor:
        raise SystemExit("Install .[dev] to run the required workflow security check")
    subprocess.run([zizmor, "--offline", "--min-severity", "medium", ".github/workflows"], check=True)
    print("All requested local checks passed")


if __name__ == "__main__":
    main()
