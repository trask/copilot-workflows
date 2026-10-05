"""Bounded merge history and deterministic two-parent Git reconstruction."""

import hashlib
import tempfile
from pathlib import Path

from loop.candidates import message
from loop.policy import CENTRAL, canonical, commit_author, digest, require
from loop.verify import MAX_PATCH, git, safe_path


def history(directory, request):
    head, base, ancestor = (request[k] for k in ("frozen_sha", "base_sha", "merge_base_sha"))
    require(git(["merge-base", head, base], directory).decode().strip() == ancestor,
            "Incomplete or contradictory frozen merge history")
    require(int(git(["rev-list", "--count", head, base], directory)) <= 256,
            "Merge history exceeds commit limit")
    return ancestor == base


def entries(directory, tree):
    return {record.split(b"\t", 1)[1].decode("utf-8"):
            tuple(record.split(b"\t", 1)[0].decode().split())
            for record in git(["ls-tree", "-r", "-z", tree], directory).split(b"\0")[:-1]}


def resolved_tree(directory, request, tree):
    output = git(["merge-tree", "--write-tree", "--name-only", "-z", "--no-messages",
                  request["frozen_sha"], request["base_sha"]], directory, allowed=(0, 1))
    records = output.split(b"\0")
    automatic = records[0].decode().strip()
    conflicts = {p.decode("utf-8") for p in records[1:] if p}
    proposed, clean = entries(directory, tree), entries(directory, automatic)
    require(all(proposed.get(p) == clean.get(p)
                for p in proposed.keys() | clean.keys() if p not in conflicts),
            "Merge altered or omitted a cleanly merged incoming change")
    before = entries(directory, request["frozen_sha"])
    incoming = entries(directory, request["base_sha"])
    paths = sorted(p for p in before.keys() | proposed.keys() if before.get(p) != proposed.get(p))
    require(len(paths) <= 100, "Merge changed path count exceeds limit")
    for path in paths:
        central = request["head_repo"] == CENTRAL and path.split("/")[0].casefold() in {".github", "loop", "tools"}
        carried = path not in conflicts and proposed.get(path) == incoming.get(path)
        if not carried or central:
            safe_path(path, request)
            require((before.get(path) or ("100644",))[0] == "100644"
                    and (proposed.get(path) or ("100644",))[0] == "100644",
                    "Worker-authored executable, symlink or submodule resolution")
    for path in conflicts:
        safe_path(path, request)
        require(path not in proposed or proposed[path][0] == "100644",
                "Unsupported conflict resolution mode")
        if path in proposed:
            blob = git(["cat-file", "blob", proposed[path][2]], directory)
            require(b"\x00" not in blob and not any(
                line.startswith((b"<<<<<<< ", b"=======", b">>>>>>> "))
                for line in blob.splitlines()), "Unresolved conflict markers")
    return paths


def reconstruct_merge(files, request, fetch_source, package_dir):
    from loop.verify import parse_json
    result = parse_json(files["result.json"])
    patch = files["candidate.patch"]
    require(fetch_source is not None and len(patch) <= MAX_PATCH
            and b"\x00" not in patch and b"GIT binary patch" not in patch
            and b"Binary files " not in patch, "Merge requires supported bound source/patch")
    with tempfile.TemporaryDirectory(prefix="verify-merge-") as directory:
        git(["init", "--bare", "--quiet"], directory)
        fetch_source(directory)
        from loop.source import snapshot_identity
        snapshot_identity(directory, request["frozen_sha"], request)
        from loop.verify import verify_diff_source
        verify_diff_source(directory, request)
        incorporated = history(directory, request)
        require(result["outcome"] == "blocked" or
                (result["outcome"] == "no_change") == incorporated,
                "Merge outcome contradicts the frozen graph")
        git(["read-tree", request["frozen_sha"]], directory)
        if patch:
            require(result["outcome"] == "merge", "No-change merge contains a patch")
            stats = git(["apply", "--numstat", "-z", "-"], directory, patch)
            lines = 0
            for record in stats.split(b"\0")[:-1]:
                added, removed, path = record.decode().split("\t", 2)
                require(added.isdigit() and removed.isdigit(), "Unsupported merge patch")
                from loop.verify import safe_source_path
                safe_source_path(path)
                lines += int(added) + int(removed)
            require(lines <= 10000, "Merge patch line limit exceeded")
            git(["apply", "--cached", "--whitespace=error-all", "-"], directory, patch)
        tree = git(["write-tree"], directory).decode().strip()
        paths, commits = [], []
        commit = request["frozen_sha"]
        if result["outcome"] == "merge":
            paths = resolved_tree(directory, request, tree)
            name, email = commit_author(request)
            subject, text = message(result["merge"], request)
            obj = (f"tree {tree}\nparent {commit}\nparent {request['base_sha']}\n"
                   f"author {name} <{email}> {request['frozen_at']} +0000\n"
                   f"committer {name} <{email}> {request['frozen_at']} +0000\n\n").encode() + text
            commit = git(["hash-object", "-t", "commit", "-w", "--stdin"], directory, obj).decode().strip()
            commits = [{"commit": commit, "tree": tree, "parent": request["frozen_sha"],
                        "parents": [request["frozen_sha"], request["base_sha"]],
                        "subject": subject, "changed_paths": paths,
                        "patch_sha256": hashlib.sha256(patch).hexdigest()}]
        cumulative = git(["diff", "--cached", "--no-renames", "--binary", request["frozen_sha"]], directory)
        require(len(cumulative) <= MAX_PATCH, "Merge cumulative patch exceeds limit")
        from loop.publication import object_bounds
        object_bounds(directory)
        git(["update-ref", "refs/heads/candidate", commit], directory)
        git(["fsck", "--strict", "--no-reflogs"], directory)
        candidate = {"commit": commit, "tree": tree, "parent": request["frozen_sha"],
                     "changed": bool(commits), "changed_paths": paths, "commits": commits,
                     "finding_commits": {}, "patch_sha256": hashlib.sha256(patch).hexdigest(),
                     "cumulative_patch_sha256": hashlib.sha256(cumulative).hexdigest()}
        if package_dir is not None:
            destination = Path(package_dir).resolve()
            destination.mkdir(exist_ok=True)
            bundle = destination / "candidate.bundle"
            if commits:
                git(["bundle", "create", str(bundle), "refs/heads/candidate",
                     "^" + request["frozen_sha"], "^" + request["base_sha"]], directory)
            else:
                bundle.write_bytes(b"")
            candidate["bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
            (destination / "manifest.json").write_bytes(canonical(dict(
                candidate, schema=2, request_digest=digest(request), prerequisite=request["frozen_sha"],
                repo=request["repo"], repo_id=request["head_repo_id"], head_repo=request["head_repo"],
                source_private=request["source_private"])))
        return candidate
