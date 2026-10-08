"""Acquire task data locally, independently of durable task state."""

from pathlib import Path

from loop.policy import direct_inputs, loop_kind, require


def acquire(api, request, directory):
    if not direct_inputs(request):
        return request
    inputs = {"identity": {}}
    if loop_kind(request) not in {"copilot_review", "self_review"}:
        from loop.recommendations import collect_diff
        inputs["pr_diff"] = collect_diff(api, request)
        inputs["identity"]["pr_diff_sha256"] = inputs["pr_diff"]["sha256"]
    if loop_kind(request) == "ci_fix":
        from loop.ci import collect, same_attempts
        same_attempts(api, request)
        inputs["ci_evidence"] = collect(
            api, request, request["ci_evidence"]["required"],
            log_dir=Path(directory, "ci-logs"))
        same_attempts(api, request)
    return dict(request, inputs=inputs)


def identity(value, request):
    expected = ({"pr_diff_sha256"} if loop_kind(request) not in
                {"copilot_review", "self_review"} else set())
    require(isinstance(value, dict) and set(value) == expected
            and all(isinstance(item, str) and len(item) == 64
                    and all(c in "0123456789abcdef" for c in item) for item in value.values()),
            "Invalid acquired task input identity")
    if "inputs" in request:
        require(value == request["inputs"]["identity"], "Task inputs changed after worker acquisition")
