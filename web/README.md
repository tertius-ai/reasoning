# reasoning workbench — web UI

The organization entry point serves beta research sessions with a typed
mathematical ledger, shared versioned files, independent review reports,
context inspection, goals, and private Markdown/TeX blog editing. It is also
the default desktop client's frontend. See the
[organization research workbench guide](../docs/research-workbench.md).
The separate local entry point described below has its own backend.

Research workbench UI with a narrow, persistent activity rail; a compact
Explorer tree for projects, chats, workflows, and saved runs; the selected
chat or workflow in the center; and a resizable ledger browser on the right.
Chats and workflows are equal-weight children of their project, while saved
runs expand beneath the workflow that produced them. Chats drive resumable
Codex or alternative-agent sessions with markdown, KaTeX, streamed reasoning
summaries, and tool activity. Visual language follows Hermes Agent's dashboard
theme (MIT; NousResearch/hermes-agent).

The initial HTML embeds the last valid, non-secret Explorer index so projects
and chats paint before the first API round trip. The cached tree carries an
explicit reconnecting or stale state until `/api/projects` reconciles it; live
server state is authoritative, and invalid cache data is ignored.

An organization-linked project uses the server's canonical research ledger.
The right-hand browser searches and pages through shared entries, showing scope,
claim status, revision, authorship and retained import provenance. Workbench
agents and MCP use the same server-backed ledger tools; independent local ledger
writes are refused. Connect a scoped bot configuration and explicitly import
source records through the [organization-ledger guide](../docs/organization-ledger.md).
Standalone projects retain their git-backed ledger. The private organization link
is stored at `ledger/.organization-ledger.json` and excluded from ledger Git history.

Storage layout under the workbench root:

    <root>/projects/<project-id>/
        project.json
        ledger/                 standalone ledger or private organization link
        artifacts/              shared by chats and workflows
        agentic/workflows/      editable, project-specific workflow graphs
        agentic/datasets/       reusable immutable case datasets
        agentic/workflow-runs/  saved workflow-run state
        agentic/cache/scientific/ provenance-preserving provider cache
        chats/<chat-id>/
            chat.json           title
            chat-thread.json    resumable codex thread id
            chat-transcript.jsonl
            scratch/            the chat's private working directory

Reusable workflow templates live at the workbench root. Creating a
workflow copies the selected template into the project so later edits are
isolated from the template and from other projects. The New Workflow
dialog offers the shared templates alongside an empty workspace and can
delete unwanted templates; deleted seeded examples remain deleted after
the application restarts.

All workflow definitions share one contract: cases × variants run through the
agent graph, then deterministic evaluators inspect saved outputs. Cases can be
repeated iterations, an embedded case array, or an immutable project dataset
pinned by revision. The Plan inspector edits that contract, selects the focused
view, previews the expanded call count, and sets concurrency, failure policy,
maximum calls, and a run-wide cost limit. Its draft capability preflight checks
every effective model route before the run button is enabled. Focused views do
not fork the underlying storage or scheduler, so a comparison or benchmark can
always be opened in Plan or the graph editor without conversion.

The Compare models template opens in a focused setup instead of the general
canvas. One question is bound to every independent agent, the model roster can
grow from two to six entries, and ready siblings run concurrently with a
bounded concurrency policy. Each result card appears as its call completes and
shows executor, provider, model, duration, tokens, finish reason, prompt
fidelity, and provider-reported or estimated price. The setup checks direct
model IDs against OpenRouter's current catalog and the run endpoint repeats
that check against the saved graph. Catalog-advertised agentic-harness-only
models are rejected for the direct Reasoning Native route with guidance to use an
agentic executor. A stale or unavailable catalog is reported
without pretending that availability was verified. A failed branch remains
visible without discarding successful siblings; the run is marked partial,
and provider errors are reduced to public actionable messages before storage.
The underlying object is still an ordinary immutable graph, available through
Edit graph for prompt, skill, tool, budget, and topology changes.

The Paper moderation template opens a focused benchmark setup. Its editable,
versioned corpus labels each submission `allow`, `review`, or `reject` and marks
malicious code, prompt injection, low-quality slop, and offensive content
independently. The starter corpus includes benign controls and adversarial TeX
and Markdown text. Every case is serialized as untrusted JSON data, expected
answers stay outside model prompts, and benchmark agents have no tools or
network access. The model browser can select up to twenty-four OpenRouter
models, with cheapest and provider-spread presets. Runs use bounded concurrency
and persist a ranked scorecard with decision accuracy, flag F1, false allows,
JSON compliance, latency, cost, per-case errors, and raw responses. Edit graph
remains available for the pinned rubric, budgets, and advanced topology.

The Generate a dataset template requires schema-valid structured output. A
completed run can promote its cases into the project dataset library, where
subsequent workflows pin an exact revision. Expected case labels are retained
for evaluators but are never included in agent prompt variables. Exact match
supports plain text and structured JSON; additional evaluator kinds use the
same post-run registry and are recorded with the resolved execution plan.

Each workflow execution checkpoints its state under `agentic/workflow-runs`
and continuously updates a human-readable Markdown report plus a complete
JSON record under `artifacts/workflow-runs`. Those reports are ordinary project
artifacts. Research chats receive a compact live inventory of project runs and
artifacts and can inspect their complete contents through read-only project
tools, separately from the claim ledger. Assistants present useful generated
artifacts as explicit per-file links. The reader is a persistent right-hand
inspector, so the active chat or workflow remains available while a file is
open. Linked artifact directories open a browsable in-app listing whose entries
use the same artifact reader. Existing UTF-8 text artifacts open edit-first and
can be previewed, reverted, and saved from that reader. Library can open the
artifact root for whichever project is selected there, independently of the
project currently open in Explorer.
Run records total executor-reported API prices and explicitly count calls
whose executor did not report a price. Per-call budgets remain upper bounds;
the optional run-wide limit stops scheduling new work after recorded spend
reaches the limit, while already-active concurrent calls may finish.

While a workflow is executing, the canvas is locked to the run's saved
revision. Active nodes pulse, incoming edges carry a directional signal,
and completed or failed paths remain visibly marked as the run advances.
Motion is suppressed when the operating system requests reduced motion.

The activity rail opens Explorer, quick navigation, the project ledger, the
global Library, the Price browser, and Settings without text-heavy chrome.
Library opens in its global scope while retaining project and chat views; its
project selector can target any active project without changing Explorer. The
Price browser compares the live OpenRouter public catalog, or the account-
filtered catalog when an OpenRouter key is configured, in USD per million
tokens, with context, reasoning, and tool-capability filters. It also shows the
append-only, prompt-free accounting trail for direct Reasoning Native calls:
provider-reported spend and token totals, project aggregates, and expandable
per-call routing and termination detail. The shared
agent/model control exposes that catalog as an `Other model…` dropdown choice
in research chats and workflow nodes; choosing a row sets that model on Reasoning
Native and adds it to a bounded workbench-wide recent-model list.
The browser labels Codex token estimates and provider-delegated executors
separately from machine-readable tariffs and realized direct-call costs, and
keeps the last successful catalog available with a visible stale warning when
a refresh fails. Codex chats compare aggregate Codex token counters with the
selected model's live OpenRouter input, cache-read, and output rates when that
model is present in the catalog. The composer labels the result as estimated;
Codex does not expose OpenRouter's provider-reported charge, and separately
metered request, media, search, or hidden reasoning charges are outside the
estimate.
Explorer can collapse to the rail alone;
its disclosure controls independently expand projects and workflow runs, and
its Archive entry opens a dedicated view for restoring archived projects and
chats or workflows. Project, chat, and workflow overflow actions use compact
vertical menus; chats and workflows can be pinned, renamed, archived, restored,
or deleted with the same lifecycle vocabulary. A collapsed project carries the same live activity pulse as a running
child. A chat waiting for permission replaces that pulse with an amber
attention dot, which also propagates to its collapsed project. Projects keep a
stable newest-first creation order instead of moving with activity; pinned
projects form a chronological group at the top, separated from the remaining
projects by a quiet rule. Pinned chats and workflows likewise stay above their
unpinned peers inside their project. The top-bar
quick-navigation field (Command-P on macOS, Control-P on
Linux and the web) searches projects, chats, project workflows, saved runs,
and shared templates and exposes common creation actions.

Modal dialogs assign initial focus once, retain a keyboard focus trap for
their lifetime, and return focus to their opener on close. Background state
refreshes do not pull focus from the control being edited.

Settings → Connections treats Reasoning Native as the shortest ordinary-chat
path: save and verify one OpenRouter key without terminal setup. Codex is
optional. Its setup control generates a short prompt for a working Codex
session to update the workbench-owned user `config.toml`, reference the saved
key only as `OPENROUTER_API_KEY`, and preserve project `.codex` policy. A
single adjacent test runs a bounded read-only tool turn through that native
configuration and records a simple valid, invalid, or untested result.

Each chat keeps its unsent composer draft under a chat-specific browser key,
so switching chats or restarting a desktop webview does not discard it.
Sending returns the transcript to its live edge. Expanding an intermediate
step group shows complete reasoning summaries plus bounded command and tool
details; completed groups open at their first step, while running groups stay
at the live edge.

With interactive approvals enabled, pending Codex sandbox escalations stay in
an action dock immediately above the composer. They are returned with every
history snapshot, so switching chats or reconnecting cannot hide them, and the
UI reports approval only after the Codex app-server acknowledges the decision.

## Run

```bash
# from the repository root
uv sync --locked --extra test
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build

# serve on loopback
REASONING_WORKBENCH_ROOT=/path/to/workbench uv run reasoning-ledger-web
# then open http://127.0.0.1:9119
```

Configuration (environment): `REASONING_WORKBENCH_ROOT` (default
`${XDG_DATA_HOME:-~/.local/share}/reasoning`),
`REASONING_CHAT_MODEL` (default `gpt-6-astra`), `REASONING_CHAT_SANDBOX`
(default `workspace-write`), `REASONING_APPROVALS` (`interactive` to surface
escalations; otherwise they are declined), `REASONING_APPROVAL_TIMEOUT`
(default 600 seconds), `OPENROUTER_BASE_URL` (optional OpenRouter API override),
and `PORT` (default 9119). Sessions get the
project ledger over MCP, web search, the shared artifacts directory
(sandbox-writable via --add-dir), and their private scratchpad as cwd —
which starts empty and must stay free of operational scripts. The ledger
usage instructions are injected automatically on the first turn of each
thread; deleting a chat's `chat-thread.json` starts its thread fresh.
Explicit development-workspace chats always surface approvals because their
selected role includes scoped repository writes; the environment setting
controls ordinary research chats.

The backend sends the workbench page with a `Content-Security-Policy` response
header that allows only same-origin scripts and forbids framing. The entry
pages carry no `<meta>` policy, and `npm --prefix web run build` fails if an
emitted page contains inline script or an inline event handler.

## Development

```bash
npm --prefix web run dev  # Vite dev server with /api proxied to :9119
```

The chat backend maintains a Codex app-server session for each active chat,
streams its events to the browser as SSE, and persists the thread id and
transcript in the working directory. The ledger column re-derives on every
ledger tool call the session makes.
