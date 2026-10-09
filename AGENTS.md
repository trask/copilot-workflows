# Repository instructions

Commit and push changes directly to `main` in this repository. Do not open a pull request or run a pull request review loop unless the user explicitly asks for one.

When working in an app-managed worktree, keep its assigned branch and push the validated commit to `origin/main` without force. Fetch `main` first and integrate any upstream changes before pushing.

## Primary engineering priority: simplicity

This is personal automation, not a general-purpose framework.
Among solutions that correctly satisfy the request, choose the simplest.

- Start by extending an existing path or deleting unnecessary code.
- Do not add abstractions, configuration, workflow stages, extra model calls,
  or new completion rules without a concrete need in the current request.
- Keep plans small too. Do not turn a local change into a broader redesign.
- Test meaningful correctness risks, not every hypothetical combination.
- Before proposing added complexity, identify the simpler alternative and
  explain why it cannot satisfy the request.
- Stop when the requested behavior works and the relevant checks pass.

## Development approach

- Keep small changes local. Reuse session context and inspect only the code,
  direct callers and tests affected by the request. Expand the search only
  to answer a concrete correctness question.
- Batch related reads and edits instead of repeatedly tracing the same code.
- Prefer native tools over custom parsers.
- Don't add content restrictions or allowlists unless explicitly requested.
- Don't add tests for the absence of removed restrictions.
- Reuse documentation and extension capabilities already checked in this
  session. Repeat setup and discovery checks only when their wiring changes;
  still reload changed extension code and verify the requested behavior.
- Use focused checks while iterating. Run the affected component's full suite
  once before publishing, then rerun only checks affected by later edits.
  Canvas-only changes need the canvas suite, not the Python suite, unless
  they change the shared workflow contract or Python behavior.
- Documentation and instruction-only changes need no tests unless the
  repository has checks for that documentation.
