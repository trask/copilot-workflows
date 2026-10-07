import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { GitHub, CENTRAL, MAX_RESPONSE, FAILED_COORDINATORS } from "./github.mjs";
import { PrDashboard, decodeDashboard } from "./pr-dashboard.mjs";
import { Checkpoints } from "./state.mjs";
import { REPOSITORIES, DEFAULT_REPOSITORY, LAUNCH_OWNER_ID, dashboardPath, targetParts } from "./repositories.mjs";
import { actionBlock, actionEvidence, filterPulls, normalizeEvidence, normalizePull, taskChoices, taskPresentation, TASK_EFFECTS } from "./prs.mjs";
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
const connection = (nodes = [], more = false) => ({ nodes, pageInfo: { hasNextPage: more, endCursor: more ? "next" : null } });
const check = (conclusion = "SUCCESS", changes = {}) => ({
    __typename: "CheckRun", name: "Tests", status: "COMPLETED", conclusion, ...changes,
});
const thread = (changes = {}) => ({
    isResolved: false, isOutdated: false,
    comments: { nodes: [{ author: { login: "copilot-pull-request-reviewer", __typename: "Bot" },
        pullRequestReview: { state: "COMMENTED" } }] }, ...changes,
});
function detail({ number = 12, head = sha, conflicts = "FAILED", checks = [], threads = [] } = {}) {
    return {
        number, headRefOid: head,
        mergeRequirements: { conditions: [{ __typename: "PullRequestMergeConflictStateCondition", result: conflicts }] },
        commits: { nodes: [{ commit: { oid: head, statusCheckRollup: { contexts: connection(checks) } } }] },
        reviewThreads: connection(threads),
    };
}
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
        pullEvidence: async (_selected, prs) => new Map(prs.map((pr) => [pr.number, {
            detail: detail({ number: pr.number, head: pr.sha }),
        }])),
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

test("file-conflict evidence ignores aggregate mergeability and other failed merge conditions", () => {
    const raw = detail({ conflicts: "PASSED" });
    raw.mergeable = "UNKNOWN";
    raw.mergeRequirements.conditions.push({ __typename: "PullRequestRulesCondition", result: "FAILED" });
    assert.equal(normalizeEvidence(raw, sha).conflicts, "no");
    raw.mergeRequirements.conditions[0].result = "FAILED";
    assert.equal(normalizeEvidence(raw, sha).conflicts, "yes");
    raw.mergeRequirements.conditions[0].result = "PENDING";
    assert.equal(normalizeEvidence(raw, sha).conflicts, "unknown");
    raw.mergeRequirements = null;
    assert.equal(normalizeEvidence(raw, sha).conflicts, "unknown");
    assert.throws(() => normalizeEvidence(raw, "f".repeat(40)), /different head/);
});

test("CI evidence distinguishes failures, pending and absent checks without counting Copilot checks", () => {
    const evidence = (checks) => normalizeEvidence(detail({ checks }), sha);
    assert.equal(evidence([check()]).ci, "passing");
    assert.equal(evidence([]).ci, "unknown");
    assert.equal(evidence([check(null, { status: "IN_PROGRESS" })]).ci, "pending");
    assert.equal(evidence([check("FAILURE"), check(null, { name: "Build", status: "IN_PROGRESS" })]).ci, "failing");
    assert.equal(evidence([check(), check("FAILURE", { name: "Copilot code review" })]).ci, "passing");
    assert.equal(evidence([{ __typename: "StatusContext", context: "Build", state: "ERROR" }]).ci, "failing");
    assert.equal(evidence([check(null)]).ci, "unknown");
    const incomplete = detail({ checks: [check()] });
    incomplete.commits.nodes[0].commit.statusCheckRollup.contexts.pageInfo.hasNextPage = true;
    assert.throws(() => normalizeEvidence(incomplete, sha), /incomplete/);
    incomplete.commits.nodes[0].commit.oid = "f".repeat(40);
    assert.throws(() => normalizeEvidence(incomplete, sha), /PR head/);
});

test("Copilot hints count only submitted, unresolved, non-outdated bot-rooted threads", () => {
    const human = thread({ comments: { nodes: [{
        author: { login: "reviewer", __typename: "User" }, pullRequestReview: { state: "COMMENTED" },
    }] } });
    const pending = thread({ comments: { nodes: [{
        author: { login: "copilot-pull-request-reviewer", __typename: "Bot" }, pullRequestReview: { state: "PENDING" },
    }] } });
    assert.equal(normalizeEvidence(detail({
        threads: [thread(), thread({ isResolved: true }), thread({ isOutdated: true }), human, pending],
    }), sha).copilotThreads, 1);
});

test("live action evidence overrides completed results without replacing active workflow state", () => {
    const pr = {
        ...normalizePull(pull(), repo, state(), account), tasks: Object.keys(KIND_LABELS), actionBlock: null,
        evidence: normalizeEvidence(detail({ checks: [check("FAILURE")], threads: [thread()] }), sha),
        phase: { kind: "pr_conflict_resolver", stage: "complete", sha },
    };
    assert.equal(taskPresentation(pr, "pr_conflict_resolver", true).tone, "needed");
    assert.equal(taskPresentation(pr, "ci_fix", true).label, "CI failing");
    assert.equal(taskPresentation(pr, "copilot_review", true).tone, "needed");
    pr.phase.stage = "running";
    assert.equal(taskPresentation(pr, "pr_conflict_resolver", true).label, "Running");
    assert.equal(taskPresentation(pr, "pr_conflict_resolver", true).tone, "active");
    pr.evidence = normalizeEvidence(detail({ conflicts: "PASSED", checks: [check()] }), sha);
    assert.equal(actionEvidence(pr, "pr_conflict_resolver").unnecessary, true);
    assert.equal(taskPresentation(pr, "ci_fix", true).disabled, true);
    assert.equal(taskPresentation(pr, "copilot_review", true).disabled, false);
    pr.evidence.sha = "f".repeat(40);
    assert.equal(taskPresentation(pr, "ci_fix", true).disabled, false);
    assert.equal(taskPresentation(pr, "ci_fix", true).label, "Status unknown");
});

test("failed live evidence remains unknown rather than falling back to saved no-conflict facts", async () => {
    const c = controller();
    c.github.pullEvidence = async () => { throw new Error("Live status unavailable"); };
    const result = await c.canvas.refresh();
    assert.equal(result.workflowReady, true);
    assert.equal(result.prs[0].actionBlock, null);
    assert.equal(result.prs[0].conflicts, "no");
    assert.equal(taskPresentation(result.prs[0], "pr_conflict_resolver", true).label, "Status unknown");
    assert.equal(taskPresentation(result.prs[0], "pr_conflict_resolver", true).disabled, false);
    assert.equal(result.auto, false);
    assert.match(result.prWarnings.join(" "), /Live status unavailable/);
});

test("fresh conflict and CI preflight rejects unnecessary tasks after an enabled snapshot", async () => {
    for (const kind of ["pr_conflict_resolver", "ci_fix"]) {
        const c = controller();
        await c.canvas.refresh();
        c.github.pullEvidence = async () => new Map([[12, { detail: detail({ conflicts: "PASSED", checks: [check()] }) }]]);
        await assert.rejects(c.canvas.launch({ target, kind, confirmed: true }), /unnecessary/);
        assert.equal(c.calls.filter((call) => typeof call === "object").length, 0);
        assert.equal(c.canvas.state().prs[0].dispatch, null);
    }
    const c = controller();
    c.github.pullEvidence = async () => new Map([[12, { detail: detail({ conflicts: "PENDING" }) }]]);
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "pr_conflict_resolver", confirmed: true });
    assert.equal(c.calls.find((call) => typeof call === "object").loop_kind, "pr_conflict_resolver");
});

test("live evidence batches PRs and paginates checks and threads before claiming clear", async () => {
    const queries = [];
    const first = detail({ checks: [check()] });
    first.commits.nodes[0].commit.statusCheckRollup.contexts.pageInfo = connection([], true).pageInfo;
    first.reviewThreads.pageInfo = connection([], true).pageInfo;
    first.reviewThreads.nodes = [thread({ isResolved: true })];
    const github = new GitHub(async (args) => {
        const query = args.find((arg) => arg.startsWith("query="));
        queries.push(query);
        if (!query.includes("after:")) return response({ data: { repository: { pr12: first, pr13: detail({ number: 13 }) } } });
        const more = query.includes("reviewThreads")
            ? { number: 12, headRefOid: sha, reviewThreads: connection([thread()]) }
            : detail({ checks: [check("FAILURE", { name: "Build" })] });
        return response({ data: { repository: { pullRequest: more } } });
    });
    const result = await github.pullEvidence(repo, [{ number: 12, sha }, { number: 13, sha }]);
    assert.equal(queries.length, 3);
    assert.ok(queries[0].includes("pr12:") && queries[0].includes("pr13:"));
    assert.equal(normalizeEvidence(result.get(12).detail, sha).ci, "failing");
    assert.equal(normalizeEvidence(result.get(12).detail, sha).copilotThreads, 1);
    assert.equal(result.get(13).error, undefined);
});

test("head drift or GraphQL errors do not produce false passing status", async () => {
    const drift = new GitHub(async () => response({ data: { repository: { pr12: detail({ head: "f".repeat(40) }) } } }));
    const result = await drift.pullEvidence(repo, [{ number: 12, sha }]);
    assert.match(result.get(12).error, /different head/);
    let reads = 0;
    const failed = new GitHub(async () => {
        reads++;
        return response({ errors: [{ message: "Access denied" }], data: { repository: { pr12: detail() } } });
    });
    assert.match((await failed.pullEvidence(repo, [{ number: 12, sha }])).get(12).error, /could not return/);
    assert.equal(reads, 1);
});

test("failed CI pagination waits for already-started thread pagination before completing", async () => {
    const first = detail({ threads: [thread()], checks: [check()] });
    first.reviewThreads.pageInfo = connection([], true).pageInfo;
    first.commits.nodes[0].commit.statusCheckRollup.contexts.pageInfo = connection([], true).pageInfo;
    let release;
    const github = new GitHub(async (args) => {
        const query = args.find((arg) => arg.startsWith("query="));
        if (!query.includes("after:")) return response({ data: { repository: { pr12: first } } });
        if (query.includes("reviewThreads")) {
            await new Promise((resolve) => release = resolve);
            return response({ data: { repository: { pullRequest: { number: 12, headRefOid: sha, reviewThreads: connection() } } } });
        }
        return response({ errors: [{ message: "Unavailable" }] });
    });
    let done = false;
    const pending = github.pullEvidence(repo, [{ number: 12, sha }]).then((result) => { done = true; return result; });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(done, false);
    release();
    assert.match((await pending).get(12).error, /could not return/);
});

test("the canvas remains loading until live evidence has settled", async () => {
    const c = controller();
    let release;
    c.github.pullEvidence = () => new Promise((resolve) => release = resolve);
    const pending = c.canvas.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(c.canvas.value.loading, false);
    assert.equal(c.canvas.state().loading, true);
    assert.equal(c.canvas.refresh(), pending);
    release(new Map([[12, { detail: detail() }]]));
    assert.equal((await pending).loading, false);
});

test("refresh preserves previous PR evidence and workflow state until the replacement is ready", async () => {
    const c = controller({ records: [checkpoint({ stage: "complete" })] });
    c.github.pullEvidence = async () => new Map([[12, { detail: detail({ conflicts: "PASSED", checks: [check()] }) }]]);
    const previous = await c.canvas.refresh();
    const nextSha = "f".repeat(40);
    c.setPulls([pull({ title: "Updated PR title", head: { sha: nextSha, repo: { full_name: repo } } })]);
    c.setRecords([checkpoint({ stage: "running", expected_sha: nextSha })]);
    let release;
    c.github.pullEvidence = () => new Promise((resolve) => release = resolve);
    const pending = c.canvas.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    const during = c.canvas.state();
    assert.equal(during.loading, true);
    assert.deepEqual(during.prs, previous.prs);
    assert.deepEqual(during.phases, previous.phases);
    assert.equal(during.auto, previous.auto);
    assert.equal(during.pauseReason, previous.pauseReason);
    assert.equal(taskPresentation(during.prs[0], "pr_conflict_resolver", true).label, "No conflicts");
    assert.equal(taskPresentation(during.prs[0], "ci_fix", true).label, "CI passing");
    release(new Map([[12, { detail: detail({ head: nextSha, checks: [check("FAILURE")] }) }]]));
    const updated = await pending;
    assert.equal(updated.loading, false);
    assert.equal(updated.prs[0].title, "Updated PR title");
    assert.equal(updated.prs[0].sha, nextSha);
    assert.equal(updated.phases[0].stage, "running");
    assert.equal(taskPresentation(updated.prs[0], "pr_conflict_resolver", true).label, "Conflicts");
    assert.equal(taskPresentation(updated.prs[0], "ci_fix", true).label, "CI failing");
});

test("GraphQL capacity is separate from REST and pauses automatic refresh when low", async () => {
    const c = controller();
    c.github.rate = { limit: 5000, remaining: 4500, reset: 3000000 };
    c.github.pullEvidence = async (_repo, prs) => {
        c.github.graphqlRate = { limit: 5000, remaining: 5, reset: 3000000 };
        return new Map(prs.map((pr) => [pr.number, { detail: detail() }]));
    };
    const result = await c.canvas.refresh();
    assert.equal(result.auto, false);
    assert.match(result.pauseReason, /capacity/);
    assert.throws(() => c.canvas.setAuto(true), /capacity/);
    const github = new GitHub();
    github.recordRate({ "x-ratelimit-resource": "core", "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4500", "x-ratelimit-reset": "3000" }, 200);
    github.recordRate({ "x-ratelimit-resource": "graphql", "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "5", "x-ratelimit-reset": "3000" }, 200);
    assert.equal(github.rate.remaining, 4500);
    assert.equal(github.graphqlRate.remaining, 5);
});

test("all open rows include drafts, bots and missing dashboard data; routing uses dashboard classifications", () => {
    const matching = normalizePull(pull(), repo, state(), account);
    assert.equal(matching.mine, true);
    assert.equal(matching.routeLabel, "Waiting on reviewers");
    assert.equal(matching.ciFailing, 0);
    const draft = normalizePull(pull({ draft: true, number: 13 }), repo, state(), account);
    const missing = normalizePull(pull({ number: 14, user: { login: "bot", id: 21, type: "Bot" } }), repo, state(), account);
    assert.equal(draft.dashboardStatus, "draft");
    assert.equal(missing.dashboardStatus, "missing");
    assert.deepEqual(filterPulls([matching, draft, missing]), [matching, draft]);
    assert.deepEqual(filterPulls([matching, draft, missing], { mine: false }), [missing]);
    assert.deepEqual(filterPulls([matching, draft, missing], { reviewers: true }), [matching]);
    for (const record of [
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

test("refresh uses the latest saved routing and facts while ownership and tasks use the live author", async () => {
    const c = controller({
        pulls: [pull({ user: { login: "renovate[bot]", id: 21, type: "Bot" } })],
        dashboardState: state(cached({ facts: facts({
            head_sha: "d".repeat(40), author: "app/renovate", is_draft: true, ci_pending_count: 2,
        }) })),
    });
    const first = await c.canvas.refresh();
    const pr = first.prs[0];
    assert.deepEqual(first.prWarnings, []);
    assert.equal(pr.routeLabel, "Waiting on reviewers");
    assert.equal(pr.ciPending, 2);
    assert.equal(pr.author, "renovate[bot]");
    assert.equal(pr.sha, sha);
    assert.equal(pr.draft, false);
    assert.equal(pr.mine, false);
    assert.deepEqual(pr.tasks, ["pr_review"]);
    assert.deepEqual(filterPulls(first.prs, { mine: false, reviewers: true }), [pr]);
    c.setDashboard(state(cached({ route: "author", facts: facts({ ci_pending_count: 0 }) })));
    const next = await c.canvas.refresh();
    assert.deepEqual(next.prWarnings, []);
    assert.equal(next.prs[0].routeLabel, "Waiting on authors");
    assert.equal(next.prs[0].ciPending, 0);
    assert.deepEqual(filterPulls(next.prs, { mine: false, reviewers: true }), []);
});

test("ownership views are disjoint and compose with reviewer and case-insensitive search filters", () => {
    const mine = normalizePull(pull({ user: { ...account, login: "TrAsK", type: "User" } }), repo, state(), account);
    const other = normalizePull(pull({ user: { login: "someone", id: 22, type: "User" } }),
        repo, state(cached({ facts: facts({ author: "someone" }) })), account);
    assert.deepEqual(filterPulls([mine, other], { mine: true, reviewers: true, search: "UNTRUSTED" }), [mine]);
    assert.deepEqual(filterPulls([mine, other], { mine: true, search: "someone" }), []);
    assert.deepEqual(filterPulls([mine, other]), [mine]);
    assert.deepEqual(filterPulls([mine, other], { mine: false }), [other]);
    assert.deepEqual(filterPulls([mine, other], { reviewers: true }), [mine]);
    assert.deepEqual(filterPulls([mine, other], { mine: false, reviewers: true, search: "SOMEONE" }), [other]);
    assert.deepEqual(filterPulls([mine, other], { mine: false, search: "trask" }), []);
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

test("viewer, PR and reviewer-dashboard reads start together and settle on a failed viewer read", async () => {
    const c = controller();
    const get = c.github.get;
    const reads = [];
    let release;
    c.github.get = async (path) => {
        reads.push(path);
        if (path === "user") throw new Error("Viewer read unavailable");
        return get(path);
    };
    c.github.pulls = () => {
        reads.push("pulls");
        return new Promise((resolve) => release = resolve);
    };
    const first = c.canvas.refresh();
    let done = false;
    first.then(() => done = true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(reads, ["user", "pulls", dashboardPath(repo)]);
    assert.equal(done, false);
    assert.equal(c.canvas.refresh(), first);
    release([pull()]);
    const result = await first;
    assert.match(result.prError, /Viewer read unavailable/);
    assert.equal(result.prLoadedAt, null);
    assert.equal(result.workflowReady, false);
    assert.deepEqual(result.prs, []);
});

test("a complete PR refresh shares three read slots and adds one batched live-status query", async () => {
    let active = 0;
    let maximum = 0;
    const github = new GitHub(async (args) => {
        maximum = Math.max(maximum, ++active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active--;
        const path = args.at(-1);
        let data;
        if (args.includes("graphql")) data = { data: { repository: { pr12: detail() } } };
        else if (path === "user") data = account;
        else if (path === `repos/${repo}/pulls?state=open&per_page=100`) data = [pull()];
        else if (path === dashboardPath(repo)) data = file(state());
        else if (path === `repos/${CENTRAL}/git/matching-refs/heads/review-loop-state`) data = [];
        else if (path === FAILED_COORDINATORS || path.startsWith(`repos/${CENTRAL}/actions/runs?status=`)) {
            data = { total_count: 0, workflow_runs: [] };
        } else throw new Error(`Unexpected refresh read ${path}`);
        return { code: 0, stdout: `HTTP/2.0 200 OK\r\n\r\n${JSON.stringify(data)}` };
    });
    const canvas = new PrDashboard(github, () => 2000000);
    const result = await canvas.refresh();
    assert.equal(maximum, 3);
    assert.equal(github.requests, 11);
    assert.equal(result.cost, 11);
    assert.equal(result.error, null);
    assert.equal(result.prError, null);
    assert.equal(result.workflowReady, true);
    assert.equal(result.prs.length, 1);
    assert.equal(result.prs[0].actionBlock, null);
});

test("fresh checkpoint absence enables Run and is rechecked before the first dispatch", async () => {
    const c = controller();
    const get = c.github.get;
    const path = `repos/${CENTRAL}/git/matching-refs/heads/review-loop-state`;
    c.github.get = async (requested) => {
        if (requested !== path) return get(requested);
        c.calls.push(requested);
        return { data: [] };
    };
    c.canvas.checkpoints = new Checkpoints(c.github);
    const result = await c.canvas.refresh();
    assert.equal(result.error, null);
    assert.equal(result.snapshot, null);
    assert.equal(result.workflowReady, true);
    assert.equal(result.prs[0].actionBlock, null);
    assert.equal(result.prs[0].canCancel, false);
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    assert.equal(c.calls.filter((call) => call === path).length, 2);
    assert.equal(c.calls.filter((call) => typeof call === "object").length, 1);
    const refreshed = await c.canvas.refresh();
    assert.equal(refreshed.prs[0].dispatch.status, "accepted");
    assert.match(refreshed.prs[0].actionBlock, /accepted/);
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

test("PR Reviewer accepts bot-authored PRs without authorizing owner-only tasks", async () => {
    const c = controller({ pulls: [pull({ user: { login: "dependabot[bot]", id: 21, type: "Bot" } })] });
    const snapshot = await c.canvas.refresh();
    assert.equal(snapshot.prs[0].actionBlock, null);
    assert.deepEqual(snapshot.prs[0].tasks, ["pr_review"]);
    const presentation = taskPresentation(snapshot.prs[0], "pr_review", snapshot.workflowReady);
    assert.equal(presentation.label, "Run");
    assert.equal(presentation.disabled, false);
    for (const kind of Object.keys(KIND_LABELS).filter((value) => value !== "pr_review")) {
        assert.equal(taskPresentation(snapshot.prs[0], kind, snapshot.workflowReady).disabled, true);
        await assert.rejects(c.canvas.launch({ target, kind, confirmed: true }), /not eligible/);
    }
    await c.canvas.launch({ target, kind: "pr_review", confirmed: true });
    assert.deepEqual(c.calls.filter((call) => typeof call === "object"), [{
        operation: "launch", target, loop_kind: "pr_review", publication_auth: "fine_grained_pat",
    }]);
    const bot = normalizePull(pull({ user: { ...account, type: "Bot" } }), repo, state(), account);
    assert.deepEqual(taskChoices(bot, account), ["pr_review"]);
    for (const type of ["Organization", "unknown", null]) {
        assert.throws(() => normalizePull(pull({ user: { ...account, type } }), repo, state(), account), /invalid/);
    }
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
    assert.equal(taskPresentation({ ...pr, phase: { ...pr.phase, sha: "f".repeat(40) } }, "self_review", true).label, "Running");
    assert.equal(taskPresentation({ ...pr, phase: { ...pr.phase, stage: "complete", sha: "f".repeat(40) } }, "self_review", true).label, "Previous head");
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
