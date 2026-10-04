"""Restore only untouched installed files; refuse to overwrite subsequent edits."""

import argparse
import hashlib
import json
import os
from pathlib import Path


def restore(manifest):
    results = []
    for record in reversed(json.loads(Path(manifest).read_text())):
        if "path" in record:
            path = Path(record["path"])
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != record["installed_sha256"]:
                results.append({"path": str(path), "status": "skipped: changed since installation"})
                continue
            if record["backup"]:
                path.write_bytes(Path(record["backup"]).read_bytes())
            else:
                path.unlink()
            results.append({"path": str(path), "status": "restored"})
        elif os.name == "nt":
            import winreg

            location = (
                r"Software\Microsoft\Windows\CurrentVersion\Run"
                if record["registry"].startswith("Run/")
                else "Environment"
            )
            name = "LocalAIRouter" if location != "Environment" else "Path"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, location, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                try:
                    current, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if current != record["installed"]:
                    results.append({"registry": record["registry"], "status": "skipped: changed"})
                    continue
                if record["previous"] is None:
                    winreg.DeleteValue(key, name)
                else:
                    winreg.SetValueEx(key, name, 0, record.get("kind", winreg.REG_SZ), record["previous"])
                results.append({"registry": record["registry"], "status": "restored"})
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("manifest")
    args = p.parse_args()
    print(json.dumps(restore(args.manifest), indent=2))
