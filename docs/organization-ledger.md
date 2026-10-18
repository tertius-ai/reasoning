# Canonical organization ledger

The organization server owns the shared research ledger, bibliography, and
private research blog. Beta topics use the typed mathematical claim/proof domain
directly, with revision-pinned dependencies and derived status. The **Ledger**
pane opens project-scoped records; the **graph icon in the top bar** opens the
organization ledger in the same application. Its scope selector chooses accessible
organization, project or topic mathematics. Humans can record and revise precise
statements. Agents record ordinary permitted research writes automatically;
positive attestation comes from independent review, not the author.

The ledger uses the shared workbench dependency graph and proof inspector:
zoom/fit controls, current/stale/broken dependency edges, exact proof/premise
revisions, verification reports and revision history. Every graph node has an
Expand button for the complete statement and a separate proof-inspection button;
expanded statements reflow their column instead of overlapping other nodes.
The statement list is a
separate scrollable view. Records are the canonical server ledger, not topic
transcript copies. Browser rendering never fetches external Markdown images.
The statement list and the graph each refresh every five seconds; **Refresh** and
recording a statement refresh both at once. Opening a statement reads its record
once and hands it to the inspector, whose code starts loading when a statement is
selected, pointed at or focused.

Humans can mark the inspected claim revision **Checked by a human**, optionally
adding a note. Checks retain the authenticated person's name and ID, timestamp,
exact revision, and canonical record hash. They do not establish a claim, certify
its proof, or imply formal verification. Revisions do not inherit checks: older
checks remain visible as earlier-revision judgments. Viewers and bots cannot
write human checks, and private scope is checked on every read and write.

The bibliography holds the canonical references cited by mathematical claims.
Expand a source and choose **Attach a statement** to record an exact theorem,
lemma, or definition with its page/theorem locator. This creates an ordinary
ledger statement linked to that reference, with an optional project scope and
an immutable source revision pin. Bibliographic corrections do not silently
replace that source evidence. Technical identifiers are collapsed by default.
Agents use `ledger_claim` with `reference_id`, `reference_revision_id`, and
`locator` for the same operation. These attributed statements are not automatically verified.
Private-source statements inherit their source scope and cannot broaden it through the source editor.
Private text artifacts and Markdown/TeX blog drafts have immutable revisions.
See [the organization research workbench](research-workbench.md) for the tools,
review process, context procedure, compute grants, and user interface.

## Blueprint discipline

Use the dependency graph as a general blueprint: precise statements, explicit
hypotheses, small proof obligations, pinned premises, and retained proof attempts.
Human checks, independent model reviews, and formal execution evidence are
different forms of evidence; none silently upgrades the others. The beta
**Blueprint** prompt profile guides decomposition with the same tools. Lean is
optional, and no separate formalization-specific ledger is required.

## Mathematical HTTP API

Under `/api/organizations/{org}/mathematics`:

- `GET` returns derived state; optional `q`, `project_id`, `topic_id` select search/scope.
- `GET /graph` returns the canonical dependency graph with the same project/topic scope checks.
- `GET /{ledger_id}?history=true` returns exact evidence or claim revision history.
- `GET /{ledger_id}/human-checks` lists retained human judgments on accessible
  claims, proofs, or references. `POST` accepts the inspected current
  `revision_id` and optional `note` (up to 2,000 characters). There is at most one
  immutable judgment by each human per revision; identical retries are safe.
  Exact `ledger_read` and per-claim `ledger_status` also expose at most 100
  judgments on the inspected revision, explicitly labeled `human-judgment`.
- `POST` accepts `name`, `arguments`, optional project/topic IDs and a stable
  `client_id` for writes. `ledger_status`, `ledger_search`, `ledger_graph`,
  `ledger_blockers`, `ledger_blast_radius`, `ledger_references`,
  `ledger_search_references` and `ledger_read` only read; every other operation
  is a write. Humans include `expected_revision_id` when revising a
  claim/proof/reference. Positive or machine-authority author verdicts are forbidden.
- `POST /import` takes `project_id`, `client_id`, and a complete `records` snapshot
  of `{path, record}` objects. Omitting `records` promotes eligible retained
  source archives in that project. An explicit `source_label` also selects that
  source's organization-scoped archives for import into the chosen project.
  Import is explicit, atomic, replay-safe, and
  retains IDs/history without trusting imported positive reviews. HTTP bodies
  are bounded to 64 KiB; larger complete snapshots must use retained source
  archives, not a truncated upload. No incremental typed import API is provided.

Generic knowledge notes and bot/import source archives remain separate from
typed mathematics. Their user-assigned labels do not determine mathematical
status. Existing source records are neither deleted nor automatically promoted.
Topic research-file routes and independent review routes are documented in
[the research workbench guide](research-workbench.md) and [inference](inference.md).

## Optional local Workbench connection

Create an organization bot with read/publish knowledge access using the
[bot onboarding guide](bots.md#codex-and-claude-code-connect-from-your-computer). Give it only the
message-topic grants it needs; reading the shared ledger does not grant access
to alpha-topic messages. Configure its credential outside the checkout:

```sh
mkdir -m 700 -p "$HOME/.config/reasoning/bots"
uv run --locked reasoning-bot configure --server https://chat.example.org \
  --output "$HOME/.config/reasoning/bots/workbench.json"
uv run --locked reasoning-bot identity \
  --config "$HOME/.config/reasoning/bots/workbench.json"
```

Replace the server URL and local ledger path with your own. To retain existing
research centrally, import it explicitly before linking:

```sh
uv run --locked reasoning-ledger --root /path/to/project/ledger organization-import \
  --config "$HOME/.config/reasoning/bots/workbench.json" \
  --source-label algebra \
  --state-dir "$HOME/.config/reasoning/ledger-imports/algebra"
uv run --locked reasoning-ledger --root /path/to/project/ledger organization-link \
  --config "$HOME/.config/reasoning/bots/workbench.json"
uv run --locked reasoning-ledger --root /path/to/project/ledger organization-status
```

Import requires a clean committed source ledger. It preserves every complete
claim head, revision, proof, verification, and bibliography record, including
original IDs, authorship, source paths, Git commit, and record hashes. Historical
revisions are separate immutable source entries, not inferred replacement
claims. Source claim status is retained as provenance; import publishes ledger
notes and never establishes a mathematical claim automatically. Original local
records are neither deleted nor rewritten.

Use the same source label, scope, bot, journal directory, and unchanged source
on retries. A lost write receipt is reconciled through stable request IDs.
Bots allow 30 write attempts per minute: a larger import can stop at the quota;
wait at least 60 seconds and rerun the identical command to continue. Oversized
source records are refused before publishing; they are not truncated.

Both commands default to the global organization write destination. Add
`--scope-kind project --scope-id PROJECT_ID` or
`--scope-kind topic --scope-id TOPIC_ID` to select a local ledger scope.
Topic scope requires the exact bot grant; private projects require a grant to
at least one topic in that project. Reads can still retrieve all entries
visible to the bot; the write destination does not narrow its existing grants.

## Connected behavior

The mode-0600 `.organization-ledger.json` in the local ledger stores only the
organization identity, destination scope, and private bot-configuration path.
Git excludes this pointer locally; it contains no credential. Moving the ledger
retains its link and local-write fence, but the credential configuration must
remain accessible at its absolute path. Restart a running Workbench/MCP process
after linking so it rebuilds its tool registry.

The Workbench ledger pane displays canonical entries and links to the
organization site for human editing. Agent tools `ledger_status`,
`ledger_search`, `ledger_graph`, `ledger_read`, `ledger_note`, and
`ledger_revise` use the server; direct MCP sessions use the same tools.
Search/status responses are bounded previews, and `ledger_read` retrieves
revision-pinned chunks. Writes use explicit stable request IDs and optimistic
revision checks.

Connected local ledger mutations are blocked, including the native CLI/API.
Local compute/workflow tools remain available, but local claim promotion and
verification tools do not become a second authoritative store. Server or
credential failure is an error, never a silent fallback to local writes.

Bots may propose notes and conjectures. Establishing/refuting a claim or editing
an already reviewed claim requires a human in the organization UI. Existing
reviewed source records remain readable as provenance. Credential expiry,
revocation, sponsor changes, private visibility, and exact topic grants are
rechecked by the server for every operation.
