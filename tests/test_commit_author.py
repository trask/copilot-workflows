from tests.support import reconstruct
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from loop.freeze import freeze
from loop.policy import AUTHOR_ID, Rejected, commit_author, digest
from loop.publication import PublisherAPI
from loop.verify import (git)
from tests.test_live import FIXTURE, TEST_TOKEN, Read, personal_request
from tests.test_loop import GOOD_PATCH, REVISION, baseline, request
from tests.test_self_review import SelfRead


class CommitAuthorTests(unittest.TestCase):
    def test_both_loop_kinds_freeze_verified_pr_author_account(self):
        for kind, read in (("copilot_review", Read()), ("self_review", SelfRead())):
            with self.subTest(kind=kind):
                frozen = freeze(read, 1, REVISION, 100, FIXTURE, loop_kind=kind)
                self.assertEqual({"id": AUTHOR_ID, "login": "launch-owner"},
                                 frozen["commit_author"])
                self.assertEqual(("launch-owner",
                                  f"{AUTHOR_ID}+launch-owner@users.noreply.github.com"),
                                 commit_author(frozen))
                altered = copy.deepcopy(frozen)
                altered["commit_author"]["login"] = "other-owner"
                self.assertNotEqual(digest(frozen), digest(altered))

    def test_missing_or_unsafe_api_login_cannot_freeze_a_placeholder_author(self):
        for login in (None, "", "bad\nidentity", "bad <identity>", "bot[bot]", "-owner", "owner-"):
            read = Read()
            read.pr["user"]["login"] = login
            with self.subTest(login=login), self.assertRaisesRegex(
                    Rejected, "frozen GitHub commit author"):
                freeze(read, 1, REVISION, 100, FIXTURE)

    def test_missing_forged_or_extended_frozen_authors_fail_before_git_or_publication(self):
        for author in (None, {}, {"id": AUTHOR_ID + 1, "login": "launch-owner"},
                       {"id": True, "login": "launch-owner"},
                       {"id": AUTHOR_ID, "login": "launch-owner", "email": "shadow@invalid"},
                       {"id": AUTHOR_ID, "login": "owner\ncommitter injected"}):
            req = request()
            if author is None:
                req.pop("commit_author")
            else:
                req["commit_author"] = author
            with self.subTest(author=author), patch("loop.verify.git") as native, \
                    self.assertRaisesRegex(Rejected, "frozen GitHub commit author"):
                reconstruct({"candidate.patch": GOOD_PATCH}, req, Mock())
            native.assert_not_called()
            published = personal_request()
            if author is None:
                published.pop("commit_author")
            else:
                published["commit_author"] = author
            with self.assertRaisesRegex(Rejected, "frozen GitHub commit author"):
                PublisherAPI(TEST_TOKEN, published, "fine_grained_pat")

    def test_packaged_commit_uses_account_for_both_identities_and_freeze_timestamp(self):
        with tempfile.TemporaryDirectory() as package, tempfile.TemporaryDirectory() as restored:
            git(["init", "--bare", "--quiet"], restored)
            req = dict(personal_request(), frozen_sha=baseline(restored), frozen_at=100)
            candidate = reconstruct({"candidate.patch": GOOD_PATCH}, req, baseline, package)
            git(["-c", "protocol.file.allow=always", "fetch", "--quiet",
                 str(Path(package) / "candidate.bundle"),
                 "refs/heads/candidate:refs/heads/candidate"], restored)
            self.assertEqual([
                "launch-owner", f"{AUTHOR_ID}+launch-owner@users.noreply.github.com", "100",
                "launch-owner", f"{AUTHOR_ID}+launch-owner@users.noreply.github.com", "100",
            ], git(["show", "-s", "--format=%an%n%ae%n%at%n%cn%n%ce%n%ct",
                    candidate["commit"]], restored).decode().splitlines())
            self.assertEqual(candidate["parent"],
                             git(["rev-parse", "candidate^"], restored).decode().strip())
            self.assertEqual(candidate["tree"],
                             git(["rev-parse", "candidate^{tree}"], restored).decode().strip())
            body = git(["show", "-s", "--format=%B", candidate["commit"]], restored).decode()
            self.assertTrue(body.startswith("Address Copilot review comments: Validate input\n"))
            for finding in req["findings"]:
                self.assertIn("Copilot comment:\n\n" + finding["body"], body)
            self.assertIn("\nAnalysis: Investigated\n", body)
            self.assertIn("Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>", body)
            with patch.dict(os.environ, {
                    "GIT_AUTHOR_NAME": "attacker", "GIT_AUTHOR_EMAIL": "attacker@invalid",
                    "GIT_COMMITTER_NAME": "attacker", "GIT_COMMITTER_EMAIL": "attacker@invalid"}):
                again = reconstruct({"candidate.patch": GOOD_PATCH}, req, baseline)
            self.assertEqual(candidate["commit"], again["commit"])
            self.assertEqual(candidate["tree"], again["tree"])
            changed_author = copy.deepcopy(req)
            changed_author["commit_author"]["login"] = "another-owner"
            self.assertNotEqual(candidate["commit"], reconstruct(
                {"candidate.patch": GOOD_PATCH}, changed_author, baseline)["commit"])

    def test_invalid_freeze_timestamp_rejects_before_git(self):
        for timestamp in (None, True, -1, "100"):
            req = dict(request(), frozen_at=timestamp)
            with self.subTest(timestamp=timestamp), patch("loop.verify.git") as native, \
                    self.assertRaisesRegex(Rejected, "commit timestamp"):
                reconstruct({"candidate.patch": b""}, req, Mock())
            native.assert_not_called()
