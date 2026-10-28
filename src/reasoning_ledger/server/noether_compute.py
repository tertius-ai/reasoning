"""Discover the fixed Noether protocol and submit it through existing compute approval paths.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import ast
import copy
import hashlib
import json
from functools import cache
from pathlib import Path

from .store import StoreError

JOB_TOOLS = {"noether_compute", "noether_status", "noether_describe"}


@cache
def protocol():
    # Read literal descriptions without importing the execution worker or Noether on the server.
    source = (Path(__file__).parents[1] / "agents/noether_worker.py").read_bytes()
    descriptions = next(ast.literal_eval(node.value) for node in ast.parse(source).body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "_WRAPPER_DESCRIPTIONS" for target in node.targets))
    return hashlib.sha256(source).hexdigest(), descriptions


def operations(operation=None):
    digest, descriptions = protocol()
    result = {"worker_sha256": digest, "runtime_verified": False,
              "execution_tool": "noether_compute", "result_artifact": "noether-result.json",
              "note": "This is the submitted worker protocol, not an image readiness or mathematical truth certificate."}
    if operation is not None:
        if operation not in descriptions:
            raise StoreError(400, "select an executable operation from noether_operations")
        return {**result, "operation": copy.deepcopy(descriptions[operation])}
    return {**result, "operations": [{"name": name, "summary": item["summary"], "domain": item["domain"]}
                                     for name, item in descriptions.items()],
            "describe_tool": "noether_operations", "describe_argument": "operation"}


def submission(name, arguments):
    digest, descriptions = protocol()
    if name == "noether_status":
        operation, payload = "capabilities", {}
    elif name == "noether_describe":
        operation, payload = "describe", {"operation": arguments["operation"]}
    else:
        operation, payload = arguments["operation"], arguments["payload"]
    if operation not in {*descriptions, "capabilities", "describe"}:
        raise StoreError(400, "unsupported Noether wrapper operation; read noether_operations for exact names and schemas")
    request = json.dumps({"operation": operation, "payload": payload}, ensure_ascii=False, separators=(",", ":"))
    if len(request.encode()) > 12000:
        raise StoreError(400, "Noether request exceeds its bounded input")
    # Bind the advertised operation schemas to the exact worker executed inside the approved image.
    program = (
        "import hashlib, json, subprocess\nfrom pathlib import Path\n"
        'worker = Path("/opt/reasoning/noether_worker.py")\n'
        f"if hashlib.sha256(worker.read_bytes()).hexdigest() != {digest!r}:\n"
        '    raise SystemExit("Noether worker protocol differs from the advertised operation schemas")\n'
        'p = subprocess.run(["python3", str(worker)], input=' + repr(request) + ", text=True, capture_output=True)\n"
        "try:\n    result = json.loads(p.stdout)\n"
        "except ValueError:\n    result = {\"ok\": False, \"error\": \"invalid_noether_response\"}\n"
        "if not isinstance(result, dict):\n    result = {\"ok\": False, \"error\": \"invalid_noether_response\"}\n"
        + (f"result['protocol_operations'] = {list(descriptions)!r}\n" if operation == "capabilities" else "")
        + 'text = json.dumps(result, ensure_ascii=False)\nPath("/work/out/noether-result.json").write_text(text, encoding="utf-8")\n'
        "print(p.stdout)\nprint(p.stderr)\nraise SystemExit(p.returncode or (0 if result.get('ok') is True else 1))\n"
    )
    return {"image_id": arguments["image_id"], "language": "python", "program": program, "resources": arguments["resources"]}


def definitions():
    from .research_tools import schema, string

    common = {"image_id": string(64), "resources": {"type": "object"}}
    return [
        {"name": "noether_compute", "description": "Submit an executable Noether wrapper from noether_operations to an approved "
         "Python image. Uses existing compute authorization; returns a job or exact-action approval. "
         "Read noether-result.json with compute_result and artifact_read; results are evidence, not proof certification.",
         "risk": "external", "input_schema": schema({**common, "operation": string(200), "payload": {"type": "object"}},
                                                     ["image_id", "operation", "payload", "resources"])},
        {"name": "noether_status", "description": "Check the installed Noether version and capabilities in an approved sandbox. "
         "Returns an asynchronous job or approval, with output in noether-result.json. Use noether_operations for unpaid discovery.",
         "risk": "external", "input_schema": schema(common, ["image_id", "resources"])},
        {"name": "noether_describe", "description": "Request an installed Noether operation schema in an approved sandbox. "
         "Returns a job or approval; read noether-result.json. The executable wrappers and examples are in noether_operations.",
         "risk": "external", "input_schema": schema({**common, "operation": string(200)}, ["image_id", "operation", "resources"])},
    ]


def register_jobs(registry, submit):
    def handler(name):
        def invoke(**arguments):
            return submit(**submission(name, arguments))
        return invoke

    for definition in definitions():
        registry.tool(**definition)(handler(definition["name"]))
