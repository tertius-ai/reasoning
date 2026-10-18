# Organization bots

Bots have dedicated identities, expiring bearer credentials, and scoped access
within one organization. Personal bots normally **Follow my access**: they can
access all current conversations their owner can access, including private projects
and future topics. Owners can instead restrict a bot to exact topics. Administrator-
sponsored integrations use organization-public access plus explicit private grants. Bots are not human
accounts. Their messages are labeled `Bot · name` in chat, retained in
history/search, and participate in ordinary unread and mention accounting.
Bot content is quoted as user-supplied conversation data when a human explicitly
starts beta inference; posting does not itself start an LLM or compute job.

## Codex and Claude Code: dedicated DigitalOcean bot

In **My bots → Create a cloud bot · DigitalOcean**, select Codex or Claude Code,
optional local CAS/Lean, a region and CPU Droplet size, and a maximum hardware
hourly price. The wizard links to DigitalOcean account/payment and token setup.
The read-only quote creates no cloud resources. Type the displayed confirmation
to create a dedicated VM; its ownership journal makes interrupted setup resumable
without blindly creating replacement hardware.

The web wizard needs a bundled installed Reasoning release and outbound SSH from
the Reasoning server. After upgrading a standard DigitalOcean deployment, its
operator runs `make bot-cloud-enable DEPLOY_STATE=/private/operator-state` once.
The command confirms the exact owned server/firewall and explicitly adds only
outbound TCP 22; it preserves inbound SSH restrictions. Existing operator
open/close routines recognize and preserve this supported egress rule. On other
hardware, the administrator permits outbound TCP 22 in that host's network policy.
This is a server prerequisite; local `make bot-cloud` uses the operator computer's
SSH egress instead. The web wizard's DigitalOcean token is encrypted with the installation
key and bound to the human owner, organization and setup identity. Bot bearer
credentials cannot access this controller. The token and SSH operator key remain
on the controller, not in chat, bot workspaces or VM user data. The VM's original
SSH host key is initialized through DigitalOcean and pinned before connections.
Only SSH from the controller's public IPv4 `/32` is admitted; the bot uses outbound
HTTPS and has no public chat/DNS ports. This restricted SSH rule stays installed
for sign-in verification and resumable operator actions.

After installation, open the linked owned Droplet, then **Settings → Recovery
console → Launch Console**. The SSH-based Droplet Console is blocked by the
restricted firewall; the [Recovery Console](https://docs.digitalocean.com/products/droplets/how-to/recovery/recovery-console/)
is out-of-band. **Show my private setup-console access** reveals an owner-only,
temporary password for the unprivileged `reasoning-bot` user and its login command.
Log in as that user, not root. Password-based SSH remains disabled; the setup
password is locked and its retained value discarded after successful runner start.
Codex uses `codex login --device-auth`; device login
must be enabled in the relevant ChatGPT account/workspace. Claude Code uses
`claude auth login`, with its browser URL and returned login code. Credentials
stay in that dedicated VM user's home. **Verify sign-in and start** checks provider
authentication without invoking inference, then enables the reconnecting systemd
runner. Online bot presence is reported by the runner, not inferred from VM state.

The checkout-based equivalent uses the same provisioner and installer:

```bash
make bot-cloud
# Select a different private state root for another cloud bot:
make bot-cloud BOT_CLOUD_STATE=/private/path/another-cloud-bot
```

The CLI asks for the server, creates a source archive from the checkout, pairs in
the web interface, quotes hardware, provisions, then opens an interactive pinned
SSH provider login and starts the service. It does not require Codex, Claude Code,
Docker or Node locally. Its DigitalOcean token stays in mode-0600 local state.
`BOT_SERVER=https://chat.example.org` supplies the server. Repeat the same command
with the same state directory after interruption; never discard ownership or
invocation journals. `reasoning-bot-cloud --action status|login|start --state-dir PATH`
inspects retained setup, repeats login, or checks authentication and starts the runner.

Tokens need `account:read`, `sizes:read`, `ssh_key:read`, `ssh_key:create`,
`tag:read`, `tag:create`, `firewall:read`, `firewall:create`, `droplet:read`, and
`droplet:create`. Select the Team that will own the bot. Follow-my-access bot
permissions include conversation and shared-knowledge reads/writes; owners can
edit them in My bots. Other users cannot request this bot unless explicitly
enabled, and local execution always requires the bot owner's exact-command
approval, including requests originating from another person.

Local CAS installs SymPy, GAP, PARI/GP and Singular (minimum 4 GiB RAM).
Lean installs Lean 4.24.0 and a pinned, offline-probed mathlib tree matching the
compute-image inputs (minimum 8 GiB). Toolchains live under system paths visible
inside the no-network bubblewrap sandbox, outside provider credentials. The
installer checks selected tools before offering sign-in. Inference-only bots
need at least 2 GiB; provider models run at the provider, not on this CPU VM.

Quotes exclude model/provider charges, tax, transfer overages, storage, backups
and other services. Powered-off and offline Droplets still incur VM charges.
Manage or destroy the dedicated Droplet in DigitalOcean to stop VM billing;
deleting/revoking the bot identity in Reasoning does not delete cloud hardware.
The installer does not start inference during provisioning or authentication
checks. Web-owned cloud setup and ownership journals are included in encrypted
Reasoning backups, including the exact installed bot source needed for retries;
CLI-owned state must be backed up privately by its operator.

## Codex and Claude Code: connect from your computer

Install and sign in to [Codex CLI](https://learn.chatgpt.com/docs/non-interactive-mode)
or [Claude Code](https://code.claude.com/docs/en/headless) locally. Claude Code
must support `--restricted` (version 2.1.248 or later); `make bot-doctor` checks
the required flags without invoking inference. Their local
account authentication is reused; no OpenRouter key is required for this bridge.
From a Reasoning checkout:

```bash
make bot-connect
```

The wizard asks for the server hostname or HTTPS URL, then selects the CLI,
advertised CLI models, a bot name, and whether to allow local tools/CAS.
It opens a browser; compare the displayed connection
code, select the organization and conversation/knowledge access, and approve. The bot
is paired to that selected organization only, even when the server hosts several
organizations. Access does not switch with the browser's active organization;
use a separate bot pairing and `BOT_STATE` directory for another organization. The computer
generates its bot credential and saves private state outside the workspace. The
server receives only its hash. Approval expires after ten minutes. Do not approve
unsolicited connection codes. `BOT_STATE=/private/path` selects another bot.
After approval, the same command starts the reconnecting runner. Keep the terminal
open; Ctrl-C stops it without discarding its pairing. `BOT_SERVER=chat.example.org`
supplies the server without a prompt. With an existing pairing, the server prompt
offers its saved URL and the runner reuses that pairing; another server requires a
different private state directory. `BOT_PAIR_ONLY=1` saves the pairing and exits
without starting the runner.
New connections use `$XDG_DATA_HOME/reasoning/bots`, or
`~/.local/share/reasoning/bots`. An existing `~/.reasoning-bot/config.json` remains
usable. Credentials, connection state, invocation receipts and managed workspaces
live under this one private root. Workspace directories are separated by
organization/project/channel/topic, using immutable identities. Changing model
or effort does not change the topic's workspace.

Inspect the paired bot, or restart it after stopping, with the same private state:

```bash
make bot-status
make bot-doctor
make bot-run
```

`BOT_STATE=/private/path` selects another bot for all four make commands.
`BOT_CONFIG=/private/path/config.json` selects a separately saved rotated credential
for run/status/doctor without changing the retained state directory.
Only one command can hold a bot's private state lock at a time. If a command
reports that another process is already using the state directory, stop the
existing runner with Ctrl-C in its terminal before restarting. `make bot-status`
can inspect the pairing while the runner is active.
There is no frontend/Node build for a bot. Stop the old runner with Ctrl-C,
update the checkout with `git pull --ff-only`, and run `make bot-run` to use the new Python code;
`uv run --locked` synchronizes dependencies. Existing pairing files are reused.
`make upgrade` updates the organization server, not independently running bots.
Update and restart each local runner after upgrading the server. A dedicated
cloud bot runs installed source; its application and provider CLI need separate
updates before service restart. Restarting alone does not install newer code.
The direct CLI equivalents are `reasoning-bot status`, `doctor`, and `start`
(`run` is also accepted), with optional `--state-dir` and `--config` overrides.
`reasoning-bot connect` pairs and starts the runner; `connect --pair-only` only pairs.
`status --json` and `run --once` are noninteractive machine-readable checks.
To configure without terminal prompts while retaining browser approval:

```bash
uv run --locked reasoning-bot connect --server https://chat.example.org \
  --engine codex --name "My Codex" --no-local-tools --models default --pair-only
```

`--no-browser` prints the approval URL for another computer. This does not skip
human approval. Local tools remain disabled unless explicitly requested.
Keep the runner process running, then type `@` and select the bot in a granted topic. By
default only its owner's mentions invoke it; `make bot-run BOT_ALLOW_OTHER_USERS=1`
(`reasoning-bot run --allow-other-users`) explicitly trusts other humans in those
topics. Without it, another member's mention is not run: the topic shows a
cancelled bot activity under that message saying the bot accepts requests only
from its owner. No CLI is invoked, the request text is not retained in the bot
run, and the bot posts no reply. Bot/assistant messages never invoke it.
Each invocation starts with the current human request or workflow node prompt,
the latest twenty authorized messages through its captured sequence, and the
beta topic's system instructions. Forks use their own copied history, never the
origin topic's later messages. Delegated workers receive only their assigned
objective and explicit context, not the coordinator's recent conversation.
Earlier topic history, canonical mathematical records, bibliography and blog
are recovered on demand through authenticated tools; unrelated organization
conversation is not loaded into the prompt. The CLI receives prompt data on
stdin, never as shell syntax. Explicit message search returns at most 20 matches
and intersects the bot's grants with the requester's current access, so another
member cannot use your bot to read your private projects. There is no default
model output-token cap.
Long-running invocations refresh bot presence and delegated access every 15
seconds, including the active topic's current read/respond grants. A rejected
access probe stops the local CLI process and retains its
unknown invocation outcome for explicit recovery.
Mentioning an enabled topic bot suppresses the composer's automatic OpenRouter
reply in a beta-topic; it does not start both paid inference paths.
Type `@BotName-` to autocomplete that bot's advertised CLI models. The selection
applies to this invocation only; plain `@BotName` uses its first advertised model.
After a model, type `!` to autocomplete its advertised effort, for example
`@BotName-gpt-5.5!high`. Beta reply and goal selectors, gamma comparisons, and
workflow AI steps also offer effort selection. Default effort omits the override.
Selecting a model opens its advertised effort choices; the closed selector shows
one `model/effort` label. Model favorites belong to the signed-in person and are
stored on the server, so they follow that person across devices and organizations.
The new beta-topic dialog also offers bot models whose following-access policy
covers that channel. Bots restricted to explicit existing topics are not silently
granted a newly created topic. The selected model and initial effort are retained
on the new topic; inference admission checks the live grants again.
Beta preferences are remembered per user, topic, and model. Codex levels come
from the installed CLI's offline model catalog; unknown identifiers do not
invent supported levels. OpenRouter choices use its per-model reasoning metadata.
Mentions retain immutable bot/model identities rather than guessing from names.
Conflicting models for the same bot in one message are refused. Editing a source
message does not change an already admitted bot invocation or replay it.
Chat and People show compact allowlisted activity from CLI/tool events, such as
reading topic history, writing ledger entries, and waiting for approval.
Heartbeats preserve tool activity; the run retains its last twelve activity labels.
Private reasoning, tool arguments, and CLI stderr are not included in activity.

Each bot run exposes `timing` in the bot-run API and topic JSON; **Wait stages**
shows it on activity cards of runs that have not succeeded and in workflow node
details. The server stamps `enqueued_at_ms`, `claimed_at_ms` and, for a
succeeded run, `committed_at_ms` when the reply message is written, in epoch
milliseconds on its own clock; `queue_wait_ms` and `claim_to_commit_ms` are
their differences. `timing.runner` holds stages reported
by the bot's runner in whole milliseconds after it received the claim, on its own
monotonic clock: `cli_started_ms` (CLI process spawned), `cli_ready_ms` (Codex
`thread.started` or Claude Code `system/init`), `first_text_ms` and
`final_text_ms` (first and last assistant-text event) and `cli_exited_ms`. The
runner observes CLI output at its 0.2-second poll, and CLI events are whole
messages rather than token deltas, so text stages bound when text became visible
to the runner. `resolved_model` is the model named in Claude Code's session-start event
and is null for Codex, whose JSON events the runner reads no model from. No
effective reasoning effort is recorded; the requested level is the run's `effort`. Runner stages carry no text, only
bounded integers and a model identifier. A runner sends them only to a server
whose `/api/bot/runtime` response includes `run_timing: true`, so older servers
never receive the field; completions from older runners, failures before a stage,
and runs created before these fields leave stages `null`. Stages are never
derived from the second-rounded `created_at`/`updated_at` fields.

Codex uses a read-only sandbox with native shell execution disabled; Claude Code
has no built-in tools. The only configured MCP server is Reasoning's scoped
gateway. Before invoking either paid CLI, the runner initializes that gateway
over real MCP transport and checks its tool list. Codex additionally treats the
gateway as a required MCP server and waits up to 30 seconds for it to initialize
before starting the model turn. A successful bot reply also
requires a gateway connection from the CLI invocation; missing tools are a
specific failure, not a successful tool-less response. Read-only preflight is
not charged as inference. Workspace read/write tools reject traversal, symlinks, hardlinks and
nonregular files. Local execution is optional and needs **Linux bubblewrap**
(`bwrap`). On Debian/Ubuntu install it with `sudo apt-get install bubblewrap`.
When it is unavailable, execution is refused; it is never replaced by an
unrestricted shell. Inference and shared-knowledge tools do not require it.

Each exact command requires **the bot owner's** approval in **People**, including
when another member requested the computation. A pending-request symbol replaces
the bot's connection dot; click its name or symbol to review the exact action.
Local execution, container computation and GitHub issue requests share this dialog.
The one exception to owner approval is a GitHub issue requested by a member the
owner lets approve their own issues (see [Developer bots](#developer-bots)); that
member, not the owner, sees it in this dialog, and the topic's bot activity says
which of them is awaited. Closing it leaves requests pending. Requests have no decision-age timeout:
they remain until decided, stopped, or invalidated by access/action changes.
The runner's active runtime allowance excludes server-confirmed consent waits;
provider CLIs and MCP transports can still impose their own connection limits.
If transport ends before any execution, issue publication, or submitted compute
effect, the exact request remains visible with an interrupted-delivery warning.
Allow/Publish is disabled: reject it and stop the retained invocation in its
topic, restart `make bot-run`, and submit a new request. The runner clears only
this known-unexecuted journal after reading its terminal lifecycle receipt;
it does not replay the old action or start a replacement CLI automatically.
After a consumed execution/publication, interrupted outcomes remain unknown and
require reconciliation; a pending later request does not erase that uncertainty.
Disconnected running execution retains heartbeat fencing and is never replayed.
Approval and execution recheck live owner/requester access and exact request hashes.
Consent is consumed durably once
before process creation. The execution sandbox exposes system toolchains
read-only and the topic workspace read/write, with a private temporary directory,
no host home directory, no inherited environment secrets, and no network access.
Each local command has a five-minute runtime and 128 KiB output safety allowance;
timeouts and truncation are reported explicitly. Output is drained through a
bounded pipe, not an unbounded host temporary file.
User-home installations of toolchains are not mounted. macOS local execution is
not enabled by this Linux sandbox; use inference/knowledge tools or remote compute.
An untrusted bot program still needs its own OS/container isolation: a server
cannot constrain a malicious program its owner independently chooses to run.
The runner never uses bypass-permissions/YOLO flags. Codex user configuration,
exec rules and apps are disabled; Claude user customizations are disabled. `doctor`
checks required CLI flags and reports installed CAS without invoking a model:

```bash
uv run --locked reasoning-bot doctor --config ~/.local/share/reasoning/bots/config.json \
  --state-dir ~/.local/share/reasoning/bots
```

Replies are checkpointed before posting and use stable message identities.
Idle polling reconnects automatically after network errors or temporary HTTP
408/425/500/502/503/504 failures, with 1–60-second capped backoff. Rate limits wait
60 seconds. Authentication/permission failures stop with instructions for
checking My bots and renewing access. A request refused with HTTP 4xx other
than 401 reports its status and the server's short explanation, reduced to one
printable line of at most 1000 characters; other response content, redirect
and server-failure bodies, and secrets are not logged. Tools receive the same
explanation, for example which argument failed validation.
Tool argument refusals name the failing field and the violated schema rule,
never the submitted values.
Active invocations tolerate brief heartbeat outages with at most three attempts;
extended outages or revoked access terminate the local process and retain its
unknown outcome. Reconnection never replays an unknown CLI invocation.

The continuous runner keeps connection status and brief activity in one live
terminal panel. Thinking and tool phases update in place rather than printing
heartbeat lines. During an outage the panel has a yellow outline and shows
`Bot offline`, the first offline time, and the retry countdown. `doctor`,
`status`, and single-step runs retain their machine-readable output.

The shared beta/gamma stop (■) control also covers bot-provided inference,
whether selected as the reply model or invoked by mention. Only the requester,
bot owner, or an organization administrator with current topic write access may
stop it. A queued invocation becomes cancelled before it can be claimed. A
claimed invocation becomes outcome unknown: its lease is fenced and unconsumed
execution approvals are denied, but this does not prove that an external CLI,
provider call, or approved local process physically stopped. Consumed approval
receipts and retained output remain intact. Unknown bot invocations are not
automatically replayed by play (▶); reconcile the owner's local receipt first.
Running activity pulses the phase actually reported by the bot, respecting the
system's reduced-motion preference.

An interrupted CLI invocation is not automatically retried: paid calls and local
side effects may already have happened. Inspect the CLI's local session, then run
`reasoning-bot recover --state-dir … --action skip` after reconciling it, then
submit a new request in Reasoning if another attempt is wanted. Recovery requires
typed confirmation. A ready reply retries only its idempotent completion receipt.
Runtime/output safety bounds are 30 minutes, 8 MiB CLI output, and 128 KiB answer
text. Failures do not expose raw CLI stderr or authentication material. CLI
usage is charged to the local provider account, not the organization's OpenRouter
ledger; no invoice or carbon figures are fabricated for these replies.

Connected bot models appear in the beta reply-model selector and gamma workflow
model selectors. They use `bot/<identity>/<CLI-model>` routing identifiers but
display readable bot names. Choose `default` to use the CLI's default model, or
advertise explicit model IDs supported by that CLI account during connection.
The server does not infer account access from a model name. Selected routes are
retained per user/topic; no failure falls back to OpenRouter. Beta `/goal` uses
the selected bot without requiring an OpenRouter key or reservation. Time,
turn, and tool allowances still apply; provider-account spending is managed
outside Reasoning, and no OpenRouter charge is invented. `goal_checkpoint`
records progress or completion. Failed or unknown turns require inspection.
Bot workers can delegate narrow tasks using `agent_delegate`, `agent_tasks`,
and `agent_read`; the server integrates completed results exactly once.
Independent organization-model review remains OpenRouter-backed.
Bot-backed gamma nodes use the same durable workflow dependency graph and
input receipts as provider-backed nodes. Chat and People show activity phases,
not private reasoning text. Unknown outcomes are held, never automatically rerun.

## Personal access and custom bots

### Developer bots

In **My bots → Developer tools**, enable a bot and specify exact GitHub
`owner/repository` allowlists and other permitted organization requesters.
The owner is always permitted; other requesters also need the runner's
other-user policy (`BOT_ALLOW_OTHER_USERS=1`) and current topic write access.
For any other requester the developer tools are absent, and `topic_context`
returns a notice telling the bot to explain that the owner must add that person
as a permitted requester; it does not disclose the repositories. Disabling or changing
developer access invalidates pending approvals. This grant is separate from local
CAS execution and does not expose SSH, shell, deployment credentials or raw logs.
The `server_diagnostics` tool returns sanitized counts, status buckets and
allowlisted error codes for the current topic only, plus the deployment release
identity and source commit when its immutable manifest can be verified. It never
returns server logs, deployment credentials or another topic's contents.
Managed release directories retain the exact public manifest beside `web/`;
diagnostics return its source commit only when the manifest hash matches the
running release identity. Missing or unverified manifests leave the commit null.

My bots shows compact access and local-execution summaries, not conversation
history. Following your access includes new topics; use restricted access only
when exact topic grants are needed. Developer-enabled bots have a hexagonal
marker in My bots and People; hover their name to see the runner engine, models
and connection status. Enabling developer access does not start a runner or
enable local execution on its computer.

The local gateway's `topic_context`, `tool_search`, and `tool_call` include both
server tools and locally configured workspace/developer tools. Discovery checks
the live lease and current permissions. Cached CLI tool lists cannot authorize a
revoked grant; developer endpoints recheck it before reads or approved effects.

#### Development-box tests

A developer bot can inspect the source checkout and author regression tests on
the computer running it, without installing a bot on the application server.
From the trusted checkout, configure the paired bot's private policy:

```sh
make dev-bot-config DEV_REPOSITORY=owner/repository
```

Use the same `BOT_STATE` as `make bot-run`. This creates `developer.json` there
without replacing an existing policy. Its exact repository must also be allowed
in **My bots → Developer tools**; enable local execution when pairing. Restart
`make bot-run` after configuring. Linux bubblewrap, the checkout's `.venv` with
test dependencies, and installed frontend dependencies are required for their
respective profiles. Run `make dev-setup` first to prepare them. Configuration
remembers the selected Node executable, so the bot's shell does not need to
reactivate nvm for frontend requests. This setup does not create a daemon or change other CLI
sessions. It does not require a DigitalOcean account or deployment access.

The `development_source_read` tool reads bounded, exact tracked text files and
returns the checkout commit and file content hash.
Ignored files, private bot state, `.env` files and symlinks are excluded. The
owner must trust the configured checkout and avoid committing credentials.
`development_test` runs one of `smoke`, `bots`, `knowledge` or `frontend` against
a frozen snapshot of tracked source, including tracked local edits. It can also
include a Python regression saved with `workspace_write`, using `test_path` with
a Python profile. The source checkout is not edited. Untracked source files are
not part of the snapshot; add intended source files to Git before testing them.

An app request requires exact owner approval in **People → the bot's pending requests**.
The preview identifies repository, profile, source commit and snapshot hash.
Approvals are consumed once. Tests have no network, host home, Git metadata,
inherited secrets or deployment state, and cannot change the source or toolchain.
Only temporary sandbox storage is writable. A profile is an offline local check,
not a CI release gate, live-provider acceptance test or full resource-isolation
guarantee for hostile source. Wall time and captured output are bounded.

For example, mention your developer bot with: “Inspect the deployed release and
run the smoke tests on your development checkout. If useful, write an isolated
regression test and request approval to run it. Do not publish an issue yet.”
The bot receives status, exit code, failing pytest test identifiers, commit and
snapshot hash, not raw logs. Output and interrupted-operation receipts remain
private on the development box. Inspect them locally before reporting sensitive
failure details or opening a public issue.

The same runner works without a chat invocation or model call:

```sh
make dev-bot-test DEV_PROFILE=smoke
make dev-bot-test DEV_PROFILE=knowledge
```

The CLI previews the snapshot and asks for consent. For explicit unattended local
testing, run `uv run --locked python -m reasoning_ledger.server.bot_testing
--state-dir /absolute/private/bot-state test --profile smoke --yes` on one line.
Each result prints its private receipt location. No tests are run on the deployed
server, and no paid inference is performed by these CLI commands.

Install and authenticate GitHub CLI (`gh auth login`) on the bot computer.
The `github_issue_create` tool proposes an exact repository, title and body in
**People → the bot's pending requests**. The bot owner approves publication,
except as described below. When another member requested the issue, the server
appends a final line, `Requested in Reasoning by <member> through the bot <bot>.`,
with both names as inert code spans; it is part of the previewed and approved text.
GitHub shows the owner's `gh` account as the author.
Review the preview carefully: an issue may be public and its body can contain
private context. Approval is consumed durably once before the local gateway's
fixed authenticated GitHub API request. GitHub credentials stay on that computer;
they are not available to the model or the no-network computation sandbox.
Successful writes retain the issue link on the server and in a protected local
receipt outside the topic workspace. A timeout or interrupted write remains
unconfirmed/unknown and is never republished automatically; inspect GitHub and the
receipt before proposing a new request. No inferred model text grants developer
privileges or bypasses this approval.

Self-approved issues are off by default. Under **Other permitted requesters**,
the owner can mark a permitted requester as **Approves their own GitHub issues**
and set a limit of 1–20 self-approved issues per member in any 24 hours
(default 3). Such a member's issue requests go to that member instead of the
owner: the bot's **!** marker appears for them in **People**, and they review
and publish the exact preview, including the attribution line. They cannot
decide anyone else's request, and the owner cannot decide theirs; either can
stop the invocation. Such a member can publish, without the owner's review,
anything the bot can read during their invocation: the topic and its workspace,
sanitized diagnostics, and tracked development source and test results on the
bot computer. Enable it only for members trusted with that. The allowlisted repositories, exact request hash,
consume-once publication, unknown-outcome handling and the owner's local `gh`
credentials are unchanged. Approval and consumption recheck that the member is
still a permitted, self-approving requester with current topic access. Requests
beyond the limit, counting approved and pending ones, go to the owner instead,
and approval past the limit is refused. Any developer-access change denies
pending and approved, unconsumed issue requests. Each decision records who made
it: the audit log has a `bot.issue.approve` or `bot.issue.deny` event with that
person as actor, and the owner's issue approval list
(`GET /api/organizations/{organization_id}/my-bots/issue-approvals`) shows
consumed, published and unknown-outcome self-approved requests with the deciding
member. The configuration is the
`issue_self_approvers` (a subset of `requesters`, excluding the owner) and
`issue_daily_limit` fields of
`PUT /api/organizations/{organization_id}/my-bots/{bot_id}/developer`; omitted
fields reset to off and 3.

Every organization member can open **Account menu → My bots** to create and
manage their own personal bot. Use **Follow my access**, or select exact topics, and message/knowledge
permissions. Viewers can delegate read access only. Personal bots recheck their
owner's current membership and topic access on every request. Following access
immediately drops inaccessible topics but keeps the rest usable; explicit-only
grants with lost access disable the bot until repaired. No token grants organization administration
or permission to execute without owner consent. People groups each bot underneath its attached person, including
offline bots. Online state is advisory recent API activity.

Personal management uses `/api/organizations/{organization_id}/my-bots` with the
same methods and bodies as the administrator routes below. Lists and mutations
are restricted to bots created by the signed-in user; administrators can inspect
and revoke all bots through organization administration. A user can have at most
10 unrevoked personal identities within the organization-wide admission ceiling.
The `delegation_kind` metadata distinguishes personal access from administrator
sponsorship. Administration cannot expand a personal bot beyond its owner's access.

1. Open **My bots → Custom bot / manual credential** for personal access, or
   **Administration → Bots** for administrator-sponsored integrations.
2. Choose **Send updates**, **Watch and respond**, or **Read events**. These map
   to write-only, read/write, and read-only access; no other organization powers
   are implied. Separately choose no knowledge access, read access, or read and
   publish access for the shared ledger, bibliography, and research journal.
3. Choose **Follow my access** for a personal bot (or exact topics), name the bot, choose a credential lifetime,
   and select **Create bot and show credential**.
4. Copy the one-time credential. On the machine that will run the integration,
   install [uv](https://docs.astral.sh/uv/getting-started/installation/) and clone
   this repository if necessary. The bot client needs Python dependencies; it
   does not need Node.js, a web build, deployment state, or a DigitalOcean token:

   ```bash
   git clone https://github.com/tertius-ai/reasoning.git
   cd reasoning
   mkdir -m 700 -p ~/.config/reasoning/bots
   uv run --locked reasoning-bot configure --server https://chat.example.org \
     --output ~/.config/reasoning/bots/my-bot.json
   uv run --locked reasoning-bot identity \
     --config ~/.config/reasoning/bots/my-bot.json
   uv run --locked reasoning-bot topics \
     --config ~/.config/reasoning/bots/my-bot.json
   ```

   `configure` asks for the credential at a private prompt, writes a mode-0600
   configuration, and refuses to overwrite an existing file. For unattended
   setup, `--token-file` reads the credential from an already protected file.
   Replace `https://chat.example.org` with your organization's HTTPS origin.
   `identity` confirms enabled access; `topics` prints the exact granted names,
   kinds, channel IDs, and topic IDs for subsequent commands. Neither starts a
   bot process. Run these commands from the checkout, or use an installed source release and
   invoke `reasoning-bot` directly.

5. If the bot can post, create a short text file such as
   `~/.config/reasoning/bots/hello.txt` with your text editor and set it to mode
   0600. Copy a topic ID from `topics`, then test the connection:

   ```bash
   chmod 600 ~/.config/reasoning/bots/hello.txt
   uv run --locked reasoning-bot post \
     --config ~/.config/reasoning/bots/my-bot.json \
     --topic TOPIC_ID --message-file ~/.config/reasoning/bots/hello.txt \
     --client-id first-bot-hello
   ```

   The message appears in that topic as `Bot · name`. After a lost response,
   rerun the same command with the same file content and client ID. A read-only
   bot can verify identity/topics but cannot run this posting test.

6. For a simple responding bot, choose **Watch and respond** and run the
   [arithmetic polling example](#arithmetic-research-example). Leave its process
   running and send `!gcd 12 18` in a granted topic; it replies with 6. Replace
   that example with your own integration when ready. Custom local-model or Slurm
   integrations run on that machine; the credential authorizes organization
   access only. Codex and Claude Code use the built-in CLI bridge above.

Creating the identity does not deploy or start a process. Keep its configuration
outside source control, then run your integration separately using the API or
the examples below. Rotate the credential if it was not saved safely.

## Administration and permissions

Owners and administrators use **Administration → Bots** to create a bot,
choose public conversations and/or up to 64 explicit topics, and select read-only, write-only, or read/write message
access. The API names those scopes `messages:read` and `messages:write`.
Knowledge separately uses `knowledge:read` and `knowledge:write`; write access
also includes read access in the web form. Organization-scoped entries are
shared; project-scoped entries require project visibility, and topic-scoped
entries require current topic access. Following personal bots intersect all
knowledge access with their owner's current permissions; other bots can access a
private project's knowledge only when granted a topic within that project. Deleted
scopes are unavailable.
Explicit grants apply to those topic identities, including readable history.
For administrator-sponsored bots, public conversation access includes future topics
in organization-visible projects; private topics still need explicit grants. Personal
**Follow my access** includes future private conversations visible to the owner.
The interface identifies project, #channel, and
topic, and supports renaming/grant changes, credential rotation, and permanent
revocation. Credentials are displayed once, with explicit copy/show controls,
and kept only in component memory—not browser storage. Save them into a private
bot token file and dismiss/close the dialog. Lists never expose credentials or
their hashes. If a creation/rotation response is lost, inspect metadata and
rotate again; the server cannot return the lost secret.

Credential lifetime is one hour through 365 days (default 30 days); the web
creation form uses whole days. Rotation immediately invalidates the old token,
sets a new expiry, and makes the approving administrator the sponsor. A revoked
identity cannot be reactivated; create a new bot. Its historical messages remain.
Each organization can have 100 unrevoked bots, including expired identities;
revoke unused ones to free admission capacity.

The sponsoring administrator must retain owner/admin membership in the bot's
organization. Removing or demoting that sponsor disables the bot immediately.
Another current administrator can explicitly approve changed permissions or
rotate its credential to take sponsorship. Every operation rechecks current
credentials, expiry, sponsorship, scopes, and topic grants in the data
transaction. A credential cannot become a human session, accept an invitation,
create an organization, access organization/provider administration, or invoke
OpenRouter inference directly. Local-agent invocation tools use a separately
fenced run lease. `compute_submit` proposes the exact program, immutable image
digest and resources in **People → the bot's pending requests**. Only the
bot owner can approve, even when another human requested the computation.
Approval rechecks current requester/owner access and image identity and queues
at most one job. Configured DigitalOcean, owned-hardware or Modal workers use the
same image queue, result and artifact contracts. Neither host shell nor Docker
socket is exposed to the model. Local CAS execution remains separately approved.
Pending/unknown computation prevents goal completion; completed results trigger
integration without paid busy-polling.

## API

Human administrator routes use ordinary session-cookie/CSRF or human bearer
authentication under `/api/organizations/{organization_id}/bots`:

| Suffix | Method | Body/result |
| --- | --- | --- |
| empty | GET | Bot metadata, scopes, topic IDs, expiry/revocation/sponsor state |
| empty | POST | `name`, `topic_ids`, `scopes`, optional `public_access` (default false) and `lifetime_seconds`; metadata and one-time `token` |
| `/{bot_id}` | PUT | `name`, `topic_ids`, `scopes`, optional `public_access` (default false); replace grants and approve sponsorship |
| `/{bot_id}/token` | POST | Optional `lifetime_seconds`; metadata and one-time replacement `token` |
| `/{bot_id}` | DELETE | Permanently revoke; 204 |

For `/my-bots`, `public_access=true` means follow all current owner-visible
conversations; for administrator `/bots` it means organization-public conversations.
The personal web wizard enables following access by default. `public_access=false`
retains only the explicitly selected topic identities.

Bot routes accept **only** `Authorization: Bearer <bot token>`—never cookies,
query parameters, human sessions, or worker tokens. Tokens begin with `rnb_`;
the server stores only their SHA-256 hashes. Use HTTPS (HTTP loopback is allowed
for local development). Supplied foreign browser Origins are rejected and
responses are `Cache-Control: no-store`. The bot's organization is inferred from
its credential, not a caller-selected organization ID.

| Path | Method | Result |
| --- | --- | --- |
| `/api/bot/identity` | GET | Own metadata and effective enabled state |
| `/api/bot/topics` | GET | Only granted topics' IDs, names, kinds, and channel IDs |
| `/api/bot/messages?after=0&limit=100` | GET | Granted-topic messages in increasing sequence; requires read scope |
| `/api/bot/topics/{topic_id}/messages` | POST | `content`, `client_id`; requires write scope and exact topic grant |
| `/api/bot/knowledge?kind=journal&q=query&limit=100&before=CURSOR` | GET | Visible current heads; exclusive `revised_at:id` cursor, 1–500 entries; requires `knowledge:read` |
| `/api/bot/knowledge/{entry_id}?revision=N` | GET | Visible exact entry/revision; omit `revision` for current head; requires `knowledge:read` |
| `/api/bot/knowledge` | POST | Create a scoped ledger, bibliography, or journal entry; requires `knowledge:write` |
| `/api/bot/knowledge/{entry_id}` | PUT | Append an optimistic immutable revision; requires `knowledge:write` |

Knowledge writes use organization/project/topic scopes from the human
[shared-knowledge surface](server.md#shared-knowledge-and-presence).
Bots may propose ledger notes and conjectures, but cannot establish/refute a
claim or modify/demote an established/refuted ledger entry. Those actions need
human review in the organization UI. Journal and bibliography states follow the
human surface. Reading alpha-topic messages is never implied by knowledge access.
They require a stable 16–128-character `client_id`; replaying the same exact
write returns its original revision, while changed input conflicts. Revision
writes also require the current `expected_revision`. Bot authorship remains
visible. Published journal entries can enter beta context; drafts cannot.
With the topic's blog and knowledge-write grants, invocation tools can search and
read drafts, create and revise project posts, and publish requested writing using
`blog_publish` or `state: published`. Published posts are editable with the current
head revision; saving a draft keeps the previous publication until publication.
Stale revisions conflict rather than overwrite another author's work. Blog tools
also cover shared organization posts. They do not broaden the topic's project
audience or read other topics' private drafts.

Workbench agents can use this access for the organization's
[canonical ledger](organization-ledger.md), with explicit source import and a
private link instead of an independent local ledger.

The invocation gateway's `topic_context.scope` includes the current topic title,
kind, channel name, project name, and their immutable IDs. `topics_search` discovers
authorized topics without requiring them to contain messages: `query` is a
case-insensitive substring of the topic title; `project_name` and `channel_name`
are exact case-insensitive filters (use the channel name without its displayed
`#`). Optional project, channel, and topic IDs narrow results. All filters combine;
up to 20 results are returned, with a default of 10. Duplicate titles remain
separate results with their full project/channel location and IDs. An empty list
means no authorized match, not an unavailable tool. `messages_search` includes
the same source metadata beside each message. Both searches intersect live bot,
owner, and requester permissions within the paired organization, including new
topics covered by **Follow my access**. Narrow delegated workers receive only
assigned context, not conversation discovery tools.

Polling is a subscription to all currently granted topics. Persist the exclusive
`after` cursor after processing each message. Pages contain 1–100 requested
messages (possibly zero); the next cursor is the last returned sequence. No
ungranted topic or other organization's message is returned. Independently hosted
bots can poll or receive signed outgoing webhooks as described below.

Posts contain 1–16000 characters and a stable 1–128-character `client_id`.
Reusing the same ID for exactly the same bot/topic/content returns the same
message. Different content/topic with that ID is a 409 conflict. Bots cannot
choose sender IDs/kinds or other trusted metadata. All request bodies have the
server's 64 KiB transport limit. Missing/revoked/expired credentials return 401;
missing permissions/sponsorship return 403; unavailable/ungranted topics return
404; quota exhaustion returns 429.

Per bot, a persistent rolling 60-second window allows 300 requests, including
at most 30 write attempts. Valid authenticated attempts that fail scope/topic
checks still consume quota. Requests reauthenticate after metering, so revocation
between metering and data access cannot bypass authorization. Limits survive
server restarts; retry quota failures after at least 60 seconds. These limits
are not a general unauthenticated network-abuse defense.

## Outgoing webhooks

Administration → Bots supports one active outgoing subscription per bot.
The bot must be active and have `messages:read`; both its sponsor and the
subscription's creating administrator must retain owner/admin membership.
Select the bot, a public HTTPS receiver URL, and an optional exact case-sensitive
message prefix. An empty prefix captures every new human message in granted
topics. Bot/model replies, historical messages, and ungranted topics are excluded.
Message persistence and capture share one transaction; an idempotent message
replay cannot create a second delivery. Bot replies do not create feedback loops.

Human administrator routes under
`/api/organizations/{organization_id}/bot-webhooks` use the same cookie/CSRF
or human bearer authentication as bot management. Bot tokens cannot manage them.

| Suffix | Method | Body/result |
| --- | --- | --- |
| empty | GET | Subscription metadata, effective authority, pending and dropped counts; no secrets |
| `/status` | GET | `worker_running` |
| empty | POST | `bot_id`, `url`, optional `prefix` (0–64 characters; character codes 0–31 refused), `client_id` (16–128 URL-safe characters); metadata and `signing_secret` |
| `/{webhook_id}` | DELETE | Irreversibly revoke and erase the signing secret; 204 |
| `/{webhook_id}/deliveries` | GET | Up to 100 metadata records in descending sequence; optional exclusive `before`, default `limit=20` |

Creation is an exact-input recovery operation: replay the same request ID/body as
the same current administrator to recover the original active subscription and
secret. Changing the input with that ID is a 409. Lists and delivery records
never return the secret. The web form retains a failed creation request in memory
for explicit replay while those settings stay open; new inputs are locked during
recovery. Reloading or closing settings loses that capture. Inspect metadata and
revoke a lost subscription before replacing it. Secrets are encrypted with the
installation key and organization/subscription-bound authenticated encryption;
the browser keeps them only in component memory with explicit show/dismiss controls.

Changing an endpoint or signing secret requires revoking and creating a new
subscription, with a new secret, ID, and receiver journal. There are at most
1,000 total subscription creations per organization, including revoked records.
Grant removal, sponsor demotion/removal, bot expiry/revocation, or subscription
revocation prevents further sends. Explicit grant and membership changes fence
ineligible captures in that same transaction, so restoring a grant does not
resurrect those messages. Authorization is rechecked on claim and immediately
before transmitting the HTTP request after DNS/TLS setup. Bytes already sent
cannot be recalled; prior/in-flight uncertain attempts remain unknown.

### Transport and receiver contract

Endpoints use ASCII HTTPS URLs on port 443 without credentials, query parameters,
or fragments. Literal and resolved addresses must be public unicast addresses;
loopback, private, shared-address, link-local/metadata, mapped and well-known IPv6
transition addresses are refused. Every DNS answer is checked before connecting.
The actual TLS connection uses the checked numeric address without a second DNS
resolution; SNI and certificate verification use the original hostname. A bounded,
killable resolver subprocess prevents hung lookups from accumulating threads.
There are no environment proxies, redirects, cookies, or bot/provider credentials
in outbound requests. Operators should also enforce network-level egress rules;
application checks cannot establish the routing behavior of their infrastructure.

The UTF-8 JSON envelope has `format=1`, `event="message.created"`, stable delivery
`id`, `webhook_id`, `bot_id`, `organization_id`, capture `created_at`, and
`message` containing `id`, `sequence`, `topic_id`, `author_id`, `author_kind`,
`content`, and message `created_at`. Email, mutable sender names, and message
client IDs are excluded. Captured bytes and SHA-256 identity remain unchanged
across attempts. Requests include:

- `X-Reasoning-Delivery`: the stable 32-character delivery ID.
- `X-Reasoning-Timestamp`: integer Unix seconds for this transmission.
- `X-Reasoning-Signature`: `v1=` followed by the lowercase hex HMAC-SHA256,
  keyed by the complete ASCII signing secret over
  `timestamp + "." + delivery_id + "." + raw_body_bytes`.

Use `reasoning_ledger.server.webhook_transport.verify_signature` on the raw
body before parsing, reject duplicate signature/ID/timestamp headers, and allow
at most five minutes of timestamp skew. Then verify the expected organization,
bot, and subscription bindings. Keep durable delivery-ID/body-digest deduplication
for at least the whole 24-hour capture lifetime plus clock-skew allowance.
Return 2xx only after durably accepting responsibility for the delivery; this
means receiver acceptance, not completion of its work. Lost acknowledgments can
cause retries even if the receiver acted: exactly-once external effects require
receiver-side idempotency. Protect receiver administration and private secrets
separately from its signed callback endpoint.

Each attempt has a 20-second transport deadline, five-second DNS deadline,
verified TLS, 128 KiB payload cap, and bounded response headers (16 KiB per event,
32 KiB total). Response bodies are not consumed or retained. Redirects and ordinary
4xx are terminal refusals; 408/425/429, 5xx, and transport uncertainty can retry.
There are at most five attempts, with 30/120/600/3,600-second delays and a 24-hour
capture lifetime. Restart recovery uses the same ID/bytes; it cannot prove whether
an interrupted request reached the receiver. A later refusal does not erase an
earlier uncertain outcome. One exclusively locked worker performs sends per
installation; no network I/O happens inside a database write transaction.

Queues hold at most 100 pending captures per subscription and 1,000 per
organization. Overflow increments the subscription's visible `dropped` count
without failing human chat. This is a bounded notification channel, not a lossless
replacement for polling. Persistent rolling quotas allow 10 attempts/minute and
500/day per subscription, and 30/minute and 1,000/day per organization; rejected
attempts consume quota. Quota waits do not extend capture lifetime. Each
subscription retains at most 100 terminal metadata records; terminal payloads
are erased. Administrators inspect pending/dropped counts and delivery history
in settings. Backup verification checks secret encryption and captured bytes,
digests, message/tenant bindings; restoration revokes all subscriptions, erases
their secrets/payloads, and fences outbound delivery instead of replaying a snapshot.

### Arithmetic webhook receiver

The installed `reasoning-bot-webhook` example accepts signed `/hooks` callbacks
and answers human `!gcd A B` commands through the ordinary scoped bot post API.
It uses the same bounded integer algorithm as the polling example, not shell,
evaluation, model calls, or computer-algebra containers. Create a read/write bot
and `!gcd` subscription, save its token and signing secret in separate protected
0600 files, and copy the subscription ID from settings. Prepare private parent
directories and configure the bot as in the notification example:

```bash
reasoning-bot-webhook serve --config /private/research-bot.json \
  --secret-file /private/webhook-signing-secret --webhook-id WEBHOOK_ID \
  --state-dir /private/webhook-receiver --port 8081
```

The receiver binds only to `127.0.0.1`. Put a certificate-verified public HTTPS
reverse proxy on port 443 in front of `/hooks`, with body/time/concurrency limits;
for example, a [Caddy site](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) for `bot.example.org` can use
`reverse_proxy 127.0.0.1:8081`. Point DNS at that receiver host and authorize its
inbound HTTPS firewall rule. The Reasoning server itself does not provision this
external bot host. Configure the receiver before sending test commands.

The receiver checkpoint binds the server, bot, organization, subscription, and secret
digest, and has an exclusive process lock. It authenticates raw requests and
stores the exact reply/topic/stable post ID before network I/O. After a crash or
lost reply, it resubmits that saved reply rather than generating a new one. A
completed delivery is acknowledged without posting again. Journal bounds are
5,000 records/100 pending replies; completed deduplication records last 25 hours.
HTTP serving allows 16 concurrent requests and serializes processing, caps bodies
at 128 KiB, and disables access logging. Provider keys are never used.

To recover pending replies after server retries stop, first stop the receiver,
then run a bounded recovery batch with the same configuration and state:

```bash
reasoning-bot-webhook recover --config /private/research-bot.json \
  --secret-file /private/webhook-signing-secret --webhook-id WEBHOOK_ID \
  --state-dir /private/webhook-receiver
```

Each batch attempts at most ten replies and stops on an API error with pending
state retained. Review current grants and quota before retrying. Restore or a
replacement identity/subscription requires a new journal; do not edit the
existing binding to bypass that separation.

## Notification example

The installed `reasoning-bot` CLI reads bounded private configuration and optional
token files with protected parents, owner-only permissions, no symlinks/hardlinks,
and no overwrite on configuration creation. It verifies HTTPS certificates, ignores
environment proxy settings, refuses redirects/compressed responses, and bounds
response bytes. Tokens are not placed in command arguments or URLs and error
bodies are not logged. Prepare an owner-only configuration directory and a 0600
message file before running:

```bash
mkdir -m 700 -p ~/.config/reasoning/bots
reasoning-bot configure --server https://chat.example.org \
  --output ~/.config/reasoning/bots/notification.json
reasoning-bot identity --config ~/.config/reasoning/bots/notification.json
reasoning-bot post --config ~/.config/reasoning/bots/notification.json \
  --topic TOPIC_ID --message-file /private/notification.txt \
  --client-id notification-2026-09-30-example
```

A notification bot only needs `messages:write`. A successful post prints its
message ID and sequence, not credentials or message content. After a lost
response, rerun with **the same** topic, file content, and client ID. The CLI
does not blindly regenerate request IDs or automatically retry arbitrary errors.
The Python `BotClient` exposes `identity()`, `topics()`, `messages(after, limit)`, and
`post(topic, content, client_id)` for other public integrations.

## Arithmetic research example

Create a separate bot with read/write access to the intended research topic(s),
then run the same CLI using its own private configuration and a dedicated
owner-owned 0700 state directory (or a new directory under a protected parent):

```bash
reasoning-bot research --config /private/research-bot.json \
  --state-dir /private/research-state --once
reasoning-bot research --config /private/research-bot.json \
  --state-dir /private/research-state
```

A human message `!gcd 12 18` receives the integer result 6. This is a bounded
arithmetic integration example, not an autonomous LLM researcher or a CAS-worker
substitute. It uses `math.gcd`, accepts signed integers of at most 100 digits,
does not use `eval`/shell execution, and ignores assistant/bot messages to avoid
feedback loops. Its first run begins at sequence zero, including granted history.

The polling checkpoint is bound to the configured server and bot ID and has an
exclusive process lock. Pending reply content/topic/request ID are fsynced
before sending; acknowledgment advances the checkpoint. Lost receipts/restarts
reuse the saved reply, not a newly generated request. `--once` processes one
20-message page plus any pending reply; normal mode polls every five seconds
and waits 60 seconds on rate limits. Other errors stop the process with the
checkpoint/pending reply retained. Grant removal or revocation can deliberately
prevent pending delivery; an operator must review such changes, not bypass them.

[Backup restoration](backup.md) preserves labeled bot history but permanently
revokes bot identities and clears request windows. Create and review replacement
bots explicitly. A new identity needs a new journal; its zero cursor can revisit
historical commands. Neither restore nor deployment starts an external bot.

## Verification

```bash
uv run --extra test pytest -q tests/test_public_bots.py tests/test_public_webhooks.py
npm --prefix web test
npm --prefix web run build
npm --prefix web run test:public-webhooks
```

Tests exercise actual SQLite persistence/concurrency, scopes/tenant isolation,
revocation/rotation/expiry/sponsorship, limits, provenance and beta context,
backup recovery, bounded HTTP-client fixtures, and lost-receipt journaling.
Webhook fixtures exercise real TLS sockets with a test trust root and a controlled
mapping from the checked public address to a local receiver; they check hostname
verification, raw signed bytes, redirects, and oversized headers, not public DNS
or cloud routing. Receiver fixtures verify durable lost-acknowledgment/restart
replay through the actual bot store. The rendered HTTP/browser fixture covers bot
creation, retained webhook creation, secret storage boundaries, delivery records,
and revocation using an intentionally unreachable `.invalid` receiver. Production
deployment and a public HTTPS bot journey remain release verification requirements.
