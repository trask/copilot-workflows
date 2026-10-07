# Setup and recovery

## Workflow kinds and permissions

All workflow kinds require a public PR repository and a public head repository, including forks. Private, internal or unknown visibility is rejected before review/diff collection or source acquisition. `trask/copilot-workflows` cannot be a PR or head repository, regardless of visibility or capitalization. Frozen private or central-target requests are read-only under the current runtime and cannot dispatch, verify, publish or route to older pinned workflows. A live visibility change stops source preparation, verification and publication.

Choose one independently launched `loop_kind`. The existing defaults, `copilot_review` and `self_review`, keep their review/clean-CI semantics. The new choices are `pr_conflict_resolver`, `ci_fix`, `pr_description`, `pr_simplify`, `pr_review` and `pr_consistency`. See the [behavior table](../README.md#independent-tasks).

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f loop_kind=pr_review \
  -f publication_auth=fine_grained_pat -f target=owner/repository#123
```

Only the central launch owner can dispatch. PR Reviewer accepts the owner's or another author's open PR, including bot-authored PRs, and cannot gain source publication permission. All other kinds require the owner's open PR. A launch authorizes only the selected task; none of the six new kinds posts top-level comments, replies to people, resolves threads, changes draft state, submits/approves reviews or lands a PR.

| Task effect | Selected secret owner | Required target access |
| --- | --- | --- |
| Fix batches or base merge | Head repository | Head Contents write, base PR read; Workflows write for workflow edits |
| Description title/body PATCH | Base repository | Base Contents read and Pull requests write; no fork push permission |
| Viewer-owned pending review | Base repository | Base Contents read and Pull requests write; no fork push permission |
| CI repair code push | Head repository | Head Contents write, base PR/Checks/Statuses/Actions read |
| Evidence-based failed-jobs rerun | Base repository | Base PR/Checks/Statuses/Actions read and Actions write; no head push permission |

The selected fine-grained PAT must authenticate as the launch owner and be issued only for the intended public target/head repositories, excluding the central repository. The publisher checks the actor, frozen repository identities and bound effects; it never probes central readability or claims to attest the PAT's complete repository scope. Its API client permits only target/head reads and authorized target mutations. No task falls back to another token. CI launch authorizes possible source repairs with the head-owner secret. An accepted rerun proposal explicitly routes that effect to the base-owner secret, not to a fallback credential. A missing base-owner mapping or denied Actions write stops the rerun. Git source acquisition is always unauthenticated, never using the publisher, inference or optional API read token.

New diff-based tasks require full head/merge-base trees and the complete authoritative PR diff. The diff is bounded to 200,000 UTF-8 bytes, 1,000 files and 10,000 added-line anchors. Source acquisition, verification and publication share a 100,000-object limit, 64 MiB of unique Git object content and 4 MiB per object. Each expanded source tree is bounded separately to 64 MiB. GitHub binary markers identify changed files; their bytes come from the bound complete Git snapshots, not invented text-line anchors. Truncation or missing source stops rather than narrowing scope. Conflict resolution also freezes bounded history through the merge base, at most 256 commits. Its publisher preserves exact frozen head/base parents, including merges whose tree equals the original head, and every cleanly merged incoming change.

CI repair waits for active checks before diagnosis. Trusted preflight binds run IDs, current attempts, job/check identities and at most 120,000 bytes of combined evidence, including logs bounded to 60,000 bytes per job. Non-Actions output participates when available. Missing evidence and unknown causes cannot clear the phase. Every failed check needs an evidenced diagnosis. Unrelated/pre-existing failures remain explicit warnings with failed CI, never green clearance.

The one failed-jobs rerun allowance is per Actions run. Any existing manual or automated retry consumes it. The publisher records an intent, rechecks head/base/run/attempts, sends one POST, and confirms exactly the next attempt. Lost responses reconcile without another POST. Code pushes and confirmed retries do not establish passing CI. CI repair retains the five-worker/two-hour ceiling.

Simplify and consistency use ordinary root-cause batches without another review pass or Copilot request. No qualifying change means no commit. Consistency reports retain classifications, explanations and frozen source citations. Single-pass source tasks finish separately from resulting CI: pending checks wait; failed, unknown or unavailable CI remains recorded even when task completion is `complete`.

Description rechecks the complete diff and original title/body before a title/body-only PATCH and confirms exact live text afterward. PR Reviewer verifies every RIGHT-side changed-line anchor, viewer identity and existing pending reviews before a single review POST containing `commit_id` and comments but no `event`. An existing viewer-owned pending review remains untouched. No findings means no review mutation. Neither task waits for green CI.

Description results always contain a complete `proposal` object with `title` and `body`, including `no_change` and `blocked`. A no-change result copies the frozen `metadata` exactly and performs no metadata mutation. A null or missing proposal is rejected.

All effects bind the verified result, request digest, generation, frozen target and durable intent. Drift, denial, ambiguous confirmation or a lost effect that cannot reconcile stops explicitly. All tasks retain cancellation, source/artifact provenance and historical read-only rules. A completed task is not the existing review loops' `clean` guarantee.

The bot accepts an explicit `owner/repo#number` or GitHub PR URL. It has no language setting or repository allowlist. The default `loop_kind=copilot_review` investigates existing verified Copilot findings. `loop_kind=self_review` reviews the full PR and fixes warranted problems in one fresh worker per pass. Both read repository instructions, format changes and run appropriate existing checks.

## Credentials only when needed

### Protected Actions environment

Store all custom Actions secrets in the `protected` environment in `trask/copilot-workflows`, not at repository level. Its deployment policy allows only branch refs named `main` or matching `review-loop-revisions/*`. Both are needed because continuations run on immutable automation revision branches. Do not allow tags or arbitrary branches.

No required reviewers or wait timer are configured, so authorized jobs run automatically. This is a branch restriction, not a manual approval gate. Jobs that read custom secrets, including credential-presence checks, worker activation, inference and detection, explicitly reference `protected`. Fast checks and the structural verifier do not.

The worker's `on.manual-approval: protected` compiler setting binds its activation job to the same environment. It requires human approval only if the environment has required reviewers.

Add the tokens through the environment's secrets UI or the CLI's interactive prompts:

```bash
gh secret set COPILOT_GITHUB_TOKEN --repo trask/copilot-workflows --env protected
gh secret set TEST_PUBLISH_TOKEN --repo trask/copilot-workflows --env protected
gh secret set OPENTELEMETRY_PUBLISH_TOKEN --repo trask/copilot-workflows --env protected
```

Do not keep repository-level copies of these secrets: jobs without the environment can access repository secrets. GitHub cannot retrieve existing secret values; adding or moving a secret requires its token value.

Public PR/review reads use the central Actions token because GraphQL requires authentication. An optional `REVIEW_LOOP_SOURCE_READ_TOKEN` can supply scoped public API reads, but cannot authorize a private target or head and is never passed to Git source retrieval. Missing API access stops as `human_gate_target_repository_read_access`. The controller never reads local CLI credentials or borrows inference auth.

If used, store `REVIEW_LOOP_SOURCE_READ_TOKEN` in `protected` too. Keep the non-secret `PUBLISHER_SECRETS` routing map as a repository variable.

Workers also need `COPILOT_GITHUB_TOKEN`, a personal inference credential with Copilot Requests read access and no repository write access. The pinned engine proxies inference and excludes it from the agent environment. Do not add inherited MCP, OTLP or broad repository credentials to the worker.

Launch uses explicit `publication_auth=fine_grained_pat`. The two review loops publish warranted fixes. Copilot review always replies to and resolves eligible original Copilot threads; self-review never does. There are no preview/shadow modes or optional thread switches. The credential must authenticate as the launch owner, read the actual target PR and push the actual head repository/ref. Issue it only for the intended target/head repositories and keep it separate from the central Actions token. Missing or disabled auth records `human_gate_target_repository_push_and_review_access`; the publisher checks identity and actual target/head access before effects.

### Owner-scoped publisher secrets

Create one fine-grained PAT per resource owner, limited to the repositories that the workflow should publish to. Use `TEST_PUBLISH_TOKEN` for personal test PRs and `OPENTELEMETRY_PUBLISH_TOKEN` for OpenTelemetry PRs. Store these as Actions secrets in the repository's `protected` environment, not as plaintext variables. Other workflows using these names must also reference `protected`.

Set the repository's shared Actions variable `PUBLISHER_SECRETS` to:

```json
{
  "trask": "TEST_PUBLISH_TOKEN",
  "open-telemetry": "OPENTELEMETRY_PUBLISH_TOKEN"
}
```

The mapping supports any GitHub owner, not just these examples. Keys match case-insensitively and must be unique; values must be uppercase Actions secret names that start with a letter, contain only letters, digits and underscores, and end in `_PUBLISH_TOKEN`. The variable contains names only, never token values. An explicit `{}` disables all publisher routing. Without the variable, only effects in repositories owned by the central owner use `TEST_PUBLISH_TOKEN`.

GitHub does not expose saved secret values for renaming. When migrating secret names, add the token values under the names in this mapping through the Actions secrets UI. Existing secrets under other names are not copied or used as fallbacks.

Source selection uses the actual PR **head repository owner**, so a personal fork of an organization repository uses the personal token. Description and pending reviews instead select the base owner. A single fine-grained PAT cannot grant permissions across resource owners; if the selected token cannot read/review the base PR as well as push the head, publication stops. There is no second-token fallback.

The launch checks the selected secret's presence before admitting a worker and rechecks the effect repository after freezing the PR. Continuations and explicit reconciliation select from the frozen effect-repository identity and the current mapping. The live job verifies the selection again before authenticating. A mapping change between selection and publication blocks instead of using a mismatched token. Only the selected token is injected into the publisher step; the coordinator sees names and a presence boolean, and workers, verifiers and the waiter receive no publisher tokens.

Source-changing publisher tokens need Contents read/write and external-review tokens also need Pull requests read/write. Report-only tokens need base Contents read and Pull requests write, not head push access. CI repair needs base Actions read/write for evidence and reruns. Include Workflows write when fixes can change workflow files. Organization policies may require approval. Select only intended public target/head repositories, excluding the central repository. The personal test token must stay scoped to the test repository; adding a mapping does not broaden it or provision any token.

External-review publication freezes an existing submitted verified Copilot review at the current PR head and starts the worker without an initial review request, Git push dry run or Copilot permission probe. Missing or outdated reviews reject the launch. Copilot review-request permission is assumed; if the post-publication request fails, the loop stops with an explicit error and retains the confirmed publication. Uncertain requests are reconciled, never blindly reissued. Self-review neither reads nor requests Copilot reviews and needs no review-request permission.

## Explicit operations

Dispatch `coordinator.yml` from central `main`. Only the existing personal owner can launch work. The target must be open and authored by that user except for PR Reviewer.

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f publication_auth=fine_grained_pat -f target=owner/repository#123
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=status -f target=owner/repository#123
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f loop_kind=self_review \
  -f publication_auth=fine_grained_pat -f target=owner/repository#123
```

Launch authorizes real publication, not a dry run. The worker remains artifact-only, and the trusted publisher independently accepts its output before pushing or handling threads. Offline tests cover malformed artifacts and effects without consuming inference or changing targets.

Use Run again after a terminal phase has stopped. Ordinary fresh launches observe the prior checkpoint without requiring copied `previous_request` or `previous_generation` inputs. They verify prior executions are quiescent and reject unknown or in-flight side effects. A new phase archives prior evidence and may select any kind, even at the same head. Review and CI loops allow five workers; single-pass tasks allow one. All use the two-hour ceiling. It cannot reuse an old candidate or replace an active phase. A missing-auth gate can likewise be followed by a fresh authorized phase. An unfrozen read-access gate stays separate; restored access freezes a new numeric-repository checkpoint.

`operation=cancel` requires the exact observed `previous_request` and `previous_generation`. It changes generation inside the state CAS transaction, preserves evidence including thread-effect intents, and prevents later finalization/new effects. It does not kill unrelated runners or undo an already authorized in-flight mutation.

There is one fresh launch operation. Old mode, replacement, and start-publication inputs are not supported. Historical requests, receipts, reports and source fixtures remain read-only and cannot be resumed or reinterpreted.

### Automation revision pins

Each new phase saves `workflow_revision` and `workflow_ref`, a central branch named `review-loop-revisions/<workflow_revision>`. The launch uses its Actions run's exact central-main commit, even if `main` advances while that run is starting. A branch is created once per revision and reused only when its exact ref, commit-object type and SHA match. The automation never updates or force-pushes these branches.

The shared waiter remains on current `main` and routes each phase to its saved ref. Worker dispatch and targeted coordinator ticks run the frozen workflow definition and code; verifier, finalizer and publisher jobs check out the bound revision. Later review/fix passes retain the same phase pin. New pushes to `main` do not block pinned phases, and no phase silently adopts newer automation. Ref drift, cancellation, target drift, deadlines and provenance failures still stop work.

Keep revision branches while their phases or retained evidence may need them. Deleting or moving one blocks its affected work without a `main` fallback. Cancel and relaunch an active phase to pick up an automation fix. Existing phases without `workflow_ref` keep their original central-main revision guard; this does not rewrite historical checkpoints or restart phases already blocked by an update.

## Source and candidate boundary

The freeze binds base repository name/ID, actual head repository name/ID/ref, source/target visibility, author/launch actor, exact SHA, trusted workflow revision, request digest, baseline review IDs and verified findings. Forks use the head repository for Git and the base repository for PR/review APIs. Credentialed operations recheck open/author/head/source identity immediately before acting.

New freezes bind `commit_author`, the launch owner's verified GitHub numeric ID and login. For source-changing workflows this is also the PR author. PR Reviewer separately freezes the PR author's numeric identity, which may belong to the launch owner, another person or a bot, and never constructs a code commit. Deterministic candidate commits use that login as author and committer and `<id>+<login>@users.noreply.github.com` as both emails. Both dates use the freeze timestamp. The publisher verifies its authenticated account matches the frozen author before pushing, and the existing Copilot co-author trailer is retained.

Requests without this frozen author cannot publish or have their candidates reconstructed under the current runtime. Historical evidence stays unchanged. After the prior phase is verified quiescent, start a fresh phase to freeze an attributed candidate; do not reuse or rewrite a saved candidate acceptance.

Self-review freezes base ref/tip and the merge-base SHA instead of review IDs/findings. The tip is read from the live base branch ref, not the PR's base SHA metadata, which can lag behind that branch. Head and base identity are rechecked before accepting results, publication, refreezing and recording clean. A changed base stops self-review without merging or rebasing. This stricter base policy does not apply to the external-review loop.

Git retrieval is unauthenticated and limited to the frozen public base/head repositories, with no hooks, helpers or target scripts. External review retrieves the frozen public head directly. Every self-review pass packages complete head and merge-base trees as two shallow snapshots, or one when their SHAs coincide, with exact source/run/attempt/workflow/artifact bindings. The bounded Git bundle, not a paginated or truncated API file list, defines the full review input. Combined expanded self-review trees and Git objects are each limited to 16 MiB, with 4 MiB per object. Missing, oversized or unsupported source fails rather than narrowing review. Workers and verifiers receive the bundle, never a source credential. Base permissions do not imply fork push access.

Public-only execution does not sanitize retained Git history, checkpoint branches, logs or artifacts. Before changing the central repository's visibility, inspect that historical data and stop any private phases still running old pinned code. Pinned phases retain their original publisher checks; use a fresh phase for the current runtime. No visibility change or historical cleanup is performed by the public-target policy.

The verifier checks the bounded three-file archive, exact semantic results, API-issued run/attempt/artifact identities, retention/digests and patch paths/modes. It applies ordered patch spans and constructs one deterministic single-parent commit per root-cause batch in fresh bare Git without checking out or executing target code. It checks every intermediate change and the final cumulative diff. The package binds the source prerequisite, every commit/tree/parent, subjects, changed paths, finding mappings and patch/bundle hashes. Extra archive members, including `validation.json`, reject.

Repository configuration and instruction files, binary files, executable files, symlinks and submodule pointers may be read and changed. UTF-8 filenames have no ASCII-only or 240-character policy limit; Git parses quoted paths and publisher comparisons use NUL-delimited names. Paths must remain relative without traversal, NUL or `.git` metadata components. Trusted jobs use Git 2.43 or newer to read attributes from bound trees and reconstruct Git objects without checking out target code, following symlinks or retrieving submodule contents. Target instructions cannot alter the central protocol, and actual secrets must never enter candidate artifacts. Central trusted runtime files retain separate protections. GitHub enforces actual workflow-file publication permissions.

## Worker repository checks

The worker chooses relevant existing repository checks and runs them inside its AWF sandbox. It records actual commands, exit codes and failures in `diagnostics.txt`, not a structured validation plan. Missing tools, blocked dependencies and failed checks must be reported honestly.

Before returning artifacts, the worker runs `python3 -m loop.worker_output` in the central workspace inside AWF. This offline schema and patch-span check lets it correct malformed output before upload. It does not execute target code, establish provenance or replace trusted verification.

Trusted jobs never execute target code or rerun worker commands. The finalizer accepts only structural verifier results bound to the frozen request/run and unchanged live target. Results enter `publish_pending` for fresh trusted acceptance; this transition alone cannot authorize a push.

Workers use hosted Ubuntu 24.04 and available AWF/toolchain mounts, with a fresh writable `/tmp/review-loop-worker-home` and empty JVM/cache directories. The explicit firewall covers GitHub and the listed Maven/Gradle, PyPI, npm, NuGet, crates.io and Go proxy endpoints. There is no unrestricted environment/network mode or credential fallback.

The publisher requires the bound coordinator's successful `verify` and `finalize` jobs. It downloads fresh server-bound worker/verifier artifacts, reconstructs the entire chain again, imports its thin bundle and checks every parent/tree plus complete linear history. Acceptance binds source, generation, worker run/attempt and workflow revision, then records a digest of the structural report. The ordinary non-force push publishes the accepted chain once.

Worker diagnostics are untrusted feedback. Structural acceptance proves the candidate's patch/Git identity and provenance, not passing tests or coverage. Required target CI remains the independent exact-head clean-completion gate. No-change runs never push an empty packaging commit.

## Self-review passes

Each fresh worker reviews the complete frozen merge-base-to-head PR diff, relevant surrounding code and repository instructions, then fixes only concrete warranted problems introduced or directly affected by the PR. There is no separate discovery, fixer or evaluator agent.

Both loops return exactly `candidate.patch`, `result.json` and `diagnostics.txt`. Self-review's exact semantic result is small:

```json
{"schema":2,"request_digest":"<frozen digest>","outcome":"clean","batches":[]}
```

`outcome` is `fixes`, `clean` or `blocked`. Fixes require nonempty ordered code batches and cannot also claim clean. Clean requires an explicit completed review, empty batches and an empty patch. Missing, malformed, blocked or contradictory output stops without publishing the candidate. Each code batch includes `summary`, `analysis`, `upsides`, `downsides`, `offset`, `length` and `sha256`; no external finding IDs, inventory or dispositions are allowed.

An accepted changed pass publishes its exact structurally verified candidate, refreezes the new head and starts a fresh full-PR worker with the same phase, consumed count, deadline and publication history. It does not wait for Copilot or attempt CI repair between passes.

A clean no-change pass still runs worker-side repository checks and undergoes structural acceptance. It then observes exact-head target CI. Pending CI waits in the shared waiter; missing, failed, absent or unknown CI stops. Clean additionally requires unchanged head/base. Fifth-pass fixes retain their publication but end exhausted because no later clean pass fits the budget. A fifth no-change clean pass can finish.

## PR Description inputs

PR Description reads frozen title/body and the GitHub PR diff without fetching a separate changed-file list or packaging source. Binary-file markers are supported input, not candidate patches. It does not require diff anchors, matching per-file line counts, a frozen base tip or reconstruction of the PR's Git tree. API response and checkpoint storage bounds still apply. The verifier requires an empty source patch, binds the proposal to the request and writes an empty candidate bundle with no source tree. Before PATCH, the publisher independently revalidates artifact provenance, exact PR head, unchanged GitHub diff and original title/body. Other task source and publication checks remain unchanged.

## Fresh review and CI

Only submitted reviews from the verified Copilot numeric/node/type identity participate. Full bodies and unresolved original bot roots are frozen; human roots and bot replies in human threads are excluded. Fresh review requires exact expected SHA, absence from the durable baseline, submission strictly after it, complete paginated bodies/comments/threads and a two-minute propagation delay.

Unknown bodies cannot establish clean. The bounded CCR v2 zero-finding grammar accepts `Approval recommended` and `Needs a closer look` headings with their standard status icons, `Review effort: Balanced` and `Findings: None`. A clean workflow result means no remaining findings and passing exact-head CI, not human approval or readiness to merge.

A recognized clean overview may include a counted `Resolved since last review` section containing only links to original verified bot roots. Those roots must independently be resolved or outdated in live thread data. Missing roots, extra markup, duplicate links and inconsistent counts cannot establish clean.

Every fresh body participates, so a later clean summary cannot erase an earlier finding. Open or hidden findings outside the resolved section and unresolved live threads still prevent clean. No inline comments, no edits, blocked checks or a `Findings: None` substring is not proof of clean.

Publication selects existing non-Copilot CI check names/status contexts from the target's frozen head. Exact-SHA paginated checks/statuses retain `none`, `missing`, `pending`, `failed` and `unknown`. Duplicate status contexts and unrecognized/neutral/skipped results do not pass. Copilot is not CI, and no CI means no clean result.

Separate GitHub Actions workflows can use the same check name, such as `test`. The watcher verifies each check's Actions app, exact head, check suite, first-attempt workflow run and job/check linkage before treating them as independent. Every matching check must pass. Same-workflow duplicates, reruns, unbound links and mixed check/status contexts remain unknown. These extra provenance reads are limited to 100 executions per CI collection and require actual API access to the public target.

External-review continuations may read only the exact public Copilot user-identity endpoint outside their frozen target/head repositories. They verify the returned bot ID and node ID before freezing another worker. Other user endpoints and mutations remain forbidden.

Fresh external findings with passing exact-head target CI can freeze another worker within the same phase. Retained old roots do not block genuinely new inline findings or new body-only findings in a complete counted CCR v2 `Previously missed` section. The next worker still receives every eligible unresolved root. Repeated finding collections, or entirely reused roots without new body-only findings, stop another pipeline; rewording an overview alone does not count as new work. These rules never clear unresolved threads or establish clean.

Both kinds allow five total model pipelines including the initial and failed admitted work, with a two-hour deadline. Active phases never replenish those limits. Frozen historical budgets stay unchanged; only a new authorized, quiescent phase gets fresh budgets. No sixth worker exists.

Landing PRs, changing draft state, force pushes, top-level comments and human-rooted thread replies are not allowed. Only `pr_description` can update title/body; only `pr_conflict_resolver` can publish a base merge. Explicit Copilot-review launch authorizes warranted commits, original-bot replies, resolution, and then fresh review. Self-review authorizes commits only.

## Batches, messages, and mandatory thread effects

The only executable protocol is `reviewable-v1`, frozen by the trusted runtime. All worker results use schema 2. `candidate.patch` concatenates ordered batch patches against consecutive trees, including encoded Git binary patches and mode changes; `result.json` describes complete contiguous byte spans with SHA-256 hashes. The limit is 100 batches, 2 MiB total patch bytes, 100 changed files and 10,000 added/deleted text lines across batches. Binary changes count against patch/object size limits. Every intermediate change must satisfy path-safety, object bounds and trusted-runtime protections.

Both kinds supply single-line summaries at most 120 UTF-8 bytes and analysis, upsides and downsides at most 2,000 UTF-8 bytes each. Empty batches, omitted bytes, duplicate/foreign findings, incomplete mappings and oversized reviewer evidence reject rather than truncate.

External results additionally include every frozen finding exactly once, with `key`, `disposition`, `analysis`, `upsides` and `downsides`. Dispositions are `fixed`, `not_warranted` or `blocked`. Every fixed finding belongs to exactly one batch's `findings` array. Supported no-code findings belong to no code batch. Any blocked finding prevents publication of partial work.

Copilot-review commits use `Address Copilot review comment: <summary>`, or `comments` for multiple associated findings. Each original is inserted verbatim under its own `Copilot comment:` block, then Analysis/Upsides/Downsides and the co-author trailer follow. Self-review uses an issue-specific subject and the same tradeoffs, without fabricated Copilot comments. See the [message example](../README.md#reviewable-commits-and-thread-handling).

Only the trusted publisher posts and resolves. After confirming both branch ref and PR head, it replies to accepted original verified Copilot roots. Fixes name the actual mapped batch commit, not merely the iteration tip. Supported rejections or already-present fixes use `No code change.` with concrete analysis and tradeoffs. No-change never creates or pushes an empty commit. Body-only findings have no reply or resolution.

Complete bounded conversations are frozen and rechecked before effects. Human-rooted threads are excluded, even if Copilot replies there. Human intervention, changed root text/identity, or a new objection prevents automatic handling of that thread and records the reason. Already resolved threads and outdated no-code threads are skipped without claiming responsibility. A published fix can make its frozen original thread outdated; that thread still receives the mapped-commit reply and resolution.

The publisher persists a reply intent before posting to `repos/{repo}/pulls/{pr}/comments/{original_root_id}/replies`. It confirms the exact actor/root/body/marker match before persisting a resolution intent and issuing only the bound `resolveReviewThread` mutation. Resolution requires a matching response acknowledgment and live resolved state. Broad GraphQL mutation access, source-read mutations, and worker/waiter publication remain forbidden.

Before each effect, the publisher rechecks full state CAS, owner execution, request/generation, deadlines, authenticated actor, exact head/ref and current root/conversation. GitHub cannot atomically bind a thread mutation to a PR-head comparison; the publisher rechecks immediately before effects and confirms afterward. A fresh review and exact-head CI remain mandatory clean gates.

## Shared Actions waiting

`waiter.yml` maintains one synchronous waiter for every active authorized PR, regardless of loop kind. Only one fixing loop can be active per PR, but different PRs can use different kinds concurrently. Launch each PR normally; additional launches join the same polling loop. Workers, structural verification and publication can run independently for different PRs. No per-PR job sleeps waiting for review or CI.

The waiter reloads checkpoints once a minute and checks each unchanged due PR at most every five minutes. New reviews must satisfy the exact-head identity and two-minute propagation requirements before work is dispatched. Full review/CI validation still happens in the per-PR coordinator, not in the readiness probe.

Coordinator/worker activity starts or queues the waiter. Global waiter concurrency allows one running waiter and a queued successor; queued runs do not occupy runners. After 55 minutes it dispatches a successor before the 65-minute job timeout. It exits when all phases are terminal. Cron remains only a best-effort recovery trigger. A runner cancellation or failed handoff can require a fresh manual `gh workflow run waiter.yml --repo trask/copilot-workflows --ref main`.

The waiter has central state/Actions access and optional target source-read access, never inference or publisher credentials. Durable coordinator run claims prevent duplicate verification/publication work and make it wait for the producing workflow to finish before accepting its artifacts. A blocked PR does not stop other authorized PRs.

## Checkpoints and uncertain operations

Version-2 checkpoints use `pr-v2-<actual-base-repository-ID>-<PR>.json`. An unfrozen access gate uses the first 32 hex characters of the case-folded repository name's SHA-256 instead of inventing an ID. The JSON-only branch checks projected limits before writes: 1,000 files, 1 MiB per file, 16 MiB total. Large logs/bundles stay in bound Actions artifacts.

Writes create a child of the observed state ref and update it with `force:false`. Competing children cannot both fast-forward. Conflicts reload/retry at most twenty times with bounded randomized backoff; replacement archives exact prior state. Finalization/mutations require the same complete request/generation/state in CAS checks. The waiter serializes globally; short coordinator jobs and workers serialize per request/PR. Durable run ownership permits different PRs' verification pipelines to overlap.

REST GET timeouts and HTTP 500/502/503/504 responses allow up to three attempts, with one- and two-second backoffs. Retries log only the failure category and attempt, respect the existing execution deadline and raise the final error if reads do not recover. Permission, validation and rate-limit errors are not retried. POST, PATCH, PUT and DELETE calls, including GraphQL POSTs, are never retried by the API client; uncertain effects still require reconciliation.

Initial requests record `launch_run` with actual dispatch run ID, attempt and actor. HTTP 204 acknowledges dispatch, not an arbitrary latest checkpoint. Clients must bind a queued acknowledgment to the observed launch run and resulting request, or leave it unconfirmed. Repeating POST to manufacture confirmation is forbidden.

Dispatch, publication, reply, resolution and review-request intents persist before side effects. Reconciliation checks exact Actions/PR/ref/review identities without retrying effects. Publication waits for agreeing head-ref/PR candidate SHA. An unchanged original head after uncertain push blocks for inspection; another head is stale. Missing/duplicate dispatch identities and unknown requests block with evidence retained.

Lost reply responses are reconcile-only. A unique exact reply body, actor, root and bound marker can confirm the effect without another POST. A resolution response lost before its acknowledgment is saved cannot be uniquely attributed by resolved state alone; it stops as uncertain even if the thread is resolved. An acknowledged resolution may confirm through live reads. No thread mutation is blindly retried.

Unknown effects retain their intent and stop with `thread_reply_uncertain_no_retry` or `thread_resolution_uncertain_no_retry`. Cancellation and deadlines preserve intents. A new phase cannot clear them; inspect and reconcile the evidence before further work.

`operation=reconcile-publication` requires a fresh owner dispatch, explicit fine-grained authorization and exact stopped request/generation. It confirms only the already-authorized unexpired current-protocol candidate, saved acceptance/artifacts and agreeing live tip/tree identity. It archives the stopped checkpoint and records separate execution provenance. It neither reaccepts under another protocol nor pushes again. Historical candidates are never resumed.

### Restarting after an unpublished push

After correcting publisher access, start new work instead of rerunning the failed Actions job:

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f publication_auth=fine_grained_pat \
  -f target=owner/repository#123 -f restart_unpublished=true \
  -f previous_request=<observed-request-id> -f previous_generation=<observed-generation>
```

The owner must explicitly select `restart_unpublished` and provide the exact stopped checkpoint identity. The old phase must be blocked on an uncertain push or publication propagation timeout. Its publisher, verification pipeline, worker, coordinator, launch and source-acquisition runs must have stopped, with no reruns or duplicate worker identities. Wait fifteen minutes after the original publisher's last update so ref propagation can settle.

Both the actual head ref and PR head must equal the original frozen head, and the freshly frozen target identity must still match. An already-published candidate, unrelated head change, pending review-request effect or retired acceptance blocks this restart. An expired old phase is eligible because the owner authorizes a separate new phase, not a continuation.

The same non-force state transaction archives the old checkpoint unchanged as `request-<id>.json` and creates a new request with restart provenance, five pipelines and two hours. The old publication intent keeps its original status and acceptance. No candidate, result, publication or pending effect carries into the new phase. Corrected permissions do not automatically unblock old runs, and ordinary ticks cannot authorize this reset.

Earlier checkpoints/artifacts remain readable as historical evidence but cannot execute. There is no old/new execution compatibility, protocol switch or in-place migration. A fresh launch may archive a terminal old checkpoint after verifying its exact bound executions have stopped and all effect intents are settled. Old capability or unknown effect evidence blocks fresh admission. The unchanged [pilot record](legacy-pilot.md) documents historical restrictions/results; none of its commands authorize continuation.

## Dashboard troubleshooting

The project canvas loads automatically in a Copilot app session opened in this repository. After editing its extension, reload extensions before opening PR workflows, which retains the `workflow-dashboard` canvas ID. Use the extension inspector's log if it fails to register.

Authenticate `gh` to `github.com` with target PR read access, shared-workflows dashboard Contents read access, and central repository Contents/Actions read access. Run and Cancel additionally need central Actions write access. The canvas uses fixed REST read endpoints and read-only GraphQL queries. Writes are central `coordinator.yml` dispatches or cancellation of an exact recorded central launch run. It never extracts tokens, receives Actions secrets or publishes directly to a target. Account/PR eligibility and existing checkpoints are checked again before dispatch; the coordinator retains final authorization.

The dropdown's default is Java instrumentation. The other choices are semantic-conventions-conformance and shared-workflows; edit `repositories.mjs` to extend the list. My PRs is selected by default and matches the authenticated viewer's author login. Switch to Not my PRs for everyone else's PRs, including bots. The two views are mutually exclusive and both include drafts and PRs without dashboard data. My PRs shows all eight tasks; Not my PRs shows only Draft review, plus Cancel when authorized. Waiting on reviewers selects current, nondraft dashboard records with `route: "approver"` from `<repository-name>/dashboard-state.json` on shared-workflows' `otelbot/pull-request-dashboard-state/<repository-name>` branch. It does not infer readiness from pending review requests. The reader supports dashboard state version 18; unsupported versions, missing records and mismatched heads stay unknown or stale.

Repository switches immediately show a loading indicator and replace the previous repository's cards with a loading message. Repository, refresh and automatic-refresh controls stay disabled until the request completes or fails. The PR region exposes its busy state to assistive technology. Older background responses cannot replace a completed repository switch; failures remain visible and unlock the controls for a manual retry.

Refreshing the current repository keeps the previous PR list, button statuses and workflow results visible while reads are in progress. The replacement snapshot appears only after the live action evidence and workflow reads have settled; partially loaded data does not reset buttons to unknown.

A failed reviewer-dashboard read leaves the open PR list usable and shows a warning. A failed live-PR read retains the last successful list as stale. A failed or malformed central-state read disables task controls rather than claiming there is no active workflow. Missing dashboard facts alone do not prevent task dispatch. PR cards report workflow status through their task buttons and a short completion result. Draft review distinguishes No findings from Review ready, with the new comment count and pending-review link. Existing comments are separate from generated findings. Results from a previous PR commit are marked explicitly. Expand Run details for head, timing, logs, archived runs and review comments; Downloads contains artifacts. Output verification checks saved outputs, not review quality or passing tests.

Yellow/amber buttons indicate detected work from live, head-matched GitHub data. PR Conflict Resolver reads `PullRequestMergeConflictStateCondition`: `FAILED` means file conflicts, `PASSED` disables unnecessary conflict resolution, and other or missing results remain unknown. Aggregate mergeability and unrelated failed merge conditions do not determine this button. CI Fix Loop is highlighted for failing current-head checks and disabled only when a complete check collection confirms passing CI with no pending checks. Copilot checks are excluded from CI repair status.

Buttons show only their task name. Needed work uses an amber tint and border; confirmed-unnecessary fixes are disabled and dimmed. Active tasks use blue styling, with a spinner beside the name while busy. Reduced-motion mode uses a static hourglass instead. Explanations remain in hover tooltips and accessible labels, without permanent bottom status labels or circles.

Address Copilot feedback is highlighted for unresolved, non-outdated threads whose root comment is from Copilot in a submitted review, not for human-rooted conversations with bot replies. No open threads is not a clean-review guarantee. Review and fix, Update title & description, Simplify code, Draft review and Align with existing code remain manual actions unless a saved run supplies their status. Active tasks and dispatch locks keep their existing priority; a completed task does not conceal newly detected work.

Missing, failed, incomplete or head-mismatched live action evidence shows unknown status and a warning, without falling back to saved dashboard facts or claiming a fix is unnecessary. The PR list and eligible manual actions remain usable. Pending CI and absent checks are not confirmed passing. Conflict and CI launches re-read live evidence before dispatch, so an old enabled button cannot launch a task that is now confirmed unnecessary.

Before the first task, the central `review-loop-state` branch does not exist. A successful matching-ref listing without that exact branch means empty saved workflow history; it neither creates a branch nor restores old checkpoints. Authentication failures, HTTP errors and malformed or incomplete listings remain errors, not empty history.

Run dispatches `operation=launch` with `publication_auth=fine_grained_pat` on central `main` immediately on click, without a confirmation dialog. Task effects remain in button tooltips. No worker or waiter is dispatched directly. Cancel likewise dispatches the exact observed request/generation immediately; a changed checkpoint rejects cancellation rather than adopting newer work. Clicking either button supplies the API's explicit `confirmed: true` intent; agent actions still require an explicit user request. Pending, accepted and uncertain dispatches disable further submissions until matching checkpoint evidence is observed. Failed launches reads one page of up to 20 failed dispatches across all repositories, including launches without a task checkpoint. Failed launches and Background jobs stay collapsed until expanded. Inspect the linked logs and use the CLI recovery commands rather than repeatedly clicking Run. Launch titles identify the task and target; older unmatched runs require inspecting their logs. A failed Actions run does not replace saved task evidence or unlock an unconfirmed dispatch. No dispatch is automatically retried.

Accepted launches expose Cancel launch using the exact run receipt from API version `2026-03-10`. Confirmation must match that launch ID, task kind and owner. Cancellation rechecks current checkpoints first and uses the normal request/generation protocol if that launch has saved a task. Otherwise it verifies the recorded run's owner, workflow, target, branch and first attempt before cancelling it. A task saved during cancellation is also cancelled by its exact identity. Acknowledgement alone does not mean cancellation completed, and cancellation does not undo published changes or guarantee termination of an already authorized effect. While visible, pending dispatch cancellation is reconciled once a minute even with Auto refresh off; read errors, low capacity and rate limits stop this polling. Dispatch receipts are held for the provider's lifetime. A legacy dispatch without a receipt stays locked until checkpoint confirmation and cannot cancel a guessed run.

An exact launch that finishes without a confirmed task stops showing a busy icon and displays its conclusion and Actions link. It remains locked for inspection rather than assuming there are no workers or published effects.

Refresh every 60 seconds uses cached authenticated ETags for the viewer, selected repository's open-PR pages, its dashboard file, the central state matching-ref listing, five active Actions status listings and one recent coordinator failure page. A 304 response consumes no primary GitHub capacity. Changed state requires the new commit/tree and changed current blobs. The first history expansion reads retained archives, then caches them by Git SHA.

Independent refresh reads run concurrently, with at most three GitHub CLI reads in flight. Reads of the same endpoint share only their in-flight response; subsequent refreshes still revalidate its ETag. Pagination and pinned Git object dependencies remain ordered. A refresh waits for every started read to settle, including after a failure, before allowing another refresh. Out-of-order rate headers retain the lowest remaining capacity within a reset window, and concurrent rate-limit responses retain the longest backoff. Queued reads do not start CLI requests during that backoff.

Live status uses read-only GraphQL queries in batches of ten of your PRs, sharing the same three read slots. Check contexts and review threads are paginated before interpreting complete results, and every page must match the listed head. GraphQL reads are not ETag-cached and do not retry POSTs. REST and GraphQL capacity are tracked separately. These reads count toward the existing refresh-cost threshold, so a repository with many of your PRs or large thread histories may switch to manual refresh.

Connection resets, unexpected EOFs and network timeouts before an HTTP response receive one GET retry after 250 ms, within the same three-read limit. A rate-limit cooldown prevents that retry from starting. Authentication, certificate, CLI process/output failures and received HTTP errors are not retried. Refresh state records network retry attempts; the canvas shows a sanitized failure reason if recovery fails, without exposing CLI stderr. Launch and Cancel dispatches never retry, including after a connection reset.

Steady-state refreshes taking more than 10 seconds or using more than 12 counted reads disable automatic refresh. Counted reads include uncached REST responses and live GraphQL queries. Less than 10 percent remaining REST or GraphQL capacity, authentication/read errors, and rate-limit responses also pause it. The canvas displays read errors and automatic pause reasons; choosing manual refresh does not add a notice. Load timestamps, refresh cost and rate-limit metadata remain in refresh state, not in the header. Retry-After and reset headers prevent premature reads. Refresh manually after resolving the problem, and explicitly enable automatic refresh to resume polling. Hidden or closed canvases do not keep polling, and panels within the same extension provider share a refresh.

Requests stop at 10,000 open PRs or 16 MiB of listing data, 1 MiB per reviewer dashboard file, 1,000 entries per Actions status, 1,000 central state files, 1 MiB per central file and 16 MiB total central state. Pagination follows trusted next-page links; Actions `total_count` is advisory because active runs can start or finish during a read. Unfinished page chains or malformed data produce an explicit error or per-checkpoint warning, not a partial success claim. Mutating loopback requests require the canvas's exact origin and a validated JSON body of at most 4 KiB. Last successful data remains visible and marked stale when a refresh fails.

## Local development

Use `python tools/test_docker.py [unittest-selector]` for fast Linux Git/Python tests on Windows, then the full suite before publication. The tools-only container runs in non-root tmpfs without network, host mounts or secrets. Workflow changes also require the real pinned compiler, linter, zero-write audit and reproducible generated bytes.

There is no standalone qualification workflow, Gradle smoke dependency or automatic E2E target. A live test needs an explicit PR and user authorization. Unit/static success is not live credential or target-runtime proof.
