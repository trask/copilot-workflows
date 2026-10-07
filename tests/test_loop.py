from tests.support import launch, reconstruct
from tests.fixtures import (FIXTURE, REPOSITORIES, TARGET)
import copy
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import zipfile
from functools import lru_cache
from pathlib import Path
from unittest.mock import Mock, patch

from loop.api import API, APIError
from loop.cli import choose_verification, finalize
from loop.coordinator import (cancel, checkpoint, dispatch, reconcile, record_result, run_binding)
from loop.freeze import select_findings, unresolved_ids
from loop.policy import (AUTHOR_ID, BOT_ID, BOT_NODE, CENTRAL, DEFAULTS, Rejected, bot, checkpoint_name, digest, dispositions, eligible, parse_target, publication_gate, unchanged)
from loop.state import Conflict, State
from loop.publication import plan, reconcile_uncertain_push
from loop.worker_home import prepare_home
from loop.verify import (FILES, artifact_metadata, git, parse_json, read_zip, safe_path, verify)
from loop.source import (SourceAPI, bind_manifest, download_source, gated_request, import_source,
                         package_source, public_fetch, source_api, source_metadata, target_api)

SHA = "a" * 40
REVISION = "b" * 40
BOT = {"id": BOT_ID, "node_id": BOT_NODE, "type": "Bot",
       "login": "copilot-pull-request-reviewer[bot]"}


def pr():
    repo = {"id": REPOSITORIES[TARGET], "full_name": TARGET, "private": False}
    return {"number": 1, "state": "open", "merged": False,
            "user": {"id": AUTHOR_ID, "type": "User", "login": "launch-owner"},
            "head": {"sha": SHA, "ref": "trask-fix", "repo": repo.copy()},
            "base": {"ref": "main", "repo": repo.copy()}}


def review(body="A finding", **kwargs):
    return dict({"id": 12, "user": BOT.copy(), "commit_id": SHA,
                 "state": "COMMENTED", "body": body, "submitted_at": "2026-09-30T00:00:00Z"},
                **kwargs)


def request():
    return dict(eligible(pr()), schema=2, protocol="reviewable-v1", request_id="d" * 32, workflow_revision=REVISION,
                commit_author={"id": AUTHOR_ID, "login": "launch-owner"},
                frozen_at=10, frozen_at_iso="1970-01-01T00:00:10Z", deadline=7210,
                budgets=DEFAULTS.copy(),
                findings=[{"key": "review:12", "body": "A finding"}],
                baseline_review_ids=[12])


def result(req, outcome="no_change", disposition="not_warranted"):
    from tests.support import semantic
    return semantic(req, outcome, GOOD_PATCH if outcome == "fixes" else b"", disposition)


def verified_result(req):
    return {"schema": 2, "request_digest": digest(req), "verification": "verified",
            "publication_eligible": False, "run_id": 24, "run_attempt": 1,
            "candidate": {"changed": False}, "dispositions": result(req)}


def run():
    return {"id": 24, "run_attempt": 1, "display_title": "Copilot worker " + "d" * 32,
            "repository": {"full_name": CENTRAL}, "head_repository": {"full_name": CENTRAL},
            "event": "workflow_dispatch", "path": ".github/workflows/copilot-worker.lock.yml",
            "head_sha": REVISION, "status": "completed", "conclusion": "success",
            "html_url": "https://github.com/trask/copilot-workflows/actions/runs/24"}


class MemoryState:
    def __init__(self):
        self.entries = {}

    def snapshot(self):
        return None, None, copy.deepcopy(self.entries)

    def update(self, name, operation):
        value = operation(copy.deepcopy(self.entries.get(name)))
        self.entries[name] = copy.deepcopy(value)
        return value


class FakeAPI:
    def __init__(self, runs=None):
        self.runs = [] if runs is None else runs
        self.calls = []
        self.uncertain = False
        self.revision_refs = {}

    def call(self, path, method="GET", data=None):
        self.calls.append((path, method, data))
        if path.endswith("ref/heads/main"):
            return {"object": {"sha": REVISION}}
        if "/git/ref/heads/review-loop-revisions/" in path:
            ref = "refs/heads/" + path.split("/git/ref/heads/", 1)[1]
            if ref not in self.revision_refs:
                raise APIError(404, "Missing pinned ref")
            return {"ref": ref, "object": {"type": "commit", "sha": self.revision_refs[ref]}}
        if method == "POST" and self.uncertain:
            raise APIError(503, "Interrupted dispatch")
        if path.endswith("/git/refs") and method == "POST":
            self.revision_refs[data["ref"]] = data["sha"]
        return None

    def pages(self, *_args):
        if _args[-1] == "artifacts":
            return []
        if _args[-1] == "jobs":
            return [{"name": "agent", "conclusion": "success"}]
        return self.runs


class PolicyTests(unittest.TestCase):
    def test_exact_target_and_author_and_fork(self):
        eligible(pr())
        for change in ["author", "fork", "sha", "closed", "branch"]:
            value = pr()
            if change == "author":
                value["user"]["id"] = 999
                value["user"]["login"] = "trask"
            elif change == "fork":
                value["head"]["repo"]["id"] = 2
            elif change == "sha":
                value["head"]["sha"] = "-command"
            elif change == "closed":
                value["state"] = "closed"
            else:
                value["head"]["ref"] = "foo/../../.git"
            with self.subTest(change=change), self.assertRaises(Rejected):
                eligible(value)

    def test_identity_is_not_login(self):
        self.assertTrue(bot(BOT))
        self.assertTrue(bot(dict(BOT, login="Copilot")))
        self.assertFalse(bot(dict(BOT, id=999, login="copilot-pull-request-reviewer[bot]")))
        self.assertFalse(bot(dict(BOT, type="User")))
        self.assertFalse(bot(dict(BOT, node_id="fake")))

    def test_stale_head(self):
        live = pr()
        live["head"]["sha"] = "f" * 40
        with self.assertRaises(Rejected):
            unchanged(request(), live)

    def test_every_disposition_exactly_once(self):
        req = request()
        dispositions(result(req), req)
        for mutation in ["missing", "extra", "duplicate", "false_clean", "wrong_digest", "keys"]:
            value = result(req)
            if mutation == "missing":
                value["findings"] = []
            elif mutation == "extra":
                value["findings"][0]["key"] = "review:999"
            elif mutation == "duplicate":
                value["findings"] *= 2
            elif mutation == "false_clean":
                value["outcome"] = "clean"
            elif mutation == "wrong_digest":
                value["request_digest"] = "0" * 64
            else:
                value["publication_eligible"] = True
            with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                dispositions(value, req)

    def test_no_change_and_blocked_cannot_fake_fixed(self):
        with self.assertRaises(Rejected):
            dispositions(result(request(), "no_change", "fixed"), request())
        dispositions(result(request(), "blocked", "blocked"), request())

    def test_all_publication_app_fork_gates_always_closed(self):
        for mode in ["shadow", "publish", "app_installed", "fork_allow_edits", "pat"]:
            with self.subTest(mode=mode), self.assertRaises(Rejected):
                publication_gate(mode=mode, authorized=True, qualified=True)

    def test_body_hidden_findings_and_submitted_requirement(self):
        body = "<!-- ccr-overview-v2 --> **Findings:** None <strong>Previously missed (1)</strong> bug"
        selected = select_findings([review(body)], [], set(), SHA)
        self.assertEqual(body, selected[0]["body"])
        with self.assertRaises(Rejected):
            select_findings([review(submitted_at=None)], [], set(), SHA)

    def test_bot_reply_in_human_thread_is_not_actionable(self):
        comment = {"id": 20, "in_reply_to_id": 19, "user": BOT,
                   "pull_request_review_id": 12, "commit_id": SHA, "path": "foo.java",
                   "original_commit_id": SHA, "original_line": 10,
                   "line": 10, "body": "Reply to human"}
        self.assertEqual(["review:12"], [f["key"] for f in
                         select_findings([review()], [comment], {19, 20}, SHA)])
        comment.pop("in_reply_to_id")
        self.assertEqual(["review:12", "inline:20"], [f["key"] for f in
                         select_findings([review()], [comment], {20}, SHA)])

    def test_complete_feedback_and_conversations_keep_every_verified_root(self):
        from loop.effects import conversation
        from loop.policy import supported_checkpoint
        comments = [{"id": 20 + index, "user": BOT, "pull_request_review_id": 12,
                     "commit_id": SHA, "original_commit_id": SHA, "path": "Foo.java",
                     "line": 1, "original_line": 1, "body": "\u00e9" * 3000}
                    for index in range(101)]
        selected = select_findings([review("")], comments, {c["id"] for c in comments}, SHA)
        self.assertEqual([c["body"] for c in comments], [f["body"] for f in selected])
        replies = [dict(comments[0], id=1000 + index, in_reply_to_id=20) for index in range(101)]
        context = conversation(comments + replies, 20)
        self.assertEqual([20, *range(1000, 1101)], [c["id"] for c in context])
        self.assertEqual(hashlib.sha256(comments[0]["body"].encode()).hexdigest(),
                         context[0]["body_hash"])
        req = dict(request(), findings=selected)
        dispositions(result(req), req)
        state = dict(checkpoint(req), effects=[
            {"key": f["key"], "root": f["comment_id"], "thread": str(f["comment_id"]),
             "status": "pending"} for f in selected])
        supported_checkpoint(state)
        state["effects"].append(state["effects"][0])
        with self.assertRaisesRegex(Rejected, "Duplicate current thread effects"):
            supported_checkpoint(state)

    def test_thread_pagination_and_root_only(self):
        class Threads:
            def graphql(self, _query, variables):
                page = 0 if variables["cursor"] is None else 1
                return {"repository": {"pullRequest": {"reviewThreads": {
                    "pageInfo": {"hasNextPage": page == 0, "endCursor": "next"},
                    "nodes": [{"id": str(page), "isResolved": False, "isOutdated": False,
                               "comments": {"nodes": [{"databaseId": page + 1}]}}],
                }}}}
        self.assertEqual({1, 2}, unresolved_ids(Threads(), 1, TARGET))


class ProtocolTests(unittest.TestCase):
    def replacement_context(self, stage="blocked"):
        req = dict(request(), repo=FIXTURE, repo_id=REPOSITORIES[FIXTURE], head_repo=FIXTURE,
                   head_repo_id=REPOSITORIES[FIXTURE],
                   source_private=False)
        old = checkpoint(req)
        old.update(stage=stage, iteration=2, generation=3, reason="failed",
                   run={"id": 24, "attempt": 1}, intent={"id": req["request_id"]},
                   report={"validation": "failed"}, artifacts=[{"id": 77}])
        store = MemoryState()
        name = checkpoint_name(FIXTURE, 1, REPOSITORIES[FIXTURE])
        store.entries[name] = copy.deepcopy(old)
        new = dict(req, request_id="e" * 32, workflow_revision="f" * 40,
                   frozen_at=500, deadline=7700)
        return store, FakeAPI([run()]), old, new, name




    def test_replacement_archive_transaction_preserves_provenance_and_caps(self):
        _, _, old, new, name = self.replacement_context()
        value = dict(checkpoint(new), generation=4)
        api = FakeAPI()
        api.call = Mock(return_value={"sha": SHA})
        store = State(api)
        with patch.object(store, "snapshot", return_value=(SHA, REVISION, {name: old})):
            store.write(name, value, old)
        blobs = [json.loads(call.args[2]["content"]) for call in api.call.call_args_list
                 if call.args[0].endswith("/blobs")]
        self.assertEqual([value, old], blobs)
        tree = next(call.args[2] for call in api.call.call_args_list
                    if call.args[0].endswith("/trees"))
        self.assertEqual({name, "request-" + "d" * 32 + ".json"},
                         {item["path"] for item in tree["tree"]})
        api.call.reset_mock()
        entries = {name: old, "request-" + "d" * 32 + ".json": {"wrong": "provenance"}}
        with patch.object(store, "snapshot", return_value=(SHA, REVISION, entries)):
            with self.assertRaises(Rejected):
                store.write(name, value, old)
        api.call.assert_not_called()

    def test_manual_rerun_after_skipped_worker_starts_a_fresh_phase(self):
        store, api = MemoryState(), FakeAPI([run()])
        name, _ = launch(store, request(), "shadow", True)
        dispatch(store, name, api, 11)
        original = api.pages
        api.pages = lambda path, key: ([{"name": "agent", "conclusion": "skipped"}]
                                      if key == "jobs" else original(path, key))
        reconcile(store, name, api, 311)
        self.assertEqual("worker_agent_skipped", store.entries[name]["reason"])
        stopped = copy.deepcopy(store.entries[name])
        with self.assertRaises(Rejected):
            launch(store, request(), "shadow", True, api=api)
        retry = dict(request(), request_id="e" * 32, frozen_at=500, deadline=7700)
        _, changed = launch(store, retry, "shadow", True, api=api)
        self.assertEqual("ready", changed["stage"])
        self.assertEqual(0, changed["iteration"])
        self.assertEqual(7700, changed["request"]["deadline"])
        self.assertNotEqual(stopped["phase"], changed["phase"])
        self.assertEqual(1, stopped["iteration"])



    def test_uncertain_dispatch_reconciles_without_retry(self):
        store, api = MemoryState(), FakeAPI()
        name, _ = launch(store, request(), "shadow", True)
        api.uncertain = True
        with self.assertRaises(APIError):
            dispatch(store, name, api, 11)
        self.assertEqual("dispatch_intent", store.entries[name]["stage"])
        api.runs = [run()]
        reconcile(store, name, api, 311)
        self.assertEqual("verify_pending", store.entries[name]["stage"])
        self.assertEqual(1, sum(method == "POST" for _, method, _ in api.calls))

    def test_uncertain_absence_is_blocked_not_redispatched(self):
        store, api = MemoryState(), FakeAPI()
        name, _ = launch(store, request(), "shadow", True)
        dispatch(store, name, api, 11)
        reconcile(store, name, api, 1000)
        self.assertEqual("dispatch_uncertain_no_blind_retry", store.entries[name]["reason"])

    def test_duplicate_runs_and_rerun(self):
        store, api = MemoryState(), FakeAPI([run(), dict(run(), id=25)])
        name, _ = launch(store, request(), "shadow", True)
        dispatch(store, name, api, 11)
        reconcile(store, name, api, 311)
        self.assertEqual("duplicate_run_identity", store.entries[name]["reason"])
        with self.assertRaises(Rejected):
            run_binding(dict(run(), run_attempt=2), request())

    def test_cancellation_invalidates_result(self):
        store, api = MemoryState(), FakeAPI([run()])
        name, _ = launch(store, request(), "shadow", True)
        dispatch(store, name, api, 11)
        expected = reconcile(store, name, api, 311)
        cancel(store, name, expected["request"]["request_id"], expected["generation"], 312)
        with self.assertRaises(Rejected):
            record_result(store, name, expected, {"validation": "unattested"}, [])
        self.assertEqual("cancelled", store.entries[name]["stage"])

    def test_cancel_binds_identity_in_transaction_and_preserves_terminal_evidence(self):
        store = MemoryState()
        name, state = launch(store, request(), "shadow", True)
        for identity, generation in [("", 1), ("e" * 32, 1), ("d" * 32, 0),
                                     ("d" * 32, 2), ("d" * 32, True)]:
            with self.subTest(identity=identity, generation=generation), self.assertRaises(Rejected):
                cancel(store, name, identity, generation, 12)
            self.assertEqual(state, store.entries[name])
        result = cancel(store, name, "d" * 32, 1, 12)
        self.assertEqual(2, result["generation"])
        self.assertEqual(state["request"], result["request"])
        self.assertEqual("ready", result["cancellation"]["previous_stage"])
        self.assertEqual(result, cancel(store, name, "d" * 32, 2, 20))
        self.assertEqual(12, store.entries[name]["cancelled_at"])
        for stage in ("clean", "blocked", "failed", "exhausted", "preview_complete"):
            store.entries[name] = dict(state, stage=stage)
            with self.subTest(stage=stage), self.assertRaises(Rejected):
                cancel(store, name, "d" * 32, 1, 12)
            self.assertEqual(stage, store.entries[name]["stage"])

    def test_cancel_rejects_concurrent_replacement_and_missing_checkpoint(self):
        store = MemoryState()
        name, state = launch(store, request(), "shadow", True)
        update = store.update
        def race(path, operation):
            replacement = dict(state, request=dict(state["request"], request_id="e" * 32), generation=2)
            store.entries[path] = replacement
            return update(path, operation)
        store.update = race
        with self.assertRaises(Rejected):
            cancel(store, name, "d" * 32, 1, 12)
        self.assertEqual("e" * 32, store.entries[name]["request"]["request_id"])
        store = MemoryState()
        with self.assertRaises(Rejected):
            cancel(store, name, "d" * 32, 1, 12)

    def test_timeout_and_failure_are_terminal(self):
        for conclusion, expected in [("failure", "failed"), ("cancelled", "cancelled"),
                                     ("timed_out", "failed")]:
            store, api = MemoryState(), FakeAPI([dict(run(), conclusion=conclusion)])
            name, _ = launch(store, request(), "shadow", True)
            dispatch(store, name, api, 11)
            reconcile(store, name, api, 311)
            self.assertEqual(expected, store.entries[name]["stage"])
        store, api = MemoryState(), FakeAPI()
        name, _ = launch(store, request(), "shadow", True)
        dispatch(store, name, api, 7211)
        self.assertEqual("exhausted", store.entries[name]["stage"])

    def test_wrong_workflow_revision_rejected(self):
        with self.assertRaises(Rejected):
            run_binding(dict(run(), head_sha="f" * 40), request())

    def test_expired_verification_is_never_launched_and_becomes_exhausted(self):
        store = MemoryState()
        state = checkpoint(request())
        state.update(stage="verify_pending", iteration=1, run={"id": 24, "attempt": 1})
        store.entries["pr-v2-210933087-1.json"] = state
        with patch("loop.cli.output") as emit:
            choose_verification(store, 7211)
            emit.assert_not_called()
        reconcile(store, "pr-v2-210933087-1.json", FakeAPI(), 7211)
        self.assertEqual("exhausted", store.entries["pr-v2-210933087-1.json"]["stage"])

    def test_stale_head_in_finalize_persists_terminal_block(self):
        req = request()
        store = MemoryState()
        state = checkpoint(req)
        state.update(stage="verify_pending", run={"id": 24, "attempt": 1})
        store.entries["pr-v2-210933087-1.json"] = state
        report = {"schema": 2, "request_id": req["request_id"], "generation": 1,
                  "run_id": 24, "run_attempt": 1, "request_digest": digest(req), "artifacts": [],
                  "verification": "verified",
                  "result": verified_result(req)}
        class Args:
            pr = "1"
            repo = TARGET
            request_id = req["request_id"]
            generation = "1"
        live = pr()
        live["head"]["sha"] = "f" * 40
        api = FakeAPI()
        api.call = lambda *_args: live
        with patch("loop.cli.time.time", return_value=100), \
                patch("loop.cli.Path.exists", return_value=True), \
                patch("loop.cli.Path.is_symlink", return_value=False), \
                patch("loop.cli.Path.stat") as stats, \
                patch("loop.cli.Path.read_bytes", return_value=json.dumps(report).encode()):
            stats.return_value.st_size = 100
            with self.assertRaises(Rejected):
                finalize(api, store, Args())
        self.assertEqual("blocked", store.entries["pr-v2-210933087-1.json"]["stage"])
        self.assertEqual("stale_target", store.entries["pr-v2-210933087-1.json"]["reason"])

    def test_state_nonforce_cas_race_and_retry(self):
        class RaceAPI:
            def __init__(self):
                self.calls = []
            def call(self, path, method="GET", data=None):
                self.calls.append((path, method, data))
                if method == "PATCH":
                    self.assert_no_force = data["force"] is False
                    raise APIError(422, "Race")
                return {"sha": SHA}
        api = RaceAPI()
        store = State(api)
        before = checkpoint(request())
        after = dict(before, generation=2)
        with patch.object(store, "snapshot", return_value=(SHA, REVISION, {"pr-v2-210933087-1.json": before})):
            with self.assertRaises(Conflict):
                store.write("pr-v2-210933087-1.json", after, before)
        self.assertTrue(api.assert_no_force)
        commit = [data for path, _, data in api.calls if path.endswith("/commits")][0]
        self.assertEqual([SHA], commit["parents"])
        tree = [data for path, _, data in api.calls if path.endswith("/trees")][0]
        self.assertEqual(REVISION, tree["base_tree"])

    def test_legacy_verifier_report_is_terminal_and_retains_finalizer_identity(self):
        req = request()
        store = MemoryState()
        state = checkpoint(req)
        state.update(stage="verify_pending", run={"id": 24, "attempt": 1})
        store.entries["pr-v2-210933087-1.json"] = state
        report = {"schema": 2, "request_id": req["request_id"], "generation": 1,
                  "run_id": 24, "run_attempt": 1, "request_digest": digest(req), "artifacts": [],
                  "verification": "verified",
                  "result": {"request_digest": digest(req), "publication_eligible": False,
                             "validation": "unattested"}}
        args = Mock(pr="1", request_id=req["request_id"], generation="1", repo=TARGET)
        api = FakeAPI()
        api.call = lambda *_args: pr()
        with patch("loop.cli.time.time", return_value=100), \
                patch("loop.cli.Path.exists", return_value=True), \
                patch("loop.cli.Path.is_symlink", return_value=False), \
                patch("loop.cli.Path.stat") as stats, \
                patch("loop.cli.Path.read_bytes", return_value=json.dumps(report).encode()), \
                patch.dict(os.environ, {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1"}):
            stats.return_value.st_size = 100
            with self.assertRaises((KeyError, Rejected)):
                finalize(api, store, args)
        self.assertEqual("failed", store.entries["pr-v2-210933087-1.json"]["stage"])
        self.assertEqual("finalization_failed", store.entries["pr-v2-210933087-1.json"]["reason"])
        self.assertEqual("99", store.entries["pr-v2-210933087-1.json"]["finalizer_run"])

    def test_structural_finalizer_reads_only_verification_report(self):
        req = request()
        store = MemoryState()
        _, state = launch(store, req, "publish", True)
        req = state["request"]
        state.update(stage="verify_pending", iteration=1, run={"id": 24, "attempt": 1})
        name = "pr-v2-210933087-1.json"
        store.entries[name] = state
        report = {"schema": 2, "request_id": req["request_id"], "generation": 1,
                  "run_id": 24, "run_attempt": 1, "request_digest": digest(req), "artifacts": [],
                  "verification": "verified", "result": verified_result(req)}
        args = Mock(pr="1", request_id=req["request_id"], generation="1", repo=TARGET)
        api = FakeAPI()
        api.call = lambda *_args: pr()
        with patch("loop.cli.time.time", return_value=100), \
                patch("loop.cli.Path.exists", return_value=True), \
                patch("loop.cli.Path.is_symlink", return_value=False), \
                patch("loop.cli.Path.stat") as stats, \
                patch("loop.cli.Path.read_bytes", side_effect=[json.dumps(report).encode()]) as read, \
                patch.dict(os.environ, {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1"}), \
                patch("loop.cli.summary"):
            stats.return_value.st_size = 100
            finalize(api, store, args)
        read.assert_called_once()
        self.assertEqual("publish_pending", store.entries[name]["stage"])
        self.assertEqual("pending_fresh_trusted_personal_acceptance", store.entries[name]["reason"])

    def test_projected_unicode_checkpoint_and_archive_limits_precede_writes(self):
        api = FakeAPI()
        store = State(api)
        value = checkpoint(request())
        value["request"]["findings"][0]["body"] = "\u4e00" * 200000
        with patch.object(store, "snapshot", return_value=(SHA, REVISION, {})):
            with self.assertRaises(Rejected):
                store.write("pr-v2-210933087-1.json", value, None)
        self.assertEqual([], api.calls)
        before = checkpoint(request())
        after = copy.deepcopy(before)
        after["request"]["request_id"] = "e" * 32
        entries = {f"pr-v2-210933087-{i + 1}.json": before for i in range(1000)}
        with patch.object(store, "snapshot", return_value=(SHA, REVISION, entries)):
            with self.assertRaises(Rejected):
                store.write("pr-v2-210933087-1.json", after, before)
        self.assertEqual([], api.calls)
        store.sizes = {key: 20000 for key in entries}
        with patch.object(store, "snapshot", return_value=(SHA, REVISION, entries)):
            with self.assertRaises(Rejected):
                store.write("pr-v2-210933087-1.json", before, before)
        self.assertEqual([], api.calls)


class PublisherTests(unittest.TestCase):
    def context(self):
        req = request()
        candidate = {"parent": SHA, "commit": "c" * 40, "tree": "e" * 40}
        authorization = {"request_id": req["request_id"], "expected_sha": SHA,
                         "candidate_commit": candidate["commit"], "installation_id": 123}
        return req, candidate, authorization

    def test_shadow_request_never_enables_personal_executor(self):
        req, candidate, authorization = self.context()
        with self.assertRaisesRegex(Rejected, "owner-authorized"):
            plan(req, pr(), candidate, authorization, "publish")
        with self.assertRaises(Rejected):
            plan(req, pr(), candidate, {}, "publish")
        with self.assertRaises(Rejected):
            plan(req, pr(), candidate, authorization, "shadow")

    def test_uncertain_publication_never_blindly_retries(self):
        req, candidate, _ = self.context()
        self.assertEqual("blocked_uncertain_publication_requires_operator",
                         reconcile_uncertain_push(req, candidate, pr()))
        live = pr()
        live["head"]["sha"] = candidate["commit"]
        self.assertEqual("published_waiting_review_request",
                         reconcile_uncertain_push(req, candidate, live))
        live["head"]["repo"]["id"] = 99
        with self.assertRaises(Rejected):
            reconcile_uncertain_push(req, candidate, live)


class WorkerHomeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "AWF preparation is hosted Linux only")
    def test_fresh_home_cache_directories_are_owned_writable_and_never_reused(self):
        with tempfile.TemporaryDirectory(prefix="review-home-test-", dir="/tmp") as directory:
            home = Path(directory)
            home.rmdir()
            with patch("loop.worker_home.WORKER_HOME", home):
                prepare_home(home)
                for path in [home, home / ".gradle", home / ".m2", home / ".cache"]:
                    self.assertEqual(os.getuid(), path.stat().st_uid)
                    self.assertEqual(0o700, path.stat().st_mode & 0o777)
                    (path / "write-probe").write_text("owned", encoding="ascii")
                with self.assertRaises(FileExistsError):
                    prepare_home(home)
                self.assertEqual("owned", (home / ".gradle" / "write-probe").read_text())

    def test_home_outside_dedicated_directory_is_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "untrusted"
            with self.assertRaises(Rejected):
                prepare_home(home)
            self.assertFalse(home.exists())


@lru_cache(maxsize=1)
def baseline_objects():
    with tempfile.TemporaryDirectory() as directory:
        git(["init", "--bare", "--quiet"], directory)
        blob = git(["hash-object", "-w", "--stdin"], directory, b"old\n").decode().strip()
        tree = git(["mktree"], directory, f"100644 blob {blob}\tFoo.java\n".encode()).decode().strip()
        commit = git(["hash-object", "-t", "commit", "-w", "--stdin"], directory,
                     (f"tree {tree}\nauthor Test <test@invalid> 0 +0000\n"
                      "committer Test <test@invalid> 0 +0000\n\nbase\n").encode()).decode().strip()
        return commit, {path.relative_to(directory): path.read_bytes()
                        for path in Path(directory, "objects").rglob("*") if path.is_file()}


def baseline(directory):
    commit, objects = baseline_objects()
    for relative, content in objects.items():
        destination = Path(directory, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    Path(directory, "FETCH_HEAD").write_text(commit + "\n", encoding="ascii")
    return commit


GOOD_PATCH = b"""diff --git a/Foo.java b/Foo.java
index 3367afd..3e75765 100644
--- a/Foo.java
+++ b/Foo.java
@@ -1 +1 @@
-old
+new
"""


class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            git(["init", "--bare", "--quiet"], directory)
            cls.frozen_sha = baseline(directory)

    def setUp(self):
        self.req = dict(request(), frozen_sha=self.frozen_sha)

    def payload(self, patch_data=GOOD_PATCH, outcome="fixes", disposition="fixed"):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("result.json", json.dumps(result(self.req, outcome, disposition)))
            archive.writestr("candidate.patch", patch_data)
            archive.writestr("diagnostics.txt", "candidate command logs")
        return output.getvalue()

    def test_warranted_fix_derives_ids_but_never_accepts_claims(self):
        report = verify(self.payload(), self.req, run(), {"id": 33, "digest": "sha256:" + "e" * 64},
                        baseline)
        self.assertTrue(report["candidate"]["changed"])
        self.assertEqual(["Foo.java"], report["candidate"]["changed_paths"])
        self.assertEqual(self.req["frozen_sha"], report["candidate"]["parent"])
        self.assertEqual("verified", report["verification"])
        self.assertNotIn("validation_claim", report)
        self.assertNotIn("objective_validation", report)
        self.assertFalse(report["publication_eligible"])

    def test_persistent_candidate_bundle_roundtrip(self):
        with tempfile.TemporaryDirectory() as package, tempfile.TemporaryDirectory() as restored:
            report = verify(self.payload(), self.req, run(),
                            {"id": 33, "digest": "server"}, baseline, package)
            bundle = Path(package, "candidate.bundle")
            self.assertEqual(report["candidate"]["bundle_sha256"],
                             hashlib.sha256(bundle.read_bytes()).hexdigest())
            git(["init", "--bare", "--quiet"], restored)
            self.assertEqual(self.req["frozen_sha"], baseline(restored))
            git(["bundle", "verify", str(bundle)], restored)
            # No untrusted bundle is imported by production code. This roundtrip consumes
            # a trusted packager-created fixture and verifies the published artifact format.
            git(["-c", "protocol.file.allow=always", "fetch", "--quiet", str(bundle),
                 "refs/heads/candidate:refs/heads/restored"], restored)
            candidate = report["candidate"]
            self.assertEqual(candidate["commit"],
                             git(["rev-parse", "restored"], restored).decode().strip())
            self.assertEqual(candidate["parent"],
                             git(["rev-parse", "restored^"], restored).decode().strip())
            self.assertEqual(candidate["tree"],
                             git(["rev-parse", "restored^{tree}"], restored).decode().strip())
            self.assertEqual(b"new\n", git(["show", "restored:Foo.java"], restored))

    def test_no_change_is_verified_without_a_test_plan(self):
        no_change = verify(self.payload(b"", "no_change", "not_warranted"), self.req, run(),
                           {"id": 33, "digest": "server"}, baseline)
        self.assertFalse(no_change["candidate"]["changed"])
        self.assertEqual("verified", no_change["verification"])
        with self.assertRaises(Rejected):
            verify(self.payload(b""), self.req, run(), {"id": 33, "digest": "server"}, baseline)

    def test_malicious_archive_members(self):
        for member in ["../result.json", "/result.json", "foo/result.json", "result.json", "validation.json"]:
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as archive:
                for name in FILES:
                    archive.writestr(name, "{}")
                archive.writestr(member, "{}")
            with self.subTest(member=member), self.assertRaises(Rejected):
                read_zip(payload.getvalue())
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            for name in FILES:
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, "/etc/passwd")
        with self.assertRaises(Rejected):
            read_zip(payload.getvalue())

    def test_missing_malformed_json_and_duplicate_keys(self):
        for data in [b'{"a":1,"a":2}', b'{"a":NaN}', b'invalid']:
            with self.subTest(data=data), self.assertRaises((Rejected, ValueError)):
                parse_json(data)
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("result.json", "{}")
        with self.assertRaises(Rejected):
            read_zip(payload.getvalue())

    def test_malicious_paths(self):
        for path in ["../x", "/x", "C:/x", ".git/config", ".GIT/config"]:
            with self.subTest(path=path), self.assertRaises(Rejected):
                safe_path(path)

    def test_cross_run_stale_missing_provenance(self):
        candidate = {"id": 33, "name": "candidate-24-1", "expired": False, "size_in_bytes": 100,
                     "digest": "sha256:" + "e" * 64,
                     "workflow_run": {"id": 24, "head_sha": REVISION}}
        class Artifacts:
            def pages(self, path, key):
                if key == "jobs":
                    return [{"name": "agent", "conclusion": "success"}]
                return [candidate]
        artifact_metadata(Artifacts(), run(), request())
        for change in ["run", "revision", "digest", "expired"]:
            original = copy.deepcopy(candidate)
            if change == "run":
                candidate["workflow_run"]["id"] = 99
            elif change == "revision":
                candidate["workflow_run"]["head_sha"] = SHA
            elif change == "digest":
                candidate["digest"] = ""
            else:
                candidate["expired"] = True
            with self.subTest(change=change), self.assertRaises(Rejected):
                artifact_metadata(Artifacts(), run(), request())
            candidate.clear()
            candidate.update(original)

    def test_no_credentials_and_hidden_windows_subprocess(self):
        with patch.dict(os.environ, {"GH_TOKEN": "sentinel", "GIT_CONFIG_COUNT": "1"}):
            with patch("loop.verify.subprocess.run") as child:
                child.return_value = subprocess.CompletedProcess([], 0, b"ok", b"")
                git(["version"], Path.cwd())
                kwargs = child.call_args.kwargs
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                self.assertNotIn("GIT_CONFIG_COUNT", kwargs["env"])
                self.assertEqual(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                 kwargs["creationflags"])


if __name__ == "__main__":
    unittest.main()
