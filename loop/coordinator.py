"""Durable launch intent, bounded run reconciliation, and cancellation."""

import time
import uuid

from loop.api import APIError
from loop.revisions import check_pin, workflow_ref
from loop.policy import (CENTRAL,
                         REQUEST, SHA, TERMINAL, WORKER, WORKER_PATH,
                         Rejected, candidate_outcome, checkpoint_name, digest, loop_kind,
                         pipeline_budget, pipeline_limit, require, staged_source, supported_checkpoint)


def checkpoint(request):
    pipeline_limit(request)
    require(request["schema"] == 2, "Legacy requests are read-only")
    return {"schema": 2, "request": request, "stage": "ready",
            "phase": request.get("publication", {}).get("phase", uuid.uuid4().hex),
            "generation": 1, "iteration": 0, "expected_sha": request["frozen_sha"],
            "next_check_at": request["frozen_at"], "intent": None, "run": None,
            "artifacts": [], "report": None, "reason": None, "cancelled_at": None}


def quiescent(api, state, read=None):
    require(not state.get("capability") and not state.get("capability_probe")
            and "reviewable_retry" not in state["request"].get("publication", {})
            and not state["request"].get("publication", {}).get("reply_bot_threads"),
            "Retired publication capability evidence requires manual inspection")
    if state["request"].get("protocol") == "git-candidate-v1":
        pipeline_budget(state)
    require(state["stage"] in TERMINAL, "An active phase cannot be restarted")
    require(state["reason"] not in {"source_acquisition_uncertain_no_retry",
                                   "duplicate_run_identity"},
            "Uncertain effects require reconciliation, not a new phase")
    for key in ("publication_intent", "task_intent"):
        intent = state.get(key)
        require(not intent or intent.get("status") == "confirmed",
                "Uncertain effects require reconciliation, not a new phase")
    from loop.effects import pending
    require(not pending(state), "Uncertain thread effects require reconciliation, not a new phase")
    require(not state.get("effects") or state["request"].get("protocol") == "git-candidate-v1"
            or state["request"].get("protocol") == "reviewable-v1"
            and workflow_ref(state["request"]) != "main",
            "Retired effect-bearing checkpoints require manual inspection")
    executions_quiescent(api, state)
    confirmation = None
    review = state.get("review_request")
    if review and review.get("status") != "confirmed":
        require(read is not None and loop_kind(state["request"]) == "copilot_review"
                and review.get("sha") == state["expected_sha"],
                "Uncertain effects require reconciliation, not a new phase")
        from loop.live import observed_review_request
        confirmation = observed_review_request(read, state, review, allow_head_change=True)
        require(confirmation is not None,
                "Uncertain effects require reconciliation, not a new phase")
    return confirmation


def executions_quiescent(api, state):
    if state.get("intent"):
        require(api is not None, "Previous dispatch requires live quiescence evidence")
        runs = api.pages(f"repos/{CENTRAL}/actions/workflows/{WORKER}/runs?event=workflow_dispatch"
                         f"&created=%3E%3D{state['request']['frozen_at_iso']}", "workflow_runs")
        runs = [run for run in runs if run["display_title"] in {
            "Copilot worker " + state["request"]["request_id"],
            "Copilot shadow " + state["request"]["request_id"]}]
        require(len(runs) == 1, "Previous worker dispatch is uncertain or duplicated")
        run_binding(runs[0], state["request"])
        require(runs[0]["status"] == "completed"
                and (state.get("run") is None or state["run"]["id"] == runs[0]["id"]),
                "Previous worker remains active or unbound")
    else:
        require(state.get("run") is None, "Previous worker has no dispatch identity")
    if state.get("coordinator_run"):
        from loop.control import busy
        require(api is not None and not busy(api, state), "Previous coordinator is still active")
    launch_run = state["request"].get("launch_run")
    if launch_run:
        require(api is not None, "Previous launch requires live quiescence evidence")
        run = api.call(f"repos/{CENTRAL}/actions/runs/{launch_run['id']}")
        require(run["id"] == launch_run["id"] and run["run_attempt"] == launch_run["attempt"] == 1
                and run["repository"]["full_name"] == run["head_repository"]["full_name"] == CENTRAL
                and run["path"].split("@")[0] == ".github/workflows/coordinator.yml"
                and run["head_sha"] == state["request"]["workflow_revision"]
                and run["event"] == "workflow_dispatch" and run["status"] == "completed",
                "Previous launch is active or unbound")
    if state.get("source_claim"):
        require(api is not None, "Previous source acquisition requires live quiescence evidence")
        claim = state["source_claim"]
        run = api.call(f"repos/{CENTRAL}/actions/runs/{claim['run_id']}")
        require(run["id"] == claim["run_id"] and run["run_attempt"] == claim["run_attempt"] == 1
                and run["repository"]["full_name"] == run["head_repository"]["full_name"] == CENTRAL
                and run["path"].split("@")[0] == ".github/workflows/coordinator.yml"
                and run["head_sha"] == state["request"]["workflow_revision"]
                and run["status"] == "completed", "Previous source acquisition remains active or unbound")






def due(state, now):
    return (state.get("schema") == 2 and state["request"].get("schema") == 2
            and (state["request"].get("protocol") == "git-candidate-v1"
                 or state["request"].get("protocol") == "reviewable-v1"
                 and "workflow_ref" in state["request"])
            and state["stage"] not in TERMINAL
            and state["next_check_at"] <= now)


def dispatch(store, name, api, now):
    claim = uuid.uuid4().hex

    def operation(state):
        require(state is not None, "Unknown checkpoint")
        require(state["schema"] == 2 and state["request"]["schema"] == 2,
                "Legacy checkpoints are read-only")
        supported_checkpoint(state)
        if state["stage"] != "ready":
            return state
        require(state["request"].get("freeze_status") != "not_frozen",
                "Unfrozen access gates cannot dispatch")
        maximum = pipeline_budget(state)
        if state["iteration"] >= maximum:
            state.update(stage="exhausted", reason="budget_exhausted")
            return state
        ref = workflow_ref(state["request"])
        if ref == "main":
            main = api.call(f"repos/{CENTRAL}/git/ref/heads/main")["object"]["sha"]
            if (state["request"].get("mode") == "publish"
                    and main != state["request"]["workflow_revision"]):
                state.update(stage="blocked", reason="trusted_revision_changed_before_dispatch")
                return state
            require(main == state["request"]["workflow_revision"], "Main changed before dispatch")
        else:
            try:
                check_pin(api, ref, state["request"]["workflow_revision"])
            except (APIError, Rejected) as error:
                if isinstance(error, APIError) and error.status != 404:
                    raise
                state.update(stage="blocked", reason="pinned_workflow_ref_unavailable",
                             error=type(error).__name__ + ": " + str(error)[:1000])
                return state
        require(not staged_source(state["request"]) or state.get("source"),
                "Review source artifact is not durably bound")
        if state["request"].get("mode") == "publish" and loop_kind(state["request"]) == "copilot_review":
            require(state["request"]["findings"],
                    "A publication worker requires existing frozen Copilot findings")
        state.update(stage="dispatch_intent", iteration=state["iteration"] + 1,
                     next_check_at=now + 300,
                     intent={"id": state["request"]["request_id"], "claim": claim,
                             "recorded_at": now, "dispatch_status": "uncertain"})
        return state

    state = store.update(name, operation)
    # Only the writer of a new durable intent may attempt the side effect.
    if (state["stage"] != "dispatch_intent" or state["intent"]["claim"] != claim
            or state["intent"]["recorded_at"] != now):
        return state
    # Never retry this POST. A network interruption leaves durable uncertain intent.
    api.call(f"repos/{CENTRAL}/actions/workflows/{WORKER}/dispatches", "POST", {
        "ref": workflow_ref(state["request"]),
        "inputs": {"request_id": state["request"]["request_id"], "pr": str(state["request"]["pr"]),
                   "repo": state["request"]["repo"]},
    })

    def dispatched(current):
        if current["stage"] == "dispatch_intent" and current["intent"] == state["intent"]:
            current["stage"] = "dispatched"
            current["intent"]["dispatch_status"] = "acknowledged"
        return current

    return store.update(name, dispatched)


def matching_runs(api, request):
    runs = api.pages(f"repos/{CENTRAL}/actions/workflows/{WORKER}/runs?event=workflow_dispatch"
                     f"&created=%3E%3D{request['frozen_at_iso']}", "workflow_runs")
    return [r for r in runs if r["display_title"] == "Copilot worker " + request["request_id"]]


def run_binding(run, request):
    require(run["repository"]["full_name"] == CENTRAL
            and run["head_repository"]["full_name"] == CENTRAL
            and run["event"] == "workflow_dispatch"
            and run["path"].split("@")[0] == WORKER_PATH
            and run["head_sha"] == request["workflow_revision"]
            and ("workflow_ref" not in request or run.get("head_branch") == workflow_ref(request))
            and run["run_attempt"] == 1, "Wrong workflow/run provenance or rerun")


def reconcile(store, name, api, now):
    _, _, entries = store.snapshot()
    state = entries[name]
    if not due(state, now):
        return state
    pipeline_budget(state)
    request = state["request"]
    runs = matching_runs(api, request) if state["intent"] else []

    def operation(current):
        if current["generation"] != state["generation"] or current["stage"] in TERMINAL:
            return current
        if len(runs) > 1:
            current.update(stage="blocked", reason="duplicate_run_identity",
                           report={"run_ids": [r["id"] for r in runs]})
        elif current["stage"] == "source_pending":
            if now >= request["frozen_at"] + 900:
                current.update(stage="blocked", reason="source_acquisition_uncertain_no_retry")
                if current.get("source_claim"):
                    current["artifacts"] = [
                        {key: item.get(key) for key in
                         ("id", "name", "size_in_bytes", "expired", "digest")}
                        for item in api.pages(
                            f"repos/{CENTRAL}/actions/runs/"
                            f"{current['source_claim']['run_id']}/artifacts", "artifacts")
                    ]
        elif len(runs) == 1:
            run = runs[0]
            try:
                run_binding(run, request)
            except Rejected as error:
                current.update(stage="blocked", reason=str(error),
                               report={"run_ids": [run["id"]]})
                return current
            binding = {"id": run["id"], "attempt": run["run_attempt"],
                       "url": run["html_url"], "conclusion": run["conclusion"]}
            require(current["run"] is None or current["run"]["id"] == run["id"],
                    "Run identity changed")
            current["run"] = binding
            if run["status"] == "completed":
                metadata = api.pages(
                    f"repos/{CENTRAL}/actions/runs/{run['id']}/artifacts", "artifacts")
                current["artifacts"] = [
                    {key: item.get(key) for key in
                     ("id", "name", "size_in_bytes", "expired", "digest")} for item in metadata
                ]
                if run["conclusion"] == "cancelled":
                    current.update(stage="cancelled", reason="worker_cancelled", cancelled_at=now)
                elif run["conclusion"] != "success":
                    current.update(stage="failed", reason="worker_" + str(run["conclusion"]))
                else:
                    jobs = api.pages(
                        f"repos/{CENTRAL}/actions/runs/{run['id']}/attempts/1/jobs", "jobs")
                    agent = [job for job in jobs if job["name"] == "agent"]
                    if len(agent) == 1 and agent[0]["conclusion"] == "skipped":
                        current.update(stage="failed", reason="worker_agent_skipped",
                                       report={"run_ids": [run["id"]], "agent_started": False})
                    else:
                        current["stage"] = "verify_pending"
            else:
                current["stage"] = "running"
        elif current["intent"] and now > current["intent"]["recorded_at"] + 900:
            # Actions listing can be delayed, unavailable, or incomplete. Absence is not proof
            # a dispatch never happened; an operator must inspect before creating a new request.
            current.update(stage="blocked", reason="dispatch_uncertain_no_blind_retry")
        current["next_check_at"] = now if current["stage"] == "verify_pending" else now + 300
        return current

    return store.update(name, operation)


def cancel(store, name, previous_id, previous_generation, now=None):
    now = int(time.time()) if now is None else now

    def operation(state):
        require(state is not None, "Unknown checkpoint")
        require(state["schema"] == 2, "Legacy checkpoints are read-only")
        if not (state["request"].get("protocol") == "reviewable-v1"
                and workflow_ref(state["request"]) != "main"):
            supported_checkpoint(state)
        require(REQUEST.fullmatch(previous_id or "") and type(previous_generation) is int
                and previous_generation > 0
                and state["request"]["request_id"] == previous_id
                and state["generation"] == previous_generation,
                "Cancellation requires the exact observed request and generation")
        if state["stage"] == "cancelled":
            return state
        require(state["stage"] not in TERMINAL, "Completed terminal evidence cannot be cancelled")
        cancellation = {"request_id": previous_id, "observed_generation": previous_generation,
                       "previous_stage": state["stage"], "previous_reason": state["reason"]}
        state.update(stage="cancelled", reason="durable_user_cancellation", cancelled_at=now,
                     generation=state["generation"] + 1, next_check_at=now,
                     cancellation=cancellation)
        return state

    return store.update(name, operation)


def record_result(store, name, expected, report, artifacts, now=None):
    now = int(time.time()) if now is None else now

    def operation(state):
        require(state["schema"] == state["request"]["schema"] == 2,
                "Legacy results are read-only")
        require(state["generation"] == expected["generation"] and state["stage"] == "verify_pending"
                and state["run"] == expected["run"]
                and state["request"] == expected["request"], "Cancelled or stale verification result")
        pipeline_budget(state)
        require(report.get("schema") == 2 and report.get("verification") == "verified"
                and report["publication_eligible"] is False
                and not {"validation", "validation_claim", "objective_validation"} & report.keys()
                and report["request_digest"] == digest(state["request"])
                and report["run_id"] == state["run"]["id"]
                and report["run_attempt"] == state["run"]["attempt"] == 1,
                "Wrong structural verification identity")
        candidate_outcome(report["dispositions"], state["request"], report["candidate"])
        if report["dispositions"]["outcome"] == "blocked":
            state.update(stage="blocked", report=report, artifacts=artifacts, reason="worker_blocked")
        elif state["request"].get("mode") == "publish":
            state.update(stage="publish_pending", report=report, artifacts=artifacts,
                         verification_run=report["verification_run"], next_check_at=now,
                         reason="pending_fresh_trusted_personal_acceptance")
        else:
            raise Rejected("Only owner-authorized publication results can advance")
        return state
    return store.update(name, operation)
