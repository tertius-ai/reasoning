"""Bounded, metadata-only discovery from the bot owner's installed CLI.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import suppress

from .bot_runtime import MODEL
from .reasoning_effort import CLI_EFFORTS

MAX_OUTPUT = 4 * 1024 * 1024
PROBE_SECONDS = 20
REFRESH_SECONDS = 300
RETRY_SECONDS = 60
MAX_MODELS = 20


def _probe(engine):
    """Never submit a user message, start a turn, or run a model to test access."""
    request = None
    if engine == "codex":
        args = ["codex", "debug", "models"]
    elif engine == "claude":
        # The Agent SDK's supportedModels() reads models from this initialize response.
        # Disable hooks, project settings, tools and MCP; only initialize is sent.
        args = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--setting-sources", "", "--settings", '{"disableAllHooks":true}', "--disable-slash-commands",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--tools", "",
                "--permission-mode", "dontAsk", "--no-session-persistence"]
        request = {"type": "control_request", "request_id": "model-discovery",
                   "request": {"subtype": "initialize", "hooks": None}}
    else:
        raise ValueError("unknown CLI")
    with tempfile.TemporaryDirectory(prefix="reasoning-models-") as directory, subprocess.Popen(
            args, cwd=directory, stdin=subprocess.PIPE if request else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            if request:
                process.stdin.write((json.dumps(request) + "\n").encode())
                process.stdin.flush()
            deadline, output, pending = time.monotonic() + PROBE_SECONDS, bytearray(), bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        break
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        if request or process.wait(timeout=max(0.01, deadline - time.monotonic())):
                            raise ValueError("metadata unavailable")
                        return json.loads(output)
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        raise ValueError("metadata exceeds allowance")
                    if request:
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, pending = pending.partition(b"\n")
                            event = json.loads(line)
                            if event.get("type") == "control_response":
                                response = event.get("response", {})
                                if response.get("request_id") == request["request_id"]:
                                    if response.get("subtype") != "success":
                                        raise ValueError("metadata unavailable")
                                    return response["response"]
                raise ValueError("metadata timed out")
        finally:
            # Also bound and reap any subprocesses; no metadata process survives the probe.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def discover(engine):
    document = _probe(engine)
    rows = document.get("models")
    if not isinstance(rows, list) or not rows:
        raise ValueError("model catalog unavailable")
    catalog = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("slug" if engine == "codex" else "value")
        if not isinstance(model, str) or not MODEL.fullmatch(model):
            continue
        if engine == "codex" and row.get("visibility") != "list":
            continue
        name = row.get("display_name" if engine == "codex" else "displayName")
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
            name = model
        levels = row.get("supported_reasoning_levels" if engine == "codex" else "supportedEffortLevels", [])
        if not isinstance(levels, list):
            levels = []
        if engine == "codex":
            levels = [item.get("effort") for item in levels if isinstance(item, dict)]
        catalog[model] = {"name": name, "efforts": [level for level in CLI_EFFORTS if level in levels]}
    if not catalog:
        raise ValueError("no usable models")
    return catalog


class ModelDiscovery:
    """Refresh off the lease/heartbeat thread; failed refreshes retain the last good catalog."""

    def __init__(self, engine):
        self.engine = engine
        self._lock = threading.Lock()
        self._catalog = {}
        self._next_refresh = 0
        self._refreshing = False

    def _refresh(self):
        catalog = None
        try:
            catalog = discover(self.engine)
        except (OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.TimeoutExpired):
            # CLI stderr, account metadata and credentials never enter the advertisement.
            pass
        finally:
            with self._lock:
                if catalog:
                    self._catalog = catalog
                self._next_refresh = time.monotonic() + (REFRESH_SECONDS if catalog else RETRY_SECONDS)
                self._refreshing = False

    def advertisement(self, configured=None):
        with self._lock:
            if not self._refreshing and time.monotonic() >= self._next_refresh:
                self._refreshing = True
                threading.Thread(target=self._refresh, name="bot-model-discovery", daemon=True).start()
            catalog = self._catalog.copy()
        # A saved default-only profile also discovers models automatically. Explicit lists remain an owner allowlist.
        automatic = not configured or configured == ["default"]
        models = ["default", *(model for model in catalog if model != "default")][:MAX_MODELS] if automatic else configured
        names = {model: catalog.get(model, {}).get("name", model) for model in models}
        names["default"] = "CLI default"
        efforts = {model: catalog.get(model, {}).get("efforts", []) for model in models}
        # Codex's raw catalog does not identify the effective CLI default. Never guess its effort levels.
        return models, {"efforts": efforts, "model_names": {model: names[model] for model in models}, "gateway": "topic-v2"}
