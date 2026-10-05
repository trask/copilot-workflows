"""Prepare the dedicated writable home for the AWF worker."""

import sys
from pathlib import Path

from loop.policy import require

WORKER_HOME = Path("/tmp/review-loop-worker-home")


def prepare_home(home=WORKER_HOME):
    require(home == WORKER_HOME and home.parent == Path("/tmp"),
            "Worker home must be the dedicated temporary directory")
    home.mkdir(mode=0o700)
    # AWF pre-seeds JVM proxy files before dropping to the runner UID.
    for name in (".gradle", ".m2", ".cache"):
        (home / name).mkdir(mode=0o700)


if __name__ == "__main__":
    require(not sys.argv[1:], "Unsupported worker home operation")
    prepare_home()
