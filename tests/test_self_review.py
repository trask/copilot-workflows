from tests.support import launch, reconstruct
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from loop.cli import main as cli_main
from loop.coordinator import (cancel, checkpoint, dispatch, quiescent, record_result)
from loop.freeze import freeze
from loop.live import advance, publish, start
from loop.policy import (AUTHOR_ID, CENTRAL, Rejected, candidate_outcome,
                         canonical, digest, iso, loop_kind, pipeline_budget, worker_result)
from loop.publication import PublisherAPI, acceptance, evidence
from loop.source import bind_manifest, import_source, package_source, public_fetch
from loop.state import State
from loop.verify import git, verify
from loop.waiter import poll
from loop.revisions import revision_ref
from tests import test_live as live_fixtures
from tests.test_live import (CI_CHECK, FIXTURE, Publisher, Read, TEST_TOKEN,
                            personal_request, stored, zipped)
from tests.test_loop import (FakeAPI, GOOD_PATCH, MemoryState, REVISION, SHA, baseline, run)
from tests.test_waiter import Controls, Reads, controller, phases

BASE = "9" * 40
MERGE_BASE = "8" * 40
EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def self_request(publish_mode=True):
    req = personal_request(max_pipelines=5)
    req.update(loop_kind="self_review", base_ref="main", base_sha=BASE,
               merge_base_sha=MERGE_BASE, findings=[], baseline_review_ids=[])
    if not publish_mode:
        req.pop("publication")
        req["mode"] = "shadow"
    return req


def semantic(req, outcome="clean"):
    from tests.support import semantic as current_semantic
    return current_semantic(req, outcome, GOOD_PATCH if outcome == "fixes" else b"")


class SelfRead(Read):
    def __init__(self, req=None):
        super().__init__(req or self_request())
        self.pr["base"].update(ref=self.req["base_ref"], sha=self.req["base_sha"])
        self.base_tip = self.req["base_sha"]
        self.paths = []
        for side, name, identity, private in (
                ("head", "head_repo", "head_repo_id", "source_private"),
                ("base", "repo", "repo_id", "target_private")):
            self.pr[side]["repo"].update(full_name=self.req[name], id=self.req[identity],
                                          private=self.req[private])

    def call(self, path, *args):
        self.paths.append(path)
        if path == f"repos/{self.req['repo']}/git/ref/heads/{self.req['base_ref']}":
            return {"object": {"sha": self.base_tip}}
        if "/compare/" in path:
            return {"status": "ahead", "base_commit": {"sha": self.base_tip},
                    "merge_base_commit": {"sha": self.req["merge_base_sha"]}}
        if path.startswith("users/"):
            raise AssertionError("Self-review must not look up Copilot identity")
        return super().call(path, *args)

    def pages(self, path, key=None):
        if path.endswith(("/reviews", "/comments")):
            raise AssertionError("Self-review must not collect external reviews")
        return super().pages(path, key)

    def graphql(self, *_args):
        raise AssertionError("Self-review must not collect external threads")


def accepted_state(changed=False, pinned=False):
    state, manifest = live_fixtures.AcceptanceTests().context(changed)
    state["request"] = self_request()
    req = state["request"]
    if pinned:
        req["workflow_ref"] = revision_ref(req["workflow_revision"])
    state["report"]["request_digest"] = digest(req)
    state["report"]["dispositions"] = semantic(req, "fixes" if changed else "clean")
    candidate = state["report"]["candidate"]
    candidate["finding_commits"] = {}
    manifest["finding_commits"] = {}
    candidate["source_bundle_sha256"] = "3" * 64
    manifest["source_bundle_sha256"] = candidate["source_bundle_sha256"]
    state["source"] = {"manifest": {"bundle_sha256": candidate["source_bundle_sha256"]}}
    if not changed:
        candidate["patch_sha256"] = EMPTY_HASH
    manifest["request_digest"] = digest(req)
    manifest["patch_sha256"] = candidate["patch_sha256"]
    return state, manifest


def published_state(changed=False):
    state, manifest = accepted_state(changed)
    accepted = acceptance(state, manifest)
    candidate = state["report"]["candidate"]
    state.update(stage="published", expected_sha=candidate["commit"] if changed else SHA,
                 publication_intent={"status": "confirmed", "candidate": candidate,
                                     "acceptance": accepted},
                 publications=[{"sha": candidate["commit"] if changed else SHA,
                                "candidate": candidate, "acceptance": accepted}])
    read = SelfRead(state["request"])
    read.pr["head"]["sha"] = state["expected_sha"]
    read.runs[0]["head_sha"] = read.checks[0]["head_sha"] = state["expected_sha"]
    return state, read


def review_objects(directory):
    old = baseline(directory)
    head_tree = git(["rev-parse", old + "^{tree}"], directory).decode().strip()
    blob = git(["hash-object", "-w", "--stdin"], directory, b"prior\n").decode().strip()
    tree = git(["mktree"], directory, (
        f"100644 blob {blob}\tFoo.java\n100644 blob {blob}\tRemoved.java\n").encode()).decode().strip()
    def commit(tree_id, parent=""):
        return git(["hash-object", "-t", "commit", "-w", "--stdin"], directory,
                   (f"tree {tree_id}\n{parent}author T <t@invalid> 0 +0000\n"
                    "committer T <t@invalid> 0 +0000\n\nsnapshot\n").encode()).decode().strip()
    base = commit(tree)
    head = commit(head_tree, f"parent {base}\n")
    git(["update-ref", "refs/heads/snapshot", head], directory)
    git(["update-ref", "refs/heads/review-base", base], directory)
    Path(directory, "shallow").write_text(head + "\n" + base + "\n", encoding="ascii")
    Path(directory, "FETCH_HEAD").write_text(head + "\n", encoding="ascii")
    return head, base


class SelfReviewTests(unittest.TestCase):
    def test_self_admission_needs_no_copilot_review_or_identity(self):
        read = SelfRead()
        req = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="self_review")
        self.assertEqual("self_review", req["loop_kind"])
        self.assertEqual([], req["findings"])
        self.assertEqual((BASE, MERGE_BASE, "main"),
                         tuple(req[key] for key in ("base_sha", "merge_base_sha", "base_ref")))
        store = MemoryState()
        name, state = start(store, FakeAPI(), req, "", 0, True,
                            "fine_grained_pat", [CI_CHECK], True, 100)
        self.assertEqual("source_pending", state["stage"])
        state["source"] = {"durably": "bound"}
        state["stage"] = "ready"
        store.entries[name] = state
        api = FakeAPI()
        result = dispatch(store, name, api, 110)
        self.assertEqual("dispatched", result["stage"])
        self.assertEqual(1, result["iteration"])
        self.assertEqual(1, sum(method == "POST" for _, method, _ in api.calls))

    def test_stale_pr_base_metadata_freezes_live_tip_and_merge_base(self):
        read = SelfRead()
        read.pr["base"]["sha"] = "7" * 40
        req = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="self_review")
        self.assertEqual(BASE, req["base_sha"])
        self.assertEqual(MERGE_BASE, req["merge_base_sha"])
        self.assertIn(f"repos/{FIXTURE}/compare/{BASE}...{SHA}", read.paths)
        self.assertNotIn(f"repos/{FIXTURE}/compare/{'7' * 40}...{SHA}", read.paths)

    def test_missing_or_invalid_live_base_tip_rejects_freeze(self):
        for tip in ("", None, 123, "x" * 40):
            read = SelfRead()
            read.base_tip = tip
            with self.subTest(tip=tip), self.assertRaises(Rejected):
                freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="self_review")

    def test_missing_merge_base_or_wrong_comparison_rejects_freeze(self):
        for mutation in ("missing", "wrong_base"):
            read = SelfRead()
            call = read.call
            def altered(path):
                value = call(path)
                if "/compare/" in path:
                    if mutation == "missing":
                        value["merge_base_commit"]["sha"] = ""
                    else:
                        value["base_commit"]["sha"] = "f" * 40
                return value
            read.call = altered
            with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="self_review")

    def test_upstream_commits_during_freeze_keep_the_bound_base_and_merge_base(self):
        read = SelfRead()
        call = read.call
        def advancing(path):
            value = call(path)
            if "/compare/" in path:
                read.base_tip = "f" * 40
            return value
        read.call = advancing
        req = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind="self_review")
        self.assertEqual(BASE, req["base_sha"])
        self.assertEqual(MERGE_BASE, req["merge_base_sha"])
        self.assertEqual(SHA, req["frozen_sha"])

    def test_cli_routes_explicit_kind_and_uses_existing_worker_and_source(self):
        env = {"GITHUB_REPOSITORY": CENTRAL, "GITHUB_REF": "refs/heads/main",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR_ID": str(AUTHOR_ID),
               "GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": REVISION,
               "PUBLICATION_AUTH_MODE": "fine_grained_pat", "PUBLISHER_AVAILABLE": "true",
               "PUBLISHER_HEAD_REPO": FIXTURE,
               "PUBLISHER_SECRET_NAME": "TEST_PUBLISH_TOKEN",
               "PUBLISHER_SECRET_MAP": json.dumps({
                   FIXTURE.split("/")[0]: "TEST_PUBLISH_TOKEN"}),
               "INFERENCE_AVAILABLE": "true", "LOOP_KIND": "self_review"}
        store, read = MemoryState(), SelfRead()
        with patch.dict(os.environ, env, clear=True), \
                patch("sys.argv", ["loop.cli", "launch", "--target", FIXTURE + "#1"]), \
                patch("loop.cli.API", return_value=FakeAPI()), \
                patch("loop.cli.State", return_value=store), \
                patch("loop.cli.target_api", return_value=read), \
                patch("loop.cli.stage_source") as source, patch("loop.cli.summary"), \
                patch("loop.cli.time.time", return_value=100):
            cli_main()
        source.assert_called_once()
        state = next(iter(store.entries.values()))
        self.assertEqual("self_review", loop_kind(state["request"]))
        self.assertEqual("source_pending", state["stage"])
        self.assertEqual(88, state["request"]["launch_run"]["id"])

    def test_publication_time_is_refreshed_after_slow_freeze(self):
        env = {"GITHUB_REPOSITORY": CENTRAL, "GITHUB_REF": "refs/heads/main",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR_ID": str(AUTHOR_ID),
               "GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": REVISION,
               "PUBLICATION_AUTH_MODE": "fine_grained_pat", "PUBLISHER_AVAILABLE": "true",
               "PUBLISHER_HEAD_REPO": FIXTURE,
               "PUBLISHER_SECRET_NAME": "TEST_PUBLISH_TOKEN",
               "PUBLISHER_SECRET_MAP": json.dumps({
                   FIXTURE.split("/")[0]: "TEST_PUBLISH_TOKEN"}),
               "INFERENCE_AVAILABLE": "true", "LOOP_KIND": "self_review"}
        store, read = MemoryState(), SelfRead()
        with patch.dict(os.environ, env, clear=True), \
                patch("sys.argv", ["loop.cli", "launch", "--target", FIXTURE + "#1"]), \
                patch("loop.cli.API", return_value=FakeAPI()), \
                patch("loop.cli.State", return_value=store), \
                patch("loop.cli.target_api", return_value=read), \
                patch("loop.cli.freeze", side_effect=lambda *args, **kwargs: freeze(
                    *args, now=120, **kwargs)), \
                patch("loop.cli.stage_source") as source, patch("loop.cli.summary"), \
                patch("loop.cli.time.time", side_effect=[100, 120, 120]):
            cli_main()
        source.assert_called_once()
        state = next(iter(store.entries.values()))
        self.assertEqual(120, state["request"]["frozen_at"])
        self.assertEqual(120, state["request"]["publication"]["authorized_at"])
        self.assertEqual(7320, state["request"]["deadline"])
        self.assertEqual("source_pending", state["stage"])

    def test_worker_semantics_are_exact_digest_bound_and_separate(self):
        req = self_request()
        for outcome in ("fixes", "clean", "blocked"):
            worker_result(semantic(req, outcome), req)
        for value in (dict(semantic(req), findings=[]), dict(semantic(req), outcome="no_change"),
                      dict(semantic(req), outcome="incomplete"), dict(semantic(req), schema=True),
                      dict(semantic(req), request_digest="0" * 64), {}):
            with self.subTest(value=value), self.assertRaises(Rejected):
                worker_result(value, req)
        with self.assertRaises(Rejected):
            worker_result(semantic(req), personal_request())

    def test_explicit_clean_requires_no_patch_and_fixes_require_changes(self):
        req = self_request()
        for changed, outcome, patch_hash in ((True, "clean", "1" * 64),
                                            (False, "fixes", EMPTY_HASH),
                                            (False, "clean", "1" * 64),
                                            (True, "blocked", "1" * 64)):
            with self.subTest(outcome=outcome), self.assertRaises(Rejected):
                candidate_outcome(semantic(req, outcome), req,
                                  {"changed": changed, "patch_sha256": patch_hash})

    def test_changed_publication_uses_exact_candidate_then_new_full_pr_pass(self):
        state, manifest = accepted_state(True, pinned=True)
        accepted = acceptance(state, manifest)
        store, name = stored(state)
        read = SelfRead(state["request"])
        publisher = Publisher(read)
        candidate = state["report"]["candidate"]
        def pushed(_directory, req, sha, token):
            self.assertEqual((state["request"], candidate["commit"], TEST_TOKEN), (req, sha, token))
            read.pr["head"]["sha"] = sha
        with patch("loop.live.evidence", return_value=(accepted, candidate)), \
                patch("loop.live.authenticated_push", side_effect=pushed) as push, \
                patch("loop.live.time.time", return_value=100):
            result = publish(store, name, state, Mock(), read, publisher, 100)
            result = advance(store, name, result, Mock(), read, publisher, 100)
        push.assert_called_once()
        self.assertEqual([], publisher.posts)
        self.assertEqual("source_pending", result["stage"])
        self.assertEqual(candidate["commit"], result["request"]["frozen_sha"])
        self.assertEqual(MERGE_BASE, result["request"]["merge_base_sha"])
        self.assertEqual(1, result["iteration"])
        self.assertEqual(state["request"]["deadline"], result["request"]["deadline"])
        self.assertEqual(state["request"]["workflow_ref"], result["request"]["workflow_ref"])
        self.assertEqual(state["request"]["publication"]["phase"], result["phase"])
        self.assertEqual(state["request"]["publication"]["phase"],
                         result["request"]["publication"]["phase"])
        self.assertNotEqual(state["request"]["request_id"], result["request"]["request_id"])
        self.assertIsNone(result["run"])
        self.assertIsNone(result["report"])
        self.assertEqual(1, len(result["publications"]))
        self.assertNotIn("source", result)

    def test_clean_pass_is_independently_accepted_without_push_or_external_review(self):
        state, manifest = accepted_state()
        accepted = acceptance(state, manifest)
        store, name = stored(state)
        read, publisher = SelfRead(state["request"]), Publisher(SelfRead(state["request"]))
        with patch("loop.live.evidence", return_value=(accepted, state["report"]["candidate"])), \
                patch("loop.live.authenticated_push") as push, \
                patch("loop.live.time.time", return_value=100):
            published = publish(store, name, state, Mock(), read, publisher, 100)
            clean = advance(store, name, published, Mock(), read, publisher, 100)
        push.assert_not_called()
        self.assertEqual("clean", clean["stage"])
        self.assertEqual(SHA, clean["expected_sha"])
        self.assertEqual([], publisher.posts)

    def test_clean_waits_for_exact_ci_and_missing_failed_unknown_ci_stop(self):
        for decision in ("pending", "failed", "missing", "none", "unknown"):
            state, read = published_state()
            store, name = stored(state)
            with self.subTest(decision=decision), patch("loop.live.exact_ci", return_value={"decision": decision}):
                result = advance(store, name, state, Mock(), read, Publisher(read), 100)
                self.assertEqual("waiting_ci" if decision == "pending" else "blocked", result["stage"])
                if decision == "pending":
                    with patch("loop.live.exact_ci", return_value={"decision": "passed"}):
                        result = advance(store, name, result, Mock(), read, Publisher(read), 400)
                    self.assertEqual("clean", result["stage"])

    def test_fifth_fixes_remain_published_but_cannot_declare_clean(self):
        state, read = published_state(True)
        state["iteration"] = 5
        store, name = stored(state)
        with patch("loop.live.freeze") as next_pass:
            result = advance(store, name, state, Mock(), read, Publisher(read), 100)
        self.assertEqual("exhausted", result["stage"])
        self.assertEqual("self_review_requires_later_clean_pass", result["reason"])
        self.assertEqual(state["publications"], result["publications"])
        self.assertEqual(5, result["iteration"])
        next_pass.assert_not_called()

    def test_fifth_clean_can_complete_without_a_sixth_pipeline(self):
        state, read = published_state()
        state["iteration"] = 5
        store, name = stored(state)
        result = advance(store, name, state, Mock(), read, Publisher(read), 100)
        self.assertEqual("clean", result["stage"])
        self.assertEqual(5, result["iteration"])
        api = FakeAPI()
        self.assertEqual(result, dispatch(store, name, api, 200))
        self.assertEqual([], api.calls)

    def test_base_metadata_drift_does_not_prevent_clearance(self):
        state, read = published_state()
        store, name = stored(state)
        read.pr["base"]["sha"] = "7" * 40
        result = advance(store, name, state, Mock(), read, Publisher(read), 100)
        self.assertEqual("clean", result["stage"])
        self.assertEqual(BASE, result["request"]["base_sha"])

    def test_stale_base_metadata_allows_next_full_pr_pass(self):
        state, read = published_state(True)
        store, name = stored(state)
        read.pr["base"]["sha"] = "7" * 40
        result = advance(store, name, state, Mock(), read, Publisher(read), 100)
        self.assertEqual("source_pending", result["stage"])
        self.assertEqual(BASE, result["request"]["base_sha"])
        self.assertEqual(MERGE_BASE, result["request"]["merge_base_sha"])

    def test_head_and_base_branch_drift_prevent_clearance(self):
        for mutation in ("head", "base_ref"):
            state, read = published_state()
            store, name = stored(state)
            if mutation == "head":
                read.pr["head"]["sha"] = "f" * 40
            else:
                read.pr["base"]["ref"] = "release"
            with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                advance(store, name, state, Mock(), read, Publisher(read), 100)
            self.assertEqual(state, store.entries[name])

    def test_base_tip_advancing_after_ci_collection_keeps_exact_head_clearance(self):
        state, read = published_state()
        store, name = stored(state)
        def ci(*_args):
            read.base_tip = "f" * 40
            return {"decision": "passed"}
        with patch("loop.live.exact_ci", side_effect=ci):
            result = advance(store, name, state, Mock(), read, Publisher(read), 100)
        self.assertEqual("clean", result["stage"])
        self.assertEqual(state["expected_sha"], result["expected_sha"])
        self.assertEqual(state["request"], result["request"])

    def test_next_full_pr_pass_freezes_the_new_base_without_resetting_its_budget(self):
        state, read = published_state(True)
        store, name = stored(state)
        read.base_tip = "f" * 40
        result = advance(store, name, state, Mock(), read, Publisher(read), 100)
        self.assertEqual("source_pending", result["stage"])
        self.assertEqual(read.base_tip, result["request"]["base_sha"])
        self.assertEqual(state["expected_sha"], result["request"]["frozen_sha"])
        self.assertEqual(state["request"]["deadline"], result["request"]["deadline"])
        self.assertEqual(state["request"]["budgets"], result["request"]["budgets"])
        self.assertEqual(state["iteration"], result["iteration"])
        self.assertEqual(state["phase"], result["phase"])

    def test_base_tip_advancing_does_not_change_the_accepted_candidate(self):
        state, manifest = accepted_state(True)
        accepted = acceptance(state, manifest)
        read = SelfRead(state["request"])
        read.base_tip = "f" * 40
        store, name = stored(state)
        candidate = state["report"]["candidate"]
        def pushed(_directory, req, sha, token):
            self.assertEqual(state["request"], req)
            self.assertEqual(candidate["commit"], sha)
            read.pr["head"]["sha"] = sha
        with patch("loop.live.evidence", return_value=(accepted, state["report"]["candidate"])), \
                patch("loop.live.authenticated_push", side_effect=pushed) as push, \
                patch("loop.live.time.time", return_value=100):
            result = publish(store, name, state, Mock(), read, Publisher(read), 100)
        push.assert_called_once()
        self.assertEqual("published", result["stage"])
        self.assertEqual(candidate["commit"], result["expected_sha"])
        self.assertEqual(state["request"], result["request"])

    def test_self_publisher_cannot_request_copilot_review(self):
        publisher = PublisherAPI(TEST_TOKEN, self_request(), "fine_grained_pat")
        with self.assertRaises(Rejected):
            publisher.authorize(f"repos/{FIXTURE}/pulls/1/requested_reviewers", "POST",
                                {"reviewers": ["copilot-pull-request-reviewer[bot]"]})

    def test_clean_acceptance_rejects_legacy_test_claims(self):
        state, manifest = accepted_state()
        acceptance(state, manifest)
        for key in ("validation", "validation_claim", "objective_validation"):
            bad = copy.deepcopy(state)
            bad["report"][key] = {"status": "passed", "commands": []}
            with self.subTest(key=key), self.assertRaises(Rejected):
                acceptance(bad, manifest)

    def test_source_bundle_identity_is_required_without_native_test_receipts(self):
        state, manifest = accepted_state()
        state["source"]["manifest"]["bundle_sha256"] = "4" * 64
        with self.assertRaisesRegex(Rejected, "source bundle"):
            acceptance(state, manifest)

    def test_cancelled_and_stale_results_do_not_advance(self):
        for cancelled in (True, False):
            state, *_ = accepted_state()
            state["stage"] = "verify_pending"
            store, name = stored(state)
            if cancelled:
                cancel(store, name, state["request"]["request_id"], 6, 100)
            else:
                store.entries[name]["generation"] += 1
            with self.subTest(cancelled=cancelled), self.assertRaises(Rejected):
                record_result(store, name, state, state["report"], [], 100)
            self.assertNotEqual("publish_pending", store.entries[name]["stage"])

    def test_rejected_structural_result_retains_consumed_budget(self):
        state, _ = accepted_state()
        state["stage"] = "verify_pending"
        state["iteration"] = 4
        state["report"]["verification"] = "failed"
        store, name = stored(state)
        with self.assertRaises(Rejected):
            record_result(store, name, state, state["report"], [], 100)
        self.assertEqual("verify_pending", store.entries[name]["stage"])
        self.assertEqual(4, store.entries[name]["iteration"])


class ReviewSourceTests(unittest.TestCase):
    def package(self, root, *, private=False, fork=False, publish_mode=False):
        req = self_request(publish_mode)
        with tempfile.TemporaryDirectory() as directory:
            git(["init", "--bare", "--quiet"], directory)
            req["frozen_sha"], req["merge_base_sha"] = review_objects(directory)
        req.update(source_private=private, target_private=private)
        if fork:
            req.update(head_repo="owner/fork", head_repo_id=77)
        read = SelfRead(req)
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1"}), \
                patch("loop.source.time.time", return_value=100):
            manifest = package_source(req, 6, read, Path(root, "source"), review_objects)
        return req, manifest, Path(root, "source")

    def test_complete_two_tree_source_roundtrip_in_public_repositories_and_forks(self):
        for private, fork in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(private=private, fork=fork), tempfile.TemporaryDirectory() as root, \
                    tempfile.TemporaryDirectory() as restored:
                if private:
                    with self.assertRaisesRegex(Rejected, "Only public"):
                        self.package(root, private=private, fork=fork)
                    self.assertFalse(Path(root, "source").exists())
                    continue
                req, manifest, source = self.package(root, private=private, fork=fork)
                git(["init", "--bare", "--quiet"], restored)
                import_source(restored, source / "source.bundle", manifest, req)
                self.assertEqual(2, manifest["history_count"])
                self.assertEqual(req["frozen_sha"], git(["rev-parse", "FETCH_HEAD"], restored).decode().strip())
                diff = git(["diff", "--name-only", req["merge_base_sha"], req["frozen_sha"]], restored)
                self.assertEqual(b"Foo.java\nRemoved.java\n", diff)
                self.assertEqual(req["base_sha"], manifest["review_scope"]["base_sha"])

    def test_public_source_never_uses_an_available_repository_or_inference_token(self):
        with patch("loop.source.subprocess.run", return_value=Mock(returncode=0)) as child, \
                patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "inference", "GH_TOKEN": "central"}):
            public_fetch(".", self_request(False))
        env = child.call_args.kwargs["env"]
        self.assertEqual("0", env["GIT_CONFIG_COUNT"])
        self.assertNotIn("GIT_CONFIG_VALUE_0", env)
        self.assertFalse(any("TOKEN" in key for key in env))
        self.assertNotIn("must-not-be-used", str(child.call_args.args))

    def test_source_binding_rejects_private_requests_wrong_fork_base_or_incomplete_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            req, manifest, source = self.package(root, fork=True)
            for key, value in (("head_repo_id", 78), ("merge_base_sha", SHA), ("base_sha", SHA),
                               ("source_private", True), ("head_repo", "wrong/fork")):
                with self.subTest(key=key), self.assertRaises(Rejected):
                    bind_manifest(manifest, dict(req, **{key: value}), 6)
            incomplete = copy.deepcopy(manifest)
            incomplete.pop("review_scope")
            with self.assertRaises(Rejected):
                bind_manifest(incomplete, req, 6)
            with tempfile.TemporaryDirectory() as restored:
                git(["init", "--bare", "--quiet"], restored)
                manifest["review_scope"]["tree"] = SHA
                with self.assertRaises(Rejected):
                    import_source(restored, source / "source.bundle", manifest, req)

    def test_candidate_semantics_and_real_git_reconstruction_for_fixes_and_clean(self):
        with tempfile.TemporaryDirectory() as root:
            req, manifest, source = self.package(root)
            def fetch(directory):
                import_source(directory, source / "source.bundle", manifest, req)
            for outcome, patch_data in (("fixes", GOOD_PATCH), ("clean", b"")):
                payload = zipped({"result.json": json.dumps(semantic(req, outcome)),
                                  "candidate.patch": patch_data,
                                  "diagnostics.txt": "Reviewed complete PR"})
                report = verify(payload, req, run(), {"id": 33, "digest": "server"}, fetch)
                self.assertEqual(outcome == "fixes", report["candidate"]["changed"])
                self.assertEqual("verified", report["verification"])
                self.assertFalse(report["publication_eligible"])
            for outcome, patch_data in (
                    ("clean", GOOD_PATCH), ("fixes", b""), ("blocked", GOOD_PATCH)):
                payload = zipped({"result.json": json.dumps(semantic(req, outcome)),
                                  "candidate.patch": patch_data,
                                  "diagnostics.txt": "Incomplete"})
                with self.subTest(outcome=outcome), self.assertRaises(Rejected):
                    verify(payload, req, run(), {"id": 33, "digest": "server"}, fetch)
            with self.assertRaises(Rejected):
                verify(zipped({"candidate.patch": b"",
                               "diagnostics.txt": "No explicit result"}),
                       req, run(), {"id": 33, "digest": "server"}, fetch)

    def test_incomplete_source_never_substitutes_a_public_checkout(self):
        with self.assertRaises(Rejected):
            reconstruct({"candidate.patch": b""}, self_request(False))
        with tempfile.TemporaryDirectory() as directory:
            git(["init", "--bare", "--quiet"], directory)
            head = baseline(directory)
        with self.assertRaises(Rejected):
            reconstruct({"candidate.patch": b""}, dict(self_request(False), frozen_sha=head), baseline)

    def test_identical_head_and_merge_base_transport_has_one_commit_and_both_refs(self):
        req = self_request(False)
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as bare, \
                tempfile.TemporaryDirectory() as restored:
            git(["init", "--bare", "--quiet"], bare)
            req["frozen_sha"] = req["merge_base_sha"] = baseline(bare)
            def same_snapshot(directory):
                head = baseline(directory)
                git(["update-ref", "refs/heads/review-base", head], directory)
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1"}), \
                    patch("loop.source.time.time", return_value=100):
                source = Path(root, "source")
                manifest = package_source(req, 6, SelfRead(req), source, same_snapshot)
            git(["init", "--bare", "--quiet"], restored)
            import_source(restored, source / "source.bundle", manifest, req)
            self.assertEqual(1, manifest["history_count"])
            self.assertEqual(b"", git(["diff", req["merge_base_sha"], req["frozen_sha"]], restored))

    def test_publisher_reconstructs_exact_candidate_from_server_bound_full_review_source(self):
        for changed in (True, False):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as root:
                req, source_manifest, source = self.package(root, publish_mode=True)
                root = Path(root)
                package = root / "candidate-package"
                restored = root / "restored"
                restored.mkdir()
                payload = zipped({"result.json": canonical(semantic(req, "fixes" if changed else "clean")),
                                  "candidate.patch": GOOD_PATCH if changed else b"",
                                  "diagnostics.txt": b"Reviewed"})
                worker = run()
                worker_artifact = {"id": 33, "name": "candidate-24-1", "size_in_bytes": len(payload),
                                   "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                                   "expired": False, "workflow_run": {"id": 24, "head_sha": REVISION}}
                def fetch(directory):
                    import_source(directory, source / "source.bundle", source_manifest, req)
                result = verify(payload, req, worker, worker_artifact, fetch, package)
                candidate = result["candidate"]
                candidate["source_bundle_sha256"] = source_manifest["bundle_sha256"]
                manifest = json.loads((package / "manifest.json").read_bytes())
                manifest["source_bundle_sha256"] = source_manifest["bundle_sha256"]
                state = live_fixtures.live_state(req)
                state["source"] = {"manifest": source_manifest}
                state["report"] = copy.deepcopy(result)
                state["report"]["verification_run"] = state["verification_run"]
                report = {"schema": 2, "request_id": req["request_id"],
                          "request_digest": digest(req), "generation": 6, "verification": "verified",
                          "run_id": 24, "run_attempt": 1, "result": result, "request": req,
                          "source": state["source"]}
                verification = zipped({"verification-report.json": canonical(report),
                                       "candidate-package/manifest.json": canonical(manifest),
                                       "candidate-package/source.bundle": (source / "source.bundle").read_bytes(),
                                       "candidate-package/candidate.bundle": (package / "candidate.bundle").read_bytes()})
                artifacts = [{
                    "id": identity, "name": name, "size_in_bytes": len(data), "expired": False,
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "workflow_run": {"id": 99, "head_sha": REVISION},
                    "created_at": iso(100), "expires_at": iso(100 + 14 * 86400),
                } for identity, name, data in [(34, "verification-99-1", verification)]]
                state["artifacts"] = artifacts
                pipeline = dict(worker, id=99, path=".github/workflows/coordinator.yml", created_at=iso(90))
                class Artifacts:
                    def call(self, path):
                        return pipeline if path.endswith("/99") else worker
                    def pages(self, path, key):
                        if key == "jobs":
                            names = ("verify", "finalize") if "/99/" in path else ("agent",)
                            return [{"name": name, "conclusion": "success"} for name in names]
                        return artifacts if "/99/" in path else [worker_artifact]
                    def artifact_zip(self, identity, _limit):
                        return {33: payload, 34: verification}[identity]
                with patch("loop.publication.time.time", return_value=100):
                    accepted, derived = evidence(Artifacts(), state, restored)
                self.assertEqual(candidate, derived)
                self.assertEqual(digest(req), accepted["request_digest"])
                self.assertEqual(candidate["commit"],
                                 git(["rev-parse", "candidate"], restored).decode().strip())
                self.assertEqual(req["merge_base_sha"],
                                 git(["rev-parse", "review-base"], restored).decode().strip())


class ManualPhaseTests(unittest.TestCase):
    def test_terminal_runs_allow_either_kind_same_head_with_fresh_budgets(self):
        for old_kind in ("copilot_review", "self_review"):
            for new_kind in ("copilot_review", "self_review"):
                for stage in ("clean", "exhausted", "cancelled", "blocked", "failed"):
                    req = self_request() if old_kind == "self_review" else personal_request(max_pipelines=5)
                    old = checkpoint(req)
                    old.update(stage=stage, iteration=5, effects=[], publications=[{"sha": SHA}])
                    store, name = stored(old)
                    fresh = self_request(False) if new_kind == "self_review" else personal_request(max_pipelines=5)
                    fresh = dict(fresh, mode="shadow", request_id="e" * 32, frozen_at=8000, deadline=15200)
                    fresh.pop("publication", None)
                    before = copy.deepcopy(old)
                    with self.subTest(old=old_kind, new=new_kind, stage=stage):
                        _, result = start(store, FakeAPI(), fresh, "", 0, True,
                                          "fine_grained_pat", [CI_CHECK], True, 8000)
                        self.assertEqual(0, result["iteration"])
                        self.assertEqual(15200, result["request"]["deadline"])
                        self.assertEqual(5, pipeline_budget(result))
                        self.assertNotEqual(old["phase"], result["phase"])
                        self.assertEqual(new_kind, loop_kind(result["request"]))
                        self.assertEqual([], result["publications"])
                        self.assertIsNone(result["report"])
                        self.assertEqual(before, old)

    def test_active_and_unknown_effects_never_restart_or_switch_kinds(self):
        for kind in ("copilot_review", "self_review"):
            for mutation in ("active", "dispatch", "push", "review", "source", "run"):
                state = checkpoint(self_request())
                state.update(stage="blocked", effects=[], publications=[])
                if mutation == "active":
                    state["stage"] = "ready"
                elif mutation == "dispatch":
                    state.update(iteration=1, intent={"dispatch_status": "uncertain"})
                elif mutation == "push":
                    state["publication_intent"] = {"status": "uncertain"}
                elif mutation == "review":
                    state["review_request"] = {"status": "acknowledged"}
                elif mutation == "source":
                    state["reason"] = "source_acquisition_uncertain_no_retry"
                else:
                    state.update(iteration=1, run={"id": 24})
                store, name = stored(state)
                fresh = self_request(False) if kind == "self_review" else personal_request(max_pipelines=5)
                fresh["request_id"] = "e" * 32
                with self.subTest(kind=kind, mutation=mutation), self.assertRaises(Rejected):
                    start(store, FakeAPI(), fresh, "", 0, True, "fine_grained_pat", [], True, 100)
                self.assertEqual(state, store.entries[name])


    def test_previous_worker_launch_and_coordinator_must_all_be_completed(self):
        state = checkpoint(self_request())
        state.update(stage="exhausted", iteration=5, intent={"id": "d" * 32},
                     run={"id": 24}, effects=[], publications=[],
                     coordinator_run={"id": 99, "attempt": 1, "revision": REVISION})
        state["request"]["launch_run"] = {"id": 88, "attempt": 1, "actor_id": AUTHOR_ID}
        api = Controls()
        worker = run()
        api.pages = Mock(return_value=[worker])
        api.controllers.update({99: controller(99, "completed"), 88: controller(88, "completed")})
        quiescent(api, state)
        for identity in (24, 88, 99):
            with self.subTest(identity=identity):
                selected = worker if identity == 24 else api.controllers[identity]
                selected["status"] = "in_progress"
                with self.assertRaises(Rejected):
                    quiescent(api, state)
                selected["status"] = "completed"
        api.pages.return_value = [worker, worker]
        with self.assertRaises(Rejected):
            quiescent(api, state)

    def test_new_phase_archives_exact_previous_evidence_in_the_same_cas(self):
        old = checkpoint(self_request())
        old.update(stage="exhausted", iteration=5, effects=[], publications=[], report={"retained": True})
        store, name = stored(old)
        _, new = start(store, FakeAPI(), dict(self_request(False), request_id="e" * 32),
                       "", 0, True, "fine_grained_pat", [], True, 100)
        api = Mock()
        api.call.return_value = {"sha": SHA}
        state_store = State(api)
        with patch.object(state_store, "snapshot", return_value=(SHA, REVISION, {name: old})):
            state_store.write(name, new, old)
        blobs = [json.loads(call.args[2]["content"]) for call in api.call.call_args_list
                 if call.args[0].endswith("/blobs")]
        self.assertEqual([new, old], blobs)
        ref = next(call.args[2] for call in api.call.call_args_list if "/refs/heads/" in call.args[0])
        self.assertFalse(ref["force"])

    def test_concurrent_fresh_launch_cannot_replace_a_newly_admitted_phase(self):
        old = checkpoint(self_request())
        old.update(stage="clean", effects=[], publications=[])
        store, name = stored(old)
        original = store.update
        def race(path, operation):
            store.entries[path] = dict(old, stage="ready")
            return original(path, operation)
        store.update = race
        with self.assertRaises(Rejected):
            start(store, FakeAPI(), dict(self_request(False), request_id="e" * 32),
                  "", 0, True, "fine_grained_pat", [], True, 100)
        self.assertEqual("ready", store.entries[name]["stage"])


class MixedReads(Reads):
    def call(self, path):
        if "/git/ref/heads/" in path:
            return self.selected.call(path)
        self.selected = self.reader(path)
        return super().call(path)


class MixedWaiterTests(unittest.TestCase):
    def test_ten_mixed_prs_share_waiter_and_only_ready_prs_wake(self):
        store, reads = phases(10)
        for number in (2, 4, 6, 8, 10):
            name = next(name for name, state in store.entries.items() if state["request"]["pr"] == number)
            state = store.entries[name]
            state["request"].update(loop_kind="self_review", base_ref="main", base_sha=BASE,
                                    merge_base_sha=MERGE_BASE, findings=[])
            state["stage"] = "waiting_ci"
            reads[number] = SelfRead(state["request"])
            reads[number].pr["number"] = number
            reads[number].pr["head"]["ref"] = state["request"]["head_ref"]
            reads[number].checks[0].update(status="in_progress", conclusion=None)
        store.entries[next(name for name, state in store.entries.items()
                           if state["request"]["pr"] == 3)]["stage"] = "verify_pending"
        reads[6].checks[0].update(status="completed", conclusion="success")
        api = Controls()
        with patch("loop.waiter.target_api", return_value=MixedReads(reads)):
            self.assertTrue(poll(api, store, 400, REVISION, {}))
        self.assertCountEqual([FIXTURE + "#3", FIXTURE + "#6"],
                              [body["inputs"]["target"] for _, method, body in api.calls if method == "POST"])
        later, _ = phases(11, "verify_pending")
        name = next(name for name, state in later.entries.items() if state["request"]["pr"] == 11)
        store.entries[name] = later.entries[name]
        with patch("loop.waiter.target_api", return_value=MixedReads(reads)):
            poll(api, store, 460, REVISION, {})
        self.assertEqual(FIXTURE + "#11", api.calls[-1][2]["inputs"]["target"])

    def test_self_base_branch_change_does_not_stop_external_or_other_self_work(self):
        store, reads = phases(3, "verify_pending")
        first = next(name for name, state in store.entries.items() if state["request"]["pr"] == 1)
        store.entries[first]["stage"] = "waiting_ci"
        req = store.entries[first]["request"]
        req.update(loop_kind="self_review", base_ref="main", base_sha=BASE, merge_base_sha=MERGE_BASE)
        reads[1] = SelfRead(req)
        reads[1].pr["head"]["ref"] = req["head_ref"]
        reads[1].pr["base"]["ref"] = "release"
        third = next(name for name, state in store.entries.items() if state["request"]["pr"] == 3)
        store.entries[third]["request"].update(loop_kind="self_review", base_ref="main",
                                             base_sha=BASE, merge_base_sha=MERGE_BASE)
        api = Controls()
        with patch("loop.waiter.target_api", return_value=MixedReads(reads)), \
                patch("loop.waiter.summary"), patch("sys.stderr"):
            poll(api, store, 400, REVISION, {})
        self.assertEqual("blocked", store.entries[first]["stage"])
        self.assertCountEqual([FIXTURE + "#2", FIXTURE + "#3"],
                              [body["inputs"]["target"] for _, method, body in api.calls if method == "POST"])
