"""Build and inspect public research images; never push or approve silently."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .image_context import stage_noether, validate_noether
from .server.compute_catalog import DEFAULT_RESOURCES, DIGEST, ComputeImage, canonical, fits, resources
from .server.compute_docker import COMMANDS, ENVIRONMENT, SUPERVISOR_USER, RuntimeFault, verify_evidence

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYERS = {"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip"}
TARGETS = {"etc/reasoning/capabilities.json": 1024**2, "usr/share/reasoning/sbom.spdx.json": 8 * 1024**2}
SCHEMA_PATH = "contracts/executor/v1alpha1/image-capabilities.schema.json"
SOURCE_PATHS = ("images/research-cpu", "src/reasoning_ledger/agents/noether_worker.py", "LICENSE", "NOTICE", SCHEMA_PATH)
PROFILES = {"research-cpu": "Reasoning research CPU", "lean-mathlib": "Reasoning Lean and mathlib", "tex": "Reasoning TeX"}
# Committed files that make up each profile's recipe. The TeX image does not build on the CPU image.
PROFILE_PATHS = {
    "research-cpu": SOURCE_PATHS,
    "lean-mathlib": (*SOURCE_PATHS, "images/lean-mathlib"),
    "tex": ("images/tex", "LICENSE", "NOTICE", SCHEMA_PATH),
}
# Worker languages each profile's installed entry points must provide, in COMMANDS order.
PROFILE_LANGUAGES = {"research-cpu": tuple(COMMANDS), "lean-mathlib": tuple(COMMANDS), "tex": ("python",)}
TEX_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]*")
DEB_VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~_-]*")


def profile_paths(profile: str) -> tuple[str, ...]:
    if profile not in PROFILE_PATHS:
        raise ValueError("unknown image profile")
    return PROFILE_PATHS[profile]


def digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def json_body(body: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON keys in image evidence")
            result[key] = value
        return result

    result = json.loads(body, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    if not isinstance(result, dict):
        raise ValueError("image evidence must be a JSON object")
    return result


class BoundedTarInfo(tarfile.TarInfo):
    def _proc_gnusparse_00(self, *args):
        raise ValueError("sparse image entries are unsupported")

    _proc_gnusparse_01 = _proc_gnusparse_00
    _proc_gnusparse_10 = _proc_gnusparse_00

    def _proc_member(self, archive):
        archive.image_headers = getattr(archive, "image_headers", 0) + 1
        if archive.image_headers > 1_000_000:
            raise ValueError("image archive contains too many headers")
        if self.type == tarfile.GNUTYPE_SPARSE or self.size < 0:
            raise ValueError("sparse or negative-sized image entry")
        if self.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK} and self.size > 65536:
            raise ValueError("image archive extended header exceeds 64 KiB")
        archive.image_extended_depth = getattr(archive, "image_extended_depth", 0) + 1
        try:
            if archive.image_extended_depth > 16:
                raise ValueError("image archive extended header nesting exceeds limit")
            return super()._proc_member(archive)
        finally:
            archive.image_extended_depth -= 1


class LimitedReader:
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.count = stream, maximum, 0
        self.hasher = hashlib.sha256()

    def read(self, size=-1):
        body = self.stream.read(min(1024**2 if size < 0 else size, self.maximum - self.count + 1))
        self.count += len(body)
        self.hasher.update(body)
        if self.count > self.maximum:
            raise ValueError("decompressed image layer exceeds 64 GiB")
        return body


def member_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\x00" in name or len(name) > 4096:
        raise ValueError("unsafe image archive path")
    return str(path)


def validate_inventory(inventory: dict) -> None:
    packages = inventory.get("packages")
    if (
        inventory.get("spdxVersion") != "SPDX-2.3"
        or inventory.get("SPDXID") != "SPDXRef-DOCUMENT"
        or inventory.get("dataLicense") != "CC0-1.0"
        or not isinstance(packages, list)
        or not packages
        or not isinstance(inventory.get("documentNamespace"), str)
        or not inventory["documentNamespace"].startswith("https://")
    ):
        raise ValueError("installed SBOM lacks an SPDX package inventory")
    identifiers = {"SPDXRef-DOCUMENT"}
    for package in packages:
        if (
            not isinstance(package, dict)
            or any(
                not isinstance(package.get(key), str) or not package[key]
                for key in ("SPDXID", "name", "versionInfo", "licenseDeclared", "licenseConcluded")
            )
            or not package["SPDXID"].startswith("SPDXRef-")
            or package["SPDXID"] in identifiers
            or package.get("filesAnalyzed") is not False
        ):
            raise ValueError("invalid or duplicate installed SPDX package")
        identifiers.add(package["SPDXID"])
    relationships = inventory.get("relationships")
    if not isinstance(relationships, list) or not relationships:
        raise ValueError("installed SPDX inventory lacks relationships")
    for relationship in relationships:
        if (
            not isinstance(relationship, dict)
            or relationship.get("spdxElementId") not in identifiers
            or relationship.get("relatedSpdxElement") not in identifiers
            or not isinstance(relationship.get("relationshipType"), str)
        ):
            raise ValueError("installed SPDX relationship has invalid endpoints")


class OCIArchive:
    """Read regular OCI blobs in place; never extract archive paths to disk."""

    def __init__(self, path: Path):
        if path.stat().st_size > 32 * 1024**3:
            raise ValueError("OCI archive exceeds 32 GiB")
        self.archive = tarfile.open(path, mode="r:", tarinfo=BoundedTarInfo)  # noqa: SIM115 -- owned until close(), including failed setup
        self.members = {}
        self.verified = set()
        try:
            for entry in self.archive:
                name = member_name(entry.name)
                if entry.isdir() and name in {".", "blobs", "blobs/sha256"}:
                    continue
                if (
                    not entry.isreg()
                    or entry.sparse is not None
                    or any(key.startswith("GNU.sparse") for key in entry.pax_headers)
                    or name in self.members
                    or entry.size < 0
                    or entry.size > 32 * 1024**3
                    or name not in {"index.json", "oci-layout"}
                    and not re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", name)
                    or len(self.members) >= 4096
                ):
                    raise ValueError("invalid, duplicate, or oversized OCI layout entry")
                self.members[name] = entry
            if json_body(self.read("oci-layout", 1024)) != {"imageLayoutVersion": "1.0.0"}:
                raise ValueError("unsupported OCI layout")
        except BaseException:
            self.archive.close()
            raise

    def close(self):
        self.archive.close()

    def read(self, name: str, maximum: int) -> bytes:
        member = self.members.get(name)
        if member is None or member.size > maximum:
            raise ValueError("missing or oversized OCI evidence")
        with self.archive.extractfile(member) as stream:
            body = stream.read(maximum + 1)
        if len(body) != member.size:
            raise ValueError("truncated OCI evidence")
        return body

    def blob(self, descriptor: dict) -> str:
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("digest"), str) or not DIGEST.fullmatch(descriptor["digest"]):
            raise ValueError("OCI descriptor requires a SHA256 digest")
        name = "blobs/sha256/" + descriptor["digest"][7:]
        member = self.members.get(name)
        if member is None or type(descriptor.get("size")) is not int or member.size != descriptor["size"]:
            raise ValueError("OCI descriptor size differs from blob")
        if name not in self.verified:
            hasher = hashlib.sha256()
            with self.archive.extractfile(member) as stream:
                while body := stream.read(1024**2):
                    hasher.update(body)
            if "sha256:" + hasher.hexdigest() != descriptor["digest"]:
                raise ValueError("OCI blob digest mismatch")
            self.verified.add(name)
        return name

    def document(self, descriptor: dict) -> dict:
        return json_body(self.read(self.blob(descriptor), 1024**2))

    def evidence(self, layers: list[dict], diff_ids: list[str]) -> dict[str, bytes]:
        found = {}
        remaining_bytes = 64 * 1024**3
        for layer, diff_id in zip(layers, diff_ids, strict=True):
            if layer.get("mediaType") not in LAYERS:
                raise ValueError("only distributable uncompressed/gzip OCI layers are supported")
            name = self.blob(layer)
            with self.archive.extractfile(self.members[name]) as compressed:
                stream = gzip.GzipFile(fileobj=compressed) if layer["mediaType"].endswith("+gzip") else compressed
                reader = LimitedReader(stream, remaining_bytes)
                with tarfile.open(fileobj=reader, mode="r|", tarinfo=BoundedTarInfo) as entries:
                    whiteouts, changed = [], {}
                    seen = set()
                    for member in entries:
                        entries.members.clear()  # Do not retain a million upstream filesystem headers.
                        path = member_name(member.name)
                        if member.sparse is not None or any(key.startswith("GNU.sparse") for key in member.pax_headers) or member.size < 0:
                            raise ValueError("sparse or negative-sized layer entry")
                        parents = {str(parent) for target in TARGETS for parent in PurePosixPath(target).parents}
                        if path in parents and not member.isdir():
                            raise ValueError("image evidence ancestor is not a directory")
                        filename = PurePosixPath(path).name
                        if filename.startswith(".wh."):
                            if not member.isreg() or member.size != 0:
                                raise ValueError("invalid OCI whiteout")
                            parent = str(PurePosixPath(path).parent)
                            removed = parent if filename == ".wh..wh..opq" else str(PurePosixPath(path).parent / filename[4:])
                            whiteouts.append(removed)
                        elif path in TARGETS:
                            if path in seen or not member.isreg() or member.size > TARGETS[path]:
                                raise ValueError("invalid or duplicate installed evidence file")
                            seen.add(path)
                            with entries.extractfile(member) as payload:
                                changed[path] = payload.read(TARGETS[path] + 1)
                            if len(changed[path]) != member.size:
                                raise ValueError("truncated installed evidence")
                    # Whiteouts affect lower layers, regardless of member ordering.
                    for removed in whiteouts:
                        for target in list(found):
                            if removed == "." or target == removed or target.startswith(removed + "/"):
                                del found[target]
                    found.update(changed)
                while reader.read(1024**2):
                    pass
                remaining_bytes -= reader.count
                if stream is not compressed:
                    stream.close()
                if "sha256:" + reader.hasher.hexdigest() != diff_id:
                    raise ValueError("OCI uncompressed layer digest differs from config rootfs")
        if set(found) != set(TARGETS):
            raise ValueError("image lacks current installed capabilities or SBOM")
        return found


def inspect_image(path: Path, schema: dict, repository: str) -> tuple[dict, dict[str, bytes]]:
    archive = OCIArchive(path)
    try:
        index = json_body(archive.read("index.json", 1024**2))
        if index.get("schemaVersion") != 2 or not isinstance(index.get("manifests"), list) or len(index["manifests"]) != 1:
            raise ValueError("OCI layout must name exactly one release descriptor")
        release = index["manifests"][0]
        runtime = release
        if release.get("mediaType") == INDEX:
            image_index = archive.document(release)
            if image_index.get("schemaVersion") != 2 or len(image_index.get("manifests", [])) != 1:
                raise ValueError("release must contain exactly one platform, without unverified attachments")
            runtime = image_index["manifests"][0]
        if runtime.get("mediaType") != MANIFEST:
            raise ValueError("release must contain an OCI runtime manifest")
        if runtime.get("platform", {"os": "linux", "architecture": "amd64"}) != {"os": "linux", "architecture": "amd64"}:
            raise ValueError("release platform differs from linux/amd64")
        manifest = archive.document(runtime)
        if (
            manifest.get("schemaVersion") != 2
            or manifest.get("artifactType")
            or manifest.get("subject")
            or manifest.get("config", {}).get("mediaType") != CONFIG
        ):
            raise ValueError("not a runtime image manifest")
        config = archive.document(manifest["config"])
        layers = manifest.get("layers")
        if not isinstance(layers, list) or not 1 <= len(layers) <= 256:
            raise ValueError("image must contain 1–256 layers")
        diff_ids = config.get("rootfs", {}).get("diff_ids")
        if (
            config.get("rootfs", {}).get("type") != "layers"
            or not isinstance(diff_ids, list)
            or len(diff_ids) != len(layers)
            or any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in diff_ids)
        ):
            raise ValueError("image config lacks exact uncompressed layer identities")
        evidence = archive.evidence(layers, diff_ids)
        caps = evidence["etc/reasoning/capabilities.json"]
        sbom = evidence["usr/share/reasoning/sbom.spdx.json"]
        capabilities, inventory = json_body(caps), json_body(sbom)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(capabilities)
        # jsonschema's date-time checker has optional dependencies; never silently
        # omit this profile's UTC timestamp check when those are absent.
        timestamp = capabilities["build_timestamp"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", timestamp):
            raise ValueError("installed build timestamp must be an RFC3339 UTC instant")
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        settings = config.get("config", {})
        revision = capabilities["source_revision"]
        required_env = {f"{key}={value}" for key, value in ENVIRONMENT.items()}
        if (
            config.get("os") != "linux"
            or config.get("architecture") != "amd64"
            or settings.get("User") != SUPERVISOR_USER
            or settings.get("Entrypoint") not in (None, [])
            or settings.get("Cmd") != ["/bin/sleep", "60"]
            or settings.get("WorkingDir") != "/"
            or settings.get("Volumes")
            or not required_env <= set(settings.get("Env") or [])
            or settings.get("Labels", {}).get("org.opencontainers.image.revision") != revision
            or settings.get("Labels", {}).get("org.reasoning.executor.contract") != "1.0.0-alpha.1"
        ):
            raise ValueError("image config differs from the public worker profile")
        validate_inventory(inventory)
        profile = capabilities["image_name"].removeprefix("reasoning/")
        if profile not in PROFILES:
            raise ValueError("unknown installed research image profile")
        entry_points = {entry for tool in capabilities["tools"] for entry in tool.get("entry_points", [])}
        languages = [language for language, (_, argv) in COMMANDS.items() if argv[0] in entry_points]
        if tuple(languages) != PROFILE_LANGUAGES[profile]:
            raise ValueError("installed entry points differ from the profile's worker languages")
        image = ComputeImage.parse(
            {
                "id": profile,
                "name": PROFILES[profile],
                "reference": repository + "@" + release["digest"],
                "languages": languages,
                "source_revision": revision,
                "capabilities_digest": digest(caps),
                "sbom_digest": digest(sbom),
                "capabilities_path": "/etc/reasoning/capabilities.json",
                "sbom_path": "/usr/share/reasoning/sbom.spdx.json",
                "maximum_resources": resources(capabilities["resource_limits"]["maximums"]),
            }
        )
        for language in image.languages:
            verify_evidence(caps, sbom, image, DEFAULT_RESOURCES, language)
        report = {
            "format": 1,
            "image": image.record(),
            "platform_manifest_digest": runtime["digest"],
            "config_digest": manifest["config"]["digest"],
            "layer_digests": [layer["digest"] for layer in layers],
            "verification": "archive-integrity-and-installed-metadata-only",
            "execution_verified": False,
            "license_source_audit_verified": False,
            "signature_verified": False,
        }
        return report, evidence
    finally:
        archive.close()


def command(argv: list[str], *, cwd: Path | None = None) -> bytes:
    return subprocess.run(argv, cwd=cwd, capture_output=True, check=True, timeout=60).stdout


def validate_recipe(root: Path) -> dict:
    lock = json_body((root / "images/research-cpu/inputs.json").read_bytes())
    recipe = (root / "images/research-cpu/Dockerfile").read_text()
    resources(lock["maximum_resources"])
    if not fits(DEFAULT_RESOURCES, lock["maximum_resources"]) or lock["platform"] != "linux/amd64":
        raise ValueError("recipe must support the public worker defaults/platform")
    if recipe.splitlines()[0] != "# syntax=" + lock["frontend"] or "FROM " + lock["base"] + "\n" not in recipe:
        raise ValueError("recipe frontend/base differs from its lock")
    for artifact in lock["downloads"].values():
        if not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"]) or not artifact["url"].startswith("https://"):
            raise ValueError("downloads need immutable SHA256 and HTTPS")
        expected_add = "ADD --checksum=sha256:" + artifact["sha256"] + " \\\n" + "    " + artifact["url"] + " "
        if expected_add not in recipe:
            raise ValueError("recipe download differs from its lock")
    if set(lock["versions"]) != set(COMMANDS) | {"noether"}:
        raise ValueError("locked runtime versions must cover the worker languages")
    noether = validate_noether(lock)
    for field, stage in (("rust_image", "rust-toolchain"), ("manylinux_image", "noether-wheel")):
        if not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}", noether.get(field, "")) or (
                "FROM " + noether[field] + " AS " + stage + "\n") not in recipe:
            raise ValueError("Noether build image differs from its immutable lock")
    uv = noether["uv"]
    if (not re.fullmatch(r"[a-f0-9]{64}", uv["sha256"]) or not uv["url"].startswith("https://")
            or ("ADD --checksum=sha256:" + uv["sha256"] + " \\\n" + "    " + uv["url"] + " ") not in recipe
            or noether["commit"] not in recipe or noether["version"] not in recipe
            or noether["rust_version"] not in recipe or "COPY --from=noether-source . /work/noether" not in recipe):
        raise ValueError("Noether source/toolchain/download differs from its lock")
    runtime = noether.get("rust_runtime", {})
    directory = "/usr/share/reasoning/licenses/rust-" + noether["rust_version"]
    notices = runtime.get("notices", {})
    if (runtime.get("notice_directory") != directory
            or set(notices) != {"COPYRIGHT-library.html", "LICENSE-MIT", "LICENSE-APACHE"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in notices.values())):
        raise ValueError("Rust runtime notices need exact release names and SHA256 pins")
    prefix = "/usr/local/rustup/toolchains/" + noether["rust_version"] + "-x86_64-unknown-linux-gnu/share/doc/rust/"
    copies = (
        ("COPYRIGHT-library.html", "COPYRIGHT-library.html"),
        ("licenses/MIT.txt", "LICENSE-MIT"), ("licenses/Apache-2.0.txt", "LICENSE-APACHE"),
    )
    for source, target in copies:
        if "COPY --from=rust-toolchain " + prefix + source + " " + directory + "/" + target + "\n" not in recipe:
            raise ValueError("Rust runtime notice copy differs from its compiler lock")
    for field, suffix in (("compiler_distribution", "-x86_64-unknown-linux-gnu.tar.xz"), ("source_distribution", "-src.tar.xz")):
        artifact = runtime.get(field, {})
        if (artifact.get("url") != "https://static.rust-lang.org/dist/rustc-" + noether["rust_version"] + suffix
                or not isinstance(artifact.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"])
                or type(artifact.get("bytes")) is not int or not 0 < artifact["bytes"] <= 512 * 1024**2):
            raise ValueError("Rust runtime notice/source inputs need bounded official release pins")
    return lock


def validate_mathlib(root: Path, cpu_lock: dict) -> dict:
    lock = json_body((root / "images/lean-mathlib/inputs.json").read_bytes())
    fragment = (root / "images/lean-mathlib/Containerfile.fragment").read_text()
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", lock.get("ubuntu_snapshot", "")) or set(lock.get("build_packages", {})) != {
        "git",
        "curl",
        "ca-certificates",
    }:
        raise ValueError("mathlib build tools need an immutable Ubuntu snapshot and exact package versions")
    if any(not re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", version) for version in lock["build_packages"].values()):
        raise ValueError("invalid locked mathlib build package version")
    ca_url = (
        "https://snapshot.ubuntu.com/ubuntu/"
        + lock["ubuntu_snapshot"]
        + "/pool/main/c/ca-certificates/ca-certificates_"
        + lock["build_packages"]["ca-certificates"]
        + "_all.deb"
    )
    if lock.get("ca_bootstrap_url") != ca_url or not re.fullmatch(r"[a-f0-9]{64}", lock.get("ca_bootstrap_sha256", "")):
        raise ValueError("CA bootstrap must match the locked snapshot and certificate package")
    ca_add = "ADD --checksum=sha256:" + lock["ca_bootstrap_sha256"] + " \\\n" + "    " + ca_url + " "
    if ca_add not in fragment:
        raise ValueError("recipe CA bootstrap differs from its lock")
    if lock["lean_version"] != cpu_lock["versions"]["lean"] or not re.fullmatch(r"[a-f0-9]{40}", lock["mathlib_revision"]):
        raise ValueError("mathlib needs the locked compatible Lean toolchain")
    if not re.fullmatch(r"[a-f0-9]{64}", lock["mathlib_sha256"]) or not lock["mathlib_url"].endswith(lock["mathlib_revision"]):
        raise ValueError("mathlib needs immutable source identity")
    expected = "ADD --checksum=sha256:" + lock["mathlib_sha256"] + " \\\n" + "    " + lock["mathlib_url"] + " "
    if (
        expected not in fragment
        or not lock["dependencies"]
        or any(not re.fullmatch(r"[a-f0-9]{40}", version) for version in lock["dependencies"].values())
    ):
        raise ValueError("mathlib source or dependency lock differs from the recipe")
    return lock


def validate_tex(root: Path) -> dict:
    """The TeX recipe installs exact Ubuntu packages from one signed snapshot and nothing else."""
    lock = json_body((root / "images/tex/inputs.json").read_bytes())
    recipe = (root / "images/tex/Dockerfile").read_text()
    resources(lock["maximum_resources"])
    if not fits(DEFAULT_RESOURCES, lock["maximum_resources"]) or lock["platform"] != "linux/amd64":
        raise ValueError("recipe must support the public worker defaults/platform")
    if any(not isinstance(lock.get(key), str) or not lock[key] for key in ("buildx_version", "buildkit_version", "buildkit_image")):
        raise ValueError("TeX recipe needs locked build tools")
    if not re.fullmatch(r"docker\.io/library/ubuntu:jammy-[0-9.]+@sha256:[a-f0-9]{64}", lock.get("base", "")):
        raise ValueError("TeX recipe needs an immutable Ubuntu 22.04 base image")
    if recipe.splitlines()[0] != "# syntax=" + lock["frontend"] or "FROM " + lock["base"] + "\n" not in recipe:
        raise ValueError("recipe frontend/base differs from its lock")
    if recipe.count("FROM ") != 1 or recipe.count("\nADD ") != 1:
        raise ValueError("TeX recipe must have one stage and one locked download")
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", lock.get("ubuntu_snapshot", "")):
        raise ValueError("TeX packages need an immutable Ubuntu snapshot")
    mirror = "https://snapshot.ubuntu.com/ubuntu/" + lock["ubuntu_snapshot"]
    if "printf 'deb " + mirror + " %s main universe\\n' jammy jammy-updates jammy-security" not in recipe:
        raise ValueError("recipe package sources differ from the locked snapshot")
    ca_url = lock.get("ca_bootstrap_url", "")
    if (not re.fullmatch(re.escape(mirror + "/pool/main/c/ca-certificates/ca-certificates_") + r"[0-9][A-Za-z0-9.+~]*_all\.deb", ca_url)
            or not re.fullmatch(r"[a-f0-9]{64}", lock.get("ca_bootstrap_sha256", ""))
            or "ADD --checksum=sha256:" + lock["ca_bootstrap_sha256"] + " \\\n    " + ca_url + " " not in recipe):
        raise ValueError("recipe certificate bootstrap differs from its lock")
    packages = lock.get("packages")
    if (not isinstance(packages, dict) or not packages
            or any(not TEX_PACKAGE.fullmatch(name) or not isinstance(version, str) or not DEB_VERSION.fullmatch(version)
                   for name, version in packages.items())):
        raise ValueError("TeX packages need exact Debian names and versions")
    if not {"python3", "texlive-binaries", "texlive-latex-base", "latexmk"} <= set(packages):
        raise ValueError("TeX recipe must install Python, pdfTeX, LaTeX and latexmk")
    block = ("--no-install-recommends \\\n" + "".join(f"         {name}={version} \\\n" for name, version in sorted(packages.items()))
             + '    && apt-get "$@" clean')
    if recipe.count('apt-get "$@" install') != 1 or block not in recipe:
        raise ValueError("recipe package versions differ from the lock")
    return lock


def build_lock(root: Path, profile: str) -> dict:
    """The lock whose build tools and platform bind this profile's candidate."""
    if profile not in PROFILES:
        raise ValueError("unknown image profile")
    return validate_tex(root) if profile == "tex" else validate_recipe(root)


def build_image(root: Path, output: Path, builder: str, profile="research-cpu", *,
                noether_source_root=None, noether_source_archive=None) -> None:
    root = root.resolve()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", builder):
        raise ValueError("select a named local Buildx builder")
    lock = build_lock(root, profile)
    if profile == "tex" and (noether_source_root is not None or noether_source_archive is not None):
        raise ValueError("the TeX image has no Noether input")
    mathlib = validate_mathlib(root, lock) if profile == "lean-mathlib" else None
    if command(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root):
        raise ValueError("image builds require a clean committed source tree")
    revision = command(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision):
        raise ValueError("image source must have a complete committed Git object identity")
    epoch = int(command(["git", "show", "-s", "--format=%ct", revision], cwd=root))
    timestamp = datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")
    docker = ["docker", "--host", "unix:///var/run/docker.sock"]
    if command([*docker, "buildx", "version"]).decode().strip() != lock["buildx_version"]:
        raise ValueError("Buildx does not match the locked version")
    details = command([*docker, "buildx", "inspect", builder, "--bootstrap"]).decode()
    builder_container = verify_builder(details, builder, lock)
    selected_image = command([*docker, "inspect", "--format", "{{.Config.Image}}", builder_container]).decode().strip()
    if selected_image.removeprefix("docker.io/") != lock["buildkit_image"].removeprefix("docker.io/"):
        raise ValueError("builder container does not use the locked immutable BuildKit image")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="reasoning-image-context-") as temporary:
        context = Path(temporary)
        # Materialize only explicitly selected committed files, never credentials
        # or a complete checkout. Git archive itself runs without a shell.
        with tempfile.TemporaryFile() as source:
            subprocess.run(["git", "archive", revision, *profile_paths(profile)], cwd=root, stdout=source, check=True, timeout=60)
            source.seek(0)
            with tarfile.open(fileobj=source, mode="r:") as archive:
                for entry in archive:
                    name = member_name(entry.name)
                    if entry.isdir():
                        (context / name).mkdir(parents=True, exist_ok=True)
                    elif entry.isreg():
                        path = context / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(archive.extractfile(entry).read())
                    else:
                        raise ValueError("committed image build inputs must be regular files/directories")
        if build_lock(context, profile) != lock:
            raise ValueError("committed image inputs changed during build preparation")
        contexts = []
        if profile == "tex":
            recipe = context / "images/tex/Dockerfile"
        else:
            noether_context = stage_noether(lock["noether"], context / "noether-source",
                                           source_root=noether_source_root, source_archive=noether_source_archive)
            contexts = ["--build-context", "noether-source=" + str(noether_context)]
            recipe = context / "images/research-cpu/Dockerfile"
        if mathlib:
            if validate_mathlib(context, lock) != mathlib:
                raise ValueError("committed mathlib lock changed during build preparation")
            recipe = context / "images/lean-mathlib/Dockerfile"
            recipe.write_bytes(
                (context / "images/research-cpu/Dockerfile").read_bytes()
                + b"\n"
                + (context / "images/lean-mathlib/Containerfile.fragment").read_bytes()
            )
        schema = json_body((context / SCHEMA_PATH).read_bytes())
        subprocess.run(
            [
                *docker,
                "buildx",
                "build",
                "--builder",
                builder,
                "--platform",
                lock["platform"],
                *contexts,
                "--file",
                str(recipe),
                "--build-arg",
                "SOURCE_REVISION=" + revision,
                "--build-arg",
                "SOURCE_DATE_EPOCH=" + str(epoch),
                "--build-arg",
                "BUILD_TIMESTAMP=" + timestamp,
                "--no-cache",
                "--provenance=false",
                "--sbom=false",
                "--metadata-file",
                str(output / "buildx-metadata.json"),
                "--output",
                f"type=oci,dest={output / (profile + '.oci.tar')},compression=gzip,rewrite-timestamp=true,oci-mediatypes=true",
                str(context),
            ],
            check=True,
        )
    # This is a local candidate archive, not a signed or published image.
    report, evidence = inspect_image(output / (profile + ".oci.tar"), schema, "localhost/reasoning/" + profile)
    build_metadata = json_body((output / "buildx-metadata.json").read_bytes())
    if (
        build_metadata.get("containerimage.digest") != report["image"]["reference"].split("@", 1)[1]
        or build_metadata.get("containerimage.config.digest") != report["config_digest"]
    ):
        raise ValueError("Buildx output digest metadata differs from the inspected OCI archive")
    report["build_tools"] = {key: lock[key] for key in ("buildx_version", "buildkit_version", "buildkit_image")}
    report["source_date_epoch"] = epoch
    write_evidence(output, report, evidence)


def write_new(path: Path, body: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(body)


def verify_builder(text: str, name: str, lock: dict) -> str:
    """Buildx v0.32.1 inspect has text output, not a --format option."""
    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields.setdefault(key.strip(), []).append(value.strip())
    if (
        fields.get("Name") != [name, name + "0"]
        or fields.get("Driver") != ["docker-container"]
        or fields.get("Endpoint") != ["unix:///var/run/docker.sock"]
        or fields.get("Status") != ["running"]
        or fields.get("BuildKit version") != [lock["buildkit_version"]]
        or "Error" in fields
    ):
        raise ValueError("builder must have one running locked local docker-container BuildKit node")
    return "buildx_buildkit_" + name + "0"


def write_evidence(output: Path, report: dict, evidence: dict[str, bytes]) -> None:
    for name, body in evidence.items():
        write_new(output / Path(name).name, body)
    write_new(output / "image-evidence.json", (canonical(report) + "\n").encode())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build a local candidate from a clean committed public source tree")
    build.add_argument("--source-root", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--builder", required=True)
    build.add_argument("--profile", choices=tuple(PROFILES), default="research-cpu")
    build.add_argument("--noether-source-root", type=Path, help="Git repository containing the exact pinned Noether object")
    build.add_argument("--noether-source-archive", type=Path, help="exact SHA256/byte-locked credential-free Noether source archive")
    inspect = commands.add_parser("inspect", help="verify archive bytes and embedded evidence, without execution or signature claims")
    inspect.add_argument("--archive", required=True, type=Path)
    inspect.add_argument("--schema", required=True, type=Path)
    inspect.add_argument("--repository", required=True)
    inspect.add_argument("--output", required=True, type=Path)
    inspect.add_argument(
        "--write-approval-catalog", action="store_true", help="explicitly approve the inspected digest; not proof of publication/execution"
    )
    compare = commands.add_parser("compare", help="compare two independently built candidates, including manifest/config/layer digests")
    compare.add_argument("first", type=Path)
    compare.add_argument("second", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            build_image(args.source_root, args.output.absolute(), args.builder, args.profile,
                        noether_source_root=args.noether_source_root, noether_source_archive=args.noether_source_archive)
        elif args.command == "inspect":
            report, evidence = inspect_image(args.archive, json_body(args.schema.read_bytes()), args.repository)
            args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
            write_evidence(args.output, report, evidence)
            if args.write_approval_catalog:
                write_new(args.output / "compute-images.json", (canonical([report["image"]]) + "\n").encode())
        else:
            first, second = json_body(args.first.read_bytes()), json_body(args.second.read_bytes())
            keys = ("image", "platform_manifest_digest", "config_digest", "layer_digests", "build_tools", "source_date_epoch")
            for report in (first, second):
                ComputeImage.parse(report["image"])
                if not all(key in report for key in keys) or not report["layer_digests"]:
                    raise ValueError("compare requires complete original build evidence")
            if any(first.get(key) != second.get(key) for key in keys):
                raise ValueError("repeat builds differ in immutable image/evidence/build identity")
            print("Repeat candidate image/evidence/config/layer identity matches; execution and license/source audit are separate.")
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, tarfile.TarError, ValidationError, RuntimeFault) as exc:
        parser.exit(1, f"image release failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
