"""Per-PR ownership of short coordinator work and verification pipelines."""

import os

from loop.live import execution_revision
from loop.policy import CENTRAL, SHA, require
from loop.revisions import revision_ref, workflow_ref


def owner():
    value = {"id": int(os.environ["GITHUB_RUN_ID"]),
             "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
             "revision": os.environ["GITHUB_SHA"]}
    require(value["id"] > 0 and value["attempt"] == 1 and SHA.fullmatch(value["revision"]),
            "Coordinator reruns or invalid execution identities are forbidden")
    return value


def busy(api, state):
    claim = state.get("coordinator_run")
    if claim is None:
        return False
    run = api.call(f"repos/{CENTRAL}/actions/runs/{claim['id']}")
    require(run["id"] == claim["id"]
            and run["run_attempt"] == claim["attempt"] == 1
            and run["repository"]["full_name"] == run["head_repository"]["full_name"] == CENTRAL
            and run["path"].split("@")[0] == ".github/workflows/coordinator.yml"
            and run["head_branch"] in ({"main", revision_ref(claim["revision"])}
                                      if workflow_ref(state["request"]) != "main" else {"main"})
            and run["head_sha"] == claim["revision"]
            and run["status"] in {"queued", "in_progress", "waiting", "pending", "requested", "completed"},
            "Unbound coordinator execution")
    return run["status"] != "completed"


def claim(store, name, state, api):
    execution = owner()
    require(execution["revision"] == execution_revision(state),
            "Coordinator must run at the frozen trusted revision")
    if workflow_ref(state["request"]) != "main":
        require(os.environ.get("GITHUB_REF") in {
            "refs/heads/main", "refs/heads/" + revision_ref(execution["revision"]),
        }, "Coordinator must run on main or its pinned revision ref")
    if state.get("coordinator_run") == execution:
        return state
    if busy(api, state):
        return None
    def acquire(current):
        require(current == state, "Cancelled or concurrently claimed coordinator work")
        current["coordinator_run"] = execution
        return current
    return store.update(name, acquire)


def owned(state):
    require(state.get("coordinator_run") is None or state["coordinator_run"] == owner(),
            "Work belongs to another coordinator execution")
