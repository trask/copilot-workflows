"""Package native worker commits without network or publication authority."""

import sys
from pathlib import Path

from loop.policy import loop_kind, require, source_effect, worker_result
from loop.verify import FILES, git, parse_json

LIMITS = {"result.json": 256000, "candidate.bundle": None, "diagnostics.txt": 4194304}


def package(request, repository, directory=Path("loop-output")):
    value = parse_json((directory / "result.json").read_bytes())
    worker_result(value, request)
    bundle = (directory / "candidate.bundle").resolve()
    require(not bundle.exists(), "Candidate bundle already exists")
    if value["outcome"] in {"fixes", "merge"}:
        require(source_effect(request) and repository is not None, "Code outcome requires a target repository")
        require(not git(["status", "--porcelain"], repository), "Candidate worktree has uncommitted changes")
        tip = git(["rev-parse", "HEAD"], repository).decode().strip()
        git(["merge-base", "--is-ancestor", request["frozen_sha"], tip], repository)
        require(tip != request["frozen_sha"], "Code outcome requires a new commit")
        git(["update-ref", "refs/heads/candidate", tip], repository)
        exclusions = ["^" + request["frozen_sha"]]
        if loop_kind(request) == "pr_conflict_resolver":
            exclusions.append("^" + request["base_sha"])
        git(["bundle", "create", str(bundle), "refs/heads/candidate", *exclusions], repository)
    else:
        if repository is not None and value["outcome"] != "blocked":
            require(not git(["status", "--porcelain"], repository)
                    and git(["rev-parse", "HEAD"], repository).decode().strip() == request["frozen_sha"],
                    "No-change outcome has unreported source changes")
        bundle.write_bytes(b"")
    check_output(request, directory)


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
    require(bool(files["candidate.bundle"]) == (value["outcome"] in {"fixes", "merge"}),
            "Worker outcome contradicts candidate bundle")


if __name__ == "__main__":
    require(len(sys.argv) <= 2, "Use worker_output with an optional target repository")
    request = parse_json(Path("frozen-request.json").read_bytes())
    directory = Path("loop-output")
    if (directory / "candidate.bundle").exists():
        check_output(request, directory)
    else:
        package(request, Path(sys.argv[1]) if len(sys.argv) == 2 else None, directory)
    print("Worker result and bundle are packaged; trusted verification is still required.")
