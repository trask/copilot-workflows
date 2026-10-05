"""Attempt-bound CI diagnosis, without relaxing review-loop clearance."""

import re

from loop.api import APIError
from loop.policy import canonical, check_target, exact, require
from loop.reviews import check_decision, ci_items, copilot_check


def collect(api, request, required):
    sha, repo = request["frozen_sha"], request["repo"]
    check_target(api, request)
    checks, statuses = ci_items(api, repo, sha)
    executions, selected, failures = {}, [], []
    for check in checks:
        if check["name"] not in required or copilot_check(check):
            continue
        identity = None
        if check.get("app", {}).get("id") == 15368:
            match = re.fullmatch(r"https://github\.com/" + re.escape(repo)
                                + r"/actions/runs/([1-9][0-9]*)/job/([1-9][0-9]*)",
                                check.get("details_url") or "")
            require(match is not None, "Unbound Actions check URL")
            run_id, job_id = int(match[1]), int(match[2])
            if run_id not in executions:
                require(len(executions) < 100, "CI execution evidence exceeds limit")
                run = api.call(f"repos/{repo}/actions/runs/{run_id}")
                require(run["id"] == run_id and run["head_sha"] == sha
                        and run["repository"]["full_name"] == repo
                        and type(run["workflow_id"]) is int and run["workflow_id"] > 0
                        and re.fullmatch(r"\.github/workflows/[^/\\@?#]+\.ya?ml",
                                         run["path"].split("@")[0]) is not None
                        and type(run["run_attempt"]) is int and 1 <= run["run_attempt"] <= 100,
                        "CI run identity or attempt differs")
                jobs = api.pages(f"repos/{repo}/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs", "jobs")
                executions[run_id] = run, jobs
            run, jobs = executions[run_id]
            matched = [j for j in jobs if j["id"] == job_id]
            if not matched:
                if (check_decision(check, sha) != "passed" or run["run_attempt"] == 1
                        or any(j["name"] == check["name"] for j in jobs)):
                    continue
                previous = api.call(f"repos/{repo}/actions/jobs/{job_id}")
                require(type(previous["run_attempt"]) is int
                        and 1 <= previous["run_attempt"] < run["run_attempt"]
                        and previous["conclusion"] == "success",
                        "Unbound reused successful job")
                matched = [previous]
            job = matched[0]
            require(len(matched) == 1 and job["run_id"] == run_id
                    and 1 <= job["run_attempt"] <= run["run_attempt"] and job["id"] == job_id
                    and check.get("app", {}).get("slug") == "github-actions"
                    and job["name"] == check["name"]
                    and job["check_run_url"] == f"https://api.github.com/repos/{repo}/check-runs/{check['id']}"
                    and run["check_suite_id"] == check["check_suite"]["id"],
                    "CI job/check/attempt binding differs")
            identity = {"run_id": run_id, "attempt": run["run_attempt"], "job_id": job_id,
                        "job_attempt": job["run_attempt"],
                        "workflow_id": run["workflow_id"], "path": run["path"]}
        decision = check_decision(check, sha)
        item = {"name": check["name"], "id": check["id"], "decision": decision, "actions": identity}
        selected.append(item)
        if decision == "failed":
            output = check.get("output") or {}
            text = "\n".join(output.get(k) or "" for k in ("title", "summary", "text"))
            availability = "check_output" if text.strip() else "unavailable"
            if identity:
                try:
                    log = api.signed_download(f"repos/{repo}/actions/jobs/{identity['job_id']}/logs", 60000)
                except APIError as error:
                    require(error.status in {403, 404, 410}, "Failed to collect CI logs")
                    availability = "logs_unavailable_" + str(error.status)
                else:
                    text = log.decode("utf-8")
                    availability = "job_log"
            require(len(text.encode("utf-8")) <= 60000, "CI evidence exceeds limit")
            failures.append({"key": "check:" + str(check["id"]), **item,
                             "availability": availability, "evidence": text})
    for status in statuses:
        if status["context"] not in required:
            continue
        decision = {"success": "passed", "pending": "pending", "failure": "failed",
                    "error": "failed"}.get(status["state"], "unknown")
        selected.append({"name": status["context"], "id": status["id"],
                         "decision": decision, "actions": None})
        if decision == "failed":
            failures.append({"key": "status:" + str(status["id"]),
                             "name": status["context"], "id": status["id"],
                             "decision": decision, "actions": None,
                             "availability": "status_description" if status.get("description") else "unavailable",
                             "evidence": status.get("description") or ""})
    names = [s["name"] for s in selected]
    decisions = [s["decision"] for s in selected]
    active_runs = api.pages(f"repos/{repo}/actions/runs?head_sha={sha}", "workflow_runs")
    require(len(active_runs) <= 100, "CI run collection exceeds limit")
    active = any(r["head_sha"] == sha
                 and r["repository"]["full_name"] == repo
                 and re.fullmatch(r"\.github/workflows/[^/\\@?#]+\.ya?ml",
                                  r["path"].split("@")[0]) is not None
                 and r["status"] in {"queued", "in_progress", "waiting", "pending", "requested"}
                 for r in active_runs)
    duplicate = any(names.count(n) > 1 and (
        any(not s["actions"] for s in selected if s["name"] == n)
        or len({s["actions"]["workflow_id"] for s in selected if s["name"] == n}) != names.count(n))
                    for n in set(names))
    decision = ("pending" if active or "pending" in decisions else "unknown" if duplicate or "unknown" in decisions
                else "missing" if set(required) - set(names) else "failed" if failures
                else "passed" if required else "none")
    result = {"sha": sha, "required": required, "checks": selected, "failures": failures,
              "runs": [{"id": r["id"], "attempt": r["run_attempt"], "status": r["status"],
                        "conclusion": r["conclusion"]} for r, _ in executions.values()],
              "decision": decision}
    require(len(canonical(result)) <= 120000, "Combined CI diagnosis evidence exceeds limit")
    check_target(api, request)
    return result


def diagnoses(value, request):
    evidence = request["ci_evidence"]
    require(evidence["decision"] not in {"pending", "missing", "unknown"},
            "Incomplete CI evidence cannot authorize diagnosis")
    items = value["diagnoses"]
    by_key = {f["key"]: f for f in evidence["failures"]}
    require(isinstance(items, list) and len(items) == len(by_key), "Every failed check needs diagnosis")
    seen, decisions = set(), set()
    from loop.candidates import prose
    for item in items:
        exact(item, {"key", "decision", "analysis", "evidence"})
        require(isinstance(item["key"], str) and item["key"] in by_key and item["key"] not in seen
                and item["decision"] in {"fix", "rerun", "unrelated", "unknown"}, "Invalid CI diagnosis")
        seen.add(item["key"])
        decisions.add(item["decision"])
        prose(item["analysis"], 2000)
        failure = by_key[item["key"]]
        require(isinstance(item["evidence"], list) and
                (0 if item["decision"] == "unknown" else 1) <= len(item["evidence"]) <= 5,
                "CI diagnosis requires frozen evidence quotations")
        for quote in item["evidence"]:
            prose(quote, 1000)
            require(quote in failure["evidence"], "CI evidence citation is not in the frozen logs")
        require(item["decision"] == "unknown" or failure["availability"] != "unavailable",
                "Missing failure evidence cannot clear CI")
        if item["decision"] == "rerun":
            require(failure["actions"] is not None and failure["actions"]["attempt"] == 1
                    and failure["actions"]["run_id"] == value["rerun_run"],
                    "Transient rerun is unbound or the run has already been retried")
    outcome = value["outcome"]
    require(outcome == "blocked" or "unknown" not in decisions, "Unknown CI diagnosis cannot clear")
    require(outcome != "fixes" or "fix" in decisions, "Code fix lacks PR-attributable failure")
    require(outcome != "no_change" or decisions <= {"unrelated"}, "Unresolved CI failure")
    require(outcome != "rerun" or decisions <= {"rerun", "unrelated"} and "rerun" in decisions,
            "Unsupported rerun recommendation")
    require((type(value["rerun_run"]) is int and value["rerun_run"] > 0) if outcome == "rerun"
            else value["rerun_run"] is None, "Unexpected rerun target")


def same_attempts(api, request):
    current = collect(api, request, request["ci_evidence"]["required"])
    require(current == request["ci_evidence"], "CI checks, runs, attempts or evidence changed")
    return current
