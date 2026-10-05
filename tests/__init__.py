import os
import shutil
from pathlib import Path


if os.name == "nt":
    selected = shutil.which("git")
    if selected:
        launcher = Path(selected)
        native = launcher.parent.parent / "mingw64" / "bin" / "git.exe"
        if launcher.parent.name.casefold() == "cmd" and native.is_file():
            # Use the same installation without its extra launcher process.
            os.environ["PATH"] = str(native.parent) + os.pathsep + os.environ.get("PATH", "")
