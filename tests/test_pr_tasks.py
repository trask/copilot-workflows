import copy
import hashlib
import io
import json
import os
import tempfile
import textwrap
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from loop.api import API, APIError
from loop.candidates import semantic
from loop.ci import collect, diagnoses, same_attempts
from loop.coordinator import cancel, checkpoint, quiescent
from loop.freeze import freeze
from loop.live import advance, start, watch_ci_fix, watch_single
from loop.policy import (AUTHOR_ID, CENTRAL, LOOP_KINDS, Rejected, candidate_outcome, check_target,
                         canonical, digest, effect_repository, eligible, pipeline_budget)
from loop.policy import iso, pipeline_limit, staged_source
from loop.publication import PublisherAPI, authenticated_push, evidence, import_candidate
from loop.recommendations import collect_diff, comments, description_diff, diff_anchors
from loop.source import SourceAPI, import_source, package_source
from loop.task_effects import confirm_task, publish_task
from loop.revisions import revision_ref
from loop.verify import git, reconstruct, verify
from loop.worker_output import check_output
from tests.fixtures import CI_CHECK, FIXTURE
from tests.support import REASONING, batch
from tests.test_live import personal_pr, personal_request, stored, zipped, TEST_TOKEN
from tests.test_loop import FakeAPI, MemoryState, REVISION, SHA, GOOD_PATCH, run
from tests.test_self_review import BASE, MERGE_BASE, SelfRead, self_request


def task_request(kind):
    req = self_request()
    req["loop_kind"] = kind
    req["pr_diff"] = {"text": GOOD_PATCH.decode(),
                      "sha256": hashlib.sha256(GOOD_PATCH).hexdigest(),
                      "anchors": {"Foo.java": [1]}}
    if kind == "pr_description":
        req["pr_diff"] = description_diff(GOOD_PATCH.decode())
        req["metadata"] = {"title": "Original", "body": "Original body"}
    if kind == "pr_review":
        req["pr_author_id"] = 999
    if kind == "ci_fix":
        req["ci_evidence"] = {"sha": req["frozen_sha"], "decision": "failed",
                              "required": [CI_CHECK], "checks": [], "runs": [],
                              "failures": [{"key": "check:333", "availability": "job_log",
                                            "evidence": "temporary runner network outage",
                                            "actions": {"run_id": 200, "attempt": 1}}]}
    return req


def result(req, outcome="no_change", patch_bytes=b""):
    value = {"schema": 2, "request_digest": digest(req), "outcome": outcome,
             "batches": [batch(patch_bytes)] if patch_bytes else []}
    kind = req["loop_kind"]
    if kind == "pr_description":
        value["proposal"] = dict(req["metadata"])
    elif kind == "pr_review":
        value["comments"] = []
    elif kind == "pr_consistency":
        value["consistency"] = [{"path": "Foo.java", "classification": "avoidable",
                                 "explanation": "Use the compliant nearby check",
                                 "citations": ["Foo.java:1"]}]
    elif kind == "pr_conflict_resolver":
        value["merge"] = dict(REASONING, summary="Merge current base")
    elif kind == "ci_fix":
        value.update(rerun_run=None, diagnoses=[{"key": "check:333", "decision": "unrelated",
                                               "analysis": "Failure predates the changed code",
                                               "evidence": ["temporary runner network outage"]}])
    return value


class TaskRead(SelfRead):
    def __init__(self, req):
        super().__init__(req)
        self.pr.update(title="Original", body="Original body", changed_files=1)
        self.diff = req["pr_diff"]["text"]
        if req["loop_kind"] == "pr_review":
            self.pr["user"]["id"] = req["pr_author_id"]
        self.reviews = []
        self.comments = []
        self.log = b"temporary runner network outage"
        self.runs[0].update(check_suite_id=500, run_number=1)
        self.checks[0].update(check_suite={"id": 500},
                              details_url=f"https://github.com/{FIXTURE}/actions/runs/200/job/333")
        self.jobs = [{"id": 333, "name": CI_CHECK, "run_id": 200, "run_attempt": 1,
                     "check_run_url": f"https://api.github.com/repos/{FIXTURE}/check-runs/333"}]

    def call(self, path, *args, **kwargs):
        if path == f"user/{AUTHOR_ID}":
            return {"id": AUTHOR_ID, "login": self.req["commit_author"]["login"]}
        if kwargs.get("raw"):
            self.paths.append(path)
            return self.diff.encode()
        if path == f"repos/{FIXTURE}/actions/runs/200":
            return copy.deepcopy(self.runs[0])
        return super().call(path, *args)

    def pages(self, path, key=None):
        if path.endswith("/status"):
            return copy.deepcopy(list({s["context"]: s for s in reversed(self.statuses)}.values()))
        if path.endswith("/files"):
            return [{"filename": "Foo.java", "additions": 1, "deletions": 1}]
        if path.endswith("/reviews"):
            return copy.deepcopy(self.reviews)
        if path.endswith("/comments"):
            return copy.deepcopy(self.comments)
        if path.endswith("/jobs"):
            return copy.deepcopy(self.jobs)
        return super().pages(path, key)

    def signed_download(self, path, limit=None, *, log_windows=()):
        return self.log


class TaskPublisher:
    def __init__(self, read):
        self.read = read
        self.effect = None
        self.posts = []
        self.uncertain = False
        self.denied = False

    def identity(self, req):
        return {"actor_id": AUTHOR_ID}

    def bind_effect(self, intent):
        self.effect = intent

    def pages(self, path, key=None):
        return self.read.pages(path, key)

    def call(self, path, method="GET", data=None):
        if method == "GET":
            return self.read.call(path)
        self.posts.append((path, method, data))
        if self.denied:
            raise APIError(403, "permission rejected")
        if path.endswith("/rerun-failed-jobs"):
            self.read.runs[0]["run_attempt"] += 1
        elif method == "PATCH":
            self.read.pr.update(data)
        else:
            review = {"id": 42, "state": "PENDING", "submitted_at": None,
                      "user": {"id": AUTHOR_ID}, "commit_id": data["commit_id"]}
            self.read.reviews.append(review)
            self.read.comments = data["comments"]
        if self.uncertain:
            raise APIError(503, "lost response")
        return self.read.reviews[-1] if self.read.reviews else None


def task_state(req, value):
    state = checkpoint(req)
    state.update(stage="publish_pending", iteration=1, generation=req["publication"]["generation"],
                 publications=[], effects=[], report={"dispositions": value})
    return state


class TaskContractsTests(unittest.TestCase):
    def test_all_eight_kinds_keep_existing_default_and_exact_new_schemas(self):
        self.assertEqual(8, len(LOOP_KINDS))
        for kind in LOOP_KINDS - {"copilot_review", "self_review", "pr_conflict_resolver"}:
            req = task_request(kind)
            value = result(req)
            semantic(value, req)
            for mutation in ("extra", "foreign_kind", "source_batch"):
                changed = copy.deepcopy(value)
                if mutation == "extra":
                    changed["event"] = "APPROVE"
                elif mutation == "foreign_kind":
                    changed["request_digest"] = "0" * 64
                else:
                    changed["batches"] = [batch(GOOD_PATCH)]
                with self.subTest(kind=kind, mutation=mutation), self.assertRaises(Rejected):
                    semantic(changed, req)

    def test_reviewer_exception_does_not_authorize_source_or_metadata_on_other_authors(self):
        pr = personal_pr()
        pr["user"]["id"] = 999
        self.assertEqual(999, eligible(pr, FIXTURE, AUTHOR_ID, "pr_review")["pr_author_id"])
        self.assertEqual(AUTHOR_ID, eligible(
            personal_pr(), FIXTURE, AUTHOR_ID, "pr_review")["pr_author_id"])
        for kind in LOOP_KINDS - {"pr_review"}:
            with self.subTest(kind=kind), self.assertRaises(Rejected):
                eligible(pr, FIXTURE, AUTHOR_ID, kind)
        for kind in ("pr_description", "pr_review"):
            req = task_request(kind)
            req["head_repo"] = "fork/head"
            self.assertEqual(FIXTURE, effect_repository(req))
            with self.assertRaises(Rejected):
                authenticated_push(".", req, "f" * 40, TEST_TOKEN)

    def test_reviewer_freezes_own_pr_and_rechecks_author_identity(self):
        req = task_request("pr_review")
        req["pr_author_id"] = AUTHOR_ID
        read = TaskRead(req)
        frozen = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="pr_review")
        self.assertEqual(AUTHOR_ID, frozen["pr_author_id"])
        self.assertEqual(AUTHOR_ID, frozen["commit_author"]["id"])
        self.assertEqual(req["pr_diff"], frozen["pr_diff"])
        self.assertEqual("source_pending", start(
            MemoryState(), FakeAPI(), frozen, "", 0, True,
            "fine_grained_pat", [], True, 100)[1]["stage"])
        check_target(read, frozen)
        read.pr["user"]["id"] = 999
        with self.assertRaisesRegex(Rejected, "Target changed after freeze"):
            check_target(read, frozen)

    def test_reviewer_freezes_bot_authors_without_granting_owner_only_task_access(self):
        req = task_request("pr_review")
        read = TaskRead(req)
        read.pr["user"].update(type="Bot", login="dependabot[bot]")
        frozen = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="pr_review")
        self.assertEqual(999, frozen["pr_author_id"])
        self.assertEqual(req["commit_author"], frozen["commit_author"])
        self.assertEqual(req["pr_diff"], frozen["pr_diff"])
        check_target(read, frozen)
        for kind in LOOP_KINDS - {"pr_review"}:
            with self.subTest(kind=kind), self.assertRaisesRegex(Rejected, "Wrong author"):
                eligible(read.pr, FIXTURE, AUTHOR_ID, kind)
        for author_type in ("Organization", "unknown", None):
            read.pr["user"]["type"] = author_type
            with self.subTest(author_type=author_type), self.assertRaisesRegex(Rejected, "Wrong author"):
                eligible(read.pr, FIXTURE, AUTHOR_ID, "pr_review")
        read.pr["user"]["type"] = "Bot"
        read.pr["user"]["id"] = AUTHOR_ID
        for kind in LOOP_KINDS - {"pr_review"}:
            with self.subTest(kind=kind), self.assertRaisesRegex(Rejected, "Wrong author"):
                eligible(read.pr, FIXTURE, AUTHOR_ID, kind)
        with self.assertRaisesRegex(Rejected, "Target changed after freeze"):
            check_target(read, frozen)

    def test_copilot_pr_attributed_to_owner_freezes_owner_identity_and_rechecks_ownership(self):
        for kind in ("self_review", "pr_description"):
            req = task_request(kind)
            read = TaskRead(req)
            read.pr["user"].update(id=999, type="Bot", login="Copilot")
            ownership = {
                "repository": {"nameWithOwner": FIXTURE},
                "search": {"pageInfo": {"hasNextPage": False},
                           "nodes": [{"number": 1, "repository": {"nameWithOwner": FIXTURE}}]},
            }
            read.graphql = Mock(return_value=ownership)
            with self.subTest(kind=kind):
                frozen = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind=kind)
                self.assertEqual(999, frozen["pr_author_id"])
                self.assertEqual(req["commit_author"], frozen["commit_author"])
                self.assertEqual(req["commit_author"]["id"], frozen["authorized_actor_id"])
                self.assertIn("author:launch-owner 1",
                              read.graphql.call_args.args[1]["searchQuery"])
                check_target(read, frozen)
                read.pr["user"]["id"] = 1000
                with self.assertRaisesRegex(Rejected, "Target changed after freeze"):
                    check_target(read, frozen)
                read.pr["user"]["id"] = 999
                ownership["search"]["nodes"] = []
                with self.assertRaisesRegex(Rejected, "ownership changed"):
                    check_target(read, frozen)
                with self.assertRaisesRegex(Rejected, "Wrong author"):
                    freeze(read, 1, REVISION, 100, FIXTURE, loop_kind=kind)
                ownership["search"]["pageInfo"]["hasNextPage"] = True
                with self.assertRaisesRegex(Rejected, "ownership search is incomplete"):
                    freeze(read, 1, REVISION, 100, FIXTURE, loop_kind=kind)

    def test_new_freezes_include_live_base_and_complete_actual_diff(self):
        for kind in ("pr_review", "pr_description", "pr_simplify", "pr_consistency", "pr_conflict_resolver"):
            req = task_request(kind)
            read = TaskRead(req)
            frozen = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind=kind)
            self.assertEqual(req["pr_diff"], frozen["pr_diff"])
            if kind == "pr_description":
                self.assertNotIn("base_sha", frozen)
                self.assertNotIn("merge_base_sha", frozen)
            else:
                self.assertEqual(BASE, frozen["base_sha"])
            self.assertEqual(AUTHOR_ID, frozen["commit_author"]["id"])
            self.assertEqual("ready" if kind == "pr_description" else "source_pending", start(
                MemoryState(), FakeAPI(), frozen, "", 0, True, "fine_grained_pat", [], True, 100)[1]["stage"])
        read.pr["changed_files"] = 2
        with self.assertRaisesRegex(Rejected, "Incomplete"):
            collect_diff(read, req)

    def test_target_checks_keep_frozen_scope_when_the_base_tip_advances(self):
        req = task_request("pr_conflict_resolver")
        frozen = copy.deepcopy(req)
        read = TaskRead(req)
        read.base_tip = "f" * 40
        read.pr["base"]["sha"] = read.base_tip
        self.assertEqual(read.pr, check_target(read, req))
        self.assertEqual(frozen, req)
        read.pr["head"]["sha"] = "e" * 40
        with self.assertRaisesRegex(Rejected, "Target changed after freeze"):
            check_target(read, req)
        read.pr["head"]["sha"] = req["frozen_sha"]
        read.pr["base"]["ref"] = "release"
        with self.assertRaisesRegex(Rejected, "PR base branch changed after freeze"):
            check_target(read, req)

    def test_description_accepts_binary_diff_without_file_inventory_or_source_access(self):
        req = task_request("pr_description")
        read = TaskRead(req)
        read.diff += ("diff --git a/gradle-wrapper.jar b/gradle-wrapper.jar\n"
                      "new file mode 100644\n"
                      "Binary files /dev/null and b/gradle-wrapper.jar differ\n")
        read.pr["head"]["repo"].update(full_name="public/fork", id=123, private=False)
        read.pr["changed_files"] = 1001
        read.pages = Mock(side_effect=AssertionError("Description must not request a file inventory"))
        frozen = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="pr_description")
        self.assertEqual(description_diff(read.diff), frozen["pr_diff"])
        self.assertFalse(staged_source(frozen))
        self.assertFalse(any("/git/" in path or "/compare/" in path for path in read.paths))
        self.assertEqual("ready", start(
            MemoryState(), FakeAPI(), frozen, "", 0, True, "fine_grained_pat", [], True, 100)[1]["stage"])

    def test_description_does_not_apply_code_diff_size_or_anchor_limits(self):
        req = task_request("pr_description")
        read = TaskRead(req)
        read.diff = ("diff --git a/Large.java b/Large.java\nnew file mode 100644\n"
                     "--- /dev/null\n+++ b/Large.java\n@@ -0,0 +1,10001 @@\n"
                     + "+a sufficiently long added line\n" * 10001)
        self.assertGreater(len(read.diff.encode()), 200000)
        frozen = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="pr_description")
        self.assertEqual(read.diff, frozen["pr_diff"]["text"])
        self.assertNotIn("anchors", frozen["pr_diff"])
        self.assertEqual(5, pipeline_limit(frozen))
        changed = copy.deepcopy(frozen)
        changed["pr_diff"]["text"] += "changed"
        with self.assertRaisesRegex(Rejected, "binding"):
            pipeline_limit(changed)

    def test_prior_description_request_binding_remains_readable(self):
        req = task_request("pr_description")
        req["pr_diff"] = {"text": GOOD_PATCH.decode(),
                          "sha256": hashlib.sha256(GOOD_PATCH).hexdigest(),
                          "anchors": {"Foo.java": [1]}}
        self.assertEqual(5, pipeline_limit(req))
        req["pr_diff"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(Rejected, "binding"):
            pipeline_limit(req)

    def test_description_requires_complete_proposal_even_for_no_change_or_blocked(self):
        req = task_request("pr_description")
        for outcome in ("no_change", "blocked", "proposal"):
            value = result(req, outcome)
            if outcome == "proposal":
                value["proposal"]["title"] = "Updated title"
            semantic(value, req)
            value["proposal"] = None
            with self.subTest(outcome=outcome), self.assertRaisesRegex(
                    Rejected, "Description proposal must contain title and body"):
                semantic(value, req)
        value = result(req)
        del value["proposal"]
        with self.assertRaises(Rejected):
            semantic(value, req)
        value = result(req)
        value["proposal"]["body"] = "Unexpected metadata change"
        with self.assertRaisesRegex(Rejected, "Metadata outcome contradicts"):
            semantic(value, req)

    def test_description_verification_never_fetches_or_reconstructs_source(self):
        for private in (False, True):
            req = task_request("pr_description")
            req.update(source_private=private, target_private=private)
            if private:
                with self.assertRaisesRegex(Rejected, "Only public"):
                    reconstruct({"result.json": canonical(result(req)), "candidate.patch": b""}, req)
                continue
            for key in ("base_ref", "base_sha", "merge_base_sha"):
                req.pop(key)
            value = result(req)
            files = {"result.json": canonical(value), "candidate.patch": b"",
                     "diagnostics.txt": b"Compared frozen title/body and GitHub diff"}
            fetch = Mock(side_effect=AssertionError("Description must not fetch source"))
            with self.subTest(private=private), tempfile.TemporaryDirectory() as package, \
                    patch("loop.verify.git", side_effect=AssertionError("Description must not invoke Git")):
                report = verify(zipped(files), req, run(), {"id": 33, "digest": "sha256:" + "0" * 64},
                                fetch, package)
                candidate = report["candidate"]
                candidate_outcome(value, req, candidate)
                self.assertIsNone(candidate["tree"])
                self.assertFalse(candidate["changed"])
                self.assertEqual([], candidate["commits"])
                self.assertEqual(b"", Path(package, "candidate.bundle").read_bytes())
                self.assertIn("metadata", report["scope"])
                fetch.assert_not_called()
                with self.assertRaisesRegex(Rejected, "source patch"):
                    reconstruct({**files, "candidate.patch": GOOD_PATCH}, req)
                with self.assertRaisesRegex(Rejected, "source candidate"):
                    candidate_outcome(value, req, {**candidate, "tree": SHA})

    def test_diff_anchors_reject_context_deleted_missing_or_truncated_lines(self):
        req = task_request("pr_review")
        comments([{"path": "Foo.java", "line": 1, "side": "RIGHT", "body": "Concrete bug"}], req)
        for anchor in ({"line": 2}, {"side": "LEFT"}, {"path": "other.java"}):
            item = {"path": "Foo.java", "line": 1, "side": "RIGHT", "body": "Concrete bug", **anchor}
            with self.subTest(anchor=anchor), self.assertRaises(Rejected):
                comments([item], req)
        with self.assertRaises(Rejected):
            diff_anchors(GOOD_PATCH.decode().replace("+new\n", ""))

    def test_no_change_simplify_and_consistency_finish_independently_of_failed_CI(self):
        for kind in ("pr_simplify", "pr_consistency"):
            req = task_request(kind)
            state = task_state(req, result(req))
            state.update(stage="published", expected_sha=SHA)
            state["report"]["candidate"] = {"changed": False}
            read = TaskRead(req)
            read.checks[0]["conclusion"] = "failure"
            store, name = stored(state)
            complete = watch_single(store, name, state, read, 100)
            self.assertEqual("complete", complete["stage"])
            self.assertEqual("failed", complete["ci"]["decision"])
            self.assertEqual("no_change", complete["task_completion"]["publication"])
            self.assertNotIn("fresh_review", complete)

    def test_incorporated_base_needs_no_source_or_model(self):
        req = task_request("pr_conflict_resolver")
        req["merge_base_sha"] = req["base_sha"]
        _, state = start(MemoryState(), FakeAPI(), req, "", 0, True,
                         "fine_grained_pat", [], False, 100)
        self.assertEqual("complete", state["stage"])
        self.assertEqual(0, state["iteration"])
        self.assertIsNone(state["intent"])

    def test_single_pass_checkpoints_cannot_admit_a_second_worker(self):
        from loop.coordinator import dispatch
        for kind in ("pr_simplify", "pr_consistency", "pr_description", "pr_review"):
            req = task_request(kind)
            state = checkpoint(req)
            state.update(stage="ready", iteration=1, source={"durably": "bound"}, publications=[])
            store, name = stored(state)
            api = FakeAPI()
            exhausted = dispatch(store, name, api, 100)
            self.assertEqual("exhausted", exhausted["stage"])
            self.assertEqual(1, exhausted["iteration"])
            self.assertFalse(api.calls)


class NonCodeEffectsTests(unittest.TestCase):
    def context(self, kind, author_id=999):
        req = task_request(kind)
        if kind == "pr_review":
            req["pr_author_id"] = author_id
        value = result(req, "proposal" if kind == "pr_description" else "comments")
        if kind == "pr_description":
            value["proposal"] = {"title": "New title", "body": "New body"}
        else:
            value["comments"] = [{"path": "Foo.java", "line": 1, "side": "RIGHT", "body": "Concrete correctness issue"}]
        state = task_state(req, value)
        read = TaskRead(req)
        store, name = stored(state)
        publisher = TaskPublisher(read)
        return state, read, store, name, publisher

    def publish(self, state, read, store, name, publisher):
        with patch("loop.task_effects.evidence", return_value=(
                {"verification_sha256": digest(state["report"])}, {})), \
                patch("loop.task_effects.time.time", return_value=100):
            return publish_task(store, name, state, Mock(), read, publisher, 100)

    def test_exact_title_body_PATCH_and_pending_review_without_submission(self):
        for kind, author_id in (("pr_description", AUTHOR_ID),
                                ("pr_review", 999), ("pr_review", AUTHOR_ID)):
            state, read, store, name, publisher = self.context(kind, author_id)
            complete = self.publish(state, read, store, name, publisher)
            self.assertEqual("complete", complete["stage"])
            self.assertEqual("confirmed", complete["task_intent"]["status"])
            self.assertEqual(1, len(publisher.posts))
            path, method, data = publisher.posts[0]
            if kind == "pr_description":
                self.assertEqual("PATCH", method)
                self.assertEqual({"title": "New title", "body": "New body"}, data)
            else:
                self.assertTrue(path.endswith("/reviews"))
                self.assertEqual({"commit_id", "comments"}, set(data))
                self.assertNotIn("event", data)
                self.assertEqual(42, complete["task_completion"]["review_id"])
            self.assertFalse(complete["publications"])

    def test_existing_own_pr_pending_review_is_preserved(self):
        state, read, store, name, publisher = self.context("pr_review", AUTHOR_ID)
        read.reviews = [{"id": 7, "state": "PENDING", "user": {"id": AUTHOR_ID}}]
        with self.assertRaisesRegex(Rejected, "Existing viewer-owned pending review is preserved"):
            self.publish(state, read, store, name, publisher)
        self.assertEqual([], publisher.posts)
        self.assertEqual([{"id": 7, "state": "PENDING", "user": {"id": AUTHOR_ID}}], read.reviews)

    def test_owner_attributed_copilot_pr_can_publish_metadata(self):
        state, read, _, _, publisher = self.context("pr_description")
        state["request"]["pr_author_id"] = 999
        read.pr["user"].update(id=999, type="Bot", login="Copilot")
        read.graphql = Mock(return_value={
            "repository": {"nameWithOwner": FIXTURE},
            "search": {"pageInfo": {"hasNextPage": False},
                       "nodes": [{"number": 1, "repository": {"nameWithOwner": FIXTURE}}]},
        })
        store, name = stored(state)
        complete = self.publish(state, read, store, name, publisher)
        self.assertEqual("complete", complete["stage"])
        self.assertEqual([(f"repos/{FIXTURE}/pulls/1", "PATCH",
                           {"title": "New title", "body": "New body"})], publisher.posts)
        self.assertEqual(AUTHOR_ID, complete["request"]["commit_author"]["id"])
        self.assertEqual(999, complete["request"]["pr_author_id"])

    def test_bot_authored_pr_gets_only_an_owner_owned_pending_review(self):
        state, read, store, name, publisher = self.context("pr_review")
        read.pr["user"].update(type="Bot", login="dependabot[bot]")
        complete = self.publish(state, read, store, name, publisher)
        self.assertEqual("complete", complete["stage"])
        self.assertEqual(1, len(publisher.posts))
        path, method, data = publisher.posts[0]
        self.assertEqual(f"repos/{FIXTURE}/pulls/1/reviews", path)
        self.assertEqual("POST", method)
        self.assertEqual({"commit_id", "comments"}, set(data))
        self.assertEqual(SHA, data["commit_id"])
        self.assertEqual("PENDING", read.reviews[0]["state"])
        self.assertEqual(AUTHOR_ID, read.reviews[0]["user"]["id"])
        self.assertEqual([], complete["publications"])

    def test_existing_pending_review_metadata_or_source_drift_prevents_mutation(self):
        for mutation in ("existing", "metadata", "head", "diff"):
            kind = "pr_review" if mutation == "existing" else "pr_description"
            state, read, store, name, publisher = self.context(kind)
            if mutation == "existing":
                read.reviews = [{"id": 7, "state": "PENDING", "user": {"id": AUTHOR_ID}}]
            elif mutation == "metadata":
                read.pr["title"] = "Human edit"
            elif mutation == "head":
                read.pr["head"]["sha"] = "0" * 40
            else:
                read.diff += "\nChanged GitHub diff"
            with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                self.publish(state, read, store, name, publisher)
            self.assertEqual([], publisher.posts)

    def test_description_ignores_base_tip_drift_when_actual_diff_is_unchanged(self):
        state, read, store, name, publisher = self.context("pr_description")
        read.base_tip = "0" * 40
        complete = self.publish(state, read, store, name, publisher)
        self.assertEqual("complete", complete["stage"])
        self.assertEqual(1, len(publisher.posts))

    def test_real_description_artifacts_are_reverified_without_source_reconstruction(self):
        for private in (False, True):
            req = task_request("pr_description")
            req.update(source_private=private, target_private=private)
            if private:
                with self.assertRaisesRegex(Rejected, "Only public"):
                    PublisherAPI(TEST_TOKEN, req, "fine_grained_pat")
                continue
            value = result(req, "proposal")
            value["proposal"] = {"title": "New title", "body": "New body"}
            payload = zipped({"result.json": canonical(value), "candidate.patch": b"",
                              "diagnostics.txt": b"Compared metadata and diff"})
            worker = run()
            worker_artifact = {"id": 33, "name": "candidate-24-1", "size_in_bytes": len(payload),
                               "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                               "expired": False, "workflow_run": {"id": 24, "head_sha": REVISION}}
            with self.subTest(private=private), tempfile.TemporaryDirectory() as package, \
                    tempfile.TemporaryDirectory() as restored, \
                    patch("loop.verify.git", side_effect=AssertionError("No description Git verification")), \
                    patch("loop.publication.git", side_effect=AssertionError("No description source import")):
                report = verify(payload, req, worker, worker_artifact, package_dir=package)
                state = task_state(req, value)
                state["run"] = {"id": 24, "attempt": 1}
                state["verification_run"] = {"id": 99, "attempt": 1}
                state["report"] = copy.deepcopy(report)
                state["report"]["verification_run"] = state["verification_run"]
                verification = zipped({
                    "verification-report.json": canonical({
                        "schema": 2, "request_id": req["request_id"], "request_digest": digest(req),
                        "generation": state["generation"], "verification": "verified",
                        "run_id": 24, "run_attempt": 1, "result": report}),
                    "candidate-package/manifest.json": Path(package, "manifest.json").read_bytes(),
                    "candidate-package/candidate.bundle": Path(package, "candidate.bundle").read_bytes(),
                })
                artifact = {"id": 34, "name": "verification-99-1", "size_in_bytes": len(verification),
                            "digest": "sha256:" + hashlib.sha256(verification).hexdigest(), "expired": False,
                            "workflow_run": {"id": 99, "head_sha": REVISION},
                            "created_at": iso(100), "expires_at": iso(100 + 14 * 86400)}
                state["artifacts"] = [artifact]
                pipeline = dict(worker, id=99, path=".github/workflows/coordinator.yml", created_at=iso(90))
                api = Mock()
                api.call.side_effect = lambda path: pipeline if path.endswith("/99") else worker
                api.pages.side_effect = lambda path, key: (
                    [{"name": name, "conclusion": "success"} for name in
                     (("verify", "finalize") if "/99/" in path else ("agent",))]
                    if key == "jobs" else [artifact] if "/99/" in path else [worker_artifact])
                api.artifact_zip.side_effect = lambda identity, limit: {33: payload, 34: verification}[identity]
                with patch("loop.publication.time.time", return_value=100):
                    accepted, candidate = evidence(api, state, restored)
                self.assertEqual(report["candidate"], candidate)
                self.assertEqual(digest(state["report"]), accepted["verification_sha256"])
                pipeline["head_sha"] = SHA
                with self.assertRaises(Rejected):
                    evidence(api, state, restored)

    def test_lost_responses_reconcile_once_and_never_repeat_POST(self):
        for kind in ("pr_description", "pr_review"):
            state, read, store, name, publisher = self.context(kind)
            publisher.uncertain = True
            with self.assertRaises(APIError):
                self.publish(state, read, store, name, publisher)
            pending = store.entries[name]
            complete = confirm_task(store, name, pending, read, 101)
            self.assertEqual("complete", complete["stage"])
            self.assertEqual(1, len(publisher.posts))

    def test_cancellation_and_permission_denial_never_create_another_effect(self):
        state, read, store, name, publisher = self.context("pr_review")
        publisher.denied = True
        with self.assertRaises(APIError):
            self.publish(state, read, store, name, publisher)
        pending = copy.deepcopy(store.entries[name])
        cancel(store, name, pending["request"]["request_id"], pending["generation"], 101)
        with self.assertRaises(Rejected):
            confirm_task(store, name, pending, read, 102)
        with self.assertRaises(Rejected):
            quiescent(None, store.entries[name])
        self.assertEqual(1, len(publisher.posts))

    def test_report_publisher_cannot_push_submit_approve_or_mutate_unbound_fields(self):
        for kind in ("pr_description", "pr_review"):
            req = task_request(kind)
            publisher = PublisherAPI(TEST_TOKEN, req, "fine_grained_pat")
            with self.assertRaises(ValueError):
                publisher.authorize(f"repos/{FIXTURE}/pulls/1", "PATCH", {"draft": False})
            with self.assertRaises(ValueError):
                publisher.authorize(f"repos/{FIXTURE}/pulls/1/reviews", "POST", {"event": "APPROVE"})
            with self.assertRaises(ValueError):
                publisher.authorize(f"repos/{FIXTURE}/issues/1/comments", "POST", {"body": "bad"})

    def test_report_identity_does_not_require_head_push_permission(self):
        for kind in ("pr_description", "pr_review"):
            req = task_request(kind)
            req.update(head_repo="fork/head", head_repo_id=123)
            read = TaskRead(req)
            publisher = PublisherAPI(TEST_TOKEN, req, "fine_grained_pat")
            calls = []
            def response(path, *_args, **_kwargs):
                calls.append(path)
                if path == "user":
                    return {"id": AUTHOR_ID, "type": "User", "login": req["commit_author"]["login"]}
                if path == f"repos/{CENTRAL}":
                    raise AssertionError("Report-only identity must not probe the central repository")
                if path == f"repos/{FIXTURE}":
                    return {"id": req["repo_id"], "full_name": FIXTURE, "private": False,
                            "node_id": "base-node", "permissions": {"pull": True, "push": False}}
                if path == "repos/fork/head":
                    raise AssertionError("Report-only identity must not require fork push access")
                return read.call(path)
            publisher.call = response
            publisher.identity(req)
            self.assertNotIn("repos/fork/head", calls)
            self.assertNotIn(f"repos/{CENTRAL}", calls)

    def test_rerun_credentials_route_to_base_without_head_push_permission(self):
        req = task_request("ci_fix")
        req.update(head_repo="fork/head", head_repo_id=123)
        self.assertEqual(FIXTURE, effect_repository(req, "rerun"))
        self.assertEqual("fork/head", effect_repository(req, "fixes"))
        publisher = PublisherAPI(TEST_TOKEN, req, "fine_grained_pat", source_write=False)
        self.assertFalse(publisher.source_write)
        with self.assertRaises(Rejected):
            PublisherAPI(TEST_TOKEN, task_request("pr_simplify"), "fine_grained_pat", source_write=False)
        from loop.cli import choose_live
        for outcome, secret in (("rerun", "BASE_PUBLISH_TOKEN"), ("fixes", "HEAD_PUBLISH_TOKEN")):
            state = task_state(req, {"outcome": outcome})
            store, name = stored(state)
            with patch.dict(os.environ, {"PUBLISHER_SECRET_MAP": json.dumps({
                    FIXTURE.split("/")[0]: "BASE_PUBLISH_TOKEN", "fork": "HEAD_PUBLISH_TOKEN"})}), \
                    patch("loop.cli.output") as output:
                self.assertEqual(name, choose_live(store, 100))
            self.assertEqual(secret, dict(call.args for call in output.call_args_list)["live_publisher_secret"])

    def test_unchanged_description_still_rechecks_original_metadata(self):
        state, read, store, name, publisher = self.context("pr_description")
        state["report"]["dispositions"] = result(state["request"])
        store.entries[name] = copy.deepcopy(state)
        read.pr["body"] = "Concurrent human edit"
        with self.assertRaises(Rejected):
            self.publish(state, read, store, name, publisher)
        self.assertEqual([], publisher.posts)

    def test_no_findings_or_matching_description_makes_no_mutation(self):
        for kind in ("pr_description", "pr_review"):
            state, read, store, name, publisher = self.context(kind)
            state["report"]["dispositions"] = result(state["request"])
            store.entries[name] = copy.deepcopy(state)
            complete = self.publish(state, read, store, name, publisher)
            self.assertEqual("complete", complete["stage"])
            self.assertEqual([], publisher.posts)
            self.assertNotIn("task_intent", complete)


def objects(directory, texts, parents=(), subject="Commit"):
    records = []
    for path, text in sorted(texts.items()):
        blob = git(["hash-object", "-w", "--stdin"], directory, text.encode()).decode().strip()
        records.append(f"100644 blob {blob}\t{path}\n")
    tree = git(["mktree"], directory, "".join(records).encode()).decode().strip()
    obj = (f"tree {tree}\n" + "".join(f"parent {p}\n" for p in parents)
           + "author T <t@invalid> 0 +0000\ncommitter T <t@invalid> 0 +0000\n\n" + subject + "\n").encode()
    return git(["hash-object", "-t", "commit", "-w", "--stdin"], directory, obj).decode().strip()


class MergeGitTests(unittest.TestCase):
    def context(self, directory, equal=False, conflicting=False):
        git(["init", "--bare", "--quiet"], directory)
        ancestor = objects(directory, {"Foo.java": "old\n"})
        head = objects(directory, {"Foo.java": "head\n"}, [ancestor])
        base_text = "head\n" if equal else "base\n" if conflicting else "old\n"
        texts = {"Foo.java": base_text}
        if not equal and not conflicting:
            texts["Incoming.txt"] = "incoming\n"
        base = objects(directory, texts, [ancestor], subject="Base commit")
        req = task_request("pr_conflict_resolver")
        req.update(frozen_sha=head, base_sha=base, merge_base_sha=ancestor)
        pr_diff = git(["diff", ancestor, head], directory).decode()
        req["pr_diff"] = {"text": pr_diff, "sha256": hashlib.sha256(pr_diff.encode()).hexdigest(),
                          "anchors": diff_anchors(pr_diff)[0]}
        for name, sha in (("snapshot", head), ("incoming", base), ("review-base", ancestor)):
            git(["update-ref", "refs/heads/" + name, sha], directory)
        Path(directory, "FETCH_HEAD").write_text(head + "\n", encoding="ascii")
        return req

    def copy_source(self, source):
        def fetch(destination):
            for path in Path(source, "objects").rglob("*"):
                if path.is_file():
                    target = Path(destination, path.relative_to(source))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
            for name in ("snapshot", "incoming", "review-base"):
                sha = git(["rev-parse", name], source).decode().strip()
                git(["update-ref", "refs/heads/" + name, sha], destination)
            Path(destination, "FETCH_HEAD").write_text(
                git(["rev-parse", "snapshot"], source).decode(), encoding="ascii")
        return fetch

    def test_real_merges_include_exact_parents_even_with_equal_trees(self):
        for equal in (False, True):
            with self.subTest(equal=equal), tempfile.TemporaryDirectory() as directory, \
                    tempfile.TemporaryDirectory() as package, tempfile.TemporaryDirectory() as imported:
                req = self.context(directory, equal=equal)
                patch_bytes = b""
                if not equal:
                    git(["read-tree", req["frozen_sha"]], directory)
                    blob = git(["hash-object", "-w", "--stdin"], directory, b"incoming\n").decode().strip()
                    git(["update-index", "--add", "--cacheinfo", "100644", blob, "Incoming.txt"], directory)
                    patch_bytes = git(["diff", "--cached", req["frozen_sha"]], directory)
                value = result(req, "merge")
                candidate = reconstruct({"result.json": canonical(value), "candidate.patch": patch_bytes},
                                        req, self.copy_source(directory), package)
                candidate_outcome(value, req, candidate)
                self.assertTrue(candidate["changed"])
                self.assertEqual([req["frozen_sha"], req["base_sha"]], candidate["commits"][0]["parents"])
                self.assertEqual([] if equal else ["Incoming.txt"], candidate["changed_paths"])
                import_candidate(imported, Path(package, "candidate.bundle"), req, candidate,
                                 self.copy_source(directory))
                self.assertEqual(candidate["commit"] + " " + req["frozen_sha"] + " " + req["base_sha"],
                                 git(["rev-list", "--parents", "-1", candidate["commit"]], imported).decode().strip())

    def test_missing_clean_incoming_change_and_unresolved_conflicts_reject(self):
        for conflicting in (False, True):
            with self.subTest(conflicting=conflicting), tempfile.TemporaryDirectory() as directory:
                req = self.context(directory, conflicting=conflicting)
                patch_bytes = b"" if not conflicting else b"""diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -1 +1,5 @@
+<<<<<<< HEAD
 head
+=======
+base
+>>>>>>> base
"""
                with self.assertRaises(Rejected):
                    reconstruct({"result.json": canonical(result(req, "merge")),
                                 "candidate.patch": patch_bytes}, req, self.copy_source(directory))

    def test_conflict_resolution_keeps_both_intents_and_is_not_single_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            req = self.context(directory, conflicting=True)
            patch_bytes = b"""diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -1 +1,2 @@
 head
+base
"""
            candidate = reconstruct({"result.json": canonical(result(req, "merge")),
                                     "candidate.patch": patch_bytes}, req, self.copy_source(directory))
            self.assertEqual(2, len(candidate["commits"][0]["parents"]))

    def test_large_incoming_merge_survives_worker_staging_verification_and_import(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as package, tempfile.TemporaryDirectory() as imported:
            req = self.context(directory, conflicting=True)
            incoming = {
                f"Incoming-{file:03}.txt": "".join(
                    f"{file:03}:{line:04}:{hashlib.sha256(f'{file}:{line}'.encode()).hexdigest()}\n"
                    for line in range(1300))
                for file in range(101)}
            incoming["Foo.java"] = "base\n"
            incoming["Inherited.txt"] = "Incoming content \t\n\n"
            req["base_sha"] = objects(directory, incoming, [req["merge_base_sha"]])
            git(["update-ref", "refs/heads/incoming", req["base_sha"]], directory)
            resolved = dict(incoming, **{"Foo.java": "head\nbase\n"})
            proposed = objects(directory, resolved)
            git(["read-tree", proposed], directory)
            patch_bytes = git(["diff", "--cached", req["frozen_sha"]], directory)
            self.assertGreater(len(patch_bytes), 8 * 1024 * 1024)

            workspace = Path(root, "worker")
            output = workspace / "loop-output"
            output.mkdir(parents=True)
            (output / "result.json").write_bytes(canonical(result(req, "merge")))
            (output / "candidate.patch").write_bytes(patch_bytes)
            (output / "diagnostics.txt").write_bytes(b"Preserved incoming files and both conflict intents.")
            check_output(req, output)
            prompt = (Path(__file__).resolve().parents[1] / ".github" /
                      "workflows" / "copilot-worker.md").read_text(encoding="utf-8")
            staging = prompt.split("/usr/bin/python3 -I - <<'PY'\n", 1)[1].split("\n      PY", 1)[0]
            with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(workspace),
                                        "RUNNER_TEMP": root, "GITHUB_RUN_ID": "24"}):
                exec(textwrap.dedent(staging), {})
            files = {path.name: path.read_bytes() for path in Path(root, "candidate-staged-24").iterdir()}
            self.assertEqual(patch_bytes, files["candidate.patch"])
            payload = zipped(files)
            report = verify(payload, req, run(),
                            {"id": 33, "digest": "sha256:" + hashlib.sha256(payload).hexdigest()},
                            self.copy_source(directory), package)
            candidate = report["candidate"]
            self.assertEqual(sorted(resolved), candidate["changed_paths"])
            self.assertEqual([req["frozen_sha"], req["base_sha"]], candidate["commits"][0]["parents"])
            self.assertEqual(git(["rev-parse", proposed + "^{tree}"], directory).decode().strip(),
                             candidate["tree"])
            import_candidate(imported, Path(package, "candidate.bundle"), req, candidate,
                             self.copy_source(directory))
            self.assertEqual(candidate["commit"] + " " + req["frozen_sha"] + " " + req["base_sha"],
                             git(["rev-list", "--parents", "-1", candidate["commit"]], imported).decode().strip())
            self.assertEqual(b"head\nbase\n", git(["show", candidate["commit"] + ":Foo.java"], imported))
            self.assertEqual(b"Incoming content \t\n\n",
                             git(["show", candidate["commit"] + ":Inherited.txt"], imported))

    def test_bound_history_source_package_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as imported:
            req = self.context(directory)
            read = TaskRead(req)
            read.pr["head"]["sha"] = req["frozen_sha"]
            read.base_tip = req["base_sha"]
            destination = Path(root, "package")
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1"}), \
                    patch("loop.source.time.time", return_value=100):
                manifest = package_source(req, 6, read, destination, self.copy_source(directory))
            git(["init", "--bare", "--quiet"], imported)
            import_source(imported, destination / "source.bundle", manifest, req)
            self.assertEqual(3, manifest["history_count"])
            self.assertEqual(req["base_sha"], git(["rev-parse", "incoming"], imported).decode().strip())

    def test_conflict_source_deepens_through_older_merge_base(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as imported:
            req = self.context(directory)
            base = req["base_sha"]
            git(["config", "pack.threads", "1"], directory)
            history = bytearray()
            for index in range(1100):
                history.extend(b"commit refs/heads/incoming\n"
                               b"committer T <t@invalid> 0 +0000\ndata 5\nbase\n")
                if index == 0:
                    history.extend(f"from {base}\n".encode())
                history.extend(b"\n")
            git(["fast-import", "--quiet"], directory, bytes(history))
            base = git(["rev-parse", "incoming"], directory).decode().strip()
            req["base_sha"] = base
            read = TaskRead(req)
            read.pr["head"]["sha"] = req["frozen_sha"]
            read.base_tip = base
            destination = Path(root, "package")

            def fetch(target, request, repo=None, sha=None, depth=1):
                git(["-c", "protocol.file.allow=always", "fetch", "--quiet",
                     "--depth=" + str(depth), "--no-auto-maintenance", directory,
                     sha or request["frozen_sha"]], target)

            with patch.dict(os.environ, {"GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1"}), \
                    patch("loop.source.time.time", return_value=100), \
                    patch("loop.source.public_fetch", side_effect=fetch):
                manifest = package_source(req, 6, read, destination)
            self.assertEqual(1103, manifest["history_count"])
            git(["init", "--bare", "--quiet"], imported)
            import_source(imported, destination / "source.bundle", manifest, req)
            self.assertEqual(req["merge_base_sha"], git(
                ["merge-base", req["frozen_sha"], base], imported).decode().strip())

    def test_shallow_merge_history_preserves_side_branch_boundaries_on_import(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as imported:
            git(["init", "--bare", "--quiet"], directory)
            earlier = objects(directory, {"Foo.java": "earlier\n"})
            oldest = objects(directory, {"Foo.java": "oldest\n", "History.txt": "x" * 16384}, [earlier])
            older = objects(directory, {"Foo.java": "older\n"}, [oldest])
            ancestor = objects(directory, {"Foo.java": "old\n"}, [older])
            side = objects(directory, {"Foo.java": "side\n"}, [older])
            head = objects(directory, {"Foo.java": "head\n"}, [ancestor, side])
            base = objects(directory, {"Foo.java": "old\n", "Incoming.txt": "incoming\n"}, [ancestor])
            for name, sha in (("head", head), ("base", base)):
                git(["update-ref", "refs/heads/" + name, sha], directory)
            req = task_request("pr_conflict_resolver")
            req.update(frozen_sha=head, base_sha=base, merge_base_sha=ancestor)
            read = TaskRead(req)
            read.pr["head"]["sha"] = head
            read.base_tip = base
            destination = Path(root, "package")

            def fetch(target, request, repo=None, sha=None, depth=1):
                git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--depth=4",
                     "--no-auto-maintenance", directory, sha or request["frozen_sha"]], target)

            with patch.dict(os.environ, {"GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1"}), \
                    patch("loop.source.time.time", return_value=100), \
                    patch("loop.source.public_fetch", side_effect=fetch):
                manifest = package_source(req, 6, read, destination)
            self.assertEqual(5, manifest["history_count"])
            self.assertEqual(sorted([ancestor, older]), manifest["merge_history"]["shallow_commits"])
            git(["init", "--bare", "--quiet"], imported)
            import_source(imported, destination / "source.bundle", manifest, req)
            self.assertEqual(ancestor, git(["merge-base", head, base], imported).decode().strip())
            self.assertEqual(b"head\n", git(["show", "snapshot:Foo.java"], imported))
            self.assertEqual(b"incoming\n", git(["show", "incoming:Incoming.txt"], imported))

    def test_ordinary_fixes_and_report_results_bind_full_source_scope(self):
        for kind in ("pr_simplify", "pr_consistency", "pr_review"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                merge_req = self.context(directory)
                req = task_request(kind)
                req.update({key: merge_req[key] for key in
                            ("frozen_sha", "base_sha", "merge_base_sha", "pr_diff")})
                value = result(req)
                candidate = reconstruct({"result.json": canonical(value), "candidate.patch": b""},
                                        req, self.copy_source(directory))
                candidate_outcome(value, req, candidate)
                self.assertFalse(candidate["changed"])
                self.assertEqual([], candidate["commits"])
                req["pr_diff"]["text"] = ""
                req["pr_diff"]["sha256"] = hashlib.sha256(b"").hexdigest()
                req["pr_diff"]["anchors"] = {}
                value["request_digest"] = digest(req)
                with self.assertRaisesRegex(Rejected, "complete frozen head"):
                    reconstruct({"result.json": canonical(value), "candidate.patch": b""},
                                req, self.copy_source(directory))

    def test_simplify_and_consistency_publish_ordinary_root_cause_chains(self):
        for kind in ("pr_simplify", "pr_consistency"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                merge_req = self.context(directory)
                req = task_request(kind)
                req.update({key: merge_req[key] for key in
                            ("frozen_sha", "base_sha", "merge_base_sha", "pr_diff")})
                patch_bytes = GOOD_PATCH.replace(b"-old", b"-head")
                value = result(req, "fixes", patch_bytes)
                candidate = reconstruct({"result.json": canonical(value), "candidate.patch": patch_bytes},
                                        req, self.copy_source(directory))
                candidate_outcome(value, req, candidate)
                self.assertEqual(1, len(candidate["commits"]))
                self.assertEqual(req["frozen_sha"], candidate["commits"][0]["parent"])
                self.assertNotIn("parents", candidate["commits"][0])
                if kind == "pr_consistency":
                    value["consistency"][0]["classification"] = "needed"
                    with self.assertRaises(Rejected):
                        candidate_outcome(value, req, candidate)

class CIRepairTests(unittest.TestCase):
    def test_read_credential_cannot_download_logs_from_another_repository(self):
        source = SourceAPI("read-token", FIXTURE)
        with self.assertRaises(Rejected):
            source.signed_download("repos/foreign/repo/actions/jobs/333/logs", 60000)

    def context(self, attempt=1):
        req = task_request("ci_fix")
        read = TaskRead(req)
        read.checks[0]["conclusion"] = "failure"
        read.runs[0].update(run_attempt=attempt, conclusion="failure")
        read.jobs[0]["run_attempt"] = attempt
        req["ci_evidence"] = collect(read, req, [CI_CHECK])
        return req, read

    def test_attempt_bound_logs_and_manual_retries_consume_allowance(self):
        for attempt in (1, 2, 3):
            req, read = self.context(attempt)
            value = result(req, "rerun")
            value["rerun_run"] = 200
            value["diagnoses"][0]["decision"] = "rerun"
            if attempt == 1:
                diagnoses(value, req)
            else:
                with self.subTest(attempt=attempt), self.assertRaises(Rejected):
                    diagnoses(value, req)
            self.assertEqual(attempt, req["ci_evidence"]["failures"][0]["actions"]["attempt"])
            self.assertEqual("temporary runner network outage", req["ci_evidence"]["failures"][0]["evidence"])

    def test_large_authoritative_ci_diff_freezes_and_keeps_its_binding(self):
        req = task_request("ci_fix")
        read = TaskRead(req)
        read.diff = ("diff --git a/Foo.java b/Foo.java\n"
                     "--- a/Foo.java\n+++ b/Foo.java\n@@ -1 +1,11001 @@\n-old\n"
                     + ("+" + "x" * 90 + "\n") * 11001)
        read.diff += "".join(
            f"diff --git a/New-{index}.txt b/New-{index}.txt\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/New-{index}.txt\n@@ -0,0 +1 @@\n+new\n"
            for index in range(1000))
        read.pr["changed_files"] = 1001
        read.pages = Mock(side_effect=lambda path, key=None: (
            [{"filename": "Foo.java", "additions": 11001, "deletions": 1},
             *[{"filename": f"New-{index}.txt", "additions": 1, "deletions": 0}
               for index in range(1000)]]
            if path.endswith("/files") else TaskRead.pages(read, path, key)))
        frozen = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="ci_fix")
        self.assertEqual(read.diff, frozen["pr_diff"]["text"])
        self.assertEqual(list(range(1, 11002)), frozen["pr_diff"]["anchors"]["Foo.java"])
        self.assertEqual({f"New-{index}.txt": [1] for index in range(1000)},
                         {path: lines for path, lines in frozen["pr_diff"]["anchors"].items()
                          if path != "Foo.java"})
        self.assertEqual(5, pipeline_limit(frozen))

    def test_many_large_logs_preserve_complete_attempt_bound_evidence(self):
        req, read = self.context()
        read.log = (b"verbose output\n" * 10000
                    + ("unicode \u00e9 and escaped \x1b output\n" * 3000).encode()
                    + b"FAILURE: exact root cause\n")
        for offset in range(1, 60):
            read.checks.append(dict(
                read.checks[0], id=333 + offset, name=f"check-{offset}-" + "x" * 250,
                details_url=f"https://github.com/{FIXTURE}/actions/runs/200/job/{333 + offset}"))
            read.jobs.append(dict(
                read.jobs[0], id=333 + offset, name=read.checks[-1]["name"],
                check_run_url=f"https://api.github.com/repos/{FIXTURE}/check-runs/{333 + offset}"))
        read.signed_download = Mock(wraps=read.signed_download)
        evidence = collect(read, req, [c["name"] for c in read.checks])
        self.assertEqual("failed", evidence["decision"])
        self.assertEqual(60, len(evidence["failures"]))
        self.assertTrue(all(f["availability"] == "job_log"
                            and f["evidence"] == read.log.decode()
                            for f in evidence["failures"]))
        self.assertTrue(all(c.args == (f"repos/{FIXTURE}/actions/jobs/{333 + index}/logs",)
                            and not c.kwargs
                            for index, c in enumerate(read.signed_download.call_args_list)))
        req["ci_evidence"] = evidence
        self.assertEqual(evidence, same_attempts(read, req))

    def test_unavailable_log_permissions_keep_the_check_output(self):
        req, read = self.context()
        read.checks[0]["output"] = {"summary": "runner failure"}
        read.signed_download = Mock(side_effect=APIError(403, "Logs forbidden"))
        evidence = collect(read, req, [CI_CHECK])
        self.assertEqual("logs_unavailable_403", evidence["failures"][0]["availability"])
        self.assertEqual("runner failure", evidence["failures"][0]["evidence"].strip())

    def test_missing_signed_log_blob_keeps_the_check_output(self):
        req, read = self.context()
        read.checks[0]["output"] = {"summary": "runner failure"}
        read.signed_download = API("read-token").signed_download
        redirect = urllib.error.HTTPError("https://api.github.com/logs", 302, "redirect",
                                          {"Location": "https://example.com/signed-log"}, io.BytesIO())
        missing = urllib.error.HTTPError("https://example.com/signed-log", 404,
                                         "The specified blob does not exist.", {}, io.BytesIO())
        with patch("loop.api.urllib.request.build_opener") as opener, \
                patch("loop.api.urllib.request.urlopen", side_effect=missing):
            opener.return_value.open.side_effect = redirect
            evidence = collect(read, req, [CI_CHECK])
        self.assertEqual("failed", evidence["decision"])
        self.assertEqual("logs_unavailable_404", evidence["failures"][0]["availability"])
        self.assertEqual("runner failure", evidence["failures"][0]["evidence"].strip())

    def test_failed_step_evidence_keeps_the_failure_instead_of_post_job_cleanup(self):
        req, read = self.context()
        read.jobs[0]["steps"] = [{
            "name": "Test", "conclusion": "failure",
            "started_at": "2026-10-07T01:00:00Z", "completed_at": "2026-10-07T01:01:00Z"}]
        read.signed_download = API("read-token").signed_download
        payload = (b"2026-10-07T01:00:59.123Z ##[error]FAILURE: expected remote parent\n"
                   + b"2026-10-07T01:01:01.456Z Post job cleanup: cache saved\n" * 2000)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.readline.side_effect = io.BytesIO(payload).readline
        redirect = urllib.error.HTTPError("https://api.github.com/logs", 302, "redirect",
                                          {"Location": "https://example.com/signed-log"}, io.BytesIO())
        with patch("loop.api.urllib.request.build_opener") as opener, \
                patch("loop.api.urllib.request.urlopen", return_value=response):
            opener.return_value.open.side_effect = redirect
            evidence = collect(read, req, [CI_CHECK])
        self.assertEqual("failed_step_log_excerpt", evidence["failures"][0]["availability"])
        self.assertIn("FAILURE: expected remote parent", evidence["failures"][0]["evidence"])
        self.assertNotIn("Post job cleanup", evidence["failures"][0]["evidence"])

    def test_newer_same_head_runs_supersede_failures_but_independent_workflows_do_not(self):
        req, read = self.context()
        newer = dict(read.runs[0], id=201, run_number=2, check_suite_id=501, conclusion="success")
        read.runs.append(newer)
        read.checks.append(dict(
            read.checks[0], id=334, conclusion="success", check_suite={"id": 501},
            details_url=f"https://github.com/{FIXTURE}/actions/runs/201/job/334"))
        read.jobs.append(dict(
            read.jobs[0], id=334, run_id=201,
            check_run_url=f"https://api.github.com/repos/{FIXTURE}/check-runs/334"))
        original = read.call
        read.call = lambda path, *args, **kwargs: (
            copy.deepcopy(newer) if path == f"repos/{FIXTURE}/actions/runs/201"
            else original(path, *args, **kwargs))
        read.signed_download = Mock(wraps=read.signed_download)
        state = checkpoint(req)
        state.update(stage="waiting_ci", publications=[], effects=[])
        store, name = stored(state)
        complete = watch_ci_fix(store, name, state, read, 100)
        self.assertEqual("complete", complete["stage"])
        self.assertEqual("CI_passed", complete["task_completion"]["outcome"])
        self.assertEqual([334], [c["id"] for c in complete["ci"]["checks"]])
        read.signed_download.assert_not_called()
        read.checks.pop()
        newer.update(status="queued", conclusion=None)
        self.assertEqual("pending", collect(read, req, [CI_CHECK])["decision"])
        read.signed_download.assert_not_called()
        newer.update(status="completed", conclusion="success", workflow_id=999)
        evidence = collect(read, req, [CI_CHECK])
        self.assertEqual("failed", evidence["decision"])
        self.assertEqual([333], [f["id"] for f in evidence["failures"]])

    def test_next_ci_pass_freezes_the_new_base_without_resetting_its_budget(self):
        req, read = self.context()
        state = checkpoint(req)
        state.update(stage="waiting_ci", iteration=1, publications=[], effects=[])
        store, name = stored(state)
        read.base_tip = "f" * 40
        ready = watch_ci_fix(store, name, state, read, 100)
        self.assertEqual("source_pending", ready["stage"])
        self.assertEqual(read.base_tip, ready["request"]["base_sha"])
        self.assertEqual(req["frozen_sha"], ready["request"]["frozen_sha"])
        self.assertEqual(req["deadline"], ready["request"]["deadline"])
        self.assertEqual(req["budgets"], ready["request"]["budgets"])
        self.assertEqual(1, ready["iteration"])
        self.assertEqual(state["phase"], ready["phase"])

    def test_failed_checks_allow_diagnosis_and_nonblocking_checks_allow_clearance(self):
        req, read = self.context()
        read.checks.append(dict(
            read.checks[0], id=334, name="optional job", conclusion="skipped",
            details_url=f"https://github.com/{FIXTURE}/actions/runs/200/job/334"))
        read.jobs.append(dict(
            read.jobs[0], id=334, name="optional job",
            check_run_url=f"https://api.github.com/repos/{FIXTURE}/check-runs/334"))
        required = [CI_CHECK, "optional job"]
        req["publication"]["required_checks"] = required
        req["ci_evidence"] = collect(read, req, required)
        self.assertEqual("failed", req["ci_evidence"]["decision"])
        diagnoses(result(req), req)
        state = checkpoint(req)
        state.update(stage="waiting_ci", publications=[], effects=[])
        store, name = stored(state)
        ready = watch_ci_fix(store, name, state, read, 100)
        self.assertEqual("source_pending", ready["stage"])
        self.assertEqual(0, ready["iteration"])
        read.checks[0]["conclusion"] = "success"
        for conclusion in ("skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                read.checks[1]["conclusion"] = conclusion
                store, name = stored(ready)
                complete = watch_ci_fix(store, name, ready, read, 101)
                self.assertEqual("complete", complete["stage"])
                self.assertEqual("passed", complete["ci"]["decision"])
                self.assertEqual("CI_passed", complete["task_completion"]["outcome"])

    def test_repeated_status_contexts_use_github_combined_status(self):
        req, read = self.context()
        read.checks = []
        latest = {"id": 334, "context": CI_CHECK, "state": "success"}
        read.statuses = [latest, dict(latest, id=333, state="failure")]
        read.pages = Mock(wraps=read.pages)
        read.signed_download = Mock(wraps=read.signed_download)
        evidence = collect(read, req, [CI_CHECK])
        self.assertEqual("passed", evidence["decision"])
        self.assertEqual([334], [c["id"] for c in evidence["checks"]])
        read.signed_download.assert_not_called()
        read.pages.assert_any_call(f"repos/{FIXTURE}/commits/{req['frozen_sha']}/status", "statuses")
        latest["state"] = "pending"
        self.assertEqual("pending", collect(read, req, [CI_CHECK])["decision"])
        latest["state"] = "failure"
        evidence = collect(read, req, [CI_CHECK])
        self.assertEqual("failed", evidence["decision"])
        self.assertEqual([334], [f["id"] for f in evidence["failures"]])

    def test_evidence_attribution_unknown_and_run_drift_fail_closed(self):
        req, read = self.context()
        value = result(req, "no_change")
        value["diagnoses"][0]["evidence"] = ["made up"]
        with self.assertRaises(Rejected):
            diagnoses(value, req)
        value["diagnoses"][0].update(decision="unknown", evidence=[])
        with self.assertRaises(Rejected):
            diagnoses(value, req)
        value["outcome"] = "blocked"
        diagnoses(value, req)
        read.runs[0]["run_attempt"] = read.jobs[0]["run_attempt"] = 2
        with self.assertRaises(Rejected):
            same_attempts(read, req)

    def test_unrelated_failures_remain_explicit_warnings_not_green(self):
        req, read = self.context()
        state = task_state(req, result(req))
        state.update(stage="published")
        store, name = stored(state)
        complete = watch_ci_fix(store, name, state, read, 100)
        self.assertEqual("complete", complete["stage"])
        self.assertEqual("failed", complete["ci"]["decision"])
        self.assertEqual(1, len(complete["ci_warnings"]))
        self.assertEqual("warnings_not_CI_clearance", complete["task_completion"]["outcome"])

    def test_durable_rerun_confirmed_attempt_and_lost_POST_never_repeated(self):
        req, read = self.context()
        value = result(req, "rerun")
        value["rerun_run"] = 200
        value["diagnoses"][0]["decision"] = "rerun"
        state = task_state(req, value)
        store, name = stored(state)
        publisher = TaskPublisher(read)
        publisher.uncertain = True
        with patch("loop.task_effects.evidence", return_value=(
                {"verification_sha256": digest(state["report"])}, {})), \
                patch("loop.task_effects.time.time", return_value=100), self.assertRaises(APIError):
            publish_task(store, name, state, Mock(), read, publisher, 100)
        confirmed = confirm_task(store, name, store.entries[name], read, 101)
        self.assertEqual("waiting_ci", confirmed["stage"])
        self.assertEqual(2, confirmed["ci_reruns"][0]["resulting_attempt"])
        self.assertEqual(1, len(publisher.posts))

    def test_active_CI_waits_without_admitting_a_worker(self):
        req, read = self.context()
        read.checks[0].update(status="in_progress", conclusion=None)
        state = checkpoint(req)
        state.update(stage="waiting_ci", publications=[], effects=[])
        store, name = stored(state)
        waiting = watch_ci_fix(store, name, state, read, 100)
        self.assertEqual("waiting_ci", waiting["stage"])
        self.assertEqual(0, waiting["iteration"])
        self.assertIsNone(waiting["intent"])

    def test_failed_ci_next_pass_keeps_the_phase_revision_pin(self):
        req, read = self.context()
        req["workflow_ref"] = revision_ref(REVISION)
        state = task_state(req, result(req))
        state.update(stage="waiting_ci", report=None)
        store, name = stored(state)
        next_pass = watch_ci_fix(store, name, state, read, 100)
        self.assertEqual("source_pending", next_pass["stage"])
        self.assertEqual(req["workflow_revision"], next_pass["request"]["workflow_revision"])
        self.assertEqual(req["workflow_ref"], next_pass["request"]["workflow_ref"])
        self.assertEqual(state["phase"], next_pass["phase"])
        self.assertEqual(1, next_pass["iteration"])

    def test_failed_jobs_rerun_preserves_proven_previous_nonblocking_jobs(self):
        req, read = self.context(attempt=2)
        read.checks[0]["conclusion"] = "success"
        reused = dict(read.checks[0], id=334, name="other check",
                      details_url=f"https://github.com/{FIXTURE}/actions/runs/200/job/334")
        read.checks.append(reused)
        original = read.call
        for conclusion in ("success", "skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                read.checks[1]["conclusion"] = conclusion
                def response(path, *args, **kwargs):
                    if path == f"repos/{FIXTURE}/actions/jobs/334":
                        return {"id": 334, "run_id": 200, "run_attempt": 1,
                                "conclusion": conclusion, "name": "other check",
                                "check_run_url": f"https://api.github.com/repos/{FIXTURE}/check-runs/334"}
                    return original(path, *args, **kwargs)
                read.call = response
                ci = collect(read, req, [CI_CHECK, "other check"])
                self.assertEqual("passed", ci["decision"])
                self.assertEqual(1, ci["checks"][1]["actions"]["job_attempt"])
                self.assertEqual(2, ci["checks"][1]["actions"]["attempt"])

    def test_non_actions_missing_evidence_and_rerun_unknown_never_clear(self):
        req, read = self.context()
        read.checks[0].update(app={"id": 777, "slug": "external"},
                              output={"summary": "External build failed on unchanged base"})
        req["ci_evidence"] = collect(read, req, [CI_CHECK])
        self.assertEqual("check_output", req["ci_evidence"]["failures"][0]["availability"])
        value = result(req)
        value["diagnoses"][0]["evidence"] = ["unchanged base"]
        diagnoses(value, req)
        value.update(outcome="rerun", rerun_run=200)
        value["diagnoses"][0]["decision"] = "rerun"
        with self.assertRaises(Rejected):
            diagnoses(value, req)
        read.checks[0]["output"] = {}
        req["ci_evidence"] = collect(read, req, [CI_CHECK])
        value = result(req, "blocked")
        value["diagnoses"][0].update(decision="unknown", evidence=[])
        diagnoses(value, req)

    def test_fresh_CI_registration_and_active_rerun_wait_instead_of_clearing(self):
        req, read = self.context(attempt=2)
        read.checks = []
        read.runs[0]["status"] = "queued"
        self.assertEqual("pending", collect(read, req, [CI_CHECK])["decision"])
        read.runs[0]["status"] = "completed"
        state = checkpoint(req)
        state.update(stage="published", effects=[], iteration=1,
                     publications=[{"sha": req["frozen_sha"], "effect": "push", "confirmed_at": 100}])
        store, name = stored(state)
        waiting = watch_ci_fix(store, name, state, read, 101)
        self.assertEqual("waiting_ci", waiting["stage"])
        self.assertEqual("missing", waiting["ci"]["decision"])
        self.assertEqual("fresh_CI_registration_pending", waiting["reason"])
        store.entries[name] = copy.deepcopy(waiting)
        stopped = watch_ci_fix(store, name, waiting, read, 1001)
        self.assertEqual("blocked", stopped["stage"])
