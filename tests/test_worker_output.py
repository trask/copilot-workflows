import copy
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
        Path(directory, "candidate.patch").write_bytes(patch_bytes)
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

    def test_patch_spans_are_checked_without_source_or_git_execution(self):
        req = task_request("pr_simplify")
        value = result(req, "fixes", GOOD_PATCH)
        with tempfile.TemporaryDirectory() as directory, patch(
                "loop.verify.git", side_effect=AssertionError("Self-check must not execute Git")):
            self.files(directory, value, GOOD_PATCH)
            worker_output.check_output(req, Path(directory))
            self.files(directory, value, GOOD_PATCH + b"extra")
            with self.assertRaisesRegex(Rejected, "unaccounted"):
                worker_output.check_output(req, Path(directory))
            self.files(directory, value, GOOD_PATCH.replace(b"+new", b"+bad"))
            with self.assertRaisesRegex(Rejected, "hash differs"):
                worker_output.check_output(req, Path(directory))
            req = task_request("pr_conflict_resolver")
            self.files(directory, result(req, "merge"), GOOD_PATCH)
            worker_output.check_output(req, Path(directory))

    def test_missing_extra_oversized_and_duplicate_key_outputs_are_rejected(self):
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
            Path(directory, "candidate.patch").write_bytes(b"x" * (worker_output.MAX_PATCH + 1))
            with self.assertRaisesRegex(Rejected, "exceeds limit"):
                worker_output.check_output(req, root)
            self.files(directory, result(req))
            Path(directory, "diagnostics.txt").unlink()
            Path(directory, "diagnostics.txt").mkdir()
            with self.assertRaisesRegex(Rejected, "Non-regular"):
                worker_output.check_output(req, root)

    def test_entry_point_accepts_no_caller_selected_operation(self):
        script = Path(worker_output.__file__)
        with patch("sys.argv", [str(script), "unknown"]), self.assertRaisesRegex(
                Rejected, "Unsupported worker output operation"):
            runpy.run_path(str(script), run_name="__main__")

    def test_worker_prompt_constructs_a_valid_no_change_result_from_frozen_inputs(self):
        req = task_request("pr_description")
        prompt = (Path(__file__).resolve().parents[1] / ".github" /
                  "workflows" / "copilot-worker.md").read_text(encoding="utf-8")
        example = prompt.split("```python\n", 1)[1].split("\n```", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "loop-output"
            output.mkdir()
            self.files(output, result(req))
            Path(directory, "frozen-request.json").write_bytes(canonical(req))
            Path(directory, "frozen-digest.txt").write_text(digest(req), encoding="ascii")
            original_path = Path
            def in_workspace(path):
                return original_path(directory, path)
            with patch("pathlib.Path", in_workspace):
                exec(example, {})
            worker_output.check_output(req, output)
