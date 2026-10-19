"""Actual disposable GitHub Ubuntu host/tool probes; no cloud/model calls.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from reasoning_ledger import source_install
from reasoning_ledger.deployment import bot_cloud_host as host

if os.geteuid() != 0 or os.environ.get("GITHUB_ACTIONS") != "true":
    raise RuntimeError("host acceptance may only mutate a disposable GitHub runner")
engine = sys.argv[1]
wheel = Path("reasoning-source.zip")
host.ROOT.mkdir(mode=0o700, exist_ok=True)
shutil.copyfile(wheel, host.ROOT / wheel.name)
shutil.copyfile(source_install.__file__, host.ROOT / "source-installer.py")
request = {"plan_id": "a" * 64, "deployment_id": "b" * 32}
Path("/etc/reasoning-provisioning.json").write_text(json.dumps(request))
spec = {"engine": engine, "models": ["default"], "cas": True, "lean": engine == "codex",
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
(host.ROOT / "input.json").write_text(json.dumps({"request": request, "spec": spec, "wheel_name": wheel.name,
    "console_password": "a" * 32,
    "config": {"format": 1, "server": "https://example.invalid", "token": "rnb_" + "a" * 43}}))
original_run = host.run


def run(args, **kwargs):
    # Runners have no Droplet cloud-init service. Everything else is real.
    if args != ["cloud-init", "status", "--wait"]:
        original_run(args, **kwargs)


host.run = run
host.install()
status = subprocess.check_output(["passwd", "--status", "reasoning-bot"], text=True).split()
assert status[1] == "P", "temporary out-of-band console password was not installed"
venv = "/usr/local/lib/reasoning-bot/bin/python"
probe = '''
from pathlib import Path
from reasoning_ledger.server.bot_agent import doctor
from reasoning_ledger.server.bot_gateway import bounded_execute, sandbox_arguments
root = Path("/var/lib/reasoning-bot/.local/share/reasoning/bot/workspaces")
root.mkdir(mode=0o700, exist_ok=True)
checked = doctor(ENGINE, root, True)
assert checked["local_execution_available"]
commands = [
 ["/usr/bin/python3", "-c", "import sympy; assert sympy.gcd(12,18)==6; print('CAS verified')"],
 ["gap", "-q", "-c", "if Gcd(12,18) <> 6 then FORCE_QUIT_GAP(1); fi; QUIT;"],
 ["gp", "-q", "--version"], ["Singular", "--version"],
]
if ENGINE == "codex":
 (root / "Example.lean").write_text("import Mathlib\\nexample : (2 : ℕ) + 3 = 5 := by norm_num\\n")
 commands += [["lean", "Example.lean"]]
for command in commands:
 result = bounded_execute(sandbox_arguments(root, command))
 assert result["exit_code"] == 0, result
 assert not result["timed_out"], result
print("Sandbox CAS/Lean and CLI isolation verified without inference")
'''.replace("ENGINE", repr(engine))
subprocess.run(["sudo", "-u", "reasoning-bot", "-H", "env", "PATH=/usr/local/bin:/usr/bin:/bin", venv, "-c", probe],
               check=True, timeout=300)
