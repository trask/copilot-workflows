"""Frozen merge ancestry and unresolved conflict-marker checks."""

import re

from loop.policy import require


def history(directory, request):
    from loop.verify import git
    head, base, ancestor = (request[k] for k in ("frozen_sha", "base_sha", "merge_base_sha"))
    require(git(["merge-base", head, base], directory).decode().strip() == ancestor,
            "Incomplete or contradictory frozen merge history")
    return ancestor == base


def resolved_tree(directory, request, tree):
    from loop.verify import git, safe_path, tree_entries
    output = git(["--attr-source=" + request["frozen_sha"],
                  "merge-tree", "--write-tree", "--name-only", "-z", "--no-messages",
                  request["frozen_sha"], request["base_sha"]], directory, allowed=(0, 1))
    records = output.split(b"\0")
    automatic = tree_entries(directory, records[0].decode().strip())
    proposed = tree_entries(directory, tree)
    for path in (p.decode("utf-8") for p in records[1:] if p):
        entry = automatic.get(path)
        resolved = proposed.get(path)
        if entry and resolved and entry[1] == resolved[1] == "blob":
            blob = git(["cat-file", "blob", entry[2]], directory)
            marker = re.search(br"(?m)^(<+) " + request["frozen_sha"].encode() + br"(?=\r?$|:)", blob)
            if marker:
                width = len(marker[1])
                resolved_blob = git(["cat-file", "blob", resolved[2]], directory)
                require(not any(line.startswith((
                    b"<" * width + b" ", b"=" * width, b">" * width + b" "))
                    for line in resolved_blob.splitlines()), "Unresolved conflict markers")
    paths = git(["diff", "--name-only", "--no-renames", "-z", request["frozen_sha"], tree],
                directory).decode("utf-8").split("\0")[:-1]
    for path in paths:
        safe_path(path, request)
    return paths
