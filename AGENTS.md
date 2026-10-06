# Repository instructions

Commit and push changes directly to `main` in this repository. Do not open a pull request or run a pull request review loop unless the user explicitly asks for one.

When working in an app-managed worktree, keep its assigned branch and push the validated commit to `origin/main` without force. Fetch `main` first and integrate any upstream changes before pushing.

## Development approach

Favor simplicity and short feedback loops over exhaustive coverage.
This is personal automation, not a general-purpose framework.

- Solve the requested case. Don't generalize for hypothetical future needs.
- Prefer deletion and native tools over new abstractions or custom parsers.
- Don't add content restrictions or allowlists unless explicitly requested.
- Test meaningful correctness risks, not the absence of removed restrictions.
- Use focused checks while iterating. Run the full suite once before
  publishing, then rerun only checks affected by later edits.
- Avoid exhaustive one-off fixture matrices.
- Stop when the requested behavior works and relevant checks pass.
