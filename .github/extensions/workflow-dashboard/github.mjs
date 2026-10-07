import { execFile } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";
import { REPOSITORIES, configuredRepository, dashboardPath } from "./repositories.mjs";
import { KIND_LABELS } from "./kinds.mjs";

export const CENTRAL = "trask/copilot-workflows";
export const MAX_RESPONSE = 16 * 1024 * 1024;
const READ_CONCURRENCY = 3;
const READ_RETRY_DELAY = 250;
const CHECK_FIELDS = `
    __typename
    ... on CheckRun { name status conclusion checkSuite { app { slug } } }
    ... on StatusContext { context state }`;
const THREAD_FIELDS = `
    isResolved isOutdated comments(first: 1) {
        nodes { author { login __typename } pullRequestReview { state } }
    }`;
const EVIDENCE_FIELDS = `
    number headRefOid
    mergeRequirements { conditions { __typename result } }
    commits(last: 1) { nodes { commit { oid statusCheckRollup {
        contexts(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { ${CHECK_FIELDS} }
        }
    } } } }
    reviewThreads(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes { ${THREAD_FIELDS} }
    }`;

function connection(value) {
    if (!Array.isArray(value?.nodes) || value.nodes.length > 100 ||
        value.pageInfo?.hasNextPage && !value.nodes.length || typeof value.pageInfo?.hasNextPage !== "boolean" ||
        value.pageInfo.hasNextPage && typeof value.pageInfo.endCursor !== "string") {
        throw new GitHubError("Live PR status contains an incomplete connection.");
    }
    return value;
}
export const ACTIVE_STATUSES = ["queued", "in_progress", "waiting", "pending", "requested"];
export const FAILED_COORDINATORS = `repos/${CENTRAL}/actions/workflows/coordinator.yml/runs?event=workflow_dispatch&status=failure&per_page=20`;

export class GitHubError extends Error {
    constructor(message, retryAt = null) {
        super(message);
        this.retryAt = retryAt;
        this.transient = false;
    }
}

function cliFailure(error, stderr) {
    if (error.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER") {
        return new GitHubError("GitHub CLI output exceeds the dashboard size limit.");
    }
    if (error.killed) return new GitHubError("GitHub CLI request exceeded its 20-second time limit.");
    if (typeof error.code !== "number") {
        return new GitHubError("GitHub CLI could not complete the request. Check that gh is installed, authenticated, and online.");
    }
    if (/gh auth login|authentication (?:failed|required)|failed to (?:get|read|retrieve).*credential/i.test(stderr)) {
        return new GitHubError("GitHub CLI could not authenticate before sending the request. Check gh auth status.");
    }
    if (/x509:|certificate.*(?:invalid|unknown|expired)|certificate signed by unknown authority/i.test(stderr)) {
        return new GitHubError("GitHub TLS certificate validation failed. Check the network proxy and trusted certificates.");
    }
    let message;
    if (/TLS handshake timeout/i.test(stderr)) message = "GitHub TLS handshake timed out before an HTTP response.";
    else if (/i\/o timeout|context deadline exceeded|Client\.Timeout exceeded/i.test(stderr)) {
        message = "GitHub network request timed out before an HTTP response.";
    } else if (/unexpected EOF|connection reset|forcibly closed|server closed idle connection/i.test(stderr)) {
        message = "Network connection closed before GitHub returned an HTTP response.";
    }
    if (message) {
        const failure = new GitHubError(message);
        failure.transient = true;
        return failure;
    }
    return new GitHubError(`GitHub CLI exited with code ${error.code} without an HTTP response. Check authentication and network connectivity.`);
}

export function runGh(args, execute = execFile) {
    const env = { ...process.env, GH_PROMPT_DISABLED: "1", GH_PAGER: "cat" };
    delete env.GH_DEBUG;
    return new Promise((resolve, reject) => {
        execute("gh", args, { windowsHide: true, timeout: 20000, maxBuffer: MAX_RESPONSE + 65536, env },
            (error, stdout, stderr) => {
                if (error && (typeof error.code !== "number" || error.killed ||
                    !/^HTTP\/[\d.]+ \d{3}\b/.test(stdout))) {
                    reject(cliFailure(error, stderr));
                } else {
                    resolve({ code: error?.code ?? 0, stdout });
                }
            });
    });
}

export function parseResponse(stdout) {
    const boundary = /\r?\n\r?\n/.exec(stdout);
    if (!boundary) throw new GitHubError("GitHub CLI returned no HTTP response headers.");
    const head = stdout.slice(0, boundary.index).split(/\r?\n/);
    const match = /^HTTP\/[\d.]+ (\d{3})\b/.exec(head.shift());
    if (!match) throw new GitHubError("GitHub CLI returned an invalid HTTP status.");
    const headers = {};
    for (const line of head) {
        const colon = line.indexOf(":");
        if (colon > 0) headers[line.slice(0, colon).toLowerCase()] = line.slice(colon + 1).trim();
    }
    const body = stdout.slice(boundary.index + boundary[0].length);
    if (Buffer.byteLength(body) > MAX_RESPONSE) throw new GitHubError("GitHub response exceeds the dashboard size limit.");
    return { status: Number(match[1]), headers, body };
}

function safePath(path) {
    if (path === FAILED_COORDINATORS) return path;
    if (path === "user" || REPOSITORIES.some((repo) => path === dashboardPath(repo))) return path;
    for (const repo of REPOSITORIES) {
        if (path === `repos/${repo}/pulls` || new RegExp(`^repos/${repo}/pulls/[1-9][0-9]{0,7}$`).test(path)) return path;
        if (path.startsWith(`repos/${repo}/pulls?`)) {
            const query = new URLSearchParams(path.split("?")[1]);
            if (query.get("state") === "open" && [...query.keys()].every((key) => ["state", "per_page", "page"].includes(key)) &&
                [...query.keys()].every((key) => query.getAll(key).length === 1) &&
                (!query.has("per_page") || query.get("per_page") === "100") &&
                (!query.has("page") || /^[1-9][0-9]{0,2}$/.test(query.get("page")))) return path;
        }
    }
    if (!new RegExp(`^repos/${CENTRAL}/(?:git/(?:(?:ref|matching-refs)/heads/review-loop-state|commits/[0-9a-f]{40}|trees/[0-9a-f]{40}|blobs/[0-9a-f]{40})|actions/runs)(?:\\?[^\\r\\n]*)?$`).test(path)) {
        throw new GitHubError("Dashboard GitHub read is outside the allowed repository endpoints.");
    }
    return path;
}

export class GitHub {
    constructor(run = runGh, now = () => Date.now(), wait = sleep) {
        this.run = run;
        this.now = now;
        this.wait = wait;
        this.cache = new Map();
        this.readQueue = [];
        this.inFlightReads = new Map();
        this.activeReads = 0;
        this.requests = 0;
        this.counted = 0;
        this.cacheHits = 0;
        this.readRetries = 0;
        this.rate = null;
        this.graphqlRate = null;
        this.retryAt = null;
    }

    get(path) {
        safePath(path);
        return this.queueRead(path, () => this.read(path));
    }

    queueRead(key, read) {
        if (this.inFlightReads.has(key)) return this.inFlightReads.get(key);
        const task = new Promise((resolve, reject) => {
            this.readQueue.push({ read, resolve, reject });
        }).finally(() => this.inFlightReads.delete(key));
        this.inFlightReads.set(key, task);
        this.drainReads();
        return task;
    }

    drainReads() {
        while (this.activeReads < READ_CONCURRENCY && this.readQueue.length) {
            const { read, resolve, reject } = this.readQueue.shift();
            this.activeReads++;
            read().then(resolve, reject).finally(() => {
                this.activeReads--;
                this.drainReads();
            });
        }
    }

    recordRate(headers, status) {
        const field = headers["x-ratelimit-resource"] === "graphql" ? "graphqlRate" : "rate";
        if (headers["x-ratelimit-limit"] && headers["x-ratelimit-remaining"] && headers["x-ratelimit-reset"]) {
            const rate = {
                limit: Number(headers["x-ratelimit-limit"]),
                remaining: Number(headers["x-ratelimit-remaining"]),
                reset: Number(headers["x-ratelimit-reset"]) * 1000,
            };
            if (!this[field] || rate.reset > this[field].reset) this[field] = rate;
            else if (rate.reset === this[field].reset) {
                this[field] = { ...rate, remaining: Math.min(this[field].remaining, rate.remaining) };
            }
        }
        const limited = status === 429 || status === 403 &&
            (headers["retry-after"] || this[field]?.remaining === 0) ||
            field === "graphqlRate" && this[field]?.remaining === 0;
        if (limited) {
            const retryAt = headers["retry-after"]
                ? this.now() + Number(headers["retry-after"]) * 1000
                : this[field]?.remaining === 0 ? this[field].reset : this.now() + 60000;
            this.retryAt = Math.max(this.retryAt ?? 0, retryAt);
        }
        return Boolean(limited);
    }

    async read(path) {
        const cached = this.cache.get(path);
        const args = ["api", "--hostname", "github.com", "--method", "GET", "--include",
            "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"];
        if (cached?.etag) args.push("-H", `If-None-Match: ${cached.etag}`);
        args.push(path);
        let result;
        for (let attempt = 0; attempt < 2; attempt++) {
            if (this.retryAt && this.now() < this.retryAt) {
                throw new GitHubError("GitHub reads are paused until the recorded rate-limit reset.", this.retryAt);
            }
            this.requests++;
            if (attempt) this.readRetries++;
            try {
                result = await this.run(args);
                break;
            } catch (error) {
                if (!(error instanceof GitHubError) || !error.transient) throw error;
                if (attempt) throw new GitHubError(`${error.message} The read failed after one retry; refresh to try again.`);
                await this.wait(READ_RETRY_DELAY);
            }
        }
        const response = parseResponse(result.stdout);
        const { status, headers } = response;
        const limited = this.recordRate(headers, status);
        if (status === 304) {
            if (!cached) throw new GitHubError("GitHub returned a cache hit without cached data.");
            this.cacheHits++;
            return cached;
        }
        this.counted++;
        if (status < 200 || status >= 300 || result.code !== 0) {
            const hint = status === 401 || status === 403 || status === 404
                ? " Check gh authentication and repository read access. A 404 can also mean missing state."
                : "";
            throw new GitHubError(`GitHub dashboard read failed with HTTP ${status}.${hint}`, limited ? this.retryAt : null);
        }
        let data;
        try {
            data = JSON.parse(response.body);
        } catch {
            throw new GitHubError("GitHub returned malformed JSON.");
        }
        const value = { data, etag: headers.etag, link: headers.link };
        if (!/\/git\/(?:blobs|commits|trees)\//.test(path)) {
            this.cache.set(path, value);
            while (this.cache.size > 100) this.cache.delete(this.cache.keys().next().value);
        }
        return value;
    }

    async pages(path) {
        let next = safePath(`${path}${path.includes("?") ? "&" : "?"}per_page=100`);
        const results = [];
        for (let page = 0; next && page < 10; page++) {
            const response = await this.get(next);
            const items = response.data.workflow_runs;
            if (!Array.isArray(items) || !Number.isSafeInteger(response.data.total_count) ||
                response.data.total_count < 0 || items.length > 100) {
                throw new GitHubError("GitHub Actions listing is incomplete or exceeds the dashboard limit.");
            }
            results.push(...items);
            if (results.length > 1000) throw new GitHubError("GitHub Actions listing exceeds the dashboard limit.");
            const link = response.link?.split(",").find((item) => /;\s*rel="next"/.test(item));
            // Active-run totals are not a snapshot; next links define the page chain.
            if (!link) next = null;
            else {
                const match = /^\s*<([^>]+)>/.exec(link);
                if (!match) throw new GitHubError("GitHub returned an invalid pagination link.");
                const url = new URL(match[1]);
                if (url.origin !== "https://api.github.com") throw new GitHubError("GitHub pagination left the trusted host.");
                next = safePath(url.pathname.slice(1) + url.search);
            }
        }
        if (next) throw new GitHubError("GitHub Actions pagination is incomplete.");
        return results;
    }

    async failedCoordinators() {
        const { data } = await this.get(FAILED_COORDINATORS);
        if (!Array.isArray(data.workflow_runs) || data.workflow_runs.length > 20 ||
            !Number.isSafeInteger(data.total_count) || data.total_count < 0) {
            throw new GitHubError("GitHub returned an invalid recent coordinator failure listing.");
        }
        return data.workflow_runs;
    }

    async pulls(repo) {
        configuredRepository(repo);
        const root = `repos/${repo}/pulls`;
        let next = `${root}?state=open&per_page=100`;
        const results = new Map();
        let size = 0;
        for (let page = 0; next && page < 100; page++) {
            const response = await this.get(next);
            if (!Array.isArray(response.data) || response.data.length > 100) {
                throw new GitHubError("GitHub open-PR listing is malformed or incomplete.");
            }
            size += Buffer.byteLength(JSON.stringify(response.data));
            if (size > MAX_RESPONSE) throw new GitHubError("GitHub open-PR listing exceeds the 16 MiB total limit.");
            for (const pr of response.data) {
                if (!Number.isSafeInteger(pr?.number) || pr.number < 1) throw new GitHubError("GitHub returned an invalid PR identity.");
                results.set(pr.number, pr);
            }
            const link = response.link?.split(",").find((item) => /;\s*rel="next"/.test(item));
            next = null;
            if (link) {
                const match = /^\s*<([^>]+)>/.exec(link);
                if (!match) throw new GitHubError("GitHub returned an invalid PR pagination link.");
                const url = new URL(match[1]);
                if (url.origin !== "https://api.github.com" || url.pathname !== `/${root}`) {
                    throw new GitHubError("PR pagination left the selected repository.");
                }
                next = safePath(url.pathname.slice(1) + url.search);
            }
        }
        if (next) throw new GitHubError("GitHub open-PR pagination exceeds the 10,000 PR limit.");
        return [...results.values()];
    }

    graphql(query, repo) {
        configuredRepository(repo);
        return this.queueRead(`graphql:${repo}:${query}`, async () => {
            if (this.retryAt > this.now()) throw new GitHubError("GitHub reads are paused until the recorded rate-limit reset.", this.retryAt);
            const [owner, name] = repo.split("/");
            this.requests++;
            this.counted++;
            const result = await this.run(["api", "--hostname", "github.com", "--method", "POST", "--include",
                "graphql", "-f", `query=${query}`, "-f", `owner=${owner}`, "-f", `name=${name}`]);
            const response = parseResponse(result.stdout);
            this.recordRate(response.headers, response.status);
            if (response.status !== 200) {
                throw new GitHubError(`Live PR status read failed with HTTP ${response.status}. Check gh authentication and repository access.`);
            }
            let payload;
            try {
                payload = JSON.parse(response.body);
            } catch {
                throw new GitHubError("GitHub returned malformed live PR status JSON.");
            }
            if (result.code !== 0 || payload?.errors?.length || !payload?.data?.repository) {
                throw new GitHubError("GitHub could not return live PR status. Check repository access and GraphQL query support.");
            }
            return payload.data.repository;
        });
    }

    async pullEvidence(repo, pulls) {
        configuredRepository(repo);
        if (pulls.some((pr) => !Number.isSafeInteger(pr.number) || pr.number < 1 || pr.number >= 100000000)) {
            throw new GitHubError("Invalid PR identity for live status.");
        }
        const results = new Map();
        const batches = [];
        for (let index = 0; index < pulls.length; index += 10) batches.push(pulls.slice(index, index + 10));
        await Promise.all(batches.map(async (batch) => {
            try {
                const data = await this.graphql(`query($owner: String!, $name: String!) {
                    repository(owner: $owner, name: $name) {
                        ${batch.map((pr) => `pr${pr.number}: pullRequest(number: ${pr.number}) { ${EVIDENCE_FIELDS} }`).join("\n")}
                    }
                }`, repo);
                await Promise.all(batch.map(async (pr) => {
                    try {
                        const detail = data[`pr${pr.number}`];
                        if (detail?.number !== pr.number || detail.headRefOid !== pr.sha) {
                            throw new GitHubError("Live PR status is missing or belongs to a different head. Refresh to try again.");
                        }
                        const commit = detail.commits?.nodes?.[0]?.commit;
                        if (detail.commits?.nodes?.length !== 1 || commit?.oid !== pr.sha ||
                            !Object.hasOwn(commit, "statusCheckRollup")) {
                            throw new GitHubError("Live CI status does not match the PR head.");
                        }
                        const threads = connection(detail.reviewThreads);
                        const checks = commit.statusCheckRollup === null ? null : connection(commit.statusCheckRollup?.contexts);
                        const pages = await Promise.allSettled([
                            this.completeConnection(repo, pr, threads, "threads"),
                            checks && this.completeConnection(repo, pr, checks, "checks"),
                        ]);
                        const failed = pages.find((page) => page.status === "rejected");
                        if (failed) throw failed.reason;
                        results.set(pr.number, { detail });
                    } catch (error) {
                        results.set(pr.number, { error: error.message });
                    }
                }));
            } catch (error) {
                for (const pr of batch) results.set(pr.number, { error: error.message });
            }
        }));
        return results;
    }

    async completeConnection(repo, pr, value, kind) {
        const cursors = new Set();
        let size = Buffer.byteLength(JSON.stringify(value.nodes));
        while (value.pageInfo.hasNextPage) {
            const cursor = value.pageInfo.endCursor;
            if (!cursor || cursors.has(cursor) || size > MAX_RESPONSE) {
                throw new GitHubError("Live PR status pagination is incomplete or oversized.");
            }
            cursors.add(cursor);
            const fields = kind === "threads"
                ? `reviewThreads(first: 100, after: ${JSON.stringify(cursor)}) {
                    pageInfo { hasNextPage endCursor }
                    nodes { ${THREAD_FIELDS} }
                }`
                : `commits(last: 1) { nodes { commit { oid statusCheckRollup {
                    contexts(first: 100, after: ${JSON.stringify(cursor)}) {
                        pageInfo { hasNextPage endCursor }
                        nodes { ${CHECK_FIELDS} }
                    }
                } } } }`;
            const data = await this.graphql(`query($owner: String!, $name: String!) {
                repository(owner: $owner, name: $name) {
                    pullRequest(number: ${pr.number}) { number headRefOid ${fields} }
                }
            }`, repo);
            const detail = data.pullRequest;
            if (detail?.number !== pr.number || detail.headRefOid !== pr.sha) {
                throw new GitHubError("PR head changed while reading live status. Refresh to try again.");
            }
            const commit = detail.commits?.nodes?.[0]?.commit;
            if (kind === "checks" && commit?.oid !== pr.sha) throw new GitHubError("Live CI pagination belongs to a different commit.");
            const page = connection(kind === "threads" ? detail.reviewThreads : commit?.statusCheckRollup?.contexts);
            size += Buffer.byteLength(JSON.stringify(page.nodes));
            if (size > MAX_RESPONSE) throw new GitHubError("Live PR status exceeds the dashboard size limit.");
            value.nodes.push(...page.nodes);
            value.pageInfo = page.pageInfo;
        }
    }

    async dispatch(inputs) {
        const keys = Object.keys(inputs);
        if (inputs.operation === "launch") {
            if (keys.length !== 4 || !keys.every((key) => ["operation", "target", "loop_kind", "publication_auth"].includes(key)) ||
                inputs.publication_auth !== "fine_grained_pat" ||
                !Object.hasOwn(KIND_LABELS, inputs.loop_kind)) {
                throw new GitHubError("Invalid coordinator launch inputs.");
            }
        } else if (inputs.operation === "cancel") {
            if (keys.length !== 4 || !keys.every((key) => ["operation", "target", "previous_request", "previous_generation"].includes(key)) ||
                !/^[0-9a-f]{32}$/.test(inputs.previous_request) || !/^[1-9][0-9]*$/.test(inputs.previous_generation)) {
                throw new GitHubError("Invalid coordinator cancellation inputs.");
            }
        } else throw new GitHubError("Only launch and cancel dispatches are supported.");
        const target = /^(.*)#([1-9][0-9]{0,7})$/.exec(inputs.target);
        if (!target || !REPOSITORIES.includes(target[1])) throw new GitHubError("Dispatch target is outside the configured repositories.");
        if (this.retryAt > this.now()) throw new GitHubError("Wait for the GitHub rate-limit reset before dispatching.", this.retryAt);
        this.requests++;
        this.counted++;
        let result;
        try {
            result = await this.run(["api", "--hostname", "github.com", "--method", "POST", "--include",
                "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28",
                `repos/${CENTRAL}/actions/workflows/coordinator.yml/dispatches`,
                "-f", "ref=main", ...Object.entries(inputs).flatMap(([key, value]) => ["-f", `inputs[${key}]=${value}`])]);
            const response = parseResponse(result.stdout);
            this.recordRate(response.headers, response.status);
            if (response.status >= 400 && response.status < 500 && result.code !== 0) {
                const rejected = new GitHubError(`Coordinator dispatch was rejected with HTTP ${response.status}. Check gh authentication and central Actions write access.`, this.retryAt);
                rejected.rejected = true;
                throw rejected;
            }
            if (response.status !== 204 || result.code !== 0) throw new Error("Unconfirmed dispatch response.");
            return { status: "accepted" };
        } catch (error) {
            if (error.rejected) throw error;
            const uncertain = new GitHubError("Dispatch outcome is uncertain. Inspect central Actions and refresh; do not blindly retry.");
            uncertain.uncertain = true;
            throw uncertain;
        }
    }
}
