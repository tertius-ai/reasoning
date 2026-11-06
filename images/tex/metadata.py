"""Check the installed TeX toolchain and emit deterministic capabilities and SPDX inventory.

Runs with the image's Python, using only the standard library. It compiles a probe
document with the installed tools before writing any evidence. The inventory does not
assert a completed license or corresponding-source audit.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

CAPABILITIES_PATH = "/etc/reasoning/capabilities.json"
SBOM_PATH = "/usr/share/reasoning/sbom.spdx.json"
DEFAULTS = {
    "cpu": 1,
    "memory_bytes": 1073741824,
    "disk_bytes": 268435456,
    "pids": 128,
    "wall_time_seconds": 120,
    "artifact_bytes": 1048576,
}
# Each tool's version is the exact installed Debian package that provides it.
TOOLS = (
    ("python", "python3", ("python3",), ("python-program-execution",)),
    ("pdftex", "texlive-binaries", ("pdflatex", "pdftex", "latex"), ("latex-to-pdf",)),
    ("latexmk", "latexmk", ("latexmk",), ("latex-build-automation",)),
    ("bibtex", "texlive-binaries", ("bibtex",), ("bibliography",)),
    ("biber", "biber", ("biber",), ("biblatex-bibliography",)),
    ("makeindex", "texlive-binaries", ("makeindex",), ("index-generation",)),
)
PROBE = r"""\documentclass{article}
\usepackage{amsmath,amssymb,amsthm,mathtools}
\usepackage{tikz-cd}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage{hyperref}
\newtheorem{theorem}{Theorem}
\begin{document}
\begin{theorem}
Every group of prime order $p$ is cyclic.
\end{theorem}
\[ \begin{tikzcd} A \arrow[r, "f"] & B \end{tikzcd} \]
\end{document}
"""
BIBLIOGRAPHY = r"""\documentclass{article}
\usepackage[backend=biber]{biblatex}
\addbibresource{references.bib}
\begin{document}
Sylow's theorems \cite{sylow}.
\printbibliography
\end{document}
"""
REFERENCES = """@article{sylow,
  author = {Sylow, Ludwig},
  title = {Th\\'eor\\`emes sur les groupes de substitutions},
  journal = {Mathematische Annalen},
  volume = {5},
  year = {1872},
  pages = {584--594},
}
"""


def run(argv: list[str], *, cwd: str | None = None) -> str:
    result = subprocess.run(argv, text=True, capture_output=True, check=True, timeout=300, cwd=cwd)
    if len(result.stdout) + len(result.stderr) > 1024 * 1024:
        raise ValueError("toolchain probe exceeded its output limit")
    return (result.stdout + result.stderr).strip()


def installed() -> dict[str, dict[str, str]]:
    packages = {}
    query = "-f=${db:Status-Abbrev}\t${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n"
    for line in run(["dpkg-query", "-W", query]).splitlines():
        status, name, release, source, source_version = line.split("\t")
        if status.strip() != "ii" or name in packages:
            raise ValueError("installed package state is incomplete or duplicated")
        packages[name] = {"version": release, "source": source, "source_version": source_version}
    return packages


def probe() -> None:
    """Compile probe documents with the installed formats, fonts and biber."""
    for tool in sorted({entry for _, _, entries, _ in TOOLS for entry in entries}):
        if shutil.which(tool) is None:
            raise ValueError("installed tool is missing: " + tool)
    for name, source in (("probe", PROBE), ("bibliography", BIBLIOGRAPHY)):
        with tempfile.TemporaryDirectory(prefix="reasoning-tex-probe-") as directory:
            Path(directory, name + ".tex").write_text(source)
            Path(directory, "references.bib").write_text(REFERENCES)
            run(["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", name + ".tex"], cwd=directory)
            if not Path(directory, name + ".pdf").read_bytes().startswith(b"%PDF-"):
                raise ValueError("probe document did not produce a PDF")


def notices(lock: dict, root: Path = Path("/usr/share/doc")) -> None:
    """The base image keeps each package's copyright file while dropping other documentation."""
    for name in lock["packages"]:
        path = root / name / "copyright"
        if not path.is_file() or not path.read_bytes().strip():
            raise ValueError("installed package lacks its copyright notice: " + name)


def documents(lock: dict, packages: dict, python: str, revision: str, timestamp: str) -> tuple[dict, dict]:
    observed = {name: packages.get(name, {}).get("version") for name in lock["packages"]}
    if observed != lock["packages"]:
        raise ValueError("installed TeX packages differ from locked versions")
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision):
        raise ValueError("source revision must be a complete Git object ID")
    if not timestamp.endswith("Z") or datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset().total_seconds() != 0:
        raise ValueError("build timestamp must be UTC")
    capabilities = {
        "schema_id": "reasoning.executor.image-capabilities",
        "schema_version": "1.0.0-alpha.1",
        "manifest_scope": "installed",
        "image_name": "reasoning/tex",
        "build_timestamp": timestamp,
        "source_revision": revision,
        "platform": {"os": "linux", "architecture": "amd64"},
        "runtimes": [
            {"name": "python", "version": python},
            {"name": "texlive", "version": packages["texlive-base"]["version"]},
        ],
        "tools": [
            {"name": name, "version": packages[package]["version"], "entry_points": list(entries), "operations": list(operations)}
            for name, package, entries, operations in TOOLS
        ],
        "compatibility": {"executor_contract": "1.0.0-alpha.1", "runner": "reasoning-compute-worker"},
        "paths": {"working": "/work", "input": "/work/inputs.json", "output": "/work/out", "cache": "/tmp", "temporary": "/tmp"},
        "resource_limits": {"defaults": DEFAULTS, "maximums": lock["maximum_resources"]},
        "network": {"supported_modes": ["denied"], "possible_destinations": []},
        "devices": [],
        "restrictions": [
            "Installed capability is not per-job permission; the worker must enforce limits and deny network/devices.",
            "Writable job paths are bounded worker tmpfs mounts, not image volumes.",
            "Jobs are Python programs: write the .tex sources, run latexmk or pdflatex with subprocess, "
            "and copy the PDF to /work/out.",
            "TeX Live 2021 from Ubuntu 22.04: LaTeX base, recommended and extra, TikZ and pictures, science, "
            "recommended fonts, biblatex with biber. XeLaTeX, LuaLaTeX and TeX Live documentation are not installed.",
            "Nothing is downloaded or installed while a job runs; a missing package or font fails the compile.",
            "Shell escape is limited to TeX Live's restricted list.",
        ],
        "sbom": {"format": "spdx-json", "path": SBOM_PATH},
    }
    records = []
    for name, package in sorted(packages.items()):
        identity = json.dumps({"kind": "deb", "name": name, **package}, sort_keys=True, separators=(",", ":"))
        records.append(
            {
                "SPDXID": "SPDXRef-Package-" + hashlib.sha256(identity.encode()).hexdigest(),
                "name": name,
                "versionInfo": package["version"],
                "filesAnalyzed": False,
                "downloadLocation": "NOASSERTION",
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "comment": identity,
            }
        )
    if not records or len({p["SPDXID"] for p in records}) != len(records):
        raise ValueError("installed package inventory is empty or duplicated")
    inventory_digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "reasoning-tex-installed-inventory",
        "documentNamespace": f"https://schemas.reasoning.invalid/spdx/tex/{revision}/{inventory_digest}",
        "creationInfo": {"created": timestamp, "creators": ["Tool: reasoning-installed-inventory/1"]},
        "documentComment": (
            "Installed Debian package inventory of " + lock["base"] + " and the locked TeX packages, "
            "not a complete filesystem scan or license/source audit."
        ),
        "packages": records,
        "relationships": [
            {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": p["SPDXID"]} for p in records
        ],
    }
    return capabilities, sbom


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--build-timestamp", required=True)
    parser.add_argument("--capabilities", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(args.lock.read_bytes())
    os.umask(0o022)
    probe()
    notices(lock)
    capabilities, sbom = documents(lock, installed(), platform.python_version(), args.source_revision, args.build_timestamp)
    for path, body, limit in ((args.capabilities, capabilities, 1024**2), (args.sbom, sbom, 8 * 1024**2)):
        encoded = (json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(encoded) > limit:
            raise ValueError("installed evidence exceeds worker limits")
        path.write_bytes(encoded)


if __name__ == "__main__":
    main()
