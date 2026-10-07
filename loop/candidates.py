"""The single current worker contract and deterministic review messages."""

import hashlib

from loop.policy import bot, digest, exact, loop_kind, require

PROTOCOL = "reviewable-v1"
TRAILER = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
MESSAGE_FIELDS = {"summary", "analysis", "upsides", "downsides"}


def current_request(request):
    require(request.get("protocol") == PROTOCOL, "Historical requests are read-only")


def prose(value, *, summary=False):
    require(isinstance(value, str) and value.strip() == value and value,
            "Nonempty prose is required")
    require(not any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value),
            "Invalid message text")
    if summary:
        require("\n" not in value and "\t" not in value, "Summary must be one line")


def reasoning(value):
    for key in ("analysis", "upsides", "downsides"):
        prose(value[key])


def semantic(value, request):
    current_request(request)
    kind = loop_kind(request)
    external = kind == "copilot_review"
    extra = {"pr_description": {"proposal"}, "pr_review": {"comments"},
             "pr_consistency": {"consistency"}, "pr_conflict_resolver": {"merge"},
             "ci_fix": {"diagnoses", "rerun_run"}}.get(kind, set())
    exact(value, {"schema", "request_digest", "outcome", "batches"}
          | ({"findings"} if external else set()) | extra)
    require(type(value["schema"]) is int and value["schema"] == 2
            and value["request_digest"] == digest(request), "Wrong current worker binding")
    outcomes = ({"fixes", "no_change", "blocked"} if external else
                {"fixes", "clean", "blocked"} if kind == "self_review" else
                {"fixes", "no_change", "blocked", "rerun"} if kind == "ci_fix" else
                {"merge", "no_change", "blocked"} if kind == "pr_conflict_resolver" else
                {"proposal", "no_change", "blocked"} if kind == "pr_description" else
                {"comments", "no_change", "blocked"} if kind == "pr_review" else
                {"fixes", "no_change", "blocked"})
    require(value["outcome"] in outcomes, "Unknown worker outcome")
    if kind == "pr_description":
        from loop.recommendations import proposal
        proposal(value["proposal"])
        require(not value["batches"], "Metadata task cannot change source")
        require(value["outcome"] == "blocked" or
                (value["outcome"] == "proposal") == (value["proposal"] != request["metadata"]),
                "Metadata outcome contradicts proposed text")
    if kind == "pr_review":
        from loop.recommendations import comments
        comments(value["comments"], request)
        require(not value["batches"] and (value["outcome"] == "blocked" or
                (value["outcome"] == "comments") == bool(value["comments"])),
                "Review-only task cannot change source or invent findings")
    if kind == "pr_consistency":
        from loop.recommendations import consistency
        consistency(value["consistency"])
        require(value["outcome"] != "fixes" or any(
            item["classification"] == "avoidable" for item in value["consistency"]),
            "Consistency fixes require an avoidable difference")
    if kind == "ci_fix":
        from loop.ci import diagnoses
        diagnoses(value, request)
    if kind == "pr_conflict_resolver":
        require(not value["batches"], "Merge is not a linear code batch")
        exact(value["merge"], MESSAGE_FIELDS)
        prose(value["merge"]["summary"], summary=True)
        reasoning(value["merge"])
    batches = value["batches"]
    require(isinstance(batches, list) and len(batches) <= 100, "Invalid batch count")
    assigned = set()
    offset = 0
    for batch in batches:
        exact(batch, MESSAGE_FIELDS | {"offset", "length", "sha256"}
              | ({"findings"} if external else set()))
        prose(batch["summary"], summary=True)
        reasoning(batch)
        require(type(batch["offset"]) is int and batch["offset"] == offset
                and type(batch["length"]) is int and batch["length"] > 0,
                "Patch spans must be contiguous, ordered and nonempty")
        offset += batch["length"]
        require(isinstance(batch["sha256"], str)
                and len(batch["sha256"]) == 64
                and all(c in "0123456789abcdef" for c in batch["sha256"]),
                "Invalid patch span hash")
        if external:
            keys = batch["findings"]
            require(isinstance(keys, list) and keys and all(isinstance(k, str) for k in keys)
                    and len(keys) == len(set(keys)) and not assigned.intersection(keys),
                    "Duplicate or missing batch findings")
            assigned.update(keys)
    if external:
        expected = {finding["key"] for finding in request["findings"]}
        require(len(expected) == len(request["findings"]), "Duplicate frozen finding")
        findings = value["findings"]
        require(isinstance(findings, list) and len(findings) == len(expected),
                "Every frozen finding must be accounted for")
        seen, fixed, kinds = set(), set(), set()
        for item in findings:
            exact(item, {"key", "disposition", "analysis", "upsides", "downsides"})
            require(isinstance(item["key"], str) and item["key"] in expected
                    and item["key"] not in seen, "Missing, duplicate, or foreign finding")
            seen.add(item["key"])
            require(item["disposition"] in {"fixed", "not_warranted", "blocked"},
                    "Invalid disposition")
            kinds.add(item["disposition"])
            reasoning(item)
            if item["disposition"] == "fixed":
                fixed.add(item["key"])
        require(assigned == fixed, "Batch mapping must account for every fixed finding exactly once")
        require(value["outcome"] != "no_change" or kinds <= {"not_warranted"},
                "False no-change")
        require(value["outcome"] != "blocked" or "blocked" in kinds, "Blocked without blocker")
        require(value["outcome"] == "blocked" or "blocked" not in kinds,
                "Blocked findings cannot publish partial work")
    require(value["outcome"] == "blocked" or (value["outcome"] == "fixes") == bool(batches),
            "Outcome contradicts code batches")


def patches(value, patch):
    require(sum(batch["length"] for batch in value["batches"]) == len(patch),
            "Patch bytes are omitted or unaccounted for")
    parts = []
    for batch in value["batches"]:
        part = patch[batch["offset"]:batch["offset"] + batch["length"]]
        require(hashlib.sha256(part).hexdigest() == batch["sha256"], "Batch patch hash differs")
        parts.append(part)
    return parts


def tradeoffs(value):
    return "\n\n".join(f"{key.title()}: {value[key]}"
                       for key in ("analysis", "upsides", "downsides"))


def message(batch, request):
    if loop_kind(request) == "copilot_review":
        subject = ("Address Copilot review comment" +
                   ("s" if len(batch["findings"]) > 1 else "") + ": " + batch["summary"])
        by_key = {finding["key"]: finding for finding in request["findings"]}
        comments = []
        for key in batch["findings"]:
            finding = by_key[key]
            if finding.get("kind") == "inline":
                require(bot(finding.get("root_author")), "Unverified original review author")
            body = finding["body"]
            require(isinstance(body, str) and not any(
                ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in body),
                "Original reviewer text contains prohibited controls")
            comments.append("Copilot comment:\n\n" + body)
        text = subject + "\n\n" + "\n\n".join(comments) + "\n\n" + tradeoffs(batch)
    else:
        subject = batch["summary"]
        text = subject + "\n\n" + tradeoffs(batch)
    text += "\n\n" + TRAILER + "\n"
    return subject, text.encode("utf-8")
