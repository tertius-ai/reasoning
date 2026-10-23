"""Permission-scoped mathematical tools shared by the API and beta harness."""

from __future__ import annotations

import copy
import inspect
import json

from .. import service
from ..agents.tools import ToolDefinition
from ..canonical import canonical_json
from ..records import LedgerRecordError
from ..store import LedgerStoreError
from .mathematical_store import MathematicalStore
from .store import StoreError

OPERATIONS = {
    "ledger_status": service.status_payload,
    "ledger_search": service.search_payload,
    "ledger_graph": service.graph_payload,
    "ledger_blockers": service.blockers_payload,
    "ledger_blast_radius": service.blast_payload,
    "ledger_claim": service.claim_payload,
    "ledger_refine": service.refine_payload,
    "ledger_prove": service.prove_payload,
    "ledger_revise_proof": service.revise_proof_payload,
    "ledger_reference": service.reference_payload,
    "ledger_refine_reference": service.refine_reference_payload,
    "ledger_references": service.references_payload,
    "ledger_search_references": service.search_references_payload,
    "ledger_withdraw": service.withdraw_payload,
    "ledger_abandon": service.abandon_payload,
    "ledger_verify": service.verify_payload,
}
READS = {
    "ledger_status",
    "ledger_search",
    "ledger_graph",
    "ledger_blockers",
    "ledger_blast_radius",
    "ledger_references",
    "ledger_search_references",
    "ledger_read",
}
DESCRIPTIONS = {
    "ledger_status": "Inspect derived mathematical status, exact revisions, stale/broken proofs, cycles and contradictions.",
    "ledger_search": "Search mathematical claims before recording new results; returns precise statements and revisions.",
    "ledger_graph": "Read the mathematical dependency graph, not conversation summaries.",
    "ledger_blockers": "Find the exact obligations preventing a claim from being established.",
    "ledger_blast_radius": "Inspect mathematical dependents before revising or withdrawing a claim.",
    "ledger_claim": "Record one self-contained claim or definition. No approval needed; not certified. "
    "reference_id must be an existing ref_ ID returned by ledger_reference, never a URL or citation string. "
    "Create/search the reference first, or omit reference_id and retain attribution in notes.",
    "ledger_refine": "Create an immutable claim revision. You must have read its current revision; stale writes are refused.",
    "ledger_prove": "Record a proof attempt after reading its current target and premise revisions. "
    "A stale target is refused with its exact current statement; reconcile it and retry. This does not verify the proof.",
    "ledger_revise_proof": "Create an immutable revision of a proof after reading it; preserve its dependencies.",
    "ledger_reference": "Record a deduplicated scholarly reference with exact source identifiers; claims cite its returned ID.",
    "ledger_refine_reference": "Correct or augment a scholarly reference using immutable revisions.",
    "ledger_references": "List scholarly references accessible to this research session.",
    "ledger_search_references": "Search existing scholarly references before creating a duplicate.",
    "ledger_withdraw": "Withdraw or restore a mathematical claim without deleting its history.",
    "ledger_abandon": "Abandon or resume a proof attempt without deleting its history.",
    "ledger_verify": "Record a flaw, inconclusive check, or counterexample. Authors cannot issue positive or machine verdicts. "
    'evidence needs a nonempty kind, for example {"kind": "proof-review", "text": "the substantive report"}.',
}
# Stored verifications require a nonempty evidence.kind (schemas/verification.schema.json);
# further evidence fields are retained as given.
EVIDENCE_SCHEMA = {
    "type": "object",
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "minLength": 1, "maxLength": 200,
                 "description": "Evidence category, for example proof-review, counterexample or computation."},
        "text": {"type": "string", "maxLength": 32000, "description": "The substantive report."},
    },
    "description": 'For example {"kind": "proof-review", "text": "the substantive report"}.',
}


def operation_schema(function):
    """Derive names/defaults from the shared service, with bounded JSON types."""
    strings = {
        "claim_id",
        "proof_id",
        "reference_id",
        "reference_revision_id",
        "revision_id",
        "target_revision_id",
        "statement",
        "argument",
        "text",
        "notes",
        "locator",
        "author",
        "title",
        "arxiv",
        "url",
        "doi",
        "isbn",
        "file",
    }
    booleans = {"history", "abandoned", "restore", "resume", "dependents"}
    objects = {"changes", "evidence"}
    arrays = {"premises", "new_premises", "artifacts"}
    properties, required = {}, []
    for name, parameter in inspect.signature(function).parameters.items():
        if name in {"store", "observations"} or name == "author" and function != service.reference_payload:
            continue
        if name in strings:
            field = {"type": "string", "maxLength": 32000 if name in {"statement", "argument", "notes"} else 1000}
        elif name in booleans:
            field = {"type": "boolean"}
        elif name == "evidence":
            field = copy.deepcopy(EVIDENCE_SCHEMA)
        elif name in objects:
            field = {"type": "object"}
        elif name in arrays:
            field = {"type": "array", "maxItems": 100, "items": {"type": "string"} if name == "new_premises" else {"type": "object"}}
        elif name == "kind":
            field = {"enum": ["claim", "definition"]}
        elif name == "statements":
            field = {"enum": ["all"]}
        elif name == "authority":
            field = {"enum": ["review"]}
        elif name == "verdict":
            field = {"enum": ["flawed", "inconclusive", "refuted"]}
        else:
            field = {"type": "integer", "minimum": 1, "maximum": 100000}
        if parameter.default is None:
            field = {"anyOf": [field, {"type": "null"}]}
        properties[name] = field
        if name == "reference_id" and function == service.claim_payload:
            field["description"] = "Existing ref_ ID from ledger_reference; not a free-form citation."
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


class MathematicalLedger:
    def __init__(self, store):
        self.store = store

    def execute(
        self,
        user,
        org,
        name,
        arguments,
        *,
        client_id=None,
        project=None,
        topic=None,
        agent=False,
        run_id=None,
        observations=None,
        bot_invocation=None,
        read_scope=False,
    ):
        if name not in OPERATIONS and name != "ledger_read":
            raise StoreError(400, "unknown mathematical operation")
        write = name not in READS
        identity = canonical_json({"name": name, "arguments": arguments, "project": project, "topic": topic})
        with self.store.transaction(write=write) as db:
            bot = None
            if bot_invocation is not None:
                from .bot_runtime import BotRuntime
                from .bot_store import BotStore

                bot, run = BotRuntime(BotStore(self.store)).leased(db, *bot_invocation)
                if (user, org, project, topic) != (bot["created_by"], run["organization_id"], run["project_id"], run["topic_id"]):
                    raise StoreError(403, "bot mathematical operation is outside its invocation")
                if ("knowledge:write" if write else "knowledge:read") not in json.loads(bot["scopes_json"]):
                    raise StoreError(403, "bot lacks mathematical knowledge access")
                policy = self.store._topic_access(db, user, org, topic)[4]["knowledge_policy_json"]
                if ("bibliography" if "reference" in name else "ledger") not in json.loads(policy):
                    raise StoreError(403, "topic no longer grants this mathematical collection")
            if topic:
                access = self.store._topic_access(db, user, org, topic, write=write)
                if access[4]["kind"] == "alpha" or project is not None and project != access[1]["id"]:
                    raise StoreError(404, "mathematical research scope unavailable")
                project = access[1]["id"]
            elif project:
                self.store._project_access(db, user, org, project, write=write)
            ledger = MathematicalStore(self.store, db, user, org, project=project, topic=topic, agent=agent)
            window = None
            targeted = name == "ledger_read" or any(arguments.get(key) for key in ("claim_id", "proof_id", "reference_id"))
            if read_scope is not False and not write and topic:
                from .research_scope import KnowledgeWindow

                def source_access(connection, source_topic):
                    if bot is not None:
                        runtime = BotRuntime(BotStore(self.store))
                        runtime.access(connection, bot, source_topic)
                        self.store._topic_access(connection, run["requested_by"], org, source_topic)

                window = KnowledgeWindow(self.store, db, user, org, topic, "project" if targeted else read_scope,
                                         topic_authorize=source_access if bot is not None else None)
                ledger = window.base() if targeted else window.ledger()
            if write:
                if not client_id or len(client_id) > 200:
                    raise StoreError(400, "mathematical writes require a stable client identity")
                replay = db.execute(
                    "SELECT * FROM mathematical_requests WHERE organization_id=? AND actor_id=? AND client_id=?", (org, user, client_id)
                ).fetchone()
                if replay:
                    if replay["arguments_json"] != identity:
                        raise StoreError(409, "mathematical request identity was reused with different arguments")
                    # Permission is rechecked even for successful retries.
                    self.store._require(db, user, org, write=True)
                    result = json.loads(replay["result_json"])
                    for field in ("claim_id", "proof_id", "reference_id"):
                        if result.get(field):
                            target = db.execute(
                                "SELECT * FROM mathematical_records WHERE organization_id=? AND object_id=? AND record_type LIKE '%-head'",
                                (org, result[field]),
                            ).fetchone()
                            if target is None or not ledger._visible(target):
                                raise StoreError(404, "mathematical record unavailable")
                    return result
            try:
                if name == "ledger_read":
                    view = ledger.load()
                    identity = arguments["ledger_id"]
                    if identity in view.claim_heads:
                        result = service.status_payload(
                            ledger, identity, observations=observations, history=bool(arguments.get("history", False))
                        )
                    else:
                        record = next(
                            (
                                records[identity]
                                for records in (
                                    view.claim_revisions,
                                    view.proof_heads,
                                    view.proof_revisions,
                                    view.reference_heads,
                                    view.reference_revisions,
                                    view.verifications,
                                )
                                if identity in records
                            ),
                            None,
                        )
                        if record is None:
                            archived = db.execute(
                                "SELECT * FROM mathematical_records WHERE organization_id=? AND object_id=? AND record_type='verification'",
                                (org, identity),
                            ).fetchone()
                            if archived is None or not ledger._visible(archived):
                                raise LedgerStoreError("mathematical record unavailable")
                            result = {
                                "record": json.loads(archived["body_json"]),
                                "trust_state": archived["trust_state"],
                                "counts_as_evidence": False,
                            }
                            return result
                        result = {"record": record}
                        if identity in view.reference_heads:
                            result["revision"] = view.reference_revisions[record["current_revision_id"]]
                            if observations:
                                observations.note(identity, record["current_revision_id"])
                        if observations and identity in view.reference_revisions:
                            observations.note(record["reference_id"], record["revision_id"])
                        if identity in view.proof_heads:
                            result["revision"] = view.proof_revisions[record["current_revision_id"]]
                            if observations:
                                observations.note(identity, record["current_revision_id"])
                        if observations and identity in view.claim_revisions:
                            observations.note(record["claim_id"], record["revision_id"])
                        if observations and identity in view.proof_revisions:
                            observations.note(record["proof_id"], record["revision_id"])
                else:
                    function = OPERATIONS[name]
                    values = dict(arguments)
                    if name == "ledger_prove" and observations is not None:
                        claim_id = values["claim_id"]
                        head = ledger.claim_head(claim_id)
                        if observations.get(claim_id) != head["current_revision_id"]:
                            result = service._stale_refusal(ledger, claim_id, observations)
                            result["message"] = (
                                "The proof target changed or has not been read. Its exact current "
                                "statement follows; check that your argument proves this revision, then retry."
                            )
                            return result
                    if name == "ledger_refine_reference" and observations is not None:
                        head = ledger.reference_head(values["reference_id"])
                        if observations.get(values["reference_id"]) != head["current_revision_id"]:
                            return {"refused": "stale-reference-read", "message": "Read the current reference before revising it."}
                    if name == "ledger_revise_proof" and observations is not None:
                        head = ledger.proof_head(values["proof_id"])
                        if observations.get(values["proof_id"]) != head["current_revision_id"]:
                            current = ledger.load().proof_revisions[head["current_revision_id"]]
                            observations.note(values["proof_id"], head["current_revision_id"])
                            return {
                                "refused": "stale-proof-read",
                                "proof_id": values["proof_id"],
                                "current_revision_id": head["current_revision_id"],
                                "revision": current,
                                "message": "The exact current proof follows. "
                                "Reconcile its argument, target and dependencies before retrying.",
                            }
                    if name == "ledger_verify" and (
                        values.get("verdict") not in {"flawed", "inconclusive", "refuted"} or values.get("authority") != "review"
                    ):
                        raise StoreError(403, "authors cannot issue positive or machine-authority verdicts")
                    if name == "ledger_verify" and (problem := service.evidence_problem(values.get("evidence"))):
                        raise StoreError(400, "ledger_verify " + problem)
                    if agent and name == "ledger_status":
                        values.setdefault("statement_budget", 8000)
                    if agent and name == "ledger_search":
                        values.setdefault("limit", 20)
                    signature = inspect.signature(function).parameters
                    if "observations" in signature:
                        values["observations"] = observations
                    if "author" in signature and name != "ledger_reference":
                        values["author"] = {
                            "operator": user,
                            "agent": "bot/" + bot["id"] if bot is not None else "organization-beta" if agent else None,
                            "session": topic or project or org,
                        }
                    result = function(ledger, **values)
                    if window and not targeted:
                        result = window.bounded(result)
                    if observations and result.get("proof_id") and result.get("revision_id"):
                        observations.note(result["proof_id"], result["revision_id"])
                    if observations and result.get("reference_id") and result.get("revision_id"):
                        observations.note(result["reference_id"], result["revision_id"])
                    if observations and name in {"ledger_references", "ledger_search_references"}:
                        for reference in result.get("references", result.get("matches", [])):
                            observations.note(reference["reference_id"], reference["revision_id"])
                if name == "ledger_read" or name == "ledger_status" and arguments.get("claim_id"):
                    from .ledger_human_checks import HumanChecks

                    subject = result.get("record", result)
                    object_id = subject.get("proof_id") or subject.get("reference_id") or subject.get("claim_id")
                    revision_id = subject.get("current_revision_id") or subject.get("revision_id")
                    if object_id and revision_id:
                        result["human_checks"] = HumanChecks.for_revision(db, org, object_id, revision_id)
                if write and not result.get("error") and not result.get("refused"):
                    db.execute(
                        "INSERT INTO mathematical_requests VALUES(?,?,?,?,?)", (org, user, client_id, identity, canonical_json(result))
                    )
            except (LedgerStoreError, LedgerRecordError, KeyError, TypeError, ValueError) as exc:
                raise StoreError(409, str(exc)[:1000]) from exc
            if window:
                result = window.note_sources(result)
        return result

    def register(self, research):
        references = {"ledger_reference", "ledger_refine_reference", "ledger_references", "ledger_search_references"}
        for name, function in OPERATIONS.items():
            if ("bibliography" if name in references else "ledger") not in research.collections:
                continue

            def handler(_name=name, **arguments):
                read_scope = arguments.pop("scope", None) if _name in READS else False
                if _name == "ledger_claim" and arguments.get("reference_id"):
                    with self.store.transaction() as db:
                        ledger = MathematicalStore(
                            self.store, db, research.user, research.org, project=research.project, topic=research.topic, agent=True
                        )
                        if arguments["reference_id"] not in ledger.reference_heads():
                            raise StoreError(
                                400,
                                "unknown-reference-id: create or search the reference with "
                                "ledger_reference/ledger_search_references, "
                                "then use its returned ref_ ID; alternatively omit reference_id and put the citation in notes",
                            )
                client = research.row["id"] + ":" + str(research.sequence)
                return self.execute(
                    research.user,
                    research.org,
                    _name,
                    arguments,
                    client_id=client,
                    project=research.project,
                    topic=research.topic,
                    agent=True,
                    run_id=research.row["id"],
                    observations=research.observations,
                    read_scope=read_scope,
                )

            inputs = operation_schema(function)
            if name in READS:
                inputs["properties"]["scope"] = {"enum": ["working_set", "topic", "project"],
                    "description": "Default follows the topic working set; project explicitly broadens authorized retrieval."}
            research.registry.register(
                ToolDefinition(name, DESCRIPTIONS[name], inputs, handler, risk="read" if name in READS else "write")
            )
        aliases = {
            "bibliography_search": "ledger_search_references",
            "bibliography_add": "ledger_reference",
        }
        for alias, target in aliases.items():
            if "bibliography" not in research.collections:
                break
            definition = research.registry.get(target)
            research.registry.register(
                ToolDefinition(alias, definition.description, definition.input_schema, definition.handler, risk=definition.risk)
            )
        research.registry.tool(
            name="ledger_read",
            description="Read an exact claim, proof, reference, revision or verification by its canonical ID.",
            input_schema={
                "type": "object",
                "properties": {"ledger_id": {"type": "string", "maxLength": 200}},
                "required": ["ledger_id"],
                "additionalProperties": False,
            },
        )(self.read_handler(research))
        if "bibliography" in research.collections:
            research.registry.tool(
                name="bibliography_get",
                description="Read the exact current bibliographic revision by reference ID.",
                input_schema={
                    "type": "object",
                    "properties": {"reference_id": {"type": "string", "maxLength": 200}},
                    "required": ["reference_id"],
                    "additionalProperties": False,
                },
            )(lambda reference_id: self.read_handler(research)(reference_id))
            research.registry.tool(
                name="bibliography_export",
                description="Export accessible scholarly references as bounded BibTeX.",
                input_schema={"type": "object", "properties": {"scope": {"enum": ["working_set", "topic", "project"]}},
                              "additionalProperties": False},
            )(lambda scope=None: self.export(research, scope))

    def read_handler(self, research):
        def read(ledger_id):
            collection = "bibliography" if ledger_id.startswith(("ref_", "refrev_")) else "ledger"
            if collection not in research.collections:
                raise StoreError(403, "this mathematical collection is not granted")
            return self.execute(
                research.user,
                research.org,
                "ledger_read",
                {"ledger_id": ledger_id},
                project=research.project,
                topic=research.topic,
                agent=True,
                observations=research.observations,
                read_scope="project",
            )

        return read

    def export(self, research, scope=None):
        from ..agents.bibliography import _bibtex_escape

        refs = self.execute(
            research.user, research.org, "ledger_references", {}, project=research.project, topic=research.topic, agent=True,
            read_scope=scope,
        )["references"]
        entries = []
        for reference in refs[:100]:
            fields = [(key, reference.get(key)) for key in ("author", "title", "year", "doi", "url", "isbn")]
            if reference.get("arxiv"):
                fields += [("eprint", reference["arxiv"]), ("archivePrefix", "arXiv")]
            body = ",\n".join(f"  {key} = {{{_bibtex_escape(value)}}}" for key, value in fields if value is not None)
            entries.append("@misc{" + reference["reference_id"] + ",\n" + body + "\n}")
        return {"format": "bibtex", "text": "\n\n".join(entries), "truncated": len(refs) > 100, "total_references": len(refs)}
