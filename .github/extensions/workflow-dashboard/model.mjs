import { CENTRAL } from "./github.mjs";

export const TERMINAL = new Set(["preview_complete", "shadow_complete", "blocked", "failed", "cancelled", "exhausted", "clean", "complete"]);
export { KIND_LABELS } from "./kinds.mjs";
const REPO = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const SHA = /^[0-9a-f]{40}$/;
const REQUEST = /^[0-9a-f]{32}$/;
const OLD_STAGES = new Set(["auth_pending", "capability_intent", "waiting_capability", "capability_recheck", "root_effects"]);
const STAGES = new Set([...TERMINAL, "ready", "source_pending", "dispatch_intent", "dispatched",
    "running", "verify_pending", "publish_pending", "publication_intent", "published",
    "review_request_intent", "waiting_review", "waiting_ci", "thread_effects", "threads_settled", "task_effect_intent"]);

export function repository(value) {
    return typeof value === "string" && REPO.test(value) && !value.split("/").some((part) => part === "." || part === "..");
}

export function commitLink(repo, sha) {
    return repository(repo) && SHA.test(sha) ? `https://github.com/${repo}/commit/${sha}` : null;
}

export function compareLink(repo, base, head) {
    return repository(repo) && SHA.test(base) && SHA.test(head) && base !== head
        ? `https://github.com/${repo}/compare/${base}...${head}` : null;
}

export function prChangesLink(repo, pr, base, head) {
    return repository(repo) && Number.isSafeInteger(pr) && pr > 0 &&
        SHA.test(base) && SHA.test(head) && base !== head
        ? `https://github.com/${repo}/pull/${pr}/changes/${base}..${head}` : null;
}

export function runLink(run) {
    return Number.isSafeInteger(run?.id) && run.id > 0
        ? `https://github.com/${CENTRAL}/actions/runs/${run.id}${Number.isInteger(run.attempt) && run.attempt > 0 ? `/attempts/${run.attempt}` : ""}`
        : null;
}

export function phaseKey(state) {
    const request = state.request;
    return state.phase || request.publication?.phase || request.request_id;
}

export function historical(state) {
    const request = state.request;
    const supported = request.protocol === "git-candidate-v1" ||
        request.protocol === "reviewable-v1" && SHA.test(request.workflow_revision) &&
        request.workflow_ref === `review-loop-revisions/${request.workflow_revision}`;
    return state.schema !== 2 || request.schema !== 2 || !supported ||
        OLD_STAGES.has(state.stage) || Boolean(state.capability || state.capability_probe ||
            request.publication?.reply_bot_threads || request.publication?.reviewable_retry ||
            state.report?.objective_validation || state.report?.validation_claim);
}

export function checkedRecord(record) {
    const s = record.state;
    const r = s?.request;
    if (!r || !repository(r.repo) || !Number.isSafeInteger(r.pr) || r.pr < 1 || r.pr >= 100000000 ||
        typeof r.request_id !== "string" || !REQUEST.test(r.request_id) || typeof s.stage !== "string" ||
        !Number.isInteger(s.iteration) || s.iteration < 0 || s.iteration > 100 ||
        !Number.isInteger(r.frozen_at) || r.deadline !== undefined && !Number.isInteger(r.deadline) ||
        r.budgets?.max_iterations !== undefined && !Number.isInteger(r.budgets.max_iterations) ||
        ![r.frozen_sha, s.expected_sha].every((value) => value === null || value === undefined ||
            typeof value === "string" && SHA.test(value)) ||
        ![s.phase, r.publication?.phase].every((phase) => phase === undefined || typeof phase === "string" && REQUEST.test(phase)) ||
        ![s.publications, s.artifacts, s.effects, r.findings, s.report?.candidate?.changed_paths,
            s.report?.candidate?.commits,
            s.report?.dispositions?.findings, s.report?.dispositions?.consistency,
            s.report?.dispositions?.comments, s.task_completion?.comments,
            s.ci_reruns, s.ci_warnings, s.ci?.checks, s.fresh_review?.inline_ids]
            .every((items) => items === undefined || Array.isArray(items) && items.every((item) => item !== null))) {
        throw new Error(`Checkpoint ${record.name} has an unsupported or malformed identity.`);
    }
    for (const publication of s.publications ?? []) {
        if (!publication || typeof publication !== "object" || !REQUEST.test(publication.request_id) ||
            !SHA.test(publication.sha) || !["push", "no_change"].includes(publication.effect)) {
            throw new Error(`Checkpoint ${record.name} has malformed publication evidence.`);
        }
    }
    if (!(r.findings ?? []).every((item) => item && typeof item === "object") ||
        !(s.artifacts ?? []).every((item) => item && typeof item === "object") ||
        !(s.ci?.checks ?? []).every((item) => item && typeof item === "object") ||
        !(s.report?.dispositions?.findings ?? []).every((item) => item && typeof item === "object") ||
        ![s.report?.dispositions?.comments, s.task_completion?.comments].every((items) =>
            (items ?? []).every((item) => item && typeof item.path === "string" &&
                typeof item.body === "string" && Number.isSafeInteger(item.line) && item.line > 0)) ||
        !(s.report?.candidate?.changed_paths ?? []).every((item) => typeof item === "string")) {
        throw new Error(`Checkpoint ${record.name} has malformed iteration evidence.`);
    }
    const candidates = [s.report?.candidate, s.publication_intent?.candidate,
        ...(s.publications ?? []).map((publication) => publication.candidate)];
    for (const candidate of candidates) {
        if (candidate?.commits !== undefined &&
            (!Array.isArray(candidate.commits) || candidate.commits.length > 100 ||
                !candidate.commits.every((entry) => entry && typeof entry === "object" &&
                    typeof entry.commit === "string" && SHA.test(entry.commit) &&
                    typeof entry.subject === "string" && entry.subject.length > 0)) ||
            candidate?.finding_commits !== undefined &&
            (!candidate.finding_commits || typeof candidate.finding_commits !== "object" ||
                Array.isArray(candidate.finding_commits) ||
                !Object.values(candidate.finding_commits).every((sha) => typeof sha === "string" && SHA.test(sha)))) {
            throw new Error(`Checkpoint ${record.name} has malformed batch evidence.`);
        }
    }
    if (!historical(s) && !(s.effects ?? []).every((effect) =>
        effect && typeof effect === "object" && typeof effect.key === "string" &&
        Number.isSafeInteger(effect.root) && effect.root > 0 && typeof effect.thread === "string" &&
        ["pending", "confirmed", "skipped", "failed", "uncertain"].includes(effect.status) &&
        [effect.reply, effect.resolution].every((intent) => intent === undefined ||
            intent && typeof intent === "object" &&
            ["uncertain", "acknowledged", "confirmed", "failed"].includes(intent.status)))) {
        throw new Error(`Checkpoint ${record.name} has malformed thread-effect evidence.`);
    }
    return record;
}

function time(value) {
    return Number.isFinite(value) ? value * 1000 : null;
}

function findings(s, publication) {
    const dispositions = s.report?.dispositions?.findings;
    const byKey = new Map(Array.isArray(dispositions) ? dispositions.map((item) => [item.key, item]) : []);
    return (Array.isArray(s.request.findings) ? s.request.findings : []).map((item) => ({
        key: item.key, path: typeof item.path === "string" ? item.path : null,
        body: typeof item.body === "string" ? item.body : "",
        disposition: byKey.get(item.key)?.disposition ?? null,
        reason: byKey.get(item.key)?.analysis ?? byKey.get(item.key)?.reason ?? null,
        upsides: byKey.get(item.key)?.upsides ?? null,
        downsides: byKey.get(item.key)?.downsides ?? null,
        commitUrl: publication?.effect === "push"
            ? commitLink(s.request.head_repo, publication.candidate?.finding_commits?.[item.key]) : null,
        effects: (s.effects ?? []).filter((effect) => effect.key === item.key).map((effect) => ({
            status: effect.status, reason: effect.reason,
            reply: effect.reply?.status ?? (effect.status === "skipped" ? "skipped" : "pending"),
            resolution: effect.resolution?.status ?? (effect.status === "skipped" ? "skipped" : "pending"),
            replyUrl: effect.reply?.status === "confirmed" && Number.isSafeInteger(effect.reply.reply_id) &&
                effect.reply.reply_id > 0
                ? `https://github.com/${s.request.repo}/pull/${s.request.pr}#discussion_r${effect.reply.reply_id}` : null,
        })),
        url: Number.isSafeInteger(item.comment_id) && item.comment_id > 0
            ? `https://github.com/${s.request.repo}/pull/${s.request.pr}#discussion_r${item.comment_id}` : null,
    }));
}

export function phaseSummary(record) {
    checkedRecord(record);
    const s = record.state;
    const r = s.request;
    const old = historical(s);
    const known = STAGES.has(s.stage);
    return {
        id: `${r.repo}#${r.pr}:${phaseKey(s)}`, target: `${r.repo}#${r.pr}`,
        phase: phaseKey(s), checkpoint: record.name, historical: old, stage: s.stage,
        category: old ? "historical" : !known ? "attention" :
            ["blocked", "failed", "exhausted", "cancelled"].includes(s.stage) ? "attention" :
                TERMINAL.has(s.stage) ? "completed" :
                    ["waiting_ci", "waiting_review", "review_request_intent", "task_effect_intent"].includes(s.stage) ? "waiting" : "active",
        kind: r.loop_kind ?? "copilot_review", mode: r.mode ?? "unknown",
        outcome: s.task_completion?.outcome ?? null,
        reviewCommentCount: (s.task_completion?.comments ?? s.report?.dispositions?.comments)?.length ?? null,
        pendingReviewUrl: s.task_completion?.outcome === "pending_review" &&
            Number.isSafeInteger(s.task_completion.review_id) && s.task_completion.review_id > 0
            ? `https://github.com/${r.repo}/pull/${r.pr}#pullrequestreview-${s.task_completion.review_id}` : null,
        reason: s.reason ?? null, error: s.error ?? null,
        iteration: s.iteration, maximum: ["pr_conflict_resolver", "pr_description", "pr_simplify",
            "pr_review", "pr_consistency"].includes(r.loop_kind) ? 1 : r.budgets?.max_iterations ?? null,
        started: time(r.publication?.authorized_at ?? r.frozen_at),
        deadline: time(r.deadline), nextCheck: time(s.next_check_at),
        sha: s.expected_sha ?? r.frozen_sha, headRepo: r.head_repo ?? r.repo,
        url: `https://github.com/${r.repo}/pull/${r.pr}`,
        commitUrl: commitLink(r.head_repo ?? r.repo, s.expected_sha ?? r.frozen_sha),
        requestId: r.request_id, generation: s.generation, authorizedActorId: r.authorized_actor_id,
        runUrl: runLink(s.run),
        coordinatorUrl: runLink(s.coordinator_run),
        findingCount: Array.isArray(r.findings) ? r.findings.length : null,
        publications: Array.isArray(s.publications) ? s.publications.length : 0,
        unknownStage: !known && !old,
        launchId: r.launch_run?.id ?? null,
        sourceId: s.source_claim?.run_id ?? null,
        verificationId: s.verification_run?.id ?? s.validation_run?.id ?? null,
        coordinatorId: s.coordinator_run?.id ?? null,
        workerId: s.run?.id ?? null,
    };
}

function iteration(record, publication) {
    const s = record.state;
    const r = s.request;
    const candidate = s.report?.candidate ?? s.publication_intent?.candidate ?? null;
    const headRepo = r.head_repo ?? r.repo;
    const review = s.fresh_review;
    return {
        requestId: r.request_id, number: s.iteration, generation: s.generation,
        frozen: time(r.frozen_at), dispatched: time(s.intent?.recorded_at),
        inputSha: r.frozen_sha, inputUrl: commitLink(headRepo, r.frozen_sha),
        workerUrl: runLink(s.run), workerConclusion: s.run?.conclusion ?? null,
        verifierUrl: runLink(s.verification_run ?? s.validation_run), coordinatorUrl: runLink(s.coordinator_run),
        kind: r.loop_kind ?? "copilot_review",
        stage: s.stage, reason: s.reason ?? null, error: s.error ?? s.report?.error ?? null,
        historical: historical(s), outcome: s.report?.dispositions?.outcome ?? null,
        verification: historical(s) ? "historical evidence" : s.report?.verification ?? "not recorded",
        changedPaths: Array.isArray(candidate?.changed_paths) ? candidate.changed_paths : [],
        candidateSha: ["pr_description", "pr_review"].includes(r.loop_kind) ||
            candidate?.changed === false ? null : candidate?.commit ?? null,
        publication: publication ? {
            sha: publication.sha, effect: publication.effect, confirmed: time(publication.confirmed_at),
            url: publication.effect === "push" ? commitLink(headRepo, publication.sha) : null,
            compareUrl: publication.effect === "push" ? compareLink(headRepo, publication.candidate?.parent, publication.sha) : null,
            commits: publication.effect === "push" ? (publication.candidate?.commits ?? []).map((entry) => ({
                sha: entry.commit, subject: entry.subject, url: commitLink(headRepo, entry.commit),
                parents: (entry.parents ?? [entry.parent]).map((sha) => ({
                    sha, url: commitLink(headRepo, sha),
                })),
            })) : [],
        } : null,
        findings: findings(s, publication),
        reviewComments: (s.report?.dispositions?.comments ?? s.task_completion?.comments ?? []).map((item) => ({
            path: item.path, line: item.line, body: item.body,
        })),
        review: review ? {
            decision: review.decision,
            url: Number.isSafeInteger(review.review_id) && review.review_id > 0
                ? `https://github.com/${r.repo}/pull/${r.pr}#pullrequestreview-${review.review_id}` : null,
            submitted: review.submitted_at ?? null,
            inlineUrls: (Array.isArray(review.inline_ids) ? review.inline_ids : []).filter(Number.isSafeInteger)
                .map((id) => `https://github.com/${r.repo}/pull/${r.pr}#discussion_r${id}`),
        } : null,
        ci: s.ci ? { decision: s.ci.decision, sha: s.ci.sha, checks: s.ci.checks ?? [] } : null,
        task: s.task_completion ?? null,
        proposal: r.loop_kind === "pr_description" ? s.report?.dispositions?.proposal ?? null : null,
        taskEffect: s.task_intent ? {
            kind: s.task_intent.kind, status: s.task_intent.status,
            reviewUrl: Number.isSafeInteger(s.task_intent.review_id) && s.task_intent.review_id > 0
                ? `https://github.com/${r.repo}/pull/${r.pr}#pullrequestreview-${s.task_intent.review_id}` : null,
        } : null,
        consistency: s.report?.dispositions?.consistency ?? [],
        diagnoses: s.report?.dispositions?.diagnoses ?? [],
        ciWarnings: s.ci_warnings ?? [],
        ciReruns: (s.ci_reruns ?? []).map((entry) => ({
            runId: entry.run_id, attempt: entry.resulting_attempt, status: entry.status,
            url: Number.isSafeInteger(entry.run_id) && entry.run_id > 0
                ? `https://github.com/${r.repo}/actions/runs/${entry.run_id}` : null,
        })),
        artifacts: (Array.isArray(s.artifacts) ? s.artifacts : []).map((item) => ({
            name: item.name, expired: item.expired,
            url: artifactLink(s, item),
        })),
    };
}

function artifactLink(state, item) {
    const pipeline = /^(?:verification|validation)-([1-9][0-9]*)-[1-9][0-9]*$/.exec(item.name);
    const source = typeof item.name === "string" && item.name.startsWith("source-");
    const run = pipeline ? Number(pipeline[1]) : source ? state.source_claim?.run_id : state.run?.id;
    return Number.isSafeInteger(item.id) && item.id > 0 && Number.isSafeInteger(run) && run > 0
        ? `https://github.com/${CENTRAL}/actions/runs/${run}/artifacts/${item.id}` : null;
}

export function targetHistory(records, target) {
    const groups = new Map();
    const warnings = [];
    for (const record of records) {
        try {
            checkedRecord(record);
        } catch (error) {
            warnings.push(error.message);
            continue;
        }
        const s = record.state;
        if (`${s.request.repo}#${s.request.pr}` !== target) continue;
        const key = phaseKey(s);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(record);
    }
    const phases = [];
    for (const items of groups.values()) {
        const requests = new Map();
        const publications = new Map();
        for (const record of items) {
            const s = record.state;
            const previous = requests.get(s.request.request_id);
            const score = (entry) => (entry.name.startsWith("pr-") ? 1000000 : entry.name.startsWith("stopped-") ? 0 : 100000) +
                (entry.state.generation ?? 0);
            if (!previous || score(record) > score(previous)) requests.set(s.request.request_id, record);
            for (const publication of s.publications ?? []) publications.set(publication.request_id, publication);
        }
        const sorted = [...requests.values()].sort((a, b) =>
            a.state.request.frozen_at - b.state.request.frozen_at || (a.state.generation ?? 0) - (b.state.generation ?? 0));
        const latest = sorted.findLast((entry) => entry.name.startsWith("pr-")) ?? sorted.at(-1);
        const iterations = sorted.filter((entry) => entry.state.intent || entry.state.run || entry.state.report ||
            publications.has(entry.state.request.request_id))
            .map((entry) => iteration(entry, publications.get(entry.state.request.request_id)));
        const actualNumbers = new Set(iterations.map((entry) => entry.number));
        const gaps = Array.from({ length: latest.state.iteration }, (_, index) => index + 1)
            .filter((number) => !actualNumbers.has(number));
        const missingPublications = [...publications.keys()].filter((id) => !requests.has(id));
        phases.push({
            ...phaseSummary(latest), current: items.some((entry) => entry.name.startsWith("pr-")),
            changeRanges: publicationRanges([...publications.values()], latest.state.request),
            iterations, gaps, missingPublications,
            orphanPublications: missingPublications.map((id) => {
                const publication = publications.get(id);
                const headRepo = latest.state.request.head_repo ?? latest.state.request.repo;
                return {
                    requestId: id, sha: publication.sha, effect: publication.effect,
                    confirmed: time(publication.confirmed_at),
                    url: publication.effect === "push" ? commitLink(headRepo, publication.sha) : null,
                };
            }),
            transitions: sorted.filter((entry) => !iterations.some((item) => item.requestId === entry.state.request.request_id))
                .map((entry) => ({ requestId: entry.state.request.request_id, stage: entry.state.stage,
                    reason: entry.state.reason, frozen: time(entry.state.request.frozen_at) })),
        });
    }
    return { target, phases: phases.sort((a, b) => (b.started ?? 0) - (a.started ?? 0)), warnings };
}

function publicationRanges(publications, request) {
    const ranges = [];
    for (const publication of publications.filter((item) => item.effect === "push")) {
        const base = publication.candidate?.parent ?? null;
        const head = publication.sha;
        const commits = publication.candidate?.commits?.length || 1;
        const previous = ranges.at(-1);
        if (previous?.url && base === previous.head) {
            previous.head = head;
            previous.commits += commits;
            previous.url = prChangesLink(request.repo, request.pr, previous.base, head);
            previous.commitUrl = commitLink(request.head_repo ?? request.repo, head);
        } else ranges.push({
            base, head, commits, url: prChangesLink(request.repo, request.pr, base, head),
            commitUrl: commitLink(request.head_repo ?? request.repo, head),
        });
    }
    return ranges;
}

export const RUN_LOG_WINDOW = 24 * 60 * 60 * 1000;

export function recentTaskLog(records, runs, now) {
    const since = now - RUN_LOG_WINDOW;
    const targets = new Map();
    const warnings = [];
    for (const record of records) {
        try {
            checkedRecord(record);
            const target = `${record.state.request.repo}#${record.state.request.pr}`;
            if (!targets.has(target)) targets.set(target, []);
            targets.get(target).push(record);
        } catch (error) {
            warnings.push(error.message);
        }
    }
    const entries = new Map();
    for (const [target, items] of targets) {
        for (const phase of targetHistory(items, target).phases) {
            const activity = items.filter((item) => phaseKey(item.state) === phase.phase).flatMap(({ state: s }) => [
                time(s.request.frozen_at), time(s.intent?.recorded_at), time(s.cancelled_at),
                ...(s.publications ?? []).map((publication) => time(publication.confirmed_at)),
            ]);
            const lastActivity = Math.max(phase.started ?? 0, ...activity.filter(Number.isFinite));
            if (lastActivity < since && !["active", "waiting"].includes(phase.category)) continue;
            entries.set(phase.launchId ?? phase.id, {
                id: phase.id, phase: phase.phase, target: phase.target, url: phase.url,
                kind: phase.kind, stage: phase.stage, category: phase.category,
                started: phase.started, lastActivity, reason: phase.reason, error: phase.error,
                historical: phase.historical, unknownStage: phase.unknownStage,
                outcome: phase.outcome, pendingReviewUrl: phase.pendingReviewUrl,
                reviewCommentCount: phase.reviewCommentCount, changeRanges: phase.changeRanges,
                runUrl: runLink({ id: phase.launchId, attempt: 1 }), evidence: true,
            });
        }
    }
    for (const run of runs) {
        if (!run.display_title?.startsWith("Review loop launch ") || Date.parse(run.created_at) < since) continue;
        const action = actionSummary(run, []);
        const match = /^Review loop launch (\S+) ([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)#([1-9][0-9]{0,7})$/.exec(run.display_title);
        const entry = entries.get(run.id);
        if (entry) {
            entry.started = Date.parse(run.created_at);
            entry.runUrl = action.url;
            continue;
        }
        const [, kind, repo, number] = match ?? [];
        entries.set(run.id, {
            id: `launch:${run.id}`, kind: kind ?? null,
            target: match ? `${repo}#${number}` : null,
            url: match && repository(repo) ? `https://github.com/${repo}/pull/${number}` : null,
            title: action.title, started: Date.parse(run.created_at),
            runUrl: action.url, stage: run.status, conclusion: run.conclusion,
            category: run.status === "completed" ? "attention" : "active",
            changeRanges: [], evidence: false,
        });
    }
    return { entries: [...entries.values()].sort((a, b) => b.started - a.started), warnings };
}

export function actionSummary(run, summaries) {
    if (!Number.isSafeInteger(run.id) || run.id < 1 ||
        !["queued", "in_progress", "waiting", "pending", "requested", "completed"].includes(run.status)) {
        throw new Error("GitHub returned an invalid worker Actions run.");
    }
    return summarizeAction(run, summaries);
}

function summarizeAction(run, summaries) {
    const matched = summaries.filter((phase) =>
        [phase.workerId, phase.coordinatorId, phase.launchId, phase.sourceId, phase.verificationId].includes(run.id) ||
        run.display_title === `Copilot worker ${phase.requestId}` ||
        phase.historical && run.display_title === `Copilot shadow ${phase.requestId}`);
    return {
        id: run.id, name: run.name, title: run.display_title, status: run.status,
        created: run.created_at, url: runLink({ id: run.id, attempt: run.run_attempt }),
        targets: [...new Set(matched.map((phase) => phase.target))],
    };
}
