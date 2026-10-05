import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from loop.candidates import message, patches, semantic
from loop.coordinator import cancel, quiescent
from loop.effects import pending, reply_body
from loop.live import advance, start
from loop.policy import AUTHOR_ID, Rejected, digest
from loop.publication import acceptance, import_candidate
from loop.verify import git, reconstruct
from tests.support import batch, semantic as result
from tests import test_live as fixtures
from tests.test_live import Publisher, Read, personal_request, stored
from tests.test_loop import GOOD_PATCH, SHA, baseline

SECOND = b"""diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -1 +1 @@
-new
+final
"""


class BatchTests(unittest.TestCase):
    def request(self):
        req = personal_request()
        with tempfile.TemporaryDirectory() as directory:
            git(["init", "--bare", "--quiet"], directory)
            req["frozen_sha"] = baseline(directory)
        return req

    def files(self, req):
        value = result(req, "fixes", GOOD_PATCH)
        keys = [finding["key"] for finding in req["findings"]]
        value["batches"] = [batch(GOOD_PATCH, [keys[0]]),
                            batch(SECOND, [keys[1]], len(GOOD_PATCH))]
        return {"candidate.patch": GOOD_PATCH + SECOND, "result.json": json.dumps(value).encode(),
                "diagnostics.txt": b"Investigated"}, value

    def test_sequential_same_file_chain_messages_mappings_and_independent_import(self):
        req = self.request()
        req["findings"][0]["body"] = "First original\n\nUnicode \u00e9\r\n"
        files, value = self.files(req)
        with tempfile.TemporaryDirectory() as package, tempfile.TemporaryDirectory() as restored:
            candidate = reconstruct(files, req, baseline, package)
            self.assertEqual(2, len(candidate["commits"]))
            first, second = candidate["commits"]
            self.assertEqual(req["frozen_sha"], first["parent"])
            self.assertEqual(first["commit"], second["parent"])
            self.assertEqual(second["commit"], candidate["commit"])
            self.assertEqual({"review:12": first["commit"], "inline:20": second["commit"]},
                             candidate["finding_commits"])
            self.assertEqual(["Foo.java"], candidate["changed_paths"])
            import_candidate(restored, Path(package, "candidate.bundle"), req, candidate, baseline)
            body = git(["cat-file", "commit", first["commit"]], restored)
            self.assertIn(req["findings"][0]["body"].encode(), body)
            self.assertIn(b"Address Copilot review comment: Validate input\n", body)
            self.assertEqual(b"final\n", git(["show", candidate["commit"] + ":Foo.java"], restored))
            self.assertEqual(candidate, reconstruct(files, req, baseline, package))
            forged = copy.deepcopy(candidate)
            forged["commits"][1]["parent"] = req["frozen_sha"]
            with self.assertRaises(Rejected):
                import_candidate(restored, Path(package, "candidate.bundle"), req, forged, baseline)

    def test_plural_template_preserves_every_original_comment_and_trailers(self):
        req = self.request()
        fields = batch(GOOD_PATCH, [finding["key"] for finding in req["findings"]])
        subject, body = message(fields, req)
        self.assertEqual("Address Copilot review comments: Validate input", subject)
        self.assertEqual(2, body.count(b"Copilot comment:"))
        for finding in req["findings"]:
            self.assertIn(finding["body"].encode(), body)
        self.assertIn(b"\nAnalysis: ", body)
        self.assertIn(b"\nUpsides: ", body)
        self.assertIn(b"\nDownsides: ", body)
        self.assertIn(b"Co-authored-by: Copilot App", body)

    def test_span_hash_order_and_complete_finding_accounting(self):
        req = self.request()
        files, valid = self.files(req)
        for change in ("gap", "overlap", "reorder", "hash", "duplicate", "foreign",
                       "unmapped", "overflow", "control", "summary"):
            value = copy.deepcopy(valid)
            if change in {"gap", "overlap"}:
                value["batches"][1]["offset"] += 1 if change == "gap" else -1
            elif change == "reorder":
                value["batches"].reverse()
            elif change == "hash":
                value["batches"][0]["sha256"] = "0" * 64
            elif change == "duplicate":
                value["batches"][1]["findings"] = ["review:12"]
            elif change == "foreign":
                value["findings"][0]["key"] = "inline:999"
            elif change == "unmapped":
                value["batches"].pop()
            elif change == "overflow":
                value["batches"][0]["analysis"] = "\u00e9" * 1001
            elif change == "control":
                value["batches"][0]["summary"] = "title\x00evil"
            else:
                value["batches"][0]["summary"] = "title\nforged header"
            with self.subTest(change=change), self.assertRaises(Rejected):
                semantic(value, req)
                patches(value, files["candidate.patch"])
        with self.assertRaises(Rejected):
            patches(valid, files["candidate.patch"] + b"unaccounted")

    def test_no_change_has_no_commit_and_no_bundle_history(self):
        req = self.request()
        files = {"candidate.patch": b"", "result.json": json.dumps(
            result(req, "no_change", disposition="not_warranted")).encode()}
        with tempfile.TemporaryDirectory() as package, tempfile.TemporaryDirectory() as restored:
            candidate = reconstruct(files, req, baseline, package)
            self.assertEqual(req["frozen_sha"], candidate["commit"])
            self.assertEqual([], candidate["commits"])
            self.assertEqual({}, candidate["finding_commits"])
            self.assertEqual(b"", Path(package, "candidate.bundle").read_bytes())
            import_candidate(restored, Path(package, "candidate.bundle"), req, candidate, baseline)

    def test_intermediate_protected_change_cannot_be_hidden_by_later_batch(self):
        req = self.request()
        req.update(loop_kind="self_review", findings=[], base_ref="main",
                   base_sha=req["frozen_sha"], merge_base_sha=req["frozen_sha"])
        protected = b"""diff --git a/.env b/.env
new file mode 100644
--- /dev/null
+++ b/.env
@@ -0,0 +1 @@
+secret
"""
        undo = b"""diff --git a/.env b/.env
deleted file mode 100644
--- a/.env
+++ /dev/null
@@ -1 +0,0 @@
-secret
"""
        value = result(req, "fixes", protected + GOOD_PATCH + undo)
        value["batches"] = [
            batch(protected), batch(GOOD_PATCH, offset=len(protected)),
            batch(undo, offset=len(protected) + len(GOOD_PATCH))]
        def source(directory):
            sha = baseline(directory)
            git(["update-ref", "refs/heads/snapshot", sha], directory)
            git(["update-ref", "refs/heads/review-base", sha], directory)
        with self.assertRaisesRegex(Rejected, "protected"):
            reconstruct({"candidate.patch": protected + GOOD_PATCH + undo,
                         "result.json": json.dumps(value).encode()},
                        req, source)

    def test_batches_cannot_cancel_all_changes(self):
        req = self.request()
        undo = GOOD_PATCH.replace(b"-old\n+new\n", b"-new\n+old\n")
        files, value = self.files(req)
        value["batches"][1] = batch(undo, ["inline:20"], len(GOOD_PATCH))
        files.update({"candidate.patch": GOOD_PATCH + undo, "result.json": json.dumps(value).encode()})
        with self.assertRaisesRegex(Rejected, "cancel"):
            reconstruct(files, req, baseline)

    def test_self_review_never_fabricates_comments_or_external_mappings(self):
        req = self.request()
        req["loop_kind"] = "self_review"
        req["findings"] = []
        fields = batch(GOOD_PATCH)
        subject, body = message(fields, req)
        self.assertEqual("Validate input", subject)
        self.assertNotIn(b"Copilot comment:", body)
        value = result(req, "fixes", GOOD_PATCH)
        semantic(value, req)
        value["findings"] = []
        with self.assertRaises(Rejected):
            semantic(value, req)

    def test_publisher_rejects_incomplete_or_foreign_finding_mapping(self):
        state, manifest = fixtures.AcceptanceTests().context()
        state["report"]["candidate"]["finding_commits"].pop("inline:20")
        with self.assertRaisesRegex(Rejected, "mapping"):
            acceptance(state, manifest)


class EffectTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": SHA})
        self.clock = patch("loop.live.time.time", return_value=100)
        self.environment.start()
        self.clock.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.clock.stop)

    def context(self, changed=False, multiple=False):
        state, manifest = fixtures.AcceptanceTests().context(changed)
        if multiple:
            candidate = state["report"]["candidate"]
            first = dict(candidate["commits"][0], commit="b" * 40, tree="d" * 40,
                         subject="Address Copilot review comment: Validate input")
            candidate["commits"][0].update(parent=first["commit"],
                                          subject=first["subject"],
                                          patch_sha256=hashlib.sha256(SECOND).hexdigest())
            candidate["commits"].insert(0, first)
            candidate["finding_commits"]["inline:20"] = first["commit"]
            state["report"]["dispositions"]["batches"] = [
                batch(GOOD_PATCH, ["inline:20"]),
                batch(SECOND, ["review:12"], len(GOOD_PATCH))]
            manifest.update(candidate)
        accepted = acceptance(state, manifest)
        candidate = state["report"]["candidate"]
        state.update(stage="published", expected_sha=candidate["commit"],
                     publication_intent={"status": "confirmed", "candidate": candidate,
                                         "acceptance": accepted},
                     publications=[{"effect": "push" if changed else "no_change",
                                    "sha": candidate["commit"]}])
        read = Read(state["request"])
        read.pr["head"]["sha"] = state["expected_sha"]
        publisher = Publisher(read)
        store, name = stored(state)
        return store, name, state, read, publisher

    def step(self, store, name, state, read, publisher):
        return advance(store, name, state, None, read, publisher, 100)

    def test_publish_reply_resolve_review_order_with_no_code_explanation(self):
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("thread_effects", state["stage"])
        self.assertEqual([], publisher.posts)
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("confirmed", state["effects"][0]["reply"]["status"])
        self.assertFalse(read.resolved)
        self.assertTrue(publisher.posts[0][2]["body"].startswith("No code change.\n\nAnalysis:"))
        self.assertNotIn("Copilot comment:", publisher.posts[0][2]["body"])
        state = self.step(store, name, state, read, publisher)
        self.assertTrue(read.resolved)
        self.assertEqual("confirmed", state["effects"][0]["resolution"]["status"])
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("threads_settled", state["stage"])
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("waiting_review", state["stage"])
        self.assertEqual(["repos/example/another-project/pulls/1/comments/20/replies", "graphql",
                          "repos/example/another-project/pulls/1/requested_reviewers"],
                         [call[0] for call in publisher.posts])

    def test_fixed_reply_uses_mapping_not_iteration_tip(self):
        store, name, state, read, publisher = self.context(True, multiple=True)
        state = self.step(store, name, state, read, publisher)
        state = self.step(store, name, state, read, publisher)
        sha = state["report"]["candidate"]["finding_commits"]["inline:20"]
        self.assertNotEqual(state["report"]["candidate"]["commit"], sha)
        self.assertTrue(publisher.posts[0][2]["body"].startswith("Addressed in " + sha + "."))

    def test_human_intervention_and_new_objection_are_skipped_not_resolved(self):
        for author in ({"id": 999, "node_id": "human", "type": "User"},
                       Read().comments[0]["user"]):
            store, name, state, read, publisher = self.context()
            read.comments.append({"id": 21, "in_reply_to_id": 20, "body": "New objection",
                                  "user": author})
            state = self.step(store, name, state, read, publisher)
            state = self.step(store, name, state, read, publisher)
            self.assertEqual("skipped", state["effects"][0]["status"])
            self.assertIn("intervention", state["effects"][0]["reason"])
            self.assertEqual([], publisher.posts)
            self.assertFalse(read.resolved)

    def test_already_resolved_threads_are_not_claimed_or_replied_to(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                store, name, state, read, publisher = self.context(changed)
                read.resolved = True
                read.outdated = True
                state = self.step(store, name, state, read, publisher)
                state = self.step(store, name, state, read, publisher)
                self.assertEqual("thread_already_settled", state["effects"][0]["reason"])
                self.assertNotIn("reply", state["effects"][0])
                self.assertEqual([], publisher.posts)

    def test_published_fix_replies_to_and_resolves_its_outdated_original_thread(self):
        store, name, state, read, publisher = self.context(True)
        read.outdated = True
        state = self.step(store, name, state, read, publisher)
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("confirmed", state["effects"][0]["reply"]["status"])
        self.assertTrue(publisher.posts[0][2]["body"].startswith("Addressed in "))
        state = self.step(store, name, state, read, publisher)
        self.assertTrue(read.resolved)
        self.assertEqual("confirmed", state["effects"][0]["resolution"]["status"])
        self.assertEqual(2, len(publisher.posts))

    def test_outdated_no_code_thread_is_skipped_without_claiming_resolution(self):
        store, name, state, read, publisher = self.context()
        read.outdated = True
        state = self.step(store, name, state, read, publisher)
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("thread_already_settled", state["effects"][0]["reason"])
        self.assertNotIn("reply", state["effects"][0])
        self.assertFalse(read.resolved)
        self.assertEqual([], publisher.posts)

    def test_lost_reply_response_is_reconcile_only_and_unknown_effect_blocks_restart(self):
        from loop.api import APIError
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        publisher.uncertain = True
        with self.assertRaises(APIError):
            self.step(store, name, state, read, publisher)
        state = copy.deepcopy(store.entries[name])
        self.assertTrue(pending(state))
        with self.assertRaises(Rejected):
            quiescent(Mock(), dict(state, stage="blocked"))
        publisher.uncertain = False
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("thread_reply_uncertain_no_retry", state["reason"])
        self.assertEqual(1, len(publisher.posts))

    def test_unique_observed_reply_reconciles_without_second_post(self):
        from loop.api import APIError
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        original_call = publisher.call
        def interrupted(path, method="GET", data=None, **kwargs):
            response = original_call(path, method, data, **kwargs)
            if path.endswith("/replies"):
                raise APIError(503, "Response lost after effect")
            return response
        publisher.call = interrupted
        with self.assertRaises(APIError):
            self.step(store, name, state, read, publisher)
        state = copy.deepcopy(store.entries[name])
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("confirmed", state["effects"][0]["reply"]["status"])
        self.assertEqual(1, len(publisher.posts))

    def test_resolution_response_loss_stays_uncertain_even_after_human_intervention(self):
        from loop.api import APIError
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        state = self.step(store, name, state, read, publisher)
        original_call = publisher.call
        def interrupted(path, method="GET", data=None, **kwargs):
            response = original_call(path, method, data, **kwargs)
            if path == "graphql":
                raise APIError(503, "Resolution response lost")
            return response
        publisher.call = interrupted
        with self.assertRaises(APIError):
            self.step(store, name, state, read, publisher)
        state = copy.deepcopy(store.entries[name])
        self.assertTrue(read.resolved)
        read.comments.append({"id": 99, "in_reply_to_id": 20, "body": "Wait",
                              "user": {"id": 999, "node_id": "human", "type": "User"}})
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("thread_resolution_uncertain_no_retry", state["reason"])
        self.assertTrue(pending(state))
        self.assertEqual(2, len(publisher.posts))
        with self.assertRaises(Rejected):
            quiescent(Mock(), state)

    def test_acknowledged_resolution_confirms_without_repeating_mutation(self):
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        state = self.step(store, name, state, read, publisher)
        original_graphql = read.graphql
        def interrupted(query, variables):
            if read.resolved:
                raise OSError("Live confirmation interrupted")
            return original_graphql(query, variables)
        read.graphql = interrupted
        with self.assertRaises(OSError):
            self.step(store, name, state, read, publisher)
        state = copy.deepcopy(store.entries[name])
        self.assertEqual("acknowledged", state["effects"][0]["resolution"]["status"])
        read.graphql = original_graphql
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("confirmed", state["effects"][0]["status"])
        self.assertFalse(pending(state))
        self.assertEqual(2, len(publisher.posts))

    def test_modified_confirmed_reply_prevents_resolution(self):
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        state = self.step(store, name, state, read, publisher)
        read.comments[-1]["body"] += " modified"
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("confirmed_thread_reply_changed_or_missing", state["effects"][0]["reason"])
        self.assertEqual("skipped", state["effects"][0]["status"])
        self.assertFalse(read.resolved)
        self.assertEqual(1, len(publisher.posts))

    def test_complete_comment_pagination_includes_second_page_human_objection(self):
        from loop.api import API
        store, name, state, read, publisher = self.context()
        human = {"id": 999, "node_id": "human", "type": "User"}
        unrelated = [{"id": 1000 + index, "body": "Unrelated", "user": human}
                     for index in range(99)]
        read.comments = [read.comments[0]] + unrelated + [
            {"id": 99, "in_reply_to_id": 20, "body": "Second-page objection", "user": human}]
        pages = []
        api = API()
        def page_call(path):
            page = int(path.rsplit("=", 1)[1])
            pages.append(page)
            return read.comments[(page - 1) * 100:page * 100]
        api.call = page_call
        original_pages = read.pages
        read.pages = lambda path, key=None: (
            api.pages(path, key) if path.endswith("/comments") else original_pages(path, key))
        state = self.step(store, name, state, read, publisher)
        state = self.step(store, name, state, read, publisher)
        self.assertEqual([1, 2], pages)
        self.assertEqual("skipped", state["effects"][0]["status"])
        self.assertEqual([], publisher.posts)

    def test_permission_rejection_records_failed_intent_without_retry(self):
        from loop.api import APIError
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        publisher.error = APIError(403, "Forbidden")
        with self.assertRaises(APIError):
            self.step(store, name, state, read, publisher)
        state = store.entries[name]
        self.assertEqual("publisher_permission_rejected", state["reason"])
        self.assertEqual("failed", state["effects"][0]["reply"]["status"])
        self.assertFalse(pending(state))
        self.assertEqual(1, len(publisher.posts))

    def test_expired_wrong_owner_and_stale_generation_never_mutate(self):
        for change in ("expired", "owner", "generation"):
            store, name, state, read, publisher = self.context()
            state = self.step(store, name, state, read, publisher)
            if change == "expired":
                state = advance(store, name, state, None, read, publisher,
                                state["request"]["deadline"])
                self.assertEqual("exhausted", state["stage"])
            else:
                if change == "owner":
                    state["coordinator_run"] = {"id": 100, "attempt": 1, "revision": SHA}
                    store.entries[name] = copy.deepcopy(state)
                else:
                    store.entries[name]["generation"] += 1
                with self.subTest(change=change), self.assertRaises(Rejected):
                    self.step(store, name, state, read, publisher)
            self.assertEqual([], publisher.posts)

    def test_cancellation_keeps_reply_intent_and_prevents_resolution(self):
        from loop.api import APIError
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        publisher.uncertain = True
        with self.assertRaises(APIError):
            self.step(store, name, state, read, publisher)
        current = copy.deepcopy(store.entries[name])
        cancelled = cancel(store, name, current["request"]["request_id"], current["generation"], 100)
        self.assertTrue(pending(cancelled))
        with self.assertRaises(Rejected):
            self.step(store, name, current, read, publisher)
        self.assertEqual(1, len(publisher.posts))

    def test_wrong_root_author_body_thread_and_head_never_mutate(self):
        for change in ("author", "body", "thread", "head"):
            store, name, state, read, publisher = self.context()
            state = self.step(store, name, state, read, publisher)
            if change == "author":
                read.comments[0]["user"] = {"id": 999, "type": "User"}
            elif change == "body":
                read.comments[0]["body"] += " changed"
            elif change == "thread":
                state["effects"][0]["thread"] = "foreign"
                store.entries[name] = copy.deepcopy(state)
            else:
                read.pr["head"]["sha"] = "f" * 40
            with self.subTest(change=change), self.assertRaises(Rejected):
                self.step(store, name, state, read, publisher)
            self.assertEqual([], publisher.posts)

    def test_unbound_mutations_and_old_requests_are_rejected(self):
        store, name, state, read, publisher = self.context()
        with self.assertRaises(ValueError):
            publisher.call("repos/example/another-project/pulls/1/comments/20/replies", "POST",
                           {"body": "unbound"})
        old = copy.deepcopy(state)
        old["request"].pop("protocol")
        with self.assertRaises(Rejected):
            self.step(store, name, old, read, publisher)

    def test_reply_endpoint_requires_frozen_pr_number_and_original_root(self):
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        state = self.step(store, name, state, read, publisher)
        effect = state["effects"][0]
        intent = dict(effect["reply"], body=reply_body(state, effect))
        publisher.bind_effect(intent)
        publisher.authorize("repos/example/another-project/pulls/1/comments/20/replies",
                            "POST", {"body": intent["body"]})
        for path in ("repos/example/another-project/pulls/comments/20/replies",
                     "repos/example/another-project/pulls/2/comments/20/replies",
                     "repos/example/another-project/pulls/1/comments/21/replies"):
            with self.subTest(path=path), self.assertRaises(Rejected):
                publisher.authorize(path, "POST", {"body": intent["body"]})
