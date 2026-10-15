# Contributing

Reasoning welcomes focused bug reports, documentation improvements, tests, and
code changes. The project is pre-1.0: public interfaces can still change, but a
change must preserve durable state and security boundaries deliberately.

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report
security-sensitive problems through [SECURITY.md](SECURITY.md), not a public
issue.

## Before opening a change

1. Search existing issues and pull requests.
2. For a large feature, data migration, public-contract change, or new cloud
   dependency, open a design issue before implementation.
3. Keep unrelated work out of the change. Never commit credentials, deployment
   state, local databases, generated provider tokens, or private research data.
4. Add or update tests and current documentation in the same change as behavior.

Current guides describe how the code works now. Dated design history belongs in
`docs/planning/`; living guides must not accumulate migration narratives or
references to removed implementations.

## Development setup

The package requires Python 3.11 or newer. Development uses Python 3.12 through
`.python-version`, [uv](https://docs.astral.sh/uv/), and Node.js 24 through
`.nvmrc` (within the frontend's supported engine range).

```sh
git clone https://github.com/tertius-ai/reasoning.git
cd reasoning
make dev
```

The [development guide](docs/development.md) covers fixture accounts, hot
reload, persistent local state, and simulated inference. `make dev-setup`
prepares dependencies without starting servers. Install desktop dependencies
only when working on the Electron client:

```sh
npm --prefix desktop ci
```

## Tests

Use changed-subsystem checks for the local feedback loop:

```sh
make check-fast
make check-fast TESTS="tests/test_public_provider_stream.py"
```

The command reports its scope; it is not a release gate. CI runs the deployment
fixtures and independent frontend/desktop checks. Before a release, run the
complete source gate and applicable behavior journeys:

```sh
uv run ruff check src tests
uv run pytest -q
npm --prefix web test
npm --prefix web run build
```

For desktop changes:

```sh
npm --prefix desktop test
npm --prefix desktop run build:local
```

For deployment and public-server changes:

```sh
uv run pytest --deployment -q
```

`make build` must run from a clean committed checkout. It synchronizes the
locked runtime dependencies without removing installed development extras,
builds the web application, collects verified Python source, creates a
content-verified release bundle, and inspects that bundle. It is not a substitute
for tests.

Optional browser journeys and real-provider gates are listed in
[release status](docs/release-status.md) and the relevant guide. Do not describe
fixture or mock-provider success as real-host verification.

## Change standards

- Preserve existing user data unless the change includes a tested, atomic
  migration.
- Treat retryability, idempotency, and uncertain external outcomes as product
  behavior, not exceptional cleanup.
- Keep secrets out of command output, receipts, exceptions, tests, and fixtures.
- Update versioned contracts when wire behavior changes, and document the
  compatibility rule.
- Add third-party license and notice material when introducing vendored or
  redistributed content.
- Use clear commit messages that explain the user-visible or operational result.

## Pull requests

All changes to the default branch use pull requests. The public maintainer team
can merge its own pull requests after required checks pass; a separate approval
is not mandatory. Other contributors submit pull requests for a maintainer to
consider and merge.

A pull request should state:

- the problem and resulting behavior;
- affected security, persistence, or compatibility boundaries;
- tests actually run; and
- remaining real-provider or platform verification.

By contributing, you agree that your contribution is licensed under the
repository's [Apache License 2.0](LICENSE).
