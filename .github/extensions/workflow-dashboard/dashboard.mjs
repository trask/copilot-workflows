import { GitHub, CENTRAL, ACTIVE_STATUSES } from "./github.mjs";
import { Checkpoints } from "./state.mjs";
import { phaseSummary, targetHistory, actionSummary, failedActionSummary } from "./model.mjs";

export class Dashboard {
    constructor(github = new GitHub(), now = () => Date.now()) {
        this.github = github;
        this.now = now;
        this.checkpoints = new Checkpoints(github);
        this.pending = null;
        this.historyPending = new Map();
        this.timer = null;
        this.viewers = new Map();
        this.auto = true;
        this.pauseReason = null;
        this.value = {
            phases: [], actions: [], failures: [], warnings: [], loadedAt: null, error: null, loading: false,
            snapshot: null, latency: null, cost: null,
        };
    }

    state() {
        return {
            ...this.value, auto: this.auto, pauseReason: this.pauseReason,
            rate: this.github.rate, retryAt: this.github.retryAt,
            metrics: { requests: this.github.requests, counted: this.github.counted, cacheHits: this.github.cacheHits,
                readRetries: this.github.readRetries ?? 0 },
        };
    }

    heartbeat(id, visible) {
        this.viewers.set(id, { visible, at: this.now() });
        this.schedule();
    }

    removeViewer(id) {
        this.viewers.delete(id);
        this.schedule();
    }

    visible() {
        return [...this.viewers.values()].some((entry) => entry.visible && this.now() - entry.at < 45000);
    }

    schedule() {
        if (this.timer) clearTimeout(this.timer);
        this.timer = null;
        if (!this.auto || !this.visible()) return;
        const delay = Math.max(1000, (this.value.loadedAt ?? this.now()) + 60000 - this.now());
        this.timer = setTimeout(() => {
            this.timer = null;
            if (this.auto && this.visible()) void this.refresh();
        }, delay);
        this.timer.unref?.();
    }

    setAuto(enabled) {
        if (enabled && this.github.retryAt > this.now()) throw new Error("Wait for the GitHub rate-limit reset before enabling automatic refresh.");
        if (enabled && this.github.rate && this.github.rate.remaining < this.github.rate.limit * 0.1 &&
            this.github.rate.reset > this.now()) throw new Error("Automatic refresh is paused while less than 10 percent of GitHub capacity remains.");
        this.auto = enabled;
        this.pauseReason = null;
        this.schedule();
        return this.state();
    }

    refresh() {
        if (this.pending) return this.pending;
        this.pending = (async () => {
            await Promise.allSettled([...this.historyPending.values()]);
            return this.update();
        })().finally(() => {
            this.pending = null;
            this.schedule();
        });
        return this.pending;
    }

    async update() {
        const started = this.now();
        const counted = this.github.counted;
        const warm = this.value.loadedAt !== null;
        this.value.loading = true;
        try {
            const reads = await Promise.allSettled([
                this.github.failedCoordinators(),
                this.checkpoints.load(),
                ...ACTIVE_STATUSES.map((status) =>
                    this.github.pages(`repos/${CENTRAL}/actions/runs?status=${status}`)),
            ]);
            if (reads[0].status === "fulfilled") {
                this.value.failures = reads[0].value.map((run) => failedActionSummary(run, []));
            }
            const failure = reads.find((read) => read.status === "rejected");
            if (failure) throw failure.reason;
            const [failedRuns, snapshot, ...runPages] = reads.map((read) => read.value);
            const phases = [];
            const warnings = [];
            for (const record of snapshot.current) {
                try {
                    phases.push(phaseSummary(record));
                } catch (error) {
                    warnings.push(error.message);
                }
            }
            const runs = new Map();
            for (const items of runPages) {
                for (const item of items) runs.set(item.id, item);
            }
            const actions = [...runs.values()].filter((run) => run.status !== "completed")
                .map((run) => actionSummary(run, phases));
            const failures = failedRuns.map((run) => failedActionSummary(run, phases));
            phases.sort((a, b) => (b.started ?? 0) - (a.started ?? 0));
            this.value = {
                phases, actions, failures, warnings, loadedAt: this.now(), snapshot: snapshot.sha,
                error: null, loading: false, latency: this.now() - started, cost: this.github.counted - counted,
            };
            const rate = this.github.rate;
            if (rate && rate.remaining < rate.limit * 0.1) this.pauseReason = "Less than 10 percent of GitHub capacity remains.";
            else if (warm && this.value.latency > 10000) this.pauseReason = "Refresh took more than 10 seconds.";
            else if (warm && this.value.cost > 12) this.pauseReason = "Refresh used more than 12 primary-counted GitHub requests.";
            else this.pauseReason = null;
            if (this.pauseReason) this.auto = false;
        } catch (error) {
            this.value = { ...this.value, loading: false, error: error.message,
                latency: this.now() - started, cost: this.github.counted - counted };
            this.auto = false;
            this.pauseReason = "Automatic refresh paused after a failed GitHub read.";
        }
        return this.state();
    }

    history(target) {
        if (this.pending) return this.pending.then(() => this.history(target));
        if (!this.value.loadedAt) return Promise.reject(new Error("Refresh the dashboard before loading history."));
        if (!this.value.phases.some((phase) => phase.target === target)) return Promise.reject(new Error("Target is not in this dashboard snapshot."));
        const snapshot = this.checkpoints.snapshot;
        if (snapshot.sha !== this.value.snapshot) return Promise.reject(new Error("Refresh the dashboard to reconcile its state snapshot."));
        const key = snapshot.sha;
        if (!this.historyPending.has(key)) {
            const pending = this.checkpoints.history(snapshot).finally(() => this.historyPending.delete(key));
            this.historyPending.set(key, pending);
        }
        return this.historyPending.get(key).then((records) => {
            if (this.github.rate && this.github.rate.remaining < this.github.rate.limit * 0.1) {
                this.auto = false;
                this.pauseReason = "Less than 10 percent of GitHub capacity remains.";
                this.schedule();
            }
            return { ...targetHistory(records, target), snapshot: snapshot.sha };
        }).catch((error) => {
            this.auto = false;
            this.pauseReason = `History read failed: ${error.message}`;
            this.schedule();
            throw error;
        });
    }
}
