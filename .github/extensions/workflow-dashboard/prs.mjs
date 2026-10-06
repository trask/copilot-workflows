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
        else if (cached.facts.head_sha !== pr.head.sha ||
            cached.facts.is_draft !== pr.draft ||
            typeof cached.facts.author !== "string" ||
            cached.facts.author.toLowerCase() !== pr.user.login.toLowerCase()) status = "stale";
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

export function actionBlock(pr, viewer, phase, ready, dispatch) {
    if (!ready) return "Refresh successfully before running a task.";
    if (!viewer || viewer.id !== LAUNCH_OWNER_ID) return "The workflows only accept the configured personal owner's dispatch.";
    if (dispatch) return dispatch.message;
    if (!pr.headAvailable) return "The PR head repository is unavailable.";
    if (phase?.historical || phase?.unknownStage) return "Unsupported checkpoint. Inspect its saved evidence.";
    if (phase && ["active", "waiting"].includes(phase.category)) return "A task is already active on this PR.";
    return null;
}

export function taskPresentation(pr, kind, workflowReady, actions = []) {
    const phase = pr.phase?.kind === kind ? pr.phase : null;
    const dispatch = pr.dispatch && (pr.dispatch.kind === kind ||
        pr.dispatch.operation === "cancel" && phase) ? pr.dispatch : null;
    const disabled = Boolean(!workflowReady || pr.actionBlock || pr.dispatch) || !pr.tasks.includes(kind);
    const result = (label, tone = "idle", busy = false) => ({
        label, tone, busy, disabled,
        detail: dispatch?.message ?? (phase ? phase.reason : null) ?? pr.actionBlock ?? TASK_EFFECTS[kind],
    });
    if (dispatch) {
        if (dispatch.status === "uncertain") return result("Dispatch uncertain", "attention");
        if (dispatch.operation === "cancel") return result("Cancelling", "active", true);
        return result(dispatch.status === "accepted" ? "Starting" : "Dispatching", "active", true);
    }
    if (!workflowReady) return result("Status unavailable", "unknown");
    if (!phase) return result(pr.tasks.includes(kind) ? "Run" : kind === "pr_review" ? "Unavailable" : "Own PRs only");
    if (phase.historical) return result("Historical", "unknown");
    if (phase.unknownStage) return result("Unknown state", "attention");
    if (phase.sha && phase.sha !== pr.sha) return result("Previous head", "unknown");
    const terminal = {
        clean: ["Clean", "complete"], complete: ["Completed", "complete"],
        blocked: ["Blocked", "attention"], failed: ["Failed", "attention"],
        exhausted: ["Budget exhausted", "attention"], cancelled: ["Cancelled", "idle"],
    };
    if (terminal[phase.stage]) return result(...terminal[phase.stage]);
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
