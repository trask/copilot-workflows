"""Read-only source acquisition and credential-free snapshot transport."""

import base64
import hashlib
import io
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from loop.api import API
from loop.conflicts import MAX_HISTORY
from loop.policy import (AUTHOR_ID, BOT_IDENTITY_PATH, CENTRAL, DEFAULTS, REPO, Rejected, canonical,
                         check_target, diff_scope, digest, exact, iso, loop_kind, public_request,
                         require, staged_source)
from loop.verify import git as object_git, object_bounds, parse_json, tree_entries

MAX_SOURCE = 64 * 1024 * 1024
MAX_OBJECT_BYTES = 128 * 1024 * 1024
MAX_OBJECTS = 100000
MAX_OBJECT = 4 * 1024 * 1024


def git(args, directory):
    return object_git(args, directory, limits=source_limits)


class SourceAPI(API):
    def __init__(self, token, repo, head_repo=None):
        require(REPO.fullmatch(repo) and (head_repo is None or REPO.fullmatch(head_repo)),
                "Invalid read target")
        super().__init__(token)
        self.repo = repo
        self.repositories = {repo} if head_repo is None else {repo, head_repo}

    def authorize(self, path, method, data):
        require(self.token, "Missing target repository read access")
        require((method == "GET" and (
            any(path.startswith(f"repos/{repo}/") or path == f"repos/{repo}"
                for repo in self.repositories)
            or path == BOT_IDENTITY_PATH or path == f"user/{AUTHOR_ID}"))
            or (path == "graphql" and method == "POST"
                and data["query"].lstrip().startswith("query")
                and data["variables"].get("owner") == self.repo.split("/")[0]
                and data["variables"].get("name") == self.repo.split("/")[1]),
            "Source credential may only read the explicitly selected repository")

    def call(self, path, method="GET", data=None, **kwargs):
        self.authorize(path, method, data)
        return super().call(path, method, data, **kwargs)


def source_api(repo, head_repo=None):
    token = os.environ.get("SOURCE_READ_TOKEN")
    require(token, "Missing target repository read credential; inference auth is not source auth")
    return SourceAPI(token, repo, head_repo)


def target_api(central, repo, head_repo=None):
    require(REPO.fullmatch(repo), "Invalid target repository")
    return source_api(repo, head_repo) if os.environ.get("SOURCE_READ_TOKEN") else central


def gated_request(repo, number, revision, now):
    from loop.candidates import PROTOCOL
    return {
        "schema": 2, "protocol": PROTOCOL, "repo": repo, "pr": number, "head_repo_id": None,
        "repo_id": None, "head_repo": None, "authorized_actor_id": None,
        "head_ref": None, "frozen_sha": None, "freeze_status": "not_frozen",
        "workflow_revision": revision, "request_id": uuid.uuid4().hex,
        "frozen_at": now, "frozen_at_iso": iso(now),
        "deadline": now + DEFAULTS["deadline_seconds"], "budgets": DEFAULTS.copy(),
        "baseline_review_ids": [], "findings": [],
    }


def public_fetch(directory, request, repo=None, sha=None, depth=1):
    public_request(request)
    repo = request["head_repo"] if repo is None else repo
    sha = request["frozen_sha"] if sha is None else sha
    require(request["schema"] == 2 and repo in {request["repo"], request["head_repo"]}
            and REPO.fullmatch(repo), "Public source acquisition requires a frozen repository")
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1",
               GIT_CONFIG_COUNT="0")
    result = subprocess.run(
        ["git", "-c", "credential.helper=", "-c", "core.hooksPath=" + os.devnull,
         "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
         "-c", "http.followRedirects=false", "-c", "fetch.unpackLimit=1", "-c", "pack.threads=1",
         "fetch", "--quiet", "--no-auto-maintenance", "--depth=" + str(depth), "--no-tags",
         "https://github.com/" + repo + ".git", sha],
        cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        timeout=180, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        **({"preexec_fn": source_limits} if os.name == "posix" else {}),
    )
    if result.returncode:
        raise Rejected(f"Frozen public Git retrieval failed for {repo}@{sha} "
                       f"(exit {result.returncode}): "
                       + result.stderr.decode("utf-8", errors="replace")[:2000])


def source_limits():
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_SOURCE, MAX_SOURCE))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))


def snapshot_identity(directory, sha, request=None):
    require(git(["rev-parse", "refs/heads/snapshot"], directory).decode().strip() == sha,
            "Source snapshot head mismatch")
    refs = ["snapshot"]
    commits = {sha}
    conflict = request is not None and loop_kind(request) == "pr_conflict_resolver"
    if request is not None and diff_scope(request):
        require(git(["rev-parse", "refs/heads/review-base"], directory).decode().strip()
                == request["merge_base_sha"], "Source snapshot merge-base mismatch")
        refs.append("review-base")
        commits.add(request["merge_base_sha"])
        if conflict:
            require(git(["rev-parse", "refs/heads/incoming"], directory).decode().strip()
                    == request["base_sha"], "Source snapshot incoming base mismatch")
            refs.append("incoming")
            from loop.conflicts import history
            history(directory, request)
    count = int(git(["rev-list", "--count", *refs], directory))
    require(0 < count <= MAX_HISTORY if conflict else count == len(commits),
            "Source snapshot history is incomplete or exceeds limits")
    object_sizes = object_bounds(directory)
    for ref in refs:
        total = 0
        entries = tree_entries(directory, ref)
        require(len(entries) <= 100000, "Source exceeds file limit")
        for _, kind, oid in entries.values():
            if kind == "commit":
                continue
            require(oid in object_sizes, "Source snapshot is missing a blob")
            total += object_sizes[oid]
        require(total <= MAX_SOURCE, "Source snapshot tree exceeds expanded limit")
    git(["fsck", "--strict", "--no-reflogs"], directory)
    return git(["rev-parse", "snapshot^{tree}"], directory).decode().strip(), count


def package_source(request, generation, api, destination, fetch=None):
    require(request["schema"] == 2 and staged_source(request),
            "Staged source requires a complete public PR review scope")
    require(int(time.time()) < request["deadline"], "Source acquisition deadline passed")
    check_target(api, request)
    destination = Path(destination).resolve()
    destination.mkdir(exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="review-source-") as directory:
        git(["init", "--bare", "--quiet"], directory)
        shallow_path = Path(directory, "shallow")
        if fetch is None:
            if loop_kind(request) == "pr_conflict_resolver":
                public_fetch(directory, request, request["repo"], request["base_sha"], depth=MAX_HISTORY)
                git(["update-ref", "refs/heads/incoming", request["base_sha"]], directory)
                public_fetch(directory, request, depth=MAX_HISTORY)
                git(["update-ref", "refs/heads/review-base", request["merge_base_sha"]], directory)
                revisions = git(["rev-list", "--boundary", request["frozen_sha"],
                                 request["base_sha"], "^" + request["merge_base_sha"]],
                                directory).decode().splitlines()
                boundaries = {request["merge_base_sha"]}
                boundaries.update(line[1:] for line in revisions if line.startswith("-"))
                if shallow_path.exists():
                    boundaries.update(shallow_path.read_text(encoding="ascii").splitlines())
                shallow_path.write_text("\n".join(sorted(boundaries)) + "\n", encoding="ascii")
            elif diff_scope(request):
                public_fetch(directory, request, request["repo"], request["merge_base_sha"])
                require(git(["rev-parse", "FETCH_HEAD"], directory).decode().strip()
                        == request["merge_base_sha"], "Source acquisition returned a different merge-base")
                git(["update-ref", "refs/heads/review-base", request["merge_base_sha"]], directory)
            if loop_kind(request) != "pr_conflict_resolver":
                public_fetch(directory, request)
        else:
            fetch(directory)
        require(git(["rev-parse", "FETCH_HEAD"], directory).decode().strip() == request["frozen_sha"],
                "Source acquisition returned a different head")
        git(["update-ref", "refs/heads/snapshot", request["frozen_sha"]], directory)
        if loop_kind(request) == "pr_conflict_resolver":
            git(["repack", "-a", "-d"], directory)
            git(["prune", "--expire=now"], directory)
        tree, count = snapshot_identity(directory, request["frozen_sha"], request)
        bundle = destination / "source.bundle"
        refs = ["refs/heads/snapshot"]
        if diff_scope(request):
            refs.append("refs/heads/review-base")
        if loop_kind(request) == "pr_conflict_resolver":
            refs.append("refs/heads/incoming")
        git(["bundle", "create", str(bundle), *refs], directory)
        require(bundle.stat().st_size <= MAX_SOURCE, "Source bundle exceeds limit")
        manifest = {
            "schema": 2, "repo": request["repo"], "repo_id": request["head_repo_id"], "pr": request["pr"],
            "request_id": request["request_id"], "request_digest": digest(request),
            "frozen_sha": request["frozen_sha"], "tree": tree, "history_count": count,
            "shallow": git(["rev-parse", "--is-shallow-repository"], directory).strip() == b"true",
            "workflow_revision": request["workflow_revision"], "generation": generation,
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        }
        if diff_scope(request):
            manifest["review_scope"] = {
                key: request[key] for key in ("base_ref", "base_sha", "merge_base_sha")}
            manifest["review_scope"]["tree"] = git(
                ["rev-parse", "review-base^{tree}"], directory).decode().strip()
        if loop_kind(request) == "pr_conflict_resolver":
            reachable = set(git(["rev-list", "snapshot", "incoming"], directory).decode().splitlines())
            manifest["merge_history"] = {
                "base_tree": git(["rev-parse", "incoming^{tree}"], directory).decode().strip(),
                "boundary": request["merge_base_sha"],
                "shallow_commits": sorted(
                    set(shallow_path.read_text(encoding="ascii").splitlines()) & reachable
                ) if manifest["shallow"] else []}
        require(manifest["run_attempt"] == 1, "Source acquisition reruns are forbidden")
        (destination / "manifest.json").write_bytes(canonical(manifest))
    check_target(api, request)
    return manifest


def bind_manifest(manifest, request, generation):
    public_request(request)
    fields = {"schema", "repo", "repo_id", "pr", "request_id", "request_digest",
              "frozen_sha", "tree", "history_count", "workflow_revision", "generation",
              "run_id", "run_attempt", "bundle_sha256", "shallow"}
    count = 1
    if diff_scope(request):
        fields.add("review_scope")
        scope = manifest.get("review_scope")
        exact(scope, {"base_ref", "base_sha", "merge_base_sha", "tree"})
        require(all(scope[key] == request[key] for key in ("base_ref", "base_sha", "merge_base_sha"))
                and re.fullmatch(r"[0-9a-f]{40}", scope["tree"]), "Review source scope mismatch")
        count = len({request["frozen_sha"], request["merge_base_sha"]})
    if loop_kind(request) == "pr_conflict_resolver":
        fields.add("merge_history")
        exact(manifest.get("merge_history"), {"base_tree", "boundary", "shallow_commits"})
        require(manifest["merge_history"]["boundary"] == request["merge_base_sha"]
                and re.fullmatch(r"[0-9a-f]{40}", manifest["merge_history"]["base_tree"]),
                "Merge history source binding differs")
        boundaries = manifest["merge_history"]["shallow_commits"]
        require(isinstance(boundaries, list) and len(boundaries) <= MAX_HISTORY
                and all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)
                        for sha in boundaries)
                and boundaries == sorted(set(boundaries))
                and bool(boundaries) == manifest["shallow"],
                "Merge history shallow boundaries differ")
        count = manifest["history_count"]
        require(type(count) is int and 0 < count <= MAX_HISTORY, "Merge history exceeds limit")
    exact(manifest, fields)
    require(type(manifest["schema"]) is int and manifest["schema"] == 2
            and request["schema"] == 2 and manifest["repo"] == request["repo"]
            and manifest["repo_id"] == request["head_repo_id"]
            and manifest["pr"] == request["pr"] and manifest["request_id"] == request["request_id"]
            and manifest["request_digest"] == digest(request)
            and manifest["frozen_sha"] == request["frozen_sha"]
            and manifest["workflow_revision"] == request["workflow_revision"]
            and type(manifest["generation"]) is int and manifest["generation"] == generation
            and type(manifest["run_attempt"]) is int and manifest["run_attempt"] == 1
            and type(manifest["run_id"]) is int and manifest["run_id"] > 0
            and re.fullmatch(r"[0-9a-f]{40}", manifest["tree"])
            and type(manifest["history_count"]) is int and manifest["history_count"] == count
            and type(manifest["shallow"]) is bool
            and re.fullmatch(r"[0-9a-f]{64}", manifest["bundle_sha256"]),
            "Frozen source manifest binding mismatch")


def source_metadata(api, source, request):
    public_request(request)
    manifest = source["manifest"]
    run = api.call(f"repos/{CENTRAL}/actions/runs/{manifest['run_id']}")
    require(run["id"] == manifest["run_id"] and run["repository"]["full_name"] == CENTRAL
            and run["head_repository"]["full_name"] == CENTRAL
            and run["event"] in {"workflow_dispatch", "workflow_run", "schedule"}
            and run["path"].split("@")[0] == ".github/workflows/coordinator.yml"
            and run["head_sha"] == request["workflow_revision"] and run["run_attempt"] == 1,
            "Source producer workflow provenance mismatch")
    artifact = api.call(f"repos/{CENTRAL}/actions/artifacts/{source['artifact_id']}")
    require(artifact["id"] == source["artifact_id"]
            and artifact["name"] == "source-" + request["request_id"]
            and artifact["workflow_run"]["id"] == manifest["run_id"]
            and artifact["workflow_run"]["head_sha"] == request["workflow_revision"]
            and artifact["digest"] == source["artifact_digest"]
            and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"])
            and not artifact["expired"] and 0 < artifact["size_in_bytes"] <= MAX_SOURCE + 65536,
            "Source artifact provenance mismatch")


def download_source(api, state, destination):
    request, source = state["request"], state["source"]
    bind_manifest(source["manifest"], request, state["generation"])
    source_metadata(api, source, request)
    payload = api.artifact_zip(source["artifact_id"], MAX_SOURCE + 65536)
    require("sha256:" + hashlib.sha256(payload).hexdigest() == source["artifact_digest"],
            "Source server artifact digest mismatch")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        require(len(entries) == 2 and {e.filename for e in entries}
                == {"source.bundle", "manifest.json"}, "Unexpected source artifact members")
        require(sum(e.file_size for e in entries) <= MAX_SOURCE + 16384,
                "Expanded source artifact exceeds limit")
        for entry in entries:
            require(not entry.is_dir() and not stat.S_ISLNK(entry.external_attr >> 16)
                    and stat.S_IFMT(entry.external_attr >> 16) in {0, stat.S_IFREG}
                    and not entry.flag_bits & 1
                    and entry.file_size <= max(1, entry.compress_size) * 200,
                    "Unsafe source archive entry")
            require(entry.filename != "manifest.json" or entry.file_size <= 16384,
                    "Source manifest exceeds limit")
        manifest = parse_json(archive.read("manifest.json"))
        bundle = archive.read("source.bundle")
    require(manifest == source["manifest"] and len(bundle) <= MAX_SOURCE
            and hashlib.sha256(bundle).hexdigest() == manifest["bundle_sha256"],
            "Source package content mismatch")
    destination = Path(destination)
    destination.mkdir(exist_ok=False)
    (destination / "source.bundle").write_bytes(bundle)
    (destination / "manifest.json").write_bytes(canonical(manifest))
    return destination


def import_source(directory, bundle, manifest, request):
    bind_manifest(manifest, request, manifest["generation"])
    require(not Path(bundle).is_symlink() and Path(bundle).stat().st_size <= MAX_SOURCE
            and hashlib.sha256(Path(bundle).read_bytes()).hexdigest() == manifest["bundle_sha256"],
            "Source bundle changed")
    heads = git(["bundle", "list-heads", str(Path(bundle).resolve())], directory).decode().splitlines()
    expected = [request["frozen_sha"] + " refs/heads/snapshot"]
    if diff_scope(request):
        expected.append(request["merge_base_sha"] + " refs/heads/review-base")
    if loop_kind(request) == "pr_conflict_resolver":
        expected.append(request["base_sha"] + " refs/heads/incoming")
    require(sorted(heads) == sorted(expected),
            "Unexpected source bundle heads")
    if manifest["shallow"]:
        git_dir = git(["rev-parse", "--git-dir"], directory).decode().strip()
        commits = {request["frozen_sha"]}
        if diff_scope(request):
            commits.add(request["merge_base_sha"])
        if loop_kind(request) == "pr_conflict_resolver":
            commits = set(manifest["merge_history"]["shallow_commits"])
        (Path(directory) / git_dir / "shallow").write_text(
            "\n".join(sorted(commits)) + "\n", encoding="ascii")
    git(["bundle", "verify", str(Path(bundle).resolve())], directory)
    refs = ["refs/heads/snapshot:refs/heads/snapshot"]
    if diff_scope(request):
        refs.append("refs/heads/review-base:refs/heads/review-base")
    if loop_kind(request) == "pr_conflict_resolver":
        refs.append("refs/heads/incoming:refs/heads/incoming")
    git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-auto-maintenance",
         str(Path(bundle).resolve()), *refs], directory)
    tree, count = snapshot_identity(directory, request["frozen_sha"], request)
    require((tree, count) == (manifest["tree"], manifest["history_count"]),
            "Imported source tree/history mismatch")
    if diff_scope(request):
        require(git(["rev-parse", "review-base^{tree}"], directory).decode().strip()
                == manifest["review_scope"]["tree"], "Imported merge-base tree mismatch")
    if loop_kind(request) == "pr_conflict_resolver":
        require(git(["rev-parse", "incoming^{tree}"], directory).decode().strip()
                == manifest["merge_history"]["base_tree"], "Imported incoming tree mismatch")
