"""Fail if compilation introduces mutation permissions or unpinned dependencies."""

import json
import re
from pathlib import Path


def audit():
    path = Path(".github/workflows/copilot-worker.lock.yml")
    text = path.read_text(encoding="utf-8")
    metadata = json.loads(text.splitlines()[0].split(": ", 1)[1])
    assert metadata["compiler_version"] == "v0.89.21" and metadata["strict"]
    assert metadata["agent_id"] == "copilot"
    # Only inspect permission mappings, not explanatory strings emitted by the compiler.
    for line in text.splitlines():
        assert not re.fullmatch(r"\s+(?:contents|actions|issues|pull-requests|id-token|"
                                r"packages|checks|discussions|copilot-requests): write", line), line
    for action in re.findall(r"^\s+uses: (\S+)", text, re.MULTILINE):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), action
    manifest = json.loads(text.splitlines()[1].split(": ", 1)[1])
    assert all(re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", c.get("pinned_image", c["image"]))
               for c in manifest["containers"])
    assert all(s["name"] != "github" for s in manifest["mcp_servers"])
    agent_job = text.split("\n  agent:", 1)[1].split("\n  conclusion:", 1)[0]
    assert set(re.findall(r"secrets\.([A-Z_]+)", agent_job)) <= {
        "COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN",
    }
    assert not any(x in text for x in ['"create_issue":', '"add_comment":', '"push_to_pull_request_branch":',
                                     '"create_report_incomplete_issue":'])
    assert 'GH_AW_FAILURE_REPORT_AS_ISSUE: "false"' in text
    assert 'comment_id: ""' in text and "GH_AW_MISSING_TOOL_CREATE_ISSUE" not in text
    assert "status-comment: false" in Path(".github/workflows/copilot-worker.md").read_text()
    assert 'bots: ["github-actions[bot]"]' in Path(".github/workflows/copilot-worker.md").read_text()
    assert 'GH_AW_ALLOWED_BOTS: "github-actions[bot]"' in text
    assert "activation-comments: false" in Path(".github/workflows/copilot-worker.md").read_text()
    assert "OTEL_EXPORTER_OTLP_ENDPOINT: \"\"" in text
    assert "GH_AW_OTLP_ENDPOINTS: \"[]\"" in text or "GH_AW_OTLP_ENDPOINTS: '[]'" in text
    assert text.index("Remove compiler-added central Git credential") < text.index("id: agentic_execution")
    assert "persist-credentials: true" not in text
    assert "--exclude-env COPILOT_GITHUB_TOKEN" in text
    assert "SOURCE_READ_TOKEN" not in agent_job
    for setting in ("AWF_CHROOT_IDENTITY_HOME: /tmp/review-loop-worker-home",
                    "XDG_CACHE_HOME: /tmp/review-loop-worker-home/.cache"):
        assert setting in text
    assert text.index("Prepare fresh writable sandbox home") < text.index("id: agentic_execution")
    assert "run: python3 -m loop.worker_home" in text
    assert "loop.validation" not in text
    assert text.index("Prepare fresh writable sandbox home") < text.index("Install GitHub Copilot CLI")
    top_env = text.split("\nenv:", 1)[1].split("\njobs:", 1)[0]
    assert "AWF_CHROOT_IDENTITY_HOME:" not in top_env
    assert "GRADLE_USER_HOME:" not in top_env
    assert "JAVA_TOOL_OPTIONS:" not in top_env
    assert "REVIEW_LOOP_SOURCE_APP_PRIVATE_KEY" not in text
    assert "install_awf_binary.sh\" v0.28.49 --rootless" in text
    controller = Path(".github/workflows/coordinator.yml").read_text()
    assert "\n  validate:" not in controller and "objective-validation" not in controller
    assert not Path("loop/validation.py").exists()
    for job in ("verify", "finalize"):
        section = re.split(r"\n  [a-z_]+:", controller.split("\n  " + job + ":", 1)[1])[0]
        if job != "finalize":
            assert "SOURCE_READ_TOKEN" not in section
        assert "REVIEW_LOOP_SOURCE_APP_PRIVATE_KEY" not in section
        assert "create-github-app-token" not in section
        assert "PUBLISHER_TOKEN" not in section and "PUBLISH_TOKEN" not in section
        assert "COPILOT_GITHUB_TOKEN" not in section
    assert "create-github-app-token" not in controller
    assert "SOURCE_READ_TOKEN" in controller.split("\n  coordinate:", 1)[1].split("\n  verify:", 1)[0]
    assert "REVIEW_LOOP_SOURCE_APP_PRIVATE_KEY" not in controller
    assert "PUBLISH_TOKEN" not in text
    live = controller.split("\n  personal_live:", 1)[1]
    assert "python3 -m loop.live" in live and "PUBLISHER_TOKEN:" in live
    assert "PUBLISHER_TOKEN: ${{ secrets[needs.coordinate.outputs.live_publisher_secret] }}" in live
    assert "PUBLISHER_SECRET_NAME: ${{ needs.coordinate.outputs.live_publisher_secret }}" in live
    assert "PUBLISHER_SECRET_MAP: ${{ vars.PUBLISHER_SECRETS }}" in live
    coordinate = controller.split("\n  coordinate:", 1)[1].split("\n  verify:", 1)[0]
    assert "PUBLISHER_TOKEN:" not in coordinate and "toJSON(secrets)" not in controller
    assert "secrets[steps.publisher.outputs.publisher_secret] != ''" in coordinate
    assert "python3 -m loop.cli select-publisher" in coordinate
    assert "COPILOT_GITHUB_TOKEN" not in live and "loop.validation" not in live
    assert "actions/download-artifact" not in live and "persist-credentials: false" in live
    assert "ref: ${{ needs.coordinate.outputs.live_revision }}" in live
    assert "publish" in controller and "fine_grained_pat" in controller
    fast = Path(".github/workflows/validate.yml").read_text()
    assert "\n  push:" in fast and "\n  pull_request:" in fast
    assert "\n  workflow_dispatch:" not in fast
    assert re.findall(r"^  ([a-z_]+):$", fast.split("\njobs:", 1)[1], re.MULTILINE) == ["deterministic"]
    assert not any(value in fast for value in (
        "loop.validation", "no_model_awf", "personal_contract", "gradle", "docker", "secrets.",
        "copilot-review-loop-test", "contents: write"))
    assert not Path(".github/workflows/qualification.yml").exists()
    assert {p.name for p in Path(".github/workflows").glob("*.yml")} == {
        "coordinator.yml", "waiter.yml", "validate.yml", "copilot-worker.lock.yml",
    }
    waiter = Path(".github/workflows/waiter.yml").read_text()
    frontmatter = Path(".github/workflows/copilot-worker.md").read_text().split("\n---\n", 1)[0]
    assert "\nenvironment: protected\n" in frontmatter
    assert "\n  manual-approval: protected\n" in frontmatter
    for workflow in (text, controller, waiter):
        jobs = workflow.split("\njobs:\n", 1)[1]
        for job, section in re.findall(
                r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
                jobs, re.MULTILINE | re.DOTALL):
            secrets = re.findall(r"secrets(?:\.([A-Z][A-Z0-9_]*)|\[)", section)
            if any(secret != "GITHUB_TOKEN" for secret in secrets):
                assert re.search(r"(?m)^    environment: protected$", section), job
    assert "group: central-review-loop-waiter" in waiter and "cancel-in-progress: false" in waiter
    assert "python3 -m loop.waiter" in waiter and "persist-credentials: false" in waiter
    assert "SOURCE_READ_TOKEN" in waiter and "actions: write" in waiter
    assert not any(value in waiter for value in (
        "PUBLISHER_TOKEN", "COPILOT_GITHUB_TOKEN", "loop.validation", "download-artifact"))
    assert "\nconcurrency:" not in controller
    assert "central-review-loop-coordinate-${{ inputs.previous_request || inputs.target || github.run_id }}" in controller
    for action in re.findall(r"^\s+(?:- )?uses: (\S+)", waiter, re.MULTILINE):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), action
    for action in re.findall(r"^\s+(?:- )?uses: (\S+)", fast, re.MULTILINE):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), action
    print("Lock audit passed: strict Copilot/AWF, immutable action/container pins, no write permissions.")


if __name__ == "__main__":
    audit()
