import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from loop.live import main as live_main, publisher_for
from loop.candidates import TRAILER
from loop.policy import Rejected, canonical, commit_author
from loop.verify import git, verify
from loop.worker_output import package
from tests.test_live import Publisher, Read, TEST_TOKEN, live_state, personal_request, stored, zipped
from tests.test_loop import REVISION, baseline, run
from tests.fixtures import ROOT_PATH
from tests.support import semantic


class NativeCandidateTests(unittest.TestCase):
    def candidate(self, root, *, author=None, trailer=TRAILER):
        repository, output = Path(root, "target"), Path(root, "output")
        repository.mkdir()
        output.mkdir()
        git(["init", "--quiet"], repository)
        head = baseline(repository / ".git")
        request = personal_request(head)
        git(["checkout", "--quiet", "--detach", head], repository)
        name, email = commit_author(request)
        git(["config", "user.name", name], repository)
        git(["config", "user.email", author or email], repository)
        source = Path(repository, ROOT_PATH)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("values.length\n", encoding="utf-8")
        git(["add", ROOT_PATH], repository)
        git(["commit", "--quiet", "-m", "Handle the upper boundary\n\n" + trailer], repository)
        (output / "result.json").write_bytes(canonical(semantic(request, "fixes")))
        (output / "diagnostics.txt").write_bytes(b"Checked the boundary.")
        package(request, repository, output)
        def fetch(directory):
            git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-tags",
                 str(repository), head], directory)
        payload = zipped({path.name: path.read_bytes() for path in output.iterdir()})
        return request, repository, payload, fetch

    def test_worker_packages_ordinary_commits_and_verifier_preserves_their_identity(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as verified:
            request, repository, payload, fetch = self.candidate(root)
            report = verify(payload, request, run(), {"id": 33, "digest": "server"},
                            fetch, repository_dir=verified)
            candidate = report["candidate"]
            self.assertEqual(git(["rev-parse", "HEAD"], repository).decode().strip(), candidate["commit"])
            self.assertEqual("Handle the upper boundary", candidate["commits"][0]["subject"])
            self.assertEqual(request["frozen_sha"], candidate["commits"][0]["parent"])
            self.assertEqual({finding["key"]: candidate["commit"] for finding in request["findings"]},
                             candidate["finding_commits"])
            self.assertEqual(b"values.length\n", git(["show", "candidate:" + ROOT_PATH], verified))

    def test_description_only_result_packages_and_verifies_without_candidate_commits(self):
        with tempfile.TemporaryDirectory() as root:
            repository, output = Path(root, "target"), Path(root, "output")
            repository.mkdir()
            output.mkdir()
            git(["init", "--quiet"], repository)
            head = baseline(repository / ".git")
            request = personal_request(head)
            request["metadata"] = {"title": "Images", "body": "Keep tar.gz extraction."}
            git(["checkout", "--quiet", "--detach", head], repository)
            value = semantic(request, "no_change", disposition="description_updated")
            value["proposal"] = {"title": "Images", "body": "Use ZIP archives."}
            (output / "result.json").write_bytes(canonical(value))
            (output / "diagnostics.txt").write_bytes(b"Archive support and history confirm ZIP is intended.")
            package(request, repository, output)
            self.assertEqual(b"", (output / "candidate.bundle").read_bytes())
            def fetch(directory):
                git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-tags",
                     str(repository), head], directory)
            payload = zipped({path.name: path.read_bytes() for path in output.iterdir()})
            report = verify(payload, request, run(), {"id": 33, "digest": "server"}, fetch)
            self.assertEqual(value, report["dispositions"])
            self.assertFalse(report["candidate"]["changed"])
            self.assertEqual([], report["candidate"]["commits"])
            self.assertEqual({}, report["candidate"]["finding_commits"])
            self.assertEqual(head, report["candidate"]["commit"])

    def test_native_commits_cannot_change_author_or_omit_attribution(self):
        for options in ({"author": "someone@invalid"}, {"trailer": ""}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as root:
                with patch("loop.worker_output.verify_commit_author"):
                    request, _, payload, fetch = self.candidate(root, **options)
                with self.assertRaisesRegex(Rejected, "author|co-author"):
                    verify(payload, request, run(), {"id": 33, "digest": "server"}, fetch)

    def test_packager_rejects_wrong_commit_identity_before_creating_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(Rejected, "author differs from launch owner"):
                self.candidate(root, author="github-actions[bot]@users.noreply.github.com")
            self.assertFalse(Path(root, "output", "candidate.bundle").exists())

    def test_publication_does_not_accept_a_model_authored_patch_artifact(self):
        request = personal_request()
        payload = zipped({"result.json": canonical(semantic(request, "fixes")),
                          "candidate.patch": b"patch", "diagnostics.txt": b"Checked"})
        with self.assertRaisesRegex(Rejected, "artifact files"):
            verify(payload, request, run(), {"id": 33, "digest": "server"})

    def test_packager_rejects_a_no_change_claim_with_unreported_commits(self):
        with tempfile.TemporaryDirectory() as root:
            request, repository, _, _ = self.candidate(root)
            output = Path(root, "output")
            (output / "candidate.bundle").unlink()
            (output / "result.json").write_bytes(
                canonical(semantic(request, "no_change", disposition="not_warranted")))
            with self.assertRaisesRegex(Rejected, "unreported source changes"):
                package(request, repository, output)

    def test_one_trusted_job_verifies_and_pushes_the_same_native_objects(self):
        with tempfile.TemporaryDirectory() as root:
            request, repository, payload, _ = self.candidate(root)
            state = live_state(request, "verify_pending")
            store, name = stored(state)
            read = Read(request)
            publisher = Publisher(read)
            artifact = {"id": 33, "name": "candidate-24-1", "size_in_bytes": len(payload),
                        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(), "expired": False,
                        "workflow_run": {"id": 24, "head_sha": REVISION}}
            central = Mock()
            central.call.return_value = run()
            central.pages.side_effect = lambda _path, key: (
                [{"name": "agent", "conclusion": "success"}] if key == "jobs" else [artifact])
            central.artifact_zip.return_value = payload
            def source_git(args, directory):
                remote = "https://github.com/" + request["head_repo"] + ".git"
                if remote in args:
                    args = list(args)
                    args[args.index(remote)] = str(repository)
                    args = ["-c", "protocol.file.allow=always", *args]
                return git(args, directory)
            def push(directory, _request, commit, _token):
                self.assertEqual("publication_intent", store.entries[name]["stage"])
                self.assertEqual(commit, git(["rev-parse", "candidate"], directory).decode().strip())
                self.assertEqual(b"values.length\n", git(["show", "candidate:" + ROOT_PATH], directory))
                read.pr["head"]["sha"] = commit
            environment = {"PR": "1", "TARGET_REPO": request["repo"],
                           "REQUEST_ID": request["request_id"], "GENERATION": "6",
                           "GITHUB_SHA": REVISION, "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "99",
                           "EXPECTED_STAGE": "verify_pending"}
            self.addCleanup(Path("verification-report.json").unlink, missing_ok=True)
            with patch.dict(os.environ, environment, clear=True), \
                    patch("loop.live.API", return_value=central), \
                    patch("loop.live.State", return_value=store), \
                    patch("loop.live.publisher_for", return_value=publisher), \
                    patch("loop.live.git", return_value=REVISION.encode()), \
                    patch("loop.cli.git", return_value=REVISION.encode()), \
                    patch("loop.publication.git", side_effect=source_git), \
                    patch("loop.live.time.time", return_value=100), \
                    patch("loop.live.evidence", side_effect=AssertionError("Already verified")), \
                    patch("loop.live.authenticated_push", side_effect=push) as published:
                live_main()
            published.assert_called_once()
            self.assertEqual("waiting_review", store.entries[name]["stage"])
            self.assertEqual(git(["rev-parse", "HEAD"], repository).decode().strip(),
                             store.entries[name]["expected_sha"])
            central.artifact_zip.assert_called_once()

    def test_ci_rerun_selects_the_frozen_target_credential_after_verification(self):
        from tests.test_pr_tasks import task_request
        request = task_request("ci_fix")
        request["head_repo"] = "fork-owner/project"
        target_token = TEST_TOKEN + "_target"
        environment = {"PUBLISHER_SECRET_MAP": '{"fork-owner":"FORK_PUBLISH_TOKEN",'
                       '"example":"TARGET_PUBLISH_TOKEN"}',
                       "PUBLISHER_SECRET_NAME": "FORK_PUBLISH_TOKEN", "PUBLISHER_TOKEN": TEST_TOKEN,
                       "TARGET_PUBLISHER_SECRET_NAME": "TARGET_PUBLISH_TOKEN",
                       "TARGET_PUBLISHER_TOKEN": target_token}
        with patch.dict(os.environ, environment, clear=True):
            verification = publisher_for(request, None)
            rerun = publisher_for(request, "rerun")
        self.assertEqual(TEST_TOKEN, verification.token)
        self.assertEqual(target_token, rerun.token)
        self.assertFalse(verification.source_write)
        self.assertFalse(rerun.source_write)
