from tests.support import launch, reconstruct
from tests.fixtures import (FIXTURE, REPOSITORIES, TARGET)
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from loop.api import API
from loop.cli import attach_source, get_state
from loop.coordinator import (cancel, dispatch, reconcile)
from loop.freeze import freeze
from loop.policy import (Rejected, checkpoint_name, digest, eligible, parse_target, unchanged)
from loop.publication import import_candidate
from loop.source import (MAX_OBJECT, MAX_SOURCE, SourceAPI, bind_manifest, download_source,
                         gated_request, import_source, package_source, public_fetch,
                         snapshot_identity, source_api, source_metadata, target_api)
from loop.verify import git, object_bounds
from tests.test_loop import (FakeAPI, MemoryState, REVISION, SHA, baseline, pr, request, review)


def fixture_pr():
    value = pr()
    for side in ("head", "base"):
        value[side]["repo"] = {"id": REPOSITORIES[FIXTURE], "full_name": FIXTURE, "private": False}
    return value


def fixture_request():
    return dict(request(), **eligible(fixture_pr(), FIXTURE), loop_kind="self_review",
                findings=[], base_ref="main", base_sha=SHA, merge_base_sha=SHA)


class SourceTests(unittest.TestCase):
    def test_generic_explicit_target_identity_and_public_compatibility(self):
        self.assertEqual((TARGET, 20330), parse_target(TARGET + "#20330"))
        self.assertEqual((FIXTURE, 1), parse_target("https://github.com/" + FIXTURE + "/pull/1"))
        self.assertEqual(TARGET, eligible(pr())["repo"])
        self.assertEqual(FIXTURE, eligible(fixture_pr(), FIXTURE)["repo"])
        for name in ["trask/other", "attacker/copilot-review-loop-test"]:
            self.assertEqual((name, 1), parse_target("https://github.com/" + name + "/pull/1"))
        with self.assertRaises(Rejected):
            parse_target("20330")
        for changed in ("base", "head"):
            value = fixture_pr()
            value[changed]["repo"]["id"] = 77
            with self.assertRaises(Rejected):
                eligible(value, FIXTURE)
        value = pr()
        value["head"]["repo"]["id"] = value["base"]["repo"]["id"] = 77
        self.assertEqual(77, eligible(value)["repo_id"])

    def test_public_test_access_and_visibility_binding_never_use_source_auth(self):
        value = fixture_pr()
        for side in ("head", "base"):
            value[side]["repo"]["private"] = False
        req = dict(request(), **eligible(value, FIXTURE))
        with patch.object(API, "call", return_value={
                "id": REPOSITORIES[FIXTURE], "full_name": FIXTURE, "private": False}), \
                patch("loop.source.source_api") as source:
            central = API("central")
            api = target_api(central, FIXTURE)
            self.assertIs(central, api)
            source.assert_not_called()
        store = MemoryState()
        old_name, old = launch(store, gated_request(FIXTURE, 1, REVISION, 10), "shadow", True, False)
        name, state = launch(store, req, "shadow", True)
        self.assertNotEqual(old_name, name)
        self.assertEqual("ready", state["stage"])
        self.assertEqual(1, state["generation"])
        self.assertFalse(state["request"]["source_private"])
        self.assertEqual(old, store.entries[old_name])
        value["head"]["repo"]["private"] = value["base"]["repo"]["private"] = True
        with self.assertRaises(Rejected):
            unchanged(req, value)

    def test_namespaced_state_cancellation_and_stale_head(self):
        store = MemoryState()
        public, _ = launch(store, request(), "shadow", True)
        private, state = launch(store, fixture_request(), "shadow", True)
        self.assertNotEqual(public, private)
        self.assertEqual("pr-v2-1400255214-1.json", private)
        self.assertEqual("source_pending", state["stage"])
        self.assertEqual(private, get_state(store, 1, repo=FIXTURE)[0])
        cancel(store, private, store.entries[private]["request"]["request_id"],
               store.entries[private]["generation"], 20)
        self.assertEqual("ready", store.entries[public]["stage"])
        self.assertEqual("cancelled", store.entries[private]["stage"])
        live = fixture_pr()
        live["head"]["sha"] = "f" * 40
        with self.assertRaises(Rejected):
            unchanged(fixture_request(), live)

    def test_missing_auth_is_durable_unfrozen_gate_without_dispatch(self):
        store, api = MemoryState(), FakeAPI()
        req = gated_request(FIXTURE, 1, REVISION, 10)
        name, state = launch(store, req, "shadow", True, False)
        self.assertEqual("blocked", state["stage"])
        self.assertEqual("human_gate_target_repository_read_access", state["reason"])
        self.assertIsNone(state["request"]["frozen_sha"])
        self.assertEqual([], state["request"]["findings"])
        dispatch(store, name, api, 20)
        self.assertEqual([], api.calls)
        req = fixture_request()
        req["request_id"] = "e" * 32
        _, state = launch(store, req, "shadow", True, True)
        self.assertEqual("source_pending", state["stage"])
        self.assertEqual(1, state["generation"])

    def test_source_uncertain_claim_is_never_reacquired_by_watcher(self):
        store, api = MemoryState(), FakeAPI()
        name, state = launch(store, fixture_request(), "shadow", True)
        state["source_claim"] = {"run_id": 99, "run_attempt": 1}
        store.entries[name] = state
        reconcile(store, name, api, 1000)
        self.assertEqual("blocked", store.entries[name]["stage"])
        self.assertEqual("source_acquisition_uncertain_no_retry", store.entries[name]["reason"])
        self.assertEqual([], api.calls)

    def test_cancelled_source_attachment_cannot_dispatch(self):
        store, api = MemoryState(), FakeAPI()
        name, state = launch(store, fixture_request(), "shadow", True)
        cancel(store, name, store.entries[name]["request"]["request_id"],
               store.entries[name]["generation"], 20)
        from argparse import Namespace
        args = Namespace(pr="1", repo=FIXTURE, request_id=state["request"]["request_id"],
                         generation="1")
        with self.assertRaises(Rejected):
            attach_source(api, store, args)
        self.assertEqual([], api.calls)

    def test_read_credential_is_explicit_and_scoped_to_selected_repository(self):
        env = {"SOURCE_READ_TOKEN": "sentinel-source", "COPILOT_GITHUB_TOKEN": "sentinel-inference"}
        with patch.dict(os.environ, env):
            self.assertEqual("sentinel-source", source_api(FIXTURE).token)
            self.assertEqual(TARGET, target_api(API("central"), TARGET).repo)
        with patch.dict(os.environ, {"SOURCE_READ_TOKEN": "", "COPILOT_GITHUB_TOKEN": "inference"}):
            with self.assertRaises(Rejected):
                source_api(FIXTURE)

    def test_source_api_forbids_mutations_and_other_repositories(self):
        api = SourceAPI("sentinel", FIXTURE)
        for path, method in [(f"repos/{FIXTURE}/pulls/1", "PATCH"),
                             (f"repos/{TARGET}/pulls/1", "GET"),
                             ("repos/trask/copilot-workflows/git/refs", "POST")]:
            with self.subTest(path=path), self.assertRaises(Rejected):
                api.call(path, method)
        with patch.object(API, "call", return_value={"ok": True}) as parent:
            api.call(f"repos/{FIXTURE}/pulls/1")
            parent.assert_called_once()

    def test_public_fetch_has_no_credential_persistence_or_inference_environment(self):
        with patch.dict(os.environ, {"GH_TOKEN": "central", "COPILOT_GITHUB_TOKEN": "inference"}), \
                patch("loop.source.subprocess.run") as child:
            child.return_value = subprocess.CompletedProcess([], 0)
            public_fetch(Path.cwd(), fixture_request())
            argv = child.call_args.args[0]
            env = child.call_args.kwargs["env"]
            self.assertNotIn("source-sentinel", " ".join(argv))
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("COPILOT_GITHUB_TOKEN", env)
            self.assertNotIn("SOURCE_READ_TOKEN", env)
            self.assertNotIn("GIT_CONFIG_VALUE_0", env)
            self.assertEqual("0", env["GIT_CONFIG_COUNT"])
            self.assertIn("fetch.unpackLimit=1", argv)
            self.assertIn("pack.threads=1", argv)
            self.assertIn("--no-auto-maintenance", argv)
            self.assertEqual(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                             child.call_args.kwargs["creationflags"])
        for key in ("source_private", "target_private"):
            req = dict(fixture_request(), **{key: True})
            with patch("loop.source.subprocess.run") as child, self.assertRaises(Rejected):
                public_fetch(Path.cwd(), req)
            child.assert_not_called()

    def test_public_fetch_failure_reports_git_error_and_exact_source(self):
        with patch("loop.source.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 128, stderr=b"fatal: unable to create pack\n")), self.assertRaisesRegex(
                Rejected, FIXTURE + "@" + SHA + r" .*exit 128.*fatal: unable to create pack"):
            public_fetch(Path.cwd(), fixture_request())

    def test_public_review_freeze_uses_exact_repo_and_thread_variables(self):
        class Reviews:
            def call(self, path):
                from tests.test_loop import BOT
                return BOT if path.startswith("users/") else fixture_pr()

            def pages(self, path):
                self.assert_path = path
                return [review()] if path.endswith("/reviews") else []

            def graphql(self, query, variables):
                assert variables["owner"] == FIXTURE.split("/")[0]
                assert variables["name"] == FIXTURE.split("/")[1]
                return {"repository": {"pullRequest": {"reviewThreads": {
                    "pageInfo": {"hasNextPage": False}, "nodes": []}}}}
        req = freeze(Reviews(), 1, REVISION, now=10, repo=FIXTURE)
        self.assertEqual(FIXTURE, req["repo"])
        self.assertEqual(SHA, req["frozen_sha"])
        self.assertEqual(["review:12"], [f["key"] for f in req["findings"]])

    def package(self, directory):
        req = fixture_request()
        with tempfile.TemporaryDirectory() as bare:
            git(["init", "--bare", "--quiet"], bare)
            req["frozen_sha"] = baseline(bare)
            req["base_sha"] = req["merge_base_sha"] = req["frozen_sha"]
        class Live:
            token = "not-used"
            def call(self, path):
                if "/git/ref/" in path:
                    return {"object": {"sha": req["base_sha"]}}
                value = fixture_pr()
                value["head"]["sha"] = req["frozen_sha"]
                return value
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1"}), \
                patch("loop.source.time.time", return_value=100):
            def fetch(destination):
                sha = baseline(destination)
                git(["update-ref", "refs/heads/review-base", sha], destination)
                return sha
            manifest = package_source(req, 1, Live(), Path(directory, "package"), fetch)
        return req, manifest, Path(directory, "package")

    def test_exact_snapshot_roundtrip_and_candidate_binding(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as restored:
            req, manifest, package = self.package(directory)
            git(["init", "--bare", "--quiet"], restored)
            import_source(restored, package / "source.bundle", manifest, req)
            self.assertEqual(req["frozen_sha"], git(["rev-parse", "snapshot"], restored).decode().strip())
            def snapshot(destination):
                import_source(destination, package / "source.bundle", manifest, req)
            candidate = reconstruct({"candidate.patch": b""}, req, snapshot)
            self.assertEqual(req["frozen_sha"], candidate["parent"])
            for key, value in [("repo_id", 77), ("frozen_sha", SHA), ("generation", 2),
                               ("run_attempt", 2), ("request_digest", "0" * 64)]:
                wrong = dict(manifest, **{key: value})
                with self.subTest(key=key), self.assertRaises(Rejected):
                    bind_manifest(wrong, req, 1)
            bad_bundle = package / "bad.bundle"
            bad_bundle.write_bytes(b"invalid")
            with self.assertRaises(Rejected):
                import_source(restored, bad_bundle, manifest, req)

    def test_large_two_tree_snapshot_verifies_and_imports_a_fix(self):
        from tests.test_loop import GOOD_PATCH
        from tests.test_self_review import SelfRead, self_request
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "original")
            source.mkdir()
            git(["init", "--bare", "--quiet"], source)
            stream = bytearray(b"blob\nmark :1\ndata 4\nold\n\n")
            for index in range(11000):
                content = str(index).encode().ljust(3072, b"x")
                stream.extend(f"blob\nmark :{index + 2}\ndata {len(content)}\n".encode())
                stream.extend(content + b"\n")
            stream.extend(b"commit refs/heads/review-base\nmark :11002\n"
                          b"committer T <t@invalid> 0 +0000\ndata 5\nbase\n"
                          b"M 100644 :1 Foo.java\n")
            for index in range(11000):
                stream.extend(f"M 100644 :{index + 2} file-{index}.txt\n".encode())
            stream.extend(b"\ncommit refs/heads/snapshot\ncommitter T <t@invalid> 0 +0000\n"
                          b"data 5\nhead\nfrom :11002\n\ndone\n")
            git(["fast-import", "--quiet"], source, bytes(stream))
            del stream
            req = self_request()
            req["frozen_sha"] = git(["rev-parse", "snapshot"], source).decode().strip()
            req["base_sha"] = req["merge_base_sha"] = git(
                ["rev-parse", "review-base"], source).decode().strip()
            sizes = object_bounds(source)
            self.assertGreater(len(sizes), 10000)
            self.assertGreater(sum(sizes.values()), MAX_SOURCE // 2)

            def fetch(destination):
                git(["-c", "protocol.file.allow=always", "fetch", "--quiet",
                     "--no-auto-maintenance", str(source),
                     "refs/heads/review-base:refs/heads/review-base",
                     "refs/heads/snapshot:refs/heads/snapshot"], destination)
                Path(destination, "FETCH_HEAD").write_text(req["frozen_sha"] + "\n", encoding="ascii")

            with patch.dict(os.environ, {"GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1"}), \
                    patch("loop.source.time.time", return_value=100):
                manifest = package_source(req, 6, SelfRead(req), Path(directory, "source"), fetch)
            self.assertEqual(2, manifest["history_count"])

            def snapshot(destination):
                import_source(destination, Path(directory, "source", "source.bundle"), manifest, req)

            package = Path(directory, "candidate")
            candidate = reconstruct({"candidate.patch": GOOD_PATCH}, req, snapshot, package)
            imported = Path(directory, "imported")
            imported.mkdir()
            import_candidate(imported, package / "candidate.bundle", req, candidate, snapshot)
            self.assertEqual(b"new\n", git(["show", candidate["commit"] + ":Foo.java"], imported))
            self.assertGreater(len(object_bounds(imported)), 10000)

    def test_expanded_tree_limit_counts_repeated_blob_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            git(["init", "--bare", "--quiet"], directory)
            blob = git(["hash-object", "-w", "--stdin"], directory,
                       b"x" * MAX_OBJECT).decode().strip()
            tree = git(["mktree"], directory, "".join(
                f"100644 blob {blob}\tfile-{index}.txt\n"
                for index in range(MAX_SOURCE // MAX_OBJECT + 1)).encode()).decode().strip()
            sha = git(["hash-object", "-t", "commit", "-w", "--stdin"], directory,
                      (f"tree {tree}\nauthor T <t@invalid> 0 +0000\n"
                       "committer T <t@invalid> 0 +0000\n\nsnapshot\n").encode()).decode().strip()
            git(["update-ref", "refs/heads/snapshot", sha], directory)
            with self.assertRaisesRegex(Rejected, "tree exceeds expanded limit"):
                snapshot_identity(directory, sha)

    def test_snapshot_reconstruction_uses_only_credential_free_git_without_checkout_or_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            req, source_manifest, source_package = self.package(directory)
            root = Path(directory)
            package = root / "candidate-package"
            def snapshot(destination):
                import_source(destination, source_package / "source.bundle", source_manifest, req)
            with patch("loop.verify.subprocess.run", wraps=subprocess.run) as child, \
                    patch.dict(os.environ, {"GH_TOKEN": "central", "SOURCE_READ_TOKEN": "source",
                                            "COPILOT_GITHUB_TOKEN": "inference"}):
                candidate = reconstruct({"candidate.patch": b""}, req, snapshot, package)
            self.assertEqual(req["frozen_sha"], candidate["parent"])
            self.assertGreater(child.call_count, 0)
            for call in child.call_args_list:
                self.assertEqual("git", call.args[0][0])
                self.assertIn("pack.threads=1", call.args[0])
                self.assertNotIn("checkout", call.args[0])
                self.assertFalse(any("https://github.com/" in arg for arg in call.args[0]))
                self.assertFalse(any("TOKEN" in key for key in call.kwargs["env"]))

    def test_source_download_checks_api_provenance_and_archive_members(self):
        with tempfile.TemporaryDirectory() as directory:
            req, manifest, package = self.package(directory)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                for file in package.iterdir():
                    archive.writestr(file.name, file.read_bytes())
            payload = output.getvalue()
            source = {"manifest": manifest, "artifact_id": 55,
                      "artifact_digest": "sha256:" + hashlib.sha256(payload).hexdigest()}
            run = {"id": 99, "repository": {"full_name": "trask/copilot-workflows"},
                   "head_repository": {"full_name": "trask/copilot-workflows"},
                   "event": "workflow_dispatch", "path": ".github/workflows/coordinator.yml",
                   "head_sha": REVISION, "run_attempt": 1}
            artifact = {"id": 55, "name": "source-" + req["request_id"], "digest": source["artifact_digest"],
                        "workflow_run": {"id": 99, "head_sha": REVISION}, "expired": False,
                        "size_in_bytes": len(payload)}
            class Central:
                def call(self, path):
                    return run if "/runs/" in path else artifact
                def artifact_zip(self, *_args):
                    return payload
            state = {"request": req, "generation": 1, "source": source}
            download_source(Central(), state, Path(directory, "received"))
            artifact["workflow_run"]["id"] = 100
            with self.assertRaises(Rejected):
                download_source(Central(), state, Path(directory, "wrong"))
            artifact["workflow_run"]["id"] = 99
            run["head_sha"] = "f" * 40
            with self.assertRaises(Rejected):
                source_metadata(Central(), source, req)
            run["head_sha"] = REVISION
            for event in ("workflow_dispatch", "workflow_run", "schedule"):
                run["event"] = event
                source_metadata(Central(), source, req)
            run["event"] = "pull_request"
            with self.assertRaises(Rejected):
                source_metadata(Central(), source, req)
            run["event"] = "workflow_dispatch"
            for name, mode in [("../source.bundle", stat.S_IFREG), ("source.bundle", stat.S_IFLNK)]:
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = (mode | 0o644) << 16
                    archive.writestr(info, b"invalid")
                    archive.writestr("manifest.json", b"{}")
                payload = output.getvalue()
                source["artifact_digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
                artifact["digest"] = source["artifact_digest"]
                artifact["size_in_bytes"] = len(payload)
                with self.subTest(name=name), self.assertRaises(Rejected):
                    download_source(Central(), state, Path(directory, "hostile"))


if __name__ == "__main__":
    unittest.main()
