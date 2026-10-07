import { KIND_LABELS } from "./kinds.mjs";
import { LAUNCH_OWNER_ID } from "./repositories.mjs";

const SHA = /^[0-9a-f]{40}$/;
const ROUTES = {
    approver: "Waiting on reviewers", author: "Waiting on authors",
    maintainer: "Waiting on maintainers", "transient-failure": "Dashboard retrieval failed",
    unknown: "Unknown",
};

export const TASK_EFFECTS = {
    copilot_review: "Investigate Copilot findings and push warranted fixes. Reply to and resolve eligible bot threads, then request fresh review until clean with passing CI.",
    self_review: "Review the full PR and push warranted fixes in fresh passes until clean with passing CI.",
    pr_conflict_resolver: "Merge the live base into this PR and push the resolved merge. Never land the PR.",
    ci_fix: "Diagnose CI failures, push warranted repairs, and possibly request one evidence-based failed-jobs rerun.",
    pr_description: "Update the PR title and description only when needed.",
    pr_simplify: "Make one pass of major behavior-preserving simplifications and push qualifying changes.",
    pr_review: "Create a pending review on GitHub for warranted findings. Never submit or approve it.",
    pr_consistency: "Compare the PR with repository conventions and push fixes for avoidable differences.",
};

export function checkedPull(pr, repo) {
    if (!pr || !Number.isSafeInteger(pr.number) || pr.number < 1 || pr.number >= 100000000 ||
        pr.state !== "open" || pr.merged === true || typeof pr.title !== "string" ||
        typeof pr.draft !== "boolean" || typeof pr.user?.login !== "string" ||
        !Number.isSafeInteger(pr.user.id) || pr.user.id < 1 || !["User", "Bot"].includes(pr.user.type) ||
        !SHA.test(pr.head?.sha) || pr.base?.repo?.full_name?.toLowerCase() !== repo.toLowerCase()) {
        throw new Error("GitHub returned an invalid or no-longer-open PR.");
    }
    return pr;
}

export function normalizePull(pr, repo, dashboard, viewer) {
    checkedPull(pr, repo);
    const cached = dashboard?.prs[String(pr.number)];
    let status = "missing";
    let facts = null;
    let route = null;
    if (cached) {
        if (cached.pr_number !== pr.number ||
            cached.pr_url !== `https://github.com/${repo}/pull/${pr.number}` ||
            !Object.hasOwn(ROUTES, cached.route) || !cached.facts ||
            typeof cached.facts !== "object" || Array.isArray(cached.facts)) status = "invalid";
        else if (cached.failed === true || ["transient-failure", "unknown"].includes(cached.route)) status = "failed";
        else {
            status = "current";
            facts = cached.facts;
            route = cached.route;
        }
    } else if (pr.draft) status = "draft";
    const count = (key) => Number.isSafeInteger(facts?.[key]) && facts[key] >= 0 ? facts[key] : null;
    return {
        target: `${repo}#${pr.number}`, repo, number: pr.number, title: pr.title,
        url: `https://github.com/${repo}/pull/${pr.number}`, author: pr.user.login,
        authorId: pr.user.id, authorType: pr.user.type,
        mine: pr.user.login.toLowerCase() === viewer.login.toLowerCase(),
        draft: pr.draft, sha: pr.head.sha, headAvailable: Boolean(pr.head.repo),
        updated: pr.updated_at, dashboardStatus: status, route,
        routeLabel: route ? ROUTES[route] : pr.draft ? "Draft" : `Dashboard ${status}`,
        waitingSince: typeof facts?.waiting_since === "string" ? facts.waiting_since : null,
        reviewers: Array.isArray(facts?.reviewers) ? facts.reviewers.filter((item) =>
            item && typeof item.login === "string").map((item) => ({
            login: item.login, approved: item.approved === true || item.approved_non_team === true,
            feedback: item.changes_requested === true || item.open_thread === true || item.top_level_feedback === true,
        })) : [],
        conflicts: ["yes", "no", "unknown"].includes(facts?.conflicts) ? facts.conflicts : "unknown",
        ciFailing: count("ci_failing_count"), ciPending: count("ci_pending_count"),
    };
}

export function filterPulls(prs, { mine = true, reviewers = false, search = "" } = {}) {
    const query = search.trim().toLowerCase();
    return prs.filter((pr) => pr.mine === mine &&
        (!reviewers || !pr.draft && pr.dashboardStatus === "current" && pr.route === "approver") &&
        [pr.title, pr.target, pr.author].join(" ").toLowerCase().includes(query));
}

export function taskChoices(pr, viewer) {
    if (!viewer || viewer.id !== LAUNCH_OWNER_ID) return [];
    return Object.keys(KIND_LABELS).filter((kind) => kind === "pr_review" ||
        pr.authorType === "User" && pr.mine && pr.authorId === viewer.id);
}

export function normalizeEvidence(detail, sha) {
    if (detail?.headRefOid !== sha || !Object.hasOwn(detail, "mergeRequirements") ||
        detail.mergeRequirements !== null && !Array.isArray(detail.mergeRequirements?.conditions)) {
        throw new Error("Live conflict status is incomplete or belongs to a different head.");
    }
    const conditions = detail.mergeRequirements?.conditions.filter((item) =>
        item?.__typename === "PullRequestMergeConflictStateCondition") ?? [];
    if (conditions.length > 1) throw new Error("Live conflict status contains duplicate file-conflict conditions.");
    const conflicts = { FAILED: "yes", PASSED: "no" }[conditions[0]?.result] ?? "unknown";
    const commit = detail.commits?.nodes?.[0]?.commit;
    if (detail.commits?.nodes?.length !== 1 || commit?.oid !== sha ||
        !Object.hasOwn(commit, "statusCheckRollup")) throw new Error("Live CI status does not match the PR head.");
    const rollup = commit.statusCheckRollup;
    const checks = rollup === null ? [] : rollup?.contexts?.nodes;
    if (!Array.isArray(checks) || rollup !== null && rollup.contexts?.pageInfo?.hasNextPage !== false) {
        throw new Error("Live CI status is incomplete.");
    }
    let failing = 0;
    let pending = 0;
    let total = 0;
    let unknown = false;
    for (const check of checks) {
        const name = check?.__typename === "CheckRun" ? check.name : check?.__typename === "StatusContext" ? check.context : null;
        if (typeof name !== "string" || !name) throw new Error("Live CI status contains an unsupported check.");
        if (/copilot/i.test(name) || check.checkSuite?.app?.slug === "copilot-pull-request-reviewer") continue;
        total++;
        if (check.__typename === "CheckRun") {
            if (["QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "PENDING"].includes(check.status)) pending++;
            else if (check.status !== "COMPLETED") unknown = true;
            else if (["FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"].includes(check.conclusion)) failing++;
            else if (!["SUCCESS", "NEUTRAL", "SKIPPED"].includes(check.conclusion)) unknown = true;
        } else if (["ERROR", "FAILURE"].includes(check.state)) failing++;
        else if (["EXPECTED", "PENDING"].includes(check.state)) pending++;
        else if (check.state !== "SUCCESS") unknown = true;
    }
    const threads = detail.reviewThreads;
    if (!Array.isArray(threads?.nodes) || threads.pageInfo?.hasNextPage !== false) {
        throw new Error("Live Copilot thread status is incomplete.");
    }
    let copilotThreads = 0;
    for (const thread of threads.nodes) {
        if (typeof thread?.isResolved !== "boolean" || typeof thread.isOutdated !== "boolean" ||
            !Array.isArray(thread.comments?.nodes) || thread.comments.nodes.length !== 1) {
            throw new Error("Live review thread contains incomplete root-comment data.");
        }
        const root = thread.comments.nodes[0];
        if (!thread.isResolved && !thread.isOutdated && root.author?.__typename === "Bot" &&
            root.author.login?.toLowerCase().replace(/\[bot\]$/, "") === "copilot-pull-request-reviewer" &&
            ["COMMENTED", "APPROVED", "CHANGES_REQUESTED"].includes(root.pullRequestReview?.state)) copilotThreads++;
    }
    return {
        sha, conflicts, copilotThreads, failing, pending,
        ci: failing ? "failing" : pending ? "pending" : total && !unknown ? "passing" : "unknown",
    };
}

export function actionEvidence(pr, kind) {
    if (!["pr_conflict_resolver", "ci_fix", "copilot_review"].includes(kind)) return null;
    const evidence = pr.evidence?.sha === pr.sha ? pr.evidence : null;
    const result = (label, detail, tone = "idle", unnecessary = false) => ({ label, detail, tone, unnecessary });
    if (!evidence || evidence.error) return result("Status unknown",
        evidence?.error ?? "Live action status is unavailable. Refresh to check whether this task is needed.", "unknown");
    if (kind === "pr_conflict_resolver") {
        if (evidence.conflicts === "yes") return result("Conflicts", "GitHub confirms file conflicts with the PR base.", "needed");
        if (evidence.conflicts === "no") return result("No conflicts", "GitHub confirms no file conflicts. Conflict resolution is unnecessary.", "idle", true);
        return result("Status unknown", "GitHub has not determined whether this PR has file conflicts.", "unknown");
    }
    if (kind === "ci_fix") {
        if (evidence.ci === "failing") return result("CI failing", `${evidence.failing} current-head CI check(s) failed.`, "needed");
        if (evidence.ci === "passing") return result("CI passing", "Current-head CI has passed. CI repair is unnecessary.", "idle", true);
        if (evidence.ci === "pending") return result("CI pending", "Current-head CI is still running. No failed checks are currently detected.");
        return result("Status unknown", "Current-head CI is absent or has an unknown result.", "unknown");
    }
    if (evidence.copilotThreads) return result("Open Copilot threads",
        `${evidence.copilotThreads} unresolved, non-outdated Copilot review thread(s).`, "needed");
    return result("Run", "No open Copilot-rooted threads detected. This is not proof of review clearance.");
}

export function actionBlock(pr, viewer, phase, ready, dispatch) {
    if (!ready) return "Refresh successfully before running a task.";
    if (!viewer || viewer.id !== LAUNCH_OWNER_ID) return "The workflows only accept the configured personal owner's dispatch.";
    if (dispatch) return dispatch.message;
    if (!pr.headAvailable) return "The PR head repository is unavailable.";
    if (phase?.historical || phase?.unknownStage) return "Unsupported checkpoint. Inspect its saved evidence.";
    if (phase && ["active", "waiting"].includes(phase.category)) return "A task is already active on this PR.";
    return null;
}

export function completionPresentation(phase) {
    if (phase.historical || phase.unknownStage || !["complete", "clean"].includes(phase.stage)) return null;
    if (phase.stage === "clean") return { label: "Clean", detail: "Review is clean and CI passed for the recorded commit." };
    if (phase.kind === "pr_review") {
        if (phase.outcome === "no_change") return {
            label: "No findings", detail: "No new findings. No pending review was created.",
        };
        if (phase.outcome === "pending_review" && phase.pendingReviewUrl) return {
            label: "Review ready",
            detail: phase.reviewCommentCount == null
                ? "A pending GitHub review was created. Only you can see it until you submit it."
                : `${phase.reviewCommentCount} review comment${phase.reviewCommentCount === 1 ? "" : "s"} in a pending GitHub review. Only you can see it until you submit it.`,
        };
    }
    if (phase.outcome === "no_change") return { label: "No changes", detail: "No changes were published." };
    if (phase.outcome === "metadata_updated") return { label: "Updated", detail: "The PR title and description were updated." };
    if (phase.outcome === "CI_passed") return { label: "CI passed", detail: "CI passed for the recorded commit." };
    if (phase.outcome === "warnings_not_CI_clearance") return {
        label: "CI still failing", detail: "The remaining CI failures were classified as unrelated to this PR.",
    };
    return { label: "Completed", detail: "Task completed. See run details for the recorded result." };
}

export function taskPresentation(pr, kind, workflowReady, actions = []) {
    const phase = pr.phase?.kind === kind ? pr.phase : null;
    const dispatch = pr.dispatch && (pr.dispatch.kind === kind ||
        pr.dispatch.operation === "cancel" && phase) ? pr.dispatch : null;
    const evidence = actionEvidence(pr, kind);
    const disabled = Boolean(!workflowReady || pr.actionBlock || pr.dispatch || evidence?.unnecessary) || !pr.tasks.includes(kind);
    const result = (label, tone = "idle", busy = false) => ({
        label, tone, busy, disabled,
        detail: dispatch?.message ?? (phase ? completionPresentation(phase)?.detail ?? phase.reason?.replaceAll("_", " ") : null) ??
            pr.actionBlock ?? TASK_EFFECTS[kind],
    });
    if (dispatch) {
        if (dispatch.status === "uncertain") return result("Dispatch uncertain", "attention");
        if (dispatch.status === "finished") return result(
            dispatch.conclusion === "failure" ? "Launch failed" : dispatch.conclusion === "cancelled" ? "Launch cancelled" : "Launch finished", "attention");
        if (dispatch.status === "failed") return result("Cancellation failed", "attention");
        if (["cancel", "cancel_dispatch"].includes(dispatch.operation)) return result("Cancelling", "active", true);
        return result(dispatch.status === "accepted" ? "Starting" : "Dispatching", "active", true);
    }
    if (!workflowReady) return result("Status unavailable", "unknown");
    const idle = () => evidence ? {
        ...result(evidence.label, evidence.tone), detail: pr.actionBlock ?? evidence.detail,
    } : result("Run");
    if (!phase) return pr.tasks.includes(kind) ? idle() : result(kind === "pr_review" ? "Unavailable" : "Own PRs only");
    if (phase.historical) return result("Historical", "unknown");
    if (phase.unknownStage) return result("Unknown state", "attention");
    if (phase.sha && phase.sha !== pr.sha &&
        ["clean", "complete", "blocked", "failed", "exhausted", "cancelled"].includes(phase.stage)) {
        return evidence ? idle() : result("Previous head", "unknown");
    }
    const completion = completionPresentation(phase);
    if (completion) {
        return evidence && (kind !== "copilot_review" || evidence.tone === "needed" || evidence.tone === "unknown")
            ? idle() : result(completion.label, "complete");
    }
    const terminal = {
        clean: ["Clean", "complete"], complete: ["Completed", "complete"],
        blocked: ["Blocked", "attention"], failed: ["Failed", "attention"],
        exhausted: ["Budget exhausted", "attention"], cancelled: ["Cancelled", "idle"],
    };
    if (terminal[phase.stage]) {
        const presentation = result(...terminal[phase.stage]);
        return evidence ? { ...presentation, detail: `${presentation.detail} ${evidence.detail}` } : presentation;
    }
    const waiting = {
        waiting_ci: "Waiting for CI", waiting_review: "Waiting for review",
        review_request_intent: "Requesting review", task_effect_intent: "Updating PR",
    };
    if (waiting[phase.stage]) return result(waiting[phase.stage], "waiting");
    const worker = actions.find((run) => run.id === phase.workerId);
    if (["dispatched", "running"].includes(phase.stage) &&
        ["queued", "pending", "requested", "waiting"].includes(worker?.status)) {
        return result("Queued", "active", true);
    }
    if (phase.stage === "dispatched") {
        return result(worker?.status === "in_progress" ? "Running" : "Starting", "active", true);
    }
    const active = {
        ready: "Starting", source_pending: "Preparing source", dispatch_intent: "Starting",
        running: "Running", verify_pending: "Verifying",
        publish_pending: "Preparing publication", publication_intent: "Publishing", published: "Continuing",
        thread_effects: "Updating threads", threads_settled: "Continuing",
    };
    if (active[phase.stage]) return result(active[phase.stage], "active", true);
    return result("Unknown state", "attention");
}
