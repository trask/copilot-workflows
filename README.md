# PR workflows

Review and fix explicitly selected PRs. The default `loop_kind=copilot_review` investigates existing verified Copilot findings. `loop_kind=self_review` uses one fresh worker per pass to review the complete PR diff and fix warranted problems. Both read repository instructions, format changes and run appropriate existing checks. There is no repository or language allowlist or per-repository adapter.

The scope stays personal and opt-in. Only a fresh owner dispatch can launch work. Source-changing tasks and description require that owner's open PR, including Copilot PRs GitHub attributes to the owner; PR Reviewer can review the owner's or another author's open PR, including bot-authored PRs. The watcher advances existing authorized checkpoints; it does not discover PRs or scan repositories. Both existing loops publish warranted fixes. Copilot review always replies to and resolves eligible original Copilot threads before requesting fresh review; no other kind posts replies or resolves threads. There are no preview/shadow modes or reply switches.

## Independent tasks

All eight kinds share `coordinator.yml`, one worker per pass, structural verification, credential routing, durable intents, checkpoint storage and the shared waiter. Select one `loop_kind` per launch. They do not form a combined pipeline. Workers return result artifacts; only trusted publisher jobs make GitHub changes.

The shared Copilot worker uses `gpt-6.1-sol` with high reasoning effort. `.github/workflows/copilot-worker.md` sets the canonical `engine.model: gpt-6.1-sol` and passes `--reasoning-effort high` through `engine.args`. The gh-aw threat detector inherits the model and transport settings but keeps its default launcher and reasoning effort. Both pin Copilot CLI `1.0.93` and use the Responses wire API. `COPILOT_PROVIDER_MODEL_ID: gpt-6.1-sol` selects the CLI's built-in model configuration. GitHub's external Copilot PR reviews use GitHub-managed settings, not this worker configuration.

The sandbox permits Mise's version metadata service at `mise-versions.jdx.dev` and Sigstore's trust-root service at `tuf-repo-cdn.sigstore.dev` so repository-pinned linters can install with TLS and artifact attestation verification enabled.

| Display name | `loop_kind` | Completion |
| --- | --- | --- |
| Address Copilot feedback | `copilot_review` | Existing findings, warranted fixes, mandatory bot-thread handling and fresh external review until no findings remain |
| Review and fix | `self_review` | Full-PR review/fix passes until an explicit later clean pass |
| Resolve conflicts | `pr_conflict_resolver` | One merge of the launch-time base snapshot into the frozen PR head, with head first and base second. Never lands the PR. An incorporated base uses no worker or empty commit |
| Fix CI | `ci_fix` | Diagnose exact-head failures, repair only PR-attributable causes, then observe resulting CI. One evidence-based failed-jobs rerun per Actions run, including manual retries |
| Update title & description | `pr_description` | Compare title/body with the supplied GitHub PR diff and PATCH only changed proposed text. No source checkout, commit or CI gate |
| Simplify code | `pr_simplify` | One pass of major behavior-preserving simplifications in changed or directly affected code. No cosmetic or product-scope cleanup |
| Draft review | `pr_review` | One correctness review of the actual GitHub PR diff. Create one viewer-owned pending review on changed-line anchors, never submit or approve it |
| Align with existing code | `pr_consistency` | Compare changed code with nearby examples and applicable instructions, report needed/avoidable/unclear differences and publish avoidable fixes only |

For example, launch a description task:

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f loop_kind=pr_description \
  -f publication_auth=fine_grained_pat -f target=owner/repository#123
```

Replace `pr_description` with any kind in the table. Only Fix CI waits for target CI or uses it to decide completion. All other tasks finish or continue their review passes independently of CI; the canvas shows live CI separately. Single-pass tasks never claim the review loops' clean-review guarantee. No qualifying fixes/findings means no invented commit/review. An existing viewer-owned pending review is preserved and blocks a second review.

Checkpoints record PR/head/base identities, CI run/attempt/job metadata and task progress, not source bundles, PR diffs or CI logs. Workers fetch the selected public Git commits directly inside the sandbox. Trusted setup on the worker runner downloads the authoritative GitHub PR diff and selected CI logs to local files. Copilot searches those files and diagnoses relevant failures before proposing fixes. Missing inputs stop explicitly; logs are not truncated or filtered by keywords.

Verification and publication independently fetch the same source and reacquire needed diff/log evidence. Worker results bind the acquired diff hash, and changed CI runs or attempts stop publication. Conflict source acquisition deepens until both selected tips reach the merge base, without a commit-count limit. Contradictory intent blocks instead of choosing a side. Existing pinned phases retain their original input transport.

Read-only API requests and signed log/artifact downloads retry transient server errors, timeouts, TLS EOFs and connection resets up to three attempts within the execution deadline. Interrupted downloads restart with a fresh signed URL and discard partial evidence. Certificate verification and permission failures are not retried, and mutations are never retried.

Upstream commits do not invalidate a frozen task. PR-head changes and retargeting to a different base branch still stop it. Conflict resolution merges the launch-time base snapshot; newer upstream commits are not included in that merge. Later review/fix and CI-repair passes freeze the current base without resetting their worker budget. Description and pending-review publication still recheck the actual GitHub PR diff.

Conflict resolution can add new files when needed to preserve both sides' intent, such as moving incoming release notes into changelog fragments. Existing nonconflicting paths must match Git's automatic merge exactly, including deletions. A conflict-free merge cannot include extra changes.

PR Description records the original title/body and PR head, then downloads the GitHub diff on the worker runner. Binary-file markers are retained as input. It needs no separate changed-file inventory, added-line anchors, base-tip freeze, source bundle, repository checkout or Git-tree reconstruction. Before editing metadata, the publisher rechecks the exact PR head, acquired diff hash and original title/body. A changed base tip alone does not stop it when the actual diff is unchanged. Worker artifacts and title/body-only publication still require trusted verification.

## Launch

Use a full GitHub PR URL or `owner/repo#number`. Bare numbers have no default repository.

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f publication_auth=fine_grained_pat -f target=owner/repository#123
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=status -f target=owner/repository#123
```

Launch requires the isolated `COPILOT_GITHUB_TOKEN` inference credential and explicit publisher authorization. Repository access never borrows inference auth or local CLI credentials. Missing private read access creates an explicit blocked checkpoint, not a language or allowlist error.

Once a run finishes, use Run again with the same inputs. A terminal, verified quiescent run permits a new phase of any kind, even at the same head. The coordinator verifies prior executions have stopped and archives their evidence. Review and CI loops allow five workers; single-pass tasks allow one. Phases have no overall deadline: CI and review waits do not consume a time budget, and later failures can trigger another pass while worker slots remain. Individual Actions jobs and API operations keep their timeouts. Ordinary reruns need no copied checkpoint IDs. Active phases cannot reset budgets or switch kinds; uncertain effects require reconciliation.

See [workflow budgets and deadlines](docs/workflow-limits.md) for worker, inference, job, retry, retention, resource and dashboard limits.

A fresh launch can reconcile an unconfirmed Copilot review request from a submitted review or exact Copilot workflow on its saved head, even after the PR head advances. The new phase records that confirmation and archives the previous evidence without issuing another review request. A requested reviewer on a different head is not proof, and unknown pushes or thread effects still block restart.

New phases pin the automation commit their launch ran on. Workers and coordinator continuations use a verified `review-loop-revisions/<commit-sha>` branch, and verification/publication check out that same commit. Updating central `main` does not stop these phases or change their code. New launches use the revision selected by their Actions dispatch. To pick up an automation fix in an active phase, cancel it and launch again after it stops.

Revision branches are shared by phases on the same commit, retained for continuation and provenance, and never updated by the automation. A missing or changed revision branch stops affected work without falling back to `main`. Older phases without a saved pin retain their existing revision-change guard; blocked historical phases are not resumed automatically.

A failed push at the unchanged original head can start a fresh phase with `restart_unpublished=true` and the exact `previous_request` and `previous_generation`. Wait fifteen minutes after the publisher stops. The coordinator checks both live refs and all bound executions, archives the old intent without marking it confirmed, and freezes new work. It never retries the old candidate. See [unpublished restart](docs/setup.md#restarting-after-an-unpublished-push) for the command and restrictions.

The publisher authenticates as the launch owner. Source effects require actual frozen head push access plus PR read access. Description and pending reviews require base-repository PR access only, never fork push access.

The default loop starts from unresolved comments in existing submitted, verified Copilot reviews, including older reviews and outdated threads. It also investigates nonempty review bodies at the current PR head. If all reviews are on older commits and no findings remain, it requests a fresh current-head review directly without consuming a worker pass. It does not request a first-ever review or probe Copilot permissions. After publication and thread handling it requests a fresh review; a rejected request stops the loop with the publication evidence retained.

Self-review needs no existing Copilot review and never requests one:

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f loop_kind=self_review \
  -f publication_auth=fine_grained_pat -f target=owner/repository#123
```

Each self-review pass reviews the full frozen merge-base-to-head diff and fixes only concrete problems introduced or directly affected by the PR. A changed pass publishes its exact accepted candidate and starts a fresh full-PR pass. It cannot also claim clean. Clean requires a later explicit no-change result and structural verification, regardless of target CI. Fifth-pass fixes remain published but exhaust the phase without a later clean pass. Head drift or a base branch change stops the phase; upstream commits on that branch do not. There is no automatic merge, rebase or CI repair.

Publisher tokens are selected by the repository where the effect occurs. Source pushes select the head repository owner; title/body updates, pending reviews and failed-jobs reruns select the base repository owner. CI repair needs Actions read for evidence, and its selected rerun credential needs base Actions write. Set the shared repository variable `PUBLISHER_SECRETS` to an owner-to-secret JSON mapping, then store each fine-grained PAT in its named Actions secret:

```json
{
  "trask": "TRASK_PUBLISH_TOKEN",
  "open-telemetry": "OPENTELEMETRY_PUBLISH_TOKEN"
}
```

The variable and secret names are reusable by other workflows in this repository. Personal and organization tasks can run concurrently with separate credentials. Owners match case-insensitively; secret names must be uppercase, start with a letter and end in `_PUBLISH_TOKEN`. Fork source pushes and fork API reads use the head owner's token. Upstream reads and PR operations use the base owner's token, including GraphQL and CI logs. For an OpenTelemetry PR from a personal fork, that means `TRASK_PUBLISH_TOKEN` for the fork and `OPENTELEMETRY_PUBLISH_TOKEN` for upstream. Both tokens must authenticate as the launch owner. Only the trusted publisher receives them; workers never do. Report-only tasks use the base owner's token. Missing mappings, missing secrets or denied permissions block without retrying with another credential.

Without the variable, only effects in repositories owned by the central owner select `TRASK_PUBLISH_TOKEN`. An explicit mapping replaces that default. The personal token can cover selected repositories or all personal repositories, including forks; an organization entry needs its own token and any required organization approval. All-repository access includes private repositories such as the central workflow repository. Private and central PR targets remain rejected, and workers never receive publisher tokens. The publisher checks live identity, target/head access and bound effects, not the token's complete repository scope. The loop neither provisions credentials nor changes their permissions.

Published commits use `Trask Stalnaker` as both author and committer display name, with the launch owner's verified numeric-ID noreply email. The account identity is frozen from GitHub PR metadata, bound to the candidate before verification and checked against the authenticated publisher by ID and login. For Copilot-authored PRs, the owner's account is verified through GitHub's author search and frozen separately from the bot author. Workers create ordinary Git commits and credit Copilot in the co-author trailer. Push credentials alone do not determine Git commit authorship.

Only description can update title/body, and only conflict resolution can publish a two-parent base update. Landing PRs, changing draft state, forced pushes, top-level comments, submitted reviews and human-rooted thread replies remain outside the protocol.

## Reviewable commits and thread handling

One worker iteration can produce several ordinary commits. Related findings can share a commit; unrelated causes remain separate, including sequential edits to the same file. Messages need a useful subject and the co-author trailer, not prescribed analysis or tradeoff sections:

```text
Reject stale snapshot generations

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
```

The worker returns a small semantic result alongside its native Git bundle. Copilot findings select their fixing commit by its 1-based history index; the trusted publisher derives the actual SHA from Git.

Only the trusted publisher handles threads. Fixed replies start `Addressed in <commit sha>.`; supported no-code decisions start `No code change.` Both include the finding's concrete explanation, without repeating the original comment. No-code decisions push no empty commit. Body-only findings receive no invented inline or top-level comment.

The publisher confirms the exact branch ref and PR head before replying, confirms the reply before resolving, and confirms resolution before requesting fresh review. Human intervention or changed conversation prevents automatic handling of that thread and records the reason. Already resolved threads are explicitly skipped. An outdated or already-addressed comment still gets an explanatory reply and resolution; a fixed thread gets its mapped-commit reply. An outdated thread alone does not establish review clearance.

Every effect has a durable intent. Lost replies can reconcile only against a unique exact body/actor/root marker match. A lost resolution response without a saved matching acknowledgment remains uncertain even if the thread is now resolved. The loop never blindly retries mutations, discards an uncertain intent, or treats thread resolution as review clearance.

## PR workflows canvas

Open a Copilot app session in this repository and ask to "open the PR workflows canvas." The existing `workflow-dashboard` canvas ID opens a single PR list with workflow controls. The repository dropdown lists eight `open-telemetry` repositories: `opentelemetry-java-instrumentation`, `semantic-conventions-conformance`, `shared-workflows`, `semantic-conventions-genai`, `semantic-conventions`, `github-threat-detection`, `admin` and `opentelemetry-java`. Java instrumentation is the default. Extend the list in `.github/extensions/workflow-dashboard/repositories.mjs`.

Switch between My PRs and Not my PRs, using the account authenticated in `gh`. The default is My PRs, which includes your directly authored PRs and GitHub's `gh pr list --author @me` results, including Copilot PRs created on your behalf. Not my PRs shows the remaining open PRs. Both include drafts and PRs without saved workflow or dashboard data. Combine either view with Waiting on reviewers and title/number/author search. Waiting on reviewers uses the OpenTelemetry dashboard's latest saved `route: "approver"` directly from `otelbot/pull-request-dashboard-state/<repository-name>` in `open-telemetry/shared-workflows`, not GitHub review requests. Dashboard classifications and facts are used without comparing their saved author, head commit or draft status with the live PR. Ownership, task permissions and draft filtering use live GitHub data. Missing, failed or invalid classifications remain explicit and do not match that filter.

My PRs shows all eight task buttons. Copilot PRs created on your behalf have the same task eligibility as your directly authored PRs. Ownership is rechecked before dispatch and by the central workflow; source publication still requires actual head-repository push access. Not my PRs shows only Draft review, including for bot-authored PRs; owner-only tasks are hidden. Eligible tasks can run; unavailable tasks stay disabled with an explanation. Completed tasks are dimmed when disabled. The active task is highlighted with its recorded status, including dispatching, starting, queued, running, waiting, completion or failure. Dispatch acceptance is not shown as a running worker, and unavailable status is not shown as success. Run and task cancellation dispatch `coordinator.yml` on central `main` immediately on click, without a confirmation dialog. Task effects are shown in button tooltips. These are real publishing tasks, not previews. Cancel task remains available in either view when authorized and requires the exact displayed request and generation; it does not undo publication or guarantee termination of an already authorized effect. Duplicate or uncertain dispatches stay locked until checkpoint evidence confirms them. Recovery commands remain CLI-only.

Cancel launch is available while an accepted launch is awaiting task confirmation. It uses the exact run ID returned by GitHub, never a guessed latest run. A saved task from that launch uses normal task cancellation; otherwise the launch run itself is cancelled. If its task appears during cancellation, the canvas also requests cancellation of that exact task. Cancellation is checked once a minute while the canvas is visible, even in manual refresh mode, until confirmed or a read/rate-limit error stops polling. The Auto refresh checkbox stays unchanged. A confirmed failed launch permits another explicit launch after a fresh checkpoint read shows no active or unsupported task. It never retries automatically; read failures and uncertain dispatches stay locked. A newer owner-authorized task frozen after the failed launch finished supersedes its button status; the failed Actions run remains in history.

Buttons show only the task name, without a bottom status row. In-progress tasks add a spinner beside the name, including while waiting for reviews, CI repair checks or PR updates to be confirmed. Cancel also shows it while cancellation awaits confirmation. The spinner becomes a static hourglass when reduced motion is enabled. Completed, failed, cancelled and uncertain operations do not show a busy indicator. Themed tooltips appear on hover or keyboard focus, with status above a short action description. Disabled controls explain why they cannot run, without advertising an unavailable action, and remain focusable for reading that explanation. Press Escape to dismiss a tooltip.

Needed actions are highlighted in yellow/amber. Disabled buttons are dimmed when a task is unavailable or unnecessary. Resolve conflicts uses GitHub's live file-conflict condition, not aggregate mergeability, and is disabled when no file conflicts are confirmed. Fix CI uses GitHub's overall check status for the current PR head, without downloading individual CI checks during refresh. Failure or error enables and highlights it; passing, pending, absent, unknown or stale results disable it. Tooltips report CI passing, pending or failing without check counts. Launches still fetch the complete check collection and apply the detailed latest-execution rules before dispatch. Address Copilot feedback checks unresolved Copilot-rooted threads, including older and outdated threads, and submitted review bodies on the current PR head, using the verified review bot's stable ID rather than its display login. Open feedback uses the amber Address Copilot feedback button. When no feedback remains and all submitted Copilot reviews are on older commits, the same task appears as a normal enabled Refresh Copilot review button. Its tooltip explains that Copilot reviewed an older commit; launching requests a fresh current-head review directly. Otherwise, it is disabled when neither contains feedback. Recognized zero-finding summaries, including informational What changed and resolved-finding sections and Copilot's survey or code-review skill footers, do not count as body feedback; unrecognized nonempty bodies remain actionable. This is not proof of review clearance. Other tasks remain manual actions with their saved run results.

Live action evidence is matched to the PR head and refreshed independently of saved reviewer-dashboard routing. Unknown, unavailable, absent or pending evidence is not treated as passing or no conflicts. Conflict and Copilot-feedback tasks remain runnable with unknown evidence, subject to existing task locks; Fix CI requires a known failure. These three tasks recheck live evidence before dispatch and reject tasks that are now disabled, including CI that has cleared or no longer has a known failure. Once a CI repair stops, Fix CI's tooltip and styling reflect current CI rather than that run's outcome, including failures or exhausted budgets. New live work can replace an older completed button result; saved evidence remains in GitHub.

PR headings show Draft and Approved labels immediately after the linked title, including both when applicable. Draft uses live GitHub status. Approved means the latest valid saved dashboard reports at least one approval from the repository's configured approver team, not necessarily a CODEOWNER for the changed files or clearance to merge. Missing or failed dashboard data shows no Approved label. Not my PRs also shows the author's username on the right; My PRs omits it. Neither view shows routing or dashboard-status pills.

Right-click a PR title or another canvas link and choose Copy link to copy its URL. Left-click still opens GitHub. Press Escape or click outside the menu to dismiss it. Clipboard failures appear in the canvas error notice.

PR cards show workflow status and task errors in button tooltips, and an Open pending review link when a draft GitHub review is ready. Pending reviews are visible only to you until submitted. Buttons use content-sized widths, 14px task text and 10px gaps, with space for a busy indicator only when needed. Labels wrap only between words. Buttons, including Cancel when available, move onto another row when needed.

The canvas uses existing `gh` authentication for target PR/dashboard reads, central Contents/Actions reads and central Actions write access for dispatch. It never reads Actions secrets or uses local credentials to publish to targets. Only the workflow's configured personal owner can launch tasks; the central publisher still requires its separately configured credentials. No local agent sessions or model calls run in the canvas. Refreshes and iteration summaries use no model tokens.

Ask Copilot to inspect GitHub Actions logs and saved checkpoint history when you need diagnostics. The canvas has no detailed troubleshooting view. Output verification checks saved outputs, not review quality or passing tests; completion is not approval.

Workflow run log loads only when you click Load run log. Click Refresh run log for a fresh read; PR refreshes, automatic refreshes and repository switches do not load it. Switching repositories clears the displayed log, and log loading does not block PR refresh or repository selection. It lists PR tasks launched or active in the past 24 hours in the selected repository, independent of the author, reviewer and search filters. Compact rows show the linked PR number and title, task, status, time and any Changes link. Each launch remains separate, including failed and no-change tasks. Titles come from the open-PR list or a cached PR read for closed PRs. A Changes link covers the task's confirmed pushes across all worker passes, from the parent of its first published commit to its last published commit, using `https://github.com/owner/repo/pull/123/changes/<before>..<after>`. No-change passes and unpublished candidates add no commit link. Disconnected push ranges stay separate; a missing range boundary shows the known pushed commit. The log includes archived tasks and survives canvas restarts. Failed reads retain the previous log marked stale.

Task buttons read the exact worker runs recorded by dispatched or running checkpoints to distinguish queued work from running work. Shared polling and unrelated Actions runs are not listed or polled. Failed launches appear in the on-demand run log; ordinary refreshes do not collect a separate failure listing.

Automatic refresh runs every 60 seconds while visible, with cached conditional REST requests and batched live GraphQL status reads for your PRs. It does not refresh the run log. Run-log errors stay local and do not pause PR refresh. Failed data sources, slow steady-state refreshes, low REST or GraphQL capacity and API errors switch it to manual refresh. Manual mode uses the unchecked Auto refresh control without a separate notice. Read errors and automatic pause reasons remain visible without routine load timestamps, account details or request metrics. Individual missing, failed or invalid classifications show warnings without stopping refresh. See [dashboard troubleshooting](docs/setup.md#dashboard-troubleshooting).

## Worker checks and structural verification

This is the real pinned gh-aw Copilot engine running inside Actions/AWF, not GitHub Agent Tasks. The compiler is gh-aw v0.89.21 at `c35393777e5604a63721d09512263b1383301d4f`. The worker and threat detector explicitly pin Copilot CLI 1.0.93, which supports GPT-6.1 Sol, rather than relying on the compiler's older CLI default. Rootless AWF is pinned to 0.28.49, which keeps Chat Completions custom tools separate from Responses tool translation. Action and container pins are immutable.

The worker runs relevant repository checks inside its AWF sandbox for candidate edits and the two review loops, and records commands, exit codes and failures in `diagnostics.txt`. A completed Simplify or Consistency investigation with no qualifying change returns `no_change` without requiring builds or tests of unchanged code. Missing SDKs or optional check failures remain explicit in diagnostics but do not block that result; incomplete investigation or unavailable checks needed to assess a change still block. It returns exactly `candidate.bundle`, `result.json` and `diagnostics.txt`. There is no `validation.json` or command-plan protocol.

One trusted coordinator job verifies and publishes the worker result. It downloads the server-bound artifact, imports the native bundle as data, and checks ancestry, authorship, paths and the semantic result without checking out or executing target code. Acceptance binds every commit/tree/parent, finding mapping, acquired input, request digest, generation, worker run/attempt and trusted workflow revision. Publication reuses the verified Git objects rather than passing a second candidate artifact between jobs. An interrupted continuation revalidates the original worker evidence. PR Description verifies its request-bound proposal and empty bundle without constructing a source tree.

There is no independent test runner or native test receipt. Worker diagnostics are untrusted feedback, not proof of passing tests or coverage. A clean review result does not establish passing target CI.

Workers run on hosted Ubuntu 24.04 with pinned AWF and Java 25 installed before sandbox execution. Gradle uses a fresh writable directory in the dedicated worker home; AWF's JVM proxy settings stay in effect. Not every language or SDK is preinstalled. GitHub, Maven/Gradle, PyPI, npm, NuGet, crates.io and Go proxy domains are explicitly allowed by the firewall. Other dependency endpoints and unsupported setup remain blocked. There is no unrestricted network or environment passthrough, shared credential cache, Docker socket exposure, or automatic credential fallback.

Public Git retrieval is unauthenticated. An optional `REVIEW_LOOP_SOURCE_READ_TOKEN` supplies target/head API reads, never private Git retrieval. Trusted Git-only acquisition creates a frozen snapshot artifact; workers and verifiers receive that bundle, never the credential. Every self-review pass uses this transport with both complete frozen head and merge-base trees. API file-list or diff truncation cannot reduce the review scope. No App is created or registered.

Source acquisition and verification preserve complete frozen Git objects without source byte-size, object-count or file-count caps. Source preserves regular/executable files, symlinks and submodule pointers without following links or fetching submodule repositories in trusted jobs.

Temporary Git repositories retain fetched packs and skip automatic fetch maintenance so no background repack races cleanup. Packing uses one thread to stay within the process memory limit. Missing or invalid source fails explicitly, never as a partial review. Git failures report their exit code and error.

Any target-repository file may be read or changed when the task requires it, including configuration, instructions, binary files, executable files, symlinks and submodule pointers. Conflict resolution can repair semantic incompatibilities in files Git merged without textual conflicts. UTF-8 filenames, including Git-quoted names, have no ASCII-only or 240-character policy limit. Paths must remain relative without traversal, NUL or `.git` metadata components. Target instructions remain untrusted task input, and actual secrets must never enter candidate artifacts. Central trusted runtime source has separate protections. GitHub enforces the selected publisher's actual workflow-file permissions.

## Reviews, CI and state

External-review freezes include full submitted verified Copilot bodies at the exact head and every unresolved original bot root, including older-head threads. Human roots and bot replies in human threads are excluded. Fresh external review decisions require the verified bot identity, a new submitted review after the durable baseline, exact expected SHA, complete paginated body/inline collection and a propagation delay. Unknown overviews cannot establish clean, but do not override verified open findings.

Recognized CCR v2 `0 open findings` summaries and legacy `Findings: None` overviews may recommend approval or human review. Counted resolved sections must link only to independently resolved original verified bot roots. Clean means no remaining findings, not passing CI or approval to merge.

Retained roots do not prevent a bounded continuation for new inline findings or new body-only findings in a complete counted CCR v2 `Previously missed` section. Every unresolved root remains in the worker's frozen findings. Repeated collections and entirely reused roots without new body-only feedback still stop; changing overview wording does not reopen an investigation.

Fix CI selects existing CI check names and status contexts from the target's frozen head. Its watcher collects paginated check runs and latest status updates at the resulting exact SHA. No CI is `none`, an absent selected check is `missing`, active checks are `pending`, failures are `failed`, and unrecognized results are `unknown`. Like GitHub, completed `success`, `skipped` and `neutral` checks are nonblocking. Pending CI waits; missing, absent or unknown results block; known failures proceed to diagnosis. Superseded Actions executions do not override the newest run number for each workflow and event. Matching check names in independent workflows or events require verified run, check-suite and job identities, with every current check nonblocking. Reruns use current-attempt jobs and can retain proven nonblocking jobs from an earlier attempt. Ambiguous same-execution duplicates and unbound checks remain `unknown`. Other tasks neither collect target CI for completion nor wait for it.

Source-based independent tasks freeze complete GitHub PR diffs without byte-size, file-count or added-line caps. CI repair uses the newest exact-head run number for each Actions workflow and event; older executions do not override it, and independent executions still have to pass. Commit-status contexts use GitHub's combined-status endpoint, which returns the latest update per context. CI repair also accepts proven nonblocking jobs retained across failed-job retries.

CI diagnosis collects complete failed-step log windows, excluding later cleanup outside those windows. Jobs without failed-step timestamps supply their complete logs. Evidence has no byte budget; the worker selects relevant quotations for each diagnosis. Denied, missing or expired log downloads are recorded as unavailable, including failures from signed log storage, and retain any check output. Insufficient evidence still requires an unknown diagnosis, not CI clearance.

Copilot feedback includes every unresolved verified finding and its complete conversation, without finding-count or body-length caps. Workers return concise explanations rather than mandatory upside/downside sections. GitHub's text limits and API response, artifact, process-resource and runtime limits still apply.

All fresh requests use `git-candidate-v1` and schema 2. Workers supply native Git history and only task-specific semantic claims; external results account for every frozen finding exactly once. Bundles have no byte-size, changed-file or changed-line caps, including incoming base changes in merge candidates. Up to 100 linear candidate commits are supported, or one two-parent conflict-resolution commit. Artifact downloads are bound to GitHub's recorded size and digest. Checkpoints retain the `pr-v2-<actual-base-repository-ID>-<PR>.json` namespace, and `launch_run` binds requests to the actual owner dispatch run/attempt.

Existing `reviewable-v1` phases continue at their own pinned runtime; current code routes them but never adapts or publishes their old candidates. Other earlier protocols remain read-only. A fresh launch archives a terminal prior checkpoint only after proving bound executions have stopped and effect intents are settled. Unknown old effects block launch. The [historical exhausted pilot](docs/legacy-pilot.md#historical-central-qualification) stays stopped.

One shared Actions waiter polls all active authorized PRs, including phases launched while it is running. Ten PRs can run independently without ten waiting runners. The waiter dispatches short per-PR coordinator runs when reviews, CI or workers are ready; model workers and structural verification pipelines run concurrently across PRs. It never publishes or receives inference/publisher credentials.

The waiter starts on coordinator/worker activity, exits when no active phases remain and hands off after 55 minutes. A global concurrency group permits at most one running waiter; another can queue without occupying a runner. Its five-minute cron is only a best-effort recovery trigger, not the mechanism for normal progress.

One workflow kind can be active per PR. All kinds share the worker, coordinator, publisher, state store and global waiter. A loop phase keeps its identity, consumed pipeline count and publication history while each new head receives a new request digest and worker invocation. Review and CI phases allow five total model pipelines, including failed admitted work; single-pass tasks allow one, regardless of elapsed time or batch count. The sixth pipeline never dispatches. Per-PR ownership, non-force state CAS, cancellation checks, exact fresh source checks and uncertain-effect reconciliation remain in place.

State is compact JSON only, with no file-count, per-checkpoint size or total storage cap. Large candidate bundles and logs stay in bound Actions artifacts. See [setup and recovery](docs/setup.md) for credentials and operation details.

After an acknowledged state write, reads wait up to five attempts for that commit or a descendant to become visible. Older API ref results cannot invalidate the saved transition; newer cancellation or concurrent updates still take effect. Persistent lag or divergent history stops explicitly.

## Development

Use the tools-only Linux Docker runner on Windows. It runs real Git/Python unit tests in non-root tmpfs with no network, mounts, secrets or target repository dependency.

```bash
python tools/test_docker.py tests.test_generic
python tools/test_docker.py
```

The first image build needs network; warm runs reuse it. Python 3.12 and Git 2.43 or newer can also run `python -m unittest discover -q` directly on Linux.

The canvas has dependency-free Node tests:

```bash
node --test .github/extensions/workflow-dashboard/dashboard.test.mjs
```

For workflow or prompt changes, run the real pinned compiler, linter, zero-write audit and lock reproducibility check:

```bash
python tools/compiler.py
python tools/lint.py
python tools/audit_lock.py
```

Everyday CI runs these static/offline checks only. There is no standalone qualification workflow or Gradle/container smoke prerequisite. A live PR/E2E run requires a concrete PR and explicit user authorization.
