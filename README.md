# Reasoning

Reasoning is Apache-2.0 software for self-hosted research organizations: chat,
model-assisted research, shared knowledge, bots, and isolated computation.
Projects contain `#channels` and alpha (human), beta (agent), and gamma
(workflow/compute) topics. Shared knowledge lives in the ledger, bibliography,
and research blog.

This is an academic open-source project by Benjamin Antieau and Deven Manam.

You own the installation, data, provider accounts, and cloud resources. There is
no hosted Reasoning account or control plane. Users build the server and optional
desktop client from this checkout; no published Reasoning package or prebuilt
client download is required.

The source version is **0.1.0**, with pre-1.0 interfaces and incomplete
real-provider acceptance. Read [release status](docs/release-status.md) before
using it for important or sensitive work.

## Deploy your first organization

You need macOS or Linux, Python 3.11 or newer, Git, Make, SSH/`scp`/`ssh-keygen`,
`dig`, [uv](https://docs.astral.sh/uv/), and Node.js `^20.19.0 || >=22.12.0`
with npm. Development and desktop builds use Node.js 24; with nvm, run
`nvm install && nvm use` in the checkout.

Have a funded DigitalOcean account, a domain or delegated subdomain you control,
and an OpenRouter account ready. Account/payment steps happen with the providers.

```sh
git clone https://github.com/tertius-ai/reasoning.git
cd reasoning
make build
make deploy
```

`make build` requires a clean committed checkout and verifies
`build/reasoning-release.tar.gz`. The guided deployment configures one Ubuntu
24.04 Droplet, DNS, HTTPS, the first owner, and OpenRouter. It opens provider
setup pages and accepts credentials privately. No billable server is created
before DNS delegation and the exact launch are confirmed.

The wizard offers skippable Modal compute setup. Compute also requires a
published, approved image catalog; setup alone does not establish that a cloud
job has run. Configure invitation email afterward with `make email-setup`.
See [Deployment](docs/deployment.md) and [Compute](docs/compute.md).

Private operator state defaults to `~/.reasoning-deploy`, outside the checkout.
For a paused first deployment, rerun `make deploy` with the same inputs and state.
Keep that state and the independent recovery kit or second-operator handoff.

## Existing servers and your own hardware

```sh
git pull --ff-only
make upgrade
```

Upgrades retain exact releases and a verified encrypted backup. If an upgrade
is interrupted, use `make upgrade-resume` with its original state; do not rebuild
the pending target. `make doctor` checks the installed server and HTTPS.

On a new operator computer, `make onboard` prepares a dedicated key and guides
the existing operator's private handoff. Application ownership and deployment
operator access are separate. See [Operations](docs/operations.md).

DigitalOcean is optional: [Deployment](docs/deployment.md) covers installation
on owned hardware, and [Server](docs/server.md#install-and-start) covers direct
local server setup. Keep the separate local-workbench server off public networks.

## Connect a bot

Install and authenticate Codex or Claude Code on the bot's computer, then run:

```sh
make bot-connect
```

The CLI asks for the server, opens browser pairing, and starts the runner. Pairing
selects the organization and delegated access. A bot uses its owner's CLI account,
not the organization's OpenRouter key. To restart a saved pairing, use
`make bot-run`; `make bot-doctor` provides read-only diagnostics.

See [Bots](docs/bots.md) for permissions, local CAS/Lean, developer tools, and
dedicated cloud bots. [Using Reasoning](docs/user-guide.md) explains the chat UI
and small acceptance tests. In-app **?** lists shortcuts and tools.

## Build the desktop client

The default Electron client connects to an existing server; it does not include
the Python server or copy provider credentials.

```sh
npm --prefix desktop ci
npm --prefix desktop start
```

See [Desktop](desktop/README.md) for source builds on macOS and Linux.

## Develop locally

```sh
make dev
make check-fast
```

The disposable development organization uses simulated inference: no cloud
account, real provider key, or deployment is needed. Changes reload locally.
See [Development](docs/development.md) for fixture logins and scoped checks.

For the complete source gate, run `make check`; then build the committed release
with `make build`. Scientific campaigns are opt-in with `uv run pytest --science -q`.
Fixture tests do not replace live provider or installed-client acceptance.
See [Contributing](CONTRIBUTING.md) and [Releasing](docs/releasing.md).

## Guides and source

- [Documentation index](docs/README.md): all current guides and interfaces.
- [Deployment](docs/deployment.md), [Operations](docs/operations.md), and
  [Backup](docs/backup.md): install, upgrade, and recover.
- [Server/API](docs/server.md), [Bots](docs/bots.md), and
  [Compute](docs/compute.md): integration and execution boundaries.
- [Security](SECURITY.md) and [Support](SUPPORT.md): report problems.

Python code is in `src/reasoning_ledger/`, the shared React frontend in `web/`,
the Electron client in `desktop/`, and compute inputs in `images/`.
Versioned interfaces and fixtures are in `contracts/`, `conformance/`, and `tests/`.

## License

Copyright Benjamin Antieau and Deven Manam. First-party code and documentation
use [Apache-2.0](LICENSE); [REUSE.toml](REUSE.toml) provides file attribution.
Third-party components retain their own notices in [NOTICE](NOTICE),
[LICENSES/](LICENSES/), and source-adjacent metadata.
