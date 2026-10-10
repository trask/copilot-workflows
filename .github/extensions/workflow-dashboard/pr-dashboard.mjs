import { createHash } from "node:crypto";
import { Dashboard } from "./dashboard.mjs";
import { CENTRAL, GitHub } from "./github.mjs";
import { KIND_LABELS } from "./kinds.mjs";
import { phaseSummary, TERMINAL } from "./model.mjs";
import { DEFAULT_REPOSITORY, REPOSITORIES, configuredRepository, dashboardPath, targetParts, LAUNCH_OWNER_ID } from "./repositories.mjs";
import { actionBlock, actionEvidence, checkedPull, normalizeEvidence, normalizePull, taskChoices } from "./prs.mjs";

export function decodeDashboard(response) {
    if (response?.type !== "file" || response.encoding !== "base64" || typeof response.content !== "string" ||
        !Number.isSafeInteger(response.size) || response.size < 0 || response.size > 1024 * 1024 ||
        !/^[0-9a-f]{40}$/.test(response.sha)) throw new Error("Dashboard state file is unsupported or oversized.");
    const bytes = Buffer.from(response.content, "base64");
    if (bytes.length !== response.size ||
        createHash("sha1").update(`blob ${bytes.length}\0`).update(bytes).digest("hex") !== response.sha) {
        throw new Error("Dashboard state does not match its Git blob identity.");
    }
    let state;
    try {
        state = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    } catch {
        throw new Error("Dashboard state contains malformed JSON or UTF-8.");
    }
    if (state?.version !== 18 || !state.prs || typeof state.prs !== "object" || Array.isArray(state.prs) ||
        !Array.isArray(state.draft_pr_numbers) || Object.keys(state.prs).length > 10000 ||
        !Object.keys(state.prs).every((key) => /^[1-9][0-9]{0,7}$/.test(key))) {
        throw new Error("Dashboard state has an unsupported version or shape.");
    }
    return state;
}

function viewer(data) {
    if (!data || !Number.isSafeInteger(data.id) || data.id < 1 || typeof data.login !== "string" || !data.login) {
        throw new Error("GitHub returned an invalid authenticated account.");
    }
    return { id: data.id, login: data.login };
}

function checkedLaunch(run, target, entry, account) {
    if (run?.id !== entry.runId || run.run_attempt !== 1 || run.event !== "workflow_dispatch" ||
        run.path !== ".github/workflows/coordinator.yml" || run.head_branch !== "main" ||
        run.repository?.full_name !== CENTRAL || run.actor?.id !== account?.id ||
        run.display_title !== `Review loop launch ${entry.kind} ${target}` ||
        !["queued", "in_progress", "waiting", "pending", "requested", "completed"].includes(run.status)) {
        throw new Error("The recorded launch run does not match this task and account. Inspect central Actions.");
    }
    return run;
}

export class PrDashboard extends Dashboard {
    constructor(github = new GitHub(), now = () => Date.now()) {
        super(github, now);
        this.repository = DEFAULT_REPOSITORY;
        this.prs = [];
        this.viewer = null;
        this.prLoadedAt = null;
        this.prError = null;
        this.prWarnings = [];
        this.dispatches = new Map();
        this.selecting = false;
        this.refreshState = null;
        this.runLogTitles = new Map();
        this.runLogTitleWarnings = [];
    }

    state() {
        const workflow = this.refreshState ?? super.state();
        const workflowReady = Boolean(workflow.loadedAt && !workflow.error && !workflow.warnings.length);
        const ready = Boolean(this.prLoadedAt && !this.prError && workflowReady);
        const phases = workflow.phases;
        return {
            ...workflow, loading: this.value.loading || Boolean(this.refreshState),
            repository: this.repository, repositories: REPOSITORIES,
            viewer: this.viewer, prLoadedAt: this.prLoadedAt, prError: this.prError,
            prWarnings: this.prWarnings, workflowReady,
            runLogLoadedAt: this.value.runLogLoadedAt, runLogError: this.value.runLogError,
            runLogLoading: this.value.runLogLoading,
            runLogHours: this.value.runLogHours,
            runLog: this.value.runLog.filter((task) => task.target?.startsWith(`${this.repository}#`))
                .map((task) => ({
                    ...task, number: targetParts(task.target).number,
                    title: this.prs.find((pr) => pr.target === task.target)?.title ?? this.runLogTitles.get(task.target) ?? null,
                })),
            runLogWarnings: [...this.value.runLogWarnings, ...this.runLogTitleWarnings],
            prs: this.prs.map((pr) => {
                const phase = phases.find((item) => item.target === pr.target);
                const dispatch = this.dispatches.get(pr.target);
                return {
                    ...pr, phase: phase ?? null, dispatch: dispatch ?? null,
                    tasks: taskChoices(pr, this.viewer),
                    actionBlock: actionBlock(pr, this.viewer, phase, ready, dispatch),
                    canCancelDispatch: Boolean(ready && this.viewer?.id === LAUNCH_OWNER_ID &&
                        dispatch?.operation === "launch" && dispatch.status === "accepted" &&
                        Number.isSafeInteger(dispatch.runId) && dispatch.runId > 0),
                    canCancel: Boolean(ready && this.viewer?.id === LAUNCH_OWNER_ID && !dispatch && phase &&
                        !phase.historical && !phase.unknownStage && !TERMINAL.has(phase.stage) &&
                        Number.isSafeInteger(phase.generation) && phase.generation > 0),
                };
            }),
        };
    }

    async selectRepository(repo) {
        configuredRepository(repo);
        if (this.selecting) throw new Error("Repository selection is already refreshing.");
        this.selecting = true;
        try {
            if (this.pending) await this.pending;
            if (repo !== this.repository) {
                this.repository = repo;
                this.prs = [];
                this.prLoadedAt = null;
                this.prError = null;
                this.prWarnings = [];
                this.resetRunLog();
            }
            return await this.refresh();
        } finally {
            this.selecting = false;
        }
    }

    async update() {
        const started = this.now();
        const counted = this.github.counted;
        const warm = this.prLoadedAt !== null;
        this.refreshState = super.state();
        this.value.loading = true;
        const reads = Promise.allSettled([
            this.github.get("user").then((response) => viewer(response.data)),
            this.github.pulls(this.repository),
            this.github.get(dashboardPath(this.repository)).then((response) => decodeDashboard(response.data)),
            this.github.ownPullNumbers(this.repository),
        ]);
        const workflow = super.update();
        try {
            const [account, livePulls, reviewerDashboard, ownPulls] = await reads;
            if (account.status === "rejected") throw account.reason;
            if (livePulls.status === "rejected") throw livePulls.reason;
            if (ownPulls.status === "rejected") throw ownPulls.reason;
            const pulls = livePulls.value;
            const dashboard = reviewerDashboard.status === "fulfilled" ? reviewerDashboard.value : null;
            const warnings = [];
            if (reviewerDashboard.status === "rejected") {
                warnings.push(`Reviewer dashboard unavailable: ${reviewerDashboard.reason.message}`);
            }
            const prs = pulls.map((raw) => {
                const pr = normalizePull(raw, this.repository, dashboard, account.value);
                if (!pr.bot) pr.mine ||= ownPulls.value.has(pr.number);
                return pr;
            });
            const incomplete = prs.filter((pr) => !["current", "draft"].includes(pr.dashboardStatus)).length;
            if (incomplete) warnings.push(`${incomplete} PR(s) have missing, failed or invalid dashboard classifications. They remain in their ownership view unless Waiting on reviewers is selected.`);
            if (!warm) {
                this.viewer = account.value;
                this.prs = prs;
                this.prWarnings = warnings;
            }
            const [liveStatus] = await Promise.allSettled([
                this.github.pullEvidence(this.repository, prs.filter((pr) => pr.mine || pr.bot), { summaryCI: true }),
                workflow,
            ]);
            for (const pr of prs) {
                if (!pr.mine && !pr.bot) continue;
                const read = liveStatus.status === "fulfilled" ? liveStatus.value.get(pr.number) : null;
                try {
                    if (!read?.detail) throw new Error(read?.error ??
                        (liveStatus.status === "rejected" ? liveStatus.reason.message : "Live action status is missing."));
                    pr.evidence = normalizeEvidence(read.detail, pr.sha, { summaryCI: true });
                } catch (error) {
                    pr.evidence = { sha: pr.sha, error: error.message };
                    warnings.push(`${pr.target}: ${error.message}`);
                }
            }
            await this.observeDispatches(account.value, warnings);
            this.viewer = account.value;
            this.prs = prs;
            this.prWarnings = warnings;
            this.prLoadedAt = this.now();
            this.prError = null;
            if (prs.some((pr) => pr.evidence?.error)) {
                this.auto = false;
                this.pauseReason = "Live action status is unavailable. Refresh manually.";
            } else if ([this.github.rate, this.github.graphqlRate].some((rate) =>
                rate && rate.remaining < rate.limit * 0.1)) {
                this.auto = false;
                this.pauseReason = "Less than 10 percent of GitHub capacity remains.";
            } else if (!dashboard) {
                this.auto = false;
                this.pauseReason = "Reviewer dashboard is unavailable. Refresh manually.";
            }
        } catch (error) {
            await workflow;
            this.prError = error.message;
            this.value.loading = false;
            this.auto = false;
            this.pauseReason = "Automatic refresh paused after a failed PR read.";
        } finally {
            await workflow;
            this.value.latency = this.now() - started;
            this.value.cost = this.github.counted - counted;
            if (warm && this.value.latency > 10000 && !this.pauseReason) {
                this.auto = false;
                this.pauseReason = "Refresh took more than 10 seconds.";
            }
            this.value.loading = false;
            this.refreshState = null;
        }
        return this.state();
    }

    resetRunLog() {
        super.resetRunLog();
        this.runLogTitles.clear();
        this.runLogTitleWarnings = [];
    }

    async updateRunLog(version, hours) {
        await super.updateRunLog(version, hours);
        if (version === this.runLogVersion && !this.value.runLogError) await this.loadRunLogTitles(version);
    }

    async loadRunLogTitles(version) {
        const repository = this.repository;
        const targets = [...new Set(this.value.runLog.map((task) => task.target))]
            .filter((target) => target?.startsWith(`${repository}#`) &&
                !this.prs.some((pr) => pr.target === target) && !this.runLogTitles.has(target));
        const titles = new Map(this.runLogTitles);
        const warnings = [];
        await Promise.all(targets.map(async (target) => {
            try {
                const { number } = targetParts(target);
                const pr = (await this.github.get(`repos/${repository}/pulls/${number}`)).data;
                if (pr?.number !== number || typeof pr.title !== "string" ||
                    pr.base?.repo?.full_name?.toLowerCase() !== repository.toLowerCase()) {
                    throw new Error("GitHub returned a mismatched PR title.");
                }
                titles.set(target, pr.title);
            } catch (error) {
                warnings.push(`${target}: PR title unavailable. ${error.message}`);
            }
        }));
        if (version === this.runLogVersion) {
            this.runLogTitles = titles;
            this.runLogTitleWarnings = warnings;
        }
    }

    schedule() {
        const cancelling = [...this.dispatches.values()].some((entry) =>
            (entry.operation === "cancel_dispatch" || entry.operation === "cancel" && entry.cancelRunId) &&
            entry.status === "accepted" && !entry.reconcileError);
        if (this.auto || !cancelling) return super.schedule();
        if (this.timer) clearTimeout(this.timer);
        this.timer = null;
        if (!this.visible() || this.value.error || this.prError || this.github.retryAt > this.now() ||
            [this.github.rate, this.github.graphqlRate].some((rate) => rate && rate.remaining < rate.limit * 0.1)) return;
        const delay = Math.max(1000, (this.value.loadedAt ?? this.now()) + 60000 - this.now());
        this.timer = setTimeout(() => {
            this.timer = null;
            if (this.visible()) void this.refresh();
        }, delay);
        this.timer.unref?.();
    }

    async observeDispatches(account = this.viewer, warnings = this.prWarnings) {
        if (this.value.error || this.value.snapshot !== this.checkpoints.snapshot?.sha) return;
        await Promise.all([...this.dispatches].map(async ([target, entry]) => {
            if (entry.status === "pending") return;
            const phase = this.value.phases.find((item) => item.target === target);
            const matched = phase && phase.kind === entry.kind && phase.authorizedActorId === account?.id &&
                (entry.runId ? phase.launchId === entry.runId : phase.launchId && phase.launchId !== entry.previousLaunch);
            if ((entry.operation === "launch" && matched) ||
                (entry.operation === "cancel" && phase?.requestId === entry.requestId &&
                phase.generation > entry.generation && phase.stage === "cancelled")) {
                this.dispatches.delete(target);
                return;
            }
            const superseded = (finishedAt) => entry.operation === "launch" &&
                phase && !phase.historical && !phase.unknownStage &&
                phase.authorizedActorId === account?.id && phase.launchId && phase.launchId !== entry.runId &&
                Number.isFinite(phase.started) && phase.started > Date.parse(finishedAt);
            if (entry.status === "finished" && entry.conclusion === "failure" && superseded(entry.finishedAt)) {
                this.dispatches.delete(target);
                return;
            }
            if (!entry.runId || entry.status !== "accepted" ||
                !["launch", "cancel_dispatch"].includes(entry.operation)) return;
            try {
                if (entry.operation === "cancel_dispatch" && matched) {
                    if (TERMINAL.has(phase.stage)) this.dispatches.delete(target);
                    else await this.cancelPhase(target, entry, phase, account);
                    return;
                }
                const run = checkedLaunch((await this.github.get(`repos/${CENTRAL}/actions/runs/${entry.runId}`)).data,
                    target, entry, account);
                if (entry.status !== "accepted" || this.dispatches.get(target) !== entry) return;
                delete entry.reconcileError;
                if (run.status !== "completed") return;
                if (entry.operation === "launch" && run.conclusion === "failure") {
                    const current = await this.current(target);
                    if (entry.status !== "accepted" || this.dispatches.get(target) !== entry) return;
                    if (!actionBlock(current.pr, current.viewer, current.phase, true, null)) {
                        this.dispatches.delete(target);
                        return;
                    }
                }
                if (entry.operation === "cancel_dispatch" && run.conclusion === "cancelled") {
                    const current = await this.current(target);
                    if (current.phase?.launchId === entry.runId && !TERMINAL.has(current.phase.stage)) {
                        await this.cancelPhase(target, entry, current.phase, current.viewer);
                    } else this.dispatches.delete(target);
                } else {
                    entry.status = "finished";
                    entry.conclusion = run.conclusion;
                    entry.finishedAt = run.updated_at;
                    entry.message = `Launch finished with ${run.conclusion ?? "unknown conclusion"}, without a confirmed task. Inspect its Actions run; do not blindly relaunch.`;
                    if (run.conclusion === "failure" && superseded(run.updated_at)) this.dispatches.delete(target);
                }
            } catch (error) {
                if (entry.operation === "cancel" || error.uncertain) entry.status = error.uncertain ? "uncertain" : "failed";
                entry.message = error.message;
                entry.reconcileError = error.message;
                warnings.push(`${target}: ${error.message}`);
                this.auto = false;
                this.pauseReason = "Dispatch status could not be reconciled. Inspect central Actions.";
            }
        }));
    }

    async current(target) {
        const { repo, number } = targetParts(target);
        const account = viewer((await this.github.get("user")).data);
        if (account.id !== LAUNCH_OWNER_ID) throw new Error("The workflows only accept the configured personal owner's dispatch.");
        const pr = checkedPull((await this.github.get(`repos/${repo}/pulls/${number}`)).data, repo);
        const snapshot = await this.checkpoints.load();
        const phases = snapshot.current.map(phaseSummary).filter((phase) => phase.target.toLowerCase() === target.toLowerCase());
        if (phases.length > 1) throw new Error("Multiple checkpoints exist for this PR. Inspect central state before dispatching.");
        const normalized = normalizePull(pr, repo, null, account);
        if (!normalized.mine && !normalized.bot) normalized.mine = (await this.github.ownPullNumbers(repo)).has(number);
        return { pr: normalized, viewer: account, phase: phases[0] };
    }

    launch(input) {
        if (!input || Object.keys(input).some((key) => !["target", "kind", "confirmed"].includes(key)) ||
            input.confirmed !== true || !Object.hasOwn(KIND_LABELS, input.kind)) {
            return Promise.reject(new Error("Confirm the explicit target and supported task before launch."));
        }
        return this.mutate(input.target, "launch", async () => {
            const current = await this.current(input.target);
            const blocked = actionBlock(current.pr, current.viewer, current.phase, true, null);
            if (blocked) throw new Error(blocked);
            if (!taskChoices(current.pr, current.viewer).includes(input.kind)) throw new Error("This task is not eligible for the PR's author.");
            if (["pr_conflict_resolver", "ci_fix", "copilot_review"].includes(input.kind)) {
                const read = (await this.github.pullEvidence(current.pr.repo, [current.pr])).get(current.pr.number);
                if (!read?.detail) throw new Error(read?.error ?? "Live action status is unavailable. Refresh before launching.");
                current.pr.evidence = normalizeEvidence(read.detail, current.pr.sha);
                const evidence = actionEvidence(current.pr, input.kind);
                if (evidence.disabled) throw new Error(evidence.detail);
            }
            return {
                inputs: { operation: "launch", target: input.target, loop_kind: input.kind, publication_auth: "fine_grained_pat" },
                kind: input.kind, previousLaunch: current.phase?.launchId ?? null,
            };
        }, input.kind);
    }

    cancel(input) {
        if (input && Object.hasOwn(input, "runId")) return this.cancelDispatch(input);
        if (!input || Object.keys(input).some((key) => !["target", "requestId", "generation", "confirmed"].includes(key)) ||
            input.confirmed !== true || !/^[0-9a-f]{32}$/.test(input.requestId) ||
            !Number.isSafeInteger(input.generation) || input.generation < 1) {
            return Promise.reject(new Error("Confirm cancellation with the exact observed request and generation."));
        }
        return this.mutate(input.target, "cancel", async () => {
            const { phase } = await this.current(input.target);
            if (!phase || phase.historical || phase.unknownStage || TERMINAL.has(phase.stage) ||
                phase.requestId !== input.requestId || phase.generation !== input.generation) {
                throw new Error("Cancellation selection is stale or unsupported. Refresh before cancelling.");
            }
            return {
                inputs: { operation: "cancel", target: input.target, previous_request: input.requestId,
                    previous_generation: String(input.generation) },
                requestId: input.requestId, generation: input.generation,
            };
        });
    }

    async cancelPhase(target, entry, phase, account) {
        if (phase.historical || phase.unknownStage || phase.kind !== entry.kind || phase.authorizedActorId !== account?.id ||
            !Number.isSafeInteger(phase.generation) || phase.generation < 1) {
            throw new Error("The task checkpoint is unsupported. Inspect central state before cancelling.");
        }
        Object.assign(entry, { operation: "cancel", status: "pending", requestId: phase.requestId,
            generation: phase.generation, message: "Cancelling the confirmed task." });
        const receipt = await this.github.dispatch({
            operation: "cancel", target, previous_request: phase.requestId, previous_generation: String(phase.generation),
        });
        entry.cancelRunId = receipt.runId;
        entry.status = "accepted";
        entry.message = "Task cancellation accepted. Refresh to confirm it has stopped.";
    }

    async cancelDispatch(input) {
        if (Object.keys(input).some((key) => !["target", "runId", "confirmed"].includes(key)) ||
            input.confirmed !== true || !Number.isSafeInteger(input.runId) || input.runId < 1) {
            throw new Error("Confirm cancellation with the exact accepted launch run ID.");
        }
        targetParts(input.target);
        const entry = this.dispatches.get(input.target);
        if (entry?.operation !== "launch" || entry.status !== "accepted" || entry.runId !== input.runId) {
            throw new Error("The accepted dispatch selection is stale or unsupported. Refresh before cancelling.");
        }
        if (this.selecting) throw new Error("Wait for repository selection to finish.");
        const previous = { ...entry };
        Object.assign(entry, { operation: "cancel_dispatch", status: "pending", message: "Cancelling the accepted dispatch." });
        try {
            if (this.pending) await this.pending;
            const { viewer: account, phase } = await this.current(input.target);
            if (phase?.launchId === entry.runId) {
                if (TERMINAL.has(phase.stage)) throw new Error("The dispatched task has already finished. Refresh its result.");
                await this.cancelPhase(input.target, entry, phase, account);
            } else {
                const run = checkedLaunch((await this.github.get(`repos/${CENTRAL}/actions/runs/${entry.runId}`)).data,
                    input.target, entry, account);
                if (run.status === "completed") {
                    throw new Error(`The launch has already finished with ${run.conclusion ?? "unknown conclusion"}. Inspect its Actions run.`);
                }
                await this.github.cancelRun(entry.runId);
                entry.status = "accepted";
                entry.message = "Dispatch cancellation requested. Refresh to confirm it has stopped.";
            }
            this.schedule();
            return { target: input.target, operation: entry.operation, status: entry.status,
                runId: entry.runId, message: entry.message };
        } catch (error) {
            if (error.uncertain) {
                entry.status = "uncertain";
                entry.message = error.message;
            } else this.dispatches.set(input.target, previous);
            throw error;
        }
    }

    async mutate(target, operation, prepare, kind = null) {
        targetParts(target);
        if (this.selecting) throw new Error("Wait for repository selection to finish.");
        if (this.dispatches.has(target)) throw new Error(this.dispatches.get(target).message);
        if (!this.prs.some((pr) => pr.target === target) || !this.prLoadedAt || this.prError ||
            !this.value.loadedAt || this.value.error || this.value.warnings.length) {
            throw new Error("Refresh this repository successfully before dispatching a task.");
        }
        const entry = { operation, kind, status: "pending", message: "Dispatch is pending. Do not submit again." };
        this.dispatches.set(target, entry);
        try {
            if (this.pending) await this.pending;
            const prepared = await prepare();
            Object.assign(entry, prepared);
            delete entry.inputs;
            const receipt = await this.github.dispatch(prepared.inputs);
            entry.runId = receipt.runId;
            entry.runUrl = receipt.runUrl;
            entry.status = "accepted";
            entry.message = "Dispatch accepted. Execution is not yet confirmed; refresh to observe central state.";
            return { target, operation, status: entry.status, message: entry.message,
                runId: entry.runId, runUrl: entry.runUrl };
        } catch (error) {
            if (error.uncertain) {
                entry.status = "uncertain";
                entry.message = error.message;
            } else this.dispatches.delete(target);
            throw error;
        }
    }
}
