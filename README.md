# PR workflows

Review and fix explicitly selected PRs. The default `loop_kind=copilot_review` investigates existing verified Copilot findings. `loop_kind=self_review` uses one fresh worker per pass to review the complete PR diff and fix warranted problems. Both read repository instructions, format changes and run appropriate existing checks. There is no repository or language allowlist or per-repository adapter.

The scope stays personal and opt-in. Only a fresh owner dispatch can launch work. Source-changing tasks and description require that owner's open PR, including Copilot PRs GitHub attributes to the owner; PR Reviewer can review the owner's or another author's open PR, including bot-authored PRs. The watcher advances existing authorized checkpoints; it does not discover PRs or scan repositories. Both existing loops publish warranted fixes. Copilot review always replies to and resolves eligible original Copilot threads before requesting fresh review; no other kind posts replies or resolves threads. There are no preview/shadow modes or reply switches.

## Independent tasks

All eight kinds share `coordinator.yml`, one artifact-only worker per pass, structural verification, credential routing, durable intents, checkpoint storage and the shared waiter. Select one `loop_kind` per launch. They do not form a combined pipeline.

| Display name | `loop_kind` | Completion |
| --- | --- | --- |
| Address Copilot feedback | `copilot_review` | Existing findings, warranted fixes, mandatory bot-thread handling and fresh external review until clean with passing exact-head CI |
| Review and fix | `self_review` | Full-PR review/fix passes until an explicit later clean pass with passing exact-head CI |
| Resolve conflicts | `pr_conflict_resolver` | One merge of the frozen live base into the frozen PR head, with head first and base second. Never lands the PR. An incorporated base uses no worker or empty commit |
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

Replace `pr_description` with any kind in the table. Source-changing single-pass tasks record completion and publication separately from CI. Pending CI uses the waiter; failed, absent or unknown CI remains explicit and does not erase a completed task. They never claim the existing loops' clean-review guarantee. Description and pending-review completion do not require green CI. No qualifying fixes/findings means no invented commit/review. An existing viewer-owned pending review is preserved and blocks a second review.

Tasks other than Copilot Review and PR Description freeze the live base and complete source trees. The five source-based independent tasks additionally freeze the authoritative GitHub PR diff. Unavailable, truncated, binary or oversized scope stops explicitly. Their diff limit is 200,000 UTF-8 bytes with at most 1,000 files and 10,000 added-line anchors. CI freezes run/attempt/check/log evidence. Conflict source acquisition deepens until both frozen tips reach the merge base, without a commit-count limit. Side-branch cuts and shallow boundaries are bound to the source manifest and restored on import. Contradictory intent blocks instead of choosing a side.

PR Description freezes the original title/body, PR head and GitHub diff directly. Binary-file markers are retained as input. It needs no separate changed-file inventory, added-line anchors, base-tip freeze, source bundle, repository checkout or Git-tree reconstruction. The source-task diff limits do not apply; the shared API response and checkpoint storage bounds still do. Before editing metadata, the publisher rechecks the exact PR head, diff and original title/body. A changed base tip alone does not stop it when the actual diff is unchanged. Worker artifacts and title/body-only publication still require trusted verification.

## Launch

Use a full GitHub PR URL or `owner/repo#number`. Bare numbers have no default repository.

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f publication_auth=fine_grained_pat -f target=owner/repository#123
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=status -f target=owner/repository#123
```

Launch requires the isolated `COPILOT_GITHUB_TOKEN` inference credential and explicit publisher authorization. Repository access never borrows inference auth or local CLI credentials. Missing private read access creates an explicit blocked checkpoint, not a language or allowlist error.

Once a run finishes, use Run again with the same inputs. A terminal, verified quiescent run permits a new phase of any kind, even at the same head. The coordinator verifies prior executions have stopped and archives their evidence. Review and CI loops allow five workers; single-pass tasks allow one. All have a two-hour deadline. Ordinary reruns need no copied checkpoint IDs. Active phases cannot reset budgets or switch kinds; uncertain effects require reconciliation.

New phases pin the automation commit their launch ran on. Workers and coordinator continuations use a verified `review-loop-revisions/<commit-sha>` branch, and verification/publication check out that same commit. Updating central `main` does not stop these phases or change their code. New launches use the revision selected by their Actions dispatch. To pick up an automation fix in an active phase, cancel it and launch again after it stops.

Revision branches are shared by phases on the same commit, retained for continuation and provenance, and never updated by the automation. A missing or changed revision branch stops affected work without falling back to `main`. Older phases without a saved pin retain their existing revision-change guard; blocked historical phases are not resumed automatically.

A failed push at the unchanged original head can start a fresh phase with `restart_unpublished=true` and the exact `previous_request` and `previous_generation`. Wait fifteen minutes after the publisher stops. The coordinator checks both live refs and all bound executions, archives the old intent without marking it confirmed, and freezes new work. It never retries the old candidate. See [unpublished restart](docs/setup.md#restarting-after-an-unpublished-push) for the command and restrictions.

The publisher authenticates as the launch owner. Source effects require actual frozen head push access plus PR read access. Description and pending reviews require base-repository PR access only, never fork push access.

The default loop starts from unresolved comments in existing submitted, verified Copilot reviews, including older reviews and outdated threads. It also investigates nonempty review bodies at the current PR head. It does not request an initial review or probe Copilot permissions. After publication and thread handling it requests a fresh review; a rejected request stops the loop with the publication evidence retained.

Self-review needs no existing Copilot review and never requests one:

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=launch -f loop_kind=self_review \
  -f publication_auth=fine_grained_pat -f target=owner/repository#123
```

Each self-review pass reviews the full frozen merge-base-to-head diff and fixes only concrete problems introduced or directly affected by the PR. A changed pass publishes its exact accepted candidate and starts a fresh full-PR pass. It cannot also claim clean. Clean requires a later explicit no-change result, structural verification and passing exact-head target CI. Fifth-pass fixes remain published but exhaust the phase without a later clean pass. Head drift or any base ref/tip change stops the phase; a fresh launch can review the new base. There is no automatic merge, rebase or CI repair.

Publisher tokens are selected by the repository where the effect occurs. Source pushes select the head repository owner; title/body updates, pending reviews and failed-jobs reruns select the base repository owner. CI repair needs Actions read for evidence, and its selected rerun credential needs base Actions write. Set the shared repository variable `PUBLISHER_SECRETS` to an owner-to-secret JSON mapping, then store each fine-grained PAT in its named Actions secret:

```json
{
  "trask": "TEST_PUBLISH_TOKEN",
  "open-telemetry": "OPENTELEMETRY_PUBLISH_TOKEN"
}
```

The variable and secret names are reusable by other workflows in this repository. Personal and organization tasks can run concurrently with separate credentials. Owners match case-insensitively; secret names must be uppercase, start with a letter and end in `_PUBLISH_TOKEN`. Only the selected token reaches the trusted publisher. Fork source pushes select the head owner's token, which must also have target PR read access; report-only tasks select the base owner's token. Missing mappings, missing secrets or denied permissions block without trying another token.

Without the variable, only effects in repositories owned by the central owner select `TEST_PUBLISH_TOKEN`. An explicit mapping replaces that default. The personal test token must stay scoped to the test repository; an organization entry needs its own token and any required organization approval. Live identity, target/head access and isolation from central private contents are still checked. The loop neither provisions credentials nor changes their permissions.

Published commits use the launch owner's verified GitHub account as both author and committer, with the account's numeric-ID noreply email. This identity is frozen from GitHub PR metadata, bound to the candidate before verification and checked against the authenticated publisher. For Copilot-authored PRs, the owner's account is verified through GitHub's author search and frozen separately from the bot author. The commit date is the freeze time and Copilot remains credited in the co-author trailer. Push credentials alone do not determine Git commit authorship.

Only description can update title/body, and only conflict resolution can publish a two-parent base update. Landing PRs, changing draft state, forced pushes, top-level comments, submitted reviews and human-rooted thread replies remain outside the protocol.

## Reviewable commits and thread handling

One worker iteration can produce several ordered root-cause commits. Related findings share a commit; unrelated causes remain separate, including sequential edits to the same file. Copilot-review messages use this format, repeating the original-comment block for each associated finding:

```text
Address Copilot review comment: Reject stale snapshot generations

Copilot comment:

<verbatim frozen original comment>

Analysis: Reject a non-snapshot head with a snapshot ancestor.

Upsides: Prevents accepting stale state.

Downsides: Requires an ancestry read.

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
```

Self-review uses an issue-specific subject and Analysis/Upsides/Downsides without fabricated Copilot comments. Both use the frozen owner identity/date and co-author trailer.

Only the trusted publisher handles threads. Fixed replies start `Addressed in <batch sha>.`; supported no-code decisions start `No code change.` Both include analysis and tradeoffs, without repeating the original comment. No-code decisions push no empty commit. Body-only findings receive no invented inline or top-level comment.

The publisher confirms the exact branch ref and PR head before replying, confirms the reply before resolving, and confirms resolution before requesting fresh review. Human intervention or changed conversation prevents automatic handling of that thread and records the reason. Already resolved threads are explicitly skipped. An outdated or already-addressed comment still gets an explanatory reply and resolution; a fixed thread gets its mapped-commit reply. An outdated thread alone does not establish review clearance.

Every effect has a durable intent. Lost replies can reconcile only against a unique exact body/actor/root marker match. A lost resolution response without a saved matching acknowledgment remains uncertain even if the thread is now resolved. The loop never blindly retries mutations, discards an uncertain intent, or treats thread resolution as review clearance.

## PR workflows canvas

Open a Copilot app session in this repository and ask to "open the PR workflows canvas." The existing `workflow-dashboard` canvas ID opens a single PR list with workflow controls. The repository dropdown defaults to `open-telemetry/opentelemetry-java-instrumentation` and also includes `open-telemetry/semantic-conventions-conformance` and `open-telemetry/shared-workflows`. Extend the list in `.github/extensions/workflow-dashboard/repositories.mjs`.

Switch between My PRs and Not my PRs, using the account authenticated in `gh`. The default is My PRs, which includes your directly authored PRs and GitHub's `gh pr list --author @me` results, including Copilot PRs created on your behalf. Not my PRs shows the remaining open PRs. Both include drafts and PRs without saved workflow or dashboard data. Combine either view with Waiting on reviewers and title/number/author search. Waiting on reviewers uses the OpenTelemetry dashboard's latest saved `route: "approver"` directly from `otelbot/pull-request-dashboard-state/<repository-name>` in `open-telemetry/shared-workflows`, not GitHub review requests. Dashboard classifications and facts are used without comparing their saved author, head commit or draft status with the live PR. Ownership, task permissions and draft filtering use live GitHub data. Missing, failed or invalid classifications remain explicit and do not match that filter.

My PRs shows all eight task buttons. Copilot PRs created on your behalf have the same task eligibility as your directly authored PRs. Ownership is rechecked before dispatch and by the central workflow; source publication still requires actual head-repository push access. Not my PRs shows only Draft review, including for bot-authored PRs; owner-only tasks are hidden. Eligible tasks can run; unavailable tasks stay disabled with an explanation. The active task is highlighted with its recorded status, including dispatching, starting, queued, running, waiting, completion or failure. Dispatch acceptance is not shown as a running worker, and unavailable status is not shown as success. Run and task cancellation dispatch `coordinator.yml` on central `main` immediately on click, without a confirmation dialog. Task effects are shown in button tooltips. These are real publishing tasks, not previews. Cancel task remains available in either view when authorized and requires the exact displayed request and generation; it does not undo publication or guarantee termination of an already authorized effect. Duplicate or uncertain dispatches stay locked until checkpoint evidence confirms them. Recovery commands remain CLI-only.

Cancel launch is available while an accepted launch is awaiting task confirmation. It uses the exact run ID returned by GitHub, never a guessed latest run. A saved task from that launch uses normal task cancellation; otherwise the launch run itself is cancelled. If its task appears during cancellation, the canvas also requests cancellation of that exact task. Cancellation is checked once a minute while the canvas is visible, even in manual refresh mode, until confirmed or a read/rate-limit error stops polling. The Auto refresh checkbox stays unchanged. Failed launches stop spinning and link to their Actions run, without silently unlocking another launch. A newer owner-authorized task frozen after the failed launch finished supersedes its button status; the failed Actions run remains in history.

Buttons show only the task name, without a bottom status row. Busy tasks add a spinner beside the name; running tasks use blue styling. The spinner becomes a static hourglass when reduced motion is enabled. Status explanations remain in hover tooltips and accessible labels.

Needed actions are highlighted in yellow/amber. Disabled buttons are dimmed when a fix is confirmed unnecessary. Resolve conflicts uses GitHub's live file-conflict condition, not aggregate mergeability, and is disabled when no file conflicts are confirmed. Fix CI is highlighted for failing current-head checks and disabled when CI is confirmed passing, with no pending checks. Address Copilot feedback is highlighted for all unresolved Copilot-rooted threads, identified by the verified review bot's stable ID rather than its display login; no open threads alone does not establish review clearance. Other tasks remain manual actions with their saved run results.

Live action evidence is matched to the PR head and refreshed independently of saved reviewer-dashboard routing. Unknown, unavailable, absent or pending evidence is not treated as passing or no conflicts. Eligible fixes remain runnable in those states, subject to existing task locks. Conflict and CI launches recheck live evidence before dispatch and reject fixes that are now confirmed unnecessary. New live work can replace an older completed button result without removing its saved run details.

PR headings show the linked title with the author's username on the right in Not my PRs. My PRs omits the author. Neither view shows routing, dashboard-status or draft pills. PR cards show workflow status in the task buttons and a short completion result below them. Draft review shows No findings when it generated no new comments, or Review ready with a comment count and link to the pending GitHub review. Pending reviews are visible only to you until submitted. Results from an older PR commit are marked explicitly. Buttons use content-sized widths, 14px task text and 10px gaps, with space for a busy indicator only when needed. Buttons, including Cancel when available, stay in one row and shrink on narrower panels, wrapping their labels instead.

The canvas uses existing `gh` authentication for target PR/dashboard reads, central Contents/Actions reads and central Actions write access for dispatch. It never reads Actions secrets or uses local credentials to publish to targets. Only the workflow's configured personal owner can launch tasks; the central publisher still requires its separately configured credentials. No local agent sessions or model calls run in the canvas. Refreshes and iteration summaries use no model tokens.

Expand Troubleshooting at the bottom of the page for launch messages and links, task-blocking explanations, and active or recent runs in the selected repository, including runs for closed or filtered-out PRs. Expand a run's Run details for commit, timing, logs, previous runs, published changes, review comments and other task evidence. Downloads contains saved artifacts. Output verification checks the saved outputs, not review quality or passing tests; completion is not approval. Missing evidence stays explicit, legacy records stay historical, and unpublished candidates are not shown as published fixes. No-change passes have no new commit.

Troubleshooting is collapsed by default and also shows up to 20 failed launches across all repositories, including launches that failed before saving a task checkpoint. Task buttons read the exact worker runs recorded by dispatched or running checkpoints to distinguish queued work from running work. Shared polling and unrelated Actions runs are not listed or polled.

Automatic refresh runs every 60 seconds while visible, with cached conditional REST requests, batched live GraphQL status reads for your PRs, and on-demand history. Failed data sources, slow steady-state refreshes, low REST or GraphQL capacity and API errors switch it to manual refresh. Manual mode uses the unchecked Auto refresh control without a separate notice. Read errors and automatic pause reasons remain visible without routine load timestamps, account details or request metrics. Individual missing, failed or invalid classifications show warnings without stopping refresh. See [dashboard troubleshooting](docs/setup.md#dashboard-troubleshooting).

## Worker checks and structural verification

This is the real pinned gh-aw Copilot engine running inside Actions/AWF, not GitHub Agent Tasks. The compiler is gh-aw v0.89.21 at `c35393777e5604a63721d09512263b1383301d4f`. The lock uses Copilot CLI 1.0.87 and rootless AWF 0.28.23, with immutable Action and container pins.

The worker runs relevant repository checks inside its AWF sandbox and records commands, exit codes and failures in `diagnostics.txt`. It returns exactly `candidate.patch`, `result.json` and `diagnostics.txt`. There is no `validation.json` or command-plan protocol.

The trusted verifier reconstructs linear fix batches or a real two-parent merge from Git objects without checking out or executing target code. Report-only results have no manufactured commits. It checks every intermediate change and the cumulative diff. The publisher downloads fresh server-bound worker/verifier artifacts and reconstructs the entire chain again. Acceptance binds patch spans/hashes, every commit/tree/parent, finding mappings, source bundle, request digest, generation, worker run/attempt and trusted workflow revision. PR Description instead verifies the request-bound proposal and empty patch/package without importing source or constructing a Git tree.

There is no independent test runner or native test receipt. Worker diagnostics are untrusted feedback, not proof of passing tests or coverage. Required target CI at the resulting exact head remains a separate clean-completion gate.

Workers run on hosted Ubuntu 24.04 with pinned AWF and its available tools. Not every language or SDK is preinstalled. GitHub, Maven/Gradle, PyPI, npm, NuGet, crates.io and Go proxy domains are explicitly allowed by the firewall. Other dependency endpoints and unsupported setup remain blocked. There is no unrestricted network or environment passthrough, shared credential cache, Docker socket exposure, or automatic credential fallback.

Public Git retrieval is unauthenticated. An optional `REVIEW_LOOP_SOURCE_READ_TOKEN` supplies target/head API reads, never private Git retrieval. Trusted Git-only acquisition creates a frozen snapshot artifact; workers and verifiers receive that bundle, never the credential. Every self-review pass uses this transport with both complete frozen head and merge-base trees. API file-list or diff truncation cannot reduce the review scope. No App is created or registered.

Source acquisition, verification and publisher reconstruction preserve complete frozen Git objects without source byte-size, object-count or file-count caps. Source preserves regular/executable files, symlinks and submodule pointers without following links or fetching submodule repositories in trusted jobs.

Temporary Git repositories retain fetched packs and skip automatic fetch maintenance so no background repack races cleanup. Packing uses one thread to stay within the process memory limit. Missing or invalid source fails explicitly, never as a partial review. Git failures report their exit code and error.

Repository configuration and instruction files, binary files, executable files, symlinks and submodule pointers are valid source and edits. UTF-8 filenames, including Git-quoted names, have no ASCII-only or 240-character policy limit. Paths must remain relative without traversal, NUL or `.git` metadata components. Target instructions remain untrusted task input, and actual secrets must never enter candidate artifacts. Central trusted runtime source has separate protections. GitHub enforces the selected publisher's actual workflow-file permissions.

## Reviews, CI and state

External-review freezes include full submitted verified Copilot bodies and unresolved original bot roots at the exact head. Human roots and bot replies in human threads are excluded. Fresh external review decisions require the verified bot identity, a new submitted review after the durable baseline, exact expected SHA, complete paginated body/inline collection and a propagation delay. Unknown review bodies fail closed.

Recognized CCR v2 `Findings: None` overviews may recommend approval or human review. Clean means no remaining findings and passing exact-head CI, not approval to merge.

Retained roots do not prevent a bounded continuation for new inline findings or new body-only findings in a complete counted CCR v2 `Previously missed` section. Every unresolved root remains in the worker's frozen findings. Repeated collections and entirely reused roots without new body-only feedback still stop; changing overview wording does not reopen an investigation.

The source workflows select existing non-Copilot CI check names and status contexts from the target's frozen head. The watcher collects paginated check runs and statuses at the resulting exact SHA. No CI is `none`, an absent selected check is `missing`, active checks are `pending`, failures are `failed`, and unrecognized results are `unknown`. Matching check names can come from separate GitHub Actions workflows; the watcher verifies their run, check-suite and job identities and requires every check to pass. Reruns, duplicated jobs in one workflow and duplicate status contexts remain `unknown`. Copilot is not target CI. Clean requires both an explicit review result appropriate to the loop kind and successful selected target CI.

Source-based independent tasks freeze complete GitHub PR diffs without byte-size, file-count or added-line caps. CI repair uses the newest exact-head run number for each Actions workflow; older executions do not override it, and distinct workflows still have to pass independently. Commit-status contexts use GitHub's combined-status endpoint, which returns the latest update per context. Like GitHub, CI repair accepts completed `success`, `skipped` and `neutral` checks as nonblocking, including proven jobs retained across failed-job retries. Review-loop clearance remains success-only.

CI diagnosis collects complete failed-step log windows, excluding later cleanup outside those windows. Jobs without failed-step timestamps supply their complete logs. Evidence has no byte budget; the worker selects relevant quotations for each diagnosis. Denied, missing or expired log downloads are recorded as unavailable, including failures from signed log storage, and retain any check output. Insufficient evidence still requires an unknown diagnosis, not CI clearance.

Copilot feedback includes every unresolved verified finding and its complete conversation, without finding-count or body-length caps. Output prose has no custom length caps; workers are instructed to write concise explanations, and GitHub enforces its own text limits. Summaries remain single-line and analysis, upsides and downsides remain required. Checkpoint, artifact, process-resource and runtime limits still apply.

All fresh requests use the single `reviewable-v1` protocol. Requests, state, verifier reports, manifests, and worker results use schema 2. Worker results include ordered patch spans and messages; external results account for every frozen finding exactly once. Candidate patches have no byte-size, changed-file or changed-line caps; this includes clean incoming changes when merging the frozen base into a PR. Up to 100 ordered code batches are supported. Candidate artifacts use native Actions uploads, and downloads are bound to GitHub's recorded artifact size and digest rather than a fixed size ceiling. Unknown protocols and old artifacts cannot execute. Checkpoints retain the `pr-v2-<actual-base-repository-ID>-<PR>.json` storage namespace; an unfrozen access gate uses a repository-name digest. `launch_run` binds requests to the actual owner dispatch run/attempt.

Earlier protocols are historical and read-only. There is no execution compatibility or in-place migration. A fresh launch archives a terminal prior checkpoint only after proving bound executions have stopped and effect intents are settled. Unknown old effects block launch. The [historical exhausted pilot](docs/legacy-pilot.md#historical-central-qualification) stays stopped.

One shared Actions waiter polls all active authorized PRs, including phases launched while it is running. Ten PRs can run independently without ten waiting runners. The waiter dispatches short per-PR coordinator runs when reviews, CI or workers are ready; model workers and structural verification pipelines run concurrently across PRs. It never publishes or receives inference/publisher credentials.

The waiter starts on coordinator/worker activity, exits when no active phases remain and hands off after 55 minutes. A global concurrency group permits at most one running waiter; another can queue without occupying a runner. Its five-minute cron is only a best-effort recovery trigger, not the mechanism for normal progress.

One workflow kind can be active per PR. All kinds share the worker, coordinator, publisher, state store and global waiter. A loop phase keeps its identity, consumed pipeline count, deadline and publication history while each new head receives a new request digest and worker invocation. Review and CI phases allow five total model pipelines, including failed admitted work; single-pass tasks allow one, all within two hours regardless of batch count. The sixth pipeline never dispatches. Per-PR ownership, non-force state CAS, cancellation checks, exact fresh source checks and uncertain-effect reconciliation remain in place.

State is compact JSON only, capped before writes at 1,000 files, 1 MiB per file and 16 MiB total. Large candidate bundles and logs stay in bound Actions artifacts. See [setup and recovery](docs/setup.md) for credentials and operation details.

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
