import copy
import unittest
from unittest.mock import Mock

from loop.api import APIError
from loop.policy import Rejected
from loop.reviews import exact_ci
from tests.fixtures import CI_CHECK, FIXTURE
from tests.test_live import Read
from tests.test_loop import SHA


class IndependentWorkflowCITests(unittest.TestCase):
    def context(self):
        read = Read()
        checks, executions, jobs = [], {}, {}
        for offset in (0, 1):
            run_id, check_id, suite_id = 200 + offset, 333 + offset, 500 + offset
            checks.append(dict(read.checks[0], id=check_id,
                               details_url=f"https://github.com/{FIXTURE}/actions/runs/{run_id}/job/{check_id}",
                               check_suite={"id": suite_id}))
            executions[run_id] = dict(read.runs[0], id=run_id, workflow_id=600 + offset,
                                      check_suite_id=suite_id,
                                      path=f".github/workflows/test-{offset}.yml")
            jobs[run_id] = [{"id": check_id, "run_id": run_id, "run_attempt": 1, "name": CI_CHECK,
                             "check_run_url": f"https://api.github.com/repos/{FIXTURE}/check-runs/{check_id}"}]
        read.checks = checks
        call, pages = read.call, read.pages
        read.call = Mock(side_effect=lambda path: (
            executions[int(path.rsplit("/", 1)[1])]
            if path.startswith(f"repos/{FIXTURE}/actions/runs/") else call(path)))
        read.pages = Mock(side_effect=lambda path, key=None: (
            jobs[int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])]
            if path.endswith("/attempts/1/jobs") else pages(path, key)))
        return read, executions, jobs

    def test_distinct_verified_workflows_with_matching_names_all_must_pass(self):
        for status, conclusion, expected in [
            ("completed", "success", "passed"),
            ("in_progress", None, "pending"),
            ("completed", "failure", "failed"),
            ("completed", "skipped", "passed"),
            ("completed", "neutral", "passed"),
        ]:
            read, _, _ = self.context()
            read.checks[1].update(status=status, conclusion=conclusion)
            with self.subTest(status=status, conclusion=conclusion):
                result = exact_ci(read, FIXTURE, SHA, [CI_CHECK])
            self.assertEqual(expected, result["decision"])
            self.assertEqual([333, 334], result["checks"][0]["ids"])
            self.assertEqual([600, 601], [item["workflow_id"]
                                        for item in result["checks"][0]["workflows"]])

    def test_duplicate_contexts_require_bound_independent_first_attempt_workflows(self):
        for mutation in [
            "same_workflow", "same_path", "duplicate_check", "foreign_link", "foreign_app",
            "unknown_app", "wrong_check_head", "wrong_run_head", "wrong_repository",
            "wrong_run_id", "rerun", "wrong_suite", "missing_suite", "dynamic_path",
            "missing_job", "duplicate_job", "wrong_job_id", "wrong_job_run", "job_rerun",
            "wrong_job_name", "wrong_check_link", "mixed_status",
        ]:
            read, executions, jobs = self.context()
            if mutation == "same_workflow":
                executions[201]["workflow_id"] = 600
            elif mutation == "same_path":
                executions[201]["path"] = executions[200]["path"]
            elif mutation == "duplicate_check":
                read.checks[1]["id"] = 333
            elif mutation == "foreign_link":
                read.checks[1]["details_url"] = "https://example.com/actions/runs/201/job/334"
            elif mutation == "foreign_app":
                read.checks[1]["app"] = {"id": 99, "slug": "github-actions"}
            elif mutation == "unknown_app":
                read.checks[1]["app"] = {"id": 15368, "slug": "unknown"}
            elif mutation == "wrong_check_head":
                read.checks[1]["head_sha"] = "0" * 40
            elif mutation == "wrong_run_head":
                executions[201]["head_sha"] = "0" * 40
            elif mutation == "wrong_repository":
                executions[201]["repository"] = {"full_name": "other/repository"}
            elif mutation == "wrong_run_id":
                executions[201]["id"] = 202
            elif mutation == "rerun":
                executions[201]["run_attempt"] = 2
            elif mutation == "wrong_suite":
                executions[201]["check_suite_id"] = 502
            elif mutation == "missing_suite":
                executions[201].pop("check_suite_id")
                read.checks[1].pop("check_suite")
            elif mutation == "dynamic_path":
                executions[201]["path"] = "dynamic/other/workflow"
            elif mutation == "missing_job":
                jobs[201] = []
            elif mutation == "duplicate_job":
                jobs[201] *= 2
            elif mutation == "wrong_job_id":
                jobs[201][0]["id"] = 335
            elif mutation == "wrong_job_run":
                jobs[201][0]["run_id"] = 200
            elif mutation == "job_rerun":
                jobs[201][0]["run_attempt"] = 2
            elif mutation == "wrong_job_name":
                jobs[201][0]["name"] = "different"
            elif mutation == "wrong_check_link":
                jobs[201][0]["check_run_url"] = f"https://api.github.com/repos/{FIXTURE}/check-runs/335"
            elif mutation == "mixed_status":
                read.statuses = [{"id": 335, "context": CI_CHECK, "state": "success"}]
            with self.subTest(mutation=mutation):
                result = exact_ci(read, FIXTURE, SHA, [CI_CHECK])
            self.assertEqual("unknown", result["decision"])
            self.assertEqual("duplicate_or_unbound_CI_identity", result["checks"][0]["reason"])

    def test_execution_proofs_are_cached_across_check_names(self):
        read, _, jobs = self.context()
        for offset, check in enumerate(copy.deepcopy(read.checks)):
            check.update(id=433 + offset, name="another check",
                         details_url=f"https://github.com/{FIXTURE}/actions/runs/{200 + offset}/job/{433 + offset}")
            read.checks.append(check)
            jobs[200 + offset].append({
                "id": check["id"], "run_id": 200 + offset, "run_attempt": 1, "name": check["name"],
                "check_run_url": f"https://api.github.com/repos/{FIXTURE}/check-runs/{check['id']}"})
        result = exact_ci(read, FIXTURE, SHA, [CI_CHECK, "another check"])
        self.assertEqual("passed", result["decision"])
        self.assertEqual(2, read.call.call_count)
        self.assertEqual(2, sum(call.args[0].endswith("/jobs") for call in read.pages.call_args_list))

    def test_provenance_read_failures_remain_explicit_errors(self):
        read, _, _ = self.context()
        read.call.side_effect = APIError(403, "Missing Actions read access")
        with self.assertRaisesRegex(APIError, "Actions read access"):
            exact_ci(read, FIXTURE, SHA, [CI_CHECK])

    def test_duplicate_workflow_collection_is_bounded_at_one_hundred_executions(self):
        read, executions, jobs = self.context()
        for offset in range(2, 101):
            read.checks.append(dict(
                read.checks[0], id=333 + offset, check_suite={"id": 500 + offset},
                details_url=f"https://github.com/{FIXTURE}/actions/runs/{200 + offset}/job/{333 + offset}"))
            executions[200 + offset] = dict(
                executions[200], id=200 + offset, workflow_id=600 + offset,
                check_suite_id=500 + offset, path=f".github/workflows/test-{offset}.yml")
            jobs[200 + offset] = [{
                "id": 333 + offset, "run_id": 200 + offset, "run_attempt": 1, "name": CI_CHECK,
                "check_run_url": f"https://api.github.com/repos/{FIXTURE}/check-runs/{333 + offset}"}]
        last = read.checks.pop()
        self.assertEqual("passed", exact_ci(read, FIXTURE, SHA, [CI_CHECK])["decision"])
        read.checks.append(last)
        with self.assertRaisesRegex(Rejected, "execution limit"):
            exact_ci(read, FIXTURE, SHA, [CI_CHECK])
