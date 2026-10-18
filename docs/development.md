# Fast development loop

Development commands use Python 3.12 from `.python-version`; `.nvmrc` selects
Node.js 24. Install uv and Node/npm first. Make build and development commands
keep a compatible Node already on PATH, or load nvm and select an installed
version from `.nvmrc` for that command and its children. They recognize `NVM_DIR`,
`$XDG_CONFIG_HOME/nvm`, and `$HOME/.nvm`; shell startup files are not required.
If the requested version is missing, run `nvm install` once in the checkout.
No automatic downloads or global default-version changes occur.
No cloud account, domain, deployment
state, or real OpenRouter key is required for this loop.

## Local organization

```sh
make dev
```

Open `http://127.0.0.1:5173/organization.html`. Sign in as
`owner@example.test`, `member@example.test`, or `viewer@example.test` with the
fixture password `Reasoning local development only`. The organization contains
a Development project, a playground channel, and alpha, beta, and gamma topics.

The Vite frontend and API listen only on 127.0.0.1. Frontend edits hot reload;
Python, prompt Markdown, and JSON changes restart the API through Uvicorn's
watcher. Authentication, project permissions, streaming, accounting, and topic
exports use the public application implementation. Development does not relax
production authentication or activate through production configuration.

Inference uses an in-memory provider fixture, never a real model endpoint.
Choose `development/fast` or `development/slow`; the latter streams more slowly
for cancellation and loading-state work. Recorded provider charges are zero;
fixture token counts are synthetic. Responses are labelled simulated, are not
mathematical evidence, and do not execute model-requested research or CAS tools.
Autonomous goals record a blocker because a simulated model cannot do research.
Outgoing webhook deliveries are also simulated; no mail service or compute
worker is configured. Do not enter real API keys or private research.

State is ephemeral by default, outside the repository and deployment state.
Ctrl-C stops both servers and removes that ephemeral state. To retain local
edits between sessions, choose a new empty private directory outside the source
checkout:

```sh
make dev DEV_STATE=/secure/local-reasoning-dev
```

Persistent state is marked as development-only. An existing unmarked directory,
including a deployment or application database, is refused rather than reset
or adopted. Keep development servers local; these known fixture credentials
are not appropriate for a public or shared server.

Use `DEV_FRONTEND_PORT=5174 DEV_BACKEND_PORT=9120` if the defaults are occupied.
Ports must be distinct and unprivileged; Vite uses the matching API proxy and
the application uses the matching browser origin. Ctrl-C, SIGTERM, and SIGHUP
stop the owned processes; one server exiting stops the other. Abrupt SIGKILL
cannot run cleanup.
Use separate worktrees and browser profiles for parallel local servers: browser
cookies are scoped to hostname, not port.

## Fast checks

For owner-configured offline checks through a connected developer bot or this
development box's terminal, see [Development-box tests](bots.md#development-box-tests).
`make dev-bot-config DEV_REPOSITORY=owner/repository` selects the trusted checkout;
`make dev-bot-test DEV_PROFILE=smoke` previews and tests a frozen tracked-source
snapshot without network access, deployment credentials or paid inference.
This requires Linux bubblewrap and installed test dependencies. The `frontend`
profile also needs the repository's Node version and frontend dependencies.
Source CI exercises all four profiles on a disposable Ubuntu runner.

PostgreSQL CI runs storage/leases, tools/context, and runtime/controllers in
separate shards. The required `PostgreSQL organization parity` check succeeds only
if every shard succeeds. Shards install/check psycopg and set a disposable
`REASONING_TEST_POSTGRES_DSN`. `--skip-sqlite-params` omits only explicitly
parameterized SQLite installation cases; non-parametric checks remain selected.
The backend deployment gate independently runs the SQLite cases. Without an
explicit test DSN, the shard flag refuses collection rather than silently
skipping PostgreSQL verification.

```sh
make check-fast
make check-fast TESTS="tests/test_public_provider_stream.py"
make check-fast CHECK_BASE=HEAD
```

Selection compares the working tree against `origin/main`, including committed
branch changes, staged/unstaged edits, and untracked source files. Set
`CHECK_BASE` explicitly when comparing against a different Git revision.
The command prints its selection and timing. It runs Python lint and public
server tenancy/authentication smoke tests, adds focused provider/research or
operator tests for those module families, and includes changed Python test
files. Markdown changes select documentation checks. `TESTS` overrides the
Python selection; frontend and desktop selection still follow changed paths.

Frontend changes run Node tests and the production typecheck/build. Desktop
changes run desktop contract tests. npm dependencies are reused only when
package metadata, lockfiles, Node version, platform, installed lock metadata,
and required compiler/package files still match the local cache marker.
`make dev-setup` prepares the same local environment without starting servers.
Release builds continue to use clean npm installs; source-build Python sync
preserves installed development extras without putting them in release runtime
requirements.

Fast checks are feedback, not proof that every affected path is covered. The
mapping is deliberately bounded; unrecognized code changes still get the
server smoke tests. Use explicit test paths for additional affected behavior.
Scientific campaigns require their separate `--science` invocation. Browser
journeys and real-provider/host gates remain separate.

`npm --prefix web run test:dev` exercises the hot-reload server in Chromium:
sign-in, simulated beta streaming, compact navigation and composer keyboard
behavior, prompt inspection during polling, compact alpha/beta/gamma JSON exports,
full-trace round trips, clipboard fallback/download, shared reply/goal model
pickers, expandable capabilities, gamma discussion and composer actions,
dedicated ledger tabs with long-record scrolling, and narrow-screen layout.
It makes no paid calls.

## CI and release checks

GitHub Actions runs lint, documentation, licensing, source-archive installation,
publication contracts, and the public-history secret scan on every PR. Integration
suites are selected conservatively from changed paths. Documentation-only PRs
skip integration suites; desktop changes select desktop contracts; frontend
changes select backend, browser, and developer-bot checks. Bot-client changes in
`bot_agent.py`, `bot_gateway_probe.py`, or `bot_runner.py` and their focused tests
select backend and developer-bot checks. Shared server/library code, dependency
changes, CI/test infrastructure, and unknown paths select every suite. Main and
manual workflow runs always select every suite.

Three deterministic deployment-test shards run with two isolated pytest workers
each; `--deployment --deployment-shard INDEX/3` selects each partition locally.
Source-installation, publication, and developer-bot tests run in their dedicated
jobs instead of repeating in deployment shards. The Backend deployment gate
requires source-quality checks and every selected deployment shard. Required
aggregate gates reject failed selection, failed tests, and unexpected skips;
deliberately unselected suites do not block merging. Four release-safety shards
cover migration/preparation, operator workflows, activation/rollback, and recovery;
Release upgrade and recovery requires all four when selected. No test is omitted by partitioning.
The frontend job runs
Node tests and its production build; the desktop job runs contract tests.
The frontend job also checks the local development workflow in Chromium:
login, simulated beta streaming, prompt inspection, and owned-process/state
cleanup. Locally, prepare with `make dev-setup` and run
`npm --prefix web run test:dev` after installing Playwright's Chromium browser.
The production-browser checks run `npm --prefix web run test:topic-agents`,
`npm --prefix web run test:direct-messages` and:

```bash
node --test --test-concurrency=1 web/tests/public-ledger.browser.mjs web/tests/public-blog.browser.mjs \
  web/tests/navigation.browser.mjs web/tests/topic-media.browser.mjs \
  web/tests/bot-controls.browser.mjs web/tests/bot-approvals.browser.mjs \
  web/tests/model-selection.browser.mjs web/tests/chat-polish.browser.mjs \
  web/tests/invitations.browser.mjs web/tests/organization-responsiveness.browser.mjs \
  web/tests/organization-requests.browser.mjs web/tests/research-requests.browser.mjs \
  web/tests/model-picker.browser.mjs web/tests/chat-links.browser.mjs
```

The batch runs serially because its Vite fixtures share a dependency optimizer
cache with different plugin configurations.
They exercise ordinary beta delegation, reactions, cutoff-safe forks, retained
comparisons, exact-revision human ledger checks, expandable graph statements,
blog deletion and accessible mathematics, phone navigation persistence,
scoped picture/PDF uploads and previews, bot-access editing, bot-model mentions,
invitation batches with live provider-status totals, conversation, model, and bot rendering
while secondary startup requests are held, and request budgets for opening and idling in a beta
topic, revisiting a topic, reopening model capabilities, revisiting a topic's tool receipts, and returning
from the Bibliography, measured against a production build, and a 451-model catalog in the reply picker
and capabilities browser: no rows while closed, then search, favorites, effort selection and the complete
list once open. They also open copied message links into a long topic, check notices for unavailable
links, and drive the `#` reference menu by keyboard through to the stored tokens and rendered names.
The PostgreSQL shards repeat storage, permission, review, fork, feedback, blog
deletion and navigation contracts against PostgreSQL 16. Organization-storage
contracts and leases/project-compute contracts run in separate parallel jobs;
the parity gate requires every shard to pass.
Jobs use locked dependencies, cached downloads, read-only repository permissions,
and no deployment/provider secrets. Superseded runs are cancelled.

`make check` is the complete ordinary source-release gate, including Python
tests outside the deployment subset. It prevents group/other write access on newly created test paths
and builds the frontend before Python tests that exercise its routes, so no
previous build or shell permission setting is required. `make build` still requires clean committed
source and verifies the release bundle. CI fixtures are not paid-provider,
installed-desktop, or live-deployment verification.

On a development machine with spare CPU and memory, the same complete gate can
run Python tests in parallel without changing the locked application dependencies:

```sh
make check PYTEST='uv run pytest -n 8'
```

This uses isolated test workers, not model agents, and does not omit tests.
Use fewer workers on smaller machines; the default remains sequential.

The offline browsing benchmark measures a burst of concurrent health,
conversation, bot-list, and model-configuration reads with real SQLite and a
mocked public catalog:

```sh
uv run --locked --extra test python tests/benchmarks/organization.py --clients 30 --configuration-delay-ms 25
```

The delay simulates slow configuration reads and can be set to zero. Output
reports per-route p95 latency and upstream catalog request count. It makes no
external requests or inference calls and uses disposable fixture state. This
small contention workload does not establish production capacity or reproduce
large conversation histories, active inference, or network/proxy behavior.

Builds report dependency-install and frontend-build durations.
Upgrade progress reports elapsed phase time on success and failure. Preserve
ownership checks, pinned SSH trust, backups, journals, and rollback safeguards;
use those measurements to identify costly release phases before optimizing them.
