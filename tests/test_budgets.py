from tests.support import launch
from tests.fixtures import (FIXTURE, REPOSITORIES)
import copy
import unittest
from unittest.mock import Mock, patch

from loop.api import APIError
from loop.cli import choose_live
from loop.coordinator import (checkpoint, dispatch, due, record_result)
from loop.freeze import freeze
from loop.live import publish, start, watch_review
from loop.policy import (DEFAULTS, Rejected, iso, pipeline_budget, pipeline_limit)
from tests.test_live import NONCLEAN, Publisher, Read, live_state, personal_request, stored
from tests.test_loop import FakeAPI, REVISION, SHA, request, review, run, verified_result


class BudgetTests(unittest.TestCase):
    def waiting(self, maximum, count):
        state = live_state(personal_request(max_pipelines=maximum), "waiting_review")
        state["iteration"] = count
        state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
        read = Read(state["request"])
        read.reviews = [review(id=13, body=NONCLEAN, submitted_at=iso(200))]
        return state, read

    def test_new_freeze_and_personal_phase_allow_five_total_including_initial(self):
        fresh = freeze(Read(), 1, REVISION, 100, FIXTURE)
        self.assertEqual(5, fresh["budgets"]["max_iterations"])
        self.assertEqual(7300, fresh["deadline"])
        old = checkpoint(fresh)
        old.update(stage="blocked", reason="validation_unqualified")
        store, _ = stored(old)
        new = freeze(Read(), 1, "f" * 40, 200, FIXTURE)
        _, phase = start(store, FakeAPI(), new, fresh["request_id"], 1,
                         True, "fine_grained_pat", ["Repository checks"], True, 200)
        self.assertEqual(5, pipeline_budget(phase))
        self.assertEqual(5, phase["request"]["publication"]["max_pipelines"])
        self.assertEqual(0, phase["iteration"])
        self.assertEqual(7400, phase["request"]["deadline"])

    def test_five_dispatches_and_fresh_findings_exhaust_without_sixth(self):
        state = live_state(personal_request(max_pipelines=5), "ready")
        state.update(iteration=0, run=None, intent=None)
        original = copy.deepcopy(state["request"])
        store, name = stored(state)
        api = FakeAPI()
        for count in range(1, 6):
            state = dispatch(store, name, api, 110 + count)
            self.assertEqual("dispatched", state["stage"])
            self.assertEqual(count, state["iteration"])
            state.update(stage="waiting_review", review_request={
                "baseline_review_ids": [], "recorded_at": 100, "sha": SHA})
            store.entries[name] = copy.deepcopy(state)
            read = Read(state["request"])
            read.resolved = True
            read.reviews = [review(
                id=12 + count, body=NONCLEAN.replace("Include the final array element",
                                                   f"Finding after pipeline {count}"),
                submitted_at=iso(200))]
            state = watch_review(store, name, state, read, 400 + count)
            self.assertEqual(count, state["iteration"])
            self.assertEqual(original["budgets"], state["request"]["budgets"])
            self.assertEqual(original["deadline"], state["request"]["deadline"])
            for key in ("phase", "max_pipelines", "continuation_deadline"):
                self.assertEqual(original["publication"][key], state["request"]["publication"][key])
            self.assertEqual("exhausted" if count == 5 else "ready", state["stage"])
        before = copy.deepcopy(state)
        self.assertEqual(before, dispatch(store, name, api, 500))
        self.assertEqual("remaining_findings_pipeline_budget", state["reason"])
        self.assertEqual(5, sum(method == "POST" for _, method, _ in api.calls))
        self.assertFalse(due(state, 600))

    def test_fourth_and_fifth_publications_work_but_sixth_never_touches_target(self):
        for count in (4, 5, 6):
            state = live_state(personal_request(max_pipelines=5))
            state["iteration"] = count
            from tests.test_live import AcceptanceTests
            candidate = AcceptanceTests().context()[0]["report"]["candidate"]
            store, name = stored(state)
            read = Read(state["request"])
            publisher = Publisher(read)
            def pushed(*_args):
                read.pr["head"]["sha"] = candidate["commit"]
            with patch("loop.live.evidence", return_value=({"profile": "test"}, candidate)) as evidence, \
                    patch("loop.live.authenticated_push", side_effect=pushed) as push, \
                    patch("loop.live.time.time", return_value=400), self.subTest(count=count):
                if count == 6:
                    with self.assertRaises(Rejected):
                        publish(store, name, state, Mock(), read, publisher, 400)
                    evidence.assert_not_called()
                    push.assert_not_called()
                    self.assertEqual(state, store.entries[name])
                else:
                    result = publish(store, name, state, Mock(), read, publisher, 400)
                    self.assertEqual("published", result["stage"])
                    self.assertEqual(count, result["iteration"])
                    self.assertEqual("confirmed", result["publication_intent"]["status"])
                    push.assert_called_once()
                self.assertEqual([], publisher.posts)

    def test_ready_at_five_never_dispatches_an_extra_worker(self):
        state = live_state(personal_request(max_pipelines=5), "ready")
        state["iteration"] = 5
        store, name = stored(state)
        api = FakeAPI()
        result = dispatch(store, name, api, 400)
        self.assertEqual("exhausted", result["stage"])
        self.assertEqual(5, result["iteration"])
        self.assertEqual([], api.calls)

    def test_uncertain_fifth_dispatch_consumes_last_slot_without_retry_or_reset(self):
        state = live_state(personal_request(max_pipelines=5), "ready")
        state.update(iteration=4, intent=None, run=None)
        store, name = stored(state)
        api = FakeAPI()
        api.uncertain = True
        with self.assertRaises(APIError):
            dispatch(store, name, api, 400)
        uncertain = copy.deepcopy(store.entries[name])
        self.assertEqual(5, uncertain["iteration"])
        self.assertEqual("uncertain", uncertain["intent"]["dispatch_status"])
        self.assertEqual(uncertain, dispatch(store, name, api, 401))
        with self.assertRaises(Rejected):
            launch(store, state["request"], "shadow", True)
        self.assertEqual(uncertain, store.entries[name])
        self.assertEqual(1, sum(method == "POST" for _, method, _ in api.calls))


    def test_invalid_frozen_budgets_never_dispatch_or_publish(self):
        for change in ("missing", "missing_max", "extra", "zero", "six", "string", "boolean",
                       "disagree", "missing_publication_max", "deadline_extension"):
            state = live_state(personal_request(max_pipelines=5), "ready")
            req = state["request"]
            if change == "missing":
                req.pop("budgets")
            elif change == "missing_max":
                req["budgets"].pop("max_iterations")
            elif change == "extra":
                req["budgets"]["extra"] = 1
            elif change in {"zero", "six", "string", "boolean"}:
                req["budgets"]["max_iterations"] = {
                    "zero": 0, "six": 6, "string": "5", "boolean": True}[change]
            elif change == "disagree":
                req["publication"]["max_pipelines"] = 2
            elif change == "missing_publication_max":
                req["publication"].pop("max_pipelines")
            else:
                req["publication"]["continuation_deadline"] += 1
            store, name = stored(state)
            api = FakeAPI()
            with self.subTest(change=change):
                with self.assertRaises(Rejected):
                    dispatch(store, name, api, 400)
                state["stage"] = "publish_pending"
                with patch("loop.live.evidence") as evidence:
                    with self.assertRaises(Rejected):
                        publish(store, name, state, Mock(), Read(), Publisher(Read()), 400)
                    evidence.assert_not_called()
                self.assertEqual([], api.calls)

    def test_invalid_or_inconsistent_consumed_counts_fail_explicitly(self):
        for count in (None, -1, True, "4", 4.0, 6):
            state = live_state(personal_request(max_pipelines=5), "ready")
            state["iteration"] = count
            store, name = stored(state)
            api = FakeAPI()
            with self.subTest(count=count), self.assertRaises(Rejected):
                dispatch(store, name, api, 400)
            self.assertEqual(state, store.entries[name])
            self.assertEqual([], api.calls)
        state = live_state()
        for change in ("run_without_count", "publications", "carried_count"):
            invalid = copy.deepcopy(state)
            if change == "run_without_count":
                invalid["iteration"] = 0
            elif change == "publications":
                invalid["publications"] = [{}, {}]
            else:
                invalid["request"]["publication"]["reviewable_retry"] = {"consumed_pipelines": 2}
            with self.subTest(change=change), self.assertRaises(Rejected):
                pipeline_budget(invalid)


    def test_stopped_activation_can_be_rerun_with_a_fresh_budget(self):
        state = checkpoint(dict(request(), repo=FIXTURE, repo_id=REPOSITORIES[FIXTURE], head_repo=FIXTURE, head_repo_id=1400255214,
                                source_private=False))
        state.update(stage="failed", reason="worker_agent_skipped", iteration=2)
        store, name = stored(state)
        retry = dict(state["request"], workflow_revision="f" * 40, request_id="e" * 32)
        with patch("loop.coordinator.time.time", return_value=500):
            _, result = launch(store, retry, "shadow", True)
        self.assertEqual("ready", result["stage"])
        self.assertEqual(0, result["iteration"])
        self.assertNotEqual(state["phase"], result["phase"])
        self.assertEqual(5, result["request"]["budgets"]["max_iterations"])


    def test_uncertain_terminal_record_cannot_be_bypassed_by_a_new_launch(self):
        state, _ = self.waiting(5, 5)
        state.update(stage="exhausted", reason="remaining_findings_pipeline_budget",
                     publications=[{"sha": SHA}, {"sha": SHA}])
        before = copy.deepcopy(state)
        store, name = stored(state)
        api = FakeAPI()
        self.assertEqual(before, dispatch(store, name, api, 400))
        with patch("loop.cli.output") as output:
            choose_live(store, 400, api)
            output.assert_not_called()
        with self.assertRaises(Rejected):
            launch(store, dict(request(), repo=FIXTURE, repo_id=REPOSITORIES[FIXTURE], head_repo=FIXTURE,
                               head_repo_id=1400255214, source_private=False), "shadow", True, api=api)
        new = dict(personal_request(max_pipelines=5), request_id="e" * 32,
                   workflow_revision="f" * 40)
        with self.assertRaises(Rejected):
            start(store, api, new, "d" * 32, 6, True, "fine_grained_pat",
                  ["Repository checks"], True, 400)
        self.assertEqual(before, store.entries[name])
        self.assertEqual([], api.calls)
