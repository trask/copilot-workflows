import copy
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from loop.cli import choose_live, main as cli_main, verify_pending
from loop.coordinator import dispatch
from loop.freeze import freeze
from loop.live import advance, start
from loop.policy import (CENTRAL, LOOP_KINDS, Rejected, check_target, eligible, pipeline_limit,
                         supported_checkpoint)
from loop.publication import PublisherAPI, authenticated_push
from loop.source import download_source, import_source, package_source, public_fetch
from loop.waiter import poll, wake
from tests.test_live import Read, TEST_TOKEN, live_state, stored
from tests.test_loop import BOT, FakeAPI, MemoryState, REVISION, pr
from tests.test_pr_tasks import task_request


class PublicTargetTests(unittest.TestCase):
    def test_central_repository_cannot_be_a_public_target_or_fork_for_any_kind(self):
        for kind in LOOP_KINDS:
            for side in ("base", "head"):
                for name in (CENTRAL, CENTRAL.upper()):
                    live = pr()
                    live[side]["repo"].update(id=99, full_name=name, private=False)
                    with self.subTest(kind=kind, side=side, name=name), \
                            self.assertRaisesRegex(Rejected, "Central automation repository"):
                        eligible(live, live["base"]["repo"]["full_name"], kind=kind)

    def test_frozen_central_targets_cannot_start_recheck_construct_publishers_or_push(self):
        for kind in LOOP_KINDS:
            for key in ("repo", "head_repo"):
                req = task_request(kind)
                req[key] = CENTRAL.upper()
                store, api = MemoryState(), Mock()
                for operation in (
                        lambda: pipeline_limit(req),
                        lambda: check_target(api, req),
                        lambda: start(store, api, req, "", 0, True, "fine_grained_pat",
                                      [], True, 100),
                        lambda: PublisherAPI(TEST_TOKEN, req, "fine_grained_pat"),
                        lambda: authenticated_push(".", req, "c" * 40, TEST_TOKEN)):
                    with self.subTest(kind=kind, key=key), \
                            patch("loop.publication.subprocess.run") as child, \
                            self.assertRaisesRegex(Rejected, "Central automation repository"):
                        operation()
                    child.assert_not_called()
                    self.assertEqual([], api.mock_calls)
                    self.assertEqual({}, store.entries)

    def test_publisher_api_cannot_read_or_mutate_the_central_repository(self):
        publisher = PublisherAPI(TEST_TOKEN, task_request("pr_description"), "fine_grained_pat")
        for method, path, data in (
                ("GET", f"repos/{CENTRAL}", None),
                ("GET", f"repos/{CENTRAL}/git/ref/heads/main", None),
                ("PATCH", f"repos/{CENTRAL}/git/refs/heads/main", {"sha": "c" * 40}),
                ("POST", f"repos/{CENTRAL}/actions/workflows/coordinator.yml/dispatches", {})):
            with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                publisher.authorize(path, method, data)

    def test_all_kinds_require_public_base_and_head_including_forks(self):
        for kind in LOOP_KINDS:
            live = pr()
            live["head"]["repo"].update(id=99, full_name="owner/public-fork")
            frozen = eligible(live, kind=kind)
            self.assertFalse(frozen["source_private"])
            self.assertFalse(frozen["target_private"])
            for side in ("base", "head"):
                for visibility in ("private", "internal", "missing", "unknown"):
                    changed = copy.deepcopy(live)
                    repository = changed[side]["repo"]
                    if visibility == "private":
                        repository["private"] = True
                    elif visibility == "missing":
                        repository.pop("private")
                    else:
                        repository["visibility"] = visibility
                    with self.subTest(kind=kind, side=side, visibility=visibility), \
                            self.assertRaises(Rejected):
                        eligible(changed, kind=kind)

    def test_private_freezes_stop_before_diff_reviews_logs_or_source_collection(self):
        for kind in LOOP_KINDS:
            for side in ("base", "head"):
                live = pr()
                live["head"]["repo"].update(id=99, full_name="owner/fork")
                live[side]["repo"]["private"] = True
                api = Mock()
                api.call.side_effect = lambda path: BOT if path.startswith("users/") else live
                with self.subTest(kind=kind, side=side), self.assertRaisesRegex(Rejected, "Only public"):
                    freeze(api, 1, REVISION, now=100, repo=live["base"]["repo"]["full_name"],
                           loop_kind=kind)
                api.pages.assert_not_called()
                api.graphql.assert_not_called()
                self.assertFalse(any("/git/" in call.args[0] or "/compare/" in call.args[0]
                                     for call in api.call.call_args_list))

    def test_frozen_private_and_unknown_visibility_cannot_start_or_recheck(self):
        for kind in LOOP_KINDS:
            for key in ("source_private", "target_private"):
                for value in (True, None, 0):
                    req = task_request(kind)
                    req[key] = value
                    store, api = MemoryState(), Mock()
                    with self.subTest(kind=kind, key=key, value=value):
                        with self.assertRaisesRegex(Rejected, "Only public"):
                            pipeline_limit(req)
                        with self.assertRaisesRegex(Rejected, "Only public"):
                            check_target(api, req)
                        api.assert_not_called()
                        api.call.assert_not_called()
                        with self.assertRaisesRegex(Rejected, "Only public"):
                            start(store, api, req, "", 0, True, "fine_grained_pat",
                                  [], True, 100)
                        self.assertEqual({}, store.entries)

    def test_visibility_drift_is_rejected_before_reading_the_new_private_diff(self):
        for kind in LOOP_KINDS:
            req = task_request(kind)
            read = Read(req)
            for side in ("base", "head"):
                read.pr[side]["repo"]["private"] = True
            with self.subTest(kind=kind), self.assertRaisesRegex(Rejected, "Only public"):
                check_target(read, req)

    def test_saved_private_checkpoints_cannot_dispatch_verify_publish_or_wake(self):
        for key in ("source_private", "target_private"):
            state = live_state(stage="verify_pending")
            state["request"][key] = True
            store, name = stored(state)
            before = copy.deepcopy(state)
            central, reader, publisher = Mock(), Mock(), Mock()
            args = SimpleNamespace(pr="1", request_id=state["request"]["request_id"],
                                   generation=str(state["generation"]), repo=state["request"]["repo"])
            for operation in (
                    lambda: supported_checkpoint(state),
                    lambda: dispatch(store, name, central, 100),
                    lambda: verify_pending(central, store, args),
                    lambda: choose_live(store, 100, central),
                    lambda: advance(store, name, state, central, reader, publisher, 100),
                    lambda: wake(central, state)):
                with self.subTest(key=key), self.assertRaisesRegex(Rejected, "Only public"):
                    operation()
                self.assertEqual(before, store.entries[name])
                self.assertEqual([], central.mock_calls)
                self.assertEqual([], reader.mock_calls)
                self.assertEqual([], publisher.mock_calls)
            with redirect_stderr(io.StringIO()) as errors:
                self.assertFalse(poll(central, store, 100, REVISION, {}))
            self.assertIn("Only public", errors.getvalue())
            self.assertEqual(before, store.entries[name])
            self.assertEqual([], central.mock_calls)

    def test_current_tick_cannot_route_private_requests_to_old_revision_code(self):
        state = live_state(stage="dispatched")
        state["request"]["source_private"] = True
        state["next_check_at"] = 0
        store, name = stored(state)
        api = FakeAPI()
        with patch("sys.argv", ["loop.cli", "tick", "--target",
                               state["request"]["repo"] + "#1"]), \
                patch("loop.cli.API", return_value=api), \
                patch("loop.cli.State", return_value=store), \
                patch("loop.cli.time.time", return_value=100), \
                patch.dict(os.environ, {"GITHUB_SHA": "f" * 40}), \
                patch("loop.waiter.wake") as old_revision, \
                self.assertRaisesRegex(Rejected, "Only public"):
            cli_main()
        old_revision.assert_not_called()
        self.assertEqual([], api.calls)
        self.assertEqual(state, store.entries[name])

    def test_an_unfrozen_label_cannot_bypass_the_frozen_private_request_guard(self):
        state = live_state(stage="dispatched")
        state["request"].update(source_private=True, freeze_status="not_frozen")
        api = Mock()
        with self.assertRaisesRegex(Rejected, "unfrozen gate"):
            wake(api, state)
        api.call.assert_not_called()

    def test_private_source_never_fetches_downloads_imports_or_creates_output(self):
        req = task_request("self_review")
        req["source_private"] = True
        api = Mock()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, "source")
            for operation in (
                    lambda: public_fetch(directory, req),
                    lambda: package_source(req, 1, api, destination),
                    lambda: download_source(api, {"request": req, "source": {"manifest": {}},
                                                 "generation": 1}, destination),
                    lambda: import_source(directory, destination / "source.bundle", {"generation": 1}, req)):
                with patch("loop.source.subprocess.run") as child, self.assertRaisesRegex(
                        Rejected, "Only public"):
                    operation()
                child.assert_not_called()
                self.assertEqual([], api.mock_calls)
                self.assertFalse(destination.exists())
