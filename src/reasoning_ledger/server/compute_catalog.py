"""Operator-approved immutable research images and bounded job resources."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

LANGUAGES = {"sage", "python", "julia", "gap", "pari", "lean"}
RESOURCE_BOUNDS = {
    "cpu": (1, 64),
    "memory_bytes": (64 * 1024**2, 256 * 1024**3),
    "disk_bytes": (8 * 1024**2, 64 * 1024**3),
    "pids": (16, 4096),
    "wall_time_seconds": (1, 86400),
    "artifact_bytes": (1, 1024**3),
}
DEFAULT_RESOURCES = {
    "cpu": 1,
    "memory_bytes": 1024**3,
    "disk_bytes": 256 * 1024**2,
    "pids": 128,
    "wall_time_seconds": 120,
    "artifact_bytes": 1024**2,
}
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
REFERENCE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?/[a-z0-9]+(?:[./_-][a-z0-9]+)*@sha256:[a-f0-9]{64}\Z")


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def resources(value: dict) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(RESOURCE_BOUNDS):
        raise ValueError("resources require CPU, memory, disk, PIDs, wall time, and artifact limits")
    if any(type(value[key]) is not int or not low <= value[key] <= high for key, (low, high) in RESOURCE_BOUNDS.items()):
        raise ValueError("resource limit is outside the supported bounds")
    if value["artifact_bytes"] > value["disk_bytes"] // 2:
        raise ValueError("artifact limit exceeds the job's output scratch allowance")
    return dict(value)


def fits(requested: dict, maximum: dict) -> bool:
    return all(requested[key] <= maximum[key] for key in RESOURCE_BOUNDS)


@dataclass(frozen=True)
class ComputeImage:
    id: str
    name: str
    reference: str
    languages: tuple[str, ...]
    source_revision: str
    capabilities_digest: str
    sbom_digest: str
    capabilities_path: str
    sbom_path: str
    maximum_resources: dict[str, int]

    @classmethod
    def parse(cls, value: dict) -> ComputeImage:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("invalid compute image fields")
        if not isinstance(value["id"], str) or not IDENTIFIER.fullmatch(value["id"]):
            raise ValueError("invalid compute image identifier")
        if not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 200:
            raise ValueError("invalid compute image name")
        if not isinstance(value["reference"], str) or not REFERENCE.fullmatch(value["reference"]):
            raise ValueError("compute images require an immutable registry digest")
        if not isinstance(value["source_revision"], str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value["source_revision"]):
            raise ValueError("invalid image source revision")
        if any(not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]) for key in ("capabilities_digest", "sbom_digest")):
            raise ValueError("image evidence requires SHA-256 digests")
        for key in ("capabilities_path", "sbom_path"):
            path = value[key]
            if not isinstance(path, str) or not re.fullmatch(r"/(?:etc|usr/share)/[A-Za-z0-9._/-]{1,180}", path) or ".." in path.split("/"):
                raise ValueError("image evidence must be a bounded /etc or /usr/share file")
        languages = value["languages"]
        if not isinstance(languages, list) or not languages or any(not isinstance(item, str) for item in languages):
            raise ValueError("invalid image languages")
        if len(languages) != len(set(languages)) or not set(languages) <= LANGUAGES:
            raise ValueError("unsupported or duplicate image languages")
        return cls(**{**value, "languages": tuple(languages), "maximum_resources": resources(value["maximum_resources"])})

    def record(self) -> dict:
        return {**asdict(self), "languages": list(self.languages)}


def _catalog(path: Path | None) -> list[tuple[ComputeImage, dict | None]]:
    if path is None:
        return []
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("compute catalog exceeds 64 KiB")
    body = json.loads(raw)
    if not isinstance(body, list) or len(body) > 32:
        raise ValueError("compute catalog must contain at most 32 approved images")
    entries, seen = [], set()
    for row in body:
        # An entry may carry the image's capabilities document beside its record. It is not part of the image's
        # identity: jobs and workers compare the record alone.
        document = row.get("capabilities") if isinstance(row, dict) else None
        image = ComputeImage.parse({key: value for key, value in row.items() if key != "capabilities"} if isinstance(row, dict) else row)
        if image.id in seen:
            raise ValueError("duplicate compute image identifier")
        seen.add(image.id)
        if isinstance(row, dict) and "capabilities" in row and (
                not isinstance(document, dict) or capabilities_digest(document) != image.capabilities_digest):
            raise ValueError("catalog capabilities do not match the image's approved capabilities digest")
        entries.append((image, document))
    return entries


def capabilities_digest(document: dict) -> str:
    """The digest of the image's own /etc/reasoning/capabilities.json bytes, which capabilities_digest pins."""
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def load_catalog(path: Path | None) -> dict[str, ComputeImage]:
    return {image.id: image for image, _ in _catalog(path)}


def load_capabilities(path: Path | None) -> dict[str, dict]:
    """Installed runtimes, tools, paths and restrictions for the entries that carry them, verified against each digest."""
    return {image.id: document for image, document in _catalog(path) if document is not None}
