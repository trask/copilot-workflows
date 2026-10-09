"""One bounded Actions waiter for all explicitly authorized PR checkpoints."""

import copy
import os
import sys
import time
from pathlib import Path

from loop.api import API, APIError, DeadlineReached
from loop.cli import summary
from loop.control import busy
from loop.live import cas, execution_revision, observed_review_request
from loop.policy import (CENTRAL, DEFAULTS, TERMINAL, Rejected, bot, check_target, loop_kind, pipeline_budget,
                         require, supported_checkpoint, timestamp)
from loop.reviews import exact_ci
from loop.source import target_api
from loop.state import State
from loop.verify import git
from loop.revisions import check_pin, execution_ref

POLL_SECONDS = 60
PR_POLL_SECONDS = 300
DURATION = 55 * 60


def ready(api, state, now):
    stage = state["stage"]
    if stage in {"verify_pending", "publish_pending", "publication_intent", "published", "ready",
                 "thread_effects", "threads_settled", "task_effect_intent"}:
        return True
    if stage == "source_pending":
        return not state.get("source_claim") or now >= state["request"]["frozen_at"] + 900
    if stage in {"dispatch_intent", "dispatched", "running"}:
        if state["run"]:
            run = api.call(f"repos/{CENTRAL}/actions/runs/{state['run']['id']}")
            return run["status"] == "completed"
        return True
    read = target_api(api, state["request"]["repo"], state["request"]["head_repo"])
    read.deadline = api.deadline
    if stage == "review_request_intent":
        return (observed_review_request(read, state, state["review_request"]) is not None
                or now >= state["review_request"]["recorded_at"] + 900)
    require(stage in {"waiting_review", "waiting_ci"}, "Unsupported waiter stage")
    check_target(read, dict(state["request"], frozen_sha=state["expected_sha"]))
    if stage == "waiting_ci":
        if loop_kind(state["request"]) == "ci_fix":
            from loop.ci import collect
            return collect(read, dict(state["request"], frozen_sha=state["expected_sha"]),
                           state["request"]["publication"]["required_checks"])["decision"] != "pending"
        return exact_ci(read, state["request"]["repo"], state["expected_sha"],
                        state["request"]["publication"]["required_checks"])["decision"] != "pending"
    intent = state["review_request"]
    reviews = read.pages(f"repos/{state['request']['repo']}/pulls/{state['request']['pr']}/reviews")
    fresh = [review for review in reviews if bot(review.get("user"))
             and review["id"] not in intent["baseline_review_ids"]
             and review.get("submitted_at") and review["commit_id"] == state["expected_sha"]
             and review["state"] in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}
             and timestamp(review["submitted_at"]) > intent["recorded_at"]]
    return bool(fresh) and now >= max(timestamp(review["submitted_at"]) for review in fresh
                                     ) + DEFAULTS["propagation_seconds"]


def wake(api, state):
    supported_checkpoint(state)
    request = state["request"]
    ref = execution_ref(state)
    if ref != "main":
        check_pin(api, ref, execution_revision(state))
    runs = api.pages(f"repos/{CENTRAL}/actions/workflows/coordinator.yml/runs"
                     f"?event=workflow_dispatch&created=%3E%3D{request['frozen_at_iso']}", "workflow_runs")
    title = "Review loop tick " + request["request_id"]
    if any(run["display_title"] == title and run["status"] != "completed" for run in runs):
        return
    # Repeated wake-ups are safe; the coordinator claims work before executing it.
    api.call(f"repos/{CENTRAL}/actions/workflows/coordinator.yml/dispatches", "POST", {
        "ref": ref,
        "inputs": {"operation": "tick", "target": request["repo"] + "#" + str(request["pr"]),
                   "previous_request": request["request_id"],
                   "previous_generation": str(state["generation"])},
    })


def poll(api, store, now, revision, checked):
    _, _, entries = store.snapshot()
    active = False
    for name, state in sorted(entries.items()):
        if (not name.startswith("pr-v2-") or state.get("schema") != 2
                or state["stage"] in TERMINAL):
            continue
        try:
            supported_checkpoint(state)
            pipeline_budget(state)
        except (Rejected, ValueError, KeyError, TypeError) as error:
            print(f"READ ONLY {name}: {type(error).__name__}: {error}", file=sys.stderr)
            continue
        try:
            if execution_ref(state) == "main" and revision != execution_revision(state):
                summary(cas(store, name, state, stage="blocked",
                            reason="trusted_revision_changed_before_waiter"))
                continue
            active = True
            previous = checked.get(name)
            if (previous is not None and previous[0] == state and now < previous[1]
                    or now < state["next_check_at"]):
                continue
            checked[name] = (copy.deepcopy(state), now + PR_POLL_SECONDS)
            if not busy(api, state) and ready(api, state, now):
                wake(api, state)
        except DeadlineReached:
            raise
        except (Rejected, ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
            print(f"WAIT FAILED {name}: {type(error).__name__}: {str(error)[:1000]}", file=sys.stderr)
            if isinstance(error, APIError) and error.rate_limited:
                checked.pop(name, None)
                raise
            transient = (isinstance(error, OSError)
                         or isinstance(error, APIError) and (
                             error.status == 429 or error.status >= 500))
            _, _, latest = store.snapshot()
            if not transient and latest.get(name) == state:
                summary(cas(store, name, state, stage="blocked", reason="waiter_operation_rejected",
                            error=type(error).__name__ + ": " + str(error)[:1000]))
    checked_keys = set(checked) - set(entries)
    for name in checked_keys:
        del checked[name]
    return active


def run(api, store, duration=DURATION):
    started = time.monotonic()
    api.deadline = started + duration
    checked = {}
    try:
        while True:
            try:
                revision = api.call(f"repos/{CENTRAL}/git/ref/heads/main")["object"]["sha"]
                if revision != os.environ["GITHUB_SHA"]:
                    print("Trusted main changed; handing waiting to a fresh runner.")
                    break
                if not poll(api, store, int(time.time()), revision, checked):
                    print("No active authorized PRs remain.")
                    return
            except APIError as error:
                if not error.rate_limited:
                    raise
                delay = max(60, (error.retry_at or int(time.time()) + 60) - int(time.time()) + 1)
                if delay >= duration - (time.monotonic() - started):
                    print("API quota resets after this waiter window; the scheduled waiter will retry.")
                    return
                print(f"API rate limited; shared waiter pauses for {delay} seconds.")
                time.sleep(delay)
                continue
            remaining = duration - (time.monotonic() - started)
            if remaining <= 0:
                break
            time.sleep(min(POLL_SECONDS, remaining))
    except DeadlineReached:
        print("Shared polling deadline reached.")
    finally:
        api.deadline = None
    api.call(f"repos/{CENTRAL}/actions/workflows/waiter.yml/dispatches", "POST", {"ref": "main"})
    print("Shared waiter handed off before its runner timeout.")


def main():
    require(os.environ.get("GITHUB_REPOSITORY") == CENTRAL
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_EVENT_NAME") in {"workflow_dispatch", "workflow_run", "schedule"}
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1"
            and git(["rev-parse", "HEAD"], Path.cwd()).decode().strip() == os.environ["GITHUB_SHA"],
            "Waiter requires a fresh trusted central-main runner")
    api = API()
    run(api, State(api))


if __name__ == "__main__":
    main()
