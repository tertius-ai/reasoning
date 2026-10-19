"""Fixed root installer for a dedicated bot host; no cloud or model credential.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path("/root/reasoning-bot-install")
HOME = Path("/var/lib/reasoning-bot")
STATE = HOME / ".local/share/reasoning/bot"
NODE_VERSION = "24.15.0"
NODE_SHA = "472655581fb851559730c48763e0c9d3bc25975c59d518003fc0849d3e4ba0f6"
LEAN_VERSION = "4.24.0"
LEAN_SHA = "8f2ce2aa6de5ede5847a3e9517a930ede3d6954444f44535155da3250ac7f2fc"
MATHLIB_REV = "f897ebcf72cd16f89ab4577d0c826cd14afaafc7"
MATHLIB_SHA = "58274ab9ab85211e49cfa88eb84f17dee5e2d62623893549eef076ef9472103d"
CLI = {"codex": "@openai/codex@0.160.1", "claude": "@anthropic-ai/claude-code@2.1.248"}


def runner_command(spec):
    return f"/usr/local/lib/reasoning-bot/bin/reasoning-bot run --state-dir {STATE} --config {STATE}/config.json"


def run(args, **kwargs):
    subprocess.run(args, check=True, timeout=1200, **kwargs)


def download(url, path, checksum):
    with urllib.request.urlopen(url, timeout=60) as source, path.open("wb") as output:
        shutil.copyfileobj(source, output)
    if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
        raise ValueError("toolchain download checksum mismatch")


def install_lean():
    lean = Path("/usr/local/lib/reasoning-lean")
    archive = ROOT / "lean.zip"
    download(f"https://github.com/leanprover/lean4/releases/download/v{LEAN_VERSION}/lean-{LEAN_VERSION}-linux.zip",
             archive, LEAN_SHA)
    run(["unzip", "-q", "-o", str(archive), "-d", str(ROOT)])
    if not lean.exists():
        shutil.move(str(ROOT / f"lean-{LEAN_VERSION}-linux"), lean)
    mathlib = Path("/usr/local/share/reasoning-mathlib")
    if not mathlib.exists():
        archive = ROOT / "mathlib.tar.gz"
        download(f"https://codeload.github.com/leanprover-community/mathlib4/tar.gz/{MATHLIB_REV}", archive, MATHLIB_SHA)
        with tarfile.open(archive) as source:
            source.extractall(ROOT, filter="data")
        shutil.move(str(ROOT / f"mathlib4-{MATHLIB_REV}"), mathlib)
    if (mathlib / "lean-toolchain").read_text().strip() != "leanprover/lean4:v" + LEAN_VERSION:
        raise ValueError("mathlib Lean toolchain mismatch")
    if not (mathlib / ".git").exists():
        run(["git", "init"], cwd=mathlib)
        run(["git", "remote", "add", "origin", "https://github.com/leanprover-community/mathlib4.git"], cwd=mathlib)
        run(["git", "fetch", "--depth", "1", "origin", MATHLIB_REV], cwd=mathlib)
        run(["git", "reset", "--mixed", "FETCH_HEAD"], cwd=mathlib)
    env = {**os.environ, "PATH": str(lean / "bin") + ":" + os.environ["PATH"]}
    run([str(lean / "bin/lake"), "exe", "cache", "get"], cwd=mathlib, env=env)
    manifest = json.loads((mathlib / "lake-manifest.json").read_bytes())
    for item in manifest["packages"]:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=mathlib / ".lake/packages" / item["name"], text=True)
        if revision.strip() != item["rev"]:
            raise ValueError("mathlib dependency revision mismatch")
    paths = subprocess.check_output([str(lean / "bin/lake"), "env", "printenv", "LEAN_PATH"], cwd=mathlib, env=env, text=True).strip()
    paths = ":".join(str((mathlib / item).resolve()) for item in paths.split(":"))
    for name in ("lean", "lake"):
        wrapper = Path("/usr/local/bin") / name
        wrapper.write_text(f'#!/bin/sh\nexport LEAN_PATH="{paths}"\nexec {lean}/bin/{name} "$@"\n')
        wrapper.chmod(0o755)
    run(["/usr/local/bin/lean", "--stdin"], input="import Mathlib\nexample : (2 : ℕ) + 3 = 5 := by norm_num\n", text=True)


def install():
    with Path("/run/reasoning-bot-install.lock").open("a") as lock:
        # SSH disconnects need not stop a remote installer. A resumed owner
        # operation must never race an earlier installer still on this host.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _install()


def _install():
    # Public toolchains must be readable by the non-root bot. Secrets below
    # have explicit 0600 files inside 0700 root/bot private directories.
    os.umask(0o022)
    value = json.loads((ROOT / "input.json").read_bytes())
    spec, config, request = value["spec"], value["config"], value["request"]
    original = json.loads(Path("/etc/reasoning-provisioning.json").read_bytes())
    if original != {key: request[key] for key in ("plan_id", "deployment_id")}:
        raise ValueError("original provisioning identity mismatch")
    # Cloud-init owns initial apt work; never race package locks on a new VM.
    run(["cloud-init", "status", "--wait"])
    run(["apt-get", "update"], env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
    packages = ["python3-venv", "python3-pip", "bubblewrap", "ca-certificates", "git", "curl", "unzip", "apparmor"]
    if spec["cas"]:
        packages += ["python3-sympy", "gap", "pari-gp", "singular"]
    run(["apt-get", "install", "-y", "--no-install-recommends", *packages],
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
    try:
        pwd.getpwnam("reasoning-bot")
    except KeyError:
        run(["useradd", "--system", "--create-home", "--home-dir", str(HOME), "--shell", "/bin/bash", "reasoning-bot"])
        pwd.getpwnam("reasoning-bot")
    HOME.chmod(0o700)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    node = Path("/usr/local/lib/reasoning-node")
    if not node.exists():
        archive = ROOT / "node.tar.xz"
        download(f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz", archive, NODE_SHA)
        with tarfile.open(archive) as source:
            source.extractall(ROOT, filter="data")
        shutil.move(str(ROOT / f"node-v{NODE_VERSION}-linux-x64"), node)
    for name in ("node", "npm", "npx"):
        link = Path("/usr/local/bin") / name
        if not link.exists():
            link.symlink_to(node / "bin" / name)
    run(["npm", "install", "--global", "--prefix", "/usr/local", CLI[spec["engine"]]])
    wheel = ROOT / value["wheel_name"]
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != spec["wheel_sha256"]:
        raise ValueError("bot wheel checksum mismatch")
    venv = Path("/usr/local/lib/reasoning-bot")
    run(["python3", "-m", "venv", "--system-site-packages", str(venv)])
    # Install the exact confirmed artifact. Source uses locked dependencies,
    # without building or downloading a Reasoning distribution.
    if wheel.name == "reasoning-source.zip":
        # Validate every archive member before reading or installing anything.
        import importlib.util
        from zipfile import ZipFile

        module_spec = importlib.util.spec_from_file_location("source_installer", ROOT / "source-installer.py")
        helper = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(helper)
        with ZipFile(wheel) as archive:
            helper.inspect_source(archive)
            requirements = ROOT / "requirements.txt"
            requirements.write_bytes(archive.read("requirements.txt"))
        run([str(venv / "bin/pip"), "install", "--require-hashes", "--index-url", "https://pypi.org/simple", "-r", str(requirements)])
        run([str(venv / "bin/python"), "-I", str(ROOT / "source-installer.py"), str(wheel)])
    else:
        run([str(venv / "bin/pip"), "install", "--force-reinstall", str(wheel)])
    if spec["lean"]:
        install_lean()
    if spec["cas"] or spec["lean"]:
        # Scope Ubuntu's userns allowance to the installed sandbox executable,
        # not a global relaxation of the host's AppArmor protection.
        profile = Path("/etc/apparmor.d/reasoning-bwrap")
        profile.write_text('abi <abi/4.0>,\ninclude <tunables/global>\n/usr/bin/bwrap flags=(unconfined) {\n  userns,\n}\n')
        run(["apparmor_parser", "-r", str(profile)])
        run(["sudo", "-u", "reasoning-bot", "-H", "bwrap", "--unshare-all", "--die-with-parent",
             "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
             "--proc", "/proc", "--dev", "/dev", "/usr/bin/true"])
    if spec["cas"]:
        run(["/usr/bin/python3", "-c", "import sympy; assert sympy.gcd(12,18)==6"])
        run(["gap", "-q"], input="if Gcd(12,18) <> 6 then FORCE_QUIT_GAP(1); fi; QUIT;\n", text=True)
        run(["gp", "-q"], input="if(gcd(12,18)!=6,error(\"CAS probe\"));quit\n", text=True)
        run(["Singular", "-q"], input="ring r=0,x,dp; poly f=x^2-1; factorize(f); quit;\n", text=True)
    config_path = STATE / "config.json"
    if config_path.exists() and json.loads(config_path.read_bytes()) != config:
        raise ValueError("existing bot enrollment differs")
    config_path.write_bytes(json.dumps(config, sort_keys=True, separators=(",", ":")).encode())
    config_path.chmod(0o600)
    agent = {"engine": spec["engine"], "models": spec["models"], "local_tools": spec["cas"] or spec["lean"],
             "workspace": str(STATE / "workspaces")}
    (STATE / "agent.json").write_bytes(json.dumps(agent, sort_keys=True, separators=(",", ":")).encode())
    (STATE / "agent.json").chmod(0o600)
    run(["chown", "-R", "reasoning-bot:reasoning-bot", str(HOME)])
    if value.get("console_password") is not None:
        # Out-of-band console access only; ssh_pwauth remains false. The
        # controller locks this password after provider authentication/start.
        run(["chpasswd"], input="reasoning-bot:" + value["console_password"] + "\n", text=True)
    unit = f'''[Unit]
Description=Reasoning personal bot
After=network-online.target
Wants=network-online.target
[Service]
User=reasoning-bot
Group=reasoning-bot
Environment=HOME={HOME}
Environment=PATH=/usr/local/bin:/usr/bin:/bin
WorkingDirectory={HOME}
ExecStart={runner_command(spec)}
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={HOME}
[Install]
WantedBy=multi-user.target
'''
    Path("/etc/systemd/system/reasoning-bot.service").write_text(unit)
    run(["systemctl", "daemon-reload"])
    (ROOT / "input.json").unlink()
    print("Installation verified. Sign in as reasoning-bot, then start the service.")


if __name__ == "__main__":
    # Root-only fixed log; never echo the input document or tokens.
    with Path("/var/log/reasoning-bot-install.log").open("a") as log:
        os.chmod(log.name, 0o600)
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        install()
