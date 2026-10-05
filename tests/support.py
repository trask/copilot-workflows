"""Current artifact and admission fixtures for offline protocol tests."""

import hashlib
import json

from loop.candidates import PROTOCOL
from loop.policy import DEFAULTS, PROFILE, digest
from loop.verify import reconstruct as reconstruct_candidate

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
    value = {"schema": 2, "request_digest": digest(request), "outcome": outcome,
             "batches": [batch(patch, keys if external else None)] if patch else []}
    if external:
        value["findings"] = [dict(REASONING, key=key, disposition=disposition) for key in keys]
    return value


def reconstruct(files, request, *args, **kwargs):
    files = dict(files)
    patch = files["candidate.patch"]
    if "result.json" not in files:
        files["result.json"] = json.dumps(semantic(
            request, "fixes" if patch else "clean" if request.get("loop_kind") == "self_review"
            else "no_change", patch, "fixed" if patch else "not_warranted")).encode()
    return reconstruct_candidate(files, request, *args, **kwargs)


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
