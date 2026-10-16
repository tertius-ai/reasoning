"""Collect pinned source archives and signed-index OS sources without approving a release.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pwd
import re
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

import httpx

from .image_context import NOETHER_PUBLIC_COMMIT
from .image_publication import hash_file, public_url
from .image_release import SOURCE_PATHS, BoundedTarInfo, command, member_name, write_new
from .server.compute_catalog import canonical

MAX_ARTIFACT = 8 * 1024**3
NOETHER_PIN = NOETHER_PUBLIC_COMMIT


def inventory(sbom: dict) -> list[dict]:
    """Keep exact binary/source versions; do not infer licenses or source correspondence."""
    packages = sbom.get("packages")
    if not isinstance(packages, list) or not 1 <= len(packages) <= 10000:
        raise ValueError("a bounded installed SPDX package inventory is required")
    result, seen = [], set()
    for package in packages:
        identity, name, version = (package.get(key) for key in ("SPDXID", "name", "versionInfo"))
        if not all(isinstance(value, str) and value for value in (identity, name, version)) or identity in seen:
            raise ValueError("installed component identity is missing or duplicated")
        seen.add(identity)
        detail = {}
        if isinstance(package.get("comment"), str):
            try:
                parsed = json.loads(package["comment"])
                if isinstance(parsed, dict):
                    detail = parsed
            except ValueError:
                pass
        result.append(
            {
                "id": identity,
                "name": name,
                "version": version,
                "kind": detail.get("kind", "distribution"),
                "source": detail.get("source", name),
                "source_version": detail.get("source_version", version),
            }
        )
    return sorted(result, key=lambda item: item["id"])


def download(url: str, destination: Path, expected: str, *, client=None) -> dict:
    """Retain exact bytes, accept only HTTPS redirects, never extract or execute upstream inputs."""
    public_url(url)
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("every upstream archive needs a previously locked SHA256")
    own_client = client is None
    client = client or httpx.Client(trust_env=False, follow_redirects=False, timeout=120, headers={"Accept-Encoding": "identity"})
    try:
        current = url
        for _ in range(6):
            # GitHub's generated release-asset URL can contain public signature parameters;
            # never accept caller-supplied credentials or propagate cookies/authorization.
            parsed = httpx.URL(current)
            if parsed.scheme != "https" or parsed.userinfo:
                raise ValueError("source archive redirect must stay on credential-free HTTPS")
            public_url(str(parsed.copy_with(query=None, fragment=None)))
            with client.stream("GET", current, headers={"Cookie": "", "Authorization": ""}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    current = str(response.url.join(response.headers["location"]))
                    continue
                if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("source archive download failed")
                count, digest = 0, hashlib.sha256()
                with destination.open("xb") as output:
                    for chunk in response.iter_bytes(1024**2):
                        count += len(chunk)
                        if count > MAX_ARTIFACT:
                            raise ValueError("source archive exceeds its allowance")
                        output.write(chunk)
                        digest.update(chunk)
                if not count or digest.hexdigest() != expected:
                    raise ValueError("source archive differs from the locked SHA256")
                return {"sha256": expected, "bytes": count, "url": url}
        raise ValueError("source archive exceeded its redirect allowance")
    finally:
        if own_client:
            client.close()


def apt_sources(components: list[dict], payload: Path, *, snapshot: str, keyring: Path) -> tuple[dict, list[dict]]:
    """Use only authenticated deb-src indexes in private apt state; never install packages."""
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", snapshot):
        raise ValueError("choose an immutable Ubuntu snapshot timestamp")
    hash_file(keyring, maximum=1024**2)
    sources, unresolved = {}, []
    pairs = sorted({(item["source"], item["source_version"]) for item in components if item["kind"] == "deb"})
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) or not re.fullmatch(r"[A-Za-z0-9.+:~_-]+", version) for name, version in pairs):
        raise ValueError("OS source name or exact source version is invalid")
    with tempfile.TemporaryDirectory(prefix="reasoning-apt-sources-") as temporary:
        state = Path(temporary)
        for directory in ("lists/partial", "cache/archives/partial"):
            (state / directory).mkdir(parents=True)
        source_list = state / "sources.list"
        source_list.write_text(
            "".join(
                f"deb-src https://snapshot.ubuntu.com/ubuntu/{snapshot} {suite} main universe restricted multiverse\n"
                for suite in ("jammy", "jammy-updates", "jammy-security")
            )
        )
        options = [
            "apt-get",
            "-o",
            "Dir::Etc::main=-",
            "-o",
            "Dir::Etc::parts=-",
            "-o",
            "Dir::Etc::sourcelist=" + str(source_list),
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "Dir::Etc::Trusted=" + str(keyring),
            "-o",
            "Dir::Etc::TrustedParts=-",
            "-o",
            "Dir::State=" + str(state),
            "-o",
            "Dir::State::lists=" + str(state / "lists"),
            "-o",
            "Dir::Cache=" + str(state / "cache"),
            "-o",
            "Dir::Log=" + str(state),
            "-o",
            "APT::Sandbox::User=" + pwd.getpwuid(os.getuid()).pw_name,
            "-o",
            "APT::Architecture=amd64",
            "-o",
            "Acquire::Check-Valid-Until=false",
            "-o",
            "Acquire::AllowInsecureRepositories=false",
            "-o",
            "APT::Get::AllowUnauthenticated=false",
            "-o",
            "APT::Update::Error-Mode=any",
        ]
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8"}
        update = subprocess.run([*options, "update"], cwd=state, env=environment, capture_output=True, timeout=600)
        if update.returncode:
            return {}, [
                {"source": name, "source_version": version, "reason": "signed source index update failed"} for name, version in pairs
            ]
        evidence = payload / "apt-evidence" / snapshot
        evidence.mkdir(parents=True)
        (evidence / "archive-keyring.gpg").write_bytes(keyring.read_bytes())
        (evidence / "sources.list").write_bytes(source_list.read_bytes())
        for path in sorted((state / "lists").iterdir()):
            if path.is_file() and not path.is_symlink() and path.name != "lock":
                (evidence / path.name).write_bytes(path.read_bytes())
        for name, version in pairs:
            identity = hashlib.sha256((name + "\0" + version).encode()).hexdigest()[:20]
            directory = payload / "apt" / snapshot / identity
            directory.mkdir(parents=True)
            result = subprocess.run(
                [*options, "--download-only", "source", name + "=" + version],
                cwd=directory,
                env=environment,
                capture_output=True,
                timeout=600,
            )
            files = sorted(directory.iterdir())
            if result.returncode or not files or not any(path.suffix == ".dsc" for path in files):
                failure_root = payload.parent / "download-failures"
                failure_root.mkdir(mode=0o700, exist_ok=True)
                failure_snapshot = failure_root / snapshot
                failure_snapshot.mkdir(mode=0o700, exist_ok=True)
                retained = failure_snapshot / (identity + "-" + uuid.uuid4().hex)
                # Rename only this operation's exact owned directory. Partial source
                # bytes remain recoverable, but cannot enter the bundle or notice scan.
                directory.rename(retained)
                unresolved.append(
                    {
                        "source": name,
                        "source_version": version,
                        "reason": "exact source version unavailable or authenticated download failed",
                        "retained_in": str(retained.relative_to(payload.parent)),
                    }
                )
                # Incomplete archive bytes are never considered included source evidence.
                continue
            if any(path.is_symlink() or not path.is_file() for path in files):
                raise ValueError("apt source download produced a non-regular artifact")
            sources[(name, version)] = [str(path.relative_to(payload)) for path in files]
    return sources, unresolved


def cargo_packages(source_archive: Path) -> list[dict]:
    import tomllib

    with tarfile.open(source_archive, tarinfo=BoundedTarInfo) as archive:
        entries = [item for item in archive if item.name == "Cargo.lock" and item.isreg()]
        if len(entries) != 1 or entries[0].size > 4 * 1024**2:
            raise ValueError("Noether source archive must include its bounded root Cargo.lock")
        packages = tomllib.loads(archive.extractfile(entries[0]).read().decode())["package"]
    if not isinstance(packages, list) or not 1 <= len(packages) <= 10000:
        raise ValueError("Cargo source inventory must be bounded")
    return packages


def local_workspace_sources(source_archive: Path) -> set[tuple[str, str]]:
    """Match exact local lock identities to actual retained package manifests."""
    import tomllib

    local = {(item["name"], item["version"]) for item in cargo_packages(source_archive) if "source" not in item}
    manifests, total = {}, 0
    with tarfile.open(source_archive, tarinfo=BoundedTarInfo) as archive:
        for entry in archive:
            if entry.name != "Cargo.toml" and not entry.name.endswith("/Cargo.toml"):
                continue
            total += entry.size
            if not entry.isreg() or entry.name in manifests or entry.size > 1024**2 or total > 32 * 1024**2:
                raise ValueError("local Cargo package manifests must be unique bounded regular files")
            manifests[entry.name] = tomllib.loads(archive.extractfile(entry).read().decode())
    workspace_version = manifests.get("Cargo.toml", {}).get("workspace", {}).get("package", {}).get("version")
    matches = set()
    for manifest in manifests.values():
        package = manifest.get("package", {})
        version = package.get("version")
        if version == {"workspace": True}:
            version = workspace_version
        name = package.get("name")
        if isinstance(name, str) and isinstance(version, str) and (name, version) in local:
            matches.add((name, version))
    return matches


def cargo_sources(source_archive: Path, payload: Path, *, cached_crates: Path | None = None) -> list[str]:
    """Convey the whole exact Cargo.lock source superset, not a fabricated runtime closure."""
    packages = cargo_packages(source_archive)
    paths = []
    directory = payload / "cargo"
    directory.mkdir()
    for package in packages:
        if "source" not in package:
            continue
        if (
            package["source"] != "registry+https://github.com/rust-lang/crates.io-index"
            or not re.fullmatch(r"[A-Za-z0-9_-]+", package["name"])
            or not re.fullmatch(r"[A-Za-z0-9.+_-]+", package["version"])
        ):
            raise ValueError("Cargo source is not a locked supported registry crate")
        name, version = package["name"], package["version"]
        destination = directory / f"{name}-{version}.crate"
        cached = cached_crates / destination.name if cached_crates else None
        if cached is not None and cached.exists():
            if hash_file(cached, maximum=MAX_ARTIFACT)[0] != package["checksum"]:
                raise ValueError("cached registry crate differs from the immutable Cargo.lock checksum")
            with cached.open("rb") as source, destination.open("xb") as target:
                import shutil

                shutil.copyfileobj(source, target, length=1024**2)
        else:
            download(f"https://static.crates.io/crates/{name}/{name}-{version}.crate", destination, package["checksum"])
        paths.append(str(destination.relative_to(payload)))
    return paths


def rust_recipe(recipe: bytes) -> dict:
    """Read the Rust pin from the retained commit's recipe, not a mutable worktree."""
    matches = []
    with tarfile.open(fileobj=io.BytesIO(recipe), mode="r:*", tarinfo=BoundedTarInfo) as archive:
        for entry in archive:
            if entry.name == "images/research-cpu/inputs.json":
                if not entry.isreg() or entry.size > 1024**2:
                    raise ValueError("Rust recipe must be a bounded regular input")
                matches.append(json.loads(archive.extractfile(entry).read()))
    if len(matches) != 1:
        raise ValueError("Rust source collection requires exactly one retained recipe input")
    noether = matches[0].get("noether", {})
    if noether.get("commit") != NOETHER_PIN or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", noether.get("rust_version", "")):
        raise ValueError("Rust source collection differs from the approved Noether/compiler pin")
    runtime = noether.get("rust_runtime", {})
    notice_hashes = runtime.get("notices", {})
    if set(notice_hashes) != {"COPYRIGHT-library.html", "LICENSE-MIT", "LICENSE-APACHE"} or any(
        not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in notice_hashes.values()
    ):
        raise ValueError("Rust recipe requires exactly three locked notice hashes")
    return noether


def validate_rust_artifact(item: dict, receipt: dict, noether: dict) -> None:
    role = "compiler" if item["id"] == "rust-compiler-notices" else "source"
    locked = noether["rust_runtime"].get(role + "_distribution", {})
    suffix = "x86_64-unknown-linux-gnu" if role == "compiler" else "src"
    url = f"https://static.rust-lang.org/dist/rustc-{noether['rust_version']}-{suffix}.tar.xz"
    if (
        locked.get("url") != url
        or item["url"] != url
        or receipt["url"] != url
        or item["sha256"] != locked.get("sha256")
        or receipt["sha256"] != locked.get("sha256")
        or type(locked.get("bytes")) is not int
        or receipt["bytes"] != locked["bytes"]
    ):
        raise ValueError("Rust source/notice artifact differs from the retained compiler pin")


def rust_notices(payload: Path, destination: Path, artifact: dict, noether: dict) -> list[dict]:
    """Retain the owned compiler input privately, conveying only three exact notices."""
    private = payload.parent / "build-materials"
    private.mkdir(mode=0o700, exist_ok=True)
    retained = private / destination.name
    if retained.exists() or retained.is_symlink():
        raise FileExistsError("retained compiler input already exists; preserve the original evidence")
    destination.rename(retained)
    artifact.update({"conveyed": False, "role": "compiler-license-input", "retained_in": str(retained.relative_to(payload.parent))})
    actual, size = hash_file(retained, maximum=MAX_ARTIFACT)
    if (actual, size) != (artifact["sha256"], artifact["bytes"]):
        raise ValueError("retained Rust compiler input differs from its verified receipt")
    base = f"rustc-{noether['rust_version']}-x86_64-unknown-linux-gnu/rustc/share/doc/rust/"
    members = {
        base + "COPYRIGHT-library.html": "COPYRIGHT-library.html",
        base + "licenses/MIT.txt": "LICENSE-MIT",
        base + "licenses/Apache-2.0.txt": "LICENSE-APACHE",
    }
    contents = {}
    with tarfile.open(retained, "r|*", tarinfo=BoundedTarInfo) as archive:
        for entry in archive:
            archive.members.clear()
            if entry.name not in members:
                continue
            name = members[entry.name]
            if name in contents or not entry.isreg() or entry.size > 4 * 1024**2:
                raise ValueError("Rust compiler notice member is duplicated, linked or oversized")
            content = archive.extractfile(entry).read()
            if hashlib.sha256(content).hexdigest() != noether["rust_runtime"]["notices"][name]:
                raise ValueError("Rust compiler notice differs from its exact pinned hash")
            contents[name] = (entry.name, content)
    if set(contents) != set(members.values()):
        raise ValueError("Rust compiler input omits a required pinned notice")
    (payload / "notices").mkdir(exist_ok=True)
    records = []
    for name, (member, content) in sorted(contents.items()):
        identity = hashlib.sha256((artifact["path"] + "\0" + member).encode()).hexdigest()
        path = "notices/" + identity + ".txt"
        write_new(payload / path, content)
        records.append(
            {
                "path": path,
                "archive": artifact["path"],
                "member": member,
                "notice_name": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "archive_sha256": artifact["sha256"],
                "role": "compiler-license-notice",
            }
        )
    return records


def notices(payload: Path, retained: list[dict] | None = None) -> list[dict]:
    """Copy only bounded regular notice members; never extract upstream paths or follow links."""
    records = list(retained or [])
    total = sum(item["bytes"] for item in records)
    directory = payload / "notices"
    directory.mkdir(exist_ok=True)
    candidates = [*sorted((payload / "upstream").iterdir()), *sorted((payload / "cargo").glob("*"))]
    candidates.extend(
        path
        for path in sorted((payload / "apt").rglob("*"))
        if path.is_file() and path.name.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz", ".tbz2", ".txz"))
    )
    if (payload / "noether-source.tar").exists():
        candidates.append(payload / "noether-source.tar")
    for path in candidates:
        with tarfile.open(path, "r|*", tarinfo=BoundedTarInfo) as archive:
            for entry in archive:
                archive.members.clear()
                basename = Path(entry.name).name.lower()
                if not entry.isreg() or not re.fullmatch(r"(?:licen[sc]e|copying|copyright|notice)(?:[._-].*)?", basename):
                    continue
                if entry.size > 4 * 1024**2 or len(records) >= 10000 or total + entry.size > 64 * 1024**2:
                    raise ValueError("source notice inventory exceeds its bounded allowance; manual collection required")
                content = archive.extractfile(entry).read()
                total += len(content)
                identity = hashlib.sha256((str(path.relative_to(payload)) + "\0" + entry.name).encode()).hexdigest()
                name = "notices/" + identity + ".txt"
                write_new(payload / name, content)
                records.append(
                    {
                        "path": name,
                        "archive": str(path.relative_to(payload)),
                        "member": entry.name,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "bytes": len(content),
                    }
                )
    write_new(directory / "inventory.json", canonical(records).encode())
    return records


def candidates(component: dict, artifacts: list[dict], payload: Path, apt: dict) -> list[str]:
    """Suggest retained files for review, never assert corresponding-source completeness."""
    kind, name, version = (component[key] for key in ("kind", "name", "version"))
    available = {item["id"]: item["path"] for item in artifacts}
    if kind == "compiler-runtime" and name == "rust-standard-library-runtime":
        path = available.get("rust-standard-library-source")
        return [path] if path else []
    if kind == "deb":
        return apt.get((component["source"], component["source_version"]), [])
    if kind == "noether-rust":
        # The local workspace crate is delivered in the pinned repository,
        # not in a crates.io archive. Registry dependencies still need their
        # individual Cargo.lock-checked source archives.
        repository = available.get("noether")
        if repository is not None and (name, version) in local_workspace_sources(payload / repository):
            return [repository]
        path = "cargo/" + name + "-" + version + ".crate"
        return [path] if (payload / path).is_file() else []
    if kind == "noether-native":
        path = available.get("noether-" + name.lower() + "-source")
        return [path] if path else []
    if "noether" in name.lower():
        return [available["noether"]] if "noether" in available else []
    if kind == "source":
        path = available.get(name + "-source")
        return [path] if path else []
    if kind == "python" or "SageMath" in name:
        # This full distribution is a source candidate, not a proven match for
        # every installed Python wheel, local patch or native dependency.
        path = available.get("sage-full-source")
        return [path] if path else []
    for prefix, identity in (("julia", "julia-full-source"), ("lean", "lean-source")):
        if name.startswith(prefix) and identity in available:
            return [available[identity]]
    return []


def notice_origins(component: dict, artifacts: list[dict]) -> list[str]:
    origins = list(component["source_candidates"])
    if component["kind"] == "compiler-runtime" and component["name"] == "rust-standard-library-runtime":
        origins.extend(item["path"] for item in artifacts if item["id"] == "rust-compiler-notices")
    return origins


def assemble(
    root: Path,
    sbom_path: Path,
    lock_path: Path,
    output: Path,
    *,
    profile: str,
    snapshot: str | list[str] | None = None,
    keyring: Path | None = None,
    noether_root: Path | None = None,
    cached_archives: Path | None = None,
    cached_crates: Path | None = None,
) -> dict:
    hash_file(sbom_path, maximum=8 * 1024**2)
    hash_file(lock_path, maximum=1024**2)
    sbom = json.loads(sbom_path.read_bytes())
    components = inventory(sbom)
    locks = json.loads(lock_path.read_bytes())
    if (
        set(locks) != {"format", "artifacts"}
        or type(locks.get("format")) is not int
        or locks["format"] != 1
        or not isinstance(locks.get("artifacts"), list)
        or not 1 <= len(locks["artifacts"]) <= 10000
    ):
        raise ValueError("choose a source artifact lock with format 1")
    seen = set()
    for item in locks["artifacts"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "profiles", "file", "url", "sha256"}
            or not isinstance(item["id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_+-]{1,200}", item["id"])
            or item["id"] in seen
            or not isinstance(item["profiles"], list)
            or not item["profiles"]
            or any(value not in ("research-cpu", "lean-mathlib") for value in item["profiles"])
            or not isinstance(item["file"], str)
            or member_name(item["file"]) != item["file"]
            or "/" in item["file"]
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
            or not isinstance(item["url"], str)
        ):
            raise ValueError("source artifact identity is invalid or duplicated")
        public_url(item["url"])
        seen.add(item["id"])
    if snapshot is not None and keyring is None or keyring is not None and snapshot is None:
        raise ValueError("OS source collection requires both snapshot and explicitly trusted archive keyring")
    revision = command(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    paths = (*SOURCE_PATHS, *(["images/lean-mathlib"] if profile == "lean-mathlib" else []))
    recipe = command(["git", "archive", revision, *paths], cwd=root)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    payload = output / "payload"
    payload.mkdir()
    write_new(payload / "recipe.tar", recipe)
    write_new(payload / "installed.spdx.json", canonical(sbom).encode())
    write_new(payload / "source-inputs.json", canonical(locks).encode())
    upstream = payload / "upstream"
    upstream.mkdir()
    artifacts, compiler_notices = [], []
    for item in locks["artifacts"]:
        if profile not in item["profiles"]:
            continue
        name = item["file"]
        if member_name(name) != name or "/" in name:
            raise ValueError("source archive filename must be a single regular filename")
        public_url(item["url"])
        destination = upstream / name
        candidate = cached_archives / name if cached_archives else None
        if candidate is not None and candidate.exists():
            actual, size = hash_file(candidate, maximum=MAX_ARTIFACT)
            if actual != item["sha256"]:
                raise ValueError("cached source archive differs from its lock")
            with candidate.open("rb") as source, destination.open("xb") as target:
                import shutil

                shutil.copyfileobj(source, target, length=1024**2)
            receipt = {"sha256": actual, "bytes": size, "url": item["url"]}
        else:
            receipt = download(item["url"], destination, item["sha256"])
        artifact = {**receipt, "path": str(destination.relative_to(payload)), "id": item["id"]}
        if item["id"] in {"rust-compiler-notices", "rust-standard-library-source"}:
            noether = rust_recipe(recipe)
            validate_rust_artifact(item, receipt, noether)
            if any(
                component["kind"] == "compiler-runtime"
                and component["name"] == "rust-standard-library-runtime"
                and component["version"] != noether["rust_version"]
                for component in components
            ):
                raise ValueError("installed Rust compiler runtime differs from the retained source pin")
            if item["id"] == "rust-compiler-notices":
                compiler_notices.extend(rust_notices(payload, destination, artifact, noether))
        artifacts.append(artifact)
    if noether_root is not None:
        pin = command(["git", "rev-parse", NOETHER_PIN], cwd=noether_root).decode().strip()
        if pin != NOETHER_PIN:
            raise ValueError("Noether source revision is not the immutable approved input")
        archive = payload / "noether-source.tar"
        write_new(archive, command(["git", "archive", NOETHER_PIN], cwd=noether_root))
        artifacts.append(
            {
                "id": "noether",
                "path": archive.name,
                "sha256": hash_file(archive)[0],
                "bytes": archive.stat().st_size,
                "source_revision": NOETHER_PIN,
            }
        )
        cargo_sources(archive, payload, cached_crates=cached_crates)
    apt, unresolved, attempts = {}, [], []
    if snapshot:
        snapshots = [snapshot] if isinstance(snapshot, str) else snapshot
        for timestamp in snapshots:
            pending = [item for item in components if item["kind"] == "deb" and (item["source"], item["source_version"]) not in apt]
            if not pending:
                break
            found, missing = apt_sources(pending, payload, snapshot=timestamp, keyring=keyring)
            apt.update(found)
            attempts.append({"snapshot": timestamp, "unresolved": missing})
        pairs = sorted({(item["source"], item["source_version"]) for item in components if item["kind"] == "deb"} - apt.keys())
        unresolved.extend(
            {
                "source": name,
                "source_version": version,
                "reason": "exact OS source was not retrieved against any supplied authenticated snapshot",
            }
            for name, version in pairs
        )
    else:
        unresolved.append({"reason": "exact installed OS sources have not been collected against authenticated indexes"})
    notice_records = notices(payload, compiler_notices)
    if noether_root is None and any("noether" in item["name"].lower() for item in components):
        unresolved.append({"reason": "Noether source archive and checksummed Cargo.lock crate sources were not supplied"})
    unresolved.extend(
        [
            {"reason": "human review must match binary native/Python components to sources, notices, patches and build/relink materials"},
            {"reason": "full installed dependency closure is not proved by this package inventory or upstream source archives"},
            {
                "reason": "Julia BinaryBuilder native builds and Lean compiler/native dependencies "
                "need exact corresponding build/source review"
            },
        ]
    )
    for item in components:
        item["source_candidates"] = candidates(item, artifacts, payload, apt)
        origins = notice_origins(item, artifacts)
        item["notice_candidates"] = [notice["path"] for notice in notice_records if notice["archive"] in origins]
        if not item["source_candidates"]:
            unresolved.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "version": item["version"],
                    "reason": "no retained exact source candidate was found for this installed component",
                }
            )
    report = {
        "format": 1,
        "profile": profile,
        "source_revision": revision,
        "artifacts": artifacts,
        "components": components,
        "notices": notice_records,
        "apt_attempts": attempts,
        "unresolved": unresolved,
        "dependency_closure_reviewed": False,
        "source_delivery_approved": False,
        "public_binary_distribution_approved": False,
    }
    write_new(payload / "collection.json", canonical(report).encode())
    files = []
    bundle = output / "sources.tar"
    with tarfile.open(bundle, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(payload.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            sha, size = hash_file(path, maximum=MAX_ARTIFACT)
            name = str(path.relative_to(payload))
            files.append({"path": name, "sha256": sha, "bytes": size})
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = size, 0o644, 0
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    sha, size = hash_file(bundle)
    draft = {
        "format": 1,
        "profile": profile,
        "reviewer": "",
        "reviewed_at": "",
        "source_revision": revision,
        "recipe_sha256": hashlib.sha256(recipe).hexdigest(),
        "image_digest": "",
        "source_bundle_sha256": sha,
        "source_bundle_bytes": size,
        "source_bundle_url": "",
        "files": files,
        "coverage": [
            {
                "id": item["id"],
                "disposition": "pending-review",
                "sources": item["source_candidates"],
                "notices": item["notice_candidates"],
                "reason": "Unsigned collection draft; source correspondence and notice obligations require review.",
            }
            for item in components
        ],
        "dependency_closure_reviewed": False,
        "source_delivery_approved": False,
        "public_binary_distribution_approved": False,
    }
    write_new(output / "source-review.draft.json", canonical(draft).encode())
    return {"bundle": str(bundle), "sha256": sha, "bytes": size, "files": len(files), "unresolved": len(unresolved), "approved": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=("research-cpu", "lean-mathlib"), required=True)
    parser.add_argument(
        "--ubuntu-snapshot", action="append", help="repeat to cover exact versions from multiple signed immutable snapshots"
    )
    parser.add_argument("--ubuntu-keyring", type=Path)
    parser.add_argument("--noether-source-root", type=Path)
    parser.add_argument("--cached-archives", type=Path)
    parser.add_argument("--cached-crates", type=Path)
    args = parser.parse_args()
    print(
        canonical(
            assemble(
                args.source_root,
                args.sbom,
                args.lock,
                args.output,
                profile=args.profile,
                snapshot=args.ubuntu_snapshot,
                keyring=args.ubuntu_keyring,
                noether_root=args.noether_source_root,
                cached_archives=args.cached_archives,
                cached_crates=args.cached_crates,
            )
        )
    )


if __name__ == "__main__":
    main()
