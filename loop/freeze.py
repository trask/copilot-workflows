"""Freeze verified reviews and unresolved inline findings from live API data."""

import time
import uuid

from loop.policy import (AUTHOR_ID, BOT_IDENTITY_PATH, BOT_NODE, DEFAULTS, LOOP_KINDS, SHA, attributed_owner, bot, check_target, commit_author, diff_scope, eligible,
                         iso, require, safe_ref, unchanged)

THREADS = """
query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved isOutdated comments(first:1) {
          nodes { databaseId }
        } }
      }
    }
  }
}
"""
def threads(api, number, repo):
    result = {}
    cursor = None
    for _ in range(100):
        owner, name = repo.split("/")
        connection = api.graphql(THREADS, {"owner": owner, "name": name,
                                         "number": number, "cursor": cursor})[
            "repository"]["pullRequest"]["reviewThreads"]
        for thread in connection["nodes"]:
            roots = thread["comments"]["nodes"]
            require(len(roots) == 1, "Missing thread root")
            root = roots[0]["databaseId"]
            require(root not in result, "Duplicate thread root")
            result[root] = {"id": thread["id"], "resolved": thread["isResolved"],
                            "outdated": thread["isOutdated"]}
        if not connection["pageInfo"]["hasNextPage"]:
            return result
        cursor = connection["pageInfo"]["endCursor"]
    raise ValueError("Thread pagination limit exceeded")


def unresolved_ids(api, number, repo):
    return {root for root, thread in threads(api, number, repo).items()
            if not thread["resolved"]}


def complete_threads(reviews, comments, roots):
    submitted = {r["id"]: r for r in reviews if bot(r.get("user")) and r.get("submitted_at")
                 and r["state"] in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}}
    require(set(roots) <= {c["id"] for c in comments}
            and all(c["id"] in roots
                    and c["original_commit_id"] == submitted[c["pull_request_review_id"]]["commit_id"]
                    for c in comments if c["pull_request_review_id"] in submitted
                    and bot(c.get("user"))
                    and not c.get("in_reply_to_id")),
            "Incomplete or inconsistent inline thread collection")


def select_findings(reviews, comments, unresolved, sha):
    submitted = {r["id"]: r for r in reviews if bot(r["user"]) and r.get("submitted_at")
                 and r["state"] in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}}
    require(submitted, "No submitted verified Copilot review")
    current = {key: review for key, review in submitted.items() if review["commit_id"] == sha}
    findings = []
    # Review bodies cannot be resolved like inline threads. Preserve every nonempty body,
    # including ambiguous 'Findings: None' summaries containing hidden details.
    for review_id, review in sorted(current.items()):
        if review["body"] and review["body"].strip():
            findings.append({"key": f"review:{review_id}", "kind": "body", "review_id": review_id,
                             "comment_id": None, "path": None, "line": None,
                             "body": review["body"]})
    for comment in comments:
        if (comment["id"] in unresolved and not comment.get("in_reply_to_id") and bot(comment["user"])
                and comment["pull_request_review_id"] in submitted
                and comment["original_commit_id"]
                == submitted[comment["pull_request_review_id"]]["commit_id"]):
            findings.append({"key": f"inline:{comment['id']}", "kind": "inline",
                             "review_id": comment["pull_request_review_id"],
                             "review_commit_id": submitted[comment["pull_request_review_id"]]["commit_id"],
                             "review_submitted_at": submitted[comment["pull_request_review_id"]]["submitted_at"],
                             "comment_commit_id": comment["commit_id"],
                             "original_commit_id": comment["original_commit_id"],
                             "root_author": {key: comment["user"][key]
                                             for key in ("id", "node_id", "type")},
                             "comment_id": comment["id"], "path": comment["path"],
                             "line": comment["line"],
                             "original_line": comment["original_line"],
                             "body": comment["body"]})
    require(findings, "Empty finding set")
    require(len({f["key"] for f in findings}) == len(findings), "Duplicate API findings")
    return findings


def probe_target(api, number, revision, now=None, repo=None, actor_id=AUTHOR_ID,
                 loop_kind="copilot_review"):
    require(SHA.fullmatch(revision), "Trusted workflow revision must be a full SHA")
    require(loop_kind in LOOP_KINDS, "Unknown launch loop kind")
    if loop_kind == "copilot_review":
        identity = api.call(BOT_IDENTITY_PATH)
        require(bot(identity) and identity["node_id"] == BOT_NODE, "Copilot identity changed")
    path = f"repos/{repo}/pulls/{number}"
    pr = api.call(path)
    owner = attributed_owner(api, pr, repo, actor_id) if loop_kind != "pr_review" else None
    target = eligible(pr, repo, actor_id, loop_kind, owner)
    author = owner or (api.call(f"user/{actor_id}") if loop_kind == "pr_review" else pr["user"])
    target["commit_author"] = {"id": author["id"], "login": author.get("login")}
    commit_author(target)
    if diff_scope(dict(loop_kind=loop_kind)):
        base = pr["base"]
        require(safe_ref(base["ref"]), "Invalid self-review base ref")
        ref = api.call(f"repos/{repo}/git/ref/heads/{base['ref']}")
        base_sha = ref["object"]["sha"]
        require(isinstance(base_sha, str) and SHA.fullmatch(base_sha),
                "Invalid self-review base tip")
        comparison = api.call(f"repos/{repo}/compare/{base_sha}...{target['frozen_sha']}")
        require(comparison["base_commit"]["sha"] == base_sha
                and comparison["status"] in {"ahead", "behind", "diverged", "identical"}
                and SHA.fullmatch(comparison["merge_base_commit"]["sha"]),
                "Incomplete self-review merge-base identity")
        target.update(base_ref=base["ref"], base_sha=base_sha,
                      merge_base_sha=comparison["merge_base_commit"]["sha"])
    if loop_kind == "pr_description":
        target["metadata"] = {"title": pr["title"], "body": pr["body"] or ""}
    now = int(time.time()) if now is None else now
    from loop.candidates import PROTOCOL
    return dict(target, schema=2, protocol=PROTOCOL, request_id=uuid.uuid4().hex, workflow_revision=revision,
                frozen_at=now, frozen_at_iso=iso(now),
                deadline=now + DEFAULTS["deadline_seconds"],
                budgets=DEFAULTS.copy(), baseline_review_ids=[], findings=[], loop_kind=loop_kind)


def freeze(api, number, revision, now=None, repo=None, actor_id=AUTHOR_ID,
           loop_kind="copilot_review"):
    request = probe_target(api, number, revision, now, repo, actor_id, loop_kind)
    path = f"repos/{repo}/pulls/{number}"
    if loop_kind != "copilot_review":
        check_target(api, request)
        if loop_kind != "self_review":
            from loop.recommendations import collect_diff
            request["pr_diff"] = collect_diff(api, request)
        if loop_kind == "ci_fix":
            from loop.ci import collect
            from loop.reviews import select_checks
            request["ci_evidence"] = collect(api, request, select_checks(api, request))
        return request
    reviews = api.pages(path + "/reviews")
    comments = api.pages(path + "/comments")
    roots = threads(api, number, repo)
    complete_threads(reviews, comments, roots)
    findings = select_findings(reviews, comments, {
        root for root, thread in roots.items() if not thread["resolved"]
    }, request["frozen_sha"])
    for finding in findings:
        if finding["kind"] == "inline":
            from loop.effects import conversation
            finding["thread_id"] = roots[finding["comment_id"]]["id"]
            finding["thread_context"] = conversation(comments, finding["comment_id"])
    # API pagination is not an atomic snapshot. A head change during collection invalidates it.
    unchanged(request, api.call(path))
    return dict(request, baseline_review_ids=sorted(r["id"] for r in reviews), findings=findings)
