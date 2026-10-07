"""Bounded merge history and deterministic two-parent Git reconstruction."""

import hashlib
import re
import tempfile
from pathlib import Path

from loop.candidates import message
from loop.policy import canonical, commit_author, digest, require
from loop.verify import git, safe_path, tree_entries as entries

MAX_HISTORY = 1024


def history(directory, request):
    head, base, ancestor = (request[k] for k in ("frozen_sha", "base_sha", "merge_base_sha"))
    require(git(["merge-base", head, base], directory).decode().strip() == ancestor,
            "Incomplete or contradictory frozen merge history")
    require(int(git(["rev-list", "--count", head, base], directory)) <= MAX_HISTORY,
            "Merge history exceeds commit limit")
    return ancestor == base


def resolved_tree(directory, request, tree):
    output = git(["--attr-source=" + request["frozen_sha"],
                  "merge-tree", "--write-tree", "--name-only", "-z", "--no-messages",
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
    original_blobs = {oid for side in (before, incoming) for _, kind, oid in side.values()
                      if kind == "blob"}
    paths = sorted(p for p in before.keys() | proposed.keys() if before.get(p) != proposed.get(p))
    for path in paths:
        safe_path(path, request)
    for path in conflicts:
        safe_path(path, request)
        automatic_entry = clean.get(path)
        if (path in proposed and proposed[path][1] == "blob"
                and automatic_entry is not None and automatic_entry[1] == "blob"
                and automatic_entry[2] not in original_blobs):
            automatic_blob = git(["cat-file", "blob", automatic_entry[2]], directory)
            marker = re.search(br"(?m)^(<+) " + request["frozen_sha"].encode() + br"(?=\r?$|:)",
                               automatic_blob)
            if marker is not None:
                width = len(marker[1])
                blob = git(["cat-file", "blob", proposed[path][2]], directory)
                require(not any(
                    line.startswith((b"<" * width + b" ", b"=" * width, b">" * width + b" "))
                    for line in blob.splitlines()), "Unresolved conflict markers")
    return paths


def reconstruct_merge(files, request, fetch_source, package_dir):
    from loop.verify import parse_json
    result = parse_json(files["result.json"])
    patch = files["candidate.patch"]
    require(fetch_source is not None and b"\x00" not in patch,
            "Merge requires supported bound source/patch")
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
        cumulative = git(["--attr-source=" + tree, "diff", "--cached", "--no-ext-diff",
                          "--no-textconv", "--no-renames", "--binary", request["frozen_sha"]], directory)
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
