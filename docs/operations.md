# Server inspection, release changes, and recovery

Wizard-created single-server test deployments without compute use the guided
path from a clean checkout:

```sh
git pull --ff-only
make upgrade
```

It synchronizes this operator with the verified installed release before rebuilding, derives exact host identity
from the original protected deployment state, opens SSH only to the operator's
current `/32`, and drives the read-only plan, preparation, activation, HTTPS
verification, and SSH closure below. Plans and operation IDs survive retries in
`~/.reasoning-deploy/upgrades/`. Keep that entire directory. Production use and
deployments with external compute still require the manual fencing and rehearsal
requirements in this document.

A refused preflight prints known failed/unverified check names and does not
attempt preparation or activation. This does not establish that the existing
service is healthy. Repair the reported checks before retrying. On retry, a
refused plan with no operation, preparation receipt, or operator state is retained
as a private `refused-plan-*.json` snapshot and the server is checked again.
Accepted plans and plans bound to an operation are not replaced. Raw remote
output, service logs, and credentials are not printed as diagnostic details.

If synchronization finds this operator's unfinished upgrade, `make upgrade`
refuses before retaining or rebuilding a new target. Run `make upgrade-resume`
with the same `DEPLOY_STATE` to verify ownership and resume the exact retained
archive, plan, and operation. It prompts locally for SSH access and upgrade
confirmations, requires no Node/frontend build, and closes temporary SSH afterward.
With no pending upgrade it only synchronizes operator state; another operator's
operation is refused rather than adopted. After completion, `make upgrade` can
build the current checkout.

`make email-setup` enables Resend invitations and account-verification email on
an existing managed server. It synchronizes release identity and refuses pending
upgrades, collects protected sending credentials and typed consent locally,
and closes its ownership-checked temporary SSH access after installation or
failure. The installed release must declare `existing-mail-setup-v1`; otherwise
upgrade first. Preserve `DEPLOY_STATE/mail-setup/` to resume the same exact input.
See [existing-server email setup](deployment.md#configure-invitation-email-separately-on-an-installed-host).
Email and compute setup support both original and independently onboarded
operators. The confirmed cloud plan and synchronized installed release bind the
server URL; credential-only onboarding state does not need a duplicate URL.

Upgrade backup and migration scratch uses private, operation-specific directories
under `/var/lib/reasoning-upgrade-scratch/` on the data filesystem, not RAM-backed
`/run`. Allow disk space for the source snapshot, encrypted archive, and decrypted
verification copies. Preparation seals scratch directories against service-account
access before activation. Resumption also seals any retained `/run` scratch for the
same operation without replacing its original backup or journals.

Resumed preparation uses the current operator tooling without rebuilding the
retained target. Its service-user migration runner recognizes the raw-SQL
executor shape and, when SQLite table rebuilds are required, uses the verified
target's schema-operation helpers in memory. Target archives, installed files,
operation identities, backups and activation guards are unchanged. Unrecognized
executor shapes are refused. Ordinary offline migration and the compatibility
runner disable foreign-key enforcement only on their migration connection before
the transaction, then require exact schema, integrity and full foreign-key checks
before committing. Both the authenticated dry run and live migration use this path.

The migration runner also supports the exact claim-index repair in retained
targets. It repairs only derived claim lookup identities inside a savepoint,
retaining record bodies, scope, authorship and timestamps. The exact revision
immutability guard is verified before the operation and restored before it
finishes; failure rolls back the repair and guard together. Current source
deployments use the same checked SQLite operation. PostgreSQL performs its
derived-index update with the corresponding guard bracketed inside the atomic
migration transaction. Original release archives and recovery identities remain
unchanged when resuming a pending target with current operator tooling.

A remotely reported failure can include the known, identity-bound upgrade journal
phase (for example, `migrating`) and a fixed migration diagnosis. Diagnoses distinguish
disk/quota exhaustion, permissions, read-only filesystems, in-flight execution,
maintenance locks, schema/integrity failures, retained-state mismatches and helper
termination/timeouts. Unknown causes remain generic. The current operator tooling
collects these diagnoses when resuming a retained target, without rebuilding or
changing that target. Exception text, SQL, database contents and helper stderr
are not returned. Use `make upgrade-resume` to retry the exact pending operation;
a fresh build is not a recovery step. This is diagnostic information, not a completion
receipt or authorization to restart the server. Unknown, malformed or differently
bound reports remain unverified; raw exception text, credentials and database
contents are not forwarded. Preserve the original operation and recovery state.

Offline preparation and verification acquire exclusive server/worker leases.
Under those leases, persisted local inference/mail/webhook execution labels are
retained evidence, not proof of an executing process. Migration preserves those
execution-record values while applying schema changes and retains the original snapshot
byte-for-byte. Activation uses normal worker recovery, retaining unknown inference charges
without replaying interrupted model requests. Retained targets use the same
recognized worker-guard compatibility path for migration and prepared-state
verification. Active external compute workers, running/unknown compute jobs,
unresolved non-LLM workflow execution and missing/mismatched inference children
still refuse preparation. No charge or external computation is declared settled
by this path.
Rollback and replacement-recovery receipt validators accept the optional
`work_workers_revoked` fencing count as well as `workflow_runs_held`; unknown
fields, Boolean counts and negative counts remain refused.
CI's separate Release upgrade and recovery gate exercises populated historical
SQLite migrations, host preparation/activation, rollback, replacement recovery
and lost-reply resumption. These checks are separate from first-server setup.

After each confirmation, the terminal immediately prints the bounded work ahead,
keeps an animated status visible while the operation runs, and records either a
verified checkpoint or an explicitly resumable unverified outcome. Preparation
lists SSH trust capture, exact release upload, restart fencing, service stop,
encrypted backup verification, and migration preparation before it begins.

## Recover after losing an operator computer

The first-organization wizard protects deployment access with either an
encrypted offline recovery kit or a second operator. Keep the kit and its
recovery code separately from the original computer. The kit contains an
independent authorized SSH identity, the installed release, public host trust,
and cloud ownership metadata; it contains no provider token or application data.

On a replacement macOS/Linux computer, clone the repository and save a fresh
DigitalOcean token from the deployment's existing Team with Account read and
Firewall read/update access in a private `0600` file. Choose an empty protected
state directory, then run:

```sh
make operator-recover \
  RECOVERY_KIT=/secure/reasoning-operator-recovery.rnrk \
  DO_TOKEN_FILE=/secure/digitalocean-token \
  DEPLOY_STATE="$HOME/.reasoning-deploy"
```

The recovery code is entered through a hidden prompt. Recovery decrypts and
validates the kit, verifies the token's Team identity, and retains the recovery
SSH key inside the new state directory. `make doctor` and `make upgrade` synchronize
the recovered state with the current server even when another operator has
upgraded it since the kit was created. Do not copy the lost computer's setup directory or place
the recovery kit inside the new state directory.

This restores deployment control, not server data. Keep encrypted application
backups and their keys independently. If neither a usable kit and code nor a
second operator survives, the DigitalOcean account alone cannot reconstruct the
pinned operator state through this supported flow.

## Add a deployment operator

Organization roles do not grant infrastructure access. The same person may also
be an application `admin`, but that role is optional and remains separate.
Add the operator to the existing DigitalOcean Team; a token from an unrelated
account cannot manage the Droplet or firewall. The operator creates an Ed25519
key on their own computer. A remote development box owned by the same person
is also a separate operator: it keeps its own key, token, and deployment state.

### 1. On the remote box

Install Git, Make, [uv](https://docs.astral.sh/uv/getting-started/installation/),
Node.js matching `web/package.json`, OpenSSH (`ssh`, `scp`, `ssh-keygen`,
`ssh-add`, `ssh-agent`), and
`dig` (`dnsutils` on Ubuntu). Use a normal user account with public IPv4 egress,
then clone the repository and start the guided onboarding routine:

```sh
git clone https://github.com/tertius-ai/reasoning.git
cd reasoning
make onboard
```

The wizard defaults to generating a dedicated Ed25519 key; `ssh-keygen` asks for
an optional passphrase in your terminal. Selecting an existing protected private
key is also supported. It saves preparation privately under
`~/.reasoning-onboard`, exports `operator.pub`, and prints its standard SSH
fingerprint (`SHA256:…`, as `ssh-keygen -l` shows it). Send only that public key
to the existing operator. Their review shows it as the "Collaborator SSH key
fingerprint"; compare the two through your authenticated connection. The
private key stays on this box. This is infrastructure access, not an
organization invitation/login.

For a passphrase-protected key, load it into an SSH agent in the same terminal
before live verification; remote SSH commands use `BatchMode=yes`. If no agent
is running, start one with `eval "$(ssh-agent -s)"`, then load the generated key:

```sh
ssh-add -t 1h "$HOME/.reasoning-onboard/ssh/id_ed25519"
```

Use the selected private-key path instead if importing a key or overriding the
preparation directory. The local `ssh-keygen` check that derives the public key
asks for the passphrase once per `make onboard` run; the join reuses that
result.

If the handoff is not available yet, leave the archive-path prompt blank. The
wizard pauses without cloud API calls or remote changes and prints the exact
resume command. Rerun `make onboard` after authorization below; key preparation
and the selected state paths are reused. Do not delete either private directory.

Defaults and overrides:

```sh
make onboard DEPLOY_STATE=/private/new-deployment-state \
  ONBOARD_STATE=/private/onboarding-preparation
```

The two directories must be separate, non-nested, and outside Git checkouts.
Deployment state must be new/empty; existing operator state is not adopted or
overwritten. Onboarding requires `uv` and the SSH/DNS tools but neither an existing
release bundle nor a frontend build. A suitable Node.js version is needed for
later builds/upgrades, not for this join.

### 2. On the existing operator computer

Save the received public key in a file, open the existing checkout, and run:

```sh
make operator-authorize
```

The command asks for the public-key file, or accepts the pasted single
`ssh-ed25519 …` line. It reads the file once; the file does not need a
permanent location. `OPERATOR_PUBLIC_KEY=/path/to/key.pub` skips the question.

Before SSH opens or the key is enrolled, the command checks the handoff
destination. By default the handoff is
`DEPLOY_STATE/handoffs/operator-KEY-release-RELEASE.tar.gz` (normally under
`~/.reasoning-deploy/handoffs/`, an owner-only `0700` directory). `KEY` and
`RELEASE` are prefixes of the collaborator key's SHA-256 and the installed
release ID, so a handoff for another person or release never overwrites this
one. Rerunning for the same key and release reuses the existing archive without
authorizing again. `OPERATOR_HANDOFF=/path/to/handoff.tar.gz` chooses another
file; its directory must already exist. A missing directory, a symlink, or a
group- or world-writable directory anywhere in the path stops the command with
the exact path and reason.

The review shows the site, the "Collaborator SSH key fingerprint" in the same
`SHA256:…` format that `make onboard` prints, the temporary SSH address, and the
handoff archive path. Compare the fingerprint with the collaborator, then type
`authorize operator HOSTNAME` (for example `authorize operator chat.example.org`)
to confirm. Authorization opens SSH temporarily and closes it again.

The command then prints the "Handoff archive checksum" (SHA-256) and a
ready-to-send message with the archive name, the checksum, the collaborator's
key fingerprint, and the recipient's steps. Send the archive file privately.
Send the message through a channel where the collaborator recognizes you; the
checksum must reach them authenticated. The archive contains the exact
installed release, cloud ownership metadata, and the server's public host key;
it contains no provider credential, account password, operator private key, or
host private key.

### 3. Join from the remote box

Resume `make onboard`. Supply the received handoff archive's path and the
handoff archive checksum (SHA-256) from the existing operator's authenticated
message. Keep the archive owner-private
(`chmod 600 /path/to/received-handoff.tar.gz`). The wizard checks the digest,
authorized SSH key, installed release and public trust before retaining an exact
private snapshot. No digest computed solely from a downloaded file substitutes
for authenticated confirmation.
The release-bearing handoff archive is bounded to 129 MiB; individual operator
metadata documents remain bounded to 256 KiB.

It then offers to open the DigitalOcean token page and displays these custom
token permissions, grouped in dashboard order. Select the deployment's existing
Team before creating this box's own token:

| Resource | Permissions |
| --- | --- |
| Account | `account:read` |
| Domain | `domain:read` |
| Droplet | `droplet:read` |
| Firewall | `firewall:read`, `firewall:update` |
| Tag | `tag:read` |

All six permissions belong on the same token. `domain:read` allows existing-server
email and compute setup to verify the retained DNS zone and record without
changing DNS. DigitalOcean filters firewall membership fields unless the token
can read the attached resources, and firewall
updates require that read access. Empty membership fields alone do not establish
that a firewall was detached. See the [DigitalOcean firewall API](https://docs.digitalocean.com/reference/api/reference/firewalls/).
Organization onboarding does not require resource-create/delete permissions or
`ssh_key:create`; the existing operator authorizes this computer's key directly.

Enter the token through the wizard's hidden prompt, or provide an existing
protected `0600` token-file path. It is never placed in command arguments,
output, Git, or the handoff. Before any API call, the wizard checks the token's
shape (`dop_v1_` followed by 64 lowercase hexadecimal characters). A token
pasted twice, a refresh token, or a value with spaces is refused without being
saved, and the hidden prompt asks again. The browser question and other visible
prompts refuse an answer that looks like a token without repeating it and ask
again. The terminal displayed that token: revoke it in DigitalOcean and create
a new one; the same run refuses it at the hidden prompt. The token must resolve
to the existing Team and the private key must match the authorized handoff.
Type `onboard HOSTNAME` to confirm joining and temporary SSH access to this
box's current `/32`.

The wizard runs the existing join, live operator synchronization, and diagnostic
checks, and closes temporary inbound SSH. It neither creates an organization/VM
nor stops services, opens the application database, or upgrades the application.
Success is `operator_onboarded` only after SSH/HTTPS verification and SSH closure.
It ends with each verified check:

```text
Deployment access verified: https://chat.example.org

✓ DigitalOcean access
✓ SSH authentication and server identity
✓ Installed release and HTTPS
✓ Temporary SSH access closed

This computer is onboarded. No application upgrade was performed.

Ready to upgrade.
Coordinate with the other operator, then run: make upgrade
Optional later recheck: make doctor
```

Each line reflects evidence from this run: this operator's token opened SSH,
the pinned server answered the diagnosis, every installed-release and HTTPS
check passed, and the firewall confirmed SSH closure. The last line of standard
output is the JSON receipt.
Both directories remain necessary: deployment state references this computer's
key/token preparation. A complete join with a lost receipt is observed and reused,
not repeated. Partial/inconsistent join state stops for reconciliation rather
than being overwritten. A failed synchronization/diagnostic check retains the
joined state so the same `make onboard` can retry verification. Every rerun
rechecks access instead of trusting an old success receipt.

To replace a wrong, expired, or under-scoped token, keep the existing key,
handoff, and both private directories, and run:

```sh
make onboard ONBOARD_REPLACE_TOKEN=1
```

The wizard shows the required scopes, offers the token page, and collects the
replacement through a hidden prompt or protected file. It verifies the same
Team, then requires the site-specific approval before changing token references.
Old token files remain untouched. A completed join is not repeated. A join that
failed before writing any deployment state (the state directory is absent or
holds only its lock), for example at the first DigitalOcean call, is retried
with the replacement; its error message prints this exact command with the
selected directories, and so does a saved token whose shape is invalid. A join
that wrote deployment state must be resumed without replacement first; partial
joins still stop for reconciliation. After verified onboarding, revoke the old
token in DigitalOcean. The direct CLI equivalent is
`reasoning-deploy onboard --replace-token` with the same state-directory overrides.

An independent operator can refresh its local SSH journal from an exactly owned,
closed firewall before opening its own temporary `/32`. This only reconciles
closed SSH; changed membership, unrelated rules, and another operator's open
address remain fail-closed and are not overwritten.

For independently prepared credentials, the lower-level join is also available:

```sh
make operator-join \
  OPERATOR_HANDOFF="$HOME/.config/reasoning/reasoning-operator-handoff.tar.gz" \
  OPERATOR_IDENTITY="$HOME/.ssh/reasoning-deploy" \
  DO_TOKEN_FILE="$HOME/.config/reasoning/digitalocean-token" \
  OPERATOR_HANDOFF_SHA256=DIGEST_FROM_EXISTING_OPERATOR
make doctor
```

Use protected files and the authenticated digest with the lower-level command.
New deployment state defaults to `~/.reasoning-deploy` and must be empty before
joining. Use `DEPLOY_STATE=/new/path` on every Make command if choosing another
directory. Joining retains the installed archive inside the state; a fresh
checkout does not need an existing `build/reasoning-release.tar.gz`.

`make doctor` is an optional later recheck after `make onboard`, and the
verification step after the lower-level join. It opens SSH to this box's
current `/32`, verifies pinned SSH trust, inspects the exact installed release
and public HTTPS, then closes inbound SSH. It never stops the application, opens
its database, or upgrades it. It ends with the same four verified checks and
"Ready to upgrade", followed by the JSON receipt as the last line of standard
output. The command fails, and prints no readiness, if a check or SSH closure is
unverified.

### 4. Upgrade from either computer

After diagnosis passes, coordinate with the other operator and run:

```sh
git pull --ff-only
make upgrade
```

This uses the remote operator's own credentials and retained installed archive.
Do not run `make build` first. Local locks and operation journals do not form a
distributed lock: operators must not upgrade at the same time. Each computer
keeps independent state; `make upgrade`, `make doctor`, `make operator-authorize`,
and `make operator-kit` automatically run `operator-sync` before using it.
Run `make operator-sync` explicitly to refresh without upgrading.

`make upgrade`, `make doctor`, `make operator-sync`, `make operator-authorize`,
and `make operator-kit` prepare local SSH authentication before contacting
DigitalOcean. They reuse an existing agent only when it can sign with the exact
protected private key selected in `setup.json`. An unencrypted key needs no
agent. Otherwise, an interactive terminal prompts for the passphrase through
OpenSSH and starts a private temporary agent for the whole command, including
the upgrade build. The agent is stopped on completion, failure, or cancellation;
the existing keyring and parent shell environment are not modified. No
passphrase is stored, passed as a command argument, or sent to the server.
Temporary keys expire after one hour even if the CLI is abruptly terminated.
If this interrupts an unusually long operation, resume with the same state
and unlock again.

An unattended command with a locked key fails before cloud access. To prepare
an existing agent yourself, use the selected `identity` path in private
`setup.json`:

```sh
eval "$(ssh-agent -s)"
ssh-add -t 1h "$HOME/.reasoning-onboard/ssh/id_ed25519"
make upgrade
```

Enter the passphrase locally, never in chat or a command argument. Verification
errors distinguish authentication, pinned host trust, key format/permissions,
network access, and a remote command failure without printing raw SSH/remote
stderr. Preserve operator state when retrying; never bypass host-key checking.

The same command-scoped wrapper is available directly:

```sh
uv run --locked reasoning-deploy operator-session --state-dir "$HOME/.reasoning-deploy" -- \
  uv run --locked reasoning-deploy operator-sync --state-dir "$HOME/.reasoning-deploy"
```

Source builds check the frontend's exact Node engine range before any upgrade
cloud access. Build and upgrade commands keep a compatible Node on PATH, or
automatically load nvm and select an installed version from `.nvmrc` (Node.js 24)
for the command and its children, including recursive builds and operator sessions.
This does not change the calling shell or nvm's global default. nvm is discovered
via `NVM_DIR`, `$XDG_CONFIG_HOME/nvm`, or `$HOME/.nvm`. Missing versions report a
one-time install command before release retention or cloud access; Node is never
downloaded automatically. Deploying an existing bundle and operator-only commands
do not require Node.
Development Python is selected by `.python-version`. Build and upgrade progress
report elapsed phase times; use [the development loop](development.md) for UI
and backend feedback without deploying each edit. Release builds retain clean
npm installs and hash-verified runtime requirements, while source Python sync
preserves installed development extras.

Synchronization verifies the token's original Team, pinned SSH host key,
original DigitalOcean VM identity, protected installation records, every
installed-release diagnostic check, and public HTTPS readiness. If the release
changed, it downloads the exact archive retained by the server's verified
upgrade history. It preserves this computer's own token/key and previous archive,
and saves before-state and verified receipts under `~/.reasoning-deploy/operator-sync/`.
Each observation is keyed by its source release, target release, and complete
verified receipt hash. Enabling email or updating a verified compute catalog
without changing the release retains a separate observation; it does not replace
earlier evidence. Repeating an identical observation reuses its original record.
It does not stop services or open the application database. It refuses incomplete
installations, missing archive custody, and another operator's active upgrade;
an exact locally owned pending upgrade can resume. A failed check is not
permission to edit release metadata. No surviving copy of the lost computer's
state or newly generated handoff is required. Keep previous state and recovery
records; never copy another operator's setup directory or secrets.

`reasoning-deploy doctor` inspects an exact managed server over verified root
SSH. `upgrade-plan` adds a target-release comparison. Both are read-only: they
do not upload files, create staging directories, install packages, stop/start
services, open SQLite, submit jobs, send mail, or change cloud resources.
They are not upgrade, rollback, or replacement-service recovery commands.

Keep the original installed release archive, its `inspect` receipt, the private
installation/provisioning inputs, and the original verified SSH identity. An
installation journal alone does not provide the manifest needed to verify all
installed files. These commands use current locally installed diagnostic tooling
and can inspect older release bundles without a bundled diagnostic script.

## Diagnose an installation

```sh
reasoning-deploy doctor \
  --bundle /secure/installed-release.tar.gz \
  --confirm-release INSTALLED_RELEASE_ID \
  --public-url https://chat.example.org \
  --ssh-host SERVER_ADDRESS \
  --identity /secure/ssh-identity \
  --known-hosts /secure/known-hosts
```

Use the release manifest digest from `reasoning-deploy inspect`, not the archive
SHA-256 or Git revision. The archive is snapshotted and verified locally before
network access. SSH ignores user/global configuration, proxies and alternate
trust mechanisms; the supplied known-hosts file must contain the independently
verified host key. Host keys are not learned from the connection.

The bounded JSON report separates checks of the private installation journal,
bootstrap lock, protected release payloads, installed first-party Python sources,
exact server/proxy units, Caddy configuration, loaded systemd identity, service
activity, protected catalog bytes, private server state, managed mail binding,
original DigitalOcean VM identity, and public HTTPS health. Unknown/unavailable
evidence is not success. Failed checks still return a report; an invalid input,
transport failure, oversized response, or incorrectly bound receipt fails the
command. No arbitrary service output, logs, state contents, email addresses,
provider keys, or mail credentials are returned.

Reports are point-in-time diagnostics, not durable permission to mutate a host.
The bootstrap lock is observed without creating or modifying it; a busy lock is
a failed check. Independent checks can observe a changing host. Every mutating
operation must acquire exclusive ownership and revalidate its inputs/state.

New bootstrap bundles declare `server-release-identity-v1`. Their exact managed
unit passes the manifest digest using `reasoning-server serve
--deployment-release`; `/api/health` returns it in `X-Reasoning-Release`, which
every server response carries, and as `release_id`.
Readiness rejects a missing, duplicate, or different header. This is an
operator-configured identity assertion, not a publisher signature, process
attestation, or proof that an arbitrary HTTPS endpoint runs the selected code.
Older bundles use the exact unit without this argument; their HTTPS health can
pass while `https_release_identity` remains `unverified`.

Python source checks compare package bytes with the selected installed wheel
and reject unexpected source/native modules and links. They do not attest the
interpreter, dependencies, entry-point wrappers, running process, or bytecode.
Catalog checks hash protected JSON-list bytes; they do not approve image digests
or verify the application's complete image/resource policy. Private-state
checks inspect ownership, modes and file types, not SQLite contents or integrity.
The expected schema comes from the archive manifest. No database is opened,
including SQLite's WAL/shared-memory files: a read-only WAL connection can still
need to create auxiliary files ([SQLite documentation](https://www.sqlite.org/wal.html#read_only_databases)).

An optional mail file configured outside the managed `--mail-file` installation
flow cannot be adopted into its managed binding by diagnosis. A missing/changed
mail journal is reported as a failure; recreate no credentials to bypass it.
An owned DigitalOcean installation requires its original provisioning journal,
cloud-init marker and live VM metadata ID. Hardware with neither journal nor
marker reports that check as `not_applicable`; no provider API token is needed.

## Compare a proposed release

```sh
reasoning-deploy upgrade-plan \
  --bundle /secure/installed-release.tar.gz \
  --confirm-release INSTALLED_RELEASE_ID \
  --target-bundle /secure/target-release.tar.gz \
  --confirm-target-release TARGET_RELEASE_ID \
  --public-url https://chat.example.org \
  --ssh-host SERVER_ADDRESS \
  --identity /secure/ssh-identity \
  --known-hosts /secure/known-hosts \
  --output /secure/upgrade-plan.json
```

Both archives are snapshotted and verified before networking. The plan binds the
source/target release IDs, archive SHA-256 values, source revisions, origin,
expected schemas, and observed catalog/mail digests. Its `plan_id` identifies
those inputs, not a completed or authorized upgrade. Dirty releases, an unchanged
target, a target without HTTPS release identity, schema downgrades, and failed or
unverified source checks are refusals. The older source's missing release header
is explicitly tolerated for planning; other checks are not waived.

`preflight_checks_passed` means only these checks passed. The report always says
`upgrade_performed: false` and lists required work: physical worker drain/fencing,
encrypted backup using the matching release, offline database/migration checks,
runtime verification, journaled activation/recovery, post-activation readiness,
and an explicit rollback/data-restore policy. No SQL migration compatibility or
safe downgrade is inferred from a schema number alone.

`--output` exclusively creates a private 0600 canonical plan document; it does
not overwrite an existing plan or change the remote host. Keep this original
document through preparation, activation, and any rollback. A refusal report can
be saved for inspection but cannot authorize a release change.

## Verified-SSH release changes

`upgrade-prepare`, `upgrade-activate`, `rollback-prepare`, and `rollback-activate`
call the root-owned backend below on dedicated Ubuntu 24.04 amd64 hosts. They
are limited to disposable-host development rehearsals until actual root/systemd/
HTTPS operation and recovery have been verified. They do not provision hardware,
automatically fence remote workers/controllers, reverse external effects, or
activate a replacement-hardware recovery.

Choose one fresh random 64-hex operation ID (for example, using
`openssl rand -hex 32`) and one new operator state directory for the upgrade.
Reuse both, the saved plan, verified SSH host, key and known-hosts contents for
every retry and subsequent activation/rollback. Physically stop/fence execution
workers, controllers, unmanaged servers and outbound execution before confirming
preparation. Keep off-host backups and recovery keys independently of the host.

```sh
reasoning-deploy upgrade-prepare \
  --plan /secure/upgrade-plan.json --confirm-plan PLAN_ID \
  --operation-id OPERATION_ID --state-dir /secure/upgrade-operation \
  --source-bundle /secure/installed-release.tar.gz \
  --target-bundle /secure/target-release.tar.gz \
  --ssh-host SERVER_ADDRESS --identity /secure/ssh-identity \
  --known-hosts /secure/known-hosts --confirm-workers-fenced

reasoning-deploy upgrade-activate \
  --plan /secure/upgrade-plan.json --confirm-plan PLAN_ID \
  --operation-id OPERATION_ID --state-dir /secure/upgrade-operation \
  --ssh-host SERVER_ADDRESS --identity /secure/ssh-identity \
  --known-hosts /secure/known-hosts --confirm-activation
```

Preparation snapshots both archives and SSH trust inputs before network access.
It records original operation/plan/host/trust/backup-allowance bindings in the
private operator directory before requesting host changes. The default backup
allowance is 8 GiB; optional `--maximum-bytes` accepts 1 MiB through 64 GiB only
during upgrade preparation. Retries retain the original allowance. The directory
is exclusively locked for each operator invocation; exact bounded verified
receipts are retained without replacing differently bound evidence.

One strict-host-key SSH connection runs trusted local stdlib tooling in isolated
Python without importing the deployed application's environment or writing
executable tooling to the host. Preparation streams the bounded verified archives
into a unique root-private staging directory. Both archive hashes, release
bindings and schemas must match the saved plan before backend invocation. Normal
endpoint exit removes its own temporary uploads; interrupted processes can leave
private staging files, not a new authorization or active-operation identity.
The backend retains its own original archives and recovery evidence. Activation
and rollback upload no replacement archives, use retained root inputs, and
require the host's original plan identity to match the saved operator plan.

If rollback is required, choose one fresh 64-hex rollback ID. Preparation
explicitly acknowledges losing newer target data from the live installation;
the displaced target evidence is retained privately, not merged into the source.
Source activation requires a separate acknowledgment of potential external effects.

```sh
reasoning-deploy rollback-prepare \
  --plan /secure/upgrade-plan.json --confirm-plan PLAN_ID \
  --operation-id OPERATION_ID --rollback-id ROLLBACK_ID \
  --state-dir /secure/upgrade-operation \
  --ssh-host SERVER_ADDRESS --identity /secure/ssh-identity \
  --known-hosts /secure/known-hosts \
  --confirm-workers-fenced --confirm-newer-data-loss

reasoning-deploy rollback-activate \
  --plan /secure/upgrade-plan.json --confirm-plan PLAN_ID \
  --operation-id OPERATION_ID --rollback-id ROLLBACK_ID \
  --state-dir /secure/upgrade-operation \
  --ssh-host SERVER_ADDRESS --identity /secure/ssh-identity \
  --known-hosts /secure/known-hosts --confirm-activation
```

An SSH error, deadline, incomplete or invalid receipt means **host changes may
have occurred**. Killing/reaping the local SSH process does not establish that
the remote backend stopped. Retain the original plan, operation directory and
IDs; retry the same phase and confirmations. A busy host bootstrap lease refuses
concurrent changes. Do not make a new plan against a stopped/prepared service,
change IDs to bypass ownership, replay preparation after activation, delete a
fence, or restore a snapshot to interpret a transport error as failure. The root
journals determine what can safely resume. Changed host trust inputs require
operator investigation, not an automatic trust reset. The bounded transport
deadline is two hours; arbitrary stderr/logs are not forwarded as receipts.

## Root-owned host preparation backend

The `reasoning_ledger.deployment.server_upgrade` backend and verified bundle's
`server-upgrade.py` implement preparation and separately confirmed activation,
for development rehearsals on disposable dedicated Ubuntu 24.04 amd64 hosts.
Preparation leaves the source stopped and persistently restart-fenced;
activation switches installation bindings and verifies the running target.
Rollback preparation and separately confirmed source activation are described
below. Subsequent upgrades explicitly hand restored-source provenance to the
authenticated migration. The verified-SSH commands above retain operator identities
and invoke these same guarded transactions.
Do not use this backend for production upgrades before recovery and real-host
rehearsals, or delete its fence to force an unjournaled start.

The backend requires root, the exact confirmed `upgrade-plan` identity, original
source and target bundle hashes, a fresh 64-hex operation ID, protected private
0600 input archives, and explicit confirmation that compute workers, controllers,
unmanaged servers and outbound execution have been physically stopped or fenced.
It does not drain remote workers or settle uncertain provider execution itself.
The source schema must be at least 13, and the target must declare
`server-maintenance-v1` and `server-upgrade-preparation-v1`. Dirty releases,
unsupported schemas/features, failed source preflight, changed configuration,
or an unowned existing target release are refused. Its command options can be
inspected without changing a host:

```sh
python3 -I -S -B /verified/target/server-upgrade.py --help
```

Preparation retains the original bootstrap lock exclusively and revalidates
source diagnosis under that owned descriptor. It binds the operation through
private `active-server-upgrade.json` and
`server-upgrades/OPERATION_ID/operation.json` in `/var/lib/reasoning-deploy`.
Bootstrap refuses the active upgrade ownership record; preparation does not
adopt or overwrite a different operation. Source/target bundles, original
installation/mail/provisioning metadata, and configured mail credentials are
retained in the private operation directory. Source and target payloads and
installed first-party Python bytes are checked on resumption. Mail digests,
compute catalog, and original cloud VM marker/metadata are revalidated.

The target is extracted into its distinct root-owned release directory and a
venv is installed with hash-locked dependencies and the selected wheel, without
replacing source code or starting the target. Before backup or SQL migration,
the backend retains `server-upgrade-fence` and the exact managed systemd drop-in
`90-reasoning-upgrade.conf`, whose `ConditionPathExists` prevents server startup
while that fence exists. It reloads systemd, stops the source, and checks exact
loaded identity/drop-ins, no pending reload, inactive/dead state, and zero main
PID. A successful stop response alone is not sufficient. The persistent condition
also applies to source executables without the application's maintenance guard.

The source's matching CLI creates and authenticates the encrypted snapshot under
an escrowed random key. Protected root-private copies are retained and hashed
before invoking the target's offline migration. The target's journal must match
the original operation, archive/key digests, schemas, release and byte allowance.
Prepared-state verification requires no backup-key access and never starts the
application. Service-owned scratch credentials/plaintext directories are sealed
behind root-owned 0700 permissions before `prepared` is recorded; missing scratch
after reboot is acceptable, but existing insecure/unsealed scratch is refused.
The vault is host-local, not an off-host backup or secure-erasure mechanism.
Retain separate off-host archive/key copies and use encrypted disks.

Durable phases are `preparing`, `target_ready`, `fencing`, `source_stopped`,
`backup_retained`, `migrating`, `sealing`, and `prepared`. Resume the same operation,
plan, releases and allowance. Lost backup/migration responses recover the original
verified outputs rather than taking a replacement snapshot or restoring old data.
Command failures retain ownership, backups and the restart fence; they do not
restart the source. The receipt says `activation_performed: false`,
`upgrade_completed: false`, and `runtime_dependency_attestation: false`.
Package/interpreter/entry-point/bytecode attestation and actual systemd/cloud
behavior are not established by local fixtures or first-party source comparison.

### Activate the retained prepared target

Activation requires the original operation and plan, explicit target release
confirmation, and acknowledgment that starting the target can produce external
effects. It uses the root-private retained source/target archives rather than
accepting replacement inputs. The target must declare `server-maintenance-v1`,
`server-release-identity-v1`, and `server-upgrade-activation-v1`. The original
backup/key, sealed scratch, payloads, first-party installed sources, configuration,
mail and cloud identity are rechecked under the original exclusive bootstrap
lease. Operator confirmation does not attest interpreter/dependency/bytecode
integrity or fence external execution; those remain independent requirements.

On a disposable prepared host, the selected verified script accepts:

```sh
python3 -I -S -B /verified/target/server-upgrade.py \
  --activate --operation-id "$OPERATION_ID" --confirm-plan "$PLAN_ID" \
  --confirm-target-release "$TARGET_RELEASE_ID" --confirm-activation
```

The `switching` phase verifies exact prepared application state with the source
still stopped/fenced. It atomically transitions only the known original or target
installation/mail/provisioning bindings and managed server unit. Mail credentials,
cloud resource ownership, proxy configuration and the compute catalog are not
replaced. Mixed writes after interruption are recognized only within their
original phase; arbitrary changes are refused, not adopted.

The `activating` phase is durable **before** removing the original restart
sentinel, because its removal permits startup after reboot. The target unit binds
the original maintenance operation and target release. `systemctl start` is used,
not restart; the application records `activation_started` before initialization
and workers. A lost response can resume without another snapshot or migration.
After this boundary, preparation refuses replay: live messages, account changes
and external effects may exist. Failure leaves the target potentially running;
it never implicitly restores or restarts the source.

Readiness checks require the exact loaded managed unit/account/drop-ins, no
pending reload, an active/running process with positive main PID, public HTTPS
with the exact target release header, and the mail worker when configured.
`reasoning-server maintenance-observe` verifies the original activated journal,
exact target SQL schema/integrity and completed history when present. It shares
the running server's maintenance lease; it does not start a server, advance the
journal, compare live data to the old snapshot, verify backups, or check HTTPS.
The release header is the configured operator identity, not executable attestation.

After fresh readiness, `ready` acknowledges application completion and marks the
target installation started. `finalizing` installs the ordinary target unit,
removes only the original managed restart-condition drop-in, reloads systemd,
rechecks completed application history, and runs fresh full host diagnosis and
HTTPS readiness. It publishes immutable `completion.json` in the original
operation directory before marking `completed` and removing the exact active
ownership pointer. Original recovery material and application history remain.
The receipt reports host upgrade completion, schema/release readiness and retained
root-private recovery material, with runtime dependency attestation explicitly
false. It is not proof of independent-release compatibility or actual cloud
operation from fixture tests.

Completed retries perform fresh diagnosis and application/schema checks without
starting, stopping, migrating or resnapshotting. Missing or changed completed
root/application history is refused, never recreated. Resume the original IDs;
do not edit journals or use bootstrap to bypass pending ownership. Explicit
rollback preparation retains displaced state; separately confirmed source
activation is described below. Off-host backups and physical execution fencing
remain operator responsibilities.

## Root-owned rollback preparation backend

`HostUpgrade.rollback_prepare` and the verified standalone script's
`--rollback-prepare` mode prepare an explicit point-in-time source restoration on
disposable dedicated Ubuntu 24.04 amd64 hosts. They do not activate the source,
switch managed bindings, complete rollback or undo external effects. Source
activation is a separate operation below. Verified-SSH operator commands invoke
these transactions; production verification remains unfinished. Do not remove
fences or edit journals to force either installation to start.

Preparation requires the original upgrade operation/plan/source/target IDs, a
fresh rollback ID, physically fenced workers/controllers/unmanaged servers and
outbound execution, and **explicit acknowledgment that newer data and security
changes are not in the restored snapshot**. The original source must declare
`server-rollback-state-v1`; its matching CLI must verify/restore the exact source
schema. Root/first-party code comparison is not interpreter, dependency,
entry-point or bytecode attestation. The original encrypted archive/key and
installation/configuration/VM evidence must remain intact. Failed target health
never authorizes automatic restoration.

The development backend accepts:

```sh
python3 -I -S -B /verified/target/server-upgrade.py \
  --rollback-prepare --operation-id "$OPERATION_ID" --rollback-id "$ROLLBACK_ID" \
  --confirm-plan "$PLAN_ID" --confirm-source-release "$SOURCE_RELEASE_ID" \
  --confirm-target-release "$TARGET_RELEASE_ID" \
  --confirm-workers-fenced --confirm-newer-data-loss
```

Private `server-upgrades/OPERATION_ID/rollback.json` binds the unchanged original
upgrade journal and retained binding/unit before-images. Phases are `fencing`,
`target_stopped`, `restoring`, `sealing`, `restored`, `displacing`, `prepared`.
The original restart sentinel/condition and active ownership pointer are retained
or established before stopping the target. Exact loaded identity, inactive/dead
state and zero PID are checked; a stop reply alone is insufficient. Target/source
upgrade replay is refused once the rollback record exists.

The matching source helper authenticates the original encrypted archive and
restores **new state only** under `/var/lib/reasoning-rollback-ROLLBACK_ID/state`.
Its source provenance/startup fence, private maintenance lease and prepared-state
identity are synced before atomic no-replace publication. Configured original
mail credentials are bound before this fingerprint. Lost replies reuse the exact
restoration rather than rotating its setup credential or taking another snapshot.
Restore applies the [backup recovery fencing](backup.md): old sessions/invitations,
workers, bots, subscriptions and scaler credentials are revoked; queued/in-flight
execution is held and workflows are restore-fenced. This does not revoke provider
credentials outside the host, settle provider billing or undo completed effects.
Review restored memberships, keys, policy and external accounting before exposure.

Temporary service access to archive/key/mail copies is sealed behind a root-owned
0700 scratch directory under `/run` after restoration. Missing scratch after
reboot is acceptable; the source candidate/provenance and root recovery vault are
durable under `/var/lib`. Private plaintext scratch can remain after interruption;
this is not off-host retention or secure erasure.

Before displacement the backend holds the original maintenance/initializer/local
execution leases exclusively. A root-owned application block is retained before
inventorying the stopped target. Every regular file, including SQLite/WAL,
secrets, artifacts and maintenance history, is bound by a private filesystem
inventory. Symlinks, hardlinks, non-regular entries, changed reads, excessive
depth/count/bytes or another operation are refused. The bounded inventory allows
262,247 entries, paths up to 1,024 bytes/32 components and at most 130 GiB of file
bytes. This is **raw evidence retention, not target SQL validation**: a corrupt
target database can be preserved without trusting its schema or executable health.

Same-filesystem Linux atomic no-replace rename retains the entire target directory
as root-private `displaced-state` in the original operation directory. The canonical
state path becomes a root-owned, blocked placeholder, not an unguarded empty
installation. Interrupted rename/sealing/placeholder publication can resume only
against the original inventory and ownership. The prepared source remains private
and application-fenced; no binding/unit/source startup change occurs. Success says
`source_activation_performed: false`, `rollback_completed: false`,
`displaced_database_validated: false`, and retains both the original backup and
displaced state. Never delete either to bypass an error. Exact prepared retries
require original provenance, placeholder, before-images and inventory; missing or
changed records are refused rather than recreated.

## Activate the prepared rollback source

`HostUpgrade.rollback_activate` and standalone `--rollback-activate` activate only
the original fully prepared restoration. The source must declare
`server-rollback-state-v1` and `server-release-identity-v1`; the retained target
must declare `server-rollback-activation-v1`. The exact original operation, plan,
source, target and rollback IDs and a separate external-effect confirmation are
required. Preparation inputs, replacement snapshots/byte allowances and new
credentials are not accepted. Physical execution fencing and the preparation's
newer-data-loss acknowledgment remain prerequisites; activation does not reverse
provider effects or restore newer security changes.

```sh
python3 -I -S -B /verified/target/server-upgrade.py \
  --rollback-activate --operation-id "$OPERATION_ID" --rollback-id "$ROLLBACK_ID" \
  --confirm-plan "$PLAN_ID" --confirm-source-release "$SOURCE_RELEASE_ID" \
  --confirm-target-release "$TARGET_RELEASE_ID" --confirm-activation
```

Phases are `publishing`, `switching`, `activating`, `ready`, `finalizing`,
`completed`. Every retry requires unchanged original root backup/key, release
archives, first-party files, before-images, configuration, VM ownership and
displaced-state inventory. Rollback preparation cannot replay once publication
intent is retained. The original upgrade journal/completion remain unchanged.

Publication verifies the exact fenced source and blocked canonical placeholder,
then uses same-filesystem Linux atomic directory exchange under the source's
exclusive maintenance/initializer/local-worker leases. The canonical data path
never becomes an unguarded absent directory. Root takes ownership of the stage
parent with traverse-only 0711 access before exchange, allowing the matching
service CLI to inspect its private source child without replacing that child.
After exchange the stage contains the original root-owned placeholder and is
sealed 0700. Recovery recognizes both exact locations and never swaps back or
overwrites unowned live state.

Switching accepts only known before/source binding and unit bytes. Original mail
and cloud ownership records bind the source without new credentials/resources;
the source uses its ordinary exact-release unit, not the target migration unit.
`activating` is durable before releasing the application's prepared-state guard
or removing the persistent restart sentinel. Source release is replayable while
the sentinel and stopped-service proof remain. Once the sentinel is absent,
recovery uses shared-lease source observation, never another exclusive release or
old-state fingerprint check. Start is idempotent, not restart; live writes after
startup are preserved even when acknowledgments or HTTPS checks fail.

Readiness requires exact loaded managed identity, active/running state, positive
main PID, no pending reload, public HTTPS with the source release header,
configured mail-worker readiness and the exact released source journal/schema/
integrity. The release header is operator-configured identity, not executable
attestation. Finalization records the started source binding, removes only the
original restart condition, reloads systemd and requires fresh full host diagnosis.
Immutable root `rollback-completion.json` precedes journal completion and exact
active-pointer removal. Completed retries verify fresh readiness/provenance and
retained evidence without starting, stopping, restoring or resnapshotting; missing
or changed completed history is refused, never reconstructed. The application's
source provenance stays `released`; the root record establishes managed completion.

Success reports `rollback_completed: true`, `source_activation_performed: true`,
`https_release_verified: true`, `database_schema_verified: true` and retained
backup/displaced evidence. It still reports `displaced_database_validated: false`
and `runtime_dependency_attestation: false`. This backend is limited to disposable
host development rehearsals, not proof of real root UID separation, systemd/TLS,
cloud operation or independent released-version compatibility. Subsequent managed
upgrades require the provenance handoff described below. Do not delete provenance
to force a later release to start. Verified-SSH release changes and replacement-host
recovery have separate operator commands described in this document.

## Upgrade a restored installation

A completed rollback's source can be upgraded through the same separately
confirmed preparation/activation backend. The target must declare
`server-rollback-upgrade-v1`; the backend refuses an unsupported target before
stopping the source. Original protected upgrade/rollback journals and root rollback
completion must remain intact, and current source provenance must match that
protected completion. This also applies when later upgrades retain an already
superseded source record. An ordinary installation without rollback provenance
does not require this additional target feature; source rollback activation is
separate from later-upgrade capability.

Use a fresh stopped-state backup of the **current** restored installation, a fresh
upgrade operation and a different target release. The original pre-rollback
snapshot does not authorize replacing subsequent live data. The selected target's
host-local `migrate-offline` performs the same handoff under exclusive maintenance/
initializer/local-worker leases. It authenticates its matching source snapshot,
compares current source state and completes a dry run before retaining migration
intent; prepared restorations, root blocks and captured targets are ineligible.
Workers and uncertain external execution must still be drained/settled/fenced.

Before SQL changes, the target retains `migrating` intent, copies the exact released
source record to private `rollback-history/ROLLBACK_ID.json`, then replaces the
active `server-rollback.json` with `phase: superseded`. Its `superseded_by` contains
the exact phase-independent migration identity: operation, source/target schemas,
target release, authenticated backup/key digests, allowance and source/target
state fingerprints, with format version 1. Original rollback identity/recovery
fields remain unchanged; the released history is immutable, service-owned 0600
inside a private 0700 directory. The normal upgrade does not rotate restored
credentials, revoke new sessions or clear held work's recovery fences.

Lost history-directory/publication/provenance/SQL acknowledgments can resume only
the original intent. Before the superseded marker is published, retry requires
the exact unchanged source schema/fingerprint; it cannot adopt already migrated
or changed SQL. Once superseded, missing or changed released history is refused,
never reconstructed. Partial startup and ordinary initialization remain fenced
by the migration journal. Same-schema upgrades still require the new exact release;
an old source release is not authorized just because its SQL version matches.

Serving and prepared/live/completion helpers verify original released history and
the successor's exact journal identity. They enforce the current executable's
schema and the target release; implicit migrations remain forbidden. After target
activation, live state is not compared to or replaced by the old fingerprint.
Original `rollback-release`/`rollback-observe` cannot reopen a superseded source.
For additional upgrades, the first handoff remains unchanged; its exact completed
maintenance history and the immediately preceding completed journal/history must
remain valid. Deleted provenance with retained rollback history also blocks
initialization. Root upgrade/rollback completion and displaced-state evidence
remain retained rather than rewritten by the handoff.

## Host-local rollback state primitives

`reasoning-server rollback-capture` verifies/fences an exact-schema stopped target
under its maintenance and local worker leases; it is optional stricter logical
capture, not the root backend's corrupt-state filesystem-retention path.
`rollback-restore` authenticates a matching source backup and creates the fenced
new source state with durable provenance. Both require original operation/rollback/
plan/release/archive identities and literal stopped/newer-data-loss acknowledgments.
Restoration accepts optional original private mail input plus its digest.

`rollback-verify --rollback-id ID --source-release RELEASE` checks exact prepared
state without archive/key access or startup. `rollback-release` requires the same
IDs and `--confirm-activation`, verifies prepared state, then records `released`
before permitting ordinary source initialization. It does not start services,
switch directories/bindings, verify health or complete managed rollback. Treat
release as an external-effect boundary: subsequent live changes must not be
compared to or replaced by the old fingerprint. Ordinary `serve` requires the
exact source `--deployment-release`; another target schema cannot implicitly
migrate the restoration. `rollback-observe` shares the running source's lease and
verifies its exact released journal/schema/integrity without advancing state or
claiming HTTPS health. These primitives alone are not the root-managed source
publication/activation and completion transaction described above.

## Verified-SSH replacement-host recovery

`recovery-plan`, `recovery-prepare`, and `recovery-activate` expose the root backend
below through trusted local tooling and verified SSH. They are limited to
disposable-host development rehearsals until real root UID separation, systemd,
HTTPS and replacement recovery acceptance have been verified. These commands do
not provision hardware, move DNS, physically fence the source or remote workers,
reconcile uncertain costs, or establish the complete multi-user release gate.

Retain the original clean matching release archive, encrypted backup and its
verified SHA-256, separate original 32-byte backup key, and optional mail input.
Backup/key/mail inputs must be operator-owned private regular files without links
and with protected nonsymlink parents. Use a protected directory for the immutable
plan and a separate private operator operation directory. Reserve temporary space
for local input snapshots, including another copy of the encrypted backup; normal
cleanup is not secure erasure. The server separately retains protected recovery
inputs and restored state. Keep source/off-host recovery copies until the real
organization journey succeeds.

Create the original plan without contacting a host:

```sh
reasoning-deploy recovery-plan \
  --bundle /secure/matching-release.tar.gz --confirm-release RELEASE_MANIFEST_ID \
  --backup /secure/source.rnbk --expected-backup-sha256 BACKUP_SHA256 \
  --key-file /secure/backup.key --public-url https://chat.example.org \
  --output /secure/recovery-plan.json
```

Planning verifies the release archive, clean source identity, preparation/activation
support, original encrypted archive checksum and exact private key length. It
binds the release/URL/schema, archive/key digests and byte allowance; it does not
decrypt/authenticate the archive or prove physical fencing. Its
`archive_authenticated: false` and `host_changed: false` are explicit boundaries.
`--output` exclusively creates a private canonical plan and never overwrites it.
Use its `input_sha256` as the confirmation, not the release ID or backup checksum.
The default allowance is 8 GiB; `--maximum-bytes` accepts 1 MiB through 64 GiB only
during planning. A changed allowance requires a new plan, not a retry of a pending
operation. A plan is an identity, never a substitute for fresh root-host preflight.

Optional `--mail-file /secure/mail.json` binds the private replacement sending
configuration. It must be supplied again with the same bytes during preparation,
not added during activation. `--provisioning-file /secure/replacement-vm.json`
binds the original canonical four-field VM request (`format`, `plan_id`,
`deployment_id`, `droplet_id`) for an owned replacement DigitalOcean host. Do not
use the source VM's request or a provider token. Root preparation/activation
checks the replacement's original marker and live metadata; the plan does not
contact the provider or create a VM.

Choose a fresh random 64-lowercase-hex recovery ID with `openssl rand -hex 32`.
Retain the ID, exact plan, private operation directory, host name and original SSH
key/known-hosts contents for every retry. Physically stop/fence the source server,
remote execution workers/controllers and outbound execution, including automatic
reboot activation, before confirming preparation:

```sh
reasoning-deploy recovery-prepare \
  --plan /secure/recovery-plan.json --confirm-recovery INPUT_SHA256 \
  --recovery-id RECOVERY_ID --state-dir /secure/recovery-operation \
  --ssh-host REPLACEMENT_ADDRESS --identity /secure/ssh-identity \
  --known-hosts /secure/known-hosts \
  --bundle /secure/matching-release.tar.gz --backup /secure/source.rnbk \
  --key-file /secure/backup.key --confirm-execution-fenced
```

Add the original `--mail-file` if it is bound in the plan. Preparation snapshots
all input/SSH-trust bytes before networking and persists the local original
recovery/plan/host/trust intent before requesting changes. One strict-host-key
SSH session streams bounded trusted standard-library tool sources into isolated
in-memory modules and release/backup/key/mail data into unique root-private
staging. Uploaded executables and the installed application's environment are
not imported by the transport. Bounded lengths and exact digests, matching release,
schema, mail and VM bindings are verified before the root preparation backend.
On success the server is stopped/fenced and restored candidate state remains
unpublished; the private local `recovery-prepare.json` retains the exact receipt.

After reviewing restored permissions/provider keys and arranging the replacement
URL/DNS, separately confirm activation with no archive/key/mail uploads:

```sh
reasoning-deploy recovery-activate \
  --plan /secure/recovery-plan.json --confirm-recovery INPUT_SHA256 \
  --recovery-id RECOVERY_ID --state-dir /secure/recovery-operation \
  --ssh-host REPLACEMENT_ADDRESS --identity /secure/ssh-identity \
  --known-hosts /secure/known-hosts --confirm-activation
```

Activation compares the retained root intent with the saved operator plan and
invokes the original guarded publication/activation transaction. A valid exact
completion receipt is retained in `recovery-activate.json`. Local receipts do not
authorize rebuilding missing root/application history or replacing inputs. Missing,
malformed, duplicate-key, oversized, secret-bearing or differently bound remote
output is unverified: host changes may already have happened. Preserve all original
inputs and resume the same operation. A lost reply after restore or activation
does not require another restore or a new recovery ID. After activation, never
replay preparation or restore a snapshot over live data.

Transport bounds the SSH session to two hours and suppresses arbitrary stderr.
Failure kills/reaps its owned local SSH process group but does not claim that a
remote backend, source process, container or provider request has stopped. Normal
exits clean unique temporary uploads; abrupt interruption can leave protected
staging. Root bootstrap/application leases, original phase journals and immutable
completion evidence establish safe recovery, not a successful SSH exit alone.

## Replacement-host recovery backend

The bundled `server-recovery.py` runs from trusted verified tooling alongside
`installer.py`, `server-operations.py`, and `server-upgrade.py`. Its read-only
`--plan` mode binds a clean matching release, replacement HTTPS origin, original
encrypted archive/key digests, payload allowance, and optional private mail/owned-VM
inputs. Planning does not authenticate the backup, contact a host, create
resources, or establish physical fencing. Preparation and activation are separate
root-owned transactions on fresh dedicated Ubuntu 24.04 amd64 hardware. The
verified-SSH operator commands above call this backend; actual root/systemd/HTTPS
recovery acceptance remains unverified. Use only disposable development rehearsals
until those gates are verified.

Keep trusted tooling and all inputs in protected directories. The supplied release,
encrypted archive and 32-byte backup key must be root-owned private regular files
without links for mutation. Preserve off-host backup/key copies separately. The
release declares `server-maintenance-v1`, `server-release-identity-v1`, and
`server-recovery-preparation-v1`; activation also requires
`server-recovery-activation-v1`. Recovery requires the backup's exact schema and
matching built-in migrations, not a downgrade or upgrade inferred from a version.

First inspect the release and plan the exact inputs:

```sh
python3 -I -S -B /verified/release/server-recovery.py --plan \
  --bundle /secure/release.tar.gz --expected-sha256 BUNDLE_SHA256 \
  --public-url https://chat.example.org \
  --backup-sha256 BACKUP_SHA256 --key-sha256 KEY_SHA256
```

Use its `input_sha256` as the confirmation below, not the release ID or archive
digest. Choose a fresh random 64-lowercase-hex recovery ID, for example with
`openssl rand -hex 32`, and retain it for every retry. Before preparation,
physically stop/fence the original server, compute workers, fleet controllers,
and outbound execution, including automatic reboot activation. Revoked restored
credentials and expired leases cannot stop containers or provider requests that
already started on the source. The backend records this acknowledgment but never
claims to have established physical source fencing.

```sh
sudo python3 -I -S -B /verified/release/server-recovery.py \
  --bundle /secure/release.tar.gz --expected-sha256 BUNDLE_SHA256 \
  --public-url https://chat.example.org \
  --backup /secure/source.rnbk --backup-sha256 BACKUP_SHA256 \
  --key-file /secure/backup.key --key-sha256 KEY_SHA256 \
  --recovery-id RECOVERY_ID --confirm-recovery INPUT_SHA256 \
  --confirm-execution-fenced
```

Both plan and preparation accept paired `--mail-input FILE` /
`--expected-mail-sha256 DIGEST`, and `--provisioning FILE` /
`--expected-provisioning-sha256 DIGEST`. Mail is not in the application backup:
use the exact private sending input for the replacement and retain it. An owned
DigitalOcean replacement needs its original canonical VM request, cloud-init marker
and live metadata ID; source-server VM ownership cannot authorize a different
replacement. A present ownership marker without that request is a refusal.
The default payload allowance is 8 GiB; `--maximum-bytes` accepts 1 MiB through
64 GiB and is bound to planning/preparation. Reserve space for retained archives,
decrypted validation copies, restored data and the installed release.

Preparation refuses existing unmanaged state, service accounts, configuration
and unowned staging. It records original ownership before installing packages,
retains `release.tar.gz`, `source.rnbk`, `backup.key`, and optional `mail.json` under
root-private `/var/lib/reasoning-deploy/server-recoveries/RECOVERY_ID`, and installs
a persistent restart fence. It stages the matching release without initializing
an empty database or enabling/starting the organization server/proxy. Actual
restore authenticates the archive, preserves accounts, keys and artifacts, revokes
credentials, and holds uncertain jobs as described in [backup recovery](backup.md).
Private candidate state and separate runtime input copies are sealed under root
ownership. Exact restored provenance, logical fingerprint, schema, local leases,
installed payloads/configuration and original VM identity are reverified. No SQL
migration or data publication to `/var/lib/reasoning` occurs during preparation.

Activate only after reviewing restored permissions/provider keys, establishing
source fencing, and arranging the replacement's DNS and URL:

```sh
sudo python3 -I -S -B /verified/release/server-recovery.py --activate \
  --recovery-id RECOVERY_ID --confirm-recovery INPUT_SHA256 \
  --target-release RELEASE_MANIFEST_ID --confirm-activation
```

Activation accepts no replacement archives, credentials or byte allowance. Under
the original exclusive bootstrap lease it checks retained inputs and exact
prepared state, then journals publication before atomic same-filesystem,
no-replace rename into the absent `/var/lib/reasoning`. A lost rename reply can
recognize the exact published state; existing or ambiguous data is never
overwritten. The operation-bound unit and persistent restart fence remain in
place until the durable external-effect boundary. Only then can the backend
enable/start the server and proxy. It verifies loaded server identity, process
state, HTTPS release health, required mail-worker health, and matching application
activation/schema observation before acknowledging completion.

Application maintenance/recovery provenance and root completion history are
retained immutably. The final ordinary managed unit and fresh installed-host
diagnosis are verified before pending root ownership is released. Completed
retries require original history and fresh live checks; they do not restore,
republish, enable, or restart the service. An interruption after activation keeps
new live writes and resumes with the same original recovery ID/input identity;
it never compares live data to the prepared fingerprint or restores over it.
The backend does not attest runtime dependencies, deliver invitations, reconcile
unknown costs, clean remote containers, or prove the full multi-user journey.
Review [post-restore responsibilities](backup.md#restore-into-new-state) before
issuing fresh credentials or launching new work. Do not remove journals/fences
or use ordinary bootstrap to bypass pending recovery.

## Prepare an offline migration

`reasoning-server migrate-offline` is a host-local migration primitive, run as
the installation-state owner using the selected target release. It does not
install a release, stop services, switch managed units, verify public HTTPS,
provision hardware, or automate an upgrade/rollback. The managed preparation and
activation/rollback and replacement-host recovery backends above are separate.
Do not modify bootstrap journals or use a new
bootstrap to bypass pending ownership.

Before calling it, drain compute workers and settle running or uncertain jobs,
model runs, workflow attempts, mail sends, and webhook sends. Stop or physically
fence all servers, workers, fleet controllers, and outbound execution. Disable
automatic restart of the old server, including reboot activation. Expired leases
or stopped polling do not prove that containers or provider requests have ended.
The command requires an explicit confirmation of this operator-established
condition; it cannot establish it on remote hardware. Older server executables
and direct application-factory callers do not hold the foreground CLI's
maintenance lease and must be fenced independently.

After stopping the source, create and verify an [encrypted backup](backup.md)
with that source's matching release. Retain the archive, its SHA-256, the separate
backup key, both releases, and installation-local mail configuration. Use
protected paths owned by the state owner: state/scratch directories are private
0700, and archive/key files are private regular 0600 files without links. Keep
verified off-host copies and preserve the key separately from the archive.
The helper does not implement root-only key escrow or seal temporary key access
before activation; credential custody is the operator's responsibility.

```sh
reasoning-server --data-dir /private/reasoning-state migrate-offline \
  --backup /private/reasoning-backups/stopped-source.rnbk \
  --key-file /private/reasoning-backups/encryption-key \
  --expected-sha256 ARCHIVE_SHA256 \
  --source-schema SOURCE_SCHEMA \
  --target-release TARGET_RELEASE_ID \
  --operation-id OPERATION_ID \
  --confirm-source-stopped
```

The target release and operation IDs are each 64 lowercase hexadecimal
characters. Use the verified target release's manifest digest and a fresh
operation identity; neither is inferred from the archive's name. The source
schema must be at least 13 and no newer than the target. The helper verifies the
exact built-in source-schema prefix, not only its version number. An incompatible
schema or target validation failure is a refusal, not permission to guess or
downgrade. `backup-inspect` and `restore` still require their matching release;
their default schema checks are not relaxed by maintenance.

The helper acquires an exclusive maintenance lease and available local
initializer/execution-worker locks. It authenticates the source backup into a
private temporary directory beside the archive, incorporates the unchanged
installation-local `mail.json`, and compares its logical database, original
installation secrets, and referenced artifacts with the exact stopped state.
It dry-runs migrations and validates the target before publishing intent. The
live SQL migration is transactional; credentials, accounts, sessions, and queued
work are not subjected to restore-time revocation. The original encrypted
archive and backup-key digest are rechecked before intent, after live migration,
and before a successful preparation receipt.

External compute workers require draining or fencing; running/unknown compute
jobs and unresolved non-LLM workflow execution refuse migration. Under the
exclusive local-worker leases, LLM workflow nodes with a verified same-organization
child request may be retained whether that child is queued, running or terminal.
Missing or mismatched children still refuse migration. Persisted local inference
and delivery labels remain unchanged until normal startup recovery; unknown
outcomes and billing reservations are retained evidence, not executing workers.
Migration never settles charges, fabricates results, or retries these requests.

The private `server-maintenance.json` records `validated`, `migrating`, and
`migrated` phases with source/target state identities, archive/key identities,
release/operation IDs, and byte allowance. An interrupted SQL commit can resume
from the exact original source or recognized committed target. Resume with the
same arguments and original inputs; unrecognized state is never automatically
restored. Initialization is refused while preparation is unfinished.

The payload allowance defaults to 8 GiB; `--maximum-bytes` accepts 1 MiB through
64 GiB and is bound to the operation. Ensure free space for the decrypted clone
and SQLite migration work. Normal exits remove plaintext scratch files; a killed
process can leave private scratch directories. Preserve recovery evidence,
inspect abandoned directories, and use encrypted disks: cleanup is not secure
erasure.

## Activate and acknowledge prepared foreground state

For an operator-controlled foreground installation, start the exact target
release with the original operation ID and the normal installation arguments:

```sh
reasoning-server --data-dir /private/reasoning-state serve \
  --public-url https://chat.example.org \
  --frontend-dir /verified/target/web/dist \
  --deployment-release TARGET_RELEASE_ID \
  --maintenance-operation OPERATION_ID
```

Retain the installation's normal compute catalog, mail configuration, and other
explicit configuration flags. `serve` requires an existing database to have the
exact target schema; it does not silently migrate an older installation.
It holds a shared maintenance lease for its lifetime and verifies the prepared
state before writing `activation_started`, **before** application construction
or worker startup. At that point external effects are possible: offline
migration replay and automatic snapshot restoration are refused. Confirmed
target restarts use the same operation/release but may retain new live data.
This foreground command does not perform a managed systemd upgrade.

After independently verifying the target service, exact HTTPS release identity,
and required application behavior, acknowledge that verification:

```sh
reasoning-server --data-dir /private/reasoning-state maintenance-finish \
  --operation-id OPERATION_ID \
  --target-release TARGET_RELEASE_ID \
  --confirm-activation-verified
```

This command records an operator acknowledgment, not its own health check. Its
receipt explicitly says that health and backup retention were not verified by
this command and that services were not changed. It retains a canonical private
completed record in `maintenance-history/OPERATION_ID.json` before completing
the active journal. Exact retries are idempotent; missing or changed completed
history is not reconstructed as success. Completed foreground startups still
require the selected target release but no longer require an operation argument.
A subsequent upgrade requires a fresh stopped-state backup, a different target
release and operation ID; previous completed records remain intact.

`maintenance-verify` checks exact `migrated` state under exclusive maintenance and
existing initializer/execution locks, using the journal's byte allowance. It
validates schema, secrets, artifacts, idle execution and target state identity
without needing the backup archive/key or advancing activation. It refuses
changed, unfinished, activated, differently bound or concurrently owned state.
It can open SQLite's auxiliary files and is not the filesystem-read-only
`doctor` command. Verification of prepared state does not attest backup retention.

```sh
reasoning-server --data-dir /private/reasoning-state maintenance-verify \
  --operation-id OPERATION_ID \
  --target-release TARGET_RELEASE_ID
```

## Backup and recovery boundaries

Use the [encrypted backup and restore commands](backup.md) for retained snapshots.
Restore requires the exact matching release/schema and a new destination; it
fences restored credentials and pending external execution. The current tooling
does not activate restored state as the managed service or automate upgrades and
rollback. Do not delete journals, rewrite managed units, or invoke a new
bootstrap to force a release change.

After a new server starts it can execute workflows, model requests, mail and bot
deliveries. A failed health check cannot justify silently restoring an older
snapshot: new data and external effects may exist. Schema/data rollback requires
an explicit snapshot identity, confirmation of data discarded since that
snapshot, and actual server/worker/outbound-execution fencing. Expired leases
alone are not physical fencing. Retain the displaced state, original releases,
encrypted archive/key, and operation journals for recovery and reconciliation.
