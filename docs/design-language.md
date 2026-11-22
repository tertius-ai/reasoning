# Reasoning design language

Reasoning should feel like a beautiful research instrument: part
museum reading room, part early networked workstation, and part contemporary
scientific control surface. It is not a generic SaaS dashboard. The interface
should make dense mathematical work calm and legible.

## Character

- **Archival, not nostalgic.** Neutral or warm dark fields, restrained research
  accents, and clear typography evoke a serious instrument without decorative
  skeuomorphism.
- **Technical, not cyberpunk.** Monospace type, graph paper, ports, and compact
  status marks carry meaning. Avoid glow, gratuitous neon, and console cosplay.
- **Editorial where it matters.** A destination such as Library may use a
  large title and generous negative space. Repeated work surfaces stay dense.
- **Quietly alive.** Motion explains state—an active workflow edge, a running
  node, a status pulse. Nothing moves merely to decorate an idle screen.

The useful references are the disciplined information density of VS Code, the
monospace research identity of Nous Research, and the explanatory precision of
Modal's GPU glossary. These are cues, not templates to copy.

## Visual system

The canonical semantic tokens live at the top of `web/src/styles.css`. The
default Studio appearance uses a neutral charcoal canvas with quiet gray
surfaces and uses green for healthy or active state. Ink offers a
warmer, bookish dark palette. Teal uses deep-teal surfaces
with restrained contrast. All three presets map the same tokens:
canvas, surfaces, borders, primary and secondary text, focus/link blue, success
green, attention amber, and destructive red. New components must use those
tokens rather than palette-specific literals. The selected preset is local UI
preference, applies immediately, and never changes research data.

Dark mode is the only complete contrast mode until every legacy surface uses
the semantic system. Do not expose a partial light mode; add it only with the
same screen-size, native-control, and contrast verification as the dark
presets.

Use the bundled IBM Plex Sans for prose, navigation, buttons, labels, and
controls that benefit from rapid scanning. Use the bundled IBM Plex Mono only
for identity and genuinely technical values such as paths, models, amounts,
revisions, and machine state. Platform fonts remain the
fallback for scripts and glyphs outside the bundled Latin coverage.

Corners come from four shared tokens rather than per-component guesses:
`--radius-composer` (20px) for the message composer, `--radius-control` (10px)
for buttons and list rows, `--radius-card` (12px) for cards and popovers, and
`--radius-pill` for chips and circular icon buttons. Pills are for chips and
single-glyph controls; broad surfaces keep the structural radii.

Hairline borders, faint graph rules, and negative space should establish depth
before shadows do. Shadows are reserved for true overlays, where
`--shadow-overlay` is the one soft drop shadow. Every icon should be
a simple line glyph with a tooltip or accessible name; never make color or an
unfamiliar symbol the only explanation.

Motion is short and eased: `--motion-fast` and `--motion` with `--ease-out`
cover hover, active, and expand; overlays fade in with a small scale. Every one
of those transitions and overlay animations is switched off under
`prefers-reduced-motion`, which is the last block in `styles.css` to touch
them and repeats each selector at its own specificity so it always wins. The
status pulses that report live work are a separate, deliberate exception.

The local workbench transcript reads as a conversation, not a log. What the person said is a
right-aligned bubble on the second surface, at most three quarters of the
column wide. What the agent wrote is plain prose on the canvas at the full
column width, with its timestamp appearing on hover. Runs of intermediate steps
collapse into a quiet chip carrying a rotating chevron, the step count, and a
sans summary; opening one turns the chip into a card. Tool, review, and
approval cards are hairline cards at the card radius, and their state is a
small chip inside the card rather than a coloured border around it. A live turn
with nothing landed yet shows a three-dot indicator. Finished tasks recede into
muted text behind a check mark; nothing in the interface is struck through.

## Information architecture

The organization chat uses a shared app bar, project/channel/topic navigation,
and a primary group conversation. Topic kinds use alpha, beta, and gamma glyphs
with accessible names, not color alone. Project, channel, and topic creation
controls sit at the hierarchy level they affect. Channels use alternating-color
`#` labels without disclosure arrows; topic labels join an italic,
alternating-color Greek glyph to the name with a hyphen. Author labels and
timestamps identify participants. Unread, mention and reply badges reflect server
state; unfollowed topics remain accessible but recede in the navigation. Amber
marks what is for the reader: `@` with the number of mentions and `↩` with the
number of replies to them, never color alone, added up on collapsed channels,
closed projects, the sidebar header and the navigation toggle.
Organization creation is an owner/administrator action and presents the fresh,
explicitly reused, and no-AI credential choices before creation. The sidebar overlays the
conversation below 700 px; its always-visible toggle and Ctrl/Command+B open
and close it. Escape dismisses menus and dialogs, not project navigation. The settings dialog uses
native focus containment and explicit confirmations for access changes.
Conversation content supports Markdown and mathematics without rendering raw
HTML, external images, or member-name Markdown. A disconnected view keeps its
loaded transcript visible and explicitly reports reconnecting state.
Beta topics place a compact single-model selector beside Send. Its account-menu
style popover summarizes the fixed topic-history and scoped-knowledge policy and
the provider-reported output maximum; exclusion details stay behind a disclosure.
Ordinary completed runs recede into the
transcript; active, failed, cancelled, and uncertain runs retain clear recovery
controls. Live text is plain progressive output; saved replies use the
transcript's Markdown/mathematics rendering and explicit LLM labels.
Partial answers are marked incomplete. Unknown charges remain visible with
administrator reconciliation controls rather than appearing as free runs.
Gamma topics keep immutable definition authoring separate from explicit run
submission. The default surface is a saved-workflow list plus two primary entry
points: a common-question model comparison and an ordered AI/approval/compute
builder. Raw definitions and digests are advanced evidence, not the starting
interface. Shared run views identify the pinned version, revision, ordered node
states, and permission boundary. Approval, cancellation, and retry actions
name their exact target; attempt history, dependency captures, and linked
results stay behind inspection controls. Uncertain requests keep their original
identity across in-app navigation, and restored history is visibly fenced from
new execution. Read-only participants can inspect the same progress and results.
The app bar exposes a collapsible right presence pane. It separates active
people from sponsored bots and treats recent activity as advisory presence, not
as proof that an external process or executor is healthy.

The local workbench's persistent hierarchy is:

1. the native app bar and fast navigation;
2. the narrow activity rail;
3. one persistent or collapsible contextual explorer or library pane;
4. the primary chat, graph, run, library reader, or ledger surface;
5. an optional inspector or ledger pane.

The local workbench shell is chat-first. Its centered quick-navigation control and
narrow activity rail remain visible. That control is a subtle rounded search
field carrying a magnifier glyph and its shortcut as a key chip; opening it
raises the command palette, whose rows are sans and whose group headings are
small sans labels rather than monospace capitals. The app bar holds one line
at every supported width: the wordmark never wraps, the breadcrumb ellipsizes,
and the command field is what gives up width first. On desktop, Explorer starts
open as a resizable left column, the active conversation or work surface owns
the elastic center, and ledger context opens on demand as a resizable right column. These
panes participate in layout flow: they never cover transcript text or the
composer at desktop widths. At compact widths, side context may become a
dismissible overlay so the center retains a usable reading width. The current
project and work surface remain legible in the app-bar breadcrumb when a pane
is collapsed.

Projects contain peer chats, workflows, and saved runs. Do not nest
workflows under chats or make “research” and “workflow” competing top-level modes. Global
libraries—sources, prompts, skills, templates, connections—are visible
from any project and acquire local views or grants where needed.

Library opens to the global catalogue. Its project and chat selectors are
independent of the Explorer's current selection, so local source views,
research actions, and project artifact browsing can target another project
without first leaving Library.

Prefer disclosure over explanation. Put frequent destinations in the activity
rail, project objects in the explorer tree, cross-workbench navigation in the
command palette, and infrequent configuration behind compact Settings tabs.
Those tabs are sans chips with a filled selected state, and Settings' own
cards follow the shared card radius with their readiness shown as a chip
rather than a coloured border.
Use prose when a permission boundary or scientific distinction genuinely needs
explaining; remove labels that merely restate the visible structure.

The chat composer is one rounded container sitting directly on the canvas —
no band behind it — holding the message field above a single row of controls:
a circular attach button, the run summary chip, the details disclosure, and a
filled circular send control carrying the foreground color. A live turn adds an
outlined stop control beside it. Send carries that fill only when a message can
actually be sent: while the connection or run configuration needs attention it
is disabled, and the row carries a quiet inline "Set up" link — never a filled
block — as the action instead. The composer keeps the active agent/model and
reasoning effort visible in a single compact summary. All selectors and
automatic routing live behind that one-click disclosure; an idle composer must not present a configuration form.
Connection diagnostics, pricing source, context behavior, prompt fidelity, and
token usage stay behind one compact disclosure so an idle composer remains a
quiet place to write. Exact token tariffs belong in the global Money view,
whose current-prices section uses live machine-readable provider catalogs and
distinguishes them from estimated token spend and realized CLI run costs. A
separate usage-history section keeps the append-only, prompt-free trail of
direct Reasoning Native OpenRouter calls, exposing provider-reported spend and
tokens first by project and then per call; unpriced calls remain visible and
outside spend totals. A Codex chat may apply
the selected OpenRouter model's published token rates to Codex's aggregate
token counters, but the composer must label that value as estimated and omit it
when the selected model has no catalog match. Never present heuristic cost
classes as exact prices. Price rows expose provider-reported reasoning and tool
capabilities beside the rates. The agent/model dropdown opens that catalog from
an “Other model…” option labeled as OpenRouter via Reasoning Native; selecting a
row returns to the originating chat or workflow node and switches its executor
explicitly. Recently selected OpenRouter model IDs remain available in later
agent/model dropdowns, with a short bounded list that does not obscure the
built-in choices. User-starred OpenRouter models form a durable ordered group
above recents and remain available across restarts. The catalog may be sorted
by task-compatible Reasoning run evidence when opened from a workflow, but it
keeps provider rates and capabilities visually distinct from observed
reliability, latency, cost, and evaluator scores.

## Interaction rules

- A user should always know what project, chat/workflow/run, agent/model, and
  permission boundary are active.
- The Explorer should paint its last non-secret server snapshot immediately,
  visibly mark it as reconnecting or stale, and reconcile in place. Cached
  navigation is never authoritative and must not turn a missing live object
  into a destructive action.
- Provider onboarding should offer one short primary path and one real test.
  Saving and verifying an OpenRouter key is sufficient for Reasoning Native.
  Optional Codex setup may provide a pasteable native-config prompt and a
  bounded tool-using test, but must never write a secret into config or hide
  provider overrides in a launch command.
- Research scratchpads and repository-writing development chats must look and
  read differently before the first message is sent.
- Destructive actions require an explicit confirmation state and should say
  whether recovery is possible.
- The Explorer names things in sans and lays them out as rows at the control
  radius: hovering fills a row, and the open one keeps that fill. Its header
  carries one prominent action — a new chat in the current project, or a new
  project when none is open. Drag handles and compact menus appear on hover or
  keyboard focus, so an idle Explorer shows names and nothing else. The
  activity rail marks its active destination with the same filled shape behind
  the icon.
- Projects and chats keep a durable user-defined order. Their drag handles,
  keyboard reorder controls, and compact-menu move actions all change the same
  persisted sequence, which takes precedence over recency and pinned state.
  The Explorer keeps every active project and chat visible; archiving is the
  explicit way to move one out of that list. Compact vertical menus also hold
  rename, archive/restore, and delete actions without widening the Explorer.
- Workflow nodes drag from a palette, connect port-to-port, animate during a
  run, and save every run as a browsable artifact with cost and provenance.
- One workflow noun covers organizations, comparisons, benchmarks, and data
  generation. Its visible plan is cases × variants × graph, followed by
  evaluators. Cases may come from repeated iterations, an embedded corpus, or a
  pinned immutable project dataset. The Plan inspector shows this expansion,
  its scheduled-call count, concurrency and failure policy, maximum calls, and
  run-wide cost limit before launch. Model-capability preflight is server-backed
  and blocks launch while pending or incompatible; it is never inferred from a
  model name alone.
- Project chats may create and revise that same workflow object. They save and
  pin immutable role and task prompt revisions into its nodes, import or reuse
  an immutable dataset, and select explicit model variants from a disclosed
  catalog ordering before preflighting one complete immutable workflow
  revision. A request such as “test the 20 cheapest models on this benchmark”
  should yield a ready-to-review cases × variants → harness graph → evaluators
  composition, not an empty workflow that asks the user to wire internal
  objects. Chats expose reasoning as a per-agent setting and
  leave the result as a reviewable draft unless the current request explicitly
  asks to run it. A chat launch pins the approved revision and repeats live
  preflight, so a concurrent edit cannot silently change what runs. At the end
  of the chat turn, the newest authored revision opens through in-app state
  navigation without a window or application reload. Continuation turns wait
  for true completion before changing the active surface.
- Focused workflow recipes simplify common jobs without creating parallel data
  models. `presentation.recipe` is the authoritative renderer contract. A known
  recipe opens its specialized studio; a general graph or dataset generator
  opens a readable immutable overview of its organization, exact pinned prompt
  bodies, executors, models, reasoning effort, context, tools, skills,
  evaluators, and guardrails. Switching between an overview or focused recipe
  and the graph editor preserves the same prompts, variants, datasets,
  evaluators, guardrails, and revision history. Advanced configuration remains
  available in Plan and the graph inspector instead of being duplicated into
  every focused surface. Revising the selected workflow head refetches that
  revision in place.
- Direct model comparison is a focused workflow recipe: one prominent question,
  a compact two-to-six-model roster, concurrent failure-isolated progress, and
  side-by-side result cards. Current-catalog preflight catches retired direct
  models and catalog-advertised agentic-harness restrictions before launch; an
  unavailable catalog is labeled as unverified rather
  than silently treated as success. Each card keeps its executor, serving
  provider, model, duration, total and reasoning tokens, finish reason, prompt
  fidelity, and price basis visible. Failed calls distinguish "before usage"
  from completed calls whose executor did not report a price.
  Task-aware recommendations can replace the roster with two to six observed
  configurations ordered by benchmark quality, quality/cost frontier,
  reliable latency, reliable cost, or publisher diversity. Recommendations
  explain their criterion and compatible-run count, exclude configurations
  that current executor and catalog readiness cannot reproduce, and preserve
  the tested executor, provider, model, and reasoning effort. Quality modes
  remain unavailable until evaluator evidence exists.
  Advanced prompts, skills, tools, budgets, and topology remain available
  through the ordinary graph editor rather than crowding the comparison setup.
- Reusable model benchmarks are focused, corpus-first workflow recipes. Paper
  moderation exposes editable labeled cases, a broad but bounded model roster,
  call and budget estimates, and a ranked scorecard with quality, false-allow,
  structured-output, latency, and cost measures. Malicious code, prompt
  injection, slop, and offensive content remain separate flags; submissions
  are serialized as untrusted data, answer keys never enter prompts, and the
  default screening agents have neither tools nor network access. Case-level
  mistakes and raw responses remain inspectable, while prompt and topology
  customization stays in the graph editor. A benchmark may reuse the same
  task-aware tested-model sets used by direct comparison; live-catalog
  cheapest and provider-spread presets remain separate catalog-only sweeps.
- Dataset generation is a focused workflow recipe whose output is schema-bound.
  A completed generator run may promote its cases into the project dataset
  library as a new immutable revision; no unvalidated prose is silently treated
  as a benchmark corpus.
- Empty states should offer one concrete next action, not a tour of the UI.
  They centre a short sans greeting with one sentence under it; where an
  empty state needs actions of its own they are quiet pills, never filled
  blocks. An empty chat's greeting sits centred with the composer directly
  under it — the composer is already the action — and the composer settles
  into its docked position at the foot of the column once the first message
  lands.
- Keyboard navigation, visible focus, reduced-motion preferences, narrow
  windows, and screen-reader names are release requirements.

## Platform contract

The organization client uses native window frames on macOS and Linux. Its
Connection menu exposes server switching and local-session forgetting without
giving the remote chat a native bridge. The connection screen is app-owned;
the group conversation is the same server-served interface as the browser.

The local workbench on macOS uses its hidden inset title bar and native traffic
lights inside the app bar, and gives its window the system sidebar vibrancy: the activity rail and
the Explorer are translucent there while every other surface paints its own
opaque canvas. Linux keeps the window manager's native frame, removes
Electron's generic menu bar, and stays opaque throughout. Do not simulate
macOS controls on Linux or sacrifice native window behavior for pixel
identity. Shared content, spacing, keyboard paths, icons, loading states, and
feature availability must otherwise remain equal.

Every desktop release should be checked at 1440×920, 920×640, a
compact browser-width layout, and a narrow overlay layout. Test keyboard-only
creation and navigation, long mathematical names, missing executors, stale
repository paths, empty and populated libraries, active workflow motion, and
`prefers-reduced-motion`.

In the local workbench, at widths up to 1180 px the Ledger is a dismissible
overlay with a backdrop, close action, and Escape behavior; it must never reduce the chat to an unusable
sliver. The workflow toolbar may reflow, hide a saved revision identifier, or
shorten secondary labels, but the workflow name and run action must remain
available at the 920 px desktop minimum. Settings keeps its title and tabs
stable while its active section scrolls at 920×640.

Local-workbench artifact paths in chat open the shared in-app reader as a persistent right-hand
inspector, keeping the chat or workflow visible and interactive. Images and PDFs
render inside that reader without depending on a browser extension or
operating-system viewer; its same-origin open action works as a browser tab.
Artifact directory paths open a compact in-app listing whose files and
subdirectories use the same reader; Library can open a selected project's
artifact root directly. UTF-8 text artifacts open in an editor by default and
can be previewed, reverted, and saved in place. The extra-window action is
browser-only; desktop keeps artifacts beside the active work surface in its
native workbench window. On desktop, every rendered artifact reference,
catalogue row, directory entry, and open inspector exposes the same compact
reveal action. Electron resolves only the selected project's existing artifact
tree, follows real paths, and refuses traversal or symlinks that leave that
tree before asking Finder or the Linux file manager to reveal the target.
Narrow layouts may overlay the inspector to preserve a usable reading width,
but it remains non-modal. Raw artifact responses carry their native media type
and inline disposition so direct links remain useful on every platform.

For repositories with native or generated prerequisites, the owner can save an
argv-only readiness check and setup command in Reasoning's private workspace
settings. The mapping belongs to the application, while generic commands such
as `make bootstrap` remain ordinary repository interfaces. Merely registering,
browsing, or editing the repository never runs these commands. Settings shows
the configured environment as a separate readiness line, offers an explicit
check and confirmed setup action, and preserves its diagnostic across renderer
reloads.

## Review test

Before accepting a broad UI change, ask:

- Is the active object and its scope obvious without explanatory copy?
- Did the change reduce visual hierarchy to the fewest useful layers?
- Does every ornament encode state, structure, or identity?
- Does it still feel at home beside the Library reader and workflow canvas?
- Can it be used with keyboard, narrow window, reduced motion, macOS, and
  Linux?
- Would a working mathematician leave it open all day?
