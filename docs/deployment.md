# Public release and Linux deployment

## Interactive first organization

This is the shortest supported path from a fresh checkout to one HTTPS
organization on DigitalOcean. It creates the initial owner and configures
OpenRouter. Add invitation email, Docker workers, or Modal compute only after
this basic path works.

Before starting, have:

- macOS or Linux with Git, Make, [uv](https://docs.astral.sh/uv/), Node.js matching
  `web/package.json`, `ssh`, `scp`, `ssh-keygen`, and `dig`;
- a funded DigitalOcean account and an API token—the wizard prints the exact
  scopes before asking for it;
- a domain or delegated subdomain that can use DigitalOcean nameservers;
- an OpenRouter account, credits, and at least one model ID; and
- an internet connection with public IPv4 egress for temporary SSH access.

From a clean committed checkout:

```bash
make build
make deploy
```

`make build` installs locked Python and web dependencies, builds the application
source archive and web frontend,
creates `build/reasoning-release.tar.gz`, and verifies its manifest. `make deploy`
starts the interactive `reasoning-deploy setup` wizard with that exact bundle and
stores private credentials and resumable ownership state in
`~/.reasoning-deploy`. The state is outside the checkout and is created with mode
0700.

The wizard creates or reuses the DigitalOcean DNS zone first. If public
delegation is not ready, it creates no billable VM and shows registrar
instructions. It then checks public NS records every ten seconds for up to ten
minutes and continues automatically when all three DigitalOcean nameservers
appear. The animated status shows the nameservers currently visible; `Ctrl-C`
is safe. If the wait times out, run `make deploy` again. Keep the same checkout,
release bundle, and state directory for every retry. Do not run `make build`
again while a deployment is pending.

When a selected parent zone uses Cloudflare and the chat hostname is beneath
it, the wizard offers to use the chat hostname for both the application address
and DigitalOcean DNS zone. Accepting keeps the parent and its other records on
Cloudflare. The wizard then prints the three NS records to add at the child
label in Cloudflare.

Before provisioning, the wizard shows the selected Droplet, current hourly
price, 730-hour estimate, exclusions, and provider-owned LLM spending limits. It proceeds
only after the exact `launch HOSTNAME` confirmation. Failed or interrupted
launches can leave billable resources; always resume with `make deploy` rather
than starting a second state directory.

After HTTPS and final SSH closure, setup requires a deployment-access protection
choice. The default creates and verifies an encrypted offline recovery kit; the
other protected choice authorizes a second operator the same way as
`make operator-authorize`, offering the per-key handoff path under the state's
`handoffs/` directory as the default. Explicit deferral is for
disposable testing and reports the deployment as unprotected.

For a disposable test, use a dedicated domain or delegated subdomain so DNS
changes cannot disrupt existing web or mail records. The wizard does not copy
existing records or change registrar settings itself.

Useful overrides are:

```bash
make build RELEASE=/secure/reasoning-release.tar.gz
make deploy RELEASE=/secure/reasoning-release.tar.gz \
  DEPLOY_STATE="$HOME/.reasoning-deploy-test"
make deploy COMPUTE_BACKEND=modal
```

Modal setup additionally requires the optional SDK, a Modal account, and an
approved published compute-image catalog. `make deploy` installs the optional
SDK when `COMPUTE_BACKEND=auto` or `modal`; the default is `auto`, which offers
compute setup with a yes/no prompt. Choose no, or use `COMPUTE_BACKEND=none`, to
skip compute and enable it later with `make compute-setup`.

### Protect deployment access

Deployment-operator access is independent of application owner/admin roles. One
person may hold both roles, but an application `admin` cannot upgrade the server
without separate operator state, a same-Team DigitalOcean token, and an
authorized SSH key.

For a remote development box or collaborator, run `make onboard` and follow the
[operator onboarding walkthrough](operations.md#add-a-deployment-operator).
The wizard prepares a key, waits for existing-operator authorization, collects
the handoff and own Team token, joins, and verifies SSH trust, the installed
release, HTTPS, and temporary SSH closure. It retains key/token preparation in
`~/.reasoning-onboard` separately from deployment state in `~/.reasoning-deploy`.
Keep both directories.

Operator tokens need `account:read`, `domain:read`, `droplet:read`, `tag:read`,
`firewall:read`, and `firewall:update`; use `make onboard ONBOARD_REPLACE_TOKEN=1`
to collect a replacement privately and resume a completed join or a join that
wrote no deployment state. Onboarding ends with the verified checks and the
next step; `make doctor` is an optional later recheck.
The lower-level `make operator-join` instead requires prepared inputs and a
subsequent `make doctor`. Then `make upgrade` works from the new
checkout without a preexisting local build. Each operator keeps their own key,
token, and state. Coordinate upgrades; `make upgrade` automatically synchronizes
that state against the current protected server release. An older handoff or
recovery kit does not require access to the original operator's laptop.

The default setup choice creates an independent Ed25519 recovery operator,
authorizes its public key on the server, and stores its private key and exact
handoff inside an AES-256-GCM encrypted recovery kit. Setup displays a random
high-entropy recovery code and verifies the finished kit by asking for the code
again. The kit contains no DigitalOcean or OpenRouter token, owner password,
application data, or host private key.

Store the kit and recovery code separately where both remain available without
the operator computer. A password manager plus offline/removable storage is a
suitable pattern. A completed deployment can create another kit with:

```bash
make operator-kit RECOVERY_KIT=/secure/reasoning-operator-recovery.rnrk
```

On a replacement computer, clone the repository and save a fresh token from the
deployment's existing DigitalOcean Team with Account read and Firewall
read/update access in a private `0600` file. Then run:

```bash
make operator-recover \
  RECOVERY_KIT=/secure/reasoning-operator-recovery.rnrk \
  DO_TOKEN_FILE=/secure/digitalocean-token
```

The hidden recovery-code prompt decrypts and validates the kit, checks Team
identity, and creates independent protected state at `DEPLOY_STATE`. The kit
restores operator access only; encrypted application backups and replacement-host
recovery remain separate. See [server operations](operations.md#recover-after-losing-an-operator-computer).

### Upgrade a wizard-managed test server

For a completed deployment with `COMPUTE_BACKEND=none`, update a clean checkout
and run:

```bash
git pull --ff-only
make upgrade
```

Do not run `make build` first. `make upgrade` retains the currently installed
bundle before replacing the build output, builds and verifies the checkout, and
then starts the interactive upgrade. New deployments also retain their release
archive in the protected deployment state. Every retry uses the same source and
target archives, plan, operation ID, SSH identity, and `~/.reasoning-deploy`
directory.

The wizard opens SSH only to the current public `/32`, diagnoses the live source,
and refuses a dirty, unchanged, downgraded, or unhealthy target. A typed prepare
confirmation authorizes server downtime, a restart fence, and an encrypted
backup. A separate activation confirmation switches releases and verifies HTTPS.
Both confirmations use the recognizable public URL; the review panel displays
the exact installed and target release identities without requiring either
64-character digest to be retyped.
Progress remains visible during both phases; inbound SSH is closed afterward.
If transport or activation is uncertain, rerun `make upgrade` without changing
the checkout or state. The retained journals determine whether work is replayed.

This path currently supports the single-server, no-compute test deployment.
Externally executing workers/controllers require independent physical fencing,
and production use still requires real-host upgrade and rollback rehearsal.

### Prepare a verified release

`make build` is the supported source-checkout build. It runs `uv sync --locked`,
`npm ci`, the production web build, source collection, bundle construction, and
bundle inspection. The source must be a clean committed revision. The output is
replaced only after a complete new bundle verifies successfully.

The builder verifies the source archive, Python entry points, and
legal material. It exports hash-locked production dependencies from `uv.lock`
without changing the lock. It includes only the organization HTML entry point
and its reachable JavaScript, CSS, and static dependencies. Files have bounded
sizes and names, and symlinks are refused.

Maintainers can call `reasoning-deploy bundle` directly with
`--compute-catalog /path/to/compute-images.json` to include an approved published
CAS catalog. The builder validates 1–32 unique immutable image records and their
source, evidence, and resource-limit fields. This does not publish images or
verify registry availability; see [image distribution](images.md).

The release ID is the SHA-256 of the manifest, which records the Git revision,
dirty flag, database schema, installation features, and every payload hash and
size. Hash verification establishes content identity, not publisher trust.

### Guided credential and SSH preparation

Run the wizard on your own macOS/Linux computer with `ssh-keygen`, SSH, `scp`,
and `dig` available. DigitalOcean signup/payment, OpenRouter credits, Modal
signup/payment, domain purchase, and registrar delegation are not automated.
The wizard asks for one custom DigitalOcean token and lists resources
alphabetically so each resource's permissions can be selected together. It does
not require a separate token for each scope. The resources
are Account (read), Domain (read/create), Droplet (read/create), Firewall
(read/create/update), Sizes (read), SSH key (read/create), and Tag (read/create).
`ssh_key:create` is unnecessary only when the exact public key is already in
the account; include it for the default generated-key path.

Enter the token at the hidden prompt or give the path of a protected `0600`
file. Before any API call, the wizard checks its shape: `dop_v1_` (or `doo_v1_`)
followed by 64 lowercase hexadecimal characters, or 64 hexadecimal characters
for an older token. A token pasted twice, a refresh token, or a value with
spaces is refused without being saved, and the hidden prompt asks again up to
three times. Visible prompts, such as the question that offers to open the
token page, refuse an answer that looks like a token or API key without
repeating it and ask again. The terminal displayed that value, so revoke it in
DigitalOcean and create a new token; the same run also refuses it at the hidden
prompt.

If a launch made with an older or incorrectly scoped token reaches final SSH
closure without `firewall:update`, the server remains running and SSH remains
restricted to the saved administrator `/32`. On resume, the wizard explains
that DigitalOcean token scopes cannot be edited, accepts a replacement token
from the same account, and retries only the firewall closure; it does not launch
a second server or repeat application configuration. Revoke the old token in
DigitalOcean after setup succeeds.

- SSH selection defaults to generating a dedicated Ed25519 key inside the
  protected operator directory. `ssh-keygen` asks for an optional passphrase;
  keep it available for later SSH commands. An explicit existing private-key
  path is also supported. The public key is derived locally rather than
  trusting an adjacent `.pub` file or asking the operator to guess a key ID.
  An exactly matching account key is reused. Otherwise, typed `upload ssh key`
  confirms uploading only the public key. Account/key identity and intent are
  saved before the upload; an uncertain reply is reconciled by exact public-key
  reads, never a duplicate upload. Changed or removed ready keys are refused.
  Interrupted local generation never overwrites or silently regenerates a key.
- The wizard reads `https://api.ipify.org` to detect the operator's current
  public IPv4, restricts SSH to that single `/32` for the setup session, refreshes
  the rule on resume, and removes inbound SSH after successful provider and
  application configuration. If detection is unavailable, it asks for an
  explicit `/32`. The ownership-bound access journal reconciles uncertain
  firewall updates and refuses unjournaled rule changes.
- OpenRouter authentication defaults to browser authorization with S256 PKCE
  and a temporary `127.0.0.1` callback on a random port/path. The listener
  accepts only the bound callback/Host and one authorization code, suppresses
  request logs, returns no secrets, and closes on completion or after ten
  minutes. Choose `code` for SSH/remote terminals: the provider page displays
  a single-use code entered privately at the terminal. Choose `manual` to use
  an existing protected API-key file or hidden prompt. The key is saved in a
  private file, not printed. Exchange intent is persisted before its request;
  an uncertain exchange stops for key recovery/manual reconciliation instead
  of minting another key. No model inference is called during authentication.
- Modal authentication defaults to the pinned SDK's `modal token new` browser
  flow, bounded to ten minutes. It uses private `modal-browser.toml` and a
  dedicated profile in the operator directory, removes inherited Modal token,
  profile and endpoint overrides, and leaves ordinary user profiles untouched.
  Complete saved credentials are reused, including a completed token flow
  with a lost final receipt. An incomplete uncertain flow stops for recovery,
  not another token creation. Choose `manual` for existing token ID/secret
  files or hidden prompts. Provider authentication does not launch a compute VM.

Browser authorization and its provider-side identity/credit requirements follow
the official [OpenRouter PKCE](https://openrouter.ai/docs/guides/overview/auth/oauth)
and [Modal token CLI](https://modal.com/docs/cli/latest/token) protocols. Browser
flow acceptance against real provider accounts remains unverified.

The command prepares the DNS zone with explicit confirmation, checks public
nameserver resolution, and stops without creating billable resources if delegation
is pending. Its registrar handoff is described below.
Resume the same command/directory after propagation; saved credentials and owner
inputs are reused. Plans bind one release and hostname; setup refuses replacing
them with a different deployment. The operator sees the saved hourly quote,
730-hour estimate and exclusions before typing `launch https://HOSTNAME`.
This authorizes the exact cloud/root changes and provider-delivered host-key
pinning. Launch independently rechecks live prices and original resource identity.
Failure/pending readiness retains the original billable resources and journals;
do not start a new directory to retry. Saved plan quotes are not invoice caps.

### Guided registrar delegation

When a parent zone uses Cloudflare and the HTTPS hostname is its child, `setup`
offers to make that hostname the DigitalOcean zone as well. For example,
`chat.example.org` becomes both the application hostname and delegated zone,
while `example.org` stays on Cloudflare. The saved choice is resumed without
re-entering credentials. The Cloudflare handoff shows three ordinary NS records
with the child label, such as `chat`; it does not ask for an apex nameserver
change or repeat the generic DNS-migration warning.

For other pending delegation, `setup` recognizes Cloudflare authoritative
nameservers and asks the operator to confirm the detected DNS provider before
showing provider-specific instructions. Nameservers identify the DNS provider,
not necessarily the registrar; declining detection opens the ordinary selection.
Otherwise, `setup` offers the 14 registrars in
[DigitalOcean's catalog](https://docs.digitalocean.com/products/networking/dns/getting-started/dns-registrars/).
Enter a numbered choice, registrar name, or search fragment. Ambiguous searches
show matching choices; `0`, `other`, or `manual` selects the unlisted-registrar
fallback. Choose that fallback for a delegated subdomain: keep the registered
domain's nameservers and add NS records for the exact subdomain at its parent DNS
provider. No registrar account credentials are requested.

| Registrar | Nameserver settings / official instructions |
| --- | --- |
| Cloudflare DNS (detected) | [For a delegated subdomain, add three NS records at its label in the parent Cloudflare zone](https://developers.cloudflare.com/dns/manage-dns-records/how-to/subdomains-outside-cloudflare/). A domain registered through Cloudflare Registrar [cannot change its apex to third-party nameservers](https://developers.cloudflare.com/registrar/faq/#can-i-use-my-own-third-party-nameservers); use a delegated subdomain or transfer the registration. |
| Bluehost | [Domains → domain → Nameservers → Change Nameservers](https://www.bluehost.com/help/article/custom-nameservers) |
| Domain.com | [Use the account-login branch in the official DNS guide](https://www.networksolutions.com/help/article/ns-what-is-dns); Network Solutions account manager: Domains → domain → Advanced Tools → Nameservers (DNS) → Manage |
| DreamHost | [Manage Domains → Manage → Nameservers → edit → Use another host's nameservers](https://help.dreamhost.com/hc/en-us/articles/360038897151-Changing-your-nameservers-at-DreamHost) |
| Dynadot | [My Domains → Manage Domains → select domain → Action → DNS Settings → Name Servers](https://www.dynadot.com/help/question/set-name-servers) |
| Enom | [Domains → My domains → domain → DNS server settings → Custom](https://support.enom.com/support/solutions/articles/201000065375) |
| Gandi | [Domain → domain → Nameservers → Change → External](https://docs.gandi.net/en/domain_names/common_operations/changing_nameservers.html) |
| GoDaddy | [Domain Portfolio → domain → DNS → Nameservers → I'll use my own nameservers](https://www.godaddy.com/en/help/change-my-domain-nameservers-664) |
| HostGator | [Customer Portal → Domains → domain → Nameservers → Change Nameservers](https://www.hostgator.com/help/article/what-are-my-name-servers) |
| Hover | [Domain → Overview → Nameservers → Edit](https://support.hover.com/support/solutions/articles/201000064742) |
| IONOS | [Domains → Actions gear → Name Server → Use Custom Name Servers](https://www.ionos.com/help/domains/using-your-own-name-servers/using-your-own-name-servers-for-a-domain/) |
| Name.com | [My Domains → domain → Domain Actions → Manage Nameservers → Delete All; add each nameserver, then Save Changes](https://www.name.com/support/articles/205934547-changing-nameservers-for-dns-management) |
| Namecheap | [Domain List → Manage → Nameservers → CustomDNS](https://www.namecheap.com/support/knowledgebase/article.aspx/767/10/how-to-change-dns-for-a-domain/) |
| Porkbun | [Domain Management → Details → edit Nameservers](https://kb.porkbun.com/article/22-how-to-change-nameservers) |
| Squarespace | [Domains dashboard → domain → DNS → Domain Nameservers → Use Custom Nameservers](https://support.squarespace.com/hc/en-us/articles/4404183898125-Review-change-or-reset-your-domain-s-nameservers) |

The wizard prints a dashboard link, official help link, short instructions, and
the three copyable nameservers. It offers to open the dashboard and guide in
your local browser; answer `n` for a remote/headless terminal. Account layouts
can vary, so the official guide is the reference for the account you use.
For a registered domain, change its registration-level nameserver settings, not
ordinary NS records or glue/host registrations. For a delegated subdomain, keep
those settings and publish the delegation as NS records in the parent zone. Use
all three hostnames without IP addresses:

```text
ns1.digitalocean.com
ns2.digitalocean.com
ns3.digitalocean.com
```

Before changing delegation, copy all existing DNS records into the DigitalOcean
zone, including website records and email MX/TXT/SPF/DKIM/DMARC records. The wizard
neither copies records nor inspects DNSSEC.
[DigitalOcean DNS does not support DNSSEC](https://docs.digitalocean.com/support/why-does-my-domain-fail-to-resolve/):
if enabled, disable it/remove parent DS records through the registrar and allow
that removal to propagate before switching nameservers. If the registrar cannot
use external nameservers, stop and contact its support; the wizard does not
transfer domains or purchase replacements.

The selection is saved privately in a registrar-guide journal bound to the
original release and DNS zone, and reused on retry. A Cloudflare child-zone
choice is separately saved in `dns-delegation.json`; an interrupted write is
completed on resume. A saved manual selection can change to a subsequently
detected Cloudflare guide only after operator confirmation.
After the handoff, an animated status checks public NS records every ten seconds
for up to ten minutes and setup continues in the same run when delegation
matches. On timeout, the wizard prints a shell-quoted resume command retaining
the original bundle, operator directory, and optional Modal selection. DNS
propagation can take up to 48 hours. Resume from the same checkout and directory:
pending delegation returns `dns_delegation_required` before owner creation,
deployment planning, or paid provisioning. A matching result from the system's
recursive resolver allows setup to proceed; this is not proof of global
propagation or DNSSEC correctness.

### Organization policy activation

After actual HTTPS/owner readiness, the CLI signs in temporarily over verified
HTTPS, verifies the approved model suggestions, uploads the
OpenRouter key into encrypted organization storage, and revokes its temporary
session. The key can use any available OpenRouter text model or router; setup
does not ask for or install a model whitelist. It checks actual server model
policy and key metadata. Configure the key's daily spending limit in OpenRouter;
the application displays provider-reported daily usage without a second local
organization cap. Provider-key creation
is journaled before submission; a lost reply is reconciled through reads, not
blindly repeated. Changed administrator policy, duplicate/revoked setup keys,
and mismatched saved inputs are refused. Reusing the key shares its provider
allowance with every application using it. Credit top-ups do not reset its daily limit.
The private operator directory retains credential inputs; do not publish it.

`organization_ready` establishes server/provider configuration, not live
multi-user acceptance. Sign in, invite two users through administration links,
and verify alpha chat, beta inference, an LLM-only gamma workflow, a scoped bot,
and persistence/reconnection after restart. Email, real container execution,
autoscaling, and native-client acceptance are separate verification scopes.

### Modal in interactive setup

For an organization with optional on-demand compute, run:

```bash
make deploy COMPUTE_BACKEND=modal
```

Use a current bundle advertising `modal-relay-v1`; its verified contents include
separate hash-locked `modal-requirements.txt`. The ordinary chat environment
does not install or import the optional SDK. The wizard selects a nonempty
catalog of approved published digest-pinned CAS images from the trusted release
or an explicit private file. It offers Modal browser login or existing token
ID/secret through protected files or hidden prompts, and asks for app/environment,
per-job logical CPU/memory/wall-time ceilings, 1–8 simultaneous jobs, and monthly
application CPU-time quota. Catalog digests/source/evidence must come from an
actual image release, not illustrative values. If the trusted bundle supplies
`compute-images.json`, setup verifies its manifest/hash, saves a protected
private copy, and uses it without asking users to build or prepare images.
If absent, an explicit catalog-file prompt is required. No built-in published
catalog is currently supplied. Account signup/payment and
workspace-wide provider spending controls remain operator steps.

Selections are saved privately and Modal authentication is checked read-only
before DNS or billable server provisioning. A source-only installation needs its
`modal` extra installed on the operator machine for this probe.
The launch confirmation also authorizes adding the catalog, scoped relay
enrollment, and on-demand paid Modal VMs when users submit eligible compute.
The CLI displays per-job limits, concurrency, quota, and billing exclusions;
it does not present an application CPU-time quota as a Modal invoice cap.
Resume the same bundle/state directory with `--compute-backend modal`; omitting
the original compute selection is refused.

After HTTPS/owner readiness, setup uses the freshly checked original DO VM and
its provider-pinned SSH identity. The host installation journal must be at
`services_started` for that exact release and site. Under the shared managed-host
bootstrap lock, it installs the approved catalog only into an empty or identical
catalog and restarts the server. It then configures organization compute policy through
HTTPS, creates one scoped scaler grant, and recoverably enrolls the exact relay
slots with saved client-owned nonces/credentials. A lost one-time scaler-grant
creation reply stops for credential recovery or explicit manual reconciliation;
it does not create another grant. Changed policy, revoked enrollment, pending
server upgrade/recovery, or changed original inputs are not overwritten.

The relay environment is `/opt/reasoning/modal/RELEASE/venv`, installed from the
original source archive and hash-locked dependencies. Each `reasoning-modal-WORKER_ID`
systemd service runs as `reasoning-modal`, with private
`/var/lib/reasoning-modal/WORKER_ID` state and no access to chat state, operator
staging, or installation configuration. It holds the Modal credentials and its
organization-scoped worker token; the chat host needs no Docker daemon. Setup
performs a no-job enrollment/app probe and verifies loaded service identity,
absence of overrides, and activation. Its
receipt remains `live_compute_verified:false`: use the gamma compute controls
to verify actual algebra/artifacts, cancellation, and restart recovery before
claiming live compute acceptance. See [Modal compute](compute.md#modal-vm-compute)
for execution, billing, ownership, and cleanup boundaries.

### Scope and deeper operations

`reasoning-deploy` installs over verified SSH on a dedicated Ubuntu 24.04 amd64
host. `install` uses already-owned hardware; `do-launch` provisions and installs
a DigitalOcean server. Compute workers require separate hosts, while Modal relays
may share the chat host and execute jobs in remote VMs. Optional invitation email
uses operator-owned sending credentials.

Read-only diagnosis, upgrades, rollback, replacement-host recovery, backups,
worker fleets, and owned-hardware commands are documented in
[operations](operations.md), [backup](backup.md), and the reference sections
below. Real-host deployment, recovery, and the complete multi-user acceptance
journey remain unverified; fixture tests are not release acceptance.

## Prepare DNS through the CLI

DigitalOcean zone setup needs an active account token with `account:read`,
`domain:read`, and `domain:create`. It does not register or purchase a domain:
the domain must already belong to the operator. The API creates only the zone,
without an apex A record. Existing zones/records are left untouched:

```bash
reasoning-deploy do-dns-setup --token-file /private/digitalocean-token \
  --zone example.org --state-dir /private/reasoning-dns \
  --confirm-zone example.org
reasoning-deploy do-dns-check --zone example.org
```

Delegate the zone at its registrar to `ns1.digitalocean.com`,
`ns2.digitalocean.com`, and `ns3.digitalocean.com`. Zone creation uses a private
account-bound journal; an uncertain creation outcome is observed on retry, never
blindly submitted again. Keep the original directory. A definitive rejected
request can be retried, but absence after an uncertain request or a ready zone
requires operator/provider reconciliation. Zone setup does not change registrar
settings, DNS records, or create a VM.

`do-dns-check` requires `dig` (Ubuntu: `dnsutils`; macOS: system `dig`) and uses
an explicit bounded answer-only lookup. It verifies that the current system
recursive resolver returns exactly the expected three NS records. This is not a
global propagation or DNSSEC proof. Exit 0 means matching nameservers;
exit 1 means delegation is pending/mismatched; invalid input or failed lookups
return an error. No provider token is needed. The hostname A record is created
by the confirmed deployment after VM/firewall readiness; installation then
verifies actual public HTTPS.

## Plan DigitalOcean resources

Create an account and private API token in the
[DigitalOcean control panel](https://cloud.digitalocean.com/account/api/tokens).
`do-plan` only reads account identity, size availability/prices, an account SSH key,
and the selected DNS zone/records. It requires `account:read`, `sizes:read`,
`ssh_key:read`, and `domain:read` scopes. Give one token those read permissions,
store it in an owner-owned 0600 file without a symlink, and do not put its value
in shell arguments or source control. Add your SSH public key to the account and
delegate a DNS zone you control to DigitalOcean before using its subdomains.
The account must be active with verified email. Use an operator-owned protected
directory for plans; the provisioning journal directory must be private (0700).
Files and parent paths must not traverse symlinks or allow group/public writes.

```bash
reasoning-deploy do-plan --token-file /secure/digitalocean-token \
  --name reasoning-mathematics --region fra1 --size s-2vcpu-4gb \
  --public-url https://chat.example.org --dns-zone example.org \
  --ssh-key-id 123456 --admin-cidr 203.0.113.10/32 \
  --maximum-hourly-usd 0.10 --pin-ssh-host-key \
  --output /secure/reasoning-deploy/plan.json
```

Replace every example with your actual account/domain/network selections.
The plan rejects unavailable/undersized plans, prices above the explicit ceiling,
worldwide/non-IPv4 SSH CIDRs, a hostname outside the selected zone, and existing
address/alias records or conflicting subdomain delegation. It needs at least 2 GiB RAM and 10 GiB boot
disk. The result excludes the token and is marked `creates_resources: false`.
No API writes or billable resources result from this command.
`--output` exclusively creates a private 0600 canonical plan, with a deployment
identity and SHA-256 plan ID. It binds the selected user/team account, public-key
bytes, resource shape, hostname, administrator network, and price quote. Omitting
`--output` prints a read-only plan without creating an applyable plan file.

`--pin-ssh-host-key` explicitly trusts DigitalOcean's authenticated user-data
delivery to initialize a unique Ed25519 host key. The private host key is saved
in `digitalocean-ssh.json` before the VM request and sent in provider user data.
Preserve this private file with original ownership state: pending/created VMs
must never receive a replacement key. Provider user data is accessible to host
root and through VM metadata, so anyone with that access can impersonate the
host. This option is not independent console verification. Omitting it requires
an independently verified known-hosts file for `do-install` instead; `do-launch`
requires the original plan's explicit pinning consent. No keyscan or
trust-on-first-use fallback is used.

The output gives a live hourly compute quote and a 730-hour estimate, not an
invoice cap. Tax, transfer overages, backups, storage, and other services are
excluded; actual billing depends on the selected plan and provider policy.
See [DigitalOcean pricing](https://docs.digitalocean.com/products/droplets/details/pricing/).
The example price ceiling is a user limit, not a quoted price. DNS delegation,
provider resource readiness, SSH authentication, and TLS readiness are distinct checks.

## Create or resume DigitalOcean resources

For creation, the token additionally needs `droplet:read`, `firewall:read`, and
`tag:read` for inspection, plus
`droplet:create`, `firewall:create`, `tag:create`, and `domain:create` for the four
creation requests. Direct creation does not need update/delete scopes. The guided
wizard additionally uses `firewall:update` to rotate and close its temporary SSH
rule. Provider endpoint details
are documented for [Droplets](https://docs.digitalocean.com/reference/api/reference/droplets/),
[firewalls](https://docs.digitalocean.com/reference/api/reference/firewalls/), and
[domain records](https://docs.digitalocean.com/reference/api/reference/domain-records/).
Account signup/payment setup and registrar zone delegation remain operator
steps. Direct provisioning expects an enrolled key; the interactive wizard can
prepare/enroll it. The CLI does not collect provider account passwords.

```bash
reasoning-deploy do-apply --token-file /secure/digitalocean-token \
  --plan /secure/reasoning-deploy/plan.json \
  --state-dir /secure/reasoning-deploy/ownership \
  --confirm-plan PLAN_ID_FROM_DO_PLAN --wait-seconds 60
```

Exact plan confirmation acknowledges ongoing provider charges. Before creation,
apply revalidates account, key, resources, availability, and current price. An
increased quote is refused even below the ceiling; a decreased quote is allowed.
Existing charged resources are not deleted on failure or price/account drift.
Do not start another plan until accounting for any existing resources.

The CLI creates a unique deployment tag, a tag-scoped firewall, one Ubuntu
24.04 amd64 CPU Droplet, and an unused DNS A record with TTL 300. Inbound TCP
22 is restricted to the confirmed IPv4 administrator CIDR; TCP 80/443 are public.
Outbound IPv4 TCP 53/80/443, UDP 53/123, and ICMP support DNS, packages, HTTPS,
time synchronization, and network diagnostics. IPv6 and provider automated
backups/monitoring are not requested. Cloud-init disables password SSH and writes
a provisioning identity and, only for explicitly pinned plans, the unique SSH
host private/public key. It contains no provider token or organization secrets.

The firewall is created before the Droplet. Once the Droplet is active, its
public IPv4 address, exact associated firewall/rules, and completed firewall
application status are checked before DNS publication. Extra/mismatched firewalls and changed
resources are refused, not overwritten. Provider firewall/tag propagation is not
claimed to be atomic with initial VM boot. No application secrets are installed
until the separate verified-SSH installation step.

The guided wizard opens SSH only for its current setup session and closes it
after the organization and optional compute backend are configured. The same
ownership-checked operation is available explicitly:

```bash
reasoning-deploy do-ssh-access --token-file /secure/digitalocean-token \
  --plan /secure/reasoning-deploy/plan.json \
  --state-dir /secure/reasoning-deploy/ownership \
  --confirm-plan PLAN_ID_FROM_DO_PLAN --open-cidr 203.0.113.10/32

reasoning-deploy do-ssh-access --token-file /secure/digitalocean-token \
  --plan /secure/reasoning-deploy/plan.json \
  --state-dir /secure/reasoning-deploy/ownership \
  --confirm-plan PLAN_ID_FROM_DO_PLAN --close
```

Only one exact IPv4 `/32` is accepted. Interrupted updates retain their intent
for read-before-retry reconciliation; externally changed rules are refused.
An update is reported as `open` or `closed` only after DigitalOcean reports a
succeeded firewall with no pending changes; otherwise it remains `opening` or
`closing` and the same command can be resumed. The wizard saves its completed
application/provider receipt before requesting closure, so a closure retry does
not reopen SSH or repeat configuration.

The command waits between readiness checks for at most the selected 0–60 seconds;
individual bounded API requests also take time. `firewall_pending` and
`droplet_pending` retain ownership and require resuming the same command/journal.
`ssh_verification_required` gives the public address, resource IDs, and provider
console URL; it explicitly reports `application_installed: false` and
`https_ready: false`. Verify the host key through that console, then use the
owned installation command below. Pinned plans report `ssh_key_pinned` instead;
this indicates provider readiness and the persisted pin, not an actual SSH
connection, installation, or HTTPS readiness.

Private journal entries are fsynced before every creation request and exclusively
locked. Initial provisioning uses exactly one journal directory/operator host per
saved plan. Setup protects later access through an encrypted offline recovery
operator or a [deployment-operator handoff](operations.md#add-a-deployment-operator),
which creates independent, secret-free upgrade state for an authorized same-Team
collaborator. `make upgrade` synchronizes independent or recovered operator
state with the current verified server release; `make operator-sync` performs
that read-only refresh explicitly. It retains previous local evidence, verifies
original VM ownership and pinned SSH trust, and obtains a changed release only
from verified archives retained on that server. It requires neither the first
operator's computer nor a new handoff after each upgrade.
Verified configuration changes on the same release retain separate content-bound
sync observations without replacing earlier evidence or weakening host checks.
Its lock is local, not a distributed provider lock, so operators must not apply
changes concurrently from different directories or machines. If a response is
lost, resumption only adopts one matching resource with
the expected deployment identity and configuration. A definitively rejected
client-error request can be retried; a request of uncertain outcome is never
blindly replayed. If no resource is visible yet, keep the journal and retry
observation later. Continued absence, conflicting ownership, or duplicate resources
requires operator/provider reconciliation; an explicit absent-request recovery
command is not implemented. Do not delete/edit the journal to force another POST.

Retain the saved plan and private ownership journal. They do not contain the API
token or operator private SSH key; `digitalocean-ssh.json` contains the pinned
host private key when selected and must never be copied into a collaborator
handoff. Failed provisioning can leave billable resources;
inspect the recorded IDs/tag in the provider console and account for them before
starting over. The CLI has no automatic destruction or rollback of cloud resources.
Destroying a Droplet is destructive and requires an intentional operator decision;
powering it off does not stop its compute billing.

## Launch the organization server on DigitalOcean

Prepare the initial account through private password prompts; no organization,
server, or cloud resource is created by this input-preparation command:

```bash
reasoning-deploy owner-input --email owner@example.org --name Owner \
  --organization Mathematics --output /secure/reasoning-owner.json
```

The exclusively created 0600 canonical file contains the initial password and
a fresh random replay credential. Keep it outside repositories, bundles, user
data, and backups intended for other people. Preserve the same file until
initial setup and any uncertain reply have been reconciled; regenerating its
nonce/password is not a retry. Its parents must be protected nonsymlink paths.
The command emits only its filename and nonsecret setup ID, not credentials.

After reviewing a saved pinned-SSH plan and the release from `inspect`, one
command creates/resumes its resources, installs that exact release, and creates
the organization and initial owner:

```bash
reasoning-deploy do-launch --token-file /secure/digitalocean-token \
  --plan /secure/reasoning-deploy/plan.json \
  --state-dir /secure/reasoning-deploy/ownership \
  --bundle reasoning-release.tar.gz --identity /secure/reasoning-ssh-key \
  --owner-file /secure/reasoning-owner.json \
  --confirm-plan PLAN_ID_FROM_DO_PLAN --confirm-release RELEASE_ID_FROM_INSPECT \
  --wait-seconds 60
```

The two confirmations authorize ongoing provider charges and root installation
on the exact original VM. The CLI validates local inputs and snapshots the
confirmed release before provisioning. A private `digitalocean-launch.json`
binds plan/release/URL before paid requests; changing that binding is refused.
When `--owner-file` is supplied, `digitalocean-owner.json` separately binds the
setup ID and input digest before paid launch requests. Its pending/initialized
receipt contains no password, replay credential, or login session. Changing the
original owner inputs is refused, including after a lost success reply.
No provider login password, organization password, or application secret is
placed in shell arguments or user data.

Pending provider readiness returns `application_installed: false` and
`https_ready: false`. Resume the same command, plan, release, and ownership
directory and owner input; do not start another launch. SSH/bootstrap/TLS failures retain the
original resources and exact installation intent and do not stop billing.
The provider wait is bounded to 0–60 seconds, not an end-to-end deadline;
package/SSH operations have longer individual timeouts.

For resources created separately with `do-apply`, install without further cloud
creation or deletion:

```bash
reasoning-deploy do-install --token-file /secure/digitalocean-token \
  --plan /secure/reasoning-deploy/plan.json \
  --state-dir /secure/reasoning-deploy/ownership \
  --bundle reasoning-release.tar.gz --identity /secure/reasoning-ssh-key \
  --owner-file /secure/reasoning-owner.json \
  --confirm-plan PLAN_ID_FROM_DO_PLAN --confirm-release RELEASE_ID_FROM_INSPECT \
  --confirm-droplet DROPLET_ID_FROM_ORIGINAL_OWNERSHIP
```

For an unpinned plan, also supply `--known-hosts /secure/reasoning-known-hosts`
with the independently verified key. Installation needs read access to the
original account, DNS zone/records, Droplet, tag, and firewall, but no
create/update/delete scopes. Both commands refuse incomplete ownership,
duplicates, drift, a changed DNS target, nonexclusive/pending firewall
association, and retirement intent. They journal `installing` before SSH.

On the host, the original cloud-init marker and DigitalOcean metadata VM ID are
checked before package changes and after activation. A protected
`server-provisioning.json` binds VM/release/URL; retries cannot omit/change it.
The returned bounded receipt must match the release and VM exactly, contain
only approved secret-free fields, and confirm both active, loaded systemd units
without drop-ins/identity drift plus successful verified HTTPS health. The
operator then independently checks public HTTPS and rechecks provider ownership
before persisting `installed`. An old receipt or a successful SSH exit is not a
substitute for fresh verification. With an owner file, the output must report
`initialized: true` and an exact `owner_setup` receipt with organization/user
IDs. Then sign in at the HTTPS URL using the initial email/password and invite
members. Supply `--mail-file` to configure optional invitation email during
installation, or configure it separately as described below.

The owner input is uploaded only over verified SSH into protected root staging,
then passed through a temporary private service-owned `/run` directory to the
release's `reasoning-server setup-file`. Temporary service input is removed on
normal success/failure; the uploaded staging copy is removed after successful
setup/readiness. Abrupt host/process termination can retain a private copy; do
not publish staging/runtime state. The operator's original file is not deleted.
The host-local transaction creates owner, organization, and keyed replay receipt
atomically, with no login session and no plaintext credentials stored in SQLite.
Exact retries recover the original IDs without resetting passwords or changing
roles. A different setup or an already initialized installation with no matching
host receipt is refused, not adopted. Later password recovery uses the explicit
reset command, not a replacement owner file.

`--owner-file` is optional on `do-launch`, `do-install`, and the owned-hardware
`install` command. Omit it only when owner creation will be performed separately.
The existing interactive host-local setup command below remains supported.

## Install on a rented or owned host

Use a dedicated fresh Ubuntu 24.04 amd64 installation with Python 3, systemd,
root SSH access, outbound package/DNS/HTTPS access, and a public DNS hostname.
The installer refuses existing unmanaged Reasoning paths/accounts, an unmanaged
Caddy installation, or active existing web services. It is not an installer for
adding Reasoning to an arbitrary shared production server. The host must have
public TCP 80/443 reachable for certificate issuance and chat; SSH should be
restricted to administrators. The backend remains loopback-only on port 8766.

Verify the SSH host public key through an independent trusted channel, such as
the provider console or the hardware's console. Put the verified entry in the
specified known-hosts file. An unauthenticated `ssh-keyscan` alone does not verify
the key. Installation refuses unknown/changed keys; it does not disable SSH host
verification or infer trust-on-first-use. Your selected private SSH key remains
on the operator computer and is not bundled or uploaded.
Known-hosts files may be public-readable, but must be bounded regular files,
operator/system-owned, protected from group/public writes, and without symlinks
or hardlinks. Private tokens/keys must be operator-owned and private (0600).

```bash
reasoning-deploy inspect --bundle reasoning-release.tar.gz
reasoning-deploy install --bundle reasoning-release.tar.gz \
  --public-url https://chat.example.org --ssh-host 203.0.113.20 \
  --identity /secure/reasoning-ssh-key --known-hosts /secure/reasoning-known-hosts \
  --owner-file /secure/reasoning-owner.json \
  --confirm-release RELEASE_ID_FROM_INSPECT
```

The exact release confirmation acknowledges root host changes. The CLI takes
a private bounded copy of the input archive, verifies that copy, and rechecks
confirmation against it before any remote command. The CLI uploads
that verified public bundle and its standalone standard-library installer into
a release-specific private staging directory. The host verifies the entire
archive SHA-256, canonical manifest, sizes, hashes, names, and regular-file types before
installation. It rejects archive extension/sparse/link/device metadata before
parser expansion. It never extracts user-controlled paths with `extractall`.

The installer installs Ubuntu's Caddy and Python venv packages, hash-locked PyPI
dependencies, and the verified application source. No application wheel is built or published. Application code/frontend are
root-owned under `/opt/reasoning/releases/RELEASE_ID`. The unprivileged
`reasoning` account owns private 0700 state at `/var/lib/reasoning`; its secrets
are not in the public release. `/etc/reasoning` holds the approved image catalog
and Caddy configuration. `reasoning-server.service` serves the organization app;
`reasoning-proxy.service` runs Caddy with separate certificate state. Units use
filesystem/privilege hardening. Docker/compute privileges are not granted to the
server account. The packaged default Caddy service is disabled in favor of the
dedicated proxy service.

Caddy obtains and renews HTTPS certificates and replaces forwarded client IP
headers with the actual peer. Its `reverse_proxy` flushes `text/event-stream`
responses immediately, and the topic event stream's `Cache-Control: no-transform`
keeps the `encode` directive from compressing or holding it back; application
security headers pass through unchanged. Public requests cannot choose the server's origin
or spoof their peer through these headers. The CLI waits for a verified HTTPS
`/api/health` response; failure reports readiness as unverified and retains state
for diagnosis/resumption. DNS/certificate issuance may take longer than its
60-second readiness window. The CLI validates a structured exact installation
receipt, not only upload metadata. Package/bootstrap subprocess output is not
mixed into that receipt. Consult `journalctl -u reasoning-server -u
reasoning-proxy`, DNS, and firewall settings, then retry the same release.

Bootstrap is resumable for the exact same release and public origin; changing
either is refused by bootstrap. Release changes use the separately confirmed
upgrade preparation/activation commands described in [operations](operations.md).
Repeating bootstrap can repeat package/setup commands. It does not reset organization data.
Existing generated service/proxy configurations must match their expected bytes;
operator edits are refused before package changes, even if the managed marker remains.
Do not delete its journal to force an upgrade. Host mutation and real systemd/
Caddy/kernel behavior require a real host verification; fixture tests are not a
production boot test.

If no owner file was supplied during installation, initialize the owner through
the host-local account setup command, which prompts privately for the password:

```bash
ssh -t -i /secure/reasoning-ssh-key root@203.0.113.20 \
  runuser -u reasoning -- /opt/reasoning/releases/RELEASE_ID/venv/bin/reasoning-server \
  --data-dir /var/lib/reasoning setup --email owner@example.org \
  --name Owner --organization Mathematics
```

Verify the host key for this connection as well. Then sign in at the configured
HTTPS URL and use organization invitations and provider-key administration.
The installer prints only the setup token file path, never its value. It does
not email owner credentials. Use the host-local [encrypted backup and
recovery commands](backup.md) as the `reasoning` state owner. For example, an
administrator can prepare `/var/lib/reasoning-backups` with owner `reasoning`
and mode 0700, then run the release's `reasoning-server` through `runuser -u
reasoning --` with `--data-dir /var/lib/reasoning` and that private backup directory.

### Configure invitation email during installation

Create your own Resend account, verify the sending domain's required DNS records,
and obtain a sending-only API key scoped to that domain. Both DigitalOcean and
owned-hardware installations use the same optional HTTPS adapter. It requires no
additional inbound port or organization-server SMTP daemon. Provider signup and
sender DNS verification are not performed by `do-launch`.

Prepare a private sending configuration on the operator's computer:

```bash
reasoning-deploy mail-input --sender invites@example.org \
  --output /secure/reasoning-mail.json --daily-limit 100
```

The hidden prompt accepts the sending-only API key. This command creates no
resources, contacts no provider, sends no email, and refuses overwriting the
exclusive 0600 input file. Add `--mail-file /secure/reasoning-mail.json` to
`do-launch`, `do-install`, or owned-hardware `install`, alongside any `--owner-file`.
Use a bundle declaring `invitation-mail-v1` in its `installation_features`; the
commands reject a mail-incompatible bundle before provider access or host changes.
`inspect --bundle FILE` reports the database schema and installation features.
Older bundles remain usable without the new mail installation option.

The operator validates and snapshots the bounded canonical input before paid
requests. DigitalOcean ownership records bind its digest in private
`digitalocean-mail.json` before launch, and must be preserved. Every retry must
include the same input; changing or omitting it is refused. Root bootstrap records
the exact mail/release/URL binding in private `server-mail.json`. The credential
travels only in a private staging file over verified SSH, never in command
arguments, a cloud plan, provider user data, or a public release bundle. The original
operator input is retained for lost-response recovery; the uploaded staging copy
is removed after verified installation. Abrupt termination may leave private
staging files. Do not publish them.

Bootstrap publishes complete 0600 configuration with service-account ownership at
`/var/lib/reasoning/mail.json`, without overwriting existing operator-managed or
changed files. It restarts the organization service and verifies fresh host and
operator HTTPS readiness, including actual email-worker liveness. The secret-free
`mail_setup` receipt contains `format`, `input_sha256`, `configuration_installed`,
and `worker_running`; it proves neither provider credentials nor sender/domain
acceptance nor inbox delivery. Installation never submits a test email; ordinary
queued email can be processed when the service starts/restarts. Test a real
invitation deliberately after onboarding. Configuration failures retain the
original VM/journals for exact retry rather than creating replacement resources.

### Configure invitation email separately on an installed host

From an onboarded operator checkout, use:

```sh
make email-setup
```

The command synchronizes the installed release, refuses unfinished upgrades,
and requires the `existing-mail-setup-v1` installation feature. If absent, run
`make upgrade` first. It opens the Resend domain and API-key pages, asks for a
verified sender and daily limit, and accepts a sending-only key from a protected
file or hidden terminal prompt. No key belongs in chat, command arguments, shell
environment, a cloud plan or a release bundle.

After typed consent, it opens ownership-checked temporary SSH, uses the pinned
server key, records the exact mail/release/URL binding in private
`server-mail.json`, and exclusively creates service-owned 0600
`/var/lib/reasoning/mail.json`. It restarts the service and verifies its managed
unit, exact HTTPS release and live mail worker, then closes SSH in a finally
path. It does not alter the release, database, proxy or service definition.
Existing DigitalOcean backups are accepted without changing their configuration.
The retained Team, resource identities, VM shape, IPv4 DNS, firewall, pinned
SSH trust, and exact installed release must still match. Existing-server Modal
relay setup uses the same provider checks; first-server provisioning remains
subject to its original backup-free plan.

Private inputs and retry intent remain in `DEPLOY_STATE/mail-setup/`. Repeat the
same command after a lost restart/readiness reply; it verifies existing bytes,
never overwrites them, and does not create another credential. Unmanaged files,
changed bindings, removed verified credentials, and uncertain interrupted file
creation are refused for reconciliation. This command is not a rotation routine.
Retain the entire private operator state and sending key off-host; encrypted
database backups exclude the separate mail credential. Recovery preserves the
managed mail binding, and restored invitations/mail are fenced against replay.

No test email is submitted by setup; queued invitations and verification email
can be processed once sending is enabled. Worker readiness does not establish
provider key/domain acceptance or inbox delivery. Test a real invitation
deliberately in organization settings. See
[mail configuration, limits, and failure states](server.md#optional-invitation-email).

## Provision a DigitalOcean compute host

`do-worker-plan` reads the existing organization's HTTPS worker identity and
DigitalOcean account/size/key metadata. It creates no cloud resources, claims no
jobs, and does not change last-seen activity. Prepare the private enrollment and
approved catalog described in the installation section below. Use a distinct
registration and private ownership directory for each worker machine; do not
copy a credential to several hosts or create new plans to escape uncertain
receipts. The provider token needs `account:read`, `sizes:read`, and
`ssh_key:read` for planning, but no DNS-zone scopes or delegation.

```bash
reasoning-deploy do-worker-plan --token-file /secure/digitalocean-token \
  --server-url https://chat.example.org \
  --registration /secure/worker-registration.json \
  --catalog /secure/approved-images.json \
  --name mathematics-cpu --region fra1 --size c-4 \
  --ssh-key-id 123456 --admin-cidr 203.0.113.10/32 \
  --maximum-hourly-usd 0.50 --output /secure/reasoning-deploy/worker-plan.json

reasoning-deploy do-worker-apply --token-file /secure/digitalocean-token \
  --server-url https://chat.example.org \
  --registration /secure/worker-registration.json \
  --catalog /secure/approved-images.json \
  --plan /secure/reasoning-deploy/worker-plan.json \
  --state-dir /secure/reasoning-deploy/worker-ownership \
  --confirm-plan WORKER_PLAN_ID_FROM_DO_WORKER_PLAN --wait-seconds 60
```

Replace the sample size and price ceiling with an available plan and your own
limit. The CLI reads live prices; the example ceiling is not a quoted rate.
Selected CPU/RAM must fit the enrollment's full allowance plus 256 MiB RAM,
and the allowance must fit each selected image's approved maxima. Metadata does
not prove real daemon capacity, kernel enforcement, image availability, or
execution readiness; installation checks those prerequisites separately. Boot
disk is at least 10 GiB, but image layers, package installation, and private job
output need additional operator-sized headroom. The compute quote excludes
tax/storage/transfer/other services and is not a total invoice or monthly cap.

The saved private canonical plan binds the provider account, public SSH key,
shape, administrator CIDR, quote, existing server URL, worker/organization IDs,
resource allowance, and hashes of normalized enrollment and selected catalog.
It contains neither the worker nor provider token. Different input formatting
or unused catalog records do not change that binding; changed selected data or
credentials are refused. Omit `--output` for a readonly unsaved plan; applying
requires the saved file and exact plan confirmation.

Apply additionally needs `droplet:read`, `firewall:read`, and `tag:read`, plus
`droplet:create`, `firewall:create`, and `tag:create`.
It creates only a unique compute tag, tag-scoped SSH-only firewall, and separate
Ubuntu 24.04 amd64 CPU Droplet. TCP 22 is restricted to the administrator CIDR;
no public 80/443, application ports, or DNS records are created. Outbound rules
match the server provisioner's package/DNS/HTTPS/time access. Cloud-init contains
public provisioning identity and SSH-password disabling; opt-in host-key pinning
also includes that host's private SSH key. It never includes enrollment,
provider tokens, registry authentication, or organization keys.

Account/key/resource/availability/price drift is checked before creation; a higher
quote than the confirmed price is refused. Remote enrollment is checked before
provider requests and immediately before each creation, including the billable
VM. Revoked, unavailable, or mismatched credentials stop further creation but do
not delete resources already charged. There is still a race between authority
checks and external creation: installation independently verifies enrollment
before service activation. Drain an enrollment if queued work must not start.

The worker journal follows the server provisioner's exclusive locking,
fsynced intent, lost-receipt reconciliation, refusal of unmanaged/duplicate/changed
resources, and no blind uncertain retries. Server and worker plans/journals are
not interchangeable. Keep one saved plan/journal on one operator host and resume
it; do not run copied journals from different machines. Missing owned resources
are not recreated, and failures can leave billable VMs. There is no automatic
cloud deletion or rollback. An unavailable enrollment does not stop existing
provider charges; use the recorded IDs and provider console for reconciliation.

Readiness requires an active public-IPv4 VM and its exact applied SSH-only
firewall. `firewall_pending` and `droplet_pending` mean resume the same inputs.
`ssh_verification_required` supplies the address/console URL and reports
`worker_installation_verified: false` and `container_execution_verified: false`.
It does not assert whether an already-provisioned host has subsequently been
installed. For manual-verification plans, independently verify the SSH host key, then run
[`do-worker-install`](#install-an-owned-digitalocean-worker) with the original
ownership journal. These standalone commands do not scale automatically; use the
confirmed controller below for queue-driven actions among prepared plans.
More workers do not distribute a single computation.

### Initialize pinned worker SSH identities

Add `--pin-ssh-host-key` to `do-worker-plan` to authorize initializing a unique
Ed25519 host key through DigitalOcean user data. This is part of the saved plan
and changes its confirmation ID; an existing manual-verification plan is not
silently converted. Applying a pinned plan durably creates private
`digitalocean-ssh.json` in its original ownership directory before recording the
paid VM request. Cloud-init installs that exact key instead of generating other
host keys. After provider readiness, the result is `ssh_key_pinned`, not proof
of a successful SSH connection or installed worker.

`do-worker-install`, `do-worker-retire`, and controller host callbacks use a
temporary protected known-hosts file containing only the original public key
and the freshly checked owned VM address. They ignore a supplied manual file
for pinned plans; `--known-hosts` may be omitted. SSH/SCP use strict verification,
disable global/config/DNS/command-based alternate trust, and do not learn or
update keys. Unknown or changed keys fail. First-boot SSH can be unavailable or
still present another key before cloud-init finishes; retry the same original
operation and journals, never disable verification. Self-hosted machines and
manual-verification plans still require independently verified keys.

**Trust boundary:** this option trusts the authenticated DigitalOcean control
plane to deliver the key. DigitalOcean exposes user data through the VM's
[metadata API](https://docs.digitalocean.com/reference/api/metadata/droplet-properties/),
and cloud-init retains local provisioning data. Anyone who can read those data
can impersonate that host. Use dedicated trusted compute hosts; do not permit
untrusted local processes or workloads to access host metadata. Job containers
use no network, but that is not proof against a container/kernel escape.
If this provider/metadata exposure is unsuitable, omit the option and verify a
host-generated key independently. This is not provider-independent attestation.
The key configuration follows cloud-init's
[SSH module](https://cloudinit.readthedocs.io/en/latest/reference/modules.html#ssh).

Preserve the original plan, ownership state, and private SSH identity journal
together on one operator host. Recovery reuses the exact key; a missing or invalid
key after VM intent stops rather than generating a replacement. Keep it out of
source control and logs. A new deployment gets a new key; these commands do not
rotate an existing host's key or retrofit a VM. Provider charges can continue
when key recovery or verification fails.

## Recoverably enroll workers with scoped authorization

In **Administration → Compute → Scoped compute automation**, an owner
or administrator creates a named scaler grant with selected approved images,
all six resource maxima, a one-to-64 worker ceiling, and a one-to-365-day
lifetime. The maxima must fit every selected image. Copy the one-time `rns_`
credential into an owner-only private token file. Browser storage never retains
it. A lost grant creation response is not retried automatically: inspect the
grant list and rotate or revoke the exact inaccessible grant before creating
another. Rotation keeps the grant ID, immutable bounds, and worker request
namespace, invalidates its old token, and makes the rotating administrator its
sponsor. Revocation is irreversible. At most eight current grants are allowed.

The credential reads tenant-wide resource/queue metadata and enrolls workers
within its image/resource/count bounds. It has no chat, provider-key, human
administration, cloud API, or worker protocol access. **Enrollment delegates
trusted execution:** the worker credentials it issues can claim compatible
jobs and read their assigned programs. Give this credential only to your
trusted compute controller, never to a job container or untrusted bot. The
grant ceiling includes revoked workers with running/unknown jobs until settled;
it is not a provider spending cap. Grant expiry/revocation or sponsoring human
administrator demotion/removal disables scaler access, but does not revoke
existing workers, stop containers, settle jobs, or delete hardware.

Create a private `resources.json` containing all six integer limits, for example:

```json
{"cpu":1,"memory_bytes":1073741824,"disk_bytes":268435456,"pids":128,"wall_time_seconds":120,"artifact_bytes":1048576}
```

Use actual approved image IDs and resource allowances. In a protected operator
directory, run:

```bash
reasoning-deploy worker-enroll --server-url https://chat.example.org \
  --organization-id ORGANIZATION_ID --scaler-token-file /secure/scaler-token \
  --name algebra-worker-1 --image-id APPROVED_IMAGE_ID \
  --resources-file /secure/resources.json \
  --state-dir /secure/worker-1-enrollment \
  --registration-output /secure/worker-1.json \
  --catalog-output /secure/worker-1-catalog.json
```

This command creates no cloud resources and claims no jobs. It validates the
scoped identity and approved catalog, fsyncs a private original request nonce
and client-generated `rnw_` worker credential before its enrollment POST, and
saves the same worker receipt and selected catalog into exclusive 0600 files.
The server stores credential hashes and a request mapping, not the plaintext
worker credential, and returns metadata only. Enrollment accepts at most 30
attempts per grant per rolling minute, including conflicting/rejected attempts.
New workers also require currently organization-allowed images and installation
image maxima; ordinary organization enrollment limits still apply.

After a lost response or local output-write failure, rerun the **exact command**
with its original directory and output paths. The same nonce and credential
recover the original worker; changed arguments, catalog, grant ID, or existing
output bytes are refused, not overwritten. A rotated token for the same grant
can resume. Expired/revoked grants need valid administrator direction; revoked
or retired worker nonces never create a replacement. Preserve this private
enrollment directory along with registration/catalog files. Do not use a cloud
ownership or fleet-budget directory as enrollment state. A saved receipt without
its original intent is not authority for a new enrollment.

Use the resulting files with `do-worker-plan`, `install-worker`, or a worker on
your own hardware. Enrollment alone does not install, provision, or activate a
host. Confirmed fleet amendment and a queue-driven prepared-fleet controller
are available below. New worker plans can opt into provider-initialized SSH
pinning. The [worker pool](#run-a-replenishing-worker-pool) combines fresh scoped
enrollment, template-bound planning, fleet amendment, and lifecycle execution.

## Plan queue-aware worker capacity

`do-worker-fleet` saves a private bounded fleet of original enrolled worker plans.
Each slot has a distinct worker enrollment, cloud deployment ID, and ownership
directory in the same organization and DigitalOcean account. Register workers,
save their one-time private registrations/catalogs, and create `do-worker-plan`
documents first. A slot may be unprovisioned or already have its original
provisioning journal; no credential is shared across machines.

```bash
reasoning-deploy do-worker-fleet --server-url https://chat.example.org \
  --organization-id ORGANIZATION_ID --minimum-workers 0 --maximum-workers 4 \
  --maximum-hourly-usd 0.50 --idle-seconds 300 \
  --slot /secure/worker-plan.json /secure/worker-registration.json \
    /secure/approved-images.json /secure/worker-ownership \
  --output /secure/fleet.json

reasoning-deploy do-worker-scale-plan --fleet /secure/fleet.json \
  --session-file /secure/admin-session-token \
  --token-file /secure/digitalocean-read-token \
  --output /secure/scaling-decision.json
```

Repeat `--slot PLAN REGISTRATION CATALOG STATE_DIR` for each candidate/owned
machine. The builder validates original private inputs before creating its 0600
canonical JSON document; it neither registers workers nor contacts the provider.
Plans, registrations, catalogs, and state paths are stored as absolute paths.
Choose a new output filename for a revised fleet or decision; files are not
overwritten. Fleet limits allow 0–64 machines, a nonnegative decimal-string
hourly ceiling, and a 60–86400 second idle threshold. The warm minimum cannot
exceed the maximum; a minimum that exceeds available slots is reported unmet.
The builder prints `fleet_sha256` for exact amendment confirmation. Each
definition is immutable; save revisions to separate protected files.

The `--session-file` contains either a current scoped `rns_` scaler credential
or a current owner/admin bearer access token, not a login response, password,
provider key, or worker token. Prefer the scoped credential above for automation;
the human session is available through the bearer login described in
[server setup](server.md). Keep either private and renew/rotate when expired.
The DigitalOcean token needs the planning/read
inspection scopes above, including Droplet/firewall/tag reads; no create/delete
scopes are needed. The command uses fixed HTTPS GETs without redirects or
environment proxies and never prints either credential.

The planner validates live account, key, size/resources, and prices against each
saved plan, then inspects exact owned resource IDs and complete marker discovery.
Missing or changed identities, duplicates, unmanaged marker matches, and
disagreeing reads are not adopted. Pending creation receipts reserve a full
declared VM rate even if only free resources are currently visible. Retirement
intent prevents reuse of that slot. Existing journals are never rewritten,
created, or discarded by a planning command.

The organization snapshot is read after provider inspection and must be no more
than 30 seconds old, allowing at most five seconds of clock skew. It is a single
administrator/scaler read transaction, not a job claim or settlement. Job metadata contains
requirements, pinned references, runnable policy state, and assigned-worker/
lease timing, but no programs, logs, artifacts, or attempt/worker credentials.
Reads do not renew leases, change last-seen activity, expire attempts, or settle
policy-blocked jobs. Running and unknown jobs occupy concurrency slots even if
their leases have expired.

Matching respects each job's full resource profile and enrolled images. Each
worker can cover only one job concurrently: summed CPU across smaller machines
does not satisfy one larger computation. Current policy/catalog/quota and
requester permissions must still permit queued jobs. Only non-draining,
non-revoked workers with a poll/heartbeat within 45 seconds count as ready.
Unmanaged workers can cover compatible demand but are never retirement targets
and their provider costs are not included. A closed managed VM is not counted as
capacity merely because an old server poll remains recent.

Recommendations use exact decimal VM quotes, maximum worker count, a warm
minimum, and the organization concurrency ceiling. Provisioning, retiring,
stale, revoked, changed, or uncertain managed slots put recommendations on hold
via `reconcile_workers`; they do not trigger blind paid replacements. Idle
retirement uses time since enrollment/latest completed assigned job, not time
since the last poll. It preserves needed concurrency and the last compatible
machine for each queued requirement, even while global concurrency is full.
Provision and retirement recommendations are not mixed in one decision.

The result is explicitly `mode: advisory`, `creates_resources: false`, and
`destroys_resources: false`. It includes exact worker/plan IDs, current/projected
reserved owned VM rates, uncovered demand, unmet warm minimum, and an expiring
decision ID bound to fleet, server snapshot, and provider inventory hashes.
These are observations and recommendations, **not an execution
authorization, physical readiness proof, account-wide cost guarantee, or durable
fleet spending enforcement**. Provider and server observations are not one
cross-system transaction; external administrators/actions can race them. Taxes,
overages, storage, chat servers, and unmanaged resources are excluded.

Apply a selected original worker plan explicitly using `do-worker-apply`, use its
original pin or independently verify the new host key, and install its exact enrollment/release, or use the managed
fleet action path below. Execute retirement
only through the separately confirmed `do-worker-retire` path with fresh host
checks and VM/data consent. An advisory decision file cannot be passed as a
standalone creation/deletion plan. The confirmed controller schedules prepared
worker lifecycles; automatic new enrollment remains unimplemented.
Self-hosted workers need none of these cloud commands.

## Execute a budgeted fleet action

`do-worker-scale-apply` creates or resumes **one** explicitly confirmed worker
under a durable fleet-wide worker-count and quoted-hourly-VM ceiling. It does
not install workers, perform SSH connections, drain jobs, or delete resources.
The fleet uses the same original enrollment, catalog, plan, and ownership paths
as the planner. The coordinator directory must be an explicit absolute private
path, separate from every worker ownership directory; worker directories cannot
nest inside one another.

```bash
reasoning-deploy do-worker-fleet-reconcile --fleet /secure/fleet.json \
  --fleet-state-dir /secure/fleet-state \
  --token-file /secure/digitalocean-read-token

reasoning-deploy do-worker-scale-plan --fleet /secure/fleet.json \
  --session-file /secure/admin-session-token \
  --token-file /secure/digitalocean-read-token \
  --output /secure/fresh-decision.json

reasoning-deploy do-worker-scale-apply --fleet /secure/fleet.json \
  --fleet-state-dir /secure/fleet-state \
  --session-file /secure/admin-session-token \
  --token-file /secure/digitalocean-create-token \
  --decision /secure/fresh-decision.json --confirm-decision DECISION_ID \
  --confirm-worker WORKER_ID --confirm-plan PLAN_ID
```

The creation token needs worker provisioning scopes described above; the admin
session and provider token stay in separate private files. A new action requires
the exact hash of a provision recommendation that has not reached its 30-second
expiry, plus that action's original worker/plan IDs. Local confirmation failures
precede credential/provider access and coordinator changes. Fresh provider and
server observations must still recommend this worker; recommendations are not
authority to ignore changed demand, enrollment, policy, prices, or budget.
Demand and durable ceilings are rechecked before each creation POST, including
the paid VM after free tag/firewall creation. Cross-system observations still
cannot prevent an external administrator racing the final check.

The private `fleet-budget.json` binds the active complete fleet, slot paths,
account identities, plans, and VM quotes. Its exclusive lock serializes managed
creation. Each slot receives a `fleet-binding.json` and original provisioning
journal; ordinary `do-worker-apply` refuses bound slots without the live
coordinator capability. Preserve **all** of these files together, including
`digitalocean.json` and retirement journals. Never discard, edit, or relocate
them to evade a reservation. Amendments preserve birth namespaces, unchanged
ownership, and historical generation snapshots. Use the explicit amendment
command below; changing a fleet file or selecting a second coordinator does
not amend its budget.

A full VM reservation is saved before the first creation POST, even when only
free resources have been created. Lost responses, timeouts, rejected requests,
read failures, and idle time do not release it by themselves. Confirmed terminal
unpaid cancellation or complete verified retirement is required. `do-worker-fleet-reconcile`
performs provider reads and local journal writes, not cloud mutations; it reports
reservation IDs and totals. Existing original provisioning intents are counted
and given an adoption reservation without granting new creation consent. A
fully retired slot releases its reservation only after its bound complete
retirement/proof journal and fresh exact-ID plus marker-discovery absence for
all three owned resources agree. Partial or uncertain retirement retains the
full quote. Closed or cancelled slots cannot be reused as new enrollments or replacement VMs.

To resume a saved intent, obtain its `reservation_id` from reconciliation and
confirm the same worker/plan:

```bash
reasoning-deploy do-worker-scale-apply --fleet /secure/fleet.json \
  --fleet-state-dir /secure/fleet-state \
  --session-file /secure/admin-session-token \
  --token-file /secure/digitalocean-create-token \
  --resume-reservation RESERVATION_ID \
  --confirm-worker WORKER_ID --confirm-plan PLAN_ID
```

An old decision does not authorize another reservation. A free-resource-only
resume must still satisfy current admissible demand or the usable owned-worker
warm minimum. Other unfinished reservations continue to consume budget but do
not supply capacity or deadlock this one-worker recovery. Only healthy,
non-draining, non-revoked, recently polled active owned workers count toward the
warm minimum; healthy unmanaged workers may cover queued demand but not this
owned-fleet minimum. Running and unknown jobs still consume concurrency,
including attempts with expired leases. Recovery cannot replace a worker that
has already polled, has an assigned attempt, or has changed enrollment/catalog
requirements. Every creation remains subject to the full durable budget.
Fresh unreserved growth still follows the advisory planner's fleet hold rules.
An already-started VM request is
reconciled under its original intent without blindly retrying an uncertain POST.
Provider readiness may require repeating this exact resume command. The planner
itself does not read coordinator reservations: an empty-but-reserved ownership
journal may still appear in its advice, but the action executor refuses a new
reservation and requires this resume path. After readiness, use the original
pin or independently verify the host key, then use `do-worker-install` with the original worker ownership
directory. Retirement remains separately confirmed
through `do-worker-retire`, followed by budget reconciliation.

Ceilings cover reserved VM quotes for this explicit fleet, not a total cloud
invoice or monthly spending guarantee. Existing costs above the ceiling are
reported and cannot authorize more creation. Unmanaged machines, the chat
server, taxes, storage, backups, and bandwidth overages are excluded. These
guards coordinate this CLI; an operator with account/root access can still
create resources directly or destroy the local state. These manual commands do
not loop; the separately authorized controller below can provision/install/drain
prepared slots, using original provider-initialized pins or independently verified
host keys. The separately confirmed pool can prepare fresh generations.
Journal-history compaction is not implemented; fleet amendment itself does not
enroll workers or plan new hosts.

## Amend a managed worker fleet

Use `worker-enroll` and `do-worker-plan` to prepare fresh candidate workers, then
save a **separate** next definition with `do-worker-fleet`. Retain every unresolved
slot with the exact original normalized enrollment, selected catalog, cloud plan,
and input/ownership paths. You can change the warm minimum, worker/count cost
ceilings, idle threshold, and slot list, but not the organization, HTTPS origin,
provider account, retained plans, or existing worker identity. The next worker
and hourly ceilings must cover all still-reserved original VM quotes.

To remove a slot, first complete confirmed unpaid cancellation or full verified
paid-host retirement and reconcile its budget. Only terminal `cancelled` or
`closed` slots with fresh exact-ID and marker-discovery absence may be removed.
An unreserved unused slot must remain in the definition; amendment does not
implicitly cancel it. New slots must be unbound with no cloud creation intent.
Never recycle an archived worker ID, cloud plan/deployment ID, or ownership
directory. New workers require fresh enrollment, saved plans, and private paths.

The original coordinator must exist. Use the current/next `fleet_sha256` values
printed by the fleet builder and confirm both:

```bash
reasoning-deploy do-worker-fleet-amend \
  --fleet /secure/fleet.json --next-fleet /secure/fleet-next.json \
  --fleet-state-dir /secure/fleet-state \
  --confirm-fleet CURRENT_FLEET_SHA256 \
  --confirm-next-fleet NEXT_FLEET_SHA256 \
  --token-file /secure/digitalocean-read-token
```

This uses provider inspection/read scopes, including the exact owned-resource
and visibility checks above; it does **not** POST, PATCH, or DELETE cloud
resources, register/revoke workers, claim jobs, install hosts, or acquire SSH
keys. It holds the original coordinator lock and old/new ownership locks, checks
live ownership/availability/prices, preserves original reservation IDs and
per-slot birth fleet hashes, and snapshots each historical generation. Pending
paid VM requests remain reserved and can resume under the next definition with
their original reservation IDs, never a replacement request.

A private `fleet-amendment.json` is fsynced before new bindings or budget
activation. While its phase is prepared, ordinary coordination/provisioning
with either definition refuses. Interrupted binding, ledger, or completion
writes must resume this exact command with the same two definitions, directory,
target path, and confirmations. The saved transition and original or completed
ledger checksum determine recovery; changed state is refused, not overwritten.
Once applied, repeating this amendment returns its metadata; the superseded
definition cannot coordinate again. Use `--fleet /secure/fleet-next.json` for
all following planning, reconciliation, action, and cancellation commands, with
the **same** coordinator directory. Current scoped/server/worker credentials and
fresh paid-action confirmations are still separately required.

Keep the original and current fleet files, all historical input/ownership/
retirement/cancellation files, and the coordinator together. Missing or altered
history/transition records cannot reset an amended ledger, even with no active
reservations. Historical normalized inputs are verified against their saved
scopes; old worker bindings and cancellation receipts retain original birth
namespaces. Removed terminal slots stay recorded but do not consume capacity or
VM budget. The protected journal limit is 256 KiB and at most 128 historical
generations; an oversized amendment refuses without truncating history or
discarding reservations. Automatic compaction and never-reserved unused-slot
cancellation are not implemented. A running controller does not follow a fleet
amendment implicitly: authorize its next policy explicitly as described below.

### Cancel an unpaid worker setup

`do-worker-fleet-cancel` irreversibly stops a specific original reserved slot
**before any paid VM request has started**. It deletes only its exact owned
firewall and unused tag, then releases its VM reservation after fresh absence
checks agree. It cannot DELETE a VM, settle jobs, revoke enrollment, or discard
unknown paid creation. It requires the original private fleet/coordinator and
exact worker, plan, and reservation IDs; no new advisory decision is involved.

```bash
reasoning-deploy do-worker-fleet-cancel --fleet /secure/fleet.json \
  --fleet-state-dir /secure/fleet-state \
  --token-file /secure/digitalocean-delete-token \
  --confirm-worker WORKER_ID --confirm-plan PLAN_ID \
  --confirm-reservation RESERVATION_ID
```

The provider token needs account, Droplet, firewall, tag, image, volume,
volume-snapshot, and database read visibility, plus firewall/tag deletion scopes.
There is no Droplet-delete requirement. The worker's normal enrollment may
already be revoked: this path relies on the original local intent and provider
ownership, not permission to run another job. Revoke unused enrollment through
organization administration separately.

Under both coordinator and worker locks, cancellation records a private
`fleet-cancellation.json` terminal intent before deletion. Every provisioning
path refuses that slot thereafter, including if no provider resource is yet
visible. Original provisioning and budget journals remain intact. VM state must
stay exactly `not_started` with no matching VM; `request_pending`, a known VM,
retirement intent, account changes, duplicate markers, unmanaged resources, or
unrelated firewall members refuse cancellation. Empty discovery never proves
that a paid request was not sent.

Cancellation handles original free-resource receipts without issuing another
creation POST. An unknown free creation keeps the reservation and reports
`free_creation_uncertain`; retry this same cancellation when the original
resource becomes visible. Waiting firewalls remain pending until their rules
are ready. Before deleting the tag, it verifies firewall absence, zero known
association counts, and complete broader read visibility. Unknown associations
or denied inspection preserve the tag.

Deletion acceptance is not absence. Lost responses retain a durable pending
DELETE and reservation. Ordinary repetition performs reads without blindly
reissuing that DELETE. If the original exact resource remains present, an
operator may explicitly repeat only the current pending request using
`--retry-delete firewall` or `--retry-delete tag`; scope/ownership/absence checks
run again. A definitive rejection records that no deletion was accepted and
allows the same confirmed cancellation to retry. A reappearing cancelled
identity is not automatically deleted.

Only complete, bound cancellation plus fresh exact/discovery absence releases
the reservation to terminal status `cancelled`. Partial or uncertain cleanup
keeps the full quote. Read-only planning excludes a completed cancelled slot;
partial cancellation holds new growth. Cancellation and paid-host retirement
are distinct workflows. Cancelled and retired identities are not recyclable;
an explicit fleet amendment can archive terminal slots and add fresh enrollments.

## Run a bounded worker controller

The controller executes queue-driven lifecycle actions within an explicitly
prepared fleet. First enroll workers, save their DigitalOcean plans, and build
the bounded `do-worker-fleet` definition. Preserve each original ownership
directory and the original fleet budget directory. Supply a verified public
release and a private SSH identity. Supply a protected known-hosts file containing
independently verified keys for manual plans; it may be omitted only when every
prepared plan enables provider-initialized pinning. The same Python release runs the organization
server and worker protocol; a policy is not an upgrade mechanism.

Plan locally, then separately authorize the exact printed `policy_sha256`:

```bash
reasoning-deploy do-worker-controller-plan \
  --fleet /secure/fleet.json --fleet-state-dir /secure/fleet-state \
  --bundle /secure/reasoning-release.tar.gz \
  --identity /secure/operator-ssh-key --known-hosts /secure/verified-known-hosts \
  --allow-provision --allow-retirement --confirm-delete-host-data \
  --output /secure/controller-policy.json

reasoning-deploy do-worker-controller-run \
  --policy /secure/controller-policy.json --confirm-controller POLICY_SHA256 \
  --token-file /secure/digitalocean-token --session-file /secure/scaler-token \
  --cycles 10000 --interval-seconds 15
```

Planning has no provider access or cloud writes and exclusively creates a
private immutable policy. It binds the original fleet definition, every slot's
plan/input/ownership scope, budget directory, release/bundle identity, SSH paths,
and private SSH identity hash. Read the policy before authorizing it: running
permits root installation on its owned worker hosts. `--allow-provision` permits
new paid creation within the fleet's declared worker-count and hourly VM quote
ceilings. `--allow-retirement` requires `--confirm-delete-host-data` and permits
idle VM/local-data destruction and unpaid setup cleanup. Both permissions default
off; **running with them off is not a read-only probe**. It may install an existing
VM or reconcile an original pending paid creation without making another paid
request. Use the advisory planner for read-only decisions.

The token needs the creation/read scopes of the existing worker provisioning
path and, when cleanup is enabled, the exact deletion/visibility scopes of
[worker retirement](#destroy-a-provisioned-digitalocean-worker). The session file
holds a current organization-scoped scaler credential or owner/admin bearer
session. Provider/session credentials authenticate only their fixed HTTPS APIs;
neither is forwarded to worker hosts, job containers, or printed metadata.
Only the original worker enrollment goes to its host, never to job containers.

Each cycle holds the original global budget lock through one worker action and
also takes the selected worker ownership lock during installation or cleanup.
The private `fleet-controller.json` records the confirmed policy, completed cycle
count, and bounded last-action metadata. Per-worker provisioning/installation/
retirement journals, not a controller log, provide durable operation recovery.
Cycles prioritize original pending retirement/cancellation, incomplete provisioning
reservations, and installation of active VMs before fresh growth or idle selection.
New creation uses the same durable reservation and live budget/demand capability
as the manually confirmed action path; queue/policy changes before a paid request
can prevent it even after free resources were created.

Installation checks the owned resources, trusted SSH host, root provisioning
marker/metadata identity, and exact fresh receipt. A completed installation is
not usable capacity until a fresh matching server worker poll appears. Stale,
revoked, draining, mismatched, or otherwise unresolved workers hold decisions;
the controller does not replace them or restart an already active worker simply
because a poll or response is missing. Idle selection preserves the warm minimum
and compatible queued demand, rechecks the queue before committing drain, and
uses the full existing host retirement/cleanup proof before exact VM deletion.
Busy/unknown jobs can leave retirement draining across cycles without stopping
the service. A reserved unpaid setup whose demand disappears can be cancelled;
a pending paid request can never use that cancellation path.

Uncertain POSTs/DELETEs are never blindly retried. A lost installation receipt
resumes the exact host inputs without reinstalling an active service. Present
resources with pending deletion produce a pending result and retain their full
reservation; use the explicit original manual retry/reconciliation command when
required. Budgets release only after bound terminal proof and fresh exact resource
absence. Closed/cancelled slots cannot be recycled. `prepared_slots_exhausted`
requires fresh enrollment/planning and a confirmed fleet amendment;
`capacity_or_budget_unmet` means prepared capacity or limits do not cover current
demand. Neither condition authorizes resources beyond the policy.

The run defaults to one cycle. `--cycles` accepts 1–10000 and `--interval-seconds`
accepts 10–60; each completed cycle prints one secret-free JSON result. Any error
stops the loop. Repeat the same confirmed command under an operator-managed timer
or supervisor if desired. Exiting, stopping the controller, credential expiry,
or an error does not stop provider billing or settle running computations.
Hourly ceilings are reserved VM quotes, not a monthly/total cloud spending cap;
other services and account-external actions remain outside this budget.

To change fleet limits/slots, first complete the explicit fleet amendment using
the same original budget directory. Save a separate next controller policy with
that current fleet and the same release. Run it with both
`--confirm-controller NEXT_POLICY_SHA256` and
`--replace-controller CURRENT_POLICY_SHA256`. Exact explicit replacement retains
the original budget, per-slot birth namespaces, reservations, and release. It
does not follow amended definitions or edited policies implicitly. Policy
replacement cannot change the original budget directory or installed release;
host upgrades require separate tooling. Keep all historical private inputs and
journals; do not delete them to bypass checks.

Pinned plans reuse their original provider-initialized keys; manual plans require
independent verification. The controller cannot trust an unknown SSH key just
because a Droplet exists. An independently verified new entry may be added to
the protected known-hosts file for a manual plan, then the same run resumed.
This controller manages a finite prepared fleet. Use the separately confirmed
pool below for fresh enrollment/planning and generation activation.
Real DigitalOcean/SSH/systemd/
Docker/CAS controller operation remains unverified; fixture success does not
prove physical isolation, algebra results, or a production deployment.

## Run a replenishing worker pool

`do-worker-pool-plan` authorizes fresh worker generations around an original
controller policy. Prepare the original fleet and save its controller policy
first. Select one or more original worker IDs as templates: their approved image
catalog, execution-resource allowance, DigitalOcean account/SSH key, region,
size, administrator CIDR, and confirmed VM quote bound every new worker. Original
plans/catalogs/registrations remain necessary even after those hosts retire.
Use a separate private pool directory, outside the budget and original worker
ownership directories. Planning is local and neither enrolls workers nor creates
cloud resources. Keep the saved policy file outside the pool preparation directory;
the run creates and retains its own journal there.

```bash
reasoning-deploy do-worker-pool-plan \
  --controller-policy /secure/controller-policy.json \
  --pool-state-dir /secure/worker-pool \
  --template-worker ORIGINAL_WORKER_ID --scaler-id ORIGINAL_SCALER_GRANT_ID \
  --maximum-new-workers 32 --pin-ssh-host-key \
  --output /secure/pool-policy.json

reasoning-deploy do-worker-pool-run \
  --policy /secure/pool-policy.json --confirm-pool POOL_SHA256 \
  --token-file /secure/digitalocean-token \
  --scaler-token-file /secure/scaler-token --cycles 10000 --interval-seconds 15
```

Read the whole policy before confirming its hash. It embeds the original
controller policy, requires that policy's paid-growth permission, preserves its
retirement/data-destruction permissions, and binds the scoped scaler ID and
template IDs. `--maximum-new-workers` accepts 1–64 and caps fresh enrollments
over this policy's lifetime, **not just concurrently running VMs**. The original
worker-count and aggregate hourly-quote caps still govern every paid request.
The same public release, budget directory, SSH identity, organization, URL, and
fleet limits cannot change through pool generations. A template price increase,
account/key/resource drift, changed approval catalog, expired/revoked grant, or
changed administrator sponsor stops progress. Rotation of the same scaler grant
can resume with its updated private token file; another grant ID cannot replace it.

Every new host uses provider-initialized SSH pinning, requiring explicit
`--pin-ssh-host-key` consent when planning the pool. Understand the
[provider/metadata trust boundary](#initialize-pinned-worker-ssh-identities).
Original manual-verification hosts still use the initial policy's protected
known-hosts file. New credentials are transferred only to their owned host, never
to job containers; the scoped scaler and DigitalOcean tokens remain on the
operator machine and authenticate only the existing fixed HTTPS APIs.

Each cycle retains the pool lock and performs one existing controller action
or one preparation/activation step. The original global budget and per-worker
locks still cover resource operations. A fresh worker is considered only after
the existing controller has no selected action, capacity or warm minimum is
unmet, no original uncertainty/readiness holds growth, an authorized template
can improve compatible capacity, and a VM can fit the original count/quote caps.
Multiple selected templates let the pool choose compatible resource classes;
it cannot invent larger hardware or distribute a single computation. Existing
prepared capacity is used before fresh enrollment.

Before any new credential is minted, private `fleet-pool.json` records the
original pending generation and template. Each `worker-NNNN` directory retains
the recoverable enrollment intent/receipt, registration, selected catalog,
immutable cloud plan, and separate `ownership` directory. Partial/lost server
responses recover the original nonce and token. An existing plan is never
overwritten or regenerated. A fresh `fleet-NNNN.json` retains every unresolved
slot and removes only terminal slots whose exact resources are freshly absent
through the existing fleet-amendment protocol. Original reservation IDs, birth
namespaces, ownership journals, and history remain intact.

The pool then activates a derived controller policy that changes only the fleet
file and exact fleet/scope hashes. It records the previous policy and uses the
existing confirmed replacement mechanism; it does not accept unrelated policy
edits or follow operator amendments implicitly. Lost amendment/activation replies
resume the same source/target pair and generation. Fresh enrollment/amendment
reports `action: replenish` without creating cloud resources. Later cycles reserve
the VM budget, provision, install, and await matching worker polls through the
existing lifecycle controller. Withdrawal of demand during preparation can leave
an unused enrollment; paid creation still rechecks current demand and caps.

`fleet-pool-binding.json` binds the original budget to this pool authority.
Preserve it, `fleet-pool.json`, every generation file/private input, the controller
journal, and the original budget/ownership histories together on one operator
host. Missing or changed journals are not permission to reset counters, adopt
other fleets, recycle credentials, or forget unresolved billing. Do not run
copies on different machines or delete state to bypass a limit. Do not drive the
same budget with competing pool policies or manually alter its current generation.

The run defaults to one cycle and accepts 1–10000 cycles with 10–60 seconds
between them. Each completed cycle prints one secret-free JSON result; any error
stops the loop. Repeat the same confirmed policy under an operator-managed timer
or supervisor if desired. `authorized_fresh_enrollment_limit_reached` requires
operator intervention, not automatic counter renewal. Pool-policy replacement
and fleet-history compaction are not implemented. The existing private journal
size and history bounds can also stop an operation; preserve its pending intent.
Scoped enrollment limits are independent: unrevoked or still-busy workers occupy
grant capacity, including unused cancelled setups. Revoking those credentials
requires separate administrator direction; the pool does not implicitly revoke
workers or settle jobs. Exiting, errors, or credential expiry do not stop cloud
billing. Hourly quotes are not a total/monthly cloud invoice cap.

This command is DigitalOcean orchestration, not a prerequisite for self-hosted
workers on existing hardware. Actual cloud/bootstrap/container behavior and the
complete public organization journey remain unverified on the development host.

## Install a separate compute host

Create a dedicated fresh Ubuntu 24.04 amd64 machine with systemd, root SSH,
Python 3, cgroup v2, and outbound DNS/package/HTTPS access. This machine can be
a `do-worker-apply` Droplet, manually provisioned rented server, or owned hardware. Its Docker
daemon must provide CPU, memory/swap, and PID enforcement, built-in seccomp,
and enough CPU/RAM for the entire enrollment allowance, including the worker's
256 MiB RAM margin. Choose an allowance that also fits every enrolled image's
approved maxima. This command does not create a Droplet, change DNS/firewalls,
or resize a machine. Workers connect outbound to the organization's HTTPS URL;
they do not need their own public web hostname or open chat ports.

Register the worker in **Administration → Compute** and save its one-time
enrollment JSON in an operator-owned 0600 file under a protected private directory.
Prepare a private 0600 copy of the operator-approved image catalog described in
[compute](compute.md). Select verified published registry digests, not example
pins. The installer selects only the enrolled image IDs; it does not infer or
change server/organization image approval. Registry images must be accessible
without authentication: the installer uses an empty Docker credentials file and
does not upload registry or OpenRouter keys. Independently verify this machine's
SSH host key as described above.

```bash
reasoning-deploy inspect --bundle reasoning-release.tar.gz
reasoning-deploy install-worker --bundle reasoning-release.tar.gz \
  --server-url https://chat.example.org \
  --registration /secure/worker-registration.json \
  --catalog /secure/approved-images.json --ssh-host 203.0.113.30 \
  --identity /secure/reasoning-ssh-key --known-hosts /secure/reasoning-known-hosts \
  --confirm-release RELEASE_ID_FROM_INSPECT \
  --confirm-worker WORKER_ID_FROM_REGISTRATION
```

Exact release and worker confirmations acknowledge root changes and **starting
authorized queued computations**. To keep this worker from claiming immediately,
drain its registration before installation; draining still permits identity
verification. A revoked registration cannot be installed. The CLI verifies one
private archive snapshot and normalized enrollment/catalog bytes before SSH.
Private enrollment is uploaded only into an input-bound root-owned 0700 staging
directory. Credentials do not appear in command arguments, release archives,
or printed enrollment metadata. The root-only staging enrollment is retained
for resumption; protect it and the original transfer copy as credentials.

The bootstrap installs Ubuntu's `docker.io` and hash-locked Python dependencies.
It refuses an existing Docker CE installation, existing containers, unmanaged
Reasoning worker paths/accounts, or a chat-server installation. It never stops
or deletes unrelated containers. Immutable release files live under
`/opt/reasoning-compute/releases/RELEASE_ID`; private enrollment, selected catalog,
and execution journals live under `/var/lib/reasoning-compute`.
`reasoning-compute.service` runs as the nonshell `reasoning-compute` account with
access to the Docker group. **Docker access is effectively root-equivalent on
this host**, despite the service's filesystem/privilege hardening; do not put
organization state or unrelated workloads here. Job containers do not receive
that socket, host credentials, or worker enrollment.

Before starting the service, the installer pulls only enrolled Linux amd64
digest pins, verifies the remote enrollment over certificate-verified HTTPS,
and checks local Docker enforcement/capacity and image metadata. This probe does
not create a container, claim a job, verify capability/SBOM file bytes, or establish
a real algebra result. Successful installation reports an active systemd unit
and `container_execution_verified: false`. The SSH wrapper requires a bounded,
duplicate-free JSON receipt matching every release/enrollment/catalog binding;
a successful SSH exit alone is not installation evidence. It verifies the loaded
unit's identity, absence of drop-ins, and lack of pending daemon reload.
Every actual job independently checks
its image evidence and container policy; real hardware execution must still be
verified before production use.

`/var/lib/reasoning-deploy/worker-installation.json` binds the exact archive,
release, server origin, worker/organization identity, enrollment hash, and selected
catalog hash. An exclusive host-local bootstrap lock protects installation.
Retry only the same command and inputs after interruption. Package-phase retries
can repeat package installation, configuration, and image pulls while the worker
service is inactive. Once configured, retries check managed bytes and identity
without reinstalling packages or reconfiguring enrollment. Startup intent is
durable before service activation: a lost startup receipt does not explicitly
restart an already active worker. A completed-installation retry only verifies
identity/metadata and checks service activity. Missing or changed managed state
is refused, not silently rebuilt. Changed release/enrollment/catalog requires
future explicit upgrade support; do not edit/delete the journal to force it.

Inspect `journalctl -u reasoning-compute -u docker` on the worker after failure.
An active service is not proof that a job succeeded or that old containers were
physically cleaned up. Before retiring hardware, drain it, establish completion
and result delivery or reconcile held outcomes after verified cleanup, then
intentionally destroy provider resources. The installer does not automate these
steps implicitly or provide upgrade/rollback; the separately confirmed controller
can select idle workers and call these retirement operations.
Revocation alone does not terminate a VM. Use the explicit retirement commands below.

## Install an owned DigitalOcean worker

For workers created with `do-worker-apply` or `do-worker-scale-apply`, use the
original private worker plan and ownership directory instead of supplying an
arbitrary SSH address. Pinned plans use their original private SSH identity
journal; omit `--known-hosts` if desired. For manual plans, independently verify
the host key in the provider console and put it in your protected known-hosts
file first. The command needs read-only
DigitalOcean access to the account, Droplets, firewalls, and tags; it does not
create, resize, or delete cloud resources.

```bash
reasoning-deploy do-worker-install \
  --token-file /secure/digitalocean-token \
  --plan /secure/reasoning-worker/plan.json \
  --state-dir /secure/reasoning-worker/ownership \
  --bundle reasoning-release.tar.gz --server-url https://chat.example.org \
  --registration /secure/worker-registration.json --catalog /secure/images.json \
  --identity /secure/operator-ssh-key --known-hosts /secure/verified-known-hosts \
  --confirm-plan WORKER_PLAN_ID --confirm-worker WORKER_ID \
  --confirm-release RELEASE_ID --confirm-droplet DROPLET_ID
```

Confirm the original plan, worker, release, and VM IDs. The command takes the
worker ownership lock, refuses retirement/cancellation intent, and checks the
exact provider account, deployment tag, VM configuration, active address,
SSH-only firewall, and sole applied firewall association. Missing, duplicated,
changed, or unfinished resources are not adopted or replaced. It snapshots the
verified bundle and normalized private enrollment/catalog before remote setup.

Before SSH changes, `/secure/reasoning-worker/ownership/digitalocean-installation.json`
durably records the original target, release/enrollment/catalog binding, resource
IDs, and address. The host checks its protected `/etc/reasoning-provisioning.json`
against the original plan/deployment identity and its DigitalOcean metadata VM
ID before package changes and again after activation. Its private
`/var/lib/reasoning-deploy/worker-provisioning.json` pins that request and binding;
retries cannot omit or change them. Self-hosted `install-worker` needs no provider
marker or metadata and continues to work without a DigitalOcean account.

Success requires an exact host receipt, fresh provider rechecks, and live matching
worker enrollment. It reports `worker_installation_verified: true` and
`container_execution_verified: false`; it does not prove a worker poll or actual
container execution. An unavailable response leaves `installing` intent. Retry
the same command and original inputs: even a saved `installed` receipt is
rechecked on the host, not treated as current readiness. An active installation
retry does not reinstall packages or explicitly restart the worker. Preserve both
ownership and installation journals; neither a failed setup nor a revoked
enrollment stops existing provider charges. The [controller](#run-a-bounded-worker-controller) coordinates
prepared worker lifecycles with opt-in provider-initialized pinning; the separately
confirmed pool prepares fresh generations. Worker upgrade/rollback is not implemented.

## Retire an installed worker

`retire-worker` drains, stops, and irreversibly retires a managed dedicated worker
over independently verified SSH. It applies equally to rented and owned hardware.
Supply the exact installed release archive, original private enrollment/catalog,
and exact release/worker confirmations:

```bash
reasoning-deploy retire-worker --bundle reasoning-release.tar.gz \
  --server-url https://chat.example.org \
  --registration /secure/worker-enrollment.json \
  --catalog /secure/worker-images.json \
  --ssh-host 203.0.113.20 --identity /secure/worker-ssh-key \
  --known-hosts /secure/worker-known-hosts \
  --confirm-release INSTALLED_RELEASE_ID --confirm-worker WORKER_ID
```

These confirmations authorize host retirement, not VM/data deletion. The installed
release must include the retirement protocol and bootstrap operation; this is not
an upgrade command. A different release or changed installation, enrollment,
catalog, service identity, or unmanaged service override is refused. The command
uses one verified upload snapshot and protected private staging, as installation
does. Credentials are not placed in command arguments or public archives.

The host verifies the managed unit's bytes, loaded file/account, absence of
drop-ins, and reload state. A private root-owned retirement journal shares the
installation lock and binds the exact installed release, origin, organization,
worker, and configuration hashes. Intent is durable before draining or shutdown;
bootstrap refuses reactivation once retirement is requested.

If running or unknown outcomes remain, the command returns `phase: draining`,
leaves the service running if it was already running, and does not stop it or seal
enrollment. Repeat after settlement. Unknown outcomes need their existing explicit
cleanup/reconciliation procedure; no program is automatically rerun. Server
quiescence is advisory: separate administrator actions can change authority or
service state concurrently.

Once no unsettled outcomes are observed, the command records shutdown intent,
disables/stops only the managed service, and verifies its stopped/disabled state.
It runs the installed worker's retirement command as its service account to
verify runtime cleanup and deliver retained output without executing programs.
Success requires the fixed server receipt, a matching durable private worker
receipt, and another service-state check. The root journal then records retirement.
Repeating after an interruption or lost receipt rechecks cleanup and service state
without reinstalling, starting, or restarting the worker.

`phase: retired` reports `service_active: false`, `service_disabled: true`, and
verified worker-container cleanup/local settlement. It explicitly reports
`cloud_resources_deleted: false` and `host_deletion_verified: false`. This does
not establish absence of unrelated containers/data or independently attest a
malicious host. This host-only command does not verify provider ownership or delete
resources; it does not stop provider billing. Keep installation,
retirement, enrollment, and provider ownership journals for recovery and auditing.

### Destroy a provisioned DigitalOcean worker

`do-worker-retire` couples managed-host retirement to deletion of the exact
Droplet, firewall, and unused compute tag recorded by `do-worker-apply`. It does
not apply to chat servers, unmanaged VMs, partially provisioned workers, or owned
hardware. Keep the original private plan/ownership directory, installed archive,
enrollment/catalog, SSH identity, and independently verified host key:

```bash
reasoning-deploy do-worker-retire --token-file /secure/digitalocean-token \
  --plan /secure/reasoning-deploy/worker-plan.json \
  --state-dir /secure/reasoning-deploy/worker-ownership \
  --bundle reasoning-release.tar.gz --server-url https://chat.example.org \
  --registration /secure/worker-registration.json \
  --catalog /secure/approved-images.json --identity /secure/worker-ssh-key \
  --known-hosts /secure/worker-known-hosts \
  --confirm-plan WORKER_PLAN_ID --confirm-worker WORKER_ID \
  --confirm-release INSTALLED_RELEASE_ID --confirm-droplet OWNED_DROPLET_ID \
  --confirm-delete-host-data
```

The confirmations authorize irreversible VM destruction, including **all local
VM data**. Retain anything needed off-host first. The command derives its SSH
address from the exact provider VM; there is no arbitrary SSH-host argument.
The archive must contain the `prepare-destroy` bootstrap protocol and exactly
match the installed release. It does not upgrade an older installation.

The provider token needs `account:read`, `droplet:read`, `firewall:read`,
`tag:read`, and their `droplet:delete`, `firewall:delete`, `tag:delete` scopes.
Unused-tag verification additionally reads images, volumes, snapshots, and
databases, requiring `image:read`, `block_storage:read`, `snapshot:read`,
`block_storage_snapshot:read`, and `database:read`, plus the dependencies the
provider requires for those scopes. Grant no create or unrelated delete scopes
for this operation. Tagged resource counts are permission-filtered, so missing
read access prevents tag cleanup rather than establishing emptiness.
See [tag visibility](https://docs.digitalocean.com/reference/api/reference/tags/)
and [snapshot permissions](https://docs.digitalocean.com/reference/api/reference/snapshots/).

The original provisioning journal is preserved. A separate private
`digitalocean-retirement.json` shares its exclusive lock, binds exact resource
IDs and installation hashes, and prevents provisioning from recreating resources.
Provider checks require the original account, marker, VM configuration with no
attached volumes, and firewall rules/membership; duplicates, replacements, and
disagreement between exact reads and complete marker discovery are refused.

Before VM deletion, the SSH host verifies its private provisioning marker and
DigitalOcean metadata ID. It drains/stops/seals the exact managed worker, checks
the recorded Docker endpoint and daemon identity before and after enumeration,
requires **no containers at all**, including stopped or unrelated containers,
and rechecks that the service is stopped/disabled. Busy or unknown jobs return
`phase: draining` without deletion. Host changes or remaining containers refuse
deletion; the command never removes unrelated containers to obtain a proof.
The operator must dedicate the machine and exclude concurrent administrators or
other workloads. These observations neither inventory every non-container
process/data file nor attest a malicious root user; the explicit data-deletion
confirmation authorizes destruction of remaining local data.

Deletion proceeds Droplet → firewall → tag. Each exact DELETE has durable intent
before the request. A 204 response is acceptance, not proof of disappearance:
both exact-ID absence and complete marker discovery must agree. Lost responses
or failed reads retain intent. Repeat the same command for reconciliation;
`phase: deletion_pending` does not automatically send another DELETE. After
independently inspecting a still-present pending resource, explicitly append
`--retry-delete droplet`, `firewall`, or `tag` for that one pending request. VM
retries repeat fresh SSH checks; an old receipt alone never authorizes DELETE.

The firewall is removed only after the VM is observed absent and no other VM
shares the marker. The tag is removed only after authorized reads establish no
tagged resources across its supported resource types. Other resources, associated
snapshots, external volumes, DNS, and other installations are never deleted.
Bulk/tag-based Droplet deletion and associated-resource destruction are forbidden.

`owned_vm_absence_observed: true` identifies removal of the owned compute VM,
even if later free-resource cleanup fails. `cloud_resources_deleted: true` and
`phase: deleted` require observed absence of all three owned resources, not a
claim that the entire account has no charges. `fresh_host_destruction_check`
distinguishes checks in this invocation from read-only reconciliation using a
saved sealed receipt. Preserve all journals after completion. Actual cloud/host
execution and queue-aware controller operation remain unverified on real hardware.

### Manual host-local retirement

The worker protocol can also be used without the deployment command. Use the
exact installed release path and registered worker ID. As root, drain while
leaving the service running to finish execution and result delivery:

```bash
runuser -u reasoning-compute -- \
  /opt/reasoning-compute/releases/RELEASE_ID/venv/bin/reasoning-compute-worker \
  --data-dir /var/lib/reasoning-compute drain --confirm-worker WORKER_ID
```

Repeat to inspect advisory quiescence. Then stop and disable the service before
irreversible retirement:

```bash
systemctl disable --now reasoning-compute
runuser -u reasoning-compute -- \
  /opt/reasoning-compute/releases/RELEASE_ID/venv/bin/reasoning-compute-worker \
  --data-dir /var/lib/reasoning-compute retire --confirm-worker WORKER_ID
```

Stopping a still-running attempt interrupts it; wait for completion first unless
that interruption is intended. Retirement refuses a live execution lock,
mismatched runtime, uncertain cleanup, unfinished delivery, and orphaned output.
It never reruns a program. It verifies absence of its own labeled containers
before sealing enrollment and retaining a private restart-blocking receipt.
Repeating recovers the same receipt after fresh cleanup verification. Keep root
installation/enrollment and provider ownership journals; `install-worker` cannot
reactivate a retired enrollment or substitute a new registration.

The output reports `cloud_resources_deleted: false`. The server trusts worker
cleanup confirmation, not independent physical attestation. Other containers,
unrelated host data, and provider resources are not checked or removed. Before
deleting a VM, independently check exact provider ownership, host/runtime, all
workloads/data, and retained result delivery. Disabling services or credentials
does not stop billing. For a managed DigitalOcean worker, use the separately
confirmed cloud destruction command above. Queue-aware managed retirement is
available through the [bounded worker controller](#run-a-bounded-worker-controller),
with an explicitly confirmed retirement policy and fresh queue/host/provider
checks. Real-host controller acceptance remains unverified.

## Verification

The first organization-server gate excludes optional scientific campaigns and
worker-fleet/upgrade-recovery campaigns. It covers accounts/tenancy, invitations,
chat, inference, workflows, bots/webhooks, request boundaries, backups, public
distribution, and confirmed DNS/provisioning/installation/setup behavior:

```bash
uv run --extra test pytest --deployment -q
```

The default `pytest -q` includes application and compute integration/security
checks but excludes `tests/test_science_*.py` and `tests/test_local_science_tools.py`.
Run those explicitly when working on research features:

```bash
uv run --extra test pytest --science tests/test_local_science_tools.py tests/test_science_*.py -q
```

Both gates use local fixtures; real public HTTPS and multi-user/provider journeys
must still be exercised on the deployed host. They do not revalidate upstream CAS
mathematics. Combining `--deployment` and `--science` is refused.

```bash
uv run --extra test pytest -q tests/test_public_deployment.py tests/test_public_operator_handoff.py tests/test_public_operator_recovery.py tests/test_public_worker_deployment.py tests/test_public_worker_host_retirement.py tests/test_public_worker_cloud_retirement.py tests/test_public_compute_scaling.py tests/test_public_compute_fleet.py tests/test_public_compute_fleet_recovery.py tests/test_public_provisioning.py tests/test_public_worker_provisioning.py tests/test_public_worker_retirement.py tests/test_python_distribution.py tests/test_public_backup.py
uv run --extra test ruff check src/reasoning_ledger/deployment tests/test_public_deployment.py
uv run python -m reasoning_ledger.distribution dist/reasoning_ledger-0.1.0-py3-none-any.whl
```

Deployment checks use command/HTTP fixtures to verify archive bounds and
integrity, current legal material, reproducibility, protected host state,
server/worker separation, private enrollment, metadata-only preflight,
resumption, refusal of edited configurations, verified upload snapshots, SSH verification, read-only
provider planning/creation/reconciliation, strict readiness responses, and
worker self-draining/retirement with retained-output recovery and runtime identity
checks, and managed-host service retirement with override/identity verification
and interrupted shutdown/receipt recovery, exact owned-resource destruction,
fresh whole-daemon emptiness checks, and lost DELETE reconciliation without blind
retries. They do not create or delete a real VM,
boot systemd/Caddy, issue a certificate, or execute a container. A release must
also be verified on its actual target hardware before treating those behaviors
as established.

`tests/test_public_server_installation.py` additionally exercises owned server
launch/install, provider/DNS/VM binding, release-before-paid-intent persistence,
pinned SSH upload and host receipt validation, service identity, HTTPS failure,
lost receipts, concurrent ownership, and pending launch resumption.

`tests/test_public_owner_setup.py` exercises real owner transactions, schema
migration, concurrent exact replay, password-reset preservation, sessionless
receipts, encrypted backup/restore, and complete CLI/host-main fixtures for
DigitalOcean launch/install and owned hardware, followed by login/invitation
acceptance. These are not live SSH/TLS or rendered-browser tests.
