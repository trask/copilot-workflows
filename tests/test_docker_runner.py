import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import test_docker as runner


class DockerRunnerTests(unittest.TestCase):
    def test_current_bytes_new_files_deletions_and_exclusions(self):
        with tempfile.TemporaryDirectory(prefix="source with spaces ") as directory:
            root = Path(directory)
            files = {
                "loop/api.py": b"dirty source",
                "tests/test_new.py": b"new test",
                "tools/test_docker.py": b"runner",
                ".github/workflows/validate.yml": b"fixture",
                "README.md": b"docs",
                ".git": b"gitdir: outside",
                ".env": b"private",
                "tests/.env": b"private",
                "tests/__pycache__/test_new.py": b"cache",
                "tests/unrelated/data.py": b"unrelated",
                "tools/credentials.py": b"private",
                ".copilot/state.json": b"private",
                ".tools/cache.py": b"cache",
                "out/report.json": b"artifact",
                "other/source.py": b"unrelated",
            }
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            listed = b"\0".join(name.encode() for name in [*files, "loop/deleted.py"]) + b"\0"
            with patch.object(runner, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=listed)) as launch:
                snapshot, count, size = runner.source_snapshot(root)
                (root / "loop" / "api.py").write_bytes(b"second edit")
                fresh, _, _ = runner.source_snapshot(root)
            self.assertEqual(root, launch.call_args.kwargs["cwd"])
            self.assertIn("--others", launch.call_args.args[0])
            self.assertEqual(5, count)
            self.assertEqual(sum(len(files[name]) for name in list(files)[:5]), size)
            with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive:
                self.assertEqual(set(list(files)[:5]), set(archive.getnames()))
                self.assertEqual(b"dirty source", archive.extractfile("loop/api.py").read())
                self.assertTrue(all(entry.isfile() and entry.uid == 1000
                                    for entry in archive.getmembers()))
            with tarfile.open(fileobj=io.BytesIO(fresh)) as archive:
                self.assertEqual(b"second edit", archive.extractfile("loop/api.py").read())

    def test_traversal_and_control_paths(self):
        for name in ("/loop/api.py", "../loop/api.py", "loop/../api.py",
                     "loop//api.py", "loop\\api.py", "C:/loop/api.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                runner.allowed_source(name)
        for name in (".git/config", ".env", "loop/.env.py", "tests/private.pem",
                     "tools/secret.py", "tests/test_new.py.log", "docs/.cache/report.md"):
            with self.subTest(name=name):
                self.assertFalse(runner.allowed_source(name))
        self.assertTrue(runner.allowed_source("tests/test with spaces.py"))

    def test_source_links_are_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "loop").mkdir()
            (root / "loop" / "api.py").write_bytes(b"not read")
            paths = subprocess.CompletedProcess([], 0, stdout=b"loop/api.py\0")
            with patch.object(runner, "run", return_value=paths), \
                    patch.object(Path, "is_junction", return_value=True), \
                    patch.object(Path, "read_bytes") as read, self.assertRaisesRegex(
                        ValueError, "links"):
                runner.source_snapshot(root)
            read.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Creating symlinks requires Windows privileges")
    def test_file_and_directory_symlinks_never_copy_outside_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "api.py").write_bytes(b"private")
            (root / "loop").symlink_to(outside, target_is_directory=True)
            paths = subprocess.CompletedProcess([], 0, stdout=b"loop/api.py\0")
            with patch.object(runner, "run", return_value=paths), self.assertRaisesRegex(
                    ValueError, "links"):
                runner.source_snapshot(root)
            (root / "loop").unlink()
            (root / "loop").mkdir()
            (root / "loop" / "api.py").symlink_to(outside / "api.py")
            with patch.object(runner, "run", return_value=paths), self.assertRaisesRegex(
                    ValueError, "links"):
                runner.source_snapshot(root)

    def test_subprocesses_are_bounded_and_hidden_on_windows(self):
        for system, expected in (("nt", 0x08000000), ("posix", 0)):
            with self.subTest(system=system), patch.object(runner.os, "name", system), \
                    patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                    patch.object(subprocess, "run") as launch:
                runner.run(["git", "--version"])
                self.assertEqual(expected, launch.call_args.kwargs["creationflags"])
                self.assertEqual(30, launch.call_args.kwargs["timeout"])
                self.assertNotIn("shell", launch.call_args.kwargs)

    def test_tools_image_is_cached_by_dockerfile_not_source(self):
        def installed(args, **kwargs):
            output = b"unix:///var/run/docker.sock" if "context" in args else b"linux\n"
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(runner, "run", side_effect=installed) as launch:
            first = runner.ensure_image(b"FROM pinned")
            self.assertEqual(first, runner.ensure_image(b"FROM pinned"))
            self.assertNotEqual(first, runner.ensure_image(b"FROM other-pin"))
        self.assertFalse(any("build" in call.args[0] for call in launch.call_args_list))

    def test_first_build_receives_only_dockerfile(self):
        responses = [
            subprocess.CompletedProcess([], 0, stdout=b"unix:///var/run/docker.sock"),
            subprocess.CompletedProcess([], 0, stdout=b"linux\n"),
            subprocess.CompletedProcess([], 1, stderr=b"Error: No such image: test"),
            subprocess.CompletedProcess([], 0),
        ]
        with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new_callable=io.StringIO), \
                patch.object(runner, "run", side_effect=responses) as launch:
            runner.ensure_image(b"FROM pinned")
        build = launch.call_args
        self.assertEqual("-", build.args[0][-1])
        self.assertEqual(b"FROM pinned", build.kwargs["input"])
        self.assertTrue(build.kwargs["check"])

    def test_unavailable_or_nonlinux_docker_never_falls_back(self):
        for response in (subprocess.CompletedProcess([], 0, stdout=b"windows\n"),
                         subprocess.CalledProcessError(1, ["docker", "info"])):
            with self.subTest(response=response), patch.dict(os.environ, {}, clear=True), patch.object(
                    runner, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, stdout=b"unix:///var/run/docker.sock"),
                        response]), self.assertRaises(
                        (RuntimeError, subprocess.CalledProcessError)):
                runner.ensure_image(b"FROM pinned")
        with patch.object(runner, "ensure_image", side_effect=FileNotFoundError("docker")), \
                patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO), \
                patch.object(runner, "source_snapshot") as snapshot:
            self.assertEqual(2, runner.main([]))
        snapshot.assert_not_called()

    def test_container_has_no_mount_credentials_or_network_and_keeps_argv(self):
        selector = "tests.test_loop.ArtifactTests; echo nope"
        with patch.dict(os.environ, {"GH_TOKEN": "must-not-copy"}):
            command = runner.container_command("image", "name", [selector])
        self.assertEqual(selector, command[-1])
        self.assertEqual("none", command[command.index("--network") + 1])
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop", command)
        self.assertIn("--tmpfs", command)
        self.assertEqual("1000:1000", command[command.index("--user") + 1])
        self.assertTrue(all(command[index + 1].endswith("_PROXY=") or
                            command[index + 1].endswith("_proxy=")
                            for index, arg in enumerate(command) if arg == "--env"))
        for forbidden in ("--volume", "--mount", "--env-all", "--env-file", "--privileged",
                          "--cap-add", "GH_TOKEN", "must-not-copy"):
            self.assertNotIn(forbidden, command)

    def test_native_failure_and_cleanup(self):
        responses = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0, stdout=b"false 73\n"),
            subprocess.CompletedProcess([], 0),
        ]
        with patch.object(runner, "run", side_effect=responses) as launch:
            self.assertEqual(73, runner.execute("image", b"source", ["tests.test_loop"], 17))
        calls = launch.call_args_list
        name = calls[0].args[0][calls[0].args[0].index("--name") + 1]
        self.assertEqual(b"source", calls[1].kwargs["input"])
        self.assertEqual(17, calls[1].kwargs["timeout"])
        self.assertEqual(["docker", "rm", "--force", name], calls[-1].args[0])

    def test_timeout_interrupt_and_attach_error_remove_only_created_container(self):
        for error in (subprocess.TimeoutExpired(["docker", "start"], 17),
                      KeyboardInterrupt(), RuntimeError("attach")):
            with self.subTest(error=error), patch.object(runner, "run", side_effect=[
                    subprocess.CompletedProcess([], 0), error,
                    subprocess.CompletedProcess([], 0)]) as launch, self.assertRaises(
                        type(error)):
                runner.execute("image", b"source", [], 17)
            self.assertEqual(["docker", "rm", "--force"], launch.call_args.args[0][:3])

    def test_cleanup_failure_is_explicit(self):
        with patch.object(runner, "run", side_effect=[
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=b"false 0\n"),
                subprocess.CompletedProcess([], 1, stderr=b"daemon error")]), \
                self.assertRaisesRegex(RuntimeError, "Cannot remove test container"):
            runner.execute("image", b"source", [], 17)

    def test_remote_docker_endpoint_never_receives_source(self):
        for environment in ({"DOCKER_HOST": "tcp://remote:2375"},
                            {"DOCKER_HOST": "ssh://remote"},
                            {"DOCKER_CONTEXT": "remote", "DOCKER_HOST": "unix:///ignored"}):
            with self.subTest(environment=environment), patch.dict(
                    os.environ, environment, clear=True), patch.object(
                    runner, "run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout=b"ssh://remote")) as launch, self.assertRaisesRegex(
                            RuntimeError, "local Docker engine"):
                runner.ensure_image(b"FROM pinned")
            self.assertTrue(all("context" in call.args[0] for call in launch.call_args_list))

    def test_main_preserves_exit_timeout_and_interrupt(self):
        for outcome, expected in ((73, 73), (subprocess.TimeoutExpired(["docker", "start"], 17), 124),
                                  (KeyboardInterrupt(), 130)):
            with self.subTest(outcome=outcome), patch.object(
                    runner, "ensure_image", return_value="image"), patch.object(
                    runner, "source_snapshot", return_value=(b"source", 1, 6)), patch.object(
                    runner, "execute", side_effect=[outcome]), patch(
                    "sys.stdout", new_callable=io.StringIO), patch(
                    "sys.stderr", new_callable=io.StringIO):
                self.assertEqual(expected, runner.main(["--timeout", "17", "tests.test_loop"]))

    def test_failed_create_and_attach_failure_do_not_report_success(self):
        error = subprocess.CalledProcessError(1, ["docker", "create"])
        with patch.object(runner, "run", side_effect=[
                error, subprocess.CompletedProcess([], 1, stderr=b"No such container")]), \
                self.assertRaises(subprocess.CalledProcessError):
            runner.execute("image", b"source", [], 17)
        with patch.object(runner, "run", side_effect=[
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 125),
                subprocess.CompletedProcess([], 0, stdout=b"false 0\n"),
                subprocess.CompletedProcess([], 0)]), self.assertRaisesRegex(
                    RuntimeError, "attach failed"):
            runner.execute("image", b"source", [], 17)
