import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { request as httpRequest } from "node:http";
import { GitHub, GitHubError, CENTRAL, FAILED_COORDINATORS, parseResponse } from "./github.mjs";
import { Checkpoints } from "./state.mjs";
import { phaseSummary, targetHistory, actionSummary, failedActionSummary, commitLink, compareLink } from "./model.mjs";
import { Dashboard } from "./dashboard.mjs";
import { startServer } from "./server.mjs";
import { KIND_LABELS } from "./kinds.mjs";
import { filterPulls, taskPresentation, TASK_EFFECTS } from "./prs.mjs";

const sha = (letter) => letter.repeat(40);
const requestId = (letter) => letter.repeat(32);
const target = "example/project#12";
const fixture = (updates = {}, request = {}) => ({
    schema: 2, stage: "running", phase: requestId("f"), generation: 1, iteration: 1,
    expected_sha: sha("a"), intent: { recorded_at: 1001 },
    run: { id: 101, attempt: 1, conclusion: null },
    request: {
        schema: 2, protocol: "reviewable-v1", repo: "example/project", head_repo: "example/fork", pr: 12,
        request_id: requestId("a"), frozen_at: 1000, deadline: 8200, frozen_sha: sha("a"),
        loop_kind: "copilot_review", mode: "publish", budgets: { max_iterations: 5 },
        publication: { authorized_at: 1000 }, ...request,
    },
    ...updates,
});
const record = (state, name = "pr-v2-123-12.json") => ({ state, name });

test("all independent kinds expose completion without claiming review or CI clearance", () => {
    assert.equal(Object.keys(KIND_LABELS).length, 8);
    for (const kind of Object.keys(KIND_LABELS).filter((value) => !["copilot_review", "self_review"].includes(value))) {
        const s = fixture({
            stage: "complete", task_completion: { outcome: "no_change" },
            ci: { decision: "failed", sha: sha("a"), checks: [] },
            report: { verification: "verified", dispositions: { outcome: "no_change",
                consistency: kind === "pr_consistency" ? [{ path: "A.java", classification: "needed",
                    explanation: "Necessary deviation", citations: ["AGENTS.md:1"] }] : [] } },
        }, { loop_kind: kind });
        const summary = phaseSummary(record(s));
        assert.equal(summary.kind, kind);
        assert.equal(summary.category, "completed");
        assert.equal(summary.maximum, kind === "ci_fix" ? 5 : 1);
        const item = targetHistory([record(s)], target).phases[0].iterations[0];
        assert.equal(item.ci.decision, "failed");
        assert.equal(item.task.outcome, "no_change");
        assert.equal(item.publication, null);
        if (kind === "pr_consistency") assert.equal(item.consistency[0].citations[0], "AGENTS.md:1");
    }
});

test("merge parents, pending reviews and CI reruns retain separate saved evidence", () => {
    const s = fixture({
        stage: "complete",
        task_completion: { outcome: "pending_review", review_id: 42 },
        task_intent: { kind: "pending_review", status: "confirmed", review_id: 42 },
        ci_reruns: [{ run_id: 91, resulting_attempt: 2, status: "confirmed" }],
        ci_warnings: [{ analysis: "Pre-existing failure" }],
        publications: [{ request_id: requestId("a"), sha: sha("b"), effect: "push", confirmed_at: 1100,
            candidate: { parent: sha("a"), commits: [{ commit: sha("b"), subject: "Merge base",
                parents: [sha("a"), sha("c")] }] } }],
    }, { loop_kind: "pr_conflict_resolver" });
    const item = targetHistory([record(s)], target).phases[0].iterations[0];
    assert.deepEqual(item.publication.commits[0].parents.map((parent) => parent.sha), [sha("a"), sha("c")]);
    assert.match(item.taskEffect.reviewUrl, /pullrequestreview-42$/);
    assert.equal(item.ciReruns[0].attempt, 2);
    assert.equal(item.ciWarnings[0].analysis, "Pre-existing failure");
});
function response(data, headers = {}, status = 200, code = status === 304 ? 1 : 0) {
    return {
        code,
        stdout: `HTTP/2.0 ${status} ${status === 304 ? "Not Modified" : "OK"}\r\n${Object.entries(headers).map(([key, value]) => `${key}: ${value}\r\n`).join("")}\r\n${status === 304 ? "" : JSON.stringify(data)}`,
    };
}
const rateHeaders = (remaining = 5000) => ({
    "X-Ratelimit-Limit": "5000", "X-Ratelimit-Remaining": String(remaining), "X-Ratelimit-Reset": "9000",
});

test("304 requires an actual HTTP response and reuses data without charging primary requests", async () => {
    const calls = [];
    const github = new GitHub(async (args) => {
        calls.push(args);
        return calls.length === 1
            ? response([], { Etag: '"one"', ...rateHeaders() })
            : response(null, { ...rateHeaders() }, 304);
    });
    const path = `repos/${CENTRAL}/git/matching-refs/heads/review-loop-state`;
    assert.deepEqual((await github.get(path)).data, (await github.get(path)).data);
    assert.ok(calls[1].includes('If-None-Match: "one"'));
    assert.equal(github.requests, 2);
    assert.equal(github.counted, 1);
    assert.equal(github.cacheHits, 1);
    assert.ok(calls.every((args) => args.includes("GET") && args.includes("--hostname")));
    assert.throws(() => github.get("repos/example/project/issues"), /outside/);
    assert.throws(() => github.get(path + "-other"), /outside/);
    assert.throws(() => github.get(`repos/${CENTRAL}/git/matching-refs/heads`), /outside/);
    assert.throws(() => parseResponse('error: token=do-not-echo'), /no HTTP/);
    const emptyCache = new GitHub(async () => response(null, {}, 304));
    await assert.rejects(emptyCache.get(path), /without cached/);
});

test("GitHub reads run concurrently with a maximum of three, and missing output stays an error", async () => {
    let active = 0;
    let maximum = 0;
    const github = new GitHub(async () => {
        maximum = Math.max(maximum, ++active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active--;
        return response({ workflow_runs: [], total_count: 0 });
    });
    await Promise.all(["queued", "pending", "waiting", "requested", "in_progress"].map((status) =>
        github.get(`repos/${CENTRAL}/actions/runs?status=${status}`)));
    assert.equal(maximum, 3);
    assert.equal(github.activeReads, 0);
    assert.equal(github.readQueue.length, 0);
    assert.equal(github.inFlightReads.size, 0);
    const bad = new GitHub(async () => ({ code: 1, stdout: "" }));
    await assert.rejects(bad.get(`repos/${CENTRAL}/actions/runs`), /no HTTP/);
});

test("concurrent reads of one path share an in-flight response, not a later refresh", async () => {
    let release;
    const calls = [];
    const github = new GitHub(async (args) => {
        calls.push(args);
        if (calls.length === 1) return new Promise((resolve) => release = resolve);
        return response(null, rateHeaders(), 304);
    });
    const path = `repos/${CENTRAL}/git/matching-refs/heads/review-loop-state`;
    const first = github.get(path);
    const second = github.get(path);
    assert.equal(first, second);
    release(response([], { Etag: '"shared"', ...rateHeaders() }));
    assert.equal(await first, await second);
    assert.equal(calls.length, 1);
    await github.get(path);
    assert.equal(calls.length, 2);
    assert.ok(calls[1].includes('If-None-Match: "shared"'));
    assert.equal(github.counted, 1);
    assert.equal(github.cacheHits, 1);
});

test("a rejected shared read releases its slot and permits a fresh read", async () => {
    let release;
    let calls = 0;
    const github = new GitHub(async () => ++calls === 1
        ? new Promise((resolve) => release = resolve) : response([]));
    const path = `repos/${CENTRAL}/git/matching-refs/heads/review-loop-state`;
    const first = github.get(path);
    const second = github.get(path);
    const settled = Promise.allSettled([first, second]);
    release(response({}, {}, 500, 1));
    assert.ok((await settled).every((read) => read.status === "rejected"));
    assert.deepEqual((await github.get(path)).data, []);
    assert.equal(calls, 2);
    assert.equal(github.activeReads, 0);
    assert.equal(github.inFlightReads.size, 0);
});

test("out-of-order rate headers cannot increase capacity or replace a newer reset window", () => {
    const github = new GitHub();
    github.recordRate(parseResponse(response({}, rateHeaders(100)).stdout).headers, 200);
    github.recordRate(parseResponse(response({}, rateHeaders(4990)).stdout).headers, 200);
    assert.equal(github.rate.remaining, 100);
    github.recordRate(parseResponse(response({}, {
        ...rateHeaders(), "X-Ratelimit-Reset": "10000",
    }).stdout).headers, 200);
    assert.equal(github.rate.remaining, 5000);
    assert.equal(github.rate.reset, 10000000);
    github.recordRate(parseResponse(response({}, rateHeaders(0)).stdout).headers, 200);
    assert.equal(github.rate.remaining, 5000);
    assert.equal(github.rate.reset, 10000000);
});

test("rate limits stop queued CLI reads and preserve the longest concurrent backoff", async () => {
    const releases = [];
    const github = new GitHub(async () => new Promise((resolve) => releases.push(resolve)), () => 1000000);
    const pending = ["queued", "pending", "waiting", "requested", "in_progress"].map((status) =>
        github.get(`repos/${CENTRAL}/actions/runs?status=${status}`));
    const settled = Promise.allSettled(pending);
    assert.equal(releases.length, 3);
    releases[0](response({}, { "Retry-After": "120" }, 429, 1));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(github.retryAt, 1120000);
    assert.equal(releases.length, 3);
    releases[1](response({}, { "Retry-After": "30" }, 429, 1));
    releases[2](response({ total_count: 0, workflow_runs: [] }, rateHeaders()));
    const results = await settled;
    assert.deepEqual(results.map((read) => read.status), [
        "rejected", "rejected", "fulfilled", "rejected", "rejected",
    ]);
    assert.match(results[3].reason.message, /paused/);
    assert.equal(github.retryAt, 1120000);
    assert.equal(github.requests, 3);
    assert.equal(github.activeReads, 0);
    assert.equal(github.readQueue.length, 0);
});

test("pagination follows trusted links and rejects malformed, oversized, or hostile listings", async () => {
    const path = `repos/${CENTRAL}/actions/runs?status=queued`;
    let calls = 0;
    const github = new GitHub(async () => ++calls === 1
        ? response({ total_count: 2, workflow_runs: [{ id: 1 }] }, {
            Link: `<https://api.github.com/${path}&per_page=100&page=2>; rel="next"`,
        })
        : response({ total_count: 2, workflow_runs: [{ id: 2 }] }));
    assert.deepEqual(await github.pages(path), [{ id: 1 }, { id: 2 }]);
    for (const result of [
        response({ total_count: 2, workflow_runs: null }),
        response({ total_count: -1, workflow_runs: [] }),
        response({ total_count: 1001, workflow_runs: Array.from({ length: 1001 }, (_, id) => ({ id })) }),
        response({ total_count: 1, workflow_runs: [] }, { Link: '<https://evil.test/steal>; rel="next"' }),
    ]) {
        await assert.rejects(new GitHub(async () => result).pages(path), /incomplete|limit|trusted host/);
    }
    const forever = new GitHub(async () => response({ total_count: 1000, workflow_runs: [] }, {
        Link: `<https://api.github.com/${path}&per_page=100&page=2>; rel="next"`,
    }));
    await assert.rejects(forever.pages(path), /incomplete/);
    let pages = 0;
    const boundary = new GitHub(async () => {
        const current = ++pages;
        return response({
            total_count: 1000,
            workflow_runs: Array.from({ length: 100 }, (_, index) => ({ id: (current - 1) * 100 + index + 1 })),
        }, current < 10 ? { Link: `<https://api.github.com/${path}&per_page=100&page=${current + 1}>; rel="next"` } : {});
    });
    assert.equal((await boundary.pages(path)).length, 1000);
    assert.equal(pages, 10);
});

test("pagination treats mutable totals as advisory, including cached final pages", async () => {
    const path = `repos/${CENTRAL}/actions/runs?status=in_progress`;
    let calls = 0;
    const github = new GitHub(async () => ++calls === 1
        ? response({ total_count: 2, workflow_runs: [{ id: 1 }] }, { Etag: '"active"' })
        : response(null, {}, 304));
    assert.deepEqual(await github.pages(path), [{ id: 1 }]);
    assert.deepEqual(await github.pages(path), [{ id: 1 }]);
    assert.equal(github.counted, 1);
    assert.equal(github.cacheHits, 1);
    const empty = new GitHub(async () => response({ total_count: 1, workflow_runs: [] }));
    assert.deepEqual(await empty.pages(path), []);
    const staleLargeTotal = new GitHub(async () => response({ total_count: 1001, workflow_runs: [{ id: 1 }] }));
    assert.deepEqual(await staleLargeTotal.pages(path), [{ id: 1 }]);
});

const failedCoordinator = (updates = {}) => ({
    id: 37368497585, run_attempt: 1, status: "completed", conclusion: "failure",
    name: "Review loop coordinator", display_title: "Review loop launch 37368497585",
    created_at: "2026-10-05T20:14:29Z", ...updates,
});

test("recent coordinator failures use one fixed bounded read, not historical pagination", async () => {
    const calls = [];
    const github = new GitHub(async (args) => {
        calls.push(args);
        return response({ total_count: 100, workflow_runs: [failedCoordinator()] }, {
            Link: `<https://api.github.com/${FAILED_COORDINATORS}&page=2>; rel="next"`,
        });
    });
    assert.equal((await github.failedCoordinators())[0].id, 37368497585);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].at(-1), FAILED_COORDINATORS);
    for (const data of [
        { total_count: 1, workflow_runs: null }, { total_count: -1, workflow_runs: [] },
        { total_count: 21, workflow_runs: Array.from({ length: 21 }, () => failedCoordinator()) },
    ]) await assert.rejects(new GitHub(async () => response(data)).failedCoordinators(), /invalid/);
    assert.throws(() => github.get(FAILED_COORDINATORS + "&page=2"), /outside/);
});

test("pagination completes when run totals grow or shrink between linked pages", async () => {
    const path = `repos/${CENTRAL}/actions/runs?status=in_progress`;
    const firstPage = Array.from({ length: 100 }, (_, index) => ({ id: index + 1 }));
    for (const total of [99, 103]) {
        let calls = 0;
        const github = new GitHub(async () => ++calls === 1
            ? response({ total_count: 101, workflow_runs: firstPage }, {
                Link: `<https://api.github.com/${path}&per_page=100&page=2>; rel="next"`,
            })
            : response({ total_count: total, workflow_runs: [{ id: 101 }] }));
        assert.deepEqual(await github.pages(path), [...firstPage, { id: 101 }]);
        assert.equal(calls, 2);
    }
});

test("rate limits honor Retry-After or reset and never leak response bodies", async () => {
    let now = 1000000;
    const path = `repos/${CENTRAL}/actions/runs`;
    const github = new GitHub(async () =>
        response({ message: "sensitive remote text" }, { ...rateHeaders(0), "Retry-After": "120" }, 403, 1), () => now);
    await assert.rejects(github.get(path), (error) => error.retryAt === now + 120000 && !error.message.includes("sensitive"));
    await assert.rejects(github.get(path), /paused/);
    assert.equal(github.requests, 1);
    now += 120000;
    await assert.rejects(github.get(path), /HTTP 403/);
    const reset = new GitHub(async () => response({}, rateHeaders(0), 429, 1), () => 1000000);
    await assert.rejects(reset.get(path), (error) => error.retryAt === 9000000);
    for (const status of [401, 404, 500]) {
        await assert.rejects(new GitHub(async () => response({}, {}, status, 1)).get(path), new RegExp(`HTTP ${status}`));
    }
});

function gitBlob(value) {
    const content = Buffer.isBuffer(value) ? value : Buffer.from(JSON.stringify(value));
    const hash = createHash("sha1").update(`blob ${content.length}\0`).update(content).digest("hex");
    return {
        entry: { path: "", type: "blob", mode: "100644", sha: hash, size: content.length },
        blob: { sha: hash, encoding: "base64", content: content.toString("base64"), size: content.length },
    };
}

function stateClient() {
    const current = gitBlob(fixture());
    current.entry.path = "pr-v2-123-12.json";
    const archive = gitBlob(fixture({ iteration: 0, intent: null, run: null }, { request_id: requestId("b") }));
    archive.entry.path = `request-${requestId("b")}.json`;
    const calls = [];
    const objects = new Map([
        ["matching-refs/heads/review-loop-state", [{
            ref: "refs/heads/review-loop-state", object: { type: "commit", sha: sha("c") },
        }]],
        ["commits/" + sha("c"), { sha: sha("c"), tree: { sha: sha("d") } }],
        ["trees/" + sha("d"), { sha: sha("d"), truncated: false, tree: [current.entry, archive.entry] }],
        ["blobs/" + current.entry.sha, current.blob], ["blobs/" + archive.entry.sha, archive.blob],
    ]);
    return {
        calls, objects,
        github: { get: async (path) => {
            const key = path.split("/git/")[1];
            calls.push(key);
            assert.ok(objects.has(key), `Unexpected read ${key}`);
            return { data: structuredClone(objects.get(key)) };
        } },
    };
}

test("state pins commit/tree/blob identities, defers archives, and caches immutable blobs", async () => {
    const client = stateClient();
    const store = new Checkpoints(client.github);
    const snapshot = await store.load();
    assert.equal(snapshot.current.length, 1);
    assert.equal(client.calls.length, 4);
    await store.load();
    assert.equal(client.calls.length, 5);
    assert.equal((await store.history(snapshot)).length, 2);
    assert.equal(client.calls.length, 6);
    await store.history(snapshot);
    assert.equal(client.calls.length, 6);
    const nextCommit = sha("e");
    client.objects.set("matching-refs/heads/review-loop-state", [{
        ref: "refs/heads/review-loop-state", object: { type: "commit", sha: nextCommit },
    }]);
    client.objects.set("commits/" + nextCommit, { sha: nextCommit, tree: { sha: sha("d") } });
    await store.load();
    assert.equal(client.calls.filter((path) => path.startsWith("blobs/")).length, 2);
});

test("absent state is explicit empty history, detects initialization, and clears removed state", async () => {
    const client = stateClient();
    const refs = client.objects.get("matching-refs/heads/review-loop-state");
    client.objects.set("matching-refs/heads/review-loop-state", []);
    const store = new Checkpoints(client.github);
    const empty = await store.load();
    assert.equal(empty.sha, null);
    assert.equal(empty.entries.size, 0);
    assert.deepEqual(empty.current, []);
    assert.deepEqual(await store.history(empty), []);
    assert.equal(await store.load(), empty);
    assert.deepEqual(client.calls, [
        "matching-refs/heads/review-loop-state", "matching-refs/heads/review-loop-state",
    ]);
    client.objects.set("matching-refs/heads/review-loop-state", refs);
    assert.equal((await store.load()).current.length, 1);
    assert.equal(store.blobs.size, 1);
    client.objects.set("matching-refs/heads/review-loop-state", []);
    const cleared = await store.load();
    assert.equal(cleared.sha, null);
    assert.deepEqual(cleared.current, []);
    assert.equal(cleared.entries.size, 0);
    assert.equal(store.blobs.size, 0);
    assert.deepEqual(await store.history(cleared), []);
    client.objects.set("matching-refs/heads/review-loop-state", refs);
    assert.equal((await store.load()).current.length, 1);
    assert.equal(client.calls.filter((path) => path.startsWith("blobs/")).length, 2);
});

test("state requires the exact branch, not another matching prefix", async () => {
    const client = stateClient();
    client.objects.get("matching-refs/heads/review-loop-state")[0].ref += "-archive";
    const snapshot = await new Checkpoints(client.github).load();
    assert.equal(snapshot.sha, null);
    assert.deepEqual(snapshot.current, []);
    assert.deepEqual(client.calls, ["matching-refs/heads/review-loop-state"]);
});

test("state listing access failures never become an empty snapshot", async () => {
    for (const status of [401, 403, 404, 500]) {
        const store = new Checkpoints(new GitHub(async () => response({}, {}, status, 1)));
        await assert.rejects(store.load(), new RegExp(`HTTP ${status}`));
        assert.equal(store.snapshot, null);
    }
});

test("state rejects malformed, duplicate, unpinned and incomplete reference listings", async () => {
    const ref = { ref: "refs/heads/review-loop-state", object: { type: "commit", sha: sha("c") } };
    for (const refs of [
        null, {}, [null], [{ ...ref, ref: "refs/heads/main" }], [ref, ref],
        [{ ...ref, object: { type: "tree", sha: sha("c") } }],
        [{ ...ref, object: { type: "commit", sha: "invalid" } }],
        Array.from({ length: 1001 }, () => ref),
    ]) {
        await assert.rejects(new Checkpoints(new GitHub(async () => response(refs))).load(), /State/);
    }
    const incomplete = new GitHub(async () => response([], {
        Link: `<https://api.github.com/repos/${CENTRAL}/git/matching-refs/heads/review-loop-state?page=2>; rel="next"`,
    }));
    await assert.rejects(new Checkpoints(incomplete).load(), /incomplete/);
});

test("state rejects truncation, oversized trees, unsafe paths, and corrupted objects", async () => {
    for (const mutation of [
        (c) => c.objects.get("trees/" + sha("d")).truncated = true,
        (c) => c.objects.get("trees/" + sha("d")).tree[0].size = 1024 * 1024 + 1,
        (c) => c.objects.get("trees/" + sha("d")).tree[0].path = "../secrets.json",
        (c) => c.objects.get("commits/" + sha("c")).sha = sha("e"),
        (c) => { const entry = c.objects.get("trees/" + sha("d")).tree[0]; c.objects.get("blobs/" + entry.sha).content = "e30="; },
        (c) => { const tree = c.objects.get("trees/" + sha("d")); tree.tree = Array.from({ length: 1001 }, () => tree.tree[0]); },
        (c) => { const tree = c.objects.get("trees/" + sha("d")); tree.tree = Array.from({ length: 17 }, (_, index) => ({ ...tree.tree[0], path: `pr-${index}.json`, size: 1024 * 1024 })); },
    ]) {
        const client = stateClient();
        mutation(client);
        await assert.rejects(new Checkpoints(client.github).load(), /State|state/);
    }
});

test("state rejects malformed JSON and invalid UTF-8 even when Git object identity is correct", async () => {
    for (const content of [Buffer.from("{invalid"), Buffer.from('{"text":"\xff"}', "latin1")]) {
        const client = stateClient();
        const bad = gitBlob(content);
        bad.entry.path = "pr-v2-123-12.json";
        client.objects.get("trees/" + sha("d")).tree[0] = bad.entry;
        client.objects.set("blobs/" + bad.entry.sha, bad.blob);
        await assert.rejects(new Checkpoints(client.github).load(), /malformed JSON/);
    }
});

test("timeline deduplicates carried publications and overlapping snapshots without linking no-change packaging commits", () => {
    const publication = {
        request_id: requestId("a"), sha: sha("b"), effect: "push", confirmed_at: 1100, candidate: { parent: sha("a") },
    };
    const first = fixture({
        stage: "published", publications: [publication],
        artifacts: [{ id: 501, name: "candidate-101-1" }, { id: 502, name: "verification-201-1" }],
        report: { verification: "verified", dispositions: { outcome: "fixes", findings: [{ key: "one", disposition: "fixed", reason: "Guard missing input" }] },
            candidate: { commit: sha("b"), changed_paths: ["src/guard.js"] } },
    }, { findings: [{ key: "one", body: "<script>not executable</script>", path: "src/guard.js", comment_id: 33 }] });
    const final = fixture({
        stage: "clean", iteration: 2, generation: 3,
        run: { id: 102, attempt: 1, conclusion: "success" },
        publications: [publication, { request_id: requestId("b"), sha: sha("b"), effect: "no_change", confirmed_at: 1200 }],
        report: { verification: "verified", dispositions: { outcome: "clean" }, candidate: { commit: sha("c"), changed_paths: [] } },
    }, { request_id: requestId("b"), loop_kind: "self_review", frozen_at: 1150, frozen_sha: sha("b") });
    const result = targetHistory([
        record(first, `request-${requestId("a")}.json`),
        record({ ...first, stage: "blocked" }, `stopped-${requestId("a")}-1.json`),
        record(final),
    ], target);
    assert.equal(result.phases.length, 1);
    const phase = result.phases[0];
    assert.deepEqual(phase.gaps, []);
    assert.equal(phase.iterations.length, 2);
    assert.equal(phase.iterations[0].publication.url, `https://github.com/example/fork/commit/${sha("b")}`);
    assert.equal(phase.iterations[0].publication.compareUrl, `https://github.com/example/fork/compare/${sha("a")}...${sha("b")}`);
    assert.equal(phase.iterations[0].findings[0].reason, "Guard missing input");
    assert.equal(phase.iterations[0].stage, "published");
    assert.equal(phase.iterations[0].artifacts[0].url, `https://github.com/${CENTRAL}/actions/runs/101/artifacts/501`);
    assert.equal(phase.iterations[0].artifacts[1].url, `https://github.com/${CENTRAL}/actions/runs/201/artifacts/502`);
    assert.equal(phase.iterations[1].publication.url, null);
    assert.equal(phase.iterations[1].publication.compareUrl, null);
});

test("phases, replacements, cancellation generations, previews, failures, and gaps stay distinct", () => {
    const failed = fixture({ stage: "failed", run: { id: 101, attempt: 1, conclusion: "failure" } });
    const replacement = fixture({ generation: 2, intent: null, run: null, stage: "source_pending" },
        { request_id: requestId("b"), frozen_at: 1100 });
    const cancelled = { ...replacement, stage: "cancelled", generation: 3 };
    const preview = fixture({ phase: requestId("d"), stage: "preview_complete", iteration: 0, intent: null, run: null },
        { request_id: requestId("c"), frozen_at: 1200, mode: "preview", publication: undefined });
    const history = targetHistory([record(failed, `request-${requestId("a")}.json`),
        record(cancelled, `request-${requestId("b")}.json`), record(preview)], target);
    assert.equal(history.phases.length, 2);
    assert.equal(history.phases[0].iterations.length, 0);
    assert.equal(history.phases[1].iterations.length, 1);
    assert.equal(history.phases[1].transitions.length, 1);
    assert.equal(history.phases[1].iterations[0].number, 1);
    const missing = targetHistory([record(fixture({ iteration: 3 }))], target);
    assert.deepEqual(missing.phases[0].gaps, [1, 2]);
    assert.equal(phaseSummary(record(failed)).category, "attention");
    assert.equal(phaseSummary(record(fixture({ stage: "shadow_complete" }))).category, "completed");
    assert.equal(phaseSummary(record(fixture({ stage: "waiting_review" }))).category, "waiting");
});

test("historical and unknown records cannot appear as active generic phases", () => {
    assert.equal(phaseSummary(record(fixture({ schema: 1 }))).category, "historical");
    assert.equal(phaseSummary(record(fixture({ capability_probe: { prior: true } }))).historical, true);
    assert.equal(phaseSummary(record(fixture({ report: { objective_validation: { status: "passed" } } }))).historical, true);
    assert.equal(phaseSummary(record(fixture({ stage: "new_unknown_stage" }))).category, "attention");
    assert.throws(() => phaseSummary(record(fixture({}, { repo: "../secrets" }))), /malformed/);
    assert.throws(() => phaseSummary(record(fixture({ publications: {} }))), /malformed/);
    assert.throws(() => phaseSummary(record(fixture({ publications: [{}] }))), /malformed publication/);
    assert.throws(() => phaseSummary(record(fixture({}, { findings: ["not a finding"] }))), /malformed iteration/);
    assert.equal(commitLink("../secrets", sha("a")), null);
    assert.equal(compareLink("example/fork", sha("a"), sha("a")), null);
    assert.equal(targetHistory([record({})], target).warnings.length, 1);
});

test("current batch publication and thread effects link only confirmed published commits and replies", () => {
    const candidate = {
        parent: sha("a"), commit: sha("c"), changed_paths: ["guard.js"],
        commits: [{ commit: sha("b"), subject: "First root cause" }, { commit: sha("c"), subject: "Second root cause" }],
        finding_commits: { one: sha("b") },
    };
    const state = fixture({
        stage: "thread_effects",
        report: { verification: "verified", candidate, dispositions: { outcome: "fixes",
            findings: [{ key: "one", disposition: "fixed", analysis: "Reject input", upsides: "No crash", downsides: "Extra branch" }] } },
        effects: [{ key: "one", root: 33, thread: "PRRT_test", status: "uncertain",
            reply: { status: "confirmed", reply_id: 34 }, resolution: { status: "uncertain" } }],
        publications: [{ request_id: requestId("a"), sha: sha("c"), effect: "push", candidate }],
    }, { findings: [{ key: "one", comment_id: 33, body: "Check input" }] });
    assert.equal(phaseSummary(record(state)).historical, false);
    const item = targetHistory([record(state)], target).phases[0].iterations[0];
    assert.deepEqual(item.publication.commits.map((entry) => entry.subject), ["First root cause", "Second root cause"]);
    assert.equal(item.findings[0].commitUrl, `https://github.com/example/fork/commit/${sha("b")}`);
    assert.equal(item.findings[0].effects[0].resolution, "uncertain");
    assert.equal(item.findings[0].effects[0].replyUrl, "https://github.com/example/project/pull/12#discussion_r34");
    const unpublished = { ...state, publications: [] };
    const pending = targetHistory([record(unpublished)], target).phases[0].iterations[0];
    assert.equal(pending.publication, null);
    assert.equal(pending.findings[0].commitUrl, null);
    const historical = fixture({}, { protocol: undefined });
    assert.equal(phaseSummary(record(historical)).historical, true);
});

test("thread statuses remain distinct and malformed batches or effects are rejected", () => {
    for (const status of ["pending", "confirmed", "skipped", "failed", "uncertain"]) {
        const state = fixture({
            effects: [{ key: "one", root: 33, thread: "PRRT_test", status }],
        }, { findings: [{ key: "one", comment_id: 33 }] });
        const effect = targetHistory([record(state)], target).phases[0].iterations[0].findings[0].effects[0];
        assert.equal(effect.status, status);
        assert.equal(effect.reply, status === "skipped" ? "skipped" : "pending");
        assert.equal(effect.replyUrl, null);
    }
    for (const malformed of [
        { effects: ["not an effect"] },
        { report: { candidate: { commits: [{ commit: "bad", subject: "Candidate" }] } } },
        { report: { candidate: { finding_commits: { one: "bad" } } } },
        { publications: [{ request_id: requestId("a"), sha: sha("c"), effect: "push",
            candidate: { commits: "not batches" } }] },
    ]) {
        const result = targetHistory([record(fixture(malformed))], target);
        assert.equal(result.phases.length, 0);
        assert.equal(result.warnings.length, 1);
    }
});

test("missing snapshots retain confirmed publication links without inventing iteration numbers", () => {
    const state = fixture({
        publications: [{ request_id: requestId("b"), sha: sha("b"), effect: "push", confirmed_at: 1100 }],
    });
    const history = targetHistory([record(state)], target);
    assert.deepEqual(history.phases[0].missingPublications, [requestId("b")]);
    assert.equal(history.phases[0].orphanPublications[0].url, `https://github.com/example/fork/commit/${sha("b")}`);
    assert.equal(history.phases[0].iterations.length, 1);
});

test("active Actions associate by recorded identities and expose unmatched runs", () => {
    const phase = phaseSummary(record(fixture({}, { launch_run: { id: 202 } })));
    const run = { id: 202, run_attempt: 1, status: "queued", name: "Coordinator" };
    assert.deepEqual(actionSummary(run, [phase]).targets, [target]);
    assert.deepEqual(actionSummary({ ...run, id: 303 }, [phase]).targets, []);
    assert.throws(() => actionSummary({ ...run, status: "unrecognized" }, [phase]), /invalid/);
});

function fakeDashboard(now = () => 2000000) {
    const github = {
        requests: 0, counted: 0, cacheHits: 0, rate: { limit: 5000, remaining: 4990, reset: 9000000 },
        pages: async () => { github.requests++; github.counted++; return []; },
        failedCoordinators: async () => { github.requests++; github.counted++; return []; },
    };
    const dashboard = new Dashboard(github, now);
    const snapshot = { sha: sha("a"), current: [record(fixture())] };
    dashboard.checkpoints = {
        snapshot, load: async () => { github.requests++; github.counted++; return snapshot; },
        history: async () => snapshot.current,
    };
    return dashboard;
}

test("a fresh state branch absence loads Actions and failed launches without inventing history", async () => {
    const github = new GitHub(async (args) => {
        const path = args.at(-1);
        if (path.endsWith("/git/matching-refs/heads/review-loop-state")) return response([]);
        if (path === FAILED_COORDINATORS) return response({ total_count: 1, workflow_runs: [failedCoordinator()] });
        assert.match(path, /\/actions\/runs\?status=/);
        return response({ total_count: 0, workflow_runs: [] });
    });
    const dashboard = new Dashboard(github, () => 2000000);
    const state = await dashboard.refresh();
    assert.equal(state.error, null);
    assert.equal(state.loadedAt, 2000000);
    assert.equal(state.snapshot, null);
    assert.equal(state.auto, true);
    assert.deepEqual(state.phases, []);
    assert.deepEqual(state.actions, []);
    assert.equal(state.failures[0].id, failedCoordinator().id);
    await assert.rejects(dashboard.history(target), /not in/);
});

test("refresh coalesces, keeps stale data on failure, and respects manual/low-capacity mode", async () => {
    const dashboard = fakeDashboard();
    const first = dashboard.refresh();
    assert.equal(first, dashboard.refresh());
    const result = await first;
    assert.equal(result.cost, 7);
    assert.equal(result.phases.length, 1);
    dashboard.github.pages = async () => { throw new GitHubError("HTTP 403 access denied"); };
    const failed = await dashboard.refresh();
    assert.equal(failed.loadedAt, result.loadedAt);
    assert.equal(failed.phases.length, 1);
    assert.match(failed.error, /403/);
    assert.equal(failed.auto, false);
    dashboard.github.rate.remaining = 499;
    assert.throws(() => dashboard.setAuto(true), /10 percent/);
    assert.equal(dashboard.state().rate.remaining, 499);
    const initialFailure = fakeDashboard();
    initialFailure.checkpoints.load = async () => { throw new Error("state missing"); };
    assert.equal((await initialFailure.refresh()).loadedAt, null);
});

test("parallel refresh reads settle before releasing refresh coalescing after a failure", async () => {
    const dashboard = fakeDashboard();
    let release;
    const waiting = new Promise((resolve) => release = resolve);
    dashboard.github.failedCoordinators = async () => { throw new Error("Failure listing unavailable"); };
    dashboard.github.pages = async () => { await waiting; return []; };
    const first = dashboard.refresh();
    let done = false;
    first.then(() => done = true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(done, false);
    assert.equal(dashboard.refresh(), first);
    release();
    const state = await first;
    assert.match(state.error, /Failure listing unavailable/);
    assert.equal(state.loadedAt, null);
    assert.equal(state.loading, false);
    assert.equal(state.auto, false);
});

test("a late failure listing remains visible when another parallel state read fails", async () => {
    const dashboard = fakeDashboard();
    let release;
    dashboard.github.failedCoordinators = () => new Promise((resolve) => release = resolve);
    dashboard.checkpoints.load = async () => { throw new Error("State unavailable"); };
    const pending = dashboard.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(dashboard.refresh(), pending);
    release([failedCoordinator()]);
    const state = await pending;
    assert.match(state.error, /State unavailable/);
    assert.equal(state.failures[0].id, failedCoordinator().id);
    assert.equal(state.loadedAt, null);
});

test("failed launches survive a fresh controller without inventing a checkpoint or PR association", async () => {
    const run = failedCoordinator();
    for (let restart = 0; restart < 2; restart++) {
        const dashboard = fakeDashboard();
        dashboard.checkpoints.snapshot.current = [];
        dashboard.github.failedCoordinators = async () => [run];
        const state = await dashboard.refresh();
        assert.equal(state.phases.length, 0);
        assert.equal(state.actions.length, 0);
        assert.equal(state.failures.length, 1);
        assert.equal(state.failures[0].conclusion, "failure");
        assert.deepEqual(state.failures[0].targets, []);
        assert.match(state.failures[0].url, /\/37368497585\/attempts\/1$/);
    }
    const phase = phaseSummary(record(fixture({}, { launch_run: { id: run.id } })));
    assert.deepEqual(failedActionSummary(run, [phase]).targets, [target]);
    const missingState = fakeDashboard();
    missingState.github.failedCoordinators = async () => [run];
    missingState.checkpoints.load = async () => { throw new Error("State branch missing"); };
    const firstFailure = await missingState.refresh();
    assert.equal(firstFailure.loadedAt, null);
    assert.equal(firstFailure.failures[0].id, run.id);
    assert.match(firstFailure.error, /State branch missing/);
    assert.equal(firstFailure.auto, false);
    for (const updates of [{ status: "in_progress" }, { conclusion: "success" }, { id: -1 },
        { created_at: "invalid" }, { display_title: null }]) {
        assert.throws(() => failedActionSummary(failedCoordinator(updates), []), /invalid/);
    }
    const dashboard = fakeDashboard();
    dashboard.github.failedCoordinators = async () => [run];
    await dashboard.refresh();
    dashboard.github.failedCoordinators = async () => { throw new Error("Failed-run read unavailable"); };
    const stale = await dashboard.refresh();
    assert.equal(stale.failures.length, 1);
    assert.match(stale.error, /Failed-run read unavailable/);
    assert.equal(stale.auto, false);
});

test("automatic refresh pauses for a slow or costly steady-state cycle, not initial history loading", async () => {
    let now = 2000000;
    const dashboard = fakeDashboard(() => now);
    await dashboard.refresh();
    dashboard.checkpoints.load = async () => { now += 10001; return dashboard.checkpoints.snapshot; };
    assert.equal((await dashboard.refresh()).auto, false);
    assert.match(dashboard.state().pauseReason, /10 seconds/);
    const costly = fakeDashboard();
    await costly.refresh();
    costly.github.pages = async () => { costly.github.counted += 3; return []; };
    assert.match((await costly.refresh()).pauseReason, /12 primary/);
    const low = fakeDashboard();
    low.github.rate.remaining = 499;
    assert.equal((await low.refresh()).auto, false);
});

test("visibility leases stop polling and history uses the displayed pinned snapshot", async () => {
    const dashboard = fakeDashboard();
    await dashboard.refresh();
    dashboard.heartbeat("one", true);
    assert.ok(dashboard.timer);
    dashboard.heartbeat("two", true);
    dashboard.removeViewer("one");
    assert.ok(dashboard.timer);
    dashboard.heartbeat("two", false);
    assert.equal(dashboard.timer, null);
    const history = await dashboard.history(target);
    assert.equal(history.snapshot, dashboard.value.snapshot);
    await assert.rejects(dashboard.history("example/project#99"), /not in/);
    dashboard.checkpoints.snapshot = { ...dashboard.checkpoints.snapshot, sha: sha("b") };
    await assert.rejects(dashboard.history(target), /reconcile/);
});

test("history coalesces archive reads, reports failures, and cannot mix with a refresh snapshot", async () => {
    const dashboard = fakeDashboard();
    await dashboard.refresh();
    let loads = 0;
    let release;
    dashboard.checkpoints.history = async () => {
        loads++;
        await new Promise((resolve) => release = resolve);
        return dashboard.checkpoints.snapshot.current;
    };
    const one = dashboard.history(target);
    const two = dashboard.history(target);
    const refresh = dashboard.refresh();
    release();
    assert.equal((await one).snapshot, (await two).snapshot);
    await refresh;
    assert.equal(loads, 1);
    dashboard.checkpoints.history = async () => { throw new Error("Missing archive"); };
    await assert.rejects(dashboard.history(target), /Missing archive/);
    assert.equal(dashboard.state().auto, false);
    assert.match(dashboard.state().pauseReason, /History read failed/);
});

test("loopback serves assets and read-only endpoints; cross-origin data reads and invalid inputs fail explicitly", async (t) => {
    const dashboard = fakeDashboard();
    const server = await startServer(dashboard);
    t.after(() => server.close());
    assert.equal((await fetch(server.url, { headers: { "sec-fetch-site": "cross-site" } })).status, 200);
    assert.equal((await fetch(new URL("app.mjs", server.url))).status, 200);
    assert.equal((await fetch(new URL("kinds.mjs", server.url))).status, 200);
    assert.equal((await fetch(new URL("api/state", server.url), { headers: { Origin: "https://evil.test" } })).status, 403);
    const hostileHost = await new Promise((resolve, reject) => {
        const req = httpRequest(new URL("api/state", server.url), { headers: { Host: "evil.test" } },
            (res) => { res.resume(); resolve(res.statusCode); });
        req.on("error", reject);
        req.end();
    });
    assert.equal(hostileHost, 403);
    assert.equal((await fetch(new URL("api/visibility?visible=maybe", server.url), { method: "POST" })).status, 400);
    assert.equal((await fetch(new URL("api/refresh", server.url), { method: "POST" })).status, 200);
    const history = await fetch(new URL(`api/history?target=${encodeURIComponent(target)}`, server.url));
    assert.equal(history.status, 200);
    assert.equal((await history.json()).phases.length, 1);
    assert.equal((await fetch(new URL("api/history?target=../secret", server.url))).status, 400);
    assert.equal((await fetch(new URL("api/cancel", server.url), { method: "POST" })).status, 404);
    dashboard.github.pages = async () => { throw new Error("GitHub unavailable"); };
    const stale = await fetch(new URL("api/refresh", server.url), { method: "POST" });
    assert.equal(stale.status, 502);
    assert.equal((await stale.json()).phases.length, 1);
});

test("renderer preserves safe history and shows per-task buttons with confirmation and activity", async () => {
    class Node {
        constructor(tag = "") { this.tag = tag; this.children = []; this.events = {}; this.value = ""; this.isConnected = true; }
        append(...nodes) { this.children.push(...nodes); }
        replaceChildren(...nodes) { this.children = nodes; }
        get firstChild() { return this.children[0]; }
        addEventListener(name, action) { this.events[name] = action; }
        setAttribute(name, value) { this[name] = value; }
        showModal() { this.open = true; }
    }
    const nodes = new Map();
    for (const id of ["refresh", "auto", "freshness", "cost", "pause", "error", "mine", "reviewers", "pr-count",
        "repo", "search", "warnings", "prs", "actions", "actions-count", "failures", "failures-count",
        "confirm", "confirm-title", "confirm-target", "confirm-effects", "confirm-submit"]) nodes.set(id, new Node());
    const s = fixture({ stage: "blocked", reason: "target_ci_failed",
        publications: [{ request_id: requestId("a"), sha: sha("b"), effect: "push" }],
    }, { findings: [{ key: "one", body: '<img src=x onerror="throw Error()">', path: "src/example.js" }] });
    const dashboard = fakeDashboard();
    dashboard.github.failedCoordinators = async () => [failedCoordinator({
        display_title: "Review loop launch pr_description example/project#12 <img src=x>",
    })];
    dashboard.checkpoints.snapshot.current = [record(s)];
    const state = await dashboard.refresh();
    Object.assign(state, {
        repository: "example/project", repositories: ["example/project"], prLoadedAt: state.loadedAt,
        prError: null, prWarnings: [], workflowReady: true, viewer: { login: "trask" },
        prs: [{
            target, number: 12, title: "<untrusted title>", url: "https://github.com/example/project/pull/12",
            author: "trask", mine: true, sha: sha("a"), draft: false,
            dashboardStatus: "missing", routeLabel: "Dashboard missing", reviewers: [], tasks: [],
            phase: state.phases[0], canCancel: false, actionBlock: "Read only",
        }],
    });
    const history = await dashboard.history(target);
    const document = {
        hidden: false, getElementById: (id) => nodes.get(id), createElement: (tag) => new Node(tag),
        createElementNS: (_namespace, tag) => new Node(tag),
        createTextNode: (text) => Object.assign(new Node("#text"), { textContent: text }), addEventListener() {},
    };
    const script = await readFile(new URL("app.mjs", import.meta.url), "utf8");
    const launches = [];
    let releaseLaunch;
    const renderer = await runInNewContext(`(async () => { ${script.replace(/^import .+;\r?$/gm, "")} return { render }; })()`, {
        KIND_LABELS, filterPulls, taskPresentation, TASK_EFFECTS, document, setInterval() {}, fetch: async (path, options) => {
            if (path === "/api/launch") {
                launches.push(JSON.parse(options.body));
                return new Promise((resolve) => releaseLaunch = () => {
                    state.prs[0].dispatch = { operation: "launch", kind: "pr_description", status: "accepted", message: "Dispatch accepted" };
                    resolve({ ok: true, json: async () => state.prs[0].dispatch });
                });
            }
            return { ok: true, json: async () => path.includes("history") ? history : path.includes("visibility") ? {} : state };
        },
    });
    const row = nodes.get("prs").firstChild;
    assert.equal(nodes.get("failures-count").textContent, 1);
    const failure = nodes.get("failures").firstChild;
    assert.equal(failure.firstChild.firstChild.textContent,
        "Review loop launch pr_description example/project#12 <img src=x>");
    assert.match(failure.firstChild.firstChild.href, /\/37368497585\/attempts\/1$/);
    assert.equal(failure.firstChild.children[1].textContent, "Failed");
    assert.ok(row.children.some((node) => node.children?.some((child) => child.textContent === "#12 <untrusted title>")));
    const card = row.children.find((node) => node.tag === "details");
    card.open = true;
    card.events.toggle();
    await new Promise((resolve) => setImmediate(resolve));
    const all = [];
    const visit = (node) => { all.push(node); for (const child of node.children ?? []) visit(child); };
    visit(card);
    assert.ok(all.some((node) => node.textContent === '<img src=x onerror="throw Error()">'));
    assert.equal(all.filter((node) => node.tag === "img" || node.tag === "script").length, 0);
    assert.ok(all.some((node) => node.href === `https://github.com/example/fork/commit/${sha("b")}` && node.rel === "noopener noreferrer"));
    const buttons = () => nodes.get("prs").firstChild.children.find((node) => node.className === "task-grid").children;
    assert.equal(buttons().length, Object.keys(KIND_LABELS).length);
    assert.ok(buttons().every((button) => button.disabled && button.children[0].firstChild.tag === "svg"));
    assert.equal(row.children.filter((node) => node.tag === "select").length, 0);

    Object.assign(state.prs[0], {
        tasks: ["self_review"], actionBlock: "A task is already active",
        phase: { ...state.phases[0], kind: "self_review", stage: "running" },
    });
    renderer.render();
    const active = buttons().filter((button) => button["aria-busy"] === "true");
    assert.equal(active.length, 1);
    assert.equal(active[0]["aria-label"], "Self-review: Running");
    assert.equal(active[0]["data-tone"], "active");

    Object.assign(state.prs[0], { tasks: ["pr_description"], actionBlock: null, phase: null });
    renderer.render();
    const description = buttons().find((button) => button["aria-label"] === "PR Description: Run");
    const dialog = nodes.get("confirm");
    const decline = description.events.click();
    assert.equal(dialog.open, true);
    assert.match(nodes.get("confirm-effects").textContent, /title and description/);
    assert.equal(launches.length, 0);
    dialog.returnValue = "no";
    dialog.open = false;
    dialog.events.close();
    await decline;
    assert.equal(launches.length, 0);

    const accept = description.events.click();
    dialog.returnValue = "yes";
    dialog.open = false;
    dialog.events.close();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(launches, [{ target, kind: "pr_description", confirmed: true }]);
    assert.ok(buttons().every((button) => button.disabled));
    assert.ok(buttons().some((button) => button["aria-label"] === "PR Description: Dispatching" && button["aria-busy"] === "true"));
    releaseLaunch();
    await accept;
    assert.ok(buttons().some((button) => button["aria-label"] === "PR Description: Starting"));
});
