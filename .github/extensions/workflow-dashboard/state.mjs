import { createHash } from "node:crypto";
import { CENTRAL, GitHubError } from "./github.mjs";

const SHA = /^[0-9a-f]{40}$/;
const PREFIX = `repos/${CENTRAL}/git/`;
const REF = "refs/heads/review-loop-state";

function check(condition, message) {
    if (!condition) throw new GitHubError(message);
}

export class Checkpoints {
    constructor(github) {
        this.github = github;
        this.blobs = new Map();
        this.snapshot = null;
    }

    async load() {
        const listing = await this.github.get(PREFIX + "matching-refs/heads/review-loop-state");
        const refs = listing.data;
        check(Array.isArray(refs) && refs.length <= 1000 &&
            !listing.link?.split(",").some((item) => /;\s*rel="next"/.test(item)),
        "State branch listing is incomplete or oversized.");
        check(refs.every((item) => typeof item?.ref === "string" && item.ref.startsWith(REF)),
            "State branch listing contains invalid references.");
        const matches = refs.filter((item) => item.ref === REF);
        check(matches.length <= 1, "State branch listing contains duplicate references.");
        if (!matches.length) {
            this.blobs.clear();
            if (this.snapshot?.sha !== null) {
                this.snapshot = { sha: null, entries: new Map(), current: [], history: null };
            }
            return this.snapshot;
        }
        const ref = matches[0];
        const sha = ref.object?.sha;
        check(ref.object?.type === "commit" && typeof sha === "string" && SHA.test(sha),
            "State branch has an invalid commit identity.");
        if (this.snapshot?.sha === sha) return this.snapshot;
        const commit = (await this.github.get(PREFIX + "commits/" + sha)).data;
        check(commit.sha === sha && SHA.test(commit.tree?.sha), "State commit does not match its pinned identity.");
        const tree = (await this.github.get(PREFIX + "trees/" + commit.tree.sha)).data;
        check(tree.sha === commit.tree.sha && tree.truncated === false &&
            Array.isArray(tree.tree), "State tree is incomplete.");
        const entries = new Map();
        for (const item of tree.tree) {
            check(item.type === "blob" && item.mode === "100644" &&
                typeof item.path === "string" && /^[A-Za-z0-9-]+\.json$/.test(item.path) &&
                SHA.test(item.sha) && Number.isInteger(item.size) && item.size >= 0,
            "State tree contains an unsafe entry.");
            check(!entries.has(item.path), "State tree contains duplicate checkpoint paths.");
            entries.set(item.path, item);
        }
        const current = [];
        for (const entry of entries.values()) {
            if (entry.path.startsWith("pr-")) current.push({ name: entry.path, state: await this.blob(entry) });
        }
        const retained = new Set([...entries.values()].map((entry) => entry.sha));
        for (const key of this.blobs.keys()) if (!retained.has(key)) this.blobs.delete(key);
        this.snapshot = { sha, entries, current, history: null };
        return this.snapshot;
    }

    async blob(entry) {
        if (this.blobs.has(entry.sha)) return this.blobs.get(entry.sha);
        const blob = (await this.github.get(PREFIX + "blobs/" + entry.sha)).data;
        check(blob.sha === entry.sha && blob.encoding === "base64" &&
            typeof blob.content === "string" && blob.size === entry.size, "State blob identity or encoding differs.");
        const content = Buffer.from(blob.content, "base64");
        check(content.length === entry.size &&
            createHash("sha1").update(`blob ${content.length}\0`).update(content).digest("hex") === entry.sha,
        "State blob does not match its Git object identity.");
        let state;
        try {
            state = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(content));
        } catch {
            throw new GitHubError(`Checkpoint ${entry.path} contains malformed JSON.`);
        }
        this.blobs.set(entry.sha, state);
        return state;
    }

    async history(snapshot) {
        if (snapshot.history) return snapshot.history;
        const records = [...snapshot.current];
        for (const entry of snapshot.entries.values()) {
            if (/^(request-|stopped-)/.test(entry.path)) {
                records.push({ name: entry.path, state: await this.blob(entry) });
            }
        }
        snapshot.history = records;
        return records;
    }
}
