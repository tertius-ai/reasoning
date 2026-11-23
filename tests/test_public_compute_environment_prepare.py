"""Preparing an approved Lean environment: the VM's program and the relay's orchestration, without Modal or GitHub.

The VM program's git checks run against local repositories; its proxy runs against a local upstream. The steps that need
root in a Linux VM (network namespaces, dropping to the build user) are checked by their command lines here and live by
`make compute-environment-prepare`.
"""

import hashlib
import io
import json
import os
import socket
import stat
import subprocess
import threading
import tomllib
from types import SimpleNamespace

import httpx
import pytest

from reasoning_ledger.server import compute_environment_prepare as prepare
from reasoning_ledger.server import compute_environment_vm as vm
from reasoning_ledger.server import lean_environments
from reasoning_ledger.server.compute_docker import RuntimeFault

VERSION = "v4.34.0-rc1"
MATHLIB_URL = "https://github.com/leanprover-community/mathlib4"
SPECIFICATION = {
    "format": 1, "toolchain": "leanprover/lean4:" + VERSION,
    "packages": [
        {"name": "InfinityCosmos", "url": "https://github.com/emilyriehl/infinity-cosmos", "rev": "8" * 40, "sub_dir": None},
        {"name": "batteries", "url": "https://github.com/leanprover-community/batteries", "rev": "3" * 40, "sub_dir": None},
        {"name": "mathlib", "url": MATHLIB_URL, "rev": "1" * 40, "sub_dir": None},
        {"name": "verso", "url": "https://github.com/leanprover/verso.git", "rev": "9" * 40, "sub_dir": "src/lib"},
    ],
}
KEY = lean_environments.key(SPECIFICATION)
ORG, WORKER, ENVIRONMENT = "a" * 32, "b" * 32, "c" * 32
ASSET = {"version": VERSION, "url": vm.release_url(VERSION), "sha256": "e" * 64, "bytes": 312_000_000}
PROFILE = {"format": 1, "app": "reasoning-compute", "environment": "main", "token_id": "ak-" + "1" * 16,
           "token_secret": "as-" + "2" * 16, "supervisor_image": "docker.io/library/ubuntu@sha256:" + "4" * 64,
           "resources": {"cpu": 4, "memory_bytes": 8 * 1024**3, "disk_bytes": 8 * 1024**3, "wall_time_seconds": 3600,
                         "artifact_bytes": 16 * 1024**2, "pids": 512}}


def plan(**changes):
    return {"format": 1, "key": KEY, "attempt": "d" * 32, "toolchain": dict(ASSET),
            "packages": [dict(package) for package in SPECIFICATION["packages"]], "seconds": 3000, **changes}


def environment(**changes):
    return {"id": ENVIRONMENT, "organization_id": ORG, "key": KEY, "status": "approved",
            "specification": SPECIFICATION, **changes}


# --- The VM program: plan, workspace, seal ---------------------------------------------------------------------------


def test_a_relay_plan_is_accepted_as_it_is():
    assert vm.plan_problems(plan()) == []
    assert prepare.plan(environment(), ASSET, "d" * 32) == plan(seconds=prepare.PROGRAM_SECONDS)


@pytest.mark.parametrize(("change", "problem"), [
    ({"key": "K" * 64}, "key"),
    ({"attempt": "../x"}, "attempt"),
    ({"seconds": 10}, "time"),
    ({"toolchain": {**ASSET, "version": "nightly-2026-01-01"}}, "official Lean release"),
    ({"toolchain": {**ASSET, "url": "https://example.org/lean.tar.zst"}}, "Linux archive"),
    ({"toolchain": {**ASSET, "sha256": None}}, "SHA-256"),
    ({"toolchain": {**ASSET, "bytes": 3 * 1024**3}}, "size"),
    ({"packages": []}, "between 1 and 64"),
    ({"packages": [{**SPECIFICATION["packages"][0], "url": "https://gitlab.com/a/b"}]}, "github.com"),
    ({"packages": [{**SPECIFICATION["packages"][0], "rev": "main"}]}, "pinned"),
    ({"packages": [{**SPECIFICATION["packages"][0], "sub_dir": "../up"}]}, "sub-directory"),
    ({"packages": [SPECIFICATION["packages"][1], SPECIFICATION["packages"][0]]}, "in order"),
    ({"packages": [SPECIFICATION["packages"][0], SPECIFICATION["packages"][0]]}, "distinct"),
    ({"extra": 1}, "unexpected fields"),
])
def test_the_vm_refuses_a_plan_it_cannot_prepare(change, problem):
    assert any(problem in item for item in vm.plan_problems(plan(**change)))


def test_the_workspace_requires_every_package_at_its_commit_and_pins_it_for_lake():
    value = plan()
    lakefile = tomllib.loads(vm.lakefile(value))
    assert lakefile["name"] == vm.WORKSPACE_NAME
    assert lakefile["require"] == [
        {"name": package["name"], "git": package["url"], "rev": package["rev"],
         **({"subDir": package["sub_dir"]} if package["sub_dir"] else {})}
        for package in SPECIFICATION["packages"]]
    configs = {package["name"]: "lakefile.lean" for package in SPECIFICATION["packages"]} | {"verso": "lakefile.toml"}
    manifest = vm.manifest(value, configs)
    assert (manifest["version"], manifest["packagesDir"], manifest["lakeDir"]) == ("1.1.0", ".lake/packages", ".lake")
    assert [entry["name"] for entry in manifest["packages"]] == [package["name"] for package in SPECIFICATION["packages"]]
    verso = manifest["packages"][-1]
    # Lake checks the root's requirements against these, then materializes each without fetching: rev = inputRev.
    assert verso == {"type": "git", "name": "verso", "scope": "", "inherited": False, "configFile": "lakefile.toml",
                     "manifestFile": "lake-manifest.json", "url": "https://github.com/leanprover/verso.git",
                     "rev": "9" * 40, "inputRev": "9" * 40, "subDir": "src/lib"}


def test_only_packages_nothing_else_needs_are_built(tmp_path):
    for name, needs in {"InfinityCosmos": ["mathlib", "batteries"], "mathlib": ["batteries"], "batteries": []}.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "lake-manifest.json").write_text(json.dumps({"packages": [{"name": item} for item in needs]}))
    (tmp_path / "verso/src/lib").mkdir(parents=True)
    (tmp_path / "verso/src/lib/lake-manifest.json").write_text("{not json")
    assert vm.top_packages(plan(), str(tmp_path)) == ["InfinityCosmos", "verso"]
    # A pipe or a link where a manifest belongs is not read, and does not block.
    (tmp_path / "verso/src/lib/lake-manifest.json").unlink()
    os.mkfifo(tmp_path / "verso/src/lib/lake-manifest.json")
    (tmp_path / "mathlib/lake-manifest.json").unlink()
    (tmp_path / "mathlib/lake-manifest.json").symlink_to(tmp_path / "InfinityCosmos/lake-manifest.json")
    assert vm.top_packages(plan(), str(tmp_path)) == ["InfinityCosmos", "verso"]


def test_the_seal_lists_every_entry_with_its_digest(tmp_path):
    root = tmp_path / "environment"
    (root / "toolchain/bin").mkdir(parents=True)
    (root / "toolchain/bin/lean").write_bytes(b"\x7fELF lean")
    (root / "toolchain/bin/lean").chmod(0o755)
    (root / "workspace").mkdir()
    (root / "workspace/a.olean").write_bytes(b"olean")
    (root / "workspace/link").symlink_to("a.olean")
    (root / "attempt.json").write_text("{}")
    (root / vm.SEAL).write_text("an earlier seal is not part of the environment")

    entries = vm.seal_entries(str(root), workers=2)
    assert [entry[0] for entry in entries] == ["attempt.json", "toolchain", "toolchain/bin", "toolchain/bin/lean",
                                              "workspace", "workspace/a.olean", "workspace/link"]
    assert entries[3] == ["toolchain/bin/lean", "f", 0o755, 9, hashlib.sha256(b"\x7fELF lean").hexdigest()]
    assert entries[6] == ["workspace/link", "l", "a.olean"]

    summary = vm.write_seal(plan(), {}, str(root))
    body = (root / vm.SEAL).read_bytes()
    assert summary["sha256"] == hashlib.sha256(body).hexdigest()
    assert (summary["files"], summary["bytes"]) == (3, 9 + 5 + 2)
    sealed = json.loads(body)
    assert sealed["key"] == KEY and sealed["packages"] == SPECIFICATION["packages"] and sealed["entries"] == entries


@pytest.mark.parametrize("kind", ["absolute", "escaping", "chained", "pipe"])
def test_the_seal_refuses_what_could_reach_outside_the_environment(tmp_path, kind):
    tmp_path = tmp_path / "environment"
    (tmp_path / "workspace").mkdir(parents=True)
    if kind == "absolute":
        (tmp_path / "workspace/link").symlink_to("/etc/passwd")
    elif kind == "escaping":
        (tmp_path / "workspace/link").symlink_to("../../outside")
    elif kind == "chained":
        # Each link's text stays inside, but through the first the second leaves the environment.
        (tmp_path / "workspace/up").symlink_to("..")
        (tmp_path / "workspace/z").symlink_to("up/..")
    else:
        os.mkfifo(tmp_path / "workspace/pipe")
    with pytest.raises(vm.PrepareError) as raised:
        vm.seal_entries(str(tmp_path), workers=1)
    assert raised.value.step == "seal"


# --- The VM program: downloads and git ---------------------------------------------------------------------------------


class Opener:
    def __init__(self, body):
        self.body, self.urls = body, []

    def open(self, url, timeout):
        self.urls.append(url)
        return io.BytesIO(self.body)


def test_the_release_archive_must_match_githubs_digest_and_size(tmp_path):
    body = b"lean release" * 100
    digest = hashlib.sha256(body).hexdigest()
    target = tmp_path / "toolchain.tar.zst"
    vm.download(ASSET["url"], str(target), digest, len(body), timeout=60, opener=Opener(body))
    assert target.read_bytes() == body
    with pytest.raises(vm.PrepareError, match="does not match"):
        vm.download(ASSET["url"], str(target), "0" * 64, len(body), timeout=60, opener=Opener(body))
    with pytest.raises(vm.PrepareError, match="larger than"):
        vm.download(ASSET["url"], str(target), digest, len(body) - 1, timeout=60, opener=Opener(body))


@pytest.mark.parametrize(("target", "allowed"), [
    ("https://release-assets.githubusercontent.com/x", True),
    ("https://objects.githubusercontent.com/x", True),
    ("http://objects.githubusercontent.com/x", False),
    ("https://example.org/x", False),
    ("https://user@objects.githubusercontent.com/x", False),
    ("https://objects.githubusercontent.com:8443/x", False),
])
def test_release_downloads_follow_redirects_only_to_githubs_hosts(target, allowed):
    import urllib.request

    request = urllib.request.Request(ASSET["url"])
    handler = vm.Redirects()
    if allowed:
        assert handler.redirect_request(request, None, 302, "Found", {}, target).full_url == target
    else:
        with pytest.raises(vm.PrepareError, match="not allowed"):
            handler.redirect_request(request, None, 302, "Found", {}, target)


def run_git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=0+fixture@users.noreply.github.com", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """A repository with a commit on its default branch, one only on a tag, one only on another branch, and one that is
    fetchable by hash but on no branch or tag: what GitHub serves for a fork's commit under the parent's URL."""
    monkeypatch.setattr(vm, "SCRATCH", str(tmp_path / "scratch"))
    work = tmp_path / "work"
    work.mkdir()
    run_git("init", "--quiet", "--initial-branch=main", cwd=work)
    (work / "lakefile.toml").write_text('name = "fixture"\n')
    (work / "src/lib").mkdir(parents=True)
    (work / "src/lib/lakefile.lean").write_text("import Lake\n")
    run_git("add", ".", cwd=work)
    run_git("commit", "--quiet", "-m", "main", cwd=work)
    commits = {"main": run_git("rev-parse", "HEAD", cwd=work)}
    for name in ("tagged", "side", "fork"):
        run_git("checkout", "--quiet", "-b", name, "main", cwd=work)
        (work / name).write_text(name)
        run_git("add", ".", cwd=work)
        run_git("commit", "--quiet", "-m", name, cwd=work)
        commits[name] = run_git("rev-parse", "HEAD", cwd=work)
    run_git("tag", "v1", "tagged", cwd=work)
    run_git("checkout", "--quiet", "main", cwd=work)
    run_git("branch", "--quiet", "-D", "tagged", "fork", cwd=work)
    bare = tmp_path / "upstream.git"
    run_git("clone", "--quiet", "--bare", "--mirror", str(work), str(bare), cwd=tmp_path)
    # Put the fork's commit back as an object only, the way a fork network shares objects.
    pack = subprocess.run(["git", "pack-objects", "--stdout", "--revs"], cwd=work, input=(commits["fork"] + "\n").encode(),
                          capture_output=True, check=True, text=False).stdout
    subprocess.run(["git", "index-pack", "--stdin"], cwd=bare, input=pack, check=True, capture_output=True)
    for option in ("uploadpack.allowFilter=true", "uploadpack.allowAnySHA1InWant=true"):
        run_git("config", *option.split("="), cwd=bare)
    options = (*vm.GIT_OPTIONS, "-c", "protocol.file.allow=always")
    return {"url": "file://" + str(bare), "commits": commits, "options": options, "root": tmp_path}


@pytest.mark.parametrize(("commit", "described"), [("main", "default branch"), ("tagged", "tag v1"), ("side", "branch side")])
def test_a_pinned_commit_must_be_on_a_branch_or_tag_of_the_named_repository(upstream, commit, described):
    package = {"name": "fixture", "url": upstream["url"], "rev": upstream["commits"][commit], "sub_dir": None}
    scratch = upstream["root"] / "reachability"
    scratch.mkdir()
    assert vm.reachable(package, str(scratch), timeout=60, options=upstream["options"]) == described
    assert list(scratch.iterdir()) == []


def test_a_commit_served_only_by_hash_is_refused(upstream):
    package = {"name": "fixture", "url": upstream["url"], "rev": upstream["commits"]["fork"], "sub_dir": None}
    scratch = upstream["root"] / "reachability"
    scratch.mkdir()
    # The server would hand it out by hash, so a checkout alone would not notice.
    vm.checkout(package, str(upstream["root"] / "probe"), timeout=60, options=upstream["options"])
    with pytest.raises(vm.PrepareError, match="on no branch or tag"):
        vm.reachable(package, str(scratch), timeout=60, options=upstream["options"])


def test_a_checkout_holds_only_the_pinned_commit_with_the_manifests_url(upstream):
    package = {"name": "fixture", "url": upstream["url"], "rev": upstream["commits"]["side"], "sub_dir": None}
    directory = upstream["root"] / "packages/fixture"
    assert vm.checkout(package, str(directory), timeout=60, options=upstream["options"]) == "lakefile.toml"
    assert run_git("rev-parse", "HEAD", cwd=directory) == upstream["commits"]["side"]
    assert run_git("remote", "get-url", "origin", cwd=directory) == upstream["url"]
    assert run_git("rev-list", "--count", "HEAD", cwd=directory) == "1"
    nested = {**package, "name": "nested", "sub_dir": "src/lib"}
    assert vm.checkout(nested, str(upstream["root"] / "packages/nested"), timeout=60,
                       options=upstream["options"]) == "lakefile.lean"
    missing = {**package, "name": "missing", "sub_dir": "absent"}
    with pytest.raises(vm.PrepareError, match="sub-directory"):
        vm.checkout(missing, str(upstream["root"] / "packages/missing"), timeout=60, options=upstream["options"])


def test_git_runs_with_only_https_no_redirects_hooks_or_submodules():
    options = dict(zip(vm.GIT_OPTIONS[1::2], [None] * 99, strict=False))
    assert {"protocol.allow=never", "protocol.https.allow=always", "http.followRedirects=false",
            "core.hooksPath=/dev/null", "fetch.recurseSubmodules=false"} <= set(options)


# --- The VM program: running dependency code -------------------------------------------------------------------------


def test_dependency_code_runs_as_the_build_user_without_privileges_or_network():
    command, fds = vm.builder_command(["lake", "build"], None)
    assert command[:3] == ["unshare", "--net", "--"] and fds == ()
    drop = command[3:command.index("--", 3) + 1]
    assert drop == ["setpriv", "--reuid=10001", "--regid=10001", "--clear-groups", "--no-new-privs", "--inh-caps=-all",
                    "--bounding-set=-all", "--"]
    assert command[-2:] == ["lake", "build"]
    command, fds = vm.builder_command(["lake", "exe", "cache", "get"], 7)
    assert command[:3] == ["nsenter", "--net=/proc/self/fd/7", "--"] and fds == (7,)
    environment = vm.builder_environment({"https_proxy": "http://127.0.0.1:1"})
    assert environment["PATH"].startswith(vm.TOOLCHAIN + "/bin:") and environment["MATHLIB_CACHE_DIR"].startswith(vm.SCRATCH)
    assert not any(key in environment for key in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"))


@pytest.fixture
def cache_host():
    """A stand-in for mathlib's cache host: it serves any path and records each request it receives."""
    import http.server

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            seen.append(("GET", self.path))
            body = b"ltar:" + self.path.encode()
            self.send_response(404 if "missing" in self.path else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            seen.append(("HEAD", self.path))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], seen
    server.shutdown()
    server.server_close()


def test_the_cache_gateway_answers_only_reads_of_cache_files(cache_host):
    import http.client

    port, seen = cache_host
    gateway = vm.CacheGateway("127.0.0.1", listener=socket.create_server(("127.0.0.1", 0)),
                              connect=lambda: http.client.HTTPConnection("127.0.0.1", port, timeout=10))
    assert gateway.url == f"http://127.0.0.1:{gateway.address[1]}/mathlib4-master"
    client = http.client.HTTPConnection(*gateway.address, timeout=10)

    def ask(method, path):
        client.request(method, path)
        response = client.getresponse()
        return response.status, response.read()

    try:
        assert ask("GET", "/mathlib4-master/f/b1e5fe941e866d87.ltar") == (200, b"ltar:/mathlib4-master/f/b1e5fe941e866d87.ltar")
        assert ask("HEAD", "/mathlib4-master/f/0a.ltar") == (200, b"")
        assert ask("GET", "/mathlib4-master/f/missing0.ltar")[0] == 403
        for method, path in [("GET", "/mathlib4-master/c/0a"), ("GET", "/other-account/f/0a.ltar"),
                             ("GET", "/mathlib4-master/f/0a.ltar?comp=list"), ("PUT", "/mathlib4-master/f/0a.ltar"),
                             ("GET", "/mathlib4-master/f/../../x.ltar"), ("GET", "http://github.com/x")]:
            assert ask(method, path)[0] == 403, (method, path)
    finally:
        client.close()
        stats = gateway.close()
    # Only the two well-formed reads reached the host, by root's own connection.
    assert seen == [("GET", "/mathlib4-master/f/b1e5fe941e866d87.ltar"), ("HEAD", "/mathlib4-master/f/0a.ltar")]
    assert (stats["requests"], stats["files"], stats["refused"]) == (2, 1, 7)
    assert stats["bytes_in"] == len(b"ltar:/mathlib4-master/f/b1e5fe941e866d87.ltar")
    assert "PUT /mathlib4-master/f/0a.ltar" in stats["refused_requests"]
    with pytest.raises(OSError):
        socket.create_connection(gateway.address, timeout=2).recv(1)


def test_the_cache_gateway_reports_an_unreachable_host_as_a_gateway_error():
    import http.client

    def unreachable():
        raise ConnectionRefusedError

    gateway = vm.CacheGateway("127.0.0.1", listener=socket.create_server(("127.0.0.1", 0)), connect=unreachable)
    client = http.client.HTTPConnection(*gateway.address, timeout=10)
    try:
        client.request("GET", "/mathlib4-master/f/0a.ltar")
        assert client.getresponse().status == 502
    finally:
        client.close()
        stats = gateway.close()
    assert stats["upstream_errors"] == 1 and stats["files"] == 0


def test_refused_requests_are_recorded_as_printable_text():
    assert vm.printable("GET /\x1b[31mred\n") == "GET /?[31mred?"
    assert len(vm.printable("x" * 500)) == 120


def test_only_live_processes_of_the_build_user_are_killed(tmp_path):
    for pid, state, uid in [(10, "S (sleeping)", "10001\t10001\t10001\t10001"), (11, "Z (zombie)", "10001\t10001\t10001\t10001"),
                            (12, "R (running)", "0\t10001\t0\t0"), (13, "S (sleeping)", "0\t0\t0\t0")]:
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "status").write_text(f"Name:\tlake\nState:\t{state}\nUid:\t{uid}\n")
    (tmp_path / "self").mkdir()
    assert sorted(vm.builder_processes(10001, str(tmp_path))) == [10, 12]


def test_the_vm_reports_a_bad_plan_or_a_bad_start_as_a_failure(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(vm.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"format": 2}')))
    assert vm.main() == 1
    line = capsys.readouterr().out.strip()
    assert line.startswith("PREPARE-RESULT ") and json.loads(line.split(" ", 1)[1])["failure"]["step"] == "plan"
    monkeypatch.setattr(vm, "ROOT", str(tmp_path))
    monkeypatch.setattr(vm.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(vm, "kill_builder", lambda: None)
    record = vm.prepare(plan())
    assert record["failure"] == {"step": "setup", "message": "the VM must run as root on a fresh environment directory"}


# --- The relay ---------------------------------------------------------------------------------------------------------


def release(**changes):
    asset = {"name": f"lean-{VERSION[1:]}-linux.tar.zst", "digest": "sha256:" + "e" * 64, "size": ASSET["bytes"],
             "browser_download_url": ASSET["url"], "state": "uploaded"}
    other = {**asset, "name": f"lean-{VERSION[1:]}-darwin.tar.zst", "digest": "sha256:" + "f" * 64}
    return {"tag_name": VERSION, "draft": False, "assets": [other, {**asset, **changes}]}


def test_the_relay_reads_githubs_digest_for_the_linux_archive():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=release())

    assert prepare.toolchain_asset(VERSION, transport=httpx.MockTransport(handler)) == ASSET
    assert str(seen[0].url) == prepare.RELEASE_API + VERSION and "authorization" not in seen[0].headers


@pytest.mark.parametrize("change", [{"digest": None}, {"digest": "md5:abc"}, {"size": 0},
                                    {"browser_download_url": "https://example.org/lean.tar.zst"}, {"state": "new"},
                                    {"name": "lean-other-linux.tar.zst"}, "draft", "status"])
def test_a_release_without_a_trustworthy_digest_is_refused(change):
    def handler(_):
        if change == "status":
            return httpx.Response(403, json={"message": "rate limited"})
        body = release() if change == "draft" else release(**change)
        if change == "draft":
            body["draft"] = True
        return httpx.Response(200, json=body)

    with pytest.raises(RuntimeFault, match="toolchain_unavailable"):
        prepare.toolchain_asset(VERSION, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("change", [{"status": "requested"}, {"organization_id": "f" * 32}, {"id": "f" * 32},
                                    {"key": "0" * 64}, {"specification": {**SPECIFICATION, "format": 2}}, "missing"])
def test_only_an_approved_environment_of_the_relays_organization_is_prepared(change):
    value = None if change == "missing" else environment(**change)
    with pytest.raises(RuntimeFault, match="environment_not_approved"):
        prepare.approved(value, ORG, ENVIRONMENT)


class Volume:
    def __init__(self, sdk):
        self.sdk = sdk


class FakeSDK:
    """Modal, as the relay sees it: one Volume, one VM, and the VM program's answer."""

    def __init__(self, result=None, *, seal=b'{"seal": true}', terminate=True, fail=None):
        self.calls, self.files, self.removed, self.result = [], {}, [], result
        self.seal, self.terminate_ok, self.fail = seal, terminate, fail

    def organization_volume(self, name):
        self.calls.append(("volume", name))
        if self.fail == "volume":
            raise RuntimeError("provider body that must not be reported")
        return Volume(self)

    def put(self, volume, path, body):
        self.files[path] = body

    @staticmethod
    def mounted(volume, *, read_only, sub_path=None):
        return ("mount", read_only, sub_path)

    def preparation_vm(self, name, limits, volumes, *, timeout, domains):
        self.calls.append(("vm", name, limits["cpu"], volumes, timeout, domains))
        if self.fail == "create":
            raise TimeoutError("the answer was lost")
        return SimpleNamespace(object_id="sb-fixture")

    def lookup(self, name):
        self.calls.append(("lookup", name))
        if self.fail != "create":
            raise RuntimeFault("modal_creation_unreconciled")
        return SimpleNamespace(object_id="sb-orphan")

    def pump(self, sandbox, args, *, timeout, input_bytes, maximum, stderr_sink):
        self.calls.append(("pump", args[:3], timeout, maximum))
        self.plan = json.loads(input_bytes)
        assert args[3] == prepare.PROGRAM and "def prepare(" in args[3]
        stderr_sink(b"== toolchain\n")
        if self.fail == "pump":
            raise RuntimeFault("wall_time_limit")
        result = self.result or {"format": 1, "key": KEY, "attempt": self.plan["attempt"], "failure": None,
                                 "seal": {"sha256": hashlib.sha256(self.seal).hexdigest(), "files": 3, "bytes": 16}}
        return (1 if result.get("failure") else 0), b"noise\nPREPARE-RESULT " + json.dumps(result).encode() + b"\n"

    def terminate(self, sandbox):
        self.calls.append(("terminate", sandbox.object_id))
        if not self.terminate_ok:
            raise RuntimeError("unverified")

    def read(self, volume, path, maximum):
        self.calls.append(("read", path))
        return self.seal

    def remove(self, volume, path):
        self.removed.append(path)


def run(tmp_path, sdk, **changes):
    return prepare.prepare_environment(tmp_path, WORKER, ORG, PROFILE, environment(**changes), ENVIRONMENT, sdk=sdk,
                                       lookup=lambda version: dict(ASSET))


def test_a_preparation_mounts_only_its_own_directory_and_verifies_the_seal_it_reads_back(tmp_path):
    sdk = FakeSDK()
    receipt = run(tmp_path, sdk)
    attempt = receipt["attempt"]
    path = f"/environments/{KEY}/{attempt}"
    assert receipt["failures"] == [] and receipt["seal_verified"] is True and receipt["cleanup_verified"] is True
    assert (receipt["volume"], receipt["sub_path"], receipt["removed"]) == ("reasoning-lean-" + ORG, path, False)
    assert sdk.calls[0] == ("volume", "reasoning-lean-" + ORG)
    _, name, cpu, volumes, timeout, domains = sdk.calls[1]
    assert volumes == {vm.ROOT: ("mount", False, path)} and cpu == 4 and timeout == prepare.PREPARE_SECONDS
    assert set(domains) == {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
                            "lakecache.blob.core.windows.net"}
    assert sdk.calls[2][1] == ["python3", "-I", "-c"] and sdk.calls[3] == ("terminate", "sb-fixture")
    assert sdk.calls[4] == ("read", path + "/seal.json")
    assert sdk.plan == plan(attempt=attempt, seconds=prepare.PROGRAM_SECONDS)
    assert json.loads(sdk.files[path + "/attempt.json"])["environment_id"] == ENVIRONMENT
    saved = tmp_path / "environment-preparations"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o700
    assert json.loads((saved / f"{attempt}.json").read_text()) == receipt
    assert (saved / f"{attempt}.log").read_bytes() == b"== toolchain\n"
    assert PROFILE["token_secret"] not in json.dumps(sdk.calls) and PROFILE["token_secret"] not in json.dumps(receipt)


@pytest.mark.parametrize("case", ["vm-failure", "timeout", "no-result"])
def test_an_unfinished_preparation_is_reported_and_its_directory_removed(tmp_path, case):
    if case == "vm-failure":
        sdk = FakeSDK({"format": 1, "failure": {"step": "packages", "message": "package x: commit is on no branch or tag"}})
        expected = "the VM failed at packages: package x: commit is on no branch or tag"
    elif case == "timeout":
        sdk = FakeSDK(fail="pump")
        expected = "stopped during program (RuntimeFault)"
    else:
        sdk = FakeSDK()
        sdk.pump = lambda *args, **options: (0, b"no result line")
        expected = "reported no result"
    receipt = run(tmp_path, sdk)
    assert any(expected in item for item in receipt["failures"]), receipt["failures"]
    assert receipt["removed"] is True and sdk.removed == [receipt["sub_path"]]
    assert ("terminate", "sb-fixture") in sdk.calls


@pytest.mark.parametrize("case", ["mismatch", "unreadable"])
def test_a_sealed_attempt_whose_seal_does_not_read_back_is_reported_but_kept(tmp_path, case):
    sdk = FakeSDK()
    if case == "mismatch":
        sdk.read = lambda volume, path, maximum: b"changed after the VM"
        expected = "does not match"
    else:
        def unreadable(volume, path, maximum):
            raise TimeoutError("provider body")
        sdk.read = unreadable
        expected = "stopped during seal (TimeoutError)"
    receipt = run(tmp_path, sdk)
    assert any(expected in item for item in receipt["failures"]), receipt["failures"]
    assert receipt["removed"] is False and sdk.removed == []


def test_nothing_starts_on_modal_without_an_approved_environment_or_githubs_digest(tmp_path):
    sdk = FakeSDK()
    receipt = prepare.prepare_environment(tmp_path, WORKER, ORG, PROFILE, environment(status="declined"), ENVIRONMENT,
                                          sdk=sdk, lookup=lambda version: pytest.fail("must not look up"))
    assert receipt == {"format": 1, "environment_id": ENVIRONMENT, "failures": [
        "the server's record is not an approved environment of this organization that matches its key"]}

    def missing(version):
        raise RuntimeFault("toolchain_unavailable")

    receipt = prepare.prepare_environment(tmp_path, WORKER, ORG, PROFILE, environment(), ENVIRONMENT, sdk=sdk,
                                          lookup=missing)
    assert receipt["failures"] == [f"GitHub gave no SHA-256 for the Linux archive of Lean {VERSION}"]
    assert sdk.calls == [] and not (tmp_path / "environment-preparations").exists()


def test_an_unverified_vm_end_keeps_the_directory_and_says_so(tmp_path):
    sdk = FakeSDK({"format": 1, "failure": {"step": "build", "message": "lake exited with status 1"}}, terminate=False)
    receipt = run(tmp_path, sdk)
    assert receipt["cleanup_verified"] is False and receipt["removed"] is False and sdk.removed == []
    assert any("end is unverified" in item for item in receipt["failures"])


def test_a_vm_whose_creation_answer_was_lost_is_found_by_name_and_stopped(tmp_path):
    sdk = FakeSDK(fail="create")
    receipt = run(tmp_path, sdk)
    name = sdk.calls[1][1]
    assert ("lookup", name) in sdk.calls and ("terminate", "sb-orphan") in sdk.calls
    assert receipt["error"]["stage"] == "vm" and receipt["sandbox_id"] == "sb-orphan"
    assert receipt["cleanup_verified"] is True and receipt["removed"] is True


def test_provider_errors_are_reported_by_stage_and_class_only(tmp_path):
    receipt = run(tmp_path, FakeSDK(fail="volume"))
    assert receipt["error"] == {"stage": "volume", "exception_type": "RuntimeError", "code": None}
    assert "provider body" not in json.dumps(receipt) and receipt["removed"] is False


def test_names_on_the_volume_are_checked():
    with pytest.raises(RuntimeFault):
        prepare.volume_name("../other")
    with pytest.raises(RuntimeFault):
        prepare.attempt_path(KEY, "../x")
    assert prepare.attempt_path(KEY, "d" * 32) == f"/environments/{KEY}/{'d' * 32}"
