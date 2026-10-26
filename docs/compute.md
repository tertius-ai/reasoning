# Public compute jobs and execution workers

`make deploy` offers Modal computation by default, with an explicit skip choice.
`make deploy COMPUTE_BACKEND=none` skips that step without installing the optional
SDK. The selection is retained for retries. Skipping leaves chat usable and
allows compute onboarding later with `make compute-setup`.

## Enable compute on an existing deployment

From an onboarded operator computer, run `make compute-setup`. It authenticates
SSH, synchronizes the verified installed release, then opens an interactive Modal
wizard. It uses the bundled approved image catalog when available, otherwise asks
for a protected published catalog. Modal signup/payment can be opened from the
wizard; provider credentials stay private. Select per-job resources, concurrency,
and an organization CPU-second quota. Confirm the site and organization before
any host/policy changes. The administrator password is prompted privately and
not persisted. Unknown grant creation is fenced, enrollment retries keep their
identities, and temporary SSH is closed on success or failure.

The routine installs isolated relay services on the existing chat host; it does
not run research programs there or create another chat Droplet. A positive policy
and active relays permit billable Modal jobs immediately. CPU-second quotas are
not dollar guarantees; set spending controls in Modal too. A successful setup
receipt means relays are active, **not that a live computation has succeeded**.
Test with a small gamma-topic run after enabling it. Keep the private
`DEPLOY_STATE/compute-setup` directory for retries. Do not repeat first-server
`make deploy` to add compute.

For owned hardware or dedicated DigitalOcean workers, use the separate-worker
installation/provisioning commands linked below. Those workers need Docker on
their execution host, not on the operator computer. Local personal CLI bots can
also use explicitly enabled, already-installed CAS without containers; that
trusted local execution is separate from the isolated organization compute queue.
Connected bots can also propose shared-image jobs through `compute_submit`.
Their owner reviews the exact source, digest and resources through the bot's
pending-request indicator in People; approval
uses this same queue on configured workers or Modal. A bot cannot self-approve,
requester access is rechecked at admission, and unknown execution is not rerun.

## Lean projects and formalization groundwork

Select an allowed `lean-mathlib` image and Lean in a gamma-topic's Compute editor.
**Lean project example** inserts a small editable JSON packet. Ordinary source
also works and can import installed mathlib. Project packets use this shape:

```json
{
  "format": 1,
  "files": {
    "Proofs/Main.lean": "import Mathlib.Tactic.NormNum\ntheorem target : (2 : Nat) + 3 = 5 := by norm_num\n"
  },
  "entry": "Proofs/Main.lean",
  "target": "target",
  "expected_type": "(2 : Nat) + 3 = 5"
}
```

Use at most 32 named `Proofs/*.lean` files, with uppercase ASCII module names.
Imports between supplied modules must use ordinary `import Proofs.Name` lines.
The runner compiles those files in dependency order. It refuses missing local
imports, cycles, traversal paths, toolchain/Lake overrides, and statement/target
command injection. The existing compute-program ceiling is 16,000 characters
including the JSON packet; no arbitrary project archive is extracted. The
expected type is a single Lean expression, at most 2,000 characters.
The mathlib editor initially requests 4 GiB of memory, clamped to the approved
image maximum and editable before submission. Small mathlib target audits can
already exceed 1 GiB; enroll a worker/Modal relay with at least 4 GiB capacity
for this example. Larger projects may need more memory and wall time.

Project checking compiles a target-type guard and separately imports its compiled
module with trust level zero and native extensions disabled. It collects the
target's transitive axiom dependencies, accepting only `propext`, `Classical.choice`
and `Quot.sound`. `sorryAx`, invented axioms and compiler-trust/native-evaluation
axioms are rejected. Warnings are errors by default. Source files and
`lean-report.json` are committed artifacts with the submitted-program hash,
exact target/type, Lean/mathlib pins and axiom list. A wrong type, missing target,
compile error or failed audit fails the job.

These are **untrusted job reports**, not a trusted server proof attestation:
submitted Lean metaprograms execute inside the job sandbox and can produce
arbitrary output or attempt to interfere with their own checking process. They
never automatically verify a mathematical ledger claim or complete a research
goal. Independent artifact validation in a separate trusted execution boundary
is required before such certification. A checked formal statement is also not
evidence that its translation matches an informal statement.

Beta topics with compute enabled can use `lean_check` for source or
`lean_project_check` for the project packet fields. Both follow the existing
topic CPU allowance/exact-action approval and organization quota controls.
There is no automatic autoformalization search loop; these checks and artifacts
are the execution foundation for one.

The organization server provides durable compute admission and a separately
authenticated worker protocol. Jobs belong to beta or gamma topics and capture an
immutable program, resource request, approved image, and source/evidence digests.
SQLite transactions protect admission, claims, cancellation, and settlement;
private files hold artifact chunks referenced by the database.

Beta agents submit within an explicit topic CPU-second allowance or propose
exact-action approval. The allowance defaults to zero and does not replace
organization quotas or worker/image limits. See [research workbench](research-workbench.md).

The server does not execute submitted programs. A bounded output-archive
harvester reads scratch via a fixed tar helper inside the running container,
as the program UID, because Docker copy does not support tmpfs contents reliably.
Root-image capability/SBOM evidence is read separately before execution. It
accepts flat regular files into a private spool without extracting
user-controlled paths; it rejects links, devices, sparse entries, duplicate
names, and oversized output. It also bounds extended archive metadata before
parsing and rejects nonzero trailing payloads. The separate
`reasoning-compute-worker` command executes approved programs with local Docker
or Docker inside optional on-demand Modal VM sandboxes,
captures bounded logs and artifacts, verifies container removal, and resumes
fenced result delivery from private durable state. The shared chat has gamma-topic
submission/history, immutable source/evidence inspection, logs, committed
artifacts, cancellation, and administrator cleanup reconciliation. Organization
settings manages compute policy, worker enrollment/draining/revocation, and
bounded scoped scaler grants with one-time credentials and rotation/revocation.
The [deployment CLI](deployment.md#install-a-separate-compute-host) installs
enrolled workers on separate Ubuntu 24.04 amd64 machines, including owned hardware.
Explicit [DigitalOcean worker planning/provisioning](deployment.md#provision-a-digitalocean-compute-host)
binds private enrollment to dedicated hardware and live price confirmation.
The [owned-worker installer](deployment.md#install-an-owned-digitalocean-worker)
checks original resources and the host VM identity, journals setup before SSH,
and validates a fresh installation receipt; it does not verify a real algebra run.
Workers can self-drain and irreversibly retire enrollment after cleanup and
retained result delivery; cloud deletion is separate.
The [queue-aware fleet planner](deployment.md#plan-queue-aware-worker-capacity)
reads bounded organization/owned-resource snapshots and recommends capacity
under worker-count and quoted-cost ceilings without executing actions.
The separately confirmed action path reserves a durable fleet-wide VM budget
before provisioning and rechecks demand before each new resource creation.
Original reservations can resume despite unfinished peers; explicitly confirmed
unpaid cancellation cleans owned free resources without deleting a VM or
forgetting a pending paid request.
Confirmed [fleet amendment](deployment.md#amend-a-managed-worker-fleet) retains
unresolved reservations and original ownership/birth namespaces while archiving
only terminal, freshly absent slots and adding fresh unbound enrollments. It
does not create/delete cloud resources or install/activate workers.
The [confirmed controller](deployment.md#run-a-bounded-worker-controller) automates
queue-driven provision/install/drain cycles within a prepared bounded fleet.
New worker plans can opt into provider-initialized SSH host-key pinning, with the
[documented metadata trust boundary](deployment.md#initialize-pinned-worker-ssh-identities).
The [worker pool](deployment.md#run-a-replenishing-worker-pool) can recoverably
enroll fresh template-bound workers and activate fleet generations under the
original budget and a lifetime enrollment ceiling.
[Versioned gamma workflow definitions](server.md#versioned-workflow-definitions)
support durable DAG authoring and history. Saving them does not enqueue jobs.
The separate [workflow run API](server.md#durable-workflow-runs) dispatches ready
compute/LLM children and records approvals with restart-safe attempt linkage.
Compute dependency data is bounded inert JSON at `/work/inputs.json`, not source
substitution. Workers must advertise input support to claim those jobs.
Registering a worker does not launch hardware. Real-container algebra execution
and operating-system resource enforcement are not verified on the current
development host; fixture tests do not establish them. Do not treat this API
and worker alone as a complete production compute service.

## Modal VM compute

The optional Modal backend uses the same organization queue, fenced leases,
workflow dependency inputs, cancellation, bounded logs/artifacts, and durable
result-delivery protocol as a Docker host. The web/desktop gamma controls need
no separate provider login. The chat process does not execute submitted code.
Each relay owns one active slot and creates a fresh VM per claimed attempt;
setup enrolls 1–8 relays under the chosen organization concurrency ceiling.
There are no warm idle compute VMs, automatic job retries, GPUs, or cross-machine
distributed jobs in this backend. Larger individual jobs use the existing CPU,
memory, and wall-time fields within the approved image and relay maxima.

Modal's [VM sandbox runtime](https://modal.com/docs/guide/sandboxes) supports
Docker. The relay runs the existing Docker executor inside that VM: immutable
linux/amd64 CAS image verification, installed capability/SBOM checks, non-root
execution, no job network, read-only root, bounded tmpfs, cgroup/PID limits,
and non-extracting artifact collection remain required. Unsupported daemon
enforcement fails closed; there is no fallback to a plain gVisor sandbox or
weaker job policy. The default supervisor uses a digest-pinned Ubuntu 24.04
base, Python 3.12, and Ubuntu's `docker.io` package installed by the Modal image
builder. The supervisor's package layer is not a fully pinned/audited CAS release.
Only the selected public CAS image is pulled by digest; private registry
credentials are not supported by this path.

Modal token ID/secret and the scoped worker enrollment remain on the relay.
No provider keys, owner sessions, registry credentials, server filesystem,
or Docker socket from the chat host are sent into submitted programs. The
remote supervisor receives Docker argv and source/input bytes over stdin.
Modal receives the programs and their outputs as the compute provider;
organization administrators must choose it with that data boundary in mind.

The SDK uses physical CPU cores and MiB; the adapter converts requested logical
CPUs to half as many physical cores and applies equal request/upper CPU and
memory limits. It reserves an additional 512 MiB for the remote supervisor and
sets a VM lifetime of job wall time plus 600 seconds (job wall time at most
85800 seconds). Startup/image-pull operations are bounded separately. Successful
jobs settle ordinary measured program usage; failed or cancelled paid starts
consume the full reserved application CPU-time allocation. Provider-side VM
startup, image builds, extra memory, cleanup, and egress can be billed outside
that allocation. **Application compute quotas are not Modal dollar or invoice
caps.** Configure provider-wide controls separately in the Modal workspace.
See Modal's [resource and billing semantics](https://modal.com/docs/guide/sandbox-resources).

Creation intent is written before the paid request with a deterministic,
provider-length-bounded name and ownership tag. A lost response is reconciled
by that name; no second sandbox or source execution is launched. Cleanup checks
the tag and original Modal app/runtime binding, terminates the original VM,
and requires a terminal provider poll before allowing another claim. A missing
running name after uncertain creation is not proof of absence: the worker stops
with its journal intact for operator reconciliation. Never delete the journal
or restart from a new state directory to bypass uncertain cleanup. Credential,
app, catalog, or resource-profile changes require explicit operator migration;
the backend will not silently adopt them.

For a first DigitalOcean organization, use
[interactive Modal setup](deployment.md#modal-in-interactive-setup). On owned
Linux hardware, install the optional SDK with `uv sync --locked --extra modal` in the
source checkout, import the ordinary one-time worker registration, and run:

```bash
reasoning-compute-worker --data-dir /private/relay configure \
  --server-url https://chat.example.org --registration /private/registration.json
reasoning-compute-worker --data-dir /private/relay verify-images \
  --backend modal --modal-profile /private/modal-profile.json --catalog /private/images.json
reasoning-compute-worker --data-dir /private/relay run \
  --backend modal --modal-profile /private/modal-profile.json --catalog /private/images.json
```

The profile is a private 0600 JSON file with `format:1`, `app`, `environment`,
`token_id`, `token_secret`, a digest-pinned `supervisor_image` base, and all six
`resources` limits. Use the setup-generated profile as the canonical example.
Its resources must equal the enrollment maxima, and the private catalog must
contain exactly the enrolled approved images. SDK 1.6.0 supports Python
3.11–3.14 here. Verification checks enrollment and Modal app/configuration,
creates no VM, and does not establish daemon/image execution readiness.
`drain` is backend-independent; `retire` needs the same Modal profile/catalog
and `--backend modal` to clean up remote execution before sealing enrollment.

Local tests verify the adapter, actual organization API enrollment, relay host
commands, bounded transport, and recovery using fixtures. Live Modal VM startup,
nested Docker cgroup enforcement, image pulls, representative algebra, artifacts,
cancellation, and billing are unverified release gates. These checks test our
integration and isolation, not upstream CAS mathematical correctness.

## Scoped compute automation and recoverable enrollment

Owners/admins manage `compute-scalers` under the organization API root:
GET lists metadata; POST creates a grant with `name`, distinct `image_ids`,
six-field `resources`, `maximum_workers` (1–64), and `lifetime_seconds`
(3600–31536000, default 2592000). The selected resource maxima must fit all
selected approved images. POST `compute-scalers/{id}/rotate` accepts
`lifetime_seconds`; DELETE permanently revokes the grant. Creation and rotation
return a one-time `token`, never included in later metadata. Bounds are immutable
and at most eight unrevoked/unexpired grants may exist. A current human
administrator sponsor is required independently of their login session.

The separate bearer-only `rns_` protocol has three routes:

| Path | Method | Contract |
| --- | --- | --- |
| `/api/compute-scaler/identity` | GET | Current grant metadata and installation-approved selected image records |
| `/api/compute-scaler/snapshot` | GET | Bounded tenant queue/resource metadata, without programs, logs, keys, or credentials |
| `/api/compute-scaler/enrollments` | POST | `request_id` (32 lowercase hex), `name`, `image_ids`, six-field `resources`, and client-generated `worker_token` (`rnw_` plus 43 URL-safe characters) |

Enrollment responds 201 with `{format:1,scaler_id,worker}` metadata only. The
server stores hashes and maps `(scaler_id,request_id)` to the original worker.
Exact repetition recovers it; changed original parameters/credential conflict,
and a revoked/retired original enrollment cannot be replaced by replay. Token
rotation preserves this namespace. Requests are limited to 30 attempts per
grant per rolling minute; failed store-level attempts count. New workers need
current organization image permission, current image resource maxima, grant
bounds, and ordinary organization capacity. Revoked workers with running or
unknown jobs still consume the grant's worker ceiling, regardless of lease expiry.

Scaler endpoints cannot access chat, provider keys, worker protocol, or cloud
APIs. **They delegate trusted execution through enrollment:** new worker tokens
can claim jobs and access assigned programs. Never expose a scaler credential
to job code. Grant expiry/revocation, sponsor demotion/removal, or backup restore
disables automation but does not itself stop physical workers or delete hardware.
Existing worker credentials require their separate cleanup/revocation workflow.

The [deployment enrollment command](deployment.md#recoverably-enroll-workers-with-scoped-authorization)
persists the private client-owned nonce/credential before POST and refuses
changed grant/catalog/output state during recovery. The legacy direct admin
registration UI/API below returns server-generated one-time credentials and
does not offer this recoverable enrollment contract. Neither method creates
cloud resources. The fleet CLI accepts a scaler token in `--session-file` for
capacity observations; paid creation remains separately confirmed and budgeted.

## Image approval and organization policy

The [public research CPU recipe and image release commands](images.md) provide
pinned open CAS inputs, installed metadata, local OCI integrity inspection,
explicit catalog creation, and an actual public-API/Docker acceptance command.
Archive inspection is not a signature, license/source audit, registry
publication, or execution proof; those checks are separate.

Compute is disabled by default: the installation has no approved images, and
each organization's policy starts with no allowed images and a zero quota.
An operator supplies `reasoning-server ... serve --compute-images PATH` with a
JSON array of at most 32 image records, in a file no larger than 64 KiB. The
catalog is read at server startup. Organization administrators cannot change
these records over HTTP; they can allow a subset for their organization.

Each record has exactly these fields:

| Field | Value |
| --- | --- |
| `id` | Lowercase identifier beginning with a letter, up to 64 characters |
| `name` | Display name, 1–200 characters |
| `reference` | Registry/repository followed by `@sha256:` and 64 lowercase hexadecimal characters; mutable tags are rejected |
| `languages` | Distinct subset of `sage`, `python`, `julia`, `gap`, `pari`, `lean` |
| `source_revision` | Full lowercase 40- or 64-character hexadecimal source revision |
| `capabilities_digest`, `sbom_digest` | SHA-256 digests of the image's capability and SBOM evidence files |
| `capabilities_path`, `sbom_path` | Bounded absolute `/etc/` or `/usr/share/` file paths without parent traversal |
| `maximum_resources` | All six bounded resource fields below |

These are approval metadata, not proof that an image exists, was signed, or
enforces the limits. The catalog loader validates structure and pins; it does
not fetch a registry, verify a signature, or inspect a container. An execution
worker must verify the actual image and evidence before running a program.
Never substitute an illustrative image reference for a verified published image.

`PUT /api/organizations/{organization_id}/compute-policy` accepts
`allowed_images`, `monthly_cpu_seconds` (0–10^12), and `max_active_jobs` (1–4096,
default 2). Optional `max_pending_jobs` and `requests_per_user_15_minutes` accept
1–1,000,000 or null; omitted/null means no queue/request ceiling. These policies
are separate from execution concurrency. Owners and admins can change policy. Other members, including
viewers, can read `compute-configuration`, which returns the policy, approved
installation catalog, and quota balance. Invalid catalog IDs or duplicate IDs
are rejected. A positive quota and an allowed image are required for admission.

## Submitting and observing jobs

### Shared web and desktop controls

Open a beta or gamma topic and choose `/compute` in its slash-command palette.
The **Stop** button in a running computation's card header, or `/stop`,
requests cancellation of active standalone computations; remote cleanup and accounting must still settle. An owner/admin
first allows operator-approved images and sets a positive CPU-time quota in
**Administration → Compute**. Installation approval and organization
permission are separate: the browser cannot add image digests to the host catalog.
Register and install a compatible separate worker; submission never launches
hardware. Jobs wait in the queue until an authorized compatible worker claims them.
Use the deployment CLI to explicitly provision DigitalOcean workers, install
workers on rented/owned hardware, or opt into Modal relays during setup. Docker
enrollment/submission does not provision a VM; an enabled Modal relay creates a
paid sandbox when it claims an eligible job under the approved setup policy.

Select an image and supported language, enter the program, and set all six
integer resource limits. Byte fields use exact bytes (1 MiB = 1048576 bytes,
1 GiB = 1073741824 bytes), not rounded units. Choosing an image initializes
defaults bounded by its approved maxima. **Submit computation** captures the
exact program, limits, and a request identifier. Lost/error receipts retain that
request; **Retry unchanged computation** replays the same values. The interface
also recognizes a matching job in shared history. Captured requests survive
topic/organization switching in this signed-in window, but are held only in
memory, not browser storage. Reload/close warns while a request remains pending;
sign-out/session loss clears it. Before starting another computation after lost
state, inspect the topic's chronological activity. **Stop retrying request** is an explicitly
confirmed local action, not cancellation of an already-accepted job.

Standalone jobs appear as computation cards among the gamma topic's messages
and workflow runs. **Earlier compute activity** pages older records in batches
of 50. Cards expand to inspect durable
state, exclusive-cursor events, and committed artifacts. Programs, evidence,
and logs are plain untrusted text, not HTML or executable UI. The worker delivers
logs after container cleanup, not as a live interpreter stream. Artifact links
download attachments in the browser; the desktop host denies downloads, so open
the server separately in your browser for files. Inspect untrusted files before
using them. Viewer accounts can inspect output but cannot submit or cancel.

The requester/admin can **Request cancellation**. Until confirmed termination
and settlement, running/unknown jobs keep their reservation. Administrators can
expand **Reconcile uncertain computation** only for an unknown outcome, supply
verified elapsed milliseconds, and explicitly confirm physical container cleanup.
Neither cancellation nor reconciliation deletes cloud hardware or reruns a job.

Administration shows worker last-seen metadata, not a claim of host readiness. Worker
enrollment returns one-time JSON: copy it into a private owner-only file before
closing settings or switching tabs, then import it with the worker configure
command below. The interface does not persist credentials in browser storage.
A dedicated Ubuntu host can instead use `reasoning-deploy install-worker` with
this private JSON and the operator's approved image catalog.
A lost registration response is not automatically retried; inspect/revoke any
inaccessible registration before explicitly creating a replacement. Draining
blocks new claims; revocation immediately denies protocol access without proving
remote termination. Orderly machine retirement needs verified cleanup and result
delivery before destroying provider resources.

`npm --prefix web run test:public-compute` runs a rendered three-account
HTTP/store journey using synthetic worker output, not Docker execution. It
requires a built frontend, the repository Python environment, installed
Playwright Chromium, and permission to run local network listeners. This fixture
does not verify actual algebra, kernel/container isolation, TLS, or cloud provisioning.

### HTTP request contract

`POST /api/organizations/{organization_id}/topics/{topic_id}/compute-jobs`
accepts `client_id`, `image_id`, `language`, `program`, and optional `resources`.
The program is nonempty UTF-8 text up to 16000 characters without NUL bytes.
Resources, when supplied, require exactly all six fields:

| Resource | Supported bounds | Default |
| --- | --- | --- |
| `cpu` | 1–64 integer logical CPUs | 1 |
| `memory_bytes` | 64 MiB–256 GiB | 1 GiB |
| `disk_bytes` | 8 MiB–64 GiB | 256 MiB |
| `pids` | 16–4096 | 128 |
| `wall_time_seconds` | 1–86400 | 120 |
| `artifact_bytes` | 1 byte–1 GiB, at most half `disk_bytes` | 1 MiB |

All limits must also fit the selected image's approved maximums. JSON booleans,
strings, or floats do not stand in for integer resource limits. The requested
language must be in the image's approved language set.

Owners, admins, and members can submit jobs; viewers cannot. Alpha and beta
topics reject compute submissions. Replaying the same client ID for the same
requester and organization returns the existing job, even after policy or
catalog changes. Changing its topic or parameters returns 409. Clients must
retain the original ID and complete request when retrying a lost response.
Starting another computation requires a fresh ID; uncertain jobs are never
automatically retried.

Submission atomically reserves `cpu * wall_time_seconds` from the quota.
Queue length is not capped by execution concurrency. Configured `max_pending_jobs`
counts queued, running and uncertain jobs; `requests_per_user_15_minutes` counts
admitted requests for that user in the organization over the preceding 15 minutes.
Neither is enabled by default. Insufficient quota returns 409; configured queue
or request-policy exhaustion returns 429.

`POST /api/organizations/{organization_id}/projects/{project_id}/compute-jobs`
accepts the same job request without requiring a topic. Project authorization is
checked at submission, observation, execution and human output reads. Jobs retain
their project ID, optional topic provenance, approved image and exact submitted
program. Topic jobs preserve topic access rules. Organization-scoped job, event,
artifact and cancellation routes work for both scopes.
The project GET `compute-jobs` route pages project-only jobs (not topic-owned
jobs), using `limit` 1–50 and an exclusive `before` sequence cursor.

The topic's GET `compute-jobs` route returns the most recent 20 jobs, with a
`limit` of 1–50. `standalone=true` omits workflow-owned children and includes a
stable `sequence` cursor. Exclusive `before` sequence pages older jobs; cursor
pages also include `sequence`. Organization-scoped `compute-jobs/{job_id}` returns current
state; its `events` route returns at most 200 durable events after an exclusive
`after` sequence cursor. Events include state transitions, log text, cancellation
intent, and committed artifacts. All current organization members can read
jobs, source programs, evidence metadata, and committed artifacts. Worker tokens,
attempt credentials, and lease hashes are not in human-facing responses.

The requester or an administrator can POST an empty JSON object to the job's
`cancel` route. A queued job becomes cancelled with zero usage immediately.
Cancellation of a running or uncertain job records intent; it does not claim
that the remote container has stopped.

## Workers and fenced leases

Owners and admins register workers with POST `compute-workers`: `name`,
`image_ids`, and the six-field `resources` maximum. The response returns a
one-time `token`. Save it privately on the separate worker host, never inside a
job container or in logs, URLs, or browser storage. GET returns metadata without
the token. Enrollment is not capped by execution concurrency; enrollment does
not authorize extra simultaneous execution or provision hardware.

Owners/admins can GET `compute-scaling-snapshot` under the organization API root.
It returns one read-only transaction: observation time, `max_active_jobs`, the
current approved image catalog, a FIFO queued-demand preview no larger than
the configured execution concurrency, every running/unknown job, and active
worker metadata plus revoked workers still assigned unsettled work. Format 2
includes `queued_remaining` for demand outside the preview; it is not an
exhaustive queue export. Small complete inventories use format 1. Current fleet
clients accept both formats with a 16 MiB wire allowance. Fully retired history
is excluded. Each worker has `idle_since_at`, the later of registration and its
latest assigned job's completion; polling does not reset it. No source program,
logs, artifacts, lease secrets, or worker token/hash is returned. The route
does not expire or settle jobs, extend leases, or update activity. This is
advisory scheduling information, not permission to delete physical hardware.
Fleet decisions preserve idle capacity while `queued_remaining` is positive;
a demand preview is not evidence that unshown work can run on the remaining
profiles. Provider budgets, ownership checks and cleanup proofs remain required.

A worker credential is tied to exactly one organization. It cannot authenticate
to account, chat, provider-key, or human compute routes. Human session credentials
cannot authenticate to worker routes. Worker requests use
`Authorization: Bearer TOKEN`; cookie sessions are not accepted. Any supplied
Origin must still match the server origin. Writes require JSON and are bounded
by the server's 64 KiB request-body limit.

| Worker path | Method | Request fields beyond credentials |
| --- | --- | --- |
| `/api/worker/identity` | GET | None; authenticated enrollment metadata without credentials |
| `/api/worker/status` | GET | None; enrollment, unsettled job IDs/status, advisory quiescence |
| `/api/worker/drain` | POST | Empty object; block fresh claims without interrupting active work |
| `/api/worker/retirement` | GET | None; fixed enrollment retirement receipt/status |
| `/api/worker/retirement` | POST | `cleanup_confirmed: true`; irreversible seal only when drained and no unsettled assigned jobs |
| `/api/worker/claim` | POST | Optional `claim_id`: 32 lowercase hexadecimal characters |
| `/api/worker/jobs/{job_id}/heartbeat` | POST | `attempt_id`, `lease` |
| `/api/worker/jobs/{job_id}/logs` | POST | Lease fields, `chunk_index`, `text` |
| `/api/worker/jobs/{job_id}/finish` | POST | Lease fields, `status`, `elapsed_ms`, optional `exit_code`, `error_code` |
| `/api/worker/jobs/{job_id}/artifacts` | POST | Lease fields, `name`, `size_bytes`, `digest` |
| `/api/worker/jobs/{job_id}/artifacts/{artifact_id}/chunks` | PUT | Lease fields, `chunk_index`, `content_base64` |
| `/api/worker/jobs/{job_id}/artifacts/{artifact_id}/commit` | POST | Lease fields |

Identity reads do not claim work, renew leases, or update last-seen activity.
They authenticate the worker's current, non-revoked organization-bound token.
The returned metadata is not evidence of machine readiness or container cleanup.
Status reads likewise do not expire or renew attempts or update last-seen.
They list only this worker's assigned running/unknown jobs without programs or
leases. An expired lease is not a settled outcome.

Claim returns null when nothing can run, or the immutable job with one
`attempt_id` and secret `lease`. Each worker has one active slot. Claims respect
the organization concurrency limit, worker resource/image limits, current
requester membership, and current image/policy/quota. A queued request whose
authority or approved image changed fails with zero usage. Claims never select
a running or uncertain job. A worker using `claim_id` can recover a lost claim
response by repeating that ID: the server returns the same immutable job,
attempt, and lease, including its current terminal or uncertain state. Replay
does not extend the lease, re-admit the job, or permit execution after a stop
response. The command durably saves the ID before claiming. An empty-object
claim has no replay identifier; losing that response can leave a held uncertain
job. Neither case justifies submitting another computation automatically.

Workers should heartbeat every ten seconds; leases expire after thirty seconds.
Heartbeats return `stop: true` for cancellation, changed authority/policy/image,
or a non-running attempt. Heartbeats and claims update worker last-seen state.
The delivery deadline is start time plus declared wall time plus
`120 + 2 * ceil(artifact_bytes / MiB)` seconds for finishing delivery; this
bounded grace does not increase permitted program runtime.
Workers must stop containers on lease loss, heartbeat refusal, or a stop response.

PATCH `compute-workers/{worker_id}` with `draining: true` stops fresh claims
without stopping an already-running job. DELETE revokes the token immediately
and prevents all further worker requests. Revocation does not itself prove
container termination. These endpoints do not delete cloud hardware.

Self-draining is idempotent and audited; only administrators can resume claims.
Retirement requires strict cleanup confirmation and no assigned running/unknown
outcomes in the transaction that permanently revokes execution access. The server
trusts the enrolled worker's cleanup report, not independent kernel attestation.
Queued unassigned jobs remain available to other workers. Retired registrations
cannot be resumed; enroll a replacement.

The retired token is denied ordinary identity/status, claims, heartbeat, delivery,
and human routes. It can only read/replay its own fixed retirement receipt after
a lost sealing response. An administratively revoked token without completed
retirement has no exception. Receipts contain worker/organization IDs, retirement
ID, timestamp, and a boolean, not credentials or proof of whole-host readiness.

## Outcomes and quota reconciliation

Jobs progress from queued to running and then succeeded, failed, cancelled, or
outcome_unknown. A lease/deadline that expires becomes outcome_unknown when the
organization is next observed or a worker request is handled. There is no
automatic rerun or background assertion of physical termination.

Finish requires the original worker and attempt credentials. It accepts
`succeeded`, `failed`, or `cancelled`, with elapsed time in milliseconds
(0–86400000). Success requires exit code zero and no error code. The first
settled outcome wins; duplicate finish delivery returns that outcome. A lost
worker can supply a late final result for the same attempt. Changed authority or
cancellation prevents a new success; elapsed time beyond the requested wall
limit fails with `wall_time_limit`.

Settlement charges allocated CPU cores multiplied by elapsed time rounded up
to whole seconds, capped at the original reservation. This is an application
admission quota, not measured processor utilization or a DigitalOcean invoice
cap. It does not account for boot time, image pulls, idle VMs, storage, bandwidth,
or other provider charges. Monthly settled usage follows the UTC settlement
month; unresolved reservations remain held across month boundaries.

An uncertain job retains both quota and an active slot. An administrator can PUT
its `reconciliation` route with `cleanup_confirmed: true` and `elapsed_ms` only
after verifying that the lost container has stopped. Reconciliation settles a
failed/cancelled outcome, records an audit event, and frees the slot. A false
confirmation is rejected. The server trusts the administrator's confirmation
and the enrolled worker's elapsed-time report; it cannot independently prove
remote cleanup or runtime accounting.

## Logs, artifacts, and state protection

Logs contain at most 1 MiB per job, delivered in up to 4096 ordered chunks of
1–8192 UTF-8 bytes. Retrying the same index and text is idempotent; changed or
out-of-order chunks are rejected. Log text is untrusted program output, not HTML
or privileged instructions. The original fenced attempt can deliver bounded
logs/artifacts while running or outcome_unknown. This permits recovery of
already-collected output after lease loss; it never permits restarting a
program. Settled jobs reject further delivery.

Artifacts have safe ASCII filenames without paths or header injection, at most
64 files, and a total declared size within the job's artifact limit. Metadata
reserves bytes even for unfinished uploads. Chunks are base64-encoded, at most
32768 decoded bytes, with their index and exact declared length. Duplicate
identical chunks are accepted; changed bytes are rejected. Each private chunk
is fsynced before its database reference. A crash orphan can be adopted only
by an identical retry. Commit verifies chunk completeness, sizes, individual
hashes, and the full artifact SHA-256 before making it available.

Human GET `compute-jobs/{job_id}/artifacts` lists committed artifacts only.
GET its `/{artifact_id}` suffix streams verified chunks as
`application/octet-stream`, attachment disposition, and `nosniff`, irrespective
of filename. Artifacts remain available after a worker finishes or disappears.
Uncommitted artifacts are not readable. Filenames never become server paths;
private directories use generated IDs, and symlink/exposed-state checks refuse
unsafe local artifact storage.

Backup/restore must include the private `artifacts/` tree as well as consistent
SQLite state and installation secrets. Do not copy only a live SQLite file and
discard its WAL. The [host-local backup commands](backup.md) include referenced
committed and incomplete chunks; restore revokes worker credentials and holds
nonterminal jobs for reconciliation without replay. Artifact retention/garbage
collection and remote physical-container cleanup are not supplied by this API.

## Running the separate worker

For a dedicated Ubuntu 24.04 amd64 machine, follow the automated
[separate-host installer](deployment.md#install-a-separate-compute-host).
The following commands also support manual/source installations on compatible
Linux hosts; they do not provision hardware or install a system service.

Use a separate Linux amd64 compute host with Docker, cgroup v2, working CPU,
memory/swap and PID limits, and Docker's built-in seccomp profile. The Docker
daemon's reported total memory must be at least the requested allocation plus
256 MiB. The worker account needs access to its explicitly selected
local Unix Docker socket; this is a privileged host capability, not a job
permission. Do not grant job programs the socket, the worker state directory,
organization API keys, or host credentials. Docker isolation is not an absolute
defense against kernel vulnerabilities; keep compute separate from chat/state.

Install the Python package on this host using the source installation instructions
in [server setup](server.md). Preload each approved image by its immutable
registry digest into this same Docker daemon. The runner uses `--pull never`,
a private credential-free Docker client configuration, an explicit local socket,
and no inherited proxy/context environment. It does not fetch an image using
job-supplied credentials. Approval of release signatures and the external
image/source/evidence attestation is an operator responsibility; the runner
checks the catalog's pinned image and evidence bytes, not an attestation signature.

As an organization administrator, POST the worker's `name`, approved `image_ids`,
and full maximum `resources` to its `compute-workers` endpoint. Save the entire
one-time JSON response privately, transfer it securely to the worker, and give
the file mode 0600. The response includes the token, worker ID, and organization
ID; do not put it in chat, shell arguments, or source control. Configure and run:

```bash
reasoning-compute-worker --data-dir /var/lib/reasoning-worker configure \
  --server-url https://reasoning.example.org \
  --registration /secure/worker-registration.json
reasoning-compute-worker --data-dir /var/lib/reasoning-worker verify-images \
  --catalog /secure/enrolled-images.json
reasoning-compute-worker --data-dir /var/lib/reasoning-worker run
```

Replace the example URL with the organization's actual origin. HTTPS is required
except for explicit loopback HTTP test servers. The state directory is created
0700 and must be owned by the worker account; existing exposed/symlink state is
refused. Configuration stores the enrollment 0600 without printing its token
and refuses changing an existing enrollment. The registration response remains
a credential: restrict or remove the transfer copy according to your secret
handling policy. `run --docker-socket PATH` selects another local socket;
`run --once` handles at most one claim or retained delivery and exits. There is
no cloud provisioning or service installation implicit in these commands.

`verify-images` requires a private catalog containing exactly the enrolled
image IDs. It uses the readonly identity route to check the exact worker,
organization, images, and strict resource limits, then checks the local daemon's
reported enforcement/capacity and immutable image metadata against the full
enrollment allowance. `verify-images --docker-socket PATH` selects its local
socket. It never claims a job, creates a container, or updates last-seen state;
it can run alongside an active worker or a drained enrollment. It reports
`container_execution_verified: false`: capability/SBOM bytes and actual runtime
isolation are not established by this probe. Preload digest-pinned images first;
the automated Ubuntu installer does this with credential-free registry pulls.

Each process takes an exclusive worker-state lock and handles one job at a time.
Execution and cleanup bind the canonical local Docker socket and reported daemon
ID in private `docker-runtime.json`. Subsequent checks must match, including before
removal and final empty-container verification. An execution journal without that
binding, or a changed endpoint/daemon, is refused. Do not delete the binding to
switch engines or force cleanup. This detects accidental daemon changes, not a
malicious host falsifying metadata. The ID comes from
[Docker's system information command](https://docs.docker.com/reference/cli/docker/system/info/).
It verifies the actual immutable image, Linux amd64 platform, capability/SBOM
hashes, source revision, installed entry point, and resource maximums before
execution. Containers have a read-only root, denied network, private namespaces,
no devices or host binds, dropped capabilities, no-new-privileges, CPU/RAM/PID
limits, and bounded noexec/nosuid/nodev writable tmpfs. Tmpfs consumes the RAM
allocation as well as the scratch allowance. The job runs as numeric UID/GID
10001, distinct from its non-root UID/GID 10002 supervisor. Images must support
these users without a writable home directory. Cache directories are redirected
under `/tmp`; the image's `HOME` is not replaced. Python uses the image's
`python3`; GAP disables its break loop; Lean checks the submitted source file.
These are fixed interpreter commands, not arbitrary host commands.

Write flat regular output files in `REASONING_OUTPUT_DIR` (`/work/out`). The
host enforces the program deadline and cancellation independently of a protected
supervisor timer. The short-lived container stays alive during bounded output
collection because Docker discards tmpfs when it stops. The worker then removes
the container and verifies its absence before recording a deliverable outcome.
If removal cannot be established, no terminal outcome is sent and the server
keeps the reservation/active slot. Startup cleans only containers carrying this
worker's ownership label; it refuses work if cleanup fails.

Heartbeats run independently of log capture every ten seconds. A host-monotonic
20-second renewal deadline stops execution even if the heartbeat HTTP thread
blocks. Heartbeat failure, server stop, or process shutdown also stops execution.
Logs are captured privately up
to 1 MiB, converted to bounded UTF-8 with replacement for invalid bytes, and
delivered after container cleanup; this command does not stream live execution
logs. Artifact spools are private, hash-checked before upload, and delivered in
idempotent bounded chunks. After acknowledged server settlement, only the
worker's generated temporary attempt files are deleted; committed server
artifacts remain available.

The fsynced journal distinguishes pending claim, claimed, executing, delivering,
finishing, and acknowledged settlement. Restart retries claims/result delivery,
never a program that reached executing state. After cleanup, a crashed claimed
attempt fails with zero usage; a crashed executing attempt fails with
`worker_stopped` and conservatively charges its full declared allocation because
elapsed execution could not be recovered. A retained completed result can finish
its original uncertain attempt without rerunning. Lost completion acknowledgments
replay the same settlement. Cancellation or changed authority still prevents a
success. Revoked credentials stop delivery and require administrator cleanup/
reconciliation; revocation alone does not delete a VM.

## Drain and retire an enrollment

Run as the owner of the configured worker state. First drain while leaving the
worker service running to complete its current attempt:

```bash
reasoning-compute-worker --data-dir /var/lib/reasoning-worker drain \
  --confirm-worker WORKER_ID_FROM_REGISTRATION
```

This command needs no Docker access and can run alongside the worker. It reports
bounded unsettled job metadata and advisory `quiescent`, not physical cleanup.
It never cancels, renews, or claims a computation. Repeat to inspect status;
running/unknown outcomes require completed delivery or reconciliation after
verified cleanup. Draining is reversible, so an observation alone cannot authorize
deleting hardware.

Stop and disable the supervisor before irreversible retirement. See the
[managed SSH retirement or Ubuntu service commands](deployment.md#retire-an-installed-worker). For a
manually managed host, prevent automatic restart, then run:

```bash
reasoning-compute-worker --data-dir /var/lib/reasoning-worker retire \
  --confirm-worker WORKER_ID_FROM_REGISTRATION
```

Use the same Docker endpoint as execution; `--docker-socket PATH` selects it.
Retirement requires the exclusive execution-state lock and refuses a live worker.
The lock must be an empty, private, owner-owned regular file with one link;
named pipes, symlinks, hard links, and changed lock contents are refused before
server requests or cleanup.
It never executes a program or submits a fresh claim. It cleans only this worker's
labeled containers, recovers a stopped/crashed claimed or executing attempt as
failure without rerunning, and delivers retained logs/artifacts/settlement.
A crashed executing attempt conservatively charges its declared allocation.
Unacknowledged delivery or unexplained attempt output is preserved and blocks
success. A pending lost claim stays blocked while its original attempt is unsettled.

After fresh runtime/empty-labeled-container verification and server sealing,
private `retirement.json` prevents local restart. Interrupted/lost receipt recovery
obtains the same receipt after fresh cleanup verification, not another attempt.
Changed/corrupt markers are refused; do not remove them to reuse enrollment.
Output explicitly says `cloud_resources_deleted: false`: retirement does not
stop/disable systemd, delete cloud resources, prove other host workloads are
absent, or end provider billing. Fixtures do not establish physical host cleanup.
For dedicated workers created by the deployment CLI, the separately confirmed
[DigitalOcean destruction command](deployment.md#destroy-a-provisioned-digitalocean-worker)
adds exact provider ownership, host metadata, whole-daemon emptiness, and
journaled deletion/absence checks. A stored enrollment receipt alone is not VM
deletion authority. Self-hosted workers need no DigitalOcean account.

Run protocol checks without provider credentials, Docker, or paid resources:

```bash
uv run --extra test pytest -q tests/test_public_compute.py
uv run --extra test pytest -q tests/test_public_compute_archive.py
uv run --extra test pytest -q tests/test_public_compute_docker.py tests/test_public_compute_worker.py
uv run --extra test pytest -q tests/test_public_worker_retirement.py
uv run --extra test ruff check src/reasoning_ledger/server tests/test_public_compute*.py
```

The catalog and worker responses in these tests are non-executing fixtures.
Successful protocol outcomes must not be reported as actual CAS results.
