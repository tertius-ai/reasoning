"""MCP server over the ledger service.

Started with `reasoning-ledger mcp`; speaks MCP over stdio, so an agent
harness config is just:

    {"command": "reasoning-ledger", "args": ["mcp"],
     "env": {"REASONING_LEDGER_ROOT": "<project>/ledger", ...}}

The shipped usage instructions become the server's MCP instructions, and
every tool wraps the same service functions as the CLI.
"""

from __future__ import annotations

import os
from importlib import resources

from . import service
from .agents.bibliography import BibliographyError, BibliographyStore
from .store import LedgerStore, LedgerStoreError
from .tasks import TASK_STATUSES, GoalError, GoalStore, TaskError, TaskStore

# Distinguishes "argument omitted" from "explicitly set to null" in the
# refine tools, so a refine can clear a reference by passing null.
_UNSET_SENTINEL = "__unset__"


def _instructions() -> str:
    return (
        resources.files("reasoning_ledger") / "prompts" / "instructions.md"
    ).read_text(encoding="utf-8")


def build_server(
    store: LedgerStore,
    author: dict,
    *,
    bibliography_store: BibliographyStore | None = None,
    project_id: str | None = None,
    chat_id: str | None = None,
):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - broken install
        raise RuntimeError(
            "MCP support requires the 'mcp' package (a core dependency — "
            "reinstall with: uv pip install -e ."
        ) from exc

    from .organization_ledger import linked_ledger

    organization = linked_ledger(store.root)
    instructions = (
        (resources.files("reasoning_ledger") / "prompts" / "organization-ledger.md").read_text(encoding="utf-8")
        if organization is not None else _instructions()
    )
    server = FastMCP("reasoning-ledger", instructions=instructions)
    # One tracker per server process = per agent (each chat and each
    # subagent runs its own MCP server): the revisions this agent has
    # actually read, driving pin-to-last-read, refine's CAS, and status's
    # statement dedup. The harness bumps the epoch file when codex
    # compacts the conversation; the tracker then forgets everything,
    # since compaction dropped those statements from the agent's context.
    observations = service.RevisionObservations(
        epoch_path=os.environ.get("REASONING_COMPACTION_EPOCH_PATH"),
        persist_path=os.environ.get("REASONING_OBSERVATIONS_PATH"),
    )

    def mirror_reference(reference: dict) -> dict:
        if bibliography_store is None or not project_id:
            return reference
        try:
            item, created = bibliography_store.mirror_ledger_reference(
                reference,
                project_id=project_id,
                chat_id=chat_id,
            )
        except (BibliographyError, OSError, ValueError) as error:
            return {
                **reference,
                "library_warning": f"ledger saved; Library mirror failed: {error}",
            }
        return {
            **reference,
            "bibliography_id": item["bibliography_id"],
            "library_created": created,
        }

    @server.tool(
        description=(
            "Assert a new claim or definition (kind: claim|definition). For a "
            "result from the literature, pass reference_id (from "
            "ledger_reference) and optionally locator (the spot in the "
            "source, e.g. 'Thm 1.19'); attesting the reference is what "
            "establishes it. Statements must be self-contained TeX prose, "
            "one result per claim."
        )
    )
    def ledger_claim(
        statement: str,
        kind: str = "claim",
        reference_id: str | None = None,
        locator: str | None = None,
        notes: str | None = None,
    ) -> dict:
        return service.claim_payload(
            store,
            statement,
            kind=kind,
            reference_id=reference_id,
            locator=locator,
            notes=notes,
            author=author,
            observations=observations,
        )

    @server.tool(
        description=(
            "Restate a claim as a new revision (the old revision and its "
            "records are kept; dependent proofs become stale, visibly). "
            "reference_id and locator are left unchanged unless passed. "
            "Refused with the current statement if the claim moved since "
            "you last read it — reconcile and retry."
        )
    )
    def ledger_refine(
        claim_id: str,
        statement: str,
        reference_id: str | None = _UNSET_SENTINEL,
        locator: str | None = _UNSET_SENTINEL,
        notes: str | None = None,
    ) -> dict:
        kwargs: dict = {"notes": notes, "author": author, "observations": observations}
        if reference_id is not _UNSET_SENTINEL:
            kwargs["reference_id"] = reference_id
        if locator is not _UNSET_SENTINEL:
            kwargs["locator"] = locator
        return service.refine_payload(store, claim_id, statement, **kwargs)

    @server.tool(
        description=(
            "Record a bibliographic reference and return its reference_id "
            "(pass to ledger_claim). author and title are required; year, "
            "arxiv, url, doi (non-arXiv only), isbn, and notes are optional. "
            "Identical references dedupe to one id. Warns if no "
            "arxiv/url/doi/isbn is given. Search first with "
            "ledger_search_references to avoid near-duplicates. `file` "
            "names a cached copy of the source in the project's sources "
            "directory (path relative to it) — set it when a copy exists "
            "so citation reviewers can actually read the source."
        )
    )
    def ledger_reference(
        author: str,
        title: str,
        year: int | None = None,
        arxiv: str | None = None,
        url: str | None = None,
        doi: str | None = None,
        isbn: str | None = None,
        notes: str | None = None,
        file: str | None = None,
    ) -> dict:
        return mirror_reference(
            service.reference_payload(
                store,
                author=author,
                title=title,
                year=year,
                arxiv=arxiv,
                url=url,
                doi=doi,
                isbn=isbn,
                notes=notes,
                file=file,
            )
        )

    @server.tool(
        description=(
            "Correct/augment a reference as a new revision (e.g. add the DOI "
            "you found later); every claim citing it follows automatically. "
            "Pass only the fields to change."
        )
    )
    def ledger_refine_reference(
        reference_id: str,
        author: str | None = _UNSET_SENTINEL,
        title: str | None = _UNSET_SENTINEL,
        year: int | None = _UNSET_SENTINEL,
        arxiv: str | None = _UNSET_SENTINEL,
        url: str | None = _UNSET_SENTINEL,
        doi: str | None = _UNSET_SENTINEL,
        isbn: str | None = _UNSET_SENTINEL,
        notes: str | None = _UNSET_SENTINEL,
        file: str | None = _UNSET_SENTINEL,
    ) -> dict:
        changes = {
            key: value
            for key, value in {
                "author": author,
                "title": title,
                "year": year,
                "arxiv": arxiv,
                "url": url,
                "doi": doi,
                "isbn": isbn,
                "notes": notes,
                "file": file,
            }.items()
            if value is not _UNSET_SENTINEL
        }
        return mirror_reference(
            service.refine_reference_payload(store, reference_id, changes)
        )

    @server.tool(description="List all bibliographic references.")
    def ledger_references() -> dict:
        return service.references_payload(store)

    @server.tool(
        description=(
            "Case-insensitive substring search over reference fields "
            "(author, title, identifiers). Search before adding a reference."
        )
    )
    def ledger_search_references(text: str) -> dict:
        return service.search_references_payload(store, text)

    @server.tool(
        description=(
            "Attach a proof to a claim. argument = prose explaining the proof "
            "(for a decomposition: why the premises suffice). premises pin "
            "existing claims ({claim_id, revision_id?} — when omitted, the "
            "revision YOU last read, so your argument pins what it was "
            "written against; pinning a claim you have never read is "
            "refused with its statement, and the response warns if a "
            "premise moved since you read it) or prose placeholders "
            "({unresolved: ...}) to backfill "
            "later. new_premises creates conjecture claims from statements and "
            "pins them in one call. artifacts optionally attach formal backing "
            "(lean/computation)."
        )
    )
    def ledger_prove(
        claim_id: str,
        argument: str,
        premises: list[dict] | None = None,
        new_premises: list[str] | None = None,
        artifacts: list[dict] | None = None,
        notes: str | None = None,
    ) -> dict:
        return service.prove_payload(
            store,
            claim_id,
            argument,
            premises=premises,
            new_premises=new_premises,
            artifacts=artifacts,
            notes=notes,
            author=author,
            observations=observations,
        )

    @server.tool(
        description=(
            "Revise a proof: new argument text, target claim revision, and/or "
            "premise pins. Passing only premises is a rebase/backfill keeping "
            "the same body. Repoint the target only after checking that the "
            "same argument proves the new claim revision."
        )
    )
    def ledger_revise_proof(
        proof_id: str,
        argument: str | None = None,
        premises: list[dict] | None = None,
        target_revision_id: str | None = None,
        notes: str | None = None,
    ) -> dict:
        return service.revise_proof_payload(
            store,
            proof_id,
            argument=argument,
            premises=premises,
            target_revision_id=target_revision_id,
            notes=notes,
            author=author,
            observations=observations,
        )

    @server.tool(
        description=(
            "Record a NEGATIVE or neutral verification, permanently: flawed "
            "or inconclusive against a proof (pass proof_id), refuted "
            "(counterexample; response includes the blast radius) against a "
            "claim revision. Positive verdicts (verified / established) are "
            "not yours to record — request an independent review with "
            "ledger_request_review. revision_id defaults to current. evidence "
            "must contain a nonempty string kind, for example "
            '{"kind": "proof-review", "text": "the substantive report"}.'
        )
    )
    def ledger_verify(
        verdict: str,
        authority: str,
        evidence: dict,
        claim_id: str | None = None,
        proof_id: str | None = None,
        revision_id: str | None = None,
    ) -> dict:
        if verdict in ("verified", "established"):
            raise LedgerStoreError(
                "positive verdicts come only from independent review: call "
                "ledger_request_review on the proof (or the cited claim) instead"
            )
        return service.verify_payload(
            store,
            verdict=verdict,
            authority=authority,
            evidence=evidence,
            claim_id=claim_id,
            proof_id=proof_id,
            revision_id=revision_id,
            author=author,
        )

    @server.tool(
        description=(
            "Request an independent adversarial review (async). Pass proof_id "
            "to have the proof's argument reviewed against its pinned "
            "premises (taken as givens), or claim_id to have a cited claim's "
            "citation checked — or, for a definition with no reference, to "
            "have the definition vetted for well-posedness and undefined or "
            "smuggled notation. A fresh reviewer with no session context reads "
            "the ledger records only — your framing never reaches it — and "
            "its verdict lands as a verification when it finishes (watch "
            "ledger_status / ledger_reviews). This is the only way a proof "
            "becomes verified or a citation attested. The proof must be "
            "self-contained modulo its premises: the reviewer takes the "
            "premises as given and judges everything else."
        )
    )
    def ledger_request_review(
        claim_id: str | None = None, proof_id: str | None = None
    ) -> dict:
        from .review import request_review

        state = request_review(
            store, claim_id=claim_id, proof_id=proof_id, requested_by=author
        )
        return {
            "review_id": state["review_id"],
            "status": state["status"],
            "subject": state["subject"],
            "model": state["model"],
            "note": (
                "async: end your turn if you have nothing else to do — do not "
                "wait or poll for this. The verdict is delivered as a "
                "verification when done, waking a fresh turn if yours has ended."
            ),
        }

    @server.tool(
        description=(
            "Request an independent review of a standalone writeup — a note "
            "or short paper you are producing for the user to read on its "
            "own. Pass the full document text. A fresh reviewer (same model "
            "as proof review) checks it for correctness, mathematical-paper "
            "style, self-containment, and the absence of out-of-place matter "
            "(references to previous versions, the ledger/chat, TODOs). "
            "Async, like other reviews; the feedback reaches you when it "
            "finishes (also in ledger_reviews). The feedback is ADVISORY: you "
            "have the user's context, so you may accept or override each "
            "point at your discretion — but if you revise the document in "
            "response, review the new version again. document_name (the "
            "file's artifacts path, e.g. 'artifacts/note.tex') is required "
            "and identifies the document: reviews of different documents "
            "run concurrently, but only one review per document at a time — "
            "a request for a document already under review is refused, and "
            "if you modified it since, cancel the stale review "
            "(ledger_cancel_review) and request a fresh one."
        )
    )
    def ledger_request_writeup_review(document: str, document_name: str) -> dict:
        from .review import request_writeup_review

        state = request_writeup_review(
            store,
            document=document,
            document_name=document_name,
            requested_by=author,
        )
        return {
            "review_id": state["review_id"],
            "status": state["status"],
            "note": (
                "async, advisory: end your turn rather than waiting — do not "
                "poll for this. The feedback is delivered when done, waking a "
                "fresh turn if yours has ended."
            ),
        }

    @server.tool(
        description=(
            "Cancel any pending or running independent review — proof, "
            "citation, or writeup. Use it when a review has gone stale: a "
            "proof review after you revised the proof, or a writeup review "
            "whose document you have modified since requesting it (cancel "
            "and request a fresh one; only one writeup review per document "
            "runs at a time). The workbench kills the reviewer shortly "
            "after."
        )
    )
    def ledger_cancel_review(review_id: str) -> dict:
        from .review import cancel_review

        return {"review": cancel_review(store.root, review_id)}

    @server.tool(
        description=(
            "Start a writer subagent: a persistent session that turns ledger "
            "results into ONE standalone document (mathematical-paper style, "
            "self-contained, readable, with a genuine introduction). Only "
            "use this when the user explicitly asked for a writeup. Pass a "
            "brief (goals, intended audience, and any background for the "
            "introduction — the writer verifies it independently), the "
            "document's artifacts path (a fresh .tex target is prefilled "
            "with the standard preamble), and the ledger ids of the results "
            "to include. Exactly the listed entries are handed to the "
            "writer — no transitive closure (a deep result's closure can "
            "exceed its context window) — so curate the list yourself: "
            "every claim, definition, and lemma the document must state "
            "or use. "
            "Async like reviews: end your turn; the writer wakes you when "
            "it returns and then stays PAUSED, holding its context — its "
            "writer_id is your handle for writer_continue / "
            "writer_terminate. When it returns, request a writeup review of "
            "the document; fix any ledger-level flaws the review surfaces "
            "in the ledger itself; then continue the writer with the review "
            "and a new brief, iterating until the review is clear or its "
            "remaining objections are ones you judge irrelevant — then "
            "terminate the writer."
        )
    )
    def writer_start(brief: str, filename: str, slugs: list) -> dict:
        from .writer import request_writer

        state = request_writer(
            store,
            brief=brief,
            filename=filename,
            slugs=[str(s) for s in slugs],
            requested_by=author,
        )
        return {
            "writer_id": state["writer_id"],
            "status": state["status"],
            "filename": f"artifacts/{state['filename']}",
            "note": (
                "async: end your turn if you have nothing else to do — the "
                "writer's return wakes you, and it then waits paused on "
                "this writer_id."
            ),
        }

    @server.tool(
        description=(
            "Reactivate a paused (or failed) writer on the same document, "
            "with its full context retained. Pass the new brief — what to "
            "change, which review objections matter, which you overrode and "
            "why — and review_id to hand it the verbatim output of that "
            "writeup review first. Fix ledger-level flaws in the ledger "
            "BEFORE continuing the writer, so it writes from corrected "
            "material."
        )
    )
    def writer_continue(
        writer_id: str, brief: str, review_id: str | None = None
    ) -> dict:
        from .writer import continue_writer

        state = continue_writer(
            store.root, writer_id, brief=brief, review_id=review_id
        )
        return {
            "writer_id": state["writer_id"],
            "status": state["status"],
            "activation": state["activation"],
            "note": "async: the writer wakes you when this activation returns.",
        }

    @server.tool(
        description=(
            "Terminate a writer once its document is done — the review came "
            "back clear, or its remaining objections are ones you judged "
            "irrelevant. Also kills a running writer (via the workbench)."
        )
    )
    def writer_terminate(writer_id: str) -> dict:
        from .writer import terminate_writer

        return {"writer": terminate_writer(store.root, writer_id)}

    @server.tool(
        description=(
            "List writer states: running, paused (report included — the "
            "writer's account of what it wrote), failed, terminated."
        )
    )
    def writer_list() -> dict:
        from .writer import load_writers

        return {
            "writers": [
                {
                    key: value
                    for key, value in state.items()
                    if key not in ("steps", "live")
                }
                for state in load_writers(store.root)
            ]
        }

    @server.tool(
        description=(
            "Review states. Without arguments: in-flight reviews plus the "
            "most recent settled verdicts, without report bodies. With "
            "review_id: that one review in full, report included — use "
            "this to read a finished review's outcome. A failed review "
            "can simply be requested again."
        )
    )
    def ledger_reviews(review_id: str | None = None) -> dict:
        from .review import load_review, review_digest

        if review_id:
            state = load_review(store.root, review_id)
            if state is None:
                raise LedgerStoreError(f"unknown review: {review_id}")
            return state
        return review_digest(store.root)

    tasks_path = os.environ.get("REASONING_TASKS_PATH")
    if tasks_path:
        task_store = TaskStore(tasks_path)

        @server.tool(
            description=(
                "Your working task list for this chat, shown to the user. "
                "Call with `tasks` to replace the whole list (send the full "
                "desired state every time — one entry per subtask, each "
                f"{{title, status}} with status one of {', '.join(TASK_STATUSES)}); "
                "call with no argument to read the current list. Keep a list "
                "for any multi-step piece of work and keep statuses current "
                "as you go — exactly one task in_progress at a time is ideal. "
                "Pass an empty list to clear it once the work is done or the "
                "plan is abandoned. This is scratch planning, not the ledger."
            )
        )
        def tasks(tasks: list | None = None) -> dict:
            if tasks is not None:
                try:
                    return {"tasks": task_store.set(tasks)}
                except TaskError as error:
                    raise LedgerStoreError(str(error)) from error
            return {"tasks": task_store.load()}

    goal_path = os.environ.get("REASONING_GOAL_PATH")
    if goal_path:
        goal_store = GoalStore(goal_path)

        @server.tool(
            description=(
                "The chat's single persistent goal, shown to the user "
                "beneath the task list. Set it whenever the user asks you "
                "for a specific goal or result. There is only ever one "
                "goal, and it must not change unless the user directs you "
                "to a new one: setting while a goal exists is an error — "
                "you must explicitly clear the old goal first "
                "(`clear=true`), and clearing is allowed only when new "
                "instructions from the user have made the goal irrelevant "
                "or superseded it. Mark it when its state changes: "
                "'completed' once achieved; 'impossible' ONLY when "
                "impossibility is actually proved (cite the proof in "
                "`reason`) — mere difficulty never qualifies; 'blocked' "
                "ONLY when nothing further can be done without the user's "
                "input (name exactly what you need in `reason`, e.g. the "
                "upload of a paywalled source). If your turn ends with "
                "the goal set but unmarked, the harness wakes you to "
                "continue it. Marks resolve at the next user message: "
                "completed/impossible clear the goal, blocked becomes "
                "active again. Main conversation only — subagents must "
                "not touch the goal. No arguments reads the current goal."
            )
        )
        def goal(
            set: str | None = None,
            mark: str | None = None,
            reason: str | None = None,
            clear: bool = False,
        ) -> dict:
            try:
                if clear:
                    goal_store.clear()
                    return {"goal": None}
                if set is not None:
                    return {"goal": goal_store.set(set)}
                if mark is not None:
                    return {"goal": goal_store.mark(mark, reason)}
            except GoalError as error:
                raise LedgerStoreError(str(error)) from error
            return {"goal": goal_store.load()}

    @server.tool(
        description="Withdraw a claim you no longer stand behind (restore=true undoes)."
    )
    def ledger_withdraw(claim_id: str, restore: bool = False) -> dict:
        return service.withdraw_payload(store, claim_id, restore=restore)

    @server.tool(
        description="Abandon a proof attempt (kept in history; resume=true undoes)."
    )
    def ledger_abandon(proof_id: str, resume: bool = False) -> dict:
        return service.abandon_payload(store, proof_id, resume=resume)

    @server.tool(
        description=(
            "Derived status. With claim_id: one claim's current revision, "
            "its live proofs with bodies, and dependency summaries "
            "(history=true adds superseded revision texts, abandoned=true "
            "abandoned proofs, dependents=true the reverse edges — every "
            "claim whose live proof rests on this one, with the citing "
            "proof and premise pin state). Without: the working set — "
            "statuses, "
            "stale/broken proofs, unresolved premises to backfill, "
            "contradictions, cycles — plus every claim's id and status; "
            "statement texts are included only for claims you have not "
            "been shown at their current revision (statements='all' "
            "resends everything). In a large project the newest claims' "
            "statements are included up to a response budget; read older "
            "ones as needed via ledger_search or per-claim status. Call "
            "this at session start."
        )
    )
    def ledger_status(
        claim_id: str | None = None,
        history: bool = False,
        abandoned: bool = False,
        dependents: bool = False,
        statements: str | None = None,
    ) -> dict:
        return service.status_payload(
            store,
            claim_id,
            observations=observations,
            history=history,
            abandoned=abandoned,
            dependents=dependents,
            statements=statements,
            statement_budget=service.STATEMENT_BUDGET,
        )

    @server.tool(
        description=(
            "What loses establishment if this claim revision were refuted, "
            "plus proofs already leaning on a stale revision of it."
        )
    )
    def ledger_blast_radius(claim_id: str, revision_id: str | None = None) -> dict:
        return service.blast_payload(store, claim_id, revision_id)

    @server.tool(
        description=(
            "Why a claim's status is degraded: every unestablished "
            "dependency in its support closure with the concrete reasons — "
            "unverified or flawed arguments, stale/broken/unresolved "
            "premises, unattested citations, bare conjectures — and the "
            "dependency path to each. Use it to find what to fix, or which "
            "leaf to attribute when the user says to take a result (GRH, a "
            "known fact) as given."
        )
    )
    def ledger_blockers(claim_id: str) -> dict:
        return service.blockers_payload(store, claim_id)

    @server.tool(
        description=(
            "Case-insensitive substring search over current claim statements. "
            "Search before claiming to avoid near duplicates. Returns up to "
            "`limit` full summaries, newest first; further matches come back "
            "as ids only."
        )
    )
    def ledger_search(text: str, limit: int = 20) -> dict:
        return service.search_payload(
            store, text, observations=observations, limit=limit
        )

    @server.tool(
        description=(
            "Referential-integrity check over the ledger tree (every pin, "
            "head, target, and verification subject must resolve). Run after "
            "any git merge of the ledger."
        )
    )
    def ledger_fsck() -> dict:
        return store.fsck()

    # Every ledger tool a batch may dispatch to. The session-scoped
    # tasks/goal tools stay out: they are workbench plumbing, not ledger
    # operations.
    batchable = {
        func.__name__: func
        for func in (
            ledger_claim,
            ledger_refine,
            ledger_reference,
            ledger_refine_reference,
            ledger_references,
            ledger_search_references,
            ledger_prove,
            ledger_revise_proof,
            ledger_verify,
            ledger_request_review,
            ledger_request_writeup_review,
            ledger_cancel_review,
            ledger_reviews,
            ledger_withdraw,
            ledger_abandon,
            ledger_status,
            ledger_blast_radius,
            ledger_blockers,
            ledger_search,
            ledger_fsck,
            writer_start,
            writer_continue,
            writer_terminate,
            writer_list,
        )
    }

    @server.tool(
        description=(
            "Run several ledger operations in one call. Pass operations as "
            '[{"tool": "ledger_...", "arguments": {...}}, ...]; they run in '
            "order and the response lists one result per operation. A "
            "failed operation reports its error in place and the rest still "
            "run. Use it to avoid round-trips on independent operations — "
            "e.g. requesting reviews of several proofs at once, or "
            "recording a batch of claims."
        )
    )
    def ledger_batch(operations: list) -> dict:
        results = []
        for op in operations:
            name = (op or {}).get("tool")
            func = batchable.get(name)
            if func is None:
                results.append(
                    {
                        "tool": name,
                        "error": f"unknown or non-batchable tool: {name!r} "
                        f"(batchable: {', '.join(sorted(batchable))})",
                    }
                )
                continue
            try:
                results.append(
                    {"tool": name, "result": func(**(op.get("arguments") or {}))}
                )
            except Exception as error:  # per-op: one failure never aborts the rest
                results.append({"tool": name, "error": str(error)})
        return {"results": results}

    if organization is not None:
        from .agents.organization_tools import register_organization_ledger_tools
        from .agents.tools import ToolRegistry

        for handler in (
            ledger_claim, ledger_refine, ledger_reference, ledger_refine_reference, ledger_references,
            ledger_search_references, ledger_prove, ledger_revise_proof, ledger_verify, ledger_request_review,
            ledger_request_writeup_review, ledger_cancel_review, ledger_reviews, ledger_withdraw, ledger_abandon,
            ledger_status, ledger_blast_radius, ledger_blockers, ledger_search, ledger_fsck, ledger_batch,
            writer_start, writer_continue, writer_terminate, writer_list,
        ):
            server.remove_tool(handler.__name__)
        registry = ToolRegistry()
        register_organization_ledger_tools(registry, organization)
        for definition in registry.list():
            server.add_tool(definition.handler, name=definition.name, description=definition.description)
    return server


def serve(
    store: LedgerStore,
    author: dict,
    *,
    bibliography_store: BibliographyStore | None = None,
    project_id: str | None = None,
    chat_id: str | None = None,
) -> None:
    build_server(
        store,
        author,
        bibliography_store=bibliography_store,
        project_id=project_id,
        chat_id=chat_id,
    ).run()
