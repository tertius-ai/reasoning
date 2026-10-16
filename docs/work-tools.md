# Scoped work tools

The [durable worker protocol](work-execution.md) exposes eighteen scoped tools,
reusing project work and existing knowledge stores. Sixteen operate
transactionally on project records; two retrieve public sources outside database
transactions. Compute submission queues approved images through the existing
compute service; work tools do not execute local shell commands.

`GET /api/organizations/{organization_id}/work-tools` is the authenticated catalog.
Worker packets advertise tools granted by both the task and current root. Roles
share this protocol; role names grant no capabilities. Model-facing schemas
inline definitions and omit credentials, lease fields, and request IDs.

## Calls and receipts

Call `POST /api/work-worker/jobs/{job_id}/tools/{tool_name}` with worker bearer
authentication and `attempt_id`, `fence`, `lease`, stable `client_id`, and tool
arguments. Responses contain `result`, its canonical JSON `sha256`, and `replayed`.

| Tool | Additional arguments | Result |
| --- | --- | --- |
| `work_delegate` | `expected_root_revision`, `specification`, optional `depends_on` | Child task and queued job |
| `work_children` | `after`, `limit` | Paged direct-child headers |
| `work_child_read` | `child_id` | Child task/job/result and exact result reference |
| `work_child_cancel` | `child_id`, `reason` | Cancellation intent and queued-descendant cancellation count |
| `knowledge_search` | `query`, optional `kind`, `after`, `limit` | Source headers/previews and exact references |
| `approach_write` | `title`, `approach`, `status`, optional obstruction/reopening condition/evidence/revision | Project-ledger note reference |
| `input_read` | `reference`, `offset`, `length` | Exact-version text slice |
| `source_search` | `provider=crossref\|arxiv`, `query`, `limit` | Exact project bibliography references and metadata headers |
| `source_fetch` | `url`, `title` | Immutable captured-source and bibliography references, short preview |
| `source_check` | `subject`, `source`, `criterion_id`, `statement`, `quote`, `offset` | Exact citation-fidelity receipt for an independent critic |
| `workspace_list` | `after`, `limit` | Branch file headers and bound exact head references |
| `workspace_read` | `path`, optional `revision`, `offset`, `length` | Exact branch-file slice and reference |
| `workspace_write` | `path`, `text`, optional `expected_sha256` | Saved file revision reference |
| `workspace_patch` | `path`, `expected_sha256`, ordered `changes` | Patched file revision reference |
| `compute_submit` | `image_id`, `language`, exactly one of `program`/`source`, `resources` | Stable computation ID and execution status |
| `compute_read` | `compute_job_id` | Assignment's execution status and exact receipt reference when terminal |
| `compute_cancel` | `compute_job_id` | Cancellation intent or confirmed queued cancellation |
| `compute_output_read` | `execution`, `artifact_id`, optional byte `offset`, `length` | Receipt-bound output bytes and optional UTF-8 text |

Pages allow 1–100 records; input slices allow 1–16,000 characters. These are
transport bounds, not limits on total investigation or tool count.

Local outputs, source bindings, accounting and immutable receipts commit in one program
transaction. Repeating a task-epoch/request identity with identical input returns
the retained result without executing or charging again. Conflicting input is
HTTP 409. A new fenced attempt can retrieve that epoch's receipts; expired workers
cannot mutate state. Use a new request ID to refresh a search or child read.

Every successful new local call counts against the program and all task ancestors'
optional `max_tool_calls`; local failures and exact replays consume no extra call.
Public retrieval reserves its call before network access, including failed reads.
Omitted ceilings are unlimited. Resource overruns and expired authorizations
refuse new work. Authorized branch cancellation remains available despite spent
numeric ceilings or a paused program, but still requires its capability, project
authority and a live lease.

## Delegation and discovery

Child grants fit the parent and current root. Children may be coordinators.
Inputs must be assigned to the parent or bound by authorized discovery/inspection;
dependencies must be direct children. Knowing a record ID does not widen scope.
Inspection exposes direct-child results, not subtree transcripts, and binds exact
results/outputs so critics can review the same versions. A retained result is
not evidence of acceptance. Branch cancellation cancels queued descendants and
requests running cancellation, without inventing physical termination, releasing
uncertain reservations, erasing results, or affecting siblings.

Knowledge search includes organization-wide or same-project ledger, bibliography
and journal entries, excluding topic-scoped and other-project entries. It binds
a bounded page of exact revisions and returns short previews, not full bodies.
Later head revisions do not rewrite bindings. Input reads and compaction can
reuse these explicit bindings, not introduce arbitrary records.

## Public sources and citation fidelity

Crossref/arXiv search reuses the scholarly normalizers and stores results as
project bibliography entries with exact retrieval provenance. Search allows
1–20 metadata records, not full papers. Provider metadata is untrusted evidence,
not an instruction or a proof. Public-source tools use no organization API keys.

Fetch accepts public HTTPS text only, checks DNS and pins the TLS connection,
and denies private networks, credentials, redirects and binary formats. The read
allowance is 1 MiB per HTTP response. Captures retain URL, retrieval time, raw
SHA-256, transformation and extracted-text SHA-256. HTML removes script/style
content; supported other text is decoded as UTF-8 with replacement. Captured
content lives in immutable, project-bound 16,000-character database chunks;
input reads touch only the requested chunks. Bibliography and tool responses
contain references, not whole documents. General artifact storage permits up to
64 MiB of UTF-8 content per blob; that does not increase the HTTP read allowance.

Retrieval journals its identity before crossing the network, without keeping a
database transaction open during the read. A concurrent or interrupted `reading`
intent returns HTTP 409 rather than repeating the request. A failed read records
`public_source_unavailable`; a refused URL, media type, size, redirect or HTTP
status also records its `reason` code and an actionable `message`, while other
exception text stays private. Terminal retries
return the retained capture or safe failure; an intentional refresh uses a new
request ID. New fenced attempts can read old receipts with current authority.
Cancellation, revoked grants or expired leases prevent late publication.

Source checks require an assigned candidate and server-captured source, a
statement in that candidate's retained summary, and the source among its outputs.
An independent critic's current controller assignment checks the exact quote at
the supplied character offset and records `source_check` only for a criterion
requesting that kind. This certifies citation fidelity—not the truth of the
statement, full coverage, a software test, or a mathematical proof. Independent
model review remains a separate acceptance gate. Knowledge and proof states are
not promoted by source checking. Divide substantive coverage into appropriate
criteria rather than using one excerpt as an exhaustive-survey certificate.

## Persistent workspaces

Workspace paths are safe relative names of at most 128 characters, including
dotfiles. A task has an isolated branch namespace in the existing project file
repository. Controller assignments share their semantic target's namespace so
repairs retain earlier files. Other branches can read outputs only when an exact
reference is assigned or explicitly bound by inspection; workspace tools do not
expose sibling namespaces. Project members retain ordinary project-file access.

Writes accept at most 64,000 characters per request; replacing a file requires
the current SHA-256, even if the new content matches. Patches require that hash
and 1–64 ordered, nonoverlapping character-range edits (`start` inclusive, `end`
exclusive, replacement `text` at most 16,000 characters). Offsets count Unicode
characters, not UTF-8 bytes. Invalid or stale edits commit no revisions, receipts,
or resource counters. Identical request-ID retries replay the earlier receipt.

Files retain exact revisions and patch-input provenance. Content larger than
256 KiB uses the shared immutable chunk store, up to 64 MiB per file; bounded
reads touch only relevant chunks. Successive patches can assemble large files.
Topic diagnostic exports use exact content references and character counts for
chunked bodies, not misleading empty text. Portable backups retain bindings,
all file versions and chunks. Workspace persistence does not grant shell or
container execution, and model-written artifacts are not verification receipts.

## Approved computation

Compute submission requires the image and `compute_submit` capability in every
ancestor and the current program, plus current organization compute policy and
project write authority. An exact assigned `research_artifact` source is pinned
by revision and SHA-256. Inline programs and file sources allow at most 16,000
characters through the compute runner transport; larger workspaces remain
stored, not silently truncated.

Submission, a stable queue identity, shared CPU reservation, provenance, and tool
receipt commit together. CPU reservation is `cpu × wall_time_seconds`, constrained
by the organization quota and all work ancestors. Settled usage replaces that
reservation. Infrastructure charges are not reported in these receipts;
`budget_microusd` does not cap infrastructure spending. Operator/provider cloud
budgets remain separately binding. A missing dollar charge is not a zero charge.

The native worker waits for submitted computation with heartbeats and scoped
status reads, without paid model polling or additional tool-count charges. Lost
replies resume the retained computation instead of submitting another one.
Terminal receipts are automatically bound to the originating assignment, even
if its worker loses the reply. Another branch needs explicit input delegation or
result inspection to read them.

Only the enrolled compute worker's outcome or administrator-confirmed compute
cleanup can settle the linked reservation. An immutable `compute_result`
reference retains image identity, exact program/parameter digests, source
revision, outcome, CPU usage and committed artifact headers. This records an
execution outcome, not `tests_passed` or `proof_checked`. A successful exit code
alone cannot satisfy either acceptance gate.

Cancellation of queued computation settles zero CPU. Running cancellation and
lost workers retain reservations until a stopped execution reports its outcome
or cleanup is confirmed. Lease maintenance cancels abandoned queued submissions;
it does not claim to have stopped a running container or automatically reopen
the originating work assignment.

Output reads require an assigned exact `compute_result` reference and an artifact
ID in its immutable manifest. A critic may inspect delegated output without
permission to submit code. The server verifies committed metadata and each
selected retained chunk's digest. Reads return at most 12,288 bytes, with exact
base64, byte offsets, a continuation offset and optional strict UTF-8 text. A
binary slice or split UTF-8 code point has `text=null`, not substituted bytes.
Empty outputs and end-of-file slices are valid. These are transport bounds, not
whole-output size limits; no full-file download is needed for a slice. Output
content remains untrusted data, including any purported verification reports.
Repeated read identities return the retained slice; a fresh read detects missing
or modified chunks rather than silently presenting corrupt evidence.

## Approach memory

Approach writes store canonical JSON with `schema_type=work_approach` and
`schema_version=1` in ordinary project ledger notes. Fields include approach,
status (`attempted`, `obstructed`, `promising`, `superseded`), obstruction,
reopening condition, evidence and task/job/fence identity. Obstructed approaches
require both obstruction and reopening condition; evidence must be assigned.

Notes use automated (`bot`) attribution under their sponsoring person; work
events and receipts identify the enrolled worker. Writes cannot establish/refute
claims or replace unrelated knowledge. Revisions require `entry_id` and
`expected_revision`; prior versions and evidence remain. Recovery retains these
notes, receipts and bindings while fencing workers and jobs, without authorizing
automatic external resubmission.
