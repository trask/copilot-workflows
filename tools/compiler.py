"""Install a checksum-pinned compiler locally, never as a global gh extension."""

import hashlib
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

VERSION = "v0.89.21"
REVISION = "c35393777e5604a63721d09512263b1383301d4f"
ASSETS = {
    ("Windows", "AMD64"): ("windows-amd64.exe",
                           "91b1f322d17daffde2f7a81b83bc8ca61cb9c09768a369776bd6d62346aa250c"),
    ("Linux", "x86_64"): ("linux-amd64",
                         "1c74ff5fc28b1891d32b67f4348a9b7f750946b6d4a721e909187a848868016b"),
}


def main():
    asset, expected = ASSETS[(platform.system(), platform.machine())]
    root = Path(__file__).resolve().parents[1]
    destination = root / ".tools" / asset
    destination.parent.mkdir(exist_ok=True)
    if not destination.exists():
        url = f"https://github.com/github/gh-aw/releases/download/{VERSION}/{asset}"
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read(100 * 1024 * 1024 + 1)
        if len(data) > 100 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError("Compiler checksum/size mismatch")
        destination.write_bytes(data)
        destination.chmod(0o755)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Installed compiler checksum mismatch")
    args = [str(destination), "compile", "copilot-worker", "--action-mode", "release",
            "--action-tag", REVISION, "--no-check-update", "--validate"] + sys.argv[1:]
    subprocess.run(args, cwd=root, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


if __name__ == "__main__":
    main()
