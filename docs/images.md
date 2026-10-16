# Public research CPU images

The public recipe is `images/research-cpu/Dockerfile`; its build inputs are locked
in `images/research-cpu/inputs.json`. It installs the pinned SageMath distribution
(including Python, GAP, and PARI/GP), Julia with bundled standard libraries, and
the core Lean toolchain and Noether 0.3.2's default CPU wheel. Additional Julia
packages and mathlib are not installed in the CPU profile. The `lean-mathlib` profile extends it with pinned
mathlib, its pinned Lake dependencies and compiled caches. Only Linux amd64
workers are supported. Noether requires x86-64-v3 (AVX2, FMA and BMI2) CPUs and
excludes MPI and optional GPL subprocess bridges. Its fixed declarative worker
is installed at `/opt/reasoning/noether_worker.py` in both profiles.

Noether is built from public release `v0.3.2`, commit `4c92c269c59da02a4d32c833c2ee093407f8e22f`, using
the upstream `make wheel` release path and locked Rust/manylinux/uv inputs.
Build tooling anonymously fetches its checksum-pinned public GitHub commit archive before Docker and stages
a credential-free `noether-source` context. Git metadata, tokens and SSH agents
are not exposed to image build stages. Native source releases and the installed
wheel's native/Rust inventories belong in the corresponding-source review.
Supplied source archives must match the public download's exact byte length and
SHA256. Local Git overrides export only the locked object, including its original
commit identity, rather than the index or working tree. Publication accepts this
approved public Noether revision only and refuses other candidates before source
upload or signing. Publication requires a hash-verified public source bundle and signed
observed-source provenance or an explicit human review. Neither path fabricates
complete legal or dependency-closure assurance.
Compiler-provided Rust standard-library/runtime code is not represented by the
wheel's Cargo closure alone. The runtime retains exact Rust 1.94.0 library
copyright and MIT/Apache texts under `/usr/share/reasoning/licenses/rust-1.94.0`,
with SHA256s checked against the official compiler release. The source collector
includes the matching official Rust source and notice/build inputs. The SPDX
compiler-runtime record identifies this separate review obligation; notice
retention does not prove static-link or corresponding-source completeness.
Only bounded integration smoke tests run: capabilities/discovery, integer
factorization and a Smith-form certificate. We do not rerun upstream CAS tests.

The recipe and first-party tooling are Apache-2.0. **The aggregate binary image
is not Apache-only.** Preserve the installed upstream notices and review
`images/research-cpu/THIRD_PARTY_NOTICES.txt`, the package inventory, and the
complete dependency closure before distributing binaries. Sage and Julia's
binary distributions include copyleft components. Source links are not a
completed corresponding-source delivery. Retain and convey exact corresponding
sources, patches, build scripts, license texts, and any required relinking or
installation information with the published image. The generated SPDX document
is an installed OS/Python package and pinned-distribution inventory, not a full
filesystem scan or a completed license/source audit; `NOASSERTION` does not grant
redistribution permission.

## Build local candidates

For a checkout without Docker access, run `make compute-images` after signing in
to GitHub CLI. `.github/workflows/compute-images.yml` builds both committed
profiles on independent ephemeral Linux amd64 GitHub runners, inspects each OCI archive, copies
it without digest changes to a loopback-only registry, pulls that exact digest,
and runs the real public-API/worker acceptance suite. Embedded binary inputs and
build/runtime/acceptance tooling trigger that job on pull requests. Source
collection, signing and publication-only changes run their focused application
checks without rebuilding unchanged CAS images. Buildx and BuildKit are locked; the downloaded
Buildx executable is SHA256-checked. SDK cleanup refuses runners not identified
as GitHub-hosted; it removes only disposable SDK installations for disk headroom.
Python tooling is installed after SDK-cache cleanup. The workflow never requires
this computer's Docker socket. Noether's builder removes its
compiler outputs, native build trees, virtual environment and download caches
in the same layer after checking the retained wheel. The CI command requires
at least 24 GiB free for `research-cpu` or 32 GiB for `lean-mathlib` before
creating its builder. These are provisioning floors, not peak-space guarantees.
It reports disk space before building, after export/cache release and after
acceptance cleanup; the workflow also reports host Docker/output usage.

The workflow uploads `image-evidence.json`, the installed `capabilities.json` and
`sbom.spdx.json`, and the completed `acceptance.json`. These public review artifacts
contain no binary images, test credentials, or private acceptance directory. The
inventory exposes exact installed package/source versions for source review;
it is not a completed license audit. The runner releases the owned builder cache after verified OCI export and the
disposable archive after digest-preserving registry transfer, before loading the
image for execution. Its loopback registry is destroyed. **This is candidate verification,
not public publication.** Signed source/notice provenance, public immutable
registry publication, anonymous pull verification, and an approved catalog are
still required before activation. GHCR packages must be public for anonymous
workers; a public Git repository does not by itself set package visibility.

Maintainers can run `make compute-images COMPUTE_RETAIN_CANDIDATE=true` to retain
the exact private OCI archives for seven days as `candidate-COMMIT-PROFILE`
artifacts. This option applies only to manually dispatched runs, never pull
requests. Download and retain the archives privately before their expiry. The
publication workflow selects that successful manual run and never rebuilds a
reviewed digest. The review/sign/source-upload commands are documented in the
[maintainer review CLI](image-review.md).
`make compute-source-publish` guides large source-bundle delivery to DigitalOcean
Spaces, retaining multipart retries and verifying the anonymous public hash.
This is a maintainer operation; organization operators need no Spaces credentials.

Use a clean committed clone, an installed Reasoning Python environment, Docker,
and Buildx **v0.32.1** at the commit recorded in the lock. Buildx must be available
as `docker buildx`. The build uses one explicitly named local `docker-container`
builder with BuildKit **v0.26.2**, bound to the locked image digest and
`unix:///var/run/docker.sock`. It does not select remote or paid builders.
Docker access is root-equivalent; use a dedicated builder/worker host.

Create a builder on that host:

```bash
docker --host unix:///var/run/docker.sock buildx create \
  --name reasoning-images --driver docker-container \
  --driver-opt image=docker.io/moby/buildkit@sha256:de10faf919fc71ba4eb1dd7bd6449566d012b0c9436b1c61bfee21d621b009aa \
  unix:///var/run/docker.sock
uv run python -m reasoning_ledger.image_release build \
  --source-root /path/to/reasoning --builder reasoning-images \
  --output /path/to/artifacts/first
uv run python -m reasoning_ledger.image_release build \
  --source-root /path/to/reasoning --builder reasoning-images \
  --output /path/to/artifacts/second
uv run python -m reasoning_ledger.image_release compare \
  /path/to/artifacts/first/image-evidence.json \
  /path/to/artifacts/second/image-evidence.json
```

The output parent must exist; each output directory must be new. The build stages
only the selected committed recipe files and legal/schema inputs, not the whole
working checkout. The source revision and commit timestamp supply deterministic
metadata and `SOURCE_DATE_EPOCH`; account password-aging dates use that same
epoch. Runtime probes' writable caches are removed from the final image.
Downloads have SHA256 checksums; no unpinned
package installation or private source credential is needed. Installed runtime
version drift fails generation. The OCI exporter rewrites timestamps and disables
unsigned scanner/provenance attachments; the embedded inventory remains included.
Every build disables instruction-cache reuse.
The build retains its raw Buildx metadata and independently inspects its archive.

A matching comparison is evidence of candidate manifest, config, layer, embedded
metadata, and build-identity equality. Use independent clean builders/caches to
establish clean-repeat reproducibility; two cache hits are not that proof.
No image has a published reference merely because a local archive was built.
These commands do not push, sign, activate an installation, or spend cloud money.

## Inspect and explicitly approve an immutable image

`reasoning_ledger.image_release build --profile lean-mathlib` composes the locked
CPU Dockerfile with `images/lean-mathlib/Containerfile.fragment`, using only
committed build inputs. `images/lean-mathlib/inputs.json` locks Lean 4.24.0, the
compatible mathlib commit, source-archive SHA256 and eight Lake dependency
revisions. Git, curl and CA certificates are exact-version installed from the
signed Ubuntu snapshot recorded in the same lock; their dependency closure is
resolved only against that snapshot and included in the installed inventory.
The base image's certificate store is bootstrapped using the same exact CA
package, downloaded by BuildKit with a locked SHA256 before HTTPS package access.
TLS and archive-signature verification remain enabled; failed indexes abort setup.
The build verifies the toolchain and dependency commits, downloads
compiled caches, probes a mathlib import, and embeds the profile's metadata and
inventory. Its image ID is `lean-mathlib`; its OCI archive is
`lean-mathlib.oci.tar`. The CPU profile's ID and archive remain `research-cpu`.
This is a mathlib-focused superset of the CPU runtimes, not a Lean-only minimal
image. Its binary publication has the same source/notice gates as the CPU image.

Workers execute with network denied and a read-only image. Job execution never
runs `lake update`, downloads packages, or invokes elan. Sources and reports are
bounded scratch/output files; installed mathlib and caches stay immutable.
The GitHub matrix tests both profiles. The mathlib profile adds successful target
checking plus rejection cases for `sorry`, an invented axiom, and a mismatched
target type. Only a passing real receipt establishes image execution.

```bash
uv run python -m reasoning_ledger.image_release inspect \
  --archive /path/to/artifacts/first/research-cpu.oci.tar \
  --schema /path/to/reasoning/contracts/executor/v1alpha1/image-capabilities.schema.json \
  --repository registry.example.org/reasoning/research-cpu \
  --output /path/to/artifacts/approval --write-approval-catalog
```

Replace the example repository with the operator's intended actual public
repository. Inspection verifies SHA256/size for every selected OCI blob, config
rootfs digests against decompressed layers, current capability schema/timestamp,
worker profile, and exact embedded evidence. It reads regular evidence files in
place without extracting filesystem paths, rejects evidence links and linked
ancestors, applies whiteouts across layers, and bounds metadata, blob counts,
layer sizes, and evidence sizes. Only a single OCI linux/amd64 image without
unverified attachments is supported; foreign and zstd layers are refused.

Without `--write-approval-catalog`, inspection emits no approval catalog. With
it, `compute-images.json` contains the exact publishable descriptor digest and
embedded evidence digests. This is explicit operator approval, **not** a signature,
registry availability, actual execution, or license audit. Preserve that exact
digest when copying the archive to a registry; verify the remote immutable
reference before use. The publication destination, signatures/trust policy,
complete corresponding-source bundle, and actual image execution are release
requirements independent of this archive inspection.

After those requirements are satisfied, a release maintainer can commit the
approved published catalog at `images/compute-images.json`. Ordinary `make build`
and `make upgrade` bundle this file automatically, without contacting a registry
or selecting unapproved candidates. An explicit `reasoning-deploy bundle
--compute-catalog /path/to/artifacts/approval/compute-images.json` overrides the
checkout catalog alongside the frontend, source-root and output
arguments. The catalog becomes canonical manifest-hashed release content.
Interactive Modal setup selects and privately
saves that catalog without requiring users to build images or prepare a catalog.
A supplied catalog is publisher approval, not independent registry availability
or a license/source audit. A checkout without a published catalog bundles none;
Modal setup then requires an explicit catalog file. Invalid or empty supplied
catalogs fail the build rather than silently producing a compute-ready release.

Supply the catalog to the [server](compute.md#image-approval-and-organization-policy)
and [worker installer](deployment.md#install-a-separate-compute-host). The worker
installer pulls immutable references; the runner never pulls while executing a
job. Organization administrators still need a positive quota and allowed image.
The image defines no writable volumes and defaults to supervisor UID/GID
10002:10002. The runner starts submitted programs as 10001:10001 with denied
network, read-only root, bounded private `/work` and `/tmp`, and kernel guards.

## Collect source delivery inputs

`images/source-inputs.json` locks the actual full Sage and Julia source
distributions, Lean source, mathlib and its Lake dependencies, and the native
GMP/MPFR/FLINT inputs for Noether. The full Sage archive includes standard-package
upstream archives and its patches/build scripts; Julia's full archive includes
dependency sources. These archives are useful review inputs, not proof that
every binary component corresponds to those sources.

The locked Rust standard-library source archive is a source candidate for
Noether's compiler-provided runtime. The separate Rust compiler distribution is
notice/build material, not standard-library corresponding source. Collection
checks both archive pins against the retained recipe and copies only its three
locked copyright/license notices into the bundle. The compiler archive remains
recoverable under private `build-materials/`, outside the bundle and general
notice scan; the collection receipt records its URL, hash and retained location.
Compiler provenance and retained notices do not prove the final binary's exhaustive
static-object dependency closure; that remains a review obligation.

On a maintainer machine, collect a bundle against an actual installed inventory:

```sh
uv run python -m reasoning_ledger.image_sources \
  --source-root /path/to/reasoning --profile research-cpu \
  --sbom /path/to/candidate/sbom.spdx.json \
  --lock /path/to/reasoning/images/source-inputs.json \
  --noether-source-root /path/to/noether \
  --ubuntu-snapshot 20261006T000000Z \
  --ubuntu-snapshot 20250215T000000Z \
  --ubuntu-keyring /usr/share/keyrings/ubuntu-archive-keyring.gpg \
  --output /path/to/new-source-delivery
```

For the mathlib profile, choose `--profile lean-mathlib` and its actual inventory.
The Noether checkout supplies the immutable approved source commit; collection
does not edit that checkout or run its scripts. The collector conveys the whole
checksummed Cargo.lock registry-source superset, including build/development
sources, without claiming those extra crates are installed. Local workspace
crates use the repository archive only when the exact name/version appears
without a registry source in Cargo.lock and a retained package manifest confirms
that identity. Registry dependencies require individually checksummed crate
archives. An explicitly
supplied trusted Ubuntu keyring authenticates the isolated source indexes; apt
downloads exact installed source versions without installing packages or changing
system apt configuration. Missing versions or failed authenticated retrieval
remain unresolved. Repeat `--ubuntu-snapshot` to cover versions from more than
one immutable archive; later snapshots are consulted only for unresolved exact
source versions. A newer snapshot need not contain older installed sources.

The new directory contains `sources.tar`, its regular-file inventory in
`source-review.draft.json`, and the retained `payload/` inputs. Downloads are
SHA256-checked; `--cached-archives /path/to/cache` reuses only identical verified
bytes, and `--cached-crates /path/to/crate-cache` does the same against Cargo.lock.
The bundle retains recipe material, installed inventory, signed apt-index
evidence when available, source archives, copied regular license/notice members,
and explicit unresolved requirements. Incomplete apt downloads remain recoverable
under private `download-failures/`, outside the bundle and notice scan; their
unresolved records identify that relative location. Upstream archive paths are
never extracted or executed.
Detached OS source signatures remain in the bundle as evidence; the notice scan
opens only supported tar archive suffixes, not signature sidecars or `.dsc` files.
Full Sage/Julia archives and duplicate retained payload/bundle bytes
require several gigabytes of local disk.

The draft deliberately leaves every human approval false and dispositions
pending. The guided publisher validates observed component identities, versions,
file hashes and retained source/notice candidates automatically, producing a
distinct machine-provenance attestation. It does not edit the draft into a human
review. Full corresponding-source assurance still requires matching components to actual
sources, build patches, notices and relinking materials, including Julia's native
binary build recipes and Lean's compiler dependencies. Nested upstream archives
retain their notices but are not exhaustively scanned. Neither source links,
successful downloads, nor `NOASSERTION` complete this review. No signing key,
signature, public registry write, public source availability, or completion
catalog is fabricated by collection.

## Maintainer publication

`make compute-publish` guides retained-candidate selection, authenticated CI
identity checks, public source delivery, encrypted signing/trust enrollment,
GHCR publication and hosted anonymous catalog verification. It does not require
Docker locally, manually edited component-review records, or rebuilding an image.
`compute-publish.yml` and `compute-combine.yml` are explicitly dispatched
maintainer workflows, not ordinary deployment. Configure the
`compute-publication` GitHub environment with the desired approval policy.

Machine attestations have `format: 2` and purpose
`observed-source-provenance-v1`, signed under
`b"reasoning-compute-observed-source-provenance-v1\n"`. They bind the original
recipe archive, exact OCI archive/digest, installed SBOM, source collection,
every outer source-bundle member, and observed component mappings. The
`coverage` disposition `observed-source-candidates` records candidates, not
certified source correspondence. Missing/version-mismatched concrete evidence
blocks; generic closure caveats are signed and displayed. Human, legal and
complete-dependency-closure verification flags remain false. An independently
enrolled public key authenticates this provenance; operator catalogs still
require actual anonymous public pulls and real execution.

The source bundle retains the exact hash-bound recipe Git archive. Hosted
machine publication safely extracts and verifies it rather than requiring the
original commit to remain fetchable. The original CI run and source revision
remain immutable provenance; no rebuilt replacement is accepted.

Explicit format-1 human reviews remain supported by the verifier and local
publisher. Their review JSON has these fields:

| Fields | Meaning |
| --- | --- |
| `format`, `profile` | Integer `1`; `research-cpu` or `lean-mathlib` |
| `reviewer`, `reviewed_at` | Responsible reviewer and UTC ISO timestamp ending in `Z` |
| `source_revision`, `recipe_sha256`, `image_digest` | Exact committed source, SHA256 of its selected recipe Git archive, and inspected `sha256:…` image descriptor |
| `source_bundle_sha256`, `source_bundle_bytes`, `source_bundle_url` | Actual bundle hash, byte length, and stable direct public HTTPS delivery URL without credentials/query tokens |
| `files` | Every regular outer-tar member as `{ "path": …, "sha256": …, "bytes": … }`; no extra, missing or linked files |
| `coverage` | Component records `{ "id": …, "disposition": …, "sources": […], "notices": […], "reason": … }` covering every observed SPDX package plus components identified by the review |
| `dependency_closure_reviewed`, `source_delivery_approved`, `public_binary_distribution_approved` | All explicitly `true`, supplied by the human reviewer, not generated by the publication tool |

Component disposition is `source-included` with corresponding source and notice
paths, or `source-not-required` with notices and an explicit reviewer rationale.
Paths must identify hashed bundle members. Every installed SBOM package must be
covered, but this check does **not** prove that the inventory is exhaustive or
that the reviewer's legal conclusions are correct. The reviewer must examine
the complete installed dependency closure, including bundled native libraries,
patches, build/relinking materials and upstream notices. Source links alone do
not satisfy this signed delivery contract.

`recipe_sha256` hashes the bytes returned by `git archive SOURCE_REVISION` over
`images/research-cpu`, `src/reasoning_ledger/agents/noether_worker.py`, `LICENSE`, `NOTICE`, and
`contracts/executor/v1alpha1/image-capabilities.schema.json`; the mathlib profile
also includes `images/lean-mathlib`. The exact source checkout must be clean.
The source bundle may contain regular upstream archives as members; inspection
streams and hashes it without extracting paths onto the host.

For format 1, the reviewer signs
`b"reasoning-compute-source-review-v1\n" + canonical(review).encode()` using
Ed25519, where `canonical` is `reasoning_ledger.server.compute_catalog.canonical`.
The signature is exactly 64 raw bytes. The independently selected public key
uses PEM SubjectPublicKeyInfo encoding; see the primary
[Ed25519](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/)
and [key serialization](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/serialization/)
documentation. The review is authenticated to that trusted key; it is not an
independent legal audit or an OCI image signature. Runtime catalogs still use
the operator-approved exact-digest trust boundary.

The guided publisher commits the selected public key at
`images/publication/review-key.pem`, and the signed provenance and signature at
`images/publication/PROFILE/SOURCE_REVISION/source-review.json` and
`source-review.ed25519` on isolated publication branches. Never commit the
signing private key. A prepared machine publication can also be dispatched:

```sh
gh workflow run compute-publish.yml --ref YOUR_PUBLICATION_BRANCH \
  -f profile=research-cpu -f source_revision=YOUR_REVIEWED_COMMIT \
  -f candidate_run_id=YOUR_SUCCESSFUL_RETAINED_CANDIDATE_RUN
```

The workflow verifies the machine-provenance signature and anonymously fetches the exact public
source bundle, then downloads the exact retained archive from the matching
successful manual candidate run. It does not rebuild after review. Every review,
recipe, image, bundle-member and component-coverage gate runs before the push.
The workflow destination is `ghcr.io/OWNER/REPOSITORY/PROFILE`, using the current
GitHub repository in lowercase. The local publisher requires an explicit
`--repository` destination for publication. GHCR packages must be
public: GitHub's [container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
describes anonymous public pulls and package authentication. Creating a package
does not automatically make it public; set its visibility separately. A private
package may be pushed, but cannot produce a completion catalog.

`python -m reasoning_ledger.image_publication --help` describes local inputs.
Provide its registry token only through `REASONING_IMAGE_REGISTRY_TOKEN` and
username through `REASONING_IMAGE_REGISTRY_USER`. The publisher uses a scoped
private auth file, suppresses credential-bearing command output, and uses
separate empty credentials to verify the anonymous digest and pull. It runs
the real worker acceptance suite on that exact public digest before writing
`compute-images.json` and `publication.json`. No failed gate creates a new
completion catalog. Registry effects can precede a later failure; preserve the
exact inputs and intent rather than treating failure as proof of no push.

Only completion evidence, the signed public review and the public acceptance
receipt are uploaded. The private acceptance directory contains enrollment
credentials and must not be published. A failed post-push workflow retains its
non-secret intent only. After reviewing successful receipts for both profiles,
the guided CLI dispatches hosted combination to produce the approved
installation catalog only after fresh anonymous checks for every selected
publication. The workflows do not change organizations, provision paid compute,
or configure package visibility.

## Actual container acceptance

On a dedicated Docker-capable Linux amd64 host, ensure the exact approved
reference has been published and pulled. The daemon must support cgroup v2,
memory/swap/CPU/PID limits, and its builtin seccomp profile. At least one CPU and
1 GiB plus daemon overhead are required; allow additional host headroom.
The mathlib profile requires a 4 GiB acceptance-worker memory ceiling plus
daemon/host overhead; its target-audit case requests that memory explicitly.

```bash
uv run python -m reasoning_ledger.image_acceptance \
  --catalog /path/to/artifacts/approval/compute-images.json \
  --image-id research-cpu --output /path/to/artifacts/acceptance
```

This command starts a disposable loopback HTTP organization server and enrolls a
worker through its public API. The production worker/runner executes all six
runtimes, Noether capabilities/discovery/factorization/Smith-form integration,
exact algebra/result artifacts, UID/capability/seccomp/read-only-root
and network checks, disk/PID/memory pressure, wall timeout, and running-job
cancellation. Exact submission replay, durable artifact bytes/digests, idle
result journals, and physical owned-container cleanup are checked. It creates
`acceptance.json` only after all cases and final cleanup/server shutdown succeed.
Failures do not produce a success receipt. No mock execution fallback exists.

The output directory is private and contains disposable server/worker secrets;
publish only `acceptance.json`, not the whole directory. A passing local receipt
does not prove public HTTPS, cloud provisioning, native clients, a multi-user
workflow, signatures, or license/source compliance. Those remain separate release
gates. Build and actual runtime proofs require a Docker-capable host; synthetic
OCI and protocol tests are not substitutes. Unavailable Docker fails the command
without a success receipt.
