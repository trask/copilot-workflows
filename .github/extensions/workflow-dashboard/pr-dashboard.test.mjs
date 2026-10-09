import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { GitHub, GitHubError, CENTRAL, MAX_RESPONSE, FAILED_COORDINATORS } from "./github.mjs";
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
const receipt = (runId = 20) => ({
    status: "accepted", runId, runUrl: `https://github.com/${CENTRAL}/actions/runs/${runId}`,
});
const launchRun = (changes = {}) => ({
    id: 20, run_attempt: 1, event: "workflow_dispatch", path: ".github/workflows/coordinator.yml",
    head_branch: "main", repository: { full_name: CENTRAL }, actor: account,
    display_title: `Review loop launch self_review ${target}`, status: "queued", conclusion: null, ...changes,
});
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
    __typename: "CheckRun", databaseId: 1, name: "Tests", status: "COMPLETED", conclusion,
    checkSuite: { app: { id: "app", slug: "github-actions" },
        workflowRun: { runNumber: 1, event: "pull_request", workflow: { id: "workflow" } } },
    ...changes,
});
const thread = (changes = {}) => ({
    isResolved: false, isOutdated: false,
    comments: { nodes: [{ author: { id: "BOT_kgDOCnlnWA", login: "copilot-pull-request-reviewer", __typename: "Bot" },
        pullRequestReview: { state: "COMMENTED" } }] }, ...changes,
});
const cleanBody = "<!-- ccr-overview-v2 -->\n\n### \u{1f7e2} Approval recommended\n\nThe change preserves behavior.\n\n**0 open findings**\n\n\u{1f9e0} **Review effort:** Balanced";
const review = (changes = {}) => ({
    id: "PRR_example",
    author: { id: "BOT_kgDOCnlnWA", login: "copilot-pull-request-reviewer", __typename: "Bot" },
    state: "COMMENTED", submittedAt: "2026-10-08T15:15:56Z", commit: { oid: sha }, body: cleanBody, ...changes,
});
const reviewNode = (value, number = 12, head = sha) => ({
    ...value, pullRequest: { number, headRefOid: head, repository: { nameWithOwner: repo } },
});
function detail({ number = 12, head = sha, conflicts = "FAILED", checks = [], threads = [], reviews = [] } = {}) {
    return {
        number, headRefOid: head,
        mergeRequirements: { conditions: [{ __typename: "PullRequestMergeConflictStateCondition", result: conflicts }] },
        commits: { nodes: [{ commit: { oid: head, statusCheckRollup: { contexts: connection(checks) } } }] },
        reviewThreads: connection(threads),
        reviews: connection(reviews),
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
        request_id: id, frozen_at: 1000, frozen_sha: sha,
        loop_kind: "self_review", authorized_actor_id: LAUNCH_OWNER_ID, launch_run: { id: 10 },
        ...request,
    }, ...changes,
});

function controller({ records = [], pulls = [pull()], dashboardState = state(), ownNumbers = [] } = {}) {
    const calls = [];
    let viewerAccount = account;
    let listing = pulls;
    let supplement = dashboardState;
    let currentRecords = records;
    let currentRun = launchRun();
    const github = {
        requests: 0, counted: 0, cacheHits: 0, rate: null,
        get: async (path) => {
            calls.push(path); github.requests++; github.counted++;
            if (path === "user") return { data: viewerAccount };
            if (path === `repos/${CENTRAL}/actions/runs/20`) return { data: structuredClone(currentRun) };
            if (path === dashboardPath(repo) || REPOSITORIES.slice(1).some((r) => path === dashboardPath(r))) {
                if (supplement instanceof Error) throw supplement;
                return { data: file(supplement) };
            }
            const pr = listing.find((item) => path.endsWith(`/pulls/${item.number}`));
            if (!pr) throw new Error(`No live PR for ${path}`);
            return { data: structuredClone(pr) };
        },
        pulls: async (selected) => { calls.push(`list:${selected}`); github.requests++; github.counted++; return structuredClone(listing); },
        ownPullNumbers: async (selected) => {
            calls.push(`mine:${selected}`); github.requests++; github.counted++;
            return new Set(ownNumbers);
        },
        pullEvidence: async (_selected, prs) => new Map(prs.map((pr) => [pr.number, {
            detail: detail({ number: pr.number, head: pr.sha }),
        }])),
        failedCoordinators: async () => [],
        recentCoordinators: async () => [],
        dispatch: async (inputs) => {
            calls.push(inputs);
            if (inputs.operation === "launch") currentRun = launchRun({
                display_title: `Review loop launch ${inputs.loop_kind} ${inputs.target}`,
            });
            return receipt(inputs.operation === "launch" ? 20 : 21);
        },
        cancelRun: async (runId) => { calls.push({ cancelRun: runId }); },
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
        setRun: (v) => currentRun = v,
    };
}

test("repository config is small, has the requested default, and bounds reads and targets", () => {
    assert.deepEqual(REPOSITORIES, [
        "open-telemetry/opentelemetry-java-instrumentation",
        "open-telemetry/semantic-conventions-conformance",
        "open-telemetry/shared-workflows",
        "open-telemetry/semantic-conventions-genai",
        "open-telemetry/semantic-conventions",
        "open-telemetry/github-threat-detection",
        "open-telemetry/admin",
        "open-telemetry/opentelemetry-java",
    ]);
    assert.equal(DEFAULT_REPOSITORY, "open-telemetry/opentelemetry-java-instrumentation");
    for (const selected of REPOSITORIES) {
        const name = selected.split("/")[1];
        assert.deepEqual(targetParts(`${selected}#12`), { repo: selected, number: 12 });
        assert.equal(dashboardPath(selected),
            `repos/open-telemetry/shared-workflows/contents/${name}/dashboard-state.json?ref=otelbot%2Fpull-request-dashboard-state%2F${name}`);
    }
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
    assert.equal(evidence([]).ci, "none");
    assert.equal(evidence([check(null, { status: "IN_PROGRESS" })]).ci, "pending");
    assert.equal(evidence([check("FAILURE"), check(null, { name: "Build", status: "IN_PROGRESS" })]).ci, "failing");
    assert.equal(evidence([check(), check("FAILURE", { name: "Copilot code review" })]).ci, "passing");
    assert.equal(evidence([{ __typename: "StatusContext", context: "Build", state: "ERROR",
        createdAt: "2026-10-07T21:00:00Z" }]).ci, "failing");
    assert.equal(evidence([check(null)]).ci, "unknown");
    const incomplete = detail({ checks: [check()] });
    incomplete.commits.nodes[0].commit.statusCheckRollup.contexts.pageInfo.hasNextPage = true;
    assert.throws(() => normalizeEvidence(incomplete, sha), /incomplete/);
    incomplete.commits.nodes[0].commit.oid = "f".repeat(40);
    assert.throws(() => normalizeEvidence(incomplete, sha), /PR head/);
});

test("superseded CI failures and cancellations do not keep a passing task highlighted", () => {
    const newer = check("SUCCESS", { databaseId: 2 });
    for (const checks of [[check("CANCELLED"), newer], [newer, check("FAILURE")]]) {
        const evidence = normalizeEvidence(detail({ checks }), sha);
        assert.equal(evidence.ci, "passing");
        assert.equal(evidence.failing, 0);
        const pr = { ...normalizePull(pull(), repo, state(), account), tasks: ["ci_fix"], actionBlock: null, evidence };
        assert.equal(taskPresentation(pr, "ci_fix", true).disabled, true);
        assert.equal(taskPresentation(pr, "ci_fix", true).tone, "idle");
    }
    assert.throws(() => normalizeEvidence(detail({ checks: [check("SUCCESS", { databaseId: null })] }), sha),
        /ordering is incomplete/);
});

test("latest CI results retain new failures and queued reruns", () => {
    for (const current of [
        check("FAILURE", { databaseId: 2 }),
        check(null, { databaseId: 2, status: "QUEUED" }),
    ]) {
        const evidence = normalizeEvidence(detail({ checks: [current, check()] }), sha);
        assert.equal(evidence.ci, current.status === "QUEUED" ? "pending" : "failing");
        const pr = { ...normalizePull(pull(), repo, state(), account), tasks: ["ci_fix"], actionBlock: null, evidence };
        assert.equal(taskPresentation(pr, "ci_fix", true).disabled, current.status === "QUEUED");
    }
});

test("Fix CI is enabled only for known failures, including failures while other checks run", () => {
    const pr = { ...normalizePull(pull(), repo, state(), account), tasks: ["ci_fix"], actionBlock: null };
    for (const [checks, label, disabled] of [
        [[check()], "CI passing", true],
        [[check(null, { status: "IN_PROGRESS" })], "CI pending", true],
        [[], "No CI results", true],
        [[check(null)], "Status unknown", true],
        [[check("FAILURE"), check(null, { name: "Build", status: "IN_PROGRESS" })], "CI failing", false],
    ]) {
        pr.evidence = normalizeEvidence(detail({ checks }), sha);
        const presentation = taskPresentation(pr, "ci_fix", true);
        assert.equal(presentation.label, label);
        assert.equal(presentation.disabled, disabled);
    }
    for (const evidence of [
        undefined, { sha, error: "CI read failed." },
        { ...normalizeEvidence(detail({ checks: [check("FAILURE")] }), sha), sha: "f".repeat(40) },
    ]) {
        pr.evidence = evidence;
        const presentation = taskPresentation(pr, "ci_fix", true);
        assert.equal(presentation.disabled, true);
        assert.equal(presentation.label, "Status unknown");
        assert.match(presentation.detail, /Refresh to check CI/);
    }
    const absent = detail();
    absent.commits.nodes[0].commit.statusCheckRollup = null;
    assert.equal(normalizeEvidence(absent, sha).ci, "none");
    assert.equal(normalizeEvidence(detail({ checks: [check("SUCCESS", { name: "Copilot code review" })] }), sha).ci, "none");
});

test("CI check selection uses workflow sequence and preserves distinct workflows and events", () => {
    const previous = check("FAILURE", { databaseId: 3 });
    const newer = check("SUCCESS", { databaseId: 2, checkSuite: {
        app: { id: "app", slug: "github-actions" },
        workflowRun: { runNumber: 2, event: "pull_request", workflow: { id: "workflow" } },
    } });
    assert.equal(normalizeEvidence(detail({ checks: [previous, newer] }), sha).ci, "passing");
    for (const run of [
        { runNumber: 2, event: "pull_request", workflow: { id: "other-workflow" } },
        { runNumber: 2, event: "push", workflow: { id: "workflow" } },
    ]) {
        const separate = check("SUCCESS", { databaseId: 4, checkSuite: {
            app: { id: "app", slug: "github-actions" }, workflowRun: run,
        } });
        assert.equal(normalizeEvidence(detail({ checks: [previous, separate] }), sha).ci, "failing");
    }
    assert.equal(normalizeEvidence(detail({ checks: [
        { __typename: "StatusContext", context: "Build", state: "ERROR", createdAt: "2026-10-07T21:00:00Z" },
        { __typename: "StatusContext", context: "Build", state: "SUCCESS", createdAt: "2026-10-07T22:00:00Z" },
    ] }), sha).ci, "passing");
});

test("Fix CI shows current evidence instead of a terminal repair outcome without rewriting history", () => {
    const phase = { kind: "ci_fix", sha, stage: "exhausted", reason: "elapsed_deadline" };
    const pr = { ...normalizePull(pull(), repo, state(), account), tasks: ["ci_fix"], actionBlock: null, phase };
    for (const [checks, label, disabled] of [
        [[check()], "CI passing", true],
        [[check(null, { status: "IN_PROGRESS" })], "CI pending", true],
        [[check("FAILURE")], "CI failing", false],
    ]) {
        pr.evidence = normalizeEvidence(detail({ checks }), sha);
        const presentation = taskPresentation(pr, "ci_fix", true);
        assert.equal(presentation.label, label);
        assert.equal(presentation.disabled, disabled);
        assert.doesNotMatch(presentation.detail, /elapsed deadline/);
    }
    pr.evidence = { sha, error: "CI read failed." };
    const unknown = taskPresentation(pr, "ci_fix", true);
    assert.equal(unknown.label, "Status unknown");
    assert.match(unknown.detail, /CI read failed/);
    assert.deepEqual(phase, { kind: "ci_fix", sha, stage: "exhausted", reason: "elapsed_deadline" });

    pr.phase = { ...phase, stage: "waiting_ci" };
    pr.evidence = normalizeEvidence(detail({ checks: [check()] }), sha);
    pr.actionBlock = "A task is already active on this PR.";
    const active = taskPresentation(pr, "ci_fix", true);
    assert.equal(active.label, "Waiting for CI");
    assert.equal(active.disabled, true);
});

test("Copilot hints count all unresolved submitted roots by stable bot identity", () => {
    const human = thread({ comments: { nodes: [{
        author: { login: "reviewer", __typename: "User" }, pullRequestReview: { state: "COMMENTED" },
    }] } });
    const pending = thread({ comments: { nodes: [{
        author: { id: "BOT_kgDOCnlnWA", login: "Copilot", __typename: "Bot" }, pullRequestReview: { state: "PENDING" },
    }] } });
    const displayAlias = thread({ comments: { nodes: [{
        author: { id: "BOT_kgDOCnlnWA", login: "Copilot", __typename: "Bot" }, pullRequestReview: { state: "COMMENTED" },
    }] } });
    const unrelatedBot = thread({ comments: { nodes: [{
        author: { id: "BOT_kgDOC9w8XQ", login: "Copilot", __typename: "Bot" }, pullRequestReview: { state: "COMMENTED" },
    }] } });
    assert.equal(normalizeEvidence(detail({
        threads: [thread(), thread({ isResolved: true }), thread({ isOutdated: true }), human, pending, displayAlias, unrelatedBot],
    }), sha).copilotThreads, 3);
});

test("zero-finding Copilot summaries and informational sections do not enable feedback tasks", () => {
    const changed = "\n\n<details>\n<summary><strong>What changed in this PR</strong></summary>\n\nSimplifies configuration tests.\n\n| File | Description |\r\n| ---- | ----------- |\r\n| test.js | Preserves supported behavior. |\n</details>";
    const resolved = "\n\n<details>\n<summary><strong>1 resolved since last review</strong></summary>\n\n- [Input guard](#discussion_r42)\n</details>";
    const footer = "\n\n---\n\nGive feedback about Copilot approvals in [this survey](https://survey.example.com/copilot) to enter a drawing for a $150 gift card.";
    const legacy = "<!-- ccr-overview-v2 -->\n\n## Copilot review overview\n\n### \u{1f535} Needs a closer look\n\nHuman validation is recommended.\n\n**Review effort:** Balanced  \n**Findings:** None\n";
    const bodies = [cleanBody, cleanBody.replace("\n\n\u{1f9e0}", changed + resolved + "\n\n\u{1f9e0}") + footer, legacy, ""];
    const evidence = normalizeEvidence(detail({
        reviews: bodies.map((body) => review({ body })), threads: [thread({ isResolved: true })],
    }), sha);
    assert.equal(evidence.copilotBodies, 0);
    const pr = { ...normalizePull(pull(), repo, state(), account), tasks: ["copilot_review"], evidence };
    assert.equal(taskPresentation(pr, "copilot_review", true).disabled, true);
    assert.equal(actionEvidence(pr, "copilot_review").label, "No Copilot feedback");
});

test("only submitted verified current-head review bodies count, and unknown text remains actionable", () => {
    const feedback = review({ body: "Fix the missing input guard." });
    const reviews = [
        feedback, review({ body: feedback.body, commit: { oid: "f".repeat(40) } }),
        review({ body: feedback.body, author: { __typename: "User", id: "BOT_kgDOCnlnWA" } }),
        review({ body: feedback.body, author: { __typename: "Bot", id: "unrelated-bot" } }),
        review({ body: feedback.body, state: "PENDING", submittedAt: null }),
        review({ body: feedback.body, state: "DISMISSED" }),
    ];
    const evidence = normalizeEvidence(detail({ reviews }), sha);
    assert.equal(evidence.copilotBodies, 1);
    const pr = { ...normalizePull(pull(), repo, state(), account), tasks: ["copilot_review"], evidence };
    assert.equal(taskPresentation(pr, "copilot_review", true).disabled, false);
    assert.equal(taskPresentation(pr, "copilot_review", true).tone, "needed");
    for (const reviewBody of [
        review({ body: cleanBody + "\nUnexpected finding" }),
        review({ body: cleanBody + "\n\n<details><summary>Previously missed (1)</summary>\nMissing guard\n</details>" }),
        review({ state: "CHANGES_REQUESTED" }),
    ]) assert.equal(normalizeEvidence(detail({ reviews: [reviewBody] }), sha).copilotBodies, 1);
});

test("the code-review skill footer does not turn a zero-finding review into feedback", () => {
    const changed = "\n\n<details>\n<summary><strong>What changed in this PR</strong></summary>\n\nRetains invalid span links with metadata.\n\n| File | Description |\r\n| ---- | ----------- |\r\n| SdkSpan.java | Retains qualifying runtime links. |\n</details>";
    const footer = '\n\n---\n\n\u{1f4a1} <a href="/open-telemetry/opentelemetry-java/new/main?filename=.github/skills/code-review/SKILL.md" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Add a `code-review` agent skill</a> or configure MCP servers for context-aware, tailored reviews. <a href="https://docs.github.com/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review?tool=webui#mcp-servers-and-agent-skills" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Learn more in the docs.</a>';
    const body = cleanBody.replace("\n\n\u{1f9e0}", changed + "\n\n\u{1f9e0}") + footer;
    const evidence = normalizeEvidence(detail({ reviews: [review({ body })] }), sha);
    assert.equal(evidence.copilotThreads, 0);
    assert.equal(evidence.copilotBodies, 0);
    const pr = { ...normalizePull(pull(), repo, state(), account), tasks: ["copilot_review"], evidence };
    assert.equal(actionEvidence(pr, "copilot_review").label, "No Copilot feedback");
    assert.equal(taskPresentation(pr, "copilot_review", true).disabled, true);
    assert.equal(normalizeEvidence(detail({ reviews: [review({ body })], threads: [thread()] }), sha).copilotThreads, 1);
    assert.equal(normalizeEvidence(detail({
        reviews: [review({ body: body.replace("**0 open findings**", "**1 open finding**") })],
    }), sha).copilotBodies, 1);
    assert.equal(normalizeEvidence(detail({
        reviews: [review({ body: body + "\nUnexpected finding" })],
    }), sha).copilotBodies, 1);
});

test("incomplete Copilot review-body evidence stays unknown rather than disabling the task", () => {
    const raw = detail();
    raw.reviews.pageInfo.hasNextPage = true;
    assert.throws(() => normalizeEvidence(raw, sha), /review-body status is incomplete/);
    delete raw.reviews;
    assert.throws(() => normalizeEvidence(raw, sha), /review-body status is incomplete/);
    assert.throws(() => normalizeEvidence(detail({ reviews: [review({ body: null })] }), sha), /review body is incomplete/);
    const pr = {
        ...normalizePull(pull(), repo, state(), account), tasks: ["copilot_review"],
        evidence: { sha, error: "Review-body read failed." },
    };
    assert.equal(taskPresentation(pr, "copilot_review", true).label, "Status unknown");
    assert.equal(taskPresentation(pr, "copilot_review", true).disabled, false);
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
    assert.equal(actionEvidence(pr, "pr_conflict_resolver").disabled, true);
    assert.equal(taskPresentation(pr, "ci_fix", true).disabled, true);
    assert.equal(taskPresentation(pr, "copilot_review", true).disabled, true);
    pr.phase = { kind: "copilot_review", stage: "running", sha };
    assert.equal(taskPresentation(pr, "copilot_review", true).label, "Running");
    assert.equal(taskPresentation(pr, "copilot_review", true).busy, true);
    pr.evidence.sha = "f".repeat(40);
    assert.equal(taskPresentation(pr, "ci_fix", true).disabled, true);
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

test("fresh conflict, CI and Copilot preflight rejects unnecessary tasks after an enabled snapshot", async () => {
    for (const [kind, message] of [
        ["pr_conflict_resolver", /No merge conflicts to resolve/],
        ["ci_fix", /Nothing to fix/],
        ["copilot_review", /No Copilot feedback to address/],
    ]) {
        const c = controller();
        c.github.pullEvidence = async () => new Map([[12, {
            detail: detail({ checks: [check("FAILURE")], threads: [thread()] }),
        }]]);
        const snapshot = await c.canvas.refresh();
        assert.equal(taskPresentation(snapshot.prs[0], kind, true).disabled, false);
        c.github.pullEvidence = async () => new Map([[12, { detail: detail({ conflicts: "PASSED", checks: [check()] }) }]]);
        await assert.rejects(c.canvas.launch({ target, kind, confirmed: true }), message);
        assert.equal(c.calls.filter((call) => typeof call === "object").length, 0);
        assert.equal(c.canvas.state().prs[0].dispatch, null);
    }
    const c = controller();
    c.github.pullEvidence = async () => new Map([[12, { detail: detail({ conflicts: "PENDING" }) }]]);
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "pr_conflict_resolver", confirmed: true });
    assert.equal(c.calls.find((call) => typeof call === "object").loop_kind, "pr_conflict_resolver");
});

test("new review-body feedback can enable a Copilot launch after a no-feedback snapshot", async () => {
    const c = controller();
    const snapshot = await c.canvas.refresh();
    assert.equal(taskPresentation(snapshot.prs[0], "copilot_review", true).disabled, true);
    c.github.pullEvidence = async () => new Map([[12, {
        detail: detail({ reviews: [review({ body: "Fix the missing input guard." })] }),
    }]]);
    await c.canvas.launch({ target, kind: "copilot_review", confirmed: true });
    assert.equal(c.calls.find((call) => typeof call === "object").loop_kind, "copilot_review");
});

test("CI launch rechecks and rejects running, absent and unknown results before dispatch", async () => {
    for (const [checks, message] of [
        [[check(null, { status: "IN_PROGRESS" })], /still running/],
        [[], /No CI results/],
        [[check(null)], /unknown result/],
    ]) {
        const c = controller();
        c.github.pullEvidence = async () => new Map([[12, { detail: detail({ checks: [check("FAILURE")] }) }]]);
        const snapshot = await c.canvas.refresh();
        assert.equal(taskPresentation(snapshot.prs[0], "ci_fix", true).disabled, false);
        c.github.pullEvidence = async () => new Map([[12, { detail: detail({ checks }) }]]);
        await assert.rejects(c.canvas.launch({ target, kind: "ci_fix", confirmed: true }), message);
        assert.equal(c.calls.filter((call) => typeof call === "object").length, 0);
        assert.equal(c.canvas.state().prs[0].dispatch, null);
    }
});

test("live evidence uses fresh batches of five PRs including the final partial batch", async () => {
    const batches = [];
    const github = new GitHub(async (args) => {
        const query = args.find((arg) => arg.startsWith("query="));
        const numbers = [...query.matchAll(/pr([0-9]+): pullRequest\(number: [0-9]+\)/g)]
            .map((match) => Number(match[1]));
        batches.push(numbers);
        return response({ data: { repository: Object.fromEntries(numbers.map((number) =>
            [`pr${number}`, detail({ number })])) } });
    });
    const pulls = Array.from({ length: 6 }, (_, index) => ({ number: index + 12, sha }));
    for (let refresh = 0; refresh < 2; refresh++) {
        const results = await github.pullEvidence(repo, pulls);
        assert.equal(results.size, 6);
        for (const pr of pulls) {
            assert.equal(normalizeEvidence(results.get(pr.number).detail, sha).sha, sha);
        }
    }
    assert.deepEqual(batches, [[12, 13, 14, 15, 16], [17], [12, 13, 14, 15, 16], [17]]);
    assert.equal(github.requests, 4);
});

test("live evidence batches PRs and paginates checks, threads and reviews before claiming clear", async () => {
    const queries = [];
    const first = detail({ checks: [check("CANCELLED")] });
    first.commits.nodes[0].commit.statusCheckRollup.contexts.pageInfo = connection([], true).pageInfo;
    first.reviewThreads.pageInfo = connection([], true).pageInfo;
    first.reviewThreads.nodes = [thread({ isResolved: true })];
    first.reviews = connection([review()], true);
    const github = new GitHub(async (args) => {
        const query = args.find((arg) => arg.startsWith("query="));
        queries.push(query);
        if (query.includes("nodes(ids:")) return response({ data: {
            repository: { nameWithOwner: repo },
            nodes: [reviewNode(review()), reviewNode(review({ id: "PRR_feedback", body: "Fix the missing input guard." }))],
        } });
        if (!query.includes("after:")) return response({ data: { repository: { pr12: first, pr13: detail({ number: 13 }) } } });
        const more = query.includes("reviewThreads")
            ? { number: 12, headRefOid: sha, reviewThreads: connection([thread()]) }
            : query.includes("reviews(")
                ? { number: 12, headRefOid: sha, reviews: connection([review({ id: "PRR_feedback", body: undefined })]) }
                : detail({ checks: [check("SUCCESS", { databaseId: 2 }), check("FAILURE", { name: "Build" })] });
        return response({ data: { repository: { pullRequest: more } } });
    });
    const result = await github.pullEvidence(repo, [{ number: 12, sha }, { number: 13, sha }]);
    assert.equal(queries.length, 5);
    assert.ok(queries[0].includes("pr12:") && queries[0].includes("pr13:"));
    assert.equal(normalizeEvidence(result.get(12).detail, sha).ci, "failing");
    assert.equal(normalizeEvidence(result.get(12).detail, sha).failing, 1);
    assert.equal(normalizeEvidence(result.get(12).detail, sha).copilotThreads, 1);
    assert.equal(normalizeEvidence(result.get(12).detail, sha).copilotBodies, 1);
    assert.equal(result.get(13).error, undefined);
});

test("only submitted current-head verified Copilot reviews require body downloads", async () => {
    const metadata = [
        review({ id: "older", commit: { oid: "f".repeat(40) }, body: undefined }),
        review({ id: "human", author: { __typename: "User", id: "user", login: "reviewer" }, body: undefined }),
        review({ id: "draft", state: "PENDING", body: undefined }),
        review({ id: "current", body: undefined }),
        review({ id: "changes", state: "CHANGES_REQUESTED", body: undefined }),
    ];
    const queries = [];
    const github = new GitHub(async (args) => {
        const query = args.find((arg) => arg.startsWith("query="));
        queries.push(query);
        if (!query.includes("nodes(ids:")) {
            assert.match(query, /reviews\(first: 100, author: "copilot-pull-request-reviewer\[bot\]"/);
            assert.doesNotMatch(query, /\bbody\b/);
            return response({ data: { repository: { pr12: detail({ reviews: metadata }) } } });
        }
        assert.deepEqual(JSON.parse(/nodes\(ids: (\[[^\]]+\])\)/.exec(query)[1]), ["current", "changes"]);
        return response({ data: {
            repository: { nameWithOwner: repo },
            nodes: [reviewNode(review({ id: "current" })),
                reviewNode(review({ id: "changes", state: "CHANGES_REQUESTED", body: "" }))],
        } });
    });
    const result = await github.pullEvidence(repo, [{ number: 12, sha }]);
    assert.equal(queries.length, 2);
    assert.equal(normalizeEvidence(result.get(12).detail, sha).copilotBodies, 1);
    assert.equal(result.get(12).detail.reviews.nodes[0].body, undefined);
});

test("historical review metadata does not trigger a body request or imply current feedback", async () => {
    let calls = 0;
    const github = new GitHub(async () => {
        calls++;
        return response({ data: { repository: { pr12: detail({
            reviews: [review({ commit: { oid: "f".repeat(40) }, body: undefined })],
        }) } } });
    });
    const result = await github.pullEvidence(repo, [{ number: 12, sha }]);
    assert.equal(calls, 1);
    assert.equal(normalizeEvidence(result.get(12).detail, sha).copilotBodies, 0);
});

test("body reads bind each review to its PR and head without failing unrelated PRs", async () => {
    const first = review({ id: "first", body: undefined });
    const second = review({ id: "second", body: undefined });
    const github = new GitHub(async (args) => {
        const query = args.find((arg) => arg.startsWith("query="));
        return response({ data: query.includes("nodes(ids:") ? {
            repository: { nameWithOwner: repo },
            nodes: [reviewNode({ ...first, body: cleanBody }, 12, "f".repeat(40)),
                reviewNode({ ...second, body: cleanBody }, 13)],
        } : { repository: { pr12: detail({ reviews: [first] }), pr13: detail({ number: 13, reviews: [second] }) } } });
    });
    const result = await github.pullEvidence(repo, [{ number: 12, sha }, { number: 13, sha }]);
    assert.match(result.get(12).error, /changed while reading/);
    assert.equal(normalizeEvidence(result.get(13).detail, sha).copilotBodies, 0);
});

test("failed review body reads remain unknown instead of using metadata as clean evidence", async () => {
    const github = new GitHub(async (args) => response(args.some((arg) => arg.includes("nodes(ids:"))
        ? { errors: [{ message: "Unavailable" }] }
        : { data: { repository: { pr12: detail({ reviews: [review({ body: undefined })] }) } } }));
    assert.match((await github.pullEvidence(repo, [{ number: 12, sha }])).get(12).error, /could not return/);
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
    assert.equal(c.canvas.state().prs[0].title, pull().title);
    assert.equal(c.canvas.state().viewer.id, account.id);
    assert.equal(c.canvas.state().prLoadedAt, null);
    assert.equal(c.canvas.state().workflowReady, false);
    assert.match(c.canvas.state().prs[0].actionBlock, /Refresh/);
    assert.equal(taskPresentation(c.canvas.state().prs[0], "self_review", false).disabled, true);
    assert.equal(c.canvas.refresh(), pending);
    release(new Map([[12, { detail: detail() }]]));
    assert.equal((await pending).loading, false);
});

test("workflow reads overlap the initial PR listing and cards appear before workflow status settles", async () => {
    const c = controller();
    const load = c.canvas.checkpoints.load;
    let releasePulls;
    let releaseWorkflow;
    c.github.pulls = () => new Promise((resolve) => releasePulls = resolve);
    c.canvas.checkpoints.load = async () => {
        await new Promise((resolve) => releaseWorkflow = resolve);
        return load();
    };
    const pending = c.canvas.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(typeof releasePulls, "function");
    assert.equal(typeof releaseWorkflow, "function");
    assert.deepEqual(c.canvas.state().prs, []);
    releasePulls([pull()]);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(c.canvas.state().prs.length, 1);
    assert.equal(c.canvas.state().loading, true);
    assert.equal(c.canvas.state().prLoadedAt, null);
    assert.equal(c.canvas.state().workflowReady, false);
    releaseWorkflow();
    const result = await pending;
    assert.equal(result.loading, false);
    assert.equal(result.workflowReady, true);
    assert.equal(result.prs[0].actionBlock, null);
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

test("approval labels use the dashboard's approver-team count, not other reviewers' approvals", () => {
    const approved = cached({ facts: facts({ approval_count: 1 }) });
    assert.equal(normalizePull(pull(), repo, state(approved), account).approved, true);
    const nonTeam = cached({ facts: facts({
        approval_count: 0, reviewers: [{ login: "outsider", approved_non_team: true }],
    }) });
    assert.equal(normalizePull(pull(), repo, state(nonTeam), account).approved, false);
    assert.equal(normalizePull(pull(), repo, state(cached()), account).approved, false);
    assert.equal(normalizePull(pull(), repo, state({ ...approved, failed: true }), account).approved, false);
});

test("refresh uses the latest saved routing and facts while ownership and tasks use the live author", async () => {
    const c = controller({
        pulls: [pull({ user: { login: "renovate[bot]", id: 21, type: "Bot" } })],
        dashboardState: state(cached({ facts: facts({
            head_sha: "d".repeat(40), author: "app/renovate", is_draft: true, ci_pending_count: 2,
            approval_count: 1,
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
    assert.equal(pr.approved, true);
    assert.equal(pr.mine, false);
    assert.deepEqual(pr.tasks, ["pr_review"]);
    assert.deepEqual(filterPulls(first.prs, { mine: false, reviewers: true }), [pr]);
    c.setDashboard(state(cached({ route: "author", facts: facts({ ci_pending_count: 0 }) })));
    const next = await c.canvas.refresh();
    assert.deepEqual(next.prWarnings, []);
    assert.equal(next.prs[0].routeLabel, "Waiting on authors");
    assert.equal(next.prs[0].ciPending, 0);
    assert.equal(next.prs[0].approved, false);
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

test("My PRs includes GitHub-attributed Copilot PRs with all owner task buttons", async () => {
    const c = controller({
        pulls: [
            pull(),
            pull({ number: 13, user: { login: "Copilot", id: 21, type: "Bot" } }),
            pull({ number: 14, user: { login: "Copilot", id: 21, type: "Bot" } }),
        ],
        ownNumbers: [12, 13],
    });
    let evidenceNumbers;
    const evidence = c.github.pullEvidence;
    c.github.pullEvidence = async (selected, prs) => {
        evidenceNumbers = prs.map((pr) => pr.number);
        return evidence(selected, prs);
    };
    const result = await c.canvas.refresh();
    assert.deepEqual(filterPulls(result.prs).map((pr) => pr.number), [12, 13]);
    assert.deepEqual(filterPulls(result.prs, { mine: false }).map((pr) => pr.number), [14]);
    assert.deepEqual(evidenceNumbers, [12, 13]);
    assert.deepEqual(result.prs[0].tasks, Object.keys(KIND_LABELS));
    assert.deepEqual(result.prs[1].tasks, Object.keys(KIND_LABELS));
    assert.deepEqual(result.prs[2].tasks, ["pr_review"]);
    c.github.ownPullNumbers = async () => new Set();
    await assert.rejects(c.canvas.launch({ target: `${repo}#13`, kind: "pr_description", confirmed: true }), /not eligible/);
    c.github.ownPullNumbers = async () => new Set([12, 13]);
    await c.canvas.launch({ target: `${repo}#13`, kind: "self_review", confirmed: true });
    assert.equal(c.calls.find((call) => typeof call === "object").loop_kind, "self_review");
    assert.equal(c.calls.find((call) => typeof call === "object").target, `${repo}#13`);
    await assert.rejects(c.canvas.launch({ target: `${repo}#14`, kind: "self_review", confirmed: true }), /not eligible/);
});

test("failed ownership reads keep an explicitly stale snapshot instead of hiding attributed PRs", async () => {
    const c = controller({
        pulls: [pull({ user: { login: "Copilot", id: 21, type: "Bot" } })],
        ownNumbers: [12],
    });
    await c.canvas.refresh();
    c.github.ownPullNumbers = async () => { throw new Error("PR ownership unavailable"); };
    const result = await c.canvas.refresh();
    assert.match(result.prError, /PR ownership unavailable/);
    assert.equal(filterPulls(result.prs).length, 1);
    assert.match(result.prs[0].actionBlock, /Refresh/);
    assert.equal(result.auto, false);
});

test("native ownership listing uses GitHub's authenticated author query and rejects incomplete data", async () => {
    const calls = [];
    const github = new GitHub(async (args) => {
        calls.push(args);
        return { code: 0, stdout: JSON.stringify([{ number: 12 }, { number: 13 }]) };
    });
    assert.deepEqual(await github.ownPullNumbers(repo), new Set([12, 13]));
    assert.deepEqual(calls, [["pr", "list", "--repo", `github.com/${repo}`,
        "--state", "open", "--author", "@me", "--limit", "10000", "--json", "number"]]);
    for (const stdout of ["{}", "not JSON", '[{"number":null}]']) {
        const invalid = new GitHub(async () => ({ code: 0, stdout }));
        await assert.rejects(invalid.ownPullNumbers(repo), /ownership/);
    }
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

test("viewer, PR, ownership and reviewer-dashboard reads start together and settle on a failed viewer read", async () => {
    const c = controller();
    const get = c.github.get;
    const reads = [];
    let release;
    let releaseWorkflow;
    const load = c.canvas.checkpoints.load;
    c.canvas.checkpoints.load = async () => {
        await new Promise((resolve) => releaseWorkflow = resolve);
        return load();
    };
    c.github.get = async (path) => {
        reads.push(path);
        if (path === "user") throw new Error("Viewer read unavailable");
        return get(path);
    };
    c.github.pulls = () => {
        reads.push("pulls");
        return new Promise((resolve) => release = resolve);
    };
    c.github.ownPullNumbers = async () => {
        reads.push("ownership");
        return new Set();
    };
    const first = c.canvas.refresh();
    let done = false;
    first.then(() => done = true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(reads, ["user", "pulls", dashboardPath(repo), "ownership"]);
    assert.equal(done, false);
    assert.equal(c.canvas.refresh(), first);
    release([pull()]);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(done, false);
    releaseWorkflow();
    const result = await first;
    assert.match(result.prError, /Viewer read unavailable/);
    assert.equal(result.prLoadedAt, null);
    assert.equal(result.workflowReady, true);
    assert.equal(result.auto, false);
    assert.equal(result.pauseReason, "Automatic refresh paused after a failed PR read.");
    assert.deepEqual(result.prs, []);
});

test("a complete PR refresh shares five read slots and adds one batched live-status query", async () => {
    let active = 0;
    let maximum = 0;
    const github = new GitHub(async (args) => {
        maximum = Math.max(maximum, ++active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active--;
        const path = args.at(-1);
        let data;
        if (args[0] === "pr") return { code: 0, stdout: '[{"number":12}]' };
        if (args.includes("graphql")) data = { data: { repository: { pr12: detail() } } };
        else if (path === "user") data = account;
        else if (path === `repos/${repo}/pulls?state=open&per_page=100`) data = [pull()];
        else if (path === dashboardPath(repo)) data = file(state());
        else if (path === FAILED_COORDINATORS || path.includes("/actions/workflows/coordinator.yml/runs?per_page=100")) {
            data = { total_count: 0, workflow_runs: [] };
        } else throw new Error(`Unexpected refresh read ${path}`);
        return { code: 0, stdout: `HTTP/2.0 200 OK\r\n\r\n${JSON.stringify(data)}` };
    });
    const canvas = new PrDashboard(github, () => 2000000);
    canvas.checkpoints.load = async () => {
        canvas.checkpoints.snapshot = { sha: null, entries: new Map(), current: [], history: null };
        return canvas.checkpoints.snapshot;
    };
    const result = await canvas.refresh();
    assert.equal(maximum, 5);
    assert.equal(github.requests, 7);
    assert.equal(result.cost, 7);
    assert.equal(result.error, null);
    assert.equal(result.prError, null);
    assert.equal(result.workflowReady, true);
    assert.equal(result.prs.length, 1);
    assert.equal(result.prs[0].actionBlock, null);
});

test("a healthy PR refresh keeps automatic refresh enabled after 20 counted requests", async () => {
    const c = controller();
    await c.canvas.refresh();
    const pullEvidence = c.github.pullEvidence;
    c.github.pullEvidence = async (...args) => {
        c.github.requests += 16;
        c.github.counted += 16;
        return pullEvidence(...args);
    };
    const result = await c.canvas.refresh();
    assert.equal(result.cost, 20);
    assert.equal(result.auto, true);
    assert.equal(result.pauseReason, null);
    assert.equal(result.error, null);
    assert.equal(result.prError, null);
});

test("fresh checkpoint absence enables Run and is rechecked before the first dispatch", async (t) => {
    const c = controller();
    let fetches = 0;
    c.canvas.checkpoints = new Checkpoints(c.github, async (args) => {
        if (args.includes("clone") || args.includes("fetch")) {
            fetches++;
            const error = new GitHubError("State branch is absent");
            error.missingState = true;
            throw error;
        }
        return Buffer.alloc(0);
    });
    t.after(() => c.canvas.checkpoints.close());
    const result = await c.canvas.refresh();
    assert.equal(result.error, null);
    assert.equal(result.snapshot, null);
    assert.equal(result.workflowReady, true);
    assert.equal(result.prs[0].actionBlock, null);
    assert.equal(result.prs[0].canCancel, false);
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    assert.equal(fetches, 2);
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

test("dispatch confirmation matches the receipt run, not another launch of the same kind", async () => {
    const c = controller();
    await c.canvas.refresh();
    const launched = await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    assert.equal(launched.runId, 20);
    c.setRecords([checkpoint({}, { launch_run: { id: 21 } })]);
    const unmatched = await c.canvas.refresh();
    assert.equal(unmatched.prs[0].dispatch.runId, 20);
    assert.equal(unmatched.prs[0].canCancelDispatch, true);
    c.setRecords([checkpoint({}, { launch_run: { id: 20 } })]);
    assert.equal((await c.canvas.refresh()).prs[0].dispatch, null);
});

test("accepted dispatch cancellation locks duplicate clicks and waits for confirmed cancellation", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    c.canvas.setAuto(false);
    c.canvas.heartbeat("test", true);
    let release;
    let cancelled = 0;
    c.github.cancelRun = async (runId) => {
        assert.equal(runId, 20);
        cancelled++;
        await new Promise((resolve) => release = resolve);
    };
    const input = { target, runId: 20, confirmed: true };
    const pending = c.canvas.cancel(input);
    await assert.rejects(c.canvas.cancel(input), /stale/);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(cancelled, 1);
    assert.equal(c.canvas.state().prs[0].dispatch.status, "pending");
    release();
    await pending;
    assert.equal(c.canvas.state().prs[0].dispatch.operation, "cancel_dispatch");
    assert.equal(c.canvas.state().prs[0].canCancelDispatch, false);
    assert.equal(c.canvas.auto, false);
    assert.ok(c.canvas.timer);
    c.setRun(launchRun({ status: "completed", conclusion: "cancelled" }));
    assert.equal((await c.canvas.refresh()).prs[0].dispatch, null);
    assert.equal(c.canvas.timer, null);
});

test("cancelling an unconfirmed running launch stops its exact saved task if a checkpoint appears", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    c.canvas.setAuto(false);
    c.canvas.heartbeat("test", true);
    c.setRun(launchRun({ status: "in_progress" }));
    await c.canvas.cancel({ target, runId: 20, confirmed: true });
    assert.deepEqual(c.calls.filter((call) => typeof call === "object").at(-1), { cancelRun: 20 });
    c.setRecords([checkpoint({ generation: 4 }, { launch_run: { id: 20 } })]);
    const cancelling = await c.canvas.refresh();
    assert.equal(cancelling.prs[0].dispatch.operation, "cancel");
    assert.equal(c.canvas.auto, false);
    assert.ok(c.canvas.timer);
    assert.deepEqual(c.calls.filter((call) => typeof call === "object").at(-1), {
        operation: "cancel", target, previous_request: id, previous_generation: "4",
    });
    c.setRecords([checkpoint({ stage: "cancelled", generation: 5 }, { launch_run: { id: 20 } })]);
    assert.equal((await c.canvas.refresh()).prs[0].dispatch, null);
    assert.equal(c.canvas.timer, null);
});

test("cancellation reconciliation reports run-read failures and stops manual-mode polling", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    c.canvas.setAuto(false);
    c.canvas.heartbeat("test", true);
    await c.canvas.cancel({ target, runId: 20, confirmed: true });
    const get = c.github.get;
    c.github.get = async (path) => {
        if (path === `repos/${CENTRAL}/actions/runs/20`) throw new Error("Run access denied");
        return get(path);
    };
    const snapshot = await c.canvas.refresh();
    assert.match(snapshot.prs[0].dispatch.message, /access denied/);
    assert.ok(snapshot.prWarnings.some((warning) => warning.includes("Run access denied")));
    assert.match(snapshot.pauseReason, /could not be reconciled/);
    assert.equal(c.canvas.timer, null);
    assert.equal(c.calls.filter((call) => typeof call === "object").length, 2);
});

test("accepted dispatch cancellation adopts only its own fresh checkpoint, not a newer launch", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    c.setRecords([checkpoint({ generation: 4 }, { launch_run: { id: 20 } })]);
    await c.canvas.cancel({ target, runId: 20, confirmed: true });
    assert.deepEqual(c.calls.filter((call) => typeof call === "object").at(-1), {
        operation: "cancel", target, previous_request: id, previous_generation: "4",
    });
    assert.equal(c.calls.some((call) => call.cancelRun), false);

    const other = controller();
    await other.canvas.refresh();
    await other.canvas.launch({ target, kind: "self_review", confirmed: true });
    await other.canvas.cancel({ target, runId: 20, confirmed: true });
    other.setRun(launchRun({ status: "completed", conclusion: "cancelled" }));
    other.setRecords([checkpoint({}, { launch_run: { id: 21 } })]);
    assert.equal((await other.canvas.refresh()).prs[0].dispatch, null);
    assert.equal(other.calls.filter((call) => call.operation === "cancel").length, 0);
});

test("cancelled launch reconciliation reloads checkpoints before considering cancellation finished", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    await c.canvas.cancel({ target, runId: 20, confirmed: true });
    const get = c.github.get;
    c.github.get = async (path) => {
        if (path !== `repos/${CENTRAL}/actions/runs/20`) return get(path);
        c.setRecords([checkpoint({}, { launch_run: { id: 20 } })]);
        return { data: launchRun({ status: "completed", conclusion: "cancelled" }) };
    };
    const result = await c.canvas.refresh();
    assert.equal(result.prs[0].dispatch.operation, "cancel");
    assert.equal(c.calls.filter((call) => call.operation === "cancel").length, 1);
});

test("accepted launch cancellation rejects stale run IDs and mismatched run ownership without mutations", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    await assert.rejects(c.canvas.cancel({ target, runId: 21, confirmed: true }), /stale/);
    await assert.rejects(c.canvas.cancel({ target, runId: 20, requestId: id, generation: 3, confirmed: true }), /exact/);
    for (const run of [launchRun({ actor: { id: 99 } }), launchRun({ run_attempt: 2 })]) {
        c.setRun(run);
        await assert.rejects(c.canvas.cancel({ target, runId: 20, confirmed: true }), /does not match/);
        assert.equal(c.canvas.state().prs[0].dispatch.operation, "launch");
    }
    assert.equal(c.calls.filter((call) => typeof call === "object").length, 1);
});

test("uncertain accepted-launch cancellation stays locked and is never automatically retried", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    let calls = 0;
    c.github.cancelRun = async () => {
        calls++;
        const error = new Error("Cancellation outcome is uncertain");
        error.uncertain = true;
        throw error;
    };
    const input = { target, runId: 20, confirmed: true };
    await assert.rejects(c.canvas.cancel(input), /uncertain/);
    assert.equal(c.canvas.state().prs[0].dispatch.status, "uncertain");
    await c.canvas.refresh();
    await assert.rejects(c.canvas.cancel(input), /stale/);
    await assert.rejects(c.canvas.launch({ target, kind: "self_review", confirmed: true }), /uncertain/);
    assert.equal(calls, 1);
});

test("a confirmed failed launch unlocks a fresh explicit launch and retains its failure link", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    const failed = launchRun({ status: "completed", conclusion: "failure",
        created_at: "1970-01-01T00:33:20Z" });
    c.setRun(failed);
    c.github.failedCoordinators = async () => [failed];
    const snapshot = await c.canvas.refresh();
    const pr = snapshot.prs[0];
    assert.equal(pr.dispatch, null);
    assert.equal(pr.actionBlock, null);
    assert.equal(pr.canCancelDispatch, false);
    assert.match(snapshot.failures[0].url, /\/actions\/runs\/20\/attempts\/1$/);
    assert.equal(taskPresentation(pr, "self_review", true).label, "Run");
    assert.equal(taskPresentation(pr, "self_review", true).busy, false);
    assert.equal(taskPresentation(pr, "self_review", true).disabled, false);
    assert.equal(c.calls.filter((call) => typeof call === "object").length, 1);
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    assert.equal(c.calls.filter((call) => typeof call === "object").length, 2);
});

test("failed launch reconciliation rechecks checkpoints after the run finishes before unlocking", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    const get = c.github.get;
    c.github.get = async (path) => {
        if (path !== `repos/${CENTRAL}/actions/runs/20`) return get(path);
        c.setRecords([checkpoint({}, { launch_run: { id: 21 } })]);
        return { data: launchRun({ status: "completed", conclusion: "failure" }) };
    };
    const pr = (await c.canvas.refresh()).prs[0];
    assert.equal(pr.dispatch.status, "finished");
    assert.equal(taskPresentation(pr, "self_review", true).disabled, true);
    await assert.rejects(c.canvas.launch({ target, kind: "self_review", confirmed: true }), /finished/);
    assert.equal(c.calls.filter((call) => typeof call === "object").length, 1);
});

test("a failed checkpoint confirmation keeps the launch locked until a successful read", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    c.setRun(launchRun({ status: "completed", conclusion: "failure" }));
    const load = c.canvas.checkpoints.load;
    let reads = 0;
    c.canvas.checkpoints.load = async () => {
        if (++reads === 2) throw new Error("Checkpoint read failed");
        return load();
    };
    const snapshot = await c.canvas.refresh();
    assert.equal(snapshot.prs[0].dispatch.status, "accepted");
    assert.match(snapshot.prs[0].dispatch.reconcileError, /Checkpoint read failed/);
    assert.ok(snapshot.prWarnings.some((warning) => warning.includes("Checkpoint read failed")));
    await assert.rejects(c.canvas.launch({ target, kind: "self_review", confirmed: true }), /Checkpoint read failed/);
    assert.equal((await c.canvas.refresh()).prs[0].dispatch, null);
    assert.equal(c.calls.filter((call) => typeof call === "object").length, 1);
});

test("a newer owner-authorized task supersedes a failed launch without adopting an older task", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    c.setRun(launchRun({ status: "completed", conclusion: "failure", updated_at: "1970-01-01T00:33:20Z" }));
    c.setRecords([checkpoint({ stage: "waiting_ci" }, { launch_run: { id: 21 } })]);
    assert.equal((await c.canvas.refresh()).prs[0].dispatch.runId, 20);
    c.setRecords([checkpoint({ stage: "waiting_ci" }, { launch_run: { id: 21 }, frozen_at: 2100 })]);
    const pr = (await c.canvas.refresh()).prs[0];
    assert.equal(pr.dispatch, null);
    assert.equal(taskPresentation(pr, "self_review", true).tone, "waiting");
    assert.equal(pr.canCancel, true);
    assert.equal(c.calls.filter((call) => typeof call === "object").length, 1);

    const first = controller();
    await first.canvas.refresh();
    await first.canvas.launch({ target, kind: "self_review", confirmed: true });
    first.setRun(launchRun({ status: "completed", conclusion: "failure", updated_at: "1970-01-01T00:33:20Z" }));
    first.setRecords([checkpoint({ stage: "waiting_ci" }, { launch_run: { id: 21 }, frozen_at: 2100 })]);
    assert.equal((await first.canvas.refresh()).prs[0].dispatch, null);
});

test("refresh retains previous PR and workflow evidence while exact launch status is being read", async () => {
    const c = controller();
    await c.canvas.refresh();
    await c.canvas.launch({ target, kind: "self_review", confirmed: true });
    const nextSha = "f".repeat(40);
    c.setPulls([pull({ head: { sha: nextSha, repo: { full_name: repo } } })]);
    c.setRecords([checkpoint({}, { launch_run: { id: 21 } })]);
    const get = c.github.get;
    let release;
    c.github.get = (path) => path === `repos/${CENTRAL}/actions/runs/20`
        ? new Promise((resolve) => release = resolve) : get(path);
    const pending = c.canvas.refresh();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(c.canvas.state().loading, true);
    assert.equal(c.canvas.state().prs[0].sha, sha);
    assert.equal(c.canvas.state().prs[0].phase, null);
    release({ data: launchRun() });
    const refreshed = await pending;
    assert.equal(refreshed.prs[0].sha, nextSha);
    assert.equal(refreshed.prs[0].phase.launchId, 21);
    assert.equal(refreshed.prs[0].dispatch.runId, 20);
});

test("duplicate calls coalesce no mutations and ambiguous outcomes cannot be blindly retried", async () => {
    const c = controller();
    await c.canvas.refresh();
    let release;
    let calls = 0;
    c.github.dispatch = async () => { calls++; await new Promise((resolve) => release = resolve); return receipt(); };
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

test("GitHub dispatches bind the documented exact run receipt and never retry", async () => {
    const calls = [];
    const runReceipt = { workflow_run_id: 20, run_url: `https://api.github.com/repos/${CENTRAL}/actions/runs/20`,
        html_url: receipt().runUrl };
    const github = new GitHub(async (args) => { calls.push(args); return response(runReceipt); });
    const inputs = { operation: "launch", target, loop_kind: "self_review", publication_auth: "fine_grained_pat" };
    assert.deepEqual(await github.dispatch(inputs), receipt());
    assert.ok(calls[0].includes(`repos/${CENTRAL}/actions/workflows/coordinator.yml/dispatches`));
    assert.ok(calls[0].includes("ref=main"));
    assert.ok(calls[0].includes("inputs[loop_kind]=self_review"));
    assert.ok(calls[0].includes("X-GitHub-Api-Version: 2026-03-10"));
    assert.equal(calls[0].includes("GET"), false);
    for (const value of [
        { ...inputs, target: "other/repo#12" }, { ...inputs, operation: "tick" },
        { ...inputs, loop_kind: "bad" }, { ...inputs, publication_auth: "disabled" }, { ...inputs, ref: "evil" },
    ]) await assert.rejects(github.dispatch(value));
    assert.equal(calls.length, 1);
    for (const result of [response(null, 500), { code: 1, stdout: "" }, response(null, 204),
        response({ ...runReceipt, workflow_run_id: "20" }), response({ ...runReceipt, html_url: "https://github.com/other/repo/actions/runs/20" })]) {
        let count = 0;
        const uncertain = new GitHub(async () => { count++; return result; });
        await assert.rejects(uncertain.dispatch(inputs), (error) => error.uncertain === true);
        assert.equal(count, 1);
    }
    await assert.rejects(new GitHub(async () => response(null, 403)).dispatch(inputs), /rejected.*403/);
});

test("GitHub cancels only the exact central run and never retries an ambiguous cancellation", async () => {
    const calls = [];
    const github = new GitHub(async (args) => { calls.push(args); return response(null, 202); });
    await github.cancelRun(20);
    assert.ok(calls[0].includes(`repos/${CENTRAL}/actions/runs/20/cancel`));
    assert.ok(calls[0].includes("POST"));
    await assert.rejects(github.cancelRun("20"), /exact/);
    assert.equal(calls.length, 1);
    let count = 0;
    const uncertain = new GitHub(async () => { count++; return { code: 1, stdout: "" }; });
    await assert.rejects(uncertain.cancelRun(20), (error) => error.uncertain === true);
    assert.equal(count, 1);
    await assert.rejects(new GitHub(async () => response(null, 409)).cancelRun(20), /rejected.*409/);
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

test("extension declares shared task handlers and closes checkpoint storage with the last panel", async (t) => {
    const { canvas } = controller();
    let closed = 0;
    let releaseClose;
    let closeStarted;
    const started = new Promise((resolve) => closeStarted = resolve);
    const finishing = new Promise((resolve) => releaseClose = resolve);
    canvas.checkpoints.close = async () => { closed++; closeStarted(); await finishing; };
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
    const recentCoordinators = canvas.github.recentCoordinators;
    canvas.github.recentCoordinators = async () => { throw new Error("Run log unavailable"); };
    await assert.rejects(declaration.actions[0].handler(), /Run log unavailable/);
    assert.equal(canvas.state().workflowReady, true);
    canvas.github.recentCoordinators = recentCoordinators;
    const first = { instanceId: "first", input: {} };
    const second = { instanceId: "second", input: {} };
    t.after(async () => {
        await declaration.onClose(first);
        await declaration.onClose(second);
    });
    await declaration.open(first);
    await declaration.open(second);
    await declaration.onClose(first);
    assert.equal(closed, 0);
    const closing = declaration.onClose(second);
    await started;
    assert.equal(closed, 1);
    let reopened = false;
    const opening = declaration.open(first).then(() => reopened = true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(reopened, false);
    releaseClose();
    await closing;
    await opening;
    assert.equal(canvas.state().loadedAt, null);
    assert.equal(canvas.state().prLoadedAt, null);
    assert.equal(canvas.state().prs.length, 0);
    await assert.rejects(canvas.history(target), /Refresh the dashboard/);
});
