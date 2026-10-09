"""Verify worker artifacts and native Git history without executing target code."""

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from contextlib import nullcontext
from pathlib import Path, PurePosixPath, PureWindowsPath

from loop.coordinator import run_binding
from loop.candidates import TRAILER, current_request, prose
from loop.policy import (CENTRAL, REPO, Rejected, candidate_outcome, commit_author, diff_scope, digest, loop_kind,
                         require, staged_source, worker_result)
from loop.policy import direct_inputs

FILES = {"result.json", "candidate.bundle", "diagnostics.txt"}


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def parse_json(data):
    return json.loads(data, object_pairs_hook=unique_json,
                      parse_constant=lambda _: (_ for _ in ()).throw(Rejected("Non-finite JSON")))


def read_zip(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        require(len(entries) == len(FILES) and {i.filename for i in entries} == FILES,
                "Missing, extra, nested, or duplicate artifact files")
        result = {}
        for entry in entries:
            mode = entry.external_attr >> 16
            require(not entry.is_dir() and not stat.S_ISLNK(mode)
                    and (stat.S_IFMT(mode) in {0, stat.S_IFREG})
                    and not (entry.flag_bits & 1), "Non-regular or encrypted artifact entry")
            if entry.filename != "candidate.bundle":
                limit = 256000 if entry.filename == "result.json" else 4194304
                require(entry.file_size <= limit, "Artifact member exceeds limit")
            result[entry.filename] = archive.read(entry)
    return result


def artifact_metadata(api, run, request):
    run_binding(run, request)
    require(run["status"] == "completed" and run["conclusion"] == "success",
            "Worker run did not complete successfully")
    jobs = api.pages(f"repos/{CENTRAL}/actions/runs/{run['id']}/attempts/1/jobs", "jobs")
    agent = [j for j in jobs if j["name"] == "agent"]
    require(len(agent) == 1 and agent[0]["conclusion"] == "success", "Agent job incomplete")
    all_artifacts = api.pages(f"repos/{CENTRAL}/actions/runs/{run['id']}/artifacts", "artifacts")
    name = f"candidate-{run['id']}-1"
    selected = [a for a in all_artifacts if a["name"] == name]
    require(len(selected) == 1, "Missing or duplicate candidate artifact")
    artifact = selected[0]
    require(not artifact["expired"] and artifact["size_in_bytes"] > 0,
            "Expired or empty artifact")
    provenance = artifact["workflow_run"]
    require(provenance["id"] == run["id"]
            and provenance["head_sha"] == request["workflow_revision"], "Cross-run artifact")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", "")),
            "Server artifact digest is missing")
    return artifact, all_artifacts


def git(args, cwd, input_data=None, limits=None, allowed=(0,)):
    # Do not pass verifier credentials or hostile runner Git configuration to child processes.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    if limits is None:
        from loop.source import source_limits
        limits = source_limits
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=",
         "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
         "-c", "core.attributesFile=" + os.devnull, "-c", "pack.threads=1"] + args,
        cwd=cwd, env=env, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=180, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        **({"preexec_fn": limits} if limits is not None and os.name == "posix" else {}),
    )
    if proc.returncode not in allowed:
        raise Rejected("Git validation failed: " + " ".join(args[:2]) + f" (exit {proc.returncode}): "
                       + proc.stderr.decode("utf-8", errors="replace")[:2000])
    return proc.stdout


def safe_source_path(path):
    require(isinstance(path, str) and path and "\x00" not in path, "Invalid Git path")
    components = path.replace("\\", "/").split("/")
    require(not PureWindowsPath(path).drive and all(p not in {"", ".", ".."}
            for p in components), "Path traversal")
    require(not any(p.casefold() == ".git" for p in components),
            "Git control path")
    return PurePosixPath(path).parts


def safe_path(path, request=None):
    parts = safe_source_path(path)
    require(request is None or request["head_repo"] != CENTRAL
            or parts[0].casefold() not in {".github", "loop", "tools"},
            "Central trusted runtime source is protected")


def tree_entries(directory, tree):
    entries = {}
    for record in git(["ls-tree", "-r", "-z", tree], directory).split(b"\0")[:-1]:
        info, path = record.decode("utf-8").split("\t", 1)
        mode, kind, oid = info.split()
        require((mode, kind) in {("100644", "blob"), ("100755", "blob"),
                                ("120000", "blob"), ("160000", "commit")},
                "Invalid Git tree entry")
        safe_source_path(path)
        entries[path] = mode, kind, oid
    return entries


def patch_stats(patch, directory=None, *, reverse=False):
    require(b"\x00" not in patch, "NUL in Git patch")
    if not patch:
        return []
    stats = git(["apply", "--numstat", "-z", *(["--reverse"] if reverse else []), "-"],
                directory, patch)
    result = []
    for record in stats.split(b"\0")[:-1]:
        added, deleted, path = record.decode("utf-8").split("\t", 2)
        safe_source_path(path)
        require((added == deleted == "-") or (added.isdigit() and deleted.isdigit()),
                "Malformed patch statistics")
        result.append((None if added == "-" else int(added),
                       None if deleted == "-" else int(deleted), path))
    return result


def patch_sections(patch, directory=None):
    if not patch:
        return []
    sections = re.split(br"(?m)(?=^diff --git )", patch)
    require(not sections[0], "Git patch must start with a diff header")
    forward, reverse = patch_stats(patch, directory), patch_stats(patch, directory, reverse=True)
    require(len(sections) - 1 == len(forward) == len(reverse), "Incomplete Git diff sections")
    return [(section, old[2], new[2], new[0], new[1])
            for section, old, new in zip(sections[1:], reverse, forward)]


def verify_candidate(files, request, fetch_source=None, *, repository_dir=None):
    require(request["schema"] == 2, "Legacy candidates are read-only")
    current_request(request)
    require(not direct_inputs(request) or loop_kind(request) in {"copilot_review", "self_review"}
            or "inputs" in request, "Task inputs must be independently acquired before verification")
    result = parse_json(files["result.json"])
    worker_result(result, request)
    bundle = files["candidate.bundle"]
    if loop_kind(request) == "pr_description":
        require(not bundle, "Description task cannot contain a source bundle")
        empty_hash = hashlib.sha256(b"").hexdigest()
        candidate = {"commit": request["frozen_sha"], "tree": None, "parent": request["frozen_sha"],
                     "changed_paths": [], "changed": False, "patch_sha256": empty_hash,
                     "cumulative_patch_sha256": empty_hash, "commits": [], "finding_commits": {},
                     "bundle_sha256": empty_hash}
        return candidate
    name, email = commit_author(request)
    with (tempfile.TemporaryDirectory(prefix="review-verify-")
          if repository_dir is None else nullcontext(repository_dir)) as directory:
        git(["init", "--bare", "--quiet"], directory)
        if fetch_source is None:
            if direct_inputs(request):
                from loop.source import acquire_source
                acquire_source(directory, request)
            else:
                require(request["schema"] == 2 and REPO.fullmatch(request["head_repo"])
                        and not staged_source(request),
                        "Review source requires a bound trusted snapshot, never public fallback")
                git(["fetch", "--quiet", "--no-auto-maintenance", "--depth=1", "--no-tags",
                     "https://github.com/" + request["head_repo"] + ".git", request["frozen_sha"]], directory)
        else:
            fetch_source(directory)
        require(git(["rev-parse", "FETCH_HEAD"], directory).decode().strip()
                == request["frozen_sha"], "Wrong source commit")
        if diff_scope(request):
            from loop.source import snapshot_identity
            snapshot_identity(directory, request["frozen_sha"], request)
            if loop_kind(request) != "self_review":
                verify_diff_source(directory, request)
        tree_entries(directory, request["frozen_sha"])
        if loop_kind(request) == "pr_consistency":
            for item in result["consistency"]:
                for citation in item["citations"]:
                    match = re.fullmatch(r"(.+):([1-9][0-9]*)(?:-([1-9][0-9]*))?", citation, re.DOTALL)
                    require(match is not None, "Consistency citation requires a source path and line")
                    safe_source_path(match[1])
                    text = git(["show", request["frozen_sha"] + ":" + match[1]], directory)
                    last = int(match[3] or match[2])
                    require(int(match[2]) <= last <= len(text.splitlines()),
                            "Consistency citation is outside the frozen source")
        head = request["frozen_sha"]
        tip = head
        if bundle:
            bundle_path = Path(directory, "worker.bundle")
            bundle_path.write_bytes(bundle)
            heads = git(["bundle", "list-heads", str(bundle_path)], directory).decode().splitlines()
            require(len(heads) == 1 and heads[0].endswith(" refs/heads/candidate"),
                    "Candidate bundle must export only refs/heads/candidate")
            tip = heads[0].split(" ", 1)[0]
            require(re.fullmatch(r"[0-9a-f]{40}", tip) and tip != head,
                    "Candidate bundle has no new commit")
            git(["bundle", "verify", str(bundle_path)], directory)
            git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-auto-maintenance",
                 "--no-tags", str(bundle_path), "refs/heads/candidate:refs/heads/candidate"], directory)
            git(["merge-base", "--is-ancestor", head, tip], directory)
        merge = loop_kind(request) == "pr_conflict_resolver"
        exclusions = ["^" + head]
        if merge:
            from loop.conflicts import history
            incorporated = history(directory, request)
            require(result["outcome"] == "blocked" or
                    (result["outcome"] == "no_change") == incorporated,
                    "Merge outcome contradicts the frozen graph")
            exclusions.append("^" + request["base_sha"])
        history = git(["rev-list", "--reverse", tip, *exclusions], directory).decode().splitlines()
        require(len(history) <= 100 and (not merge or len(history) <= 1),
                "Candidate contains unexpected commit history")
        parent = head
        commits, mapping = [], {}
        for commit in history:
            parents = git(["show", "-s", "--format=%P", commit], directory).decode().strip().split()
            require(parents == ([head, request["base_sha"]] if merge else [parent]),
                    "Candidate parents differ from the frozen linear or merge history")
            author = git(["show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", commit],
                         directory).decode().strip().split("\0")
            require(author == [name, email, name, email], "Candidate commit author differs from launch owner")
            body = git(["show", "-s", "--format=%B", commit], directory).decode("utf-8").strip()
            require(body.endswith(TRAILER), "Candidate commit lacks Copilot co-author trailer")
            subject = body.splitlines()[0]
            prose(subject, summary=True)
            tree = git(["rev-parse", commit + "^{tree}"], directory).decode().strip()
            tree_entries(directory, tree)
            paths = git(["diff", "--name-only", "--no-renames", "-z", parent, commit],
                        directory).decode("utf-8").split("\0")[:-1]
            for path in paths:
                safe_path(path, request)
            require(merge or paths, "Empty candidate commit")
            diff = git(["--attr-source=" + tree, "diff", "--no-ext-diff", "--no-textconv",
                        "--no-renames", "--binary", parent, commit], directory)
            entry = {"commit": commit, "tree": tree, "parent": parent,
                     "subject": subject, "changed_paths": paths,
                     "patch_sha256": hashlib.sha256(diff).hexdigest()}
            if merge:
                entry["parents"] = parents
            commits.append(entry)
            parent = commit
        require(parent == tip and (result["outcome"] != "blocked" or not commits),
                "Blocked or unaccounted candidate history")
        if loop_kind(request) == "copilot_review":
            for finding in result["findings"]:
                if finding["disposition"] == "fixed":
                    require(finding["commit"] <= len(commits), "Finding selects a missing candidate commit")
                    mapping[finding["key"]] = commits[finding["commit"] - 1]["commit"]
        tree = git(["rev-parse", tip + "^{tree}"], directory).decode().strip()
        base_tree = git(["rev-parse", head + "^{tree}"], directory).decode().strip()
        require(merge or not commits or tree != base_tree, "Candidate commits cancel the entire code change")
        if merge and commits:
            from loop.conflicts import resolved_tree
            resolved_tree(directory, request, tree)
        git(["read-tree", tree], directory)
        paths = changed_paths(directory, head, request)
        cumulative = git(["--attr-source=" + tree, "diff", "--no-ext-diff",
                          "--no-textconv", "--no-renames", "--binary", head, tip], directory)
        git(["update-ref", "refs/heads/candidate", tip], directory)
        git(["fsck", "--strict", "--no-reflogs"], directory)
        candidate = {"commit": tip, "tree": tree, "parent": head,
                     "changed_paths": paths, "changed": bool(commits),
                     "patch_sha256": hashlib.sha256(cumulative).hexdigest(),
                     "cumulative_patch_sha256": hashlib.sha256(cumulative).hexdigest(),
                     "commits": commits, "finding_commits": mapping,
                     "bundle_sha256": hashlib.sha256(bundle).hexdigest()}
        return candidate


def verify_diff_source(directory, request):
    git(["read-tree", request["merge_base_sha"]], directory)
    patch = request.get("inputs", request)["pr_diff"]["text"].encode("utf-8")
    if patch:
        sections = patch_sections(patch, directory)
        binary_paths = {path for _, old, new, added, _ in sections if added is None
                        for path in (old, new)}
        if binary_paths:
            # GitHub's binary markers omit bytes; use the bound snapshot's exact objects.
            while True:
                expanded = binary_paths | {path for _, old, new, _, _ in sections
                                           if old in binary_paths or new in binary_paths
                                           for path in (old, new)}
                if expanded == binary_paths:
                    break
                binary_paths = expanded
            native = git(["--literal-pathspecs", "--attr-source=" + request["frozen_sha"],
                          "diff", "--no-ext-diff", "--no-textconv",
                          "--no-renames", "--binary", request["merge_base_sha"],
                          request["frozen_sha"], "--", *sorted(binary_paths)], directory)
            replacements = [section for section, _, path, _, _ in patch_sections(native, directory)
                            if path in binary_paths]
            require(replacements, "Binary diff does not match the frozen source")
            patch = b"".join(section for section, old, new, _, _ in sections
                             if old not in binary_paths and new not in binary_paths)
            patch += b"".join(replacements)
        git(["apply", "--cached", "--whitespace=nowarn", "-"], directory, patch)
    require(git(["write-tree"], directory).decode().strip()
            == git(["rev-parse", request["frozen_sha"] + "^{tree}"], directory).decode().strip(),
            "Authoritative PR diff does not reconstruct the complete frozen head tree")


def changed_paths(directory, parent, request):
    paths = git(["diff", "--cached", "--name-only", "--no-renames", "-z", parent],
                directory).decode("utf-8").split("\0")[:-1]
    for path in paths:
        safe_path(path, request)
    return paths


def verify(payload, request, run, artifact, fetch_source=None, *, repository_dir=None):
    require(request["schema"] == 2, "Legacy reports are read-only")
    files = read_zip(payload)
    result = parse_json(files["result.json"])
    worker_result(result, request)
    candidate = verify_candidate(files, request, fetch_source, repository_dir=repository_dir)
    candidate_outcome(result, request, candidate)
    return {
        "schema": 2, "request_id": request["request_id"], "request_digest": digest(request),
        "repo": request["repo"], "pr": request["pr"], "frozen_sha": request["frozen_sha"],
        "workflow_revision": request["workflow_revision"], "run_id": run["id"],
        "run_attempt": run["run_attempt"], "artifact_id": artifact["id"],
        "server_artifact_digest": artifact["digest"],
        "download_sha256": hashlib.sha256(payload).hexdigest(),
        "candidate": candidate, "dispositions": result, "verification": "verified",
        "publication_eligible": False,
        "scope": ("Artifact/metadata verification only. No source tree or target code is reconstructed."
                  if loop_kind(request) == "pr_description" else
                  "Structural/Git verification only. No target code or tests are executed."),
    }
