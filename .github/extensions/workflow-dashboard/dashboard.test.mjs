import test from "node:test";
import assert from "node:assert/strict";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { runInNewContext } from "node:vm";
import { request as httpRequest } from "node:http";
import { GitHub, GitHubError, CENTRAL, parseResponse, runGh } from "./github.mjs";
import { Checkpoints, runGit } from "./state.mjs";
import { phaseSummary, targetHistory, actionSummary, commitLink, compareLink, recentTaskLog, RUN_LOG_WINDOW } from "./model.mjs";
import { Dashboard } from "./dashboard.mjs";
import { startServer } from "./server.mjs";
import { KIND_LABELS } from "./kinds.mjs";
import { filterPulls, taskPresentation, completionPresentation, TASK_EFFECTS } from "./prs.mjs";

const sha = (letter) => letter.repeat(40);
const requestId = (letter) => letter.repeat(32);
const target = "example/project#12";
const fixture = (updates = {}, request = {}) => ({
    schema: 2, stage: "running", phase: requestId("f"), generation: 1, iteration: 1,
    expected_sha: sha("a"), intent: { recorded_at: 1001 },
    run: { id: 101, attempt: 1, conclusion: null },
    request: {
        schema: 2, protocol: "git-candidate-v1", repo: "example/project", head_repo: "example/fork", pr: 12,
        request_id: requestId("a"), frozen_at: 1000, frozen_sha: sha("a"),
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

test("PR review results distinguish new comments from existing findings and retain pending-review evidence", () => {
    const comments = [{ path: "src/example.js", line: 12, side: "RIGHT", body: "Missing input guard" }];
    const s = fixture({
        stage: "complete", reason: "viewer_pending_review_confirmed",
        task_completion: { outcome: "pending_review", review_id: 42, comments },
        report: { dispositions: { outcome: "comments", comments } },
    }, { loop_kind: "pr_review", findings: [{ key: "old", path: "src/old.js", body: "Existing comment" }] });
    const phase = phaseSummary(record(s));
    assert.equal(phase.findingCount, 1);
    assert.equal(phase.reviewCommentCount, 1);
    assert.equal(phase.outcome, "pending_review");
    assert.equal(phase.pendingReviewUrl, "https://github.com/example/project/pull/12#pullrequestreview-42");
    assert.equal(completionPresentation(phase).label, "Review ready");
    const item = targetHistory([record(s)], target).phases[0].iterations[0];
    assert.equal(item.kind, "pr_review");
    assert.deepEqual(item.reviewComments, [{ path: "src/example.js", line: 12, body: "Missing input guard" }]);
    assert.throws(() => phaseSummary(record(fixture({
        report: { dispositions: { comments: [{ path: "src/example.js", line: -1, body: "Bad anchor" }] } },
    }))), /malformed iteration evidence/);
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

test("GitHub reads run concurrently with a maximum of five, and missing output stays an error", async () => {
    let active = 0;
    let maximum = 0;
    const github = new GitHub(async () => {
        maximum = Math.max(maximum, ++active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active--;
        return response({ workflow_runs: [], total_count: 0 });
    });
    await Promise.all(["queued", "pending", "waiting", "requested", "in_progress", "completed", "success"].map((status) =>
        github.get(`repos/${CENTRAL}/actions/runs?status=${status}`)));
    assert.equal(maximum, 5);
    assert.equal(github.activeReads, 0);
    assert.equal(github.readQueue.length, 0);
    assert.equal(github.inFlightReads.size, 0);
    const bad = new GitHub(async () => ({ code: 1, stdout: "" }));
    await assert.rejects(bad.get(`repos/${CENTRAL}/actions/runs`), /no HTTP/);
});

test("CLI transport diagnostics are sanitized and retain the no-window and bounded-process settings", async () => {
    for (const diagnostic of [
        "read tcp: wsarecv: An existing connection was forcibly closed by the remote host.",
        "unexpected EOF", "net/http: TLS handshake timeout", "i/o timeout",
    ]) {
        await assert.rejects(runGh(["api", "user"], (command, args, options, callback) => {
            assert.equal(command, "gh");
            assert.equal(options.windowsHide, true);
            assert.equal(options.timeout, 60000);
            assert.equal(options.env.GH_PROMPT_DISABLED, "1");
            assert.equal(options.env.GH_DEBUG, undefined);
            callback({ code: 1 }, "", `${diagnostic}\nAuthorization: Bearer must-not-echo`);
        }), (error) => error instanceof GitHubError && error.transient &&
            /HTTP response/.test(error.message) && !/must-not-echo|Bearer|wsarecv/.test(error.message));
    }
});

test("transient GET failure receives one shared retry, preserves ETags and exposes retry metrics", async () => {
    const calls = [];
    const delays = [];
    const github = new GitHub((args) => runGh(args, (_command, _args, _options, callback) => {
        calls.push(args);
        if (calls.length === 1) callback({ code: 1 }, "", "connection reset by peer");
        else if (calls.length === 2) callback(null, response([], { Etag: '"recovered"' }).stdout, "");
        else callback({ code: 1 }, response(null, {}, 304).stdout, "HTTP 304");
    }), () => 2000000, async (milliseconds) => delays.push(milliseconds));
    const path = `repos/${CENTRAL}/git/matching-refs/heads/review-loop-state`;
    const first = github.get(path);
    const second = github.get(path);
    assert.equal(first, second);
    assert.deepEqual((await first).data, []);
    assert.equal(await first, await second);
    assert.deepEqual(delays, [250]);
    assert.equal(calls.length, 2);
    await github.get(path);
    assert.ok(calls[2].includes('If-None-Match: "recovered"'));
    assert.equal(github.requests, 3);
    assert.equal(github.counted, 1);
    assert.equal(github.cacheHits, 1);
    assert.equal(github.readRetries, 1);
    assert.equal(new Dashboard(github).state().metrics.readRetries, 1);
    assert.equal(github.activeReads, 0);
});

test("repeated transport failures stop after one retry and never expose raw stderr", async () => {
    let calls = 0;
    const github = new GitHub((args) => runGh(args, (_command, _args, _options, callback) => {
        calls++;
        callback({ code: 1 }, "", "unexpected EOF\nAuthorization: Bearer must-not-echo");
    }), () => 2000000, async () => {});
    await assert.rejects(github.get(`repos/${CENTRAL}/actions/runs`), (error) =>
        /closed.*one retry/.test(error.message) && !/must-not-echo/.test(error.message));
    assert.equal(calls, 2);
    assert.equal(github.requests, 2);
    assert.equal(github.readRetries, 1);
    assert.equal(github.counted, 0);
    assert.equal(github.cache.size, 0);
    assert.equal(github.activeReads, 0);
    assert.equal(github.inFlightReads.size, 0);
});

test("authentication, certificate, process and unknown CLI failures do not retry", async () => {
    for (const [failure, stdout, stderr, expected] of [
        [{ code: 1 }, "", "To get started, run gh auth login. must-not-echo", /authenticate/],
        [{ code: 1 }, "", "x509: certificate signed by unknown authority", /certificate/],
        [{ code: 1 }, "", "unknown failure must-not-echo", /exited with code 1/],
        [{ code: "ENOENT" }, "", "", /installed/],
        [{ code: null, killed: true }, "", "", /60-second/],
        [{ code: "ERR_CHILD_PROCESS_STDIO_MAXBUFFER", killed: true }, "", "", /size limit/],
        [{ code: 1 }, "HTTP/2.0 200 OK", "unexpected EOF", /no HTTP/],
    ]) {
        let calls = 0;
        const github = new GitHub((args) => runGh(args, (_command, _args, _options, callback) => {
            calls++;
            callback(failure, stdout, stderr);
        }), () => 2000000, async () => assert.fail("Nontransient failures must not sleep or retry"));
        await assert.rejects(github.get(`repos/${CENTRAL}/actions/runs`), (error) =>
            expected.test(error.message) && !error.transient && !/must-not-echo/.test(error.message));
        assert.equal(calls, 1);
        assert.equal(github.readRetries, 0);
    }
});

test("received HTTP errors are not retried even when stderr contains a transport signature", async () => {
    for (const status of [401, 403, 404, 429, 500]) {
        let calls = 0;
        const github = new GitHub((args) => runGh(args, (_command, _args, _options, callback) => {
            calls++;
            callback({ code: 1 }, response({ secret: "must-not-echo" }, {}, status).stdout, "unexpected EOF");
        }), () => 2000000, async () => assert.fail("HTTP responses must not trigger transport retries"));
        await assert.rejects(github.get(`repos/${CENTRAL}/actions/runs`), (error) =>
            error.message.includes(`HTTP ${status}`) && !/must-not-echo/.test(error.message));
        assert.equal(calls, 1);
        assert.equal(github.readRetries, 0);
    }
});

test("read retries stay inside the five-request concurrency limit", async () => {
    let active = 0;
    let maximum = 0;
    const attempts = new Map();
    const github = new GitHub(async (args) => {
        maximum = Math.max(maximum, ++active);
        const path = args.at(-1);
        const attempt = (attempts.get(path) ?? 0) + 1;
        attempts.set(path, attempt);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active--;
        if (attempt === 1) throw Object.assign(new GitHubError("Connection reset."), { transient: true });
        return response({ workflow_runs: [], total_count: 0 });
    }, () => 2000000, async () => {});
    await Promise.all(["queued", "pending", "waiting", "requested", "in_progress", "completed", "success"].map((status) =>
        github.get(`repos/${CENTRAL}/actions/runs?status=${status}`)));
    assert.equal(maximum, 5);
    assert.equal(github.requests, 14);
    assert.equal(github.readRetries, 7);
    assert.ok([...attempts.values()].every((attempt) => attempt === 2));
    assert.equal(github.activeReads, 0);
});

test("a concurrent rate-limit response stops a scheduled transport retry", async () => {
    let release;
    const github = new GitHub(async () => {
        throw Object.assign(new GitHubError("Connection reset."), { transient: true });
    }, () => 2000000, () => new Promise((resolve) => release = resolve));
    const pending = github.get(`repos/${CENTRAL}/actions/runs`);
    await new Promise((resolve) => setImmediate(resolve));
    github.recordRate({ "retry-after": "120" }, 429);
    release();
    await assert.rejects(pending, /paused/);
    assert.equal(github.requests, 1);
    assert.equal(github.readRetries, 0);
    assert.equal(github.retryAt, 2120000);
});

test("a transport failure during dispatch remains uncertain and never retries", async () => {
    let calls = 0;
    const github = new GitHub((args) => runGh(args, (_command, _args, _options, callback) => {
        calls++;
        callback({ code: 1 }, "", "An existing connection was forcibly closed by the remote host.");
    }), () => 2000000, async () => assert.fail("Dispatch must never retry"));
    await assert.rejects(github.dispatch({
        operation: "launch", target: "open-telemetry/shared-workflows#441",
        loop_kind: "self_review", publication_auth: "fine_grained_pat",
    }), (error) => error.uncertain && /do not blindly retry/.test(error.message));
    assert.equal(calls, 1);
    assert.equal(github.readRetries, 0);
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
    const pending = ["queued", "pending", "waiting", "requested", "in_progress", "completed", "success"].map((status) =>
        github.get(`repos/${CENTRAL}/actions/runs?status=${status}`));
    const settled = Promise.allSettled(pending);
    assert.equal(releases.length, 5);
    releases[0](response({}, { "Retry-After": "120" }, 429, 1));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(github.retryAt, 1120000);
    assert.equal(releases.length, 5);
    releases[1](response({}, { "Retry-After": "30" }, 429, 1));
    for (const release of releases.slice(2)) {
        release(response({ total_count: 0, workflow_runs: [] }, rateHeaders()));
    }
    const results = await settled;
    assert.deepEqual(results.map((read) => read.status), [
        "rejected", "rejected", "fulfilled", "fulfilled", "fulfilled", "rejected", "rejected",
    ]);
    assert.match(results[5].reason.message, /paused/);
    assert.equal(github.retryAt, 1120000);
    assert.equal(github.requests, 5);
    assert.equal(github.activeReads, 0);
    assert.equal(github.readQueue.length, 0);
});

test("the run log paginates past polling runs and stops at the requested boundary", async () => {
    const since = Date.parse("2026-10-08T02:44:33Z");
    const calls = [];
    const github = new GitHub(async (args) => {
        const path = args.at(-1);
        calls.push(path);
        const page = Number(new URL(`https://api.github.com/${path}`).searchParams.get("page"));
        assert.equal(path, `repos/${CENTRAL}/actions/workflows/coordinator.yml/runs?per_page=100&page=${page}`);
        return response({ total_count: 10000, workflow_runs: Array.from({ length: 100 }, (_, index) => ({
            id: page * 100 + index, created_at: new Date(since + (page === 3 && index > 0 ? -1 : 1)).toISOString(),
        })) });
    });
    const runs = await github.recentCoordinators(since);
    assert.equal(runs.length, 201);
    assert.equal(calls.length, 3);
    await assert.rejects(new GitHub(async () => response({ total_count: 1,
        workflow_runs: [{ id: 1, created_at: "missing" }] })).recentCoordinators(since), /invalid recent/);
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

async function stateClient(t) {
    const directory = await mkdtemp(join(tmpdir(), "copilot-state-test-"));
    t.after(() => rm(directory, { recursive: true, force: true }));
    await runGit(["init", "--quiet", "--initial-branch=review-loop-state", directory]);
    const commit = async (files) => {
        for (const [path, value] of Object.entries(files)) {
            await writeFile(join(directory, path), Buffer.isBuffer(value) ? value : JSON.stringify(value));
        }
        await runGit(["-C", directory, "add", "--all"]);
        await runGit(["-C", directory, "-c", "user.name=Tests", "-c", "user.email=tests@example.com",
            "commit", "--quiet", "-m", "State snapshot"]);
    };
    await commit({
        "pr-v2-123-12.json": fixture(),
        [`request-${requestId("b")}.json`]: fixture({ iteration: 0, intent: null, run: null }, { request_id: requestId("b") }),
    });
    const calls = [];
    const store = new Checkpoints({}, async (args) => {
        calls.push(args);
        return runGit(args.map((arg) => arg === `https://github.com/${CENTRAL}.git` ? pathToFileURL(directory).href : arg));
    });
    t.after(() => store.close());
    return { directory, calls, commit, store };
}

test("state fetches a fresh shallow pinned snapshot and defers archives until history", async (t) => {
    const { store, calls, commit } = await stateClient(t);
    const reads = [];
    const blob = store.blob.bind(store);
    store.blob = (entry) => { reads.push(entry.path); return blob(entry); };
    const snapshot = await store.load();
    assert.match(snapshot.sha, /^[0-9a-f]{40}$/);
    assert.equal(snapshot.current.length, 1);
    assert.deepEqual(reads, ["pr-v2-123-12.json"]);
    assert.equal((await readFile(join(store.directory, ".git", "shallow"), "utf8")).trim(), snapshot.sha);
    await store.load();
    assert.equal(calls.filter((args) => args.includes("clone")).length, 1);
    assert.equal(calls.filter((args) => args.includes("fetch")).length, 1);
    assert.ok(calls.filter((args) => args.includes("clone") || args.includes("fetch"))
        .every((args) => args.includes("--depth=1") && args.includes("--no-tags")));
    assert.equal((await store.history(snapshot)).length, 2);
    assert.equal(reads.filter((path) => path.startsWith("request-")).length, 1);
    await store.history(snapshot);
    assert.equal(reads.filter((path) => path.startsWith("request-")).length, 1);
    await commit({ "pr-v2-123-12.json": fixture({ stage: "complete" }) });
    const selected = store.snapshot;
    const logSnapshot = await store.load({ updateSnapshot: false });
    assert.equal(store.snapshot, selected);
    assert.equal(logSnapshot.current[0].state.stage, "complete");
    assert.equal((await store.history(logSnapshot)).length, 2);
    const updated = await store.load();
    assert.notEqual(updated.sha, snapshot.sha);
    assert.equal(updated.current[0].state.stage, "complete");
    assert.equal(snapshot.current[0].state.stage, "running");
});

test("history and fresh fetches serialize their checkouts and cleanup permits reopening", async (t) => {
    const { store, calls, commit } = await stateClient(t);
    const snapshot = await store.load();
    await commit({ "pr-v2-123-12.json": fixture({ stage: "complete" }) });
    let release;
    let started;
    const waiting = new Promise((resolve) => started = resolve);
    const blob = store.blob.bind(store);
    store.blob = async (entry) => {
        if (entry.path.startsWith("request-")) {
            started();
            await new Promise((resolve) => release = resolve);
        }
        return blob(entry);
    };
    const history = store.history(snapshot);
    await waiting;
    const pending = store.load();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(calls.filter((args) => args.includes("fetch")).length, 0);
    release();
    assert.equal((await history).length, 2);
    assert.equal((await pending).current[0].state.stage, "complete");
    const directory = store.directory;
    await store.close();
    await assert.rejects(access(directory), { code: "ENOENT" });
    assert.equal((await store.load()).current[0].state.stage, "complete");
});

test("absent state is explicit empty history, detects initialization, and clears removed state", async (t) => {
    const { directory, store } = await stateClient(t);
    const original = (await runGit(["-C", directory, "rev-parse", "HEAD"])).toString("utf8").trim();
    await runGit(["-C", directory, "update-ref", "refs/heads/review-loop-state-archive", original]);
    await runGit(["-C", directory, "update-ref", "-d", "refs/heads/review-loop-state"]);
    const empty = await store.load();
    assert.equal(empty.sha, null);
    assert.equal(empty.entries.size, 0);
    assert.deepEqual(empty.current, []);
    assert.deepEqual(await store.history(empty), []);
    await runGit(["-C", directory, "update-ref", "refs/heads/review-loop-state", original]);
    assert.equal((await store.load()).current.length, 1);
    await runGit(["-C", directory, "update-ref", "-d", "refs/heads/review-loop-state"]);
    const cleared = await store.load();
    assert.equal(cleared.sha, null);
    assert.deepEqual(cleared.current, []);
});

test("failed Git reads preserve the previous snapshot and do not become an empty success", async (t) => {
    const { store } = await stateClient(t);
    const snapshot = await store.load();
    const run = store.run;
    store.run = async () => { throw new GitHubError("Git authentication failed"); };
    await assert.rejects(store.load(), /authentication failed/);
    assert.equal(store.snapshot, snapshot);
    store.run = run;
    assert.equal((await store.load()).sha, snapshot.sha);
});

test("Git transport hides console windows, preserves exact bytes and sanitizes errors", async () => {
    await assert.rejects(runGit(["fetch"], (command, args, options, callback) => {
        assert.equal(command, "git");
        assert.ok(args.includes("credential.helper=!gh auth git-credential"));
        assert.ok(args.includes("core.autocrlf=false"));
        assert.equal(options.windowsHide, true);
        assert.equal(options.timeout, 60000);
        assert.equal(options.encoding, "buffer");
        assert.equal(options.env.GIT_TERMINAL_PROMPT, "0");
        callback({ code: 128 }, Buffer.alloc(0), Buffer.from("fatal: Authentication failed. secret=must-not-echo"));
    }), (error) => !error.missingState && !error.message.includes("must-not-echo"));
    await assert.rejects(runGit(["fetch"], (_command, _args, _options, callback) => {
        callback({ code: 128 }, Buffer.alloc(0), Buffer.from("fatal: couldn't find remote ref refs/heads/review-loop-state"));
    }), (error) => error.missingState === true);
});

test("state rejects unsafe trees and corrupted checkout bytes before publishing a snapshot", async (t) => {
    const { store } = await stateClient(t);
    const run = store.run;
    for (const tree of [
        `100644 blob ${sha("a")} 2\t../secret.json\0`,
        `120000 blob ${sha("a")} 2\tpr-v2-123-12.json\0`,
        `100644 blob ${sha("a")} 2\tpr-v2-123-12.json`,
    ]) {
        store.run = (args) => args.includes("ls-tree") ? Promise.resolve(Buffer.from(tree)) : run(args);
        await assert.rejects(store.load(), /State tree/);
        assert.equal(store.snapshot, null);
    }
    store.run = run;
    const blob = store.blob.bind(store);
    store.blob = async (entry) => {
        await writeFile(join(store.directory, entry.path), "{}");
        return blob(entry);
    };
    await assert.rejects(store.load(), /Git object identity/);
    assert.equal(store.snapshot, null);
});

test("state rejects malformed JSON and invalid UTF-8 even when Git object identity is correct", async (t) => {
    const { store, commit } = await stateClient(t);
    for (const content of [Buffer.from("{invalid"), Buffer.from('{"text":"\xff"}', "latin1")]) {
        await commit({ "pr-v2-123-12.json": content });
        await assert.rejects(store.load(), /malformed JSON/);
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

test("older candidate contracts remain active only at their exact runtime pin", () => {
    const revision = sha("b");
    const state = fixture({ stage: "running" }, { protocol: "reviewable-v1",
        workflow_revision: revision, workflow_ref: `review-loop-revisions/${revision}` });
    assert.equal(phaseSummary(record(state)).historical, false);
    state.request.workflow_ref = "main";
    assert.equal(phaseSummary(record(state)).historical, true);
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

const launch = (changes = {}) => ({
    id: 202, run_attempt: 1, status: "completed", conclusion: "success",
    display_title: `Review loop launch self_review ${target}`,
    created_at: new Date(1000000).toISOString(), updated_at: new Date(1300000).toISOString(), ...changes,
});
const completion = (changes = {}) => ({
    id: 101, status: "completed", updated_at: new Date(1300000).toISOString(), ...changes,
});

test("the run log combines confirmed batches across worker passes and retains separate launches", () => {
    const publications = [
        { request_id: requestId("a"), sha: sha("c"), effect: "push", confirmed_at: 1100,
            candidate: { parent: sha("a"), commits: [
                { commit: sha("b"), subject: "First fix" }, { commit: sha("c"), subject: "Second fix" },
            ] } },
        { request_id: requestId("b"), sha: sha("d"), effect: "push", confirmed_at: 1200,
            candidate: { parent: sha("c"), commits: [{ commit: sha("d"), subject: "Third fix" }] } },
        { request_id: requestId("c"), sha: sha("d"), effect: "no_change", confirmed_at: 1300 },
    ];
    const first = fixture({ publications: publications.slice(0, 1) }, {
        loop_kind: "self_review", launch_run: { id: 202 },
    });
    const latest = fixture({ stage: "clean", iteration: 3, publications }, {
        loop_kind: "self_review", launch_run: { id: 202 },
        request_id: requestId("c"), frozen_sha: sha("d"), frozen_at: 1250,
    });
    const later = fixture({ phase: requestId("e"), stage: "complete", coordinator_run: { id: 203 } }, {
        request_id: requestId("d"), loop_kind: "pr_description", launch_run: { id: 203 },
        publication: { authorized_at: 1500 }, frozen_at: 1500, frozen_sha: sha("d"),
    });
    const log = recentTaskLog([
        record(first, `request-${requestId("a")}.json`),
        record(latest, `request-${requestId("c")}.json`), record(later),
    ], [completion(), launch(), launch({ id: 203, display_title: `Review loop launch pr_description ${target}`,
        created_at: new Date(1500000).toISOString(), updated_at: new Date(1600000).toISOString() })], 2000000);
    assert.equal(log.entries.length, 2);
    assert.deepEqual(log.warnings, []);
    const task = log.entries[1];
    assert.equal(task.stage, "clean");
    assert.equal(task.runUrl, `https://github.com/${CENTRAL}/actions/runs/202/attempts/1`);
    assert.equal(task.changeRanges.length, 1);
    assert.equal(task.changeRanges[0].commits, 3);
    assert.equal(task.changeRanges[0].url,
        `https://github.com/example/project/pull/12/changes/${sha("a")}..${sha("d")}`);
    assert.equal(Object.hasOwn(task, "iterations"), false);
    assert.deepEqual(log.entries[0].changeRanges, []);
});

test("the two-hour log filters and sorts by finish time, excludes ongoing tasks and can extend further back", () => {
    const now = RUN_LOG_WINDOW + 2000000;
    const old = fixture({ stage: "complete", coordinator_run: { id: 401 } });
    const boundary = fixture({ phase: requestId("b"), stage: "complete",
        coordinator_run: { id: 402 },
        report: { candidate: { commit: sha("c"), parent: sha("a"), changed: true } } }, {
        request_id: requestId("b"), publication: { authorized_at: 2000 }, frozen_at: 2000,
    });
    const recentPush = fixture({ phase: requestId("c"), stage: "failed", publications: [
        { request_id: requestId("c"), sha: sha("b"), effect: "push", confirmed_at: 2100,
            candidate: { parent: sha("a") } },
    ], coordinator_run: { id: 403 } }, { request_id: requestId("c") });
    const ongoing = fixture({ phase: requestId("d"), stage: "waiting_ci" }, {
        request_id: requestId("d"), launch_run: { id: 204 },
    });
    const records = [
        record(old, "request-old.json"), record(boundary, "request-boundary.json"),
        record(recentPush, "request-recent.json"), record(ongoing, "pr-ongoing.json"),
    ];
    const runs = [
        completion({ id: 401, updated_at: new Date(1999999).toISOString() }),
        completion({ id: 402, updated_at: new Date(2000000).toISOString() }),
        completion({ id: 403, updated_at: new Date(2100000).toISOString() }),
        launch({ id: 301, created_at: new Date(0).toISOString(),
            updated_at: new Date(2200000).toISOString(), conclusion: "failure" }),
        launch({ id: 302, updated_at: new Date(1999999).toISOString() }),
        launch({ id: 303, created_at: new Date(2100000).toISOString(), display_title: "Review loop tick abc" }),
        launch({ id: 204, status: "completed", updated_at: new Date(2300000).toISOString() }),
    ];
    const log = recentTaskLog(records, runs, now);
    assert.equal(log.entries.length, 3);
    assert.equal(log.entries.some((entry) => entry.phase === old.phase), false);
    assert.deepEqual(log.entries.find((entry) => entry.phase === boundary.phase).changeRanges, []);
    assert.equal(log.entries.find((entry) => entry.phase === recentPush.phase).changeRanges.length, 1);
    assert.equal(log.entries.some((entry) => entry.phase === ongoing.phase), false);
    assert.deepEqual(log.entries.map((entry) => entry.finished), [2200000, 2100000, 2000000]);
    const failed = log.entries.find((entry) => !entry.evidence);
    assert.equal(failed.conclusion, "failure");
    assert.equal(failed.target, target);
    assert.deepEqual(failed.changeRanges, []);
    const extended = recentTaskLog(records, runs, now, 6 * 3600000);
    assert.equal(extended.entries.some((entry) => entry.phase === old.phase), true);
    assert.equal(extended.entries.some((entry) => entry.phase === ongoing.phase), false);
    const cancelled = recentTaskLog([record(fixture({ stage: "cancelled", cancelled_at: 2300 }))], [], now);
    assert.equal(cancelled.entries[0].finished, 2300000);
});

test("missing and discontinuous publication boundaries do not invent a cumulative range", () => {
    const publications = [
        { request_id: requestId("a"), sha: sha("b"), effect: "push", candidate: { parent: sha("a") } },
        { request_id: requestId("b"), sha: sha("d"), effect: "push", candidate: { parent: sha("c") } },
        { request_id: requestId("c"), sha: sha("e"), effect: "push" },
    ];
    const log = recentTaskLog([record(fixture({ stage: "complete", publications }))], [completion()], 2000000);
    const ranges = log.entries[0].changeRanges;
    assert.equal(ranges.length, 3);
    assert.equal(ranges[0].url, `https://github.com/example/project/pull/12/changes/${sha("a")}..${sha("b")}`);
    assert.equal(ranges[1].url, `https://github.com/example/project/pull/12/changes/${sha("c")}..${sha("d")}`);
    assert.equal(ranges[2].url, null);
    assert.equal(ranges[2].commitUrl, `https://github.com/example/fork/commit/${sha("e")}`);
    assert.equal(recentTaskLog([record({})], [], 2000000).warnings.length, 1);
});

test("worker Actions associate by recorded identities and accept completed runs", () => {
    const phase = phaseSummary(record(fixture({}, { launch_run: { id: 202 } })));
    const run = { id: 202, run_attempt: 1, status: "queued", name: "Coordinator" };
    assert.deepEqual(actionSummary(run, [phase]).targets, [target]);
    assert.deepEqual(actionSummary({ ...run, id: 303 }, [phase]).targets, []);
    assert.equal(actionSummary({ ...run, status: "completed" }, [phase]).status, "completed");
    assert.throws(() => actionSummary({ ...run, status: "unrecognized" }, [phase]), /invalid/);
});

function fakeDashboard(now = () => 2000000) {
    const github = {
        requests: 0, counted: 0, cacheHits: 0, rate: { limit: 5000, remaining: 4990, reset: 9000000 },
        get: async (path) => {
            github.requests++; github.counted++;
            assert.ok([101, 102].some((id) => path === `repos/${CENTRAL}/actions/runs/${id}`));
            return { data: path.endsWith("/102") ? completion({ id: 102 }) : { id: 101, status: "in_progress" } };
        },
        recentCoordinators: async () => [],
    };
    const dashboard = new Dashboard(github, now);
    const snapshot = { sha: sha("a"), current: [record(fixture())] };
    dashboard.checkpoints = {
        snapshot, load: async () => { github.requests++; github.counted++; return snapshot; },
        history: async () => snapshot.current,
    };
    return dashboard;
}

test("a fresh state branch absence loads an empty task snapshot without inventing history", async () => {
    const github = new GitHub(async (args) => {
        const path = args.at(-1);
        if (path.endsWith("/git/matching-refs/heads/review-loop-state")) return response([]);
        if (path.includes("/actions/workflows/coordinator.yml/runs?per_page=100")) return response({ total_count: 0, workflow_runs: [] });
        throw new Error(`Unexpected dashboard read ${path}`);
    });
    const dashboard = new Dashboard(github, () => 2000000);
    dashboard.checkpoints.load = async () => {
        dashboard.checkpoints.snapshot = { sha: null, entries: new Map(), current: [], history: null };
        return dashboard.checkpoints.snapshot;
    };
    const state = await dashboard.refresh();
    assert.equal(state.error, null);
    assert.equal(state.loadedAt, 2000000);
    assert.equal(state.snapshot, null);
    assert.equal(state.auto, true);
    assert.deepEqual(state.phases, []);
    assert.deepEqual(state.actions, []);
    await assert.rejects(dashboard.history(target), /not in/);
});

test("refresh coalesces, keeps stale data on failure, and respects manual/low-capacity mode", async () => {
    const dashboard = fakeDashboard();
    const first = dashboard.refresh();
    assert.equal(first, dashboard.refresh());
    const result = await first;
    assert.equal(result.cost, 2);
    assert.equal(result.phases.length, 1);
    dashboard.github.get = async () => { throw new GitHubError("HTTP 403 access denied"); };
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

test("run-log reads coalesce, stay stale on failure and retry without pausing PR refresh", async () => {
    const dashboard = fakeDashboard();
    const archived = fixture({ phase: requestId("b"), stage: "complete", run: { id: 102 } }, {
        request_id: requestId("b"), launch_run: { id: 202 },
    });
    let archives = 0;
    let runs = 0;
    const history = async () => {
        archives++;
        return [...dashboard.checkpoints.snapshot.current, record(archived, "request-archived.json")];
    };
    dashboard.checkpoints.history = history;
    dashboard.github.recentCoordinators = async () => {
        runs++;
        return [launch(), launch({ id: 203, conclusion: "failure" })];
    };
    const initial = await dashboard.refresh();
    assert.equal(initial.runLogLoadedAt, null);
    assert.deepEqual(initial.runLog, []);
    assert.equal(archives, 0);
    assert.equal(runs, 0);
    const pending = dashboard.refreshRunLog();
    assert.equal(pending, dashboard.refreshRunLog());
    assert.equal(dashboard.state().runLogLoading, true);
    assert.equal(dashboard.state().loading, false);
    const first = await pending;
    assert.equal(first.runLog.length, 2);
    assert.equal(first.runLogLoadedAt, 2000000);
    assert.equal(first.runLogError, null);
    assert.equal(first.phases.length, 1);
    assert.equal(first.runLogLoading, false);
    assert.equal(archives, 1);
    assert.equal(runs, 1);
    assert.deepEqual((await dashboard.refresh()).runLog, first.runLog);
    assert.equal(archives, 1);
    assert.equal(runs, 1);
    dashboard.checkpoints.history = async () => { throw new Error("Archive unreadable"); };
    const stale = await dashboard.refreshRunLog();
    assert.equal(stale.error, null);
    assert.equal(stale.phases.length, 1);
    assert.deepEqual(stale.runLog, first.runLog);
    assert.equal(stale.runLogLoadedAt, first.runLogLoadedAt);
    assert.match(stale.runLogError, /Archive unreadable/);
    assert.equal(stale.auto, true);
    assert.equal(stale.runLogLoading, false);
    assert.equal((await dashboard.refresh()).auto, true);
    assert.equal(runs, 2);
    dashboard.checkpoints.history = history;
    const recovered = await dashboard.refreshRunLog();
    assert.equal(recovered.runLogError, null);
    assert.equal(recovered.runLog.length, 2);
    assert.equal(archives, 2);
    assert.equal(runs, 3);
});

test("PR refresh does not wait for an in-flight on-demand run log", { timeout: 2000 }, async () => {
    const dashboard = fakeDashboard();
    let release;
    dashboard.github.recentCoordinators = () => new Promise((resolve) => release = resolve);
    const pending = dashboard.refreshRunLog();
    const state = await dashboard.refresh();
    assert.equal(state.loading, false);
    assert.equal(state.runLogLoading, true);
    assert.equal(state.phases.length, 1);
    release([]);
    assert.equal((await pending).runLogLoading, false);
});

test("run-log hours extend the completion cutoff for tasks launched before the listing window", async () => {
    const now = 30000000;
    const dashboard = fakeDashboard(() => now);
    dashboard.checkpoints.snapshot.current = [record(fixture({
        stage: "complete", run: { id: 102 },
    }))];
    const cutoffs = [];
    dashboard.github.recentCoordinators = async (since) => { cutoffs.push(since); return []; };
    dashboard.github.get = async (path) => {
        assert.equal(path, `repos/${CENTRAL}/actions/runs/102`);
        return { data: completion({ id: 102, updated_at: new Date(now - 3 * 3600000).toISOString() }) };
    };
    const initial = await dashboard.refreshRunLog();
    assert.equal(initial.runLogHours, 2);
    assert.equal(initial.runLog.length, 0);
    const extended = await dashboard.refreshRunLog(6);
    assert.equal(extended.runLogHours, 6);
    assert.equal(extended.runLog.length, 1);
    assert.equal(extended.runLog[0].finished, now - 3 * 3600000);
    assert.deepEqual(cutoffs, [now - 2 * 3600000, now - 6 * 3600000]);
    assert.equal((await dashboard.refreshRunLog()).runLogHours, 6);
    assert.throws(() => dashboard.refreshRunLog(1.5), /positive whole number/);
    assert.throws(() => dashboard.refreshRunLog(0), /positive whole number/);
});

test("failed completion reads settle before releasing the run-log lock", async () => {
    const dashboard = fakeDashboard();
    dashboard.checkpoints.snapshot.current = [
        record(fixture({ stage: "complete", run: { id: 102 } })),
        record(fixture({ stage: "complete", run: { id: 103 } }, { pr: 13 })),
    ];
    let release;
    dashboard.github.get = async (path) => {
        if (path.endsWith("/102")) throw new Error("Completion unavailable");
        await new Promise((resolve) => release = resolve);
        return { data: completion({ id: 103 }) };
    };
    const pending = dashboard.refreshRunLog();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(dashboard.state().runLogLoading, true);
    assert.equal(pending, dashboard.refreshRunLog());
    release();
    const result = await pending;
    assert.equal(result.runLogLoading, false);
    assert.match(result.runLogError, /Completion unavailable/);
});

test("worker reads deduplicate active run IDs and preserve queued, running and completed evidence", async () => {
    const dashboard = fakeDashboard();
    dashboard.checkpoints.snapshot.current = [
        record(fixture({ stage: "dispatched" })),
        record(fixture({}, { pr: 13 }), "pr-v2-123-13.json"),
        record(fixture({ stage: "waiting_ci", run: { id: 202 } }, { pr: 14 }), "pr-v2-123-14.json"),
        record(fixture({ stage: "complete", run: { id: 303 } }, { pr: 15 }), "pr-v2-123-15.json"),
    ];
    const calls = [];
    let status = "queued";
    dashboard.github.get = async (path) => {
        calls.push(path);
        return { data: { id: 101, status } };
    };
    const queued = await dashboard.refresh();
    assert.deepEqual(calls, [`repos/${CENTRAL}/actions/runs/101`]);
    assert.equal(queued.actions.length, 1);
    assert.deepEqual(queued.actions[0].targets, [target, "example/project#13"]);
    const pr = { sha: sha("a"), tasks: ["copilot_review"], phase: queued.phases.find((phase) => phase.target === target) };
    assert.equal(taskPresentation(pr, "copilot_review", true, queued.actions).label, "Queued");
    status = "in_progress";
    const running = await dashboard.refresh();
    assert.equal(taskPresentation(pr, "copilot_review", true, running.actions).label, "Running");
    status = "completed";
    assert.equal((await dashboard.refresh()).actions[0].status, "completed");
    for (const entry of dashboard.checkpoints.snapshot.current.slice(0, 2)) entry.state.stage = "waiting_ci";
    const waiting = await dashboard.refresh();
    assert.deepEqual(waiting.actions, []);
    assert.equal(calls.length, 3);
    pr.phase = waiting.phases.find((phase) => phase.target === target);
    assert.equal(taskPresentation(pr, "copilot_review", true, waiting.actions).label, "Waiting for CI");
});

test("a mismatched worker response pauses refresh and retains the last confirmed task evidence", async () => {
    const dashboard = fakeDashboard();
    const previous = await dashboard.refresh();
    dashboard.github.get = async () => ({ data: { id: 202, status: "queued" } });
    const failed = await dashboard.refresh();
    assert.match(failed.error, /different worker run/);
    assert.equal(failed.loadedAt, previous.loadedAt);
    assert.deepEqual(failed.actions, previous.actions);
    assert.equal(failed.auto, false);
});

test("worker reads settle before releasing the refresh lock after a failure", async () => {
    const dashboard = fakeDashboard();
    dashboard.checkpoints.snapshot.current.push(
        record(fixture({ run: { id: 202 } }, { pr: 13 }), "pr-v2-123-13.json"));
    let release;
    dashboard.github.get = async (path) => {
        if (path.endsWith("/101")) throw new Error("Worker status unavailable");
        return new Promise((resolve) => release = () => resolve({ data: { id: 202, status: "queued" } }));
    };
    const first = dashboard.refresh();
    let done = false;
    first.then(() => done = true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(done, false);
    assert.equal(dashboard.refresh(), first);
    release();
    const state = await first;
    assert.match(state.error, /Worker status unavailable/);
    assert.equal(state.loading, false);
    assert.equal(state.auto, false);
});

test("manual refresh stays off without a notice, but read failures still report an automatic pause", async () => {
    const dashboard = fakeDashboard();
    const manual = dashboard.setAuto(false);
    assert.equal(manual.auto, false);
    assert.equal(manual.pauseReason, null);
    dashboard.heartbeat("manual-viewer", true);
    assert.equal(dashboard.timer, null);
    const refreshed = await dashboard.refresh();
    assert.equal(refreshed.auto, false);
    assert.equal(refreshed.pauseReason, null);
    assert.equal(dashboard.timer, null);
    dashboard.github.get = async () => { throw new Error("Manual read unavailable"); };
    const failed = await dashboard.refresh();
    assert.equal(failed.auto, false);
    assert.match(failed.error, /Manual read unavailable/);
    assert.equal(failed.pauseReason, "Automatic refresh paused after a failed GitHub read.");
    const recovered = dashboard.setAuto(false);
    assert.equal(recovered.pauseReason, null);
    assert.match(recovered.error, /Manual read unavailable/);
});

test("checkpoint reads settle before releasing refresh coalescing after a failure", async () => {
    const dashboard = fakeDashboard();
    let release;
    const waiting = new Promise((resolve) => release = resolve);
    dashboard.checkpoints.load = async () => { await waiting; throw new Error("State unavailable"); };
    const first = dashboard.refresh();
    let done = false;
    first.then(() => done = true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(done, false);
    assert.equal(dashboard.refresh(), first);
    release();
    const state = await first;
    assert.match(state.error, /State unavailable/);
    assert.equal(state.loadedAt, null);
    assert.equal(state.loading, false);
    assert.equal(state.auto, false);
});



test("automatic refresh pauses for slow steady-state cycles and low capacity, not request count", async () => {
    let now = 2000000;
    const dashboard = fakeDashboard(() => now);
    await dashboard.refresh();
    dashboard.checkpoints.load = async () => { now += 10001; return dashboard.checkpoints.snapshot; };
    assert.equal((await dashboard.refresh()).auto, false);
    assert.match(dashboard.state().pauseReason, /10 seconds/);
    const costly = fakeDashboard();
    await costly.refresh();
    costly.github.get = async () => {
        costly.github.counted += 18;
        return { data: { id: 101, status: "in_progress" } };
    };
    const highCost = await costly.refresh();
    assert.equal(highCost.cost, 19);
    assert.equal(highCost.auto, true);
    assert.equal(highCost.pauseReason, null);
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
        if (loads === 1) await new Promise((resolve) => release = resolve);
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

test("task buttons wrap between words and move to another row instead of shrinking below a word", async () => {
    const styles = await readFile(new URL("styles.css", import.meta.url), "utf8");
    const grid = styles.match(/^\.task-grid \{([^}]+)\}/m)?.[1];
    const button = styles.match(/^\.task-button \{([^}]+)\}/m)?.[1];
    assert.match(grid, /flex-wrap: wrap;/);
    assert.match(button, /min-width: min-content;/);
    assert.match(button, /overflow-wrap: normal;/);
    assert.match(button, /white-space: normal;/);
});

test("busy icons rotate normally and use a static hourglass under reduced motion", async () => {
    const styles = await readFile(new URL("styles.css", import.meta.url), "utf8");
    const spinner = styles.match(/^\.task-icon \.spinner \{([^}]+)\}/m)?.[1];
    assert.ok(spinner);
    assert.match(spinner, /animation: task-spin 1s linear infinite;/);
    assert.match(styles, /@keyframes task-spin \{ to \{ transform: rotate\(360deg\); \} \}/);
    const reduced = styles.match(/@media \(prefers-reduced-motion: reduce\) \{\s*\.task-icon \.spinner \{([^}]+)\}/)?.[1];
    assert.ok(reduced);
    for (const declaration of ["animation: none;", "border: 0;", "border-radius: 0;", "background: currentColor;"]) {
        assert.ok(reduced.includes(declaration), `Reduced-motion icon requires ${declaration}`);
    }
    assert.match(reduced, /clip-path: polygon\(0 0, 100% 0, 100% 20%, 65% 50%, 100% 80%, 100% 100%, 0 100%, 0 80%, 35% 50%, 0 20%\);/);
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
    assert.equal(dashboard.state().runLogLoadedAt, null);
    assert.equal((await fetch(new URL("api/run-log", server.url), { method: "POST" })).status, 200);
    assert.equal(dashboard.state().runLogLoadedAt, 2000000);
    const extendedLog = await fetch(new URL("api/run-log?hours=6", server.url), { method: "POST" });
    assert.equal(extendedLog.status, 200);
    assert.equal((await extendedLog.json()).runLogHours, 6);
    const invalidWindow = await fetch(new URL("api/run-log?hours=1.5", server.url), { method: "POST" });
    assert.equal(invalidWindow.status, 400);
    assert.match((await invalidWindow.json()).error, /positive whole number/);
    dashboard.github.recentCoordinators = async () => { throw new Error("Run log unavailable"); };
    const failedLog = await fetch(new URL("api/run-log", server.url), { method: "POST" });
    assert.equal(failedLog.status, 502);
    assert.match((await failedLog.json()).runLogError, /Run log unavailable/);
    assert.equal((await fetch(new URL("api/refresh", server.url), { method: "POST" })).status, 200);
    const history = await fetch(new URL(`api/history?target=${encodeURIComponent(target)}`, server.url));
    assert.equal(history.status, 200);
    assert.equal((await history.json()).phases.length, 1);
    assert.equal((await fetch(new URL("api/history?target=../secret", server.url))).status, 400);
    assert.equal((await fetch(new URL("api/cancel", server.url), { method: "POST" })).status, 404);
    dashboard.github.get = async () => { throw new Error("GitHub unavailable"); };
    const stale = await fetch(new URL("api/refresh", server.url), { method: "POST" });
    assert.equal(stale.status, 502);
    assert.equal((await stale.json()).phases.length, 1);
});

async function rendererFixture(fetch, timers = { setInterval() {}, clearInterval() {} }, navigator = {}) {
    class Node {
        constructor(tag = "") {
            this.tag = tag; this.children = []; this.events = {}; this.style = {}; this.value = ""; this.isConnected = true;
        }
        append(...nodes) {
            for (const node of nodes) node.parentElement = this;
            this.children.push(...nodes);
        }
        replaceChildren(...nodes) { this.children = nodes; }
        get firstChild() { return this.children[0]; }
        addEventListener(name, action) { this.events[name] = action; }
        setAttribute(name, value) { this[name] = value; }
        getAttribute(name) { return this[name] ?? null; }
        contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
        focus() { document.activeElement = this; }
        select() { document.activeElement = this; }
        remove() {
            this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
            this.isConnected = false;
        }
        getBoundingClientRect() {
            return ["task-tooltip", "link-menu"].includes(this.className) ? { width: 320, height: 100 }
                : { left: 20, top: 100, bottom: 140 };
        }
    }
    const html = await readFile(new URL("index.html", import.meta.url), "utf8");
    assert.doesNotMatch(html, /<dialog\b|method="dialog"|id="troubleshooting"/);
    const nodes = new Map(Array.from(html.matchAll(/\bid="([^"]+)"/g), (match) => [match[1], new Node()]));
    nodes.get("link-menu").className = "link-menu";
    nodes.get("link-menu").hidden = true;
    nodes.get("link-menu").append(nodes.get("copy-link"));
    for (const [input] of html.matchAll(/<input\b[^>]*>/g)) {
        const id = input.match(/\bid="([^"]+)"/)?.[1];
        if (id) nodes.get(id).checked = /\bchecked\b/.test(input);
    }
    const document = {
        hidden: false, getElementById: (id) => nodes.get(id), createElement: (tag) => new Node(tag),
        activeElement: null, body: new Node("body"), documentElement: { clientWidth: 800, clientHeight: 600 }, events: {},
        createTextNode: (text) => Object.assign(new Node("#text"), { textContent: text }),
        addEventListener(name, action) { this.events[name] = action; },
    };
    const window = { events: {}, addEventListener(name, action) { this.events[name] = action; } };
    const script = await readFile(new URL("app.mjs", import.meta.url), "utf8");
    const renderer = await runInNewContext(`(async () => { ${script.replace(/^import .+;\r?$/gm, "")} return { render, heartbeat, refresh }; })()`, {
        KIND_LABELS, filterPulls, taskPresentation, completionPresentation, TASK_EFFECTS, document, window, navigator, ...timers, fetch,
    });
    return { renderer, nodes, document, window, html };
}

function taskButtons(card) {
    return card.children.find((node) => node.className === "task-grid").children.map((control) => control.firstChild);
}

function tooltipFor(button) {
    return button.parentElement.children[1];
}

function rendererState(repository = "example/project") {
    return {
        repository, repositories: ["example/project", "example/other"],
        auto: true, loading: false, loadedAt: 2000000, prLoadedAt: 2000000, snapshot: sha("a"),
        error: null, prError: null, pauseReason: null, warnings: [], prWarnings: [],
        cost: 0, rate: null, metrics: { requests: 10, cacheHits: 10 },
        workflowReady: true, viewer: { login: "trask" }, phases: [], actions: [],
        runLogHours: 2, runLog: [], runLogLoadedAt: 2000000,
        prs: [{
            target: `${repository}#12`, number: 12, title: `PR in ${repository}`,
            url: `https://github.com/${repository}/pull/12`, author: "trask", mine: true,
            sha: sha("a"), draft: false, dashboardStatus: "missing", routeLabel: "Dashboard missing",
            reviewers: [], tasks: Object.keys(KIND_LABELS), phase: null, canCancel: false, actionBlock: null,
        }],
    };
}

test("right-click Copy link copies the selected PR URL without navigating or dispatching", async () => {
    const state = rendererState();
    const calls = [];
    const { nodes, document } = await rendererFixture(async (path) => {
        calls.push(path);
        return { ok: true, json: async () => state };
    });
    let clipboard;
    document.execCommand = (command) => {
        assert.equal(command, "copy");
        assert.equal(document.activeElement.tag, "textarea");
        clipboard = document.activeElement.textContent;
        return true;
    };
    const prLink = nodes.get("prs").firstChild.firstChild.firstChild;
    let prevented = false;
    let stopped = false;
    prLink.events.contextmenu({
        clientX: 790, clientY: 590,
        preventDefault() { prevented = true; }, stopPropagation() { stopped = true; },
    });
    assert.equal(prevented, true);
    assert.equal(stopped, true);
    assert.equal(nodes.get("link-menu").hidden, false);
    assert.equal(nodes.get("link-menu").style.left, "472px");
    assert.equal(nodes.get("link-menu").style.top, "492px");
    assert.equal(document.activeElement, nodes.get("copy-link"));
    await nodes.get("copy-link").events.click();
    assert.equal(clipboard, state.prs[0].url);
    assert.equal(nodes.get("link-menu").hidden, true);
    assert.equal(document.activeElement, prLink);
    assert.equal(document.body.children.length, 0);
    assert.equal(prLink.target, "_blank");
    assert.equal(prLink.events.click, undefined);
    assert.deepEqual(calls, ["/api/visibility?visible=true", "/api/state"]);
});

test("link menu dismisses on Escape, outside clicks and refresh, and copies run-log links", async () => {
    const state = rendererState();
    state.runLogLoadedAt = 2000000;
    state.runLog = [{
        number: 12, title: state.prs[0].title, url: state.prs[0].url, stage: "completed",
        kind: "self_review", changeRanges: [],
    }];
    const { renderer, nodes, document, window } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    const prLink = nodes.get("prs").firstChild.firstChild.firstChild;
    const open = (node = prLink) => node.events.contextmenu({
        clientX: 30, clientY: 140, preventDefault() {}, stopPropagation() {},
    });
    open();
    document.events.keydown({ key: "Escape" });
    assert.equal(nodes.get("link-menu").hidden, true);
    assert.equal(document.activeElement, prLink);
    open();
    document.events.pointerdown({ target: nodes.get("copy-link") });
    assert.equal(nodes.get("link-menu").hidden, false);
    document.events.pointerdown({ target: nodes.get("search") });
    assert.equal(nodes.get("link-menu").hidden, true);
    open();
    renderer.render();
    assert.equal(nodes.get("link-menu").hidden, true);
    const logLink = nodes.get("run-log").firstChild.firstChild.firstChild;
    open(logLink);
    let clipboard;
    document.execCommand = () => { clipboard = document.activeElement.textContent; return true; };
    await nodes.get("copy-link").events.click();
    assert.equal(clipboard, logLink.href);
    open(logLink);
    window.events.blur();
    assert.equal(nodes.get("link-menu").hidden, true);
});

test("Copy link falls back to the Clipboard API and surfaces denied clipboard access", async () => {
    const state = rendererState();
    let clipboard;
    let denied = false;
    const { nodes, document } = await rendererFixture(async () => ({ ok: true, json: async () => state }),
        undefined, { clipboard: { writeText: async (text) => {
            if (denied) throw new Error("Permission denied.");
            clipboard = text;
        } } });
    document.execCommand = () => false;
    const prLink = nodes.get("prs").firstChild.firstChild.firstChild;
    const open = () => prLink.events.contextmenu({
        clientX: 30, clientY: 140, preventDefault() {}, stopPropagation() {},
    });
    open();
    await nodes.get("copy-link").events.click();
    assert.equal(clipboard, prLink.href);
    assert.equal(document.body.children.length, 0);
    denied = true;
    open();
    await nodes.get("copy-link").events.click();
    assert.equal(nodes.get("error").hidden, false);
    assert.equal(nodes.get("error").textContent, "Could not copy link. Permission denied.");
    assert.equal(nodes.get("link-menu").hidden, true);
    assert.equal(document.body.children.length, 0);
});

test("compact run-log rows show PR titles and exact changes regardless of author or search filters", async () => {
    const state = rendererState();
    state.runLogLoadedAt = 2000000;
    const pushed = recentTaskLog([record(fixture({ stage: "clean", publications: [
        { request_id: requestId("a"), sha: sha("c"), effect: "push",
            candidate: { parent: sha("a"), commits: [
                { commit: sha("b"), subject: "First" }, { commit: sha("c"), subject: "Second" },
            ] } },
    ] }, { loop_kind: "self_review", launch_run: { id: 202 } }))],
    [completion()], 2000000).entries[0];
    state.runLog = [pushed, ...recentTaskLog([], [launch({ conclusion: "failure" })], 2000000).entries]
        .map((task) => ({ ...task, number: 12, title: state.prs[0].title }));
    const { renderer, nodes } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    nodes.get("search").value = "no matching PR";
    renderer.render();
    assert.equal(nodes.get("prs").firstChild.textContent, "No open PRs match these filters.");
    assert.equal(nodes.get("run-log").children.length, 2);
    assert.equal(nodes.get("run-log-count").hidden, false);
    assert.equal(nodes.get("run-log-count").textContent, 2);
    const row = nodes.get("run-log").firstChild;
    assert.equal(row.firstChild.firstChild.textContent, "#12 PR in example/project");
    assert.equal(row.children.length, 2);
    assert.equal(row.className, "run-log-entry");
    const links = row.children[1];
    const changes = links.children.find((node) => node.textContent.startsWith("Changes"));
    assert.equal(changes.href, `https://github.com/example/project/pull/12/changes/${sha("a")}..${sha("c")}`);
    assert.match(changes.textContent, /2 commits/);
    const failed = nodes.get("run-log").children[1];
    assert.equal(failed.firstChild.children[1].textContent, "Launch failure");
    assert.equal(failed.children.length, 2);
    assert.equal(failed.children[1].children.length, 2);
    assert.equal(failed.children[1].children.some((node) => node.tag === "a"), false);
    const styles = await readFile(new URL("styles.css", import.meta.url), "utf8");
    assert.match(styles, /\.run-log-entry \{ padding: 8px 0;/);
    state.runLogError = "History read failed";
    renderer.render();
    assert.equal(nodes.get("run-log-error").hidden, false);
    assert.match(nodes.get("run-log-error").textContent, /stale.*History read failed/);
    assert.equal(nodes.get("run-log").children.length, 2);
});

test("run log loads automatically after PR rendering, leaves PR controls usable and retries locally", async () => {
    let current = { ...rendererState(), runLog: [], runLogLoadedAt: null, runLogError: null, runLogLoading: false };
    let release;
    let reads = 0;
    const { renderer, nodes, html } = await rendererFixture(async (path) => {
        if (path.startsWith("/api/run-log")) {
            reads++;
            return new Promise((resolve) => release = resolve);
        }
        return { ok: true, json: async () => structuredClone(current) };
    });
    assert.equal(reads, 1);
    assert.equal(nodes.get("prs").firstChild.firstChild.firstChild.textContent, "#12 PR in example/project");
    assert.equal(nodes.get("load-run-log").textContent, "Loading run log...");
    assert.equal(nodes.get("run-log-count").textContent, "");
    assert.equal(nodes.get("run-log-count").hidden, true);
    assert.match(html, /<span id="run-log-count"[^>]*\bhidden\b/);
    await nodes.get("load-run-log").events.click();
    assert.equal(reads, 1);
    assert.equal(nodes.get("load-run-log").disabled, true);
    assert.equal(nodes.get("load-run-log").textContent, "Loading run log...");
    assert.equal(nodes.get("run-log-count").hidden, true);
    assert.equal(nodes.get("run-log")["aria-busy"], "true");
    assert.equal(nodes.get("loading").hidden, true);
    assert.equal(nodes.get("prs")["aria-busy"], "false");
    for (const id of ["repo", "refresh", "auto"]) assert.notEqual(nodes.get(id).disabled, true);
    current = { ...current, runLogLoadedAt: 2000000 };
    release({ ok: true, json: async () => structuredClone(current) });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(nodes.get("load-run-log").disabled, false);
    assert.equal(nodes.get("load-run-log").textContent, "Refresh run log");
    assert.equal(nodes.get("run-log-count").hidden, false);
    assert.equal(nodes.get("run-log-count").textContent, 0);
    assert.equal(nodes.get("run-log")["aria-busy"], "false");
    await renderer.refresh();
    assert.equal(reads, 1);
    const failed = nodes.get("load-run-log").events.click();
    current = { ...current, runLogError: "Archive unreadable" };
    release({ ok: false, status: 502, json: async () => structuredClone(current) });
    await failed;
    assert.match(nodes.get("run-log-error").textContent, /stale.*Archive unreadable/);
    assert.equal(nodes.get("run-log-count").hidden, false);
    assert.equal(nodes.get("run-log-count").textContent, 0);
    assert.equal(nodes.get("error").hidden, true);
    assert.equal(nodes.get("load-run-log").disabled, false);
    const retry = nodes.get("load-run-log").events.click();
    current = { ...current, runLogError: null };
    release({ ok: true, json: async () => structuredClone(current) });
    await retry;
    assert.equal(reads, 3);
    assert.equal(nodes.get("run-log-error").hidden, true);
});

test("initial run-log loading waits for the main refresh and Hours back reloads the selected window", async () => {
    let current = { ...rendererState(), loadedAt: null, prLoadedAt: null, runLogLoadedAt: null };
    let releaseMain;
    let mainStarted;
    const started = new Promise((resolve) => mainStarted = resolve);
    const logWindows = [];
    const fixturePending = rendererFixture(async (path) => {
        if (path === "/api/refresh") {
            mainStarted();
            await new Promise((resolve) => releaseMain = resolve);
            current = { ...rendererState(), runLogLoadedAt: null };
        }
        if (path.startsWith("/api/run-log")) {
            assert.ok(current.prLoadedAt);
            assert.ok(current.loadedAt);
            const hours = Number(new URL(path, "http://127.0.0.1").searchParams.get("hours"));
            logWindows.push(hours);
            current = { ...current, runLogLoadedAt: 2000000, runLogHours: hours };
        }
        return { ok: true, json: async () => structuredClone(current) };
    });
    await started;
    assert.deepEqual(logWindows, []);
    releaseMain();
    const { nodes } = await fixturePending;
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(logWindows, [2]);
    assert.equal(nodes.get("prs").firstChild.firstChild.firstChild.textContent, "#12 PR in example/project");
    nodes.get("run-log-hours").value = "6";
    nodes.get("run-log-hours").events.change();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(logWindows, [2, 6]);
    assert.equal(nodes.get("run-log-hours").value, 6);
    assert.equal(nodes.get("run-log").firstChild.textContent, "No PR tasks finished in the past 6 hours.");
});

test("a late run-log response cannot replace a repository switch", async () => {
    let current = { ...rendererState(), runLog: [], runLogLoadedAt: 2000000 };
    let release;
    const { nodes } = await rendererFixture(async (path) => {
        if (path.startsWith("/api/run-log")) {
            if (current.repository === "example/other") return { ok: true, json: async () => ({
                ...current, runLogLoadedAt: 2000000,
            }) };
            return new Promise((resolve) => release = resolve);
        }
        if (path === "/api/repository") current = rendererState("example/other");
        return { ok: true, json: async () => structuredClone(current) };
    });
    const log = nodes.get("load-run-log").events.click();
    assert.equal(nodes.get("run-log-count").hidden, false);
    nodes.get("repo").value = "example/other";
    const switching = nodes.get("repo").events.change();
    assert.equal(nodes.get("run-log-count").hidden, true);
    await switching;
    release({ ok: true, json: async () => ({ ...rendererState(), runLogLoadedAt: 2000000 }) });
    await log;
    assert.equal(nodes.get("repo").value, "example/other");
    assert.equal(nodes.get("prs").firstChild.firstChild.firstChild.textContent, "#12 PR in example/other");
    assert.equal(nodes.get("load-run-log").textContent, "Refresh run log");
    assert.equal(nodes.get("load-run-log").disabled, false);
    assert.equal(nodes.get("run-log-count").hidden, false);
    assert.match(nodes.get("run-log").firstChild.textContent, /No PR tasks finished/);
});

test("task tooltips separate status from effects and explain disabled controls without an action", async () => {
    const state = rendererState();
    const pr = state.prs[0];
    pr.evidence = { sha: pr.sha, conflicts: "no", ci: "failing", failing: 2, copilotThreads: 0, copilotBodies: 0 };
    const { renderer, nodes } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    const button = (kind) => taskButtons(nodes.get("prs").firstChild)
        .find((node) => node["aria-label"].startsWith(`${KIND_LABELS[kind]}:`));
    const ci = button("ci_fix");
    assert.deepEqual(tooltipFor(ci).children.map((node) => [node.className, node.textContent]), [
        ["tooltip-status", "CI failing"],
        ["tooltip-detail", "2 failing checks on the latest PR commit."],
        ["tooltip-action", TASK_EFFECTS.ci_fix],
    ]);
    const description = button("pr_description");
    assert.deepEqual(tooltipFor(description).children.map((node) => node.textContent), [TASK_EFFECTS.pr_description]);
    assert.equal(description.parentElement.tabIndex, undefined);
    for (const kind of ["pr_conflict_resolver", "copilot_review"]) {
        const disabled = button(kind);
        assert.equal(disabled.disabled, true);
        assert.equal(disabled.parentElement.tabIndex, 0);
        assert.equal(disabled.parentElement.role, "group");
        assert.equal(disabled.parentElement["aria-disabled"], "true");
        assert.equal(disabled.parentElement["aria-label"], disabled["aria-label"]);
        assert.equal(tooltipFor(disabled).children.some((node) => node.className === "tooltip-action"), false);
    }
    for (const control of nodes.get("prs").firstChild.children.find((node) => node.className === "task-grid").children) {
        const [button, tooltip] = control.children;
        assert.equal(button.title, undefined);
        assert.equal(tooltip.role, "tooltip");
        assert.equal(tooltip.hidden, true);
        assert.equal(button["aria-describedby"], tooltip.id);
        if (button.disabled) assert.equal(control["aria-describedby"], tooltip.id);
    }
    assert.equal(new Set(taskButtons(nodes.get("prs").firstChild).map((button) => tooltipFor(button).id)).size, 8);
    Object.assign(pr, { actionBlock: "Another task is running.", phase: { kind: "pr_review", stage: "complete", outcome: "no_change" } });
    renderer.render();
    assert.deepEqual(tooltipFor(button("pr_review")).children.map((node) => node.textContent), [
        "No findings", "Another task is running.",
    ]);
});

test("tooltips support hover and keyboard focus, dismissal and refresh without dispatching", async () => {
    const state = rendererState();
    state.prs[0].evidence = { sha: state.prs[0].sha, conflicts: "no", ci: "passing", copilotThreads: 0, copilotBodies: 0 };
    const requests = [];
    const { renderer, nodes, document, window } = await rendererFixture(async (path) => {
        requests.push(path);
        return { ok: true, json: async () => state };
    });

    test("Fix CI keeps its button label and explains current CI rather than an exhausted run in its tooltip", async () => {
        const state = rendererState();
        const pr = state.prs[0];
        pr.evidence = { sha: pr.sha, ci: "passing", failing: 0, pending: 0 };
        pr.phase = { kind: "ci_fix", sha: pr.sha, stage: "exhausted", reason: "elapsed_deadline" };
        const { renderer, nodes } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
        const button = () => taskButtons(nodes.get("prs").firstChild)
            .find((node) => node["aria-label"].startsWith("Fix CI:"));
        assert.equal(button().disabled, true);
        assert.equal(button()["aria-label"], "Fix CI: CI passing");
        assert.equal(button()["data-tone"], "idle");
        assert.deepEqual(button().children.map((node) => [node.className, node.textContent]), [
            ["task-name", "Fix CI"],
        ]);
        assert.deepEqual(tooltipFor(button()).children.map((node) => node.textContent), [
            "CI passing", "CI passed for the latest PR commit. Nothing to fix.",
        ]);

        Object.assign(pr.evidence, { ci: "failing", failing: 1 });
        renderer.render();
        assert.equal(button().disabled, false);
        assert.equal(button()["aria-label"], "Fix CI: CI failing");
        assert.equal(button().children.length, 1);

        Object.assign(pr.evidence, { ci: "passing", failing: 0 });
        pr.actionBlock = "A task is already active on this PR.";
        renderer.render();
        assert.equal(button().disabled, true);
        assert.equal(button().children.length, 1);
        assert.equal(tooltipFor(button()).children[1].textContent, pr.actionBlock);
    });
    const controls = nodes.get("prs").firstChild.children.find((node) => node.className === "task-grid").children;
    const first = controls[0];
    const tooltip = first.children[1];
    first.events.pointerenter();
    assert.equal(tooltip.hidden, false);
    first.events.focusin();
    document.activeElement = first;
    first.events.pointerleave();
    assert.equal(tooltip.hidden, false);
    document.events.keydown({ key: "Escape" });
    assert.equal(tooltip.hidden, true);
    first.events.focusin();
    document.activeElement = null;
    first.events.focusout({ relatedTarget: null });
    assert.equal(tooltip.hidden, true);
    first.events.pointerenter();
    first.events.focusout({ relatedTarget: null });
    assert.equal(tooltip.hidden, false);
    first.events.pointerleave();
    assert.equal(tooltip.hidden, true);
    first.events.pointerenter();
    controls[1].events.focusin();
    assert.equal(tooltip.hidden, true);
    assert.equal(controls[1].children[1].hidden, false);
    document.events.scroll({ target: controls[1].children[1] });
    assert.equal(controls[1].children[1].hidden, false);
    document.events.scroll({ target: document });
    assert.equal(controls[1].children[1].hidden, true);
    first.events.pointerenter();
    window.events.resize();
    assert.equal(tooltip.hidden, true);
    first.events.pointerenter();
    renderer.render();
    assert.equal(tooltip.hidden, true);
    assert.ok(requests.every((path) => !["/api/launch", "/api/cancel"].includes(path)));
});

test("tooltips stay within the viewport and flip above buttons near the bottom", async () => {
    const state = rendererState();
    const { nodes, document } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    const control = nodes.get("prs").firstChild.children.find((node) => node.className === "task-grid").firstChild;
    const tooltip = control.children[1];
    control.events.pointerenter();
    assert.equal(tooltip.style.left, "20px");
    assert.equal(tooltip.style.top, "140px");
    control.getBoundingClientRect = () => ({ left: 700, top: 520, bottom: 560 });
    control.events.pointerenter();
    assert.equal(tooltip.style.left, "472px");
    assert.equal(tooltip.style.top, "420px");
    Object.assign(document.documentElement, { clientWidth: 240, clientHeight: 160 });
    tooltip.getBoundingClientRect = () => ({ width: 224, height: 144 });
    control.getBoundingClientRect = () => ({ left: 20, top: 80, bottom: 120 });
    control.events.pointerenter();
    assert.equal(tooltip.style.left, "8px");
    assert.equal(tooltip.style.top, "8px");
    const css = await readFile(new URL("styles.css", import.meta.url), "utf8");
    const rule = css.match(/^\.task-tooltip \{([^}]+)\}/m)?.[1];
    assert.match(rule, /max-width: min\(320px, calc\(100vw - 16px\)\);/);
    assert.match(rule, /max-height: calc\(100vh - 16px\);/);
    assert.match(rule, /overflow: auto;/);
    assert.match(rule, /background: var\(--background-color-default,/);
    assert.match(rule, /white-space: normal;/);
    assert.match(css.match(/^\.task-button:disabled \{([^}]+)\}/m)?.[1], /pointer-events: none;/);
});

test("ownership toggle defaults to My PRs and shows only Draft review on other authors' PRs", async () => {
    const state = rendererState();
    const own = state.prs[0];
    const phase = phaseSummary(record(fixture({}, { loop_kind: "pr_review" })));
    state.prs.push({
        ...own, target: "example/project#13", number: 13, title: "Another author's PR",
        url: "https://github.com/example/project/pull/13", author: "someone", mine: false,
        dashboardStatus: "current", route: "approver", tasks: ["pr_review"],
        phase: { ...phase, target: "example/project#13" }, canCancel: true,
        actionBlock: "A task is already active on this PR.",
    }, {
        ...own, target: "example/project#14", number: 14, title: "Bot draft",
        url: "https://github.com/example/project/pull/14", author: "dependabot[bot]", mine: false,
        draft: true, tasks: ["pr_review"],
    });
    const requests = [];
    const { renderer, nodes, html } = await rendererFixture(async (path) => {
        requests.push(path);
        return { ok: true, json: async () => state };
    });
    const cards = () => nodes.get("prs").children;
    const buttons = taskButtons;
    assert.match(html, /id="mine" type="radio" name="ownership" checked/);
    assert.match(html, /id="others" type="radio" name="ownership"/);
    assert.equal(nodes.get("mine").checked, true);
    assert.equal(nodes.get("others").checked, false);
    assert.equal(nodes.get("pr-count").textContent, "1 / 3");
    assert.equal(cards()[0].firstChild.firstChild.textContent, "#12 PR in example/project");
    assert.equal(buttons(cards()[0]).length, Object.keys(KIND_LABELS).length);

    const reads = requests.length;
    nodes.get("mine").checked = false;
    nodes.get("others").checked = true;
    nodes.get("others").events.input();
    assert.equal(nodes.get("pr-count").textContent, "2 / 3");
    assert.equal(cards().length, 2);
    const active = buttons(cards()[0]);
    assert.equal(active.length, 2);
    assert.equal(active[0]["aria-label"], "Draft review: Running");
    assert.equal(active[0]["aria-busy"], "true");
    assert.equal(active[0].disabled, true);
    assert.equal(active[1]["aria-label"], "Cancel task");
    assert.equal(active[1]["aria-busy"], "false");
    assert.equal(active[1].disabled, false);
    assert.equal(buttons(cards()[1]).length, 1);
    assert.equal(buttons(cards()[1])[0]["aria-label"], "Draft review: Run");
    assert.equal(buttons(cards()[1])[0].disabled, false);
    assert.ok(cards().every((card) => !buttons(card).some((button) =>
        button["aria-label"]?.startsWith("Review and fix:"))));

    nodes.get("reviewers").checked = true;
    nodes.get("reviewers").events.input();
    assert.equal(nodes.get("pr-count").textContent, "1 / 3");
    nodes.get("search").value = "BOT";
    nodes.get("search").events.input();
    assert.equal(nodes.get("pr-count").textContent, "0 / 3");
    nodes.get("reviewers").checked = false;
    nodes.get("reviewers").events.input();
    assert.equal(nodes.get("pr-count").textContent, "1 / 3");
    assert.equal(cards()[0].firstChild.firstChild.textContent, "#14 Bot draft");
    renderer.render();
    assert.equal(nodes.get("others").checked, true);
    assert.equal(nodes.get("pr-count").textContent, "1 / 3");

    nodes.get("search").value = "";
    nodes.get("mine").checked = true;
    nodes.get("others").checked = false;
    nodes.get("mine").events.input();
    assert.equal(nodes.get("pr-count").textContent, "1 / 3");
    assert.equal(buttons(cards()[0]).length, Object.keys(KIND_LABELS).length);
    assert.equal(requests.length, reads);
});

test("renderer escapes task errors and preserves direct dispatch, disabled controls and activity", async () => {
    const state = rendererState();
    const message = '<img src=x onerror="throw Error()">';
    Object.assign(state.prs[0], {
        title: "<untrusted title>", tasks: [],
        phase: { ...phaseSummary(record(fixture({ stage: "blocked" }))), error: message },
    });
    const launches = [];
    let releaseLaunch;
    const { renderer, nodes } = await rendererFixture(async (path, options) => {
        if (path === "/api/launch") {
            launches.push(JSON.parse(options.body));
            return new Promise((resolve) => releaseLaunch = () => {
                state.prs[0].dispatch = { operation: "launch", kind: "pr_description", status: "accepted", message: "Dispatch accepted" };
                resolve({ ok: true, json: async () => state.prs[0].dispatch });
            });
        }
        return { ok: true, json: async () => path.includes("visibility") ? {} : state };
    });
    const row = nodes.get("prs").firstChild;
    assert.ok(row.children.some((node) => node.children?.some((child) => child.textContent === "#12 <untrusted title>")));
    const all = [];
    const visit = (node) => { all.push(node); for (const child of node.children ?? []) visit(child); };
    visit(row);
    assert.ok(all.some((node) => typeof node.textContent === "string" && node.textContent.includes(message)));
    assert.equal(all.filter((node) => node.tag === "img" || node.tag === "script").length, 0);
    const buttons = () => taskButtons(nodes.get("prs").firstChild);
    assert.equal(buttons().length, Object.keys(KIND_LABELS).length);
    assert.ok(buttons().every((button) => button.disabled && button.firstChild.className === "task-name"));
    assert.deepEqual(buttons().map((button) => button.firstChild.textContent), [
        "Address Copilot feedback", "Review and fix", "Resolve conflicts", "Fix CI",
        "Update title & description", "Simplify code", "Draft review", "Align with existing code",
    ]);
    assert.ok(buttons().every((button) => button.children.length === 1 && button.firstChild.children.length === 0));
    assert.equal(row.children.filter((node) => node.tag === "select").length, 0);

    Object.assign(state.prs[0], {
        tasks: ["self_review"], actionBlock: "A task is already active",
        phase: { ...state.prs[0].phase, kind: "self_review", stage: "running" },
    });
    renderer.render();
    const active = buttons().filter((button) => button["aria-busy"] === "true");
    assert.equal(active.length, 1);
    assert.equal(active[0]["aria-label"], "Review and fix: Running");
    assert.equal(active[0]["data-tone"], "active");
    const icon = active[0].children[0];
    assert.equal(icon.firstChild.className, "spinner");
    assert.equal(icon["aria-hidden"], "true");
    assert.equal(active[0].children[1].textContent, "Review and fix");
    assert.ok(buttons().filter((button) => button["aria-busy"] === "false")
        .every((button) => button.children.length === 1 && button.firstChild.className === "task-name"));

    Object.assign(state.prs[0], { tasks: ["pr_description"], actionBlock: null, phase: null });
    renderer.render();
    assert.ok(buttons().every((button) => button.children.length === 1 && button.firstChild.className === "task-name"));
    const description = buttons().find((button) => button["aria-label"] === "Update title & description: Run");
    assert.equal(tooltipFor(description).firstChild.textContent, TASK_EFFECTS.pr_description);
    const accept = description.events.click();
    assert.deepEqual(launches, [{ target, kind: "pr_description", confirmed: true }]);
    assert.ok(buttons().every((button) => button.disabled));
    assert.ok(buttons().some((button) => button["aria-label"] === "Update title & description: Dispatching" && button["aria-busy"] === "true"));
    releaseLaunch();
    await accept;
    assert.ok(buttons().some((button) => button["aria-label"] === "Update title & description: Starting"));
});

test("every task dispatches directly on click and locks duplicate and competing submissions", async () => {
    for (const [kind, label] of Object.entries(KIND_LABELS)) {
        const state = rendererState();
        const launches = [];
        let release;
        const { nodes } = await rendererFixture(async (path, options) => {
            if (path === "/api/launch") {
                launches.push(JSON.parse(options.body));
                return new Promise((resolve) => release = () => {
                    state.prs[0].dispatch = { operation: "launch", kind, status: "accepted", message: "Dispatch accepted" };
                    resolve({ ok: true, json: async () => state.prs[0].dispatch });
                });
            }
            return { ok: true, json: async () => state };
        });
        const buttons = () => taskButtons(nodes.get("prs").firstChild);
        const original = [...buttons()];
        const presentation = taskPresentation(state.prs[0], kind, state.workflowReady);
        const button = original.find((node) => node["aria-label"] === `${label}: ${presentation.label}`);
        if (!button.disabled) assert.equal(tooltipFor(button).children.at(-1).textContent, TASK_EFFECTS[kind]);
        const pending = button.events.click();
        assert.deepEqual(launches, [{ target, kind, confirmed: true }]);
        assert.ok(buttons().every((node) => node.disabled));
        const dispatching = buttons().filter((node) => node["aria-busy"] === "true");
        assert.equal(dispatching.length, 1);
        assert.equal(dispatching[0]["aria-label"], `${label}: Dispatching`);
        assert.equal(dispatching[0].firstChild.firstChild.className, "spinner");
        for (const stale of original) await stale.events.click();
        assert.equal(launches.length, 1);
        assert.match(nodes.get("error").textContent, /Do not submit again/);
        release();
        await pending;
        assert.ok(buttons().every((node) => node.disabled));
        const starting = buttons().filter((node) => node["aria-busy"] === "true");
        assert.equal(starting.length, 1);
        assert.equal(starting[0]["aria-label"], `${label}: Starting`);
        assert.equal(starting[0].firstChild.firstChild.className, "spinner");
    }
});

test("each task retains its indicator through active or waiting work and removes it on completion", async () => {
    const state = rendererState();
    const pr = state.prs[0];
    const { renderer, nodes } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    for (const [kind, stage, status] of [
        ["copilot_review", "waiting_review", "Waiting for review"],
        ["self_review", "verify_pending", "Verifying"],
        ["pr_conflict_resolver", "publish_pending", "Preparing publication"],
        ["ci_fix", "waiting_ci", "Waiting for CI"],
        ["pr_description", "task_effect_intent", "Updating PR"],
        ["pr_simplify", "source_pending", "Preparing source"],
        ["pr_review", "task_effect_intent", "Updating PR"],
        ["pr_consistency", "running", "Running"],
    ]) {
        Object.assign(pr, {
            phase: phaseSummary(record(fixture({ stage }, { loop_kind: kind }))),
            actionBlock: "A task is already active on this PR.",
        });
        renderer.render();
        const buttons = taskButtons(nodes.get("prs").firstChild);
        const busy = buttons.filter((button) => button["aria-busy"] === "true");
        assert.equal(busy.length, 1, kind);
        assert.equal(busy[0]["aria-label"], `${KIND_LABELS[kind]}: ${status}`);
        assert.equal(busy[0].disabled, true);
        assert.equal(busy[0].firstChild.className, "task-icon");
        assert.equal(busy[0].firstChild["aria-hidden"], "true");
        assert.equal(busy[0].firstChild.firstChild.className, "spinner");
        assert.equal(tooltipFor(busy[0]).firstChild.textContent, status);
        assert.ok(buttons.filter((button) => button !== busy[0]).every((button) =>
            button["aria-busy"] === "false" && button.children.length === 1));
        Object.assign(pr, { phase: { ...pr.phase, stage: "complete" }, actionBlock: null });
        renderer.render();
        assert.ok(taskButtons(nodes.get("prs").firstChild).every((button) =>
            button["aria-busy"] === "false" && button.children.length === 1));
    }
});

test("live action hints use amber, explain effects and disable tasks without actionable evidence", async () => {
    const state = rendererState();
    state.prs[0].evidence = {
        sha: state.prs[0].sha, conflicts: "yes", ci: "failing", failing: null, pending: null,
        copilotThreads: 1, copilotBodies: 0,
    };
    const { renderer, nodes } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    const buttons = () => taskButtons(nodes.get("prs").firstChild);
    const button = (kind) => buttons().find((node) => node["aria-label"].startsWith(`${KIND_LABELS[kind]}:`));
    for (const kind of ["pr_conflict_resolver", "ci_fix", "copilot_review"]) {
        assert.equal(button(kind)["data-tone"], "needed");
        assert.equal(button(kind).disabled, false);
        assert.equal(tooltipFor(button(kind)).children.at(-1).textContent, TASK_EFFECTS[kind]);
        assert.equal(button(kind)["aria-description"], taskPresentation(state.prs[0], kind, true).detail);
    }
    assert.equal(button("pr_conflict_resolver")["aria-label"], "Resolve conflicts: Conflicts");
    assert.equal(button("ci_fix")["aria-label"], "Fix CI: CI failing");
    assert.equal(tooltipFor(button("ci_fix")).children[1].textContent, "GitHub reports failing checks on the latest PR commit.");
    assert.equal(button("copilot_review")["aria-label"], "Address Copilot feedback: Open Copilot threads");
    Object.assign(state.prs[0].evidence, { conflicts: "no", ci: "passing", copilotThreads: 0 });
    renderer.render(state);
    assert.equal(button("pr_conflict_resolver").disabled, true);
    assert.equal(button("ci_fix").disabled, true);
    assert.equal(button("copilot_review").disabled, true);
    assert.equal(button("copilot_review")["aria-label"], "Address Copilot feedback: No Copilot feedback");
    Object.assign(state.prs[0].evidence, { copilotBodies: 1 });
    renderer.render();
    assert.equal(button("copilot_review").disabled, false);
    assert.equal(button("copilot_review")["data-tone"], "needed");
    Object.assign(state.prs[0].evidence, { ci: "pending" });
    renderer.render();
    assert.equal(button("ci_fix").disabled, true);
    assert.equal(tooltipFor(button("ci_fix")).children[1].textContent, "GitHub reports pending checks for the latest PR commit.");
    Object.assign(state.prs[0].evidence, { ci: "none" });
    renderer.render();
    assert.equal(button("ci_fix").disabled, true);
    assert.match(tooltipFor(button("ci_fix")).children[1].textContent, /No CI results yet/);
    assert.equal(tooltipFor(button("pr_conflict_resolver")).children[1].textContent, "No merge conflicts to resolve.");
    state.prs[0].evidence = { sha: state.prs[0].sha, error: "Status read failed." };
    renderer.render(state);
    assert.equal(button("pr_conflict_resolver").disabled, false);
    assert.equal(button("pr_conflict_resolver")["aria-label"], "Resolve conflicts: Status unknown");
    assert.match(tooltipFor(button("pr_conflict_resolver")).children[1].textContent, /Status read failed/);
    assert.equal(button("ci_fix").disabled, true);
    assert.match(tooltipFor(button("ci_fix")).children[1].textContent, /Refresh to check CI/);
    const css = await readFile(new URL("styles.css", import.meta.url), "utf8");
    assert.match(css, /data-tone="needed"/);
    assert.match(css, /data-color-mode="dark"/);
});

test("stale-only Copilot reviews use a neutral refresh button while feedback stays amber", async () => {
    const state = rendererState();
    const pr = state.prs[0];
    pr.evidence = { sha: pr.sha, copilotThreads: 0, copilotBodies: 0, copilotReviewOutdated: true };
    pr.phase = { kind: "copilot_review", stage: "clean", sha: pr.sha };
    const launches = [];
    const { renderer, nodes } = await rendererFixture(async (path, options) => {
        if (path === "/api/launch") {
            launches.push(JSON.parse(options.body));
            return { ok: true, json: async () => ({ status: "accepted", message: "Dispatch accepted" }) };
        }
        return { ok: true, json: async () => state };
    });
    const button = () => taskButtons(nodes.get("prs").firstChild)[0];
    assert.equal(button().firstChild.textContent, "Refresh Copilot review");
    assert.equal(button()["aria-label"], "Refresh Copilot review: Copilot review outdated");
    assert.equal(button()["data-tone"], "idle");
    assert.equal(button().disabled, false);
    assert.deepEqual(tooltipFor(button()).children.map((node) => node.textContent), [
        "Copilot review outdated", "Copilot reviewed an older commit. Request a review of the latest commit.",
    ]);
    for (const feedback of [{ copilotThreads: 1, copilotBodies: 0 }, { copilotThreads: 0, copilotBodies: 1 }]) {
        Object.assign(pr.evidence, feedback);
        renderer.render();
        assert.equal(button().firstChild.textContent, "Address Copilot feedback");
        assert.equal(button()["data-tone"], "needed");
        assert.equal(button().disabled, false);
    }
    Object.assign(pr.evidence, { copilotBodies: 0, copilotReviewOutdated: false });
    renderer.render();
    assert.equal(button().firstChild.textContent, "Address Copilot feedback");
    assert.equal(button().disabled, true);
    assert.equal(button()["data-tone"], "complete");
    assert.equal(button()["aria-label"], "Address Copilot feedback: Clean");
    const css = await readFile(new URL("styles.css", import.meta.url), "utf8");
    assert.match(css.match(/^\.task-button:disabled \{([^}]+)\}/m)?.[1], /opacity: \.6;/);
    assert.doesNotMatch(css.match(/^\.task-button\[data-tone="complete"\] \{([^}]+)\}/m)?.[1] ?? "", /opacity:/);
    pr.evidence.copilotReviewOutdated = true;
    renderer.render();
    await button().events.click();
    assert.deepEqual(launches, [{ target, kind: "copilot_review", confirmed: true }]);
});

test("PR headings put Draft and Approved after the title and the other author's username last", async () => {
    const state = rendererState();
    const { renderer, nodes } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    for (const mine of [true, false]) {
        nodes.get("mine").checked = mine;
        nodes.get("others").checked = !mine;
        Object.assign(state.prs[0], {
            mine, author: mine ? "trask" : "renovate[bot]", tasks: mine ? Object.keys(KIND_LABELS) : ["pr_review"],
        });
        for (const [draft, approved, labels] of [
            [false, false, []], [false, true, ["Approved"]], [true, false, ["Draft"]], [true, true, ["Draft", "Approved"]],
        ]) {
            Object.assign(state.prs[0], { draft, approved });
            renderer.render();
            const card = nodes.get("prs").firstChild;
            const heading = card.firstChild;
            assert.equal(heading.children.length, 1 + labels.length + Number(!mine));
            assert.equal(heading.className, "row pr-heading");
            assert.equal(heading.firstChild.tag, "a");
            assert.equal(heading.firstChild.textContent, "#12 PR in example/project");
            assert.equal(heading.firstChild.href, state.prs[0].url);
            for (const [index, label] of labels.entries()) {
                const badge = heading.children[index + 1];
                assert.equal(badge.tag, "span");
                assert.equal(badge.textContent, label);
                assert.equal(badge.className, label === "Draft" ? "badge muted" : "badge approved");
                if (label === "Approved") assert.match(badge.title, /approver-team approval/);
            }
            if (!mine) {
                const author = heading.children.at(-1);
                assert.equal(author.tag, "span");
                assert.equal(author.className, "pr-author muted");
                assert.equal(author.textContent, "@renovate[bot]");
            }
            assert.equal(card.children.find((node) => node.className === "task-grid").children.length,
                mine ? 8 : 1);
        }
    }
    const css = await readFile(new URL("styles.css", import.meta.url), "utf8");
    assert.doesNotMatch(css.match(/^\.pr-heading > a \{([^}]+)\}/m)?.[1] ?? "", /flex:\s*1/);
    assert.match(css, /\.pr-heading > \.badge \{ flex: none; \}/);
    assert.match(css, /\.pr-author \{ margin-left: auto;/);
    assert.match(css, /\.badge\.approved \{ color: var\(--true-color-green,/);
});

test("PR cards retain task-button status without detailed run views", async () => {
    const state = rendererState();
    state.prs[0].evidence = { sha: state.prs[0].sha, conflicts: "no", ci: "passing", copilotThreads: 0, copilotBodies: 0 };
    Object.assign(state.prs[0], {
        waitingSince: 2000000, dashboardStatus: "current",
        ciFailing: 0, ciPending: 0, conflicts: "no", reviewers: [{ login: "laurit", approved: true }],
    });
    const { renderer, nodes } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    const row = () => nodes.get("prs").firstChild;
    const texts = () => row().children.filter((node) => node.tag === "p").map((node) => node.textContent).join("\n");
    assert.doesNotMatch(texts(), /yours|Head |Waiting since|CI:|Conflicts:|Reviewers:|No saved central task/);
    assert.equal(row().children.find((node) => node.className === "controls"), undefined);
    assert.equal(row().children.find((node) => node.tag === "details"), undefined);
    const phase = phaseSummary(record(fixture({ stage: "clean" })));
    state.prs[0].phase = phase;
    renderer.render();
    assert.doesNotMatch(texts(), /yours|Head |Waiting since|CI:|Conflicts:|Reviewers:|Address Copilot feedback: clean/);
    const buttons = taskButtons(row());
    assert.equal(buttons.length, Object.keys(KIND_LABELS).length);
    assert.ok(buttons.some((node) => node["aria-label"] === "Address Copilot feedback: Clean"));
    assert.equal(row().children.find((node) => node.tag === "details"), undefined);
    const all = [];
    const visit = (node) => { all.push(node); for (const child of node.children ?? []) visit(child); };
    visit(row());
    assert.equal(all.some((node) => node.textContent === "Central Actions"), false);
});

test("review cards retain completion tooltips and pending-review links", async () => {
    const state = rendererState();
    const s = fixture({
        stage: "complete", reason: "verified_no_change",
        task_completion: { outcome: "no_change" },
        report: { verification: "verified", dispositions: { outcome: "no_change", comments: [] } },
    }, { loop_kind: "pr_review" });
    state.prs[0].phase = phaseSummary(record(s));
    const { renderer, nodes } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    const card = () => nodes.get("prs").firstChild;
    const result = () => card().children.find((node) => node.className === "run-result");
    const status = () => taskButtons(card()).find((node) => node["aria-label"]?.startsWith("Draft review:"));
    assert.equal(status()["aria-label"], "Draft review: No findings");
    assert.equal(result(), undefined);
    assert.equal(status()["aria-description"], "No new findings. No pending review was created.");

    const comments = [{ path: "src/example.js", line: 12, side: "RIGHT", body: "<img src=x> Missing input guard" }];
    Object.assign(s, {
        task_completion: { outcome: "pending_review", review_id: 42, comments },
        report: { verification: "verified", dispositions: { outcome: "comments", comments } },
    });
    state.prs[0].phase = phaseSummary(record(s));
    renderer.render();
    assert.equal(status()["aria-label"], "Draft review: Review ready");
    assert.equal(result().children.length, 1);
    assert.equal(result().firstChild.textContent, "Open pending review");
    assert.equal(result().firstChild.href, "https://github.com/example/project/pull/12#pullrequestreview-42");
    assert.equal(status()["aria-description"],
        "1 review comment in a pending GitHub review. Only you can see it until you submit it.");

    state.prs[0].sha = sha("b");
    renderer.render();
    assert.equal(status()["aria-label"], "Draft review: Previous head");
    assert.equal(result().firstChild.textContent, "Open pending review");
    Object.assign(state.prs[0], { sha: sha("a"), phase: { ...phaseSummary(record(s)), stage: "blocked" } });
    renderer.render();
    assert.equal(status()["aria-label"], "Draft review: Blocked");
    assert.equal(result(), undefined);
});



test("Cancel dispatches the exact displayed identity directly and stays locked pending confirmation", async () => {
    const state = rendererState();
    const phase = phaseSummary(record(fixture()));
    Object.assign(state.prs[0], { phase, canCancel: true, actionBlock: "A task is already active" });
    const cancellations = [];
    let release;
    const { renderer, nodes } = await rendererFixture(async (path, options) => {
        if (path === "/api/cancel") {
            cancellations.push(JSON.parse(options.body));
            return new Promise((resolve) => release = () => {
                state.prs[0].dispatch = { operation: "cancel", kind: phase.kind, status: "accepted", message: "Cancellation accepted" };
                resolve({ ok: true, json: async () => state.prs[0].dispatch });
            });
        }
        return { ok: true, json: async () => state };
    });
    const cancelButton = () => taskButtons(nodes.get("prs").firstChild)
        .find((node) => node["aria-label"]?.startsWith("Cancel task"));
    const button = cancelButton();
    assert.equal(button.disabled, false);
    assert.match(tooltipFor(button).firstChild.textContent, /Published changes stay/);
    const pending = button.events.click();
    assert.deepEqual(cancellations, [{ target, requestId: phase.requestId, generation: phase.generation, confirmed: true }]);
    assert.equal(cancelButton().disabled, true);
    assert.equal(cancelButton()["aria-busy"], "true");
    assert.equal(cancelButton().firstChild.firstChild.className, "spinner");
    await button.events.click();
    assert.equal(cancellations.length, 1);
    release();
    await pending;
    assert.equal(cancelButton().disabled, true);
    assert.equal(cancelButton()["aria-busy"], "true");
    assert.ok(taskButtons(nodes.get("prs").firstChild)
        .some((node) => node["aria-label"] === "Address Copilot feedback: Cancelling"));
    Object.assign(state.prs[0], { phase: { ...phase, stage: "cancelled" },
        canCancel: false, actionBlock: null, dispatch: null });
    renderer.render();
    assert.equal(cancelButton(), undefined);
});

test("Cancel launch uses the exact accepted run receipt and locks duplicate clicks", async () => {
    const state = rendererState();
    const pr = state.prs[0];
    Object.assign(pr, { canCancelDispatch: true, actionBlock: "Dispatch accepted", dispatch: {
        operation: "launch", kind: "self_review", status: "accepted", runId: 20,
        runUrl: "https://github.com/trask/copilot-workflows/actions/runs/20", message: "Dispatch accepted",
    } });
    const cancellations = [];
    let release;
    const { nodes, renderer } = await rendererFixture(async (path, options) => {
        if (path === "/api/cancel") {
            cancellations.push(JSON.parse(options.body));
            return new Promise((resolve) => release = () => {
                pr.dispatch.status = "accepted";
                pr.dispatch.message = "Dispatch cancellation requested";
                pr.canCancelDispatch = false;
                resolve({ ok: true, json: async () => pr.dispatch });
            });
        }
        return { ok: true, json: async () => state };
    });
    const buttons = () => taskButtons(nodes.get("prs").firstChild);
    const cancelButton = () => buttons().find((node) => node["aria-label"]?.startsWith("Cancel launch"));
    const button = cancelButton();
    assert.equal(button.disabled, false);
    const pending = button.events.click();
    assert.deepEqual(cancellations, [{ target: pr.target, runId: 20, confirmed: true }]);
    assert.equal(cancelButton().disabled, true);
    assert.equal(cancelButton()["aria-busy"], "true");
    assert.equal(cancelButton().firstChild.firstChild.className, "spinner");
    await button.events.click();
    assert.equal(cancellations.length, 1);
    release();
    await pending;
    assert.equal(cancelButton().disabled, true);
    assert.equal(cancelButton()["aria-busy"], "true");
    assert.ok(buttons().some((node) => node["aria-label"] === `${KIND_LABELS.self_review}: Cancelling`));
    assert.match(tooltipFor(buttons().find((node) => node["aria-label"]?.endsWith(": Cancelling")))
        .children.find((node) => node.className === "tooltip-detail").textContent, /Dispatch cancellation requested/);
    pr.dispatch.status = "uncertain";
    renderer.render();
    assert.equal(cancelButton()["aria-busy"], "false");
    assert.equal(cancelButton().children.length, 1);
    Object.assign(pr, { dispatch: null, actionBlock: null });
    renderer.render();
    assert.equal(cancelButton(), undefined);
});

test("repository selection shows loading before its response and hides old cards until completion", async () => {
    const previous = rendererState();
    const next = rendererState("example/other");
    let release;
    const selections = [];
    const { renderer, nodes } = await rendererFixture(async (path, options) => {
        if (path === "/api/repository") {
            selections.push(JSON.parse(options.body));
            return new Promise((resolve) => release = resolve);
        }
        return { ok: true, json: async () => structuredClone(previous) };
    });
    nodes.get("repo").value = "example/other";
    const pending = nodes.get("repo").events.change();
    assert.equal(nodes.get("loading").hidden, false);
    assert.equal(nodes.get("loading-message").textContent, "Loading example/other...");
    assert.equal(nodes.get("prs")["aria-busy"], "true");
    assert.equal(nodes.get("prs").children.length, 1);
    assert.equal(nodes.get("prs").firstChild.textContent, "Loading open PRs for example/other...");
    assert.equal(nodes.get("run-log").firstChild.textContent, "Run log will load after PRs in example/other.");
    assert.equal(nodes.get("pr-count").textContent, "Loading...");
    assert.equal(nodes.get("refresh").textContent, "Loading...");
    for (const id of ["repo", "refresh", "auto"]) assert.equal(nodes.get(id).disabled, true);
    nodes.get("search").events.input();
    assert.equal(nodes.get("repo").value, "example/other");
    assert.equal(nodes.get("prs").firstChild.textContent, "Loading open PRs for example/other...");
    assert.equal(nodes.get("loading").hidden, false);
    await renderer.refresh();
    assert.deepEqual(selections, [{ repo: "example/other" }]);
    release({ ok: true, json: async () => next });
    await pending;
    assert.equal(nodes.get("repo").value, "example/other");
    assert.equal(nodes.get("prs").firstChild.firstChild.firstChild.textContent, "#12 PR in example/other");
    assert.equal(nodes.get("pr-count").textContent, "1 / 1");
    assert.equal(nodes.get("loading").hidden, true);
    assert.equal(nodes.get("prs")["aria-busy"], "false");
    assert.equal(nodes.get("refresh").textContent, "Refresh");
    for (const id of ["repo", "refresh", "auto"]) assert.equal(nodes.get(id).disabled, false);
});

test("loading polls local state to show PR cards with disabled tasks and discards late responses", async () => {
    let current = rendererState();
    const intervals = new Map();
    const cleared = [];
    let release;
    let releaseState;
    let delayState = false;
    let reads = 0;
    const { nodes, document } = await rendererFixture(async (path) => {
        if (path === "/api/repository") return new Promise((resolve) => release = resolve);
        if (path === "/api/state") {
            reads++;
            if (delayState) return new Promise((resolve) => releaseState = resolve);
        }
        return { ok: true, json: async () => structuredClone(current) };
    }, {
        setInterval(callback, delay) { intervals.set(delay, callback); return delay; },
        clearInterval(timer) { cleared.push(timer); },
    });
    nodes.get("repo").value = "example/other";
    const pending = nodes.get("repo").events.change();
    current = { ...rendererState("example/other"), loading: true, prLoadedAt: null, workflowReady: false };
    current.prs[0].actionBlock = "Refresh before starting work.";
    document.hidden = true;
    const before = reads;
    await intervals.get(1000)();
    assert.equal(reads, before);
    document.hidden = false;
    await intervals.get(1000)();
    assert.equal(nodes.get("prs").firstChild.firstChild.firstChild.textContent, "#12 PR in example/other");
    assert.equal(nodes.get("pr-count").textContent, "1 / 1");
    assert.equal(nodes.get("loading").hidden, false);
    assert.ok(taskButtons(nodes.get("prs").firstChild).every((button) => button.disabled));
    delayState = true;
    const late = intervals.get(1000)();
    await new Promise((resolve) => setImmediate(resolve));
    await intervals.get(1000)();
    assert.equal(reads, before + 2);
    release({ ok: true, json: async () => rendererState("example/other") });
    await pending;
    assert.deepEqual(cleared, [1000]);
    releaseState({ ok: true, json: async () => current });
    await late;
    assert.equal(nodes.get("loading").hidden, true);
    assert.equal(nodes.get("repo").disabled, false);
    assert.ok(taskButtons(nodes.get("prs").firstChild).some((button) => !button.disabled));
});

test("late heartbeat responses cannot replace a pending or completed repository switch", async () => {
    const previous = rendererState();
    let releaseSelection;
    const oldReads = [];
    let delayed = false;
    const { renderer, nodes } = await rendererFixture(async (path) => {
        if (path === "/api/repository") return new Promise((resolve) => releaseSelection = resolve);
        if (path === "/api/state" && delayed) return new Promise((resolve) => oldReads.push(resolve));
        return { ok: true, json: async () => structuredClone(previous) };
    });
    delayed = true;
    const before = renderer.heartbeat();
    const after = renderer.heartbeat();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(oldReads.length, 2);
    nodes.get("repo").value = "example/other";
    const pending = nodes.get("repo").events.change();
    oldReads[0]({ ok: true, json: async () => previous });
    await before;
    assert.equal(nodes.get("repo").value, "example/other");
    assert.equal(nodes.get("loading").hidden, false);
    assert.equal(nodes.get("prs").firstChild.textContent, "Loading open PRs for example/other...");
    releaseSelection({ ok: true, json: async () => rendererState("example/other") });
    await pending;
    oldReads[1]({ ok: true, json: async () => previous });
    await after;
    assert.equal(nodes.get("repo").value, "example/other");
    assert.equal(nodes.get("prs").firstChild.firstChild.firstChild.textContent, "#12 PR in example/other");
    assert.equal(nodes.get("loading").hidden, true);
    assert.equal(nodes.get("prs")["aria-busy"], "false");
});

test("failed repository reads clear loading, expose the error and recover controls without retrying", async () => {
    for (const result of [
        { ok: false, json: async () => ({ ...rendererState("example/other"), prs: [], prLoadedAt: null,
            prError: "GitHub unavailable", auto: false }) },
        { ok: false, json: async () => ({ error: "GitHub unavailable" }) },
        new Error("GitHub unavailable"),
    ]) {
        let release;
        let requests = 0;
        const { nodes } = await rendererFixture(async (path) => {
            if (path === "/api/repository") {
                requests++;
                const response = await new Promise((resolve) => release = resolve);
                if (response instanceof Error) throw response;
                return response;
            }
            return { ok: true, json: async () => rendererState() };
        });
        nodes.get("repo").value = "example/other";
        const pending = nodes.get("repo").events.change();
        release(result);
        await pending;
        assert.equal(nodes.get("loading").hidden, true);
        assert.equal(nodes.get("prs")["aria-busy"], "false");
        assert.equal(nodes.get("refresh").textContent, "Refresh");
        for (const id of ["repo", "refresh", "auto"]) assert.equal(nodes.get(id).disabled, false);
        assert.equal(nodes.get("error").hidden, false);
        assert.equal(nodes.get("error").textContent, "GitHub unavailable");
        assert.equal(requests, 1);
    }
});

test("a late auto-refresh response cannot restore the previous repository", async () => {
    let releaseAuto;
    let releaseSelection;
    const { nodes } = await rendererFixture(async (path) => {
        if (path.startsWith("/api/auto?")) return new Promise((resolve) => releaseAuto = resolve);
        if (path === "/api/repository") return new Promise((resolve) => releaseSelection = resolve);
        return { ok: true, json: async () => rendererState() };
    });
    nodes.get("auto").checked = false;
    const auto = nodes.get("auto").events.change();
    nodes.get("repo").value = "example/other";
    const selection = nodes.get("repo").events.change();
    releaseSelection({ ok: true, json: async () => ({ ...rendererState("example/other"), auto: false }) });
    await selection;
    releaseAuto({ ok: true, json: async () => ({ ...rendererState(), auto: false }) });
    await auto;
    assert.equal(nodes.get("repo").value, "example/other");
    assert.equal(nodes.get("prs").firstChild.firstChild.firstChild.textContent, "#12 PR in example/other");
    assert.equal(nodes.get("auto").checked, false);
    assert.equal(nodes.get("loading").hidden, true);
});

test("manual and background refreshes display busy status without replacing the current PR list", async () => {
    let current = rendererState();
    let release;
    const { renderer, nodes } = await rendererFixture(async (path) => {
        if (path === "/api/refresh") return new Promise((resolve) => release = resolve);
        return { ok: true, json: async () => structuredClone(current) };
    });
    const pending = renderer.refresh();
    assert.equal(nodes.get("loading").hidden, false);
    assert.equal(nodes.get("loading-message").textContent, "Refreshing example/project...");
    assert.equal(nodes.get("prs")["aria-busy"], "true");
    assert.equal(nodes.get("prs").firstChild.firstChild.firstChild.textContent, "#12 PR in example/project");
    assert.equal(nodes.get("repo").disabled, true);
    assert.equal(nodes.get("refresh").textContent, "Refreshing...");
    release({ ok: true, json: async () => current });
    await pending;
    assert.equal(nodes.get("loading").hidden, true);
    assert.equal(nodes.get("repo").disabled, false);
    current = { ...current, loading: true };
    await renderer.heartbeat();
    assert.equal(nodes.get("loading").hidden, false);
    assert.equal(nodes.get("prs")["aria-busy"], "true");
    assert.equal(nodes.get("prs").firstChild.firstChild.firstChild.textContent, "#12 PR in example/project");
    current = { ...current, loading: false };
    await renderer.heartbeat();
    assert.equal(nodes.get("loading").hidden, true);
    assert.equal(nodes.get("prs")["aria-busy"], "false");
});

test("renderer omits routine header metadata but retains read errors and refresh pause notices", async () => {
    const state = {
        ...rendererState(), metrics: { requests: 46, cacheHits: 30, readRetries: 1 },
        rate: { remaining: 14990, limit: 15000, reset: 2000000 },
    };
    const { renderer, nodes, html } = await rendererFixture(async () => ({ ok: true, json: async () => state }));
    assert.doesNotMatch(html, /Open PRs, dashboard routing|id="freshness"|id="cost"/);
    assert.doesNotMatch(html, /Reviewer routing comes|Run and Cancel dispatch immediately/);
    assert.equal(nodes.has("freshness"), false);
    assert.equal(nodes.has("cost"), false);
    assert.equal(nodes.get("error").hidden, true);
    assert.equal(nodes.get("pause").hidden, true);
    Object.assign(state, {
        prError: "PR read unavailable", error: "Workflow read unavailable",
        pauseReason: "Automatic refresh paused for low GitHub capacity.", workflowReady: false,
    });
    for (const loaded of [true, false]) {
        state.prLoadedAt = loaded ? 2000000 : null;
        state.loadedAt = loaded ? 2000000 : null;
        renderer.render();
        assert.equal(nodes.get("error").hidden, false);
        assert.equal(nodes.get("error").textContent,
            `Open PRs are ${loaded ? "stale" : "unavailable"}. PR read unavailable Workflow status is ${loaded ? "stale" : "unavailable"}. Workflow read unavailable`);
        assert.equal(nodes.get("pause").hidden, false);
        assert.equal(nodes.get("pause").textContent, state.pauseReason);
        assert.ok(taskButtons(nodes.get("prs").firstChild).every((button) => button.disabled));
    }
});
