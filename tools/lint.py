"""Checksum-pinned actionlint for the handwritten central workflows."""

import hashlib
import io
import os
import platform
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ASSETS = {
    ("Windows", "AMD64"): ("windows_amd64.zip",
                           "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9"),
    ("Linux", "x86_64"): ("linux_amd64.tar.gz",
                         "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"),
}


def main():
    asset, digest = ASSETS[(platform.system(), platform.machine())]
    root = Path(__file__).resolve().parents[1]
    executable = "actionlint.exe" if os.name == "nt" else "actionlint"
    url = f"https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_{asset}"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("Linter checksum/size mismatch")
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            binary = archive.read(executable)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            binary = archive.extractfile(executable).read()
    directory = root / ".tools"
    directory.mkdir(exist_ok=True)
    destination = directory / executable
    destination.write_bytes(binary)
    destination.chmod(0o755)
    subprocess.run([str(destination), "-shellcheck=", "-pyflakes=",
                    ".github/workflows/coordinator.yml", ".github/workflows/waiter.yml",
                    ".github/workflows/validate.yml"],
                   cwd=root, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


if __name__ == "__main__":
    main()
