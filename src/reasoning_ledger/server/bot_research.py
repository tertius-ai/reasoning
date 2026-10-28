"""Shared read-only source adapters for scoped bot MCP invocations.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import json

from ..agents.tools import ToolRegistry
from .research_tools import DEFAULT_TOOLS, ResearchTools


class BotResearch(ResearchTools):
    def __init__(self, runtime, credential, run_id, lease, *, compute=None):
        self.runtime, self.credential, self.run_id, self.lease = runtime, credential, run_id, lease
        self.store, self.compute, self.workflows = runtime.store, compute, None
        self.registry = ToolRegistry()
        with self.store.transaction() as db:
            bot, run = runtime.leased(db, credential, run_id, lease)
            self.row = dict(run)
            self.org, self.topic, self.user, self.project = run["organization_id"], run["topic_id"], run["requested_by"], run["project_id"]
            self.collections = set()
            settings = db.execute("SELECT tools_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?",
                                  (self.org, self.topic)).fetchone()
            self.enabled = set(json.loads(settings[0])) if settings else set(DEFAULT_TOOLS)
            if "knowledge:read" not in json.loads(bot["scopes_json"]):
                self.enabled.clear()
        self._register()
        selected = self.registry
        self.registry = ToolRegistry()
        names = {"web_fetch", "web_search", "math_search", "math_read", "lmfdb_query", "oeis_search", "oeis_lookup",
                 "compute_catalog", "compute_result", "compute_log_read", "artifact_read", "noether_operations"}
        for tool in selected.list():
            if tool.name in names or tool.name.startswith(("arxiv_", "crossref_")):
                self.registry.register(tool)

    def _authorize(self):
        with self.store.transaction() as db:
            self.runtime.leased(db, self.credential, self.run_id, self.lease)

    def _target(self, topic):
        with self.store.transaction() as db:
            bot, run = self.runtime.leased(db, self.credential, self.run_id, self.lease)
            self.runtime.access(db, bot, topic)
            target = self.store._topic_access(db, run["requested_by"], self.org, topic)[4]
            return target
