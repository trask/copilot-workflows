"""Complete PR diff evidence and non-code proposals."""

import hashlib
import re

from loop.policy import check_target, exact, require

def description_diff(text):
    require(isinstance(text, str), "Invalid description diff evidence")
    return {"text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def diff_anchors(text):
    require(isinstance(text, str) and "\x00" not in text,
            "Unavailable or unsupported complete PR diff")
    from loop.verify import patch_sections
    anchors, counts = {}, {}
    for section, _, path, added, deleted in patch_sections(text.encode("utf-8")):
        require(path not in counts, "Duplicate PR diff file")
        counts[path], anchors[path] = [0, 0], []
        old_left, new_left, line = 0, 0, 0
        for record in section.decode("utf-8").split("\n")[1:]:
            if record.startswith("@@"):
                require(old_left == new_left == 0, "Truncated PR diff")
                match = re.fullmatch(r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@.*", record)
                require(match is not None, "Malformed PR diff hunk")
                old_left = int(match[2]) if match[2] is not None else 1
                line = int(match[3])
                new_left = int(match[4]) if match[4] is not None else 1
            elif old_left or new_left:
                require(record[:1] in {" ", "+", "-", "\\"}, "Truncated PR diff contents")
                if record.startswith("+"):
                    anchors[path].append(line)
                    counts[path][0] += 1
                if record.startswith("-"):
                    counts[path][1] += 1
                if record.startswith((" ", "-")):
                    old_left -= 1
                if record.startswith((" ", "+")):
                    new_left -= 1
                    line += 1
                require(old_left >= 0 and new_left >= 0, "Malformed PR diff lengths")
        require(old_left == new_left == 0 and counts[path] == [added or 0, deleted or 0],
                "Truncated or inconsistent PR diff")
    return anchors, counts


def collect_diff(api, request):
    path = f"repos/{request['repo']}/pulls/{request['pr']}"
    pr = check_target(api, request)
    from loop.policy import loop_kind
    if loop_kind(request) == "pr_description":
        text = api.call(path, raw=True, limit=None, accept="application/vnd.github.diff").decode("utf-8")
        result = description_diff(text)
        check_target(api, request)
        return result
    files = api.pages(path + "/files")
    require(len(files) == pr["changed_files"],
            "Incomplete GitHub PR file collection")
    raw = api.call(path, raw=True, limit=None, accept="application/vnd.github.diff")
    text = raw.decode("utf-8")
    anchors, counts = diff_anchors(text)
    require(set(counts) == {f["filename"] for f in files}
            and all(counts[f["filename"]] == [f["additions"], f["deletions"]] for f in files),
            "GitHub PR diff is truncated or inconsistent with complete file evidence")
    check_target(api, request)
    return {"text": text, "sha256": hashlib.sha256(raw).hexdigest(), "anchors": anchors}


def check_diff(api, request):
    require(collect_diff(api, request) == request["pr_diff"], "Authoritative PR diff changed")


def proposal(value):
    require(isinstance(value, dict),
            "Description proposal must contain title and body, including for no_change")
    exact(value, {"title", "body"})
    from loop.candidates import prose
    prose(value["title"], summary=True)
    require(isinstance(value["body"], str)
            and not any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127
                        for c in value["body"]), "Invalid proposed PR body")


def comments(value, request):
    require(isinstance(value, list) and len(value) <= 100, "Invalid review comment count")
    seen = set()
    from loop.candidates import prose
    for item in value:
        exact(item, {"path", "line", "side", "body"})
        require(isinstance(item["path"], str) and type(item["line"]) is int
                and item["side"] == "RIGHT"
                and item["line"] in request["pr_diff"]["anchors"].get(item["path"], []),
                "Review comment is not anchored on an authoritative changed line")
        prose(item["body"])
        identity = item["path"], item["line"], item["body"]
        require(identity not in seen, "Duplicate review comment")
        seen.add(identity)


def consistency(value):
    require(isinstance(value, list) and len(value) <= 100, "Invalid consistency report")
    from loop.candidates import prose
    for item in value:
        exact(item, {"path", "classification", "explanation", "citations"})
        require(item["classification"] in {"needed", "avoidable", "unclear"},
                "Unknown consistency classification")
        from loop.verify import safe_source_path
        safe_source_path(item["path"])
        prose(item["explanation"])
        require(isinstance(item["citations"], list) and 1 <= len(item["citations"]) <= 10,
                "Consistency decisions require instructions or compliant example citations")
        for citation in item["citations"]:
            prose(citation, summary=True)
