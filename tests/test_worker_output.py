import copy
import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loop import worker_output
from loop.policy import LOOP_KINDS, Rejected, canonical, digest
from tests.support import semantic as review_result
from tests.test_loop import GOOD_PATCH
from tests.test_pr_tasks import result, task_request


class WorkerOutputTests(unittest.TestCase):
    def files(self, directory, value, patch_bytes=b""):
        Path(directory, "result.json").write_bytes(canonical(value))
        Path(directory, "candidate.bundle").write_bytes(patch_bytes)
        Path(directory, "diagnostics.txt").write_bytes(b"Investigated frozen request.")

    def test_all_task_schemas_use_the_existing_strict_semantics(self):
        for kind in LOOP_KINDS:
            req = task_request(kind)
            value = (review_result(req, "no_change", disposition="not_warranted")
                     if kind == "copilot_review" else
                     result(req, "clean" if kind == "self_review" else "no_change"))
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.files(directory, value)
                worker_output.check_output(req, Path(directory))
                changed = dict(value, unexpected=True)
                self.files(directory, changed)
                with self.assertRaises(Rejected):
                    worker_output.check_output(req, Path(directory))

    def test_documented_conflict_result_uses_the_packager_merge_outcome(self):
        worker = (Path(__file__).resolve().parents[1] /
                  ".github" / "workflows" / "copilot-worker.md").read_text(encoding="utf-8")
        example = worker.split("For a completed conflict resolution,", 1)[1].split("```json\n", 1)[1]
        value = json.loads(example.split("\n```", 1)[0])
        req = task_request("pr_conflict_resolver")
        req.update(input_mode="direct", inputs={"identity": {"pr_diff_sha256": "a" * 64}})
        value["request_digest"] = digest(req)
        value["input_identity"] = req["inputs"]["identity"]
        self.assertEqual("merge", value["outcome"])
        with tempfile.TemporaryDirectory() as directory:
            self.files(directory, value, b"native merge bundle")
            worker_output.check_output(req, Path(directory))
            value["outcome"] = "fixes"
            self.files(directory, value, b"native merge bundle")
            with self.assertRaisesRegex(Rejected, "Unknown worker outcome"):
                worker_output.check_output(req, Path(directory))

    def test_failed_description_shape_is_rejected_and_frozen_metadata_passes(self):
        req = task_request("pr_description")
        req["metadata"]["body"] = "Body with\n\n```text\nsetting=value\n```\nUnicode \u00e9."
        value = result(req)
        value["proposal"] = None
        with tempfile.TemporaryDirectory() as directory:
            self.files(directory, value)
            with self.assertRaisesRegex(Rejected, "including for no_change"):
                worker_output.check_output(req, Path(directory))
            value["proposal"] = copy.deepcopy(req["metadata"])
            self.files(directory, value)
            worker_output.check_output(req, Path(directory))
            self.assertEqual(canonical(value), Path(directory, "result.json").read_bytes())
            value["proposal"]["title"] = "Changed"
            self.files(directory, value)
            with self.assertRaisesRegex(Rejected, "Metadata outcome contradicts"):
                worker_output.check_output(req, Path(directory))

    def test_no_change_preserves_optional_build_failure_diagnostics(self):
        for kind in ("pr_simplify", "pr_consistency"):
            req = task_request(kind)
            value = result(req)
            diagnostics = (b"Full investigation found no qualifying change.\n"
                           b"Optional Gradle compilation exited 1: invalid source release: 21.\n"
                           b"Available JDK is 17; no compilation or CI success is claimed.\n")
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.files(directory, value)
                Path(directory, "diagnostics.txt").write_bytes(diagnostics)
                worker_output.check_output(req, Path(directory))
                self.assertEqual(canonical(value), Path(directory, "result.json").read_bytes())
                self.assertEqual(diagnostics, Path(directory, "diagnostics.txt").read_bytes())
                self.assertEqual(b"", Path(directory, "candidate.bundle").read_bytes())


    def test_missing_extra_and_duplicate_key_outputs_are_rejected(self):
        req = task_request("pr_description")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.files(directory, result(req))
            Path(directory, "extra.txt").write_bytes(b"extra")
            with self.assertRaisesRegex(Rejected, "exactly the three"):
                worker_output.check_output(req, root)
            Path(directory, "extra.txt").unlink()
            Path(directory, "diagnostics.txt").unlink()
            with self.assertRaisesRegex(Rejected, "exactly the three"):
                worker_output.check_output(req, root)
            self.files(directory, result(req))
            Path(directory, "result.json").write_bytes(b'{"schema":2,"schema":2}')
            with self.assertRaisesRegex(Rejected, "Duplicate JSON key"):
                worker_output.check_output(req, root)
            self.files(directory, result(req))
            Path(directory, "diagnostics.txt").unlink()
            Path(directory, "diagnostics.txt").mkdir()
            with self.assertRaisesRegex(Rejected, "Non-regular"):
                worker_output.check_output(req, root)

    def test_entry_point_accepts_no_caller_selected_operation(self):
        script = Path(worker_output.__file__)
        with patch("sys.argv", [str(script), "unknown", "extra"]), self.assertRaisesRegex(
                Rejected, "optional target repository"):
            runpy.run_path(str(script), run_name="__main__")
