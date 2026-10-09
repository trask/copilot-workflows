# Setup and recovery

## Workflow kinds and permissions

All workflow kinds require a public PR repository and a public head repository, including forks. Private, internal or unknown visibility is rejected before review/diff collection or source acquisition. `trask/copilot-workflows` cannot be a PR or head repository, regardless of visibility or capitalization. Frozen private or central-target requests are read-only under the current runtime and cannot dispatch, verify, publish or route to older pinned workflows. A live visibility change stops source preparation, verification and publication.

Choose one independently launched `loop_kind`: `copilot_review`, `self_review`, `pr_conflict_resolver`, `ci_fix`, `pr_description`, `pr_simplify`, `pr_review` or `pr_consistency`. Only `ci_fix` waits for target CI or gates completion on its result. See the [behavior table](../README.md#independent-tasks).

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f loop_kind=pr_review \
  -f publication_auth=fine_grained_pat -f target=owner/repository#123
```

Only the central launch owner can dispatch. PR Reviewer accepts the owner's or another author's open PR, including bot-authored PRs, and cannot gain source publication permission. All other kinds require the owner's open PR, including Copilot PRs GitHub attributes to that owner. A launch authorizes only the selected task; none of the six new kinds posts top-level comments, replies to people, resolves threads, changes draft state, submits/approves reviews or lands a PR.

| Task effect | Selected secret owner | Required target access |
| --- | --- | --- |
| Fix commits or base merge | Head repository for push, base repository for upstream API | Head Contents write, base PR read; Workflows write for workflow edits |
| Description title/body PATCH | Base repository | Base Contents read and Pull requests write; no fork push permission |
| Viewer-owned pending review | Base repository | Base Contents read and Pull requests write; no fork push permission |
| CI repair code push | Head repository for push, base repository for upstream API | Head Contents write, base PR/Checks/Statuses/Actions read |
| Evidence-based failed-jobs rerun | Base repository | Base PR/Checks/Statuses/Actions read and Actions write; no head push permission |

Each selected fine-grained PAT must authenticate as the launch owner and have the permissions required for its repository. Source pushes and head API reads use the head-owner secret; upstream API reads and PR operations use the base-owner secret. The publisher checks both actors, frozen repository identities and bound effects; it never probes central readability or claims to attest either PAT's complete repository scope. Its API client permits only target/head reads and authorized target mutations. No task retries with another token after a denial. CI launch authorizes possible source repairs with the head-owner secret. An accepted rerun proposal explicitly routes that effect to the base-owner secret. A missing base-owner mapping or denied Actions write stops the rerun. Git source acquisition is always unauthenticated, never using the publisher, inference or optional API read token.

New diff-based tasks require full head/merge-base trees and the complete authoritative GitHub PR diff. Source and diffs have no byte-size, Git-object-count, file-count or added-line caps. GitHub binary markers identify changed files; their bytes come from the bound complete Git snapshots, not invented text-line anchors. Truncation or missing source stops rather than narrowing scope. Conflict source acquisition deepens until both frozen tips reach the merge base, without a commit-count limit. Its publisher preserves exact frozen head/base parents, including merges whose tree equals the original head, and every cleanly merged incoming change. Merge patches preserve whitespace byte for byte. Conflict resolution can add new files, such as changelog fragments carrying incoming release notes. Existing nonconflicting paths, including deletions, must exactly match Git's automatic merge tree; a conflict-free merge must match the entire automatic tree.

CI repair waits for active checks before diagnosis. Trusted preflight binds run IDs, current attempts and job/check identities. It collects complete failed-step log windows, or the whole job log when step timestamps are unavailable, without an evidence byte budget. Non-Actions output participates when available. The worker selects relevant quotations; missing evidence and unknown causes cannot clear the phase. Every failed check needs an evidenced diagnosis. Unrelated/pre-existing failures remain explicit warnings with failed CI, never green clearance.

The one failed-jobs rerun allowance is per Actions run. Any existing manual or automated retry consumes it. The publisher records an intent, rechecks head/base/run/attempts, sends one POST, and confirms exactly the next attempt. Lost responses reconcile without another POST. Code pushes and confirmed retries do not establish passing CI. CI repair retains the five-worker ceiling, without an overall deadline.

Simplify and consistency use ordinary commits without another review pass or Copilot request. No qualifying change means `no_change`, no commit and no build/test prerequisite. Optional check failures or missing SDKs do not block a completed no-change investigation and remain recorded in diagnostics. Incomplete investigation and checks needed to assess candidate changes still block. Consistency reports retain classifications, explanations and frozen source citations. Single-pass source tasks finish after their accepted work is confirmed, without collecting or waiting for target CI.

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
gh secret set TRASK_PUBLISH_TOKEN --repo trask/copilot-workflows --env protected
gh secret set OPENTELEMETRY_PUBLISH_TOKEN --repo trask/copilot-workflows --env protected
```

Do not keep repository-level copies of these secrets: jobs without the environment can access repository secrets. GitHub cannot retrieve existing secret values; adding or moving a secret requires its token value.

Coordinator PR/review reads use the central Actions token because GraphQL requires authentication. An optional `REVIEW_LOOP_SOURCE_READ_TOKEN` can supply scoped public API reads, but cannot authorize a private target or head and is never passed to Git source retrieval. The trusted publisher uses owner-scoped PATs for target/head API reads. Missing coordinator API access stops as `human_gate_target_repository_read_access`. The controller never reads local CLI credentials or borrows inference auth.

If used, store `REVIEW_LOOP_SOURCE_READ_TOKEN` in `protected` too. Keep the non-secret `PUBLISHER_SECRETS` routing map as a repository variable.

Workers also need `COPILOT_GITHUB_TOKEN`, a personal inference credential with Copilot Requests read access and no repository write access. The pinned engine proxies inference and excludes it from the agent environment. Do not add inherited MCP, OTLP or broad repository credentials to the worker.

Launch uses explicit `publication_auth=fine_grained_pat`. The two review loops publish warranted fixes. Copilot review always replies to and resolves eligible original Copilot threads; self-review never does. There are no preview/shadow modes or optional thread switches. Both owner-scoped credentials must authenticate as the launch owner, with base access to the actual target PR and head access to push the actual repository/ref. Keep them separate from the central Actions token. Missing or disabled auth records `human_gate_target_repository_push_and_review_access`; the publisher checks identity and actual target/head access before effects.

### Owner-scoped publisher secrets

Create one fine-grained PAT per resource owner. Use `TRASK_PUBLISH_TOKEN` for personal repositories, including forks, and `OPENTELEMETRY_PUBLISH_TOKEN` for OpenTelemetry repositories. Store these as Actions secrets in the repository's `protected` environment, not as plaintext variables. Other workflows using these names must also reference `protected`.

The personal token can select individual repositories or all repositories owned by `trask`. All-repository access covers current and future personal forks, but also grants access to private repositories such as `trask/copilot-workflows`. Private and central PR targets remain rejected. Only the trusted publisher receives this token; workers never receive it. Repository selection limits the credential itself, while the publisher's bound-effect checks limit what the workflow may do with it.

Set the repository's shared Actions variable `PUBLISHER_SECRETS` to:

```json
{
  "trask": "TRASK_PUBLISH_TOKEN",
  "open-telemetry": "OPENTELEMETRY_PUBLISH_TOKEN"
}
```

The mapping supports any GitHub owner, not just these examples. Keys match case-insensitively and must be unique; values must be uppercase Actions secret names that start with a letter, contain only letters, digits and underscores, and end in `_PUBLISH_TOKEN`. The variable contains names only, never token values. An explicit `{}` disables all publisher routing. Without the variable, only effects in repositories owned by the central owner use `TRASK_PUBLISH_TOKEN`.

GitHub does not expose saved secret values for renaming. When migrating secret names, add the token values under the names in this mapping through the Actions secrets UI. Existing secrets under other names are not copied or used as fallbacks.

Source selection uses the actual PR **head repository owner**, so a personal fork of an organization repository uses the personal token for fork reads and pushes. The publisher separately selects the base owner's token for upstream REST reads, GraphQL, CI logs and authorized PR operations. An OpenTelemetry PR from a personal fork therefore needs both mapping entries shown above. Both PATs must authenticate as the launch owner. Copilot feedback uses the organization token's PR-write permissions for bot-thread handling and review requests. Description and pending reviews need only the base-owner token. Missing required permissions stop publication without a credential fallback.

The launch checks both selected secrets' presence before admitting a worker and rechecks their repository routing after freezing the PR. Continuations and explicit reconciliation select from the frozen repository identities and the current mapping. The live job verifies both selections again before authenticating. A mapping change between selection and publication blocks instead of using a mismatched token. Only the trusted publisher step receives token values; the coordinator sees names and a presence boolean, and workers, verifiers and the waiter receive no publisher tokens.

Source-changing publisher tokens need Contents read/write and external-review tokens also need Pull requests read/write. Report-only tokens need base Contents read and Pull requests write, not head push access. CI repair needs base Actions read/write for evidence and reruns. Include Workflows write when fixes can change workflow files. Organization policies may require approval. Adding a mapping does not broaden a token or provision one.

External-review publication freezes every unresolved original comment from submitted, verified Copilot reviews, including older-head reviews and outdated threads, plus nonempty current-head review bodies. Existing findings start a worker without a Git push dry run or Copilot permission probe. If all verified reviews are on older commits and no findings remain, the launch requests a fresh current-head review directly without consuming a worker pass. A missing verified review still rejects the launch, as does an empty finding set when the current head already has a review. An already pending Copilot review keeps the phase waiting without issuing another request. Copilot review-request permission is assumed; if a request fails, the loop stops with an explicit error and retains any confirmed publication. Uncertain requests are reconciled, never blindly reissued. Self-review neither reads nor requests Copilot reviews and needs no review-request permission.

## Explicit operations

API errors report the failed endpoint and numeric rate-limit headers without printing response bodies or credentials. A rate-limited HTTP 403 is distinct from a permission denial. `X-RateLimit-Remaining=0` identifies an exhausted primary quota, and `X-RateLimit-Reset` gives its UTC epoch reset time. The central Actions token shares a 1,000-request hourly quota across jobs in this repository.

Checkpoint snapshots read uncached Git blobs in native GraphQL batches sized for the shared API response bound and verify the text against each Git object hash. Truncated or altered text requires a complete read through the bound REST blob endpoint; source and archive evidence are never shortened. The shared waiter treats confirmed rate-limit errors as transient and waits for the reported reset within its existing execution deadline. If the reset is later, it exits without another dispatch and the scheduled waiter resumes polling.

Dispatch `coordinator.yml` from central `main`. Only the existing personal owner can launch work. The target must be open and authored by that user or be a Copilot PR GitHub attributes to them, except for PR Reviewer.

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

Use Run again after a terminal phase has stopped. Ordinary fresh launches observe the prior checkpoint without requiring copied `previous_request` or `previous_generation` inputs. They verify prior executions are quiescent and reject unknown or in-flight side effects. A new phase archives prior evidence and may select any kind, even at the same head. Review and CI loops allow five workers; single-pass tasks allow one. There is no overall deadline. Individual job timeouts, artifact retention and uncertain-operation propagation windows still apply. It cannot reuse an old candidate or replace an active phase. A missing-auth gate can likewise be followed by a fresh authorized phase. An unfrozen read-access gate stays separate; restored access freezes a new numeric-repository checkpoint.

If an old phase stopped with an unconfirmed Copilot review request, a fresh launch checks for a submitted bot review or exact Copilot review workflow created after that intent on its saved head. This evidence can confirm the request even when the PR head has advanced or the old phase has expired. The new phase records the confirmation and archives the prior checkpoint unchanged; it does not request another review during reconciliation. Current requested-reviewer membership cannot confirm an old-head request. Missing or ambiguous evidence, uncertain pushes and pending thread effects still stop the launch.

`operation=cancel` requires the exact observed `previous_request` and `previous_generation`. It changes generation inside the state CAS transaction, preserves evidence including thread-effect intents, and prevents later finalization/new effects. It does not kill unrelated runners or undo an already authorized in-flight mutation.

There is one fresh launch operation. Old mode, replacement, and start-publication inputs are not supported. Historical requests, receipts, reports and source fixtures remain read-only and cannot be resumed or reinterpreted.

### Automation revision pins

Each new phase saves `workflow_revision` and `workflow_ref`, a central branch named `review-loop-revisions/<workflow_revision>`. The launch uses its Actions run's exact central-main commit, even if `main` advances while that run is starting. A branch is created once per revision and reused only when its exact ref, commit-object type and SHA match. The automation never updates or force-pushes these branches.

After creating a revision branch, or losing a concurrent creation race, launch confirms the exact ref with up to three reads. A temporary 404 waits one second, then two seconds before the final read. Creation is never retried. Other errors, mismatched refs and a final 404 stop the launch. Existing-phase pin checks do not retry missing refs or recreate branches.

The shared waiter remains on current `main` and routes each phase to its saved ref. Worker dispatch and targeted coordinator ticks run the frozen workflow definition and code; the trusted verification/publication job checks out the bound revision. Later review/fix passes retain the same phase pin. New pushes to `main` do not block pinned phases, and no phase silently adopts newer automation. Existing `reviewable-v1` phases keep their old contract and jobs at that pin. Ref drift, cancellation, target drift and provenance failures still stop work.

Keep revision branches while their phases or retained evidence may need them. Deleting or moving one blocks its affected work without a `main` fallback. Cancel and relaunch an active phase to pick up an automation fix. Existing phases without `workflow_ref` keep their original central-main revision guard; this does not rewrite historical checkpoints or restart phases already blocked by an update.

## Source and candidate boundary

The freeze binds base repository name/ID, actual head repository name/ID/ref, source/target visibility, author/launch actor, exact SHA, trusted workflow revision, request digest, baseline review IDs and verified findings. Forks use the head repository for Git and the base repository for PR/review APIs. Credentialed operations recheck open/author/head/source identity immediately before acting.

New freezes bind `commit_author`, the launch owner's verified GitHub numeric ID and login. For source-changing workflows this is the PR author or the verified owner of a Copilot-authored PR. Copilot attribution is checked through GitHub's author search, and its bot author ID is frozen separately and rechecked. PR Reviewer separately freezes the PR author's numeric identity, which may belong to the launch owner, another person or a bot, and never constructs a code commit. Native candidate commits use `Trask Stalnaker` as both author and committer display name and `<id>+<login>@users.noreply.github.com` as both emails. The publisher verifies its authenticated account matches the frozen author by ID and login before pushing, and the Copilot co-author trailer is retained.

Requests without this frozen author cannot publish or have their candidates reconstructed under the current runtime. Historical evidence stays unchanged. After the prior phase is verified quiescent, start a fresh phase to freeze an attributed candidate; do not reuse or rewrite a saved candidate acceptance.

Self-review freezes base ref/tip and the merge-base SHA instead of review IDs/findings. The tip is read from the live base branch ref, not the PR's base SHA metadata, which can lag behind that branch. The exact PR head and base branch identity are rechecked before accepting results, publication, refreezing and recording clean. New upstream commits do not invalidate a frozen pass. Each later review/fix or CI-repair pass freezes the current base while preserving its existing worker budget. Conflict resolution merges its launch-time base snapshot even if upstream advances; it does not include newer upstream commits or land the PR. Description and pending-review effects still require the actual GitHub PR diff to match their frozen input.

New tasks use `input_mode: direct`. Git retrieval is unauthenticated and limited to the selected public base/head repositories, with no hooks, credential helpers or target scripts. It preserves the sandbox's proxy and certificate configuration. Workers fetch the exact Git commits inside the sandbox; verifiers and publishers fetch them independently without executing target code. Complete Git trees, not paginated or truncated API file lists, define the source input. Conflict tasks retain history through the selected merge base. Missing or invalid source fails rather than narrowing review. Base permissions do not imply fork push access.

Trusted setup downloads PR diffs and CI logs directly on the worker runner. Diffs live in `frozen-request.json`'s local `inputs.pr_diff`; CI failures in `inputs.ci_evidence` point to local `log_path` files. These acquired inputs are not written back to checkpoints. Durable requests contain CI run/attempt/check/job metadata, not log text. Results include `input_identity` copied from `inputs.identity`, binding the acquired diff hash. Verification and publication reacquire the selected evidence and reject changed identities or unsupported quotations. Existing pinned phases keep their source-artifact transport.

Public-only execution does not sanitize retained Git history, checkpoint branches, logs or artifacts. Before changing the central repository's visibility, inspect that historical data and stop any private phases still running old pinned code. Pinned phases retain their original publisher checks; use a fresh phase for the current runtime. No visibility change or historical cleanup is performed by the public-target policy.

The trusted verification/publication job checks the three-file archive, semantic results, API-issued run/attempt/artifact identities and server digests. Native Git verifies the bundle and imports its single `refs/heads/candidate` export into fresh bare Git. The verifier derives every commit/tree/parent, subject, changed path, finding mapping and diff/bundle hash, then checks frozen ancestry and authorship. It never checks out or executes target code. Extra archive members, including `validation.json`, reject.

Any target-repository file may be read or changed when needed for the task, including configuration, instructions, binary files, executable files, symlinks and submodule pointers. Merge candidates may repair compatibility in files without textual conflicts. UTF-8 filenames have no ASCII-only or 240-character policy limit; comparisons use NUL-delimited names. Paths must remain relative without traversal, NUL or `.git` metadata components. Trusted jobs use Git 2.43 or newer to inspect bound objects without checking out target code, following symlinks or retrieving submodule contents. Target instructions cannot alter the central protocol, and actual secrets must never enter candidate artifacts. Central trusted runtime files retain separate protections. GitHub enforces actual workflow-file publication permissions.

## Worker repository checks

The worker chooses relevant existing repository checks and runs them inside its AWF sandbox. It records actual commands, exit codes and failures in `diagnostics.txt`, not a structured validation plan. Missing tools, blocked dependencies and failed checks must be reported honestly.

Before returning artifacts, the worker runs `python3 -m loop.worker_output /tmp/target` in the central workspace inside AWF. The helper packages its ordinary local commits into `candidate.bundle` and checks the result schema. No-source tasks omit the target argument; no-change and blocked outcomes return an empty bundle. Packaging does not establish provenance or replace trusted verification.

Trusted jobs never execute target code or rerun worker commands. One `personal_live` job verifies the worker artifact, records the structural result and publishes using those same verified Git objects. Results enter `publish_pending` before acceptance; this transition alone cannot authorize a push. Cancellation and live-target checks still guard every mutation.

Workers use hosted Ubuntu 24.04 and available AWF/toolchain mounts, with a fresh writable `/tmp/review-loop-worker-home` and empty JVM/cache directories. The explicit firewall covers GitHub and the listed Maven/Gradle, PyPI, npm, NuGet, crates.io and Go proxy endpoints. There is no unrestricted environment/network mode or credential fallback.

Acceptance binds source, generation, worker run/attempt and workflow revision, and records a digest of the structural report. No intermediate candidate artifact or separate finalizer is required. If publication resumes in another invocation, it reacquires and revalidates the original worker artifact. The ordinary non-force push publishes the accepted chain once, with its durable intent preserved if the result is uncertain.

Worker diagnostics are untrusted feedback. Structural acceptance proves the candidate's patch/Git identity and provenance, not passing tests or coverage. Clean review completion is independent of target CI. No-change runs never push an empty packaging commit.

## Self-review passes

Each fresh worker reviews the complete frozen merge-base-to-head PR diff, relevant surrounding code and repository instructions, then fixes only concrete warranted problems introduced or directly affected by the PR. There is no separate discovery, fixer or evaluator agent.

Both loops return exactly `candidate.bundle`, `result.json` and `diagnostics.txt`. Self-review's semantic result is small:

```json
{"schema":2,"request_digest":"<frozen digest>","input_identity":{},"outcome":"clean"}
```

`input_identity` copies the acquired `inputs.identity`. `outcome` is `fixes`, `clean` or `blocked`. Fixes require native commits and cannot also claim clean. Clean requires an explicit completed review and an empty bundle. Missing, malformed, blocked or contradictory output stops without publishing the candidate. No patch spans, messages, external finding IDs or review inventory are required.

An accepted changed pass publishes its exact structurally verified candidate, refreezes the new head and starts a fresh full-PR worker with the same phase, consumed count and publication history. It does not wait for Copilot or attempt CI repair between passes.

A clean no-change pass still runs worker-side repository checks and undergoes structural acceptance. It finishes without observing target CI. Clean requires an unchanged PR head and base branch, not an unchanged upstream tip. Fifth-pass fixes retain their publication but end exhausted because no later clean pass fits the budget. A fifth no-change clean pass can finish.

## PR Description inputs

PR Description reads frozen title/body and the GitHub PR diff without fetching a separate changed-file list or packaging source. Binary-file markers are supported input. It does not require diff anchors, matching per-file line counts, a frozen base tip or reconstruction of the PR's Git tree. API response bounds still apply. The verifier requires an empty candidate bundle and binds the proposal to the request without constructing a source tree. Before PATCH, the publisher revalidates artifact provenance, exact PR head, unchanged GitHub diff and original title/body.

## Fresh review and CI

Only submitted reviews from the verified Copilot numeric/node/type identity participate. Full bodies and unresolved original bot roots are frozen; human roots and bot replies in human threads are excluded. Fresh review requires exact expected SHA, absence from the durable baseline, submission strictly after it, complete paginated bodies/comments/threads and a two-minute propagation delay.

Unknown bodies cannot establish clean, but verified unresolved original bot comments, recognized positive finding counts and `CHANGES_REQUESTED` still trigger investigation. The CCR v2 zero-finding grammar accepts current `0 open findings` summaries with the trailing review-effort footer and legacy `Findings: None` overviews. GitHub's optional approval-feedback survey invitation after the review is not part of the findings. Both summary forms require `Approval recommended` or `Needs a closer look` with the standard status icon and `Review effort: Balanced`. A clean workflow result means no remaining findings, not passing CI, human approval or readiness to merge.

A recognized clean overview may include a counted resolved section, either `N resolved since last review` or `Resolved since last review (N)`, containing only links to original verified bot roots. Those roots must independently be resolved in live thread data; outdated unresolved roots still prevent clean. Missing roots, extra markup, duplicate links and inconsistent counts cannot establish clean.

Every fresh body participates, so a later clean summary cannot erase an earlier finding. A `Previously missed` section still counts as findings after a resolved section, even when the overview says `0 open findings`. Its counted body-only findings retain their identities with the native review-effort and survey footers. Open or hidden findings outside the resolved section and unresolved live threads still prevent clean. No inline comments, no edits, blocked checks or a `Findings: None` substring is not proof of clean.

Fix CI selects existing CI check names/status contexts from the target's frozen head. Exact-SHA paginated checks/statuses retain `none`, `missing`, `pending`, `failed` and `unknown`. Completed `success`, `skipped` and `neutral` checks are nonblocking, matching GitHub. Pending results keep CI repair waiting; missing, absent or unknown results block it; failures proceed to diagnosis. Commit-status contexts use the latest update returned by GitHub's combined-status endpoint. Other tasks do not collect CI for completion, including when no checks exist.

Separate GitHub Actions workflows or events can use the same check name, such as `test`. Dynamic executions, including GitHub's Copilot review workflow, participate without requiring a `.github/workflows/*.yml` path. The watcher verifies each check's Actions app, exact head, check suite, current attempt and job/check linkage. For each workflow and event, only the newest run number participates; a newer queued execution without checks keeps the result pending. Reruns replace older checks with current-attempt jobs. Failed-job retries can retain an earlier nonblocking job only when GitHub verifies its run, attempt and check linkage and no current job replaces it. Every independent current check must be nonblocking. Same-execution duplicates, unbound links and mixed check/status contexts remain unknown. These extra provenance reads are limited to 100 executions per CI collection and require actual API access to the public target.

Publisher continuations may read the exact public Copilot user-identity endpoint and the numeric user endpoint for the frozen authorized launch actor outside their target/head repositories. External reviews verify the returned Copilot bot ID and node ID. Bot-authored PRs revalidate the launch actor's identity and GitHub ownership attribution before freezing another worker. Other user endpoints and mutations remain forbidden.

Fresh external findings can freeze another worker within the same phase regardless of target CI. Retained old roots do not block genuinely new inline findings or new body-only findings in a complete counted CCR v2 `Previously missed` section. The next worker still receives every eligible unresolved root. Repeated finding collections, or entirely reused roots without new body-only findings, stop another pipeline; rewording an overview alone does not count as new work. These rules never clear unresolved threads or establish clean.

Both kinds allow five total model pipelines including the initial and failed admitted work, without an overall deadline. Active phases never replenish the worker budget. Frozen historical budgets stay unchanged; only a new authorized, quiescent phase gets fresh budgets. No sixth worker exists.

Landing PRs, changing draft state, force pushes, top-level comments and human-rooted thread replies are not allowed. Only `pr_description` can update title/body; only `pr_conflict_resolver` can publish a base merge. Explicit Copilot-review launch authorizes warranted commits, original-bot replies, resolution, and then fresh review. Self-review authorizes commits only.

## Native candidates and thread effects

Fresh requests use `git-candidate-v1` and schema 2. `candidate.bundle` contains ordinary native commits, not model-authored provenance. Up to 100 linear commits are supported, or one merge commit with the frozen PR head first and frozen base second. Every intermediate change must satisfy path-safety, Git integrity checks and trusted-runtime protections. Existing pinned phases finish with their own contract; there is no in-place migration.

Every unresolved verified finding and complete conversation is retained without count or body-length caps. Empty ordinary commits, duplicate/foreign findings and incomplete mappings reject rather than truncate. API response, artifact, process-resource and runtime limits still apply.

Copilot-review results include every frozen finding exactly once, with `key`, `disposition`, `analysis` and `commit`. Dispositions are `fixed`, `not_warranted` or `blocked`. A fixed finding selects its fixing commit by a positive 1-based history index; other dispositions use null. Any blocked finding prevents publication of partial work.

Commit messages need a useful subject and the Copilot co-author trailer, without mandatory comment copies or tradeoff sections. Author and committer must match the frozen launch owner. See the [message example](../README.md#reviewable-commits-and-thread-handling).

Only the trusted publisher posts and resolves. After confirming both branch ref and PR head, it replies to accepted original verified Copilot roots. Fixes name the actual mapped commit, not merely the iteration tip. Supported rejections or already-present fixes use `No code change.` with the finding's concrete explanation. No-change never creates or pushes an empty commit. Body-only findings have no reply or resolution.

Complete conversations are frozen and rechecked before effects. Human-rooted threads are excluded, even if Copilot replies there. Human intervention, changed root text/identity, or a new objection prevents automatic handling of that thread and records the reason. Already resolved threads are skipped without claiming responsibility. Outdated comments are checked against the frozen code, not assumed resolved. Already-addressed comments receive an explanatory no-change reply and resolution; published fixes receive their mapped-commit reply and resolution even if the original diff becomes outdated.

The publisher persists a reply intent before posting to `repos/{repo}/pulls/{pr}/comments/{original_root_id}/replies`. It confirms the exact actor/root/body/marker match before persisting a resolution intent and issuing only the bound `resolveReviewThread` mutation. Resolution requires a matching response acknowledgment and live resolved state. Broad GraphQL mutation access, source-read mutations, and worker/waiter publication remain forbidden.

Before each effect, the publisher rechecks full state CAS, owner execution, request/generation, authenticated actor, exact head/ref and current root/conversation. GitHub cannot atomically bind a thread mutation to a PR-head comparison; the publisher rechecks immediately before effects and confirms afterward. A fresh verified review remains mandatory for Copilot-review clean completion; target CI is separate.

## Shared Actions waiting

`waiter.yml` maintains one synchronous waiter for every active authorized PR, regardless of loop kind. Only one fixing loop can be active per PR, but different PRs can use different kinds concurrently. Launch each PR normally; additional launches join the same polling loop. Workers, structural verification and publication can run independently for different PRs. No per-PR job sleeps waiting for review or CI.

The waiter reloads checkpoints once a minute and checks each unchanged due PR at most every five minutes. New reviews must satisfy the exact-head identity and two-minute propagation requirements before work is dispatched. Full review validation and Fix CI's CI validation happen in the per-PR coordinator, not in the readiness probe. Each phase keeps its launch-time runtime revision; existing phases retain that revision's completion rules. A fresh launch picks up current behavior.

Coordinator/worker activity starts or queues the waiter. Global waiter concurrency allows one running waiter and a queued successor; queued runs do not occupy runners. After 55 minutes it dispatches a successor before the 65-minute job timeout. It exits when all phases are terminal. Cron remains only a best-effort recovery trigger. A runner cancellation or failed handoff can require a fresh manual `gh workflow run waiter.yml --repo trask/copilot-workflows --ref main`.

The waiter has central state/Actions access and optional target source-read access, never inference or publisher credentials. Durable coordinator run claims prevent duplicate verification/publication work and make it wait for the producing workflow to finish before accepting its artifacts. A blocked PR does not stop other authorized PRs.

## Checkpoints and uncertain operations

Version-2 checkpoints use `pr-v2-<actual-base-repository-ID>-<PR>.json`. An unfrozen access gate uses the first 32 hex characters of the case-folded repository name's SHA-256 instead of inventing an ID. The JSON-only branch retains checkpoints and archived history without file-count, per-checkpoint size or total storage caps. API response bounds still apply to individual reads, and incomplete trees or corrupted blobs reject explicitly. Source bundles and worker outputs stay in bound Actions artifacts.

Writes create a child of the observed state ref and update it with `force:false`. Competing children cannot both fast-forward. Conflicts reload/retry at most twenty times with bounded randomized backoff; replacement archives exact prior state. Finalization/mutations require the same complete request/generation/state in CAS checks. The waiter serializes globally; short coordinator jobs and workers serialize per request/PR. Durable run ownership permits different PRs' verification pipelines to overlap.

REST GET timeouts and HTTP 500/502/503/504 responses allow up to three attempts, with one- and two-second backoffs. Retries log only the failure category and attempt, respect the existing execution deadline and raise the final error if reads do not recover. Permission, validation and rate-limit errors are not retried. POST, PATCH, PUT and DELETE calls, including GraphQL POSTs, are never retried by the API client; uncertain effects still require reconciliation.

Initial requests record `launch_run` with actual dispatch run ID, attempt and actor. HTTP 204 acknowledges dispatch, not an arbitrary latest checkpoint. Clients must bind a queued acknowledgment to the observed launch run and resulting request, or leave it unconfirmed. Repeating POST to manufacture confirmation is forbidden.

Dispatch, publication, reply, resolution and review-request intents persist before side effects. Reconciliation checks exact Actions/PR/ref/review identities without retrying effects. Publication waits for agreeing head-ref/PR candidate SHA. An unchanged original head after uncertain push blocks for inspection; another head is stale. Missing/duplicate dispatch identities and unknown requests block with evidence retained.

Lost reply responses are reconcile-only. A unique exact reply body, actor, root and bound marker can confirm the effect without another POST. A resolution response lost before its acknowledgment is saved cannot be uniquely attributed by resolved state alone; it stops as uncertain even if the thread is resolved. An acknowledged resolution may confirm through live reads. No thread mutation is blindly retried.

Unknown effects retain their intent and stop with `thread_reply_uncertain_no_retry` or `thread_resolution_uncertain_no_retry`. Cancellation preserves intents. A new phase cannot clear them; inspect and reconcile the evidence before further work.

`operation=reconcile-publication` requires a fresh owner dispatch, explicit fine-grained authorization and exact stopped request/generation. It confirms only the already-authorized current-protocol candidate, saved acceptance/artifacts and agreeing live tip/tree identity. It archives the stopped checkpoint and records separate execution provenance. It neither reaccepts under another protocol nor pushes again. Historical candidates are never resumed.

### Recovering a completed worker on fixed coordinator code

When a pinned coordinator cannot process a completed worker, authorize verification and completion on current central code:

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=recover-coordinator -f publication_auth=fine_grained_pat \
  -f target=owner/repository#123 \
  -f previous_request=<observed-request-id> -f previous_generation=<observed-generation>
```

Recovery requires the exact request and generation, one successful completed worker with available artifacts, stopped claimed executions, an unchanged target and no accepted result or publication effects. It pins the current coordinator revision and retains the original frozen request, worker identity, artifacts and consumed pipeline budget. Verification and completion use the recovery pin; the worker is not rerun. Automatic retries never upgrade a phase without this explicit authorization.

### Restarting after an unpublished push

After correcting publisher access, start new work instead of rerunning the failed Actions job:

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f publication_auth=fine_grained_pat \
  -f target=owner/repository#123 -f restart_unpublished=true \
  -f previous_request=<observed-request-id> -f previous_generation=<observed-generation>
```

The owner must explicitly select `restart_unpublished` and provide the exact stopped checkpoint identity. The old phase must be blocked on an uncertain push or publication propagation timeout. Its publisher, verification pipeline, worker, coordinator, launch and source-acquisition runs must have stopped, with no reruns or duplicate worker identities. Wait fifteen minutes after the original publisher's last update so ref propagation can settle.

Both the actual head ref and PR head must equal the original frozen head, and the freshly frozen target identity must still match. An already-published candidate, unrelated head change, pending review-request effect or retired acceptance blocks this restart.

The same non-force state transaction archives the old checkpoint unchanged as `request-<id>.json` and creates a new request with restart provenance, five pipelines and no overall deadline. The old publication intent keeps its original status and acceptance. No candidate, result, publication or pending effect carries into the new phase. Corrected permissions do not automatically unblock old runs, and ordinary ticks cannot authorize this reset.

Earlier checkpoints/artifacts remain readable as historical evidence but cannot execute. There is no old/new execution compatibility, protocol switch or in-place migration. A fresh launch may archive a terminal old checkpoint after verifying its exact bound executions have stopped and all effect intents are settled. Old capability or unknown effect evidence blocks fresh admission. The unchanged [pilot record](legacy-pilot.md) documents historical restrictions/results; none of its commands authorize continuation.

## Dashboard troubleshooting

The project canvas loads automatically in a Copilot app session opened in this repository. After editing its extension, reload extensions before opening PR workflows, which retains the `workflow-dashboard` canvas ID. Use the extension inspector's log if it fails to register.

Authenticate `gh` to `github.com` with target PR read access, shared-workflows dashboard Contents read access, and central repository Contents/Actions read access. Run and Cancel additionally need central Actions write access. The canvas uses fixed REST read endpoints and read-only GraphQL queries. Writes are central `coordinator.yml` dispatches or cancellation of an exact recorded central launch run. It never extracts tokens, receives Actions secrets or publishes directly to a target. Account/PR eligibility and existing checkpoints are checked again before dispatch; the coordinator retains final authorization.

The dropdown's default is Java instrumentation. The other choices are semantic-conventions-conformance, shared-workflows, semantic-conventions-genai, semantic-conventions, github-threat-detection, admin and opentelemetry-java, all under `open-telemetry`; edit `repositories.mjs` to extend the list. My PRs is selected by default and matches the authenticated viewer's author login. Switch to Not my PRs for everyone else's PRs, including bots. The two views are mutually exclusive and both include drafts and PRs without dashboard data. My PRs shows all eight tasks; Not my PRs shows only Draft review, plus Cancel when authorized. Waiting on reviewers selects current, nondraft dashboard records with `route: "approver"` from `<repository-name>/dashboard-state.json` on shared-workflows' `otelbot/pull-request-dashboard-state/<repository-name>` branch. It does not infer readiness from pending review requests. The reader supports dashboard state version 18; unsupported versions, missing records and mismatched heads stay unknown or stale.

Repository switches immediately show a loading indicator and replace the previous repository's cards with a loading message. Repository, refresh and automatic-refresh controls stay disabled until the request completes or fails. The PR region exposes its busy state to assistive technology. Older background responses cannot replace a completed repository switch; failures remain visible and unlock the controls for a manual retry.

Refreshing the current repository keeps the previous PR list, button statuses and workflow results visible while reads are in progress. The replacement snapshot appears only after the live action evidence and workflow reads have settled; partially loaded data does not reset buttons to unknown.

A failed reviewer-dashboard read leaves the open PR list usable and shows a warning. A failed live-PR read retains the last successful list as stale. A failed or malformed central-state read disables task controls rather than claiming there is no active workflow. Missing dashboard facts alone do not prevent task dispatch. PR cards report workflow status, task errors and blocking explanations through their button tooltips. Draft review distinguishes No findings from Review ready and retains its pending-review link. The on-demand run log includes tasks for closed and filtered-out PRs. Ask Copilot to inspect GitHub Actions logs and saved checkpoint history for detailed diagnostics. Output verification checks saved outputs, not review quality or passing tests.

Yellow/amber buttons indicate detected work from live, head-matched GitHub data. PR Conflict Resolver reads `PullRequestMergeConflictStateCondition`: `FAILED` means file conflicts, `PASSED` disables unnecessary conflict resolution, and other or missing results remain unknown. Aggregate mergeability and unrelated failed merge conditions do not determine this button. CI Fix Loop displays the current head's GitHub `statusCheckRollup.state`: `FAILURE` or `ERROR` highlights and enables repair, `SUCCESS` shows passing, and `PENDING` or `EXPECTED` shows pending. Passing, pending, absent, unknown and stale results disable repair. Refreshes fetch only this CI summary, not individual checks or workflow metadata, and do not report check counts. Launch preflight still collects every CI check and applies the latest-execution rules before dispatch. Live summaries are fetched on every refresh without a CI evidence cache.

Buttons show only their task name. Needed work uses an amber tint and border; confirmed-unnecessary fixes are disabled and dimmed. In-progress tasks use blue styling and retain a spinner while running, waiting for review, waiting for Fix CI checks or confirming PR updates. Cancellation buttons show it until cancellation is confirmed. Reduced-motion mode uses a static hourglass instead. Completed, failed, cancelled and uncertain operations do not show a busy indicator. Themed tooltips show status separately from a short action description on hover or keyboard focus. Disabled controls remain focusable and explain why they cannot run, without an action description. Escape dismisses the tooltip. There are no permanent bottom status labels or circles.

Address Copilot feedback is highlighted for every unresolved thread rooted in a submitted review from the verified Copilot review bot, using its stable node ID rather than display login, and for current-head review-body feedback. Older reviews and outdated threads count; human-rooted conversations with bot replies do not. With no open feedback and only older submitted reviews, the button reads Refresh Copilot review and uses normal enabled styling instead of amber. Its tooltip says, "Copilot reviewed an older commit. Request a review of the latest commit." Both labels launch the same task. No open threads is not a clean-review guarantee. Review and fix, Update title & description, Simplify code, Draft review and Align with existing code remain manual actions unless a saved run supplies their status. Active tasks and dispatch locks keep their existing priority; a completed task does not conceal newly detected work.

Missing, failed, incomplete or head-mismatched live action evidence shows unknown status and a warning, without falling back to saved dashboard facts or claiming a fix is unnecessary. The PR list and eligible manual actions remain usable. Pending CI and absent checks are not confirmed passing. Conflict and CI launches re-read live evidence before dispatch, so an old enabled button cannot launch a task that is now confirmed unnecessary.

Before the first task, the central `review-loop-state` branch does not exist. A successful matching-ref listing without that exact branch means empty saved workflow history; it neither creates a branch nor restores old checkpoints. Authentication failures, HTTP errors and malformed or incomplete listings remain errors, not empty history.

Run dispatches `operation=launch` with `publication_auth=fine_grained_pat` on central `main` immediately on click, without a confirmation dialog. Task effects remain in button tooltips. No worker or waiter is dispatched directly. Cancel likewise dispatches the exact observed request/generation immediately; a changed checkpoint rejects cancellation rather than adopting newer work. Clicking either button supplies the API's explicit `confirmed: true` intent; agent actions still require an explicit user request. Pending, accepted and uncertain dispatches disable further submissions until matching checkpoint evidence is observed. Failed launches appear in the on-demand run log, including launches without a task checkpoint. Ask Copilot to inspect the run logs and use the CLI recovery commands rather than repeatedly clicking Run. Launch titles identify the task and target; older unmatched runs require inspecting their logs. A failed Actions run does not replace saved task evidence or unlock an unconfirmed dispatch. No dispatch is automatically retried.

Accepted launches expose Cancel launch using the exact run receipt from API version `2026-03-10`. Confirmation must match that launch ID, task kind and owner. Cancellation rechecks current checkpoints first and uses the normal request/generation protocol if that launch has saved a task. Otherwise it verifies the recorded run's owner, workflow, target, branch and first attempt before cancelling it. A task saved during cancellation is also cancelled by its exact identity. Acknowledgement alone does not mean cancellation completed, and cancellation does not undo published changes or guarantee termination of an already authorized effect. While visible, pending dispatch cancellation is reconciled once a minute even with Auto refresh off; read errors, low capacity and rate limits stop this polling. Dispatch receipts are held for the provider's lifetime. A legacy dispatch without a receipt stays locked until checkpoint confirmation and cannot cancel a guessed run.

An exact launch that finishes without a confirmed task stops showing a busy icon and displays its conclusion and Actions link. It remains locked for inspection rather than assuming there are no workers or published effects.

Refresh every 60 seconds uses cached authenticated ETags for the viewer, selected repository's open-PR pages and its dashboard file. It does not collect a failed-launch listing, the 24-hour run log, checkpoint archives or closed-PR titles. Dispatched and running checkpoints add one read per distinct worker run ID; other checkpoint stages need no worker status read. Accepted launches and cancellations retain their exact run-receipt reconciliation. A 304 response consumes no primary GitHub capacity.

Click Load run log to fetch the past 24 hours of tasks, and Refresh run log to re-read them. Log loading leaves PR refresh and repository selection usable. Repository selection clears the displayed log without starting another log read. The log combines confirmed pushes across worker passes into PR changes-range links. It includes archived tasks, ongoing tasks and failed launches without checkpoints, regardless of the other PR filters. Open PR titles reuse the listing; closed PR titles use one cached read per PR. Missing titles produce an explicit warning. A failed log read retains the previous log marked stale without disabling task controls or pausing automatic PR refresh.

Checkpoint reads use a dedicated temporary shallow Git checkout of central `review-loop-state`, shared by the provider's panels and separate from the working repository. Every PR refresh, explicit run-log load and task preflight fetches the exact branch again, pins the fetched commit, validates its tree and verifies local JSON bytes against their Git blob identities. Git reuses downloaded objects but never skips the fresh fetch. An absent branch is a valid empty snapshot; other Git failures remain errors. Existing `gh` authentication is used without changing global Git configuration or writing tokens. Git commands have a 60-second time limit and hide console windows on Windows. Archive files remain on the branch and are read locally only for an explicit run-log load or an agent's history request. Run-log loads use a separate pinned snapshot without replacing the current task-control snapshot. Fetches, checkouts and history reads are serialized. The temporary checkout is removed when the last panel closes or the provider exits.

Independent API reads run concurrently, with at most five GitHub CLI reads in flight. PR listing, the Git fetch and central workflow API reads start together. Each GitHub CLI process has a 60-second time limit. Reads of the same endpoint share only their in-flight response; subsequent refreshes still revalidate its ETag. Pagination remains ordered. A refresh waits for every started read to settle, including after a failure, before allowing another refresh. Out-of-order rate headers retain the lowest remaining capacity within a reset window, and concurrent rate-limit responses retain the longest backoff. Queued reads do not start CLI requests during that backoff.

On initial load and repository selection, PR cards appear as soon as the listing, ownership and reviewer routing reads finish. Task controls stay disabled until workflow and live PR status reads settle. During these loads, a visible panel checks local state once a second without adding GitHub requests. Later refreshes retain the previous complete PR and workflow snapshot until its replacement is ready.

Live status uses read-only GraphQL queries in batches of five of your PRs, sharing the same five read slots. Check contexts, review threads and Copilot review metadata are paginated before interpreting complete results, and every page must match the listed head. Review metadata queries filter by the Copilot author and submitted review states, without downloading bodies. Bodies are then fetched in batches of up to 100 only for verified Copilot reviews on the current head. Each body must match its expected review identity, repository, PR, head and metadata. Historical and human-authored bodies are not downloaded; older unresolved Copilot threads still count. GraphQL reads are not cached and do not retry POSTs. Repository switches and refreshes always reacquire live evidence. REST and GraphQL capacity are tracked separately.

Connection resets, unexpected EOFs and network timeouts before an HTTP response receive one GET retry after 250 ms, within the same five-read limit. A rate-limit cooldown prevents that retry from starting. Authentication, certificate, CLI process/output failures and received HTTP errors are not retried. Refresh state records network retry attempts; the canvas shows a sanitized failure reason if recovery fails, without exposing CLI stderr. Launch and Cancel dispatches never retry, including after a connection reset.

Steady-state refreshes taking more than 10 seconds disable automatic refresh. Request count alone does not pause refresh. Less than 10 percent remaining REST or GraphQL capacity, authentication/read errors, and rate-limit responses also pause it. The canvas displays read errors and automatic pause reasons; choosing manual refresh does not add a notice. Load timestamps, refresh cost and rate-limit metadata remain in refresh state, not in the header. Retry-After and reset headers prevent premature reads. Refresh manually after resolving the problem, and explicitly enable automatic refresh to resume polling. Hidden or closed canvases do not keep polling, and panels within the same extension provider share a refresh.

Requests stop at 10,000 open PRs or 16 MiB of listing data, 1 MiB per reviewer dashboard file and 1,000 entries per Actions status. Central checkpoint storage has no file-count, per-file size or aggregate size cap; individual API responses remain bounded. Pagination follows trusted next-page links; Actions `total_count` is advisory because active runs can start or finish during a read. Unfinished page chains or malformed data produce an explicit error or per-checkpoint warning, not a partial success claim. Mutating loopback requests require the canvas's exact origin and a validated JSON body of at most 4 KiB. Last successful data remains visible and marked stale when a refresh fails.

## Local development

Use `python tools/test_docker.py [unittest-selector]` for fast Linux Git/Python tests on Windows, then the full suite before publication. The tools-only container runs in non-root tmpfs without network, host mounts or secrets. Workflow changes also require the real pinned compiler, linter, zero-write audit and reproducible generated bytes.

There is no standalone qualification workflow, Gradle smoke dependency or automatic E2E target. A live test needs an explicit PR and user authorization. Unit/static success is not live credential or target-runtime proof.
