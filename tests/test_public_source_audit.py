"""Exact fixture exemptions must not suppress a new credential-like value.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import json
import os
import re
import secrets
import string
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def canary_value():
    """A fresh value that the default generic-api-key rule always reports.

    Ten distinct letters alternate with ten distinct digits. The value's Shannon entropy is therefore always
    log2(20), about 4.32 bits, above the rule's 3.5-bit threshold. No stopword of the rule alternates letters
    and digits, so none can occur in it. A random hex value missed about 1% of the time: it fell below the
    threshold or contained a stopword such as "dead".
    """
    rng = secrets.SystemRandom()
    letters, digits = rng.sample(string.ascii_lowercase, 10), rng.sample(string.digits, 10)
    return "".join(letter + digit for letter, digit in zip(letters, digits, strict=True))


class SourceAuditConfiguration(unittest.TestCase):
    def test_exemptions_keep_default_rules_and_exact_paths_and_values(self):
        config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
        self.assertEqual(config["extend"], {"useDefault": True})
        self.assertTrue(config["allowlists"])
        self.assertFalse((ROOT / ".gitleaksignore").exists())
        files = []
        for directory, children, names in os.walk(ROOT):
            children[:] = [name for name in children if name not in {"node_modules", ".venv", ".git", "build", "dist"}]
            files.extend((Path(directory) / name).relative_to(ROOT).as_posix() for name in names)
        for entry in config["allowlists"]:
            self.assertEqual(entry["targetRules"], ["generic-api-key"])
            self.assertEqual(entry["condition"], "AND")
            self.assertEqual(entry["regexTarget"], "secret")
            self.assertEqual(len(entry["paths"]), 1)
            for pattern in entry["paths"] + entry["regexes"]:
                self.assertTrue(pattern.startswith("^") and pattern.endswith("$"))
                self.assertNotIn(".*", pattern)
            self.assertTrue(any(re.fullmatch(entry["paths"][0], path) for path in files))

    @unittest.skipUnless(os.environ.get("REASONING_GITLEAKS"), "requires the pinned source-audit scanner")
    def test_new_value_in_an_exempted_fixture_is_still_detected(self):
        with tempfile.TemporaryDirectory(prefix="reasoning-audit-canary-") as temporary:
            root = Path(temporary)
            candidate = root / "tests/test_cloud_client.py"
            candidate.parent.mkdir()
            # Generated only for this scanner test; never a provider credential.
            candidate.write_text('api_key = "' + canary_value() + '"\n')
            report = root / "report.json"
            # Scan "." from the root so the finding's path is the repository-relative one the exempt paths match.
            result = subprocess.run([os.environ["REASONING_GITLEAKS"], "dir", ".", "--config", str(ROOT / ".gitleaks.toml"),
                                     "--redact=100", "--no-banner", "--log-level", "error", "--report-format", "json",
                                     "--report-path", str(report)], capture_output=True, timeout=30, cwd=root)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(any(row["RuleID"] == "generic-api-key" and row["File"] == "tests/test_cloud_client.py"
                                for row in json.loads(report.read_text())))


if __name__ == "__main__":
    unittest.main()
