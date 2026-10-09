import base64
import copy
import hashlib
import os
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from loop.api import API, APIError, DeadlineReached, MAX_RESPONSE
from loop.cli import choose_verification, finalize, main as cli_main, verify_pending
from loop.control import busy, claim, owned
from loop.policy import CENTRAL, Rejected, canonical, checkpoint_name, iso
from loop.state import Conflict, State
from loop.waiter import main, poll, ready, run as wait, wake
from tests.test_live import CLEAN, FIXTURE, Read, live_state, personal_request
from tests.test_loop import FakeAPI, GOOD_PATCH, MemoryState, REVISION, review, run


ENV = {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": REVISION}


def phases(count=1, stage="waiting_review", kind="copilot_review"):
    store = MemoryState()
    reads = {}
    for number in range(1, count + 1):
        request = personal_request(sha=f"{number + 100:040x}")
        request.update(pr=number, request_id=f"{number:032x}", head_ref=f"fix-{number}")
        if kind != "copilot_review":
            request.update(loop_kind=kind, base_ref="main", base_sha=REVISION,
                           merge_base_sha=REVISION, pr_diff={
                               "text": GOOD_PATCH.decode(),
                               "sha256": hashlib.sha256(GOOD_PATCH).hexdigest(),
                               "anchors": {"Foo.java": [1]},
                           })
        state = live_state(request, stage)
        state["review_request"] = {"sha": request["frozen_sha"], "recorded_at": 100,
                                   "baseline_review_ids": [12], "baseline_run_ids": [200]}
        name = checkpoint_name(FIXTURE, number, request["repo_id"])
        store.entries[name] = state
        read = Read(request)
        read.pr.update(number=number)
        read.pr["head"]["ref"] = request["head_ref"]
        reads[number] = read
    return store, reads


def controller(number, status="in_progress"):
    return dict(run(), id=number, path=".github/workflows/coordinator.yml",
                head_branch="main", status=status)


class Controls(FakeAPI):
    def __init__(self):
        super().__init__()
        self.deadline = None
        self.controllers = {}
        self.queued = []

    def call(self, path, method="GET", data=None):
        if method == "GET" and "/actions/runs/" in path:
            return self.controllers[int(path.rsplit("/", 1)[1])]
        result = super().call(path, method, data)
        if method == "POST" and "coordinator.yml/dispatches" in path:
            self.queued.append({"display_title": "Review loop tick " + data["inputs"]["previous_request"],
                                "status": "queued"})
        return result

    def pages(self, path, _key):
        return self.queued


class Reads:
    def __init__(self, reads):
        self.reads = reads

    def reader(self, path):
        match = re.search(r"/pulls/([0-9]+)", path)
        if match:
            return self.reads[int(match[1])]
        return next(read for read in self.reads.values() if read.req["frozen_sha"] in path)

    def call(self, path):
        return self.reader(path).call(re.sub(r"/pulls/[0-9]+", "/pulls/1", path))

    def pages(self, path, key=None):
        return self.reader(path).pages(path, key)


class WaiterTests(unittest.TestCase):
    def test_one_poll_scans_ten_prs_and_wakes_only_the_ready_subset(self):
        store, reads = phases(10)
        api, checked = Controls(), {}
        for number in (2, 5, 10):
            reads[number].reviews.append(review(id=13, commit_id=reads[number].req["frozen_sha"],
                                                submitted_at=iso(200), body=CLEAN))
        with patch("loop.waiter.target_api", return_value=Reads(reads)):
            self.assertTrue(poll(api, store, 400, REVISION, checked))
            self.assertTrue(poll(api, store, 460, REVISION, checked))
        posts = [data for _, method, data in api.calls if method == "POST"]
        self.assertCountEqual([(FIXTURE + "#" + str(number), f"{number:032x}", "6")
                               for number in (2, 5, 10)],
                              [(body["inputs"]["target"], body["inputs"]["previous_request"],
                                body["inputs"]["previous_generation"]) for body in posts])
        self.assertEqual(10, len(checked))
        self.assertTrue(all(state["stage"] == "waiting_review" for state in store.entries.values()))

    def test_ten_ready_prs_wake_ten_independent_coordinators(self):
        store, _ = phases(10, "verify_pending")
        api = Controls()
        self.assertTrue(poll(api, store, 400, REVISION, {}))
        self.assertEqual(10, len(api.calls))
        self.assertEqual(10, len({body["inputs"]["previous_request"] for _, _, body in api.calls}))
        self.assertTrue(all(path.endswith("/coordinator.yml/dispatches") and method == "POST"
                            for path, method, _ in api.calls))

    def test_later_prs_join_the_running_waiter_and_terminal_prs_leave(self):
        store, reads = phases()
        api, checked = Controls(), {}
        with patch("loop.waiter.target_api", return_value=Reads(reads)):
            poll(api, store, 400, REVISION, checked)
        later, _ = phases(2, "verify_pending")
        second = next(name for name, state in later.entries.items() if state["request"]["pr"] == 2)
        store.entries[second] = later.entries[second]
        with patch("loop.waiter.target_api", return_value=Reads(reads)):
            poll(api, store, 460, REVISION, checked)
        self.assertEqual(FIXTURE + "#2", api.calls[-1][2]["inputs"]["target"])
        for state in store.entries.values():
            state["stage"] = "cancelled"
        self.assertFalse(poll(api, store, 500, REVISION, checked))

    def test_review_readiness_requires_bot_exact_head_submission_and_propagation(self):
        store, reads = phases()
        state = next(iter(store.entries.values()))
        fresh = review(id=13, commit_id=state["expected_sha"], submitted_at=iso(200))
        mutations = [{"id": 12}, {"commit_id": "f" * 40}, {"submitted_at": iso(100)},
                     {"state": "PENDING"}, {"user": {"id": 999, "type": "User"}}]
        for mutation in mutations:
            reads[1].reviews = [dict(fresh, **mutation)]
            with self.subTest(mutation=mutation), patch("loop.waiter.target_api", return_value=Reads(reads)):
                self.assertFalse(ready(Controls(), state, 400))
        reads[1].reviews = [fresh]
        with patch("loop.waiter.target_api", return_value=Reads(reads)):
            self.assertFalse(ready(Controls(), state, 319))
            self.assertTrue(ready(Controls(), state, 320))
            reads[1].reviews.append(dict(fresh, id=14, submitted_at=iso(250)))
            self.assertFalse(ready(Controls(), state, 369))
            self.assertTrue(ready(Controls(), state, 370))

    def test_pending_ci_does_not_spawn_a_waiting_pr_runner(self):
        store, reads = phases(stage="waiting_ci", kind="ci_fix")
        state = next(iter(store.entries.values()))
        with patch("loop.waiter.target_api", return_value=Reads(reads)), \
                patch("loop.ci.collect", return_value={"decision": "pending"}) as collect:
            self.assertFalse(ready(Controls(), state, 400))
            collect.return_value = {"decision": "failed"}
            self.assertTrue(ready(Controls(), state, 400))
            collect.return_value = {"decision": "missing"}
            self.assertTrue(ready(Controls(), state, 400))

    def test_saved_non_repair_ci_waits_wake_while_ci_is_pending(self):
        for kind in ("copilot_review", "self_review", "pr_simplify"):
            store, reads = phases(stage="waiting_ci", kind=kind)
            reads[1].checks[0].update(status="in_progress", conclusion=None)
            api = Controls()
            with self.subTest(kind=kind), patch("loop.waiter.target_api", return_value=Reads(reads)):
                self.assertTrue(poll(api, store, 400, REVISION, {}))
                self.assertEqual(1, len(api.calls))
                self.assertEqual("POST", api.calls[0][1])

    def test_uncertain_request_is_confirmed_or_times_out_without_reposting(self):
        store, reads = phases(stage="review_request_intent")
        state = next(iter(store.entries.values()))
        with patch("loop.waiter.target_api", return_value=Reads(reads)):
            self.assertFalse(ready(Controls(), state, 400))
            self.assertTrue(ready(Controls(), state, 1000))
            reads[1].pr["requested_reviewers"] = [review()["user"]]
            self.assertTrue(ready(Controls(), state, 400))

    def test_active_publisher_claim_prevents_premature_push_reconciliation(self):
        store, _ = phases(stage="publication_intent")
        state = next(iter(store.entries.values()))
        state["coordinator_run"] = {"id": 99, "attempt": 1, "revision": REVISION}
        api = Controls()
        api.controllers[99] = controller(99)
        with patch("loop.waiter.ready") as probe:
            self.assertTrue(poll(api, store, 400, REVISION, {}))
        probe.assert_not_called()
        self.assertFalse(any(method == "POST" for _, method, _ in api.calls))
        api.controllers[99]["status"] = "completed"
        self.assertTrue(poll(api, store, 400, REVISION, {}))
        self.assertEqual(1, sum(method == "POST" for _, method, _ in api.calls))

    def test_one_stale_or_inaccessible_pr_does_not_block_other_ready_prs(self):
        for error in (Rejected("Target changed"), APIError(403, "Read denied"), APIError(503, "Unavailable")):
            store, reads = phases(2, "verify_pending")
            first = next(name for name, state in store.entries.items() if state["request"]["pr"] == 1)
            store.entries[first]["stage"] = "waiting_review"
            api = Controls()
            with self.subTest(error=error), patch("loop.waiter.target_api", side_effect=error), \
                    patch("loop.waiter.summary"), patch("sys.stderr"):
                self.assertTrue(poll(api, store, 400, REVISION, {}))
            self.assertEqual(FIXTURE + "#2", api.calls[-1][2]["inputs"]["target"])
            self.assertEqual("waiting_review" if isinstance(error, APIError) and error.status == 503
                             else "blocked", store.entries[first]["stage"])

    def test_cancelled_and_retired_phases_do_not_keep_the_waiter_running(self):
        store, _ = phases(2)
        states = list(store.entries.values())
        states[0]["stage"] = "cancelled"
        states[1]["stage"] = "waiting_capability"
        before = copy.deepcopy(states[1])
        with patch("loop.waiter.summary"), patch("sys.stderr"):
            self.assertFalse(poll(Controls(), store, 400, REVISION, {}))
        self.assertEqual("cancelled", next(iter(store.entries.values()))["stage"])
        self.assertEqual(before, list(store.entries.values())[1])

    def test_ci_pending_for_days_can_finish_and_wake_the_coordinator(self):
        store, reads = phases(stage="waiting_ci", kind="ci_fix")
        api, checked = Controls(), {}
        with patch("loop.waiter.target_api", return_value=Reads(reads)), \
                patch("loop.ci.collect", return_value={"decision": "pending"}) as collect:
            self.assertTrue(poll(api, store, 3 * 86400, REVISION, checked))
            self.assertEqual([], api.calls)
            collect.return_value = {"decision": "passed"}
            self.assertTrue(poll(api, store, 3 * 86400 + 300, REVISION, checked))
        self.assertEqual("waiting_ci", next(iter(store.entries.values()))["stage"])
        self.assertEqual(1, len(api.calls))
        self.assertEqual("POST", api.calls[0][1])

    def test_rate_limited_probe_preserves_phase_and_propagates_reset_before_more_reads(self):
        store, _ = phases()
        before = copy.deepcopy(store.entries)
        checked = {}
        error = APIError(403, "Quota exhausted", rate_limited=True, retry_at=1000)
        with patch("loop.waiter.target_api", side_effect=error), patch("sys.stderr"), \
                patch.object(store, "snapshot", wraps=store.snapshot) as snapshot, \
                self.assertRaises(APIError) as raised:
            poll(Controls(), store, 400, REVISION, checked)
        self.assertIs(error, raised.exception)
        self.assertEqual(before, store.entries)
        self.assertEqual({}, checked)
        snapshot.assert_called_once()

    def test_changed_trusted_revision_blocks_without_spawning_work(self):
        store, _ = phases()
        api = Controls()
        with patch("loop.waiter.summary"):
            self.assertFalse(poll(api, store, 400, "f" * 40, {}))
        self.assertEqual("blocked", next(iter(store.entries.values()))["stage"])
        self.assertEqual([], api.calls)

    def test_queued_wake_is_not_duplicated(self):
        store, _ = phases(stage="verify_pending")
        state = next(iter(store.entries.values()))
        api = Controls()
        wake(api, state)
        wake(api, state)
        self.assertEqual(1, len(api.calls))

    def test_waiter_handoff_is_bounded_and_empty_waiter_exits_without_dispatch(self):
        for active in (True, False):
            api = Controls()
            with self.subTest(active=active), patch.dict(os.environ, ENV, clear=True), \
                    patch("loop.waiter.poll", return_value=active), \
                    patch("loop.waiter.time.monotonic", side_effect=[0, 0, 120]), \
                    patch("loop.waiter.time.sleep") as sleep:
                wait(api, MemoryState(), duration=120)
            posts = [path for path, method, _ in api.calls if method == "POST"]
            self.assertEqual([f"repos/{CENTRAL}/actions/workflows/waiter.yml/dispatches"]
                             if active else [], posts)
            if active:
                sleep.assert_called_once_with(60)
            else:
                sleep.assert_not_called()

    def test_rate_limited_waiter_waits_for_reset_without_dispatching_or_blocking_tasks(self):
        api = Mock()
        api.call.side_effect = [
            APIError(403, "Quota exhausted", rate_limited=True, retry_at=200),
            {"object": {"sha": REVISION}},
        ]
        with patch.dict(os.environ, ENV, clear=True), \
                patch("loop.waiter.time.monotonic", return_value=0), \
                patch("loop.waiter.time.time", return_value=100), \
                patch("loop.waiter.time.sleep") as sleep, \
                patch("loop.waiter.poll", return_value=False) as poll_once:
            wait(api, MemoryState(), duration=120)
        sleep.assert_called_once_with(101)
        poll_once.assert_called_once()
        self.assertEqual(2, api.call.call_count)
        self.assertTrue(all(len(call.args) == 1 for call in api.call.call_args_list))
        self.assertIsNone(api.deadline)

    def test_quota_reset_outside_waiter_deadline_defers_to_schedule_without_api_handoff(self):
        api = Mock()
        api.call.side_effect = APIError(403, "Quota exhausted", rate_limited=True, retry_at=200)
        with patch.dict(os.environ, ENV, clear=True), \
                patch("loop.waiter.time.monotonic", return_value=0), \
                patch("loop.waiter.time.time", return_value=100), \
                patch("loop.waiter.time.sleep") as sleep, patch("loop.waiter.poll") as poll_once:
            wait(api, MemoryState(), duration=60)
        api.call.assert_called_once()
        sleep.assert_not_called()
        poll_once.assert_not_called()
        self.assertIsNone(api.deadline)

    def test_waiter_rejects_untrusted_runner_and_never_receives_publisher_auth(self):
        environment = dict(ENV, GITHUB_REPOSITORY=CENTRAL, GITHUB_REF="refs/heads/main",
                           GITHUB_EVENT_NAME="workflow_run")
        for field, value in (("GITHUB_REPOSITORY", FIXTURE), ("GITHUB_REF", "refs/heads/other"),
                             ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_RUN_ATTEMPT", "2")):
            with self.subTest(field=field), patch.dict(os.environ, dict(environment, **{field: value}),
                                                       clear=True), \
                    patch("loop.waiter.API") as api, self.assertRaises(Rejected):
                main()
            api.assert_not_called()

    def test_api_calls_and_paginated_reads_cannot_outlive_the_handoff_deadline(self):
        api = API("offline-test-token")
        api.deadline = 101
        with patch("loop.api.time.monotonic", return_value=100), \
                patch("loop.api.urllib.request.urlopen") as open_request:
            open_request.return_value.__enter__.return_value.read.return_value = b"{}"
            api.call(f"repos/{CENTRAL}/git/ref/heads/main")
        self.assertEqual(1, open_request.call_args.kwargs["timeout"])
        with patch("loop.api.time.monotonic", return_value=101), \
                patch("loop.api.urllib.request.urlopen") as open_request, \
                self.assertRaises(DeadlineReached):
            api.pages(f"repos/{CENTRAL}/actions/runs", "workflow_runs")
        open_request.assert_not_called()
        api = Controls()
        with patch.dict(os.environ, ENV, clear=True), patch("loop.waiter.time.monotonic", return_value=0), \
                patch("loop.waiter.poll", side_effect=DeadlineReached("Expired")), \
                patch("loop.waiter.time.sleep") as sleep:
            wait(api, MemoryState(), duration=120)
        self.assertIsNone(api.deadline)
        self.assertEqual(f"repos/{CENTRAL}/actions/workflows/waiter.yml/dispatches", api.calls[-1][0])
        sleep.assert_not_called()


class ControlTests(unittest.TestCase):
    def test_ten_prs_claim_independent_verification_runs_and_duplicate_selectors_skip_them(self):
        store, reads = phases(10, "verify_pending")
        api = Controls()
        for number, (name, state) in enumerate(store.entries.items(), 1):
            execution = dict(ENV, GITHUB_RUN_ID=str(100 + number))
            api.controllers[100 + number] = controller(100 + number)
            with patch.dict(os.environ, execution, clear=True), \
                    patch("loop.cli.target_api", return_value=Reads(reads)), \
                    patch("loop.cli.output") as output:
                self.assertEqual(name, choose_verification(store, 400, api, only=name))
            output.assert_any_call("pr", state["request"]["pr"])
        self.assertEqual(10, len({state["coordinator_run"]["id"] for state in store.entries.values()}))
        with patch.dict(os.environ, ENV, clear=True), patch("loop.cli.output") as output:
            self.assertIsNone(choose_verification(store, 400, api))
        output.assert_not_called()

    def test_completed_claim_can_be_reconciled_but_unbound_run_cannot_be_inherited(self):
        store, _ = phases(stage="published")
        name, state = next(iter(store.entries.items()))
        state["coordinator_run"] = {"id": 98, "attempt": 1, "revision": REVISION}
        api = Controls()
        api.controllers[98] = controller(98, "completed")
        with patch.dict(os.environ, ENV, clear=True):
            claimed = claim(store, name, state, api)
            self.assertEqual(99, claimed["coordinator_run"]["id"])
            owned(claimed)
            with patch.dict(os.environ, GITHUB_RUN_ID="100"):
                with self.assertRaises(Rejected):
                    owned(claimed)
        for key, value in (("head_sha", "f" * 40), ("head_branch", "other"),
                           ("path", ".github/workflows/other.yml"), ("run_attempt", 2)):
            with self.subTest(key=key), self.assertRaises(Rejected):
                api.controllers[98] = dict(controller(98), **{key: value})
                busy(api, state)

    def test_other_coordinator_cannot_verify_or_finalize_an_owned_pipeline(self):
        store, _ = phases(stage="verify_pending")
        name, state = next(iter(store.entries.items()))
        state["coordinator_run"] = {"id": 98, "attempt": 1, "revision": REVISION}
        before = copy.deepcopy(state)
        args = SimpleNamespace(pr="1", repo=FIXTURE, request_id=state["request"]["request_id"],
                               generation="6")
        for operation in (verify_pending, finalize):
            with self.subTest(operation=operation.__name__), patch.dict(os.environ, ENV, clear=True), \
                    self.assertRaisesRegex(Rejected, "another coordinator"):
                operation(Mock(), store, args)
            self.assertEqual(before, store.entries[name])

    def test_targeted_tick_cannot_select_another_pr_and_rejects_stale_generations(self):
        store, _ = phases(2, "published")
        second = next(name for name, state in store.entries.items() if state["request"]["pr"] == 2)
        request = store.entries[second]["request"]
        for generation in ("6", "5"):
            argv = ["loop.cli", "tick", "--target", FIXTURE + "#2", "--previous-request",
                    request["request_id"], "--previous-generation", generation]
            with self.subTest(generation=generation), patch("sys.argv", argv), \
                    patch.dict(os.environ, ENV, clear=True), patch("loop.cli.API", return_value=Controls()), \
                    patch("loop.cli.State", return_value=store), patch("loop.cli.time.time", return_value=400), \
                    patch("loop.cli.choose_verification", return_value=None) as verify, \
                    patch("loop.cli.choose_live") as live:
                if generation == "5":
                    with self.assertRaisesRegex(Rejected, "generation"):
                        cli_main()
                    verify.assert_not_called()
                    live.assert_not_called()
                else:
                    cli_main()
                    self.assertEqual(second, verify.call_args.kwargs["only"])
                    self.assertEqual(second, live.call_args.kwargs["only"])

    def test_one_coordinator_run_does_not_mix_another_pr_live_work_with_verification(self):
        with patch("sys.argv", ["loop.cli", "tick"]), patch("loop.cli.API"), \
                patch("loop.cli.State", return_value=MemoryState()), \
                patch("loop.cli.choose_verification", return_value="pr-v2-1-1.json"), \
                patch("loop.cli.choose_live") as live:
            cli_main()
        live.assert_not_called()


class StateCacheTests(unittest.TestCase):
    def test_acknowledged_write_retries_stale_ref_before_returning_checkpoint(self):
        before = live_state(stage="publication_intent")
        after = dict(before, stage="published")
        name = checkpoint_name(before["request"]["repo"], before["request"]["pr"],
                               before["request"]["repo_id"])
        old_head, new_head, tree = "c" * 40, "f" * 40, "e" * 40
        payload = canonical(after)
        blob = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
        api = Mock()
        api.call.return_value = {"sha": new_head}
        store = State(api)
        with patch.object(store, "snapshot", return_value=(old_head, tree, {name: before})):
            store.write(name, after, before)
        store.cached = (old_head, tree, {name: before})
        api.call.reset_mock()
        api.call.side_effect = [
            {"object": {"sha": old_head}},
            {"status": "behind", "base_commit": {"sha": new_head},
             "merge_base_commit": {"sha": old_head}},
            {"object": {"sha": new_head}},
            {"tree": {"sha": tree}},
            {"truncated": False, "tree": [
                {"path": name, "mode": "100644", "type": "blob", "sha": blob, "size": len(payload)}]},
        ]
        api.graphql.return_value = {"repository": {"blob0": {
            "oid": blob, "text": payload.decode(), "isTruncated": False}}}
        with patch("loop.state.time.sleep") as sleep:
            self.assertEqual({name: after}, store.snapshot()[2])
        sleep.assert_called_once_with(1)
        self.assertEqual(f"repos/{CENTRAL}/compare/{new_head}...{old_head}?per_page=1",
                         api.call.call_args_list[1].args[0])
        self.assertEqual(new_head, store.written_head)

    def test_newer_checkpoint_is_not_hidden_by_an_acknowledged_write(self):
        value = {"stage": "cancelled"}
        payload = canonical(value)
        blob = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
        api = Mock()
        api.call.side_effect = [
            {"object": {"sha": "f" * 40}},
            {"status": "ahead", "base_commit": {"sha": REVISION},
             "merge_base_commit": {"sha": REVISION}},
            {"tree": {"sha": "e" * 40}},
            {"truncated": False, "tree": [
                {"path": "pr-v2-1-1.json", "mode": "100644", "type": "blob",
                 "sha": blob, "size": len(payload)}]},
        ]
        api.graphql.return_value = {"repository": {"blob0": {
            "oid": blob, "text": payload.decode(), "isTruncated": False}}}
        store = State(api)
        store.written_head = REVISION
        with patch("loop.state.time.sleep") as sleep:
            self.assertEqual({"pr-v2-1-1.json": value}, store.snapshot()[2])
        sleep.assert_not_called()

    def test_missing_or_stale_ref_cannot_outlive_the_visibility_retry_budget(self):
        for missing in (False, True):
            api = Mock()
            api.call.side_effect = lambda path: (
                {"object": {"sha": "c" * 40}} if "/ref/" in path else
                {"status": "behind", "base_commit": {"sha": REVISION},
                 "merge_base_commit": {"sha": "c" * 40}})
            if missing:
                api.call.side_effect = APIError(404, "Not visible")
            store = State(api)
            store.written_head = REVISION
            with self.subTest(missing=missing), patch("loop.state.time.sleep") as sleep, \
                    self.assertRaisesRegex(Rejected, "Acknowledged state write is not yet visible"):
                store.snapshot()
            self.assertEqual([1, 2, 3, 4], [call.args[0] for call in sleep.call_args_list])
            api.graphql.assert_not_called()

    def test_diverged_state_ref_is_not_accepted_as_a_newer_checkpoint(self):
        api = Mock()
        api.call.side_effect = [
            {"object": {"sha": "f" * 40}},
            {"status": "diverged", "base_commit": {"sha": REVISION},
             "merge_base_commit": {"sha": "c" * 40}},
        ]
        store = State(api)
        store.written_head = REVISION
        with patch("loop.state.time.sleep") as sleep, \
                self.assertRaisesRegex(Rejected, "State ref diverged"):
            store.snapshot()
        sleep.assert_not_called()
        api.graphql.assert_not_called()

    def test_immutable_state_cache_avoids_refetching_archives_and_never_shares_mutable_values(self):
        value = {"schema": 2, "request": {"pr": 1}}
        payload = canonical(value)
        entry = {"path": "request-" + "d" * 32 + ".json", "type": "blob", "mode": "100644",
                 "sha": hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest(),
                 "size": len(payload)}
        api = Mock()
        api.call.side_effect = lambda path: (
            {"object": {"sha": REVISION}} if "/ref/" in path else
            {"tree": {"sha": "e" * 40}} if "/commits/" in path else
            {"truncated": False, "tree": [entry]} if "/trees/" in path else
            {"content": base64.b64encode(canonical(value)).decode()})
        api.graphql.return_value = {"repository": {"blob0": {
            "oid": entry["sha"], "text": canonical(value).decode(), "isTruncated": False}}}
        store = State(api)
        first = store.snapshot()[2]
        first[entry["path"]]["request"]["pr"] = 999
        self.assertEqual(1, store.snapshot()[2][entry["path"]]["request"]["pr"])
        self.assertEqual(4, api.call.call_count)
        api.graphql.assert_called_once()
        api.call.side_effect = lambda path: (
            {"object": {"sha": "f" * 40}} if "/ref/" in path else
            {"tree": {"sha": "e" * 40}} if "/commits/" in path else
            {"truncated": False, "tree": [entry]} if "/trees/" in path else
            self.fail("Unchanged archived blob was refetched"))
        self.assertEqual(value, store.snapshot()[2][entry["path"]])
        api.graphql.assert_called_once()

    def test_snapshot_batches_checkpoint_and_archive_reads(self):
        entries = [
            {"path": path, "type": "blob", "mode": "100644",
             "sha": hashlib.sha1(b'blob 8\0{"pr":1}').hexdigest(), "size": 8}
            for path in ("pr-v2-1-1.json", "request-" + "d" * 32 + ".json",
                         "stopped-" + "d" * 32 + "-1.json")]
        api = Mock()
        api.call.side_effect = lambda path: (
            {"object": {"sha": REVISION}} if "/ref/" in path else
            {"tree": {"sha": "e" * 40}} if "/commits/" in path else
            {"truncated": False, "tree": entries})
        api.graphql.return_value = {"repository": {
            f"blob{i}": {"oid": entry["sha"], "text": '{"pr":1}', "isTruncated": False}
            for i, entry in enumerate(entries)}}
        self.assertEqual({entry["path"]: {"pr": 1} for entry in entries},
                         State(api).snapshot()[2])
        self.assertEqual(3, api.call.call_count)
        api.graphql.assert_called_once()
        self.assertEqual({f"blob{i}": entry["sha"] for i, entry in enumerate(entries)},
                         {key: value for key, value in api.graphql.call_args.args[1].items()
                          if key.startswith("blob")})

    def test_snapshot_preserves_all_records_across_count_and_byte_sized_batches(self):
        for count, text in ((101, "small"), (3, "\u4e00" * (MAX_RESPONSE // 96))):
            with self.subTest(count=count):
                values, entries, blobs = {}, [], {}
                for number in range(count):
                    path = f"pr-v2-1-{number + 1}.json"
                    value = {"pr": number + 1, "text": text}
                    payload = canonical(value)
                    oid = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
                    values[path] = value
                    entries.append({"path": path, "type": "blob", "mode": "100644",
                                    "sha": oid, "size": len(payload)})
                    blobs[oid] = {"oid": oid, "text": payload.decode(), "isTruncated": False}
                api = Mock()
                api.call.side_effect = lambda path: (
                    {"object": {"sha": REVISION}} if "/ref/" in path else
                    {"tree": {"sha": "e" * 40}} if "/commits/" in path else
                    {"truncated": False, "tree": entries})
                api.graphql.side_effect = lambda query, variables: {"repository": {
                    key: blobs[oid] for key, oid in variables.items() if key.startswith("blob")}}
                store = State(api)
                self.assertEqual(values, store.snapshot()[2])
                self.assertGreater(api.graphql.call_count, 1)
                fetched = []
                for call in api.graphql.call_args_list:
                    batch = [oid for key, oid in call.args[1].items() if key.startswith("blob")]
                    self.assertLessEqual(len(batch), 100)
                    self.assertLessEqual(
                        sum(entry["size"] for entry in entries if entry["sha"] in batch),
                        MAX_RESPONSE // 8)
                    fetched.extend(batch)
                self.assertEqual([entry["sha"] for entry in entries], fetched)
                api.graphql.reset_mock()
                self.assertEqual(values, store.snapshot()[2])
                api.graphql.assert_not_called()

    def test_later_batch_read_failure_does_not_cache_a_partial_snapshot(self):
        payload = b'{"pr":1}'
        oid = hashlib.sha1(b'blob 8\0' + payload).hexdigest()
        entries = [{"path": f"pr-v2-1-{number + 1}.json", "type": "blob", "mode": "100644",
                    "sha": oid, "size": len(payload)} for number in range(101)]
        api = Mock()
        api.call.side_effect = lambda path: (
            {"object": {"sha": REVISION}} if "/ref/" in path else
            {"tree": {"sha": "e" * 40}} if "/commits/" in path else
            {"truncated": False, "tree": entries})
        api.graphql.side_effect = [
            {"repository": {f"blob{i}": {"oid": oid, "text": payload.decode(), "isTruncated": False}
                            for i in range(100)}},
            APIError(403, "Batch read denied"),
        ]
        store = State(api)
        with self.assertRaisesRegex(APIError, "Batch read denied"):
            store.snapshot()
        self.assertIsNone(store.cached)
        self.assertEqual({}, store.blobs)

    def test_truncated_graphql_checkpoint_fetches_the_complete_bound_rest_blob(self):
        entry = {"path": "pr-v2-1-1.json", "type": "blob", "mode": "100644",
                 "sha": hashlib.sha1(b'blob 8\0{"pr":1}').hexdigest(), "size": 8}
        api = Mock()
        api.call.side_effect = lambda path: (
            {"object": {"sha": REVISION}} if "/ref/" in path else
            {"tree": {"sha": "e" * 40}} if "/commits/" in path else
            {"truncated": False, "tree": [entry]} if "/trees/" in path else
            {"content": base64.b64encode(b'{"pr":1}').decode()})
        api.graphql.return_value = {"repository": {"blob0": {
            "oid": entry["sha"], "text": '{"pr":', "isTruncated": True}}}
        self.assertEqual({entry["path"]: {"pr": 1}}, State(api).snapshot()[2])
        self.assertEqual(4, api.call.call_count)
        self.assertEqual("repos/" + CENTRAL + "/git/blobs/" + entry["sha"],
                         api.call.call_args.args[0])

    def test_changed_graphql_text_fetches_exact_rest_bytes_even_when_json_is_valid(self):
        entry = {"path": "pr-v2-1-1.json", "type": "blob", "mode": "100644",
                 "sha": hashlib.sha1(b'blob 8\0{"pr":1}').hexdigest(), "size": 8}
        api = Mock()
        api.call.side_effect = lambda path: (
            {"object": {"sha": REVISION}} if "/ref/" in path else
            {"tree": {"sha": "e" * 40}} if "/commits/" in path else
            {"truncated": False, "tree": [entry]} if "/trees/" in path else
            {"content": base64.b64encode(b'{"pr":1}').decode()})
        api.graphql.return_value = {"repository": {"blob0": {
            "oid": entry["sha"], "text": '{"pr":0}', "isTruncated": False}}}
        self.assertEqual({entry["path"]: {"pr": 1}}, State(api).snapshot()[2])
        self.assertEqual(4, api.call.call_count)
        self.assertEqual("repos/" + CENTRAL + "/git/blobs/" + entry["sha"],
                         api.call.call_args.args[0])
        api.call.side_effect = lambda path: (
            {"object": {"sha": REVISION}} if "/ref/" in path else
            {"tree": {"sha": "e" * 40}} if "/commits/" in path else
            {"truncated": False, "tree": [entry]} if "/trees/" in path else
            {"content": base64.b64encode(b'{"pr":0}').decode()})
        with self.assertRaisesRegex(Rejected, "blob bytes differ"):
            State(api).snapshot()

    def test_batched_checkpoint_identity_is_checked_before_acceptance(self):
        entry = {"path": "pr-v2-1-1.json", "type": "blob", "mode": "100644",
                 "sha": "c" * 40, "size": 8}
        api = Mock()
        api.call.side_effect = lambda path: (
            {"object": {"sha": REVISION}} if "/ref/" in path else
            {"tree": {"sha": "e" * 40}} if "/commits/" in path else
            {"truncated": False, "tree": [entry]})
        api.graphql.return_value = {"repository": {"blob0": {
            "oid": "f" * 40, "text": '{"pr":1}', "isTruncated": False}}}
        with self.assertRaisesRegex(Rejected, "blob identity"):
            State(api).snapshot()

    def test_state_conflict_backoff_is_bounded(self):
        store = State(Mock())
        with patch.object(store, "snapshot", return_value=(None, None, {})), \
                patch.object(store, "write", side_effect=Conflict("Race")) as write, \
                patch("loop.state.time.sleep") as sleep, self.assertRaises(Conflict):
            store.update("pr-v2-1-1.json", lambda _: {"value": 1})
        self.assertEqual(20, write.call_count)
        self.assertEqual(19, sleep.call_count)
        self.assertTrue(all(0 <= call.args[0] <= 1 for call in sleep.call_args_list))

    def test_ten_concurrent_state_writers_preserve_every_pr_without_force_updates(self):
        class GitAPI:
            def __init__(self):
                self.lock = threading.Lock()
                self.barrier = threading.Barrier(10)
                self.seen = set()
                self.head = None
                self.objects = {}

            def call(self, path, method="GET", data=None):
                if method == "GET" and "/ref/" in path:
                    identity = threading.get_ident()
                    with self.lock:
                        first = identity not in self.seen
                        self.seen.add(identity)
                    if first:
                        self.barrier.wait(timeout=10)
                with self.lock:
                    if method == "GET":
                        if "/ref/" in path:
                            if self.head is None:
                                raise APIError(404, "No state branch")
                            return {"object": {"sha": self.head}}
                        return copy.deepcopy(self.objects[path.rsplit("/", 1)[1]])
                    if path.endswith("/refs") or "/refs/heads/" in path:
                        commit = self.objects[data["sha"]]
                        if commit["parents"] != ([] if self.head is None else [self.head]):
                            raise APIError(422, "Non-fast-forward race")
                        if method == "PATCH":
                            if data["force"]:
                                raise AssertionError("Forced state update")
                        elif self.head is not None:
                            raise APIError(422, "Branch already exists")
                        self.head = data["sha"]
                        return None
                    if path.endswith("/blobs"):
                        payload = data["content"].encode()
                        value = {"content": base64.b64encode(payload).decode()}
                    elif path.endswith("/trees"):
                        entries = {} if "base_tree" not in data else {
                            item["path"]: item for item in self.objects[data["base_tree"]]["tree"]}
                        for item in data["tree"]:
                            entries[item["path"]] = dict(item)
                        value = {"truncated": False, "tree": list(entries.values())}
                    else:
                        value = dict(data, tree={"sha": data["tree"]})
                    identity = (hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
                                if path.endswith("/blobs") else hashlib.sha1(canonical(value)).hexdigest())
                    self.objects[identity] = value
                    return {"sha": identity}

            def graphql(self, query, variables):
                with self.lock:
                    return {"repository": {
                        key: {"oid": identity, "isTruncated": False,
                              "text": base64.b64decode(self.objects[identity]["content"]).decode()}
                        for key, identity in variables.items() if key.startswith("blob")}}
        api = GitAPI()
        fixtures, _ = phases(10)
        def write(item):
            name, state = item
            return State(api).update(name, lambda _: copy.deepcopy(state))
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(write, fixtures.entries.items()))
        api.seen.add(threading.get_ident())
        result = State(api).snapshot()[2]
        self.assertEqual(fixtures.entries, result)
