"""Check worker artifact syntax without source access or publication authority."""

import sys
from pathlib import Path

from loop.candidates import patches
from loop.policy import loop_kind, require, worker_result
from loop.verify import FILES, parse_json

LIMITS = {"result.json": 256000, "candidate.patch": None, "diagnostics.txt": 4194304}


def check_output(request, directory=Path("loop-output")):
    require(not directory.is_symlink() and directory.is_dir(), "Invalid worker output directory")
    require({path.name for path in directory.iterdir()} == FILES,
            "Worker output must contain exactly the three artifact files")
    files = {}
    for name, limit in LIMITS.items():
        path = directory / name
        require(not path.is_symlink() and path.is_file(), "Non-regular worker output")
        with path.open("rb") as stream:
            data = stream.read() if limit is None else stream.read(limit + 1)
        require(limit is None or len(data) <= limit, "Worker output exceeds limit: " + name)
        files[name] = data
    value = parse_json(files["result.json"])
    worker_result(value, request)
    if loop_kind(request) == "pr_conflict_resolver":
        return
    patches(value, files["candidate.patch"])


if __name__ == "__main__":
    require(not sys.argv[1:], "Unsupported worker output operation")
    check_output(parse_json(Path("frozen-request.json").read_bytes()))
    print("Worker output schema and patch spans are valid; central verification is still required.")
