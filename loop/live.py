"""Durable authorized publication, exact-head review waiting and bounded iterations."""

import copy
import os
import tempfile
import time
import uuid
from pathlib import Path

from loop.api import API, APIError
from loop.coordinator import checkpoint, executions_quiescent, quiescent
from loop.publisher_auth import publisher_secret
from loop.freeze import freeze
from loop.policy import (AUTHOR_ID, CENTRAL, DEFAULTS, PROFILE, REQUEST, TERMINAL, Rejected,
                         bot, check_target, checkpoint_name, commit_author, digest, pipeline_budget, pipeline_limit,
                         candidate_outcome, effect_repository, loop_kind, public_request, require, staged_source,
                         supported_checkpoint, timestamp, unchanged)
from loop.publication import (PublisherAPI,
                              authenticated_push, chain, evidence, personal, plan,
                              reconcile_uncertain_push)
from loop.reviews import (exact_ci, finding_fingerprint, fresh_collection,
                          inline_fingerprints, missed_fingerprints)
from loop.state import State
from loop.verify import artifact_metadata, git
from loop.revisions import execution_revision, inherit_pin, pin_revision, workflow_ref

STAGES = {"publish_pending", "publication_intent", "published", "review_request_intent",
          "waiting_review", "waiting_ci", "thread_effects", "threads_settled", "task_effect_intent"}
PUSH_PROPAGATION_SECONDS = 900


def publication_invocation():
    require(os.environ.get("GITHUB_REPOSITORY") == CENTRAL
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_ACTOR_ID") == str(AUTHOR_ID)
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1",
            "A fresh explicit personal-owner Actions dispatch on central main is required")


def owner():
    return {"run_id": os.environ.get("GITHUB_RUN_ID"),
            "attempt": os.environ.get("GITHUB_RUN_ATTEMPT")}


def owns_pending(state):
    pending = [state.get("publication_intent"), state.get("review_request"), state.get("task_intent")]
    pending.extend(effect.get(kind) for effect in state.get("effects", [])
                   for kind in ("reply", "resolution"))
    return any(intent and intent.get("owner") == owner()
               and intent.get("status") in {"uncertain", "acknowledged"}
               for intent in pending)


def unpublished_restart(central, read, prior, request, now):
    personal(prior["request"])
    maximum = pipeline_budget(prior)
    require(prior["stage"] == "blocked"
            and prior["reason"] in {"blocked_uncertain_publication_requires_operator",
                                    "publication_API_propagation_timeout"}
            and not prior.get("review_request") and not prior.get("reconciliation")
            and not prior.get("effects")
            and 0 < prior["iteration"] <= maximum,
            "Restart requires a stopped unpublished push with no pending review effect")
    old = prior["request"]
    require(all(request[key] == old[key] for key in
                ("repo", "repo_id", "pr", "head_repo", "head_repo_id", "head_ref",
                 "frozen_sha", "source_private", "target_private", "authorized_actor_id")),
            "Unpublished restart target identity or original frozen head differs")
    intent, report = prior["publication_intent"], prior["report"]
    candidate = intent["candidate"]
    require(intent["status"] in {"uncertain", "acknowledged"}
            and candidate == report["candidate"] and candidate["changed"] is True
            and candidate["parent"] == prior["expected_sha"] == old["frozen_sha"]
            and intent["authorization"] == {
                "request_id": old["request_id"], "generation": prior["generation"],
                "expected_sha": old["frozen_sha"], "candidate_commit": candidate["commit"]}
            and report["schema"] == 2 and report.get("verification") == "verified"
            and not {"validation", "validation_claim", "objective_validation"} & report.keys()
            and report["request_digest"] == intent["acceptance"]["request_digest"] == digest(old)
            and intent["acceptance"]["candidate_commit"] == candidate["commit"]
            and intent["acceptance"]["profile"] == PROFILE
            and intent["acceptance"]["verification_sha256"] == digest(report)
            and report["run_id"] == prior["run"]["id"]
            and report["run_attempt"] == prior["run"]["attempt"] == 1
            and report["verification_run"] == prior["verification_run"],
            "Stopped publication intent or structural acceptance identity differs")
    executions_quiescent(central, prior)
    publisher_run = None
    for execution in ({"id": int(intent["owner"]["run_id"]),
                       "attempt": int(intent["owner"]["attempt"])}, prior["verification_run"]):
        run = central.call(f"repos/{CENTRAL}/actions/runs/{execution['id']}")
        require(run["id"] == execution["id"] and run["run_attempt"] == execution["attempt"] == 1
                and run["repository"]["full_name"] == run["head_repository"]["full_name"] == CENTRAL
                and run["path"].split("@")[0] == ".github/workflows/coordinator.yml"
                and run["head_branch"] in {"main", workflow_ref(old)}
                and run["head_sha"] == old["workflow_revision"]
                and run["status"] == "completed" and run.get("conclusion"),
                "Previous publisher or verification pipeline is active or unbound")
        if publisher_run is None:
            publisher_run = run
    require(now >= timestamp(publisher_run["updated_at"]) + PUSH_PROPAGATION_SECONDS,
            "Wait fifteen minutes after the original publisher stops before retiring its intent")
    check_target(read, request)
    ref = read.call(f"repos/{old['head_repo']}/git/ref/heads/{old['head_ref']}")
    require(ref["object"]["sha"] == old["frozen_sha"],
            "Unpublished restart requires the actual head ref and PR at the original frozen head")
    require(request["launch_run"] == {
        "id": int(os.environ["GITHUB_RUN_ID"]), "attempt": 1, "actor_id": AUTHOR_ID},
        "Restart must bind to the fresh owner dispatch")
    return {
        "kind": "stopped_unpublished_push", "previous_request_id": old["request_id"],
        "previous_generation": prior["generation"],
        "archive": f"request-{old['request_id']}.json",
        "candidate_commit": candidate["commit"], "observed_head": old["frozen_sha"],
        "publisher_run": {"id": publisher_run["id"], "attempt": publisher_run["run_attempt"],
                          "completed_at": publisher_run["updated_at"]},
        "authorized_at": now, "launch_run": copy.deepcopy(request["launch_run"]),
    }


def start(store, api, request, previous_id, previous_generation, publisher_available,
          auth_mode, required_check, inference_available, now, *, restart_unpublished=False, read=None):
    require(request["schema"] == 2, "Legacy requests are read-only")
    public_request(request)
    commit_author(request)
    require(loop_kind(request) != "copilot_review" or request["findings"],
            "Publication requires existing frozen Copilot findings")
    name = checkpoint_name(request["repo"], request["pr"], request["repo_id"])
    _, _, entries = store.snapshot()
    prior = entries.get(name)
    restart = None
    if restart_unpublished:
        publication_invocation()
        require(previous_id and previous_generation > 0 and prior is not None
                and publisher_available and inference_available and auth_mode == "fine_grained_pat"
                and read is not None,
                "Unpublished restart requires exact prior identity and configured publication access")
    require((not previous_id and previous_generation == 0)
            or (prior is not None and REQUEST.fullmatch(previous_id)
                and prior["request"]["request_id"] == previous_id
                and prior["generation"] == previous_generation),
            "Explicit previous phase identity differs from the observed checkpoint")
    if prior:
        require(request["repo"] == prior["request"]["repo"] and request["pr"] == prior["request"]["pr"],
                "New phase must address the same target PR")
        if restart_unpublished:
            restart = unpublished_restart(api, read, prior, request, now)
        else:
            confirmation = quiescent(api, prior, read)
            if confirmation is not None:
                restart = {
                    "kind": "confirmed_review_request",
                    "previous_request_id": prior["request"]["request_id"],
                    "previous_generation": prior["generation"],
                    "confirmation": confirmation, "authorized_at": now,
                }
        require(request["request_id"] != prior["request"]["request_id"],
                "A fresh phase requires a new request identity")
    require(pipeline_limit(request) == DEFAULTS["max_iterations"],
            "A new phase requires the current freshly frozen request budget")
    require(request["frozen_at"] <= now < request["deadline"], "New phase freeze expired")
    generation = 1 if prior is None else prior["generation"] + 1
    require(auth_mode in {"disabled", "fine_grained_pat"},
            "Choose an explicit supported publisher credential mode")
    require(isinstance(required_check, list) and len(required_check) <= 100
            and all(isinstance(name, str) and 0 < len(name) <= 200 for name in required_check),
            "Invalid frozen target CI selection")
    request = dict(request, mode="publish",
                   deadline=min(request["deadline"], now + DEFAULTS["deadline_seconds"]),
                   publication={
        "profile": PROFILE, "auth_mode": auth_mode, "generation": generation,
        "authorized_actor_id": request["authorized_actor_id"],
        "required_checks": required_check,
        "phase": uuid.uuid4().hex, "authorized_at": now,
        "continuation_deadline": now + DEFAULTS["deadline_seconds"],
        "max_pipelines": request["budgets"]["max_iterations"],
    })
    value = checkpoint(request)
    value.update(generation=generation, stage="source_pending" if staged_source(request) else "ready",
                 effects=[], publications=[], seen_findings=(
                     [finding_fingerprint(request["findings"])] if request["findings"] else []),
                 seen_inline_findings=inline_fingerprints(request["findings"]),
                 seen_missed_findings=missed_fingerprints(request["findings"]))
    if restart is not None:
        value["restart"] = restart
    if auth_mode == "disabled" or not publisher_available:
        value.update(stage="blocked", reason="human_gate_target_repository_push_and_review_access")
    elif loop_kind(request) == "pr_conflict_resolver" and request["merge_base_sha"] == request["base_sha"]:
        value.update(stage="complete", reason="base_already_incorporated",
                     task_completion={"outcome": "no_change"})
    elif not inference_available:
        value.update(stage="blocked", reason="human_gate_COPILOT_GITHUB_TOKEN")
    elif loop_kind(request) == "ci_fix":
        value.update(stage="waiting_ci", reason="CI_preflight", next_check_at=now)
    def replace(current):
        require(current == prior, "Cancelled or concurrently replaced publication start")
        return value
    return name, store.update(name, replace)


def cas(store, name, expected, **updates):
    def operation(current):
        require(current == expected, "Cancelled, replaced or concurrently advanced live checkpoint")
        current.update(copy.deepcopy(updates))
        return current
    return store.update(name, operation)


def authorize_publication_reconciliation(store, central, read, previous_id, generation, revision, now,
                                         repo, number):
    head, _, entries = store.snapshot()
    matches = [name for name, state in entries.items() if name.startswith("pr-v2-")
               and state.get("schema") == 2 and state["request"].get("repo_id")
               and state["request"]["repo"] == repo and state["request"]["pr"] == number
               and state["request"]["request_id"] == previous_id]
    require(len(matches) == 1, "Missing or ambiguous generic checkpoint")
    name = matches[0]
    prior = entries[name]
    request = prior["request"]
    personal(request)
    maximum = pipeline_budget(prior)
    require(request["request_id"] == previous_id and prior["generation"] == generation
            and prior["stage"] == "blocked"
            and prior["reason"] == "blocked_uncertain_publication_requires_operator"
            and not prior.get("reconciliation") and not prior["publications"] and not prior["effects"]
            and 0 < prior["iteration"] <= maximum and now < request["deadline"]
            and now < request["publication"]["continuation_deadline"],
            "Only an exact unexpired blocked publication intent can be reconciled once")
    intent = prior["publication_intent"]
    candidate = intent["candidate"]
    require(candidate == prior["report"]["candidate"] and candidate["changed"] is True
            and prior["report"]["schema"] == 2
            and candidate["parent"] == request["frozen_sha"]
            and prior["expected_sha"] == request["frozen_sha"]
            and intent["authorization"] == {
                "request_id": previous_id, "generation": generation,
                "expected_sha": request["frozen_sha"], "candidate_commit": candidate["commit"]}
            and intent["status"] in {"uncertain", "acknowledged"}
            and intent["acceptance"]["request_digest"] == digest(request)
            and intent["acceptance"]["candidate_commit"] == candidate["commit"]
            and intent["acceptance"]["profile"] == PROFILE,
            "Existing authorized publication identity differs")
    require(prior["report"].get("verification") == "verified"
            and not {"validation", "validation_claim", "objective_validation"} & prior["report"].keys()
                and intent["acceptance"]["verification_sha256"] == digest(prior["report"])
                and prior["report"]["run_id"] == prior["run"]["id"]
                and prior["report"]["run_attempt"] == prior["run"]["attempt"] == 1
                and prior["report"]["verification_run"] == prior["verification_run"],
            "Saved structural acceptance differs")
    verification_run = prior["verification_run"]
    run_id = int(intent["owner"]["run_id"])
    run = central.call(f"repos/{CENTRAL}/actions/runs/{run_id}")
    require(run["id"] == run_id and intent["owner"]["attempt"] == "1"
            and run["repository"]["full_name"] == run["head_repository"]["full_name"] == CENTRAL
            and run["head_sha"] == request["workflow_revision"]
            and run["path"].split("@")[0] == ".github/workflows/coordinator.yml"
            and run["run_attempt"] == 1 and run["status"] == "completed"
            and run["conclusion"] == "success", "Wrong original publication execution provenance")
    jobs = central.pages(f"repos/{CENTRAL}/actions/runs/{run_id}/attempts/1/jobs", "jobs")
    selected = [job for job in jobs if job["name"] == "personal_live"]
    require(len(selected) == 1 and selected[0]["conclusion"] == "success",
            "Original publication job is not successfully completed")
    pipeline = central.call(f"repos/{CENTRAL}/actions/runs/{verification_run['id']}")
    require(pipeline["id"] == verification_run["id"]
            and pipeline["head_sha"] == request["workflow_revision"]
            and pipeline["run_attempt"] == verification_run["attempt"] == 1
            and pipeline["repository"]["full_name"] == pipeline["head_repository"]["full_name"] == CENTRAL
            and pipeline["path"].split("@")[0] == ".github/workflows/coordinator.yml"
            and pipeline["status"] == "completed" and pipeline["conclusion"] == "success"
            and prior["report"]["request_digest"] == digest(request),
            "Original accepted verification pipeline provenance differs")
    artifacts = central.pages(f"repos/{CENTRAL}/actions/runs/{pipeline['id']}/artifacts", "artifacts")
    for recorded in intent["acceptance"]["artifacts"]:
        matched = [artifact for artifact in artifacts if artifact["id"] == recorded["id"]]
        require(len(matched) == 1 and not matched[0]["expired"]
                and matched[0]["name"] == recorded["name"] and matched[0]["digest"] == recorded["digest"],
                "Original accepted artifact identity is missing, expired or changed")
    worker = central.call(f"repos/{CENTRAL}/actions/runs/{prior['run']['id']}")
    artifact, _ = artifact_metadata(central, worker, request)
    require(worker["id"] == prior["run"]["id"] and worker["run_attempt"] == prior["run"]["attempt"] == 1
            and any(item["id"] == artifact["id"] and item["digest"] == artifact["digest"]
                    for item in prior["artifacts"]),
            "Original worker artifact proof differs")
    ref = read.call(f"repos/{request['head_repo']}/git/ref/heads/{request['head_ref']}")
    unchanged(dict(request, frozen_sha=candidate["commit"]),
              read.call(f"repos/{request['repo']}/pulls/{request['pr']}"))
    chain(candidate, request)
    require(ref["object"]["sha"] == candidate["commit"],
            "Live ref does not prove the exact already-authorized push")
    for entry in candidate["commits"]:
        commit = read.call(f"repos/{request['head_repo']}/git/commits/{entry['commit']}")
        require(commit["sha"] == entry["commit"] and commit["tree"]["sha"] == entry["tree"]
                and [parent["sha"] for parent in commit["parents"]]
                == entry.get("parents", [entry["parent"]]),
                "Live history does not prove the exact already-authorized batch chain")
    pinned_ref = pin_revision(central, revision) if workflow_ref(request) != "main" else None
    def reconcile(existing):
        require(existing == prior, "Cancelled or concurrently reconciled publication")
        existing.update(stage="publication_intent", reason="explicit_published_history_reconciliation",
                        next_check_at=now, reconciliation={
                            "execution_revision": revision, "original_revision": request["workflow_revision"],
                            "original_stage": prior["stage"], "original_reason": prior["reason"],
                            "prior_state_commit": head, "authorized_at": now,
                            "stopped_archive": f"stopped-{previous_id}-{generation}.json",
                            "published_ref": candidate["commit"], "owner": owner(),
                            **({"workflow_ref": pinned_ref} if pinned_ref is not None else {}),
                        })
        return existing
    return name, store.update(name, reconcile)


def current(store, name):
    _, _, entries = store.snapshot()
    require(name in entries, "Missing live checkpoint")
    return entries[name]


def guard(store, name, state, read, now, sha=None):
    pipeline_budget(state)
    require(current(store, name) == state and state["stage"] not in TERMINAL
            and now < state["request"]["deadline"]
            and now < state["request"]["publication"]["continuation_deadline"],
            "Cancelled, expired, replaced or stale mutation intent")
    check_target(read, dict(state["request"], frozen_sha=sha or state["expected_sha"]))
    ref = read.call(f"repos/{state['request']['head_repo']}/git/ref/heads/{state['request']['head_ref']}")
    require(ref["object"]["sha"] == (sha or state["expected_sha"]),
            "Actual head ref changed or disagrees with the PR")


def observed_review_request(read, state, intent, *, allow_head_change=False):
    pr = read.call(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}")
    sha = pr["head"]["sha"] if allow_head_change else intent["sha"]
    unchanged(dict(state["request"], frozen_sha=sha), pr)
    reviews = read.pages(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}/reviews")
    fresh = [r for r in reviews if bot(r.get("user")) and r.get("submitted_at")
             and r["id"] not in intent["baseline_review_ids"]
             and r["commit_id"] == intent["sha"]
             and timestamp(r["submitted_at"]) > intent["recorded_at"]
             and r["state"] in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}]
    if fresh:
        return {"kind": "submitted_review", "review_ids": sorted(r["id"] for r in fresh)}
    runs = read.pages(f"repos/{state['request']['repo']}/actions/runs?head_sha={intent['sha']}", "workflow_runs")
    fresh = [r for r in runs if r["id"] not in intent["baseline_run_ids"]
             and r["head_sha"] == intent["sha"] and bot(r.get("actor"))
             and r["event"] == "dynamic" and r["path"] == "dynamic/agents/copilot-pull-request-reviewer"
             and timestamp(r["created_at"]) >= intent["recorded_at"]
             and r["status"] in {"queued", "in_progress", "completed"}]
    if len(fresh) == 1:
        return {"kind": "copilot_workflow", "run_id": fresh[0]["id"], "sha": intent["sha"],
                "status": fresh[0]["status"], "conclusion": fresh[0].get("conclusion")}
    require(len(fresh) <= 1, "Ambiguous Copilot review-request workflows")
    if pr["head"]["sha"] == intent["sha"] and any(
            bot(user) for user in pr.get("requested_reviewers", [])):
        return {"kind": "requested_reviewer", "bot_id": 175728472, "sha": intent["sha"]}
    return None


def new_review_intent(read, state, now):
    pr = read.call(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}")
    unchanged(dict(state["request"], frozen_sha=state["expected_sha"]), pr)
    if any(bot(u) for u in pr.get("requested_reviewers", [])):
        return None
    reviews = read.pages(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}/reviews")
    runs = read.pages(f"repos/{state['request']['repo']}/actions/runs?head_sha={state['expected_sha']}", "workflow_runs")
    if any(bot(r.get("actor")) and r["status"] != "completed"
           and r["path"] == "dynamic/agents/copilot-pull-request-reviewer" for r in runs):
        return None
    return {"claim": uuid.uuid4().hex, "recorded_at": int(time.time()), "sha": state["expected_sha"],
            "baseline_review_ids": sorted(r["id"] for r in reviews),
            "baseline_run_ids": sorted(r["id"] for r in runs), "status": "uncertain",
            "owner": owner()}


def confirm_request(store, name, state, read, intent, now):
    pipeline_budget(state)
    confirmation = observed_review_request(read, state, intent)
    if confirmation is None:
        if now >= intent["recorded_at"] + 900:
            return cas(store, name, state, stage="blocked",
                       reason="review_request_uncertain_no_retry", next_check_at=now)
        return cas(store, name, state, next_check_at=now + 300)
    return cas(store, name, state, stage="waiting_review",
               review_request=dict(intent, status="confirmed", confirmation=confirmation),
               next_check_at=now + 300)


def guard_review_request(store, name, state, read):
    guard(store, name, state, read, int(time.time()))
    pr = read.call(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}")
    require(not any(bot(u) for u in pr.get("requested_reviewers", [])),
            "Copilot became pending before mutation; request not issued")


def publish(store, name, state, central, read, publisher, now):
    pipeline_budget(state)
    require(state["iteration"] > 0, "Publication requires a consumed model pipeline")
    request = state["request"]
    if loop_kind(request) in {"pr_description", "pr_review"} or (
            loop_kind(request) == "ci_fix" and state["report"]["dispositions"]["outcome"] == "rerun"):
        from loop.task_effects import publish_task
        return publish_task(store, name, state, central, read, publisher, now)
    publisher.identity(request)
    with tempfile.TemporaryDirectory(prefix="trusted-publisher-") as directory:
        accepted, candidate = evidence(central, state, directory)
        if loop_kind(request) == "ci_fix":
            from loop.ci import same_attempts
            same_attempts(read, request)
        live = read.call(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}")
        authorization = {"request_id": request["request_id"], "expected_sha": request["frozen_sha"],
                         "candidate_commit": candidate["commit"], "generation": state["generation"]}
        plan(request, live, candidate, authorization, "publish")
        intent = {"claim": uuid.uuid4().hex, "recorded_at": now, "candidate": candidate,
                  "acceptance": accepted, "authorization": authorization, "status": "uncertain",
                  "owner": owner()}
        state = cas(store, name, state, stage="publication_intent",
                    publication_intent=intent, next_check_at=now + 300)
        guard(store, name, state, read, int(time.time()))
        if loop_kind(request) == "ci_fix":
            from loop.ci import same_attempts
            same_attempts(read, request)
        if candidate["changed"]:
            authenticated_push(directory, request, candidate["commit"], publisher.token)
            state = cas(store, name, state, publication_intent=dict(
                intent, status="acknowledged", acknowledged_at=int(time.time())))
        # No-change retains the frozen head without creating or pushing a commit.
        return confirm_push(store, name, state, read, int(time.time()))


def confirm_push(store, name, state, read, now):
    intent = state["publication_intent"]
    candidate = intent["candidate"]
    live = read.call(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}")
    if not candidate["changed"]:
        unchanged(state["request"], live)
        outcome, sha = "published_waiting_review_request", state["request"]["frozen_sha"]
    else:
        ref = read.call(f"repos/{state['request']['head_repo']}/git/ref/heads/{state['request']['head_ref']}")
        ref_sha = ref["object"]["sha"]
        propagating = (ref_sha == candidate["commit"]
                       and live["head"]["sha"] == state["request"]["frozen_sha"]
                       or ref_sha == live["head"]["sha"] == state["request"]["frozen_sha"]
                       and intent["status"] == "acknowledged")
        if propagating:
            unchanged(state["request"], live)
            if now >= intent.get("acknowledged_at", intent["recorded_at"]) + PUSH_PROPAGATION_SECONDS:
                return cas(store, name, state, stage="blocked",
                           reason="publication_API_propagation_timeout", next_check_at=now)
            return cas(store, name, state, reason="publication_API_propagation",
                       next_check_at=now + 300)
        if ref_sha != live["head"]["sha"]:
            return cas(store, name, state, stage="blocked", reason="publication_ref_PR_disagree")
        outcome = reconcile_uncertain_push(state["request"], candidate, live)
        sha = candidate["commit"]
    if outcome != "published_waiting_review_request":
        return cas(store, name, state, stage="blocked", reason=outcome, next_check_at=now)
    guard(store, name, state, read, now, sha=sha)
    publications = state["publications"] + [{
        "request_id": state["request"]["request_id"], "generation": state["generation"],
        "sha": sha, "candidate": candidate, "acceptance": intent["acceptance"],
        "confirmed_at": now, "effect": "push" if candidate["changed"] else "no_change",
    }]
    return cas(store, name, state, stage="published", expected_sha=sha,
               publications=publications, publication_intent=dict(intent, status="confirmed"),
               next_check_at=now)


def request_review(store, name, state, read, publisher, now):
    pipeline_budget(state)
    require(loop_kind(state["request"]) == "copilot_review", "Self-review cannot request external review")
    require(state["stage"] == "threads_settled",
            "Fresh review requires settled mandatory thread effects")
    intent = new_review_intent(read, state, now)
    if intent is None:
        return cas(store, name, state, reason="existing_Copilot_review_pending",
                   next_check_at=now + 300)
    state = cas(store, name, state, stage="review_request_intent",
                reason="fresh_Copilot_review_request_intent", review_request=intent,
                next_check_at=now + 300)
    guard_review_request(store, name, state, read)
    publisher.call(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}/requested_reviewers", "POST",
                   {"reviewers": ["copilot-pull-request-reviewer[bot]"]})
    return confirm_request(store, name, state, read, intent, int(time.time()))


def watch_review(store, name, state, read, now):
    maximum = pipeline_budget(state)
    intent = state["review_request"]
    review = fresh_collection(read, state["request"], intent["baseline_review_ids"],
                              intent["recorded_at"], state["expected_sha"], now)
    if review["decision"].startswith("waiting"):
        return cas(store, name, state, stage="waiting_review", fresh_review=review,
                   next_check_at=now + 300)
    if review["decision"] == "unknown":
        return cas(store, name, state, stage="blocked", reason="unknown_fresh_review_body",
                   fresh_review=review)
    ci = exact_ci(read, state["request"]["repo"], state["expected_sha"], state["request"]["publication"]["required_checks"])
    guard(store, name, state, read, now)
    if ci["decision"] == "pending":
        return cas(store, name, state, stage="waiting_ci", fresh_review=review, ci=ci,
                   next_check_at=now + 300)
    if ci["decision"] != "passed":
        return cas(store, name, state, stage="blocked", reason="target_ci_" + ci["decision"],
                   fresh_review=review, ci=ci)
    if review["decision"] == "clean":
        return cas(store, name, state, stage="clean", reason="fresh_review_and_exact_target_CI",
                   fresh_review=review, ci=ci)
    require(review["decision"] == "findings", "Unknown fresh-review decision")
    if state["iteration"] >= maximum:
        return cas(store, name, state, stage="exhausted", reason="remaining_findings_pipeline_budget",
                   fresh_review=review, ci=ci)
    request = freeze(read, state["request"]["pr"], execution_revision(state), now, state["request"]["repo"])
    inherit_pin(request, state)
    fingerprint = finding_fingerprint(request["findings"])
    inline_ids = inline_fingerprints(request["findings"])
    missed_ids = missed_fingerprints(request["findings"])
    seen_inline = set(state.get("seen_inline_findings", []))
    seen_missed = set(state.get("seen_missed_findings",
                               missed_fingerprints(state["request"]["findings"])))
    if (fingerprint in state["seen_findings"]
            or (inline_ids and set(inline_ids) <= seen_inline
                and not set(missed_ids) - seen_missed)):
        return cas(store, name, state, stage="blocked", reason="repeated_findings",
                   fresh_review=review, ci=ci)
    publication = dict(state["request"]["publication"], generation=state["generation"] + 1)
    request.update(mode="publish", budgets=state["request"]["budgets"].copy(), publication=publication,
                   deadline=state["request"]["deadline"])
    if state["request"].get("launch_run"):
        request["launch_run"] = state["request"]["launch_run"]
    value = checkpoint(request)
    value.update(stage="ready", generation=state["generation"] + 1,
                 phase=state.get("phase", publication["phase"]),
                 iteration=state["iteration"], effects=[],
                 publications=state["publications"], seen_findings=state["seen_findings"] + [fingerprint])
    value["seen_inline_findings"] = state.get("seen_inline_findings", []) + inline_ids
    value["seen_missed_findings"] = sorted(seen_missed | set(missed_ids))
    if staged_source(request):
        value["stage"] = "source_pending"
    def next_iteration(current_state):
        require(current_state == state, "Cancelled or replaced next-iteration freeze")
        return value
    return store.update(name, next_iteration)


def watch_self(store, name, state, read, now):
    maximum = pipeline_budget(state)
    request = state["request"]
    result = state["report"]["dispositions"]
    candidate = state["report"]["candidate"]
    candidate_outcome(result, request, candidate)
    intent = state["publication_intent"]
    require(intent["status"] == "confirmed" and intent["candidate"] == candidate
            and intent["acceptance"]["request_digest"] == digest(request),
            "Self-review requires independently accepted candidate evidence")
    if candidate["changed"]:
        require(state["stage"] == "published", "Changed pass cannot declare clean")
        if state["iteration"] >= maximum:
            return cas(store, name, state, stage="exhausted",
                       reason="self_review_requires_later_clean_pass")
        guard(store, name, state, read, now)
        fresh = freeze(read, request["pr"], execution_revision(state), now, request["repo"],
                       request["authorized_actor_id"], loop_kind="self_review")
        inherit_pin(fresh, state)
        require(fresh["base_ref"] == request["base_ref"]
                and fresh["frozen_sha"] == state["expected_sha"],
                "Self-review head/base branch changed before next pass")
        publication = dict(request["publication"], generation=state["generation"] + 1)
        fresh.update(mode="publish", budgets=request["budgets"].copy(),
                     publication=publication, deadline=request["deadline"])
        if request.get("launch_run"):
            fresh["launch_run"] = request["launch_run"]
        value = checkpoint(fresh)
        value.update(stage="source_pending" if staged_source(fresh) else "ready", generation=state["generation"] + 1,
                     phase=state.get("phase", publication["phase"]), iteration=state["iteration"],
                     effects=[], publications=state["publications"],
                     seen_findings=[], seen_inline_findings=[])
        def next_iteration(current_state):
            require(current_state == state, "Cancelled or replaced self-review freeze")
            return value
        return store.update(name, next_iteration)
    require(result["outcome"] == "clean", "No-change is not self-review clearance")
    ci = exact_ci(read, request["repo"], state["expected_sha"], request["publication"]["required_checks"])
    guard(store, name, state, read, now)
    if ci["decision"] == "pending":
        return cas(store, name, state, stage="waiting_ci", ci=ci, next_check_at=now + 300)
    if ci["decision"] != "passed":
        return cas(store, name, state, stage="blocked", reason="target_ci_" + ci["decision"], ci=ci)
    return cas(store, name, state, stage="clean", reason="explicit_self_review_and_exact_target_CI", ci=ci)


def watch_single(store, name, state, read, now):
    request = state["request"]
    ci = exact_ci(read, request["repo"], state["expected_sha"], request["publication"]["required_checks"])
    guard(store, name, state, read, now)
    completion = {"outcome": state["report"]["dispositions"]["outcome"],
                  "publication": "pushed" if state["report"]["candidate"]["changed"] else "no_change"}
    if ci["decision"] == "pending":
        return cas(store, name, state, stage="waiting_ci", ci=ci,
                   task_completion=completion, next_check_at=now + 300)
    return cas(store, name, state, stage="complete", reason="single_pass_complete_CI_" + ci["decision"],
               task_completion=completion, ci=ci)


def watch_ci_fix(store, name, state, read, now):
    from loop.ci import collect
    request = state["request"]
    current_request = dict(request, frozen_sha=state["expected_sha"])
    ci = collect(read, current_request, request["publication"]["required_checks"])
    guard(store, name, state, read, now)
    if ci["decision"] == "pending":
        return cas(store, name, state, stage="waiting_ci", ci=ci, next_check_at=now + 300,
                   ci_reobserve=True)
    published_at = max((p["confirmed_at"] for p in state["publications"]
                        if p.get("effect") == "push" and p["sha"] == state["expected_sha"]), default=0)
    if ci["decision"] == "missing" and published_at and now < published_at + PUSH_PROPAGATION_SECONDS:
        return cas(store, name, state, stage="waiting_ci", ci=ci, next_check_at=now + 300,
                   reason="fresh_CI_registration_pending", ci_reobserve=True)
    if ci["decision"] == "passed":
        return cas(store, name, state, stage="complete", reason="exact_target_CI_passed",
                   ci=ci, task_completion={"outcome": "CI_passed"})
    if ci["decision"] != "failed":
        return cas(store, name, state, stage="blocked", reason="target_ci_" + ci["decision"], ci=ci)
    report = state.get("report")
    if (report and state["stage"] == "published"
            and report["dispositions"]["outcome"] == "no_change"):
        require(ci == request["ci_evidence"], "Unrelated-failure evidence changed")
        warnings = [item for item in report["dispositions"]["diagnoses"] if item["decision"] == "unrelated"]
        require(warnings, "Failed CI cannot finish without evidenced warnings")
        return cas(store, name, state, stage="complete", reason="CI_unrelated_failures_remain",
                   ci=ci, ci_warnings=warnings, task_completion={"outcome": "warnings_not_CI_clearance"})
    if state["iteration"] >= pipeline_budget(state):
        return cas(store, name, state, stage="exhausted", reason="CI_failure_pipeline_budget", ci=ci)
    if (state["iteration"] == 0 and ci == request["ci_evidence"]):
        return cas(store, name, state, stage="source_pending" if staged_source(request) else "ready",
                   ci=ci, next_check_at=now)
    fresh = freeze(read, request["pr"], execution_revision(state), now, request["repo"],
                   request["authorized_actor_id"], loop_kind="ci_fix")
    inherit_pin(fresh, state)
    require(fresh["base_ref"] == request["base_ref"]
            and fresh["frozen_sha"] == state["expected_sha"], "CI repair source/base branch drift")
    fresh["ci_evidence"] = collect(read, fresh, request["publication"]["required_checks"])
    require(fresh["ci_evidence"]["decision"] == "failed", "CI changed before diagnosis freeze")
    publication = dict(request["publication"], generation=state["generation"] + 1)
    fresh.update(mode="publish", publication=publication, budgets=request["budgets"].copy(),
                 deadline=request["deadline"])
    if request.get("launch_run"):
        fresh["launch_run"] = request["launch_run"]
    value = checkpoint(fresh)
    value.update(stage="source_pending" if staged_source(fresh) else "ready", generation=state["generation"] + 1,
                 phase=state["phase"], iteration=state["iteration"], publications=state["publications"],
                 effects=[], ci=ci, ci_reruns=state.get("ci_reruns", []),
                 ci_warnings=state.get("ci_warnings", []))
    def next_pass(current_state):
        require(current_state == state, "Cancelled or replaced CI freeze")
        return value
    return store.update(name, next_pass)


def advance(store, name, state, central, read, publisher, now):
    personal(state["request"])
    pipeline_budget(state)
    require(state["stage"] in STAGES, "No live transition for this checkpoint")
    if now >= state["request"]["deadline"]:
        return cas(store, name, state, stage="exhausted", reason="elapsed_deadline")
    if state["stage"] == "publication_intent":
        require(current(store, name) == state, "Cancelled or replaced publication reconciliation")
    else:
        guard(store, name, state, read, now)
    stage = state["stage"]
    if stage == "publish_pending":
        return publish(store, name, state, central, read, publisher, now)
    if stage == "publication_intent":
        return confirm_push(store, name, state, read, now)
    if stage == "task_effect_intent":
        from loop.task_effects import confirm_task
        return confirm_task(store, name, state, read, now)
    if stage == "published":
        if loop_kind(state["request"]) == "self_review":
            return watch_self(store, name, state, read, now)
        if loop_kind(state["request"]) == "ci_fix":
            return watch_ci_fix(store, name, state, read, now)
        if loop_kind(state["request"]) != "copilot_review":
            return watch_single(store, name, state, read, now)
        from loop.effects import initialize
        return cas(store, name, state, stage="thread_effects", effects=initialize(state),
                   next_check_at=now)
    if stage == "thread_effects":
        from loop.effects import advance as advance_effects
        return advance_effects(store, name, state, central, read, publisher, now)
    if stage == "threads_settled":
        return request_review(store, name, state, read, publisher, now)
    if stage == "review_request_intent":
        return confirm_request(store, name, state, read, state["review_request"], now)
    if stage in {"waiting_review", "waiting_ci"}:
        if loop_kind(state["request"]) == "ci_fix":
            return watch_ci_fix(store, name, state, read, now)
        if loop_kind(state["request"]) not in {"copilot_review", "self_review"}:
            require(stage == "waiting_ci", "Single-pass tasks cannot wait for review")
            return watch_single(store, name, state, read, now)
        if loop_kind(state["request"]) == "self_review":
            require(stage == "waiting_ci", "Self-review cannot wait for external review")
            return watch_self(store, name, state, read, now)
        return watch_review(store, name, state, read, now)
    raise Rejected("No live transition for this checkpoint")


def main():
    started = time.monotonic()
    central = API()
    store = State(central)
    from loop.cli import get_state
    name, state = get_state(store, int(os.environ["PR"]), os.environ["REQUEST_ID"],
                           os.environ["TARGET_REPO"])
    require(state["request"]["request_id"] == os.environ["REQUEST_ID"]
            and state["generation"] == int(os.environ["GENERATION"])
            and os.environ["GITHUB_SHA"] == execution_revision(state)
            and git(["rev-parse", "HEAD"], Path.cwd()).decode().strip() == os.environ["GITHUB_SHA"]
            and os.environ["GITHUB_RUN_ATTEMPT"] == "1"
            and state["stage"] in STAGES
            and state["stage"] == os.environ["EXPECTED_STAGE"], "Unbound/stale/rerun live job")
    supported_checkpoint(state)
    from loop.control import owned
    owned(state)
    try:
        outcome = (state.get("report") or {}).get("dispositions", {}).get("outcome")
        secret = publisher_secret(effect_repository(state["request"], outcome))
        require(secret, "human_gate_publisher_secret_for_head_owner")
        require(os.environ.get("PUBLISHER_SECRET_NAME") == secret,
                "Publisher secret selection differs from the frozen head owner")
        token = os.environ.get("PUBLISHER_TOKEN", "")
        require(token, "human_gate_" + secret)
        target_secret = publisher_secret(state["request"]["repo"])
        require(target_secret, "human_gate_publisher_secret_for_target_owner")
        target_credentials = {}
        if target_secret != secret:
            require(os.environ.get("TARGET_PUBLISHER_SECRET_NAME") == target_secret,
                    "Upstream publisher secret selection differs from the frozen target owner")
            target_token = os.environ.get("TARGET_PUBLISHER_TOKEN", "")
            require(target_token, "human_gate_" + target_secret)
            target_credentials["target_token"] = target_token
        publisher = PublisherAPI(token, state["request"],
                                 state["request"]["publication"]["auth_mode"],
                                 source_write=not (loop_kind(state["request"]) == "ci_fix"
                                                   and outcome == "rerun"),
                                 **target_credentials)
        read = publisher
        if state["stage"] == "publication_intent":
            live = read.call(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}")
            allowed = {state["request"]["frozen_sha"],
                       state["publication_intent"]["candidate"]["commit"]}
            require(live["head"]["sha"] in allowed, "Stale interrupted publication target")
            checked_sha = live["head"]["sha"]
        else:
            checked_sha = state["expected_sha"]
        publisher.identity(dict(state["request"], frozen_sha=checked_sha))
        # Each mutation still has its own durable intent. This is not a side-effect retry loop.
        for _ in range(310):
            before = state
            state = advance(store, name, state, central, read, publisher, int(time.time()))
            if (state["stage"] not in {"published", "thread_effects", "threads_settled"}
                    or state["next_check_at"] > int(time.time()) or state == before
                    or time.monotonic() - started >= 300):
                break
    except (Rejected, ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        latest = current(store, name)
        if (latest["request"]["request_id"] == state["request"]["request_id"]
                and latest["generation"] == state["generation"]
                and latest["stage"] not in TERMINAL
                and (latest == state or owns_pending(latest))):
            # An interrupted side effect remains reconcile-only, never ready for a second POST.
            from loop.effects import pending
            uncertain = latest["stage"] in {"publication_intent", "review_request_intent", "task_effect_intent"} or pending(latest)
            denied = isinstance(error, APIError) and error.status in {401, 403, 404, 422}
            if denied:
                uncertain = False
            expired = int(time.time()) >= latest["request"]["deadline"]
            cas(store, name, latest,
                stage="exhausted" if expired else latest["stage"] if uncertain else "blocked",
                reason=("elapsed_deadline" if expired else
                        "live_operation_requires_reconciliation" if uncertain else
                        "publisher_permission_rejected" if denied else
                        "live_operation_rejected"),
                error=type(error).__name__ + ": " + str(error)[:1000],
                next_check_at=int(time.time()) + 300)
        raise
    from loop.cli import summary
    summary(state)


if __name__ == "__main__":
    main()
