"""Source delivery installs actual modules and entry points without application wheels."""

import io
import json
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest

from reasoning_ledger import source_install
from reasoning_ledger.deployment import host, release
from reasoning_ledger.source_release import source_bytes

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "reasoning-source.zip"
    path.write_bytes(source_bytes(ROOT, b"locked dependencies\n", b"locked Modal dependencies\n"))
    return path


def test_source_archive_is_reproducible_complete_and_has_no_private_or_binary_files(archive):
    assert archive.read_bytes() == source_bytes(ROOT, b"locked dependencies\n", b"locked Modal dependencies\n")
    with ZipFile(archive) as source:
        scripts = source_install.inspect_source(source)
        assert scripts["reasoning-bot"] == "reasoning_ledger.server.bot_client:main"
        assert source.read("LICENSE") == (ROOT / "LICENSE").read_bytes()
        package = ROOT / "src/reasoning_ledger"
        expected = {path.relative_to(ROOT / "src").as_posix() for path in package.rglob("*.py")}
        assert {name for name in source.namelist() if name.endswith(".py")} == expected
        assert not any(name.endswith((".whl", ".pyc")) or "tmp5.json" in name for name in source.namelist())


def test_source_installs_in_real_venv_and_generated_entry_points_are_retry_safe(archive, tmp_path):
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    command = [str(venv / "bin/python"), "-I", source_install.__file__, str(archive)]
    subprocess.run(command, check=True)
    subprocess.run(command, check=True)
    # Reuse already-tested dependencies without network; application code is installed here.
    dependency_site = sysconfig.get_path("purelib")
    subprocess.run([str(venv / "bin/python"), "-I", "-c", "import sysconfig; from pathlib import Path; "
                    f"Path(sysconfig.get_path('purelib'), 'test-dependencies.pth').write_text({dependency_site!r} + '\\n')"], check=True)
    code = "import reasoning_ledger; from pathlib import Path; print(Path(reasoning_ledger.__file__).read_text()[:20])"
    subprocess.run([str(venv / "bin/python"), "-I", "-c", code], check=True, capture_output=True)
    subprocess.run([str(venv / "bin/reasoning-server"), "--help"], check=True, capture_output=True)
    script = venv / "bin/reasoning-server"
    assert script.read_text().startswith(f"#!{venv}/bin/python\n")
    assert script.stat().st_mode & 0o111
    script.write_text("tampered entry point")
    assert subprocess.run(command, capture_output=True).returncode == 2


@pytest.mark.parametrize("name", ["../escape.py", "reasoning_ledger/../escape.py", "reasoning_ledger/bad.pyc", "source-info.json"])
def test_source_archive_rejects_traversal_duplicates_and_bytecode(archive, name):
    output = io.BytesIO(archive.read_bytes())
    with ZipFile(output, "a") as source:
        source.writestr(name, b"bad")
    with ZipFile(output) as source, pytest.raises(ValueError):
        source_install.inspect_source(source)


def test_source_installer_dispatch_and_manifest_paths(archive, tmp_path):
    commands = []
    host.install_python_payload(commands.append, tmp_path, {"files": {"source/reasoning-source.zip": {}}})
    assert commands == [[str(tmp_path / "venv/bin/python"), "-I", str(tmp_path / "source-installer.py"),
                         str(tmp_path / "source/reasoning-source.zip")]]
    assert host.safe_name("source/reasoning-source.zip") and host.safe_name("source-installer.py")
    assert not host.safe_name("source/private.env")
    with ZipFile(archive) as source:
        assert json.loads(source.read("source-info.json"))["format"] == 1


def test_new_release_bundle_contains_source_not_a_wheel_and_verifies_every_payload(tmp_path, monkeypatch):
    frontend = {"web/organization.html": b'<script src="/assets/entry.js"></script>',
                "web/reasoning.svg": b"<svg/>", "web/assets/entry.js": b"export const fixture=true;"}
    for name, path in {"Reasoning-Apache-2.0.txt": ROOT / "LICENSE", "Reasoning-NOTICE.txt": ROOT / "NOTICE",
                       "KaTeX-MIT.txt": ROOT / "web/public/licenses/KaTeX-MIT.txt",
                       "IBM-Plex-OFL-1.1.txt": ROOT / "web/public/licenses/IBM-Plex-OFL-1.1.txt",
                       **{name: ROOT / "web/public/licenses" / name for name in release.PDFJS_NOTICES}}.items():
        frontend["web/licenses/" + name] = path.read_bytes()
    monkeypatch.setattr(release, "public_frontend", lambda _: frontend)
    monkeypatch.setattr(release, "export_requirements", lambda *args, **kwargs: b"fixture==1 --hash=sha256:" + b"a" * 64 + b"\n")
    output = tmp_path / "release.tar.gz"
    result = release.build_bundle(ROOT, None, ROOT / "web/dist", output, allow_dirty=True)
    manifest, identifier, checksum = host.inspect_bundle(output)
    assert result["release_id"] == identifier and result["bundle_sha256"] == checksum
    assert "source/reasoning-source.zip" in manifest["files"] and "source-installer.py" in manifest["files"]
    assert not any(name.startswith("wheel/") for name in manifest["files"])
    with tarfile.open(output) as bundle, ZipFile(io.BytesIO(bundle.extractfile("source/reasoning-source.zip").read())) as source:
        assert source_install.inspect_source(source)["reasoning-server"] == "reasoning_ledger.server.cli:main"
