# Public organization server

`reasoning-server` provides an installation-owned, authenticated API for
organizations, projects, channels, alpha/beta/gamma topics, messages, and
one-to-one [direct messages](#direct-messages) between members. Its
state uses SQLite with WAL transactions by default, or operator-configured
[PostgreSQL](postgresql.md). Both backends have atomic versioned migrations and
foreign-key tenant constraints. It has no dependency on a hosted identity
service or on desktop-supplied provider credentials.
Organization [bots](bots.md) use separate identities, expiring/revocable tokens,
named-topic read/write grants, a polling/posting API with persistent limits, and
signed outgoing human-message subscriptions with bounded delivery recovery.

[Beta-topic agents](topic-agents.md) are the default conversation interface for
tool use, narrowly scoped delegation, and automatic integration. The
server-managed executor requires no separate enrollment. The shared knowledge
spaces are the ledger, research blog, and bibliography.

Internal [job contracts](work-contracts.md) have immutable specifications and exact
source/result references. The [worker protocol](work-execution.md) enrolls
project/role-scoped workers, leases durable jobs, retains fenced results and
uncertain resource holds, and maintains expired leases independently of claims.
[Scoped work tools](work-tools.md) support transactional hierarchical delegation,
source discovery and versioned approach memory. Server-brokered inference uses
organization keys and shared credit. The executable project worker runs pinned
prompts and scoped tools. [Controllers](work-execution.md#controllers) consume
durable results through separate review, revision, integration and acceptance
steps; model approval does not replace source/test/proof evidence.

With `--frontend-dir`, the server serves the shared React organization chat at
its root URL. In beta topics, the web interface follows each human-message write
with a request to the OpenRouter model selected beside Send, with durable
progress, cancellation, and spend accounting. The message and inference request
remain separate API operations. Beta topics can include explicit scoped
snapshots from the shared ledger, bibliography, published research journal, and
project gamma-workflow catalogue.
Gamma topics expose a durable
[compute-job protocol and separate Docker worker](compute.md), with shared browser
submission, state/log/artifact inspection, cancellation, and administrator
worker/policy controls. Gamma topics also support [immutable workflow definitions](#versioned-workflow-definitions)
through guided model comparison, an ordered AI/approval/compute builder, and an
advanced JSON editor; saving a definition does not execute it.
The [deployment CLI](deployment.md) installs verified public
server releases and separate enrolled Docker workers on dedicated Ubuntu hosts,
and provisions DigitalOcean server or separately enrolled worker resources from
explicitly confirmed cost/resource plans. It also drains/stops/retires managed
workers over verified SSH. `do-launch` binds a confirmed server release before
paid provisioning, checks the original cloud VM on both provider and host,
and verifies active services and HTTPS from host and operator. It requires
explicit provider-initialized host-key pinning in the original plan; independently
verified SSH keys remain supported by separate provision/install commands.
Initial organization/owner setup supports private interactive prompts or the
deployment CLI's immutable owner file and host-only recoverable transaction. Owned
worker installation binds the original cloud
resources and current host VM identity to a fresh release/enrollment receipt;
it does not claim container execution success. A separate explicitly confirmed cloud retirement
command verifies the owned DigitalOcean VM and current host/runtime before
destroying its local data and cleaning up its firewall/unused tag.
An administrator or scoped-scaler read-only queue/capacity snapshot supports the deployment CLI's
bounded advisory fleet planner. A separately confirmed deployment action reserves
a durable owned-fleet VM budget before creating a worker; the server does not
automatically provision or delete workers.
Scoped compute scaler grants authorize bounded recoverable worker enrollment
without a human login session or general chat/key access. Enrollment delegates
trusted compute execution; issued workers can read assigned programs. The shared
Compute settings manages grants and one-time tokens, and the deployment CLI
persists original enrollment intent before sending a request.
Confirmed unpaid setup cancellation deletes only owned free network resources;
it cannot erase pending paid VM creation or revoke worker enrollment.
Confirmed fleet amendment preserves original reservations/ownership and archives
only terminal, freshly absent slots, without making cloud writes or recycling
old identities. A separately confirmed CLI controller coordinates queue-driven
provision/install/drain cycles within that prepared bounded fleet; cloud actions
do not run in the server. Worker plans can opt into provider-initialized SSH
host-key pinning. A separately confirmed worker pool enrolls fresh template-bound
workers and activates generations without replacing the original budget or release.
Host-local [encrypted backup and recovery](backup.md)
commands preserve consistent organization state and referenced artifacts.
The [replacement-host recovery backend](operations.md#replacement-host-recovery-backend)
stages authenticated restored state without an empty initialization and separately
publishes/activates it. Recovery revokes sessions and execution credentials while
holding uncertain jobs. Verified-SSH planning/preparation/activation commands retain
original inputs and host trust; real-host acceptance remains unverified.
[Offline maintenance](operations.md#prepare-an-offline-migration) prepares
journaled migrations against matching stopped-state snapshots and binds
foreground activation to the selected target release. It does not automate
managed host upgrades or rollback. Optional
operator-owned HTTPS invitation mail uses an encrypted durable outbox.
Gamma workflow execution is available through the run API and shared run controls.
A full real-host production
journey remains unverified. The local `reasoning-ledger-web` workbench is a separate application
with local filesystem/execution capabilities; do not expose it as the public
organization server.

## Install and start

Requires Python 3.11 or newer on Linux or macOS. Building the frontend also
requires Node.js satisfying `web/package.json` (verified with Node.js 24).
From the source checkout:

```bash
uv sync --locked
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build
uv run reasoning-server --help
uv run reasoning-server --data-dir "$HOME/.local/share/reasoning-server" init
uv run reasoning-server --data-dir "$HOME/.local/share/reasoning-server" setup \
  --email owner@example.org --name Owner --organization Mathematics
uv run reasoning-server --data-dir "$HOME/.local/share/reasoning-server" serve \
  --public-url http://127.0.0.1:8766 --frontend-dir web/dist
```

`init` creates private installation state and prints the setup credential's file
path, never its value. `setup` prompts twice for a password, creates one owner and
one organization, and prints their IDs. It cannot be repeated after installation
initialization. Sign in through the API to obtain a session. Alternatively,
`POST /api/setup` accepts the one-time credential from `setup-token`, the owner's
`email`, `name`, `password`, and `organization_name`. The browser setup form uses
the same endpoint. Open the configured URL to sign in and use the organization
interface. Without `--frontend-dir`, the server provides only its API.

The source-built [desktop client](../desktop/README.md) connects to the same URL
and uses this interface without a local Python backend. Its cookie session is
separate from the system browser. Accept invitation links in the browser before
signing in from desktop.

The frontend build has separate organization and local-workbench entry points.
The public server serves `organization.html`, not the local-workbench entry
point, and mounts only the build's assets, license material, and icon. It never
exposes the installation state directory or local-workbench API. The frontend
bundle includes Apache-2.0/NOTICE and the bundled font/KaTeX notices.
The organization entry point is a small shell that checks the session and
renders sign-in, setup and invitation forms. The signed-in application, with its
Markdown/KaTeX renderer, loads after the session check succeeds (and starts
loading when a sign-in form is submitted). The research workspaces, shared
knowledge, help, model capabilities, usage, and bot and compute administration
load as separate chunks when first opened, with a loading state and a reload
action if a chunk cannot be loaded; pointing at the research workspace links
starts loading that view.

Every build file under `/assets/` is named `<name>-<content hash>.<extension>`.
Successful responses for such names (200, or 304 on revalidation) carry
`Cache-Control: public, max-age=31536000, immutable`; every other response,
including the HTML entry point, API responses, unhashed names and missing
files, carries `Cache-Control: no-store`. All responses keep `nosniff`, the
referrer policy and HSTS; a route's own `Content-Security-Policy` (such as the
sandboxed media content route) is kept, and other responses receive the
application policy. When `--frontend-dir` is a managed release directory
(`<releases>/<release-id>/web`), a hashed name missing from the current release
is served from the `web/assets` directories of the other installed releases, so
a page loaded before an upgrade can still import its chunks.

With `--deployment-release`, every response carries the release digest as
`X-Reasoning-Release`, and the HTML entry point embeds it as
`<meta name="reasoning-release" content="<digest>">`. An open page compares the
header of the API responses and event streams it already requests with the
release that served it; when they differ, the top bar's **What's new** button
offers a reload and never reloads by itself. It does no extra polling for this.
Without `--deployment-release`, neither the header nor the meta element is
sent, and pages never offer a new version.

The frontend build embeds release notes in `organization.html` as an inert
`<script type="application/json" id="reasoning-release-notes">` block: the
build's Git revision and commit time, and up to 40 merged pull request titles
from first-parent history (commit subjects ending in ` (#number)`), newest
first. A build without Git history embeds an empty list. When a newer release is
reported, the page reads the notes from the current `organization.html` and
lists the entries newer than its own revision. The release bundle format is
unchanged.

`serve` binds to `127.0.0.1:8766` by default. For a public installation, set
`--public-url https://chat.example.org` and place a same-origin HTTPS reverse
proxy in front of this loopback listener. Public URLs must be origins without
credentials, a path, query, or fragment. HTTP is accepted only for `localhost`,
`127.0.0.1`, and `::1` development origins. TLS and DNS are the operator's
responsibility. Only loopback proxy connections can supply forwarded client IPs;
the proxy must replace incoming forwarded headers with the actual client address.
The server canonicalizes the configured origin to browser form: lowercase
scheme/host, ASCII IDNA hostnames, normalized IPv6, and no default `:443`/`:80`
port. Nondefault ports are retained. Empty credentials, query/fragment markers,
and port zero are rejected before normalization. Application origin checks and
invitation links use this canonical URL; HTTPS always selects secure cookies.

## Accounts and sessions

The deployment CLI can create an initial organization/owner through
`owner-input` followed by `install`, `do-install`, or `do-launch --owner-file`.
The private file contains a password and random replay credential; neither is
an argument, response field, or provider user-data value. The host invokes
`reasoning-server --data-dir /var/lib/reasoning setup-file --input PRIVATE_FILE
--expected-sha256 INPUT_DIGEST` as the state owner. This host-only path uses the
installation's private setup authority and atomically stores owner, organization,
and one keyed replay receipt. It never creates a session or resets existing
accounts; exact retries return the same nonsecret IDs. Backup/restore preserves
this receipt while rotating host-local setup authority. It cannot adopt an
installation initialized by a different setup method. The public `POST
/api/setup` endpoint remains one-time only and has no replay-credential input.

Interactive `reasoning-server setup --email EMAIL --name NAME --organization
NAME` remains available; it prompts privately and revokes its temporary bootstrap
session before returning IDs. Operators can configure invitation email as described below.

Passwords contain 12–1024 characters and use salted scrypt with fixed
`N=32768`, `r=8`, `p=3` parameters and a 32-byte result. Derivation concurrency is
bounded to two operations per process. This uses the 32 MiB configuration in the
[OWASP password-storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Login attempts have persistent 15-minute limits of ten per normalized email and
60 per client address. A missing account receives the same invalid-credentials
response and password derivation as a wrong password.

Public JSON request models require valid Unicode scalar values, including
credentials and nested workflow text/keys. Unpaired surrogate escapes are
rejected as validation errors before storage or execution. Valid Unicode
passwords retain their exact whitespace and combining characters; passwords
are not trimmed or Unicode-normalized. Validation responses expose only field
locations and error types, never input values or unknown extra-field names.

`POST /api/auth/login` accepts `email`, `password`, and optional `transport`:

- `cookie` (default): the HTTPS session cookie is `__Host-reasoning_session`,
  Secure, HttpOnly, SameSite=Strict, Path=/, with no Domain attribute. The response
  includes `csrf_token`, also stored in the readable `__Host-reasoning_csrf`
  cookie, but does not return the session bearer token. HTTP localhost development
  uses `reasoning_session` and `reasoning_csrf` without Secure or the prefix.
- `bearer`: returns `access_token`, `csrf_token`, `user_id`, and `expires_at`
  without setting cookies. Send `Authorization: Bearer <access_token>` on API
  requests. Do not put bearer credentials in URLs or logs.

Sessions expire after seven days; only credential hashes are stored in the database.
Cookie-authenticated writes require both `Origin` equal to `--public-url` and
`X-CSRF-Token` matching the session's CSRF credential. Bearer requests do not need
a CSRF token. An invalid Authorization header fails rather than falling back
to a cookie. Any supplied foreign Origin is rejected, including on login.
`POST /api/auth/logout` revokes the current session and clears its cookies.

### Account recovery and session management

The sign-in page provides **Forgot password?**. Sending recovery email requires
configured account email.
`GET /api/auth/password-reset` reports availability and email-worker readiness.
`POST /api/auth/password-reset` accepts `email` and a stable `client_id` and returns
the same HTTP 202 acknowledgement for existing and unknown accounts. Requests
are limited to three per email and sixty per peer in fifteen minutes; exact
retries reuse their short-lived request receipt. The installation email cap
applies equally to known and unknown addresses. A full installation allowance
keeps the public response generic without admitting an external email;
administrator requests report the limit explicitly. Recovery creates no account
and never returns a token or delivery state to an anonymous requester.

Recovery credentials are random, stored as hashes, and delivered in an
encrypted outbox message using the configured Resend account. Links use
`/organization.html#reset=...`; the browser removes the fragment before API
requests and handles replacement links in an already-open tab. Links expire
after thirty minutes and can be used once. Requesting another link does not
invalidate an earlier unconsumed link; changing the password invalidates them
all. A challenge is bound to the account's email and current password hash.
Uncertain external delivery remains unknown and is not replayed automatically.

`POST /api/auth/password-reset/confirm` accepts `token` and `password`, atomically
changes the password, verifies mailbox ownership, invalidates all outstanding
recovery links, and revokes every session for that account. It clears this
browser's cookies and requires a normal sign-in; it does not create a session.
`POST /api/auth/password` requires an authenticated session, the current
password and a new `password`; it revokes every session, clears this browser's
cookies, and requires a new sign-in. `POST /api/auth/sessions/revoke-others`
signs out only the authenticated user's other sessions. These actions are
available under **User information**.
Password changes queue a security notification when email and its daily
allowance are available; an admission-limited notification is recorded in the
account audit history without blocking a completed password change. Database
restoration invalidates recovery links and retires retained account email.

Host administrators can recover an account without email:

```bash
uv run reasoning-server --data-dir "$HOME/.local/share/reasoning-server" \
  reset-password --email owner@example.org
```

This prompts for the new password, revokes every session belonging to the account,
invalidates outstanding recovery links, and records an audit event in its
organizations. It is not an HTTP endpoint.

## Organization administration

An authenticated human who already owns or administers an organization can
create another organization and becomes its owner.
An organization has owner, admin, member, and viewer roles. Members can create
organization-visible projects, channels, topics, and messages. Administrators can
instead create a private project with an initial owner and roster. Only that roster
and organization owners/administrators can discover or access its hierarchy,
messages, knowledge, model and compute runs, workflows, search results, or usage.
Owners and administrators who are not on the roster see the project marked
`private · visible to you as admin` in navigation, and project settings list them under
"Also visible through their organization role", so a roster never implies that
nobody else can read the project.
A private-project owner can manage its roster and rename its project and channels;
at least one project owner must remain. Organization owners and administrators can
delete projects, channels, and topics. Private-project owners can delete channels
and topics within their projects. Deletion requires a fresh preview of descendant
counts and the exact entity name. The web confirmation atomically stops subtree
work and revokes pending bot approvals. API clients opt in with `stop_active: true`;
without it, active work requires cancellation first. Unknown charges and external
execution receipts remain retained, and cancellation does not claim external work stopped.
The check and deletion use one transaction, so new
work cannot be admitted while an entity is deleted. Deleted content is inaccessible
through hierarchy, direct links, search, and knowledge, and its bot topic grants are
revoked. Archived records retain audit evidence and accounting history.
Organization owners/admins can inspect unresolved deleted-topic receipts in
**Usage and energy → Retained work from deleted conversations** and reconcile
unknown charges against actual provider activity. `/retained-work` under the
organization API returns bounded billing/cleanup metadata, never message or program content.
Viewers can read organization-visible hierarchy,
members, and messages but cannot write organization content unless they own the
private project. All members,
including viewers, can manage their own read cursors and topic subscriptions.
Owners and admins manage invitations,
provider credentials, policy, bots, and audit access. Only owners manage administrators
and owners, or invite new administrators. Removing or demoting the last owner is
rejected atomically. Removing a membership denies organization access even for an
otherwise valid account session. No organization role grants access to a
[direct conversation](#direct-messages) the member does not take part in.

Manual invitation creation returns a random token once. Lists expose metadata, not the
token or its hash. Invitations expire after at most seven days and can be revoked.
`POST /api/invitations/accept` consumes the token once. A new account supplies
`name` and `password`; its email comes from the invitation. An existing account
must first sign in as the invited email. Acceptance cannot replace its password
or overwrite an existing membership's role. The administration interface returns
a shareable invitation URL with its credential in the fragment, not a query
parameter. The browser consumes and removes that fragment before making API
requests. Links can be shared manually, whether or not email is configured.
`POST /api/invitations/inspect` checks a token without consuming it and reports
pending, used, expired, revoked or invalid state. Only a pending link reveals
its intended email and whether sign-in is required. Inspection has separate
per-peer and per-link limits so it does not consume sign-in allowance. A revoked
browser session does not prevent accepting an invitation for a new account;
invalid bearer credentials and invalid CSRF remain errors.

**Administration → Users** searches members and invitation history by name or
email and filters invitation states. `GET {prefix}/user-administration` requires
current administrator access and returns member/invitation metadata, email
availability, organization-requested recovery delivery states, and the most
recent fifty account, membership and invitation audit events. It contains no
password hashes, session credentials, recovery tokens or encrypted messages.
`POST {prefix}/members/{member_id}/password-reset` requires a stable `client_id`
and sends a reset email to an existing human member. Both caller authority and
target membership are rechecked before delivery. The recipient sets their own
password. Membership removal and role changes retain owner/admin boundaries
and last-owner protection. The separate Invitations tab provides single and
batch invitation creation.

### Optional invitation email

Copy invitation links from administration and share them privately. **New link
and email** revokes an existing invitation and creates a fresh email invitation;
the old link cannot join afterward. Delivery status is not inbox ownership proof.

**User information → Verify email** queues a separate one-hour inbox link.
The recipient signs in to the matching account and explicitly confirms the link.
Challenges are single-use, stored hashed, and never returned by status APIs;
the encrypted outbox shares installation email rate limits. Resending replaces
the previous challenge. Unknown delivery is retained and not replayed automatically.
Manual invitations remain usable without configured email and never mark an address verified.

Authenticated verification API:

- `GET /api/auth/email-verification`: enabled state, verification timestamp, last delivery status.
- `POST /api/auth/email-verification`: stable `client_id` requests an email; retries keep the delivery identity.
- `POST /api/auth/email-verification/confirm`: private `token` confirms the signed-in account.

The server supports an operator-owned [Resend sending account](https://resend.com/docs/api-reference/emails/send-email)
over HTTPS. Verify a sending domain with the provider and use a sending-only API
key restricted to that domain. No Reasoning-operated email service is required.
DigitalOcean [blocks SMTP ports](https://docs.digitalocean.com/support/why-is-smtp-blocked/);
this adapter uses outbound HTTPS on port 443. SMTP and provider delivery webhooks
are not implemented.

Run as the installation state owner, supplying a plain sender email address:

```bash
reasoning-server --data-dir /private/reasoning-state mail-config \
  --sender invites@example.org --output /private/reasoning-state/mail.json \
  --daily-limit 100
```

The command prompts privately for the API key and exclusively creates a 0600
file; it refuses overwriting, symlinks, and insecure parent directories. `serve`
automatically reads `DATA_DIR/mail.json` when present, or accepts an explicit
`--mail-config PRIVATE_FILE`. Restart the server after changing configuration.
A present but invalid/unreadable/insecure file prevents startup, rather than
silently disabling mail. Keep this credential file out of source, release bundles,
shared backups, and cloud user data. The server does not expose its key or sender
through the organization API. The file is not part of installation backups.
`reasoning-deploy mail-input` prepares the same canonical input without requiring
a server data directory. Deployment `install`, `do-install`, and `do-launch`
accept `--mail-file` to privately install it and verify worker liveness during
bootstrap; see [deployment email setup](deployment.md#configure-invitation-email-during-installation).
When mail is configured, public `GET /api/health` adds only the boolean
`mail_worker_running`; it never exposes a credential, sender, or sending limit.

Owners/admins can select **Send invitation by email** in administration.
**Batch invitations** accepts pasted addresses or a one-column email CSV and
shows a live deduplicated recipient count. **Send invitations** is always visible
and becomes available for a valid batch with configured email; **Preview recipients**
optionally expands the list. The command submits at most 100 addresses and freezes
the addresses and selected role after submission so uncertain replies can retry
the same batch safely. Live totals distinguish waiting, provider-accepted,
uncertain/rejected/cancelled, and unqueued recipients; expand **Recipient status**
for individual results. Provider acceptance does not confirm inbox delivery.
Pending email invitations for the same address and role are recognized, including
after a page refresh, and are not submitted again. Use **New link and email** for
an intentional resend, or **New batch** to invite another group.
`POST {prefix}/invitations/batch` accepts `emails`, `role`, optional
`lifetime_seconds` and a stable `client_id`. Each recipient has a durable derived
request identity. One submission queues the entire valid batch within the daily
allowance; the worker paces sending automatically. Retry the same batch only for
an uncertain API reply or recipients refused by the daily limit, after its
indicated wait. Replays include the current `email_state`, even for retired links.
Results are per-recipient and never contain invitation tokens. Queued requests
are not evidence of delivery; inspect the invitation list for delivery state.
Only owners may invite administrators. Keep the batch open through uncertain
responses so retries retain their original identity.
`POST {prefix}/invitations` accepts `send_email: true` and a stable `client_id`
(16–128 ASCII letters, digits, underscores, or hyphens), in addition to the normal
email/role/lifetime. The UI preserves that ID across a failed submission retry.
Creation and encrypted outbox insertion commit atomically. Exact request replay
returns the original invitation/token while it is available; a changed request,
mail credential, sender, or public URL is refused. Invitation lists never expose
the token. `GET {prefix}/invitation-mail` is admin-only and reports `enabled`,
`worker_running`, and the installation's `daily_limit`.

One installation-owned mail worker sends queued messages. It rechecks the
requester's current role, invitation availability, and original sender/account/URL
before claiming a message. The daily admission allowance is the configured
1–10,000 per rolling 24 hours, shared across all organizations and mailbox
verification requests; replays do not consume another slot. Dispatch is paced to
ten attempts per rolling minute, recorded transactionally in the installation's
durable quota ledger before HTTPS and shared with verification emails. Queueing
a batch does not require a minute-limit retry. Rejected submissions still count. Queued
messages survive ordinary restart. Revocation, acceptance, and expiry prevent
unclaimed messages being sent and erase their encrypted recoverable credentials;
a message already claimed/in transit cannot be recalled, but its revoked link
cannot be accepted. Changing mail credentials or origin cancels old queued mail.

Lists include `email_state` and `email_error`. States are `queued`, `sending`,
`accepted`, `rejected`, `unknown`, and `cancelled`. `accepted` means the provider
accepted submission, **not** that the recipient received it. Timeout, malformed
success, ambiguous rejection, or interruption after claiming becomes `unknown`.
No rejected/uncertain message is automatically retried. Share the original link
when needed; revoke it and create a new invitation if deliberately replacing it.
Encrypted link payloads are erased on acceptance/revocation and on the next
configured-worker or enqueue expiry sweep. Restore cancels queued
mail, holds in-flight mail as unknown, erases its credentials, and revokes
unaccepted invitations; it never replays external mail. Live sender/domain and
inbox delivery verification remain operator tasks.

OpenRouter keys belong to the organization. Key-management responses expose only
ID, provider, label, fingerprint, creation time, and disabled time. Disabling a
key retains its encrypted record for audit; it does not revoke the upstream
OpenRouter credential. AES-256-GCM encryption binds ciphertext to the organization,
provider, and key ID. The server refuses startup if its encryption key is missing
for an existing database, or cannot authenticate an existing credential.
The initial monthly budget is zero.
Budget units are millionths of a US dollar; `50000000` denotes $50.
Beta inference requires an active organization key and a positive budget.
Every currently available OpenRouter text model or router can be selected;
there is no organization model whitelist. Concurrent requests reserve the available balance
atomically. These are local admission controls, not a guarantee of the upstream
invoice; configure OpenRouter key limits as well. See [beta execution and
accounting](inference.md) for context capture, failure recovery, and reconciliation.

## API surface

The machine-readable contract is `GET /openapi.json`. Requests with bodies use
`application/json`; bodies, including chunked uploads, are limited to 64 KiB.
Validation errors omit input values and arbitrary extra-field names. Responses
disable caching; access logging is disabled by the CLI.

`GET /api/health` is public and reads no organization data. It returns `status`
(`"ok"` or `"degraded"`), `initialized`, `inference_enabled`,
`inference_worker_running`, `workflow_worker_running`, `webhook_worker_running`,
`mail_worker_running` (only when mail is configured),
`release_id`, `source_revision`, `source_modified`, `schema_version` (the number
of applied server migrations), and `uptime_seconds` (whole seconds since the
application was created). `release_id` is the `--deployment-release` manifest
digest, also sent as `X-Reasoning-Release` on every response, or null without
one. Healthy workers return HTTP 200. A stopped required worker returns HTTP 503
with `status: "degraded"`, including before startup and after shutdown. Explicitly
disabled inference does not count as a worker failure. Queue polling retries
SQLite busy/locked errors after rollback; it does not replay provider calls or
deliveries with uncertain outcomes.
`source_revision` is the 40- or 64-character commit recorded in the release's
`manifest.json` beside `--frontend-dir`, and `source_modified` is that manifest's
uncommitted-changes flag; both are null unless the manifest's SHA-256 equals
`release_id`, so a development server or an unverified manifest reports no
commit. The server reads the manifest once at startup and never returns its
other contents or any file path. The help page shows these fields and links the
commit to `https://github.com/tertius-ai/reasoning/commit/<revision>`.

Paths below an organization require membership and scope every resource to that
organization. Out-of-organization IDs return 404; insufficient role returns 403.
Use the prefix `/api/organizations/{organization_id}` for organization resources.

| Path | Methods | Purpose |
| --- | --- | --- |
| `/api/health` | GET | Public readiness, initialization, verified release identity, and uptime |
| `/api/setup` | POST | One-time owner and organization creation |
| `/api/auth/login`, `/api/auth/logout` | POST | Session creation and revocation |
| `/api/auth/me` | GET | Current account, without password material |
| `/api/auth/password-reset` | GET, POST | Recovery availability and private email request |
| `/api/auth/password-reset/confirm` | POST | Single-use mailbox-proven password recovery |
| `/api/auth/password` | POST | Authenticated password change with current-password proof |
| `/api/auth/sessions/revoke-others` | POST | Revoke the authenticated account's other sessions |
| `/api/organizations` | GET, POST | Memberships and organization creation |
| `/api/invitations/accept` | POST | One-time invitation acceptance |
| `/api/invitations/inspect` | POST | Inspect an invitation without accepting it |
| `{prefix}/user-administration` | GET | Searchable user/invitation metadata, recovery delivery and scoped audit history |
| `{prefix}/members/{member_id}/password-reset` | POST | Administrator-requested recovery email to a current human member |
| `{prefix}/hierarchy` | GET | Projects, channels, typed topics, the member's topic state and direct-conversation summaries |
| `{prefix}/projects`, `{prefix}/channels`, `{prefix}/topics` | POST | Hierarchy creation; beta topics accept an eligible `default_model`, `default_effort` and `knowledge_tools` |
| `{prefix}/projects/{project_id}`, `{prefix}/channels/{channel_id}` | PATCH | Project-owner or organization-administrator rename |
| `{prefix}/{kind}/{entity_id}/deletion` | GET | Authorized deletion preview with descendant/activity counts and a plan token; kind is projects, channels, or topics |
| `{prefix}/projects/{project_id}`, `{prefix}/channels/{channel_id}`, `{prefix}/topics/{topic_id}` | DELETE | Atomic deletion; body contains the exact name in `confirmation` and current `plan_token` |
| `{prefix}/projects/{project_id}/members` | GET | Private-project roster for project owners and organization administrators |
| `{prefix}/projects/{project_id}/members/{member_id}` | PUT, DELETE | Private-project owner/member assignment and removal |
| `{prefix}/topics/{topic_id}/messages` | GET, POST | History and message submission; `adds_to` adds the new message to a running model reply |
| `{prefix}/topics/{topic_id}/message-updates` | GET | Incremental edited-message updates with a revision-event cursor |
| `{prefix}/topics/{topic_id}/events` | GET | Server-Sent Events: message watermarks and run, agent, goal and bot-invocation changes for one readable topic |
| `{prefix}/topics/{topic_id}/messages/{message_id}` | PATCH | Human author's optimistic edit with the current revision |
| `{prefix}/topics/{topic_id}/messages/{message_id}/history` | GET | Authorized immutable original and edited versions |
| `{prefix}/topics/{topic_id}/messages/{message_id}/context` | GET | A page of history around one message, for message links |
| `{prefix}/references` | GET | Access-checked current names of referenced topics and projects |
| `{prefix}/topics/{topic_id}/subscription` | PUT | Current member's topic following preference |
| `{prefix}/topics/{topic_id}/read` | PUT | Current member's monotonic read cursor |
| `{prefix}/direct-conversations` | GET, POST | The current member's [direct conversations](#direct-messages) with unread state; open or create one with another human member |
| `{prefix}/direct-conversations/{conversation_id}` | GET | One direct conversation's summary, for its participants only |
| `{prefix}/direct-conversations/{conversation_id}/messages` | GET, POST | Direct-message history and submission, with the topic message cursors and limits |
| `{prefix}/direct-conversations/{conversation_id}/message-updates` | GET | Incremental edited/reacted direct messages with independent revision and reaction cursors |
| `{prefix}/direct-conversations/{conversation_id}/messages/{message_id}/reactions` | GET, PUT | Participant-only reactions; PUT sets or removes the caller's emoji |
| `{prefix}/direct-conversations/{conversation_id}/messages/{message_id}` | PATCH | Author's optimistic edit with the current revision |
| `{prefix}/direct-conversations/{conversation_id}/messages/{message_id}/history` | GET | Immutable original and edited versions |
| `{prefix}/direct-conversations/{conversation_id}/read`, `.../mute` | PUT | Current participant's monotonic read cursor; mute preference |
| `{prefix}/direct-conversations/{conversation_id}/events` | GET | Server-Sent Events with the `messages` watermark only |
| `{prefix}/direct-blocks` | GET | Members the current member has blocked from direct messages |
| `{prefix}/direct-blocks/{member_id}` | PUT, DELETE | Block or unblock a member's direct messages |
| `{prefix}/search` | GET | Literal substring search across organization topic messages |
| `{prefix}/members` | GET | Organization members |
| `{prefix}/presence` | GET | Active human and sponsored-bot presence; marks the requesting browser active |
| `{prefix}/members/{member_id}` | PATCH, DELETE | Role change or membership removal |
| `{prefix}/invitations` | GET, POST | Invitation metadata and creation |
| `{prefix}/invitations/{invitation_id}` | DELETE | Invitation revocation |
| `{prefix}/invitation-mail` | GET | Admin-only email availability and limits |
| `{prefix}/provider-keys` | GET, POST | Key metadata and encrypted storage for all available OpenRouter text models |
| `{prefix}/provider-keys/{key_id}` | DELETE | Key disabling |
| `{prefix}/policy` | GET, PUT | Monthly budget configuration; legacy model suggestions do not restrict admission |
| `{prefix}/inference-configuration` | GET | Current model catalogue, worker availability, and budget balances |
| `{prefix}/model-prices` | GET | Member-only OpenRouter public text-model capabilities/prices; no inference or organization credential use |
| `{prefix}/model-benchmarks` | GET | Member-only cached Artificial Analysis indices via the organization OpenRouter key; metadata only, no inference |
| `{prefix}/usage` | GET | All-time recorded LLM usage, organization/project totals and coverage; optional `project_id` filter |
| `{prefix}/navigation-usage` | GET | Confirmed provider spending and unknown/external coverage by project, channel and topic |
| `{prefix}/topics/{topic_id}` | PATCH | Rename a topic without changing its identity or history |
| `{prefix}/topics/{topic_id}/reactions/{kind}/{target_id}` | GET, PUT | Counts, current human's single replaceable reaction and up to three reactor IDs/names per emoji; workflow nodes supply `node_id` |
| `{prefix}/model-feedback` | GET | Access-filtered human reaction totals attributed to recorded model identities |
| `{prefix}/topics/{topic_id}/forks` | POST | Create a beta-topic with history through the specified message and retained provenance |
| `{prefix}/topics/{topic_id}/fork-origin` | GET | Read a beta-topic's fork provenance |
| `{prefix}/impact-profiles` | GET, PUT | Member-readable model-energy/location assumptions; replacement is administrator-only |
| `{prefix}/knowledge` | GET, POST | Search/list or create versioned ledger, bibliography, and private-journal entries |
| `{prefix}/knowledge/{entry_id}` | GET, PUT | Read an exact/current revision or append a revision |
| `{prefix}/blog/{post_id}` | DELETE | Revision-checked blog deletion; retain revisions and audit while excluding the post from reads/context |
| `{prefix}/mathematics/{record_id}/human-checks` | GET, POST | Exact-revision human acknowledgements; no proof-status promotion |
| `{prefix}/topics/{topic_id}/inference-runs` | GET, POST | Recent beta runs and idempotent shared-context submission |
| `{prefix}/inference-runs/{run_id}` | GET | Durable model response state and usage |
| `{prefix}/inference-runs/{run_id}/events` | GET | Exclusive-cursor progress replay |
| `{prefix}/inference-runs/{run_id}/cancel` | POST | Requester/administrator cancellation |
| `{prefix}/inference-runs/{run_id}/cost` | PUT | Administrator reconciliation of an unknown charge |
| `{prefix}/inference-runs/{run_id}/additions` | POST | Author moves a later posted message into the running reply; its own queued reply is cancelled |
| `{prefix}/inference-runs/{run_id}/additions/{message_id}` | DELETE | Author removes an addition the reply has not read |
| `{prefix}/compute-configuration` | GET | Approved compute images, organization policy, and quota balance |
| `{prefix}/compute-policy` | PUT | Administrator compute admission/concurrency limits |
| `{prefix}/topics/{topic_id}/compute-jobs` | GET, POST | Gamma compute job history and idempotent submission |
| `{prefix}/compute-jobs/{job_id}` | GET | Durable compute state and captured program/evidence |
| `{prefix}/compute-jobs/{job_id}/events` | GET | Exclusive-cursor compute logs and state replay |
| `{prefix}/compute-jobs/{job_id}/cancel` | POST | Requester/administrator cancellation intent |
| `{prefix}/compute-jobs/{job_id}/reconciliation` | PUT | Confirmed cleanup and uncertain outcome reconciliation |
| `{prefix}/compute-jobs/{job_id}/artifacts` | GET | Committed artifact metadata |
| `{prefix}/compute-jobs/{job_id}/artifacts/{artifact_id}` | GET | Verified attachment download |
| `{prefix}/compute-workers` | GET, POST | Administrator worker metadata/enrollment |
| `{prefix}/compute-workers/{worker_id}` | PATCH, DELETE | Worker draining or credential revocation |
| `{prefix}/compute-scalers` | GET, POST | Administrator scoped capacity/enrollment grants |
| `{prefix}/compute-scalers/{scaler_id}/rotate` | POST | Rotate token/lifetime/sponsor, preserving immutable bounds and request namespace |
| `{prefix}/compute-scalers/{scaler_id}` | DELETE | Permanently revoke scaler access, not existing workers/hardware |
| `{prefix}/audit` | GET | Organization administration audit |

The separate `/api/compute-scaler/{identity,snapshot,enrollments}` protocol
requires scoped scaler bearer credentials, not cookies, human, bot, or worker
credentials; see [compute](compute.md#scoped-compute-automation-and-recoverable-enrollment).

Topic creation supplies `name`, its parent `channel_id`, and `kind`. A beta
topic may supply a `default_model`; availability is verified against OpenRouter
when inference is requested. Non-beta topics reject this field. Adding a provider
key accepts `provider`, `label`, and `key`. Every key can use the live text-model
catalog. Compatibility fields `allowed_models` and `allow_all_text_models` do not
restrict model admission. Task-scoped agent resource envelopes still constrain
which models that particular assignment may use.

The separately authenticated worker protocol includes readonly
`GET /api/worker/identity` for enrollment verification without claiming jobs,
renewing leases, or updating last-seen activity. Worker credentials cannot access
human organization routes; see [compute](compute.md) for its full route contract.
Readonly worker status, self-draining, and irreversible retirement receipts
support cleanup/result-delivery fencing. A retired token can only read/replay
its own receipt, not use ordinary worker or human routes. The server trusts
worker physical-cleanup confirmation; it does not delete cloud hardware.

Message submissions supply `content` (up to 16000 characters) and `client_id` (up
to 128 characters). Replaying the same client ID, author, organization, topic,
and content returns the same message. Reusing that ID for another topic or
content returns 409. History uses increasing `sequence` values, an exclusive
`after` cursor, and a `limit` of 1–200. Audit pagination uses an exclusive `after`
sequence cursor and returns at most 200 events. Messages also accept an exclusive
`before` cursor for backward history; the returned page remains chronological.
Do not combine a positive `after` with `before`. To fetch the latest page, use a
`before` value greater than the last sequence, bounded by `9223372036854775807`.
Read cursors must identify a message in the topic (or zero) and never move
backward. Subscriptions default to following; unfollowing does not restrict
access or discard messages. `hierarchy.topic_state` returns the current member's
read/follow state, unread count, unread mentions, and latest sequence per topic.
Mention syntax is `@[user_id]`; only current members of the same organization
receive mention records. Removing a membership clears its preferences and
mention records without deleting account or message history.

A message link is `/organization.html?organization=ID&topic=ID&message=ID`; it
names the message by its identifier, which edits do not change.
`GET {prefix}/topics/{topic_id}/messages/{message_id}/context` accepts `limit`
(2–200, default 100) and returns `message_id`, a chronological `messages` page
and `older`. At most half the page precedes the message, more when fewer later
messages exist, and `older` reports whether earlier messages precede the page.
A message that is absent or belongs to another topic returns 404, as does a topic
the member cannot read.

Messages reference a topic or project as `#[topic:ID]` or `#[project:ID]`; the
stored text never contains the target's name. `GET {prefix}/references` accepts
comma-separated `topics` and `projects` identifiers, at most 100 distinct
32-character lowercase hexadecimal identifiers in total (otherwise 400), from an
organization member (404 otherwise). It returns `{"topics": {ID: topic or null},
"projects": {ID: project or null}}`: a topic has `id`, `name`, `kind`,
`channel_id`, `channel_name`, `project_id` and `project_name`; a project has `id`
and `name`. Every target passes the same per-member project and topic checks as
the hierarchy and topic reads, so a target in a private project the member does
not belong to is `null`, exactly like a deleted or absent one. Models, bots and
search receive the stored tokens, not names.

### Topic event stream

`GET {prefix}/topics/{topic_id}/events` returns `text/event-stream` for a signed-in
member who can read the topic (session cookie without CSRF, or bearer). Unreadable
or absent topics return 404 before streaming. The response keeps the application's
security headers with `Cache-Control: no-store, no-transform` and
`X-Accel-Buffering: no`, so compressing or buffering proxies pass events through.
Events, each a JSON `data` line:

| Event | Data |
| --- | --- |
| `ready` | `heartbeat_ms` and the topic's `channels`; sent first |
| `messages` | `{"sequence", "updates"}`: the latest message sequence and message-revision sequence |
| `runs` | Beta only: `{"order": [run IDs], "runs": [changed runs]}`, in the `inference-runs` shape; a run whose output only grew carries `output_append` instead of `output_text` |
| `agents`, `goal` | Beta only: the `agents` and `goal` responses, when they change |
| `bot-runs` | The `bot-runs` response, when it changes |
| `end` | `{"reason"}`: `lifetime`, `shutdown`, `access` or `session`; the server then closes the stream |

A connection starts with every channel, so a reconnect needs no server-side
history: the browser compares the message watermarks with its own message and edit
cursors and fetches exactly the missing range with `messages?after=` and
`message-updates?after=`, and run patches apply only to runs sent on the same
connection. Within one change, `messages` precedes the `runs` event that links a
finished run to its message. One watcher per topic and server process reads a
content-free change signature (latest message and revision sequences, recent run,
event and call-receipt markers, agent threads, goal and bot invocations) every 250
ms while work is active and every second otherwise, and at once after any
successful write through the server. When a signature changes, each stream
re-reads that channel through the same access-checked reads as the REST routes.
Streams send a comment every 15 seconds when otherwise silent, re-check the
session and topic access every 30 seconds, and end after 10 minutes. A process
admits at most 256 streams and 8 per account (more return 429). Streams hold no
worker thread while waiting; their database reads run under their own concurrency
limits (four signature reads and eight stream reads at a time), not the limit shared
by ordinary requests. On SIGINT/SIGTERM streams end
within about a second, so shutdown does not wait for them. `serve --no-topic-events`
disables the route (503); browsers then keep polling.
`GET {prefix}/direct-conversations/{conversation_id}/events` is the same stream for a
direct conversation's participants, with only the `messages` channel; it shares the
admission limits, lifetime and access re-checks.

### Direct messages

A direct conversation joins exactly two human members of one organization. It is
not part of the project/channel/topic hierarchy and uses its own tables, so topic
routes, organization search, knowledge, topic exports, bots (including bots that
follow their owner's access), webhooks, inference, agent tools and workflows never
read it. Every direct-conversation route requires the caller to be one of its two
participants; anyone else, including organization owners and administrators,
receives 404. `hierarchy.direct_conversations` contains only the caller's own
summaries, without message content, so navigation refreshes their unread counts
with topic state. Direct-message actions create no audit events.

`POST {prefix}/direct-conversations` with `user_id` returns the existing
conversation with that member (200) or creates one (201). Bots, non-members and the
caller are refused. A conversation is listed for its recipient only once it holds a
message. Summaries contain `other_user` (`id`, `name`), `other_member`, `muted`,
`blocked` (whether the caller blocked the other participant), `last_read_sequence`,
`last_message_sequence`, `last_message_at` and `unread_count`, which counts the other
participant's messages after the read cursor. Sending advances the sender's own read
cursor. Messages, edits, revision history, client-ID replay and cursors behave as for
topic messages; direct messages carry `conversation_id` instead of `topic_id`, and
mention syntax creates no mention records. Direct messages support the same 24
emoji reactions as topic messages, with one reaction per participant per message.
Message pages batch reaction counts, the caller's selection and participant names;
reaction changes have an independent cursor and event watermark, including removals.
Repeated sets/removals are idempotent. Only the participants can read reactions,
and writing requires current membership, write access and an unblocked conversation.
Attachments are unavailable in direct conversations.

Owners, administrators and members can start conversations and send messages.
Viewers cannot write organization content, so they can read, mute and block in
conversations others start with them but cannot start one or reply. Muting keeps
the unread count in the summary; the interface hides it. A block is per organization:
while either participant blocks the other, neither can start a conversation, send or
edit. The blocked member receives `403 direct messages to this person are
unavailable`, the same refusal as for a recipient who has left the organization;
the member who blocked receives `403 unblock this person to send them direct
messages`. Existing history stays readable to both participants. Removing a
membership clears that member's read and mute state and ends their access; the
remaining participant keeps the history. Direct messages are retained like topic
messages: there is no separate expiry or deletion. They are not end-to-end
encrypted; operators with database or backup access can read them.

Search uses `q` (1–200 characters), with a limit of 1–100 (default 50). It searches
message content literally, including percent and underscore characters, and
returns the latest matches with project/channel/topic context. It cannot search
another organization without membership.

## Chat interface

The app bar holds message search and a sun button between search and People
that toggles light/dark mode. The initial theme follows the system; a manual
choice persists across reloads. The account name at the bottom left opens a
menu containing user information, organization information, organization
selection, administration, model capabilities, usage and energy, new organization for
eligible owners, and sign-out. The options stay hidden until the
menu opens; Escape or an outside click closes it. One account can belong to
multiple organizations on the same installation; the organization selector
changes the active membership boundary.
Only an owner of an existing organization can create another.
`GET {prefix}/provider-key-usage` is owner/admin-only and returns all-time
Reasoning provider-call totals per stored key, including disabled keys. Known
charges, current reservations and uncertain-charge holds are separate fields.
Provider-call receipts replace, rather than duplicate, legacy run-level totals.
The response includes no credential or fingerprint and does not cover usage
outside this installation or externally billed bot CLI accounts.
Creation requires an explicit AI-access choice: a fresh OpenRouter credential
with its own budget, deliberate reuse copied into a separately encrypted
organization record, or no credential, models, or budget. Members and viewers
cannot create organizations. Bot and compute credentials are never inherited.
The left navigation groups projects, alternating-color #channels, and typed
topics with unread counts and an `@` marker for unread personal mentions. The
**People** panel is also the direct-message inbox: members with a direct conversation
are listed first, most recent message first, even when offline (a partner who left
the organization is shown as `left`), with a message icon, `muted`/`blocked` marks
and an unread count (hidden for muted conversations); everyone else follows as
before. The People toolbar button shows the unread total while the panel is closed.
Clicking a person's name opens or starts a conversation with them, and `+` in the
People header starts one with any member. A direct conversation replaces the topic view, with mute and block
controls and a confirmation before blocking; the app bar shows its participant. Topics are indented beneath their channels. Creation `+` controls appear on hover beside
Projects, projects, and channels. Project, channel, and topic settings use adjacent
centered `⋯` buttons that appear on hover or keyboard focus and remain visible on touch
devices. Authorized settings expose deletion with descendant counts and typed
confirmation. Topic settings support renaming without changing the topic identity or history.
Deletion checks current child execution receipts rather than trusting a stale workflow summary;
the confirmation lists exact blocking run IDs and recovery actions. Historical completed workflows
remain retained for audit and do not prevent deletion. Active or uncertain inference, compute,
bots, reviews and delegated handoffs remain protected.
Hovering or keyboard-focusing a project, channel or topic reveals its rounded confirmed
provider total, without parentheses, in grey on the same navigation row; leaving hides it. On touch devices,
press and hold a row to inspect its total without navigating or collapsing it. Rows reserve space so hovering does not shift their layout. An asterisk identifies additional unconfirmed calls or externally billed
bot runs; the tooltip reports their counts. Provider call receipts supersede aggregate run receipts,
so multi-round runs are counted once per actual call, not again through messages or workflows.
Project/channel expansion and topic selection are stored locally per user and organization.
On narrow screens, the top-bar overflow menu exposes search, people, export and every shared
knowledge link; no workspace action is hidden to fit the toolbar.
The people, topic export, ledger, bibliography and blog controls form one evenly spaced icon group.
Ledger and bibliography open inside the chat shell in the current tab; the static blog and editor also use same-tab links and shared light/dark theme colors.
The account menu offers keyboard shortcut help (`?` outside a text field). The sun toggle's default follows the operating system, including live changes; explicit light/dark overrides persist locally and apply to the static blog when its theme script is available.
Consecutive alpha-topic messages by the same non-assistant author within five minutes and the same local day share an author header. Date separators label days; continuation timestamps appear on hover or keyboard focus, with full dates in their tooltips.
Message hover actions appear in order: reaction, message actions (**Copy link**
for every message, and beta forking when authorized), and copy. Copy places only that message's Markdown text
on the clipboard; comparison answers and retained partial responses have their
own copy controls. Read-only readers can copy without gaining write access.
Active inference and bots show their latest recorded progress in one compact
line; successful bot responses do not add a separate activity-history card.
Human authors with current topic write access can edit their own messages through
the hover action menu. Saves require the current revision and retain immutable
history. Editing does not invoke a model, bot, workflow, or webhook. Existing
responses and original bot/webhook events retain their captured inputs; future
conversation context uses current messages and invalidates stale research memory.
Beta authors can explicitly request a new reply with the selected model after
editing. Message selection survives polling, and hover reactions remain separate
from the action menu and continuation timestamps.
Project creation lets administrators choose organization visibility or a
private owner and roster. Clicking a channel name collapses or expands its topics; there is no
separate disclosure control. Topics render
as an italic, alternating-color Greek kind glyph joined to the name, such as
`α-planning`. Topic creation presents the three topic types as compact choices.
Beta creation then requires a default model when models are enabled and keeps
its default knowledge tools in a collapsed disclosure. The primary conversation
renders Markdown and KaTeX mathematics
delimited by `$…$`, `$$…$$`, `\(…\)`, or `\[…\]`, while leaving delimiters in
inline and fenced code unchanged. Author labels and timestamps accompany every
message. Private uploaded pictures and PDFs render through authenticated attachment
references; `/attach` opens the file chooser. Raw HTML and external images are not rendered;
member names inserted by mentions are escaped as Markdown text.
A message's Markdown and KaTeX output is rendered again only when its displayed
text changes; refreshes that return no change, composer typing, and model-menu
updates reuse the rendered transcript.

An open topic holds one event stream (see [topic event stream](#topic-event-stream)),
shared by its transcript, agent activity and bot invocations. While it is live,
those views apply its events directly and poll only every 30 seconds as a safety
net, discarding a poll result if an event arrived after that poll started; a newer
message watermark makes the transcript fetch the missing messages and edits at once.
A refused, broken or silent stream (no `ready` within 10 seconds, or no bytes for
three heartbeats) returns the views to the polling below at once and is retried
with backoff from one second to one minute; a refusal or an `access`/`session` end
waits a minute, so polling reports the lost session or access. A scheduled end
reconnects within a second without leaving the live state. A hidden tab closes its
stream after a minute and reopens it when visible.
Visible-tab message polling uses an exclusive sequence cursor every two seconds
and retries after a network failure. The topic hierarchy (including unread state
and direct-conversation summaries) refreshes every four seconds; members and the
organization list refresh with every third hierarchy refresh. Hidden tabs make no scheduled refreshes; returning to a
visible tab refreshes every paused view immediately. Each refresh waits for the
previous request to finish before scheduling another.

Activity views slow down while their work is idle and return to their active cadence
when work appears or the transcript gains a message: beta agent activity (runs,
agent threads, goal) every second, then up to five seconds; research proposals,
tool receipt summaries and reviews every 2.5 seconds, then up to ten; topic bot invocations
every two seconds, then up to eight; topic compute jobs every two seconds, then up
to ten; a finished compute job's log and artifacts up to thirty; and, while
**People** is open, the owner's pending bot approvals every three seconds, then up
to fifteen while none are pending. Each slows only after three quiet refreshes (one
for a finished compute job). Compute images, policy
and CPU balance load only while the submission dialog is open. The composer's model
configuration refreshes every ten seconds and topic bot models every five (beta) or
ten seconds; the server re-checks policy, membership and admission when a request is
sent, so display refreshes do not authorize work.

Views share in-memory reads of the same resource: concurrent requests for the
organization list, hierarchy, members, a topic goal, model or compute configuration,
or topic bot models use one response, and a recent response is reused within a
short per-view window (at most a few seconds). The public model catalog
(`model-prices`) and model benchmarks are shared for five minutes across topics and
dialogs; "Refresh catalog" fetches them again. Any write invalidates shared reads;
the catalog and benchmarks survive only writes within a topic. Read-only
mathematics operations sent with `POST /mathematics` (for example the
Bibliography's `ledger_references`) are not writes and invalidate nothing. The eight most
recently viewed topics keep their loaded messages and message/edit cursors in
memory: revisiting one shows them at once while the poll fetches only newer
messages and edits, and a 403 or 404 discards the snapshot (and its displayed
messages if not yet revalidated). The eight most recently viewed beta topics also keep,
per account, their five newest tool receipt summaries and receipt cursor: a revisit
shows them at once and requests only receipts created since the oldest one still
running (or since the newest one), and a 403 or 404 discards them. A receipt's
arguments and result are read only when it is opened. Shared reads and topic snapshots are cleared on
sign-out and session loss, and failures are never reused. Sending, model requests
and other spending actions are checked by the server when they are made.
Startup loads health and session state concurrently; an authenticated session can
open the workspace before health finishes. The topic tree, initial conversation
page, and bot list render independently of member, edit-history, and permission-tree
requests. Slow secondary requests do not hold up already available content.
Messages are reconciled by ID; a committed send whose response was lost reuses
its client ID on retry. Drafts are held only in memory, separately by account,
organization, and topic, and cleared on sign-out/session loss. Session tokens
are HttpOnly cookies, not browser local-storage bearer credentials. Unread state
is server-owned; the interface acknowledges displayed messages only when the
window is focused, visible, and at the bottom of its loaded history.

Use Ctrl/Command+B or the always-visible button beside the R logo to open or
close project navigation, Ctrl/Command+K for search, Alt+Up/Down for topic navigation, and
Enter to send; Shift+Enter inserts a newline. Ctrl/Command+Enter also sends.
Tab from the composer focuses Send, and Enter then
submits it. Typing `@` at a word boundary opens member-name completion; arrow
keys select a suggestion and Enter inserts the encoded mention. Tab or Escape
dismisses suggestions; there is no mentions button. Typing `#` at a word boundary
opens the same kind of list for the topics and projects in the writer's hierarchy,
with the same keys; choosing one inserts a readable label that is sent as a
reference token. Composition/IME Enter is not intercepted.
Escape closes dialogs, menus, and search results without changing project navigation.
Model capabilities display intelligence and agentic indices compactly and coding
in expanded model details, when Artificial Analysis publishes an exact model-ID
match through OpenRouter (or the catalog's explicitly reported canonical slug,
never a guessed name or stripped variant). The browser loads benchmarks separately from prices;
absent credentials/scores or provider failure never block model selection.
Snapshots retain the upstream citation, source URL, and as-of timestamp, with
CC BY 4.0 attribution. The bounded in-memory cache is isolated by organization
and key ID: successful refreshes are at most daily, failures retry after five
minutes, and retained snapshots expire after seven days. Failed refreshes are
explicitly stale; indices are not mathematical correctness guarantees.
`?` toggles the keyboard and slash-command reference outside text fields;
Ctrl/Command+Shift+P toggles people and bots. `/tex` in beta/gamma topics
prepares an owner-approved local bot compilation request; see [media](media.md).
The sidebar overlays the conversation on narrow screens. Administration provides
searchable users/roles and invitation history, shared invitation links and optional email, bots, compute, and an
Inference area combining OpenRouter keys with budget and routing policy. Existing keys
are listed separately from a closed Add key form, whose fields always represent
a new credential. Every key can use the current text-to-text catalog without
an application model whitelist. Destructive
membership/invitation/key changes require confirmation. Each Administration tab
loads only its own data when opened (user administration; invitation creation and email settings;
keys and budget; bots; compute; feedback). The research blog editor requests its published
and draft lists at once and shows each as it arrives.
Creating a beta topic selects its initial model, advertised effort and knowledge
collections. The channel's `/bot-models` endpoint lists following-access bot
routes eligible for a new topic; creation rechecks those grants atomically.
`GET /api/auth/model-favorites` and `PUT /api/auth/model-favorites` read and toggle
the signed-in person's server-stored favorites across devices and organizations.
The composer selects one available model per message, leads with mathematics
recommendations, and exposes the complete chat-model catalog. Choosing a model
with advertised reasoning-effort levels opens a compact keyboard-accessible
effort menu; the selected model and effort share one composer label. Beta topics use a
token-budgeted context compiler with cited memory, pins and source retrieval.
The `/ledger`, `/files`, `/goal`, `/context`, and `/tools` commands expose typed
mathematical records, versioned text files, independent review results, persistent
goal limits, exact prompt/context evidence, grants, and proposed paid executions.
Ordinary typed research writes save automatically; authors cannot self-certify.
See [research workbench](research-workbench.md).
Model progress arrives through the topic event stream, or without it polls every
second, and follows its triggering message in the transcript, including requests
made in the same second. Run output is applied as soon as its event or read returns; the agent-thread and goal reads that decide controls and
cadence are applied separately and never delay the next run read. A finished run
stays expanded until the transcript shows its reply message, and the transcript
refreshes at once when such a reply is missing. Play and stop control
retained agent continuations without a separate agent-status banner; failures
and uncertain charges remain available in their run records. Finished and partial
replies become topic messages labeled with their model, not their requester.
LLM messages and run records show provider-reported tokens/cost plus explicitly
configured operational kWh and kg CO₂e scenario estimates. Human/bot chat posts
do not represent inference calls. The requester or an administrator can cancel an active response. The
inference worker settles an uncertain run from the provider's record of its
generation, on a bounded schedule and without replaying the request; see
[inference accounting](inference.md). Administrators can record a verified
provider cost for a run that stays uncertain. Unsent composer text
is never included in a model request. A later message can join a running
reply instead of starting its own, from **Add to this reply** under the reply's
timeline, ↩ on its question, or a posted message's actions menu; see
[inference](inference.md).
The account menu's **My bots** provides personal bot connections bounded by the
member's current permissions. **Follow my access** resolves all owner-visible
topics dynamically, including newly created private topics. Explicit-only grants
retain their selected topic restriction. People nests bots under their owners.
Administration also provides bot topic grants, expiry, rotation, and permanent
revocation. Bot credentials are displayed once in component memory; chat labels
bot senders separately from people and LLM participants.
Read-capable bots can have one signed outgoing webhook each. Administration captures
exact creation requests for manual lost-reply recovery while the panel stays
open, displays signing secrets only in component memory, and shows bounded
delivery history, pending/dropped counts, worker availability, and revocation.
Receivers must verify signed bytes and durably deduplicate delivery IDs.
The account menu opens the model-capability browser and organization/project usage.
Top-right research buttons open dedicated ledger, bibliography, and blog tabs.
These provide searchable, versioned
organization/project/topic ledger, bibliography, and private research-blog
entries with Markdown/math rendering. The browser filters the current text-to-text catalog by provider, tool
use, reasoning, image input, and search text. It shows
modalities, supported parameters, context/output limits, and advertised token,
request, cache, reasoning, image, and web-search rates when reported. Catalog browsing
does not run models. Chat selectors omit batch-only, embedding, and reranking aliases.
Usage shows known
totals with per-metric run coverage, not invented zeros for unreported values.
Administrators configure per-model energy and assumed inference-location/grid
coefficients under Usage and energy; see [usage estimates](inference.md#usage-and-operational-impact-estimates).
Gamma topics lead with a compact workflow workspace. Compare models sends one
question to two to eight parallel models in one action. Its collapsed card shows
the complete retained question and model roster, with expandable answer cards.
Completed comparisons omit revision counters, state-event logs, and rerun controls.
New workflow builds an ordered sequence of AI,
approval, and compute steps; its run remains a separate explicit action. Saved
workflow runs appear in topic chat order, while immutable definition evidence and raw
JSON remain available through workflow diagnostics and the authenticated API. Single containerized
computations retain captured-request retry, shared history, plain-text
logs/source/evidence, and committed artifact links in their own disclosure.
Administration manages compute quota/images and one-time worker enrollment,
draining, and revocation. Unknown outcomes retain reservations until an
administrator records verified cleanup and usage.
Unresolved saves retain their original request IDs across topic navigation and
warn before closing the page; they are not stored in browser persistent storage.
See [compute operations](compute.md) for limits,
credential handling, and the browser-only artifact download path.

## Shared knowledge and presence

Shared knowledge has three collections: ledger, bibliography, and the private
research journal. Every entry is scoped to the organization, one project, or
one topic. Creation records revision 1; edits append immutable revisions with
the author and time while advancing the current head. Ledger states are
`note`, `conjecture`, `established`, and `refuted`; bibliography states
are `active` and `archived`; journal states are `draft` and `published`.
All organization members can read. Owners, administrators, and members can
create and revise; viewers cannot write. Search is a bounded literal title/body
substring search over current heads.

Beta-topic `knowledge_tools` selects `ledger`, `bibliography`, `journal`, and
`workflows`; all four are selected by default and can be cleared at creation.
Independent `agent_tools` groups select history, arXiv, public web, knowledge,
gamma workflows and compute. The native research loop retrieves authorized
organization/project/topic knowledge and exact revisions. Journal drafts and
alpha messages are excluded. Blog tools edit drafts automatically; publication
requires a human action. Paid execution proposals retain their approval checks.
Every provider continuation and compaction is separately priced and
recorded; see [research agents, context and goals](inference.md).

The presence endpoint updates the requesting human's activity and returns all
human organization members and enabled bots, including offline identities.
Bots identify their attached person; activity within 45 seconds sets the online
flag. Bot activity comes from authenticated bot API use. Presence is advisory UI state, not authentication,
job ownership, or proof that an external process remains healthy.

## Versioned workflow definitions

Workflows belong to gamma topics. Human owners, administrators, and members can
create and revise them; viewers can inspect definitions and history. Every
resource is organization-scoped. Bots, worker credentials, and removed members
cannot use these human authoring routes. Definitions can be authored before
models, images, or provider credentials are configured. Definition saving does
not check execution availability, reserve budget, enqueue compute/inference,
record an approval, or contact a provider. Execution requires a separate,
explicit [workflow run request](#durable-workflow-runs).

Gamma topics display human messages, workflow runs, and standalone computations
in one chronological stream. Typing `/` opens a command palette containing
`/workflow`, `/compare`, `/compute`, `/json`, `/ledger`, and `/files`; `?` toggles
the command and keyboard reference. The workflow builder creates ordered AI,
approval, and compute steps; **Run workflow** saves an immutable definition and
then starts a separate idempotent run. Definition and execution request IDs are
retained independently if either reply is lost. Existing run cards expose results
and approval. Composer play (▶) retries an acknowledged failed run at its exact
revision or starts the original pinned version again; stop (■) requests
cancellation of active work without implying cleanup or releasing reservations.
A retained unacknowledged request is retried with the same identity. Comparison
dates share a line with model names. Earlier activity loads incrementally without a fixed history ceiling.
Workflow-owned compute children appear inside their workflow, not as duplicate
standalone cards. Historical runs and version APIs remain available to clients.

Each definition has exactly `format`, `name`, and `nodes`. Format is the integer
`1`; name is nonblank, single-line UTF-8 text of at most 200 characters. Each node
has exactly `id`, `kind`, `depends_on`, `inputs`, and `parameters`. Node identifiers
and input aliases start with a lowercase ASCII letter and contain at most 64
lowercase letters, digits, underscores, or hyphens. Nodes form a DAG: identifiers
are unique, dependencies exist, and duplicate dependencies, self-dependencies,
and cycles are rejected. Definitions contain 1–32 nodes, at most 128 dependency
edges, and at most 32 KiB of canonical UTF-8 JSON.

Accepted node parameter shapes are:

| Kind | Required parameter fields | Accepted output binding names |
| --- | --- | --- |
| `compute` | `image_id`, `language`, `program`, `resources` | `logs`, `artifacts` |
| `llm` | `model`, `prompt`, `max_completion_tokens` | `text` |
| `approval` | `message`, `required_role` | `decision` |

Compute nodes use the [job languages and six resource bounds](compute.md#http-request-contract).
Their image identifiers follow the node identifier syntax. LLM prompts have at
most 16,000 characters; completion limits are null (provider default) or strict
integers from 16 to 2³¹−1, subject to the selected provider's capacity.
Approval messages have at most 2,000 characters, and `required_role` is `member`
or `admin`. Saving these parameters does not execute requests or grant approvals.
No node accepts a provider key, credential selector, arbitrary URL, or host path
as a configuration field. Programs and prompts are organization-visible source;
do not put credentials into their text.

`inputs` maps aliases to objects with exactly `node` and `output`. Each selector
must reference a direct dependency and one of that dependency kind's accepted
output names. Validation records these selectors as data; it does not evaluate
expressions, substitute templates, read files, or produce outputs. For example:

```json
{
  "format": 1,
  "name": "Review and reason",
  "nodes": [
    {
      "id": "review", "kind": "approval", "depends_on": [], "inputs": {},
      "parameters": {"message": "Review the question.", "required_role": "admin"}
    },
    {
      "id": "reason", "kind": "llm", "depends_on": ["review"],
      "inputs": {"review": {"node": "review", "output": "decision"}},
      "parameters": {"model": "choose-an-organization-approved-model", "prompt": "Explore the reviewed question.", "max_completion_tokens": null}
    }
  ]
}
```

Using `/api/organizations/{organization_id}` as `{prefix}`, the API is:

| Path | Methods | Purpose |
| --- | --- | --- |
| `{prefix}/topics/{topic_id}/workflows` | GET, POST | Current-head summaries and workflow creation |
| `{prefix}/workflows/{workflow_id}` | GET | Current immutable version |
| `{prefix}/workflows/{workflow_id}/versions` | GET, POST | Version metadata and optimistic revision |
| `{prefix}/workflows/{workflow_id}/versions/{version}` | GET | Exact immutable version |

Create bodies have exactly `client_id` and `definition`. Revision bodies also
require strict integer `expected_version`, equal to the current head. A stale
head returns 409 without creating a version. Clients must retain the original
16–128 character ASCII letter/digit/underscore/hyphen `client_id` and body after a
lost reply: an exact retry returns the original version even after later edits;
reusing an ID with different operation, target, expected head, or definition
returns 409. IDs are scoped to the organization and author, not authentication
credentials. HTTP 201 responses include the original client ID, author, topic,
workflow ID, version, canonical definition, SHA-256 digest, and deterministic
topological node order. The shared client verifies the saved definition and
digest before clearing its pending request.

Definitions and dependency lists are normalized by node identifier; canonical
JSON sorts object keys and retains Unicode. Each workflow has at most 128
immutable versions, and a topic has at most 100 workflows. Authors can create
at most 60 versions per organization per hour; exact retries do not consume that
allowance. List pages have at most 50 summaries: use `after` with the last workflow
ID for topic lists, or `before` with the last version number for newest-first
history. Detailed versions verify their definition digest before being returned.
SQLite transactions atomically record the head, immutable version, retry receipt,
and audit event. Database triggers reject version updates/deletions; deferred
tenant foreign keys require every head to reference an existing version.
[Encrypted backups](backup.md) preserve definitions, history, and retry receipts
and verify definition integrity before accepting an archive.

## Durable workflow runs

An explicit run pins one immutable definition version and each compute node's
approved image record. Human members can start runs; viewers can inspect them.
The server checks the requester's current membership, compute image/quota, and
model/key/budget configuration. It does not launch cloud hardware. Ready compute
and LLM nodes use the existing child queues, quota reservations, cancellation,
and accounting. Child admission and its workflow-attempt linkage commit in one
SQLite transaction. Restart reconciliation follows those original child IDs,
not new paid requests. Price quotes are fetched without organization credentials,
outside the write transaction; admission checks current policy again afterward.

The installation runs one exclusive workflow reconciler, independently of its
inference worker. Disabling inference rejects workflows containing LLM nodes;
compute and approval workflows remain available. Organization workflow
configuration reports the reconciler's actual liveness. Completed/partial LLM
output becomes a gamma-topic message through the ordinary inference executor.
Workflow LLM context contains the system message, the saved node prompt, and
captured dependency data; it does not include topic history or composer drafts.

An approval node waits until its dependencies succeed. Its configured `admin`
role requires an owner/administrator; `member` permits any current human writer.
The decision records the approver, attempt, and approve/reject choice. Rejection
fails that node; unsuccessful dependencies block descendants. Independent
branches can continue. Run states are `running`, `awaiting_approval`,
`outcome_unknown`, `succeeded`, `failed`, and `cancelled`. Approval waiting is
reported when remaining pending work is behind gates and no child is active.
Unknown child outcomes keep reservations and prevent dependent work; they are
not automatically retried. A model child whose charge was uncertain settles
automatically when the provider's generation record is found, and its node then
takes the child's status. Otherwise use the existing child reconciliation APIs
after verifying provider cost or physical-container cleanup.

Cancellation is available to the requester or an administrator. It cancels
undispatched nodes and requests cancellation of linked children. Removing or
demoting the original requester to viewer also requests cancellation. The run
does not become terminal while a linked child is active or uncertain. Explicit
retry requires a current failed/cancelled revision and settled child accounting.
It appends attempts for unsuccessful nodes, retains successful results and
approvals, and rechecks original-requester authority and current policy. Changed
compute image records require a new run. There are at most 16 attempts per node,
64 unfinished runs per organization, and 256 per installation, including
retries. New starts are limited to 20 per requester/organization per 15 minutes;
actions to 120 per 15 minutes and 4,096 per run. Child admission limits also apply.

Dependency values are captured as canonical JSON with a SHA-256 digest.
`logs` is the full successful compute log; `artifacts` supplies committed
artifact metadata, not file contents or mounted paths; `text` is the successful
LLM output; `decision` identifies the successful approval. Captures have at most
32 aliases, 64 KiB encoded bytes, eight levels of nesting, and 128 entries per
nested object/array. Oversized data fails the dependent node without truncation.
No template or expression is evaluated. Compute programs remain unchanged;
input-aware workers write inert data to `/work/inputs.json` through bounded
stdin before execution, under the program user and within the job's wall-time
allowance. Older workers cannot claim input-bearing jobs. Programs can read the
JSON file using their language's ordinary file/JSON support. Model dependency
text remains untrusted data; this transport is not a prompt-injection defense.

Using `/api/organizations/{organization_id}` as `{prefix}`:

| Path | Methods | Purpose |
| --- | --- | --- |
| `{prefix}/workflow-configuration` | GET | Runtime availability and worker liveness |
| `{prefix}/workflows/{workflow_id}/runs` | POST | Start an exact version with `version` and `client_id` |
| `{prefix}/workflow-capacity` | GET | Organization allowance, named occupied runs, released history and anonymous hidden count |
| `{prefix}/workflow-runs/{run_id}/capacity-inspection` | GET | Scoped node/child states; administrators can inspect retained operational records from deleted topics |
| `{prefix}/topics/{topic_id}/workflow-runs` | GET | Newest-first summaries; `before` sequence and `limit` up to 50 |
| `{prefix}/workflow-runs/{run_id}` | GET | Pinned definition/image records and current node attempts |
| `{prefix}/workflow-runs/{run_id}/events` | GET | Up to 200 events after exclusive `after` sequence |
| `{prefix}/workflow-runs/{run_id}/nodes/{node_id}/attempts` | GET | Bounded attempt history |
| `{prefix}/workflow-runs/{run_id}/nodes/{node_id}/attempts/{attempt}/inputs` | GET | Verified captured dependency values and digest |
| `{prefix}/workflow-runs/{run_id}/nodes/{node_id}/approval` | POST | `client_id`, strict `attempt`, and `decision` (`approve`/`reject`) |
| `{prefix}/workflow-runs/{run_id}/cancel` | POST | `client_id` |
| `{prefix}/workflow-runs/{run_id}/retry` | POST | `client_id` and strict current `revision` |
| `{prefix}/workflow-runs/{run_id}/release-capacity` | POST | Administrator retirement of stopped uncertain work; `client_id` and current `revision` |

Requests use a stable 16–128 character ASCII letter/digit/underscore/hyphen
client ID. Lost start replies replay the same run with its current progress.
Action replies are immutable receipts: exact replay returns the original
receipt even after progress changes; changed input conflicts. Current human
authority is required on replay. All reads, children, attempts, actions, and
events are organization-scoped. Cookie writes require ordinary origin/CSRF
checks; bots and worker credentials cannot use these human routes. Provider
credentials are not exposed in definitions, captures, events, or run responses.
Restored runs are fenced from dispatch, retry, and approval; recovery requires a
new deliberate run. Administrators may release their admission slots while
retaining the restore fence and uncertain outcomes.

The shared gamma frontend authors and inspects immutable definitions and starts
the selected exact version. Shared run panels show current status, revision,
ordered node attempts, approval decisions, cancellation, and explicit retry of
unsuccessful nodes. Viewers can inspect but cannot execute or approve. Current
writer/administrator roles and restored-run fences determine available controls;
the server rechecks authorization for every action and replay. Attempt inspection
shows bounded history and captured dependency inputs, with linked computation
logs/artifact downloads and model output/accounting. Unknown outcomes retain
reservations until the provider's generation record settles a model charge or
an operator reconciles a verified outcome; they are never retried automatically.

**Workflow activity**, in a gamma topic's details card, opens the organization
workflow allowance and retained-run inspector. Each unfinished workflow occupies one place; a multi-model comparison
counts as one workflow. The allowance is 64 per organization and 256 per
installation, and includes waiting/uncertain work. Actual model and compute
execution use their separate worker, quota and spending controls. Released places
are listed under their own filter and do not count toward the allowance.

Rows identify the workflow, project/channel/topic, requester and time. The total
includes all organization work; inaccessible runs contribute only an anonymous
occupied count. Administrators can inspect operational records from deleted topics,
including current node/child states and model accounting state. This read surface
contains no transcript, definition, output or credentials and does not restore
ordinary access to a deleted topic. Up to 200 records are returned, with occupied
places first and released records newest first; the summary reports truncation.

Run summaries, runs and their node attempts carry `outcome` and `cost_unknown`
beside `status`. A finished model call whose charge is uncertain keeps its node
and run `outcome_unknown`, so retry and capacity stay fenced. Its `outcome` is
still what the call did, for example `cancelled`, with `cost_unknown` true. A
run whose nodes all have known outcomes reports `cancelled` when cancellation
was requested, `succeeded` when every node succeeded, and otherwise `failed`.
Comparisons and workflow runs show such work as "cancelled · cost unknown"
rather than "outcome unknown".

An administrator can release the place of an `outcome_unknown` workflow after
inspection, including one in a deleted topic. Known active children must stop
first; a missing child record prevents release. The inspector asks for explicit
confirmation. This records an immutable action receipt, sets `capacity_released`,
and prevents further dispatch through cancellation. Child outcomes, usage
reservations, unknown accounting and original records remain. Settling a provider
charge or physical compute cleanup is a separate operation. A justified retry
after all children settle acquires a place again and still requires current topic
access. Exact action replay retains its original receipt.

Execution requests are captured immutably per signed-in user/organization/topic.
In-app navigation retains uncertain start and action requests. Explicit replay
uses the original version, approval attempt/decision, or retry revision and
verifies the returned run/action identity before clearing the request. Failures
retain it; a deliberate local-forget action warns that accepted work is not
cancelled or deleted. Requests are not automatically resubmitted or stored in
browser storage. Reloading or closing the app can lose the in-memory request;
the browser warns when such requests remain. Check shared history before
deliberately creating a new run. Run history loads up to 200 records in bounded
pages, with topic-checked lookup by ID for older runs. State events remain in
API diagnostics, not the conversation interface. A reconnecting panel keeps its loaded progress but disables new
execution/actions; explicit replay of a retained request remains available.

Rendered HTTP/store fixtures use synthetic provider and worker output. They do
not establish real Docker algebra, live OpenRouter requests, TLS/cloud recovery,
or source-built native-client behavior.

## Installation state and verification

The data directory must be owned by the server user, have no group/other
permissions, and not be a symlink. Secret and database files must also be private,
owner-owned regular files. SQLite files, `encryption.key`, `setup-token`, and the
private `artifacts/` tree are installation state. Backups need equivalent
protection. Loss of the encryption
key prevents recovery of provider credentials. Do not copy only a live SQLite
database file while discarding its WAL. Use the host-local [encrypted backup and
new-state restore commands](backup.md); the operator owns scheduling, off-host
storage, physical fencing, and replacement-service activation.

Run the boundary and real-HTTP startup tests with:

```bash
uv run --extra test pytest -q tests/test_public_server.py tests/test_public_chat.py tests/test_public_inference.py
uv run --extra test pytest -q tests/test_public_compute.py
uv run --extra test pytest -q tests/test_public_compute_ui.py
uv run --extra test pytest -q tests/test_public_workflows.py tests/test_public_workflow_runs.py
uv run --extra test ruff check src/reasoning_ledger/server tests/test_public_server.py tests/test_public_chat.py tests/test_public_inference.py
uv run --extra test ruff check tests/test_public_compute.py
```

Run component checks and the real three-user browser journey against a temporary
installation (no provider credentials or paid cloud resources required):

```bash
npm --prefix web test
(cd web && npx playwright install chromium && npm run test:public-chat)
(cd web && npm run test:public-inference)
(cd web && npm run test:public-compute)
(cd web && npm run test:public-workflows)
```

Build release packages with `uv build`, which builds the wheel from a fresh source
archive. Verify that the wheel contains exactly the current Python modules and
matching source/assets, server entry point, and license files:

```bash
uv build --no-build-logs
uv run python -m reasoning_ledger.distribution dist/reasoning_ledger-0.1.0-py3-none-any.whl
```

Direct wheel builds from a working checkout can retain files in a build-backend
cache; they are not the release build command. The verifier rejects cached files
absent from the source tree, stale bytes, and missing Python modules or legal
material.
