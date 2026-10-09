from tests.support import reconstruct
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from loop.candidates import semantic
from loop.coordinator import cancel, quiescent
from loop.effects import initialize, pending, reply_body
from loop.api import APIError
from loop.freeze import freeze
from loop.live import advance, start
from loop.policy import AUTHOR_ID, Rejected, digest
from loop.publication import acceptance, import_candidate
from loop.verify import git
from tests.support import batch, semantic as result
from tests import test_live as fixtures
from tests.test_live import Publisher, Read, personal_request, stored
from tests.test_loop import GOOD_PATCH, REVISION, SHA, baseline

SECOND = b"""diff --git a/Foo.java b/Foo.java
--- a/Foo.java
+++ b/Foo.java
@@ -1 +1 @@
-new
+final
"""




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
            for item in state["report"]["dispositions"]["findings"]:
                item["commit"] = 1 if item["key"] == "inline:20" else 2
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

    def description_context(self, changed=False):
        _, _, state, read, publisher = self.context(changed)
        req = state["request"]
        req["metadata"] = {"title": read.pr["title"], "body": read.pr["body"]}
        state["report"]["request_digest"] = digest(req)
        value = state["report"]["dispositions"]
        value["request_digest"] = digest(req)
        value["proposal"] = {"title": req["metadata"]["title"], "body": "The implementation uses ZIP archives."}
        finding = next(item for item in value["findings"] if item["key"] == "inline:20")
        finding.update(disposition="description_updated", commit=None,
                       analysis="Archive support and commit history confirm ZIP is intended. Corrected stale prose.")
        state["report"]["candidate"]["finding_commits"].pop("inline:20", None)
        state["publication_intent"]["acceptance"] = acceptance(state)
        store, name = stored(state)
        return store, name, state, read, publisher

    def test_copilot_freezes_description_as_context(self):
        read = Read()
        req = freeze(read, 1, REVISION, 100, read.req["repo"])
        self.assertEqual({"title": read.pr["title"], "body": read.pr["body"]}, req["metadata"])

    def test_description_correction_precedes_replies_and_fresh_review(self):
        for changed in (False, True):
            store, name, state, read, publisher = self.description_context(changed)
            state = self.step(store, name, state, read, publisher)
            self.assertEqual("published", state["stage"])
            self.assertEqual("confirmed", state["task_intent"]["status"])
            self.assertEqual(state["expected_sha"], publisher.assertion_identity)
            self.assertEqual(state["report"]["dispositions"]["proposal"]["body"], read.pr["body"])
            self.assertEqual("Original", read.pr["title"])
            self.assertEqual([], state["effects"])
            state = self.step(store, name, state, read, publisher)
            for _ in range(4):
                state = self.step(store, name, state, read, publisher)
            self.assertEqual("waiting_review", state["stage"])
            self.assertEqual(["PATCH", "POST", "POST", "POST"], [post[1] for post in publisher.posts])
            self.assertTrue(publisher.posts[1][2]["body"].startswith("PR description updated. No code change."))
            self.assertEqual(1, len(state["publications"]))

    def test_description_proposals_need_matching_decisions_and_frozen_metadata(self):
        _, _, state, _, _ = self.description_context()
        semantic(state["report"]["dispositions"], state["request"])
        for mutation in ("missing_proposal", "unchanged_body", "changed_title", "no_metadata", "blocked"):
            req, value = copy.deepcopy(state["request"]), copy.deepcopy(state["report"]["dispositions"])
            if mutation == "missing_proposal":
                value.pop("proposal")
            elif mutation == "unchanged_body":
                value["proposal"]["body"] = req["metadata"]["body"]
            elif mutation == "changed_title":
                value["proposal"]["title"] = "Unrelated title edit"
            elif mutation == "no_metadata":
                req.pop("metadata")
                value["request_digest"] = digest(req)
            else:
                value["outcome"] = "blocked"
                value["findings"][0]["disposition"] = "blocked"
            with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                semantic(value, req)

    def test_source_fixes_can_update_description_without_an_invented_finding(self):
        _, _, state, _, _ = self.description_context(True)
        value = state["report"]["dispositions"]
        value["findings"][1].update(disposition="fixed", commit=1)
        semantic(value, state["request"])

    def test_description_drift_or_cancellation_prevents_PATCH(self):
        for mutation in ("metadata", "head", "cancel"):
            store, name, state, read, publisher = self.description_context()
            if mutation == "metadata":
                read.pr["body"] = "Human edit"
            elif mutation == "head":
                read.pr["head"]["sha"] = REVISION
            else:
                cancel(store, name, state["request"]["request_id"], state["generation"], 100)
            with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                self.step(store, name, state, read, publisher)
            self.assertEqual([], publisher.posts)

    def test_interrupted_description_PATCH_reconciles_without_retry(self):
        for applied in (False, True):
            store, name, state, read, publisher = self.description_context()
            original = publisher.call
            def interrupted(path, method="GET", data=None):
                if applied:
                    original(path, method, data)
                else:
                    publisher.posts.append((path, method, copy.deepcopy(data)))
                raise APIError(503, "Lost PATCH response")
            with patch.object(publisher, "call", side_effect=interrupted), self.assertRaises(APIError):
                self.step(store, name, state, read, publisher)
            state = store.entries[name]
            self.assertEqual("task_effect_intent", state["stage"])
            self.assertEqual("uncertain", state["task_intent"]["status"])
            state = advance(store, name, state, None, read, publisher, 1000)
            self.assertEqual("published" if applied else "blocked", state["stage"])
            self.assertEqual(1, len(publisher.posts))
    def test_description_threads_cannot_claim_unpublished_correction(self):
        _, _, state, _, _ = self.description_context()
        with self.assertRaisesRegex(Rejected, "confirmed description"):
            initialize(state)

    def test_publish_reply_resolve_review_order_with_no_code_explanation(self):
        store, name, state, read, publisher = self.context()
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("thread_effects", state["stage"])
        self.assertEqual([], publisher.posts)
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("confirmed", state["effects"][0]["reply"]["status"])
        self.assertFalse(read.resolved)
        self.assertTrue(publisher.posts[0][2]["body"].startswith("No code change.\n\nInvestigated"))
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

    def test_outdated_already_addressed_thread_receives_reply_and_resolution(self):
        store, name, state, read, publisher = self.context()
        read.outdated = True
        state = self.step(store, name, state, read, publisher)
        state = self.step(store, name, state, read, publisher)
        self.assertEqual("confirmed", state["effects"][0]["reply"]["status"])
        self.assertTrue(publisher.posts[0][2]["body"].startswith("No code change."))
        state = self.step(store, name, state, read, publisher)
        self.assertTrue(read.resolved)
        self.assertEqual("confirmed", state["effects"][0]["resolution"]["status"])
        self.assertEqual(2, len(publisher.posts))

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

    def test_wrong_owner_and_stale_generation_never_mutate(self):
        for change in ("owner", "generation"):
            store, name, state, read, publisher = self.context()
            state = self.step(store, name, state, read, publisher)
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
