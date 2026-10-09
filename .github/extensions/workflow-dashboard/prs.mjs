import { KIND_LABELS } from "./kinds.mjs";
import { LAUNCH_OWNER_ID } from "./repositories.mjs";

const SHA = /^[0-9a-f]{40}$/;
const SUBMITTED_REVIEWS = new Set(["COMMENTED", "APPROVED", "CHANGES_REQUESTED"]);
const copilot = (author) => author?.__typename === "Bot" && author.id === "BOT_kgDOCnlnWA";

export function currentCopilotReview(review, sha) {
    return copilot(review?.author) && SUBMITTED_REVIEWS.has(review.state) && review.commit?.oid === sha;
}

const ROUTES = {
    approver: "Waiting on reviewers", author: "Waiting on authors",
    maintainer: "Waiting on maintainers", "transient-failure": "Dashboard retrieval failed",
    unknown: "Unknown",
};

export const TASK_EFFECTS = {
    copilot_review: "Fix valid Copilot findings, push changes, and reply to and resolve bot threads. Repeat review until clean with passing CI.",
    self_review: "Review this PR and push fixes until review and CI are clean.",
    pr_conflict_resolver: "Merge the base branch into this PR and push the resolved merge. Does not merge the PR.",
    ci_fix: "Investigate failing checks and push fixes. May rerun failed jobs.",
    pr_description: "Update the PR title and description if needed.",
    pr_simplify: "Simplify this PR's code without changing behavior, then push changes.",
    pr_review: "Create a private GitHub review for you to inspect and submit.",
    pr_consistency: "Match this PR's code to nearby patterns and repository instructions, then push fixes.",
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
        draft: pr.draft, approved: count("approval_count") > 0,
        sha: pr.head.sha, headAvailable: Boolean(pr.head.repo),
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
    return Object.keys(KIND_LABELS).filter((kind) => kind === "pr_review" || pr.mine);
}

function hasCopilotBodyFeedback(body) {
    if (!body.trim()) return false;
    const summary = body.replaceAll("\r\n", "\n")
        .replace(/\n\n---\n\nGive feedback about Copilot approvals in \[this survey\]\(https:\/\/[^\s()<>]+\) to enter a drawing for a \$[0-9]+ gift card\.\n?$/, "")
        .replace(/\n\n---\n\n\u{1f4a1} <a [^<>\n]+>Add a `code-review` agent skill<\/a> or configure MCP servers for context-aware, tailored reviews\. <a [^<>\n]+>Learn more in the docs\.<\/a>\n?$/u, "")
        .replace(/\n\n\u{1f9e0} \*\*Review effort:\*\* Balanced\n?$/u, "")
        .replace(/\n\n<details>\n<summary><strong>What changed in this PR<\/strong><\/summary>\n\n(?:(?!<\/?details[>\s])[\s\S])+\n<\/details>(?=\n|$)/, "")
        .replace(/\n\n<details>\n<summary><strong>(?:Resolved since last review \([1-9][0-9]*\)|[1-9][0-9]* resolved since last review)<\/strong><\/summary>\n\n(?:- (?:<picture>(?:<source [^<>\n]+>)+<img [^<>\n]+><\/picture> )?\[[^\[\]<>\n]+\]\(#discussion_r[1-9][0-9]*\)\n)+<\/details>\n?$/, "");
    const heading = String.raw`### (?:\u{1f7e2} Approval recommended|\u{1f535} Needs a closer look)\n\n[^\n<>#*]+\n\n`;
    const noFindings = new RegExp(String.raw`^<!-- ccr-overview-v2 -->\n\n(?:${heading}\*\*0 open findings\*\*|## Copilot review overview\n\n${heading}\*\*Review effort:\*\* Balanced  \n\*\*Findings:\*\* None)\n?$`, "u");
    return !noFindings.test(summary);
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
    const latest = new Map();
    for (const check of checks) {
        const name = check?.__typename === "CheckRun" ? check.name : check?.__typename === "StatusContext" ? check.context : null;
        if (typeof name !== "string" || !name) throw new Error("Live CI status contains an unsupported check.");
        const run = check.checkSuite?.workflowRun;
        const sequence = check.__typename === "CheckRun" ? check.databaseId : Date.parse(check.createdAt);
        if (!Number.isSafeInteger(sequence) || sequence < 0 ||
            check.__typename === "CheckRun" && (sequence === 0 ||
                run != null && (!Number.isSafeInteger(run.runNumber) || run.runNumber < 1))) {
            throw new Error("Live CI check ordering is incomplete.");
        }
        const key = JSON.stringify([check.__typename, name,
            check.checkSuite?.app?.id ?? null, run?.workflow?.id ?? null, run?.event ?? null]);
        const previous = latest.get(key);
        const runNumber = run?.runNumber ?? 0;
        if (!previous || runNumber > previous.runNumber ||
            runNumber === previous.runNumber && sequence >= previous.sequence) {
            latest.set(key, { check, runNumber, sequence });
        }
    }
    for (const { check } of latest.values()) {
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
        if (!thread.isResolved && copilot(root.author) &&
            SUBMITTED_REVIEWS.has(root.pullRequestReview?.state)) copilotThreads++;
    }
    const reviews = detail.reviews;
    if (!Array.isArray(reviews?.nodes) || reviews.pageInfo?.hasNextPage !== false) {
        throw new Error("Live Copilot review-body status is incomplete.");
    }
    let copilotBodies = 0;
    for (const review of reviews.nodes) {
        if (!review || typeof review.state !== "string") throw new Error("Live review state is incomplete.");
        if (!currentCopilotReview(review, sha)) continue;
        if (!review.submittedAt || typeof review.body !== "string") {
            throw new Error("Live Copilot review body is incomplete.");
        }
        if (review.state === "CHANGES_REQUESTED" || hasCopilotBodyFeedback(review.body)) copilotBodies++;
    }
    return {
        sha, conflicts, copilotThreads, copilotBodies, failing, pending,
        ci: failing ? "failing" : pending ? "pending" : !total ? "none" : unknown ? "unknown" : "passing",
    };
}

export function actionEvidence(pr, kind) {
    if (!["pr_conflict_resolver", "ci_fix", "copilot_review"].includes(kind)) return null;
    const evidence = pr.evidence?.sha === pr.sha ? pr.evidence : null;
    const result = (label, detail, tone = "idle", disabled = false) => ({ label, detail, tone, disabled });
    if (!evidence || evidence.error) {
        const detail = evidence?.error ?? "Live action status is unavailable. Refresh to check whether this task is needed.";
        return result("Status unknown", kind === "ci_fix" ? `Refresh to check CI. ${detail}` : detail,
            "unknown", kind === "ci_fix");
    }
    if (kind === "pr_conflict_resolver") {
        if (evidence.conflicts === "yes") return result("Conflicts", "This PR has merge conflicts with its base branch.", "needed");
        if (evidence.conflicts === "no") return result("No conflicts", "No merge conflicts to resolve.", "idle", true);
        return result("Status unknown", "GitHub has not determined whether this PR has file conflicts.", "unknown");
    }
    if (kind === "ci_fix") {
        if (evidence.ci === "failing") return result("CI failing",
            `${evidence.failing} failing ${evidence.failing === 1 ? "check" : "checks"} on the latest PR commit.`, "needed");
        if (evidence.ci === "passing") return result("CI passing", "CI passed for the latest PR commit. Nothing to fix.", "idle", true);
        if (evidence.ci === "pending") return result("CI pending", "CI is still running. No failing checks yet.", "idle", true);
        if (evidence.ci === "none") return result("No CI results", "No CI results yet for the latest PR commit.", "idle", true);
        return result("Status unknown", "CI has an unknown result. Refresh to check CI before running a repair.", "unknown", true);
    }
    if (evidence.copilotThreads) return result("Open Copilot threads",
        `${evidence.copilotThreads} unresolved Copilot ${evidence.copilotThreads === 1 ? "thread" : "threads"}.`, "needed");
    if (!Number.isSafeInteger(evidence.copilotBodies) || evidence.copilotBodies < 0) {
        return result("Status unknown", "Live Copilot review-body status is unavailable. Refresh before deciding whether feedback needs attention.", "unknown");
    }
    if (evidence.copilotBodies) return result("Copilot review-body feedback",
        `${evidence.copilotBodies} Copilot ${evidence.copilotBodies === 1 ? "review" : "reviews"} with feedback on the latest PR commit.`, "needed");
    return result("No Copilot feedback",
        "No Copilot feedback to address. This does not mean the PR is approved.", "idle", true);
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
    const disabled = Boolean(!workflowReady || pr.actionBlock || pr.dispatch || evidence?.disabled) || !pr.tasks.includes(kind);
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
    if ((kind === "ci_fix" || phase.sha && phase.sha !== pr.sha) &&
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
