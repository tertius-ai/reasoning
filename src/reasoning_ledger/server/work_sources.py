"""Durable public retrieval without holding database transactions over HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

from ..agents.scholarly import register_scholarly_tools
from ..agents.scientific import HttpResponse, ScientificGateway
from ..agents.tools import ToolRegistry
from . import artifact_chunks
from .research_context import encoded
from .research_network import WEB_MAXIMUM, SourceRefused, public_get, source_text
from .security import digest
from .store import StoreError, _id
from .work_contracts import VersionReference
from .work_inputs import bind


class PublicGateway(ScientificGateway):
    def request(self, **kwargs):
        return super().request(**(kwargs | {"max_attempts": 1, "allow_stale_on_error": False}))


def _transport(url, headers, timeout):
    body, response_headers = asyncio.run(public_get(url, headers=headers, maximum=1024 * 1024))
    return HttpResponse(200, body, response_headers)


class WorkSources:
    def __init__(self, tools):
        self.tools, self.jobs, self.store, self.work = tools, tools.jobs, tools.store, tools.work
        self.gateways = {}

    def _search(self, org, project, body):
        key = (org, project)
        if key not in self.gateways:
            self.gateways[key] = PublicGateway(self.store.data_dir / "research-sources" / org / project, transport=_transport)
        registry = ToolRegistry()
        register_scholarly_tools(registry, self.gateways[key])
        return registry.invoke(body.provider + "_search", {"query": body.query, "max_results": body.limit})

    async def _retrieve(self, org, project, tool, body):
        if tool == "source_search":
            return await asyncio.to_thread(self._search, org, project, body)
        raw, headers = await public_get(body.url, maximum=WEB_MAXIMUM)
        if len(raw) > WEB_MAXIMUM:
            raise SourceRefused("source_too_large", "The source exceeds this reader's 1 MiB limit.", "Use a smaller page.")
        text = source_text(raw, headers)
        media = headers.get("content-type", "").split(";", 1)[0]
        return {
            "text": text,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": media,
            "transformation": "html-text-v1" if media == "text/html" else "utf8-replacement-v1",
        }

    def _begin(self, credential, job_id, tool, body):
        program = self.jobs._job_scope(credential, job_id)
        checksum = digest(encoded(body.model_dump(exclude={"attempt_id", "fence", "lease", "client_id"})))
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            job = self.jobs._lease(db, credential, job_id, body)
            task, root = self.jobs._authorized_task(db, job)
            if db.execute(
                "SELECT 1 FROM work_tool_operations WHERE task_id=? AND task_epoch=? AND client_id=?",
                (task["id"], task["epoch"], body.client_id),
            ).fetchone():
                raise StoreError(409, "source request ID belongs to a retained local tool request")
            old = db.execute(
                "SELECT * FROM work_source_requests WHERE task_id=? AND task_epoch=? AND client_id=?",
                (task["id"], task["epoch"], body.client_id),
            ).fetchone()
            if old:
                if old["tool"] != tool or old["request_sha256"] != checksum:
                    raise StoreError(409, "source request ID was used for different input")
                self.tools._admit(db, job, task, root, tool, replay=True)
                if old["state"] == "reading":
                    raise StoreError(409, "source retrieval has a retained intent; no duplicate request was sent")
                return {"replay": {"result": json.loads(old["result_json"]), "sha256": old["result_sha256"], "replayed": True}}
            scopes = self.tools._admit(db, job, task, root, tool)
            identity = _id()
            db.execute(
                "INSERT INTO work_source_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    task["id"],
                    task["epoch"],
                    body.client_id,
                    job_id,
                    job["fence"],
                    tool,
                    checksum,
                    "reading",
                    None,
                    None,
                    None,
                    int(time.time()),
                    None,
                ),
            )
            for kind, target, _ in scopes:
                db.execute(
                    "INSERT INTO work_resource_accounts(scope_kind,scope_id,tools) VALUES(?,?,1) "
                    "ON CONFLICT(scope_kind,scope_id) DO UPDATE SET tools=work_resource_accounts.tools+1",
                    (kind, target),
                )
            return {"id": identity, "org": task["organization_id"], "project": root["project_id"], "program": program}

    def _entry(self, db, task, root, identity, title, payload):
        text = encoded(payload)
        if len(text) > 64000:
            raise ValueError("normalized source metadata exceeds its storage allowance")
        entry = self.tools.knowledge._write(
            db,
            task["requested_by"],
            "bot",
            task["organization_id"],
            client_id="work-source-" + identity,
            kind="bibliography",
            title=title[:200],
            body=text,
            state="active",
            scope_kind="project",
            scope_id=root["project_id"],
        )
        reference = VersionReference(
            kind="knowledge",
            id=entry["id"],
            revision=entry["revision"],
            sha256=digest(encoded({key: entry[key] for key in ("title", "body", "state")})),
        )
        bind(db, self.work, task, root, reference, "source_retrieval")
        return reference.model_dump()

    def _publish(self, db, context, task, root, tool, body, fetched):
        if tool == "source_search":
            items = []
            for index, item in enumerate(fetched["data"]["items"][: body.limit]):
                title = item.get("title") or "Untitled source"
                ref = self._entry(
                    db,
                    task,
                    root,
                    context["id"] + "-" + str(index),
                    title,
                    {"schema_type": "source_metadata", "item": item, "source": fetched["source"]},
                )
                items.append(
                    {
                        "reference": ref,
                        "title": title[:200],
                        "url": item.get("url"),
                        "doi": item.get("doi"),
                        "arxiv_id": item.get("arxiv_id"),
                    }
                )
            return {"items": items, "source": fetched["source"], "metadata_only": True}
        text = fetched["text"]
        checksum = artifact_chunks.put(db, task["organization_id"], root["project_id"], text)
        identity, now = _id(), int(time.time())
        reference = VersionReference(kind="source_snapshot", id=identity, revision=1, sha256=checksum)
        metadata = {key: fetched[key] for key in ("raw_sha256", "media_type", "transformation")}
        bibliography = self._entry(
            db,
            task,
            root,
            context["id"],
            body.title,
            {"schema_type": "source_capture", "reference": reference.model_dump(), "url": body.url, "retrieved_at": now, **metadata},
        )
        db.execute(
            "INSERT INTO work_source_captures VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                task["organization_id"],
                root["project_id"],
                context["id"],
                body.url,
                fetched["raw_sha256"],
                checksum,
                fetched["media_type"],
                fetched["transformation"],
                now,
                bibliography["id"],
                bibliography["revision"],
            ),
        )
        bind(db, self.work, task, root, reference, "source_retrieval")
        return {
            "reference": reference.model_dump(),
            "bibliography_reference": bibliography,
            "url": body.url,
            "preview": text[:300],
            "total_characters": len(text),
            "retrieved_at": now,
            **metadata,
        }

    def _finish(self, credential, job_id, tool, body, context, fetched, failed, reason=None):
        refused = None
        with self.store.transaction(write=True, scope=f"work-program:{context['program']}") as db:
            db.execute("SAVEPOINT source_publication")
            try:
                job = self.jobs._lease(db, credential, job_id, body)
                task, root = self.jobs._authorized_task(db, job)
                # The tool was already counted. Recheck grants without reserving another call.
                self.tools._admit(db, job, task, root, tool, counted=True)
                result = (
                    {"error": "public_source_unavailable", "retry": "Use a new request ID to retry.", **(reason or {})}
                    if failed
                    else self._publish(db, context, task, root, tool, body, fetched)
                )
            except StoreError as error:
                db.execute("ROLLBACK TO SAVEPOINT source_publication")
                refused, failed = error, True
                result = {"error": "source_authority_lost"}
            except (ValueError, KeyError, TypeError):
                db.execute("ROLLBACK TO SAVEPOINT source_publication")
                failed, result = True, {"error": "source_metadata_invalid"}
            finally:
                db.execute("RELEASE SAVEPOINT source_publication")
            payload = encoded(result)
            checksum = digest(payload)
            db.execute(
                "UPDATE work_source_requests SET state=?,result_json=?,result_sha256=?,error_code=?,finished_at=? "
                "WHERE id=? AND state='reading'",
                ("failed" if failed else "complete", payload, checksum, result.get("error"), int(time.time()), context["id"]),
            )
            self.work._event(
                db,
                context["org"],
                context["program"],
                None,
                {
                    "type": "source_retrieval",
                    "request_id": context["id"],
                    "tool": tool,
                    "state": "failed" if failed else "complete",
                    "result_sha256": checksum,
                },
            )
        if refused:
            raise refused
        return {"result": result, "sha256": checksum, "replayed": False}

    async def execute(self, credential, job_id, tool, body):
        context = await asyncio.to_thread(self._begin, credential, job_id, tool, body)
        if "replay" in context:
            return context["replay"]
        try:
            fetched = await self._retrieve(context["org"], context["project"], tool, body)
        except Exception as exc:
            # Only authored refusal reasons are retained; other exception text stays private.
            reason = {"reason": exc.code, "message": exc.explanation()} if isinstance(exc, SourceRefused) else None
            return await asyncio.to_thread(self._finish, credential, job_id, tool, body, context, None, True, reason)
        return await asyncio.to_thread(self._finish, credential, job_id, tool, body, context, fetched, False)
