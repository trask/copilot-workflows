"""Trusted metadata, pending-review and failed-jobs effects."""

import time
import uuid

from loop.policy import check_target, digest, loop_kind, require, source_effect
from loop.publication import evidence


def payload(request, result):
    kind = loop_kind(request)
    if kind == "pr_description" or source_effect(request) and "proposal" in result:
        return result["proposal"]
    if kind == "pr_review":
        return {"commit_id": request["frozen_sha"], "comments": result["comments"]}
    return None


def pending_reviews(api, request):
    return [r for r in api.pages(f"repos/{request['repo']}/pulls/{request['pr']}/reviews")
            if r["user"]["id"] == request["authorized_actor_id"] and r["state"] == "PENDING"]


def review_matches(api, request, review, expected):
    if (review["user"]["id"] != request["authorized_actor_id"] or review["state"] != "PENDING"
            or review.get("submitted_at") is not None or review["commit_id"] != request["frozen_sha"]):
        return False
    actual = api.pages(f"repos/{request['repo']}/pulls/{request['pr']}/reviews/{review['id']}/comments")
    fields = ("path", "line", "side", "body")
    return (len(actual) == len(expected) and
            sorted((tuple(c.get(k) for k in fields) for c in actual)) ==
            sorted(tuple(c[k] for k in fields) for c in expected))


def guard_task(store, name, state, read, publisher):
    from loop.live import guard
    guard(store, name, state, read, int(time.time()))
    publisher.identity(dict(state["request"], frozen_sha=state["expected_sha"]))
    if not source_effect(state["request"]) or state["report"]["dispositions"]["outcome"] == "rerun":
        from loop.recommendations import check_diff
        check_diff(read, state["request"], state["report"]["dispositions"].get("input_identity"))


def publish_task(store, name, state, central, read, publisher, now, verified=None):
    import tempfile
    from loop.live import cas, owner
    request = state["request"]
    kind = loop_kind(request)
    result = state["report"]["dispositions"]
    source_metadata = source_effect(request) and "proposal" in result
    metadata = source_metadata or kind == "pr_description"
    if source_metadata:
        from loop.publication import acceptance
        accepted = acceptance(state)
        require(state["stage"] == "published"
                and state["publication_intent"]["status"] == "confirmed"
                and state["publication_intent"]["acceptance"] == accepted
                and state["expected_sha"] == accepted["candidate_commit"],
                "Description correction requires confirmed candidate publication")
    elif verified is None:
        with tempfile.TemporaryDirectory(prefix="trusted-task-") as directory:
            accepted, _ = evidence(central, state, directory)
    else:
        accepted = verified[0]
    guard_task(store, name, state, read, publisher)
    if metadata:
        live = check_target(read, dict(request, frozen_sha=state["expected_sha"]))
        require({"title": live["title"], "body": live["body"] or ""} == request["metadata"],
                "PR metadata changed after freeze")
    if kind == "ci_fix" and not source_metadata:
        from loop.ci import same_attempts
        same_attempts(read, request)
    if result["outcome"] == "no_change" and not source_metadata:
        return cas(store, name, state, stage="complete", reason="verified_no_change",
                   task_completion={"outcome": "no_change", "acceptance": accepted})
    body = payload(request, result)
    intent = {"claim": uuid.uuid4().hex, "kind": "metadata" if metadata else
              {"pr_review": "pending_review", "ci_fix": "rerun"}[kind],
              "request_digest": digest(request), "generation": state["generation"],
              "target": [request["repo"], request["pr"]], "acceptance": accepted,
              "payload": body, "recorded_at": now, "owner": owner(), "status": "uncertain"}
    if source_metadata:
        intent["head"] = state["expected_sha"]
    root = f"repos/{request['repo']}/pulls/{request['pr']}"
    if metadata:
        endpoint, method = root, "PATCH"
    elif kind == "pr_review":
        existing = pending_reviews(publisher, request)
        require(not existing, "Existing viewer-owned pending review is preserved")
        intent["baseline_review_ids"] = [r["id"] for r in publisher.pages(root + "/reviews")]
        endpoint, method = root + "/reviews", "POST"
    else:
        run_id = result["rerun_run"]
        run = read.call(f"repos/{request['repo']}/actions/runs/{run_id}")
        require(run["run_attempt"] == 1 and run["status"] == "completed"
                and run["conclusion"] in {"failure", "cancelled", "timed_out", "action_required"}
                and not any(e["run_id"] == run_id for e in state.get("ci_reruns", [])),
                "CI rerun allowance already consumed or run is not a settled failure")
        intent.update(run_id=run_id, attempt=1)
        endpoint, method = f"repos/{request['repo']}/actions/runs/{run_id}/rerun-failed-jobs", "POST"
    state = cas(store, name, state, stage="task_effect_intent", task_intent=intent,
                next_check_at=now + 300)
    guard_task(store, name, state, read, publisher)
    if metadata:
        live = check_target(read, dict(request, frozen_sha=state["expected_sha"]))
        require({"title": live["title"], "body": live["body"] or ""} == request["metadata"],
                "PR metadata changed before PATCH")
    elif kind == "pr_review":
        require(not pending_reviews(publisher, request), "Pending review appeared before POST")
    else:
        from loop.ci import same_attempts
        same_attempts(read, request)
    publisher.bind_effect(intent)
    try:
        response = publisher.call(endpoint, method, body)
    finally:
        publisher.bind_effect(None)
    if kind == "pr_review":
        require(response["state"] == "PENDING" and response.get("submitted_at") is None,
                "Review was not created as pending")
        intent["review_id"] = response["id"]
    state = cas(store, name, state, task_intent=dict(intent, status="acknowledged"),
                next_check_at=now)
    return confirm_task(store, name, state, read, now)


def confirm_task(store, name, state, read, now):
    from loop.live import cas, guard
    request, intent = state["request"], state["task_intent"]
    require(intent["request_digest"] == digest(request)
            and intent["generation"] == state["generation"]
            and intent["target"] == [request["repo"], request["pr"]]
            and intent["acceptance"]["verification_sha256"] == digest(state["report"]),
            "Stale or unbound task reconciliation")
    guard(store, name, state, read, now)
    kind = loop_kind(request)
    if source_effect(request) and intent["kind"] == "metadata":
        require(intent["head"] == state["expected_sha"]
                and intent["payload"] == state["report"]["dispositions"]["proposal"],
                "Description intent changed")
    if intent["kind"] == "rerun":
        run = read.call(f"repos/{request['repo']}/actions/runs/{intent['run_id']}")
        require(run["head_sha"] == request["frozen_sha"] and run["id"] == intent["run_id"],
                "Rerun source identity changed")
        if run["run_attempt"] == intent["attempt"] + 1:
            effect = dict(intent, status="confirmed", resulting_attempt=run["run_attempt"])
            return cas(store, name, state, stage="waiting_ci", reason="rerun_confirmed_not_CI_clearance",
                       task_intent=effect, ci_reruns=state.get("ci_reruns", []) + [effect],
                       ci_reobserve=True, next_check_at=now + 300)
        require(run["run_attempt"] == intent["attempt"], "Unexpected additional manual/automated retry")
    elif intent["kind"] == "metadata":
        if kind == "pr_description":
            from loop.recommendations import check_diff
            check_diff(read, request, state["report"]["dispositions"].get("input_identity"))
        live = check_target(read, dict(request, frozen_sha=state["expected_sha"]))
        if {"title": live["title"], "body": live["body"] or ""} == intent["payload"]:
            if source_effect(request):
                return cas(store, name, state, stage="published",
                           task_intent=dict(intent, status="confirmed"), next_check_at=now)
            return cas(store, name, state, stage="complete", reason="exact_metadata_confirmed",
                       task_intent=dict(intent, status="confirmed"),
                       task_completion={"outcome": "metadata_updated", "proposal": intent["payload"]})
        require({"title": live["title"], "body": live["body"] or ""} == request["metadata"],
                "Metadata drift during effect reconciliation")
    else:
        from loop.recommendations import check_diff
        check_diff(read, request, state["report"]["dispositions"].get("input_identity"))
        reviews = pending_reviews(read, request)
        matches = [r for r in reviews if r["id"] not in intent["baseline_review_ids"]
                   and ("review_id" not in intent or r["id"] == intent["review_id"])
                   and review_matches(read, request, r, intent["payload"]["comments"])]
        require(len(matches) <= 1, "Ambiguous pending review effect")
        if len(matches) == 1:
            return cas(store, name, state, stage="complete", reason="viewer_pending_review_confirmed",
                       task_intent=dict(intent, status="confirmed", review_id=matches[0]["id"]),
                       task_completion={"outcome": "pending_review", "review_id": matches[0]["id"],
                                        "comments": intent["payload"]["comments"]})
        require(not reviews, "Pending review changed during reconciliation")
    if now >= intent["recorded_at"] + 900:
        return cas(store, name, state, stage="blocked", reason="task_effect_uncertain_no_retry")
    return cas(store, name, state, next_check_at=now + 300, reason="task_effect_requires_reconciliation")
