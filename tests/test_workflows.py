import json
import re
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from loop import publication, worker_home
from loop.policy import Rejected


class WorkflowTests(unittest.TestCase):
    def test_worker_and_threat_detection_use_canonical_model_and_transport(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        frontmatter = (root / "copilot-worker.md").read_text(
            encoding="utf-8").split("\n---\n", 1)[0]
        self.assertIn("\nengine:\n  id: copilot\n  version: \"1.0.93\"\n"
                      "  model: gpt-6.1-sol\n"
                      "  args: [\"--reasoning-effort\", \"high\"]\n",
                      frontmatter)
        self.assertIn("\nsandbox:\n  agent:\n    version: v0.28.49\n"
                      "    runtime: docker\n", frontmatter)
        compiled = (root / "copilot-worker.lock.yml").read_text(encoding="utf-8")
        metadata = json.loads(compiled.splitlines()[0].split(": ", 1)[1])
        self.assertEqual("1.0.93", metadata["engine_versions"]["copilot"])
        jobs = compiled.split("\njobs:\n", 1)[1]
        sections = dict(re.findall(
            r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
            jobs, re.MULTILINE | re.DOTALL))
        for job in ("agent", "detection"):
            with self.subTest(job=job):
                self.assertEqual(["gpt-6.1-sol"], re.findall(
                    r"^\s+COPILOT_MODEL: (.+)$", sections[job], re.MULTILINE))
                self.assertIn('install_copilot_cli.sh" 1.0.93', sections[job])
                self.assertEqual(["1.0.93"], re.findall(
                    r'^\s+GH_AW_INFO_VERSION: "(.+)"$', sections[job], re.MULTILINE))
                self.assertEqual(["gpt-6.1-sol"], re.findall(
                    r"^\s+COPILOT_PROVIDER_MODEL_ID: (.+)$", sections[job], re.MULTILINE))
                self.assertEqual(["responses"], re.findall(
                    r"^\s+COPILOT_PROVIDER_WIRE_API: (.+)$", sections[job], re.MULTILINE))
        self.assertIn('install_awf_binary.sh" v0.28.49 --rootless', sections["agent"])
        self.assertIn("--reasoning-effort high", sections["agent"])

    def test_custom_secrets_require_the_protected_environment(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        expected = {
            "coordinator.yml": {"coordinate", "personal_live"},
            "waiter.yml": {"wait"},
            "copilot-worker.lock.yml": {"activation", "agent", "detection"},
            "validate.yml": set(),
        }
        for name, expected_jobs in expected.items():
            text = (root / name).read_text(encoding="utf-8")
            jobs = text.split("\njobs:\n", 1)[1]
            credential_jobs = set()
            for job, section in re.findall(
                    r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
                    jobs, re.MULTILINE | re.DOTALL):
                secrets = re.findall(r"secrets(?:\.([A-Z][A-Z0-9_]*)|\[)", section)
                if any(secret != "GITHUB_TOKEN" for secret in secrets):
                    credential_jobs.add(job)
                    with self.subTest(workflow=name, job=job):
                        self.assertRegex(section, r"(?m)^    environment: protected$")
                if job in {"verify", "deterministic"}:
                    self.assertNotIn("environment:", section)
            self.assertEqual(expected_jobs, credential_jobs, name)
        frontmatter = (root / "copilot-worker.md").read_text(
            encoding="utf-8").split("\n---\n", 1)[0]
        self.assertIn("\nenvironment: protected\n", frontmatter)
        self.assertIn("\n  manual-approval: protected\n", frontmatter)

    def test_repository_checks_are_fast_and_have_no_standalone_integration_workflow(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        self.assertEqual({"validate.yml", "coordinator.yml", "waiter.yml", "copilot-worker.lock.yml"},
                         {path.name for path in root.glob("*.yml")})
        fast = (root / "validate.yml").read_text(encoding="utf-8")
        self.assertEqual(["deterministic"], re.findall(
            r"^  ([a-z_]+):$", fast.split("\njobs:", 1)[1], re.MULTILINE))
        self.assertEqual({"push", "pull_request"}, set(re.findall(
            r"^  ([a-z_]+):$", fast.split("\non:", 1)[1].split("\npermissions:", 1)[0], re.MULTILINE)))
        self.assertEqual([
            "python3 -m unittest discover -v",
            "node --test .github/extensions/workflow-dashboard/dashboard.test.mjs "
            ".github/extensions/workflow-dashboard/pr-dashboard.test.mjs",
            "python3 tools/compiler.py", "python3 tools/lint.py", "|"
        ], re.findall(r"^\s+run: (.*)$", fast, re.MULTILINE))
        for forbidden in ("loop.validation", "gradle", "docker", "copilot-review-loop-test",
                          "secrets.", "no_model_awf", "personal_contract"):
            self.assertNotIn(forbidden, fast)
        coordinator = (root / "coordinator.yml").read_text(encoding="utf-8")
        self.assertNotIn("\n  validate:", coordinator)
        self.assertNotIn("loop.validation", coordinator)
        self.assertNotIn("objective-validation", coordinator)
        self.assertEqual(["coordinate", "personal_live"], re.findall(
            r"^  ([a-z_]+):$", coordinator.split("\njobs:", 1)[1], re.MULTILINE))
        self.assertIn("run: python3 -m loop.live", coordinator)
        self.assertIn("path: verification-report.json", coordinator)
        self.assertNotIn("qualify", coordinator)
        self.assertFalse(hasattr(publication, "qualified_reviewable"))
        self.assertFalse((root.parents[1] / "loop" / "validation.py").exists())

    def test_worker_home_has_no_command_execution_entry_point(self):
        script = Path(worker_home.__file__)
        for operation in ("prepare-worker-home", "validate", "unknown"):
            with self.subTest(operation=operation), patch(
                    "sys.argv", [str(script), operation]), self.assertRaisesRegex(
                        Rejected, "Unsupported worker home operation"), patch(
                        "loop.worker_home.prepare_home") as prepare:
                runpy.run_path(str(script), run_name="__main__")
            prepare.assert_not_called()

    def test_java_checks_use_preinstalled_runtime_and_writable_gradle_home(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        worker = (root / "copilot-worker.md").read_text(encoding="utf-8")
        compiled = (root / "copilot-worker.lock.yml").read_text(encoding="utf-8")
        for text in (worker, compiled):
            with self.subTest(compiled=text is compiled):
                self.assertIn("GRADLE_USER_HOME: /tmp/review-loop-worker-home/.gradle", text)
                self.assertIn("actions/setup-java@b6effb05e454b25005698d916606bdc6ffcbf961", text)
                self.assertIn("distribution: temurin", text)
                self.assertIn('java-version: "25"', text)
                self.assertLess(text.index("Install Java runtime for sandbox checks"),
                                text.index("Prepare fresh writable sandbox home"))
        self.assertIn("preserve AWF's\n`JAVA_TOOL_OPTIONS` proxy settings", worker)

    def test_mise_installs_can_reach_version_metadata_and_sigstore_trust_roots(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        worker = (root / "copilot-worker.md").read_text(encoding="utf-8")
        network = worker.split("\nnetwork:\n", 1)[1].split("\ntools:", 1)[0]
        declared = set(re.findall(r"^    - (\S+)$", network, re.MULTILINE))
        self.assertLessEqual({"mise-versions.jdx.dev", "tuf-repo-cdn.sigstore.dev"}, declared)
        compiled = (root / "copilot-worker.lock.yml").read_text(encoding="utf-8")
        domains = re.search(r'\\"allowDomains\\":(\[.*?\])', compiled)[1]
        self.assertEqual(declared, set(json.loads(domains.replace('\\"', '"'))))

    def test_launch_titles_identify_task_and_target_without_changing_tick_identity(self):
        coordinator = (Path(__file__).resolve().parents[1] /
                       ".github" / "workflows" / "coordinator.yml").read_text(encoding="utf-8")
        title = re.search(r"^run-name: (.*)$", coordinator, re.MULTILINE)[1]
        self.assertIn("inputs.operation == 'launch'", title)
        self.assertIn("format('Review loop launch {0} {1}', inputs.loop_kind, inputs.target)", title)
        self.assertIn("format('Review loop {0} {1}', inputs.operation || 'tick', "
                      "inputs.previous_request || github.run_id)", title)

    def test_only_shared_waiter_serializes_globally_and_has_a_bounded_runner(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        waiter = (root / "waiter.yml").read_text(encoding="utf-8")
        coordinator = (root / "coordinator.yml").read_text(encoding="utf-8")
        self.assertEqual(["wait"], re.findall(
            r"^  ([a-z_]+):$", waiter.split("\njobs:", 1)[1], re.MULTILINE))
        self.assertIn("\nconcurrency:\n  group: central-review-loop-waiter\n"
                      "  cancel-in-progress: false", waiter)
        self.assertIn("timeout-minutes: 65", waiter)
        self.assertIn("types: [in_progress, completed]", waiter)
        self.assertNotIn("\nconcurrency:", coordinator)
        self.assertIn("inputs.previous_request || inputs.target || github.run_id", coordinator)
        self.assertIn("SOURCE_READ_TOKEN:", waiter)
        for forbidden in ("PUBLISHER_TOKEN", "COPILOT_GITHUB_TOKEN", "download-artifact", "loop.validation"):
            self.assertNotIn(forbidden, waiter)

    def test_loop_kind_is_a_coordinator_input_not_worker_or_waiter_authority(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        coordinator = (root / "coordinator.yml").read_text(encoding="utf-8")
        from loop.policy import LOOP_KINDS
        choices = re.search(r"      loop_kind:.*?options: \[([^\]]+)\]", coordinator, re.DOTALL)[1]
        self.assertEqual(LOOP_KINDS, set(choices.split(", ")))
        self.assertIn("LOOP_KIND: ${{ inputs.loop_kind || 'copilot_review' }}", coordinator)
        worker = (root / "copilot-worker.md").read_text(encoding="utf-8")
        frontmatter, prompt = worker.split("\n---\n", 1)
        self.assertNotIn("loop_kind:", frontmatter)
        self.assertIn("Only the trusted request selects\nthe task", prompt)
        self.assertIn("Blocked tasks return\nno candidate commits", prompt)
        waiter = (root / "waiter.yml").read_text(encoding="utf-8")
        self.assertNotIn("loop_kind:", waiter)
        self.assertEqual(1, waiter.count("group: central-review-loop-waiter"))

    def test_pinned_worker_events_and_targeted_ticks_use_revision_branches(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        coordinator = (root / "coordinator.yml").read_text(encoding="utf-8")
        waiter = (root / "waiter.yml").read_text(encoding="utf-8")
        for text in (coordinator, waiter):
            self.assertIn('branches: [main, "review-loop-revisions/**"]', text)
        condition = re.search(r"^    if: (.*)$", coordinator, re.MULTILINE)[1]
        self.assertIn("github.repository == 'trask/copilot-workflows'", condition)
        self.assertIn("github.ref == 'refs/heads/main'", condition)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.operation == 'tick'", condition)
        self.assertIn("startsWith(github.ref, 'refs/heads/review-loop-revisions/')", condition)
        worker = (root / "copilot-worker.md").read_text(encoding="utf-8")
        self.assertIn("ref: ${{ github.sha }}", worker)
        self.assertIn("ref: ${{ needs.coordinate.outputs.live_revision }}", coordinator)

    def test_unpublished_restart_is_explicit_coordinator_input_only(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        coordinator = (root / "coordinator.yml").read_text(encoding="utf-8")
        self.assertIn("restart_unpublished:", coordinator)
        self.assertIn("RESTART_UNPUBLISHED: ${{ inputs.restart_unpublished }}", coordinator)
        definition = coordinator.split("      restart_unpublished:", 1)[1].split("  workflow_run:", 1)[0]
        self.assertIn("type: boolean", definition)
        self.assertIn("default: false", definition)
        for file in ("waiter.yml", "copilot-worker.md"):
            text = (root / file).read_text(encoding="utf-8")
            self.assertNotIn("RESTART_UNPUBLISHED", text)
            self.assertNotIn("restart_unpublished:", text)

    def test_three_file_worker_outputs_keep_repository_checks_inside_awf(self):
        worker = (Path(__file__).resolve().parents[1] /
                  ".github" / "workflows" / "copilot-worker.md").read_text(encoding="utf-8")
        self.assertIn("Return exactly these three regular files", worker)
        self.assertIn("record actual check commands and results", worker)
        self.assertIn("through bash inside AWF", worker)
        self.assertIn("run: python3 -m loop.worker_home", worker)
        self.assertNotIn("loop.validation", worker)
        staging = worker.split("for name, limit in [", 1)[1].split("]:", 1)[0]
        self.assertNotIn("validation.json", staging)
        for name in ("result.json", "candidate.bundle", "diagnostics.txt"):
            self.assertIn(name, staging)

    def test_completed_no_change_investigations_do_not_require_builds(self):
        worker = (Path(__file__).resolve().parents[1] /
                  ".github" / "workflows" / "copilot-worker.md").read_text(encoding="utf-8")
        self.assertIn("Simplify and consistency tasks don't need builds solely to validate unchanged code", worker)
        self.assertIn("An incomplete investigation or a check needed to decide a change still blocks", worker)
        self.assertIn("Source changes require appropriate existing formatting and focused checks", worker)
        self.assertIn("including on a no-change pass", worker)
        self.assertIn("Run appropriate checks on every pass", worker)

    def test_publisher_routing_exposes_names_and_presence_only_outside_live_job(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        coordinator = (root / "coordinator.yml").read_text(encoding="utf-8")
        coordinate = coordinator.split("\n  coordinate:", 1)[1].split("\n  personal_live:", 1)[0]
        live = coordinator.split("\n  personal_live:", 1)[1]
        self.assertIn("if: inputs.operation == 'launch'", coordinate)
        self.assertIn('python3 -m loop.cli select-publisher --target "$TARGET"', coordinate)
        self.assertIn("vars.PUBLISHER_SECRETS", coordinate)
        self.assertNotIn("REVIEW_LOOP_PUBLISHER_SECRETS", coordinator)
        self.assertIn("PUBLISHER_HEAD_REPO: ${{ steps.publisher.outputs.publisher_head_repo }}",
                      coordinate)
        self.assertIn("secrets[steps.publisher.outputs.publisher_secret] != ''", coordinate)
        self.assertIn("secrets[steps.publisher.outputs.target_publisher_secret] != ''", coordinate)
        self.assertIn("TARGET_PUBLISHER_SECRET_NAME: ${{ steps.publisher.outputs.target_publisher_secret }}",
                      coordinate)
        self.assertNotIn("PUBLISHER_TOKEN:", coordinate)
        self.assertNotIn("toJSON(secrets)", coordinator)
        self.assertIn("PUBLISHER_TOKEN: ${{ secrets[needs.coordinate.outputs.live_publisher_secret] }}",
                      live)
        self.assertIn("PUBLISHER_SECRET_NAME: ${{ needs.coordinate.outputs.live_publisher_secret }}",
                      live)
        self.assertIn("TARGET_PUBLISHER_TOKEN: ${{ secrets[needs.coordinate.outputs.live_target_publisher_secret] }}",
                      live)
        self.assertIn("TARGET_PUBLISHER_SECRET_NAME: ${{ needs.coordinate.outputs.live_target_publisher_secret }}",
                      live)
        for name in ("copilot-worker.md", "copilot-worker.lock.yml", "waiter.yml"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("PUBLISHER_SECRET", text)
            self.assertNotIn("PUBLISH_TOKEN", text)
