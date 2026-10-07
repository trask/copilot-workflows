"""Entry points used by trusted central workflows."""

import argparse
import hashlib
import json
import os
import sys
import time
import subprocess
import zipfile
import shutil
from pathlib import Path

from loop.api import API, APIError
from loop.coordinator import (cancel, checkpoint, dispatch, due, matching_runs, quiescent, reconcile,
                              record_result, run_binding)
from loop.control import busy as coordinator_busy, claim as claim_coordinator, owned
from loop.publisher_auth import publisher_secret
from loop.freeze import freeze
from loop.policy import (AUTHOR_ID, CENTRAL, LOOP_KINDS, REQUEST, TERMINAL, Rejected, canonical,
                         check_target, checkpoint_name, digest, effect_repository, eligible, loop_kind, parse_target,
                         staged_source, publication_gate,
                         pipeline_budget, require, supported_checkpoint)
from loop.state import State
from loop.verify import artifact_metadata, git, parse_json, verify
from loop.source import (bind_manifest, download_source, gated_request, import_source,
                         package_source, source_metadata, target_api)
from loop.reviews import select_checks
from loop.live import (STAGES as LIVE_STAGES, publication_invocation,
                       authorize_publication_reconciliation, execution_revision,
                       start as start_publication)
from loop.revisions import execution_ref, pin_revision, workflow_ref


def output(name, value):
    value = str(value)
    require("\n" not in value and "\r" not in value, "Unsafe Actions output")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as file:
            file.write(f"{name}={value}\n")


def get_state(store, number, request_id=None, repo=None):
    _, _, entries = store.snapshot()
    matches = [(name, state) for name, state in entries.items()
               if name.startswith("pr-") and state["request"]["pr"] == number
               and state["request"]["repo"] == repo
               and (request_id is None or state["request"]["request_id"] == request_id)]
    frozen = [(name, state) for name, state in matches
              if state.get("schema") == 2 and state["request"].get("repo_id")]
    matches = frozen or matches
    require(len(matches) == 1, "Unknown or ambiguous PR checkpoint")
    name, state = matches[0]
    if request_id is not None:
        require(REQUEST.fullmatch(request_id) and state["request"]["request_id"] == request_id,
                "Wrong request identity")
    return name, state


def select_publisher(api, target, kind="copilot_review"):
    publication_invocation()
    repo, number = parse_target(target)
    try:
        live = target_api(api, repo).call(f"repos/{repo}/pulls/{number}")
    except APIError as error:
        if error.status not in {401, 403, 404}:
            raise
        print("Publisher selection deferred to the target repository read-access gate.")
        return
    request = eligible(live, repo, int(os.environ["GITHUB_ACTOR_ID"]), kind)
    require(request["pr"] == number, "Publisher selection returned another PR")
    output("publisher_head_repo", effect_repository(dict(request, loop_kind=kind)))
    output("publisher_secret", publisher_secret(effect_repository(dict(request, loop_kind=kind))))


def choose_verification(store, now=None, api=None, only=None):
    now = int(time.time()) if now is None else now
    _, _, entries = store.snapshot()
    for name, state in sorted(entries.items()):
        if only is not None and name != only:
            continue
        if (name.startswith("pr-v2-") and state.get("schema") == 2
                and state["stage"] == "verify_pending"
                and now < state["request"]["deadline"]):
            pipeline_budget(state)
            request = state["request"]
            if (workflow_ref(request) != "main"
                    and os.environ["GITHUB_SHA"] != execution_revision(state)):
                continue
            if api is not None:
                if coordinator_busy(api, state):
                    continue
                try:
                    check_target(target_api(api, request["repo"]), request)
                except (Rejected, RuntimeError) as error:
                    def blocked(current):
                        require(current == state, "Verification selection changed")
                        current.update(stage="blocked", reason="target_repository_read_or_stale_gate",
                                       error=type(error).__name__ + ": " + str(error)[:1000])
                        return current
                    summary(store.update(name, blocked))
                    continue
                state = claim_coordinator(store, name, state, api)
                if state is None:
                    continue
            for key, value in {"verify": "true", "pr": request["pr"],
                               "repo": request["repo"],
                               "request_id": request["request_id"],
                               "revision": request["workflow_revision"],
                               "generation": state["generation"]}.items():
                output(key, value)
            return name


def choose_live(store, now, api=None, only=None):
    _, _, entries = store.snapshot()
    for name, state in sorted(entries.items()):
        if only is not None and name != only:
            continue
        if (not name.startswith("pr-v2-") or state.get("schema") != 2
                or state["stage"] in TERMINAL):
            continue
        supported_checkpoint(state)
        if state["stage"] not in LIVE_STAGES:
            continue
        pipeline_budget(state)
        if now >= state["request"]["deadline"]:
            def expired(current):
                require(current == state, "Concurrent live deadline transition")
                current.update(stage="exhausted", reason="elapsed_deadline")
                return current
            summary(store.update(name, expired))
            continue
        if state["next_check_at"] > now:
            continue
        request = state["request"]
        if (execution_ref(state) != "main"
                and os.environ["GITHUB_SHA"] != execution_revision(state)):
            continue
        if (execution_ref(state) == "main" and api is not None
                and api.call(f"repos/{CENTRAL}/git/ref/heads/main")["object"]["sha"]
                != execution_revision(state)):
            def changed(current):
                require(current == state, "Concurrent trusted-revision transition")
                current.update(stage="blocked", reason="trusted_revision_changed_before_live")
                return current
            summary(store.update(name, changed))
            continue
        if api is not None:
            state = claim_coordinator(store, name, state, api)
            if state is None:
                continue
        for key, value in {"live": "true", "live_pr": request["pr"],
                           "live_repo": request["repo"],
                           "live_request": request["request_id"],
                           "live_revision": execution_revision(state),
                           "live_generation": state["generation"],
                           "live_stage": state["stage"],
                           "live_publisher_secret": publisher_secret(effect_repository(
                               request, (state.get("report") or {}).get("dispositions", {}).get("outcome")))}.items():
            output(key, value)
        return name


def prepare(api, store, args):
    _, state = get_state(store, int(args.pr), args.request_id, args.repo)
    require(state["stage"] in {"dispatch_intent", "dispatched", "running"},
            "Worker is not durably authorized")
    pipeline_budget(state)
    request = state["request"]
    require(int(os.environ["GITHUB_RUN_ATTEMPT"]) == 1, "Worker reruns require a new request")
    runs = matching_runs(api, request)
    require(len(runs) == 1 and runs[0]["id"] == int(os.environ["GITHUB_RUN_ID"]),
            "Duplicate or unclaimed worker dispatch")
    run_binding(runs[0], request)
    require(os.environ["GITHUB_SHA"] == request["workflow_revision"], "Worker revision mismatch")
    check_target(target_api(api, request["repo"], request["head_repo"]), request)
    if staged_source(request):
        source = download_source(api, state, "frozen-source")
        output("source_bundle", str(source / "source.bundle"))
    require(int(time.time()) < request["deadline"], "Request deadline passed")
    Path("frozen-request.json").write_bytes(canonical(request))
    Path("frozen-digest.txt").write_text(digest(request), encoding="ascii")
    output("digest", digest(request))


def verify_pending(api, store, args):
    name, state = get_state(store, int(args.pr), args.request_id, args.repo)
    require(state["stage"] == "verify_pending" and state["generation"] == int(args.generation),
            "Stale or cancelled verification claim")
    supported_checkpoint(state)
    owned(state)
    request = state["request"]
    report = {"schema": 2, "request_id": request["request_id"], "generation": state["generation"],
              "run_id": state["run"]["id"], "run_attempt": state["run"]["attempt"],
              "request_digest": digest(request), "artifacts": [], "verification": "failed"}
    try:
        require(git(["rev-parse", "HEAD"], Path.cwd()).decode().strip()
                == request["workflow_revision"],
                "Verifier did not check out the frozen trusted revision")
        require(int(time.time()) < request["deadline"], "Verification deadline passed")
        check_target(target_api(api, request["repo"], request["head_repo"]), request)
        fetch = None
        source = None
        if staged_source(request):
            source = download_source(api, state, "frozen-source")
            def fetch(directory):
                import_source(directory, source / "source.bundle", state["source"]["manifest"], request)
        run = api.call(f"repos/{CENTRAL}/actions/runs/{state['run']['id']}")
        artifact, artifacts = artifact_metadata(api, run, request)
        report["artifacts"] = [
            {k: a[k] for k in ("id", "name", "size_in_bytes", "expired", "digest")}
            for a in artifacts
        ]
        payload = api.artifact_zip(artifact["id"], artifact["size_in_bytes"])
        require("sha256:" + hashlib.sha256(payload).hexdigest() == artifact["digest"],
                "Downloaded artifact differs from trusted server digest")
        report.update(verification="verified",
                      result=verify(payload, request, run, artifact, fetch,
                                    package_dir="candidate-package"))
        if source is not None:
            shutil.copyfile(source / "source.bundle", Path("candidate-package") / "source.bundle")
            manifest_path = Path("candidate-package", "manifest.json")
            candidate_manifest = parse_json(manifest_path.read_bytes())
            source_hash = state["source"]["manifest"]["bundle_sha256"]
            candidate_manifest["source_bundle_sha256"] = source_hash
            manifest_path.write_bytes(canonical(candidate_manifest))
            report["result"]["candidate"]["source_bundle_sha256"] = source_hash
            report["source"] = state["source"]
            report["request"] = request
    except (Rejected, ValueError, KeyError, OSError, RuntimeError,
            zipfile.BadZipFile, subprocess.SubprocessError) as error:
        report["error"] = type(error).__name__ + ": " + str(error)[:1000]
    Path("verification-report.json").write_bytes(canonical(report))
    # Failures are explicit report data; finalize writes a failed checkpoint and Actions summary.
    print("Artifact verification: " + report["verification"])


def finalize(api, store, args):
    name, expected = get_state(store, int(args.pr), args.request_id, args.repo)
    require(expected["stage"] == "verify_pending"
            and expected["generation"] == int(args.generation), "Cancelled or stale finalization")
    supported_checkpoint(expected)
    owned(expected)
    try:
        _finalize(api, store, args)
    except (Rejected, ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        def failed(current):
            require(current["request"] == expected["request"]
                    and current["generation"] == expected["generation"],
                    "Cancelled or replaced finalization")
            if current["stage"] == "verify_pending":
                current.update(stage="failed", reason="finalization_failed",
                               error=type(error).__name__ + ": " + str(error)[:1000],
                               finalizer_run=os.environ.get("GITHUB_RUN_ID"),
                               finalizer_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"))
            return current
        summary(store.update(name, failed))
        raise


def _finalize(api, store, args):
    name, state = get_state(store, int(args.pr), args.request_id, args.repo)
    require(state["stage"] == "verify_pending" and state["generation"] == int(args.generation),
            "Cancelled or stale finalization")
    if int(time.time()) >= state["request"]["deadline"]:
        def exhausted(current):
            require(current == state, "Checkpoint changed during deadline finalization")
            current.update(stage="exhausted", reason="elapsed_deadline")
            return current
        summary(store.update(name, exhausted))
        return
    path = Path("verification-report.json")
    if not path.exists():
        def failed(current):
            require(current == state, "Checkpoint changed during failure finalization")
            current.update(stage="failed", reason="verifier_job_failed_no_report")
            return current
        state = store.update(name, failed)
    else:
        require(path.stat().st_size <= 1024 * 1024 and not path.is_symlink(), "Unsafe verifier report")
        report = parse_json(path.read_bytes())
        require(report["schema"] == 2 and report["request_id"] == args.request_id
                and report["generation"] == state["generation"]
                and report["run_id"] == state["run"]["id"]
                and report["run_attempt"] == state["run"]["attempt"]
                and report["request_digest"] == digest(state["request"]),
                "Verifier report does not match checkpoint")
        if report["verification"] != "verified":
            def failed(current):
                require(current == state, "Checkpoint changed during finalization")
                current.update(stage="failed", reason="artifact_verification_failed",
                               report=report, artifacts=report["artifacts"])
                return current
            state = store.update(name, failed)
        else:
            result = report["result"]
            require(result["request_digest"] == digest(state["request"])
                    and result["schema"] == 2 and result["verification"] == "verified"
                    and result["publication_eligible"] is False
                    and not {"validation", "validation_claim", "objective_validation"} & result.keys(),
                    "Wrong structural verifier result")
            if staged_source(state["request"]):
                require(result["candidate"]["source_bundle_sha256"]
                        == state["source"]["manifest"]["bundle_sha256"],
                        "Wrong verified source binding")
            try:
                check_target(target_api(api, state["request"]["repo"]), state["request"])
            except Rejected as error:
                def stale(current):
                    require(current == state, "Checkpoint changed during stale-head finalization")
                    current.update(stage="blocked", reason="stale_target",
                                   report=report, artifacts=report["artifacts"])
                    return current
                summary(store.update(name, stale))
                raise Rejected("Target changed during finalization; request is blocked") from error
            central_artifacts = api.pages(
                f"repos/{CENTRAL}/actions/runs/{os.environ['GITHUB_RUN_ID']}/artifacts", "artifacts")
            report["artifacts"].extend(
                {key: item.get(key) for key in
                 ("id", "name", "size_in_bytes", "expired", "digest")}
                for item in central_artifacts
            )
            if state["request"].get("mode") == "publish":
                result["verification_run"] = {"id": int(os.environ["GITHUB_RUN_ID"]),
                                              "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])}
                require(result["verification_run"]["attempt"] == 1,
                        "Publication verification pipeline reruns are forbidden")
            state = record_result(store, name, state, result, report["artifacts"])
    summary(state)
    require(state["stage"] != "failed", "Verification failed; inspect retained report")


def summary(state):
    text = (f"## Review loop checkpoint\n\nStage: `{state['stage']}`\n\n"
            f"Target: `{state['request']['repo']}#{state['request']['pr']}`\n\n"
            f"Request: `{state['request']['request_id']}`\n\n"
            f"Frozen head: `{state['request']['frozen_sha'] or 'not frozen'}`\n\n"
            f"Reason: `{state['reason']}`\n\n"
            f"Protocol: `{state['request'].get('protocol', 'historical')}`\n\n"
            f"Loop kind: `{loop_kind(state['request'])}`\n\n"
            f"Phase: `{state.get('phase', state['request'].get('publication', {}).get('phase', state['request']['request_id']))}`\n\n"
            f"Confirmed publication effects: `{len(state.get('publications', []))}`.\n")
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as file:
            file.write(text)


def stage_source(api, store, name, state):
    supported_checkpoint(state)
    owned(state)
    run_id, attempt = int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"])
    require(attempt == 1, "Source acquisition reruns are forbidden")
    def claim(current):
        require(current == state and not current.get("source_claim"),
                "Source acquisition already claimed; reconcile rather than retry")
        current.update(source_claim={"run_id": run_id, "run_attempt": attempt},
                       next_check_at=int(time.time()) + 300)
        return current
    state = store.update(name, claim)
    try:
        package_source(state["request"], state["generation"], target_api(api, state["request"]["repo"]),
                       "source-package")
    except (Rejected, RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        def failed(current):
            require(current == state, "Source acquisition changed")
            current.update(stage="failed", reason="source_acquisition_failed",
                           error=type(error).__name__ + ": " + str(error)[:1000])
            return current
        summary(store.update(name, failed))
        raise
    for key, value in {"source_upload": "true", "source_request": state["request"]["request_id"],
                       "source_pr": state["request"]["pr"], "source_generation": state["generation"],
                       "source_repo": state["request"]["repo"]}.items():
        output(key, value)


def attach_source(api, store, args):
    name, state = get_state(store, int(args.pr), args.request_id, args.repo)
    require(state["stage"] == "source_pending" and state["generation"] == int(args.generation)
            and state["source_claim"] == {"run_id": int(os.environ["GITHUB_RUN_ID"]),
                                         "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])}
            and int(time.time()) < state["request"]["deadline"], "Stale source attachment")
    supported_checkpoint(state)
    owned(state)
    manifest = parse_json(Path("source-package", "manifest.json").read_bytes())
    bind_manifest(manifest, state["request"], state["generation"])
    source = {"manifest": manifest, "artifact_id": int(os.environ["SOURCE_ARTIFACT_ID"]),
              "artifact_digest": os.environ["SOURCE_ARTIFACT_DIGEST"]}
    source_metadata(api, source, state["request"])
    check_target(target_api(api, state["request"]["repo"]), state["request"])
    def ready(current):
        require(current == state, "Cancelled or replaced source acquisition")
        current.update(stage="ready", source=source)
        return current
    store.update(name, ready)
    dispatch(store, name, api, int(time.time()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["launch", "reconcile-publication",
                                             "cancel", "status", "tick", "prepare",
                                             "verify", "finalize", "publish", "attach-source",
                                             "select-publisher"])
    parser.add_argument("--target", default="")
    parser.add_argument("--loop-kind", choices=sorted(LOOP_KINDS),
                        default=os.environ.get("LOOP_KIND", "copilot_review"))
    parser.add_argument("--pr", default=os.environ.get("PR", ""))
    parser.add_argument("--request-id", default=os.environ.get("REQUEST_ID", ""))
    parser.add_argument("--generation", default=os.environ.get("GENERATION", ""))
    parser.add_argument("--repo", default=os.environ.get("TARGET_REPO", ""))
    parser.add_argument("--previous-request", default=os.environ.get("PREVIOUS_REQUEST", ""))
    parser.add_argument("--previous-generation", default=os.environ.get("PREVIOUS_GENERATION", ""))
    parser.add_argument("--restart-unpublished", action="store_true",
                        default=os.environ.get("RESTART_UNPUBLISHED") == "true")
    args = parser.parse_args()
    require(not args.restart_unpublished or args.operation == "launch",
            "Unpublished restart is restricted to explicit launch")
    api = API()
    if args.operation == "select-publisher":
        select_publisher(api, args.target, args.loop_kind)
        return
    store = State(api)
    now = int(time.time())
    selection = None
    if args.operation == "publish":
        publication_gate()
    elif args.operation == "reconcile-publication":
        publication_invocation()
        repo, number = parse_target(args.target)
        require(os.environ.get("PUBLICATION_AUTH_MODE") == "fine_grained_pat",
                "Reconciliation is restricted to explicit personal publication mode")
        revision = api.call(f"repos/{CENTRAL}/git/ref/heads/main")["object"]["sha"]
        require(os.environ["GITHUB_SHA"] == revision, "Reconciliation must run at current trusted main")
        _, existing = get_state(store, number, args.previous_request, repo)
        name, state = authorize_publication_reconciliation(
            store, api, target_api(api, repo, existing["request"]["head_repo"]), args.previous_request,
            int(args.previous_generation), revision, now, repo, number)
        summary(state)
        selection = name
    elif args.operation == "launch":
        repo, number = parse_target(args.target)
        publication_invocation()
        revision = os.environ["GITHUB_SHA"]
        try:
            request = freeze(target_api(api, repo), number, revision, repo=repo,
                             actor_id=int(os.environ["GITHUB_ACTOR_ID"]), loop_kind=args.loop_kind)
        except APIError as error:
            if args.restart_unpublished or error.status not in {401, 403, 404}:
                raise
            request = gated_request(repo, number, revision, now)
            request["loop_kind"] = args.loop_kind
            request["authorized_actor_id"] = int(os.environ["GITHUB_ACTOR_ID"])
            request["launch_run"] = {"id": int(os.environ["GITHUB_RUN_ID"]), "attempt": 1,
                                     "actor_id": int(os.environ["GITHUB_ACTOR_ID"])}
            name = checkpoint_name(repo, number)
            def gated(previous):
                if previous:
                    quiescent(api, previous)
                state = checkpoint(request)
                state.update(stage="blocked", reason="human_gate_target_repository_read_access")
                return state
            state = store.update(name, gated)
            summary(state)
            return
        now = int(time.time())
        request["launch_run"] = {"id": int(os.environ["GITHUB_RUN_ID"]), "attempt": 1,
                                 "actor_id": int(os.environ["GITHUB_ACTOR_ID"])}
        require(os.environ.get("PUBLISHER_HEAD_REPO") == effect_repository(request)
                    and os.environ.get("PUBLISHER_SECRET_NAME", "")
                    == publisher_secret(effect_repository(request)),
                    "Publisher routing changed before the target freeze")
        request["workflow_ref"] = pin_revision(api, revision)
        name, state = start_publication(
                store, api, request, args.previous_request, int(args.previous_generation or "0"),
                os.environ.get("PUBLISHER_AVAILABLE") == "true",
                os.environ.get("PUBLICATION_AUTH_MODE", ""),
                [] if args.loop_kind in {"pr_description", "pr_review"} else select_checks(target_api(api, repo), request),
                os.environ.get("INFERENCE_AVAILABLE") == "true", now,
                restart_unpublished=args.restart_unpublished,
                read=target_api(api, repo, request["head_repo"]))
        summary(state)
        selection = name
        if state["stage"] == "ready":
            dispatch(store, name, api, now)
        elif state["stage"] == "source_pending" and not state.get("source_claim"):
            stage_source(api, store, name, state)
    elif args.operation in {"cancel", "status"}:
        repo, number = parse_target(args.target)
        name, state = get_state(store, number, repo=repo)
        summary(cancel(store, name, args.previous_request, int(args.previous_generation), now)
                if args.operation == "cancel" else state)
    elif args.operation == "prepare":
        prepare(api, store, args)
    elif args.operation == "verify":
        verify_pending(api, store, args)
    elif args.operation == "finalize":
        finalize(api, store, args)
    elif args.operation == "attach-source":
        attach_source(api, store, args)
    elif args.operation == "tick":
        _, _, entries = store.snapshot()
        if args.target:
            repo, number = parse_target(args.target)
            selection, state = get_state(store, number, args.previous_request or None, repo)
            require(not args.previous_generation
                    or state["generation"] == int(args.previous_generation),
                    "Stale targeted coordinator generation")
            entries = {selection: state}
        for name, state in entries.items():
            if not name.startswith("pr-"):
                continue
            if due(state, now):
                supported_checkpoint(state)
                if (execution_ref(state) != "main"
                        and os.environ["GITHUB_SHA"] != execution_revision(state)):
                    from loop.waiter import wake
                    wake(api, state)
                    continue
                if coordinator_busy(api, state):
                    continue
                if state["stage"] in LIVE_STAGES:
                    continue
                if state["stage"] == "ready":
                    dispatch(store, name, api, now)
                elif state["stage"] == "source_pending" and not state.get("source_claim"):
                    state = claim_coordinator(store, name, state, api)
                    if state is not None:
                        stage_source(api, store, name, state)
                    selection = name
                    break
                elif state["stage"] != "verify_pending" or now >= state["request"]["deadline"]:
                    reconcile(store, name, api, now)
    if args.operation in {"launch", "reconcile-publication", "tick"}:
        verification = choose_verification(store, api=api, only=selection)
        if verification is None:
            choose_live(store, now, api, only=selection)


if __name__ == "__main__":
    try:
        main()
    except (Rejected, ValueError, KeyError, RuntimeError, OSError) as error:
        print(f"FAIL CLOSED: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
