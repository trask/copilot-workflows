# Workflow budgets and deadlines

Task phases have no overall elapsed-time deadline. Fix CI's CI waits and Copilot-review waits do not consume a time budget, but worker counts, individual execution timeouts, reconciliation windows and artifact expiration still limit progress. Other tasks do not wait for target CI.

This inventory covers repository-defined limits, including the generated worker configuration. GitHub platform limits and the target repository's CI timeouts apply separately. Each phase uses its pinned automation revision, so an older phase can have different limits.

## Task-level budgets

| Scope | Limit |
| --- | --- |
| Address Copilot feedback, Review and fix, Fix CI | 5 worker passes total, including the first and failed admitted passes |
| Resolve conflicts, Update title & description, Simplify code, Draft review, Align with existing code | 1 worker pass |
| CI failed-jobs rerun | 1 rerun per upstream Actions run; an existing manual rerun consumes the allowance |
| Active work | 1 task phase per PR; different PRs can run concurrently |
| Overall elapsed time, confirmed CI waiting, confirmed review waiting | No deadline |

Later review/fix and CI-repair passes preserve the phase's consumed worker count. Waiting does not reset that count.

Sources: [policy](../loop/policy.py), [live continuation](../loop/live.py), [task effects](../loop/task_effects.py).

## Model budgets per worker

| Component | Limit |
| --- | --- |
| Main Copilot execution | 20 minutes |
| Main inference requests | `max-turns: 100`, compiled into AWF `maxRuns: 100` |
| Main AI-credit budget | 1,000 AI credits by default |
| Threat-detection execution | 10 minutes |
| Threat-detection inference requests | 500 |
| Threat-detection AI-credit budget | 400 AI credits by default |
| AWF cache-miss budget | 5, separately configured for each inference proxy |

Repository variables `GH_AW_DEFAULT_MAX_AI_CREDITS` and `GH_AW_DEFAULT_DETECTION_MAX_AI_CREDITS` override the credit defaults. Read those variables to determine the effective deployed budgets. Invalid noninteger credit values fall back to the defaults.

These are per-execution budgets, not one shared phase-level credit budget. The AWF proxy's `maxRuns` is an inference-request limit, not the number of worker passes or Actions runs.

Sources: [worker source](../.github/workflows/copilot-worker.md), [compiled worker](../.github/workflows/copilot-worker.lock.yml).

## Actions job timeouts

| Job or step | Timeout |
| --- | --- |
| Coordinator `coordinate` | 10 minutes |
| Structural verifier `verify` | 10 minutes |
| Verification finalizer `finalize` | 5 minutes |
| Trusted publisher/review watcher `personal_live` | 10 minutes |
| Worker `agent`, including setup and post-processing | 30 minutes |
| Copilot CLI step inside `agent` | 20 minutes |
| Threat detector job and detection step | 10 minutes |
| Generated safe-output job | 45 minutes |
| Shared waiter job | 65 minutes |
| Repository validation job | 15 minutes |

Generated pre-activation, activation and conclusion jobs have no explicit timeout in this repository; GitHub's platform default applies. The 30-minute agent-job timeout is not a timeout for the entire multi-job worker workflow.

Workers are serialized per repository/PR without cancelling in-progress work. The shared waiter permits one running job globally, with a queued successor. Coordinator work is serialized by request/target/run.

Sources: [coordinator](../.github/workflows/coordinator.yml), [compiled worker](../.github/workflows/copilot-worker.lock.yml), [waiter](../.github/workflows/waiter.yml), [validation](../.github/workflows/validate.yml).

## Waiting, propagation and handoff windows

These are operational windows, not overall task deadlines.

| Operation | Window or cadence |
| --- | --- |
| Shared waiter | Runs for 55 minutes, then hands off to another waiter |
| Checkpoint polling | Every 60 seconds |
| Unchanged individual PR readiness checks | At most every 5 minutes |
| Scheduled waiter recovery | Every 5 minutes, best effort |
| Fresh Copilot review propagation | Wait 2 minutes after submission |
| Worker dispatch absent from Actions listing | Block after 15 minutes rather than dispatch again |
| Staged-source acquisition | Block after 15 minutes from freeze; applies to the older staged transport, not fresh direct-input launches |
| Unconfirmed Copilot review-request acknowledgment | Block after 15 minutes |
| Unconfirmed description, pending-review or CI-rerun effect | Block after 15 minutes |
| Pushed branch/PR-head propagation | Allow 15 minutes for convergence |
| Explicit restart after an unpublished push | Require 15 minutes since the original publisher stopped |
| Publisher processing in one invocation | Yield after 5 minutes or 310 transitions, checked between transitions |

Once a review request is confirmed, the review itself can take longer. Pending upstream CI can also take longer. Unconfirmed thread replies or resolutions block without an automatic mutation retry.

Sources: [waiter](../loop/waiter.py), [coordinator](../loop/coordinator.py), [live continuation](../loop/live.py), [task effects](../loop/task_effects.py), [thread effects](../loop/effects.py).

## API, Git and retry limits

| Operation | Limit |
| --- | --- |
| Python GitHub HTTP operations | 60-second timeout per request, shortened by the waiter's remaining execution window |
| Streaming signed log/file download | Additional 60-second elapsed download window |
| Transient REST GET or signed-download failure | 3 attempts total, with 1-second and 2-second backoffs |
| Mutation requests, including GraphQL POSTs | No automatic retry |
| Newly created revision-pin visibility | 3 confirmation attempts, with 1-second and 2-second backoffs |
| State-write contention | 20 attempts, randomized backoff capped at 1 second |
| Acknowledged state-write visibility | 5 attempts, waiting 1, 2, 3 and 4 seconds |
| Waiter rate-limit recovery | Wait until the reported reset, with a minimum 60-second pause; defer to scheduled recovery if it cannot fit in the current waiter window |
| Source-fetch and verifier Git subprocesses | 180 seconds per command |
| Trusted Git push | 120 seconds |
| Source/verifier Git processes on Linux | 60 CPU seconds and 512 MiB address space per process |

Read retries apply to designated transient server and transport failures. Certificate verification and permission failures are not retried. Git resource limits constrain individual trusted subprocesses, not arbitrary worker builds as a whole.

Sources: [API client](../loop/api.py), [revision pins](../loop/revisions.py), [state store](../loop/state.py), [source acquisition](../loop/source.py), [verifier](../loop/verify.py), [publisher](../loop/publication.py), [waiter](../loop/waiter.py).

## Artifact and data budgets

| Item | Limit |
| --- | --- |
| Candidate, source and verification evidence retention | 14 days |
| Publisher artifact acceptance | Must be unexpired; advertised retention must be no more than 15 days |
| Some generated worker transient artifacts | Explicitly 1 or 3 days; other generated artifacts use repository defaults |
| Worker `result.json` | 256,000 bytes |
| Worker `diagnostics.txt` | 4 MiB |
| Trusted verification report | 1 MiB |
| Staged-source manifest | 16 KiB |
| gh-aw safe-output artifact uploader | 1 upload, maximum 100 MiB |
| Ordinary Python API response | 16 MiB by default |
| REST pagination | 100 pages / 10,000 items |
| Review-thread pagination | 100 pages, with 100 threads per page |
| CI collection | 1,000 checks/statuses, 100 selected check names and 100 workflow executions |
| Selected CI check name | 200 characters |
| Root-cause commit batches | 100 per worker result |
| Draft-review comments / consistency entries | 100 each |
| CI evidence quotations | Up to 5 per diagnosis |
| Consistency citations | Up to 10 per entry |
| State GraphQL read batches | 100 blobs, targeted at 2 MiB source content per batch |
| Git-push captured output | 1 MiB |

Artifact expiration can block a phase even without an overall deadline.

The controller has no cap on state-file count, individual checkpoint size or total checkpoint storage. Some diff/log downloads, candidate patches and source bundles also have no independent byte cap, although job, process, transport and upload limits still apply. The safe-output uploader's limit does not impose a fixed ceiling on candidate artifacts uploaded through native Actions steps.

Sources: [worker source](../.github/workflows/copilot-worker.md), [compiled worker](../.github/workflows/copilot-worker.lock.yml), [worker output](../loop/worker_output.py), [API client](../loop/api.py), [review freeze](../loop/freeze.py), [CI evidence](../loop/ci.py), [recommendations](../loop/recommendations.py), [verifier](../loop/verify.py), [verification CLI](../loop/cli.py), [source acquisition](../loop/source.py), [publisher](../loop/publication.py), [state store](../loop/state.py).

## Dashboard limits

These affect launching and observing workflows, not the worker's task budget.

| Item | Limit |
| --- | --- |
| Each `gh` subprocess | 60 seconds |
| Concurrent GitHub reads | 3 |
| Transient read retry | 1 retry, after 250 milliseconds |
| Automatic refresh / cancellation observation | Approximately every 60 seconds while visible |
| Visibility lease | 45 seconds, renewed by a 15-second heartbeat |
| Automatic refresh pauses | Below 10% remaining REST or GraphQL capacity, after a failed read, or after a warm refresh exceeds 10 seconds |
| Rate-limit recovery | Reported reset or Retry-After; fallback pause of 60 seconds |
| Loopback HTTP request/header timeout | 10 seconds |
| Action request body | 4 KiB |
| API response / aggregate open-PR listing | 16 MiB |
| Open-PR listing | 100 pages / 10,000 PRs |
| Saved reviewer-dashboard file | 1 MiB, maximum 10,000 PR records |
| Cached read responses | 100 entries |
| Failed-launch listing | 20 runs per read |
| Workflow run log | Tasks launched or active in the past 24 hours; coordinator runs read in pages of 100 back to the cutoff |

Sources: [GitHub client](../.github/extensions/workflow-dashboard/github.mjs), [workflow dashboard](../.github/extensions/workflow-dashboard/dashboard.mjs), [PR dashboard](../.github/extensions/workflow-dashboard/pr-dashboard.mjs), [state reader](../.github/extensions/workflow-dashboard/state.mjs), [loopback server](../.github/extensions/workflow-dashboard/server.mjs), [browser app](../.github/extensions/workflow-dashboard/app.mjs).

## Development-tool limits

| Tool | Limit |
| --- | --- |
| Docker test run | 5 minutes by default, configurable with `--timeout` |
| Docker helper commands / first image build | 30 seconds / 10 minutes |
| Test container writable tmpfs | 512 MiB |
| Test source snapshot | 2 MiB per file, 16 MiB total |
| Compiler download | 120 seconds, 100 MiB |
| Linter download | 60 seconds, 20 MiB |

Sources: [Docker test runner](../tools/test_docker.py), [compiler](../tools/compiler.py), [linter](../tools/lint.py).

## Unused declarations

`MAX_ACTIVATION_DISPATCHES = 2` in [policy](../loop/policy.py) has no callers. It is not an active budget.
