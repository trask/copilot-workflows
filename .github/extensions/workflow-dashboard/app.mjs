import { KIND_LABELS } from "./kinds.mjs";
import { filterPulls, taskPresentation, completionPresentation, TASK_EFFECTS } from "./prs.mjs";

const $ = (id) => document.getElementById(id);
let state = null;
let busy = false;
let loadingRepository = null;
let stateVersion = 0;
let expanded = new Set();
const histories = new Map();
let inViewport = true;
let activeTooltip = null;
let tooltipId = 0;

function visible() {
    return !document.hidden && inViewport;
}

function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
}

function hideTooltip() {
    if (activeTooltip) activeTooltip.hidden = true;
    activeTooltip = null;
}

function taskControl(button, status, detail, action) {
    const control = element("span", null, "task-control");
    const tooltip = element("span", null, "task-tooltip");
    tooltip.id = `task-tooltip-${++tooltipId}`;
    tooltip.setAttribute("role", "tooltip");
    tooltip.hidden = true;
    if (status) tooltip.append(element("span", status, "tooltip-status"));
    if (detail) tooltip.append(element("span", detail, "tooltip-detail"));
    if (action) tooltip.append(element("span", action, "tooltip-action"));
    button.setAttribute("aria-describedby", tooltip.id);
    if (button.disabled) {
        control.tabIndex = 0;
        control.setAttribute("role", "group");
        control.setAttribute("aria-disabled", "true");
        control.setAttribute("aria-label", button.getAttribute("aria-label") ?? button.textContent);
        control.setAttribute("aria-describedby", tooltip.id);
    }
    control.append(button, tooltip);
    let hovered = false;
    const show = () => {
        hideTooltip();
        activeTooltip = tooltip;
        tooltip.hidden = false;
        const bounds = control.getBoundingClientRect();
        tooltip.style.left = "8px";
        tooltip.style.top = "8px";
        const { width, height } = tooltip.getBoundingClientRect();
        const viewport = document.documentElement;
        tooltip.style.left = `${Math.max(8, Math.min(bounds.left, viewport.clientWidth - width - 8))}px`;
        tooltip.style.top = `${bounds.bottom + height <= viewport.clientHeight - 8
            ? bounds.bottom : Math.max(8, bounds.top - height)}px`;
    };
    control.addEventListener("pointerenter", () => { hovered = true; show(); });
    control.addEventListener("pointerleave", () => {
        hovered = false;
        if (!control.contains(document.activeElement) && activeTooltip === tooltip) hideTooltip();
    });
    control.addEventListener("focusin", show);
    control.addEventListener("focusout", (event) => {
        if (!hovered && !control.contains(event.relatedTarget) && activeTooltip === tooltip) hideTooltip();
    });
    return control;
}

document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideTooltip();
});
document.addEventListener("scroll", (event) => {
    if (!activeTooltip?.contains(event.target)) hideTooltip();
}, true);
window.addEventListener("resize", hideTooltip);

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
    const version = stateVersion;
    const response = await fetch(path, { method, cache: "no-store",
        ...(input ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) } : {}) });
    const value = await response.json();
    if (!response.ok) {
        if (Array.isArray(value.prs) && version === stateVersion) {
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

function renderLoading() {
    const loading = busy || Boolean(state?.loading);
    $("loading").hidden = !loading;
    $("loading-message").textContent = loading
        ? `${loadingRepository ? "Loading" : "Refreshing"} ${loadingRepository ?? state?.repository ?? "GitHub data"}...`
        : "";
    $("prs").setAttribute("aria-busy", String(loading));
}

function setBusy(value, repository = null) {
    busy = value;
    loadingRepository = value ? repository : null;
    stateVersion++;
    for (const id of ["repo", "refresh", "auto"]) $(id).disabled = value;
    $("refresh").textContent = value ? repository ? "Loading..." : "Refreshing..." : "Refresh";
    renderLoading();
}

function renderPulls() {
    hideTooltip();
    const cards = $("prs");
    if (loadingRepository && (state.repository !== loadingRepository ||
        !state.prs.length && !state.prLoadedAt)) {
        $("pr-count").textContent = "Loading...";
        cards.replaceChildren(element("p", `Loading open PRs for ${loadingRepository}...`, "empty"));
        return;
    }
    const rows = filterPulls(state.prs, { mine: $("mine").checked, reviewers: $("reviewers").checked, search: $("search").value });
    $("pr-count").textContent = `${rows.length} / ${state.prs.length}`;
    cards.replaceChildren();
    for (const pr of rows) cards.append(prCard(pr));
    if (!cards.children.length) cards.append(element("p", state.prLoadedAt ? "No open PRs match these filters." : "No successful open-PR snapshot yet.", "empty"));
}

function render() {
    if (!state) return;
    for (const key of histories.keys()) if (!key.startsWith(`${state.snapshot}:`)) histories.delete(key);
    $("auto").checked = state.auto;
    $("pause").hidden = !state.pauseReason;
    $("pause").textContent = state.pauseReason ?? "";
    error([
        state.prError ? `Open PRs are ${state.prLoadedAt ? "stale" : "unavailable"}. ${state.prError}` : null,
        state.error ? `Workflow status is ${state.loadedAt ? "stale" : "unavailable"}. ${state.error}` : null,
    ].filter(Boolean).join(" "));
    $("repo").replaceChildren();
    for (const repo of state.repositories) {
        const option = element("option", repo);
        option.value = repo;
        $("repo").append(option);
    }
    $("repo").value = loadingRepository ?? state.repository;
    const warnings = [...state.prWarnings, ...state.warnings];
    $("warnings").hidden = !warnings.length;
    $("warnings").textContent = warnings.join(" ");
    renderLoading();
    renderPulls();
    renderTroubleshooting();
}

function renderTroubleshooting() {
    const runs = $("troubleshooting-runs");
    runs.replaceChildren();
    if (loadingRepository) {
        runs.append(element("p", `Loading runs for ${loadingRepository}...`, "empty"));
        return;
    }
    const entries = new Map(state.phases.filter((phase) => phase.target.startsWith(`${state.repository}#`))
        .map((phase) => [phase.target, { target: phase.target, url: phase.url, phase }]));
    for (const pr of state.prs) {
        if (pr.phase || pr.dispatch || pr.actionBlock) entries.set(pr.target, pr);
    }
    for (const pr of [...entries.values()].sort((a, b) =>
        Number(Boolean(b.dispatch)) - Number(Boolean(a.dispatch)) ||
        (b.phase?.started ?? 0) - (a.phase?.started ?? 0))) {
        const row = element("article", null, "run");
        const heading = element("div", null, "row");
        heading.append(link(pr.title ? `#${pr.number} ${pr.title}` : pr.target, pr.url));
        if (pr.phase) heading.append(element("span",
            `${KIND_LABELS[pr.phase.kind] ?? words(pr.phase.kind)} · ${words(pr.phase.stage)}`, "muted"));
        row.append(heading);
        if (pr.dispatch) {
            const message = element("p", pr.dispatch.message, "notice");
            if (pr.dispatch.runUrl) message.append(document.createTextNode(" "), link("View launch", pr.dispatch.runUrl));
            row.append(message);
        } else if (pr.actionBlock) row.append(element("p", pr.actionBlock, "muted"));
        if (pr.phase) row.append(phaseCard(pr.phase, pr.sha));
        runs.append(row);
    }
    if (!runs.children.length) runs.append(element("p", state.loadedAt
        ? "No saved runs for this repository." : "Runs have not been loaded yet.", "empty"));
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

async function taskAction(pr, kind, cancel = false) {
    const displayed = state.prs.find((item) => item.target === pr.target);
    const dispatch = displayed?.dispatch ?? pr.dispatch;
    const cancelDispatch = cancel && dispatch?.operation === "launch" && dispatch.status === "accepted" &&
        Boolean(displayed?.canCancelDispatch ?? pr.canCancelDispatch);
    if (dispatch && !cancelDispatch) {
        error(dispatch.message);
        return;
    }
    const cancellation = cancelDispatch
        ? { target: pr.target, runId: dispatch.runId, confirmed: true }
        : cancel ? { target: pr.target, requestId: pr.phase.requestId, generation: pr.phase.generation, confirmed: true } : null;
    pr.dispatch = { ...dispatch, operation: cancelDispatch ? "cancel_dispatch" : cancel ? "cancel" : "launch", kind, status: "pending",
        message: "Dispatching. Do not submit again." };
    if (displayed) displayed.dispatch = pr.dispatch;
    render();
    let failureMessage = null;
    try {
        const result = await api(cancel ? "/api/cancel" : "/api/launch", "POST", cancel
            ? cancellation
            : { target: pr.target, kind, confirmed: true });
        pr.dispatch = { ...pr.dispatch, ...result };
    } catch (failure) {
        pr.dispatch = { ...pr.dispatch, status: "uncertain", message: failure.message };
        if (displayed) displayed.dispatch = pr.dispatch;
        failureMessage = failure.message;
    }
    try {
        const version = stateVersion;
        const next = await api("/api/state");
        if (version === stateVersion) {
            state = next;
            render();
        }
        if (failureMessage) error(failureMessage);
    } catch (failure) {
        render();
        error(failure.message);
    }
}

function prCard(pr) {
    const card = element("article", null, "pr-card");
    const heading = element("div", null, "row pr-heading");
    heading.append(link(`#${pr.number} ${pr.title}`, pr.url));
    if (pr.draft) heading.append(element("span", "Draft", "badge muted"));
    if (pr.approved) {
        const badge = element("span", "Approved", "badge approved");
        badge.title = "At least one approver-team approval in the latest saved dashboard.";
        heading.append(badge);
    }
    if (!pr.mine) heading.append(element("span", `@${pr.author}`, "pr-author muted"));
    card.append(heading);
    const tasks = element("div", null, "task-grid");
    tasks.setAttribute("data-mine", String(pr.mine));
    tasks.setAttribute("role", "group");
    tasks.setAttribute("aria-label", `Workflow tasks for ${pr.target}`);
    for (const [kind, label] of Object.entries(KIND_LABELS)) {
        if (!pr.mine && kind !== "pr_review") continue;
        const presentation = taskPresentation(pr, kind, state.workflowReady, state.actions);
        const button = element("button", null, "task-button");
        button.type = "button";
        button.disabled = presentation.disabled;
        button.setAttribute("data-tone", presentation.tone);
        button.setAttribute("aria-label", `${label}: ${presentation.label}`);
        button.setAttribute("aria-description", presentation.detail);
        button.setAttribute("aria-busy", String(presentation.busy));
        if (presentation.busy) {
            const icon = element("span", null, "task-icon");
            icon.setAttribute("aria-hidden", "true");
            icon.append(element("span", null, "spinner"));
            button.append(icon);
        }
        button.append(element("span", label, "task-name"));
        button.addEventListener("click", () => taskAction(pr, kind));
        const detail = presentation.disabled && !presentation.busy && pr.actionBlock
            ? pr.actionBlock : presentation.detail === TASK_EFFECTS[kind] ? null : presentation.detail;
        tasks.append(taskControl(button, presentation.label === "Run" ? null : presentation.label,
            detail, presentation.disabled ? null : TASK_EFFECTS[kind]));
    }
    const cancelDispatch = pr.dispatch?.operation === "cancel_dispatch" ||
        pr.dispatch?.operation === "launch" && pr.dispatch.status === "accepted";
    const cancel = element("button", cancelDispatch ? "Cancel launch" : "Cancel task");
    cancel.type = "button";
    cancel.className = "task-button";
    cancel.disabled = cancelDispatch
        ? !pr.canCancelDispatch || pr.dispatch.operation !== "launch" || pr.dispatch.status !== "accepted"
        : !pr.canCancel || Boolean(pr.dispatch);
    cancel.addEventListener("click", () => taskAction(pr, pr.dispatch?.kind ?? pr.phase?.kind, true));
    if (pr.canCancel || cancelDispatch || pr.dispatch?.operation === "cancel") {
        const cancelling = ["cancel", "cancel_dispatch"].includes(pr.dispatch?.operation);
        const detail = cancel.disabled ? cancelling ? "Cancellation is awaiting confirmation."
            : cancelDispatch && !pr.dispatch.runId ? "Refresh to find this launch before cancelling."
                : "Cancellation is not available yet. Refresh to check its status." : null;
        tasks.append(taskControl(cancel, cancelling ? "Cancelling" : null, detail, cancel.disabled ? null
            : "Request cancellation. Published changes stay; changes already underway may still finish."));
    }
    card.append(tasks);
    if (pr.phase?.pendingReviewUrl && completionPresentation(pr.phase)) {
        const result = element("div", null, "run-result");
        result.append(link("Open pending review", pr.phase.pendingReviewUrl));
        card.append(result);
    }
    return card;
}

function phaseCard(phase, currentSha) {
    const card = element("details", null, "card");
    const summary = element("summary", "Run details");
    const detail = element("div", null, "detail");
    const meta = element("div", null, "meta");
    for (const value of [
        KIND_LABELS[phase.kind] ?? words(phase.kind),
        `Head ${short(phase.sha)}`, `Started ${date(phase.started)}`,
        ...(["active", "waiting"].includes(phase.category) && Number.isFinite(phase.deadline) ? [
            `Deadline ${date(phase.deadline)}${phase.deadline < Date.now() ? " · overdue" : ""}`,
        ] : []),
    ]) meta.append(element("span", value));
    detail.append(meta);
    const completion = completionPresentation(phase);
    if (completion) {
        if (currentSha && phase.sha && phase.sha !== currentSha) {
            detail.append(element("p", "Result from a previous PR commit.", "muted"));
        }
        detail.append(element("p", completion.detail));
    }
    if (phase.historical) detail.append(element("p", "Historical protocol evidence. Not an active generic phase.", "reason"));
    if (phase.unknownStage) detail.append(element("p", "Unknown controller stage. Inspect its recorded evidence.", "reason"));
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
                if (history.phases.length > 1) timeline.append(element("h3",
                    `${item.current ? "Latest" : "Previous"} run · ${KIND_LABELS[item.kind] ?? words(item.kind)} · ${date(item.started)}`, "phase-heading"));
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
                const list = element("div", null, "timeline");
                for (const iteration of item.iterations) list.append(iterationCard(iteration));
                for (const transition of item.transitions) list.append(element("p", `${words(transition.stage)} · ${transition.reason ?? "No worker dispatched"} · ${date(transition.frozen)}`, "muted"));
                timeline.append(list);
            }
        } catch (failure) {
            histories.delete(key);
            timeline.replaceChildren(element("p", failure.message, "error"));
            const retry = element("button", "Retry loading history");
            retry.addEventListener("click", renderHistory);
            timeline.append(retry);
        }
    };
    card.addEventListener("toggle", () => {
        if (card.open) {
            expanded.add(phase.id);
            if ($("troubleshooting").open) void renderHistory();
        } else expanded.delete(phase.id);
    });
    if (expanded.has(phase.id)) card.open = true;
    return card;
}

function iterationCard(item) {
    const node = element("article", null, "iteration");
    node.append(element("h3", `Pass ${item.number} · ${item.kind === "pr_review" && item.outcome === "no_change" ? "No findings" : words(item.outcome ?? item.stage)}`));
    node.append(element("p", `Started ${date(item.dispatched)} · Worker ${item.workerConclusion ?? "not complete or not recorded"}`, "meta"));
    const links = element("div", null, "links");
    links.append(link(`Input ${short(item.inputSha)}`, item.inputUrl));
    if (item.workerUrl) links.append(link("Worker log", item.workerUrl));
    if (item.verifierUrl) links.append(link("Output verification", item.verifierUrl));
    if (item.coordinatorUrl) links.append(link("Coordinator", item.coordinatorUrl));
    node.append(links, element("p", `Output verification: ${words(item.verification)}. This checks saved outputs, not review quality or passing tests.`, "muted"));
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
    if (item.reason) node.append(element("p", `Controller result: ${item.reason}`, "muted"));
    if (item.error) node.append(element("p", item.error, "error"));
    if (item.task && item.task.outcome !== item.outcome) {
        node.append(element("p", `Recorded outcome: ${words(item.task.outcome)}.`));
    }
    if (item.proposal) {
        node.append(element("p", "Saved title/body proposal. Effect confirmation is recorded separately.", "muted"),
            element("p", item.proposal.title), element("pre", item.proposal.body));
    }
    if (item.taskEffect) {
        node.append(element("p", `${words(item.taskEffect.kind)}: ${words(item.taskEffect.status)}.`));
        if (item.taskEffect.reviewUrl) node.append(link("Pending review", item.taskEffect.reviewUrl));
    }
    for (const comment of item.reviewComments ?? []) {
        const row = element("div", null, "finding");
        row.append(element("code", `${comment.path}:${comment.line}`),
            element("p", comment.body, "finding-text"));
        node.append(row);
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
        details.append(element("summary", `${item.findings.length} existing review comment${item.findings.length === 1 ? "" : "s"} and responses`));
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
        details.append(element("summary", "Downloads"));
        for (const artifact of item.artifacts) {
            const p = element("p", null, "links");
            p.append(link(`${artifact.name}${artifact.expired ? " · expired" : ""}`, artifact.url));
            details.append(p);
        }
        node.append(details);
    }
    return node;
}

async function load(path, repository = null) {
    if (busy) return;
    setBusy(true, repository);
    error(null);
    if (repository) {
        renderPulls();
        renderTroubleshooting();
    }
    const version = stateVersion;
    let polling = false;
    const timer = !state?.prLoadedAt || repository ? setInterval(async () => {
        if (polling || !visible()) return;
        polling = true;
        try {
            await readState(version);
        } catch (failure) {
            if (version === stateVersion) error(failure.message);
        } finally {
            polling = false;
        }
    }, 1000) : null;
    let failureMessage = null;
    try {
        state = await api(path, "POST", repository ? { repo: repository } : undefined);
        histories.clear();
    } catch (failure) {
        failureMessage = failure.message;
    } finally {
        if (timer !== null) clearInterval(timer);
        setBusy(false);
        render();
        if (failureMessage) error(failureMessage);
    }
}

function refresh() {
    return load("/api/refresh");
}

$("refresh").addEventListener("click", refresh);
$("troubleshooting").addEventListener("toggle", () => {
    if (state && $("troubleshooting").open) renderTroubleshooting();
});
$("auto").addEventListener("change", async () => {
    const version = stateVersion;
    try {
        const next = await api(`/api/auto?enabled=${$("auto").checked}`, "POST");
        if (version !== stateVersion) return;
        state = next;
        render();
    } catch (failure) {
        $("auto").checked = state?.auto ?? false;
        error(failure.message);
    }
});
for (const id of ["mine", "others", "reviewers", "search"]) $(id).addEventListener("input", render);
$("repo").addEventListener("change", () => load("/api/repository", $("repo").value));

async function readState(version = stateVersion) {
    const next = await api("/api/state");
    if (!visible() || version !== stateVersion ||
        loadingRepository && next.repository !== loadingRepository) return;
    if (JSON.stringify(next) !== JSON.stringify(state)) {
        state = next;
        render();
    }
}

async function heartbeat() {
    try {
        await api(`/api/visibility?visible=${visible()}`, "POST");
        if (!visible() || busy) return;
        await readState();
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
