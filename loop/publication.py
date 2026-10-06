"""Authorized target publication. Git objects are data, never executable programs."""

import base64
import hashlib
import io
import os
import re
import stat
import subprocess
import time
import zipfile
from pathlib import Path

from loop.api import API
from loop.policy import (BOT_IDENTITY_PATH, CENTRAL, PROFILE, SHA, digest,
                         candidate_outcome, check_target, commit_author, loop_kind, pipeline_limit, require, source_effect, staged_source,
                         timestamp, unchanged)
from loop.verify import (artifact_metadata, git as object_git, parse_json, safe_path, verify)
from loop.candidates import current_request

SECRET = "TEST_PUBLISH_TOKEN"
MAX_PACKAGE = 20 * 1024 * 1024


def git(args, directory):
    from loop.source import source_limits
    return object_git(args, directory, limits=source_limits)


def object_bounds(directory):
    objects = git(["cat-file", "--batch-all-objects", "--batch-check=%(objectsize)"], directory)
    sizes = [int(line) for line in objects.splitlines()]
    require(len(sizes) <= 10000 and all(n <= 4 * 1024 * 1024 for n in sizes)
            and sum(sizes) <= 16 * 1024 * 1024, "Target Git object expansion exceeds limits")


class PublisherAPI(API):
    def __init__(self, token, request, auth_mode, *, source_write=True):
        require(auth_mode == "fine_grained_pat" and isinstance(token, str)
                and token.startswith("github_pat_") and len(token) > 30,
                "Explicit fine-grained publisher PAT required; no inference/classic/App fallback")
        personal(request)
        super().__init__(token)
        self.number = request["pr"]
        self.repo = request["repo"]
        self.head_repo = request["head_repo"]
        self.loop_kind = loop_kind(request)
        require(type(source_write) is bool and (source_write or self.loop_kind == "ci_fix"),
                "Only a CI rerun can select non-source publisher permissions")
        self.source_write = source_effect(request) and source_write
        self.request = request
        self.effect = None

    def bind_effect(self, intent):
        if intent is not None:
            if self.loop_kind in {"pr_description", "pr_review", "ci_fix"}:
                expected = {"pr_description": "metadata", "pr_review": "pending_review",
                            "ci_fix": "rerun"}[self.loop_kind]
                require(intent["kind"] == expected and intent["request_digest"] == digest(self.request)
                        and intent["generation"] == self.request["publication"]["generation"]
                        and intent["target"] == [self.repo, self.number],
                        "Unbound task effect")
                self.effect = intent
                return
            from loop.policy import bot
            require(self.loop_kind == "copilot_review" and intent["kind"] in {"reply", "resolve"}
                    and intent["request_digest"] == digest(self.request)
                    and intent["generation"] == self.request["publication"]["generation"]
                    and any(finding.get("kind") == "inline"
                            and bot(finding.get("root_author"))
                            and finding.get("comment_id") == intent["root"]
                            and finding.get("thread_id") == intent["thread"]
                            for finding in self.request["findings"]), "Unbound original-bot effect")
        self.effect = intent

    def authorize(self, path, method, data):
        if path == "graphql" and method == "POST" and data["query"].lstrip().startswith("query"):
            require(data["variables"].get("owner") == self.repo.split("/")[0]
                    and data["variables"].get("name") == self.repo.split("/")[1],
                    "Publisher query outside frozen target")
            return
        if method == "GET":
            require(path in {"user", BOT_IDENTITY_PATH}
                    or any(path == f"repos/{repo}" or path.startswith(f"repos/{repo}/")
                           for repo in {self.repo, self.head_repo}),
                    "Publisher read outside frozen target/head")
            return
        root = f"repos/{self.repo}/pulls/{self.number}"
        if self.effect and self.loop_kind == "pr_description":
            require(method == "PATCH" and path == root and data == self.effect["payload"]
                    and set(data) == {"title", "body"}, "Unbound metadata mutation")
        elif self.effect and self.loop_kind == "pr_review":
            require(method == "POST" and path == root + "/reviews" and data == self.effect["payload"]
                    and set(data) == {"commit_id", "comments"}
                    and data["commit_id"] == self.request["frozen_sha"],
                    "Only a bound pending review without submission is permitted")
        elif self.effect and self.loop_kind == "ci_fix":
            require(method == "POST" and path == f"repos/{self.repo}/actions/runs/{self.effect['run_id']}/rerun-failed-jobs"
                    and data is None, "Only the bound failed-jobs rerun is permitted")
        elif method == "POST" and path == root + "/requested_reviewers":
            require(self.loop_kind == "copilot_review"
                    and data == {"reviewers": ["copilot-pull-request-reviewer[bot]"]},
                    "Only a Copilot review request is permitted")
        elif method == "POST" and self.loop_kind == "copilot_review" and self.effect:
            from loop.effects import RESOLVE
            effect = self.effect
            if effect["kind"] == "reply":
                require(path == root + f"/comments/{effect['root']}/replies"
                        and data == {"body": effect["body"]}, "Unbound reply mutation")
            else:
                require(effect["kind"] == "resolve" and path == "graphql"
                        and data == {"query": RESOLVE, "variables": {
                            "thread": effect["thread"], "claim": effect["claim"]}},
                        "Unbound thread resolution mutation")
        else:
            raise ValueError("Publisher mutation outside the personal review protocol")

    def identity(self, request):
        user = self.call("user")
        require(user["id"] == request["authorized_actor_id"] and user["type"] == "User",
                "Publisher must authenticate as the authorized personal owner")
        login, _ = commit_author(request)
        require(isinstance(user.get("login"), str) and user["login"].casefold() == login.casefold(),
                "Publisher account differs from the frozen GitHub commit author")
        repo = None
        if self.source_write:
            repo = self.call(f"repos/{self.head_repo}")
            require(repo["id"] == request["head_repo_id"]
                    and repo["full_name"] == request["head_repo"]
                    and repo["private"] == request["source_private"]
                    and repo.get("permissions", {}).get("push") is True,
                    "Publisher lacks actual frozen head repository push access")
        target = self.call(f"repos/{self.repo}")
        require(target["id"] == request["repo_id"]
                and target["full_name"] == request["repo"]
                and target["private"] == request["target_private"],
                "Publisher lacks actual target repository read/review access")
        check_target(self, request)
        repo = repo or target
        return {"auth_mode": "fine_grained_pat", "actor_id": user["id"],
                "repo_id": repo["id"], "repo_node": repo["node_id"],
                "scope": "Explicit target/head issuance plus live identity/access checks"}


def personal(request):
    current_request(request)
    pipeline_limit(request)
    require(request["schema"] == 2 and request.get("mode") == "publish"
            and request["publication"]["profile"] == PROFILE
            and request["publication"]["auth_mode"] == "fine_grained_pat"
            and request["publication"]["authorized_actor_id"] == request["authorized_actor_id"],
            "Publication requires an owner-authorized generic phase; legacy evidence is read-only")
    commit_author(request)


def read_package(payload, names):
    require(0 < len(payload) <= MAX_PACKAGE, "Trusted artifact archive exceeds limit")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        require(len(entries) == len(names) and {e.filename for e in entries} == set(names)
                and sum(e.file_size for e in entries) <= MAX_PACKAGE, "Wrong artifact members")
        result = {}
        for entry in entries:
            mode = entry.external_attr >> 16
            require(not entry.is_dir() and stat.S_IFMT(mode) in {0, stat.S_IFREG}
                    and not entry.flag_bits & 1 and entry.file_size <= MAX_PACKAGE
                    and entry.file_size <= max(entry.compress_size, 1) * 200,
                    "Unsafe artifact member")
            result[entry.filename] = archive.read(entry)
        return result


def bound_artifact(api, run, name, names, recorded):
    artifacts = api.pages(f"repos/{CENTRAL}/actions/runs/{run['id']}/artifacts", "artifacts")
    selected = [a for a in artifacts if a["name"] == name]
    require(len(selected) == 1, "Missing/duplicate trusted pipeline artifact")
    artifact = selected[0]
    require(not artifact["expired"] and 0 < artifact["size_in_bytes"] <= MAX_PACKAGE
            and artifact["workflow_run"]["id"] == run["id"]
            and artifact["workflow_run"]["head_sha"] == run["head_sha"]
            and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"])
            and any(a["id"] == artifact["id"] and a["digest"] == artifact["digest"]
                    and a["name"] == name for a in recorded),
            "Artifact server provenance/retention differs from durable checkpoint")
    require(timestamp(artifact["created_at"]) >= timestamp(run["created_at"])
            and timestamp(artifact["expires_at"]) > int(time.time())
            and 0 < timestamp(artifact["expires_at"]) - timestamp(artifact["created_at"])
            <= 15 * 86400, "Artifact creation/retention is stale or outside the pinned policy")
    payload = api.artifact_zip(artifact["id"], MAX_PACKAGE)
    require("sha256:" + hashlib.sha256(payload).hexdigest() == artifact["digest"],
            "Artifact download hash differs")
    return read_package(payload, names), {key: artifact[key] for key in ("id", "name", "digest")}


def acceptance(state, manifest):
    request, result = state["request"], state["report"]
    personal(request)
    candidate = result["candidate"]
    chain(candidate, request)
    candidate_outcome(result["dispositions"], request, candidate)
    batches = result["dispositions"]["batches"]
    merge = loop_kind(request) == "pr_conflict_resolver"
    require(merge or len(candidate["commits"]) == len(batches)
            and all(entry["patch_sha256"] == batch["sha256"]
                    for entry, batch in zip(candidate["commits"], batches)),
            "Batch-chain semantic mapping differs")
    expected_mapping = {key: entry["commit"] for entry, batch in zip(candidate["commits"], batches)
                        for key in batch.get("findings", [])}
    require(candidate["finding_commits"] == expected_mapping, "Finding-to-commit mapping differs")
    require(result["request_digest"] == digest(request)
            and result["schema"] == 2
            and result.get("verification") == "verified"
            and result["publication_eligible"] is False
            and result["verification_run"] == state["verification_run"]
            and not {"validation", "validation_claim", "objective_validation"} & result.keys()
            and result["repo"] == request["repo"] and result["pr"] == request["pr"]
            and result["workflow_revision"] == request["workflow_revision"]
            and result["frozen_sha"] == request["frozen_sha"]
            and result["run_id"] == state["run"]["id"]
            and result["run_attempt"] == state["run"]["attempt"] == 1
            and (loop_kind(request) != "copilot_review" and result["dispositions"]["outcome"] != "blocked"
                 or result["dispositions"]["outcome"] in {"fixes", "no_change"}
                 and all(f["disposition"] != "blocked" for f in result["dispositions"]["findings"])),
            "Wrong, blocked, or stale personal result")
    require(candidate["parent"] == request["frozen_sha"]
            and all(SHA.fullmatch(candidate[k]) for k in ("commit", "parent"))
            and (candidate["tree"] is None if loop_kind(request) == "pr_description"
                 else SHA.fullmatch(candidate["tree"]))
            and manifest == dict(candidate, schema=2, request_digest=digest(request),
                                 prerequisite=request["frozen_sha"], repo=request["repo"],
                                 repo_id=request["head_repo_id"], head_repo=request["head_repo"],
                                 source_private=request["source_private"]),
            "Candidate manifest differs from independently reconstructed identity")
    if staged_source(request):
        require(candidate["source_bundle_sha256"] == state["source"]["manifest"]["bundle_sha256"],
                "Candidate differs from verified source bundle")
    return {"profile": request["publication"]["profile"], "request_digest": digest(request),
            "candidate_commit": candidate["commit"], "verification_sha256": digest(result)}


def evidence(api, state, destination):
    request = state["request"]
    personal(request)
    pipeline = api.call(f"repos/{CENTRAL}/actions/runs/{state['verification_run']['id']}")
    require(pipeline["repository"]["full_name"] == CENTRAL
            and pipeline["head_repository"]["full_name"] == CENTRAL
            and pipeline["path"].split("@")[0] == ".github/workflows/coordinator.yml"
            and pipeline["head_sha"] == request["workflow_revision"]
            and pipeline["id"] == state["verification_run"]["id"]
            and pipeline["event"] in {"workflow_dispatch", "workflow_run", "schedule"}
            and pipeline["run_attempt"] == state["verification_run"]["attempt"] == 1
            and pipeline["status"] == "completed" and pipeline["conclusion"] == "success",
            "Wrong/incomplete trusted verification pipeline")
    jobs = api.pages(f"repos/{CENTRAL}/actions/runs/{pipeline['id']}/attempts/1/jobs", "jobs")
    for name in ("verify", "finalize"):
        selected = [j for j in jobs if j["name"] == name]
        require(len(selected) == 1 and selected[0]["conclusion"] == "success",
                "Trusted verification/finalization job did not pass")
    package_names = {"verification-report.json", "candidate-package/manifest.json",
                     "candidate-package/candidate.bundle"}
    if staged_source(request):
        package_names.add("candidate-package/source.bundle")
    package, package_id = bound_artifact(
        api, pipeline, f"verification-{pipeline['id']}-1",
        package_names, state["artifacts"])
    report = parse_json(package["verification-report.json"])
    require(type(report["schema"]) is int and report["schema"] == 2
            and report["request_id"] == request["request_id"]
            and report["request_digest"] == digest(request)
            and report["generation"] == state["generation"]
            and report["verification"] == "verified"
            and report["run_id"] == state["run"]["id"] and report["run_attempt"] == 1,
            "Verification artifact request/generation differs")
    run = api.call(f"repos/{CENTRAL}/actions/runs/{state['run']['id']}")
    require(run["id"] == state["run"]["id"], "Wrong worker run identity")
    artifact, _ = artifact_metadata(api, run, request)
    payload = api.artifact_zip(artifact["id"], 6 * 1024 * 1024)
    require("sha256:" + hashlib.sha256(payload).hexdigest() == artifact["digest"],
            "Worker artifact server hash differs")
    def fetch_source(directory):
        if staged_source(request):
            from loop.source import import_source
            source_bundle = Path(destination) / "source.bundle"
            source_bundle.write_bytes(package["candidate-package/source.bundle"])
            import_source(directory, source_bundle, state["source"]["manifest"], request)
        else:
            git(["fetch", "--quiet", "--depth=1", "--no-tags",
                 "https://github.com/" + request["head_repo"] + ".git", request["frozen_sha"]], directory)
        object_bounds(directory)
    reconstructed = verify(payload, request, run, artifact, fetch_source)
    require(all(reconstructed[key] == state["report"][key]
                for key in reconstructed if key != "candidate"),
            "Fresh worker bindings/dispositions differ from accepted checkpoint")
    candidate = state["report"]["candidate"]
    for key in reconstructed["candidate"]:
        require(candidate[key] == reconstructed["candidate"][key],
                "Candidate differs from fresh credential-free reconstruction")
    require(all(report["result"][key] == state["report"][key]
                for key in report["result"]),
            "Trusted result differs from checkpoint")
    manifest = parse_json(package["candidate-package/manifest.json"])
    accepted = acceptance(state, manifest)
    bundle = package["candidate-package/candidate.bundle"]
    require(hashlib.sha256(bundle).hexdigest() == candidate["bundle_sha256"],
            "Candidate bundle hash mismatch")
    bundle_path = Path(destination) / "candidate.bundle"
    bundle_path.write_bytes(bundle)
    if loop_kind(request) == "pr_description":
        require(bundle == b"", "Description task contains a source bundle")
    else:
        import_candidate(destination, bundle_path, request, candidate, fetch_source)
    return dict(accepted, artifacts=[package_id]), candidate


def import_candidate(directory, bundle, request, candidate, fetch_source=None):
    personal(request)
    require(loop_kind(request) != "pr_description", "Description task has no Git candidate to import")
    git(["init", "--bare", "--quiet"], directory)
    if fetch_source is not None:
        fetch_source(directory)
    else:
        require(not staged_source(request), "Review import requires a bound source bundle")
        git(["fetch", "--quiet", "--depth=1", "--no-tags",
             "https://github.com/" + request["head_repo"] + ".git", request["frozen_sha"]], directory)
    chain(candidate, request)
    if not candidate["changed"]:
        require(Path(bundle).read_bytes() == b"" and candidate["commit"] == request["frozen_sha"]
                and git(["rev-parse", request["frozen_sha"] + "^{tree}"], directory).decode().strip()
                == candidate["tree"], "Invalid no-change package")
        git(["update-ref", "refs/heads/candidate", candidate["commit"]], directory)
        object_bounds(directory)
        git(["fsck", "--strict", "--no-reflogs"], directory)
        return
    if loop_kind(request) == "pr_conflict_resolver":
        heads = git(["bundle", "list-heads", str(Path(bundle).resolve())], directory).decode().splitlines()
        require(heads == [candidate["commit"] + " refs/heads/candidate"], "Unexpected merge bundle refs")
        git(["bundle", "verify", str(Path(bundle).resolve())], directory)
        git(["-c", "protocol.file.allow=always", "fetch", "--quiet", str(Path(bundle).resolve()),
             "refs/heads/candidate:refs/heads/candidate"], directory)
        require(git(["rev-list", "--parents", "-1", candidate["commit"]], directory).decode().strip()
                == candidate["commit"] + " " + request["frozen_sha"] + " " + request["base_sha"]
                and git(["rev-parse", candidate["commit"] + "^{tree}"], directory).decode().strip()
                == candidate["tree"], "Merge parents or tree differ")
        from loop.conflicts import resolved_tree
        require(resolved_tree(directory, request, candidate["tree"]) == candidate["changed_paths"],
                "Merge incoming changes or resolutions differ")
        object_bounds(directory)
        git(["fsck", "--strict", "--no-reflogs"], directory)
        return
    header = Path(bundle).read_bytes().split(b"\n\n", 1)[0].decode("utf-8")
    require(header.split("\n") == [
        "# v2 git bundle", "-" + request["frozen_sha"] + " " +
        git(["show", "-s", "--format=%s", request["frozen_sha"]], directory).decode().strip(),
        candidate["commit"] + " refs/heads/candidate"], "Unexpected bundle refs/prerequisites")
    git(["bundle", "verify", str(Path(bundle).resolve())], directory)
    git(["-c", "protocol.file.allow=always", "fetch", "--quiet", str(Path(bundle).resolve()),
         "refs/heads/candidate:refs/heads/candidate"], directory)
    object_bounds(directory)
    require(git(["rev-list", "--reverse", candidate["commit"], "^" + request["frozen_sha"]],
                directory).decode().splitlines() == [entry["commit"] for entry in candidate["commits"]],
            "Candidate contains unaccounted history")
    for entry in candidate["commits"]:
        require(git(["rev-list", "--parents", "-1", entry["commit"]], directory).decode().strip()
                == entry["commit"] + " " + entry["parent"]
                and git(["rev-parse", entry["commit"] + "^{tree}"], directory).decode().strip()
                == entry["tree"]
                and git(["show", "-s", "--format=%s", entry["commit"]], directory).decode().strip()
                == entry["subject"], "Candidate has wrong batch parent/tree/subject")
        changed = git(["diff", "--name-only", "--no-renames", "-z", entry["parent"], entry["commit"]],
                      directory).decode("utf-8").split("\0")[:-1]
        require(changed == entry["changed_paths"], "Intermediate candidate paths differ")
        for path in changed:
            safe_path(path, request)
    git(["fsck", "--strict", "--no-reflogs"], directory)
    paths = git(["diff", "--name-only", "--no-renames", "-z", request["frozen_sha"],
                 candidate["commit"]], directory).decode("utf-8").split("\0")[:-1]
    require(paths == candidate["changed_paths"], "Candidate paths differ")
    for path in paths:
        safe_path(path, request)


def authenticated_push(directory, request, commit, token):
    personal(request)
    require(source_effect(request), "Report-only tasks cannot push")
    require(SHA.fullmatch(commit) and token.startswith("github_pat_"), "Invalid push binding/auth")
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    credential = base64.b64encode(("x-access-token:" + token).encode()).decode()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1",
               GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0=f"http.https://github.com/{request['head_repo']}.git/.extraheader",
               GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + credential)
    argv = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=",
            "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
            "-c", "core.attributesFile=" + os.devnull, "push", "--porcelain"]
    proc = subprocess.run(argv + ["https://github.com/" + request["head_repo"] + ".git",
                                 commit + ":refs/heads/" + request["head_ref"]],
                          cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=120, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    require(len(proc.stdout) + len(proc.stderr) <= 1024 * 1024, "Git push output exceeds limit")
    require(proc.returncode == 0, "Publisher Git push failed or uncertain; reconcile exact live refs")


def plan(request, live, candidate, authorization, mode):
    require(mode == "publish", "Publication requires an explicit publish invocation")
    personal(request)
    unchanged(request, live)
    chain(candidate, request)
    require(candidate["parent"] == request["frozen_sha"] and SHA.fullmatch(candidate["commit"])
            and SHA.fullmatch(candidate["tree"]), "Candidate is not a one-parent descendant")
    require(authorization == {"request_id": request["request_id"],
                              "expected_sha": request["frozen_sha"],
                              "candidate_commit": candidate["commit"],
                              "generation": request["publication"]["generation"]},
            "Wrong exact publication authorization")


def chain(candidate, request):
    commits = candidate.get("commits")
    mapping = candidate.get("finding_commits")
    require(isinstance(commits, list) and len(commits) <= 100 and isinstance(mapping, dict)
            and type(candidate["changed"]) is bool and candidate["changed"] == bool(commits),
            "Malformed batch-chain evidence")
    parent = request["frozen_sha"]
    if loop_kind(request) == "pr_conflict_resolver":
        require(len(commits) <= 1 and not mapping, "Invalid merge commit count or mapping")
        if commits:
            require(commits[0]["parents"] == [request["frozen_sha"], request["base_sha"]],
                    "Frozen head must be first parent and base second")
    for entry in commits:
        require(entry["parent"] == parent and all(SHA.fullmatch(entry[key])
                for key in ("commit", "tree", "parent")), "Invalid linear batch chain")
        parent = entry["commit"]
    require(candidate["commit"] == parent and candidate["parent"] == request["frozen_sha"]
            and (not commits or candidate["tree"] == commits[-1]["tree"])
            and all(sha in {entry["commit"] for entry in commits} for sha in mapping.values()),
            "Candidate tip or finding mapping differs from chain")


def reconcile_uncertain_push(request, candidate, live):
    unchanged(dict(request, frozen_sha=live["head"]["sha"]), live)
    if live["head"]["sha"] == candidate["commit"]:
        return "published_waiting_review_request"
    if live["head"]["sha"] == request["frozen_sha"]:
        return "blocked_uncertain_publication_requires_operator"
    return "blocked_target_changed"
