# Repository instructions

Commit and push changes directly to `main` in this repository. Do not open a pull request or run a pull request review loop unless the user explicitly asks for one.

When working in an app-managed worktree, keep its assigned branch and push the validated commit to `origin/main` without force. Fetch `main` first and integrate any upstream changes before pushing.

## Development approach

Favor simplicity and short feedback loops over exhaustive coverage.
This is personal automation, not a general-purpose framework.

- Solve the requested case. Don't generalize for hypothetical future needs.
- Keep small changes local. Reuse session context and inspect only the code,
  direct callers and tests affected by the request. Expand the search only
  to answer a concrete correctness question.
- Batch related reads and edits instead of repeatedly tracing the same code.
- Prefer deletion and native tools over new abstractions or custom parsers.
- Don't add content restrictions or allowlists unless explicitly requested.
- Test meaningful correctness risks, not the absence of removed restrictions.
- Reuse documentation and extension capabilities already checked in this
  session. Repeat setup and discovery checks only when their wiring changes;
  still reload changed extension code and verify the requested behavior.
- Use focused checks while iterating. Run the affected component's full suite
  once before publishing, then rerun only checks affected by later edits.
  Canvas-only changes need the canvas suite, not the Python suite, unless
  they change the shared workflow contract or Python behavior.
- Documentation and instruction-only changes need no tests unless the
  repository has checks for that documentation.
- Avoid exhaustive one-off fixture matrices.
- Stop when the requested behavior works and relevant checks pass.
