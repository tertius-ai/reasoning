"""Build and exercise a locked candidate using a disposable loopback registry.

No public image publication or paid compute provisioning.
Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from .image_acceptance import verify
from .image_release import PROFILES, SCHEMA_PATH, build_image, build_lock, inspect_image, json_body, write_evidence, write_new
from .server.compute_catalog import canonical

REGISTRY = "docker.io/library/registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
# Provisioning floors, not predictions or guarantees of peak disk consumption.
MINIMUM_FREE_BYTES = {"research-cpu": 24 * 1024**3, "lean-mathlib": 32 * 1024**3, "tex": 12 * 1024**3}


def disk_status(output: Path, profile: str, phase: str, *, preflight=False):
    usage = shutil.disk_usage(output)
    minimum = MINIMUM_FREE_BYTES[profile]
    print(json.dumps({"phase": phase, "free_bytes": usage.free, "total_bytes": usage.total,
                      "minimum_free_bytes": minimum}), flush=True)
    if preflight and usage.free < minimum:
        raise ValueError(
            f"{profile} image build needs at least {minimum / 1024**3:.0f} GiB free before starting; "
            f"only {usage.free / 1024**3:.1f} GiB available. Use a dedicated builder with more disk; "
            "this provisioning floor does not guarantee peak build space."
        )


def run(arguments):
    subprocess.run(arguments, check=True, timeout=5400)


def verify_candidate(root: Path, output: Path, profile="research-cpu", *, retain_archive=False,
                     noether_source_root=None, noether_source_archive=None):
    lock = build_lock(root, profile)
    disk_status(output, profile, "before-build", preflight=True)
    builder, registry = "reasoning-image-acceptance", "reasoning-image-registry"
    # A separate ephemeral runner owns these names; do not attach to existing resources.
    builder_created, registry_created = False, False
    try:
        run(
            [
                *DOCKER,
                "buildx",
                "create",
                "--name",
                builder,
                "--driver",
                "docker-container",
                "--driver-opt",
                "image=" + lock["buildkit_image"],
                "unix:///var/run/docker.sock",
            ]
        )
        builder_created = True
        options = {}
        if noether_source_root is not None:
            options["noether_source_root"] = noether_source_root
        if noether_source_archive is not None:
            options["noether_source_archive"] = noether_source_archive
        build_image(root, output / "candidate", builder, profile, **options)
        disk_status(output, profile, "after-export")
        # The exported archive and evidence are complete. Free this owned builder's
        # cache before keeping registry blobs and an extracted daemon image as well.
        run([*DOCKER, "buildx", "rm", builder])
        builder_created = False
        disk_status(output, profile, "after-builder-cleanup")
        run([*DOCKER, "run", "--detach", "--name", registry, "--publish", "127.0.0.1:5000:5000", REGISTRY])
        registry_created = True
        with httpx.Client(trust_env=False, timeout=2) as client:
            for _ in range(30):
                try:
                    if client.get("http://127.0.0.1:5000/v2/").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise ValueError("ephemeral registry did not become ready")
        archive = output / "candidate" / (profile + ".oci.tar")
        repository = "127.0.0.1:5000/reasoning/" + profile
        schema = json_body((root / SCHEMA_PATH).read_bytes())
        report, evidence = inspect_image(archive, schema, repository)
        run(
            [
                "skopeo",
                "copy",
                "--all",
                "--preserve-digests",
                "--dest-tls-verify=false",
                "oci-archive:" + str(archive),
                "docker://" + repository + ":candidate",
            ]
        )
        # The loopback registry now holds the inspected digest unchanged. This
        # disposable archive is no longer needed for the exact-digest pull/test.
        if not retain_archive:
            archive.unlink()
        reference = report["image"]["reference"]
        run([*DOCKER, "pull", reference])
        approval = output / "local-approval"
        approval.mkdir(mode=0o700)
        write_evidence(approval, report, evidence)
        catalog = approval / "compute-images.json"
        write_new(catalog, canonical([report["image"]]).encode())
        verify(catalog, output / "acceptance", profile)
    finally:
        if registry_created:
            run([*DOCKER, "container", "rm", "--force", "--volumes", registry])
        if builder_created:
            run([*DOCKER, "buildx", "rm", builder])
        disk_status(output, profile, "after-acceptance-cleanup")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="research-cpu")
    parser.add_argument("--output", type=Path, required=True, help="existing private directory outside the clean checkout")
    parser.add_argument("--retain-archive", action="store_true",
                        help="retain the private exact candidate for separately reviewed publication")
    parser.add_argument("--noether-source-root", type=Path)
    parser.add_argument("--noether-source-archive", type=Path)
    args = parser.parse_args(argv)
    verify_candidate(args.source_root.resolve(), args.output.resolve(), args.profile, retain_archive=args.retain_archive,
                     noether_source_root=args.noether_source_root, noether_source_archive=args.noether_source_archive)
    print(json.dumps({"status": "candidate execution verified", "published": False}))


if __name__ == "__main__":
    main()
