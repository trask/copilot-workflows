# Setup and recovery

## Human inference gate

The repository initially has no Actions secrets. A human must create a fine-grained PAT with resource owner their own licensed user account and **Account permissions / Copilot Requests / Read**. Give it no repository write permissions. Add it through GitHub's repository Actions secret UI as `COPILOT_GITHUB_TOKEN`. Never paste the value into chat or check it into Git.

Do not use App or OAuth credentials as `COPILOT_GITHUB_TOKEN`. Do not assume the organization-billed `copilot-requests: write` path works in this personal repository. Do not bootstrap, copy, or read secrets from another repository, CLI session, or installed configuration.

After setup, a human must authorize the first real shadow run. The worker uses only the inference PAT and a read-only central Actions token. Do not add `GH_AW_GITHUB_TOKEN`, `GH_AW_GITHUB_MCP_SERVER_TOKEN`, default OTLP credentials, or publisher credentials as a shortcut. Compiler activation references legacy gh-aw secret names for OAuth validation, but the audited sandbox job accepts only inference and built-in read-only central token references. There is no broad PAT fallback.

Before calling shadow ready, observe a real run producing all four candidate files, its API artifact/run metadata, verifier output, and failure evidence. Qualify actual target Gradle/JDK/network behavior and adversarial output handling. Actions can still encounter infrastructure, quota, CLI, or model failures; the pilot does not promise fixes for them.

## Public test repository access

Only public repository `trask/copilot-review-loop-test`, ID `1400255214`, is supported in addition to the public OpenTelemetry repository, ID `210933087`. Use its full PR URL for launch/status/cancel. Bare PR numbers continue to mean OpenTelemetry. Author ID `218610`, open state, exact head, and same-repository head requirements apply to both. Recreated repositories with the same name but a different ID are rejected.

The test repository is public by explicit user decision. The coordinator checks its GitHub-issued repository ID, name, and public visibility using unauthenticated API access before freeze. Public PR/review/thread collection uses the read-only central Actions token because GraphQL requires authentication even for public repositories. It records source visibility in the request; subsequent live head checks reject visibility changes. Neither source retrieval nor review collection uses the inference token.

Both sources use credential-free public Git fetches of the exact frozen commit. Worker source checkout and commands execute inside AWF. Trusted verifier object/index operations and the validator's credential-free preparation fetch the same frozen commit independently. Candidate manifests bind repository ID and source visibility as well as request, commit/tree/parent and hashes.

No source App or additional PAT is needed or minted. If either source becomes private, stop instead of borrowing local credentials or silently switching transports. A previously blocked, unfrozen private-source request can be replaced by an explicit public launch; the state transaction archives it and freezes current authoritative metadata.

Historical private transport helpers and offline tests remain for inspecting bound source packages, but central workflows no longer mint source credentials or authorize private acquisition. Publication uses a separate explicit personal-only protocol below. Historical synthetic AWF checks were not genuine test-repository shadows.

## Data-only checkpoint branch

`review-loop-state` contains only root-level `.json` files, no executable workflows or scripts. OpenTelemetry requests retain `pr-N.json`; test repository requests use `pr-1400255214-N.json`, so identical PR numbers cannot collide. Replacing a request atomically archives it as `request-ID.json`. State readers ignore archives when scheduling. Frozen review bodies are bounded request data; large logs and candidates never go into state.

Each checkpoint records the unique request, target/head repository ID and ref, frozen SHA, trusted workflow revision, all selected findings and baseline review IDs, generation, stage, expected SHA, iteration/deadline budgets, next check time, durable dispatch intent, Actions run ID/attempt/URL/conclusion, artifact references, and report or failure reason. It contains no credentials. The branch is limited to 1,000 JSON files, 1 MiB per checkpoint, and 16 MiB total; an operator must archive old data elsewhere before these limits are reached.

The writer reads the branch ref and tree, creates blobs and a new tree based on that tree, creates a commit with the observed head as its sole parent, and updates the ref with `force: false`. Two competing children cannot both fast-forward the branch. A rejected ref transaction reloads the tree and reapplies the operation, at most five times. Updating a different PR preserves its files. Initial branch creation has the same conflict/retry rule.

All central controller runs serialize; worker concurrency serializes by repository and PR, never cancelling another target's runner. Generation checks protect finalization against a cancellation or replacement between verifier and finalizer jobs. Only deterministic trusted jobs can write state.

## Launch, reconciliation, and failure evidence

```text
preview -> preview_complete with freeze_only_no_inference
shadow -> blocked with human_gate_COPILOT_GITHUB_TOKEN, if secret absent
shadow -> ready -> dispatch_intent -> dispatched/running
       -> verify_pending -> blocked with unqualified validation report
                        -> failed with malformed/incomplete artifact evidence
```

The coordinator freezes only verified Copilot identity `id=175728472`, `node_id=BOT_kgDOCnlnWA`, type `Bot`. Logins can differ between the users endpoint and reviews. The PR author must have GitHub-issued user ID `218610`, type `User`, and the head repository ID must equal the allowed base repository ID. Review state and submitted time must be present, and the review commit must equal the frozen head.

REST review/body and inline-comment collections and GraphQL thread collections are paginated. The pilot selects unresolved, non-outdated **original thread roots**, not replies in human-rooted threads. Bodies cannot be marked resolved like inline threads; every nonempty submitted verified body at the frozen head is frozen in full. Unknown or mixed body markup is never parsed as clean. In particular, `Findings: None` can coexist with a `Previously missed` finding inside details.

Before dispatch, the coordinator commits a unique durable intent. It dispatches on `main` only after checking main still matches the frozen central revision; worker preflight rejects a revision race. The Actions run title contains the request ID, and the coordinator binds event/path/repositories/SHA/run attempt from the authenticated Actions API. Only one matching run is allowed. Neither the model's output nor an `aw_info.json` claim supplies provenance.

The compiled worker explicitly allows `github-actions[bot]`, the actor of a central `GITHUB_TOKEN` workflow dispatch. This does not bypass durable-request preflight. If GitHub confirms that the agent job was skipped, an explicit launch after a workflow-revision fix may archive and replace the failed request. That recovery preserves the original deadline and consumed dispatch count, with at most two dispatches. It is not available for uncertain runs or failed model execution.

If dispatch returns an error or the coordinator stops, the next watcher reconciles the recorded intent against run identities. It never retries the POST blindly. After 15 minutes without a matching run, the request becomes blocked, not ready. Duplicate matching runs block the request. A cancelled worker becomes cancelled, a failed worker becomes failed, and the elapsed deadline becomes exhausted. Artifacts and logs from failed runs stay attached to the central run and their references stay in state.

Cancellation requires `previous_request` and `previous_generation` from read-only status. The comparison happens inside the same `State.update` CAS transaction, not a client precheck. Missing/stale identities or a concurrent replacement reject without cancelling the newer request. Active cancellation commits a cancelled stage and increments generation while preserving request, intent, receipts and prior-stage/reason evidence. An exact already-cancelled generation is an unchanged no-op; other terminal outcomes reject. A running worker can finish, but its results cannot be finalized. Cancellation does not call broad Actions cancellation APIs and cannot revoke an already-authorized in-flight target mutation.

To recover an uncertain launch, inspect the central Actions run list and checkpoint intent before doing anything. A frozen-head request with a dispatch is not automatically relaunched. There is no retry/reset command that might conceal an uncertain side effect. Preserve its archive/evidence and use a deliberately reviewed new request only after an operator proves what happened.

## Verification and its deliberate limitation

The coordinator selects a verification claim from state. A fresh verifier checks out **the recorded central revision**, not the candidate workspace. It rechecks the live target, then checks the request generation, retrieves the exact worker attempt's job/artifact metadata, and downloads the candidate archive using its API artifact ID. Signed artifact redirects do not receive the GitHub bearer token. The archive's downloaded SHA-256 must match the server-issued digest.

The verifier reads ZIP members into bounded buffers, never extracts them into the central checkout. It uses a fresh bare Git repository, fetches the exact public source SHA with no credential helper, applies a bounded text patch to the index, and derives the candidate tree and one-parent deterministic commit itself. It executes no candidate script, Git hook, build, filter, or test. It retains the patch in the worker artifact and uploads a thin `candidate.bundle` plus a manifest binding commit/tree/parent/prerequisite, request digest, and bundle/patch hashes. A roundtrip test fetches the prerequisite, imports the bundle, and compares the exact restored commit, parent, tree, and file contents.

The finalizer runs in another fresh job at the frozen trusted revision. It receives the verifier's report and any objective receipt, rechecks generation/run/request/candidate binding and the live target head, then writes the checkpoint and Actions summary. A failed verifier or missing report cannot produce acceptance. Deadline expiry in any active stage becomes exhausted; stale finalization persists a terminal blocked checkpoint. Shadow candidates become `blocked / validation_unqualified` even when objective execution passes. New personal publication requests can instead enter `publish_pending`; that is only a claim for fresh trusted acceptance, not permission to push. `failed` and `not_run` worker validation claims remain distinct and fail finalization.

The gh-aw artifacts `agent`, `activation`, `firewall-audit-logs`, `detection`, `info`, and `usage` provide diagnostics, not attestation of target commands. Model transcripts and files under its writable workspace are forgeable.

The independent `loop.validation` executor runs in a fresh job with no inference, App, state-write, or publisher credential. It imports only the trusted verifier's hash-bound bundle, checks parent/tree, then executes candidate commands only through checksum-pinned AWF v0.28.23 with an explicit digest-pinned agent/Squid/API-proxy manifest, network isolation, no `--env-all`, and no Docker socket exposure. This AWF release always starts its API proxy; the no-model launcher supplies no provider credential and executes no model command. Its sanitized environment uses fresh HOME `/tmp/review-loop-validation-home` with an explicit narrow read-write mount, separate from the protected `/opt/review-loop-validation` receipt directory and Actions control files.

The worker uses a separate fresh `/tmp/review-loop-worker-home` through AWF's chroot identity HOME setting and existing `/tmp` mount. Both jobs pre-create runner-owned `.gradle`, `.m2`, and `.cache` directories before AWF root pre-seeding of JVM proxy files. `GRADLE_USER_HOME`, `XDG_CACHE_HOME`, and JVM `user.home` point into those isolated homes. No host credentials or Actions caches are restored there, and no cache is published. The pinned repository wrapper runs online only through the explicit Gradle/GitHub/Maven/plugin domain allowlist; dependency failures remain failures, never silent offline or system-Gradle substitutes.

Worker home settings belong to `engine.env`, not workflow-wide `env`. Trusted preparation creates the empty home before compiler tool installation; inference alone receives the sandbox HOME/cache settings. This prevents earlier setup tools from creating the home through a global cache variable before its freshness check. Existing homes are rejected rather than reused or deleted.

Native AWF exit statuses and bounded stdout logs are captured by the trusted host launcher under `/opt`, mounted read-only in the sandbox except the explicit candidate workspace. The worker cannot write these receipts. The command plan derives at most two Gradle modules from verified Java paths and runs their `spotlessJavaCheck` and `test` tasks. It never executes model-supplied argv on the host. No supported narrow plan is a failed/blocked validation, not a clean report.

Everyday push/PR checks run only the self-contained deterministic suite, pinned compiler/schema checks, actionlint, security audits and generated-lock reproducibility in `validate.yml`. They do not fetch target repositories, start AWF/Docker, run Gradle or resolve Maven dependencies. Unit tests use local Git fixtures and mocked APIs, not the public test repository.

There is no standalone container/Java qualification workflow or central Gradle smoke/baseline routine. End-to-end testing happens only when the user supplies a concrete PR and requests it. The public test repository is neither a development CI dependency nor an automatic E2E target.

Runtime activation freezes the trusted source revision and exact supported source/build/test/workflow contract without querying a prior qualification run or artifact. The actual review-loop candidate still needs fresh native command receipts, independently verified history/tree and exact post-publication target CI. These operational gates run only for an explicitly launched loop. General receipt `qualified` stays false; passing target commands do not prove that hostile tests faithfully test behavior.

## Historical central qualification

These past runs retain their original evidence. Standalone qualification is no longer available or a runtime activation/continuation precondition. Their receipts cannot replace a fresh actual candidate receipt.

[Central qualification run](https://github.com/trask/copilot-workflows/actions/runs/36799868794) passed 39 offline tests, reproducible gh-aw v0.89.21 compilation, actionlint, the lock audit, and the synthetic no-model AWF smoke. Its host receipt records native exit 73, an unchanged protected file, isolated HOME, credential absence, hidden Docker socket, and the exact three pinned runtime image references. Earlier failed runs retain their diagnostics.

[Preview run](https://github.com/trask/copilot-workflows/actions/runs/36799868373) froze live API data for [open-telemetry/opentelemetry-java-instrumentation#20332](https://github.com/open-telemetry/opentelemetry-java-instrumentation/pull/20332) as `preview_complete`. [Missing-auth gate run](https://github.com/trask/copilot-workflows/actions/runs/36799870922) left [open-telemetry/opentelemetry-java-instrumentation#20330](https://github.com/open-telemetry/opentelemetry-java-instrumentation/pull/20330) blocked with `human_gate_COPILOT_GITHUB_TOKEN`, no worker run, and no target mutation. Neither is a real Copilot shadow.

The authorized [genuine test-repository worker](https://github.com/trask/copilot-workflows/actions/runs/36897658028) executed Copilot for request `c102285b2b284723b9ed7adce3c81c49`, frozen head `abfca5bb792b0bc020e43cd76a079414ed423c5c`, central revision `6942bb3fd8a8aef922f7a3ce7fef92c26964c75b`. It repaired the loop bound in unpublished artifacts. [Trusted reconstruction and objective validation](https://github.com/trask/copilot-workflows/actions/runs/36898733783) retained candidate commit `34bc117981f6386bc80f383e06c3d7cb87a7d95f` and its bound thin bundle. Objective Gradle exited 1 because it could not create its wrapper lock in the sandbox home; no target tests ran. The request remains blocked with both dispatches consumed and original deadline unchanged. The failed evidence stays attached to those runs.

The writable-home repair does not reopen that request or revalidate its candidate under a different frozen workflow revision. After explicit user authorization, new requests used the test-repository-only terminal replacement protocol. Inference setup and first-run authorization were completed. Passing execution evidence below establishes the observed test-repository command result, not a general acceptance or publication policy.

The first new request `682b2ad1ed394441a7d38e3359592386` failed before inference because workflow-wide cache settings let setup tools create the home before its freshness check. [Failed worker evidence](https://github.com/trask/copilot-workflows/actions/runs/36903578068) remains archived. Central commit `2ee539ba306892e4b2ec17b8f1e174529a612aa2` restricted the settings to inference and prepared the home before tool installation. [Central qualification](https://github.com/trask/copilot-workflows/actions/runs/36904820085) passed 58 Linux tests, real reproducible compilation, lint, lock audit and actual no-model AWF Gradle home/dependency checks.

The second and final authorized continuation request `09929769a6774f12910c7c5b8c5a2605` froze the same public test head `abfca5bb792b0bc020e43cd76a079414ed423c5c`, review `5381739315` and inline `4157376500`, at central revision `2ee539ba306892e4b2ec17b8f1e174529a612aa2`. [Copilot worker attempt 1](https://github.com/trask/copilot-workflows/actions/runs/36905201062) produced complete bound artifacts accounting for both findings. [Fresh verifier, objective validator and finalizer](https://github.com/trask/copilot-workflows/actions/runs/36906709865) reconstructed the candidate, imported its bundle and ran `/bin/bash ./gradlew --no-daemon :fixture:spotlessJavaCheck :fixture:test` in credential-free AWF. Its host-captured native exit was zero, with both requested tasks and six actionable tasks executed, not restored from a cache.

| Evidence | Identity |
| --- | --- |
| Worker candidate artifact | `11184660197`, SHA-256 `c7a87ab3b715a44f793517fd9af793981e419459e54bfd879d73510cd1eb69fd` |
| Candidate commit / tree | `aabb5f48f2b6f5147c543a58a62348b1f1e123a6` / `3de77d7edfef923c69371bcaf9bde21eb24ea36c` |
| Candidate parent / bundle prerequisite | `abfca5bb792b0bc020e43cd76a079414ed423c5c` |
| Verification artifact with manifest/bundle | `11185095433`, SHA-256 `038af541bbb0ed8d2c99ecd05892fd776ced36e5e1606ccbbb7e87c351989c02` |
| Candidate bundle SHA-256 | `7c61f6c6f3a7ed5dea94b5c52aa204d3672956066b12a6aec2014092d252beaa` |
| Objective receipt/log artifact | `11185220626`, SHA-256 `0946b67e97ca97fbeaeaf64de338d6fc70b0e0287b92556198212909d0e7cd2a` |
| Objective command log SHA-256 | `9e74030e781ff36b71b529477d71c5112cd5c9fcf39fde76a5e17a09bb791cf9` |

The final checkpoint is generation 5, `blocked / validation_unqualified`, with objective status `passed`, `qualified: false` and `publication_eligible: false`. That is the intended shadow-only gate, not a target test failure. The continuation consumed two new pipelines without extending its shared deadline `1790884798`. It does not authorize another retry, a fresh target review or publication. The target PR remained open/draft at its original head, with no target mutation.

## Explicit terminal test-repository retry

Use this only after authorization to spend inference on a new bounded pipeline following a central implementation repair. Status supplies the exact current request and generation:

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=replace -f mode=shadow \
  -f target=https://github.com/trask/copilot-review-loop-test/pull/1 \
  -f previous_request=<exact terminal request ID> -f previous_generation=<observed generation>
```

This operation is restricted to public repository ID `1400255214`. It requires an unsuccessful terminal stage, unchanged repository/PR/head/ref/visibility, a newly frozen unique request, and a changed central revision. Any previous durable dispatch must have exactly one completed, provenance-bound worker. Active, unknown, duplicate, or uncertain runs cannot be replaced. Cancellation or a concurrent replacement causes the exact-state CAS to fail without dispatch.

The same branch transaction archives the complete prior checkpoint and creates the new generation. Existing archived provenance cannot be overwritten; projected file/count/branch limits apply before writes. The replacement inherits the prior frozen pipeline ceiling, consumed count and original deadline. It cannot replenish the budget or extend that deadline, even under a newer runtime default. At most two replacement operations are allowed, independently of the model ceiling. Exhausted model budgets, publication phases, successful requests and OpenTelemetry targets are excluded. Neither the watcher nor an ordinary launch automatically replaces a failed request, and activation-only recovery cannot bypass replacement limits.

## Personal publication credential gate

The explicit personal phase implements actual publication, bot-root replies/resolution, fresh Copilot requests, exact-head review/CI waiting and bounded further iterations. It supports only public `trask/copilot-review-loop-test#1`, repository ID `1400255214`, node ID `R_kgDOU3Yy7g`. It cannot publish to OpenTelemetry, arbitrary personal repositories or forks. It never merges, force-pushes, changes PR metadata or replies to/resolves human roots.

A human must create a **separate fine-grained PAT**, resource owner `trask`, selected repository **only** `copilot-review-loop-test`, repository permissions **Contents: Read and write**, **Pull requests: Read and write**, and automatic **Metadata: Read**. Give it a short expiry. Add it through the private central repository's Actions secret UI as `REVIEW_LOOP_TEST_PUBLISH_TOKEN`. Do not add account Copilot Requests, other repositories, Actions write, administration, workflows or broader permissions. Never paste the value into chat, copy local CLI credentials, or reuse `COPILOT_GITHUB_TOKEN`.

The `personal_live` job alone receives this token. Worker, verifier, objective validator and finalizer jobs never receive it. The coordinator sees only whether the secret exists. The explicit invocation selects `fine_grained_pat`; classic PATs, inference-token substitution, implicit App selection and credential fallbacks fail closed.

Runtime checks authenticate the actor, reverify repository numeric/node/owner/visibility identities, reject access to the private central repository, and negotiate a dry-run Git receive-pack using the exact current branch. Repository `permissions.push` alone does not prove token write permission. The dry run does not change refs and does not establish that future branch protection will permit a changed commit. GitHub does not expose the full selected-repository scope of a personal token to the token itself. Single-repository issuance remains a manual setup requirement, not a runtime attestation claim.

Before any fix push, a durable capability probe records baseline review/run IDs and the exact head, then requests Copilot through `requested_reviewers` using the chosen PAT. It requires both a durably acknowledged response and a new exact-head submitted verified bot review before launching a model worker. Requested-reviewer and GitHub-issued dynamic Copilot run metadata confirm progress, not qualification; a failed Copilot run blocks. HTTP success alone is insufficient. Existing pending reviews are not requested again. Missing, denied or ambiguous capability blocks. An uncertain probe is never retried or qualified by a later observation alone. This probe can request review but cannot post comments or publish a fix.

The private checkpoint binds this proof to the selected token's SHA-256 digest, never its value. Every live job rechecks that binding before transitions. Credential replacement blocks the phase instead of letting a different token inherit the probe's qualification; pending intents and consumed budgets remain preserved. The worker never receives this credential binding or publisher token.

App installation auth remains a future separately qualified option for other repositories. No App is registered, installed or minted by this implementation, and the personal PAT is not an upstream/fork solution.

## Start a new personal publication phase

After credential setup and personal publication authorization, dispatch the central workflow:

```bash
gh workflow run coordinator.yml --repo trask/copilot-workflows --ref main \
  -f operation=start-publication -f mode=publish -f publication_auth=fine_grained_pat \
  -f target=https://github.com/trask/copilot-review-loop-test/pull/1 \
  -f previous_request=<current terminal request ID> -f previous_generation=<observed generation>
```

The operation requires a fresh owner-ID `218610` manual Actions dispatch on private central `main`, not a schedule, rerun or another actor. It binds the current target before a chosen-token capability review, requires the exact prior terminal state and stable target, and atomically archives it. It never reuses an old candidate, acceptance flag, receipt or workflow-bound request. An absent publisher secret persists `blocked / human_gate_REVIEW_LOOP_TEST_PUBLISH_TOKEN`, with zero new model dispatches. After setup, the same explicit operation can replace only an untouched, unexpired credential/configuration gate with zero consumed pipelines, no capability and no effects. A gate resume retains its existing phase, frozen limits and deadlines rather than authorizing a new budget. Consumed or uncertain publication phases cannot be reset.

The CI setup transition permits exactly the old pinned head to the separately pinned CI-enabled head under a new implementing revision. It applies only to an untouched gate; arbitrary head/ref/repository changes remain rejected. Old deadlines, generations and evidence remain in their immutable archive. After the chosen-token request is acknowledged and its exact-head review finishes propagation, the coordinator archives the probe and freezes a new finding request in the same phase, with the inherited deadline and zero consumed pipelines. A probe without findings cannot dispatch a worker.

A newly authorized phase has at most **five total worker/model pipelines and two hours total**, including its initial worker run. Fresh iteration requests inherit the same frozen ceiling, phase/deadline and consumed count, increment generation and archive the preceding iteration. Runs four and five use that remaining budget; fresh findings after run five exhaust it and never dispatch run six. Schedule/duplicate runs cannot create extra phases or reset budgets. Shadow requests still stop after one investigation.

The runtime reads `request.budgets.max_iterations` and requires the publication `max_pipelines` to agree. Missing, malformed or inconsistent bounds/counts fail explicitly, with no fallback to the new default. Existing two-run requests, gate resumes, replacements, repairs and continuations retain their original frozen limits, counts and deadlines. The exhausted 2026-10-01 pilot is not reopened. The generic `loop.cli publish` entry remains disabled, and normal `launch` never selects publication mode.

`operation=repair-publication` is a fresh owner-dispatch operation for one central repair before any model dispatch. It requires the exact predecessor request/generation, unchanged target, a different implementing revision, zero consumed pipelines/effects, an acknowledged capability request, a successful API-bound credential job and its propagated submitted review. It archives the predecessor and creates a new finding request with the same phase/deadline, not a new budget. A trusted credential-only recheck verifies the unchanged token binding and Git dry-run before dispatch; it never repeats the Copilot request or imports old candidate artifacts. Uncertain, cancelled, expired, consumed or already-repaired phases cannot use it.

`operation=continue-personal-test` supports one deliberately pinned reviewable-test continuation after the first fully reconciled publication ends `blocked / unknown_fresh_review_body`. It is not a generic terminal reset. Exact predecessor request/generation/full-state CAS, original confirmed push and submitted bot review, resolved original bot roots, passed original exact-SHA CI and no pending effects/review are required. The complete terminal checkpoint is archived unchanged. The phase, original deadline, consumed count of one and frozen total ceiling remain; the 2026-10-01 pilot's ceiling was two. Source-contract checks or target preparation never create another budget.

The `personal-test-prefix-ci-v3` source contract adds a prefix-sum overload and six tests without changing the old four tests, build, wrapper or CI. Green reference `0a36c0115b5cc9a5d9734421242d286444b2cd4e` has sole parent `5269da9c729b4a9cede8805f6b72a7f1b60b8452`; seeded source `c8e93cab3771133d0b37a846ab48b8d2bc82377e` has sole parent that reference. Only the Java fix path differs between reference and source; the exact test blob is `e552581a3b3222a5b423c662071f4c98040dc1b4`. Repair therefore retains a meaningful method/test diff against main rather than creating an empty PR diff.

Before continuation, credential-free Git object checks verify the exact pinned reference/seed ancestry, allowed source/test changes, test blob and unchanged build/wrapper/CI contract. No standalone workflow run or baseline receipt is required or synthesized. The historical v2 source profile is not promoted or reused for v3 candidate acceptance.

New requests freeze the new source and fresh chosen-token review; an unchanged token binding and a fresh acknowledged capability probe are required. Capability freeze preserves consumed count and finding history. Wrong source/history, uncertain effects, cancellation, an already-used continuation or expiration fails closed. Only fresh native evidence for the actual candidate can satisfy publication acceptance. Historical qualification runs below remain past evidence, not runtime preconditions.

The first personal publication genuinely pushed `5269da9c729b4a9cede8805f6b72a7f1b60b8452`, passed target CI `36934434644` and obtained exact-head bot review `5386423213`. That review said no files could be reviewed, so request `a7424610cd6b4ce8b475df896f72692d`, generation 10, stopped `blocked / unknown_fresh_review_body`. It did not establish clean review. Its original stopped push-evidence archive remains byte-identical; no second push or manufactured thread reply was used to reconcile it.

## Personal candidate acceptance and mutation boundary

`personal-test-gradle-ci-v2` binds the source and CI workflow constants in `loop/personal_contract.py`. The build/tests/wrapper contract retains the original passing shadow's contents, with only the pinned target CI workflow added. Every frozen source must match that exact contract outside `fixture/src/main/java/fixture/ArraySum.java`; candidates may change only that regular Java file. Credential-free object checks verify that contract before worker dispatch and again during candidate import. Arbitrary target build scripts, test/workflow changes, other modules and upstream source remain excluded; no standalone baseline run certifies a candidate.

The CI-enabled seeded source is `b7be7ca068377c8a1c1fda0b5b32db8480a7460e`, with passing baseline `70f5f237738eb34c4f8e9595b29f05d92a8531e8`. The exact `Fixture Gradle checks` job belongs to workflow `372583409`, `.github/workflows/fixture-ci.yml`, Git blob `c1e0bdee3aa437f19cfcea624dcb5d7a08397d09`. [Baseline target CI](https://github.com/trask/copilot-review-loop-test/actions/runs/36929906923) passed four tests; [seeded-head target CI](https://github.com/trask/copilot-review-loop-test/actions/runs/36930045395) failed the expected three tests. The contract also freezes the CI setup's README update; no build, wrapper, dependency or test content changed.

Each new publication request must produce fresh worker and trusted packaging artifacts, then pass separate no-model `/bin/bash ./gradlew --no-daemon :fixture:spotlessJavaCheck :fixture:test`. The publisher independently downloads bounded artifacts, verifies API-issued IDs/run/attempt/workflow/server digests and retention, rederives the candidate from the original worker patch, imports the manifest-bound thin bundle, and checks one-parent history/tree and the unchanged source contract. It also verifies host receipt/log hashes, native zero and actual execution of both requested tasks, rejecting skipped/cached tasks. Worker validation claims, old shadow results and synthetic smoke cannot satisfy this policy.

The acceptance record names the personal profile and exact request/commit/receipt; it does not change general `qualified: false` or `publication_eligible: false`. Fixes can enter a durable publication intent only after these checks. No-change runs validate the same target tasks but never push the verifier's empty packaging commit. Their explanation does not establish a clean review.

Only trusted bare Git object operations run outside AWF. Source/bundle parsing receives no PAT, hooks/filters/helpers/configuration are disabled, and personal Git processes have CPU/memory/file/object limits. The PAT is supplied only through a sanitized Git push environment for ordinary non-force push, never command arguments, persisted config, target execution or logs. Exact PR/source checks and full-state CAS run immediately before mutation. Git's non-fast-forward rejection protects competing descendants; no forced update or rollback exists.

Publication, reply, resolution and review-request intents persist before their side effects. After interrupted push, the exact candidate head confirms publication; an unchanged original head blocks uncertain publication instead of retrying. A different head blocks as stale. Replies reconcile by exact body marker/root/actor/time and resolutions by exact API thread ownership/status. Uncertain effects are never reposted. Cancellation increments generation and prevents new effects/results, but cannot revoke an already-authorized in-flight mutation. Its durable intent remains evidence for operator inspection.

A successful native push records its acknowledgment before confirmation. The exact live branch ref and PR head must converge before any target reply/review effect; a stale PR read waits through bounded scheduled state, never a sleeping runner or a repeated push. Read errors preserve the intent. Propagation timeout or deadline exhaustion preserves acknowledgment, candidate/receipt identities and all consumed budgets.

`operation=reconcile-publication` requires a fresh owner dispatch with explicit publish/fine-grained-PAT mode and exact predecessor request/generation. It handles only the specific unexpired uncertain-publication stop, once. The original request, frozen workflow revision, acceptance, artifacts, receipt, candidate and push intent remain unchanged. It verifies the original publisher run/job and worker/verifier artifact provenance, exact agreeing live branch/PR candidate SHA, one-parent/tree/Java-only history and unchanged source contract. The same state transaction archives the complete stopped checkpoint as `stopped-<request>-<generation>.json` before recording separate recovery execution provenance. The live job rechecks the original token digest and confirms existing history only; it never imports/reaccepts a candidate or pushes again. Cancellation, other heads/history/token, reruns, expiry and repeated reconciliation reject. Bot effects and fresh review occur only after observed confirmation.

Only frozen verified Copilot original roots can receive the bounded explanation or resolution. A bot reply in a human-rooted thread cannot transfer ownership. Body reviews have no resolvable inline thread. The publisher confirms the inline reply through the proper PR review-comment endpoint before resolving; it never substitutes an issue/timeline comment.

## Fresh review and target CI

After confirmed publication/no-change and bot-root handling, the trusted job persists an exact-head review-request baseline, makes one chosen-PAT Copilot request, confirms API/run evidence and exits in `waiting_review`. The approximate five-minute watcher uses no sleeping runner or model polling. A fresh submitted review must match numeric/node/type bot identity and exact expected commit, be absent from the baseline, and be submitted strictly after the durable baseline. Complete paginated body/comment/thread collection waits at least two minutes for propagation.

GitHub can reuse an original inline thread during a new review. Its parent review remains on the original commit, while the comment's API-issued current `commit_id` advances to the current head. The collector retains that unresolved, non-outdated original bot root only when its current commit matches the frozen head and its original commit matches a submitted verified parent review. Human roots, bot replies in human roots and stale/mismatched comment contexts remain excluded.

The frozen finding separately records the live comment commit/line, immutable original commit/line, submitted parent review commit/time, root author identity and exact body. After a confirmed one-parent fix push, the same root may retain its frozen context or advance to that exact published commit, including becoming outdated. No arbitrary contextual commit is accepted. Replies/resolution reverify the parent, author, original context, stored body and thread ownership; becoming outdated does not discard the root. Stable root fingerprints exclude live-head changes and overview wording, so a reused unresolved root cannot spend a further pipeline as a new finding.

The clean grammar is limited to the observed CCR v2 `Approval recommended`, `Review effort: Balanced`, `Findings: None` format without disclosure blocks. It was checked against verified review `5373043542`. Every fresh exact-head submitted body participates, not just the latest one. Any hidden previously missed/open finding, verified unresolved root or changes-requested fresh review remains nonclean; any unknown fresh body blocks. A later clean body does not erase an earlier fresh body-only finding. No comments, no model edits or a `Findings: None` substring cannot establish clean.

Set central Actions variable `REVIEW_LOOP_TEST_CI_CHECK` to the exact centrally pinned **target GitHub Actions CI check**, never Copilot. The watcher verifies the workflow's exact Git blob at the expected published SHA and binds the named check to its API job and an owner-triggered push run from that exact static workflow path in the stable test repository. Check, job and complete run must succeed at attempt 1. Dynamic Copilot workflows do not qualify. Empty checks block as `target_ci_missing`; pending waits; failed, neutral, skipped, cancelled, duplicate/rerun, wrong-app/SHA/workflow blob or conflicting commit statuses block. There is no CI-repair loop. Pre-push objective Gradle and the Copilot check do not count as target CI.

### Bounded personal pilot outcome

The 2026-10-01 personal pilot completed actual worker, protected native validation, non-force publication, fresh submitted Copilot review and exact-head target CI on [trask/copilot-review-loop-test#1](https://github.com/trask/copilot-review-loop-test/pull/1). Its honest final outcome is **exhausted**, not clean. Phase `165eafd662904f1f879693ef0ccf2351` consumed exactly two worker pipelines and retained its original deadline `2026-10-01T23:45:37Z`.

| Evidence | First publication | Reviewable continuation |
| --- | --- | --- |
| Request/generation | `a7424610cd6b4ce8b475df896f72692d` / 10 | `1587f5bddf1c474595269cbcf0292820` / 12 |
| Frozen runtime revision | `3d119ff66efaab1ebf1ac174f67fe6ad547a74c1` | `e297cce23148656d414136ade81523d21fbf8608` |
| Frozen target SHA | `b7be7ca068377c8a1c1fda0b5b32db8480a7460e` | `c8e93cab3771133d0b37a846ab48b8d2bc82377e` |
| Worker / native verifier pipeline | `36932854996` / `36933876717` | `36939390327` / `36940280069` |
| Publisher | `36934327956`, exact-history recovery `36935695488` | `36940606792`, acknowledged and confirmed without recovery |
| Published SHA | `5269da9c729b4a9cede8805f6b72a7f1b60b8452` | `4255c988e0b8e3616417ec19166cd8fd5e08819b` |
| Submitted exact-head bot review | `5386423213`, no files reviewed | `5386762879`, one remaining finding |
| Exact target CI run / check | `36934434644` / `110611252138`, passed | `36940704996` / `110631276894`, passed |
| Terminal checkpoint | `blocked / unknown_fresh_review_body` | `exhausted / remaining_findings_pipeline_budget` |

The continuation candidate has sole parent the frozen source and tree `df75da75a9074938f8a56c648f497623dbf49c18`, exactly matching the qualified green reference. Bundle SHA-256 is `5fdda20f4010d1d598cdcf9bd902602eb567740fa017aa4b76b068a0f9461a10`; objective log SHA-256 is `d1836adeea506696359fbf88a9484265d988757ecbc5eed8b048b8562b3d24e3`. Both exact Gradle tasks actually executed with native exit zero; target CI passed all ten tests.

Worker artifact `11198989329` has server digest `sha256:3f36b36f1fbc05dbae81375afd5029f5021a3cd1b6d7f10296e6e29d3c248db4`. Trusted verification artifact `11199727456` has digest `sha256:d9c4a277611a937db1c3570541dc886e10964ee42db3980dcffead352593f043`; objective artifact `11199707561` has digest `sha256:37a4c39805fdb2b3dd400f173eebef92fbfb7b333afda86858f06bf4449534e5`. Current-main source qualification `36938290484` passed the pinned prefix baseline and isolation checks. The duplicate branch qualification `36938290281` retained a genuine Maven HTTP 429 failure; it was neither rerun nor accepted instead of the passed main proof.

All submitted Copilot review identities were verified against numeric ID `175728472`, node `BOT_kgDOCnlnWA` and type `Bot`. The remaining [verified bot root `4161499696`](https://github.com/trask/copilot-review-loop-test/pull/1#discussion_r4161499696), review `5386762879`, says:

> This does not implement the stated regression: `sum(int[])` still iterates through `values.length`, while this adds a separate, correct overload and new tests. The head therefore will not produce the described three failures, and the tests are not unchanged. Restore the intended one-line loop-bound change and remove the added API/tests, or update the PR title and description if this expanded API is intentional.

The title/body/draft state were explicitly out of mutation scope. The loop did not restore a bug, change metadata, invent a clean review or dispatch a third model. Original bug roots `4157376500` and `4161377967` were already resolved/outdated when inspected, so no unnecessary replies or resolutions were manufactured. The final review request is confirmed and there are no pending publication/thread/review effects; the remaining bot finding is left visible.

Final state blob is `172b8696d193546a54748a5aeca407ba97612875`, persisted by coordinator `36941765252`. The first stopped publication archive remains exactly `c9f7ec1ff99b72707cea82c8899b58f38ad54a48`, and the complete first terminal request archive remains exactly `dd1254281176a139c3f73ca638d1f99537d014e8`. Earlier failed/shadow records remain intact. General qualification is still false, upstream publication is still disabled, central remains private and the test repository public. Due-state transitions were exercised through bounded explicit ticks; automatic five-minute schedule delivery was not demonstrated.

A variable alone cannot create or qualify target CI. Changing the workflow, tests or build changes the pinned source contract and requires separately authorized target setup and a reviewed central contract update. Expected failing CI at the seeded buggy head does not prevent a Java-only publication after independent candidate validation; clean still requires successful target CI at the resulting pushed SHA.

Fresh findings with successful target CI can freeze further requests only within the phase's frozen budget. Repeated finding bodies/paths block; no-change cannot create an unlimited re-review loop. Remaining findings after five consumed pipelines exhaust a new phase; historical two-run phases still exhaust at two. `clean` requires both an interpreted fresh review and successful exact-head target CI. General/upstream publication remains disabled even if a personal phase eventually reaches clean.

## Pinned official references

These sources were checked against gh-aw v0.89.21, not a moving website:

- [Copilot engine](https://github.com/github/gh-aw/blob/v0.89.21/docs/src/content/docs/engines/copilot.md)
- [Authentication](https://github.com/github/gh-aw/blob/v0.89.21/docs/src/content/docs/reference/auth.mdx)
- [Custom steps and jobs](https://github.com/github/gh-aw/blob/v0.89.21/docs/src/content/docs/reference/steps-jobs.md)
- [Artifacts](https://github.com/github/gh-aw/blob/v0.89.21/docs/src/content/docs/reference/artifacts.md)
- [Sandbox](https://github.com/github/gh-aw/blob/v0.89.21/docs/src/content/docs/reference/sandbox.md)
- [Safe outputs](https://github.com/github/gh-aw/blob/v0.89.21/docs/src/content/docs/reference/safe-outputs.md)
- [Pinned AWF arbitrary-command usage](https://github.com/github/gh-aw-firewall/blob/v0.28.23/docs/usage.md)
- [Pinned AWF environment rules](https://github.com/github/gh-aw-firewall/blob/v0.28.23/docs/environment.md)
