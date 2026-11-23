"""The program a Lean environment preparation VM runs, as root, with the standard library only.

The relay sends this file's source as the VM's program and the preparation plan as its standard input. The VM sees one
directory of the organization's Modal Volume, mounted at ROOT; nothing else of the Volume is visible to it. It:

1. downloads the Lean release named by the plan and checks it against GitHub's SHA-256 for that file;
2. checks that each package's pinned commit is on a branch or tag of the named repository, then fetches exactly that
   commit (no redirects, no submodules, no hooks);
3. writes a Lake workspace that requires every package, with a manifest pinning each to its commit;
4. as an unprivileged user (BUILD_USER) in a network namespace of its own, lets mathlib's cache tool read mathlib's
   compiled files through a loopback gateway that fetches only cache files from CACHE_HOST, then builds the
   remaining packages with no network;
5. kills every process of that user, and records a seal: every file's path, mode, size and SHA-256.

Only steps 1 and 2 use the network, and they run only this program. Dependency code (Lake configurations, mathlib's
cache tool, the Lean build) runs only in step 4. Progress goes to standard error; the last line of standard output is
`PREPARE-RESULT {json}`.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = "/opt/lean-env"
TOOLCHAIN = ROOT + "/toolchain"
WORKSPACE = ROOT + "/workspace"
PACKAGES = WORKSPACE + "/.lake/packages"
SEAL = "seal.json"
# VM-local scratch, never part of the environment: downloads, git checks, the build user's home and mathlib's cache.
SCRATCH = "/var/tmp/lean-environment"
BUILD_USER = 10001
CACHE_HOST = "lakecache.blob.core.windows.net"
DOWNLOAD_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
WORKSPACE_NAME = "reasoning_lean_environment"
TOOLCHAIN_BYTES = 2 * 1024**3
LOG_LINE = 4096

VERSION = re.compile(r"v4\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})(?:-rc[1-9][0-9]{0,2})?\Z")
PACKAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
PACKAGE_URL = re.compile(r"https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
PATH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ATTEMPT = re.compile(r"[0-9a-f]{32}\Z")

GIT_OPTIONS = (
    "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false",
    "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "fetch.recurseSubmodules=false",
    "-c", "submodule.recurse=false", "-c", "advice.detachedHead=false", "-c", "init.defaultBranch=main",
)


class PrepareError(Exception):
    def __init__(self, step: str, message: str):
        super().__init__(message)
        self.step, self.message = step, message


def log(text: str) -> None:
    sys.stderr.write(text[:LOG_LINE].rstrip("\n") + "\n")
    sys.stderr.flush()


def plan_problems(plan: object) -> list[str]:
    """Why a plan cannot be prepared; empty when it can. The relay checked the specification already."""
    if not isinstance(plan, dict) or set(plan) != {"format", "key", "attempt", "toolchain", "packages", "seconds"}:
        return ["the plan has unexpected fields"]
    problems = []
    if plan["format"] != 1:
        problems.append("unknown plan format")
    if not isinstance(plan["key"], str) or not SHA256.fullmatch(plan["key"]):
        problems.append("the key is not a SHA-256")
    if not isinstance(plan["attempt"], str) or not ATTEMPT.fullmatch(plan["attempt"]):
        problems.append("the attempt is not an identifier")
    if type(plan["seconds"]) is not int or not 60 <= plan["seconds"] <= 86400:
        problems.append("the time allowance is out of range")
    toolchain = plan["toolchain"]
    if not isinstance(toolchain, dict) or set(toolchain) != {"version", "url", "sha256", "bytes"}:
        problems.append("the toolchain is malformed")
    else:
        version = toolchain["version"]
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            problems.append("the toolchain is not an official Lean release")
        elif toolchain["url"] != release_url(version):
            problems.append("the toolchain URL is not the release's Linux archive")
        if not isinstance(toolchain["sha256"], str) or not SHA256.fullmatch(toolchain["sha256"]):
            problems.append("the toolchain has no SHA-256")
        if type(toolchain["bytes"]) is not int or not 0 < toolchain["bytes"] <= TOOLCHAIN_BYTES:
            problems.append("the toolchain size is out of range")
    packages = plan["packages"]
    if not isinstance(packages, list) or not 1 <= len(packages) <= 64:
        return [*problems, "the plan needs between 1 and 64 packages"]
    names = []
    for package in packages:
        if not isinstance(package, dict) or set(package) != {"name", "url", "rev", "sub_dir"}:
            problems.append("a package is malformed")
            continue
        names.append(package["name"])
        if not isinstance(package["name"], str) or not PACKAGE_NAME.fullmatch(package["name"]):
            problems.append("a package name is not plain")
        if not isinstance(package["url"], str) or not PACKAGE_URL.fullmatch(package["url"]):
            problems.append(f"package {package['name']} is not on github.com")
        if not isinstance(package["rev"], str) or not REVISION.fullmatch(package["rev"]):
            problems.append(f"package {package['name']} is not pinned to a commit")
        sub_dir = package["sub_dir"]
        if sub_dir is not None and (not isinstance(sub_dir, str) or not 1 <= len(sub_dir.split("/")) <= 8 or any(
                not PATH_PART.fullmatch(part) or part in {".", ".."} for part in sub_dir.split("/"))):
            problems.append(f"package {package['name']} has an unsafe sub-directory")
    if not all(isinstance(name, str) for name in names) or len(set(names)) != len(names) or names != sorted(names):
        problems.append("packages must have distinct names, in order")
    return problems


def release_url(version: str) -> str:
    return f"https://github.com/leanprover/lean4/releases/download/{version}/lean-{version[1:]}-linux.tar.zst"


@contextlib.contextmanager
def step(record: dict, name: str):
    began = time.monotonic()
    log(f"== {name}")
    try:
        yield
    finally:
        record.setdefault("seconds", {})[name] = round(time.monotonic() - began, 1)


class Deadline:
    def __init__(self, seconds: int, clock=time.monotonic):
        self.clock, self.end = clock, clock() + seconds

    def left(self, step_name: str, minimum: int = 30) -> int:
        remaining = int(self.end - self.clock())
        if remaining < minimum:
            raise PrepareError(step_name, "the preparation ran out of time")
        return remaining


# --- Downloads -----------------------------------------------------------------------------------------------------


class Redirects(urllib.request.HTTPRedirectHandler):
    """Follow a release download only to GitHub's own hosts, over HTTPS."""

    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if (parts.scheme != "https" or parts.hostname not in DOWNLOAD_HOSTS or parts.port not in (None, 443)
                or parts.username or parts.password):
            raise PrepareError("toolchain", f"the release download redirected to {parts.hostname!r}, which is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, destination: str, expected_sha256: str, expected_bytes: int, *, timeout: int,
             opener=None) -> None:
    """Download `url` to `destination`, refusing anything but exactly the expected bytes."""
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirects)
    digest, size = hashlib.sha256(), 0
    deadline = time.monotonic() + timeout
    try:
        with opener.open(url, timeout=60) as response, open(destination, "wb") as stream:
            while True:
                if time.monotonic() > deadline:
                    raise PrepareError("toolchain", "the release download ran out of time")
                block = response.read(1 << 20)
                if not block:
                    break
                size += len(block)
                if size > expected_bytes:
                    raise PrepareError("toolchain", "the release archive is larger than GitHub reports")
                digest.update(block)
                stream.write(block)
    except urllib.error.URLError as exc:
        raise PrepareError("toolchain", f"the release download failed ({str(exc.reason)[:200]})") from None
    if size != expected_bytes or digest.hexdigest() != expected_sha256:
        raise PrepareError("toolchain", "the release archive does not match GitHub's SHA-256 for it")


def install_toolchain(plan: dict, record: dict, deadline: Deadline) -> None:
    toolchain = plan["toolchain"]
    archive = os.path.join(SCRATCH, "toolchain.tar.zst")
    download(toolchain["url"], archive, toolchain["sha256"], toolchain["bytes"], timeout=deadline.left("toolchain"))
    os.makedirs(TOOLCHAIN, mode=0o755)
    # Our tar, on an archive whose digest matched: no owners from the archive, nothing outside the target.
    run(["tar", "--zstd", "--extract", "--file", archive, "--directory", TOOLCHAIN, "--strip-components=1",
         "--no-same-owner", "--no-same-permissions"], step_name="toolchain", timeout=deadline.left("toolchain"))
    os.unlink(archive)
    for tool in ("lean", "lake"):
        path = os.path.join(TOOLCHAIN, "bin", tool)
        if not os.path.isfile(path) or os.path.islink(path) or not os.access(path, os.X_OK):
            raise PrepareError("toolchain", f"the release has no bin/{tool}")
    record["toolchain"] = {"version": toolchain["version"], "sha256": toolchain["sha256"], "bytes": toolchain["bytes"]}


# --- Git -----------------------------------------------------------------------------------------------------------


def git_environment() -> dict:
    home = os.path.join(SCRATCH, "git-home")
    os.makedirs(home, mode=0o700, exist_ok=True)
    return {"PATH": "/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}


def git(args: list[str], *, cwd: str | None = None, timeout: int = 600, check: bool = True,
        options: tuple[str, ...] = GIT_OPTIONS) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *options, *args], cwd=cwd, env=git_environment(), stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no detail"]
        raise PrepareError("packages", f"git {args[0]} failed: {detail[0][:300]}")
    return result


def _not_promisor(repository: str, options: tuple[str, ...]) -> None:
    # A filtered fetch makes the remote a promisor, and a promisor lazily fetches missing objects by hash, which GitHub
    # serves from any fork. With the promisor settings gone, a missing commit stays missing.
    for name in ("remote.origin.promisor", "remote.origin.partialclonefilter", "extensions.partialclone"):
        git(["config", "--unset-all", name], cwd=repository, check=False, options=options)


def reachable(package: dict, scratch: str, *, timeout: int, options: tuple[str, ...] = GIT_OPTIONS) -> str:
    """The first ref of the named repository whose history contains the pinned commit: its default branch, then its
    tags, then its other branches. GitHub serves any commit of a fork network under every repository in it, so a hash
    alone does not show that the commit is on the named repository."""
    repository = os.path.join(scratch, package["name"] + ".git")
    git(["init", "--quiet", "--bare", repository], options=options)
    git(["remote", "add", "origin", package["url"]], cwd=repository, options=options)
    tiers = (["+HEAD:refs/remotes/origin/HEAD"], ["+refs/tags/*:refs/tags/*"], ["+refs/heads/*:refs/remotes/origin/*"])
    for refspecs in tiers:
        git(["fetch", "--quiet", "--filter=tree:0", "--no-tags", "origin", *refspecs], cwd=repository, timeout=timeout,
            options=options)
        _not_promisor(repository, options)
        present = git(["cat-file", "-e", package["rev"] + "^{commit}"], cwd=repository, check=False, options=options)
        if present.returncode:
            continue
        found = git(["for-each-ref", "--contains", package["rev"], "--count=1", "--format=%(refname)",
                     "refs/remotes/origin", "refs/tags"], cwd=repository, options=options)
        ref = found.stdout.decode("utf-8", "replace").strip()
        if ref:
            shutil.rmtree(repository)
            return describe_ref(ref)
    shutil.rmtree(repository)
    raise PrepareError("packages", f"package {package['name']}: commit {package['rev']} is on no branch or tag of "
                                   f"{package['url']}")


def describe_ref(ref: str) -> str:
    if ref == "refs/remotes/origin/HEAD":
        return "default branch"
    if ref.startswith("refs/tags/"):
        return "tag " + ref.removeprefix("refs/tags/")
    return "branch " + ref.removeprefix("refs/remotes/origin/")


def checkout(package: dict, directory: str, *, timeout: int, options: tuple[str, ...] = GIT_OPTIONS) -> str:
    """Fetch only the pinned commit into `directory`, with the manifest's URL as its remote, and return its Lake
    configuration file. Lake compares that remote with the manifest, and offline it does not fetch a package whose
    checkout is already at the pinned commit."""
    git(["init", "--quiet", directory], options=options)
    git(["remote", "add", "origin", package["url"]], cwd=directory, options=options)
    git(["fetch", "--quiet", "--depth=1", "--no-tags", "origin", package["rev"]], cwd=directory, timeout=timeout,
        options=options)
    git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=directory, options=options)
    head = git(["rev-parse", "HEAD"], cwd=directory, options=options).stdout.decode().strip()
    if head != package["rev"]:
        raise PrepareError("packages", f"package {package['name']}: the checkout is not at the pinned commit")
    base = os.path.realpath(directory)
    root = os.path.realpath(os.path.join(directory, package["sub_dir"] or "."))
    if (root != base and not root.startswith(base + os.sep)) or not os.path.isdir(root):
        raise PrepareError("packages", f"package {package['name']}: its sub-directory is missing or leaves the package")
    for name in ("lakefile.lean", "lakefile.toml"):
        path = os.path.join(root, name)
        if os.path.isfile(path) and not os.path.islink(path):
            return name
    raise PrepareError("packages", f"package {package['name']} has no Lake configuration")


def fetch_packages(plan: dict, record: dict, deadline: Deadline) -> dict[str, str]:
    scratch = os.path.join(SCRATCH, "reachability")
    os.makedirs(scratch, mode=0o700, exist_ok=True)
    os.makedirs(PACKAGES, mode=0o755)
    configs, record["packages"] = {}, []
    for package in plan["packages"]:
        began = time.monotonic()
        log(f"   {package['name']}: {package['url']} at {package['rev']}")
        ref = reachable(package, scratch, timeout=deadline.left("packages"))
        configs[package["name"]] = checkout(package, os.path.join(PACKAGES, package["name"]),
                                            timeout=deadline.left("packages"))
        record["packages"].append({"name": package["name"], "on": ref, "seconds": round(time.monotonic() - began, 1)})
    return configs


# --- The Lake workspace ----------------------------------------------------------------------------------------------


def lakefile(plan: dict) -> str:
    """A root package that requires every package of the environment at its pinned commit."""
    lines = [f"name = {json.dumps(WORKSPACE_NAME)}", ""]
    for package in plan["packages"]:
        lines += ["[[require]]", f"name = {json.dumps(package['name'])}", f"git = {json.dumps(package['url'])}",
                  f"rev = {json.dumps(package['rev'])}"]
        if package["sub_dir"] is not None:
            lines.append(f"subDir = {json.dumps(package['sub_dir'])}")
        lines.append("")
    return "\n".join(lines)


def manifest(plan: dict, configs: dict[str, str]) -> dict:
    """The workspace's Lake manifest: every package pinned, so Lake materializes them without fetching."""
    return {
        "version": "1.1.0", "name": WORKSPACE_NAME, "lakeDir": ".lake", "packagesDir": ".lake/packages",
        "packages": [{"type": "git", "name": package["name"], "scope": "", "inherited": False,
                      "configFile": configs[package["name"]], "manifestFile": "lake-manifest.json",
                      "url": package["url"], "rev": package["rev"], "inputRev": package["rev"],
                      "subDir": package["sub_dir"]} for package in plan["packages"]],
    }


def read_regular(path: str, limit: int) -> bytes | None:
    """A regular file's first `limit` bytes; None for anything else. Never follows a final link or waits on a pipe."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as stream:
        return stream.read(limit) if stat.S_ISREG(os.fstat(fd).st_mode) else None


def top_packages(plan: dict, packages: str = PACKAGES) -> list[str]:
    """Packages no other package of the environment depends on, by the other packages' own manifests, read from the
    fresh checkouts before any dependency code runs. Building their default targets builds what they import;
    mathlib's own dependencies arrive compiled from its cache."""
    needed = set()
    for package in plan["packages"]:
        body = read_regular(os.path.join(packages, package["name"], package["sub_dir"] or ".", "lake-manifest.json"),
                            1024**2)
        try:
            entries = json.loads(body).get("packages", []) if body is not None else []
            needed.update(entry["name"] for entry in entries if isinstance(entry, dict)
                          and isinstance(entry.get("name"), str))
        except (ValueError, AttributeError, TypeError):
            continue
    top = [package["name"] for package in plan["packages"] if package["name"] not in needed]
    return top or [package["name"] for package in plan["packages"]]


def write_workspace(plan: dict, configs: dict[str, str]) -> None:
    with open(os.path.join(WORKSPACE, "lean-toolchain"), "w") as stream:
        stream.write(f"leanprover/lean4:{plan['toolchain']['version']}\n")
    with open(os.path.join(WORKSPACE, "lakefile.toml"), "w") as stream:
        stream.write(lakefile(plan))
    with open(os.path.join(WORKSPACE, "lake-manifest.json"), "w") as stream:
        json.dump(manifest(plan, configs), stream, indent=1)


def chown_tree(path: str, uid: int) -> None:
    for directory, names, files in os.walk(path, followlinks=False):
        os.lchown(directory, uid, uid)
        for name in names + files:
            os.lchown(os.path.join(directory, name), uid, uid)


# --- Running dependency code -----------------------------------------------------------------------------------------


def loopback_up() -> None:
    """Bring up `lo` in the calling thread's network namespace (SIOCGIFFLAGS, SIOCSIFFLAGS, IFF_UP)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        # struct ifreq: a 16-byte name, then a 24-byte union whose first member here is the short flags.
        request = struct.pack("16sH22x", b"lo", 0)
        flags = struct.unpack("16sH22x", fcntl.ioctl(probe.fileno(), 0x8913, request))[1]
        fcntl.ioctl(probe.fileno(), 0x8914, struct.pack("16sH22x", b"lo", flags | 1))


def printable(text: str, limit: int = 120) -> str:
    return "".join(character if " " <= character <= "~" else "?" for character in text[:limit])


class CacheGateway:
    """A plain-HTTP server on loopback, inside a network namespace of its own whose only interface is loopback, through
    which mathlib's cache tool reads its files (MATHLIB_CACHE_GET_URL). It answers only GET and HEAD of a cache file
    path, which it fetches itself over HTTPS from CACHE_HOST, from the VM's namespace. The cache tool never holds a
    connection to the outside, so it cannot name another host, storage account or request."""

    PATH = re.compile(r"/mathlib4-master/f/[0-9a-f]{1,32}\.ltar\Z")
    FILE_BYTES = 256 * 1024**2

    def __init__(self, host: str = CACHE_HOST, *, listener: socket.socket | None = None, connect=None):
        import http.client
        import http.server

        self.host = host
        self.connect = connect or (lambda: http.client.HTTPSConnection(host, 443, timeout=60))
        self.namespace = None
        if listener is None:
            original = os.open("/proc/self/ns/net", os.O_RDONLY)
            try:
                os.unshare(os.CLONE_NEWNET)
                self.namespace = os.open("/proc/self/ns/net", os.O_RDONLY)
                loopback_up()
                listener = socket.create_server(("127.0.0.1", 0))
            finally:
                os.setns(original, os.CLONE_NEWNET)
                os.close(original)
        self.address = listener.getsockname()
        self.stats = {"requests": 0, "files": 0, "bytes_in": 0, "refused": 0, "upstream_errors": 0, "refused_requests": []}
        self.lock, self.local = threading.Lock(), threading.local()
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                gateway.forward(self, "GET")

            def do_HEAD(self):
                gateway.forward(self, "HEAD")

            def refuse(self):
                gateway.refuse(self, self.command)

            do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_CONNECT = do_TRACE = refuse

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.server.socket.close()
        self.server.socket, self.server.server_address = listener, self.address
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://{self.address[0]}:{self.address[1]}/mathlib4-master"

    def refuse(self, handler, method: str) -> None:
        with self.lock:
            self.stats["refused"] += 1
            entry = printable(f"{method} {handler.path}")
            if len(self.stats["refused_requests"]) < 16 and entry not in self.stats["refused_requests"]:
                self.stats["refused_requests"].append(entry)
        handler.send_response(403)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _upstream(self, method: str, path: str):
        for attempt in range(2):
            connection = getattr(self.local, "connection", None)
            if connection is None:
                connection = self.local.connection = self.connect()
            try:
                connection.request(method, path, headers={"Accept-Encoding": "identity"})
                response = connection.getresponse()
                body = response.read(self.FILE_BYTES + 1)
                return response.status, body
            except (OSError, ValueError) as exc:
                connection.close()
                self.local.connection = None
                if attempt:
                    raise exc
        raise OSError("unreachable")

    def forward(self, handler, method: str) -> None:
        if not self.PATH.fullmatch(handler.path):
            self.refuse(handler, method)
            return
        with self.lock:
            self.stats["requests"] += 1
        try:
            status, body = self._upstream(method, handler.path)
            if len(body) > self.FILE_BYTES:
                raise ValueError("cache file too large")
        except Exception:  # noqa: BLE001 - the cache tool sees a gateway error
            with self.lock:
                self.stats["upstream_errors"] += 1
            status, body = 502, b""
        with self.lock:
            if method == "GET" and status == 200:
                self.stats["files"] += 1
                self.stats["bytes_in"] += len(body)
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if method == "GET":
            handler.wfile.write(body)

    def close(self) -> dict:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.namespace is not None:
            os.close(self.namespace)
            self.namespace = None
        return dict(self.stats)


def builder_environment(extra: dict | None = None) -> dict:
    home = os.path.join(SCRATCH, "home")
    return {"PATH": TOOLCHAIN + "/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8", "XDG_CACHE_HOME": home + "/.cache",
            "MATHLIB_CACHE_DIR": os.path.join(SCRATCH, "mathlib-cache"), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", **(extra or {})}


def builder_command(argv: list[str], namespace: int | None) -> tuple[list[str], tuple[int, ...]]:
    """Run `argv` as BUILD_USER with no capabilities and no new privileges, in `namespace` (an open network namespace)
    or, without one, in a fresh namespace with no interfaces up."""
    prefix = ["unshare", "--net", "--"] if namespace is None else ["nsenter", f"--net=/proc/self/fd/{namespace}", "--"]
    drop = ["setpriv", f"--reuid={BUILD_USER}", f"--regid={BUILD_USER}", "--clear-groups", "--no-new-privs",
            "--inh-caps=-all", "--bounding-set=-all", "--"]
    return [*prefix, *drop, *argv], (() if namespace is None else (namespace,))


def as_builder(argv: list[str], step_name: str, *, timeout: int, namespace: int | None = None,
               extra: dict | None = None) -> None:
    command, fds = builder_command(argv, namespace)
    try:
        run(command, step_name=step_name, timeout=timeout, cwd=WORKSPACE, env=builder_environment(extra), pass_fds=fds,
            stream=True)
    finally:
        kill_builder()


def builder_processes(uid: int = BUILD_USER, proc: str = "/proc") -> list[int]:
    """Live processes with `uid` as any of their user IDs. Zombies are skipped: they no longer run."""
    found = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(f"{proc}/{name}/status") as stream:
                fields = dict(line.split(":", 1) for line in stream if ":" in line)
            if fields["State"].split()[0] != "Z" and any(int(value) == uid for value in fields["Uid"].split()):
                found.append(int(name))
        except (OSError, ValueError, KeyError, IndexError):
            continue
    return found


def kill_builder() -> None:
    """Stop every process of the build user; dependency code must not outlive its step."""
    for _ in range(50):
        remaining = builder_processes()
        if not remaining:
            return
        for pid in remaining:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.1)
    raise PrepareError("build", "processes of the build user survived being killed")


def run(command: list[str], *, step_name: str, timeout: int, cwd: str | None = None, env: dict | None = None,
        pass_fds: tuple[int, ...] = (), stream: bool = False) -> None:
    """Run our own command (or, through as_builder, dependency code), copying its output to the progress log."""
    process = subprocess.Popen(command, cwd=cwd, env=env if env is not None else {"PATH": "/usr/bin:/bin"},
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               pass_fds=pass_fds)
    tail: list[str] = []

    def show(raw: bytes) -> None:
        # A carriage return redraws a progress line; keep only what it ends with.
        parts = raw.decode("utf-8", "replace").split("\r")
        part = next((item.rstrip() for item in reversed(parts) if item.strip()), "")
        if part:
            tail[:] = (tail + [part])[-20:]
            if stream:
                log("   " + part)

    def drain():
        pending = b""
        while chunk := process.stdout.read1(65536):
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            if len(pending) > LOG_LINE:
                lines.append(pending[:LOG_LINE])
                pending = b""
            for raw in lines:
                show(raw[:LOG_LINE])
        if pending:
            show(pending[:LOG_LINE])

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise PrepareError(step_name, "the step ran out of time") from None
    finally:
        reader.join(timeout=10)
    if code:
        if not stream:
            for line in tail:
                log("   " + line)
        raise PrepareError(step_name, f"{os.path.basename(command[0])} exited with status {code}")


# --- Seal --------------------------------------------------------------------------------------------------------------


def _hash(path: str) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        while block := stream.read(1 << 20):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def seal_entries(root: str = ROOT, workers: int = 8) -> list[list]:
    """Every entry under `root` except the seal, sorted by path: directories [path, "d", mode], files
    [path, "f", mode, size, sha256], symbolic links [path, "l", target]. Links must stay inside the environment, and
    nothing else (devices, sockets, pipes) may be there."""
    entries, files = [], []
    base = os.path.realpath(root)

    def walk(directory: str, relative: str) -> None:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
        for child in children:
            path = f"{relative}/{child.name}" if relative else child.name
            if path == SEAL:
                continue
            info = child.stat(follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                entries.append([path, "d", mode])
                walk(child.path, path)
            elif stat.S_ISREG(info.st_mode):
                entry = [path, "f", mode, None, None]
                entries.append(entry)
                files.append((entry, child.path))
            elif stat.S_ISLNK(info.st_mode):
                # Resolved through any other links, as a reader would; the build user's processes are gone by now.
                target, resolved = os.readlink(child.path), os.path.realpath(child.path)
                if os.path.isabs(target) or (resolved != base and not resolved.startswith(base + os.sep)):
                    raise PrepareError("seal", f"{printable(path)} links outside the environment")
                entries.append([path, "l", target])
            else:
                raise PrepareError("seal", f"{printable(path)} is not a file, directory or link")

    walk(root, "")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for (entry, _), (size, digest) in zip(files, pool.map(lambda item: _hash(item[1]), files), strict=True):
            entry[3], entry[4] = size, digest
    return entries


def write_seal(plan: dict, record: dict, root: str = ROOT) -> dict:
    entries = seal_entries(root, workers=max(2, os.cpu_count() or 2))
    body = json.dumps({"format": 1, "key": plan["key"], "attempt": plan["attempt"], "toolchain": plan["toolchain"],
                       "packages": plan["packages"], "entries": entries},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    path = os.path.join(root, SEAL)
    with open(path, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.sync()
    files = [entry for entry in entries if entry[1] == "f"]
    return {"sha256": hashlib.sha256(body).hexdigest(), "seal_bytes": len(body), "files": len(files),
            "bytes": sum(entry[3] for entry in files), "entries": len(entries)}


# --- Main --------------------------------------------------------------------------------------------------------------


def prepare(plan: dict) -> dict:
    record: dict = {"format": 1, "key": plan["key"], "attempt": plan["attempt"], "failure": None}
    deadline = Deadline(plan["seconds"])
    current = "setup"
    try:
        if os.geteuid() != 0 or not os.path.isdir(ROOT) or os.path.exists(TOOLCHAIN) or os.path.exists(WORKSPACE):
            raise PrepareError("setup", "the VM must run as root on a fresh environment directory")
        os.umask(0o022)
        os.chmod(ROOT, 0o755)
        os.makedirs(SCRATCH, mode=0o700, exist_ok=True)
        current = "toolchain"
        with step(record, current):
            install_toolchain(plan, record, deadline)
        current = "packages"
        with step(record, current):
            configs = fetch_packages(plan, record, deadline)
            targets = top_packages(plan)
            write_workspace(plan, configs)
            for directory in ("home", "mathlib-cache"):
                os.makedirs(os.path.join(SCRATCH, directory), mode=0o700, exist_ok=True)
                os.chown(os.path.join(SCRATCH, directory), BUILD_USER, BUILD_USER)
            os.chmod(SCRATCH, 0o711)
            chown_tree(WORKSPACE, BUILD_USER)
        current = "lean"
        with step(record, current):
            as_builder(["lean", "--version"], current, timeout=deadline.left(current))
        if any(package["name"] == "mathlib" for package in plan["packages"]):
            current = "cache"
            with step(record, current):
                gateway = CacheGateway()
                try:
                    as_builder(["lake", "exe", "cache", "get"], current, timeout=deadline.left(current),
                               namespace=gateway.namespace, extra={"MATHLIB_CACHE_GET_URL": gateway.url})
                finally:
                    record["cache"] = gateway.close()
        current = "build"
        with step(record, current):
            record["built"] = targets
            as_builder(["lake", "build", *("@" + name for name in targets)], current, timeout=deadline.left(current))
        current = "seal"
        with step(record, current):
            kill_builder()
            shutil.rmtree(SCRATCH, ignore_errors=True)
            record["seal"] = write_seal(plan, record)
    except PrepareError as exc:
        record["failure"] = {"step": exc.step, "message": exc.message}
    except Exception as exc:  # noqa: BLE001 - reported by class and step only
        record["failure"] = {"step": current, "message": f"unexpected {type(exc).__name__}"}
    finally:
        with contextlib.suppress(PrepareError):
            kill_builder()
    return record


def main() -> int:
    try:
        plan = json.loads(sys.stdin.buffer.read(1024**2))
    except ValueError:
        plan = None
    problems = plan_problems(plan)
    record = {"format": 1, "failure": {"step": "plan", "message": "; ".join(problems)}} if problems else prepare(plan)
    sys.stdout.write("PREPARE-RESULT " + json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 1 if record["failure"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
