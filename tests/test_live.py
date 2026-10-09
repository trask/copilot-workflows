from tests.support import reconstruct
from tests.fixtures import (FIXTURE, REPOSITORIES, REPO_NODE, ROOT_PATH,
                            CI_CHECK, CI_WORKFLOW_BLOB, CI_WORKFLOW_ID, CI_WORKFLOW_PATH)
import copy
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from loop.api import API, APIError
from loop.cli import choose_live, main as cli_main
from loop.coordinator import (cancel, checkpoint, dispatch, record_result)
from loop.freeze import freeze
from loop.live import (advance, cas, confirm_push, confirm_request, guard,
                       authorize_publication_reconciliation,
                       new_review_intent, observed_review_request, publish, request_review,
                       main as live_main, publication_invocation, start, watch_review)
from loop.policy import (AUTHOR_ID, CENTRAL, Rejected, canonical, checkpoint_name, digest, iso, eligible)
from loop.publication import (PROFILE, PublisherAPI, acceptance, authenticated_push, bound_artifact, evidence, import_candidate, plan, read_package)
from loop.publication import SECRET
from loop.reviews import (body_classification, exact_ci, fresh_collection,
                          inline_fingerprints, missed_fingerprints)
from loop.verify import (git, verify)
from loop.revisions import revision_ref
from tests.test_loop import BOT, FakeAPI, MemoryState, REVISION, SHA, pr, request, result, review, run

CLEAN = """<!-- ccr-overview-v2 -->

## Copilot review overview

### \U0001f7e2 Approval recommended

The workflow configuration matches the pinned setup action's supported LTS selection and the path filters cover the intended files.

**Review effort:** Balanced\u0020\u0020
**Findings:** None
"""
HUMAN_REVIEW = """<!-- ccr-overview-v2 -->

## Copilot review overview

### \U0001f535 Needs a closer look

Cross-run force-cancellation depends on delivery coverage and changing job state, requiring final human validation.

**Review effort:** Balanced\u0020\u0020
**Findings:** None
"""
RESOLVED = """
<details>
<summary><strong>Resolved since last review (1)</strong></summary>

- <picture><source media="(prefers-color-scheme: dark)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-dark.svg"><source media="(prefers-color-scheme: light)" srcset="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.svg"><img src="https://github.githubassets.com/static/images/icons/copilot-code-review/high-v2-light.png" alt="High severity" width="62" height="18" align="texttop"></picture> [Loop omits the final element](#discussion_r20)
</details>"""
CURRENT_CLEAN = """<!-- ccr-overview-v2 -->

### \U0001f7e2 Approval recommended

The breaking behavior, metadata, dependencies, documentation, and regression coverage are consistent and complete.

**0 open findings**

\U0001f9e0 **Review effort:** Balanced"""
CURRENT_RESOLVED = RESOLVED.replace(
    "Resolved since last review (1)", "1 resolved since last review")
CURRENT_CLEAN_RESOLVED = CURRENT_CLEAN.replace(
    "\n\n\U0001f9e0", "\n" + CURRENT_RESOLVED + "\n\n\U0001f9e0")
CURRENT_NONCLEAN = """<!-- ccr-overview-v2 -->

### \U0001f7e1 Changes recommended

Raw MapMessage keys need empty-key handling to prevent valid log messages from being dropped.

<details open>
<summary><strong>1 open finding</strong></summary>

- [Skip empty map keys before creating OpenTelemetry attributes](#discussion_r21) \u00b7 New
</details>

\U0001f9e0 **Review effort:** Balanced"""
NONCLEAN = """<!-- ccr-overview-v2 -->
## Copilot review overview
### Changes recommended
**Findings:** 1
<details><summary><strong>Open (1)</strong></summary>
[Include the final array element in the sum](#discussion_r4157376500)
</details>
"""
MISSED = """
<details>
<summary><strong>Previously missed (1)</strong></summary>

In code that hasn't changed since last review

<details>
<summary>Backoff retries need a wake-up</summary>

`queue.py:10`

Schedule a wake-up at the earliest notBefore time.
</details>
</details>
"""
TEST_TOKEN = "github_pat_" + "test_only_not_a_real_token" * 2


def personal_pr(sha=SHA):
    value = pr()
    value["head"]["repo"].update(id=REPOSITORIES[FIXTURE], node_id=REPO_NODE, full_name=FIXTURE)
    value["base"]["repo"].update(id=REPOSITORIES[FIXTURE], node_id=REPO_NODE, full_name=FIXTURE)
    value["head"]["sha"] = sha
    value["requested_reviewers"] = []
    return value


def personal_request(sha=SHA, max_pipelines=5):
    value = dict(request(), **eligible(personal_pr(sha), FIXTURE))
    value.update(
                 budgets=dict(request()["budgets"], max_iterations=max_pipelines),
                 source_private=False, frozen_sha=sha, mode="publish", publication={
                     "profile": PROFILE, "auth_mode": "fine_grained_pat", "generation": 6,
                     "authorized_actor_id": AUTHOR_ID,
                     "required_checks": [CI_CHECK], "phase": "1" * 32,
                     "authorized_at": 100,
                     "max_pipelines": max_pipelines,
                 })
    value["findings"] = [{
        "key": "review:12", "kind": "body", "review_id": 12, "comment_id": None,
        "path": None, "line": None, "body": NONCLEAN,
    }, {
        "key": "inline:20", "kind": "inline", "review_id": 12, "comment_id": 20,
        "review_commit_id": sha, "review_submitted_at": iso(20),
        "comment_commit_id": sha, "original_commit_id": sha, "original_line": 8,
        "root_author": {key: BOT[key] for key in ("id", "node_id", "type")},
        "thread_id": "PRRT_test", "path": ROOT_PATH, "line": 8, "body": "Omitted last element",
    }]
    from loop.effects import conversation
    value["findings"][1]["thread_context"] = conversation([{
        "id": 20, "body": "Omitted last element", "user": BOT}], 20)
    return value


def live_state(req=None, stage="publish_pending"):
    req = personal_request() if req is None else req
    state = checkpoint(req)
    state.update(stage=stage, generation=6, iteration=1,
                 effects=[], publications=[], seen_findings=[], next_check_at=100,
                 run={"id": 24, "attempt": 1}, verification_run={"id": 99, "attempt": 1})
    return state


def stored(state):
    store = MemoryState()
    name = checkpoint_name(state["request"]["repo"], state["request"]["pr"],
                           state["request"]["repo_id"])
    store.entries[name] = copy.deepcopy(state)
    return store, name


class Read:
    def __init__(self, req=None):
        self.req = req or personal_request()
        self.pr = personal_pr(self.req["frozen_sha"])
        self.reviews = [review(body=NONCLEAN, commit_id=self.req["frozen_sha"], submitted_at=iso(20))]
        self.comments = [{
            "id": 20, "user": BOT, "pull_request_review_id": 12,
            "commit_id": self.req["frozen_sha"], "path": ROOT_PATH, "line": 8,
            "original_commit_id": self.req["frozen_sha"], "original_line": 8,
            "body": "Omitted last element",
            "pull_request_url": f"https://api.github.com/repos/{FIXTURE}/pulls/1",
        }]
        self.runs = [{
            "id": 200, "head_sha": self.pr["head"]["sha"], "event": "push",
            "path": CI_WORKFLOW_PATH, "run_attempt": 1,
            "workflow_id": CI_WORKFLOW_ID, "run_number": 1,
            "repository": {"id": REPOSITORIES[FIXTURE], "full_name": FIXTURE},
            "head_repository": {"id": REPOSITORIES[FIXTURE], "full_name": FIXTURE},
            "actor": {"id": AUTHOR_ID, "type": "User"}, "status": "completed", "conclusion": "success",
        }]
        self.resolved = False
        self.outdated = False
        self.ref_sha = None
        self.commit_tree = "e" * 40
        self.commit_parent = self.req["frozen_sha"]
        self.checks = [{
            "id": 333, "head_sha": self.pr["head"]["sha"], "name": CI_CHECK,
            "app": {"id": 15368, "slug": "github-actions"},
            "status": "completed", "conclusion": "success",
        }]
        self.statuses = []

    def call(self, path, *_args):
        if "/git/ref/heads/" in path:
            return {"object": {"sha": self.ref_sha or self.pr["head"]["sha"]}}
        if "/git/commits/" in path:
            return {"sha": path.rsplit("/", 1)[1], "tree": {"sha": self.commit_tree},
                    "parents": [{"sha": self.commit_parent}]}
        if path.startswith(f"repos/{FIXTURE}/git/trees/"):
            return {"truncated": False, "tree": [
                {"path": CI_WORKFLOW_PATH, "type": "blob", "mode": "100644",
                 "sha": CI_WORKFLOW_BLOB}]}
        if path == f"repos/{FIXTURE}/pulls/1":
            return copy.deepcopy(self.pr)
        if path == f"repos/{FIXTURE}/pulls/comments/20":
            return copy.deepcopy(self.comments[0])
        if path == "users/copilot-pull-request-reviewer%5Bbot%5D":
            return BOT.copy()
        raise AssertionError(path)

    def pages(self, path, key=None):
        if path.split("?", 1)[0].endswith("/jobs"):
            return [{"id": 300, "name": CI_CHECK, "conclusion": "success",
                     "check_run_url": f"https://api.github.com/repos/{FIXTURE}/check-runs/333"}]
        if path.startswith(f"repos/{FIXTURE}/actions/runs"):
            return copy.deepcopy(self.runs)
        if path.endswith("/reviews"):
            return copy.deepcopy(self.reviews)
        if path.endswith("/comments"):
            return copy.deepcopy(self.comments)
        if "/check-runs" in path:
            return copy.deepcopy(self.checks)
        if path.endswith("/status"):
            return copy.deepcopy(list({s["context"]: s for s in reversed(self.statuses)}.values()))
        if path.endswith("/statuses"):
            return copy.deepcopy(self.statuses)
        raise AssertionError(path)

    def graphql(self, query, variables):
        return {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"id": "PRRT_test", "isResolved": self.resolved, "isOutdated": self.outdated,
                       "comments": {"nodes": [{"databaseId": 20}]}}],
        }}}}


class Publisher(PublisherAPI):
    def __init__(self, read, token=TEST_TOKEN):
        super().__init__(token, read.req, "fine_grained_pat")
        self.read = read
        self.posts = []
        self.uncertain = False
        self.error = None

    def identity(self, req):
        self.assertion_identity = req["frozen_sha"]
        return {"auth_mode": "fine_grained_pat", "actor_id": AUTHOR_ID,
                "repo_id": REPOSITORIES[FIXTURE], "repo_node": REPO_NODE}

    def call(self, path, method="GET", data=None, **_kwargs):
        self.authorize(path, method, data)
        if method == "GET":
            return self.read.call(path)
        self.posts.append((path, method, copy.deepcopy(data)))
        if self.error is not None:
            raise self.error
        if self.uncertain:
            raise APIError(503, "Simulated interrupted response")
        if path.endswith("/requested_reviewers"):
            self.read.pr["requested_reviewers"] = [BOT.copy()]
        elif path.endswith("/replies"):
            reply = {"id": max(comment["id"] for comment in self.read.comments) + 1,
                     "in_reply_to_id": int(path.split("/")[-2]), "body": data["body"],
                     "pull_request_review_id": 12, "commit_id": self.read.pr["head"]["sha"],
                     "user": {"id": AUTHOR_ID, "type": "User", "node_id": "owner"},
                     "pull_request_url": f"https://api.github.com/repos/{self.repo}/pulls/{self.number}"}
            self.read.comments.append(reply)
            return copy.deepcopy(reply)
        elif path == "graphql":
            self.read.resolved = True
            return {"data": {"resolveReviewThread": {
                "clientMutationId": data["variables"]["claim"],
                "thread": {"id": data["variables"]["thread"], "isResolved": True}}}}
        return None

    def pages(self, path, key=None):
        return self.read.pages(path, key)

    def graphql(self, query, variables):
        return self.read.graphql(query, variables)


def zipped(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


class ProtocolTests(unittest.TestCase):

    def test_personal_phase_requires_fresh_owner_dispatch_on_central_main(self):
        env = {"GITHUB_REPOSITORY": CENTRAL, "GITHUB_REF": "refs/heads/main",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR_ID": str(AUTHOR_ID),
               "GITHUB_RUN_ATTEMPT": "1"}
        with patch.dict(os.environ, env):
            publication_invocation()
        for key, value in [("GITHUB_REPOSITORY", FIXTURE), ("GITHUB_REF", "refs/heads/feature"),
                           ("GITHUB_EVENT_NAME", "schedule"), ("GITHUB_ACTOR_ID", "999"),
                           ("GITHUB_RUN_ATTEMPT", "2")]:
            with self.subTest(key=key), patch.dict(os.environ, dict(env, **{key: value})), \
                    self.assertRaises(Rejected):
                publication_invocation()

    def test_new_phase_archives_without_reusing_old_result_and_missing_auth_dispatches_nothing(self):
        old = live_state(stage="blocked")
        old["request"]["mode"] = "shadow"
        old["request"].pop("publication")
        old.update(generation=5, iteration=2, intent={"id": old["request"]["request_id"]},
                   reason="validation_unqualified", report={"old": "evidence"})
        store, name = stored(old)
        new = dict(personal_request(max_pipelines=5), request_id="e" * 32, workflow_revision="f" * 40)
        _, state = start(store, FakeAPI([run()]), new, old["request"]["request_id"], 5,
                         False, "fine_grained_pat", [], True, 100)
        self.assertEqual("blocked", state["stage"])
        self.assertEqual("human_gate_target_repository_push_and_review_access", state["reason"])
        self.assertEqual(6, state["generation"])
        self.assertEqual(0, state["iteration"])
        self.assertIsNone(state["report"])
        self.assertIsNone(state["run"])
        self.assertIsNone(state["intent"])
        self.assertEqual(5, state["request"]["budgets"]["max_iterations"])
        self.assertEqual(5, state["request"]["publication"]["max_pipelines"])
        self.assertEqual({"old": "evidence"}, old["report"])
        self.assertEqual(state, store.entries[name])

    def test_active_changed_wrong_repo_and_uncertain_publication_cannot_start_another_phase(self):
        for mutation in ["active", "generation", "repo", "uncertain"]:
            old = live_state(stage="blocked")
            old["request"]["mode"] = "shadow"
            old.update(generation=5, intent=None, run=None)
            store, _ = stored(old)
            new = dict(personal_request(max_pipelines=5), request_id="e" * 32, workflow_revision="f" * 40)
            generation = 5
            if mutation == "active":
                store.entries[checkpoint_name(FIXTURE, 1, REPOSITORIES[FIXTURE])]["stage"] = "running"
            elif mutation == "generation":
                generation = 4
            elif mutation == "repo":
                new["repo"] = "trask/other"
            elif mutation == "uncertain":
                store.entries[checkpoint_name(FIXTURE, 1, REPOSITORIES[FIXTURE])]["intent"] = {"id": "d" * 32}
            else:
                new["workflow_revision"] = REVISION
            with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                start(store, FakeAPI(), new, "d" * 32, generation, True,
                      "fine_grained_pat", [CI_CHECK], True, 100)

    def test_manual_rerun_of_a_stopped_gate_is_a_new_authorized_phase(self):
        state = live_state(stage="blocked")
        state.update(reason="human_gate_TRASK_PUBLISH_TOKEN", iteration=0,
                     intent=None, run=None)
        store, _ = stored(state)
        new = dict(personal_request(max_pipelines=5), request_id="e" * 32)
        _, ready = start(store, FakeAPI(), new, "d" * 32, 6, True,
                         "fine_grained_pat", [CI_CHECK], True, 100)
        self.assertEqual("ready", ready["stage"])
        self.assertEqual(5, ready["request"]["budgets"]["max_iterations"])
        self.assertEqual(state["request"]["budgets"], ready["request"]["budgets"])
        self.assertNotEqual(state["request"]["publication"]["phase"],
                            ready["request"]["publication"]["phase"])
        self.assertEqual(5, ready["request"]["publication"]["max_pipelines"])
        state["request"]["publication"]["required_checks"] = []
        state["reason"] = "human_gate_target_repository_read_access"
        store, _ = stored(state)
        _, configured = start(store, FakeAPI(), new, "d" * 32, 6, True,
                              "fine_grained_pat", [CI_CHECK], True, 100)
        self.assertEqual([CI_CHECK], configured["request"]["publication"]["required_checks"])
        self.assertEqual(5, configured["request"]["publication"]["max_pipelines"])
        store, _ = stored(state)
        _, delayed = start(store, FakeAPI(), new, "d" * 32, 6, True,
                           "fine_grained_pat", [CI_CHECK], True, 3 * 86400)
        self.assertEqual("ready", delayed["stage"])
        self.assertEqual(3 * 86400, delayed["request"]["publication"]["authorized_at"])
        for key, value in [("effects", [{"uncertain": True}]),
                           ("publication_intent", {"status": "uncertain"}),
                           ("capability", {"probe": True})]:
            store, _ = stored(dict(state, **{key: value}))
            with self.subTest(key=key), self.assertRaises(Rejected):
                start(store, FakeAPI(), new, "d" * 32, 6, True,
                      "fine_grained_pat", [CI_CHECK], True, 100)

    def test_publication_launch_uses_existing_review_without_publisher_preflight(self):
        environment = {
            "GITHUB_REPOSITORY": CENTRAL, "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR_ID": str(AUTHOR_ID),
            "GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": REVISION,
            "PUBLICATION_AUTH_MODE": "fine_grained_pat",
            "PUBLISHER_AVAILABLE": "true", "INFERENCE_AVAILABLE": "true",
            "PUBLISHER_HEAD_REPO": FIXTURE, "PUBLISHER_SECRET_NAME": SECRET,
            "PUBLISHER_SECRET_MAP": json.dumps({FIXTURE.split("/")[0]: SECRET}),
        }
        for private in (False, True):
            read = Read()
            read.reviews[0]["body"] = "Existing Copilot finding without a capability-summary format"
            read.pr["requested_reviewers"] = [BOT.copy()]
            read.pr["head"]["repo"]["private"] = read.pr["base"]["repo"]["private"] = private
            store, api = MemoryState(), FakeAPI()
            original = api.call
            def newer_main(path, method="GET", data=None):
                if path.endswith("/heads/main"):
                    return {"object": {"sha": "f" * 40}}
                return original(path, method, data)
            api.call = newer_main
            with self.subTest(private=private), patch.dict(os.environ, environment, clear=True), \
                    patch("sys.argv", ["loop.cli", "launch", "--target", FIXTURE + "#1"]), \
                    patch("loop.cli.API", return_value=api), \
                    patch("loop.cli.State", return_value=store), \
                    patch("loop.cli.target_api", return_value=read), \
                    patch("loop.cli.stage_source") as source, patch("loop.cli.summary"), \
                    patch("loop.live.PublisherAPI") as publisher, \
                    patch("loop.live.authenticated_push") as push, \
                    patch("loop.cli.time.time", return_value=100):
                if private:
                    with self.assertRaisesRegex(Rejected, "Only public"):
                        cli_main()
                    self.assertEqual({}, store.entries)
                    self.assertEqual([], api.calls)
                    source.assert_not_called()
                    publisher.assert_not_called()
                    push.assert_not_called()
                    continue
                cli_main()
            publisher.assert_not_called()
            push.assert_not_called()
            state = next(iter(store.entries.values()))
            self.assertEqual("dispatched", state["stage"])
            self.assertEqual(REVISION, state["request"]["workflow_revision"])
            self.assertEqual(revision_ref(REVISION), state["request"]["workflow_ref"])
            self.assertEqual({"review:12", "inline:20"},
                             {finding["key"] for finding in state["request"]["findings"]})
            self.assertEqual(read.reviews[0]["body"], state["request"]["findings"][0]["body"])
            self.assertEqual([12], state["request"]["baseline_review_ids"])
            self.assertNotIn("capability", state)
            self.assertNotIn("capability_probe", state)
            source.assert_not_called()
            posts = [path for path, method, _ in api.calls if method == "POST"]
            self.assertEqual([f"repos/{CENTRAL}/git/refs",
                f"repos/{CENTRAL}/actions/workflows/copilot-worker.lock.yml/dispatches"], posts)

    def test_review_request_confirmation_never_reissues_an_uncertain_request(self):
        state = live_state(stage="review_request_intent")
        intent = {"recorded_at": 100, "sha": SHA, "baseline_review_ids": [12],
                  "baseline_run_ids": [200], "status": "uncertain"}
        state["review_request"] = intent
        store, name = stored(state)
        read = Read()
        publisher = Publisher(read)
        waiting = advance(store, name, state, None, read, publisher, 200)
        self.assertEqual("review_request_intent", waiting["stage"])
        stopped = advance(store, name, waiting, None, read, publisher, 1000)
        self.assertEqual("blocked", stopped["stage"])
        self.assertEqual("review_request_uncertain_no_retry", stopped["reason"])
        self.assertEqual([], publisher.posts)
        store, name = stored(state)
        read.pr["requested_reviewers"] = [BOT.copy()]
        confirmed = confirm_request(store, name, state, read, intent, 200)
        self.assertEqual("waiting_review", confirmed["stage"])
        self.assertEqual([12], confirmed["review_request"]["baseline_review_ids"])
        self.assertEqual("requested_reviewer", confirmed["review_request"]["confirmation"]["kind"])

    def test_later_review_request_failure_preserves_publication_and_never_pushes_again(self):
        state = live_state(stage="threads_settled")
        state["publications"] = [{"sha": SHA, "effect": "no_change"}]
        environment = {
            "PR": "1", "REQUEST_ID": state["request"]["request_id"], "GENERATION": "6",
            "GITHUB_SHA": REVISION, "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "99",
            "EXPECTED_STAGE": "threads_settled", "PUBLISHER_TOKEN": TEST_TOKEN, "TARGET_REPO": FIXTURE,
            "PUBLISHER_SECRET_NAME": SECRET,
            "PUBLISHER_SECRET_MAP": json.dumps({FIXTURE.split("/")[0]: SECRET}),
        }
        for status in (403, 422, 503):
            read = Read()
            publisher = Publisher(read)
            publisher.error = APIError(status, "Copilot review request rejected")
            store, name = stored(state)
            with self.subTest(status=status), patch.dict(os.environ, environment, clear=True), \
                    patch("loop.live.API"), patch("loop.live.State", return_value=store), \
                    patch("loop.live.PublisherAPI", return_value=publisher), \
                    patch("loop.live.git", return_value=REVISION.encode()), \
                    patch("loop.live.time.time", return_value=100), \
                    patch("loop.live.authenticated_push") as push, self.assertRaises(APIError):
                live_main()
            push.assert_not_called()
            result = store.entries[name]
            self.assertEqual("review_request_intent" if status == 503 else "blocked", result["stage"])
            self.assertEqual("live_operation_requires_reconciliation" if status == 503
                             else "publisher_permission_rejected", result["reason"])
            self.assertIn("APIError", result["error"])
            self.assertEqual(state["publications"], result["publications"])
            self.assertEqual(SHA, result["expected_sha"])
            self.assertEqual(1, len(publisher.posts))
            self.assertEqual("uncertain", result["review_request"]["status"])
            if status == 503:
                waiting = advance(store, name, result, None, read, publisher, 400)
                self.assertEqual("review_request_intent", waiting["stage"])
            else:
                with patch("loop.cli.output") as selected:
                    choose_live(store, 400)
                selected.assert_not_called()
                self.assertEqual(result, store.entries[name])
            self.assertEqual(1, len(publisher.posts))

    def test_copilot_workflow_confirmation_uses_exact_head_bot_path_and_new_run(self):
        state = live_state()
        intent = {"recorded_at": 100, "sha": SHA, "baseline_review_ids": [12], "baseline_run_ids": []}
        read = Read()
        valid = {"id": 77, "head_sha": SHA, "actor": BOT.copy(), "event": "dynamic",
                 "path": "dynamic/agents/copilot-pull-request-reviewer",
                 "created_at": iso(100), "status": "in_progress"}
        read.runs = [valid]
        self.assertEqual(77, observed_review_request(read, state, intent)["run_id"])
        read.pr["head"]["sha"] = REVISION
        with self.assertRaises(Rejected):
            observed_review_request(read, state, intent)
        self.assertEqual(77, observed_review_request(
            read, state, intent, allow_head_change=True)["run_id"])
        read.pr["head"]["sha"] = SHA
        for key, value in [("head_sha", REVISION), ("event", "workflow_dispatch"),
                           ("path", ".github/workflows/fake.yml"),
                           ("actor", dict(BOT, id=999)), ("created_at", iso(99))]:
            read.runs = [dict(valid, **{key: value})]
            self.assertIsNone(observed_review_request(read, state, intent))
        read.runs = [valid, dict(valid, id=78)]
        with self.assertRaises(Rejected):
            observed_review_request(read, state, intent)

    def test_existing_request_or_active_review_cannot_be_requested_again(self):
        for pending in ("requested_reviewer", "active_workflow"):
            state, read = live_state(stage="threads_settled"), Read()
            state["publications"] = [{"sha": SHA, "effect": "no_change"}]
            if pending == "requested_reviewer":
                read.pr["requested_reviewers"] = [BOT]
            else:
                read.runs = [{"actor": BOT, "status": "in_progress",
                              "path": "dynamic/agents/copilot-pull-request-reviewer"}]
            store, name = stored(state)
            publisher = Publisher(read)
            with self.subTest(pending=pending):
                self.assertIsNone(new_review_intent(read, state, 100))
                waiting = advance(store, name, state, None, read, publisher, 100)
                self.assertEqual(dict(state, reason="existing_Copilot_review_pending",
                                      next_check_at=400), waiting)
                self.assertEqual([], publisher.posts)
                self.assertNotIn("review_request", waiting)
                read.pr["requested_reviewers"] = []
                read.runs = []
                with patch("loop.live.time.time", return_value=400):
                    resumed = advance(store, name, waiting, None, read, publisher, 400)
                self.assertEqual("waiting_review", resumed["stage"])
                self.assertEqual(1, len(publisher.posts))
                self.assertEqual("confirmed", resumed["review_request"]["status"])
                self.assertEqual(state["publications"], resumed["publications"])

    def test_cancellation_and_stale_head_prevent_new_side_effect(self):
        state = live_state()
        store, name = stored(state)
        cancel(store, name, "d" * 32, 6, 100)
        with self.assertRaises(Rejected):
            guard(store, name, state, Read(), 100)
        store, name = stored(state)
        read = Read()
        read.pr["head"]["sha"] = REVISION
        with self.assertRaises(Rejected):
            guard(store, name, state, read, 100)
        with self.assertRaises(Rejected):
            cas(store, name, dict(state, generation=99), stage="ready")

    def test_live_stages_remain_selectable_after_days(self):
        from loop.live import STAGES
        for stage in STAGES:
            state = live_state(stage=stage)
            store, name = stored(state)
            with patch("loop.cli.output") as output, patch("loop.cli.publisher_secret", return_value=SECRET):
                self.assertEqual(name, choose_live(store, 3 * 86400))
            self.assertEqual(state, store.entries[name])
            self.assertIn(("live", "true"), [call.args for call in output.call_args_list])



class AcceptanceTests(unittest.TestCase):




    def test_acknowledged_push_waits_for_ref_PR_convergence_without_reissuing(self):
        state, _ = self.context()
        candidate = state["report"]["candidate"]
        state.update(stage="publication_intent", publication_intent={
            "candidate": candidate, "acceptance": {"profile": PROFILE},
            "recorded_at": 90, "status": "acknowledged", "acknowledged_at": 100})
        store, name = stored(state)
        read = Read()
        read.ref_sha = candidate["commit"]
        with patch("loop.live.authenticated_push") as push:
            waiting = confirm_push(store, name, state, read, 200)
            self.assertEqual("publication_intent", waiting["stage"])
            self.assertEqual("publication_API_propagation", waiting["reason"])
            read.pr["head"]["sha"] = candidate["commit"]
            result = confirm_push(store, name, waiting, read, 500)
            self.assertEqual("published", result["stage"])
            push.assert_not_called()
        read.pr["head"]["sha"] = SHA
        store, name = stored(state)
        self.assertEqual("publication_API_propagation_timeout",
                         confirm_push(store, name, state, read, 1000)["reason"])
        read.pr["head"]["sha"] = REVISION
        store, name = stored(state)
        self.assertEqual("publication_ref_PR_disagree",
                         confirm_push(store, name, state, read, 200)["reason"])
        store, name = stored(state)
        with patch.object(read, "call", side_effect=APIError(503, "Propagation read failed")):
            with self.assertRaises(APIError):
                confirm_push(store, name, state, read, 200)
        self.assertEqual(state, store.entries[name])

    def test_publication_after_days_still_confirms_the_exact_candidate(self):
        state, _ = self.context()
        candidate = state["report"]["candidate"]
        store, name = stored(state)
        read = Read()
        publisher = Publisher(read)
        def pushed(*_args):
            read.pr["head"]["sha"] = candidate["commit"]
        with patch("loop.live.evidence", return_value=({"profile": PROFILE}, candidate)), \
                patch("loop.live.authenticated_push", side_effect=pushed) as push, \
                patch("loop.live.time.time", return_value=3 * 86400):
            published = advance(store, name, state, Mock(), read, publisher, 3 * 86400)
        push.assert_called_once()
        self.assertEqual("published", published["stage"])
        self.assertEqual("confirmed", published["publication_intent"]["status"])
        self.assertEqual(candidate["commit"], published["expected_sha"])
        self.assertEqual(3 * 86400, published["publications"][0]["confirmed_at"])

    def test_explicit_recovery_only_confirms_exact_existing_published_intent(self):
        self.recovery_case()

    def restart_case(self):
        state, manifest = self.context()
        state.update(stage="blocked", reason="blocked_uncertain_publication_requires_operator",
                     intent={"id": "d" * 32},
                     coordinator_run={"id": 100, "attempt": 1, "revision": REVISION},
                     source_claim={"run_id": 77, "run_attempt": 1})
        state["request"]["launch_run"] = {"id": 77, "attempt": 1, "actor_id": AUTHOR_ID}
        state["report"]["request_digest"] = manifest["request_digest"] = digest(state["request"])
        state["report"]["dispositions"]["request_digest"] = digest(state["request"])
        state["publication_intent"] = {
            "candidate": state["report"]["candidate"], "status": "uncertain", "recorded_at": 100,
            "owner": {"run_id": "88", "attempt": "1"},
            "authorization": {"request_id": "d" * 32, "generation": 6, "expected_sha": SHA,
                              "candidate_commit": state["report"]["candidate"]["commit"]},
            "acceptance": acceptance(state, manifest)}
        server = {number: dict(run(), id=number, path=".github/workflows/coordinator.yml",
                               head_branch="main", event="workflow_dispatch", updated_at=iso(300))
                  for number in (77, 88, 99, 100)}
        server[88]["conclusion"] = "failure"
        server[24] = run()
        central = Mock()
        refs = FakeAPI()
        def call(path, method="GET", data=None):
            if "/git/ref/heads/review-loop-revisions/" in path or path.endswith("/git/refs"):
                return refs.call(path, method, data)
            return ({"object": {"sha": "f" * 40}} if path.endswith("ref/heads/main")
                    else None if method != "GET" else server[int(path.rsplit("/", 1)[1])])
        central.call.side_effect = call
        central.pages.return_value = [server[24]]
        new = dict(personal_request(max_pipelines=5), request_id="e" * 32,
                   workflow_revision="f" * 40, frozen_at=1200, frozen_at_iso=iso(1200),
                   launch_run={"id": 102, "attempt": 1, "actor_id": AUTHOR_ID}, mode="shadow")
        new.pop("publication")
        environment = {
            "GITHUB_REPOSITORY": CENTRAL, "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_ACTOR_ID": str(AUTHOR_ID),
            "GITHUB_RUN_ID": "102", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "f" * 40,
            "PUBLICATION_AUTH_MODE": "fine_grained_pat", "RESTART_UNPUBLISHED": "true",
            "PUBLISHER_AVAILABLE": "true", "INFERENCE_AVAILABLE": "true",
            "PUBLISHER_HEAD_REPO": FIXTURE, "PUBLISHER_SECRET_NAME": SECRET,
            "PUBLISHER_SECRET_MAP": json.dumps({FIXTURE.split("/")[0]: SECRET}),
        }
        return state, new, central, server, environment

    def test_unpublished_restart_is_new_work_not_a_retry(self):
        for status in ("uncertain", "acknowledged"):
            old, new, central, _, environment = self.restart_case()
            if status == "acknowledged":
                old["publication_intent"]["status"] = status
                old["reason"] = "publication_API_propagation_timeout"
            saved = copy.deepcopy(old)
            store, name = stored(old)
            with patch.dict(os.environ, environment, clear=True), \
                    patch("loop.live.authenticated_push") as push, \
                    patch("loop.live.PublisherAPI") as publisher:
                _, fresh = start(store, central, new, "d" * 32, 6, True, "fine_grained_pat",
                                 [CI_CHECK], True, 1200, restart_unpublished=True, read=Read())
            self.assertEqual(saved, old)
            self.assertEqual("ready", fresh["stage"])
            self.assertEqual(7, fresh["generation"])
            self.assertEqual(0, fresh["iteration"])
            self.assertEqual([], fresh["publications"])
            self.assertIsNone(fresh["report"])
            self.assertIsNone(fresh["run"])
            self.assertIsNone(fresh["intent"])
            self.assertNotIn("publication_intent", fresh)
            self.assertNotEqual(old["phase"], fresh["phase"])
            self.assertEqual(5, fresh["request"]["publication"]["max_pipelines"])
            self.assertEqual("request-" + "d" * 32 + ".json", fresh["restart"]["archive"])
            self.assertEqual(new["launch_run"], fresh["restart"]["launch_run"])
            self.assertEqual(old["publication_intent"]["candidate"]["commit"],
                             fresh["restart"]["candidate_commit"])
            self.assertEqual(fresh, store.entries[name])
            push.assert_not_called()
            publisher.assert_not_called()
            self.assertTrue(all(call.args[1:] == () for call in central.call.call_args_list))

    def test_unpublished_restart_rejects_active_unbound_or_changed_evidence(self):
        mutations = [
            "worker_active", "worker_duplicate", "worker_missing", "worker_rerun", "worker_revision",
            "publisher_active", "publisher_rerun", "publisher_revision", "publisher_recent",
            "publisher_repository", "verification_active", "coordinator_active", "launch_active",
            "source_active", "review_uncertain", "review_confirmed", "published_candidate",
            "head_drift", "ref_drift", "ref_PR_disagree", "fresh_head_drift", "active_phase",
            "wrong_reason", "confirmed_intent", "legacy_report", "acceptance_digest",
            "authorization", "launch_identity", "retired_checkpoint",
        ]
        for mutation in mutations:
            old, new, central, server, environment = self.restart_case()
            read = Read()
            if mutation.endswith("_active") and mutation != "active_phase":
                number = {"worker_active": 24, "publisher_active": 88, "verification_active": 99,
                          "coordinator_active": 100, "launch_active": 77, "source_active": 76}[mutation]
                if number == 76:
                    old["source_claim"]["run_id"] = number
                    server[number] = dict(server[77], id=number)
                server[number]["status"] = "in_progress"
            elif mutation == "worker_duplicate":
                central.pages.return_value = [server[24], dict(server[24], id=25)]
            elif mutation == "worker_missing":
                central.pages.return_value = []
            elif mutation.endswith("_rerun"):
                server[24 if mutation.startswith("worker") else 88]["run_attempt"] = 2
            elif mutation.endswith("_revision"):
                server[24 if mutation.startswith("worker") else 88]["head_sha"] = "f" * 40
            elif mutation == "publisher_recent":
                server[88]["updated_at"] = iso(301)
            elif mutation == "publisher_repository":
                server[88]["repository"] = {"full_name": FIXTURE}
            elif mutation.startswith("review_"):
                old["review_request"] = {"status": mutation.split("_")[1]}
            elif mutation in {"published_candidate", "head_drift", "ref_PR_disagree"}:
                read.pr["head"]["sha"] = (old["report"]["candidate"]["commit"]
                                         if mutation == "published_candidate" else "9" * 40)
                if mutation == "ref_PR_disagree":
                    read.ref_sha = SHA
            elif mutation == "ref_drift":
                read.ref_sha = "9" * 40
            elif mutation == "fresh_head_drift":
                new["frozen_sha"] = "9" * 40
            elif mutation == "active_phase":
                old["stage"] = "publication_intent"
            elif mutation == "wrong_reason":
                old["reason"] = "duplicate_run_identity"
            elif mutation == "confirmed_intent":
                old["publication_intent"]["status"] = "confirmed"
            elif mutation == "legacy_report":
                old["report"]["objective_validation"] = {"status": "passed"}
            elif mutation == "acceptance_digest":
                old["publication_intent"]["acceptance"]["verification_sha256"] = "0" * 64
            elif mutation == "authorization":
                old["publication_intent"]["authorization"]["generation"] = 5
            elif mutation == "launch_identity":
                new["launch_run"]["id"] = 103
            elif mutation == "retired_checkpoint":
                old["effects"] = [{"retired": True}]
            store, name = stored(old)
            with self.subTest(mutation=mutation), patch.dict(os.environ, environment, clear=True), \
                    self.assertRaises(Rejected):
                start(store, central, new, "d" * 32, 6, True, "fine_grained_pat", [], True,
                      1200, restart_unpublished=True, read=read)
            self.assertEqual(old, store.entries[name])

    def test_unpublished_restart_requires_exact_owner_authorization_and_stale_CAS_rejects(self):
        old, new, central, _, environment = self.restart_case()
        for identity, generation, publisher, auth, inference in [
            ("", 0, True, "fine_grained_pat", True), ("e" * 32, 6, True, "fine_grained_pat", True),
            ("d" * 32, 5, True, "fine_grained_pat", True), ("d" * 32, 6, False, "fine_grained_pat", True),
            ("d" * 32, 6, True, "disabled", True), ("d" * 32, 6, True, "fine_grained_pat", False),
        ]:
            store, name = stored(old)
            with self.subTest(identity=identity, generation=generation, auth=auth,
                              publisher=publisher, inference=inference), \
                    patch.dict(os.environ, environment, clear=True), self.assertRaises(Rejected):
                start(store, central, new, identity, generation, publisher, auth, [], inference,
                      1200, restart_unpublished=True, read=Read())
            self.assertEqual(old, store.entries[name])
        for key, value in [("GITHUB_ACTOR_ID", "999"), ("GITHUB_RUN_ATTEMPT", "2"),
                           ("GITHUB_EVENT_NAME", "workflow_run"), ("GITHUB_REF", "refs/heads/feature")]:
            store, _ = stored(old)
            with self.subTest(key=key), patch.dict(os.environ, dict(environment, **{key: value})), \
                    self.assertRaises(Rejected):
                start(store, central, new, "d" * 32, 6, True, "fine_grained_pat", [], True,
                      1200, restart_unpublished=True, read=Read())
        store, _ = stored(old)
        with patch.dict(os.environ, environment, clear=True), self.assertRaises(Rejected):
            start(store, central, new, "d" * 32, 6, True, "fine_grained_pat", [], True, 1200)
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(store, "update", side_effect=lambda _name, operation: operation(
                    dict(old, generation=7))), self.assertRaises(Rejected):
            start(store, central, new, "d" * 32, 6, True, "fine_grained_pat", [], True,
                  1200, restart_unpublished=True, read=Read())

    def test_cli_unpublished_restart_dispatches_only_the_new_frozen_worker(self):
        for from_environment in (True, False):
            old, _, central, _, environment = self.restart_case()
            store, name = stored(old)
            args = ["loop.cli", "launch", "--target", FIXTURE + "#1",
                    "--previous-request", "d" * 32, "--previous-generation", "6"]
            if not from_environment:
                environment.pop("RESTART_UNPUBLISHED")
                args.append("--restart-unpublished")
            with self.subTest(from_environment=from_environment), \
                    patch.dict(os.environ, environment, clear=True), patch("sys.argv", args), \
                    patch("loop.cli.API", return_value=central), \
                    patch("loop.cli.State", return_value=store), \
                    patch("loop.cli.target_api", return_value=Read()), \
                    patch("loop.cli.summary"), patch("loop.cli.time.time", return_value=1200), \
                    patch("loop.live.authenticated_push") as push:
                cli_main()
            fresh = store.entries[name]
            self.assertEqual("dispatched", fresh["stage"])
            self.assertNotEqual(old["request"]["request_id"], fresh["request"]["request_id"])
            self.assertEqual({"id": 102, "attempt": 1, "actor_id": AUTHOR_ID},
                             fresh["request"]["launch_run"])
            self.assertEqual(1, fresh["iteration"])
            posts = [call for call in central.call.call_args_list if call.args[1:2] == ("POST",)]
            self.assertEqual(2, len(posts))
            self.assertEqual(f"repos/{CENTRAL}/git/refs", posts[0].args[0])
            self.assertEqual(f"repos/{CENTRAL}/actions/workflows/copilot-worker.lock.yml/dispatches",
                             posts[1].args[0])
            self.assertEqual(fresh["request"]["request_id"], posts[1].args[2]["inputs"]["request_id"])
            self.assertEqual(revision_ref("f" * 40), posts[1].args[2]["ref"])
            push.assert_not_called()
            before = copy.deepcopy(fresh)
            with patch.dict(os.environ, environment, clear=True), patch("sys.argv", args), \
                    patch("loop.cli.API", return_value=central), \
                    patch("loop.cli.State", return_value=store), \
                    patch("loop.cli.target_api", return_value=Read()), \
                    patch("loop.cli.time.time", return_value=1200), self.assertRaises(Rejected):
                cli_main()
            self.assertEqual(before, store.entries[name])
        old, _, central, _, environment = self.restart_case()
        store, name = stored(old)
        read = Read()
        with patch.dict(os.environ, environment, clear=True), patch("sys.argv", args), \
                patch("loop.cli.API", return_value=central), \
                patch("loop.cli.State", return_value=store), \
                patch("loop.cli.target_api", return_value=read), \
                patch.object(read, "call", side_effect=APIError(404, "Read access denied")), \
                self.assertRaises(APIError):
            cli_main()
        self.assertEqual(old, store.entries[name])
        with patch("sys.argv", ["loop.cli", "tick", "--restart-unpublished"]), \
                patch("loop.cli.API") as api, self.assertRaises(Rejected):
            cli_main()
        api.assert_not_called()


    def recovery_case(self, historical=False, multiple=False):
        state, _ = self.context()
        state.update(stage="blocked", reason="blocked_uncertain_publication_requires_operator")
        if multiple:
            candidate = state["report"]["candidate"]
            first = dict(candidate["commits"][0], commit="b" * 40, tree="a" * 40)
            candidate["commits"][0]["parent"] = first["commit"]
            candidate["commits"].insert(0, first)
        saved_acceptance = {"verification_sha256": digest(state["report"])}
        if historical:
            candidate = state["report"]["candidate"]
            receipt = {"schema": 2, "status": "passed", "request_digest": digest(state["request"]),
                       "generation": 6, "run_id": 24, "run_attempt": 1,
                       "candidate_commit": candidate["commit"], "candidate_tree": candidate["tree"],
                       "bundle_sha256": candidate["bundle_sha256"]}
            state["validation_run"] = state.pop("verification_run")
            state["report"].pop("verification")
            state["report"].pop("verification_run")
            state["report"].update(validation="unattested", objective_validation=receipt)
            saved_acceptance = {"receipt_sha256": digest(receipt)}
            with self.assertRaises(Rejected):
                acceptance(state, {})
        artifact = {"id": 33, "digest": "sha256:" + "0" * 64}
        state["artifacts"] = [artifact]
        originals = [{"id": 34, "name": "verification-99-1", "digest": "sha256:" + "1" * 64,
                      "expired": False}]
        state["publication_intent"] = {
            "candidate": state["report"]["candidate"], "status": "uncertain",
            "owner": {"run_id": "88", "attempt": "1"}, "recorded_at": 100,
            "authorization": {"request_id": "d" * 32, "generation": 6, "expected_sha": SHA,
                              "candidate_commit": state["report"]["candidate"]["commit"]},
            "acceptance": {"profile": PROFILE, "request_digest": digest(state["request"]),
                           **saved_acceptance,
                           "candidate_commit": state["report"]["candidate"]["commit"],
                           "artifacts": originals}}
        server = {number: dict(run(), id=number, path=".github/workflows/coordinator.yml")
                  for number in (88, 99)}
        server[24] = run()
        central = Mock()
        central.call.side_effect = lambda path: server[int(path.rsplit("/", 1)[1])]
        central.pages.side_effect = lambda path, key: (
            [{"name": "personal_live", "conclusion": "success"}] if key == "jobs" else originals)
        read = Read()
        read.pr["head"]["sha"] = state["report"]["candidate"]["commit"]
        if multiple:
            original_call = read.call
            commits = {entry["commit"]: entry for entry in state["report"]["candidate"]["commits"]}
            def live_chain(path, *args):
                if "/git/commits/" in path:
                    entry = commits[path.rsplit("/", 1)[1]]
                    return {"sha": entry["commit"], "tree": {"sha": entry["tree"]},
                            "parents": [{"sha": entry["parent"]}]}
                return original_call(path, *args)
            read.call = live_chain
        store, name = stored(state)
        with patch("loop.live.artifact_metadata", return_value=(artifact, [artifact])), \
                patch("loop.live.object_bounds", create=True) as history:
            _, recovered = authorize_publication_reconciliation(
                store, central, read, "d" * 32, 6, "f" * 40, 400, FIXTURE, 1)
        self.assertEqual("publication_intent", recovered["stage"])
        self.assertEqual(state["request"], recovered["request"])
        self.assertEqual(state["report"], recovered["report"])
        self.assertEqual(state["publication_intent"], recovered["publication_intent"])
        self.assertNotIn("capability", recovered)
        self.assertEqual(1, recovered["iteration"])
        self.assertEqual("f" * 40, recovered["reconciliation"]["execution_revision"])
        history.assert_not_called()
        for identity, generation in [("", 6), ("f" * 32, 6), ("d" * 32, 7)]:
            store, _ = stored(state)
            with self.subTest(identity=identity, generation=generation), self.assertRaises(Rejected):
                authorize_publication_reconciliation(store, central, read, identity, generation, REVISION, 400, FIXTURE, 1)
        store, _ = stored(state)
        concurrent = dict(state, generation=7)
        with patch.object(store, "update", side_effect=lambda _name, operation: operation(concurrent)), \
                patch("loop.live.artifact_metadata", return_value=(artifact, [artifact])), \
                patch("loop.live.object_bounds", create=True), self.assertRaises(Rejected):
            authorize_publication_reconciliation(store, central, read, "d" * 32, 6, REVISION, 400, FIXTURE, 1)
        for mutation in ("cancelled", "already_reconciled", "other_head",
                         "other_tree", "rerun", "changed_artifact", "changed_acceptance"):
            value = copy.deepcopy(state)
            target = Read()
            target.pr["head"]["sha"] = state["report"]["candidate"]["commit"]
            server[88]["run_attempt"] = 1
            originals[0]["digest"] = "sha256:" + "1" * 64
            if mutation == "cancelled":
                value["stage"] = "cancelled"
            elif mutation == "already_reconciled":
                value["reconciliation"] = {"execution_revision": "f" * 40}
            elif mutation == "other_head":
                target.ref_sha = REVISION
            elif mutation == "other_tree":
                target.commit_tree = REVISION
            elif mutation == "rerun":
                server[88]["run_attempt"] = 2
            elif mutation == "changed_artifact":
                originals[0]["digest"] = "sha256:" + "2" * 64
            else:
                key = "receipt_sha256" if historical else "verification_sha256"
                value["publication_intent"]["acceptance"][key] = "0" * 64
            store, _ = stored(value)
            with patch("loop.live.artifact_metadata", return_value=(artifact, [artifact])), \
                    patch("loop.live.object_bounds", create=True), self.subTest(mutation=mutation), \
                    self.assertRaises(Rejected):
                authorize_publication_reconciliation(store, central, target, "d" * 32, 6, "f" * 40, 400, FIXTURE, 1)

    def test_multibatch_publication_reconciliation_checks_every_live_parent_and_tree(self):
        self.recovery_case(multiple=True)

    def context(self, changed=True):
        state = live_state()
        req = state["request"]
        candidate = {"commit": "c" * 40, "tree": "e" * 40, "parent": SHA,
                     "changed_paths": [ROOT_PATH] if changed else [], "changed": changed,
                     "patch_sha256": "1" * 64, "bundle_sha256": "2" * 64}
        from tests.test_loop import GOOD_PATCH
        candidate.update(commit="c" * 40 if changed else SHA,
                         commits=[{"commit": "c" * 40, "tree": "e" * 40, "parent": SHA,
                                   "subject": "Address Copilot review comments: Validate input",
                                   "changed_paths": [ROOT_PATH],
                                   "patch_sha256": hashlib.sha256(GOOD_PATCH).hexdigest()}] if changed else [],
                         finding_commits={finding["key"]: "c" * 40 for finding in req["findings"]}
                         if changed else {})
        state["report"] = {
            "schema": 2, "request_digest": digest(req), "repo": FIXTURE, "pr": 1, "frozen_sha": SHA,
            "workflow_revision": REVISION, "run_id": 24, "run_attempt": 1,
            "candidate": candidate, "verification": "verified", "publication_eligible": False,
            "verification_run": state["verification_run"],
            "dispositions": result(req, "fixes" if changed else "no_change",
                                   "fixed" if changed else "not_warranted"),
        }
        manifest = dict(candidate, schema=2, request_digest=digest(req), prerequisite=SHA,
                        repo=FIXTURE, repo_id=REPOSITORIES[FIXTURE], head_repo=FIXTURE,
                        source_private=False)
        return state, manifest

    def test_explicit_profile_accepts_structural_fix_and_nochange_without_test_claims(self):
        for changed in [True, False]:
            state, manifest = self.context(changed)
            facts = acceptance(state, manifest)
            self.assertEqual(PROFILE, facts["profile"])
            self.assertEqual(digest(state["report"]), facts["verification_sha256"])
            self.assertNotIn("receipt_sha256", facts)

    def test_failed_stale_or_legacy_reports_fail_closed(self):
        for key, value in [("verification", "failed"), ("schema", 1),
                           ("run_id", 25), ("run_attempt", 2), ("request_digest", "0" * 64),
                           ("workflow_revision", SHA), ("frozen_sha", REVISION), ("repo", "other/repository"),
                           ("publication_eligible", True), ("verification_run", {"id": 98, "attempt": 1}),
                           ("validation", "passed"), ("validation_claim", {"status": "passed"}),
                           ("objective_validation", {"status": "passed"})]:
            state, manifest = self.context()
            state["report"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Rejected):
                acceptance(state, manifest)

    def test_exact_publication_mode_candidate_and_authorization_required(self):
        state, _ = self.context()
        req, candidate = state["request"], state["report"]["candidate"]
        authorization = {"request_id": req["request_id"], "expected_sha": SHA,
                         "candidate_commit": candidate["commit"], "generation": 6}
        plan(req, personal_pr(), candidate, authorization, "publish")
        for key, value in [("parent", REVISION), ("commit", "invalid"), ("tree", "invalid")]:
            with self.assertRaises(Rejected):
                plan(req, personal_pr(), dict(candidate, **{key: value}), authorization, "publish")
        for key in authorization:
            with self.assertRaises(Rejected):
                plan(req, personal_pr(), candidate, dict(authorization, **{key: "wrong"}), "publish")
        with self.assertRaises(Rejected):
            plan(req, personal_pr(), candidate, authorization, "shadow")

    def test_push_commit_and_native_subprocess_have_narrow_hidden_auth_boundary(self):
        req = personal_request()
        token = "github_pat_" + "not_a_real_test_token" * 2
        with patch.dict(os.environ, {"GH_TOKEN": "central", "COPILOT_GITHUB_TOKEN": "inference"}), \
                patch("loop.publication.subprocess.run") as launch:
            launch.return_value = Mock(returncode=0, stdout=b"ok", stderr=b"")
            authenticated_push(".", req, "c" * 40, token)
        args, kwargs = launch.call_args
        self.assertNotIn("--force", args[0])
        self.assertNotIn("--force-with-lease", args[0])
        self.assertNotIn("--dry-run", args[0])
        self.assertNotIn(token, " ".join(args[0]))
        self.assertEqual("c" * 40 + ":refs/heads/trask-fix", args[0][-1])
        self.assertNotIn("GH_TOKEN", kwargs["env"])
        self.assertNotIn("COPILOT_GITHUB_TOKEN", kwargs["env"])
        self.assertIn("credential.helper=", args[0])
        if os.name == "nt":
            self.assertEqual(subprocess.CREATE_NO_WINDOW, kwargs["creationflags"])

    def test_uncertain_push_before_after_and_external_changes_are_reconcile_only(self):
        for live_sha, expected in [(SHA, "blocked"), ("c" * 40, "published"), (REVISION, "blocked")]:
            state, _ = self.context()
            state.update(stage="publication_intent", publication_intent={
                "candidate": state["report"]["candidate"], "acceptance": {"profile": PROFILE},
                "recorded_at": 100, "status": "uncertain",
            })
            store, name = stored(state)
            read = Read()
            read.pr["head"]["sha"] = live_sha
            result = advance(store, name, state, None, read, None, 100)
            self.assertEqual(expected, result["stage"])
            if expected == "published":
                self.assertEqual(live_sha, result["expected_sha"])
                self.assertEqual(1, len(result["publications"]))

    def test_cancel_between_durable_intent_and_push_prevents_push(self):
        state, _ = self.context()
        store, name = stored(state)
        read, publisher = Read(), Publisher(Read())
        original = store.update
        def racing_update(name, operation):
            result = original(name, operation)
            if result["stage"] == "publication_intent":
                cancel(store, name, "d" * 32, 6, 100)
            return result
        with patch("loop.live.evidence", return_value=({"profile": PROFILE}, state["report"]["candidate"])), \
                patch.object(store, "update", side_effect=racing_update), \
                patch("loop.live.authenticated_push") as push, patch("loop.live.time.time", return_value=100):
            with self.assertRaises(Rejected):
                publish(store, name, state, None, read, publisher, 100)
        push.assert_not_called()
        self.assertEqual("cancelled", store.entries[name]["stage"])


class ReviewTests(unittest.TestCase):
    def test_missed_fingerprints_bind_each_body_only_finding_not_overview_wording(self):
        findings = [{"kind": "body", "body": NONCLEAN + MISSED}]
        first = missed_fingerprints(findings)
        self.assertEqual(1, len(first))
        findings[0]["body"] = NONCLEAN.replace("Changes recommended", "Needs a closer look") + MISSED
        self.assertEqual(first, missed_fingerprints(findings))
        second = MISSED.replace("Backoff retries need a wake-up", "Describe the continue outcome")
        self.assertNotEqual(first, missed_fingerprints([{"kind": "body", "body": NONCLEAN + second}]))
        combined = MISSED.replace("(1)", "(2)").replace(
            "\n</details>\n</details>",
            "\n</details>\n<details>\n<summary>Describe the continue outcome</summary>\n\n"
            "Include continue in the validation error.\n</details>\n</details>")
        self.assertEqual(2, len(missed_fingerprints([{"kind": "body", "body": NONCLEAN + combined}])))
        self.assertEqual(first, missed_fingerprints(findings * 2))
        for body in (NONCLEAN, NONCLEAN + MISSED.replace("(1)", "(2)"),
                     NONCLEAN + MISSED.replace("</details>", "", 1),
                     NONCLEAN + MISSED + "unparsed feedback",
                     NONCLEAN + MISSED.replace("Schedule a wake-up at the earliest notBefore time.",
                                               "<details>nested finding</details>"),
                     (NONCLEAN + MISSED).replace("<!-- ccr-overview-v2 -->", "")):
            with self.subTest(body=body):
                self.assertEqual([], missed_fingerprints([{"kind": "body", "body": body}]))

    def test_old_roots_do_not_block_new_inline_or_body_only_work_but_still_prevent_clean(self):
        class MixedRead(Read):
            def graphql(self, query, variables):
                value = super().graphql(query, variables)
                if len(self.comments) > 1:
                    value["repository"]["pullRequest"]["reviewThreads"]["nodes"].append({
                        "id": "PRRT_new", "isResolved": False, "isOutdated": False,
                        "comments": {"nodes": [{"databaseId": 21}]},
                    })
                return value

        for novel in ("inline", "body", "none", "summary_only", "malformed_body"):
            state = live_state(stage="waiting_review")
            state["request"]["workflow_ref"] = revision_ref(REVISION)
            state["seen_inline_findings"] = inline_fingerprints(state["request"]["findings"])
            state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
            read = MixedRead()
            body = NONCLEAN + MISSED if novel == "body" else NONCLEAN
            if novel == "summary_only":
                body = body.replace("Changes recommended", "Needs a closer look")
            elif novel == "malformed_body":
                body += MISSED.replace("(1)", "(2)")
            read.reviews.append(review(id=13, body=body, submitted_at=iso(200)))
            if novel == "inline":
                read.comments.append(dict(read.comments[0], id=21, pull_request_review_id=13,
                                          body="A distinct new finding"))
            store, name = stored(state)
            with self.subTest(novel=novel):
                result = watch_review(store, name, state, read, 400)
                if novel in {"none", "summary_only", "malformed_body"}:
                    self.assertEqual("blocked", result["stage"])
                    self.assertEqual("repeated_findings", result["reason"])
                    continue
                self.assertEqual("ready", result["stage"])
                self.assertEqual(state["iteration"], result["iteration"])
                self.assertEqual(state["phase"], result["phase"])
                self.assertEqual(state["request"]["budgets"], result["request"]["budgets"])
                self.assertEqual(state["request"]["workflow_ref"], result["request"]["workflow_ref"])
                self.assertEqual(state["publications"], result["publications"])
                self.assertIn("inline:20", {f["key"] for f in result["request"]["findings"]})
                if novel == "inline":
                    self.assertIn("inline:21", {f["key"] for f in result["request"]["findings"]})
                else:
                    self.assertEqual(1, len(result["seen_missed_findings"]))
                result["review_request"] = {"baseline_review_ids": [12, 13],
                                            "recorded_at": 500, "sha": SHA}
                read.reviews.append(review(id=14, body=body, submitted_at=iso(600)))
                store, name = stored(result)
                stopped = watch_review(store, name, result, read, 800)
                self.assertEqual("repeated_findings", stopped["reason"])
                self.assertEqual("blocked", stopped["stage"])
                self.assertFalse(read.resolved)

    def test_seen_missed_findings_survive_new_summaries_and_older_checkpoint_fallback(self):
        for legacy in (False, True):
            state = live_state(stage="waiting_review")
            state["seen_inline_findings"] = inline_fingerprints(state["request"]["findings"])
            state["request"]["findings"][0]["body"] = NONCLEAN + MISSED
            if not legacy:
                state["seen_missed_findings"] = missed_fingerprints(state["request"]["findings"])
            state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
            read = Read()
            read.reviews.append(review(id=13, body=NONCLEAN.replace(
                "Changes recommended", "Needs a closer look") + MISSED, submitted_at=iso(200)))
            store, name = stored(state)
            with self.subTest(legacy=legacy):
                self.assertEqual("repeated_findings", watch_review(store, name, state, read, 400)["reason"])

    def test_missed_findings_seen_before_the_current_request_cannot_recycle_a_worker(self):
        state = live_state(stage="waiting_review")
        state["seen_inline_findings"] = inline_fingerprints(state["request"]["findings"])
        state["seen_missed_findings"] = missed_fingerprints([{"kind": "body", "body": NONCLEAN + MISSED}])
        state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
        read = Read()
        read.reviews.append(review(id=13, body=NONCLEAN + MISSED, submitted_at=iso(200)))
        store, name = stored(state)
        self.assertEqual("repeated_findings", watch_review(store, name, state, read, 400)["reason"])

    def test_novel_feedback_cannot_bypass_an_exhausted_budget(self):
        state = live_state(stage="waiting_review")
        state["seen_inline_findings"] = inline_fingerprints(state["request"]["findings"])
        state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
        state["iteration"] = 5
        read = Read()
        read.reviews.append(review(id=13, body=NONCLEAN + MISSED, submitted_at=iso(200)))
        read.checks[0]["conclusion"] = "failure"
        store, name = stored(state)
        with patch("loop.live.freeze") as freeze_next:
            result = watch_review(store, name, state, read, 400)
        self.assertEqual("remaining_findings_pipeline_budget", result["reason"])
        freeze_next.assert_not_called()

    def test_reused_root_fingerprint_survives_new_head_and_body_summary_wording(self):
        from loop.reviews import inline_fingerprints
        req = personal_request()
        changed = copy.deepcopy(req)
        changed["findings"][0]["body"] = "Changed overview wording"
        changed["findings"][1]["comment_commit_id"] = REVISION
        self.assertEqual(inline_fingerprints(req["findings"]),
                         inline_fingerprints(changed["findings"]))
        changed["findings"][1]["original_commit_id"] = REVISION
        self.assertNotEqual(inline_fingerprints(req["findings"]),
                            inline_fingerprints(changed["findings"]))

    def test_reused_original_bot_thread_is_frozen_in_its_current_exact_head_context(self):
        from loop.freeze import freeze
        read = Read()
        read.reviews[0]["commit_id"] = REVISION
        read.reviews.append(review(id=13, body=CLEAN, submitted_at=iso(200)))
        read.comments[0]["original_commit_id"] = REVISION
        req = freeze(read, 1, REVISION, 400, FIXTURE)
        self.assertEqual({"review:13", "inline:20"}, {f["key"] for f in req["findings"]})
        self.assertEqual("findings", fresh_collection(
            read, personal_request(), [12], 100, SHA, 400)["decision"])
        for mutation in ({"user": {"id": AUTHOR_ID, "type": "User"}},
                         {"original_commit_id": "f" * 40},
                         {"in_reply_to_id": 19}):
            changed = Read()
            changed.reviews = read.reviews
            changed.comments[0]["original_commit_id"] = REVISION
            changed.comments[0].update(mutation)
            if "original_commit_id" in mutation:
                with self.assertRaises(Rejected):
                    freeze(changed, 1, REVISION, 400, FIXTURE)
                continue
            result = freeze(changed, 1, REVISION, 400, FIXTURE)
            self.assertNotIn("inline:20", {f["key"] for f in result["findings"]})

    def test_unresolved_older_review_threads_are_frozen_even_when_outdated(self):
        read = Read()
        read.reviews[0]["commit_id"] = REVISION
        read.comments[0].update(commit_id=REVISION, original_commit_id=REVISION, line=None)
        read.outdated = True
        req = freeze(read, 1, REVISION, 400, FIXTURE)
        self.assertEqual(["inline:20"], [f["key"] for f in req["findings"]])
        self.assertEqual(REVISION, req["findings"][0]["original_commit_id"])
        self.assertEqual("PRRT_test", req["findings"][0]["thread_id"])
        self.assertIsNone(req["findings"][0]["line"])
        self.assertTrue(req["findings"][0]["thread_context"])
        read.reviews.append(review(id=13, body=CLEAN, submitted_at=iso(200)))
        self.assertEqual("findings", fresh_collection(
            read, req, [12], 100, SHA, 400)["decision"])

    def test_all_fresh_bodies_survive_a_later_clean_body(self):
        read, req = Read(), personal_request()
        read.resolved = True
        read.reviews = [review(id=13, body=NONCLEAN, submitted_at=iso(200)),
                        review(id=14, body=CLEAN, submitted_at=iso(250))]
        result = fresh_collection(read, req, [12], 100, SHA, 400)
        self.assertEqual("findings", result["decision"])
        self.assertEqual([13, 14], result["review_ids"])
        read.reviews[0]["body"] = "Unrecognized body-only finding"
        self.assertEqual("unknown", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.reviews[0]["body"] = CLEAN
        read.reviews[0]["state"] = "CHANGES_REQUESTED"
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])

    def test_publication_requires_existing_findings_and_never_requests_an_initial_review(self):
        read = Read()
        read.reviews[0]["commit_id"] = REVISION
        read.comments = []
        for reviews in (read.reviews, []):
            read.reviews = reviews
            with self.subTest(reviews=reviews), \
                    patch.object(read, "graphql", return_value={"repository": {"pullRequest": {
                        "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}}), \
                    self.assertRaises(Rejected):
                freeze(read, 1, REVISION, 100, FIXTURE)
        req = personal_request(max_pipelines=5)
        req["findings"] = []
        store = MemoryState()
        api = FakeAPI()
        with self.assertRaisesRegex(Rejected, "existing frozen Copilot findings"):
            start(store, api, req, "", 0, True, "fine_grained_pat", [CI_CHECK], True, 100)
        self.assertEqual({}, store.entries)
        state = live_state(req, "ready")
        store, name = stored(state)
        with self.assertRaises(Rejected):
            dispatch(store, name, api, 100)
        self.assertEqual("ready", store.entries[name]["stage"])
        self.assertFalse(any(method == "POST" for _, method, _ in api.calls))

    def test_clean_grammar_is_narrow_and_hidden_finding_never_clears_review(self):
        self.assertEqual("clean", body_classification(CLEAN))
        self.assertEqual("findings", body_classification(NONCLEAN))
        self.assertEqual("findings", body_classification(
            CLEAN + "<details><strong>Previously missed (1)</strong> bug</details>"))
        for body in ["", "**Findings:** None", CLEAN + "<details>unknown</details>",
                     CLEAN.replace("Approval recommended", "Looks good"),
                     CLEAN + "<strong>Unknown</strong>",
                     CLEAN + "**Findings:** None"]:
            with self.subTest(body=body):
                self.assertEqual("unknown", body_classification(body))

    def test_current_summary_recognizes_open_counts_and_the_effort_footer(self):
        self.assertEqual("clean", body_classification(CURRENT_CLEAN))
        self.assertEqual("clean", body_classification(CURRENT_CLEAN_RESOLVED))
        self.assertEqual("findings", body_classification(CURRENT_NONCLEAN))
        self.assertEqual("unknown", body_classification(CURRENT_CLEAN.replace(
            "\n\n\U0001f9e0 **Review effort:** Balanced", "")))
        self.assertEqual("unknown", body_classification(CURRENT_CLEAN_RESOLVED.replace(
            "1 resolved since last review", "2 resolved since last review")))
        self.assertEqual("unknown", body_classification(CURRENT_CLEAN + "\nUnexpected finding"))

    def test_native_approval_feedback_footer_does_not_hide_review_findings(self):
        footer = ("\n\n---\n\nGive feedback about Copilot approvals in [this survey]"
                  "(https://survey.alchemer.com/s3/9011660/CCR-Public-Preview-Autoapprove-feedback-survey)"
                  " to enter a drawing for a $150 gift card.")
        self.assertEqual("clean", body_classification(CURRENT_CLEAN + footer))
        self.assertEqual("clean", body_classification(CURRENT_CLEAN_RESOLVED + footer))
        self.assertEqual("findings", body_classification(CURRENT_NONCLEAN + footer))
        self.assertEqual("unknown", body_classification(CURRENT_CLEAN + footer + "\nUnexpected finding"))
        read, req = Read(), personal_request()
        read.reviews.append(review(id=13, body=CURRENT_CLEAN_RESOLVED + footer, submitted_at=iso(200)))
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.resolved = True
        self.assertEqual("clean", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])

    def test_code_review_skill_footer_and_change_summary_preserve_review_findings(self):
        changed = ("\n\n<details>\n<summary><strong>What changed in this PR</strong></summary>\n\n"
                   "Retains invalid span links with metadata.\n\n"
                   "| File | Description |\r\n| ---- | ----------- |\r\n"
                   "| SdkSpan.java | Retains qualifying runtime links. |\n</details>")
        footer = (
            '\n\n---\n\n\U0001f4a1 <a href="/open-telemetry/opentelemetry-java/new/main'
            '?filename=.github/skills/code-review/SKILL.md" class="Link--inTextBlock"'
            ' target="_blank" rel="noopener noreferrer">Add a `code-review` agent skill</a>'
            ' or configure MCP servers for context-aware, tailored reviews. '
            '<a href="https://docs.github.com/copilot/how-tos/use-copilot-agents/request-a-code-review/'
            'use-code-review?tool=webui#mcp-servers-and-agent-skills" class="Link--inTextBlock"'
            ' target="_blank" rel="noopener noreferrer">Learn more in the docs.</a>')
        body = CURRENT_CLEAN_RESOLVED.replace(
            "\n\n\U0001f9e0", changed + "\n\n\U0001f9e0") + footer
        self.assertEqual("clean", body_classification(body))
        self.assertEqual("findings", body_classification(
            body.replace("**0 open findings**", "**1 open finding**")))
        self.assertNotEqual("clean", body_classification(body + "\nUnexpected finding"))
        read, req = Read(), personal_request()
        read.reviews.append(review(id=13, body=body, submitted_at=iso(200)))
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.resolved = True
        self.assertEqual("clean", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])

    def test_resolved_summary_keeps_missed_findings_before_native_footers(self):
        body = CURRENT_CLEAN_RESOLVED.replace(
            "\n\n\U0001f9e0", "\n" + MISSED.rstrip("\n") + "\n\n\U0001f9e0")
        footer = ("\n\n---\n\nGive feedback about Copilot approvals in [this survey]"
                  "(https://survey.alchemer.com/s3/9011660/CCR-Public-Preview-Autoapprove-feedback-survey)"
                  " to enter a drawing for a $150 gift card.")
        fingerprints = missed_fingerprints([{"kind": "body", "body": NONCLEAN + MISSED}])
        self.assertEqual(1, len(fingerprints))
        for submitted in (body, body + footer):
            with self.subTest(body=submitted):
                self.assertEqual("findings", body_classification(submitted))
                self.assertEqual(fingerprints, missed_fingerprints(
                    [{"kind": "body", "body": submitted}]))
                read, req = Read(), personal_request()
                read.resolved = True
                read.reviews.append(review(id=13, body=submitted, submitted_at=iso(200)))
                self.assertEqual("findings", fresh_collection(
                    read, req, [12], 100, SHA, 400)["decision"])
                read.resolved = False
                state = live_state(stage="waiting_review")
                state["seen_inline_findings"] = inline_fingerprints(state["request"]["findings"])
                state["review_request"] = {
                    "baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
                store, name = stored(state)
                continued = watch_review(store, name, state, read, 400)
                self.assertEqual("ready", continued["stage"])
                self.assertEqual(fingerprints, continued["seen_missed_findings"])
                continued["review_request"] = {
                    "baseline_review_ids": [12, 13], "recorded_at": 500, "sha": SHA}
                read.reviews.append(review(id=14, body=submitted, submitted_at=iso(600)))
                store, name = stored(continued)
                self.assertEqual("repeated_findings", watch_review(
                    store, name, continued, read, 800)["reason"])
        self.assertEqual([], missed_fingerprints(
            [{"kind": "body", "body": body + footer + "\nUnexpected finding"}]))

    def test_current_resolved_summary_requires_closed_verified_roots(self):
        read, req = Read(), personal_request()
        read.reviews.append(review(id=13, body=CURRENT_CLEAN_RESOLVED, submitted_at=iso(200)))
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.resolved = True
        self.assertEqual("clean", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.reviews[-1]["body"] = CURRENT_CLEAN_RESOLVED.replace(
            "#discussion_r20", "#discussion_r99")
        self.assertEqual("unknown", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.reviews[-1]["body"] = CURRENT_CLEAN_RESOLVED
        read.comments[0]["user"] = {"id": AUTHOR_ID, "type": "User"}
        self.assertEqual("unknown", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])

    def test_verified_new_inline_finding_advances_with_current_or_unknown_overview(self):
        state = live_state(stage="waiting_review")
        state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
        for body in (CURRENT_NONCLEAN, "Unknown review format"):
            read = Read()
            read.resolved = True
            read.reviews.append(review(id=13, body=body, submitted_at=iso(200)))
            read.comments.append(dict(read.comments[0], id=21, pull_request_review_id=13,
                                      body="Skip empty map keys"))
            connection = read.graphql("", {})["repository"]["pullRequest"]["reviewThreads"]
            connection["nodes"].append({
                "id": "PRRT_new", "isResolved": False, "isOutdated": False,
                "comments": {"nodes": [{"databaseId": 21}]}})
            store, name = stored(state)
            with self.subTest(body=body), patch.object(read, "graphql", return_value={
                    "repository": {"pullRequest": {"reviewThreads": connection}}}):
                fresh = fresh_collection(read, state["request"], [12], 100, SHA, 400)
                self.assertEqual("findings", fresh["decision"])
                self.assertEqual([21], fresh["inline_ids"])
                result = watch_review(store, name, state, read, 400)
                self.assertEqual("ready", result["stage"])
                self.assertIn("inline:21", {f["key"] for f in result["request"]["findings"]})

    def test_no_finding_human_review_uses_the_same_bounded_clean_grammar(self):
        self.assertEqual("clean", body_classification(HUMAN_REVIEW))
        two = RESOLVED.replace("(1)", "(2)").replace(
            "#discussion_r20", "#discussion_r4175448985").replace(
            "\n</details>",
            "\n- [Cross-PR publisher recovery is blocked by scope check]"
            "(#discussion_r4171051071)\n</details>")
        self.assertEqual("clean", body_classification(HUMAN_REVIEW + two))
        for body in (HUMAN_REVIEW.replace("Needs a closer look", "Looks good"),
                     HUMAN_REVIEW.replace("\U0001f535", "\U0001f7e2"),
                     HUMAN_REVIEW.replace("Balanced", "Unknown"),
                     HUMAN_REVIEW.replace("**Findings:** None", "**Findings:** 0"),
                     HUMAN_REVIEW + "<details>Unknown finding</details>",
                     HUMAN_REVIEW + "\nUnexpected finding",
                     HUMAN_REVIEW + "**Findings:** None",
                     HUMAN_REVIEW + two.replace("(2)", "(1)"),
                     HUMAN_REVIEW + two.replace("#discussion_r4171051071",
                                                "#discussion_r4175448985")):
            with self.subTest(body=body):
                self.assertNotEqual("clean", body_classification(body))
        for extra in (MISSED, "<details><strong>Open (1)</strong> bug</details>",
                      "[Another finding](#discussion_r21)"):
            with self.subTest(extra=extra):
                self.assertEqual("findings", body_classification(HUMAN_REVIEW + extra))
        self.assertEqual("findings", body_classification(HUMAN_REVIEW.replace(
            "**Findings:** None", "**Findings:** 1") + two))

    def test_clean_review_accepts_only_a_complete_resolved_section(self):
        for resolved in (RESOLVED, RESOLVED + "\n", RESOLVED.replace(
                RESOLVED.splitlines()[4].split(" [", 1)[0], "-")):
            with self.subTest(resolved=resolved):
                self.assertEqual("clean", body_classification(CLEAN + resolved))
        two = RESOLVED.replace("(1)", "(2)").replace(
            "\n</details>", "\n- [Another fixed finding](#discussion_r21)\n</details>")
        self.assertEqual("clean", body_classification(CLEAN + two))
        for resolved in (RESOLVED.replace("(1)", "(2)"), RESOLVED.replace("(1)", "(0)"),
                         RESOLVED.replace("</details>", "<details>hidden finding</details></details>"),
                         RESOLVED.replace("#discussion_r20", "https://example.com/discussion_r20"),
                         RESOLVED.replace("</details>", ""),
                         RESOLVED.replace("[Loop omits", "unexpected text [Loop omits"),
                         RESOLVED.replace("\n</details>", "\nNew finding\n</details>"),
                         two.replace("#discussion_r21", "#discussion_r20"),
                         RESOLVED + RESOLVED, RESOLVED + "\nUnexpected finding"):
            with self.subTest(resolved=resolved):
                self.assertNotEqual("clean", body_classification(CLEAN + resolved))
        for extra in ("<details><strong>Previously missed (1)</strong> bug</details>",
                      "<details><strong>Open (1)</strong> bug</details>",
                      "[Another finding](#discussion_r21)"):
            with self.subTest(extra=extra):
                self.assertEqual("findings", body_classification(CLEAN + extra + RESOLVED))
        self.assertEqual("unknown", body_classification(CLEAN + "**Findings:** None" + RESOLVED))
        self.assertEqual("findings", body_classification(CLEAN.replace(
            "**Findings:** None", "**Findings:** 1") + RESOLVED))

    def test_resolved_summary_requires_verified_original_bot_threads_to_be_closed(self):
        read, req = Read(), personal_request()
        read.reviews.append(review(id=13, body=CLEAN + RESOLVED, submitted_at=iso(200)))
        read.comments[0]["commit_id"] = REVISION
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.outdated = True
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.resolved = True
        self.assertEqual("clean", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        for mutation in ({"user": {"id": AUTHOR_ID, "type": "User"}},
                         {"in_reply_to_id": 19},
                         {"pull_request_review_id": 99}):
            changed = copy.deepcopy(read)
            changed.comments[0].update(mutation)
            with self.subTest(mutation=mutation):
                self.assertEqual("unknown", fresh_collection(
                    changed, req, [12], 100, SHA, 400)["decision"])
        changed = copy.deepcopy(read)
        changed.comments[0]["original_commit_id"] = REVISION
        with self.assertRaisesRegex(Rejected, "Inconsistent|inconsistent"):
            fresh_collection(changed, req, [12], 100, SHA, 400)
        read.reviews[-1]["body"] = CLEAN + RESOLVED.replace("#discussion_r20", "#discussion_r99")
        self.assertEqual("unknown", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])

    def test_resolved_summary_cannot_hide_an_open_thread_or_an_earlier_finding(self):
        read, req = Read(), personal_request()
        read.resolved = True
        read.reviews.append(review(id=13, body=CLEAN + RESOLVED, submitted_at=iso(200)))
        read.comments.append(dict(read.comments[0], id=21, pull_request_review_id=13,
                                  body="Another open finding"))
        connection = read.graphql("", {})["repository"]["pullRequest"]["reviewThreads"]
        connection["nodes"].append({"id": "PRRT_open", "isResolved": False, "isOutdated": False,
                                    "comments": {"nodes": [{"databaseId": 21}]}})
        with patch.object(read, "graphql", return_value={
                "repository": {"pullRequest": {"reviewThreads": connection}}}):
            self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.comments.pop()
        read.reviews.append(review(id=14, body=NONCLEAN, submitted_at=iso(150)))
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])

    def test_watcher_finishes_resolved_clean_review_without_another_pipeline(self):
        for body in (CLEAN + RESOLVED, HUMAN_REVIEW + RESOLVED, CURRENT_CLEAN_RESOLVED):
            with self.subTest(body=body):
                state = live_state(stage="waiting_review")
                state["review_request"] = {"recorded_at": 100, "baseline_review_ids": [12]}
                read = Read()
                read.resolved = True
                read.reviews.append(review(id=13, body=body, submitted_at=iso(200)))
                store, name = stored(state)
                publisher = Mock()
                result = advance(store, name, state, FakeAPI(), read, publisher, 400)
                self.assertEqual("clean", result["stage"])
                self.assertEqual("fresh_review", result["reason"])
                self.assertEqual(1, result["iteration"])
                self.assertEqual([], publisher.mock_calls)

    def test_no_finding_human_review_requires_closed_verified_threads_not_ci(self):
        read, req = Read(), personal_request()
        read.comments[0]["commit_id"] = REVISION
        read.reviews.append(review(id=13, body=HUMAN_REVIEW + RESOLVED, submitted_at=iso(200)))
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.resolved = True
        self.assertEqual("clean", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.reviews[-1]["state"] = "CHANGES_REQUESTED"
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.reviews[-1]["state"] = "COMMENTED"
        read.comments[0]["user"] = {"id": AUTHOR_ID, "type": "User"}
        self.assertEqual("unknown", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])

        state = live_state(stage="waiting_review")
        state["review_request"] = {"recorded_at": 100, "baseline_review_ids": [12]}
        for ci in ("pending", "failed", "missing"):
            read = Read()
            read.resolved = True
            read.reviews.append(review(id=13, body=HUMAN_REVIEW + RESOLVED, submitted_at=iso(200)))
            if ci == "pending":
                read.checks[0].update(status="in_progress", conclusion=None)
            elif ci == "failed":
                read.checks[0]["conclusion"] = "failure"
            else:
                read.checks = []
            store, name = stored(state)
            with self.subTest(ci=ci):
                result = watch_review(store, name, state, read, 400)
                self.assertEqual("clean", result["stage"])
                self.assertEqual("clean", result["fresh_review"]["decision"])
                self.assertEqual(1, result["iteration"])

    def test_fresh_review_requires_submitted_exact_head_verified_bot_and_propagation(self):
        read = Read()
        req = personal_request()
        valid = review(id=13, body=CLEAN, submitted_at=iso(200))
        read.resolved = True
        read.reviews = [valid]
        self.assertEqual("waiting_propagation",
                         fresh_collection(read, req, [12], 100, SHA, 200)["decision"])
        self.assertEqual("clean", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        for mutation in [{"user": dict(BOT, id=999)}, {"commit_id": REVISION},
                         {"submitted_at": None}, {"submitted_at": iso(100)}, {"state": "PENDING"}]:
            read.reviews = [dict(valid, **mutation)]
            self.assertEqual("waiting_review",
                             fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.reviews = [valid]
        read.resolved = False
        read.comments[0]["pull_request_review_id"] = 13
        self.assertEqual("findings", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])
        read.comments[0].update(user={"id": AUTHOR_ID, "type": "User"})
        self.assertEqual("clean", fresh_collection(read, req, [12], 100, SHA, 400)["decision"])

    def test_exact_ci_handles_empty_duplicate_pending_failed_or_status_spoof(self):
        read = Read()
        required = [CI_CHECK]
        self.assertEqual("passed", exact_ci(read, FIXTURE, SHA, required)["decision"])
        for mutation, expected in [("empty", "missing"), ("pending", "pending"), ("failed", "failed"),
                                   ("duplicate", "unknown"), ("sha", "unknown"),
                                   ("app", "passed"), ("statuses", "unknown")]:
            value = Read()
            if mutation == "empty":
                value.checks = []
            elif mutation == "pending":
                value.checks[0].update(status="in_progress", conclusion=None)
            elif mutation == "failed":
                value.checks[0]["conclusion"] = "failure"
            elif mutation == "duplicate":
                value.checks *= 2
            elif mutation == "sha":
                value.checks[0]["head_sha"] = REVISION
            elif mutation == "app":
                value.checks[0]["app"]["id"] = 999
            else:
                value.statuses = [{"id": 334, "context": required[0], "state": "success"}]
            self.assertEqual(expected, exact_ci(value, FIXTURE, SHA, required)["decision"])

    def test_exact_ci_collects_later_pages_and_status_pagination(self):
        check = Read().checks[0]
        class Pages(API):
            def __init__(self):
                self.paths = []
            def call(self, path, *args, **kwargs):
                self.paths.append(path)
                if "/git/trees/" in path:
                    return Read().call(path)
                if "/status" in path:
                    return {"statuses": []}
                if path.split("?", 1)[0].endswith("/jobs"):
                    return {"jobs": Read().pages(path, "jobs")}
                if "/actions/runs" in path:
                    return {"workflow_runs": Read().runs}
                if path.endswith("&page=1"):
                    return {"check_runs": [dict(check, id=i, name=f"noise-{i}") for i in range(100)]}
                return {"check_runs": [check]}
        api = Pages()
        self.assertEqual("passed", exact_ci(api, FIXTURE, SHA, [CI_CHECK])["decision"])
        self.assertTrue(any("page=2" in path for path in api.paths))

    def test_review_watcher_continues_with_findings_despite_pending_or_failed_ci(self):
        for conclusion in (None, "failure"):
            with self.subTest(conclusion=conclusion):
                state = live_state(stage="waiting_review")
                state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
                read = Read()
                read.reviews = [review(id=13, body=NONCLEAN, submitted_at=iso(200))]
                read.checks[0].update(status="in_progress" if conclusion is None else "completed",
                                      conclusion=conclusion)
                store, name = stored(state)
                result = watch_review(store, name, state, read, 400)
                self.assertEqual("ready", result["stage"])
                self.assertEqual(1, result["iteration"])

    def test_saved_review_ci_wait_resumes_review_work_while_ci_is_pending(self):
        state = live_state(stage="waiting_ci")
        state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
        state["request"]["publication"]["required_checks"] += ["optional job", "dependent job"]
        read = Read()
        read.reviews = [review(id=13, body=NONCLEAN, submitted_at=iso(200))]
        read.checks[0].update(status="in_progress", conclusion=None)
        read.checks.append(dict(read.checks[0], id=334, name="optional job",
                                status="completed", conclusion=None))
        store, name = stored(state)
        result = advance(store, name, state, FakeAPI(), read, Mock(), 400)
        self.assertEqual("ready", result["stage"])
        self.assertEqual(7, result["generation"])

    def test_clean_finishes_without_ci_nonclean_advances_only_remaining_bounded_pipeline(self):
        state = live_state(stage="waiting_review")
        state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
        read = Read()
        read.resolved = True
        read.reviews = [review(id=13, body=CLEAN, submitted_at=iso(200))]
        store, name = stored(state)
        self.assertEqual("clean", watch_review(store, name, state, read, 400)["stage"])
        read.checks = []
        store, name = stored(state)
        result = watch_review(store, name, state, read, 400)
        self.assertEqual("clean", result["stage"])
        read = Read()
        read.reviews = [review(id=13, body=NONCLEAN, submitted_at=iso(200))]
        store, name = stored(state)
        result = watch_review(store, name, state, read, 400)
        self.assertEqual("ready", result["stage"])
        self.assertEqual(1, result["iteration"])
        self.assertEqual(7, result["generation"])
        self.assertNotEqual(state["request"]["request_id"], result["request"]["request_id"])
        self.assertEqual(state["request"]["budgets"], result["request"]["budgets"])
        self.assertEqual(state["request"]["publication"]["phase"], result["request"]["publication"]["phase"])
        exhausted = dict(state, iteration=5)
        store, name = stored(exhausted)
        self.assertEqual("exhausted", watch_review(store, name, exhausted, read, 400)["stage"])

    def test_unknown_or_repeated_review_blocks_without_another_worker(self):
        state = live_state(stage="waiting_review")
        state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
        read = Read()
        read.reviews = [review(id=13, body="Unknown format: Findings None", submitted_at=iso(200))]
        read.resolved = True
        store, name = stored(state)
        self.assertEqual("unknown_fresh_review_body", watch_review(store, name, state, read, 400)["reason"])
        read.reviews = [review(id=13, body=NONCLEAN, submitted_at=iso(200))]
        read.comments[0]["pull_request_review_id"] = 13
        from loop.freeze import freeze
        from loop.reviews import finding_fingerprint
        state["seen_findings"] = [finding_fingerprint(freeze(read, 1, REVISION, 400, FIXTURE)["findings"])]
        store, name = stored(state)
        self.assertEqual("repeated_findings", watch_review(store, name, state, read, 400)["reason"])

    def test_publisher_can_verify_copilot_identity_and_freeze_the_next_review_iteration(self):
        state = live_state(stage="waiting_review")
        state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
        read = Read()
        read.reviews = [review(id=13, body=NONCLEAN, submitted_at=iso(200))]
        publisher = Publisher(read)
        store, name = stored(state)
        with patch.object(publisher, "authorize", wraps=publisher.authorize) as authorize:
            result = watch_review(store, name, state, publisher, 400)
        self.assertEqual("ready", result["stage"])
        self.assertEqual(7, result["generation"])
        self.assertEqual(1, result["iteration"])
        self.assertEqual(state["request"]["budgets"], result["request"]["budgets"])
        authorize.assert_any_call("users/copilot-pull-request-reviewer%5Bbot%5D", "GET", None)
        self.assertEqual([], publisher.posts)
        for path in ("users/other", "users/copilot-pull-request-reviewer%5Bbot%5D/extra",
                     "repos/other/repository/pulls/1"):
            with self.subTest(path=path), self.assertRaises(Rejected):
                publisher.authorize(path, "GET", None)
        store, name = stored(state)
        call = read.call
        with patch.object(read, "call", side_effect=lambda path: (
            dict(BOT, id=999) if path == "users/copilot-pull-request-reviewer%5Bbot%5D" else call(path))), \
                self.assertRaisesRegex(Rejected, "Copilot identity changed"):
            watch_review(store, name, state, publisher, 400)
        self.assertEqual(state, store.entries[name])

    def test_publisher_can_revalidate_bot_pr_ownership_before_continuing_review(self):
        read = Read()
        owner = dict(read.req["commit_author"])
        read.pr["user"].update(id=999, type="Bot", login="Copilot")
        read.req.update(eligible(read.pr, FIXTURE, AUTHOR_ID, "copilot_review", owner))
        state = live_state(read.req, stage="waiting_review")
        state["review_request"] = {"baseline_review_ids": [12], "recorded_at": 100, "sha": SHA}
        read.reviews = [review(id=13, body=NONCLEAN, submitted_at=iso(200))]
        call, graphql = read.call, read.graphql
        read.call = Mock(side_effect=lambda path, *args: (
            owner if path == f"user/{AUTHOR_ID}" else call(path, *args)))
        read.graphql = Mock(side_effect=lambda query, variables: (
            {"repository": {"nameWithOwner": FIXTURE},
             "search": {"pageInfo": {"hasNextPage": False},
                        "nodes": [{"number": 1, "repository": {"nameWithOwner": FIXTURE}}]}}
            if "searchQuery" in variables else graphql(query, variables)))
        publisher = Publisher(read)
        store, name = stored(state)
        result = watch_review(store, name, state, publisher, 400)
        self.assertEqual("ready", result["stage"])
        self.assertEqual(999, result["request"]["pr_author_id"])
        self.assertEqual(owner, result["request"]["commit_author"])
        self.assertEqual(state["request"]["budgets"], result["request"]["budgets"])
        read.call.assert_any_call(f"user/{AUTHOR_ID}")
        self.assertEqual([], publisher.posts)
        with self.assertRaisesRegex(Rejected, "Publisher read outside"):
            publisher.authorize(f"user/{AUTHOR_ID + 1}", "GET", None)


class CredentialAndArchiveTests(unittest.TestCase):
    def test_new_request_archives_uncertain_intent_unchanged_with_nonforce_CAS(self):
        from loop.state import Conflict, State
        old = live_state(stage="blocked")
        old.update(reason="blocked_uncertain_publication_requires_operator",
                   publication_intent={"status": "uncertain", "candidate": {"commit": "c" * 40}})
        value = checkpoint(dict(old["request"], request_id="e" * 32))
        value["restart"] = {"archive": "request-" + "d" * 32 + ".json"}
        name = checkpoint_name(FIXTURE, 1, REPOSITORIES[FIXTURE])
        api = Mock()
        api.call.return_value = {"sha": "e" * 40}
        store = State(api)
        with patch.object(store, "snapshot", return_value=(SHA, REVISION, {name: old})):
            store.write(name, value, old)
        blobs = [json.loads(call.args[2]["content"]) for call in api.call.call_args_list
                 if call.args[0].endswith("/blobs")]
        self.assertEqual([value, old], blobs)
        self.assertEqual("uncertain", blobs[1]["publication_intent"]["status"])
        self.assertEqual({"sha": "e" * 40, "force": False}, api.call.call_args_list[-1].args[2])
        for entries, error in [
            ({name: dict(old, generation=7)}, Conflict),
            ({name: old, value["restart"]["archive"]: dict(old, reason="changed")}, Rejected),
        ]:
            api.reset_mock()
            with patch.object(store, "snapshot", return_value=(SHA, REVISION, entries)), \
                    self.assertRaises(error):
                store.write(name, value, old)
            api.call.assert_not_called()

    def test_stopped_publication_is_archived_in_the_same_CAS_transaction(self):
        from loop.state import State
        old = live_state(stage="blocked")
        old["reason"] = "blocked_uncertain_publication_requires_operator"
        value = copy.deepcopy(old)
        archive = "stopped-" + old["request"]["request_id"] + "-6.json"
        value.update(stage="publication_intent", reconciliation={"stopped_archive": archive})
        name = checkpoint_name(FIXTURE, 1, REPOSITORIES[FIXTURE])
        api = Mock()
        api.call.return_value = {"sha": "e" * 40}
        store = State(api)
        with patch.object(store, "snapshot", return_value=(SHA, REVISION, {name: old})):
            store.write(name, value, old)
        blobs = [json.loads(call.args[2]["content"]) for call in api.call.call_args_list
                 if call.args[0].endswith("/blobs")]
        self.assertEqual([value, old], blobs)
        transaction = next(call.args[2] for call in api.call.call_args_list
                           if call.args[0].endswith("/trees"))
        self.assertEqual({name, archive}, {item["path"] for item in transaction["tree"]})
        api.reset_mock()
        with patch.object(store, "snapshot", return_value=(SHA, REVISION, {
                name: old, archive: dict(old, reason="different evidence")})), self.assertRaises(Rejected):
            store.write(name, value, old)
        api.call.assert_not_called()

    def test_explicit_auth_and_endpoint_policy_has_no_classic_inference_upstream_or_merge_fallback(self):
        for token, mode in [("ghp_classic", "fine_grained_pat"), ("github_pat_" + "x" * 40, "app"),
                            ("", "fine_grained_pat"), ("inference", "inference")]:
            with self.assertRaises(Rejected):
                PublisherAPI(token, personal_request(), mode)
        api = PublisherAPI("github_pat_" + "test_not_a_token" * 3, personal_request(), "fine_grained_pat")
        for path, method, data in [
            ("repos/open-telemetry/opentelemetry-java-instrumentation/pulls/1", "PATCH", {}),
            (f"repos/{FIXTURE}/pulls/1", "PATCH", {"title": "no"}),
            (f"repos/{FIXTURE}/pulls/1/merge", "PUT", {}),
            (f"repos/{FIXTURE}/issues/1/comments", "POST", {"body": "no"}),
            (f"repos/{FIXTURE}/pulls/1/requested_reviewers", "POST", {"reviewers": ["human"]}),
            (f"repos/{FIXTURE}/pulls/1/comments", "POST", {"body": "no", "in_reply_to": 20}),
            ("graphql", "POST", {"query": "mutation { resolveReviewThread }",
                                 "variables": {"thread": "human"}}),
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                api.authorize(path, method, data)

    def test_live_identity_rejects_wrong_actor_repository_node_visibility_without_central_probe(self):
        api = PublisherAPI("github_pat_" + "test_not_a_token" * 3, personal_request(), "fine_grained_pat")
        repo = {"id": REPOSITORIES[FIXTURE], "node_id": REPO_NODE, "full_name": FIXTURE,
                "private": False, "owner": {"id": AUTHOR_ID, "type": "User"},
                "permissions": {"push": True}}
        user = {"id": AUTHOR_ID, "type": "User", "login": "launch-owner"}
        def call(path, *_args, **_kwargs):
            if path == "user":
                return user
            if path == f"repos/{FIXTURE}":
                return repo
            if path == f"repos/{CENTRAL}":
                raise AssertionError("Publisher identity must not probe the central repository")
            return personal_pr()
        with patch.object(api, "call", side_effect=call):
            self.assertEqual(REPO_NODE, api.identity(personal_request())["repo_node"])
            for key, value in [("id", 999), ("full_name", "other/repo"), ("private", True),
                               ("permissions", {"push": False})]:
                before = repo[key]
                repo[key] = value
                with self.assertRaises(Rejected):
                    api.identity(personal_request())
                repo[key] = before
            user["id"] = 999
            with self.assertRaises(Rejected):
                api.identity(personal_request())
        user["id"] = AUTHOR_ID
        user["login"] = "renamed-owner"
        with patch.object(api, "call", side_effect=call), self.assertRaisesRegex(
                Rejected, "frozen GitHub commit author"):
            api.identity(personal_request())
        user["login"] = "launch-owner"
        with patch.object(api, "call", side_effect=lambda path: {
                "full_name": CENTRAL, "private": False
                } if path == f"repos/{CENTRAL}" else call(path)) as reads:
            self.assertEqual(REPO_NODE, api.identity(personal_request())["repo_node"])
            self.assertNotIn(f"repos/{CENTRAL}", [item.args[0] for item in reads.call_args_list])

    def test_trusted_zip_reader_rejects_traversal_duplicates_symlinks_and_missing_members(self):
        self.assertEqual({"receipt.json": b"{}"}, read_package(zipped({"receipt.json": b"{}"}), {"receipt.json"}))
        for files in [{"../receipt.json": b"{}"}, {"receipt.json": b"{}", "extra": b"bad"}, {}]:
            with self.assertRaises(Rejected):
                read_package(zipped(files), {"receipt.json"})
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            info = zipfile.ZipInfo("receipt.json")
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b"link")
        with self.assertRaises(Rejected):
            read_package(output.getvalue(), {"receipt.json"})

    def test_artifact_retention_server_metadata_hash_and_duplicates_are_checked(self):
        payload = zipped({"receipt.json": b"{}"})
        run = {"id": 99, "head_sha": REVISION, "created_at": iso(90)}
        artifact = {"id": 7, "name": "verification-99-1", "expired": False,
                    "workflow_run": {"id": 99, "head_sha": REVISION},
                    "size_in_bytes": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    "created_at": iso(100), "expires_at": iso(100 + 14 * 86400)}
        api = Mock()
        api.pages.return_value = [artifact]
        api.artifact_zip.return_value = payload
        with patch("loop.publication.time.time", return_value=100):
            self.assertEqual({"receipt.json": b"{}"}, bound_artifact(
                api, run, artifact["name"], {"receipt.json"}, [artifact])[0])
            for key, value in [("expired", True), ("workflow_run", {"id": 98, "head_sha": REVISION}),
                               ("digest", "sha256:" + "0" * 64), ("expires_at", iso(99)),
                               ("created_at", iso(89))]:
                broken = dict(artifact, **{key: value})
                api.pages.return_value = [broken]
                with self.subTest(key=key), self.assertRaises(Rejected):
                    bound_artifact(api, run, artifact["name"], {"receipt.json"}, [broken])
            api.pages.return_value = [artifact, artifact]
            with self.assertRaises(Rejected):
                bound_artifact(api, run, artifact["name"], {"receipt.json"}, [artifact])


class RealObjectEvidenceTests(unittest.TestCase):

    def test_real_git_candidate_bundle_server_bound_evidence_and_publication_reconciliation(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, package, restored = root / "source", root / "package", root / "restored"
            source.mkdir()
            package.mkdir()
            restored.mkdir()
            git(["init", "--bare", "--quiet"], source)
            blob = git(["hash-object", "-w", "--stdin"], source, b"values.length - 1\n").decode().strip()
            git(["update-index", "--add", "--cacheinfo", "100644", blob, ROOT_PATH], source)
            ci_blob = git(["hash-object", "-w", "--stdin"], source, b"trusted test CI\n").decode().strip()
            git(["update-index", "--add", "--cacheinfo", "100644", ci_blob, CI_WORKFLOW_PATH], source)
            tree = git(["write-tree"], source).decode().strip()
            commit = git(["hash-object", "-t", "commit", "-w", "--stdin"], source,
                         f"tree {tree}\nauthor T <t@invalid> 0 +0000\ncommitter T <t@invalid> 0 +0000\n\nbaseline\n".encode()).decode().strip()
            git(["update-ref", "refs/heads/main", commit], source)
            req = personal_request(commit)
            patch_data = f"""diff --git a/{ROOT_PATH} b/{ROOT_PATH}
--- a/{ROOT_PATH}
+++ b/{ROOT_PATH}
@@ -1 +1 @@
-values.length - 1
+values.length
""".encode()
            from tests.support import semantic
            files = {
                "result.json": canonical(semantic(req, "fixes", patch_data)),
                "candidate.patch": patch_data,
                "diagnostics.txt": b'{"commands":[{"argv":["python3","-c","raise RuntimeError()"]}]}',
            }
            payload = zipped(files)
            def fetch(directory):
                git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--depth=1",
                     str(source), commit], directory)
            worker = run()
            worker_artifact = {"id": 33, "name": "candidate-24-1", "size_in_bytes": len(payload),
                               "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                               "expired": False, "workflow_run": {"id": 24, "head_sha": REVISION}}
            result = verify(payload, req, worker, worker_artifact, fetch, package)
            state = live_state(req)
            state["report"] = copy.deepcopy(result)
            candidate = result["candidate"]
            manifest = json.loads((package / "manifest.json").read_bytes())
            result_report = {"schema": 2, "request_id": req["request_id"],
                             "request_digest": digest(req), "generation": 6, "verification": "verified",
                             "run_id": 24, "run_attempt": 1, "result": result}
            verification = zipped({"verification-report.json": canonical(result_report),
                                   "candidate-package/manifest.json": canonical(manifest),
                                   "candidate-package/candidate.bundle": (package / "candidate.bundle").read_bytes()})
            artifacts = [{
                "id": i, "name": name, "size_in_bytes": len(data), "expired": False,
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "workflow_run": {"id": 99, "head_sha": REVISION},
                "created_at": iso(100), "expires_at": iso(100 + 14 * 86400),
            } for i, name, data in [(34, "verification-99-1", verification)]]
            state["artifacts"] = artifacts
            state["report"]["verification_run"] = {"id": 99, "attempt": 1}
            pipeline = dict(worker, id=99, path=".github/workflows/coordinator.yml", created_at=iso(90))
            class Artifacts:
                def call(self, path, *_args):
                    return pipeline if path.endswith("/99") else worker
                def pages(self, path, key):
                    if key == "jobs":
                        return ([{"name": n, "conclusion": "success"} for n in ("verify", "finalize")]
                                if "/99/" in path else [{"name": "agent", "conclusion": "success"}])
                    return artifacts if "/99/" in path else [worker_artifact]
                def artifact_zip(self, identity, limit):
                    return {33: payload, 34: verification}[identity]
            original_git = git
            def redirect(args, directory):
                args = list(args)
                remote = "https://github.com/" + FIXTURE + ".git"
                if remote in args:
                    args[args.index(remote)] = str(source)
                    args = ["-c", "protocol.file.allow=always"] + args
                return original_git(args, directory)
            with patch("loop.publication.git", side_effect=redirect), \
                    patch("loop.publication.time.time", return_value=100), \
                    patch("loop.verify.subprocess.run", wraps=subprocess.run) as executor:
                accepted, derived = evidence(Artifacts(), state, restored)
            self.assertGreater(executor.call_count, 0)
            for call in executor.call_args_list:
                self.assertEqual("git", call.args[0][0])
                self.assertNotIn("checkout", call.args[0])
            self.assertEqual(candidate, derived)
            self.assertEqual(PROFILE, accepted["profile"])
            self.assertEqual(digest(state["report"]), accepted["verification_sha256"])
            self.assertEqual(candidate["commit"], git(["rev-parse", "candidate"], restored).decode().strip())
            self.assertEqual(b"values.length\n", git(["show", "candidate:" + ROOT_PATH], restored))
            self.assertIn(b"Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
                          git(["show", "-s", "--format=%B", candidate["commit"]], restored))
            for key, value in [("run_attempt", 2), ("head_sha", SHA), ("status", "in_progress"),
                               ("path", ".github/workflows/validate.yml"),
                               ("path", ".github/workflows/qualification.yml"), ("event", "pull_request")]:
                before = pipeline[key]
                pipeline[key] = value
                with self.subTest(key=key), self.assertRaises(Rejected):
                    evidence(Artifacts(), state, restored)
                pipeline[key] = before
            state["coordinator_recovery"] = {"execution_revision": "f" * 40,
                                             "workflow_ref": revision_ref("f" * 40)}
            with self.assertRaisesRegex(Rejected, "trusted verification pipeline"):
                evidence(Artifacts(), state, restored)
            pipeline["head_sha"] = "f" * 40
            artifacts[0]["workflow_run"]["head_sha"] = "f" * 40
            recovered = root / "recovered"
            recovered.mkdir()
            with patch("loop.publication.git", side_effect=redirect), \
                    patch("loop.publication.time.time", return_value=100):
                self.assertEqual((accepted, derived), evidence(Artifacts(), state, recovered))
            state.pop("coordinator_recovery")
            pipeline["head_sha"] = REVISION
            artifacts[0]["workflow_run"]["head_sha"] = REVISION
            store, name = stored(state)
            read = Read(req)
            publisher = Publisher(read)
            def push(_directory, request, commit, _token):
                self.assertEqual(req["frozen_sha"], read.pr["head"]["sha"])
                self.assertEqual("publication_intent", store.entries[name]["stage"])
                read.pr["head"]["sha"] = commit
            with patch("loop.live.evidence", return_value=(accepted, derived)), \
                    patch("loop.live.authenticated_push", side_effect=push), \
                    patch("loop.live.time.time", return_value=100):
                state = publish(store, name, state, None, read, publisher, 100)
                while state["stage"] != "waiting_review":
                    state = advance(store, name, state, None, read, publisher, 100)
            self.assertEqual("waiting_review", state["stage"])
            self.assertEqual(candidate["commit"], state["expected_sha"])
            self.assertEqual("confirmed", state["effects"][0]["status"])
            read.resolved = True
            read.pr["requested_reviewers"] = []
            read.reviews.append(review(id=13, body=CLEAN, commit_id=candidate["commit"],
                                       submitted_at=iso(200)))
            read.checks[0]["head_sha"] = candidate["commit"]
            read.runs[0]["head_sha"] = candidate["commit"]
            state = watch_review(store, name, state, read, 400)
            self.assertEqual("clean", state["stage"])
