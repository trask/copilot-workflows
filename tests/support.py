"""Current artifact and admission fixtures for offline protocol tests."""

import hashlib
import json
import io
import tempfile
import zipfile
from pathlib import Path

from loop.candidates import PROTOCOL, TRAILER
from loop.policy import DEFAULTS, PROFILE, commit_author, digest, loop_kind, require
from loop.verify import git, parse_json, verify_candidate as reconstruct_candidate, verify as verify_bundle

REASONING = {"analysis": "Investigated", "upsides": "Rejects invalid input",
             "downsides": "No material downside identified"}


def batch(patch, keys=None, offset=0):
    value = dict(REASONING, summary="Validate input", offset=offset, length=len(patch),
                 sha256=hashlib.sha256(patch).hexdigest())
    if keys is not None:
        value["findings"] = keys
    return value


def semantic(request, outcome, patch=b"", disposition="fixed"):
    external = request.get("loop_kind", "copilot_review") == "copilot_review"
    keys = [finding["key"] for finding in request["findings"]]
    value = {"schema": 2, "request_digest": digest(request), "outcome": outcome}
    if request.get("input_mode") == "direct":
        value["input_identity"] = request["inputs"]["identity"]
    if external:
        value["findings"] = [dict(analysis=REASONING["analysis"], key=key, disposition=disposition,
                                  commit=1 if disposition == "fixed" else None) for key in keys]
    return value


def native_files(files, request, fetch_source, parts=None):
    """Build native bundle fixtures from small test-local diffs."""
    files = dict(files)
    if "candidate.bundle" in files:
        return files
    patch = files["candidate.patch"]
    if "result.json" not in files:
        files["result.json"] = json.dumps(semantic(
            request, "fixes" if patch else "clean" if request.get("loop_kind") == "self_review"
            else "no_change", patch, "fixed" if patch else "not_warranted")).encode()
    value = parse_json(files["result.json"])
    del files["candidate.patch"]
    files["candidate.bundle"] = b""
    name, email = commit_author(request)
    require(type(request.get("frozen_at")) is int and request["frozen_at"] >= 0,
            "Invalid frozen commit timestamp")
    merge = loop_kind(request) == "pr_conflict_resolver" and value["outcome"] == "merge"
    if patch or merge:
        require(fetch_source is not None, "Fixture requires a bound source")
        with tempfile.TemporaryDirectory() as directory:
            git(["init", "--bare", "--quiet"], directory)
            fetch_source(directory)
            parent = request["frozen_sha"]
            git(["read-tree", parent], directory)
            for part in parts or [patch]:
                if part:
                    git(["apply", "--cached", "--whitespace=nowarn", "-"], directory, part)
                tree = git(["write-tree"], directory).decode().strip()
                parents = [parent, request["base_sha"]] if merge else [parent]
                text = "Validate input\n\nAnalysis: Investigated\n\n" + TRAILER + "\n"
                if loop_kind(request) == "copilot_review":
                    findings = request["findings"]
                    text = ("Address Copilot review comment" + ("s" if len(findings) > 1 else "")
                            + ": Validate input\n\n"
                            + "\n\n".join("Copilot comment:\n\n" + f["body"] for f in findings)
                            + "\n\nAnalysis: Investigated\n\n" + TRAILER + "\n")
                obj = (f"tree {tree}\n" + "".join(f"parent {p}\n" for p in parents)
                       + f"author {name} <{email}> {request['frozen_at']} +0000\n"
                       + f"committer {name} <{email}> {request['frozen_at']} +0000\n\n"
                       + text).encode()
                parent = git(["hash-object", "-t", "commit", "-w", "--stdin"], directory, obj).decode().strip()
            git(["update-ref", "refs/heads/candidate", parent], directory)
            destination = Path(directory, "candidate.bundle")
            exclusions = ["^" + request["frozen_sha"]]
            if merge:
                exclusions.append("^" + request["base_sha"])
            git(["bundle", "create", str(destination), "refs/heads/candidate", *exclusions], directory)
            files["candidate.bundle"] = destination.read_bytes()
    return files


def reconstruct(files, request, fetch_source=None, package_dir=None, *, parts=None):
    files = native_files(files, request, fetch_source, parts)
    candidate = reconstruct_candidate(files, request, fetch_source)
    package_fixture(candidate, request, files["candidate.bundle"], package_dir)
    return candidate


def package_fixture(candidate, request, bundle, directory):
    if directory is not None:
        destination = Path(directory)
        destination.mkdir(exist_ok=True)
        (destination / "candidate.bundle").write_bytes(bundle)
        manifest = dict(candidate, schema=2, request_digest=digest(request),
                        prerequisite=request["frozen_sha"], repo=request["repo"],
                        repo_id=request["head_repo_id"], head_repo=request["head_repo"],
                        source_private=request["source_private"])
        (destination / "manifest.json").write_bytes(json.dumps(manifest, sort_keys=True).encode())


def verify(payload, request, run, artifact, fetch_source=None, package_dir=None):
    require(request["schema"] == 2, "Legacy reports are read-only")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if "candidate.patch" in names and "result.json" in names:
            files = native_files({name: archive.read(name) for name in names}, request, fetch_source)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as converted:
                for name, data in files.items():
                    converted.writestr(name, data)
            payload = output.getvalue()
    report = verify_bundle(payload, request, run, artifact, fetch_source)
    if package_dir is not None:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            bundle = archive.read("candidate.bundle")
        package_fixture(report["candidate"], request, bundle, package_dir)
    return report


def launch(store, request, _mode, auth_available, source_available=True, api=None):
    from loop.live import start
    from loop.coordinator import checkpoint
    from loop.policy import checkpoint_name
    request = dict(request, protocol=PROTOCOL, budgets=DEFAULTS.copy())
    if request.get("freeze_status") == "not_frozen":
        state = checkpoint(request)
        state.update(stage="blocked", reason="human_gate_target_repository_read_access")
        name = checkpoint_name(request["repo"], request["pr"])
        return name, store.update(name, lambda previous: state)
    request["findings"] = [dict(finding, kind=finding.get("kind", "body"))
                           for finding in request["findings"]]
    return start(store, api, request, "", 0, source_available, "fine_grained_pat",
                 [], auth_available, request["frozen_at"])
