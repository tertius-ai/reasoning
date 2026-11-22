"""Release and host-command fixtures, not a real VM/systemd/TLS deployment."""

import gzip
import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from reasoning_ledger.deployment import host, release, server_operations, server_recovery, server_upgrade, worker_host
from reasoning_ledger.deployment.cli import STAGE_SCRIPT, install_remote, main, ssh_arguments
from reasoning_ledger.deployment.digitalocean import DigitalOcean
from reasoning_ledger.deployment.host import HostInstaller, InstallError, extract_bundle, inspect_bundle, public_origin
from reasoning_ledger.deployment.private import protected_temporary_directory, write_private


@pytest.fixture
def payload():
    return {
        "installer.py": Path(host.__file__).read_bytes(),
        "worker-installer.py": Path(worker_host.__file__).read_bytes(),
        "server-operations.py": Path(server_operations.__file__).read_bytes(),
        "server-upgrade.py": Path(server_upgrade.__file__).read_bytes(),
        "server-recovery.py": Path(server_recovery.__file__).read_bytes(),
        "docs/operations.md": (Path(__file__).resolve().parents[1] / "docs/operations.md").read_bytes(),
        "requirements.txt": b"fixture==1 --hash=sha256:" + b"a" * 64 + b"\n",
        "modal-requirements.txt": b"modal-fixture==1 --hash=sha256:" + b"b" * 64 + b"\n",
        "LICENSE": b"Apache fixture",
        "NOTICE": b"Benjamin Antieau and Deven Manam",
        "wheel/reasoning_ledger-0.1.0-py3-none-any.whl": b"noninstalled wheel fixture",
        "web/organization.html": b'<script type="module" src="/assets/organization-x.js"></script>',
        "web/reasoning.svg": b"<svg/>",
        "web/assets/organization-x.js": b'import "./shared-x.js";',
        "web/assets/shared-x.js": b"export const fixture=true;",
        "web/licenses/Reasoning-Apache-2.0.txt": b"Apache",
        "web/licenses/Reasoning-NOTICE.txt": b"Notice",
        "web/licenses/KaTeX-MIT.txt": b"MIT",
        "web/licenses/IBM-Plex-OFL-1.1.txt": b"OFL",
        **{"web/licenses/" + name: b"pdf.js notice " + name.encode() for name in release.PDFJS_NOTICES},
    }


def pack(path, payload, *, entries=None, manifest_change=None, suffix=b""):
    manifest = {
        "format": 1,
        "source_revision": "a" * 40,
        "dirty": False,
        "database_schema": 5,
        "files": {name: {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()} for name, body in payload.items()},
    }
    if manifest_change:
        manifest_change(manifest)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, body, kind in [
            ("manifest.json", host.canonical(manifest), tarfile.REGTYPE),
            *(entries if entries is not None else [(name, body, tarfile.REGTYPE) for name, body in payload.items()]),
        ]:
            row = tarfile.TarInfo(name)
            row.size, row.type = len(body), kind
            if kind == tarfile.SYMTYPE:
                row.linkname = "/etc/shadow"
                row.size = 0
            archive.addfile(row, io.BytesIO(body) if kind == tarfile.REGTYPE else None)
    path.write_bytes(gzip.compress(raw.getvalue() + suffix, mtime=0))
    return path


def test_private_temporary_directory_accepts_a_system_temp_path_behind_a_symlink(tmp_path, monkeypatch):
    actual = tmp_path / "actual-temp"
    alias = tmp_path / "system-temp-alias"
    actual.mkdir(mode=0o700)
    alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(alias))

    with protected_temporary_directory("reasoning-test-") as directory:
        assert directory.parent == actual.resolve()
        write_private(directory / "secret", b"private")


@pytest.fixture
def bundle(tmp_path, payload):
    return pack(tmp_path / "release.tar.gz", payload)


def test_release_verifies_hash_identity_retains_manifest_and_normalizes_permissions(bundle, payload, tmp_path):
    manifest, release_id, checksum = inspect_bundle(bundle)
    assert len(release_id) == len(checksum) == 64 and manifest["source_revision"] == "a" * 40
    assert inspect_bundle(bundle, checksum) == (manifest, release_id, checksum)
    directory = tmp_path / "unpacked"
    directory.mkdir(mode=0o700)
    previous = os.umask(0o077)
    try:
        extract_bundle(bundle, directory)
    finally:
        os.umask(previous)
    assert hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest() == release_id
    assert json.loads((directory / "manifest.json").read_bytes()) == manifest
    assert (directory / "manifest.json").stat().st_mode & 0o777 == 0o644
    from reasoning_ledger.server.bot_diagnostics import deployment_identity

    assert deployment_identity(directory / "web", release_id) == {"release_id": release_id, "source_revision": "a" * 40}
    for name, body in payload.items():
        assert (directory / name).read_bytes() == body
        assert (directory / name).stat().st_mode & 0o777 == 0o644
    assert (directory / "web/assets").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("mode", ["changed", "missing", "duplicate", "symlink", "traversal", "pax", "sparse", "trailing"])
def test_untrusted_release_records_are_rejected_before_any_host_command(tmp_path, payload, mode):
    entries = [(name, body, tarfile.REGTYPE) for name, body in payload.items()]
    if mode == "changed":
        entries[2] = (entries[2][0], b"different", tarfile.REGTYPE)
    elif mode == "missing":
        entries.pop()
    elif mode == "duplicate":
        entries.append(entries[-1])
    elif mode in {"symlink", "pax", "sparse"}:
        kind = {"symlink": tarfile.SYMTYPE, "pax": tarfile.XHDTYPE, "sparse": tarfile.GNUTYPE_SPARSE}[mode]
        entries.append(("malicious", b"", kind))
    elif mode == "traversal":
        entries.append(("../../escape", b"outside", tarfile.REGTYPE))
    bundle = pack(tmp_path / "invalid.tar.gz", payload, entries=entries, suffix=b"nonzero" if mode == "trailing" else b"")
    commands = []
    with pytest.raises(InstallError):
        HostInstaller(root=tmp_path / "host", command=lambda *args, **kwargs: commands.append(args)).install(
            bundle, hashlib.sha256(bundle.read_bytes()).hexdigest(), "https://chat.example.org", skip_platform=True
        )
    assert commands == []


def test_bundle_requires_expected_archive_hash_and_refuses_symlink(bundle, tmp_path):
    with pytest.raises(InstallError, match="SHA-256"):
        inspect_bundle(bundle, "0" * 64)
    link = tmp_path / "link"
    link.symlink_to(bundle)
    with pytest.raises(InstallError, match="regular archive"):
        inspect_bundle(link)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "https://127.0.0.1",
        "https://localhost",
        "https://user:pass@example.org",
        "https://Example.org",
        "https://example.org:8443",
        "https://example.org/path",
        "https://example.org?token=secret",
        "https://-chat.example.org",
        "https://example..org",
        "https://x.example.org\n{evil}",
    ],
)
def test_public_url_cannot_inject_proxy_or_systemd_configuration(url):
    with pytest.raises(InstallError):
        public_origin(url)


def test_proxy_and_service_boundaries_are_explicit():
    unit = host.server_unit("a" * 64)
    assert "User=reasoning\n" in unit and "--data-dir /var/lib/reasoning" in unit and "PrivateDevices=yes" in unit
    assert "CapabilityBoundingSet=\n" in unit and "ProtectSystem=strict" in unit and "docker" not in unit
    proxy = host.caddyfile("https://chat.example.org")
    assert "127.0.0.1:8766" in proxy and "header_up X-Forwarded-For {remote_host}" in proxy and "admin off" in proxy
    assert "access_log" not in proxy and "User=caddy" in host.proxy_unit()
    compile(STAGE_SCRIPT, "remote-stage", "exec")


class HostCommands:
    def __init__(self):
        self.calls, self.account, self.fail = [], False, None

    def __call__(self, args, **_kwargs):
        self.calls.append(args)
        if args[0] == "useradd":
            self.account = True
        if args[0] == self.fail:
            raise InstallError("synthetic package interruption")
        code = 3 if args[:2] == ["systemctl", "is-active"] else 0
        return subprocess.CompletedProcess(args, code)


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    root, commands = tmp_path / "host", HostCommands()
    root.mkdir(mode=0o700)

    def identity(name):
        assert name == "reasoning"
        if not commands.account:
            raise KeyError(name)
        return SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())

    monkeypatch.setattr(host.pwd, "getpwnam", identity)
    return HostInstaller(root=root, command=commands), commands


def test_bootstrap_fixture_keeps_code_and_private_data_separate_and_is_resumable(bundle, bootstrap):
    installer, commands = bootstrap
    checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
    state = installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True)
    assert state["phase"] == "services_started"
    root = installer.root
    assert (root / "var/lib/reasoning").stat().st_mode & 0o777 == 0o700
    assert installer.state_path.stat().st_mode & 0o777 == 0o600
    assert (root / "etc/reasoning/compute-images.json").read_bytes() == b"[]\n"
    assert not (root / "opt/reasoning" / "encryption.key").exists()
    assert any("--require-hashes" in args for args in commands.calls)
    assert ["systemctl", "disable", "--now", "caddy"] in commands.calls
    assert installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True) == state
    assert len([args for args in commands.calls if args[0] == "useradd"]) == 1


def test_interrupted_packages_resume_exact_plan_but_never_implicitly_upgrade(bundle, bootstrap):
    installer, commands = bootstrap
    checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
    commands.fail = "apt-get"
    with pytest.raises(InstallError, match="interruption"):
        installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True)
    assert json.loads(installer.state_path.read_bytes())["phase"] == "packages"
    commands.fail = None
    installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True)
    count = len(commands.calls)
    with pytest.raises(InstallError, match="explicit upgrade"):
        installer.install(bundle, checksum, "https://other.example.org", skip_platform=True)
    assert len(commands.calls) == count


@pytest.mark.parametrize("relative", ["var/lib/reasoning", "etc/reasoning", "opt/reasoning", "etc/caddy/Caddyfile"])
def test_existing_unmanaged_paths_are_preserved_before_package_changes(bundle, bootstrap, relative):
    installer, commands = bootstrap
    existing = installer.root / relative
    existing.parent.mkdir(parents=True, exist_ok=True)
    for parent in existing.parents:
        if parent == installer.root:
            break
        parent.chmod(0o755)
    existing.write_bytes(b"unrelated user data")
    with pytest.raises(InstallError):
        installer.install(bundle, hashlib.sha256(bundle.read_bytes()).hexdigest(), "https://chat.example.org", skip_platform=True)
    assert existing.read_bytes() == b"unrelated user data" and not commands.calls


def test_managed_parent_symlinks_are_refused_without_changing_their_target(bundle, bootstrap, tmp_path):
    installer, commands = bootstrap
    outside = tmp_path / "outside"
    outside.mkdir()
    (installer.root / "var").symlink_to(outside)
    with pytest.raises(InstallError, match="symlinks"):
        installer.install(bundle, hashlib.sha256(bundle.read_bytes()).hexdigest(), "https://chat.example.org", skip_platform=True)
    assert commands.calls == [] and list(outside.iterdir()) == []


def test_public_frontend_collects_only_reachable_dependencies_and_not_workbench(payload, tmp_path):
    frontend = tmp_path / "web"
    for name, body in payload.items():
        if name.startswith("web/"):
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    (frontend / "index.html").write_bytes(b"local workbench")
    (frontend / "assets/workbench-x.js").write_bytes(b"local native bridge")
    result = release.public_frontend(frontend)
    assert "web/assets/shared-x.js" in result and not any("workbench" in name or "index.html" in name for name in result)
    (frontend / "assets/organization-x.js").write_bytes(b'import "./workbench-x.js";')
    with pytest.raises(InstallError, match="workbench"):
        release.public_frontend(frontend)


def test_public_frontend_collects_asset_urls_the_bundler_writes_as_template_literals(payload, tmp_path):
    frontend = tmp_path / "web"
    for name, body in payload.items():
        if name.startswith("web/"):
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    # The shape of the built PDF viewer chunk: pdf.js's own default worker name, then the imported asset URLs.
    (frontend / "assets/shared-x.js").write_bytes(
        b"workerSrc||=`./pdf.worker.mjs`;var w=`/assets/pdf.worker.min-AbCd1234.mjs`,"
        b"j=`/assets/jbig2-AbCd1234.wasm`,f=`/assets/FoxitSymbol-AbCd1234.pfb`;"
    )
    for name in ("pdf.worker.min-AbCd1234.mjs", "jbig2-AbCd1234.wasm", "FoxitSymbol-AbCd1234.pfb"):
        (frontend / "assets" / name).write_bytes(b"built asset")
    result = release.public_frontend(frontend)
    assert {"web/assets/pdf.worker.min-AbCd1234.mjs", "web/assets/jbig2-AbCd1234.wasm",
            "web/assets/FoxitSymbol-AbCd1234.pfb"} <= result.keys()
    assert "web/assets/pdf.worker.mjs" not in result


@pytest.mark.parametrize(
    "body",
    [
        b"fixture>=1",
        b"--extra-index-url https://secret.example",
        b"fixture @ https://other.example/file.whl",
        b"-e .",
        b"fixture==1 --hash=sha256:bad",
        b"fixture==1 --hash=sha256:" + b"a" * 64 + b" --trusted-host evil",
    ],
)
def test_dependency_export_cannot_add_unpinned_sources_or_pip_options(body):
    with pytest.raises(InstallError, match="hash-locked"):
        release.validate_requirements(body)


def test_actual_lockfile_export_is_hashed_and_unchanged():
    root = Path(__file__).resolve().parents[1]
    before = (root / "uv.lock").read_bytes()
    exported = release.export_requirements(root)
    assert b"fastapi==" in exported and b"cryptography==50.0.1" in exported and b"--hash=sha256:" in exported
    assert b"modal==" not in exported
    optional = release.export_requirements(root, modal=True)
    assert b"modal==1.6.0" in optional and b"--hash=sha256:" in optional
    assert (root / "uv.lock").read_bytes() == before


def test_remote_install_uploads_public_artifacts_and_verifies_host_key_without_token_arguments(bundle, tmp_path):
    identity, known = tmp_path / "identity", tmp_path / "known hosts"
    identity.write_bytes(b"nonfunctional private key fixture")
    identity.chmod(0o600)
    known.write_bytes(b"nonfunctional host key fixture")
    known.chmod(0o644)
    calls = []
    manifest, release_id, checksum = inspect_bundle(bundle)
    receipt = {
        **host.installation_binding(manifest, release_id, checksum, "https://chat.example.org"),
        "phase": "services_started",
        "service_active": True,
        "https_ready": True,
        "initialized": False,
        "setup_token_file": "/var/lib/reasoning/setup-token",
    }

    def remote(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=host.canonical(receipt))

    result = install_remote(bundle, "https://chat.example.org", "203.0.113.20", identity, known, command=remote)
    assert result == receipt
    assert len(calls) == 4 and calls[0][0] == calls[-1][0] == "ssh"
    assert all("StrictHostKeyChecking=yes" in args and "BatchMode=yes" in args for args in calls)
    assert all("nonfunctional private key" not in " ".join(args) for args in calls)
    assert result["bundle_sha256"] in calls[-1][-1] and "--expected-sha256" in shlex.split(calls[-1][-1])


@pytest.mark.parametrize("change", ["public_write", "hardlink", "symlink", "oversized"])
def test_ssh_known_hosts_must_be_protected_and_bounded(tmp_path, change):
    identity, known = tmp_path / "identity", tmp_path / "known"
    identity.write_bytes(b"nonfunctional private key fixture")
    identity.chmod(0o600)
    known.write_bytes(b"nonfunctional host key fixture")
    known.chmod(0o644)
    if change == "public_write":
        known.chmod(0o666)
    elif change == "hardlink":
        os.link(known, tmp_path / "second")
    elif change == "symlink":
        known.rename(tmp_path / "outside")
        known.symlink_to(tmp_path / "outside")
    else:
        known.write_bytes(b"x" * (1024**2 + 1))
    with pytest.raises(InstallError):
        ssh_arguments("203.0.113.20", identity, known)


def test_inspect_cli_and_confirmation_do_not_execute_remote_changes(bundle, capsys):
    assert main(["inspect", "--bundle", str(bundle)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["release_id"]) == 64
    with pytest.raises(SystemExit) as error:
        main(
            [
                "install",
                "--bundle",
                str(bundle),
                "--public-url",
                "https://chat.example.org",
                "--ssh-host",
                "203.0.113.20",
                "--identity",
                "missing",
                "--known-hosts",
                "missing",
                "--confirm-release",
                "wrong",
            ]
        )
    assert error.value.code == 2 and "confirm-release" in capsys.readouterr().err


@pytest.fixture
def provider():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == "api.digitalocean.com"
        if request.url.path == "/v2/account":
            body = {"account": {"uuid": "a" * 40, "status": "active", "email_verified": True}}
        elif request.url.path == "/v2/sizes":
            body = {
                "sizes": [
                    {
                        "slug": "s-2vcpu-4gb",
                        "available": True,
                        "regions": ["fra1"],
                        "price_hourly": 0.035,
                        "memory": 4096,
                        "vcpus": 2,
                        "disk": 80,
                    }
                ]
            }
        elif request.url.path == "/v2/account/keys/123":
            body = {"ssh_key": {"id": 123, "public_key": "ssh-ed25519 fixture", "fingerprint": "fixture fingerprint"}}
        elif request.url.path == "/v2/domains/example.org":
            body = {"domain": {"name": "example.org"}}
        else:
            assert request.url.path == "/v2/domains/example.org/records"
            body = {"domain_records": [{"name": "@", "type": "MX", "data": "mail.example.org"}]}
        return httpx.Response(200, json=body)

    return DigitalOcean("fixture-token-" + "x" * 32, transport=httpx.MockTransport(handler)), calls


def plan(provider, **changes):
    client, _ = provider
    return client.plan(
        **{
            "name": "mathematics",
            "region": "fra1",
            "size": "s-2vcpu-4gb",
            "origin": "https://chat.example.org",
            "dns_zone": "example.org",
            "ssh_key_id": 123,
            "admin_cidr": "203.0.113.10/32",
            "maximum_hourly_usd": "0.10",
            **changes,
        }
    )


def test_digitalocean_plan_uses_live_read_only_quote_and_never_reports_a_compute_invoice_cap(provider):
    result = plan(provider)
    assert result["creates_resources"] is False and result["dns_record_name"] == "chat"
    assert result["quote"]["compute_hourly_usd"] == "0.035" and result["quote"]["estimated_730_hours_usd"] == "25.550"
    assert "backups" in result["quote"]["excluded"] and "fixture-token" not in json.dumps(result)
    assert len(provider[1]) == 5 and all(request.method == "GET" for request in provider[1])


@pytest.mark.parametrize(
    "changes",
    [
        {"maximum_hourly_usd": "0.01"},
        {"maximum_hourly_usd": "NaN"},
        {"admin_cidr": "0.0.0.0/0"},
        {"admin_cidr": "203.0.113.1/24"},
        {"origin": "https://other.example.net"},
        {"name": "-evil"},
        {"region": "nonexistent"},
        {"ssh_key_id": True},
        {"size": "gpu-h100"},
    ],
)
def test_digitalocean_plan_refuses_unsafe_or_unaffordable_selections(provider, changes):
    with pytest.raises(InstallError):
        plan(provider, **changes)


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_digitalocean_errors_never_follow_redirects_or_echo_remote_credentials(status):
    token = "fixture-private-" + "x" * 32
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://attacker.example"}, json={"detail": token})

    client = DigitalOcean(token, transport=httpx.MockTransport(handler))
    with pytest.raises(InstallError) as error:
        client.get("sizes")
    assert token not in str(error.value) and len(calls) == 1


@pytest.fixture
def builder_inputs(payload, tmp_path, monkeypatch):
    from test_python_distribution import build as wheel_build
    from test_python_distribution import release as wheel_fixture

    # Reuse the verifier's synthetic, legally complete wheel fixture without
    # claiming these two placeholder modules are a production application.
    source, contents = wheel_fixture.__wrapped__(tmp_path)
    wheel = wheel_build(source, contents)
    renamed = source / "reasoning_ledger-0.1.0-py3-none-any.whl"
    wheel.rename(renamed)
    frontend = source / "web"
    for name, body in payload.items():
        if name.startswith("web/"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    (frontend / "licenses/Reasoning-Apache-2.0.txt").write_bytes((source / "LICENSE").read_bytes())
    (frontend / "licenses/Reasoning-NOTICE.txt").write_bytes((source / "NOTICE").read_bytes())
    for name in ("KaTeX-MIT.txt", "IBM-Plex-OFL-1.1.txt", *release.PDFJS_NOTICES):
        path = source / "web/public/licenses" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((frontend / "licenses" / name).read_bytes())
    for name in ("server", "inference", "compute", "deployment", "backup", "bots", "images", "operations"):
        path = source / f"docs/{name}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"fixture operating doc")
    monkeypatch.setattr(release, "source_identity", lambda *_args: ("a" * 40, True))
    monkeypatch.setattr(release, "export_requirements", lambda *_args, **_kwargs: payload["requirements.txt"])
    return source, renamed, frontend


def test_release_builder_is_reproducible_and_requires_explicit_output_identity(builder_inputs):
    source, renamed, frontend = builder_inputs
    first, second = source / "first.tar.gz", source / "second.tar.gz"
    a = release.build_bundle(source, renamed, frontend, first, allow_dirty=True)
    b = release.build_bundle(source, renamed, frontend, second, allow_dirty=True)
    assert a["bundle_sha256"] == b["bundle_sha256"] and first.read_bytes() == second.read_bytes() and a["dirty"] is True
    assert host.safe_name("docs/images.md")
    assert "docs/images.md" in inspect_bundle(first)[0]["files"]
    with pytest.raises(InstallError, match="already exists"):
        release.build_bundle(source, renamed, frontend, first, allow_dirty=True)


def test_release_builder_bundles_checkout_published_approvals_without_network_discovery(builder_inputs):
    from test_public_compute import IMAGE

    source, wheel, frontend = builder_inputs
    catalog = source / "images/compute-images.json"
    catalog.parent.mkdir()
    catalog.write_text(json.dumps([IMAGE]))
    output = source / "with-catalog.tar.gz"
    release.build_bundle(source, wheel, frontend, output, allow_dirty=True)
    manifest = inspect_bundle(output)[0]
    expected = release.approved_catalog(catalog.read_bytes())
    assert manifest["files"]["compute-images.json"]["sha256"] == hashlib.sha256(expected).hexdigest()
    with tarfile.open(output, "r:gz") as archive:
        assert archive.extractfile("compute-images.json").read() == expected


def test_release_catalog_selection_requires_explicit_override_or_exact_published_path(tmp_path):
    assert release.select_release_catalog(tmp_path) is None
    candidate = tmp_path / "candidate.json"
    candidate.write_text("[]")
    assert release.select_release_catalog(tmp_path) is None
    assert release.select_release_catalog(tmp_path, candidate) == candidate
    images = tmp_path / "images"
    images.mkdir()
    published = images / "compute-images.json"
    published.write_text("[]")
    assert release.select_release_catalog(tmp_path) == published
    assert release.select_release_catalog(tmp_path, candidate) == candidate
    with pytest.raises(InstallError, match="1–32 approved"):
        release.approved_catalog(published.read_bytes())


def test_release_builder_refuses_invalid_default_catalog_instead_of_omitting_it(builder_inputs):
    source, wheel, frontend = builder_inputs
    catalog = source / "images/compute-images.json"
    catalog.parent.mkdir()
    catalog.write_text("[]")
    output = source / "invalid-catalog.tar.gz"
    with pytest.raises(InstallError, match="1–32 approved"):
        release.build_bundle(source, wheel, frontend, output, allow_dirty=True)
    assert not output.exists()


def test_explicit_release_catalog_overrides_checkout_defaults(builder_inputs):
    from test_public_compute import IMAGE

    source, wheel, frontend = builder_inputs
    published = source / "images/compute-images.json"
    published.parent.mkdir()
    published.write_text("[]")
    override = source / "operator-approved.json"
    override.write_text(json.dumps([IMAGE]))
    output = source / "override-catalog.tar.gz"
    release.build_bundle(source, wheel, frontend, output, allow_dirty=True, compute_catalog=override)
    with tarfile.open(output, "r:gz") as archive:
        assert archive.extractfile("compute-images.json").read() == release.approved_catalog(override.read_bytes())


def test_release_default_catalog_cannot_follow_linked_directory_or_file(builder_inputs):
    from test_public_compute import IMAGE

    source, wheel, frontend = builder_inputs
    external = source / "external"
    external.mkdir()
    (external / "compute-images.json").write_text(json.dumps([IMAGE]))
    images = source / "images"
    images.symlink_to(external, target_is_directory=True)
    output = source / "linked-catalog.tar.gz"
    with pytest.raises(InstallError, match="symlink"):
        release.build_bundle(source, wheel, frontend, output, allow_dirty=True)
    images.unlink()
    images.mkdir()
    (images / "compute-images.json").symlink_to(external / "compute-images.json")
    with pytest.raises(InstallError, match="symlink"):
        release.build_bundle(source, wheel, frontend, output, allow_dirty=True)
    assert not output.exists()


@pytest.mark.parametrize("notice", ["Reasoning-Apache-2.0.txt", "Reasoning-NOTICE.txt", "KaTeX-MIT.txt", "IBM-Plex-OFL-1.1.txt",
                                    *release.PDFJS_NOTICES])
def test_release_builder_refuses_stale_built_notices(builder_inputs, notice):
    source, wheel, frontend = builder_inputs
    (frontend / "licenses" / notice).write_bytes(b"stale notice")
    output = source / "never-written.tar.gz"
    with pytest.raises(InstallError, match="stale legal material"):
        release.build_bundle(source, wheel, frontend, output, allow_dirty=True)
    assert not output.exists()


@pytest.mark.parametrize("retain_marker", [False, True])
def test_existing_operator_edited_configuration_is_not_overwritten_during_resume(bundle, bootstrap, retain_marker):
    installer, commands = bootstrap
    checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
    installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True)
    path = installer.root / "etc/reasoning/Caddyfile"
    body = (host.MANAGED.encode() if retain_marker else b"") + b"operator-owned configuration"
    path.write_bytes(body)
    before = len(commands.calls)
    with pytest.raises(InstallError, match="unmanaged"):
        installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True)
    assert len(commands.calls) == before and path.read_bytes() == body


@pytest.mark.parametrize("change", ["symlink", "public_write"])
def test_resume_checks_configuration_parents_before_package_changes(bundle, bootstrap, tmp_path, change):
    installer, commands = bootstrap
    checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
    installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True)
    path = installer.root / "etc"
    if change == "symlink":
        outside = tmp_path / "outside-etc"
        path.rename(outside)
        path.symlink_to(outside)
    else:
        path.chmod(0o777)
    before = len(commands.calls)
    with pytest.raises(InstallError, match="configuration parents"):
        installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True)
    assert len(commands.calls) == before


@pytest.mark.parametrize("field,value", [("phase", "unknown"), ("phase", []), ("dirty", 1), ("source_revision", "0" * 40)])
def test_invalid_bootstrap_journal_refuses_resume_before_host_commands(bundle, bootstrap, field, value):
    installer, commands = bootstrap
    checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
    installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True)
    state = json.loads(installer.state_path.read_bytes())
    state[field] = value
    installer.state_path.write_bytes(host.canonical(state))
    before = len(commands.calls)
    with pytest.raises(InstallError, match="invalid private installation journal"):
        installer.install(bundle, checksum, "https://chat.example.org", skip_platform=True)
    assert len(commands.calls) == before


@pytest.mark.parametrize("encoding", ["pretty", "duplicate"])
def test_manifest_identity_requires_canonical_json_without_duplicate_keys(bundle, encoding):
    manifest, _, _ = inspect_bundle(bundle)
    body = (
        json.dumps(manifest, indent=2).encode()
        if encoding == "pretty"
        else host.canonical(manifest).replace(b'"format":1', b'"format":1,"format":1')
    )
    with pytest.raises(InstallError, match="invalid release manifest"):
        host.validate_manifest(body)


@pytest.mark.parametrize("initialized", [False, True])
def test_readiness_requires_health_shape_and_no_environment_proxy(monkeypatch, initialized):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _maximum):
            return json.dumps({"status": "ok", "initialized": initialized}).encode()

    class Opener:
        def open(self, url, timeout):
            assert url == "https://chat.example.org/api/health" and timeout == 3
            return Response()

    def build(*handlers):
        assert handlers[0].proxies == {} and isinstance(handlers[1], host.NoRedirect)
        return Opener()

    monkeypatch.setattr(host.urllib.request, "build_opener", build)
    assert host.ready("https://chat.example.org", timeout=0) == {
        "public_url": "https://chat.example.org",
        "https_ready": True,
        "initialized": initialized,
    }


@pytest.mark.parametrize("value", [[], {"status": "ok", "initialized": 1}, {"status": "wrong", "initialized": False}])
def test_malformed_readiness_never_counts_as_success(monkeypatch, value):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _maximum):
            return json.dumps(value).encode()

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(host.urllib.request, "build_opener", lambda *_args: Opener())
    with pytest.raises(InstallError, match="unverified"):
        host.ready("https://chat.example.org", timeout=0)


def test_bootstrap_has_exclusive_private_ownership(bundle, bootstrap):
    import fcntl

    installer, commands = bootstrap
    path = installer.root / "var/lib/reasoning-deploy"
    path.mkdir(parents=True, mode=0o700)
    (installer.root / "var").chmod(0o755)
    (installer.root / "var/lib").chmod(0o755)
    fd = os.open(path / "bootstrap.lock", os.O_RDONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InstallError, match="another bootstrap"):
            installer.install(bundle, hashlib.sha256(bundle.read_bytes()).hexdigest(), "https://chat.example.org", skip_platform=True)
        assert commands.calls == []
    finally:
        os.close(fd)


def test_digitalocean_conflicting_dns_records_are_not_replaced(provider):
    client, _ = provider
    original = client.pages

    def records(path, field, **params):
        if field == "domain_records":
            return [{"name": "chat", "type": "CNAME", "data": "existing.example.org"}]
        return original(path, field, **params)

    client.pages = records
    with pytest.raises(InstallError, match="already has"):
        plan(provider)
