import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { rmSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CENTRAL, GitHubError, MAX_RESPONSE } from "./github.mjs";

const SHA = /^[0-9a-f]{40}$/;
const REF = "refs/heads/review-loop-state";

function check(condition, message) {
    if (!condition) throw new GitHubError(message);
}

export function runGit(args, execute = execFile) {
    const env = { ...process.env, GIT_TERMINAL_PROMPT: "0", GH_PROMPT_DISABLED: "1" };
    delete env.GH_DEBUG;
    return new Promise((resolve, reject) => {
        execute("git", ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
            "-c", "core.autocrlf=false", ...args],
        { windowsHide: true, timeout: 60000, maxBuffer: MAX_RESPONSE + 65536, encoding: "buffer", env },
        (error, stdout, stderr) => {
            if (!error) {
                resolve(stdout);
                return;
            }
            const message = error.killed ? "Checkpoint Git command exceeded its 60-second time limit."
                : error.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER" ? "Checkpoint Git output exceeds the dashboard size limit."
                    : "Checkpoint Git command failed. Check Git, gh authentication, repository read access and network connectivity.";
            const failure = new GitHubError(message);
            failure.missingState = error.code === 128 &&
                /^fatal: (?:couldn't find remote ref refs\/heads\/review-loop-state|Remote branch review-loop-state not found in upstream origin)\r?$/m.test(stderr.toString("utf8"));
            reject(failure);
        });
    });
}

export class Checkpoints {
    constructor(github, run = runGit) {
        this.github = github;
        this.run = run;
        this.directory = null;
        this.snapshot = null;
        this.pending = Promise.resolve();
        this.onExit = () => {
            if (this.directory) rmSync(this.directory, { recursive: true, force: true });
        };
    }

    serialize(operation) {
        const result = this.pending.then(operation);
        this.pending = result.then(() => {}, () => {});
        return result;
    }

    async fetch() {
        if (this.directory) {
            await this.run(["-C", this.directory, "fetch", "--quiet", "--depth=1", "--no-tags", "origin", REF]);
            return "FETCH_HEAD^{commit}";
        }
        const directory = await mkdtemp(join(tmpdir(), "copilot-workflow-state-"));
        try {
            await this.run(["clone", "--quiet", "--no-checkout", "--depth=1", "--single-branch", "--no-tags",
                "--branch=review-loop-state", `https://github.com/${CENTRAL}.git`, directory]);
        } catch (error) {
            await rm(directory, { recursive: true, force: true });
            throw error;
        }
        this.directory = directory;
        process.once("exit", this.onExit);
        return "HEAD^{commit}";
    }

    load() {
        return this.serialize(async () => {
            if (this.github.retryAt > (this.github.now?.() ?? Date.now())) {
                throw new GitHubError("GitHub reads are paused until the recorded rate-limit reset.", this.github.retryAt);
            }
            let ref;
            try {
                ref = await this.fetch();
            } catch (error) {
                if (!error.missingState) throw error;
                this.snapshot = { sha: null, entries: new Map(), current: [], history: null };
                return this.snapshot;
            }
            const sha = (await this.run(["-C", this.directory, "rev-parse", "--verify", ref])).toString("utf8").trim();
            check(SHA.test(sha), "State branch has an invalid commit identity.");
            const tree = await this.run(["-C", this.directory, "ls-tree", "--long", "-z", sha]);
            const entries = new Map();
            const listing = new TextDecoder("utf-8", { fatal: true }).decode(tree);
            check(!listing || listing.endsWith("\0"), "State tree is incomplete.");
            for (const line of listing.split("\0").slice(0, -1)) {
                const item = /^100644 blob ([0-9a-f]{40}) +([0-9]+)\t([A-Za-z0-9-]+\.json)$/.exec(line);
                check(item && Number.isSafeInteger(Number(item[2])), "State tree contains an unsafe entry.");
                const [, hash, size, path] = item;
                check(!entries.has(path), "State tree contains duplicate checkpoint paths.");
                entries.set(path, { path, sha: hash, size: Number(size) });
            }
            await this.checkout(sha);
            const reads = await Promise.allSettled([...entries.values()]
                .filter((entry) => entry.path.startsWith("pr-"))
                .map(async (entry) => ({ name: entry.path, state: await this.blob(entry) })));
            const failed = reads.find((read) => read.status === "rejected");
            if (failed) throw failed.reason;
            this.snapshot = { sha, entries, current: reads.map((read) => read.value), history: null };
            return this.snapshot;
        });
    }

    checkout(sha) {
        check(SHA.test(sha), "State checkout has an invalid commit identity.");
        return this.run(["-C", this.directory, "checkout", "--quiet", "--detach", sha]);
    }

    async blob(entry) {
        const content = await readFile(join(this.directory, entry.path));
        check(content.length === entry.size &&
            createHash("sha1").update(`blob ${content.length}\0`).update(content).digest("hex") === entry.sha,
        "State blob does not match its Git object identity.");
        try {
            return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(content));
        } catch {
            throw new GitHubError(`Checkpoint ${entry.path} contains malformed JSON.`);
        }
    }

    history(snapshot) {
        return this.serialize(async () => {
            if (snapshot.history) return snapshot.history;
            const records = [...snapshot.current];
            if (snapshot.sha !== null) {
                await this.checkout(snapshot.sha);
                const reads = await Promise.allSettled([...snapshot.entries.values()]
                    .filter((entry) => /^(request-|stopped-)/.test(entry.path))
                    .map(async (entry) => ({ name: entry.path, state: await this.blob(entry) })));
                const failed = reads.find((read) => read.status === "rejected");
                if (failed) throw failed.reason;
                records.push(...reads.map((read) => read.value));
            }
            snapshot.history = records;
            return records;
        });
    }

    close() {
        return this.serialize(async () => {
            if (!this.directory) return;
            await rm(this.directory, { recursive: true, force: true });
            this.directory = null;
            this.snapshot = null;
            process.removeListener("exit", this.onExit);
        });
    }
}
