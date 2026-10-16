"""Shared operation layer: the same functions behind the CLI and MCP server.

Every function takes a LedgerStore and returns a JSON-serializable dict.
This is where model-facing sugar lives: premises pin the current revision
when none is given, `new_premises` creates conjecture claims inline during
decomposition, and a refutation response carries the blast radius computed
before the refutation lands.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading

from . import latex_check
from .derive import LedgerView, blast_radius
from .records import _UNSET as _RECORDS_UNSET
from .records import REFERENCE_FIELDS
from .store import LedgerStaleReadError, LedgerStore, LedgerStoreError


def _tex_or_none(value: object) -> str | None:
    """A field value to validate, or None to skip it (covers the refine
    tools' _UNSET sentinel and non-string values)."""
    return value if isinstance(value, str) else None


def _reject_bad_tex(**fields: str | None) -> None:
    """Reject the write if any field carries LaTeX KaTeX cannot render.

    The ledger is a rendered record: everything committed to it (except
    verification logs, handled separately) must display for the reader.
    A missing checker degrades to no-op, so a Node-less environment can
    still commit."""
    error = latex_check.check_fields(fields)
    if error is not None:
        raise LedgerStoreError(f"rejected: {error}")


class RevisionObservations:
    """The claim revisions one agent has actually had in front of it.

    One instance per agent process (each chat's and each subagent's MCP
    server holds its own). A claim is "observed" at a revision when its
    statement text passed through a payload to this agent: status,
    search, its own writes, or a premise warning that quoted the newer
    statement. Concurrency fixes hang off this: premises pin the
    observed revision, refine refuses to replace wording the agent never
    saw, and the status payload omits statements the agent already has.

    `epoch_path` ties the record to the agent's actual context: the
    harness bumps that file when codex compacts the conversation (which
    discards tool outputs), and the next access here forgets everything —
    what left the agent's context is no longer observed.

    `persist_path` carries the record across process restarts: the codex
    thread resumes with its context intact, so what was observed must
    not reset with the server. The harness also pre-writes this file
    (ownerless) with what the fresh-chat overview injection delivered,
    which the adopting server picks up like any other prior state.
    Every agent in a codex session gets its own MCP server with the same
    environment, so the path is claimed by the first server to actually
    touch the ledger — the main agent's session-start status call; a
    subagent's server (spawned mid-turn, later) finds a live owner and
    stays memory-only, which suffices: subagent threads never resume,
    so their record has nothing to survive. Ownership is pid + process
    start time, so a dead owner's file is adopted, never a live one's."""

    def __init__(
        self,
        epoch_path: object = None,
        persist_path: object = None,
    ) -> None:
        self._seen: dict[str, str] = {}
        self._mutex = threading.Lock()
        self._epoch_path = str(epoch_path) if epoch_path else None
        self._epoch = self._read_epoch()
        self._persist_path = str(persist_path) if persist_path else None
        self._claimed = persist_path is None

    def _read_epoch(self) -> str | None:
        if self._epoch_path is None:
            return None
        try:
            with open(self._epoch_path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return None

    @staticmethod
    def _proc_start(pid: int) -> int | None:
        """The process's kernel start time, or None if it is not
        running — (pid, start) identifies a process across pid reuse."""
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                after_comm = handle.read().rsplit(b")", 1)[1].split()
            return int(after_comm[19])  # starttime, field 22 overall
        except (OSError, ValueError, IndexError):
            pass

        # macOS and procfs-less Linux expose the process start through ps.
        try:
            result = subprocess.run(
                ["ps", "-ww", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            started = result.stdout.strip()
            if result.returncode == 0 and started:
                digest = hashlib.sha256(started.encode("utf-8")).digest()
                return int.from_bytes(digest[:8], "big")
        except (OSError, subprocess.SubprocessError):
            pass

        # Restricted sandboxes may block process inspection even though
        # existence checks work. Conservatively treat that PID as live;
        # this can delay adoption but never lets two agents write the record.
        try:
            os.kill(pid, 0)
            return pid
        except PermissionError:
            return pid
        except (ProcessLookupError, OSError):
            return None

    @staticmethod
    def _owner_alive(data: object) -> bool:
        owner = data.get("owner") if isinstance(data, dict) else None
        if not isinstance(owner, dict) or owner.get("pid") == os.getpid():
            return False
        pid, start = owner.get("pid"), owner.get("start")
        return (
            isinstance(pid, int)
            and start is not None
            and RevisionObservations._proc_start(pid) == start
        )

    def _claim_persist(self) -> None:
        """With the mutex held, on first ledger use: adopt the record if
        its owner is dead or absent (this is the main agent's server —
        possibly after a restart, possibly finding the harness's
        ownerless overview pre-write), or stay memory-only when a live
        owner holds it (this is a subagent's server; subagent threads
        never resume, so there is nothing to persist)."""
        if self._claimed:
            return
        self._claimed = True
        data: dict = {}
        try:
            with open(self._persist_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            data = {}
        if self._owner_alive(data):
            self._persist_path = None
            return
        seen = data.get("seen") if isinstance(data, dict) else None
        if isinstance(seen, dict):
            for cid, rid in seen.items():
                if isinstance(cid, str) and isinstance(rid, str):
                    self._seen.setdefault(cid, rid)
        self._write_persist()

    def _write_persist(self) -> None:
        if self._persist_path is None:
            return
        pid = os.getpid()
        payload = {
            "owner": {"pid": pid, "start": self._proc_start(pid)},
            "seen": self._seen,
        }
        try:
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self._persist_path)
        except OSError:
            pass

    def _sync(self) -> None:
        """With the mutex held: claim/adopt the persisted record on
        first use, and forget everything if the harness bumped the
        compaction epoch since the last access."""
        self._claim_persist()
        if self._epoch_path is not None:
            current = self._read_epoch()
            if current != self._epoch:
                self._epoch = current
                self._seen.clear()
                self._write_persist()

    def note(self, claim_id: str, revision_id: str) -> None:
        with self._mutex:
            self._sync()
            if self._seen.get(claim_id) != revision_id:
                self._seen[claim_id] = revision_id
                self._write_persist()

    def get(self, claim_id: str) -> str | None:
        with self._mutex:
            self._sync()
            return self._seen.get(claim_id)


def _reference_summary(view: LedgerView, reference_id: str | None, revision_id: str | None = None) -> dict | None:
    """The pinned or current revision's bibliographic fields for a cited reference,
    or None when the claim cites nothing / the reference is missing."""
    if reference_id is None:
        return None
    head = view.reference_heads.get(reference_id)
    if head is None:
        return None
    revision = view.reference_revisions.get(revision_id or head["current_revision_id"])
    if revision is None or revision["reference_id"] != reference_id:
        return None
    return {
        "reference_id": reference_id,
        "revision_id": revision["revision_id"],
        # .get: revisions recorded before a field existed lack the key
        **{field: revision.get(field) for field in REFERENCE_FIELDS},
    }


def _claim_summary(view: LedgerView, claim_id: str, status: dict) -> dict:
    revision = view.claim_revisions[status["current_revision_id"]]
    return {
        "claim_id": claim_id,
        "kind": revision["kind"],
        "statement_text": revision["statement_text"],
        "reference_id": revision["reference_id"],
        "locator": revision["locator"],
        "reference": _reference_summary(view, revision["reference_id"], revision.get("reference_revision_id")),
        "status": status["status"],
        "authority": status["authority"],
        "current_revision_id": status["current_revision_id"],
    }


_REFERENCE_ID_KEYS = ("arxiv", "url", "doi", "isbn")


def _reference_warnings(fields: dict) -> list[str]:
    warnings = []
    if not any(fields.get(key) for key in _REFERENCE_ID_KEYS):
        warnings.append(
            "no stable identifier (arxiv/url/doi/isbn) — add one so the source "
            "can be located and mechanically resolved"
        )
    doi = fields.get("doi") or ""
    if "10.48550/arxiv" in doi.lower() or "arxiv" in doi.lower():
        warnings.append(
            "this looks like an arXiv DOI — put the arXiv id in the arxiv "
            "field and reserve doi for non-arXiv DOIs"
        )
    return warnings


def reference_payload(
    store: LedgerStore,
    *,
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
    _reject_bad_tex(author=author, title=title, notes=notes)
    fields = {
        "author": author,
        "title": title,
        "year": year,
        "arxiv": arxiv,
        "url": url,
        "doi": doi,
        "isbn": isbn,
        "notes": notes,
        "file": file,
    }
    head, revision = store.create_reference(**fields)
    payload = {
        "reference_id": head["reference_id"],
        "revision_id": revision["revision_id"],
        # .get: revisions recorded before a field existed lack the key
        **{field: revision.get(field) for field in REFERENCE_FIELDS},
    }
    warnings = _reference_warnings(fields)
    if warnings:
        payload["warnings"] = warnings
    return payload


def refine_reference_payload(store: LedgerStore, reference_id: str, changes: dict) -> dict:
    _reject_bad_tex(
        author=changes.get("author"),
        title=changes.get("title"),
        notes=changes.get("notes"),
    )
    revision = store.refine_reference(reference_id, changes)
    merged = {field: revision.get(field) for field in REFERENCE_FIELDS}
    payload = {
        "reference_id": revision["reference_id"],
        "revision_id": revision["revision_id"],
        "parent_revision_id": revision["parent_revision_id"],
        **merged,
    }
    warnings = _reference_warnings(merged)
    if warnings:
        payload["warnings"] = warnings
    return payload


def references_payload(store: LedgerStore) -> dict:
    view = store.load()
    refs = [
        _reference_summary(view, reference_id)
        for reference_id in sorted(view.reference_heads)
    ]
    return {"references": refs}


def search_references_payload(store: LedgerStore, text: str) -> dict:
    view = store.load()
    needle = text.casefold()
    matches = []
    for reference_id in sorted(view.reference_heads):
        summary = _reference_summary(view, reference_id)
        haystack = " ".join(
            str(summary[field]) for field in REFERENCE_FIELDS if summary[field]
        ).casefold()
        if needle in haystack:
            matches.append(summary)
    return {"matches": matches}


def claim_payload(
    store: LedgerStore,
    statement: str,
    *,
    kind: str = "claim",
    reference_id: str | None = None,
    reference_revision_id: str | None = None,
    locator: str | None = None,
    notes: str | None = None,
    author: dict | None = None,
    observations: RevisionObservations | None = None,
) -> dict:
    _reject_bad_tex(statement=statement, notes=notes, locator=locator)
    head, revision = store.create_claim(
        statement,
        kind=kind,
        reference_id=reference_id,
        reference_revision_id=reference_revision_id,
        locator=locator,
        notes=notes,
        author=author,
    )
    if observations is not None:
        observations.note(head["claim_id"], revision["revision_id"])
    return {
        "claim_id": head["claim_id"],
        "revision_id": revision["revision_id"],
        "statement_text": revision["statement_text"],
        "reference_id": revision["reference_id"],
        "locator": revision["locator"],
    }


def refine_payload(
    store: LedgerStore,
    claim_id: str,
    statement: str,
    *,
    reference_id: str | None | object = _RECORDS_UNSET,
    locator: str | None | object = _RECORDS_UNSET,
    notes: str | None = None,
    author: dict | None = None,
    observations: RevisionObservations | None = None,
) -> dict:
    """Refine a claim; with observations, a compare-and-set.

    Refining wording one has not read replaces another agent's statement
    sight-unseen, so when an observation tracker is present the write is
    refused unless the agent has seen the current revision. The refusal
    carries the newest statement — which counts as reading it, so an
    informed retry goes through."""
    _reject_bad_tex(statement=statement, notes=notes, locator=_tex_or_none(locator))
    expected = None
    if observations is not None:
        expected = observations.get(claim_id)
        if expected is None:
            return _stale_refusal(store, claim_id, observations)
    try:
        revision = store.refine(
            claim_id,
            statement,
            reference_id=reference_id,
            locator=locator,
            notes=notes,
            author=author,
            expected_revision_id=expected,
        )
    except LedgerStaleReadError:
        return _stale_refusal(store, claim_id, observations)
    if observations is not None:
        observations.note(claim_id, revision["revision_id"])
    return {
        "claim_id": revision["claim_id"],
        "revision_id": revision["revision_id"],
        "parent_revision_id": revision["parent_revision_id"],
        "statement_text": revision["statement_text"],
    }


def _stale_refusal(
    store: LedgerStore, claim_id: str, observations: RevisionObservations | None
) -> dict:
    head = store.claim_head(claim_id)
    current = store.claim_revision(claim_id, head["current_revision_id"])
    if observations is not None:
        # The refusal quotes the current statement: it has now been read.
        observations.note(claim_id, current["revision_id"])
    return {
        "refused": "stale-read",
        "message": (
            "the claim's current revision is newer than the one you last "
            "read; its statement follows — refine again if it still needs it"
        ),
        "claim_id": claim_id,
        "current_revision_id": current["revision_id"],
        "statement_text": current["statement_text"],
    }


def _resolve_premises(
    store: LedgerStore,
    premises: list[dict] | None,
    observations: RevisionObservations | None = None,
) -> tuple[list[dict], list[str]]:
    """Pin premises. Without an explicit revision_id, the pin is the
    revision this agent last observed — the statement its argument was
    actually written against. With an observation tracker, a premise the
    agent never read cannot be pinned blind: it comes back in the second
    element for the caller to refuse on. Without a tracker (operator
    CLI) the pin falls back to the current revision."""
    resolved: list[dict] = []
    unread: list[str] = []
    for premise in premises or []:
        if "unresolved" in premise:
            resolved.append(premise)
            continue
        revision_id = premise.get("revision_id")
        if revision_id is None:
            if observations is not None:
                revision_id = observations.get(premise["claim_id"])
                if revision_id is None:
                    unread.append(premise["claim_id"])
                    continue
            else:
                revision_id = store.claim_head(premise["claim_id"])[
                    "current_revision_id"
                ]
        resolved.append(
            {
                "claim_id": premise["claim_id"],
                "revision_id": revision_id,
                "note": premise.get("note"),
            }
        )
    return resolved, unread


def _unread_refusal(
    store: LedgerStore,
    claim_ids: list[str],
    observations: RevisionObservations | None,
) -> dict:
    entries = []
    for claim_id in claim_ids:
        head = store.claim_head(claim_id)
        current = store.claim_revision(claim_id, head["current_revision_id"])
        if observations is not None:
            # Quoting the statements is the read; a retry now pins them.
            observations.note(claim_id, current["revision_id"])
        entries.append(
            {
                "claim_id": claim_id,
                "current_revision_id": current["revision_id"],
                "statement_text": current["statement_text"],
            }
        )
    return {
        "refused": "unread-premises",
        "message": (
            "these premises were pinned without having been read; their "
            "current statements follow — check your argument actually rests "
            "on them and retry"
        ),
        "premises": entries,
    }


def _premise_warnings(
    store: LedgerStore,
    resolved: list[dict],
    observations: RevisionObservations | None,
) -> list[dict]:
    """Warn about pins the head has moved past; quoting the newer
    statement counts as the agent reading it."""
    warnings = []
    for premise in resolved:
        if "unresolved" in premise:
            continue
        head = store.claim_head(premise["claim_id"])
        current_id = head["current_revision_id"]
        if current_id == premise["revision_id"]:
            continue
        current = store.claim_revision(premise["claim_id"], current_id)
        warnings.append(
            {
                "claim_id": premise["claim_id"],
                "pinned_revision_id": premise["revision_id"],
                "current_revision_id": current_id,
                "current_statement_text": current["statement_text"],
                "note": (
                    "this premise was refined after you last read it; your "
                    "proof is pinned to the revision you saw and will show "
                    "stale — rebase onto the current statement if it still "
                    "serves"
                ),
            }
        )
        if observations is not None:
            observations.note(premise["claim_id"], current_id)
    return warnings


def prove_payload(
    store: LedgerStore,
    claim_id: str,
    argument: str,
    *,
    premises: list[dict] | None = None,
    new_premises: list[str] | None = None,
    artifacts: list[dict] | None = None,
    notes: str | None = None,
    author: dict | None = None,
    observations: RevisionObservations | None = None,
) -> dict:
    _reject_bad_tex(argument=argument, notes=notes)
    for i, statement in enumerate(new_premises or []):
        _reject_bad_tex(**{f"new premise {i + 1}": statement})
    resolved, unread = _resolve_premises(store, premises, observations)
    if unread:
        # Refused before any side effect (new_premises are not created).
        return _unread_refusal(store, unread, observations)
    created = []
    for statement in new_premises or []:
        piece_head, piece_rev = store.create_claim(statement, author=author)
        if observations is not None:
            observations.note(piece_head["claim_id"], piece_rev["revision_id"])
        created.append(
            {
                "claim_id": piece_head["claim_id"],
                "revision_id": piece_rev["revision_id"],
                "statement_text": piece_rev["statement_text"],
            }
        )
        resolved.append(
            {
                "claim_id": piece_head["claim_id"],
                "revision_id": piece_rev["revision_id"],
                "note": None,
            }
        )
    body = {"kind": "argument", "text": argument, "format": "tex-prose-v1"}
    head, revision = store.add_proof(
        claim_id,
        body,
        premises=resolved,
        artifacts=artifacts,
        notes=notes,
        author=author,
    )
    payload = {"proof_id": head["proof_id"], "revision_id": revision["revision_id"]}
    if created:
        payload["new_claims"] = created
    warnings = _premise_warnings(store, resolved, observations)
    if warnings:
        payload["premise_warnings"] = warnings
    return payload


def revise_proof_payload(
    store: LedgerStore,
    proof_id: str,
    *,
    argument: str | None = None,
    premises: list[dict] | None = None,
    target_revision_id: str | None = None,
    notes: str | None = None,
    author: dict | None = None,
    observations: RevisionObservations | None = None,
) -> dict:
    _reject_bad_tex(argument=argument, notes=notes)
    body = (
        {"kind": "argument", "text": argument, "format": "tex-prose-v1"}
        if argument is not None
        else None
    )
    resolved = None
    if premises is not None:
        resolved, unread = _resolve_premises(store, premises, observations)
        if unread:
            return _unread_refusal(store, unread, observations)
    revision = store.revise_proof(
        proof_id,
        body=body,
        premises=resolved,
        target_revision_id=target_revision_id,
        notes=notes,
        author=author,
    )
    payload = {
        "proof_id": revision["proof_id"],
        "revision_id": revision["revision_id"],
        "parent_revision_id": revision["parent_revision_id"],
    }
    if resolved is not None:
        warnings = _premise_warnings(store, resolved, observations)
        if warnings:
            payload["premise_warnings"] = warnings
    return payload


EVIDENCE_EXAMPLE = '{"kind": "proof-review", "text": "the substantive report"}'


def evidence_problem(evidence) -> str | None:
    """Explain a verification evidence object that storage would refuse."""
    if not isinstance(evidence, dict):
        return "evidence must be an object, for example " + EVIDENCE_EXAMPLE
    kind = evidence.get("kind")
    if not isinstance(kind, str) or not kind:
        return "evidence.kind must be a nonempty string, for example " + EVIDENCE_EXAMPLE
    return None


def verify_payload(
    store: LedgerStore,
    *,
    verdict: str,
    authority: str,
    evidence: dict,
    claim_id: str | None = None,
    proof_id: str | None = None,
    revision_id: str | None = None,
    author: dict | None = None,
) -> dict:
    problem = evidence_problem(evidence)
    if problem:
        raise LedgerStoreError(problem)
    if proof_id is not None:
        head = store.proof_head(proof_id)
        subject = {
            "claim_id": head["claim_id"],
            "proof_id": proof_id,
            "revision_id": revision_id or head["current_revision_id"],
        }
    else:
        if claim_id is None:
            raise LedgerStoreError("verification needs a claim_id or proof_id")
        head = store.claim_head(claim_id)
        subject = {
            "claim_id": claim_id,
            "revision_id": revision_id or head["current_revision_id"],
        }
    payload = {}
    if verdict == "refuted":
        # Computed before the refutation lands, so it reports what falls.
        payload["blast_radius"] = blast_payload(
            store, subject["claim_id"], subject["revision_id"]
        )
    record = store.add_verification(
        subject, verdict=verdict, authority=authority, evidence=evidence, author=author
    )
    payload["verification_id"] = record["verification_id"]
    payload["verdict"] = record["verdict"]
    payload["subject"] = record["subject"]
    return payload


def withdraw_payload(store: LedgerStore, claim_id: str, *, restore: bool = False) -> dict:
    head = store.set_withdrawn(claim_id, not restore)
    return {"claim_id": head["claim_id"], "withdrawn": head["withdrawn"]}


def abandon_payload(store: LedgerStore, proof_id: str, *, resume: bool = False) -> dict:
    head = store.set_abandoned(proof_id, not resume)
    return {"proof_id": head["proof_id"], "abandoned": head["abandoned"]}


# Statement characters one model-bound status response may carry
# (~30K tokens): large enough that most projects fit in one delivery,
# small enough that a 900-claim project can never blow the context
# window or trip the transport's silent tool-output truncation.
STATEMENT_BUDGET = 120_000


def status_payload(
    store: LedgerStore,
    claim_id: str | None = None,
    observations: RevisionObservations | None = None,
    *,
    history: bool = False,
    abandoned: bool = False,
    dependents: bool = False,
    statements: str | None = None,
    statement_budget: int | None = None,
) -> dict:
    """The working set (project-wide) or one claim's detail.

    Project-wide, statement texts are the bulk of the payload, and most
    of the time the agent already has them: with `observations` present,
    statements the agent has been shown at their current revision —
    delivered by a payload or written by the agent itself — collapse to
    a slim entry, unless `statements="all"` forces a resend. Per-claim,
    `history` adds the superseded revision texts and `abandoned` the
    abandoned proofs — both excluded by default.

    `statement_budget` caps the serialized claim entries one response
    may carry. In a large project the full map cannot be delivered at
    once — it can exceed the model's entire context window, and an
    oversized tool result is silently truncated by the transport while
    everything in it would be marked as read. Newest claims are
    delivered first; older ones over the budget are listed slim and NOT
    marked read (targeted reads or repeat calls still reach them).
    Deliveries into a model context (the MCP server, the chat-start
    overview) always pass a budget; the browser and CLI read
    unbudgeted."""
    view = store.load()
    state = store.derived(view)
    if claim_id:
        if claim_id not in state.claim_status:
            raise LedgerStoreError(f"unknown claim: {claim_id}")
        status = state.claim_status[claim_id]
        if observations is not None:
            observations.note(claim_id, status["current_revision_id"])
        if history:
            revisions = sorted(
                (
                    rev
                    for rev in view.claim_revisions.values()
                    if rev["claim_id"] == claim_id
                ),
                key=lambda rev: rev["revision_id"],
            )
        else:
            revisions = [view.claim_revisions[status["current_revision_id"]]]
        proofs = {}
        for proof_id, report in state.proof_reports.items():
            if report["claim_id"] != claim_id:
                continue
            if report["abandoned"] and not abandoned:
                continue
            # The detail view carries the proof body so a reviewer can
            # actually read the argument being verified.
            proofs[proof_id] = {
                **report,
                "body": view.proof_revisions[report["revision_id"]]["body"],
            }
        # First-order dependencies: every claim a live proof of this one
        # directly rests on, deduped across proofs.
        dep_ids: list[str] = []
        for _proof_id, report in sorted(proofs.items()):
            if report["abandoned"]:
                continue
            for premise in report["premises"]:
                pid = premise.get("claim_id")
                if pid and pid in state.claim_status and pid not in dep_ids:
                    dep_ids.append(pid)
        if observations is not None:
            # Dependency summaries carry their statements too.
            for pid in dep_ids:
                observations.note(
                    pid, state.claim_status[pid]["current_revision_id"]
                )
        detail = {
            **_claim_summary(view, claim_id, status),
            "withdrawn": view.claim_heads[claim_id]["withdrawn"],
            "dependencies": [
                _claim_summary(view, pid, state.claim_status[pid])
                for pid in dep_ids
            ],
            "revisions": [
                {
                    "revision_id": rev["revision_id"],
                    "parent_revision_id": rev["parent_revision_id"],
                    "statement_text": rev["statement_text"],
                    "refuted": rev["revision_id"] in state.refuted_revisions,
                    "established": rev["revision_id"] in state.established_revisions,
                }
                for rev in revisions
            ],
            "proofs": proofs,
        }
        if dependents:
            # Reverse edges, on request: every live proof of another
            # claim resting on this one. Slim (no statement texts — a
            # foundational claim can have hundreds of dependents).
            detail["dependents"] = [
                {
                    "claim_id": report["claim_id"],
                    "status": state.claim_status[report["claim_id"]]["status"],
                    "proof_id": proof_id,
                    "premise_state": premise["state"],
                }
                for proof_id, report in sorted(state.proof_reports.items())
                if not report["abandoned"] and report["claim_id"] != claim_id
                for premise in report["premises"]
                if premise.get("claim_id") == claim_id
            ]
        return detail
    budgeted = statement_budget is not None
    claims = []
    claims_omitted: dict[str, list[str]] = {}
    already_shown = 0
    over_budget = 0
    budget_left = statement_budget
    included_any = False
    # Newest first: what was recorded most recently is what current work
    # builds on, so it lands inside a budgeted delivery (and at the top
    # of the browser's list); journal-less claims sort last.
    order = store.claim_order(view)
    for one_claim_id, status in sorted(
        state.claim_status.items(),
        key=lambda item: (-order.get(item[0], -1), item[0]),
    ):
        current = status["current_revision_id"]
        unseen = (
            statements == "all"
            or observations is None
            or observations.get(one_claim_id) != current
        )
        include = unseen
        summary = None
        if include:
            summary = _claim_summary(view, one_claim_id, status)
            if budgeted:
                # The bibliographic join is served once by the top-level
                # references list, not repeated per claim.
                summary.pop("reference", None)
            if budget_left is not None:
                cost = len(json.dumps(summary, ensure_ascii=False))
                # The first entry always goes through, even alone over
                # budget: every call must make progress.
                if cost > budget_left and included_any:
                    include = False
                    over_budget += 1
                else:
                    budget_left -= cost
        if include:
            included_any = True
            claims.append(summary)
            if observations is not None:
                observations.note(one_claim_id, current)
        else:
            if not unseen:
                already_shown += 1
            # Omitted claims collapse to ids grouped by status: at
            # thousands of claims, per-claim objects are the payload's
            # biggest fixed cost.
            claims_omitted.setdefault(status["status"], []).append(one_claim_id)
    counts: dict[str, int] = {}
    for status in state.claim_status.values():
        counts[status["status"]] = counts.get(status["status"], 0) + 1
    stale_proofs = sorted(
        proof_id
        for proof_id, report in state.proof_reports.items()
        if not report["abandoned"]
        and (
            not report["target_fresh"]
            or any(p["state"] == "stale" for p in report["premises"])
        )
    )
    broken_proofs = sorted(
        proof_id
        for proof_id, report in state.proof_reports.items()
        if not report["abandoned"]
        and any(p["state"] == "broken" for p in report["premises"])
    )
    unresolved = [
        {
            "proof_id": proof_id,
            "claim_id": report["claim_id"],
            "needs": [
                p["unresolved"] for p in report["premises"] if p["state"] == "unresolved"
            ],
        }
        for proof_id, report in sorted(state.proof_reports.items())
        if not report["abandoned"]
        and any(p["state"] == "unresolved" for p in report["premises"])
    ]
    truncated: dict[str, int] = {}
    if budgeted:
        # A big project mid-rework can have hundreds of stale proofs and
        # unresolved premises; a model-bound response carries a bounded
        # sample with the totals, details per claim/proof on request.
        if len(stale_proofs) > 50:
            truncated["stale_proofs"] = len(stale_proofs)
            stale_proofs = stale_proofs[:50]
        if len(broken_proofs) > 50:
            truncated["broken_proofs"] = len(broken_proofs)
            broken_proofs = broken_proofs[:50]
        if len(unresolved) > 30:
            truncated["unresolved_premises"] = len(unresolved)
            unresolved = unresolved[:30]
    from .review import active_reviews

    reviews = [
        {
            "review_id": review["review_id"],
            "subject": review["subject"],
            "status": review["status"],
            "verdict": review.get("verdict"),
            "started_at": review.get("started_at"),
        }
        for review in active_reviews(store.root)
        if review.get("status") in ("pending", "running")
    ]
    references = [
        _reference_summary(view, reference_id)
        for reference_id in sorted(view.reference_heads)
    ]
    if budgeted:
        # The full bibliographic records live behind ledger_references;
        # a model-bound status carries the identifying fields only.
        references = [
            {
                key: reference[key]
                for key in ("reference_id", "author", "title", "year")
                if key in reference
            }
            for reference in references
            if reference
        ]
    payload = {
        "counts": counts,
        "claims": claims,
        "stale_proofs": stale_proofs,
        "broken_proofs": broken_proofs,
        "unresolved_premises": unresolved,
        "contradictions": state.contradictions,
        "cycles": state.cycles,
        "reviews_running": reviews,
        "references": references,
    }
    if claims_omitted:
        payload["claims_omitted"] = claims_omitted
    if truncated:
        payload["truncated"] = {
            name: f"{total} total, first {len(payload[name])} listed"
            for name, total in truncated.items()
        }
    notes = []
    if already_shown:
        notes.append(
            f"{already_shown} claims you have already been shown appear "
            "only as ids under claims_omitted (statements='all' resends)"
        )
    if over_budget:
        notes.append(
            f"{over_budget} older statements deferred to keep this "
            "response within budget (ids under claims_omitted) — read "
            "the ones you need via ledger_search or per-claim "
            "ledger_status"
        )
    if notes:
        payload["statements_omitted"] = "; ".join(notes)
    return payload


def blockers_payload(store: LedgerStore, claim_id: str) -> dict:
    """Why a claim's status is degraded: its own local defects plus
    every unestablished dependency in its support closure, each with
    the reasons in ITS terms — so the responsible leaves (the claims to
    prove, cite, review, or have the user establish) are visible at a
    glance."""
    view = store.load()
    if claim_id not in view.claim_heads:
        raise LedgerStoreError(f"unknown claim: {claim_id}")
    state = store.derived(view)

    def local(cid: str) -> tuple[list[str], dict, list[str]]:
        """(claim-level reasons, per-proof issues, premise ids to chase)."""
        head = view.claim_heads[cid]
        current = head["current_revision_id"]
        revision = view.claim_revisions[current]
        reasons: list[str] = []
        deeper: list[str] = []
        if head["withdrawn"]:
            reasons.append("withdrawn")
        if current in state.refuted_revisions:
            reasons.append(
                "refuted — a counterexample stands against the current revision"
            )
        proofs: dict[str, list[str]] = {}
        for proof_id, report in sorted(state.proof_reports.items()):
            if report["claim_id"] != cid or report["abandoned"]:
                continue
            issues: list[str] = []
            if not report["target_fresh"]:
                issues.append(
                    "stale: targets a superseded revision of the claim "
                    "(rebase with ledger_revise_proof)"
                )
            if report["argument"] == "unchecked":
                issues.append(
                    "argument not yet verified — request "
                    f"ledger_request_review(proof_id={proof_id!r})"
                )
            elif report["argument"] == "flawed":
                issues.append("argument flawed (see its verification record)")
            for premise in report["premises"]:
                if premise["state"] == "unresolved":
                    issues.append(
                        f"unresolved premise {premise['unresolved']!r} — "
                        "backfill it"
                    )
                elif premise["state"] == "broken":
                    issues.append(
                        f"premise {premise['claim_id']} broken "
                        "(withdrawn or refuted)"
                    )
                elif premise["state"] == "stale":
                    issues.append(
                        f"premise {premise['claim_id']} pinned at a "
                        "superseded revision"
                    )
                if not premise["satisfied"] and premise.get("claim_id"):
                    premise_status = state.claim_status.get(
                        premise["claim_id"], {}
                    ).get("status", "?")
                    issues.append(
                        f"premise {premise['claim_id']} not established "
                        f"({premise_status})"
                    )
                    deeper.append(premise["claim_id"])
            proofs[proof_id] = issues
        if not proofs:
            if revision.get("reference_id"):
                reasons.append(
                    "citation not attested — request "
                    f"ledger_request_review(claim_id={cid!r}) (upload the "
                    "source first if it is paywalled)"
                )
            elif revision["kind"] == "definition":
                reasons.append(
                    "definition not yet vetted — request "
                    f"ledger_request_review(claim_id={cid!r})"
                )
            else:
                reasons.append(
                    "no proof and no reference — prove it, cite it, or "
                    "have the user establish it by fiat"
                )
        return reasons, proofs, deeper

    root_status = state.claim_status[claim_id]
    if view.claim_heads[claim_id]["current_revision_id"] in state.established_revisions:
        return {
            "claim_id": claim_id,
            "status": root_status["status"],
            "established": True,
            "blocking": [],
        }
    entries: list[dict] = []
    seen: set[str] = set()
    queue: list[tuple[str, list[str]]] = [(claim_id, [])]
    while queue:
        cid, via = queue.pop(0)
        if cid in seen:
            continue
        seen.add(cid)
        if (
            view.claim_heads[cid]["current_revision_id"]
            in state.established_revisions
        ):
            continue
        reasons, proofs, deeper = local(cid)
        entries.append(
            {
                "claim_id": cid,
                "status": state.claim_status.get(cid, {}).get("status"),
                # the dependency path from the asked-about claim
                "via": via,
                "reasons": reasons,
                "proofs": proofs,
            }
        )
        for dependency in deeper:
            queue.append((dependency, [*via, cid]))
    return {
        "claim_id": claim_id,
        "status": root_status["status"],
        "established": False,
        "blocking": entries,
    }


def blast_payload(
    store: LedgerStore, claim_id: str, revision_id: str | None = None
) -> dict:
    view = store.load()
    if claim_id not in view.claim_heads:
        raise LedgerStoreError(f"unknown claim: {claim_id}")
    return blast_radius(view, claim_id, revision_id)


def graph_payload(store: LedgerStore) -> dict:
    """The ledger as a dependency graph: claims as nodes, premise pins of
    current, non-abandoned proofs as edges (dependent -> premise). Each
    edge carries the worst premise state across the proofs that use it;
    unresolved premises are reported as per-claim counts (they have no
    target node)."""
    view = store.load()
    state = store.derived(view)
    nodes = [
        _claim_summary(view, claim_id, status)
        for claim_id, status in sorted(state.claim_status.items())
    ]
    severity = {"current": 1, "stale": 2, "broken": 3}
    edges: dict[tuple[str, str], str] = {}
    unresolved: dict[str, int] = {}
    for report in state.proof_reports.values():
        if report["abandoned"]:
            continue
        for premise in report["premises"]:
            if premise["state"] == "unresolved":
                unresolved[report["claim_id"]] = (
                    unresolved.get(report["claim_id"], 0) + 1
                )
                continue
            key = (report["claim_id"], premise["claim_id"])
            if severity.get(premise["state"], 0) > severity.get(edges.get(key), 0):
                edges[key] = premise["state"]
    return {
        "nodes": nodes,
        "edges": [
            {"from_claim": dependent, "to_claim": premise, "state": state_}
            for (dependent, premise), state_ in sorted(edges.items())
        ],
        "unresolved": unresolved,
    }


def resolve_payload(store: LedgerStore, ledger_id: str) -> dict:
    """Resolve any ledger slug — claim, proof, either kind of revision
    id, or a reference id, possibly truncated for display — to the claim
    it belongs to ({"claim_id": ...}) or the reference itself
    ({"reference_id": ...})."""
    view = store.load()
    needle = ledger_id.strip()
    if needle in view.claim_heads:
        return {"claim_id": needle}
    if needle in view.proof_heads:
        return {"claim_id": view.proof_heads[needle]["claim_id"]}
    if needle in view.claim_revisions:
        return {"claim_id": view.claim_revisions[needle]["claim_id"]}
    if needle in view.proof_revisions:
        return {"claim_id": view.proof_revisions[needle]["claim_id"]}
    if needle in view.reference_heads:
        return {"reference_id": needle}
    # Slugs are shown truncated all over the UI; accept a unique prefix.
    candidates = {cid for cid in view.claim_heads if cid.startswith(needle)}
    candidates |= {
        head["claim_id"]
        for proof_id, head in view.proof_heads.items()
        if proof_id.startswith(needle)
    }
    if len(candidates) == 1:
        return {"claim_id": candidates.pop()}
    references = {rid for rid in view.reference_heads if rid.startswith(needle)}
    if not candidates and len(references) == 1:
        return {"reference_id": references.pop()}
    raise LedgerStoreError(f"unknown ledger id: {ledger_id}")


def search_payload(
    store: LedgerStore,
    text: str,
    observations: RevisionObservations | None = None,
    limit: int | None = None,
) -> dict:
    """Substring search over current claim statements. `limit` caps the
    full summaries returned (newest matches first, mirroring status);
    matches beyond it are listed as ids only and not marked read."""
    view = store.load()
    state = store.derived(view)
    needle = text.casefold()
    order = store.claim_order(view)
    matches = []
    overflow = []
    for claim_id, status in sorted(
        state.claim_status.items(),
        key=lambda item: (-order.get(item[0], -1), item[0]),
    ):
        revision = view.claim_revisions[status["current_revision_id"]]
        if needle not in revision["statement_text"].casefold():
            continue
        if limit is not None and len(matches) >= limit:
            overflow.append(claim_id)
            continue
        matches.append(_claim_summary(view, claim_id, status))
        if observations is not None:
            observations.note(claim_id, status["current_revision_id"])
    payload = {"matches": matches}
    if overflow:
        payload["more_matches"] = overflow
        payload["matches_omitted"] = (
            f"{len(overflow)} further matches listed by id only — narrow "
            "the search or read them via per-claim ledger_status"
        )
    return payload
