"""Real loopback HTTP acceptance-command wiring with explicitly fake CAS output.

These fixtures prove lifecycle/protocol/receipt wiring, NOT image execution.
"""

import ast
import json
import threading
import time

import pytest

from reasoning_ledger.image_acceptance import (
    DISK,
    ISOLATION,
    MEMORY,
    NOETHER_PROGRAMS,
    PIDS,
    PROGRAMS,
    TEX_PROGRAMS,
    acceptance_limits,
    main,
    verify,
)
from reasoning_ledger.image_release import digest
from reasoning_ledger.server.compute_archive import CollectedArtifact
from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES
from reasoning_ledger.server.compute_docker import Outcome, RuntimeFault
from reasoning_ledger.server.security import private_file

IMAGE = {
    "id": "research-cpu",
    "name": "NONEXECUTING HTTP wiring fixture",
    "reference": "registry.example.invalid/cpu@sha256:" + "a" * 64,
    "languages": list(PROGRAMS),
    "source_revision": "b" * 40,
    "capabilities_digest": "sha256:" + "c" * 64,
    "sbom_digest": "sha256:" + "d" * 64,
    "capabilities_path": "/etc/reasoning/capabilities.json",
    "sbom_path": "/usr/share/reasoning/sbom.spdx.json",
    "maximum_resources": DEFAULT_RESOURCES,
}


def test_mathlib_acceptance_enrolls_memory_for_kernel_audit_without_changing_cpu_defaults():
    assert acceptance_limits("research-cpu") == DEFAULT_RESOURCES
    assert acceptance_limits("lean-mathlib") == {**DEFAULT_RESOURCES, "memory_bytes": 4 * 1024**3}
    assert DEFAULT_RESOURCES["memory_bytes"] == 1024**3


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "images.json"
    path.write_text(json.dumps([IMAGE]))
    return path


class NonexecutingRunner:
    instances = []
    corrupt_artifact = False
    cleanup_failure = False

    def __init__(self, directory, worker_id):
        self.jobs, self.cleanups, self.preflights = [], 0, []
        self.instances.append(self)

    def verify_runtime_identity(self):
        pass

    def preflight(self, image, limits):
        self.preflights.append((image.id, limits))

    def cleanup(self):
        self.cleanups += 1
        if self.cleanup_failure:
            raise RuntimeFault("fixture_cleanup_failure")

    def verify_cleanup(self):
        if self.cleanup_failure and len(self.jobs) == 16:
            raise RuntimeFault("fixture_cleanup_failure")

    def run(self, job, spool, log, stopped):
        source = job["parameters"]["program"]
        self.jobs.append(job)
        name = next((language for language, text in PROGRAMS.items() if source == text), None)
        if name is None:
            name = next((case for case, text in NOETHER_PROGRAMS.items() if source == text), None)
        if name is None:
            name = next((case for case, text in TEX_PROGRAMS.items() if source == text), None)
        if source == ISOLATION:
            name = "isolation"
        elif source == DISK:
            name = "disk"
        elif source == PIDS:
            name = "pids"
        elif source == MEMORY:
            log(b"REASONING_MEMORY_PRESSURE\n")
            return Outcome("failed", 1000, 137, "resource_limit", ())
        elif job["parameters"]["resources"]["wall_time_seconds"] == 1:
            return Outcome("failed", 1000, None, "wall_time_limit", ())
        elif "time.sleep(90)" in source:
            deadline = time.monotonic() + 25
            while not stopped():
                if time.monotonic() >= deadline:
                    raise AssertionError("HTTP cancellation did not reach fixture runner")
                time.sleep(0.02)
            return Outcome("cancelled", 10000, None, None, ())
        assert name is not None
        log(("REASONING_" + name.upper() + "_OK\n").encode())
        artifacts = ()
        if name == "python":
            body = b"corrupt" if self.corrupt_artifact else b'{"determinant":-2}\n'
            path = spool / "0"
            private_file(path, body)
            artifacts = (CollectedArtifact("result.json", path, len(body), digest(body)),)
        if name == "tex_pdf":
            body = b"corrupt" if self.corrupt_artifact else b"%PDF-1.5\nNONEXECUTING fixture\n"
            path = spool / "0"
            private_file(path, body)
            artifacts = (CollectedArtifact("main.pdf", path, len(body), digest(body)),)
        return Outcome("succeeded", 1000, 0, None, artifacts)


def test_actual_http_command_wiring_exercises_every_case_and_stops_owned_server(monkeypatch, catalog, tmp_path):
    NonexecutingRunner.instances = []
    monkeypatch.setattr("reasoning_ledger.image_acceptance.DockerRunner", NonexecutingRunner)
    report = verify(catalog, tmp_path / "acceptance", IMAGE["id"])
    # This injected fixture is deliberately not CAS verification evidence.
    assert len(report["cases"]) == 16
    runner = NonexecutingRunner.instances[-1]
    assert len(runner.jobs) == 16 and runner.preflights == [(IMAGE["id"], DEFAULT_RESOURCES)]
    assert runner.cleanups == 17
    assert all(result["cleanup_verified"] for result in report["cases"])
    assert json.loads((tmp_path / "acceptance/acceptance.json").read_bytes()) == report
    assert not any(thread.name == "reasoning-acceptance-server" for thread in threading.enumerate())
    assert (tmp_path / "acceptance").stat().st_mode & 0o077 == 0
    assert "token" not in json.dumps(report)


def test_algebra_artifact_corruption_prevents_success_receipt_and_stops_server(monkeypatch, catalog, tmp_path):
    monkeypatch.setattr("reasoning_ledger.image_acceptance.DockerRunner", NonexecutingRunner)
    monkeypatch.setattr(NonexecutingRunner, "corrupt_artifact", True)
    with pytest.raises(ValueError, match="artifact bytes"):
        verify(catalog, tmp_path / "acceptance", IMAGE["id"])
    assert not (tmp_path / "acceptance/acceptance.json").exists()
    assert not any(thread.name == "reasoning-acceptance-server" for thread in threading.enumerate())


def test_cleanup_failure_cannot_skip_server_shutdown_or_write_success_receipt(monkeypatch, catalog, tmp_path):
    monkeypatch.setattr("reasoning_ledger.image_acceptance.DockerRunner", NonexecutingRunner)
    monkeypatch.setattr(NonexecutingRunner, "cleanup_failure", True)
    with pytest.raises(ValueError, match="cleanup did not complete"):
        verify(catalog, tmp_path / "acceptance", IMAGE["id"])
    assert not (tmp_path / "acceptance/acceptance.json").exists()
    assert not any(thread.name == "reasoning-acceptance-server" for thread in threading.enumerate())


def test_command_never_falls_back_to_fake_execution_on_unavailable_daemon(monkeypatch, catalog, tmp_path):
    class UnavailableRunner(NonexecutingRunner):
        def preflight(self, image, limits):
            raise RuntimeFault("sandbox_unavailable")

    monkeypatch.setattr("reasoning_ledger.image_acceptance.DockerRunner", UnavailableRunner)
    with pytest.raises(SystemExit) as error:
        main(["--catalog", str(catalog), "--output", str(tmp_path / "failed")])
    assert error.value.code == 1
    assert not (tmp_path / "failed/acceptance.json").exists()
    assert not any(thread.name == "reasoning-acceptance-server" for thread in threading.enumerate())


def test_existing_output_and_unknown_image_are_refused_before_starting_server(catalog, tmp_path):
    with pytest.raises(ValueError):
        verify(catalog, tmp_path / "unknown", "missing")
    assert not (tmp_path / "unknown").exists()
    (tmp_path / "existing").mkdir()
    with pytest.raises(FileExistsError):
        verify(catalog, tmp_path / "existing", IMAGE["id"])


def test_python_pressure_and_isolation_programs_have_valid_minimum_python_syntax():
    for program in (PROGRAMS["python"], *NOETHER_PROGRAMS.values(), ISOLATION, DISK, PIDS, MEMORY):
        ast.parse(program, feature_version=(3, 11))
    # The TeX image runs Ubuntu 22.04's Python 3.10.
    for program in (*TEX_PROGRAMS.values(), ISOLATION, DISK, PIDS, MEMORY):
        ast.parse(program, feature_version=(3, 10))


TEX_IMAGE = {**IMAGE, "id": "tex", "reference": "registry.example.invalid/tex@sha256:" + "e" * 64, "languages": ["python"]}


def test_tex_acceptance_compiles_documents_and_keeps_the_python_isolation_cases(monkeypatch, tmp_path):
    NonexecutingRunner.instances = []
    monkeypatch.setattr("reasoning_ledger.image_acceptance.DockerRunner", NonexecutingRunner)
    path = tmp_path / "images.json"
    path.write_text(json.dumps([TEX_IMAGE]))
    report = verify(path, tmp_path / "acceptance", "tex")
    assert [case["case"] for case in report["cases"]] == [
        "tex_pdf", "tex_bibliography", "tex_missing_package", "isolation", "disk", "pids", "memory", "wall", "cancel"]
    runner = NonexecutingRunner.instances[-1]
    assert {job["parameters"]["language"] for job in runner.jobs} == {"python"}
    assert runner.preflights == [("tex", DEFAULT_RESOURCES)]
    assert "latexmk" in TEX_PROGRAMS["tex_pdf"] and "/work/out/main.pdf" in TEX_PROGRAMS["tex_pdf"]
    assert "biber" in TEX_PROGRAMS["tex_bibliography"] and "main.bbl" in TEX_PROGRAMS["tex_bibliography"]
    assert "returncode != 0" in TEX_PROGRAMS["tex_missing_package"]


def test_tex_acceptance_refuses_a_corrupt_pdf_and_mismatched_languages(monkeypatch, tmp_path):
    monkeypatch.setattr("reasoning_ledger.image_acceptance.DockerRunner", NonexecutingRunner)
    monkeypatch.setattr(NonexecutingRunner, "corrupt_artifact", True)
    path = tmp_path / "images.json"
    path.write_text(json.dumps([TEX_IMAGE]))
    with pytest.raises(ValueError, match="PDF artifact bytes"):
        verify(path, tmp_path / "corrupt", "tex")
    assert not (tmp_path / "corrupt/acceptance.json").exists()
    path.write_text(json.dumps([{**TEX_IMAGE, "languages": list(PROGRAMS)}, {**IMAGE, "languages": ["python"]}]))
    for image_id in ("tex", "research-cpu"):
        with pytest.raises(ValueError, match="profile's public languages"):
            verify(path, tmp_path / ("languages-" + image_id), image_id)
        assert not (tmp_path / ("languages-" + image_id)).exists()
