# Governance

Reasoning is maintained by the `public-maintainers` team. Benjamin Antieau and
Deven Manam coordinate the project and administer repository access. Maintainers
develop the project, review and merge pull requests, and make decisions about
security and compatibility.

## Decision making

All changes to the default branch go through pull requests and must pass the
required checks. A maintainer may merge their own pull request; separate approval
is not mandatory. Maintainers seek agreement for changes to durable data, public
contracts, deployment trust, licensing, or
project direction. When agreement is not available, a maintainer records the
decision and its tradeoffs in the pull request or a dated planning document.

No contributor gains commit, release, infrastructure, or provider access solely
through application roles or past contributions. Access is granted explicitly,
kept least-privilege, and can be revoked independently.

## Releases

Maintainers prepare release pull requests. Organization owners create version
tags only after the repository's release process and applicable real-system
gates are complete. Published version tags cannot be changed or deleted. Release
artifacts, tags, and notes must identify known limitations honestly. The current
process is in
[docs/releasing.md](docs/releasing.md).

## Changes to governance

Governance changes use an ordinary pull request with the required checks. The
current file is authoritative; Git history records prior arrangements.
