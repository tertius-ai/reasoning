"""Exercise real CAS images through a disposable loopback public API and worker.

Uses actual HTTP and the production Docker runner, not mocked container output.
This is not a public HTTPS, cloud, native-client, or full multi-user release gate.
The private output directory contains test server/worker credentials; publish
only acceptance.json, never the complete directory.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from .deployment.host import InstallError
from .deployment.private import protected_parent
from .image_context import NOETHER_VERSION
from .image_release import digest, write_new
from .server.api import Config, create_app
from .server.compute_catalog import DEFAULT_RESOURCES, canonical, load_catalog
from .server.compute_docker import DockerRunner, RuntimeFault
from .server.compute_worker import ComputeWorker, WorkerClient, WorkerFault, enrollment

PROGRAMS = {
    "sage": 'R.<x> = PolynomialRing(QQ)\nassert sorted((x^4-1).roots()) == [(-1, 1), (1, 1)]\nprint("REASONING_SAGE_OK")\n',
    "python": (
        "from sage.all import matrix, QQ\nfrom pathlib import Path\n"
        "assert matrix(QQ, [[1,2],[3,4]]).determinant() == -2\n"
        'Path("/work/out/result.json").write_bytes(b\'{"determinant":-2}\\n\')\nprint("REASONING_PYTHON_OK")\n'
    ),
    "gap": 'if Size(SymmetricGroup(5)) <> 120 then Error("wrong group order"); fi;\nPrint("REASONING_GAP_OK\\n");\nQUIT;\n',
    "pari": 'if(eulerphi(123456789)!=82260072,error("wrong totient"));\nprint("REASONING_PARI_OK");\nquit;\n',
    "julia": (
        'using LinearAlgebra\n@assert gcd(big(54),big(24)) == 6\n@assert det([1//1 2//1; 3//1 4//1]) == -2\nprintln("REASONING_JULIA_OK")\n'
    ),
    "lean": 'example : (2 : Nat) + 3 = 5 := rfl\n#eval IO.println "REASONING_LEAN_OK"\n',
}


def noether_program(operation: str, payload: dict, assertions: str) -> str:
    """Exercise the same image worker path used by declarative server tools."""
    body = json.dumps({"operation": operation, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return (
        "import json, subprocess\n"
        'p = subprocess.run(["python3", "/opt/reasoning/noether_worker.py"], input='
        + repr(body)
        + ", text=True, capture_output=True, timeout=30)\n"
        + "assert p.returncode == 0, p.stderr + p.stdout\nresponse = json.loads(p.stdout)\n"
        + 'assert response["ok"] is True\nr = response["result"]\n'
        + assertions
    )


NOETHER_PROGRAMS = {
    "noether_status": noether_program(
        "capabilities", {},
        f'assert r["version"] == "{NOETHER_VERSION}" and r["operations"] > 0\n'
        'assert set(r["domains"]) == {"category","combin","commalg","geometry","groups","homalg","linalg","numthy","topology"}\n'
        'print("REASONING_NOETHER_STATUS_OK")\n',
    ),
    "noether_describe": noether_program(
        "describe", {"operation": "numthy.factor"},
        'assert r["name"] == "numthy.factor"\nprint("REASONING_NOETHER_DESCRIBE_OK")\n',
    ),
    "noether_factor": noether_program(
        "factor_integer", {"value": -360},
        'assert r == {"value":-360,"unit":-1,"factors":[[2,3],[3,2],[5,1]],"certificate_verified":True}\n'
        'print("REASONING_NOETHER_FACTOR_OK")\n',
    ),
    "noether_matrix": noether_program(
        "matrix_invariants", {"ring": "Z", "rows": [[2, 0], [0, 4]]},
        'assert r["rank"] == 2 and r["invariant_factors"] == [2,4] and r["absolute_determinant"] == 8\n'
        'assert r["certificate_verified"] is True\nprint("REASONING_NOETHER_MATRIX_OK")\n',
    ),
}
ISOLATION = """import errno, os, socket
from pathlib import Path
assert os.getuid() == os.getgid() == 10001
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
assert status["NoNewPrivs"].strip() == "1"
assert status["CapEff"].strip() == "0000000000000000"
assert status["Seccomp"].strip() == "2"
assert not Path("/var/run/docker.sock").exists()
assert not Path("/run/secrets").exists()
try:
    Path("/etc/reasoning-write-probe").write_text("must refuse")
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES)
else:
    raise AssertionError("writable image root")
for address in ("1.1.1.1", "169.254.169.254"):
    with socket.socket() as connection:
        connection.settimeout(0.5)
        try:
            connection.connect((address, 80))
        except OSError:
            pass
        else:
            raise AssertionError("runtime external network reached")
assert not any(line.split()[1] == "00000000" for line in Path("/proc/net/route").read_text().splitlines()[1:])
print("REASONING_ISOLATION_OK")
"""
DISK = """import errno
try:
    with open("/tmp/space", "wb") as stream:
        for _ in range(16):
            stream.write(bytes(1024**2))
except OSError as error:
    assert error.errno == errno.ENOSPC
else:
    raise AssertionError("tmpfs disk allowance not enforced")
print("REASONING_DISK_OK")
"""
PIDS = """import errno, os, signal, time
from pathlib import Path
assert Path("/sys/fs/cgroup/pids.max").read_text().strip() == "32"
children = []
limited = False
try:
    for _ in range(64):
        try:
            child = os.fork()
        except OSError as error:
            assert error.errno == errno.EAGAIN
            limited = True
            break
        if child == 0:
            time.sleep(60)
            os._exit(0)
        children.append(child)
finally:
    for child in children:
        os.kill(child, signal.SIGKILL)
    for child in children:
        os.waitpid(child, 0)
assert limited
print("REASONING_PIDS_OK")
"""
MEMORY = """from pathlib import Path
assert Path("/sys/fs/cgroup/memory.max").read_text().strip() == "134217728"
print("REASONING_MEMORY_PRESSURE", flush=True)
allocation = bytearray(512 * 1024**2)
print("REASONING_MEMORY_UNBOUNDED", flush=True)
"""


def request(client: httpx.Client, method: str, path: str, expected: int = 200, body: dict | None = None):
    response = client.request(method, path, **({"json": body} if body is not None else {}))
    if response.status_code != expected:
        raise ValueError(f"acceptance API {method} {path} failed with HTTP {response.status_code}")
    return response.json()


def acceptance_limits(image_id: str) -> dict:
    return {**DEFAULT_RESOURCES, **({"memory_bytes": 4 * 1024**3} if image_id == "lean-mathlib" else {})}


def exercise(client: httpx.Client, worker: ComputeWorker, root: str, topic: str, image_id: str) -> list[dict]:
    cases = [(language, language, source, {}) for language, source in PROGRAMS.items()]
    cases += [(name, "python", source, {}) for name, source in NOETHER_PROGRAMS.items()]
    if image_id == "lean-mathlib":

        def lean_project(source, expected):
            return json.dumps(
                {
                    "format": 1,
                    "files": {"Proofs/Main.lean": source},
                    "entry": "Proofs/Main.lean",
                    "target": "target",
                    "expected_type": expected,
                }
            )

        source = (
            "import Mathlib.Tactic.NormNum\ntheorem target : (2 : Nat) + 3 = 5 := by norm_num\n"
            '#eval IO.println "REASONING_LEAN_PROJECT_OK"\n'
        )
        cases += [
            ("lean_project", "lean", lean_project(source, "(2 : Nat) + 3 = 5"), {"memory_bytes": 4 * 1024**3}),
            ("lean_sorry", "lean", lean_project("theorem target : True := by sorry\n", "True"), {}),
            ("lean_axiom", "lean", lean_project("axiom invented : False\ntheorem target : False := invented\n", "False"), {}),
            ("lean_wrong_type", "lean", lean_project("theorem target : True := True.intro\n", "False"), {}),
        ]
    cases += [
        ("isolation", "python", ISOLATION, {}),
        ("disk", "python", DISK, {"disk_bytes": 8 * 1024**2}),
        ("pids", "python", PIDS, {"pids": 32}),
        ("memory", "python", MEMORY, {"memory_bytes": 128 * 1024**2}),
        ("wall", "python", "import time; time.sleep(60)\n", {"wall_time_seconds": 1}),
        ("cancel", "python", "import time; time.sleep(90)\n", {}),
    ]
    results = []
    for name, language, source, changes in cases:
        body = {
            "client_id": "image-acceptance-" + name,
            "image_id": image_id,
            "language": language,
            "program": source,
            "resources": {**DEFAULT_RESOURCES, **changes},
        }
        job = request(client, "POST", root + f"/topics/{topic}/compute-jobs", 202, body)
        if request(client, "POST", root + f"/topics/{topic}/compute-jobs", 202, body) != job:
            raise ValueError("exact compute request replay differs")
        path = root + "/compute-jobs/" + job["id"]
        if name == "cancel":
            errors = []

            def run(errors=errors):
                try:
                    worker.run(once=True)
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=run, name="reasoning-acceptance-worker", daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 20
                while request(client, "GET", path)["status"] != "running":
                    if not thread.is_alive() or time.monotonic() >= deadline:
                        raise ValueError("cancellation case did not reach a running attempt")
                    time.sleep(0.1)
                request(client, "POST", path + "/cancel", body={})
            finally:
                thread.join(35)
                if thread.is_alive():
                    worker.stopping.set()
                    thread.join(15)
                if thread.is_alive():
                    raise ValueError("acceptance worker did not stop")
            if errors:
                raise errors[0]
        else:
            worker.run(once=True)
        result = request(client, "GET", path)
        events = request(client, "GET", path + "/events")
        logs = "".join(row["event"]["text"] for row in events if row["event"]["type"] == "log")
        if name == "cancel":
            valid = result["status"] == "cancelled"
        elif name == "wall":
            valid = result["status"] == "failed" and result["error_code"] == "wall_time_limit"
        elif name == "memory":
            valid = (
                result["status"] == "failed"
                and result["error_code"] in {"resource_limit", "execution_failed"}
                and "REASONING_MEMORY_PRESSURE" in logs
                and "REASONING_MEMORY_UNBOUNDED" not in logs
            )
        elif name in {"lean_sorry", "lean_axiom", "lean_wrong_type"}:
            valid = result["status"] == "failed" and result["exit_code"] not in {None, 0}
        else:
            valid = result["status"] == "succeeded" and "REASONING_" + name.upper() + "_OK" in logs
        if not valid:
            # These are our fixed, credential-free programs on a disposable fixture server.
            raise ValueError(f"real image acceptance case {name} failed ({result['status']}, {result['error_code']}): {logs[-4000:]}")
        if name == "python":
            artifacts = request(client, "GET", path + "/artifacts")
            if len(artifacts) != 1 or artifacts[0]["name"] != "result.json":
                raise ValueError("algebra artifact metadata differs")
            response = client.get(path + "/artifacts/" + artifacts[0]["id"])
            if (
                response.status_code != 200
                or response.content != b'{"determinant":-2}\n'
                or digest(response.content) != artifacts[0]["digest"]
            ):
                raise ValueError("algebra artifact bytes or digest differ")
        if name == "lean_project":
            artifacts = request(client, "GET", path + "/artifacts")
            report = next((file for file in artifacts if file["name"] == "lean-report.json"), None)
            if report is None:
                raise ValueError("Lean project did not retain its report")
            response = client.get(path + "/artifacts/" + report["id"])
            document = response.json()
            if (
                response.status_code != 200
                or digest(response.content) != report["digest"]
                or document.get("accepted") is not True
                or document.get("expected_type") != "(2 : Nat) + 3 = 5"
                or document.get("trust_level") != 0
                or document.get("program_sha256") != digest(source.encode())[7:]
            ):
                raise ValueError("Lean project report or exact statement differs")
        if worker.state() != {"phase": "idle"}:
            raise ValueError("worker result journal did not settle")
        worker.runner.verify_cleanup()
        results.append(
            {
                "case": name,
                "job_id": job["id"],
                "status": result["status"],
                "error_code": result["error_code"],
                "program_digest": digest(source.encode()),
                "logs_digest": digest(logs.encode()),
                "cleanup_verified": True,
            }
        )
    return results


def verify(catalog: Path, output: Path, image_id: str) -> dict:
    images = load_catalog(catalog)
    image = images.get(image_id)
    if image is None or set(image.languages) != set(PROGRAMS):
        raise ValueError("acceptance requires an approved image with all six public languages")
    worker_limits = acceptance_limits(image_id)
    protected_parent(output.absolute())
    output.mkdir(mode=0o700, exist_ok=False)
    worker_id = None
    server, server_thread, runner, worker_client, worker = None, None, None, None, None
    # Keep private test credentials outside the publishable receipt.
    catalog_copy = output / "compute-images.json"
    write_new(catalog_copy, (canonical([image.record()]) + "\n").encode())
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        url = "http://127.0.0.1:" + str(listener.getsockname()[1])
        config = Config(output / "server", url, inference_enabled=False, compute_images=catalog_copy)
        app = create_app(config)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, proxy_headers=False))
        server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True, name="reasoning-acceptance-server")
        server_thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if not server_thread.is_alive() or time.monotonic() >= deadline:
                raise ValueError("disposable acceptance server did not start")
            time.sleep(0.05)
        with httpx.Client(base_url=url, trust_env=False, follow_redirects=False, timeout=5) as client:
            owner = request(
                client,
                "POST",
                "/api/setup",
                201,
                {
                    "setup_token": (config.data_dir / "setup-token").read_text(),
                    "email": "image-acceptance@example.invalid",
                    "name": "Image acceptance",
                    "password": "disposable local image acceptance password",
                    "organization_name": "Image acceptance",
                    "transport": "bearer",
                },
            )
            client.headers["Authorization"] = "Bearer " + owner["access_token"]
            root = "/api/organizations/" + owner["organization_id"]
            project = request(client, "POST", root + "/projects", 201, {"name": "Image acceptance"})
            channel = request(client, "POST", root + "/channels", 201, {"name": "algebra", "project_id": project["id"]})
            topic = request(
                client, "POST", root + "/topics", 201, {"name": "Image acceptance", "channel_id": channel["id"], "kind": "gamma"}
            )
            request(
                client,
                "PUT",
                root + "/compute-policy",
                body={"allowed_images": [image_id], "monthly_cpu_seconds": 10000, "max_active_jobs": 1},
            )
            registration = request(
                client,
                "POST",
                root + "/compute-workers",
                201,
                {"name": "Image acceptance", "image_ids": [image_id], "resources": worker_limits},
            )
            worker_id = registration["id"]
            enrollment_config = enrollment(url, registration)
            runner = DockerRunner(output / "worker", worker_id)
            runner.preflight(image, worker_limits)
            worker_client = WorkerClient(enrollment_config)
            worker = ComputeWorker(output / "worker", enrollment_config, worker_client, runner)
            cases = exercise(client, worker, root, topic["id"], image_id)
        report = {
            "format": 1,
            "verification": "actual-loopback-http-public-api-and-docker-worker",
            "image": image.record(),
            "cases": cases,
            "execution_verified": True,
            "public_https_verified": False,
            "cloud_verified": False,
            "native_clients_verified": False,
            "signature_verified": False,
            "license_source_audit_verified": False,
        }
    finally:
        cleanup_errors = []
        if worker is not None:
            worker.stopping.set()
        if runner is not None and worker_id is not None:
            try:
                runner.cleanup()
                runner.verify_cleanup()
            except Exception as error:
                cleanup_errors.append(error)
        if worker_client is not None:
            try:
                worker_client.close()
            except Exception as error:
                cleanup_errors.append(error)
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(15)
            if server_thread.is_alive():
                cleanup_errors.append(ValueError("disposable acceptance server did not stop"))
        listener.close()
        if cleanup_errors:
            raise ValueError("disposable acceptance cleanup did not complete") from cleanup_errors[0]
    write_new(output / "acceptance.json", (canonical(report) + "\n").encode())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image-id", default="research-cpu")
    args = parser.parse_args(argv)
    try:
        report = verify(args.catalog, args.output.absolute(), args.image_id)
    except (ValueError, KeyError, OSError, RuntimeFault, WorkerFault, httpx.HTTPError, InstallError) as error:
        parser.exit(1, f"real image acceptance failed: {error}\n")
    print(json.dumps({"receipt": str(args.output / "acceptance.json"), "cases": len(report["cases"]), "execution_verified": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
