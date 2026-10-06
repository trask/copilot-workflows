from tests.support import launch, reconstruct
import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from loop.api import API, APIError
from loop.cli import choose_live, finalize, main as cli_main, verify_pending
from loop.coordinator import (cancel, checkpoint, dispatch, due)
from loop.live import advance, guard, main as live_main, request_review, start
from loop.policy import Rejected, checkpoint_name, digest, eligible, parse_target, unchanged
from loop.publication import PublisherAPI, acceptance, authenticated_push
from loop.reviews import exact_ci, fresh_collection, select_checks
from loop.source import SourceAPI, import_source, package_source
from loop.state import State
from loop.verify import (git, safe_path, verify)
from tests import test_live as live_fixtures
from tests import test_loop as loop_fixtures
from tests.test_live import (CI_CHECK, FIXTURE, Read, Publisher,
                            TEST_TOKEN, live_state, personal_request, stored)
from tests.test_loop import AUTHOR_ID, FakeAPI, MemoryState, REVISION, SHA, pr, request


class GenericTests(unittest.TestCase):
    def test_explicit_targets_do_not_have_repository_or_language_allowlists(self):
        for repo in ("owner/python", "organization/workflows", "someone/java"):
            self.assertEqual((repo, 17), parse_target(repo + "#17"))
            self.assertEqual((repo, 17), parse_target("https://github.com/" + repo + "/pull/17"))
        for target in ("17", "https://evil.example/a/b/pull/17", "a/..#1",
                       "a/b#0", "a/b#1?token=x"):
            with self.assertRaises(Rejected):
                parse_target(target)

    def test_live_ids_and_fork_head_are_frozen_without_upstream_push_assumptions(self):
        value = pr()
        value["base"]["repo"].update(id=77, full_name="organization/workflows")
        value["head"]["repo"].update(id=88, full_name="owner/workflows", private=False)
        value["head"]["ref"] = "main"
        frozen = eligible(value, "organization/workflows")
        self.assertEqual(("organization/workflows", 77, "owner/workflows", 88),
                         tuple(frozen[k] for k in ("repo", "repo_id", "head_repo", "head_repo_id")))
        self.assertFalse(frozen["source_private"])
        unchanged(frozen, value)
        for side, key, replacement in (("head", "id", 99), ("head", "full_name", "other/workflows"),
                                       ("head", "private", True), ("base", "id", 99)):
            live = copy.deepcopy(value)
            live[side]["repo"][key] = replacement
            with self.assertRaises(Rejected):
                unchanged(frozen, live)

    def test_repository_id_checkpoint_namespace_cannot_collide_on_pr_number(self):
        self.assertNotEqual(checkpoint_name("a/project", 1, 77),
                            checkpoint_name("b/project", 1, 88))
        self.assertEqual("pr-v2-77-1.json", checkpoint_name("a/project", 1, 77))
        self.assertNotEqual(checkpoint_name("a/project", 1), checkpoint_name("b/project", 1))

    def test_legacy_state_is_inactive_and_never_upgraded_or_dispatched(self):
        legacy = {"schema": 1, "request": dict(request(), schema=1),
                  "stage": "ready", "next_check_at": 0}
        before = copy.deepcopy(legacy)
        store = MemoryState()
        store.entries["pr-1.json"] = legacy
        self.assertFalse(due(legacy, 100))
        api = FakeAPI()
        with self.assertRaises(Rejected):
            dispatch(store, "pr-1.json", api, 100)
        self.assertEqual(before, store.entries["pr-1.json"])
        self.assertEqual([], api.calls)
        with self.assertRaisesRegex(Rejected, "read-only"):
            verify(b"", legacy["request"], {}, {})

    def test_retired_operations_reject_before_api_access(self):
        for operation in ("repair-publication", "continue-personal-test"):
            with self.subTest(operation=operation), patch("sys.argv", ["loop.cli", operation]), \
                    patch("loop.cli.API") as api, self.assertRaises(SystemExit):
                cli_main()
            api.assert_not_called()

    def test_retired_publication_checkpoints_are_read_only(self):
        retired_stages = ("auth_pending", "capability_intent", "waiting_capability",
                          "capability_recheck", "root_effects")
        for change in (*retired_stages, "capability", "probe", "effects", "replies", "retry"):
            state = live_state(stage="published")
            if change in retired_stages:
                state["stage"] = change
            elif change == "capability":
                state["capability"] = {"copilot_review_request_qualified": True}
            elif change == "probe":
                state["capability_probe"] = {"status": "uncertain"}
            elif change == "effects":
                state["effects"] = [{"status": "reply_intent"}]
            elif change == "replies":
                state["request"]["publication"]["reply_bot_threads"] = True
            else:
                state["request"]["publication"]["reviewable_retry"] = {"consumed_pipelines": 1}
            store, name = stored(state)
            before = copy.deepcopy(state)
            read = Read()
            publisher = Mock()
            with self.subTest(change=change):
                with patch("loop.cli.output") as output, self.assertRaises(Rejected):
                    choose_live(store, 100, Mock())
                output.assert_not_called()
                with self.assertRaises(Rejected):
                    advance(store, name, state, Mock(), read, publisher, 100)
                with self.assertRaises(Rejected):
                    request_review(store, name, state, read, publisher, 100)
                with self.assertRaises(Rejected):
                    cancel(store, name, state["request"]["request_id"], state["generation"], 100)
                api = FakeAPI()
                with self.assertRaises(Rejected):
                    dispatch(store, name, api, 100)
                self.assertEqual(before, store.entries[name])
                self.assertEqual([], api.calls)
                self.assertEqual([], publisher.mock_calls)
                environment = {
                    "PR": "1", "REQUEST_ID": state["request"]["request_id"], "GENERATION": "6",
                    "GITHUB_SHA": REVISION, "GITHUB_RUN_ATTEMPT": "1",
                    "EXPECTED_STAGE": state["stage"], "PUBLISHER_TOKEN": TEST_TOKEN,
                    "TARGET_REPO": FIXTURE,
                }
                with patch.dict(os.environ, environment), patch("loop.live.API"), \
                        patch("loop.live.State", return_value=store), \
                        patch("loop.live.git", return_value=REVISION.encode()), \
                        patch("loop.live.PublisherAPI") as create_publisher, \
                        self.assertRaises(Rejected):
                    live_main()
                create_publisher.assert_not_called()
                self.assertEqual(before, store.entries[name])

    def test_retired_verification_rejects_without_rewriting_evidence(self):
        state = live_state(stage="verify_pending")
        state["request"]["publication"]["reply_bot_threads"] = True
        store, name = stored(state)
        before = copy.deepcopy(state)
        args = SimpleNamespace(pr="1", request_id=state["request"]["request_id"],
                               generation="6", repo=FIXTURE)
        for operation in (verify_pending, finalize):
            with self.subTest(operation=operation.__name__), \
                    self.assertRaisesRegex(Rejected, "read-only"):
                operation(Mock(), store, args)
            self.assertEqual(before, store.entries[name])

    def test_checkpoint_writes_bind_the_frozen_repository_namespace(self):
        with self.assertRaisesRegex(Rejected, "namespace"):
            State(API("unused")).write("pr-v2-999-1.json", checkpoint(request()), None)


    def test_scoped_read_client_allows_only_the_frozen_base_and_head(self):
        reader = SourceAPI(TEST_TOKEN, "organization/project", "owner/fork")
        with patch.object(API, "call", return_value={}) as call:
            reader.call("repos/owner/fork/git/ref/heads/change")
            self.assertEqual(1, call.call_count)
            with self.assertRaises(Rejected):
                reader.call("repos/unrelated/project")
            with self.assertRaises(Rejected):
                reader.call("repos/owner/fork/issues", method="POST", body={})

    def test_disabled_publication_records_an_access_gate_not_test_only_policy(self):
        fresh = dict(personal_request(max_pipelines=5), mode="shadow")
        fresh.pop("publication")
        store = MemoryState()
        _, result = start(store, FakeAPI(), fresh, "", 0, False,
                          "disabled", [], True, 100)
        self.assertEqual("blocked", result["stage"])
        self.assertEqual("human_gate_target_repository_push_and_review_access", result["reason"])
        self.assertEqual(0, result["iteration"])

    def test_upstream_access_does_not_authorize_a_fork_push(self):
        req = personal_request()
        req.update(head_repo="owner/fork", head_repo_id=99)
        publisher = PublisherAPI(TEST_TOKEN, req, "fine_grained_pat")
        live = pr()
        live["base"]["repo"].update(full_name=req["repo"], id=req["repo_id"])
        live["head"]["repo"].update(full_name=req["head_repo"], id=99)
        metadata = {
            "user": {"id": AUTHOR_ID, "type": "User", "login": "launch-owner"},
            f"repos/{req['repo']}": {"id": req["repo_id"], "full_name": req["repo"],
                                    "private": False, "permissions": {"push": True}},
            f"repos/{req['head_repo']}": {"id": 99, "node_id": "fork-node",
                                         "full_name": req["head_repo"], "private": False,
                                         "permissions": {"push": False}},
        }
        with patch.object(publisher, "call", side_effect=lambda path: metadata[path]):
            with self.assertRaisesRegex(Rejected, "head repository push access"):
                publisher.identity(req)
        with patch("loop.publication.subprocess.run") as native:
            native.return_value = Mock(returncode=0, stdout=b"", stderr=b"")
            authenticated_push(".", req, "c" * 40, TEST_TOKEN)
            self.assertEqual("https://github.com/owner/fork.git", native.call_args.args[0][-2])

    def test_target_workflow_and_python_edits_are_valid_git_data(self):
        for path in ("src/example.py", ".github/workflows/check.yml", "build.gradle", "package.json"):
            safe_path(path)
        req = request()
        from tests.test_loop import baseline
        with tempfile.TemporaryDirectory() as root:
            git(["init", "--bare", "--quiet"], root)
            req["frozen_sha"] = baseline(root)
        patch_data = b"""diff --git a/.github/workflows/check.yml b/.github/workflows/check.yml
new file mode 100644
--- /dev/null
+++ b/.github/workflows/check.yml
@@ -0,0 +1 @@
+name: Repository checks
"""
        candidate = reconstruct({"candidate.patch": patch_data}, req, baseline)
        self.assertEqual([".github/workflows/check.yml"], candidate["changed_paths"])
        from loop.policy import CENTRAL
        with self.assertRaisesRegex(Rejected, "Central trusted runtime"):
            safe_path(".github/workflows/check.yml", dict(req, head_repo=CENTRAL))

    def test_actual_head_ref_must_agree_before_any_credentialed_operation(self):
        state, read = live_state(), Read()
        store, name = stored(state)
        read.ref_sha = "c" * 40
        with self.assertRaisesRegex(Rejected, "Actual head ref"):
            guard(store, name, state, read, 100)

    def test_structural_acceptance_binds_manifest_without_command_plan_or_receipts(self):
        state, manifest = live_fixtures.AcceptanceTests().context()
        facts = acceptance(state, manifest)
        self.assertEqual(digest(state["report"]), facts["verification_sha256"])
        for key in ("validation", "validation_claim", "objective_validation"):
            self.assertNotIn(key, state["report"])
        for key, value in (("parent", "0" * 40), ("tree", "0" * 40),
                           ("bundle_sha256", "0" * 64), ("schema", 1)):
            with self.subTest(key=key), self.assertRaises(Rejected):
                acceptance(state, dict(manifest, **{key: value}))

    def test_no_ci_and_nonterminal_or_unknown_results_never_establish_success(self):
        read = Read()
        self.assertEqual([CI_CHECK], select_checks(read, read.req))
        self.assertEqual("none", exact_ci(read, FIXTURE, SHA, [])["decision"])
        for conclusion, expected in (("neutral", "unknown"), ("skipped", "unknown"),
                                     ("cancelled", "failed"), ("timed_out", "failed"),
                                     (None, "unknown")):
            read.checks[0]["conclusion"] = conclusion
            self.assertEqual(expected, exact_ci(read, FIXTURE, SHA, [CI_CHECK])["decision"])
        read.checks[0]["name"] = "Copilot Code Review"
        self.assertEqual([], select_checks(read, read.req))
        read.checks = []
        read.statuses = [{"id": 1, "context": "external CI", "state": "success"}]
        self.assertEqual("passed", exact_ci(read, FIXTURE, SHA, ["external CI"])["decision"])

    def test_missing_rest_roots_or_inconsistent_parent_identity_cannot_establish_clean(self):
        read = Read()
        read.reviews = [live_fixtures.review(id=13, body=live_fixtures.CLEAN, submitted_at=live_fixtures.iso(200))]
        read.comments[0]["pull_request_review_id"] = 13
        read.comments[0]["original_commit_id"] = "c" * 40
        with self.assertRaisesRegex(Rejected, "inline thread collection"):
            fresh_collection(read, read.req, [12], 100, SHA, 400)
        read.comments = []
        with self.assertRaisesRegex(Rejected, "inline thread collection"):
            fresh_collection(read, read.req, [12], 100, SHA, 400)


    def test_public_shallow_snapshot_transports_only_the_frozen_commit(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            original, shallow, restored = root / "original", root / "shallow", root / "restored"
            for directory in (original, shallow, restored):
                directory.mkdir()
                git(["init", "--bare", "--quiet"], directory)
            from tests.test_loop import baseline
            parent = baseline(original)
            tree = git(["rev-parse", parent + "^{tree}"], original).decode().strip()
            commit = git(["hash-object", "-t", "commit", "-w", "--stdin"], original,
                         f"tree {tree}\nparent {parent}\nauthor T <t@invalid> 0 +0000\ncommitter T <t@invalid> 0 +0000\n\nsnapshot\n".encode()).decode().strip()
            git(["update-ref", "refs/heads/main", commit], original)
            req = dict(request(), frozen_sha=commit, loop_kind="self_review", findings=[],
                       base_ref="main", base_sha=commit, merge_base_sha=commit)
            live = pr()
            live["head"]["sha"] = commit
            req.update(eligible(live))
            reader = Mock(token="not-used")
            reader.call.side_effect = lambda path: (
                {"object": {"sha": commit}} if "/git/ref/" in path else live)
            def fetch(directory):
                git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--depth=1",
                     str(original), commit], directory)
                git(["update-ref", "refs/heads/review-base", commit], directory)
            with patch("loop.source.time.time", return_value=100), patch.dict(
                    os.environ, {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1"}):
                manifest = package_source(req, 1, reader, root / "package", fetch)
            self.assertTrue(manifest["shallow"])
            import_source(restored, root / "package" / "source.bundle", manifest, req)
            self.assertEqual(b"1\n", git(["rev-list", "--count", "snapshot"], restored))
