"""Semantic worker results; Git supplies candidate history and commit messages."""

from loop.policy import digest, direct_inputs, exact, loop_kind, require, source_effect

PROTOCOL = "git-candidate-v1"
TRAILER = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"


def current_request(request):
    require(request.get("protocol") == PROTOCOL, "Historical requests are read-only")


def prose(value, *, summary=False):
    require(isinstance(value, str) and value.strip() == value and value,
            "Nonempty prose is required")
    require(not any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value),
            "Invalid message text")
    if summary:
        require("\n" not in value and "\t" not in value, "Summary must be one line")


def semantic(value, request):
    current_request(request)
    kind = loop_kind(request)
    extra = {"copilot_review": {"findings"}, "pr_description": {"proposal"},
             "pr_review": {"comments"}, "pr_consistency": {"consistency"},
             "ci_fix": {"diagnoses", "rerun_run"}}.get(kind, set())
    if source_effect(request) and "proposal" in value:
        extra |= {"proposal"}
    exact(value, {"schema", "request_digest", "outcome"}
          | ({"input_identity"} if direct_inputs(request) else set()) | extra)
    require(type(value["schema"]) is int and value["schema"] == 2
            and value["request_digest"] == digest(request), "Wrong current worker binding")
    if direct_inputs(request):
        from loop.inputs import identity
        identity(value["input_identity"], request)
    outcomes = {"fixes", "no_change", "blocked"}
    if kind == "self_review":
        outcomes = {"fixes", "clean", "blocked"}
    elif kind == "pr_conflict_resolver":
        outcomes = {"merge", "no_change", "blocked"}
    elif kind == "ci_fix":
        outcomes |= {"rerun"}
    elif kind == "pr_description":
        outcomes = {"proposal", "no_change", "blocked"}
    elif kind == "pr_review":
        outcomes = {"comments", "no_change", "blocked"}
    require(value["outcome"] in outcomes, "Unknown worker outcome")
    if source_effect(request) and "proposal" in value:
        from loop.recommendations import proposal
        proposal(value["proposal"])
        require("metadata" in request and value["outcome"] not in {"blocked", "rerun"}
                and value["proposal"]["title"] == request["metadata"]["title"]
                and value["proposal"]["body"] != request["metadata"]["body"],
                "Description correction requires a source publication result, changed body and unchanged title")
    if kind == "pr_description":
        from loop.recommendations import proposal
        proposal(value["proposal"])
        require(value["outcome"] == "blocked" or
                (value["outcome"] == "proposal") == (value["proposal"] != request["metadata"]),
                "Metadata outcome contradicts proposed text")
    elif kind == "pr_review":
        from loop.recommendations import comments
        comments(value["comments"], request)
        require(value["outcome"] == "blocked" or
                (value["outcome"] == "comments") == bool(value["comments"]),
                "Review outcome contradicts comments")
    elif kind == "pr_consistency":
        from loop.recommendations import consistency
        consistency(value["consistency"])
        require(value["outcome"] != "fixes" or any(
            item["classification"] == "avoidable" for item in value["consistency"]),
            "Consistency fixes require an avoidable difference")
    elif kind == "ci_fix":
        from loop.ci import diagnoses
        if not direct_inputs(request) or "inputs" in request:
            diagnoses(value, request)
    elif kind == "copilot_review":
        expected = {finding["key"] for finding in request["findings"]}
        require(len(expected) == len(request["findings"]), "Duplicate frozen finding")
        findings = value["findings"]
        require(isinstance(findings, list) and len(findings) == len(expected),
                "Every frozen finding must be accounted for")
        seen, decisions = set(), set()
        for item in findings:
            exact(item, {"key", "disposition", "analysis", "commit"})
            require(isinstance(item["key"], str) and item["key"] in expected
                    and item["key"] not in seen, "Missing, duplicate, or foreign finding")
            seen.add(item["key"])
            require(item["disposition"] in {"fixed", "description_updated", "not_warranted", "blocked"},
                    "Invalid disposition")
            prose(item["analysis"])
            decisions.add(item["disposition"])
            require((type(item["commit"]) is int and item["commit"] > 0)
                    if item["disposition"] == "fixed" else item["commit"] is None,
                    "Only fixed findings select a candidate commit")
        require("description_updated" not in decisions or "proposal" in value,
                "Description decisions require a matching proposal")
        require(value["outcome"] != "no_change" or decisions <= {"not_warranted", "description_updated"},
                "False no-change")
        require(value["outcome"] != "blocked" or "blocked" in decisions, "Blocked without blocker")
        require(value["outcome"] == "blocked" or "blocked" not in decisions,
                "Blocked findings cannot publish partial work")
