import { KIND_LABELS } from "./kinds.mjs";
import { filterPulls, taskPresentation, TASK_EFFECTS } from "./prs.mjs";

const $ = (id) => document.getElementById(id);
let state = null;
let busy = false;
let expanded = new Set();
const histories = new Map();
let inViewport = true;

function visible() {
    return !document.hidden && inViewport;
}

function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
}

function link(text, url) {
    if (!url || !/^https:\/\/github\.com\//.test(url)) return element("span", text, "muted");
    const node = element("a", text);
    node.href = url;
    node.target = "_blank";
    node.rel = "noopener noreferrer";
    return node;
}

function date(value) {
    return value ? new Date(value).toLocaleString() : "not recorded";
}

function short(value) {
    return typeof value === "string" ? value.slice(0, 8) : "not frozen";
}

function words(value) {
    return typeof value === "string" ? value.replaceAll("_", " ") : "not recorded";
}

async function api(path, method = "GET", input) {
    const response = await fetch(path, { method, cache: "no-store",
        ...(input ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) } : {}) });
    const value = await response.json();
    if (!response.ok) {
        if (Array.isArray(value.prs)) {
            state = value;
            render();
        }
        throw new Error(value.prError || value.error || `Canvas request failed with HTTP ${response.status}.`);
    }
    return value;
}

function error(message) {
    $("error").hidden = !message;
    $("error").textContent = message ?? "";
}

function render() {
    if (!state) return;
    for (const key of histories.keys()) if (!key.startsWith(`${state.snapshot}:`)) histories.delete(key);
    $("auto").checked = state.auto;
    $("pause").hidden = !state.pauseReason;
    $("pause").textContent = state.pauseReason ?? "";
    $("freshness").textContent = `PRs ${state.prError ? "stale, last loaded" : "loaded"} ${date(state.prLoadedAt)}. Workflow state ${state.error ? "stale, last loaded" : "loaded"} ${date(state.loadedAt)}.${state.viewer ? ` Signed in as ${state.viewer.login}.` : ""}`;
    const rate = state.rate;
    $("cost").textContent = `Last refresh: ${state.cost ?? 0} primary-counted reads. Session: ${state.metrics.requests} requests, ${state.metrics.cacheHits} unchanged cache hits. ${rate ? `${rate.remaining.toLocaleString()} / ${rate.limit.toLocaleString()} GitHub capacity left, resets ${date(rate.reset)}.` : ""} Refresh uses no model tokens.`;
    error([state.prError, state.error].filter(Boolean).join(" "));
    $("repo").replaceChildren();
    for (const repo of state.repositories) {
        const option = element("option", repo);
        option.value = repo;
        $("repo").append(option);
    }
    $("repo").value = state.repository;
    const warnings = [...state.prWarnings, ...state.warnings];
    $("warnings").hidden = !warnings.length;
    $("warnings").textContent = warnings.join(" ");
    const rows = filterPulls(state.prs, { mine: $("mine").checked, reviewers: $("reviewers").checked, search: $("search").value });
    $("pr-count").textContent = `${rows.length} / ${state.prs.length}`;
    const cards = $("prs");
    cards.replaceChildren();
    for (const pr of rows) cards.append(prCard(pr));
    if (!cards.children.length) cards.append(element("p", state.prLoadedAt ? "No open PRs match these filters." : "No successful open-PR snapshot yet.", "empty"));
    const actions = $("actions");
    actions.replaceChildren();
    $("actions-count").textContent = state.actions.length;
    for (const run of state.actions) {
        const row = element("article", null, "run");
        const heading = element("div", null, "row");
        heading.append(link(run.title || run.name, run.url), element("span", words(run.status), "badge active"));
        row.append(heading, element("p", `${run.name} · ${date(run.created)}${run.targets.length ? ` · ${run.targets.join(", ")}` : " · shared or unmatched run"}`, "muted"));
        actions.append(row);
    }
    if (!actions.children.length) actions.append(element("p", state.loadedAt ? "No repository Actions runs are currently in flight." : "Actions have not been loaded yet.", "empty"));
    const failures = $("failures");
    failures.replaceChildren();
    $("failures-count").textContent = state.failures.length;
    for (const run of state.failures) {
        const row = element("article", null, "run");
        const heading = element("div", null, "row");
        heading.append(link(run.title || run.name, run.url), element("span", "Failed", "badge attention"));
        row.append(heading, element("p", `${date(run.created)}${run.targets.length ? ` · ${run.targets.join(", ")}` : " · no matching task checkpoint"}`, "muted"));
        failures.append(row);
    }
    if (!failures.children.length) failures.append(element("p", state.loadedAt ? "No failed coordinator dispatches found." : "Coordinator failures have not been loaded yet.", "empty"));
}

function confirmAction(pr, kind, cancel = false) {
    const dialog = $("confirm");
    if (dialog.open) return Promise.resolve(false);
    $("confirm-title").textContent = cancel ? "Cancel current task" : `Run ${KIND_LABELS[kind]}`;
    $("confirm-target").textContent = `${pr.target} ${pr.title}`;
    $("confirm-effects").textContent = cancel
        ? `Cancel request ${pr.phase.requestId}, generation ${pr.phase.generation}. This does not undo published changes or guarantee termination of an already authorized effect.`
        : TASK_EFFECTS[kind];
    $("confirm-submit").textContent = cancel ? "Cancel task" : "Run task";
    dialog.returnValue = "";
    return new Promise((resolve) => {
        dialog.addEventListener("close", () => resolve(dialog.returnValue === "yes"), { once: true });
        dialog.showModal();
    });
}

async function taskAction(pr, kind, cancel = false) {
    if (!await confirmAction(pr, kind, cancel)) return;
    pr.dispatch = { operation: cancel ? "cancel" : "launch", kind, status: "pending",
        message: "Dispatching. Do not submit again." };
    const displayed = state.prs.find((item) => item.target === pr.target);
    if (displayed) displayed.dispatch = pr.dispatch;
    render();
    let failureMessage = null;
    try {
        const result = await api(cancel ? "/api/cancel" : "/api/launch", "POST", cancel
            ? { target: pr.target, requestId: pr.phase.requestId, generation: pr.phase.generation, confirmed: true }
            : { target: pr.target, kind, confirmed: true });
        pr.dispatch = { ...pr.dispatch, ...result };
    } catch (failure) {
        pr.dispatch = { ...pr.dispatch, status: "uncertain", message: failure.message };
        if (displayed) displayed.dispatch = pr.dispatch;
        failureMessage = failure.message;
    }
    try {
        state = await api("/api/state");
        render();
        if (failureMessage) error(failureMessage);
    } catch (failure) {
        render();
        error(failure.message);
    }
}

const TASK_ICONS = {
    copilot_review: "M4 4h16v12H9l-5 4V4m4 4h8m-8 4h5",
    self_review: "M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6l-8-3m-4 9 3 3 5-6",
    pr_conflict_resolver: "M6 7v10m12-10c0 5-12 2-12 10M6 3a2 2 0 1 0 0 4 2 2 0 0 0 0-4m12 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4M6 17a2 2 0 1 0 0 4 2 2 0 0 0 0-4",
    ci_fix: "m14 6 4 4 3-3c1 5-3 8-7 6l-7 7a2 2 0 0 1-3-3l7-7c-2-4 1-8 6-7l-3 3",
    pr_description: "M14 3H5v18h14V8l-5-5v5h5M8 12h8m-8 4h6",
    pr_simplify: "M4 6h16M7 12h10m-7 6h4",
    pr_review: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7m10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6",
    pr_consistency: "M4 4h6v6H4V4m10 0h6v6h-6V4M4 14h6v6H4v-6m10 0h6v6h-6v-6",
};

function taskIcon(kind) {
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({
        viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.5",
        "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false",
    })) icon.setAttribute(key, value);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", TASK_ICONS[kind]);
    icon.append(path);
    return icon;
}

function prCard(pr) {
    const card = element("article", null, "pr-card");
    const heading = element("div", null, "row");
    heading.append(link(`#${pr.number} ${pr.title}`, pr.url),
        element("span", pr.routeLabel, "badge"));
    if (pr.draft) heading.append(element("span", "Draft", "badge"));
    card.append(heading, element("p", `${pr.author}${pr.mine ? " · yours" : ""} · Head ${short(pr.sha)}${pr.waitingSince ? ` · Waiting since ${date(pr.waitingSince)}` : ""}`, "muted"));
    if (pr.dashboardStatus === "current") {
        card.append(element("p", `CI: ${pr.ciFailing ?? "unknown"} failing, ${pr.ciPending ?? "unknown"} pending. Conflicts: ${pr.conflicts}.`, "muted"));
        if (pr.reviewers.length) card.append(element("p", `Reviewers: ${pr.reviewers.map((item) =>
            `${item.login}${item.approved ? " (approved)" : item.feedback ? " (feedback)" : ""}`).join(", ")}`, "muted"));
    }
    if (pr.phase) {
        card.append(element("p", `${KIND_LABELS[pr.phase.kind] ?? pr.phase.kind}: ${words(pr.phase.stage)}${pr.phase.reason ? ` · ${pr.phase.reason}` : ""}`));
        if (pr.phase.runUrl) card.append(link("Current worker", pr.phase.runUrl));
        if (pr.phase.coordinatorUrl) card.append(link("Coordinator", pr.phase.coordinatorUrl));
    } else card.append(element("p", state.workflowReady ? "No saved central task for this PR." : "Central task status is unavailable.", "muted"));
    const tasks = element("div", null, "task-grid");
    tasks.setAttribute("role", "group");
    tasks.setAttribute("aria-label", `Workflow tasks for ${pr.target}`);
    for (const [kind, label] of Object.entries(KIND_LABELS)) {
        const presentation = taskPresentation(pr, kind, state.workflowReady, state.actions);
        const button = element("button", null, "task-button");
        button.type = "button";
        button.disabled = presentation.disabled;
        button.title = !pr.tasks.includes(kind) && !presentation.busy
            ? `${presentation.label}. ${presentation.detail}` : presentation.detail;
        button.setAttribute("data-tone", presentation.tone);
        button.setAttribute("aria-label", `${label}: ${presentation.label}`);
        button.setAttribute("aria-busy", String(presentation.busy));
        const icon = element("span", null, "task-icon");
        icon.append(taskIcon(kind));
        const text = element("span", null, "task-text");
        const status = element("span", null, "task-status");
        const marker = element("span", null, presentation.busy ? "task-marker spinner" : "task-marker");
        marker.setAttribute("aria-hidden", "true");
        status.append(marker, element("span", presentation.label));
        text.append(element("span", label, "task-name"), status);
        button.append(icon, text);
        button.addEventListener("click", () => taskAction(pr, kind));
        tasks.append(button);
    }
    card.append(tasks);
    const controls = element("div", null, "controls");
    const cancel = element("button", "Cancel current task");
    cancel.disabled = !pr.canCancel;
    const message = element("p", pr.dispatch?.message ?? "", "notice");
    message.hidden = !pr.dispatch;
    cancel.addEventListener("click", () => taskAction(pr, pr.phase?.kind, true));
    if (pr.canCancel || pr.dispatch?.operation === "cancel") controls.append(cancel);
    controls.append(link("Central Actions", "https://github.com/trask/copilot-workflows/actions/workflows/coordinator.yml"));
    card.append(controls);
    if (pr.actionBlock && !pr.dispatch) card.append(element("p", pr.actionBlock, "muted"));
    card.append(message);
    if (pr.phase) card.append(phaseCard(pr.phase));
    return card;
}

function phaseCard(phase) {
    const card = element("details", null, "card");
    const summary = element("summary");
    summary.append(element("span", phase.target, "title"), document.createTextNode(" "),
        element("span", words(phase.stage), `badge ${phase.category}`));
    const meta = element("div", null, "meta");
    for (const value of [
        `${KIND_LABELS[phase.kind] ?? words(phase.kind)} · ${phase.mode}`, `${phase.iteration} / ${phase.maximum ?? "?"} workers`,
        `Head ${short(phase.sha)}`, `Started ${date(phase.started)}`,
        `Deadline ${date(phase.deadline)}${phase.deadline < Date.now() && ["active", "waiting"].includes(phase.category) ? " · overdue" : ""}`,
    ]) meta.append(element("span", value));
    summary.append(meta);
    if (phase.reason) summary.append(element("p", `${words(phase.reason)} (${phase.reason})`, "reason"));
    if (phase.historical) summary.append(element("p", "Historical protocol evidence. Not an active generic phase.", "reason"));
    if (phase.unknownStage) summary.append(element("p", "Unknown controller stage. Inspect its recorded evidence.", "reason"));
    const detail = element("div", null, "detail");
    const links = element("div", null, "links");
    links.append(link("Pull request", phase.url), link(`Head ${short(phase.sha)}`, phase.commitUrl));
    if (phase.runUrl) links.append(link("Current worker", phase.runUrl));
    if (phase.coordinatorUrl) links.append(link("Coordinator", phase.coordinatorUrl));
    detail.append(links);
    if (phase.error) detail.append(element("p", phase.error, "error"));
    const timeline = element("div");
    detail.append(timeline);
    card.append(summary, detail);
    const renderHistory = async () => {
        const key = `${state.snapshot}:${phase.target}`;
        if (!histories.has(key)) {
            timeline.replaceChildren(element("p", "Loading saved history...", "muted"));
            const pending = api(`/api/history?target=${encodeURIComponent(phase.target)}`);
            histories.set(key, pending);
        }
        try {
            const history = await histories.get(key);
            if (history.snapshot !== state.snapshot || !card.isConnected) return;
            timeline.replaceChildren();
            for (const message of history.warnings) timeline.append(element("p", message, "notice"));
            for (const item of history.phases) {
                timeline.append(element("h3", `${item.current ? "Current" : "Previous"} phase · ${words(item.stage)} · ${date(item.started)}`, "phase-heading"));
                if (item.gaps.length) timeline.append(element("p", `No saved worker evidence for consumed iteration(s) ${item.gaps.join(", ")}.`, "notice"));
                if (item.missingPublications.length) timeline.append(element("p", "Some published requests have no retained iteration snapshot. See recorded checkpoint evidence.", "notice"));
                for (const publication of item.orphanPublications) {
                    const saved = element("p", null, "links");
                    saved.append(publication.effect === "push"
                        ? link(`Recorded publication ${short(publication.sha)}`, publication.url)
                        : element("span", "Recorded no-change publication. No commit pushed."));
                    saved.append(element("span", date(publication.confirmed), "muted"));
                    timeline.append(saved);
                }
                if (item.reason) timeline.append(element("p", item.reason, "muted"));
                const list = element("div", null, "timeline");
                for (const iteration of item.iterations) list.append(iterationCard(iteration));
                for (const transition of item.transitions) list.append(element("p", `${words(transition.stage)} · ${transition.reason ?? "No worker dispatched"} · ${date(transition.frozen)}`, "muted"));
                timeline.append(list);
            }
        } catch (failure) {
            histories.delete(key);
            timeline.replaceChildren(element("p", failure.message, "error"));
            const retry = element("button", "Retry history");
            retry.addEventListener("click", renderHistory);
            timeline.append(retry);
        }
    };
    card.addEventListener("toggle", () => {
        if (card.open) {
            expanded.add(phase.id);
            void renderHistory();
        } else expanded.delete(phase.id);
    });
    if (expanded.has(phase.id)) card.open = true;
    return card;
}

function iterationCard(item) {
    const node = element("article", null, "iteration");
    node.append(element("h3", `Iteration ${item.number} · ${words(item.outcome ?? item.stage)}`));
    node.append(element("p", `Frozen ${date(item.frozen)} · Dispatched ${date(item.dispatched)} · Worker ${item.workerConclusion ?? "not complete or not recorded"}`, "meta"));
    const links = element("div", null, "links");
    links.append(link(`Input ${short(item.inputSha)}`, item.inputUrl));
    if (item.workerUrl) links.append(link("Worker + diagnostics", item.workerUrl));
    if (item.verifierUrl) links.append(link("Structural verifier", item.verifierUrl));
    if (item.coordinatorUrl) links.append(link("Coordinator", item.coordinatorUrl));
    node.append(links, element("p", `Verification: ${words(item.verification)}. Worker diagnostics are not independent proof of passing tests.`, "muted"));
    if (item.publication) {
        const published = element("div", null, "links");
        if (item.publication.effect === "push") published.append(link(`Published ${short(item.publication.sha)}`, item.publication.url), link("Compare changes", item.publication.compareUrl));
        else published.append(element("span", "No change. No commit pushed."));
        published.append(element("span", date(item.publication.confirmed), "muted"));
        node.append(published);
        for (const commit of item.publication.commits ?? []) {
            const row = element("div", null, "links");
            row.append(link(`${short(commit.sha)} ${commit.subject}`, commit.url));
            if (commit.parents?.length === 2) {
                commit.parents.forEach((parent, index) => row.append(link(
                    `${index === 0 ? "Head" : "Base"} parent ${short(parent.sha)}`, parent.url)));
            }
            node.append(row);
        }
    } else if (item.candidateSha) node.append(element("p", `Candidate ${short(item.candidateSha)}. Publication not confirmed.`, "muted"));
    if (item.changedPaths.length) {
        const paths = element("ul", null, "paths");
        for (const path of item.changedPaths) {
            const li = element("li");
            li.append(element("code", path));
            paths.append(li);
        }
        node.append(paths);
    }
    if (item.reason) node.append(element("p", item.reason, "muted"));
    if (item.error) node.append(element("p", item.error, "error"));
    if (item.task) {
        node.append(element("p", `Task completion: ${words(item.task.outcome)}. This is not clean-review clearance.`));
    }
    if (item.proposal) {
        node.append(element("p", "Saved title/body proposal. Effect confirmation is recorded separately.", "muted"),
            element("p", item.proposal.title), element("pre", item.proposal.body));
    }
    if (item.taskEffect) {
        node.append(element("p", `${words(item.taskEffect.kind)}: ${words(item.taskEffect.status)}.`));
        if (item.taskEffect.reviewUrl) node.append(link("Pending review. Not submitted or approved.", item.taskEffect.reviewUrl));
    }
    for (const rerun of item.ciReruns ?? []) {
        node.append(link(`Failed-jobs rerun ${rerun.runId}, attempt ${rerun.attempt ?? "not confirmed"}, ${rerun.status}`, rerun.url));
    }
    for (const warning of item.ciWarnings ?? []) node.append(element("p", `Unrelated CI warning: ${warning.analysis}`, "notice"));
    for (const diagnosis of item.diagnoses ?? []) node.append(element("p", `${diagnosis.key}: ${diagnosis.decision}. ${diagnosis.analysis}`));
    for (const report of item.consistency ?? []) {
        node.append(element("p", `${report.path}: ${report.classification}. ${report.explanation}`),
            element("p", (report.citations ?? []).join("\n"), "muted"));
    }
    if (item.review) {
        const review = element("div", null, "links");
        review.append(link(`Review: ${words(item.review.decision)}`, item.review.url));
        item.review.inlineUrls.forEach((url, index) => review.append(link(`Finding ${index + 1}`, url)));
        node.append(review);
    }
    if (item.ci) {
        node.append(element("p", `Recorded CI: ${words(item.ci.decision)} at ${short(item.ci.sha)}`));
        for (const check of item.ci.checks) {
            const row = element("div", null, "check");
            row.append(element("span", check.name), element("code", check.decision));
            node.append(row);
        }
    }
    if (item.findings.length) {
        const details = element("details");
        details.append(element("summary", `${item.findings.length} frozen finding(s) and dispositions`));
        for (const finding of item.findings) {
            const row = element("div", null, "finding");
            row.append(link(finding.path ?? "Review finding", finding.url),
                element("p", finding.body, "finding-text"));
            if (finding.disposition) row.append(element("p", `${words(finding.disposition)}: ${finding.reason ?? ""}`));
            if (finding.upsides) row.append(element("p", `Upsides: ${finding.upsides}`));
            if (finding.downsides) row.append(element("p", `Downsides: ${finding.downsides}`));
            if (finding.commitUrl) row.append(link("Published fix", finding.commitUrl));
            for (const effect of finding.effects ?? []) {
                row.append(element("p", `Thread ${words(effect.status)}. Reply ${words(effect.reply)}. Resolution ${words(effect.resolution)}.`));
                if (effect.reason) row.append(element("p", effect.reason, "muted"));
                if (effect.replyUrl) row.append(link("Confirmed reply", effect.replyUrl));
            }
            details.append(row);
        }
        node.append(details);
    }
    if (item.artifacts.length) {
        const details = element("details");
        details.append(element("summary", "Saved worker artifacts"));
        for (const artifact of item.artifacts) {
            const p = element("p", null, "links");
            p.append(link(`${artifact.name}${artifact.expired ? " · expired" : ""}`, artifact.url));
            details.append(p);
        }
        node.append(details);
    }
    return node;
}

async function refresh() {
    if (busy) return;
    busy = true;
    $("refresh").disabled = true;
    $("refresh").textContent = "Refreshing...";
    try {
        state = await api("/api/refresh", "POST");
        histories.clear();
        render();
    } catch (failure) {
        error(failure.message);
    } finally {
        busy = false;
        $("refresh").disabled = false;
        $("refresh").textContent = "Refresh";
    }
}

$("refresh").addEventListener("click", refresh);
$("auto").addEventListener("change", async () => {
    try {
        state = await api(`/api/auto?enabled=${$("auto").checked}`, "POST");
        render();
    } catch (failure) {
        $("auto").checked = state?.auto ?? false;
        error(failure.message);
    }
});
for (const id of ["mine", "reviewers", "search"]) $(id).addEventListener("input", render);
$("repo").addEventListener("change", async () => {
    if (busy) return;
    busy = true;
    $("repo").disabled = true;
    $("refresh").disabled = true;
    try {
        state = await api("/api/repository", "POST", { repo: $("repo").value });
        histories.clear();
        render();
    } catch (failure) {
        error(failure.message);
    } finally {
        busy = false;
        $("repo").disabled = false;
        $("refresh").disabled = false;
    }
});

async function heartbeat() {
    try {
        await api(`/api/visibility?visible=${visible()}`, "POST");
        if (!visible() || busy) return;
        const next = await api("/api/state");
        if (JSON.stringify(next) !== JSON.stringify(state)) {
            state = next;
            render();
        }
    } catch (failure) {
        error(failure.message);
    }
}

document.addEventListener("visibilitychange", heartbeat);
if (typeof IntersectionObserver !== "undefined") {
    new IntersectionObserver((entries) => {
        inViewport = entries.some((entry) => entry.isIntersecting);
        void heartbeat();
    }).observe(document.documentElement);
}
setInterval(heartbeat, 15000);
await heartbeat();
if (!state?.loadedAt) await refresh();
