"""Exact central workflow refs for independently pinned phases."""

import sys
import time

from loop.api import APIError
from loop.policy import CENTRAL, SHA, require

PREFIX = "review-loop-revisions/"


def revision_ref(revision):
    require(isinstance(revision, str) and SHA.fullmatch(revision),
            "Invalid trusted workflow revision")
    return PREFIX + revision


def workflow_ref(request):
    ref = request.get("workflow_ref", "main")
    require("workflow_ref" not in request or ref == revision_ref(request["workflow_revision"]),
            "Workflow ref differs from the frozen revision")
    return ref


def execution_revision(state):
    recovery = state.get("reconciliation") or state.get("coordinator_recovery") or {}
    return recovery.get(
        "execution_revision", state["request"]["workflow_revision"])


def verification_revision(state):
    return (execution_revision(state) if state.get("coordinator_recovery")
            else state["request"]["workflow_revision"])


def execution_ref(state):
    reconciliation = state.get("reconciliation") or state.get("coordinator_recovery")
    if reconciliation:
        require(workflow_ref(state["request"]) == "main" or "workflow_ref" in reconciliation,
                "Missing reconciled workflow revision pin")
        return workflow_ref({
            "workflow_revision": execution_revision(state),
            **({"workflow_ref": reconciliation["workflow_ref"]}
               if "workflow_ref" in reconciliation else {}),
        })
    return workflow_ref(state["request"])


def check_pin(api, ref, revision):
    require(ref == revision_ref(revision), "Invalid pinned workflow ref")
    live = api.call(f"repos/{CENTRAL}/git/ref/heads/{ref}")
    require(isinstance(live, dict) and isinstance(live.get("object"), dict)
            and live.get("ref") == "refs/heads/" + ref
            and live["object"].get("type") == "commit"
            and live["object"]["sha"] == revision,
            "Pinned workflow ref is missing or changed")
    return ref


def pin_revision(api, revision):
    ref = revision_ref(revision)
    try:
        return check_pin(api, ref, revision)
    except APIError as error:
        if error.status != 404:
            raise
    try:
        api.call(f"repos/{CENTRAL}/git/refs", "POST", {
            "ref": "refs/heads/" + ref, "sha": revision,
        })
    except APIError as error:
        if error.status != 422:
            raise
    for attempt in range(3):
        try:
            return check_pin(api, ref, revision)
        except APIError as error:
            if error.status != 404 or attempt == 2:
                raise
        print(f"PIN READ RETRY: Created ref not yet visible; attempt {attempt + 2}/3",
              file=sys.stderr)
        time.sleep(attempt + 1)


def inherit_pin(request, state):
    ref = execution_ref(state)
    if ref != "main":
        require(request["workflow_revision"] == execution_revision(state),
                "Next pass changed the phase's workflow revision")
        request["workflow_ref"] = ref
