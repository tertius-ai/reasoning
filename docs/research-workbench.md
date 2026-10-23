# Organization research workbench

Beta topics are research and work conversations. The default prompt answers the
participant's question, uses sources and tools when useful, and records reusable
knowledge when requested or helpful. Routine conversation does not require ledger
writes or review. Humans, the selected model,
the project's ledger, private blog, bibliography, and independently
reviewed evidence share one organization server. No Codex installation or local
filesystem is required. Gamma topics store reusable, versioned workflows; alpha
topics remain human conversations and are not readable by provider-backed beta
agents. External bots can receive separate public/private conversation grants.

## Start a session

The compact top bar shows the organization and current channel/topic, with
search, the light/dark sun toggle, People, and the topic JSON copy icon on the
right. The button beside the R logo or Ctrl/Command+B collapses or opens project navigation. Topic follow/settings actions
are behind the topic name. An expandable System prompt message starts the beta
transcript. Type `/` in the composer to choose topic commands; `?` toggles
the keyboard and command reference. The ledger, bibliography, and blog also
have top-bar icons.
Copy exports a concise complete conversation with diagnostic summaries;
Shift-click exports the deduplicated full trace. Both retain private-project
access checks. Shared model pickers use the same compact capability/price rows
as the model browser, including gamma model choices. Connected owner-advertised
bot models can supply beta replies or some/all gamma LLM nodes through local CLI
accounts. They do not use OpenRouter keys or fall back to provider inference.
See [bots](bots.md) for setup, scoped tools and local execution approvals.
The composer grows automatically with the draft. A floating down arrow returns
to the newest loaded message after scrolling away.

1. An administrator configures OpenRouter keys and a monthly
   budget under **Administration → Inference**.
2. Create a beta topic with its model and knowledge/tool grants. Ask a question
   in the composer; switch models beside Send.
3. Use `/tools` to select a research strategy or add topic
   instructions. Fresh-context review is available by default, with automatic
   reviewer selection when requested; optionally override its model or disable it.
4. **Research account** or `/account` opens the evolving account for this question.
   Start one when sustained work needs a current argument; ordinary chat needs none.
   `/ledger` opens the scoped mathematical browser; **Project files** or `/files`
   opens shared project text artifacts with source, copy, and complete-revision
   download actions. Saved-file tool results include clickable revision links.
   `/reviews` opens independent reviews.
   Expand a review for its report or cancel unfinished work.
5. Use `/goal` or `/goal OBJECTIVE` only when the agent should continue across
   turns. Specify acceptance criteria and USD, time, turn, and tool-call limits.

These same panes run in the organization's browser and default desktop client.
Updating the server frontend updates both; the desktop does not execute its own
organization inference loop.

## Research strategies

The selectable strategies are Conversation and research, Proof blueprint, Fact graph,
Author–critic, and Lean formalization. They supplement the common mathematical
instructions with an evolving proof blueprint, dependency frontier, critique and
repair loop, or faithful formal-statement and certificate obligations. Additional
topic instructions are limited to 8,000 characters. All use the same granted
tools, canonical ledger, execution approvals, and accounting. A strategy does not
grant delegation or execution permission, install a prover, or enable a backend.
The default [topic-agent runtime](topic-agents.md) supports scoped delegation and
automatic result integration for ordinary messages; `/goal` adds persistence.
The common beta instructions also accompany CLI bot requests; their actual tools
come from live bot grants and the runner. Native `answer_checkpoint` is optional
when absent from a bot's registry. Local-ledger MCP instructions are separate.

The expandable System prompt shows the complete rendered instructions. Nondefault
instructions carry a strategy version and content hash. Queued runs retain their
exact captured prompt; changing settings affects new turns, not historical evidence.

## The current mathematical account

A beta topic can pin one evolving account: the exact question, definitions and
hypotheses, current argument, sources and checks, failed approaches, remaining
gaps, next steps, and approach. People with topic write access and authorized
research agents can edit it. The editor renders Markdown and TeX, retains saved
candidates and version links, and refuses stale edits while preserving unsaved
text. Writes carry a stable retry identity and expected current version. Omitted
fields remain unchanged. Each account is limited to 48 KiB; link a complete
manuscript through a saved-file revision when needed.

**Working knowledge** searches authorized project knowledge and pins exact claim,
reference, or published shared-entry revisions. At most 40 links per collection
form the account's working set. `account_read` supports exact-version field slices,
including `field="links"` for evidence pins when a large account needs several reads. Linked evidence stays pinned when the underlying
record changes; read its current version and explicitly repin it to update the
account. Saving an account does not certify the argument or alter ledger status.
A fork has its own account; it does not copy the source topic's account.

**Prepare independent critique** places an exact-version request in the message
composer. Sending it uses the selected native tool-capable model, normal inference
budget, and fresh-context review setting. A CLI bot can maintain the account but
hands critique to a native model with review tools. The reviewer receives that
frozen account and pinned evidence, with read-only source tools, without the
author's chat history. `/reviews` labels account versions, earlier candidates,
changed fields and exact recurring strongest objections. An LLM account critique
never promotes ledger records, including after a favorable verdict.

Each approach permits at most two critique requests, including failed or cancelled
requests. An unchanged argument with the same reviewer reuses its retained
pending or completed critique; changing only next steps is not another argument.
After two requests, retain the best answer and explicit gaps or pursue another
method without critic calls. A person can use **Describe a new approach** to
record a changed mathematical strategy and start a new bounded review cycle;
models cannot reset the allowance. Review is optional, and reaching this limit
does not prove the mathematical question impossible.

## Mathematical state

The canonical ledger has typed claims and definitions, immutable revisions,
separate proof attempts, exact premise/target revision pins, scholarly references,
and verification evidence. Status is derived from the mathematical dependency
graph, not assigned by a chat summary or majority model vote. Revising a premise
can make dependent arguments stale; unread or concurrent claim/proof updates are
refused. Records retain authorship and history. Normal beta writes require no
approval; agents record reusable mathematics when requested or useful to the work.
The UI also supports recording and editing statements.
Proof creation rechecks the target claim revision against the writer's observed
revision; a concurrent refinement returns the exact current statement before
any proof is written. The agent must reconcile the argument, not silently prove
an unread replacement. Creation receipts count as reads of the created revision.

The graph icon beside Copy opens the ledger workspace in the current chat shell. It shares
the workbench's dependency graph, fit/zoom controls and proof inspector, including
exact premise revisions, stale/broken edges, verification logs and revision
history. Scope it to an organization, project or topic, or search its independently
scrollable statement list. Opening a mathematical dependency stays in that
organization workspace; it does not navigate into a local project ledger.
Graph nodes expand to their complete statement, with a separate proof-inspection
action. Humans can record **Checked by a human** on the exact current claim
revision, with attribution and an optional note. These retained judgments are
distinct from model review and formal verification; they do not promote derived
mathematical status and do not carry over to a new revision.

An independent reviewer receives the frozen statement, definitions, givens, and
proof or citation evidence in fresh context, with read-only source tools. It
does not receive the author's chat history and cannot rewrite the ledger. The
server validates its structured obligations and records revision-pinned evidence
only after a successful, settled run. A positive citation verdict also needs an
actual completed source-read receipt. An unreadable, failed or partial PDF
extraction cannot attest full source access. An unavailable citation does not refute a
claim; a flawed proof does not prove the claim false. **LLM-reviewed** is not
machine certification. Imported verification records remain readable archives but
do not count as fresh organization attestations, positive or negative.
Automatic selection uses the current OpenRouter catalogue, favors a distinct
suggested model on the mathematics shortlist, and prefers another model family
within that ranking. If no other model is suggested, isolated same-model review
is recorded explicitly as such. An optional override can select any available
OpenRouter model. Review still needs an active provider
key and sufficient authorized budget. Admission failures are reported in the
review result; no positive verdict is fabricated. After requesting review, the
author checkpoints and yields; goals wait and resume with the review result.
The scheduler yields directly after a delegate/review handoff instead of making
a tool-less paid progress call. Reviewers submit an explicit `review_submit`
verdict; prose or multiline formatting does not substitute for structured
evidence, and ambiguous verdicts are refused.

Proof blueprint, fact graph and author–critic strategies request structured,
revision-pinned evidence and review for their deliverables. Searches use short distinctive terms, because ledger search
matches substrings. Agent completion should cite exact promised records and their
derived status, proof and independent review, not simply successful write receipts.
Review objections and blockers guide specific repairs. Useful open-problem work
can retain sourced cases and explicit unresolved obligations without pretending
to settle the problem or substituting a survey for a requested proof.

Project records remain private to the project's authorized roster. The global
browser aggregates only accessible records. Beta agents see their own project's
records and organization-scoped material, not another private project or an
alpha topic. Generic notes and imported source archives have their own history;
their labels do not certify typed mathematical records.

## Context procedure

Context is layered, not a fixed last-N-message buffer:

- Versioned instructions, explicit goal/acceptance criteria, and message pins.
- Cited, immutable working memory retaining notation, hypotheses, failed
  approaches, open obligations, references, and next steps.
- Relevant exact ledger statements/revisions, recent independent reviews,
  durable tasks, and a bounded project-file index.
- A token-budgeted recent dialogue tail; original messages and receipts remain
  available through scoped recovery tools.

In `/tools`, **Default knowledge scope** selects the account's linked working
set, the current topic, or authorized project knowledge. The default working set
falls back to the topic until an account exists. For typed ledger records, topic selection includes those
created or edited there; project-shared records keep their original access policy.
A working-set ledger view includes the transitive supporting premises and exact
revision histories needed to derive honest status. Unavailable or stale premises
remain unresolved or stale; a filtered view cannot establish them.

This scope applies to automatic context, default topic ledger status/search/graph,
reference and bibliography tools, shared-entry and journal searches, and review
results. Tool responses report the effective scope, source topics, truncation and
recovery actions. List readers return bounded pages or previews; use exact IDs for
complete records and explicit `scope="project"` for wider searches. Shared-entry
and journal searches return `entries`, `knowledge_scope`, and `next_before`; pass
the cursor as `before`. `ledger_reviews` returns scoped `reviews` and accepts a
`review_id` for a full earlier report. Read-only scope controls in the topic ledger
and reviews pane allow expansion without changing topic settings. The global
organization libraries are deliberately broader authorized views.

**Automatic knowledge records (0 disables)** accepts 0–20, with eight by default.
This is a combined allowance for relevant ledger statements, scoped critiques and shared knowledge
references. Outside an explicitly linked working set, automatic records must
match terms in the latest question or pinned account question; nonmatching records
do not fill the allowance. Zero disables this automatic knowledge injection; the
pinned account, conversation and explicitly retrieved evidence remain available.
All expansion still enforces current topic/project access, collection grants and
bot owner/requester permissions. Native calls recheck captured cross-topic sources
before sending them to the provider, including after context compaction.

The composer reports selected-model tool support separately from enabled source
readers and operator-configured web search. Native gamma comparisons remain
tool-free and say so before submission; use a beta topic with a tool-capable
model for external research. Each native invocation receives the current UTC
date and its effective advertised and discoverable tool list; these facts are
retained in a `capabilities` event and run detail. Source readers and general
web search are distinct, and missing grants are not a source-reader failure.

For substantial work, `answer_checkpoint` retains a readable provisional answer,
remaining gap and source locators without completing a goal. Run detail exposes
the latest checkpoint and an `answer_state` separate from execution/accounting:
working, waiting, partial, or response_ready. A successful review/delegate
handoff remains waiting and incomplete. Interruption, timeout and budget refusal
retain useful output/checkpoints in the transcript with the stop reason; no
automatic replay of uncertain paid calls is authorized. Execution success and a
visible response do not certify the mathematics or satisfy a goal's criteria.

The input target defaults to 60% of model capacity, adjustable from 40–80% through
the research-settings API, with a conservative 128,000 UTF-8/framing upper-bound
cap. This bound is not the provider's billed tokenizer count. Overflowing older
dialogue is compacted in bounded batches. Every memory item must cite a real
source message; invalid compaction leaves the original transcript intact and
fails safely. Bulky completed tool results can be reduced to receipt-linked
previews. Tool schemas load on demand instead of putting every tool in every
request. Oversized remaining protocol context is refused, not silently truncated.

OpenRouter reasoning details are retained verbatim across native tool rounds.
The harness does not rely on OpenAI-only opaque compaction APIs or assume that
private reasoning state can move between model providers. Changing models uses
the canonical shared record, cited memory, and source dialogue.

`/context` shows the prompt, pins, memory revisions, original
captured context, and latest provider-call context. The exact base prompt ships
at [beta-research.md](../src/reasoning_ledger/prompts/beta-research.md).

## Tools

`tool_search` discovers granted capabilities and their exact schemas;
`tool_call` validates the selected tool's arguments and current authorization.
Discovery never adds permissions. Each invocation has a durable receipt.

| Family | Main tools |
| --- | --- |
| Mathematical record | `ledger_status`, `ledger_search`, `ledger_read`, `ledger_graph`, `ledger_blockers`, `ledger_blast_radius` |
| Research writes | `ledger_claim`, `ledger_refine`, `ledger_prove`, `ledger_revise_proof`, `ledger_withdraw`, `ledger_abandon`, `ledger_verify` (negative/inconclusive review evidence only; `evidence` needs a nonempty string `kind`, for example `{"kind": "proof-review", "text": "…"}`) |
| Current account | `account_read`, `account_update`, `account_request_review` |
| Independent review | `ledger_request_review`, `ledger_reviews`, `ledger_cancel_review` |
| Bibliography | `ledger_reference`, `ledger_refine_reference`, `ledger_references`, `ledger_search_references`, bibliography aliases |
| Original context | `topic_history`, `topic_message_read`, `tool_receipt_read`, `tasks`, `goal_checkpoint` |
| Literature | arXiv search/lookup/text/source archive reads; Crossref search/lookup; OEIS lookup/search |
| Mathematical sources | `math_search`, `math_read`: MathOverflow, Wikipedia, nLab, Stacks Project, Encyclopedia of Mathematics, LMFDB |
| LMFDB | `lmfdb_catalog`, `lmfdb_schema`, `lmfdb_query`: all collections exposed by its public API, discovered live |
| Private files | `artifact_list`, `artifact_write`, `research_file_read`; compute `artifact_read` |
| Private research blog | `blog_search`, `blog_read`, `blog_create`, `blog_revise`, `blog_publish` (authorized, revision-guarded writes) |
| Gamma authoring | `workflow_read`, `workflow_create`, `workflow_update`, `workflow_preflight`, `workflow_propose_run` |
| Sandbox computation | `compute_submit`, `compute_cancel`, image/job/artifact inspection; `lean_check`; `noether_compute`, `noether_status`, `noether_describe` |
| Fixed arithmetic | `scientific_calculate`, `unit_convert`, `constant_lookup`: bounded trusted implementations, no arbitrary Python execution |
| Model choices | `model_catalog`: current catalogue IDs, not a substitute for live capability/price preflight |

MathOverflow and reference encyclopedias are not automatic primary-source
attestations. Source reads are bounded public HTTPS, DNS/IP checked, TLS verified,
and deny private networks, redirects, credentials, and executable HTML. General
web search needs the operator's `REASONING_BRAVE_API_KEY`; the mathematical
adapters do not need separate search credentials. Sources can fail or rate-limit;
the agent must report missing evidence, not invent retrieved text.
Search providers receive query text; inference and compute providers receive
their submitted inputs. A private audience is not a promise of offline execution.
LMFDB supports
typed field filters and page offsets, not Python-literal queries or unrestricted
database access. No claim of access to unpublished/private LMFDB collections is
made. `web_fetch` extracts PDF text in the same bounded, isolated worker used by
attachments and reports coverage or failure explicitly. Other binary media are
refused. Scans require OCR or a text source; arXiv has dedicated HTML and source
readers. Transient failures of `web_fetch` and the mathematical adapters
(connection failures or HTTP 429/502/503/504) receive one bounded retry. The
research arXiv, Crossref and OEIS tools share one gateway per installation: arXiv
requests are at least three seconds apart across concurrent runs; connection,
rate-limit and server errors get up to three attempts that honor `Retry-After` up
to 30 seconds; and when those attempts fail, an earlier cached copy is returned
with a warning if one exists. These tools read up
to 8 MiB per response; `web_fetch` and the mathematical adapters read up to 1 MiB.
arXiv tools accept versioned and old-style identifiers (`2401.01234v2`,
`math/0211159`), an `arXiv:` prefix, or an arxiv.org `abs`, `html` or `pdf` URL.
Missing, oversized or failed arXiv HTML falls back to the largest readable
primary TeX source member, reporting its exact path, the HTML failure and the
limited coverage. A single-file submission, which arXiv serves as one gzip file,
is listed as one `.tex` member; a PDF-only submission is reported as having no
TeX source. A failed tool call returns a stable code and a message naming what
was refused, why, and what to do instead: an invalid argument and its schema
path, an ungranted tool, an unsupported media type, a size limit, a redirect
target, a missing identifier or version, or a rate-limit wait. Unexpected
exception text is not returned.

## Computation, approvals, and the private blog

Programs run only in separately enrolled, approved Docker/Modal workers, never
on the application host. The approved image advertises its actual languages.
The public CPU recipe provides Sage, Python, GAP, PARI/GP, Julia, core Lean and
Noether 0.3.2's default CPU profile; it does not include mathlib or extra Julia packages. The Noether tools
require an approved Python image containing the pinned package and the fixed
worker at `/opt/reasoning/noether_worker.py`. Missing runtimes are failures, not
permission to execute on the server. See [images](images.md) and [compute](compute.md).

A topic's explicit monthly autonomous CPU-second allowance defaults to zero.
Within it, compute still obeys organization/image/worker ceilings and atomically
reserves resources. Otherwise it becomes an exact-action approval proposal.
Workflow execution always requires approval. Saved gamma drafts are not jobs.
Application CPU quotas do not promise a provider dollar ceiling.

Bibliographic IDs deduplicate identical metadata within their original scope.
An existing inaccessible private reference is refused rather than exposing its
annotations to another project; multi-scope sharing of a private reference is
not provided by this storage adapter.

The top-right ledger and bibliography links open workspaces in the current chat shell,
within the same authenticated organization. The blog link opens the static
reading index in the current tab; its **Edit posts** link opens the Markdown editor. The ledger has a scope selector,
independently scrollable statement list and detail pane, and shared-note records.
Topic permissions apply to these workspaces; URLs carry resource IDs, not credentials.
The **Research blog** editor accepts Markdown/TeX and has a
preview. Humans can save drafts and publish within the chosen private audience.
Agents can read posts/drafts, save project drafts, edit published posts into new drafts,
and publish requested writing with `blog_publish` or `state: published`. Revisions
require the exact current head revision and retain immutable prior versions.
Published content never becomes
a public blog endpoint. Standalone writeups can be saved as versioned research
files by the session; there is no separate autonomous writer service.

The static blog and editor share the main page's theme colors and a compact layout, dated post
lists, and project filters restricted to accessible projects. Full published
articles open from their titles; the editor lists drafts separately, with project
tags derived from each post's scope. New posts default
to the selected project (or first accessible project). Markdown editing includes
math preview and explicit Save draft, Publish, and Unpublish actions. Stable
request identities and expected revisions protect lost replies and concurrent
editing. Editing a published post does not alter its published snapshot; only
Publish replaces it. Unpublish removes the reading snapshot, not immutable
source revisions. Beta context and canonical knowledge tools use the published
snapshot, while explicit blog tools can inspect drafts.

Editors can delete drafts or published posts. Deletion requires the current
head revision and a stable request identity; a changed post must be reloaded
before deletion. Deleted posts disappear from reader/editor lists, search,
direct reads (including exact revisions), and newly compiled agent context.
An immutable deletion receipt and audit event retain who deleted it and when;
source revisions and the last publication snapshot remain in database history.
There is no automatic restoration or agent deletion tool. Already captured
agent requests and historical transcripts are not rewritten by deletion.

`/blog/{organization_id}` and `/blog/{organization_id}/{post_id}` serve static
HTML to authorized readers, rendered with the OSS `markdown-it-py` parser.
No Jekyll/Ghost service or public content exposure is required. Raw author HTML
and automatic external images are disabled; local KaTeX progressively enhances
TeX while the article remains readable without JavaScript. Append `.md` to a
post URL for its published Markdown. Project/topic permissions and authentication
apply to HTML and source alike. Topic and project references (`#[topic:ID]`,
`#[project:ID]`) render as links with the current name for readers who can open
the target and as *unavailable* for everyone else; the Markdown source keeps the
tokens. The API index is
`GET /api/organizations/{organization_id}/blog?project=ID&before=CURSOR`.
`DELETE /api/organizations/{organization_id}/blog/{post_id}` accepts
`client_id` and `expected_revision` for an authorized editor's deletion.
Every static blog page loads a small script that applies the reader's explicit
light/dark choice. Only pages that show math also load KaTeX: an article whose
parsed Markdown contains dollar or bracket math (code spans and blocks do not
count), or an index page with TeX delimiters in a listed title. The KaTeX
enhancement imports only KaTeX and its stylesheet, not the interface's React
bundle. The static reader loads KaTeX styles from the build's dependency manifest when
available, or selects the emitted KaTeX stylesheet in a packaged release; the
selection is computed once per frontend build. The index's project filter reads
only the reader's accessible projects, without the topic tree or unread state.
Styles accompany local math enhancement, preserving accessible MathML without
displaying its fallback text beside the visual equation.

Gamma runs pin their definitions. **New uncapped workflow** copies the original
definition into the JSON editor with null LLM output limits; running it creates
a separate definition and execution. Ordinary retries/reruns preserve existing
explicit limits and do not mutate the original workflow.

## Accounting and recovery

Each answer, continuation, compaction, and independent review has its own priced
inference receipt and budget admission. Output limits apply per provider call;
compaction does not consume the final answer's output ceiling. Organization
research turns have no extra tool-count ceiling and allow 30 minutes; a goal
continues through multiple bounded
turns, up to its explicit time/turn/tool/USD limits. Reviews count against the
same goal cost and tool budget. Goals wait for pending reviews and queued/running computation
instead of spending turns busy-polling.

Cancellation can still incur charges. Failed, interrupted, or unknown-charge
provider submissions are never paid-replayed automatically. Reconcile unknown
charges before resuming. Original records, artifacts, pending review jobs, and
goal state survive restart; abandoned submitted reviews fail without attestation.
An unknown computation outcome also stops goal continuation until cleanup is reconciled.
See [inference](inference.md) for detailed admission and reconciliation behavior.

Connected bots can advertise owner-approved CLI inference and computation
capabilities. Beta and gamma model selectors expose eligible bot inference;
local execution uses scoped approval and workspace policies described in
[bots](bots.md). Goals continue through the selected bot or OpenRouter model;
bot-account usage is not charged to the OpenRouter ledger. Effort controls use
advertised model capabilities and preserve provider defaults when unset. There is no
built-in university Slurm execution backend; connecting a bot alone does not
grant arbitrary job execution.
