import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { GitHub, CENTRAL, MAX_RESPONSE } from "./github.mjs";
import { PrDashboard, decodeDashboard } from "./pr-dashboard.mjs";
import { REPOSITORIES, DEFAULT_REPOSITORY, LAUNCH_OWNER_ID, dashboardPath, targetParts } from "./repositories.mjs";
import { actionBlock, filterPulls, normalizePull, taskChoices, taskPresentation, TASK_EFFECTS } from "./prs.mjs";
import { startServer } from "./server.mjs";
import { KIND_LABELS } from "./kinds.mjs";

const repo = DEFAULT_REPOSITORY;
const target = `${repo}#12`;
const sha = "a".repeat(40);
const id = "b".repeat(32);
const account = { id: LAUNCH_OWNER_ID, login: "trask" };
const pull = (changes = {}) => ({
    number: 12, state: "open", title: "Handle <untrusted> input", draft: false,
    user: { ...account, type: "User" }, head: { sha, repo: { full_name: repo } },
    base: { repo: { full_name: repo } }, ...changes,
});
const facts = (changes = {}) => ({
    head_sha: sha, author: "trask", is_draft: false, reviewers: [{ login: "reviewer" }],
    ci_pending_count: 1, ci_failing_count: 0, conflicts: "no", ...changes,
});
const cached = (changes = {}) => ({
    pr_number: 12, pr_url: `https://github.com/${repo}/pull/12`, route: "approver",
    failed: false, facts: facts(), ...changes,
});
const state = (record = cached()) => ({
    version: 18, prs: record ? { "12": record } : {}, draft_pr_numbers: [],
});
function file(value) {
    const data = Buffer.from(JSON.stringify(value));
    return { type: "file", encoding: "base64", content: data.toString("base64"), size: data.length,
        sha: createHash("sha1").update(`blob ${data.length}\0`).update(data).digest("hex") };
}
const checkpoint = (changes = {}, request = {}) => ({
    schema: 2, stage: "running", phase: id, generation: 3, iteration: 1, expected_sha: sha,
    request: {
        schema: 2, protocol: "reviewable-v1", repo, head_repo: repo, pr: 12,
        request_id: id, frozen_at: 1000, deadline: 8200, frozen_sha: sha,
        loop_kind: "self_review", authorized_actor_id: LAUNCH_OWNER_ID, launch_run: { id: 10 },
        ...request,
    }, ...changes,
});

function controller({ records = [], pulls = [pull()], dashboardState = state() } = {}) {
    const calls = [];
    let viewerAccount = account;
    let listing = pulls;
    let supplement = dashboardState;
    let currentRecords = records;
    const github = {
        requests: 0, counted: 0, cacheHits: 0, rate: null,
        get: async (path) => {
            calls.push(path); github.requests++; github.counted++;
            if (path === "user") return { data: viewerAccount };
            if (path === dashboardPath(repo) || REPOSITORIES.slice(1).some((r) => path === dashboardPath(r))) {
                if (supplement instanceof Error) throw supplement;
                return { data: file(supplement) };
            }
            const pr = listing.find((item) => path.endsWith(`/pulls/${item.number}`));
            if (!pr) throw new Error(`No live PR for ${path}`);
            return { data: structuredClone(pr) };
        },
        pulls: async (selected) => { calls.push(`list:${selected}`); github.requests++; github.counted++; return structuredClone(listing); },
        pages: async () => [],
        failedCoordinators: async () => [],
        dispatch: async (inputs) => { calls.push(inputs); return { status: "accepted" }; },
    };
    const canvas = new PrDashboard(github, () => 2000000);
    canvas.checkpoints = {
        snapshot: null,
        load: async () => {
            const snapshot = { sha, current: currentRecords.map((s) => ({ name: "pr-v2-1-12.json", state: s })) };
            canvas.checkpoints.snapshot = snapshot;
            return snapshot;
        },
        history: async (snapshot) => snapshot.current,
    };
    return {
        canvas, calls, github, setViewer: (v) => viewerAccount = v,
        setPulls: (v) => listing = v, setDashboard: (v) => supplement = v,
        setRecords: (v) => currentRecords = v,
    };
}

test("repository config is small, has the requested default, and bounds reads and targets", () => {
    assert.deepEqual(REPOSITORIES, [repo, "open-telemetry/semantic-conventions-conformance", "open-telemetry/shared-workflows"]);
    assert.match(dashboardPath(repo), /ref=otelbot%2Fpull-request-dashboard-state%2Fopentelemetry-java-instrumentation$/);
    assert.deepEqual(targetParts(target), { repo, number: 12 });
    for (const invalid of ["12", "other/repo#12", `${repo}#0`, `${repo}#12\n`]) assert.throws(() => targetParts(invalid));
    assert.throws(() => dashboardPath("other/repo"));
});

test("dashboard decoding validates shape, version, Git identity, UTF-8 and size", () => {
    assert.deepEqual(decodeDashboard(file(state())), state());
    for (const invalid of [
        { ...file(state()), sha: "c".repeat(40) }, { ...file(state()), size: 1024 * 1024 + 1 },
        file({ ...state(), version: 19 }), file({ ...state(), prs: [] }), file({ ...state(), draft_pr_numbers: null }),
        file({ ...state(), prs: { "../12": cached() } }),
    ]) assert.throws(() => decodeDashboard(invalid), /Dashboard/);
});

test("all open rows include drafts, bots and missing dashboard data; routing is exact and head-bound", () => {
    const matching = normalizePull(pull(), repo, state(), account);
    assert.equal(matching.mine, true);
    assert.equal(matching.routeLabel, "Waiting on reviewers");
    assert.equal(matching.ciFailing, 0);
    const draft = normalizePull(pull({ draft: true, number: 13 }), repo, state(), account);
    const missing = normalizePull(pull({ number: 14, user: { login: "bot", id: 21, type: "Bot" } }), repo, state(), account);
    assert.equal(draft.dashboardStatus, "draft");
    assert.equal(missing.dashboardStatus, "missing");
    assert.equal(filterPulls([matching, draft, missing]).length, 3);
    assert.deepEqual(filterPulls([matching, draft, missing], { reviewers: true }), [matching]);
    for (const record of [
        cached({ facts: facts({ head_sha: "d".repeat(40) }) }),
        cached({ facts: facts({ author: "different" }) }),
        cached({ facts: facts({ is_draft: true }) }),
        cached({ pr_number: 20 }), cached({ pr_url: "https://evil.test/" }), cached({ failed: true }),
        cached({ route: "unknown" }), cached({ route: "new-route" }),
    ]) {
        const pr = normalizePull(pull(), repo, state(record), account);
        assert.notEqual(pr.dashboardStatus, "current");
        assert.equal(filterPulls([pr], { reviewers: true }).length, 0);
    }
    for (const route of ["author", "maintainer"]) {
        const pr = normalizePull(pull(), repo, state(cached({ route })), account);
        assert.equal(filterPulls([pr], { reviewers: true }).length, 0);
    }
    assert.throws(() => normalizePull(pull({ state: "closed" }), repo, state(), account), /invalid/);
});

test("my-PR and reviewer filters compose and search is case-insensitive", () => {
    const mine = normalizePull(pull({ user: { ...account, login: "TrAsK", type: "User" } }), repo, state(), account);
    const other = normalizePull(pull({ user: { login: "someone", id: 22, type: "User" } }),
        repo, state(cached({ facts: facts({ author: "someone" }) })), account);
    assert.deepEqual(filterPulls([mine, other], { mine: true, reviewers: true, search: "UNTRUSTED" }), [mine]);
    assert.deepEqual(filterPulls([mine, other], { mine: true, search: "someone" }), []);
    assert.equal(filterPulls([mine, other], { reviewers: true }).length, 2);
    assert.deepEqual(taskChoices(mine, account), Object.keys(KIND_LABELS));
    assert.deepEqual(taskChoices(other, account), ["pr_review"]);
    assert.deepEqual(taskChoices(mine, { ...account, id: 99 }), []);
    assert.equal(Object.keys(TASK_EFFECTS).length, 8);
});

test("partial data keeps the complete live list and disables only unsafe task controls", async () => {
    const c = controller({ dashboardState: new Error("404 missing dashboard") });
    let result = await c.canvas.refresh();
    assert.equal(result.prs.length, 1);
    assert.equal(result.prs[0].actionBlock, null);
    assert.equal(result.auto, false);
    assert.match(result.prWarnings.join(" "), /404/);
    c.canvas.checkpoints.load = async () => { throw new Error("Central state unavailable"); };
    result = await c.canvas.refresh();
    assert.equal(result.prs.length, 1);
    assert.equal(result.workflowReady, false);
    assert.match(result.prs[0].actionBlock, /Refresh/);
    assert.match(result.error, /Central/);
});

test("failed live reads retain an explicitly stale snapshot and repository changes never leak old rows", async () => {
    const c = controller();
    await c.canvas.refresh();
    c.github.pulls = async () => { throw new Error("PR access denied"); };
    const failed = await c.canvas.refresh();
    assert.equal(failed.prs.length, 1);
    assert.match(failed.prError, /access denied/);
    assert.match(failed.prs[0].actionBlock, /Refresh/);
    const switched = await c.canvas.selectRepository(REPOSITORIES[1]);
    assert.equal(switched.repository, REPOSITORIES[1]);
    assert.equal(switched.prs.length, 0);
    await assert.rejects(c.canvas.selectRepository("other/repo"), /configured/);
});

test("Run rechecks live author and checkpoints, dispatches exactly one kind, and waits for execution evidence", async () => {
    const c = controller();
    await c.canvas.refresh();
    await assert.rejects(c.canvas.launch({ target, kind: "self_review", confirmed: false }), /Confirm/);
    const result = await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    assert.equal(result.status, "accepted");
    assert.deepEqual(c.calls.find((call) => typeof call === "object"), {
        operation: "launch", target, loop_kind: "self_review", publication_auth: "fine_grained_pat",
    });
    await assert.rejects(c.canvas.launch({ target, kind: "pr_description", confirmed: true }), /accepted/);
    assert.equal(c.canvas.state().prs[0].dispatch.status, "accepted");
    c.setRecords([checkpoint({}, { launch_run: { id: 20 } })]);
    assert.equal((await c.canvas.refresh()).prs[0].dispatch, null);
    assert.match(c.canvas.state().prs[0].actionBlock, /already active/);
    assert.equal(c.canvas.state().prs[0].canCancel, true);
});

test("preflight prevents active, historical, duplicate, unknown and wrong-author dispatches", async () => {
    for (const records of [
        [checkpoint()], [checkpoint({ schema: 1 })], [checkpoint({ stage: "new-stage" })],
        [checkpoint({ stage: "complete" }), checkpoint({ stage: "complete" })],
    ]) {
        const c = controller({ records });
        await c.canvas.refresh();
        await assert.rejects(c.canvas.launch({ target, kind: "self_review", confirmed: true }));
        assert.equal(c.calls.filter((call) => typeof call === "object").length, 0);
    }
    const c = controller();
    await c.canvas.refresh();
    c.setPulls([pull({ user: { login: "another", id: 99, type: "User" } })]);
    await assert.rejects(c.canvas.launch({ target, kind: "self_review", confirmed: true }), /not eligible/);
    await c.canvas.launch({ target, kind: "pr_review", confirmed: true });
    assert.equal(c.calls.find((call) => typeof call === "object").loop_kind, "pr_review");
    const denied = controller();
    await denied.canvas.refresh();
    denied.setViewer({ login: "another", id: 99 });
    await assert.rejects(denied.canvas.launch({ target, kind: "self_review", confirmed: true }), /personal owner/);
    await assert.rejects(denied.canvas.launch({ target, kind: "pr_review", confirmed: true }), /personal owner/);
});

test("PR Reviewer is enabled on the owner's PR and dispatches a review task", async () => {
    const c = controller();
    const state = await c.canvas.refresh();
    const presentation = taskPresentation(state.prs[0], "pr_review", state.workflowReady);
    assert.equal(presentation.label, "Run");
    assert.equal(presentation.disabled, false);
    await c.canvas.launch({ target, kind: "pr_review", confirmed: true });
    assert.deepEqual(c.calls.filter((call) => typeof call === "object"), [{
        operation: "launch", target, loop_kind: "pr_review", publication_auth: "fine_grained_pat",
    }]);
});

test("cancellation uses the exact rendered identity and rejects stale generations instead of adopting them", async () => {
    const c = controller({ records: [checkpoint()] });
    await c.canvas.refresh();
    c.setRecords([checkpoint({ generation: 4 })]);
    await assert.rejects(c.canvas.cancel({ target, requestId: id, generation: 3, confirmed: true }), /stale/);
    c.setRecords([checkpoint()]);
    await c.canvas.cancel({ target, requestId: id, generation: 3, confirmed: true });
    assert.deepEqual(c.calls.find((call) => typeof call === "object"), {
        operation: "cancel", target, previous_request: id, previous_generation: "3",
    });
    c.setRecords([checkpoint({ stage: "cancelled", generation: 4 })]);
    assert.equal((await c.canvas.refresh()).prs[0].dispatch, null);
    assert.equal(c.canvas.state().prs[0].canCancel, false);
});

test("duplicate calls coalesce no mutations and ambiguous outcomes cannot be blindly retried", async () => {
    const c = controller();
    await c.canvas.refresh();
    let release;
    let calls = 0;
    c.github.dispatch = async () => { calls++; await new Promise((resolve) => release = resolve); };
    const first = c.canvas.launch({ target, kind: "self_review", confirmed: true });
    await assert.rejects(c.canvas.launch({ target, kind: "self_review", confirmed: true }), /pending/);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(c.canvas.state().prs[0].dispatch.kind, "self_review");
    release();
    await first;
    assert.equal(calls, 1);
    const ambiguous = controller();
    await ambiguous.canvas.refresh();
    ambiguous.github.dispatch = async () => { const error = new Error("Uncertain dispatch"); error.uncertain = true; throw error; };
    await assert.rejects(ambiguous.canvas.launch({ target, kind: "self_review", confirmed: true }), /Uncertain/);
    assert.equal(ambiguous.canvas.state().prs[0].dispatch.status, "uncertain");
    await assert.rejects(ambiguous.canvas.launch({ target, kind: "self_review", confirmed: true }), /Uncertain/);
});

test("task button states distinguish dispatch acceptance, queued workers, execution and waiting", () => {
    const pr = {
        ...normalizePull(pull(), repo, state(), account), tasks: ["self_review", "pr_review"],
        actionBlock: "A task is already active", phase: {
            kind: "self_review", stage: "running", category: "active", sha, workerId: 42,
        },
    };
    assert.equal(taskPresentation(pr, "self_review", true).label, "Running");
    assert.equal(taskPresentation(pr, "self_review", true).busy, true);
    assert.equal(taskPresentation(pr, "self_review", true).disabled, true);
    assert.equal(taskPresentation(pr, "pr_simplify", true).busy, false);
    assert.equal(taskPresentation(pr, "self_review", true, [{ id: 42, status: "queued" }]).label, "Queued");
    assert.equal(taskPresentation(pr, "self_review", true, [{ id: 99, status: "queued" }]).label, "Running");
    const dispatched = { ...pr, phase: { ...pr.phase, stage: "dispatched" } };
    assert.equal(taskPresentation(dispatched, "self_review", true).label, "Starting");
    assert.equal(taskPresentation(dispatched, "self_review", true, [{ id: 42, status: "in_progress" }]).label, "Running");
    for (const [stage, label] of Object.entries({
        ready: "Starting", source_pending: "Preparing source", verify_pending: "Verifying",
        publication_intent: "Publishing", waiting_ci: "Waiting for CI", waiting_review: "Waiting for review",
        clean: "Clean", complete: "Completed", blocked: "Blocked", failed: "Failed",
        exhausted: "Budget exhausted", cancelled: "Cancelled", unknown: "Unknown state",
    })) {
        const presentation = taskPresentation({ ...pr, phase: { ...pr.phase, stage } }, "self_review", true);
        assert.equal(presentation.label, label);
        if (["waiting_ci", "waiting_review", "clean", "complete", "blocked", "failed", "cancelled"].includes(stage)) {
            assert.equal(presentation.busy, false);
        }
    }
    assert.equal(taskPresentation({ ...pr, phase: { ...pr.phase, historical: true } }, "self_review", true).label, "Historical");
    assert.equal(taskPresentation({ ...pr, phase: { ...pr.phase, sha: "f".repeat(40) } }, "self_review", true).label, "Previous head");
    assert.equal(taskPresentation(pr, "self_review", false).label, "Status unavailable");
    assert.equal(taskPresentation(pr, "self_review", false).busy, false);
    for (const [status, label] of Object.entries({ pending: "Dispatching", accepted: "Starting", uncertain: "Dispatch uncertain" })) {
        const presentation = taskPresentation({
            ...pr, phase: null, dispatch: { kind: "self_review", operation: "launch", status },
        }, "self_review", true);
        assert.equal(presentation.label, label);
        assert.equal(presentation.busy, status !== "uncertain");
        assert.equal(presentation.disabled, true);
    }
    assert.equal(taskPresentation({
        ...pr, dispatch: { operation: "cancel", status: "accepted" },
    }, "self_review", true).label, "Cancelling");
    assert.equal(taskPresentation({ ...pr, phase: null, actionBlock: null }, "self_review", true).label, "Run");
    assert.equal(taskPresentation({ ...pr, phase: null, actionBlock: null }, "pr_review", true).label, "Run");
    assert.equal(taskPresentation({ ...pr, tasks: [], phase: null, actionBlock: null }, "pr_review", true).label, "Unavailable");
});

function response(data, status = 200, headers = {}) {
    return { code: status >= 400 ? 1 : 0,
        stdout: `HTTP/2.0 ${status} OK\r\n${Object.entries(headers).map(([k, v]) => `${k}: ${v}\r\n`).join("")}\r\n${status === 204 ? "" : JSON.stringify(data)}` };
}

test("PR pagination completes all pages, deduplicates drift, and never follows another repo", async () => {
    let calls = 0;
    const github = new GitHub(async () => ++calls === 1
        ? response([pull()], 200, { Link: `<https://api.github.com/repos/${repo}/pulls?state=open&per_page=100&page=2>; rel="next"` })
        : response([pull(), pull({ number: 13 })]));
    assert.equal((await github.pulls(repo)).length, 2);
    assert.equal(calls, 2);
    for (const path of [`repos/${REPOSITORIES[1]}/pulls?state=open&per_page=100&page=2`, "user", `repos/${repo}/pulls?state=closed&page=2`]) {
        const wrong = new GitHub(async () => response([], 200, { Link: `<https://api.github.com/${path}>; rel="next"` }));
        await assert.rejects(wrong.pulls(repo), /repository|outside/);
    }
    const overflow = new GitHub(async () => response([], 200, {
        Link: `<https://api.github.com/repos/${repo}/pulls?state=open&per_page=100&page=2>; rel="next"`,
    }));
    await assert.rejects(overflow.pulls(repo), /10,000/);
});

test("GitHub writes are only central coordinator dispatches at main and never retry", async () => {
    const calls = [];
    const github = new GitHub(async (args) => { calls.push(args); return response(null, 204); });
    const inputs = { operation: "launch", target, loop_kind: "self_review", publication_auth: "fine_grained_pat" };
    assert.deepEqual(await github.dispatch(inputs), { status: "accepted" });
    assert.ok(calls[0].includes(`repos/${CENTRAL}/actions/workflows/coordinator.yml/dispatches`));
    assert.ok(calls[0].includes("ref=main"));
    assert.ok(calls[0].includes("inputs[loop_kind]=self_review"));
    assert.equal(calls[0].includes("GET"), false);
    for (const value of [
        { ...inputs, target: "other/repo#12" }, { ...inputs, operation: "tick" },
        { ...inputs, loop_kind: "bad" }, { ...inputs, publication_auth: "disabled" }, { ...inputs, ref: "evil" },
    ]) await assert.rejects(github.dispatch(value));
    assert.equal(calls.length, 1);
    for (const result of [response(null, 500), { code: 1, stdout: "" }]) {
        let count = 0;
        const uncertain = new GitHub(async () => { count++; return result; });
        await assert.rejects(uncertain.dispatch(inputs), (error) => error.uncertain === true);
        assert.equal(count, 1);
    }
    await assert.rejects(new GitHub(async () => response(null, 403)).dispatch(inputs), /rejected.*403/);
});

test("new reads are allowlisted and read cache does not apply to dispatch", async () => {
    const github = new GitHub(async () => response(account));
    await github.get("user");
    await github.get(dashboardPath(repo));
    await github.get(`repos/${repo}/pulls/12`);
    for (const path of ["repos/other/repo/pulls", `repos/${repo}/issues/12`,
        "repos/open-telemetry/shared-workflows/contents/README.md",
        `repos/${CENTRAL}/actions/workflows/coordinator.yml/dispatches`]) {
        assert.throws(() => github.get(path), /outside/);
    }
});

test("PR aggregate size and dispatch rate limits are enforced without a second mutation", async () => {
    const large = new GitHub();
    large.get = async () => ({ data: [{ number: 12, title: "x".repeat(MAX_RESPONSE) }] });
    await assert.rejects(large.pulls(repo), /16 MiB/);
    let calls = 0;
    const github = new GitHub(async () => {
        calls++;
        return response(null, 429, { "Retry-After": "60" });
    }, () => 1000000);
    const inputs = { operation: "launch", target, loop_kind: "self_review", publication_auth: "fine_grained_pat" };
    await assert.rejects(github.dispatch(inputs), (error) => error.rejected && error.retryAt === 1060000);
    await assert.rejects(github.dispatch(inputs), /rate-limit/);
    assert.equal(calls, 1);
});

test("loopback mutations enforce origin, JSON/body bounds, confirmation and exact target inputs", async (t) => {
    const c = controller();
    await c.canvas.refresh();
    const server = await startServer(c.canvas);
    t.after(() => server.close());
    const origin = new URL(server.url).origin;
    const launch = { target, kind: "self_review", confirmed: true };
    const post = (body, headers = {}) => fetch(new URL("api/launch", server.url), {
        method: "POST", headers: { Origin: origin, "Content-Type": "application/json", ...headers },
        body: typeof body === "string" ? body : JSON.stringify(body),
    });
    assert.equal((await post(launch, { Origin: "https://evil.test" })).status, 403);
    assert.equal((await post(launch, { Origin: "" })).status, 403);
    assert.equal((await post(launch, { "Content-Type": "text/plain" })).status, 400);
    assert.equal((await post("{bad")).status, 400);
    assert.equal((await post("x".repeat(4097))).status, 400);
    assert.equal((await post({ ...launch, confirmed: false })).status, 400);
    assert.equal((await post({ ...launch, extra: "ignored?" })).status, 400);
    assert.equal((await post({ ...launch, target: "other/repo#12" })).status, 400);
    assert.equal((await post(launch)).status, 200);
    assert.equal((await post(launch)).status, 400);
    assert.equal(c.calls.filter((call) => typeof call === "object").length, 1);
    assert.match(actionBlock(normalizePull(pull(), repo, state(), account), account, null, false), /Refresh/);
});

test("extension declares one compatible canvas with schema-guarded shared task handlers", async () => {
    const { canvas } = controller();
    let joined;
    const source = await readFile(new URL("extension.mjs", import.meta.url), "utf8");
    await runInNewContext(`(async () => { ${source.replace(/^import .+;\r?$/gm, "")} })()`, {
        PrDashboard: class { constructor() { return canvas; } },
        KIND_LABELS, REPOSITORIES, CanvasError: class extends Error {
            constructor(code, message) { super(message); this.code = code; }
        }, startServer,
        createCanvas: (value) => value, joinSession: async (value) => { joined = value; },
    });
    assert.equal(joined.canvases.length, 1);
    const declaration = joined.canvases[0];
    assert.equal(declaration.id, "workflow-dashboard");
    assert.equal(declaration.displayName, "PR workflows");
    assert.deepEqual(Array.from(declaration.actions, (item) => item.name),
        ["refresh", "select_repository", "launch", "cancel", "history"]);
    const launch = declaration.actions.find((item) => item.name === "launch");
    assert.equal(launch.inputSchema.properties.confirmed.const, true);
    assert.equal(launch.inputSchema.additionalProperties, false);
    await assert.rejects(launch.handler({ input: { target, kind: "self_review", confirmed: false } }), /Confirm/);
    assert.equal((await declaration.actions[0].handler()).prs.length, 1);
});
