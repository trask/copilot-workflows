"""Conservative submitted-review and exact-SHA CI collection."""

import re

from loop.freeze import complete_threads, threads
from loop.policy import (BOT_ID, DEFAULTS, bot, canonical, digest, require,
                         timestamp, unchanged)

def body_classification(body):
    """Only complete CCR v2 zero-finding summaries can establish a clean body."""
    return _body_classification(body)[0]


def _body_classification(body):
    if not isinstance(body, str):
        return "unknown", []
    if body.count("<!-- ccr-overview-v2 -->") != 1:
        return "unknown", []
    counts = re.findall(r"\*\*Findings:\*\*\s*(None|[0-9]+)\b", body)
    counts += re.findall(r"(?:\*\*|<strong>)([0-9]+) open findings?(?:\*\*|</strong>)", body)
    if len(counts) != 1:
        return "unknown", []
    if counts[0] != "None" and int(counts[0]) > 0:
        return "findings", []
    effort = re.search(r"\n\n\U0001f9e0 \*\*Review effort:\*\* Balanced\n?\Z", body)
    if effort is not None:
        body = body[:effort.start()]
    resolved_ids = []
    resolved = re.search(
        r"\n\n<details>\n<summary><strong>(?:Resolved since last review "
        r"\(([1-9][0-9]*)\)|([1-9][0-9]*) resolved since last review)"
        r"</strong></summary>\n\n(.+)\n</details>\n?\Z",
        body, re.DOTALL)
    if resolved is not None:
        entry = (r"- (?:<picture>(?:<source [^<>\n]+>)+<img [^<>\n]+></picture> )?"
                 r"\[[^\[\]<>\n]+\]\(#discussion_r([1-9][0-9]{0,19})\)")
        for line in resolved[3].splitlines():
            item = re.fullmatch(entry, line)
            if item is None:
                return "unknown", []
            resolved_ids.append(int(item[1]))
        if (len(resolved_ids) != int(resolved[1] or resolved[2])
                or len(set(resolved_ids)) != len(resolved_ids)):
            return "unknown", []
        body = body[:resolved.start()]
    if re.search(r"discussion_r[0-9]+|Previously missed|Open \([1-9]|New \([1-9]", body):
        return "findings", []
    heading = (r"### (?:\U0001f7e2 Approval recommended|\U0001f535 Needs a closer look)"
               r"\n\n[^\n<>#*]+\n\n")
    clean = (r"<!-- ccr-overview-v2 -->\n\n" + heading + r"\*\*0 open findings\*\*\n?"
             if effort is not None else
             r"<!-- ccr-overview-v2 -->\n\n## Copilot review overview\n\n" + heading
             + r"\*\*Review effort:\*\* Balanced  \n\*\*Findings:\*\* None\n?")
    if counts[0] in {"None", "0"} and re.fullmatch(clean, body):
        return "clean", resolved_ids
    return "unknown", []


def finding_fingerprint(findings):
    return digest(sorted([{"kind": f["kind"], "path": f.get("path"), "body": f["body"]}
                          for f in findings], key=canonical))


def inline_fingerprints(findings):
    return sorted(digest({key: finding[key] for key in (
        "comment_id", "review_id", "original_commit_id", "root_author", "path", "body")})
                  for finding in findings if finding["kind"] == "inline")


def missed_fingerprints(findings):
    """Identify body-only findings in a complete counted CCR v2 missed section."""
    fingerprints = set()
    section = (r"\n\n<details>\n<summary><strong>Previously missed "
               r"\(([1-9][0-9]{0,3})\)</strong></summary>\n\n"
               r"In code that hasn't changed since last review\n\n(.+)\n</details>\n?\Z")
    entry = re.compile(
        r"<details>\n<summary>([^\n]+)</summary>\n\n"
        r"((?:(?!</?details[>\s]).)+)\n</details>", re.DOTALL)
    for finding in findings:
        if finding["kind"] != "body" or body_classification(finding["body"]) != "findings":
            continue
        missed = re.search(section, finding["body"], re.DOTALL)
        if missed is None:
            continue
        items = list(entry.finditer(missed[2]))
        if (len(items) != int(missed[1]) or entry.sub("", missed[2]).strip()
                or any(not item[2].strip() for item in items)):
            continue
        fingerprints.update(digest({"summary": item[1], "body": item[2]}) for item in items)
    return sorted(fingerprints)


def fresh_collection(api, request, baseline, requested_at, expected_sha, now):
    path = f"repos/{request['repo']}/pulls/{request['pr']}"
    unchanged(dict(request, frozen_sha=expected_sha), api.call(path))
    reviews = api.pages(path + "/reviews")
    comments = api.pages(path + "/comments")
    roots = threads(api, request["pr"], request["repo"])
    fresh = [r for r in reviews if bot(r.get("user"))
             and r["id"] not in baseline and r.get("submitted_at")
             and r["commit_id"] == expected_sha
             and r["state"] in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}
             and timestamp(r["submitted_at"]) > requested_at]
    if not fresh:
        return {"decision": "waiting_review"}
    latest = max(fresh, key=lambda r: (timestamp(r["submitted_at"]), r["id"]))
    if now < timestamp(latest["submitted_at"]) + DEFAULTS["propagation_seconds"]:
        return {"decision": "waiting_propagation", "review_id": latest["id"]}
    submitted = {r["id"]: r for r in reviews if bot(r.get("user")) and r.get("submitted_at")
                 and r["state"] in {"COMMENTED", "APPROVED", "CHANGES_REQUESTED"}}
    complete_threads(reviews, comments, roots)
    relevant = [c for c in comments if c["pull_request_review_id"] in submitted
                and bot(c.get("user"))
                and c["original_commit_id"] == submitted[c["pull_request_review_id"]]["commit_id"]
                and not c.get("in_reply_to_id") and c["id"] in roots
                and not roots[c["id"]]["resolved"]]
    parsed = {r["id"]: _body_classification(r.get("body")) for r in fresh}
    classifications = {review_id: value[0] for review_id, value in parsed.items()}
    resolved_ids = {root for _, linked in parsed.values() for root in linked}
    original_bot_roots = {c["id"] for c in comments if bot(c.get("user"))
                          and c["pull_request_review_id"] in submitted
                          and c["original_commit_id"] == submitted[c["pull_request_review_id"]]["commit_id"]
                          and not c.get("in_reply_to_id")}
    classification = classifications[latest["id"]]
    if (relevant or "findings" in classifications.values()
            or any(r["state"] == "CHANGES_REQUESTED" for r in fresh)):
        decision = "findings"
    elif ("unknown" in classifications.values()
          or not resolved_ids <= roots.keys() & original_bot_roots):
        decision = "unknown"
    else:
        decision = "clean"
    unchanged(dict(request, frozen_sha=expected_sha), api.call(path))
    return {"decision": decision, "review_id": latest["id"],
            "submitted_at": latest["submitted_at"], "body_classification": classification,
            "review_ids": sorted(classifications), "body_classifications": classifications,
            "inline_ids": [c["id"] for c in relevant]}


def ci_items(api, repo, sha, *, latest_statuses=False):
    checks = api.pages(f"repos/{repo}/commits/{sha}/check-runs?filter=all", "check_runs")
    statuses = (api.pages(f"repos/{repo}/commits/{sha}/status", "statuses") if latest_statuses
                else api.pages(f"repos/{repo}/commits/{sha}/statuses"))
    require(len(checks) + len(statuses) <= 1000, "CI collection exceeds limits")
    return checks, statuses


def select_checks(api, request):
    checks, statuses = ci_items(api, request["repo"], request["frozen_sha"])
    selected = {c["name"] for c in checks if not copilot_check(c)}
    selected.update(s["context"] for s in statuses if "copilot" not in s["context"].casefold())
    require(len(selected) <= 100
            and all(isinstance(name, str) and 0 < len(name) <= 200 for name in selected),
            "Invalid or oversized target CI selection")
    return sorted(selected)


def copilot_check(check):
    return ("copilot" in check["name"].casefold()
            or check.get("app", {}).get("id") == BOT_ID)


def check_decision(check, sha, *, accept_nonblocking=False):
    if check["head_sha"] != sha or not check.get("app", {}).get("id"):
        return "unknown"
    if check["status"] in {"queued", "in_progress", "waiting", "pending", "requested"}:
        return "pending"
    if check["status"] != "completed":
        return "unknown"
    if (check["conclusion"] == "success"
            or accept_nonblocking and check["conclusion"] in {"skipped", "neutral"}):
        return "passed"
    if check["conclusion"] in {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}:
        return "failed"
    return "unknown"


def independent_actions_checks(api, repo, sha, checks, cache):
    identities = []
    workflow_ids, workflow_paths, check_ids = set(), set(), set()
    for check in checks:
        app = check.get("app", {})
        link = check.get("details_url")
        if (app.get("id") != 15368 or app.get("slug") != "github-actions"
                or check["head_sha"] != sha or type(check["id"]) is not int
                or check["id"] <= 0 or check["id"] in check_ids or not isinstance(link, str)):
            return None
        match = re.fullmatch(r"https://github\.com/" + re.escape(repo)
                             + r"/actions/runs/([1-9][0-9]{0,19})/job/([1-9][0-9]{0,19})", link)
        if match is None:
            return None
        run_id, job_id = int(match[1]), int(match[2])
        if run_id not in cache:
            require(len(cache) < 100, "Duplicate CI provenance exceeds execution limit")
            run = api.call(f"repos/{repo}/actions/runs/{run_id}")
            jobs = api.pages(f"repos/{repo}/actions/runs/{run_id}/attempts/1/jobs", "jobs")
            cache[run_id] = run, jobs
        run, jobs = cache[run_id]
        path = run.get("path", "").split("@")[0]
        selected = [job for job in jobs if job["id"] == job_id]
        if (run["id"] != run_id or run["head_sha"] != sha or run["run_attempt"] != 1
                or run["repository"]["full_name"] != repo
                or type(run.get("workflow_id")) is not int or run["workflow_id"] <= 0
                or run["workflow_id"] in workflow_ids or path in workflow_paths
                or re.fullmatch(r"\.github/workflows/[^/\\@?#]+\.ya?ml", path) is None
                or run.get("check_suite_id") != check.get("check_suite", {}).get("id")
                or type(run.get("check_suite_id")) is not int
                or len(selected) != 1):
            return None
        job = selected[0]
        if (job["run_id"] != run_id or job["run_attempt"] != 1 or job["name"] != check["name"]
                or job["check_run_url"] != f"https://api.github.com/repos/{repo}/check-runs/{check['id']}"):
            return None
        workflow_ids.add(run["workflow_id"])
        workflow_paths.add(path)
        check_ids.add(check["id"])
        identities.append({"workflow_id": run["workflow_id"], "path": path,
                           "run_id": run_id, "attempt": 1, "job_id": job_id})
    return identities


def exact_ci(api, repo, sha, required):
    require(isinstance(required, list) and len(required) <= 100
            and len(set(required)) == len(required)
            and all(isinstance(name, str) and 0 < len(name) <= 200 for name in required),
            "Invalid frozen target CI selection")
    checks, statuses = ci_items(api, repo, sha)
    result = {"sha": sha, "required": required, "checks": []}
    if not required:
        return dict(result, decision="none")
    decisions = []
    executions = {}
    for name in required:
        runs = [c for c in checks if c["name"] == name and not copilot_check(c)]
        contexts = [s for s in statuses if s["context"] == name
                    and "copilot" not in name.casefold()]
        identities = None
        if len(runs) + len(contexts) > 1:
            if not contexts:
                identities = independent_actions_checks(api, repo, sha, runs, executions)
            if identities:
                selected = [check_decision(check, sha, accept_nonblocking=True) for check in runs]
                decision = next((d for d in ("failed", "pending", "unknown") if d in selected), "passed")
            else:
                decision = "unknown"
        elif not runs and not contexts:
            decision = "missing"
        elif runs:
            decision = check_decision(runs[0], sha, accept_nonblocking=True)
        else:
            decision = {"success": "passed", "pending": "pending",
                        "failure": "failed", "error": "failed"}.get(contexts[0]["state"], "unknown")
        decisions.append(decision)
        entry = {"name": name, "decision": decision, "ids": [c["id"] for c in runs + contexts]}
        if identities:
            entry["workflows"] = identities
        elif len(runs) + len(contexts) > 1:
            entry["reason"] = "duplicate_or_unbound_CI_identity"
        result["checks"].append(entry)
    result["decision"] = next((d for d in ("failed", "pending", "unknown", "missing")
                               if d in decisions), "passed")
    return result
