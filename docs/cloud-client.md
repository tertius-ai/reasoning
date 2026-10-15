# Cloud client boundary

`reasoning_ledger.cloud_client` is the public, transport-neutral boundary for a
Desktop or other Reasoning client that uses Cloud-backed projects and hybrid
runs. It consumes the immutable `reasoning-cloud-client-v1alpha1` contract
bundle. It does not choose an endpoint or authentication provider.

## Connection and scope

`CloudSessionProvider` owns connection initiation and returns canonical session
views. Its implementation may later use a browser or another provider-specific
mechanism, but those mechanics and all credential material remain private to
the provider. A connected public session contains only its canonical actor,
tenant, and the JCS digest of the accepted capability snapshot. Callers never
supply actor or tenant values to `CloudClient`; the implementation derives them
from its current session and checks every resource against the complete actor
type, actor ID, and tenant ID scope.

`connect()` and `disconnect()` are idempotent state changes. Connecting does not
link, copy, or transfer a project. Disconnecting or reconstructing a client
does not cancel a run or erase accepted project, event, cursor, or artifact
state.

## Canonical resources

The Cloud-client bundle owns four record types:

- `reasoning.cloud-client.session` is the disconnected or connected view.
- `reasoning.cloud-client.capabilities` advertises exact contract support and
  references canonical descriptor bytes by identity and byte digest.
- `reasoning.cloud-client.run` binds one operation to its actor, tenant, project,
  route plan, and pricing lifecycle. Active runs use `null` pricing because the
  accepted pricing contracts describe complete settlement; terminal and
  outcome-unknown runs require the full exact-version pricing record.
- `reasoning.cloud-client.run-event-page` carries typed hybrid-operation events.
  These are distinct from a single Executor job's events because one hybrid run
  may cover inference and compute. Artifact event metadata directly reuses the
  Executor artifact schema.

Project documents remain `reasoning.platform.project-bundle`. HarnessProvider,
inference-route, compute-target, route-plan, pricing, and local connector
records remain Platform-owned. Executor job, result, error, provenance,
capability-manifest, and artifact records remain Executor-owned. The client
bundle references these immutable dependencies and never forks their schemas.

Alpha prerelease negotiation is exact. Project, descriptor, connector,
Executor, Cloud-execution, and capability-manifest dependencies remain alpha.1.
Route plan and pricing are accepted only as alpha.1/alpha.1 or
alpha.2/alpha.2 pairs.

## Replay and integrity

Project, run, and cancellation mutations require caller-held idempotency keys.
Cloud-client-owned fields retain only their SHA-256 digests; an embedded exact
pricing lifecycle remains byte-compatible with its existing Platform schema.
A retry with the same scoped key and exact request returns the original result;
changed request content is a conflict.

The run `request_digest` is SHA-256 over JCS of
`{"mutation":"submit_run","run":...}` after omitting the top-level
`idempotency_key_digest`, `request_digest`, `route_plan_digest`, and
`pricing_digest` fields and the embedded pricing operation's `request_digest`.
Those excluded derived values validate independently. Thus changing any
request content while retaining an idempotency key is detected even if a caller
supplies a self-consistent replacement digest.

Event cursors are opaque and resume exclusively after the supplied value.
Delivery is at least once, so `accept_event_page` retains the JCS digest for
each event identity and suppresses byte-identical redelivery. Reuse of an event
identity for different content fails before cursor state advances.
The replay state also retains the previous run state and event timestamp so
state transitions and timestamp monotonicity remain fail-closed across page
boundaries.

Artifact metadata validates against the canonical Executor schema. Content is
returned only after both its byte size and SHA-256 digest match the metadata.
Project content receives the same size-and-digest verification before the
in-memory conformance client accepts it.

## Reference implementation

`InMemoryCloudClient` implements both public protocols for deterministic,
offline conformance. `InMemoryCloudStore` can be shared with a newly
constructed client to model a restart: the new client begins disconnected while
durable project and run state remains intact. This reference performs no I/O,
networking, provider call, authentication, billing, execution, or deployment
and is not product Cloud behavior.

## Desktop wiring

`reasoning_ledger.desktop_cloud.DesktopCloudController` composes these public
protocols with `DesktopHybridPlannerFactory` at the Desktop backend boundary.
The controller starts disconnected and accepts only an already-authenticated
object implementing both protocols; it contains no endpoint, authentication,
credential, provider, or network implementation. On connect it revalidates the
canonical session and capability snapshot, resolves every advertised descriptor
to its exact accepted bytes, and creates the existing planner from those
records. Route and capability inspection has no mutation or project-transfer
side effect.

The private `reasoning_ledger.desktop_auth` seam accepts a registered IPv4
loopback redirect at `http://127.0.0.1:<port>/auth/callback` for ports 49152
through 49167 (one exact port per authorization attempt); it rejects aliases,
other ports, and other HTTP paths. The opt-in `desktop_auth_browser` adapter
generates random attempt, state, nonce, and S256 verifier values, listens only
on the configured IPv4 loopback port, and opens the system browser with an
explicit HTTPS authorization endpoint. Its one-use callback handler checks the
Host, path, query, and state before forwarding the code to the injected
consumer, never logs the code or state, and closes after use or timeout. The
launcher also supports explicit cancellation via `close()`; constructing it
opens neither listener nor browser. The fake authority validates exact
issuer, public-client, audience, redirect, state, nonce, S256 PKCE, tenant, and
expiry bindings; consumes a callback once; and models grant validation and
revocation. Its fake browser launcher records without opening a browser, and
its macOS/Linux stores retain material only in memory without accessing an OS
credential store. Authentication constructs one closed private boundary
containing the canonical Cloud provider and `CloudProductPolicyClient`. The
controller validates both protocols, the provider's disconnected session, and
bounded policy-continuity state before it atomically publishes either client.
The expected actor and tenant are checked against the first canonical connected
session before the planner or account-policy authority is constructed.
Authentication alone does not connect, read policy, inspect routes, or transfer
project bytes.

The auth-owned injection also carries a no-argument, credential-free
session-validity guard. The Desktop controller invokes it before connect and
before every connected read or mutation consults provider code. Invalid or
unreadable grant state clears Desktop identity and locally disconnects and
detaches both auth-owned clients without invoking either one, while logout
completes the same local cleanup despite store or revocation errors and never
removes a pre-existing non-auth provider. Resume reconstructs both clients from
the fake grant and revalidates the server policy rather than making retained
continuity authoritative. Canonical provider-session or descriptor drift, or an
invalid optional-use mutation response, detaches the closed pair before any
second provider access; transport loss remains replayable through that bounded
continuity state. The separate `desktop_cognito_tokens` adapter exchanges the
PKCE code with the public Cognito OAuth client, downloads bounded public
signing keys without AWS credentials, verifies the signed ID and access tokens
with exact issuer, client, nonce, scope, expiry, and matching subject, and can
refresh a session without changing its subject. It returns identity proof only:
it cannot assert a product account, tenant membership, or entitlement. The
separate opt-in `desktop_os_credential_store` adapter persists an exact grant
in the macOS Keychain or Linux Secret Service and checks read-after-write. It
rejects null, plaintext-file, and third-party keyring backends rather than
falling back to an app file. A clearly named `.invalid` backend injection is
available for offline tests and does not contact the host OS store. The macOS
Keychain backend's OS access controls still depend on the calling executable;
the adapter does not claim to make an arbitrary Python process a signed app.
No production product session binding or assembled provider transport exists
yet; the available adapters alone cannot authenticate a Desktop into Cloud.

The private `reasoning_ledger.desktop_policy` projection attaches fail-closed
presentation evidence to exact accepted descriptors without changing them. It
can show local zero-charge estimates, explicit unknown or unavailable remote
prices, hard limits and exhaustion, planned-but-unverified data/subprocessor
paths, required telemetry purpose, and three separate optional-use choices
that default off. Hosted AWS, Modal, model, consent, billing, and spend paths
remain unavailable because canonical Cloud-client and Platform records do not
carry activation approvals, spending limits, processor paths, telemetry policy,
or optional consent state. Those are reported as a private policy gap rather
than inferred from a descriptor.

The loopback boundary retains each bounded request as raw bytes through strict
JSON parsing, including nested duplicate-key rejection, before converting it to
an endpoint input model or canonical record. Credential-shaped raw mutation
arguments fail before session or resource provider calls. Submitted and
returned runs are both checked by the connected hybrid planner, and the durable
operation binding retains the accepted request, idempotency, route, pricing,
and project lineage.

The controller's project operation requires an explicit opt-in for one named
Cloud project. It verifies the exact local project record and every supplied
content byte before invoking `put_project`, then records only that project and
scope binding. There is no global upload preference. A run may be submitted
only against that exact accepted per-project binding.

Non-secret reconnect state is stored beneath the Desktop workbench: tracked
operation bindings, the complete exclusive event cursor and deduplication
history, and artifact IDs observed in accepted events. Disconnect never calls
cancellation. After backend restart, explicit reconnect restores the same
scoped operation through the provider, continues event replay after the durable
opaque cursor, and verifies artifact metadata, size, and digest before returning
content to the loopback caller.
