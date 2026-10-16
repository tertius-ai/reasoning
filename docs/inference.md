# Shared mathematical research agents

Beta topics contain human messages and shared model responses. Each topic
selects a default model at creation. The composer places a model
selector beside Send and requests the selected model after its message write
succeeds. Responses and progress
are visible to the organization's members, including viewers. No desktop or
member-owned API key is needed. Alpha topics do not execute models. Gamma
[workflow runs](server.md#durable-workflow-runs) use this same inference executor
and organization budget with captured node prompts/dependency data instead of
beta topic history. Their paid child admission and attempt linkage are atomic.

## Configure and request

An owner or administrator uses **Administration → Inference** to add one or more
OpenRouter keys and set a positive monthly USD
budget. The closed Add key form always creates a new credential; stored secret
values are never placed in its fields or returned to the browser. A new key
can use every available text-to-text model or router; new catalog entries require
no organization policy update. Chat selectors omit batch-only, embedding, and
reranking aliases. Selectors default to tool-capable or reasoning models, connected
bots, and routers; **All chat models** exposes the other text models without
changing key permissions. The main capabilities browser is an alphabetical
catalog without recommendation controls. Availability and prices are checked at request admission. The newest active
organization key is selected when a request is accepted. The
key identifier is pinned internally; disabling it cancels affected queued or
running work rather than silently switching an accepted request to another key.
Provider-key values are decrypted only in the server executor and never included
in run records, progress events, client responses, or error messages.

Choose the initial model and knowledge access when creating a beta topic.
Sending a human message captures context through that committed message and
requests the model selected beside Send. Chat, goal turns, and independent
reviews do not impose an application output-token cap by default. Provider
output/context capacity still applies. Reservations use an 8,192-output-token
estimate, reduced for smaller model/window capacity; this estimate is not sent
to OpenRouter as a limit. A model's advertised maximum is a capability, not
the default reservation. The selector
leads with mathematics recommendations and keeps the complete allowed catalog
searchable. Model replies are independently labeled `LLM · provider/model` in
shared history.

Beta uses the same `ReasoningNativeExecutor` and validated tool registry as the
local workbench. A turn compiles context, calls the selected model, validates and
executes requested tools, records their receipts, and continues to a final answer.
Each call has its own quote, budget admission, and immutable inference receipt.
Visible output from separate tool rounds is separated by paragraphs; individual
stream fragments and output-limit continuations are concatenated unchanged.
Beta and gamma model steps use this loop. When an answer or provider-supplied
reasoning stops at an output limit, the executor can continue it, preserving
answer segments and opaque reasoning metadata. These are new paid
submissions, admitted against organization and goal budgets before execution,
not retries of failed or ambiguous calls. There is no fixed continuation count
or cumulative output-token quota. Organization research turns have no implicit
tool-count ceiling and have a bounded execution deadline. Goal turns and their
reviewers share the goal's remaining explicit tool budget; active goals can
schedule further turns. An
exhausted authorized budget or failed continuation preserves any
available partial answer and leaves the turn failed rather than reporting a
complete answer. API callers can request an explicit per-call output limit
within the model's capacity and authorized budgets.
Models without advertised tool support receive a plain response with bounded
shared-knowledge reference context. The chat server never executes submitted
programs or exposes a local filesystem to the model.

The compact **⋯ Research settings** control selects conversation history, mathematical literature, public web,
canonical shared knowledge, gamma workflows, and computation. Knowledge sources
are separately selected at topic creation. History reads are limited to beta and
gamma topics in the same project; alpha topics cannot be read. Ungranted private
projects and unsent composer text are excluded. Explicit blog tools can read
authorized project drafts; automatic shared-knowledge context excludes drafts. Every tool
rechecks current permissions before returning a result. Queued turns retain their
approved tool and knowledge grants; a changed grant stops the turn for review.
Source material and summaries are reference data, not additional system instructions.
Queued turns execute their captured instructions and report the matching prompt
version; upgrades do not silently replace that input.
Research settings also select Research, Proof blueprint, Fact graph,
Author–critic, or Lean formalization instructions and up to 8,000 characters of
additional topic instructions. Strategies use the same authorized tool registry;
they do not enable execution backends, parallel workers, or advisory councils.
The expandable System prompt and topic JSON expose the exact rendered instructions.
Instructions require checking the requested statement and correcting prior model
mistakes, not assuming a requested proof or disproof exists. These instructions
do not certify mathematical correctness; evidence and independent review remain
necessary.

The standard tools search/read original messages and immutable knowledge
revisions; retrieve arXiv/Crossref sources; fetch bounded public HTTPS text;
inspect gamma workflows, approved computation images, jobs and verified artifacts;
and automatically record typed mathematical claims, proofs, references, private
files and blog drafts. Authors cannot self-certify positive mathematical verdicts.
Fresh-context review is enabled by default, without reviewer setup. The server
selects an available model, prioritizing a distinct model from the mathematics
shortlist and then another family. Research settings can override the selection
or disable review. If no other model is suggested, review uses the author's model
in a separate fresh context and records that it is not a distinct-model review.
Review requests yield the author turn, checkpointing an active goal; the server
dispatches the reviewer and includes its result in the next goal context rather
than allowing the author to poll its own pending job. A queued review pins its
model; key revocation, permission loss, and insufficient budget stop it rather
than silently switching models.
Changes to default catalogue suggestions do not revoke an admitted reviewer;
disabling review or changing an explicit reviewer override does.
Paid workflows, compute beyond the topic allowance, and generic knowledge changes
require human approval. Authorized project blog creation, revision and publication
use direct tools with current-revision guards; publication keeps the same private
audience. Stable identities prevent duplicate
execution on lost replies. See the [research workbench guide](research-workbench.md)
for the tool families, review semantics, and private blog editor.
Public HTTPS reads check every DNS address, connect to a pinned public address,
verify TLS, refuse redirects/proxies/private networks, strip executable HTML,
and enforce time and response bounds. General web search is advertised when the
operator supplies `REASONING_BRAVE_API_KEY`; arXiv, Crossref and public source
fetching need no search credential.

## Parallelism and provider performance

The installation runs up to eight native agents concurrently by default;
`reasoning-server serve --agent-concurrency N` selects 1–256. Organization
scheduler limits and spending admission still apply. Queued work can wait when
those slots are occupied. Goal/reviewer catalog lookups run separately from the
runnable queue, so a slow background lookup does not block accepted human turns.
There is no global lock serializing OpenRouter requests.

Provider routing in Model capabilities offers recommended, cheapest, lowest
latency, and highest throughput strategies. Existing organization policy is
retained. Cheapest keeps the quoted price ceiling; the speed strategies permit
other eligible providers at their advertised prices and do not guarantee a
particular response time. Required ZDR and regional routing are retained.
See [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).

Each new provider call records time to its first content, reasoning, or tool
delta and total provider-call duration. These timings are available in inference
call details and topic diagnostics, separate from queue time. Historical or
empty responses can have no first-token measurement. Timing receipts do not
retry submissions or certify provider execution; uncertain charges remain
explicit. A long first-token delay can be upstream queueing or reasoning, while
a long total duration can include slow token generation. A specific live delay
requires inspecting its call evidence, not attributing it to the router by guess.

## Context and goals

Changing the reply model does not start a new conversation. New turns use the
same canonical topic transcript, cited memory and shared project knowledge,
compiled for the selected model's capacity; provider-private reasoning is not
transferred between models. Existing run contexts and model identities remain
immutable.

**Fork here** on a beta message creates another beta-topic in the same channel,
copying history through that message, tools and context settings, and retaining
source provenance. The browser carries over the current reply-model selection.
The fork does not copy later messages, source-topic memory, tasks, goals, jobs or
charges. Project ledger, bibliography and published blog knowledge remain shared.
Original model identities stay attached to copied responses.
Forking starts a conversation branch; it does not publish a ledger entry.

Human authors with current topic write access can edit their own messages through
the message-actions menu. Edits retain an accessible original and revision history,
use an optimistic revision guard, and update readers through the message-update
cursor. Saving an edit does not invoke a model, bot, webhook, or workflow. Existing
requests keep their captured inputs, including during historical compaction;
later requests use the revised text. Working memory is reused only for its captured
message-edit epoch. Fork, send a new request, or explicitly choose **Reply again
with selected model** in a beta message's menu to continue from corrected text.
That reply is a new billed request using the currently selected model and revised
conversation through that message; it does not replace the previous response.
Bot event polling retains original message content: an edit before delivery cannot
alter the originally requested invocation or turn an unmentioned message into one.

Hovering or keyboard-focusing a message reveals compact reaction and beta-fork
controls, with `:)` at the right and message actions immediately to its left.
Touch devices expose the same controls directly. The reaction picker
opens from an unboxed `:)` control and offers 👍, 👎 and 🙃. It closes as soon as
a choice is clicked, including when the request fails. Each person has at most
one reaction per canonical response; choosing another replaces the previous
choice. Only existing reactions appear below the message, with up to
three reactor initials and an overflow count. Initials expose full names on hover
or keyboard focus; contact information is not included. The usage
page aggregates counts under the actual recorded model, filtered by the reader's
project access. These reactions are feedback, not mathematical verification.
A conversation reads the reactions of its loaded messages with one request,
`GET /api/organizations/{organization}/topics/{topic}/reactions/messages?ids=…`
(at most 200 comma-separated message IDs, one topic access check), which returns
the single-target result keyed by message ID; messages that are absent or have no
retained output are omitted. Choosing a reaction updates only that message.

Context is governed by the selected model's token capacity. It includes versioned
mathematics instructions, an active user goal, explicit message pins, cited
working memory, relevant exact mathematical records, review results, tasks, a
file index, and a recent conversational tail. The initial input target is
60% of the smallest selected context window, capped at a conservative 128,000-token
upper bound. UTF-8 bytes plus framing provide a conservative bound, not billed
tokens. Large tool results stay in durable receipts and return bounded previews
with exact recovery tools.

When old source messages no longer fit, the runtime makes separately billed
compaction calls within its time and inference cost budgets. Output limits apply
per provider call; summary output does not reduce the final answer's ceiling.
Memory preserves goals, definitions, notation, hypotheses, claim status,
unresolved questions, decisions, citations, artifacts and next steps.
Every memory item must cite an actual original source message. Revisions
are immutable, source digests are retained, and original conversation records
remain readable. Invalid compaction fails the turn without discarding sources.
The expandable **System prompt** message at the top of every beta topic shows
versioned instructions for new turns using the chat's Markdown and mathematics
styles. Opening it suspends automatic scrolling while it is being read.
The copy icon next to People in the top bar is available in every topic type.
**Copy topic JSON** (`GET /api/organizations/{org}/topics/{topic}/export`)
returns `reasoning.topic-summary.v2`: all retained messages, current instructions,
goals/checkpoints, run/tool diagnostics, latest call accounting, and scoped
research/workflow/compute records. It omits repeated full requests, sessions,
context bodies, historical file bodies, and duplicate accounting receipts.
It does not paginate or truncate the conversation.

Shift-click the copy icon for `?detail=full`, a `reasoning.topic-trace.v2` export
preserving all retained diagnostic JSON values. Repeated large values are stored
once in `blobs` under SHA-256 keys. Resolve `{"$ref": key}` recursively through
that dictionary; decode `{"$object": [[key, value], ...]}` to an object, and
recurse normally through other objects and arrays. Only dictionaries containing
reserved keys use the escape wrapper. This preserves user-supplied `$ref`/`$object`
keys without ambiguity. The normal copy formats each summary record on one line.
`?detail=raw` and the beta-only `/research-export` endpoint provide expanded raw
diagnostics, including exact provider requests/responses, immutable accounting
receipts, contexts, sessions, and accessible project text-file revisions.
Absent detail in older records is not reconstructed. Compute log bodies and
binary artifacts are referenced by metadata, not embedded.

Every format contains private research and participant names, but excludes
operator credentials, provider keys, login sessions, and unsent drafts. Share
only with trusted recipients. Clipboard fallback provides selectable JSON and
a download of the selected format. All formats enforce the same organization,
private-project, and live-topic access checks.

Type `/` for topic commands, or toggle keyboard help with `?` outside text fields. `/ledger`,
`/files`, `/goal`, `/context`, `/tools`, `/proposals`, and `/reviews` open research
surfaces without permanent composer buttons. Play (▶) resumes stopped agent
threads or retries the exact retained submission; stop (■) stops their
continuation at a safe checkpoint. Unattached responses use the existing
cancellation path. Charges and uncertain outcomes remain retained, and commands
never authorize replaying an unknown effect. Inapplicable playback buttons are
disabled. Live round activity has a subtle pulse, disabled with reduced motion.
The message box grows with the draft and shrinks when cleared; Tab then Enter
sends. Enter sends, Shift+Enter inserts a newline, and an open mention menu uses
Enter to accept its selected suggestion. Composition/IME input is preserved.
Model selection is remembered per account, organization, and topic in optional
local storage; stale selections fall back to the current OpenRouter catalog.
Failed attempts appear in chronological position as compact expandable rows.
Running rows show brief round/tool activity, available provider reasoning summaries, and confirmed spend separately from
the next-call estimate. Answers have one tokens/cost/Wh footer, with carbon in
grams only when configured. Detailed accounting remains in the inference ledger.

`/context` shows the system prompt, memory versions, pins, initial snapshot and
the exact context of the latest provider call.

Type `/goal OBJECTIVE` or select `/goal` to define completion criteria and explicit
USD, time, turn and tool limits. A topic has one active goal, owned by its creator.
The worker schedules successive durable turns until a goal checkpoint reports
completion or a boundary is reached. Only its owner or an organization
administrator can pause, resume or end it. The model must support tools to record
research checkpoints. A recoverable model/protocol failure with known accounting
and resolved tool outcomes schedules a fresh turn from retained evidence rather
than replaying the failed submission. Missing checkpoints alone do not stop the
goal. Unknown charges, unresolved tool outcomes, nonrecoverable provider/access
failures, explicit blockers, user pauses/cancellation, and authorized budget
boundaries stop continuation with a recorded reason. A failed allowlisted
read-only source lookup is recoverable; a failed mutation with an unverified
outcome is not. Workflows and
computations retain their own approval and resource limits. Goals wait for pending
independent reviews and queued/running compute jobs; review costs count against
the goal budget. Tool schemas are discovered on demand, and native tool rounds
preserve opaque provider reasoning details rather than reconstructing them.

The server checks the current public OpenRouter model catalog for text-only
output, text input, output-limit support, context size, and prices. A retired,
disallowed, incompatible, or unaffordable selection is refused before execution.
The provider request fixes the model and requires supported parameters.
Organization routing chooses recommended load balancing or cheapest compatible
providers, with eligible-provider fallback. Cheapest keeps the quoted price
ceiling; recommended permits other eligible providers at their own prices.
No organization credential is sent when fetching the public catalog.

## API contract

`GET /api/organizations/{organization_id}/topics/{topic_id}/research-instructions`
returns the current system prompt and its prompt/harness versions, including
before the topic's first inference run.
`GET /api/organizations/{organization_id}/topics/{topic_id}/research-export`
returns `reasoning.beta-topic-diagnostics.v1` with the complete retained
diagnostic records described above. Both require access to the live beta topic
and enforce organization and private-project membership; deleted topics are
unavailable. Responses use `Cache-Control: no-store`.

`GET /api/organizations/{organization_id}/topics/{topic_id}/export` accepts
`detail=summary|full|raw` for alpha, beta, and gamma topics. Summary is the default.
It previews long tool arguments/results with SHA-256 fingerprints and
stores repeated energy assumptions once, linked through `assumptions_ref`.
The full encoding is reversible and raw is expanded. Unknown formats return 422.

Use `/api/organizations/{organization_id}/topics/{topic_id}/inference-runs`:

- `POST` returns 202 and the selected models' durable run records. Supply
  `client_id` (1–128 characters), `models` (one to four distinct IDs),
  optional `max_completion_tokens` (16–2,147,483,647; omitted/null means no
  application output cap), `context_message_limit`
  (1–100, default 50), and optional `through_sequence` identifying a message
  in this beta topic. Without that sequence, context ends at the latest message.
  A topic must have at least one message. The compatibility field
  `context_message_limit` does not select context; the server compiles it from
  token capacity, cited memory and pins.
- Replaying the same client ID, requester, organization, topic, and parameters
  returns the existing runs without another catalog fetch or provider call.
  Reusing the ID for different parameters returns 409. While a submission is in
  flight the conversation shows a sending status. After a network error, HTTP
  408, or a server error, the browser retains the attempt and its captured
  parameters in memory across topic navigation, and the conversation explains
  that the message was saved but acceptance of the reply request is unconfirmed.
  It shows the failure reason, the model, any selected effort, and where the request's
  conversation context ends ("Conversation context ends at message #N"). **Retry
  same request** resends the same client ID, model, effort, and context end, so
  an accepted request returns its existing runs instead of starting another.
  **Dismiss pending retry** clears only this browser state: it does not cancel
  an accepted run, change its accounting, or delete the message; **Stop current
  work** remains the cancellation control. Any other 4xx answer clears the
  attempt and shows its error. The in-memory retry state is cleared by reload,
  sign-out, or session loss. Inspect existing runs before manually starting
  another request after reloading.
- `GET` returns the most recent runs, with `limit` of 1–50 (default 20).
  Viewers can read but cannot submit or cancel.

Under `/api/organizations/{organization_id}/inference-runs/{run_id}`:

- `GET` returns state, accumulated text, context-end sequence, error code,
  accounting status, provider generation ID, and known tokens/cost. It does
  not return credentials, their identifiers, or the internal prompt.
- `GET /events?after=sequence` returns up to 200 durable events after an exclusive
  cursor. Events record state transitions and appended text deltas. Reconnect
  with the last received sequence; sequence numbers need not be consecutive.
- `POST /cancel` with `{}` can be used by the requester or an administrator.
  Cancelling queued work releases its reservation. Cancelling running work
  closes the provider stream and preserves partial text, but may still incur a
  provider charge.
- `PUT /cost` with `actual_microusd` records an administrator-verified cost for a
  run whose accounting state is `uncertain`. Other accounting states return 409.
  The operation is audited. This does not contact or change the provider account.

Under `/api/organizations/{organization_id}/topics/{topic_id}`:

- `GET /research-context` exposes the authorized topic's prompt and context evidence.
- `PUT /research-pins/{message_id}` with `pinned` adds/removes an exact source pin.
- `GET`/`PUT /research-settings` reads/replaces tool groups, optional reviewer
  override, context target percentage, and autonomous monthly CPU allowance.
  `review_enabled`, `prompt_profile`, and `additional_instructions` select review
  policy and instructions. Omitted context-policy fields retain their values.
- `GET /research-instructions` exposes the current rendered prompt, its version,
  selected profile, and profile catalog; captured run contexts remain immutable.
- `GET /mathematical-reviews` lists frozen independent review jobs/results;
  `POST /mathematical-reviews/{id}/cancel` requests cancellation.
- `GET /goal` reads the latest goal; `POST /goal` supplies its objective,
  acceptance criteria, model, stable client ID and explicit budgets.
- `PATCH /goal/{goal_id}` selects `active`, `paused` or `cancelled`.
- `GET /research-proposals` lists proposed actions; `POST /research-proposals/{id}`
  with `approve` records the human decision and runs an approved action.
- `GET /research-tools` and `GET /research-tools/{receipt_id}` expose authorized
  tool receipts with exact arguments and results.

`GET /api/organizations/{organization_id}/inference-configuration` returns
current catalog model choices (the compatibility field `allowed_models`),
`model_access: "all_available"`, whether an active key exists, execution/worker availability, and
budget/spent/reserved/uncertain/available balances. It never returns a key value.
All routes enforce tenant membership and the same Origin/CSRF rules as human
message writes. There can be at most eight queued/running child model runs per
organization and twenty new requests per requester/organization in fifteen
minutes. Retries of an accepted request do not consume another request slot.

## Execution and recovery

`reasoning-server serve` starts one installation-owned inference worker with
two concurrent turns and a 1,800-second execution timeout per turn. A private exclusive
file lock prevents a second executor from using the same installation. Queues,
attempt fencing, text deltas, terminal state, and assistant messages are stored
in SQLite. Provider SSE is incrementally decoded, including split UTF-8 chunks;
the browser polls durable state every second rather than maintaining a separate
browser-to-provider connection. Provider input is bounded to 64 MiB per stream,
2 MiB per event/line, and 2,000,000 accumulated output characters.
The provider stream is the single writer of cumulative visible output, including
progress accompanying tool calls. Final-answer events do not replace that prefix.
Unexpected execution errors have the `agent_execution_error` classification and
a safe exception category, not raw exception text or a misleading context failure.
Model text must contain valid Unicode scalar values; unpaired JSON surrogate
escapes fail before any invalid text reaches durable state. JSON nesting that
exceeds the parser's recursion limit is a safe protocol/catalog failure, not an
executor crash. Valid Unicode and split UTF-8 bytes retain their exact text.
The transport accepts content-free accounting frames that repeat a terminal
finish reason, as described in [OpenRouter's streaming format](https://openrouter.ai/docs/api_reference/streaming).

The worker checks cancellation, membership, budget reductions, and
key disabling while waiting for the provider, including when its stream is
silent. Completed replies are saved once. Failed, cancelled, or interrupted
partial text is saved with an explicit incomplete-response label. A stale
attempt cannot append text or publish another final message.

On shutdown, active calls are interrupted. On restart, abandoned running calls
are marked interrupted and are **not automatically retried**: they may already
have been billed. Previously queued, never-submitted calls can proceed. Known
usage persisted before an interruption is retained. If execution fails
unexpectedly, worker availability becomes false; restart the server to recover
durable work. The current server requires a single executing process per
installation, not multiple inference-enabled Uvicorn workers.
An explicit provider token-limit or filtering finish is treated as an incomplete
response, even when it includes valid charge information.

`serve --no-inference-worker` disables execution and fresh submissions while
retaining reads, cancellation, accepted-request replay, and reconciliation.
`--openrouter-url` accepts only the canonical OpenRouter HTTPS endpoint or an
HTTP loopback fixture; clients cannot redirect organization keys to arbitrary
providers. Provider redirects and environment proxy credentials are not used.

An administrator chooses routing under **Model capabilities → Provider routing**
or `PUT /api/organizations/{organization_id}/routing`, with `strategy` of
`recommended` (default) or `cheapest`, `zdr` boolean, and `region` of `global`
(default), `us`, or `eu`. The policy is captured in each accepted run's quote;
changing it does not silently reroute accepted work. All modes permit fallback
only within required parameters and privacy rules, without changing the model.
ZDR sets `provider.zdr=true` and `data_collection=deny`. US/EU use fixed regional
OpenRouter HTTPS origins and require an eligible provider account; unsupported
routes fail rather than falling back globally. Cheapest sorts by price and keeps
the quoted input/output ceiling. Recommended leaves sorting to OpenRouter and
does not impose the catalog's minimum price as an endpoint ceiling. See
[OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).

## Accounting and limits

Money is stored in integer millionths of a US dollar. Provider decimal charges
are rounded upward to that unit. The admission hold uses a one-token-per-three
UTF-8/framing-byte input estimate, the quoted prices, and either an explicitly
chosen output allowance or the 8,192-token reservation estimate. This estimate
is not a tokenizer measurement or an invoice cap and is never sent as a default
output-token limit. A running hold covers confirmed calls plus outstanding-call
estimates, not accumulated obsolete estimates. All selected models'
reservations are checked and committed in one write transaction, so competing
requests cannot independently reserve the same available balance.

Known charges settled during the current UTC calendar month count as spent.
Outstanding reservations and uncertain charges remain held across month
boundaries. Admission is blocked when their sum plus known spending cannot
cover a new request. Valid final usage settles the actual provider cost;
definite pre-submission refusals and HTTP 401/402/403 refusals before streaming
release the rejected call's reservation. Earlier successful calls in the same
turn retain their actual charges. An error inside an accepted stream, including
402, without trustworthy usage retains its reservation as uncertain. The
executor performs no automatic paid retries.

Without an explicit output ceiling, a call can exceed its output/cost estimate.
Known usage is settled in full even when it exceeds the reservation; budgets
then govern further admission. Local admission cannot guarantee an upstream invoice ceiling: provider billing,
tokenization, fees, other uses of the key, and cancellation behavior are outside
this ledger. Set spending limits on the OpenRouter credential and monitor its
activity. Usage includes the provider-reported total charged cost, not merely
the upstream inference cost; see [OpenRouter usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

Provider refusals expose a status-derived category, HTTP status, and action:
authentication, credit, permissions, request size, unsupported parameters, rate
limits, or provider availability. Credit refusals can also include allowlisted
`limit_source` and `reason` enums distinguishing key limits, balance/oversized
request estimates, and temporary concurrent spending limits. These follow
[OpenRouter's credit-limit contract](https://openrouter.ai/docs/api_reference/limits).
Raw upstream messages, error bodies, prompts, headers, and credentials are
excluded. A displayed held reservation is explicitly labeled
**Budget held**, separate from the provider's actual charge. Status guidance
does not establish whether an ambiguous submission incurred a bill.

For an uncertain run, use its generation ID and the provider's activity record
to verify the actual charge. An administrator can open Reconcile unknown charge
in the beta run panel, enter the verified USD cost, confirm that the record was
checked, and record it. A zero cost is appropriate only after verifying that no
charge occurred. Reconciliation settles the charge and removes the held
reservation. Generation lookup and automatic reconciliation are not implemented.
The inference ledger provides access to older calls. Runs with multiple
uncertain calls require verification of each call's provider record; their
charges are never divided by assumption.

## Usage and operational impact estimates

The bottom-left account menu opens **Model capabilities** and **Usage and
energy**. Capability and price metadata comes from the bounded public model
catalog, without organization API credentials or inference calls. Members can
search text-to-text models, filter by provider, tool use,
reasoning, or image input, and inspect modalities, parameters, limits, and all
reported price dimensions. Displayed catalog rates are not an invoice guarantee.
Public model choices and displayed prices share a one-minute server cache;
concurrent readers share one refresh, and failed refreshes retry after five
seconds. Membership and organization settings are checked on every request.
Inference admission fetches current availability and prices independently of
this display cache. The browser shares one copy of the catalog across topics and
dialogs for five minutes ("Refresh catalog" fetches it again), and a model's
detailed capabilities are built only when its row is first expanded.

The browser and model pickers share compact expandable rows. Topic selection
pickers are alphabetized within **Best for math**, **Best open-weights**, and
**Other models** sections; the main capabilities browser shows an ungrouped
searchable catalog without recommendation controls.
Each row shows the model ID once, catalog/organization availability, capabilities,
input/output USD per million tokens, and compact context/output capacities.
The open-weights section uses repositories explicitly reported by OpenRouter;
repository availability is not a license audit or a deployment benchmark.
Topic creation, beta replies, research goals/reviewers, and gamma model choices
use the same picker. The complete allowed catalog remains searchable.
After selecting a model, choose one of its advertised reasoning efforts; the
closed picker displays a single `model/effort` label. Default effort leaves the
provider or CLI default unchanged. Bot choices expose only models advertised by
the connected CLI and never fall back to OpenRouter. New beta topics can select
following-access bot routes before creation. Favorites are personal server-side
preferences, shared across that person's devices rather than the organization.
The versioned mathematics shortlist follows
[MathArena's mathematical evaluations](https://matharena.ai/models), selecting
the best published configuration for each distinct model and matching exact
IDs against the live catalog. Grouped pickers display the review date, evaluation
link, and tested setting. MathArena's expected performance combines observed
and estimated scores; different harnesses, reasoning effort, tools, and budgets
prevent treating these scores as measured performance of this installation.
No absent model is silently replaced by a similarly named endpoint.

The capabilities browser also shows OpenRouter's Artificial Analysis intelligence
and agentic indices, with coding in expanded details. These optional scores use
an authenticated organization OpenRouter key for metadata only, not inference.
Exact catalog canonical IDs match the reported model identifiers; missing scores
remain absent. Snapshots show their source, observation date and cache status;
successful reads cache for one day, failed reads retry after five minutes, and
stale snapshots expire after seven days. These benchmarks are not measurements
of this installation's research harness.

Optional ZDR and US/EU badges come from OpenRouter's documented filtered public
catalog queries (`zdr=true`, `region=us`, `region=eu`), cached for five minutes.
Failed metadata reads leave availability unknown. These indicate endpoints are
available, not that a particular request uses them: no country is inferred from
the model author or chat-server location. Regional routing may require an eligible
OpenRouter plan and regional API origin; ZDR requires request/account enforcement.
The default inference route is OpenRouter recommended with eligible-provider
fallback. Availability badges do not themselves enforce privacy or residency;
organization routing does. Browsing makes no paid inference calls.

Protocol failures retain a static, allowlisted parser-stage diagnostic (framing,
generation, usage, choices, content, reasoning, tool calls/validation, or encoding).
Raw provider payloads and exception text are not copied into error messages.
Streamed reasoning follows OpenRouter's
[reasoning details shape](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens):
`reasoning`/`reasoning_content` are strings or null, and `reasoning_details` is
an array of objects whose optional `type` is a non-empty string; `index` is an
optional non-negative integer; and `id`, `format`, `text`, `summary`, `data` and
`signature` are strings or null. A fragment continues the latest detail with
its index, or the latest detail when it has no index; its `text`, `summary`,
`data` and `signature` strings are appended in arrival order, and null values
add nothing. A fragment whose type, id, format or other non-text field differs
starts a new detail. Other types, such as `reasoning.server_tool_call`, and
unknown fields are preserved unchanged and returned with tool and output-limit continuations. Any other shape
fails the call with `protocol_field` (for example `reasoning_details[].index`) and
`protocol_value_type`, the JSON type received, named in the message.

A `provider_protocol` failure after submission is never retried automatically
and leaves the call's charge uncertain unless trustworthy usage arrived first.
To recover, read the call's generation ID and diagnostic in the inference ledger
or topic diagnostic export, and compare it with the OpenRouter activity record.
Then reconcile the verified cost as described above, and start a new turn
deliberately. Unknown charges or unresolved side effects still block automatic
goal continuation; known recoverable turn failures schedule a fresh durable turn.

`GET /api/organizations/{organization_id}/usage` returns all-time usage totals and
project breakdowns, optionally scoped by `project_id`. Each recorded beta or
workflow provider call contributes once, including continuation and compaction
calls, failed/cancelled calls with reported usage, and calls without a saved
response. Pending runs without a submission receipt contribute their held
reservation. Input includes the captured
model context; it is not assigned to each human message again. Token counts and
cost come from provider reports; outstanding reservations are shown separately.
Manual charge reconciliation changes the cost, not unreported token counts.
Totals include coverage counts for tokens, cost, energy, and carbon: known
partial totals must not be read as complete totals. Scope excludes CAS jobs,
human/bot message posting, unrelated provider calls, and other applications.

## Inference ledger

**Usage and energy → Inference ledger** shows individual provider calls. Each
immutable header records its actor, project/topic, model, parent call, purpose,
harness and prompt versions, context fingerprint, exact captured price quote,
routing rules, and reservation. Completion receipts append status, generation
ID, measured latency, reported tokens, actual cost, and sanitized diagnostics.
HTTP response-arrival timing and first-content/reasoning/tool-delta timing are
recorded separately when observed. Response-arrival time includes network and
upstream waiting and submission setup; it does not isolate a provider queue or
TCP connection time.
Run API views and topic JSON expose a compact `timing` object for local queue
delay, preparation before the first submission, the latest provider call, and
completed research-tool execution. Active waits distinguish local admission,
context preparation, provider waiting/streaming, and tool execution. Missing
historical measurements remain null. Timing does not change routing policy or
retry paid requests; compare identical prompts and effort under different
routing strategies before attributing model slowness to cheapest routing.
Cache, reasoning, total, and tool-token counts remain unknown when the provider
does not report them. Accounting reconciliation appends a new receipt while
preserving the original charge evidence. The database rejects updates and
deletions of headers and receipts. Historical records contain only information
actually retained; unrecorded prompt/harness versions are identified explicitly.

The browser filters by model, status, purpose, dates, project, topic, and actor.
Opening a call reveals its price schedule, routing, parent, and all immutable
receipts. **Export matching calls** downloads every matching call as JSONL.
Project permissions apply to list, details, and export; deleted topic content
and names are hidden. Organization accounting retains verified costs and held
reservations, anonymizing deleted project names.

Under `/api/organizations/{organization_id}/inference-ledger`:

- `GET` accepts `limit` (1–200), exclusive `before` sequence, `model`, `status`,
  `purpose`, `project_id`, `topic_id`, `actor`, and inclusive UNIX `since`/`until`.
  It returns `calls` and `next_before`.
- `GET /{call_id}` includes all original and reconciliation receipts.
- `GET /export` returns up to 1,000 matching JSONL records with the same filters;
  an `X-Next-Before` header supplies the next page when present.
- `PUT /{call_id}/cost` with `actual_microusd` lets an organization administrator
  record the verified cost of an uncertain call after its run stops. It also
  reconciles the enclosing run once every call is resolved. Administrators can
  reconcile retained accounting for deleted topics using the call ID.

`PUT /api/organizations/{organization_id}/model-recommendations` accepts
`{"model_ids": ["provider/model", ...]}` to pin an ordered organization
shortlist, or `{"model_ids": null}` to use the published recommendations.

Owners/administrators configure up to 100 per-model profiles under **Usage and
energy → Energy and data-center assumptions**, or replace them through
`PUT /api/organizations/{organization_id}/impact-profiles` with `{"profiles": [...]}`.
Each profile has `model`, nonnegative `input_kwh_per_million` and
`output_kwh_per_million` (at most 1,000,000 each), an assumed inference
data-center `location`, optional `grid_grams_co2e_per_kwh` (0–2000, or null), and
`source` describing references, year, and assumptions. Repeated models are
refused. Members can read these assumptions but cannot change them.
Sources and assumptions are shared with organization members; do not put
credentials or other secrets in these fields.

For provider-reported input `I` and output `O` tokens, configured coefficients
`a` and `b`, and location grid intensity `g`:

```text
estimated_kWh = (I × a + O × b) / 1,000,000
estimated_kg_CO₂e = estimated_kWh × g / 1,000
```

These are operational **scenario estimates**, not provider measurements or
life-cycle assessments. OpenRouter generation metadata does not establish the
physical data center; the chat server's location is not the model's location.
Providers may route across locations. Set grid intensity from an appropriate
location/time reference and include cooling/PUE in energy coefficients if
intended. Training, embodied hardware, networking, and user devices are not
included. An unconfigured run displays a labeled **reference** GPU-only scenario
of 1 joule per processed input or output token (1/3.6 kWh per million tokens).
This is an illustrative application assumption, not a coefficient measured for
the selected model or reported by the reference paper. The normalization follows
[Wilhelm et al., EuroMLSys 2025](https://arxiv.org/abs/2603.20224); actual hardware,
batching, context, caching, and architecture can change energy substantially.
No inference data-center region or grid intensity is guessed. Message summaries
show Wh and omit unavailable energy/carbon values. Organization/project totals
use compact spreadsheet rows; individual calls show Wh, with `*` for reference
scenarios and explicit coverage counts in the aggregate estimation details.

Each run snapshots the profile in its admission transaction. Retries preserve
that snapshot, and edits/removals affect new runs only. Historical/unconfigured
runs receive the reference scenario as a read-time view, not a database backfill.
Missing token counts leave energy unavailable; unknown grid intensity leaves
carbon unavailable. Explicit zero remains distinct from unavailable. Message
history, search, bot read responses, and model run records expose a `usage`
object for LLM responses containing tokens, cost, estimates, and captured
`assumptions`, and an `estimate_kind` of `configured` or `reference`. Read-time
reference estimates do not alter the original token, cost, or profile evidence.

## Verification

The following checks use temporary installations and local provider fixtures,
not production OpenRouter keys or paid completions:

```bash
uv run --extra test pytest -q tests/test_public_inference.py
npm --prefix web run build
(cd web && npx playwright install chromium && npm run test:public-inference)
```

The browser journey uses three account contexts and a real loopback HTTP/SSE
provider. It verifies key/budget policy, immutable group context, progressive
responses, committed-response loss and idempotent replay, shared model-labeled
math messages, cancellation, administrator reconciliation, server restart, and
narrow-screen layout. It does not verify a billed production OpenRouter call.
On Linux or macOS with a graphical session and built desktop dependencies,
`REASONING_BETA_TEST_DESKTOP=1 npm run test:public-inference` (from `web/`)
uses a private temporary `--user-data-dir` profile for the reviewing participant
instead of a third browser. The actual settings/session paths and profile lock
are checked. `REASONING_ELECTRON_TEST_EXECUTABLE` can select an unpacked client
executable. Normal desktop runs explicitly enable Playwright's Chromium sandbox
option and refuse an unexpected `--no-sandbox` switch. A test-only no-sandbox
launch is not production sandbox verification. These fixtures contain only
synthetic accounts and a local provider, never a production organization; they
do not verify native credential storage or a deployed HTTPS server. See the
[desktop verification instructions](../desktop/README.md#verification).
