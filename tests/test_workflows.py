import re
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from loop import publication, worker_home
from loop.policy import Rejected


class WorkflowTests(unittest.TestCase):
    def test_worker_and_threat_detection_share_explicit_model_and_effort(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        frontmatter = (root / "copilot-worker.md").read_text(
            encoding="utf-8").split("\n---\n", 1)[0]
        self.assertIn("\nengine:\n  id: copilot\n  version: \"1.0.93\"\n"
                      "  model: gpt-6.1-sol?effort=high\n",
                      frontmatter)
        jobs = (root / "copilot-worker.lock.yml").read_text(
            encoding="utf-8").split("\njobs:\n", 1)[1]
        sections = dict(re.findall(
            r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
            jobs, re.MULTILINE | re.DOTALL))
        for job in ("agent", "detection"):
            with self.subTest(job=job):
                self.assertEqual(["gpt-6.1-sol?effort=high"], re.findall(
                    r"^\s+COPILOT_MODEL: (.+)$", sections[job], re.MULTILINE))
                self.assertIn('install_copilot_cli.sh" 1.0.93', sections[job])
                self.assertEqual(["1.0.93"], re.findall(
                    r'^\s+GH_AW_INFO_VERSION: "(.+)"$', sections[job], re.MULTILINE))

    def test_custom_secrets_require_the_protected_environment(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        expected = {
            "coordinator.yml": {"coordinate", "finalize", "personal_live"},
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
        self.assertIn("needs: [coordinate, verify]", coordinator.split("\n  finalize:", 1)[1])
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
        self.assertIn("Only the trusted frozen request selects `loop_kind`.", prompt)
        self.assertIn("one pass", prompt)
        self.assertIn("a real changed", prompt)
        self.assertIn("an empty patch", prompt)
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
        for output in ("revision", "live_revision"):
            self.assertIn("ref: ${{ needs.coordinate.outputs." + output + " }}", coordinator)

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
        self.assertIn("Record the actual commands", worker)
        self.assertIn("must happen\nthrough bash tools **inside the AWF sandbox**", worker)
        self.assertIn("run: python3 -m loop.worker_home", worker)
        self.assertNotIn("loop.validation", worker)
        staging = worker.split("for name, limit in [", 1)[1].split("]:", 1)[0]
        self.assertNotIn("validation.json", staging)
        for name in ("result.json", "candidate.patch", "diagnostics.txt"):
            self.assertIn(name, staging)

    def test_publisher_routing_exposes_names_and_presence_only_outside_live_job(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        coordinator = (root / "coordinator.yml").read_text(encoding="utf-8")
        coordinate = coordinator.split("\n  coordinate:", 1)[1].split("\n  verify:", 1)[0]
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
        for job in ("verify", "finalize"):
            section = re.split(r"\n  [a-z_]+:", coordinator.split("\n  " + job + ":", 1)[1])[0]
            self.assertNotIn("PUBLISHER_SECRET", section)
            self.assertNotIn("PUBLISHER_TOKEN", section)
        for name in ("copilot-worker.md", "copilot-worker.lock.yml", "waiter.yml"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("PUBLISHER_SECRET", text)
            self.assertNotIn("PUBLISH_TOKEN", text)
