"""Run offline unittests with Linux Git and a fresh, local source snapshot."""

import argparse
import hashlib
import io
import os
import stat
import subprocess
import sys
import tarfile
import time
import unittest
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "tools" / "test.Dockerfile"
SOURCE_DIRS = {
    "loop": {".py"},
    "tests": {".py"},
    "tools": {".py"},
    "docs": {".md"},
    ".github/workflows": {".yml", ".md"},
    ".github/aw": {".json"},
}
SOURCE_FILES = {"README.md", ".gitattributes", ".gitignore", "tools/test.Dockerfile"}
BOOTSTRAP = """
import os, sys, tarfile, time
started = time.perf_counter()
os.makedirs('/tmp/source')
os.makedirs('/tmp/home', mode=0o700)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    archive.extractall('/tmp/source', filter='data')
print(f'Source extraction {time.perf_counter() - started:.3f}s', flush=True)
os.chdir('/tmp/source')
sys.path.insert(0, '/tmp/source')
from tools.test_docker import container_main
raise SystemExit(container_main(sys.argv[1:]))
"""


def run(args, *, timeout=30, **kwargs):
    return subprocess.run(
        args, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        **kwargs,
    )


def allowed_source(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError(f"Unsafe source path: {name!r}")
    if name in SOURCE_FILES:
        return True
    return (str(path.parent) in SOURCE_DIRS
            and path.suffix in SOURCE_DIRS[str(path.parent)]
            and not path.name.startswith(".")
            and not path.stem.casefold().startswith(("secret", "credential", "token", "password")))


def source_snapshot(root):
    paths = run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
         "--", *SOURCE_DIRS, *SOURCE_FILES],
        cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    buffer = io.BytesIO()
    count = total = 0
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in sorted({os.fsdecode(value) for value in paths.split(b"\0") if value}):
            if not allowed_source(name):
                continue
            path = root
            try:
                for part in PurePosixPath(name).parts:
                    path = path / part
                    mode = path.lstat().st_mode
                    if stat.S_ISLNK(mode) or path.is_junction():
                        raise ValueError(f"Source links are not copied: {name}")
            except FileNotFoundError:
                # Git's index can still list files deleted in the working tree.
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f"Source is not a regular file: {name}")
            size = path.stat().st_size
            if size > 2 * 1024 * 1024 or total + size > 16 * 1024 * 1024:
                raise ValueError(f"Source snapshot exceeds size limit: {name}")
            content = path.read_bytes()
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            entry.mode = 0o644
            entry.uid = entry.gid = 1000
            archive.addfile(entry, io.BytesIO(content))
            count += 1
            total += len(content)
    return buffer.getvalue(), count, total


def ensure_image(dockerfile):
    image = "copilot-workflows-unit:" + hashlib.sha256(dockerfile).hexdigest()[:16]
    endpoint = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
    if not endpoint:
        endpoint = run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()
    if not endpoint.startswith(("unix://", "npipe://")):
        raise RuntimeError("Use a local Docker engine; source is never sent to a remote Docker host.")
    operating_system = run(
        ["docker", "info", "--format", "{{.OSType}}"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()
    if operating_system != b"linux":
        raise RuntimeError("Docker must use Linux containers. Start Docker Desktop's Linux engine.")
    exists = run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if exists.returncode:
        if b"No such image" not in exists.stderr:
            raise RuntimeError(exists.stderr.decode(errors="replace").strip())
        print(f"Building reusable tools-only image {image}", flush=True)
        run(["docker", "build", "--quiet", "--tag", image, "-"], input=dockerfile,
            check=True, timeout=600)
    return image


def container_command(image, name, selectors):
    proxy_env = [argument for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY")
                 for spelling in (key, key.lower()) for argument in ("--env", spelling + "=")]
    return [
        "docker", "create", "--name", name, "--interactive", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "1000:1000",
        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
        "--workdir", "/tmp",
        *proxy_env, image, "python3", "-u", "-c", BOOTSTRAP, *selectors,
    ]


def execute(image, snapshot, selectors, timeout):
    name = "copilot-workflows-unit-" + uuid.uuid4().hex
    created = False
    started = time.perf_counter()
    try:
        run(container_command(image, name, selectors), check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        created = True
        print(f"Container create {time.perf_counter() - started:.3f}s", flush=True)
        attached = run(["docker", "start", "--attach", "--interactive", name],
                       input=snapshot, timeout=timeout)
        state = run(
            ["docker", "inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", name],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.split()
        if len(state) != 2 or state[0] != b"false":
            raise RuntimeError("Docker did not report a completed test container.")
        exit_code = int(state[1])
        if attached.returncode and attached.returncode != exit_code:
            raise RuntimeError(f"Docker attach failed with exit {attached.returncode}.")
        return exit_code
    finally:
        removed = run(["docker", "rm", "--force", name],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if removed.returncode and (created or b"No such container" not in removed.stderr):
            raise RuntimeError("Cannot remove test container " + name + ": "
                               + removed.stderr.decode(errors="replace").strip())


def container_main(selectors):
    git = run(["git", "--version"], check=True, stdout=subprocess.PIPE).stdout.decode().strip()
    mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    filesystem = next(line.split(" - ", 1)[1].split()[0]
                      for line in mounts if line.split()[4] == "/tmp")
    if sys.platform != "linux" or filesystem != "tmpfs":
        raise RuntimeError("Tests require Linux tmpfs storage.")
    if {path.name for path in Path("/sys/class/net").iterdir()} != {"lo"}:
        raise RuntimeError("Test container must have no network interface except loopback.")
    print(f"Linux Python {sys.version.split()[0]}, {git}; "
          f"source={Path.cwd()}, filesystem={filesystem}, uid={os.getuid()}, "
          "network=loopback only", flush=True)
    started = time.perf_counter()
    program = unittest.main(module=None, argv=["unittest", *(selectors or ["discover"]), "-q", "-b"],
                            exit=False)
    result = program.result
    print(f"Test body {time.perf_counter() - started:.3f}s; "
          f"{result.testsRun} tests, {len(result.skipped)} skipped", flush=True)
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=300,
                        help="test container timeout in seconds (default: 300)")
    parser.add_argument("selectors", nargs="*", help="unittest module, class or method names")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    started = time.perf_counter()
    try:
        image = ensure_image(DOCKERFILE.read_bytes())
        setup = time.perf_counter() - started
        snapshot, count, size = source_snapshot(ROOT)
        copied = time.perf_counter()
        print(f"Image {image}; setup/check {setup:.3f}s; "
              f"snapshot {copied - started - setup:.3f}s, {count} files, {size} bytes", flush=True)
        return execute(image, snapshot, args.selectors, args.timeout)
    except FileNotFoundError as error:
        print(f"Missing Docker, Git or source file: {error}. "
              "Install Docker and Git and start the Linux Docker engine.", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as error:
        print(f"Timed out after {error.timeout}s: {error.cmd[0:2]}", file=sys.stderr)
        return 124
    except subprocess.CalledProcessError as error:
        print(f"Command failed: {error.cmd[0:3]} (exit {error.returncode}). "
              "Check Docker Desktop and network access for the first image build.", file=sys.stderr)
        if error.stderr:
            print(error.stderr.decode(errors="replace").strip(), file=sys.stderr)
        return error.returncode
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; test container removed.", file=sys.stderr)
        return 130
    finally:
        print(f"Total {time.perf_counter() - started:.3f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
