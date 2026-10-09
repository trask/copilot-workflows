import { KIND_LABELS } from "./kinds.mjs";
import { filterPulls, taskPresentation, completionPresentation, TASK_EFFECTS } from "./prs.mjs";

const $ = (id) => document.getElementById(id);
let state = null;
let busy = false;
let loadingRepository = null;
let stateVersion = 0;
let runLogRequest = null;
let inViewport = true;
let activeTooltip = null;
let tooltipId = 0;
let contextLink = null;

function visible() {
    return !document.hidden && inViewport;
}

function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
}

function taskIcon() {
    const icon = element("span", null, "task-icon");
    icon.setAttribute("aria-hidden", "true");
    icon.append(element("span", null, "spinner"));
    return icon;
}

function hideTooltip() {
    if (activeTooltip) activeTooltip.hidden = true;
    activeTooltip = null;
}

function hideLinkMenu(restoreFocus = false) {
    $("link-menu").hidden = true;
    if (restoreFocus && contextLink?.isConnected) contextLink.focus({ preventScroll: true });
    contextLink = null;
}

function showLinkMenu(event, node) {
    event.preventDefault();
    event.stopPropagation();
    hideTooltip();
    contextLink = node;
    const menu = $("link-menu");
    const dialog = $("task-details");
    (dialog.open && dialog.contains(node) ? dialog : document.body).append(menu);
    menu.hidden = false;
    const bounds = node.getBoundingClientRect();
    const { width, height } = menu.getBoundingClientRect();
    const viewport = document.documentElement;
    menu.style.left = `${Math.max(8, Math.min(event.clientX || bounds.left, viewport.clientWidth - width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(event.clientY || bounds.bottom, viewport.clientHeight - height - 8))}px`;
    $("copy-link").focus({ preventScroll: true });
}

async function copyLink(url) {
    // The canvas iframe's permissions policy can block the Clipboard API.
    const text = element("textarea", url, "clipboard-text");
    text.setAttribute("readonly", "");
    document.body.append(text);
    let copied;
    try {
        text.select();
        copied = document.execCommand?.("copy");
    } finally {
        text.remove();
    }
    if (!copied) {
        if (!navigator.clipboard?.writeText) throw new Error("Clipboard access is unavailable.");
        await navigator.clipboard.writeText(url);
    }
}

$("copy-link").addEventListener("click", async () => {
    const node = contextLink;
    if (!node) return;
    try {
        await copyLink(node.href);
    } catch (failure) {
        error(`Could not copy link. ${failure.message}`);
    } finally {
        hideLinkMenu();
        if (node.isConnected) node.focus({ preventScroll: true });
    }
});
document.addEventListener("pointerdown", (event) => {
    if (!$("link-menu").contains(event.target)) hideLinkMenu();
});
document.addEventListener("contextmenu", () => hideLinkMenu());
$("link-menu").addEventListener("focusout", (event) => {
    if (!$("link-menu").contains(event.relatedTarget)) hideLinkMenu();
});
window.addEventListener("blur", () => hideLinkMenu());

$("close-task-details").addEventListener("click", () => $("task-details").close());

function taskControl(button, status, detail, action, runUrl) {
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
        control.setAttribute("role", "button");
        control.setAttribute("aria-haspopup", "dialog");
        control.setAttribute("aria-label", `Show details for ${button.getAttribute("aria-label") ?? button.textContent}`);
        control.setAttribute("aria-describedby", tooltip.id);
        const showDetails = () => {
            hideTooltip();
            hideLinkMenu();
            $("task-details-title").textContent = button.querySelector(".task-name").textContent;
            $("task-details-status").textContent = status ?? "Unavailable";
            $("task-details-message").textContent = detail ?? "";
            $("task-details-links").replaceChildren();
            if (runUrl) $("task-details-links").append(link("Open workflow run", runUrl));
            $("task-details").showModal();
        };
        control.addEventListener("click", showDetails);
        control.addEventListener("keydown", (event) => {
            if (!["Enter", " "].includes(event.key)) return;
            event.preventDefault();
            showDetails();
        });
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
    if (event.key === "Escape") {
        hideTooltip();
        hideLinkMenu(true);
    }
});
document.addEventListener("scroll", (event) => {
    if (!activeTooltip?.contains(event.target)) hideTooltip();
    hideLinkMenu();
}, true);
window.addEventListener("resize", () => {
    hideTooltip();
    hideLinkMenu();
});

function link(text, url) {
    if (!url || !/^https:\/\/github\.com\//.test(url)) return element("span", text, "muted");
    const node = element("a", text);
    node.href = url;
    node.target = "_blank";
    node.rel = "noopener noreferrer";
    node.addEventListener("click", async (event) => {
        if (event.button !== 0 || !event.ctrlKey && !event.metaKey) return;
        event.preventDefault();
        event.stopPropagation();
        try {
            await api("/api/open-external", "POST", { url });
        } catch (failure) {
            error(failure.message);
        }
    }, true);
    node.addEventListener("contextmenu", (event) => showLinkMenu(event, node));
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
        throw new Error((path.startsWith("/api/run-log") ? value.runLogError : value.prError) ||
            value.error || `Canvas request failed with HTTP ${response.status}.`);
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
    hideLinkMenu();
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
    renderRunLog();
}

function renderRunLog() {
    const log = $("run-log");
    const switching = loadingRepository && state.repository !== loadingRepository;
    const loading = !switching && Boolean(state.runLogLoading || runLogRequest?.repository === state.repository);
    log.setAttribute("aria-busy", String(loading));
    $("load-run-log").disabled = loading || busy || Boolean(state.loading);
    $("run-log-hours").disabled = $("load-run-log").disabled;
    $("run-log-hours").value = state.runLogHours ?? 2;
    $("load-run-log").textContent = loading ? "Loading run log..."
        : state.runLogLoadedAt ? "Refresh run log" : "Load run log";
    $("run-log-error").hidden = switching || !state.runLogError;
    $("run-log-error").textContent = !switching && state.runLogError
        ? `Run log is ${state.runLogLoadedAt ? "stale" : "unavailable"}. ${state.runLogError}` : "";
    $("run-log-warnings").hidden = switching || !state.runLogWarnings?.length;
    $("run-log-warnings").textContent = switching ? "" : (state.runLogWarnings ?? []).join(" ");
    $("run-log-count").hidden = Boolean(switching) || !state.runLogLoadedAt;
    $("run-log-count").textContent = $("run-log-count").hidden ? "" : (state.runLog ?? []).length;
    log.replaceChildren();
    if (switching) {
        log.append(element("p", `Run log will load after PRs in ${loadingRepository}.`, "empty"));
        return;
    }
    for (const task of state.runLog ?? []) {
        const row = element("article", null, "run-log-entry");
        const heading = element("div", null, "row pr-heading");
        heading.append(link(`#${task.number} ${task.title ?? "Title unavailable"}`, task.url));
        const status = task.evidence ? completionPresentation(task)?.label ?? words(task.stage)
            : task.stage === "completed" ? `Launch ${words(task.conclusion)}` : `Launch ${words(task.stage)}`;
        const badge = element("span", status, `badge ${task.category}`);
        if (task.error || task.reason) badge.title = task.error ?? words(task.reason);
        heading.append(badge);
        const detail = element("div", null, "run-log-meta muted");
        detail.append(element("span", KIND_LABELS[task.kind] ?? words(task.kind)),
            element("span", `Finished ${date(task.finished)}`));
        for (const range of task.changeRanges) {
            if (range.url) {
                const changes = link(`Changes · ${range.commits} commit${range.commits === 1 ? "" : "s"}`, range.url);
                changes.title = `${short(range.base)}..${short(range.head)}`;
                detail.append(changes);
            } else {
                const pushed = link(`Pushed ${short(range.head)}`, range.commitUrl);
                pushed.title = "Commit-range boundary is not recorded.";
                detail.append(pushed);
            }
        }
        row.append(heading, detail);
        log.append(row);
    }
    if (!log.children.length) log.append(element("p", loading ? "Loading run log..." : state.runLogLoadedAt
        ? `No PR tasks finished in the past ${state.runLogHours ?? 2} hours.`
        : "Run log will load after the PRs.", "empty"));
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
        const actionLabel = presentation.actionLabel ?? label;
        const button = element("button", null, "task-button");
        button.type = "button";
        button.disabled = presentation.disabled;
        button.setAttribute("data-tone", presentation.tone);
        button.setAttribute("aria-label", `${actionLabel}: ${presentation.label}`);
        button.setAttribute("aria-description", presentation.detail);
        button.setAttribute("aria-busy", String(presentation.busy));
        if (presentation.busy) button.append(taskIcon());
        button.append(element("span", actionLabel, "task-name"));
        button.addEventListener("click", () => taskAction(pr, kind));
        const detail = presentation.disabled && !presentation.busy && pr.actionBlock
            ? pr.actionBlock : presentation.detail === TASK_EFFECTS[kind] ? null : presentation.detail;
        tasks.append(taskControl(button, presentation.label === "Run" ? null : presentation.label,
            detail, presentation.disabled || presentation.actionLabel ? null : TASK_EFFECTS[kind],
            presentation.tone === "attention"
                ? pr.dispatch?.runUrl ?? pr.phase?.coordinatorUrl ?? pr.phase?.runUrl : null));
    }
    const cancelDispatch = pr.dispatch?.operation === "cancel_dispatch" ||
        pr.dispatch?.operation === "launch" && pr.dispatch.status === "accepted";
    const cancelling = ["cancel", "cancel_dispatch"].includes(pr.dispatch?.operation);
    const cancellationBusy = cancelling && ["pending", "accepted"].includes(pr.dispatch.status);
    const cancelLabel = cancelDispatch ? "Cancel launch" : "Cancel task";
    const cancel = element("button", null, "task-button");
    cancel.type = "button";
    cancel.setAttribute("aria-label", cancellationBusy ? `${cancelLabel}: Cancelling` : cancelLabel);
    cancel.setAttribute("aria-busy", String(cancellationBusy));
    cancel.setAttribute("data-tone", cancellationBusy ? "active" : "idle");
    if (cancellationBusy) cancel.append(taskIcon());
    cancel.append(element("span", cancelLabel, "task-name"));
    cancel.disabled = cancelDispatch
        ? !pr.canCancelDispatch || pr.dispatch.operation !== "launch" || pr.dispatch.status !== "accepted"
        : !pr.canCancel || Boolean(pr.dispatch);
    cancel.addEventListener("click", () => taskAction(pr, pr.dispatch?.kind ?? pr.phase?.kind, true));
    if (pr.canCancel || cancelDispatch || pr.dispatch?.operation === "cancel") {
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

async function load(path, repository = null) {
    if (busy) return;
    setBusy(true, repository);
    error(null);
    if (repository) {
        renderPulls();
        renderRunLog();
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
    } catch (failure) {
        failureMessage = failure.message;
    } finally {
        if (timer !== null) clearInterval(timer);
        setBusy(false);
        render();
        if (failureMessage) error(failureMessage);
    }
    if (!failureMessage && !state?.runLogLoadedAt && !state?.runLogError) void loadRunLog();
}

function refresh() {
    return load("/api/refresh");
}

async function loadRunLog(hours = state?.runLogHours ?? 2) {
    if (!state?.loadedAt || !state.prLoadedAt || busy || state.loading || state.runLogLoading ||
        runLogRequest?.repository === state.repository) return;
    const request = { repository: state.repository, version: stateVersion };
    state = { ...state, runLogHours: hours };
    runLogRequest = request;
    renderRunLog();
    try {
        const next = await api(`/api/run-log?hours=${hours}`, "POST");
        if (request.version !== stateVersion || next.repository !== request.repository) return;
        state = next;
    } catch (failure) {
        if (request.version === stateVersion) state = { ...state, runLogError: failure.message };
    } finally {
        if (runLogRequest === request) runLogRequest = null;
        render();
    }
}

$("refresh").addEventListener("click", refresh);
$("load-run-log").addEventListener("click", () => loadRunLog());
$("run-log-hours").addEventListener("change", () => {
    const hours = Number($("run-log-hours").value);
    if (!Number.isSafeInteger(hours) || hours < 1) {
        state = { ...state, runLogError: "Run log hours must be a positive whole number." };
        renderRunLog();
        return;
    }
    void loadRunLog(hours);
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
if (!state?.loadedAt || !state?.prLoadedAt || state.loading) await refresh();
if (!state?.runLogLoadedAt && !state?.runLogError) void loadRunLog();
