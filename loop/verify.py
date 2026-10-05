"""Parse bounded artifacts and reconstruct candidate Git objects without executing code."""

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from loop.coordinator import run_binding
from loop.candidates import current_request, message, patches
from loop.policy import (CENTRAL, REPO, Rejected, candidate_outcome, commit_author, diff_scope, digest, loop_kind,
                         private_source, require, staged_source, worker_result)

FILES = {"result.json", "candidate.patch", "diagnostics.txt"}
MAX_ZIP = 6 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024
MAX_PATCH = 2 * 1024 * 1024
CREDENTIAL_PARTS = {".env", ".netrc", ".npmrc", ".pypirc", ".ssh", ".aws", ".azure",
                    ".gitconfig", "credentials"}


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
    require(len(payload) <= MAX_ZIP, "Artifact archive exceeds limit")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        require(len(entries) == len(FILES) and {i.filename for i in entries} == FILES,
                "Missing, extra, nested, or duplicate artifact files")
        require(sum(i.file_size for i in entries) <= MAX_TOTAL, "Expanded artifact too large")
        result = {}
        for entry in entries:
            mode = entry.external_attr >> 16
            require(not entry.is_dir() and not stat.S_ISLNK(mode)
                    and (stat.S_IFMT(mode) in {0, stat.S_IFREG})
                    and not (entry.flag_bits & 1), "Non-regular or encrypted artifact entry")
            require(entry.file_size <= max(1, entry.compress_size) * 200, "Compression bomb")
            result[entry.filename] = archive.read(entry)
    require(len(result["candidate.patch"]) <= MAX_PATCH
            and len(result["result.json"]) <= 256000, "Artifact member exceeds limit")
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
    require(not artifact["expired"] and 0 < artifact["size_in_bytes"] <= MAX_ZIP,
            "Expired or oversized artifact")
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
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=",
         "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
         "-c", "core.attributesFile=" + os.devnull] + args,
        cwd=cwd, env=env, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=180, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        **({"preexec_fn": limits} if limits is not None and os.name == "posix" else {}),
    )
    require(len(proc.stdout) <= 8 * 1024 * 1024, "Git output exceeds limit")
    require(proc.returncode in allowed, "Git validation failed: " + " ".join(args[:2]))
    return proc.stdout


def safe_source_path(path):
    require(0 < len(path) <= 240 and "\\" not in path and ":" not in path
            and not any(ord(c) < 32 or ord(c) >= 127 for c in path), "Unsafe patch path")
    parts = PurePosixPath(path).parts
    require(parts and not path.startswith("/") and all(p not in {"", ".", "..", ".git"}
            for p in path.split("/")), "Path traversal")
    require(not path.startswith("-") and not any(p.casefold() == ".git" for p in parts),
            "Git control path")
    require(not any(p.casefold() in CREDENTIAL_PARTS | {".gitmodules"} for p in parts),
            "Credential or submodule source path is protected")
    return parts


def safe_path(path, request=None):
    parts = safe_source_path(path)
    require(not any(p.casefold() in {".gitattributes", ".copilot"} for p in parts)
            and not any(p.casefold() in {"agents.md", "copilot-instructions.md", "skill.md"}
                        or p.casefold().endswith(".instructions.md") for p in parts)
            and not (parts[0].casefold() == ".github" and len(parts) > 1
                     and parts[1].casefold() in {"agents", "skills", "instructions"}),
            "Credential, Git control, submodule, or instruction path is protected")
    require(request is None or request["head_repo"] != CENTRAL
            or parts[0].casefold() not in {".github", "loop", "tools"},
            "Central trusted runtime source is protected")


def reconstruct(files, request, fetch_source=None, package_dir=None):
    require(request["schema"] == 2, "Legacy candidates are read-only")
    current_request(request)
    result = parse_json(files["result.json"])
    worker_result(result, request)
    if loop_kind(request) == "pr_description":
        require(files["candidate.patch"] == b"", "Description task cannot contain a source patch")
        empty_hash = hashlib.sha256(b"").hexdigest()
        candidate = {"commit": request["frozen_sha"], "tree": None, "parent": request["frozen_sha"],
                     "changed_paths": [], "changed": False, "patch_sha256": empty_hash,
                     "cumulative_patch_sha256": empty_hash, "commits": [], "finding_commits": {}}
        return package_candidate(candidate, request, package_dir)
    if loop_kind(request) == "pr_conflict_resolver":
        from loop.conflicts import reconstruct_merge
        return reconstruct_merge(files, request, fetch_source, package_dir)
    name, email = commit_author(request)
    require(type(request.get("frozen_at")) is int and request["frozen_at"] >= 0,
            "Invalid frozen commit timestamp")
    patch = files["candidate.patch"]
    parts = patches(result, patch)
    require(b"\x00" not in patch and b"GIT binary patch" not in patch
            and b"Binary files " not in patch, "Binary patches are not supported")
    with tempfile.TemporaryDirectory(prefix="review-verify-") as directory:
        git(["init", "--bare", "--quiet"], directory)
        if fetch_source is None:
            require(request["schema"] == 2 and REPO.fullmatch(request["head_repo"])
                    and not staged_source(request),
                    "Review source requires a bound trusted snapshot, never public fallback")
            git(["fetch", "--quiet", "--depth=1", "--no-tags",
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
        for entry in git(["ls-tree", "-r", "-z", request["frozen_sha"]], directory).split(b"\0")[:-1]:
            info, path = entry.decode("utf-8").split("\t", 1)
            mode, kind, _ = info.split()
            require(mode in {"100644", "100755"} and kind == "blob", "Unsupported source object")
            safe_source_path(path)
        if loop_kind(request) == "pr_consistency":
            for item in result["consistency"]:
                for citation in item["citations"]:
                    match = re.fullmatch(r"(.+):([1-9][0-9]*)(?:-([1-9][0-9]*))?", citation)
                    require(match is not None, "Consistency citation requires a source path and line")
                    safe_source_path(match[1])
                    text = git(["show", request["frozen_sha"] + ":" + match[1]], directory)
                    last = int(match[3] or match[2])
                    require(int(match[2]) <= last <= len(text.splitlines()),
                            "Consistency citation is outside the frozen source")
        git(["read-tree", request["frozen_sha"]], directory)
        parent = request["frozen_sha"]
        commits, mapping, all_paths = [], {}, set()
        changed_lines = 0
        for batch, part in zip(result["batches"], parts):
            stats = git(["apply", "--numstat", "-z", "-"], directory, part)
            records = stats.split(b"\0")[:-1]
            require(0 < len(records) <= 100, "Patch file count exceeds limit")
            for record in records:
                added, deleted, path = record.decode("utf-8").split("\t", 2)
                require(added.isdigit() and deleted.isdigit(), "Binary or malformed patch")
                changed_lines += int(added) + int(deleted)
                safe_path(path, request)
                all_paths.add(path)
            require(changed_lines <= 10000, "Patch line count exceeds limit")
            require(len(all_paths) <= 100, "Combined batch file count exceeds limit")
            git(["apply", "--cached", "--check", "--whitespace=error-all", "-"], directory, part)
            git(["apply", "--cached", "--whitespace=error-all", "-"], directory, part)
            paths = changed_paths(directory, parent, request)
            tree = git(["write-tree"], directory).decode().strip()
            require(tree != git(["rev-parse", parent + "^{tree}"], directory).decode().strip(),
                    "Empty code batch")
            subject, text = message(batch, request)
            commit_object = (
                f"tree {tree}\nparent {parent}\n"
                f"author {name} <{email}> {request['frozen_at']} +0000\n"
                f"committer {name} <{email}> {request['frozen_at']} +0000\n\n"
            ).encode() + text
            commit = git(["hash-object", "-t", "commit", "-w", "--stdin"], directory,
                         commit_object).decode().strip()
            commits.append({"commit": commit, "tree": tree, "parent": parent,
                            "subject": subject, "changed_paths": paths,
                            "patch_sha256": batch["sha256"]})
            for key in batch.get("findings", []):
                mapping[key] = commit
            parent = commit
        paths = changed_paths(directory, request["frozen_sha"], request)
        tree = git(["write-tree"], directory).decode().strip()
        base_tree = git(["rev-parse", request["frozen_sha"] + "^{tree}"], directory).decode().strip()
        require(not commits or tree != base_tree, "Batches cancel the entire code change")
        cumulative = git(["diff", "--cached", "--no-renames", "--binary",
                          request["frozen_sha"]], directory)
        require(len(cumulative) <= MAX_PATCH, "Cumulative patch exceeds byte limit")
        sizes = [int(n) for n in git(["cat-file", "--batch-all-objects",
                                      "--batch-check=%(objectsize)"], directory).splitlines()]
        require(len(sizes) <= 10000 and max(sizes, default=0) <= 4 * 1024 * 1024
                and sum(sizes) <= 16 * 1024 * 1024, "Git object expansion exceeds limits")
        commit = parent
        git(["update-ref", "refs/heads/candidate", commit], directory)
        git(["fsck", "--strict", "--no-reflogs"], directory)
        candidate = {"commit": commit, "tree": tree, "parent": request["frozen_sha"],
                     "changed_paths": paths, "changed": tree != base_tree,
                     "patch_sha256": hashlib.sha256(patch).hexdigest(),
                     "cumulative_patch_sha256": hashlib.sha256(cumulative).hexdigest(),
                     "commits": commits, "finding_commits": mapping}
        return package_candidate(candidate, request, package_dir, directory)


def package_candidate(candidate, request, package_dir, directory=None):
    if package_dir is not None:
        destination = Path(package_dir).resolve()
        destination.mkdir(exist_ok=True)
        bundle = destination / "candidate.bundle"
        if candidate["commits"]:
            require(directory is not None, "Code candidate requires a reconstructed Git repository")
            git(["bundle", "create", str(bundle), "refs/heads/candidate",
                 "^" + request["frozen_sha"]], directory)
        else:
            bundle.write_bytes(b"")
        require(bundle.stat().st_size <= 16 * 1024 * 1024, "Candidate bundle exceeds limit")
        candidate["bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
        manifest = dict(candidate, schema=2, request_digest=digest(request),
                        prerequisite=request["frozen_sha"], repo=request["repo"],
                        repo_id=request["head_repo_id"], head_repo=request["head_repo"],
                        source_private=private_source(request))
        (destination / "manifest.json").write_bytes(
            json.dumps(manifest, sort_keys=True).encode())
    return candidate


def verify_diff_source(directory, request):
    git(["read-tree", request["merge_base_sha"]], directory)
    patch = request["pr_diff"]["text"].encode("utf-8")
    if patch:
        git(["apply", "--cached", "--whitespace=nowarn", "-"], directory, patch)
    require(git(["write-tree"], directory).decode().strip()
            == git(["rev-parse", request["frozen_sha"] + "^{tree}"], directory).decode().strip(),
            "Authoritative PR diff does not reconstruct the complete frozen head tree")


def changed_paths(directory, parent, request):
    raw = git(["diff", "--cached", "--raw", "--no-renames", "-z", parent],
              directory).split(b"\0")
    paths = []
    for index in range(0, len(raw) - 1, 2):
        header = raw[index].decode().split()
        path = raw[index + 1].decode("utf-8")
        safe_path(path, request)
        require(header[0] in {":000000", ":100644"} and header[1] in {"000000", "100644"},
                "Symlink, submodule, or executable change")
        paths.append(path)
    require(len(paths) <= 100, "Cumulative path count exceeds limit")
    return paths


def verify(payload, request, run, artifact, fetch_source=None, package_dir=None):
    require(request["schema"] == 2, "Legacy reports are read-only")
    files = read_zip(payload)
    result = parse_json(files["result.json"])
    worker_result(result, request)
    if loop_kind(request) == "self_review":
        require(result["outcome"] != "blocked", "Self-review is blocked or incomplete")
    candidate = reconstruct(files, request, fetch_source, package_dir)
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
