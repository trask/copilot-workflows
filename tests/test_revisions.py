import copy
import os
import unittest
from unittest.mock import Mock, patch

from loop.api import APIError
from loop.cli import choose_live, choose_verification, main as cli_main
from loop.control import busy, claim
from loop.coordinator import checkpoint, dispatch, run_binding
from loop.policy import CENTRAL, Rejected, pipeline_budget
from loop.revisions import (check_pin, execution_ref, inherit_pin, pin_revision,
                            revision_ref, workflow_ref)
from loop.waiter import poll
from tests.test_live import FIXTURE, Read, live_state, personal_request, stored
from tests.test_loop import FakeAPI, MemoryState, REVISION, run
from tests.test_waiter import Controls, ENV, controller, phases


class RevisionTests(unittest.TestCase):
    def test_pin_is_created_once_then_reused_without_updates(self):
        api = FakeAPI()
        ref = pin_revision(api, REVISION)
        self.assertEqual("review-loop-revisions/" + REVISION, ref)
        self.assertEqual(ref, pin_revision(api, REVISION))
        self.assertEqual([(f"repos/{CENTRAL}/git/refs", "POST", {
            "ref": "refs/heads/" + ref, "sha": REVISION,
        })], [call for call in api.calls if call[1] != "GET"])

    def test_concurrent_pin_creation_is_confirmed_without_retrying_the_post(self):
        api = FakeAPI()
        original = api.call
        def race(path, method="GET", data=None):
            value = original(path, method, data)
            if method == "POST":
                raise APIError(422, "Already created")
            return value
        api.call = race
        self.assertEqual(revision_ref(REVISION), pin_revision(api, REVISION))
        self.assertEqual(1, sum(method == "POST" for _, method, _ in api.calls))

    def test_new_pin_confirmation_waits_for_ref_visibility_without_recreating_it(self):
        for concurrent in (False, True):
            api = FakeAPI()
            original = api.call
            missing_reads = 2
            def delayed(path, method="GET", data=None):
                nonlocal missing_reads
                value = original(path, method, data)
                if method == "POST" and concurrent:
                    raise APIError(422, "Already created")
                if method == "GET" and missing_reads:
                    missing_reads -= 1
                    raise APIError(404, "Ref not visible yet")
                return value
            api.call = delayed
            with self.subTest(concurrent=concurrent), \
                    patch("time.sleep") as sleep, \
                    patch("sys.stderr") as stderr:
                self.assertEqual(revision_ref(REVISION), pin_revision(api, REVISION))
                self.assertEqual([1, 2], [call.args[0] for call in sleep.call_args_list])
                self.assertIn("PIN READ RETRY", "".join(
                    call.args[0] for call in stderr.write.call_args_list))
                self.assertEqual(1, sum(method == "POST" for _, method, _ in api.calls))
                self.assertEqual(4, sum(method == "GET" for _, method, _ in api.calls))

    def test_pin_confirmation_retries_only_404_and_stops_after_three_reads(self):
        for status, reads in ((404, 3), (403, 1), (503, 1)):
            api = FakeAPI()
            original = api.call
            def unavailable(path, method="GET", data=None):
                value = original(path, method, data)
                if method == "GET":
                    raise APIError(status, "Confirmation failed")
                return value
            api.call = unavailable
            with self.subTest(status=status), patch("time.sleep") as sleep, patch("sys.stderr"):
                with self.assertRaises(APIError) as raised:
                    pin_revision(api, REVISION)
                self.assertEqual(status, raised.exception.status)
                self.assertEqual(reads - 1, sleep.call_count)
                self.assertEqual(1, sum(method == "POST" for _, method, _ in api.calls))
                self.assertEqual(reads + 1, sum(method == "GET" for _, method, _ in api.calls))

    def test_changed_pin_is_never_overwritten_or_replaced_by_main(self):
        api = FakeAPI()
        ref = revision_ref(REVISION)
        api.revision_refs["refs/heads/" + ref] = "f" * 40
        with self.assertRaisesRegex(Rejected, "missing or changed"):
            pin_revision(api, REVISION)
        self.assertTrue(all(method == "GET" for _, method, _ in api.calls))

    def test_ref_errors_are_explicit_and_mutations_are_never_retried(self):
        for status in (403, 503):
            api = Mock()
            api.call.side_effect = APIError(status, "Read failed")
            with self.subTest(status=status), self.assertRaises(APIError):
                pin_revision(api, REVISION)
            self.assertEqual(1, api.call.call_count)
        api = FakeAPI()
        api.uncertain = True
        with self.assertRaises(APIError):
            pin_revision(api, REVISION)
        self.assertEqual(1, sum(method == "POST" for _, method, _ in api.calls))

    def test_ref_and_object_identity_must_both_match_the_revision(self):
        ref = revision_ref(REVISION)
        for mutation in ({"ref": "refs/heads/main"},
                         {"object": {"type": "tag", "sha": REVISION}},
                         {"object": {"type": "commit", "sha": "f" * 40}}):
            live = {"ref": "refs/heads/" + ref,
                    "object": {"type": "commit", "sha": REVISION}, **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                check_pin(Mock(call=Mock(return_value=live)), ref, REVISION)

    def test_frozen_pin_is_validated_and_next_passes_keep_it(self):
        req = personal_request()
        req["workflow_ref"] = revision_ref(REVISION)
        state = live_state(req)
        for invalid in ("main", "feature", revision_ref("f" * 40), None):
            bad = copy.deepcopy(state)
            bad["request"]["workflow_ref"] = invalid
            with self.subTest(ref=invalid), self.assertRaises(Rejected):
                pipeline_budget(bad)
        fresh = personal_request()
        inherit_pin(fresh, state)
        self.assertEqual(req["workflow_ref"], fresh["workflow_ref"])
        fresh["workflow_revision"] = "f" * 40
        with self.assertRaises(Rejected):
            inherit_pin(fresh, state)
        legacy = personal_request()
        inherit_pin(legacy, live_state())
        self.assertNotIn("workflow_ref", legacy)
        self.assertEqual("main", workflow_ref(legacy))
        state["reconciliation"] = {"execution_revision": "f" * 40,
                                   "workflow_ref": revision_ref("f" * 40)}
        self.assertEqual(revision_ref("f" * 40), execution_ref(state))
        fresh = dict(personal_request(), workflow_revision="f" * 40)
        inherit_pin(fresh, state)
        self.assertEqual(revision_ref("f" * 40), fresh["workflow_ref"])
        state["reconciliation"].pop("workflow_ref")
        with self.assertRaisesRegex(Rejected, "Missing reconciled"):
            execution_ref(state)

    def test_worker_dispatch_uses_the_pin_without_reading_current_main(self):
        req = personal_request()
        req["workflow_ref"] = revision_ref(REVISION)
        state = checkpoint(req)
        state.update(publications=[], effects=[])
        store, name = stored(state)
        api = FakeAPI()
        pin_revision(api, REVISION)
        api.calls.clear()
        dispatched = dispatch(store, name, api, 100)
        self.assertEqual("dispatched", dispatched["stage"])
        self.assertEqual(1, dispatched["iteration"])
        self.assertEqual(req["workflow_ref"], api.calls[-1][2]["ref"])
        self.assertFalse(any(path.endswith("/heads/main") for path, _, _ in api.calls))

    def test_missing_or_moved_worker_pin_blocks_before_spending_a_pipeline(self):
        for moved in (False, True):
            req = dict(personal_request(), workflow_ref=revision_ref(REVISION))
            state = checkpoint(req)
            state.update(publications=[], effects=[])
            store, name = stored(state)
            api = FakeAPI()
            if moved:
                api.revision_refs["refs/heads/" + req["workflow_ref"]] = "f" * 40
            with self.subTest(moved=moved):
                blocked = dispatch(store, name, api, 100)
                self.assertEqual("blocked", blocked["stage"])
                self.assertEqual("pinned_workflow_ref_unavailable", blocked["reason"])
                self.assertEqual(0, blocked["iteration"])
                self.assertIsNone(blocked["intent"])
                self.assertFalse(any(method == "POST" for _, method, _ in api.calls))

    def test_worker_provenance_requires_the_exact_pinned_branch_and_sha(self):
        req = dict(personal_request(), workflow_ref=revision_ref(REVISION))
        worker = dict(run(), head_branch=req["workflow_ref"])
        run_binding(worker, req)
        for mutation in ({"head_branch": "main"}, {"head_branch": "feature"},
                         {"head_sha": "f" * 40}, {"run_attempt": 2}):
            with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                run_binding(dict(worker, **mutation), req)

    def test_new_main_waiter_wakes_multiple_revision_pins_without_changing_phases(self):
        store, _ = phases(2, "verify_pending")
        api = Controls()
        for state, revision in zip(store.entries.values(), (REVISION, "c" * 40)):
            state["request"]["workflow_revision"] = revision
            state["request"]["workflow_ref"] = pin_revision(api, revision)
        before = copy.deepcopy(store.entries)
        api.calls.clear()
        self.assertTrue(poll(api, store, 400, "f" * 40, {}))
        posts = [data for _, method, data in api.calls if method == "POST"]
        self.assertCountEqual([revision_ref(REVISION), revision_ref("c" * 40)],
                              [post["ref"] for post in posts])
        self.assertEqual(before, store.entries)

    def test_broken_pin_blocks_only_its_phase_and_never_falls_back_to_main(self):
        store, _ = phases(2, "verify_pending")
        api = Controls()
        for state in store.entries.values():
            state["request"]["workflow_ref"] = pin_revision(api, REVISION)
        first = next(iter(store.entries.values()))
        first["request"].update(workflow_revision="c" * 40, workflow_ref=revision_ref("c" * 40))
        with patch("loop.waiter.summary"), patch("sys.stderr"):
            self.assertTrue(poll(api, store, 400, "f" * 40, {}))
        self.assertEqual("blocked", next(iter(store.entries.values()))["stage"])
        self.assertEqual("waiter_operation_rejected", next(iter(store.entries.values()))["reason"])
        posts = [data for path, method, data in api.calls
                 if method == "POST" and path.endswith("/coordinator.yml/dispatches")]
        self.assertEqual([revision_ref(REVISION)], [post["ref"] for post in posts])

    def test_main_tick_routes_exact_phase_to_old_revision_without_claiming_its_work(self):
        req = dict(personal_request(), workflow_ref=revision_ref(REVISION))
        store, _ = stored(live_state(req, "verify_pending"))
        before = copy.deepcopy(store.entries)
        api = Controls()
        pin_revision(api, REVISION)
        api.calls.clear()
        env = dict(ENV, GITHUB_SHA="f" * 40, GITHUB_REF="refs/heads/main")
        with patch.dict(os.environ, env, clear=True), \
                patch("sys.argv", ["loop.cli", "tick", "--target", FIXTURE + "#1",
                                   "--previous-request", req["request_id"],
                                   "--previous-generation", "6"]), \
                patch("loop.cli.API", return_value=api), \
                patch("loop.cli.State", return_value=store), \
                patch("loop.cli.time.time", return_value=400), \
                patch("loop.cli.output") as output:
            cli_main()
        output.assert_not_called()
        self.assertEqual(before, store.entries)
        self.assertEqual(req["workflow_ref"], api.calls[-1][2]["ref"])
        self.assertEqual(req["request_id"], api.calls[-1][2]["inputs"]["previous_request"])
        self.assertEqual("6", api.calls[-1][2]["inputs"]["previous_generation"])

    def test_new_main_does_not_select_or_block_an_old_pinned_publication(self):
        req = dict(personal_request(), workflow_ref=revision_ref(REVISION))
        store, _ = stored(live_state(req, "publish_pending"))
        before = copy.deepcopy(store.entries)
        env = dict(ENV, GITHUB_SHA="f" * 40, GITHUB_REF="refs/heads/main")
        with patch.dict(os.environ, env, clear=True), patch("loop.cli.output") as output:
            self.assertIsNone(choose_live(store, 400, Controls()))
        output.assert_not_called()
        self.assertEqual(before, store.entries)

    def test_pinned_coordinator_claim_and_live_selection_keep_exact_revision_provenance(self):
        req = dict(personal_request(), workflow_ref=revision_ref(REVISION))
        api = Controls()
        env = dict(ENV, GITHUB_REF="refs/heads/" + req["workflow_ref"])
        for stage in ("verify_pending", "published"):
            state = live_state(req, stage)
            store, name = stored(state)
            with self.subTest(stage=stage), patch.dict(os.environ, env, clear=True), \
                    patch("loop.cli.target_api", return_value=Read(req)), \
                    patch("loop.cli.output") as output:
                selected = (choose_verification(store, 400, api, only=name)
                            if stage == "verify_pending" else choose_live(store, 400, api, only=name))
            self.assertEqual(name, selected)
            output.assert_any_call("revision" if stage == "verify_pending" else "live_revision", REVISION)
            self.assertEqual(REVISION, store.entries[name]["coordinator_run"]["revision"])
        api.controllers[99] = dict(controller(99), head_branch=req["workflow_ref"])
        self.assertTrue(busy(api, store.entries[name]))
        api.controllers[99]["head_sha"] = "f" * 40
        with self.assertRaises(Rejected):
            busy(api, store.entries[name])
        for ref in ("refs/heads/feature", "refs/heads/" + revision_ref("f" * 40)):
            with self.subTest(ref=ref), patch.dict(os.environ, dict(env, GITHUB_REF=ref), clear=True), \
                    self.assertRaises(Rejected):
                claim(MemoryState(), name, state, api)
