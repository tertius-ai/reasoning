"""Lean environment keys: the exact toolchain and pinned packages that a Lean project builds against.

A project's `lean-toolchain` and `lake-manifest.json` name everything its dependencies need. The specification keeps
only what decides the built environment: the toolchain, and each package's name, git URL, revision and sub-directory.
Its SHA-256 over canonical JSON is the environment key: equal keys mean the same environment. Only official Lean
releases and release candidates, and git packages on github.com pinned to full commit hashes, are accepted.
"""

from __future__ import annotations

import hashlib
import json
import re

from .compute_catalog import canonical

FORMAT = 1
TOOLCHAIN_BYTES = 128
# A manifest entry is about 400 bytes; 32 KiB holds MAX_PACKAGES of them. JSON-escaped in a request, an ordinary manifest
# stays under the server's 64 KiB body limit.
MANIFEST_BYTES = 32 * 1024
MAX_PACKAGES = 64
# Lake has written these manifest versions with the same flat package entries.
MANIFEST_VERSIONS = (7, "1.0.0", "1.1.0")
TOOLCHAIN_REPOSITORY = "leanprover/lean4"
TOOLCHAIN = re.compile(r"leanprover/lean4:(v4\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})(?:-rc[1-9][0-9]{0,2})?)\Z")
PACKAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
# Kept verbatim, not normalized: Lake compares each package's manifest URL with its clone's remote, so `.../r` and
# `.../r.git` are different environments to it.
PACKAGE_URL = re.compile(r"https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
PATH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
KEY = re.compile(r"[0-9a-f]{64}\Z")
PACKAGE_FIELDS = ("name", "url", "rev", "sub_dir")


class SpecificationError(ValueError):
    """The toolchain or manifest names something an environment cannot be prepared from."""


def toolchain(text: object) -> str:
    """The exact toolchain a `lean-toolchain` file names: an official Lean 4 release or release candidate."""
    if not isinstance(text, str) or len(text.encode()) > TOOLCHAIN_BYTES:
        raise SpecificationError(f"lean-toolchain must be text of at most {TOOLCHAIN_BYTES} bytes")
    name = text.strip()
    if TOOLCHAIN.fullmatch(name) is None:
        raise SpecificationError(
            "lean-toolchain must name an official Lean release or release candidate, such as "
            "leanprover/lean4:v4.24.0 or leanprover/lean4:v4.34.0-rc1"
        )
    return name


def _sub_dir(value: object, name: str) -> str | None:
    if value is None:
        return None
    parts = value.split("/") if isinstance(value, str) and len(value) <= 256 else []
    if not parts or len(parts) > 8 or any(PATH_PART.fullmatch(part) is None or part in {".", ".."} for part in parts):
        raise SpecificationError(f"package {name} has an unsafe subDir; use a relative path of plain names")
    return value


def _package(entry: object) -> dict:
    if not isinstance(entry, dict):
        raise SpecificationError("each lake-manifest.json package must be an object")
    name = entry.get("name")
    if not isinstance(name, str) or PACKAGE_NAME.fullmatch(name) is None:
        raise SpecificationError("each package needs a plain name: a letter, then letters, digits, _ or -")
    if entry.get("type") != "git":
        raise SpecificationError(f"package {name} is not a git package; only git packages pinned to a commit can be fetched")
    url = entry.get("url")
    if not isinstance(url, str) or PACKAGE_URL.fullmatch(url) is None:
        raise SpecificationError(f"package {name} must come from an https://github.com/<owner>/<repository> URL")
    revision = entry.get("rev")
    if not isinstance(revision, str) or REVISION.fullmatch(revision) is None:
        raise SpecificationError(f"package {name} must be pinned to a full 40-character lowercase commit hash")
    return {"name": name, "url": url, "rev": revision, "sub_dir": _sub_dir(entry.get("subDir"), name)}


def _packages(entries: object) -> list[dict]:
    if not isinstance(entries, list) or len(entries) > MAX_PACKAGES:
        raise SpecificationError(f"lake-manifest.json must have a packages list of at most {MAX_PACKAGES} entries")
    packages = [_package(entry) for entry in entries]
    if len({package["name"] for package in packages}) != len(packages):
        raise SpecificationError("lake-manifest.json names a package more than once")
    return sorted(packages, key=lambda package: package["name"])


def packages(manifest: object) -> list[dict]:
    """Every package a `lake-manifest.json` text pins, sorted by name."""
    if not isinstance(manifest, str) or len(manifest.encode()) > MANIFEST_BYTES:
        raise SpecificationError(f"lake-manifest.json must be text of at most {MANIFEST_BYTES // 1024} KiB")
    try:
        value = json.loads(manifest, parse_constant=_constant, object_pairs_hook=_object)
    except (ValueError, RecursionError) as exc:
        raise SpecificationError("lake-manifest.json is not valid JSON, or repeats a field within an object") from exc
    if not isinstance(value, dict) or not any(value.get("version") == version and type(value.get("version")) is type(version)
                                              for version in MANIFEST_VERSIONS):
        raise SpecificationError("lake-manifest.json must be a Lake manifest of version 7, 1.0.0 or 1.1.0")
    return _packages(value.get("packages"))


def _constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def _object(pairs: list[tuple[str, object]]) -> dict:
    # A repeated field could pin one revision for us and another for Lake.
    if len({name for name, _ in pairs}) != len(pairs):
        raise ValueError("repeated field")
    return dict(pairs)


def specification(lean_toolchain: object, lake_manifest: object) -> dict:
    """The environment a project's two files describe, in the canonical form its key hashes."""
    return {"format": FORMAT, "toolchain": toolchain(lean_toolchain), "packages": packages(lake_manifest)}


def validate(value: object) -> dict:
    """A stored or transported specification, accepted only in exactly the canonical form `specification` returns."""
    if not isinstance(value, dict) or set(value) != {"format", "toolchain", "packages"} or value["format"] != FORMAT \
            or type(value["format"]) is not int:
        raise SpecificationError("not a Lean environment specification")
    entries = value["packages"]
    if not isinstance(entries, list) or any(not isinstance(entry, dict) or set(entry) != set(PACKAGE_FIELDS) for entry in entries):
        raise SpecificationError("not a Lean environment specification")
    if TOOLCHAIN.fullmatch(value["toolchain"] if isinstance(value["toolchain"], str) else "") is None:
        raise SpecificationError("not a Lean environment specification")
    packages = _packages([{"name": entry["name"], "type": "git", "url": entry["url"], "rev": entry["rev"],
                           "subDir": entry["sub_dir"]} for entry in entries])
    if packages != entries:
        raise SpecificationError("Lean environment packages must be sorted by name")
    return {"format": FORMAT, "toolchain": value["toolchain"], "packages": packages}


def key(value: dict) -> str:
    """The environment key: SHA-256 of the canonical specification."""
    return hashlib.sha256(canonical(validate(value)).encode()).hexdigest()


def sources(value: dict) -> list[dict]:
    """Everything preparing the environment fetches, by URL and exact revision, in the order an approval lists them.
    `value` is a specification from `specification` or `validate`. A commit hash names the commit, not the repository it
    is on: GitHub serves a fork's commits under the parent repository's URL too."""
    version = TOOLCHAIN.fullmatch(value["toolchain"])[1]
    return [
        {"kind": "toolchain", "name": "Lean", "url": f"https://github.com/{TOOLCHAIN_REPOSITORY}/releases/tag/{version}",
         "revision": version, "sub_dir": None},
        *({"kind": "package", "name": package["name"], "url": package["url"], "revision": package["rev"],
           "sub_dir": package["sub_dir"]} for package in value["packages"]),
    ]
