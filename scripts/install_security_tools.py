"""Install a pinned Gitleaks binary after verifying its published SHA-256.

Only the named executable is extracted; archive paths are never trusted.
"""

import argparse
import hashlib
import io
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "8.30.1"
CHECKSUMS = {
    "windows_x64.zip": "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e",
    "windows_arm64.zip": "b95f5e4f5c425cedca7ee203d9afd29597e692c4924a12ed42f970537c72cc0f",
    "linux_x64.tar.gz": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    "linux_arm64.tar.gz": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
    "darwin_x64.tar.gz": "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
    "darwin_arm64.tar.gz": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
}


def install(directory):
    system = platform.system().lower()
    arch = "arm64" if platform.machine().lower() in {"aarch64", "arm64"} else "x64"
    suffix = system + "_" + arch + (".zip" if system == "windows" else ".tar.gz")
    expected = CHECKSUMS[suffix]
    name = "gitleaks_" + VERSION + "_" + suffix
    url = "https://github.com/gitleaks/gitleaks/releases/download/v" + VERSION + "/" + name
    with urllib.request.urlopen(url, timeout=60) as response:  # nosec B310: fixed HTTPS host and pinned artifact hash
        data = response.read(30_000_001)
    if len(data) > 30_000_000 or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Gitleaks download failed its pinned checksum check")
    executable = "gitleaks.exe" if system == "windows" else "gitleaks"
    if system == "windows":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            binary = archive.read(executable)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            member = archive.getmember(executable)
            if not member.isfile() or member.size > 80_000_000:
                raise ValueError("Invalid executable in Gitleaks archive")
            binary = archive.extractfile(member).read()
    path = Path(directory).resolve() / executable
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(binary)
    path.chmod(0o755)
    print(path)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".router/tools")
    install(parser.parse_args().directory)
