import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

from loop.api import APIError
from loop.cli import choose_live, main as cli_main, select_publisher
from loop.publisher_auth import publisher_secret
from loop.live import main as live_main
from loop.policy import AUTHOR_ID, CENTRAL, Rejected, checkpoint_name
from loop.publication import SECRET
from tests.test_live import FIXTURE, TEST_TOKEN, Read, live_state, personal_pr, stored
from tests.test_loop import FakeAPI, MemoryState, REVISION

MAPPING = json.dumps({
    FIXTURE.split("/")[0]: "TEST_PUBLISH_TOKEN",
    "organization": "OPENTELEMETRY_PUBLISH_TOKEN",
})
DISPATCH = {
    "GITHUB_REPOSITORY": CENTRAL, "GITHUB_REF": "refs/heads/main",
    "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR_ID": str(AUTHOR_ID),
    "GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": REVISION,
}


class PublisherCredentialTests(unittest.TestCase):
    def test_default_is_personal_only_and_explicit_mapping_has_no_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(SECRET, publisher_secret(CENTRAL.split("/")[0] + "/test"))
            self.assertEqual("", publisher_secret("organization/workflows"))
        with patch.dict(os.environ, {"PUBLISHER_SECRET_MAP": MAPPING}, clear=True):
            self.assertEqual("TEST_PUBLISH_TOKEN", publisher_secret(FIXTURE))
            self.assertEqual("OPENTELEMETRY_PUBLISH_TOKEN",
                             publisher_secret("Organization/workflows"))
            self.assertEqual("", publisher_secret("unconfigured/workflows"))
        with patch.dict(os.environ, {"PUBLISHER_SECRET_MAP": "{}"}, clear=True):
            self.assertEqual("", publisher_secret(FIXTURE))

    def test_mapping_rejects_ambiguous_unsafe_or_credential_values(self):
        invalid = [
            "[]", "null", "not json", '{"trask":"A_PUBLISH_TOKEN",'
            '"trask":"B_PUBLISH_TOKEN"}',
            json.dumps({"trask": SECRET, "Trask": SECRET}),
            json.dumps({"bad/owner": SECRET}),
            json.dumps({"-owner": SECRET}),
            json.dumps({"owner-": SECRET}),
            json.dumps({"owner": "GITHUB_TOKEN"}),
            json.dumps({"owner": "COPILOT_GITHUB_TOKEN"}),
            json.dumps({"owner": "REVIEW_LOOP_SOURCE_READ_TOKEN"}),
            json.dumps({"owner": TEST_TOKEN}),
            json.dumps({"owner": "BAD\n_PUBLISH_TOKEN"}),
            json.dumps({"owner": "PUBLISH_TOKEN"}),
            json.dumps({"owner": "1_PUBLISH_TOKEN"}),
            json.dumps({"owner": "test_PUBLISH_TOKEN"}),
            json.dumps({"owner": 17}),
            json.dumps({"owner": "A" * 100 + "_PUBLISH_TOKEN"}),
            json.dumps({f"owner{i}": SECRET for i in range(101)}),
            " " * 16385,
        ]
        for value in invalid:
            with self.subTest(mapping=value), patch.dict(
                    os.environ, {"PUBLISHER_SECRET_MAP": value}, clear=True), \
                    self.assertRaises(ValueError):
                publisher_secret(FIXTURE)
        for repo in (None, "owner", "owner/project\n"):
            with self.subTest(repo=repo), self.assertRaises(Rejected):
                publisher_secret(repo)

    def test_selection_uses_fork_head_owner_without_publisher_or_state_access(self):
        value = personal_pr()
        value["base"]["repo"].update(id=77, full_name="organization/workflows")
        api = Mock()
        api.call.return_value = value
        with patch.dict(os.environ, dict(DISPATCH, PUBLISHER_SECRET_MAP=MAPPING), clear=True), \
                patch("sys.argv", ["loop.cli", "select-publisher",
                                   "--target", "organization/workflows#1"]), \
                patch("loop.cli.API", return_value=api), \
                patch("loop.cli.State") as state, \
                patch("loop.cli.output") as output, \
                patch("loop.live.PublisherAPI") as publisher:
            cli_main()
        state.assert_not_called()
        publisher.assert_not_called()
        api.call.assert_called_once_with("repos/organization/workflows/pulls/1")
        self.assertEqual({"publisher_head_repo": FIXTURE,
                          "publisher_secret": "TEST_PUBLISH_TOKEN"},
                         dict(call.args for call in output.call_args_list))

    def test_read_access_gate_is_preserved_and_other_api_failures_are_not_hidden(self):
        for status in (401, 403, 404, 503):
            api = Mock()
            api.call.side_effect = APIError(status, "read rejected")
            with self.subTest(status=status), patch.dict(os.environ, DISPATCH, clear=True), \
                    patch("loop.cli.output") as output, patch("builtins.print") as log:
                if status == 503:
                    with self.assertRaises(APIError):
                        select_publisher(api, FIXTURE + "#1")
                    log.assert_not_called()
                else:
                    select_publisher(api, FIXTURE + "#1")
                    log.assert_called_once()
                output.assert_not_called()

    def test_selection_requires_fresh_owner_dispatch_and_eligible_pr(self):
        api = Mock()
        with patch.dict(os.environ, dict(DISPATCH, GITHUB_ACTOR_ID="1"), clear=True), \
                self.assertRaises(Rejected):
            select_publisher(api, FIXTURE + "#1")
        api.call.assert_not_called()
        for change in ("author", "number", "closed"):
            value = personal_pr()
            if change == "author":
                value["user"]["id"] = 1
            elif change == "number":
                value["number"] = 2
            else:
                value["state"] = "closed"
            api.call.return_value = value
            with self.subTest(change=change), patch.dict(os.environ, DISPATCH, clear=True), \
                    patch("loop.cli.output") as output, self.assertRaises(Rejected):
                select_publisher(api, FIXTURE + "#1")
            output.assert_not_called()

    def test_launch_rechecks_head_routing_and_missing_secret_blocks_before_inference(self):
        for head_repo, secret, available in (
                (FIXTURE, "TEST_PUBLISH_TOKEN", "true"),
                (FIXTURE, "TEST_PUBLISH_TOKEN", "false"),
                ("organization/workflows", "TEST_PUBLISH_TOKEN", "true"),
                (FIXTURE, "OPENTELEMETRY_PUBLISH_TOKEN", "true")):
            store = MemoryState()
            environment = dict(
                DISPATCH, PUBLISHER_SECRET_MAP=MAPPING, PUBLISHER_HEAD_REPO=head_repo,
                PUBLISHER_SECRET_NAME=secret, PUBLISHER_AVAILABLE=available,
                PUBLICATION_AUTH_MODE="fine_grained_pat", INFERENCE_AVAILABLE="true",
            )
            with self.subTest(head=head_repo, secret=secret), \
                    patch.dict(os.environ, environment, clear=True), \
                    patch("sys.argv", ["loop.cli", "launch",
                                       "--target", FIXTURE + "#1"]), \
                    patch("loop.cli.API", return_value=FakeAPI()), \
                    patch("loop.cli.State", return_value=store), \
                    patch("loop.cli.target_api", return_value=Read()), \
                    patch("loop.cli.dispatch") as dispatch, patch("loop.cli.stage_source") as source, \
                    patch("loop.cli.summary"), patch("loop.cli.time.time", return_value=100):
                if head_repo == FIXTURE and secret == "TEST_PUBLISH_TOKEN":
                    cli_main()
                    state = next(iter(store.entries.values()))
                    if available == "false":
                        self.assertEqual("blocked", state["stage"])
                        self.assertEqual("human_gate_target_repository_push_and_review_access",
                                         state["reason"])
                        dispatch.assert_not_called()
                    else:
                        self.assertEqual("ready", state["stage"])
                        dispatch.assert_called_once()
                    self.assertEqual(0, state["iteration"])
                else:
                    with self.assertRaisesRegex(Rejected, "routing changed"):
                        cli_main()
                    self.assertEqual({}, store.entries)
                    dispatch.assert_not_called()
                source.assert_not_called()

    def test_continuation_and_reconciliation_select_each_frozen_head_independently(self):
        for stage in ("publish_pending", "publication_intent", "waiting_review", "waiting_ci"):
            for head_repo, expected in (
                    (FIXTURE, "TEST_PUBLISH_TOKEN"),
                    ("organization/workflows", "OPENTELEMETRY_PUBLISH_TOKEN"),
                    ("unconfigured/workflows", "")):
                state = live_state(stage=stage)
                state["request"]["head_repo"] = head_repo
                store, name = stored(state)
                with self.subTest(stage=stage, head=head_repo), patch.dict(
                        os.environ, {"PUBLISHER_SECRET_MAP": MAPPING}, clear=True), \
                        patch("loop.cli.output") as output:
                    self.assertEqual(name, choose_live(store, 100))
                outputs = dict(call.args for call in output.call_args_list)
                self.assertEqual(expected, outputs["live_publisher_secret"])
                self.assertEqual(stage, outputs["live_stage"])
                self.assertEqual(state, store.entries[name])

    def test_live_job_only_accepts_selected_owner_secret_and_never_falls_back(self):
        for head_repo, secret, token, expected_error in (
                (FIXTURE, "TEST_PUBLISH_TOKEN", TEST_TOKEN, None),
                ("organization/workflows", "OPENTELEMETRY_PUBLISH_TOKEN", TEST_TOKEN, None),
                (FIXTURE, "OPENTELEMETRY_PUBLISH_TOKEN", TEST_TOKEN, "selection differs"),
                (FIXTURE, "", TEST_TOKEN, "selection differs"),
                ("organization/workflows", "OPENTELEMETRY_PUBLISH_TOKEN", "",
                 "human_gate_OPENTELEMETRY_PUBLISH_TOKEN"),
                ("unconfigured/workflows", "", TEST_TOKEN,
                 "human_gate_publisher_secret_for_head_owner")):
            state = live_state()
            state["request"]["head_repo"] = head_repo
            store, name = stored(state)
            environment = dict(
                DISPATCH, PUBLISHER_SECRET_MAP=MAPPING, PUBLISHER_SECRET_NAME=secret,
                PUBLISHER_TOKEN=token, PR="1", REQUEST_ID=state["request"]["request_id"],
                GENERATION="6", EXPECTED_STAGE=state["stage"], TARGET_REPO=FIXTURE,
            )
            with self.subTest(head=head_repo, secret=secret, error=expected_error), \
                    patch.dict(os.environ, environment, clear=True), \
                    patch("loop.live.API"), patch("loop.live.State", return_value=store), \
                    patch("loop.live.git", return_value=REVISION.encode()), \
                    patch("loop.live.PublisherAPI") as publisher, \
                    patch("loop.live.advance", return_value=copy.deepcopy(state)) as advance, \
                    patch("loop.cli.summary"), patch("loop.live.time.time", return_value=100):
                if expected_error:
                    with self.assertRaisesRegex(Rejected, expected_error):
                        live_main()
                    publisher.assert_not_called()
                    advance.assert_not_called()
                    self.assertEqual("blocked", store.entries[name]["stage"])
                    self.assertIn(expected_error, store.entries[name]["error"])
                else:
                    live_main()
                    publisher.assert_called_once_with(token, state["request"], "fine_grained_pat",
                                                      source_write=True)
                    publisher.return_value.identity.assert_called_once()
                    advance.assert_called_once()

    def test_concurrent_owner_phases_do_not_share_secret_selection(self):
        personal = live_state()
        store, personal_name = stored(personal)
        organization = live_state()
        organization["request"].update(
            repo="organization/workflows", repo_id=77, head_repo="organization/workflows",
            head_repo_id=77, request_id="e" * 32,
        )
        organization_name = checkpoint_name("organization/workflows", 1, 77)
        store.entries[organization_name] = organization
        for name, secret in ((personal_name, "TEST_PUBLISH_TOKEN"),
                             (organization_name, "OPENTELEMETRY_PUBLISH_TOKEN")):
            with self.subTest(checkpoint=name), patch.dict(
                    os.environ, {"PUBLISHER_SECRET_MAP": MAPPING}, clear=True), \
                    patch("loop.cli.output") as output:
                self.assertEqual(name, choose_live(store, 100, only=name))
            outputs = dict(call.args for call in output.call_args_list)
            self.assertEqual(secret, outputs["live_publisher_secret"])
            self.assertEqual(store.entries[name]["request"]["request_id"], outputs["live_request"])
        self.assertEqual(personal, store.entries[personal_name])
        self.assertEqual(organization, store.entries[organization_name])
