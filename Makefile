SHELL := /bin/sh

UV ?= uv
NPM ?= npm
PYTEST ?= $(UV) run pytest
RELEASE ?= build/reasoning-release.tar.gz
DEPLOY_STATE ?= $(HOME)/.reasoning-deploy
ONBOARD_STATE ?= $(HOME)/.reasoning-onboard
ONBOARD_REPLACE_TOKEN ?= 0
COMPUTE_BACKEND ?= auto
COMPUTE_RETAIN_CANDIDATE ?= false
SOURCE_BUNDLE ?=
SOURCE_BUCKET ?=
SOURCE_REGION ?=
SOURCE_STATE ?= $(HOME)/.reasoning-compute-publication
COMPUTE_PUBLICATION_STATE ?= $(HOME)/.reasoning-compute-publish
COMPUTE_HANDOFF ?=
ALLOW_DIRTY ?= 0
CHECK_BASE ?= origin/main
TESTS ?=
DEV_STATE ?=
DEV_FRONTEND_PORT ?= 5173
DEV_BACKEND_PORT ?= 9119
OPERATOR_HANDOFF ?=
OPERATOR_PUBLIC_KEY ?=
RECOVERY_KIT ?= $(HOME)/reasoning-operator-recovery.rnrk
BOT_SERVER ?=
BOT_PAIR_ONLY ?= 0
BOT_ALLOW_OTHER_USERS ?= 0
BOT_STATE ?= $(if $(wildcard $(HOME)/.reasoning-bot/config.json),$(HOME)/.reasoning-bot,$(if $(XDG_DATA_HOME),$(XDG_DATA_HOME),$(HOME)/.local/share)/reasoning/bots)
BOT_CONFIG ?= $(BOT_STATE)/config.json
DEV_REPOSITORY ?=
DEV_PROFILE ?= smoke
BOT_CLOUD_STATE ?= $(if $(XDG_DATA_HOME),$(XDG_DATA_HOME),$(HOME)/.local/share)/reasoning/cloud-bots/default

.DEFAULT_GOAL := help
.PHONY: dev dev-setup check-fast

.PHONY: help check build build-release deploy upgrade onboard doctor operator-sync operator-authorize operator-join operator-kit operator-recover inspect check-uv check-build-tools check-deploy-tools check-source
.PHONY: _node_check _node_build _node_build-release _node_dev _node_dev-setup _node_check-fast _node_upgrade _node_check-build-tools

# A separate make inherits the selected PATH throughout recipes and operator sessions.
check:
	@umask 022; /bin/sh web/with-node.sh $(MAKE) --no-print-directory _node_check

build build-release dev dev-setup check-fast upgrade check-build-tools:
	@/bin/sh web/with-node.sh $(MAKE) --no-print-directory _node_$@

help:
	@printf '%s\n' \
		'make check   Run the source-release verification suite' \
		'make dev     Start a disposable local organization with hot reload and simulated inference' \
		'make bot-connect Prompt for the server, pair a personal CLI bot, and keep it online' \
		'make bot-connect BOT_PAIR_ONLY=1  Pair without starting the runner' \
		'make bot-run    Start a paired bot with automatic reconnection; Ctrl-C to stop' \
		'make bot-run BOT_ALLOW_OTHER_USERS=1  Also accept mentions from other members of granted topics' \
		'make bot-status Inspect connection and retained invocation state' \
		'make dev-bot-config DEV_REPOSITORY=owner/repository  Enable local offline test profiles' \
		'make dev-bot-test DEV_PROFILE=smoke  Run an isolated development-box test with consent' \
		'make bot-doctor Check CLI isolation and connection without invoking inference' \
		'make bot-cloud  Create a dedicated DigitalOcean Codex/Claude Code bot with optional CAS/Lean' \
		'make bot-cloud-enable  Operator opt-in for web installer outbound SSH; no inbound SSH change' \
		'make compute-images Build and exercise the CPU image on GitHub Actions (no local Docker)' \
		'make compute-setup Enable Modal compute for this existing deployment, with CLI consent' \
		'make compute-source-publish Publish a maintainer source archive to Spaces, with private CLI consent' \
		'make compute-publish Sign exact observed-source provenance and verify public images on hosted runners' \
		'make email-setup   Enable Resend invitations and verification on this existing deployment' \
		'make check-fast Run scoped local checks; CHECK_BASE=revision TESTS="tests/path.py" override selection' \
		'make build   Build and verify build/reasoning-release.tar.gz' \
		'make deploy  Launch or resume the interactive first-organization setup' \
		'make upgrade Preserve the installed release, build this checkout, and upgrade interactively' \
		'make upgrade-resume Resume the exact retained upgrade without rebuilding' \
		'make onboard Prepare this computer, receive an authorized handoff, join and verify deployment access' \
		'make onboard ONBOARD_REPLACE_TOKEN=1  Replace this operator token privately and resume the join or verification' \
		'make doctor  Recheck operator access, installed release, and HTTPS; close SSH afterward' \
		'make operator-sync  Refresh independent operator state from the verified running server' \
		'make operator-authorize  Authorize another deployer; asks for the received public-key file' \
		'make operator-join OPERATOR_HANDOFF=/path/handoff.tar.gz OPERATOR_IDENTITY=/path/key DO_TOKEN_FILE=/path/token OPERATOR_HANDOFF_SHA256=…  Join as that deployer' \
		'make operator-kit RECOVERY_KIT=/secure/recovery.rnrk  Create an encrypted offline recovery operator' \
		'make operator-recover RECOVERY_KIT=/secure/recovery.rnrk DO_TOKEN_FILE=/secure/token  Recover on a new computer' \
		'make inspect Verify the existing release bundle' \
		'' \
		'Overrides: RELEASE=path DEPLOY_STATE=path ONBOARD_STATE=path COMPUTE_BACKEND=auto|none|modal OPERATOR_PUBLIC_KEY=path OPERATOR_HANDOFF=path RECOVERY_KIT=path'

check-uv:
	@command -v $(UV) >/dev/null || { printf '%s\n' 'Missing required tool: uv (https://docs.astral.sh/uv/)' >&2; exit 2; }

.PHONY: bot-connect bot-run bot-status bot-doctor bot-cloud bot-cloud-enable
.PHONY: dev-bot-config dev-bot-test
dev-bot-config: check-uv
	@test -n "$(DEV_REPOSITORY)" || { printf '%s\n' 'Set DEV_REPOSITORY to the exact GitHub owner/repository.' >&2; exit 2; }
	@/bin/sh web/with-node.sh $(UV) run --locked python -m reasoning_ledger.server.bot_testing \
		--state-dir "$(BOT_STATE)" configure --repository "$(DEV_REPOSITORY)"

dev-bot-test: check-uv
	@/bin/sh web/with-node.sh $(UV) run --locked python -m reasoning_ledger.server.bot_testing \
		--state-dir "$(BOT_STATE)" test --profile "$(DEV_PROFILE)"

bot-cloud-enable: check-uv
	@$(UV) run --locked reasoning-bot-cloud --enable-web --deploy-state "$(DEPLOY_STATE)"

bot-cloud: check-uv
	@$(UV) run --locked reasoning-bot-cloud $(if $(BOT_SERVER),--server "$(BOT_SERVER)") --state-dir "$(BOT_CLOUD_STATE)"

bot-connect: check-uv
	@set -eu; \
		pair_flag=''; \
		case "$(BOT_PAIR_ONLY)" in \
			0) :;; 1) pair_flag='--pair-only';; \
			*) printf '%s\n' 'BOT_PAIR_ONLY must be 0 or 1' >&2; exit 2;; \
		esac; \
		$(UV) run --locked reasoning-bot connect $(if $(BOT_SERVER),--server "$(BOT_SERVER)") --state-dir "$(BOT_STATE)" $$pair_flag

bot-run: check-uv
	@set -eu; \
		others=''; \
		case "$(BOT_ALLOW_OTHER_USERS)" in \
			0) :;; 1) others='--allow-other-users';; \
			*) printf '%s\n' 'BOT_ALLOW_OTHER_USERS must be 0 or 1' >&2; exit 2;; \
		esac; \
		$(UV) run --locked reasoning-bot run --state-dir "$(BOT_STATE)" --config "$(BOT_CONFIG)" $$others

bot-status bot-doctor: check-uv
	@$(UV) run --locked reasoning-bot $(patsubst bot-%,%,$@) --state-dir "$(BOT_STATE)" --config "$(BOT_CONFIG)"

.PHONY: compute-images compute-setup _compute-setup compute-source-publish compute-publish
compute-publish: check-uv
	@$(UV) run --locked --extra publication python -m reasoning_ledger.compute_publish \
		--source-root "$(CURDIR)" --state "$(COMPUTE_PUBLICATION_STATE)" --source-state "$(SOURCE_STATE)" \
		$(if $(COMPUTE_HANDOFF),--handoff "$(COMPUTE_HANDOFF)")

compute-source-publish: check-uv
	@$(UV) run --locked --extra publication python -m reasoning_ledger.image_review publish-source-spaces \
		$(if $(SOURCE_BUNDLE),--source-bundle "$(SOURCE_BUNDLE)") \
		$(if $(SOURCE_BUCKET),--bucket "$(SOURCE_BUCKET)") $(if $(SOURCE_REGION),--region "$(SOURCE_REGION)") --state "$(SOURCE_STATE)"

compute-images:
	@command -v gh >/dev/null || { printf '%s\n' 'Install GitHub CLI and run gh auth login first.' >&2; exit 2; }
	@case "$(COMPUTE_RETAIN_CANDIDATE)" in true|false) :;; *) printf '%s\n' 'COMPUTE_RETAIN_CANDIDATE must be true or false' >&2; exit 2;; esac
	@gh workflow run compute-images.yml --ref main -f retain_candidate="$(COMPUTE_RETAIN_CANDIDATE)"
	@printf '%s\n' 'Build queued. Evidence: gh run list --workflow compute-images.yml' 'This verifies a candidate; public publication has its own source/license approval gate.'

compute-setup: check-uv check-deploy-tools
	@$(UV) run --locked --extra modal reasoning-deploy operator-session --state-dir "$(DEPLOY_STATE)" -- \
		$(MAKE) --no-print-directory _compute-setup DEPLOY_STATE="$(DEPLOY_STATE)"

_compute-setup:
	@$(UV) run --locked --extra modal reasoning-deploy operator-sync --state-dir "$(DEPLOY_STATE)" --require-no-pending-upgrade
	@$(UV) run --locked --extra modal reasoning-deploy compute-setup --state-dir "$(DEPLOY_STATE)"

.PHONY: email-setup _email-setup
email-setup: check-uv check-deploy-tools
	@$(UV) run --locked reasoning-deploy operator-session --state-dir "$(DEPLOY_STATE)" -- \
		$(MAKE) --no-print-directory _email-setup DEPLOY_STATE="$(DEPLOY_STATE)"

_email-setup:
	@$(UV) run --locked reasoning-deploy operator-sync --state-dir "$(DEPLOY_STATE)" --require-no-pending-upgrade
	@$(UV) run --locked reasoning-deploy email-setup --state-dir "$(DEPLOY_STATE)"

_node_check-build-tools: check-uv
	@command -v git >/dev/null || { printf '%s\n' 'Missing required tool: git' >&2; exit 2; }
	@command -v node >/dev/null || { printf '%s\n' 'Missing required tool: Node.js' >&2; exit 2; }
	@command -v $(NPM) >/dev/null || { printf '%s\n' 'Missing required tool: npm' >&2; exit 2; }
	@node web/check-node.cjs

check-deploy-tools:
	@command -v ssh >/dev/null || { printf '%s\n' 'Missing required tool: ssh' >&2; exit 2; }
	@command -v scp >/dev/null || { printf '%s\n' 'Missing required tool: scp' >&2; exit 2; }
	@command -v ssh-keygen >/dev/null || { printf '%s\n' 'Missing required tool: ssh-keygen' >&2; exit 2; }
	@command -v ssh-add >/dev/null || { printf '%s\n' 'Missing required tool: ssh-add' >&2; exit 2; }
	@command -v ssh-agent >/dev/null || { printf '%s\n' 'Missing required tool: ssh-agent' >&2; exit 2; }
	@command -v dig >/dev/null || { printf '%s\n' 'Missing required tool: dig (install dnsutils on Ubuntu)' >&2; exit 2; }

check-source:
	@set -eu; \
		case "$(ALLOW_DIRTY)" in \
			0|false|no) test -z "$$(git status --porcelain)" || { \
				printf '%s\n' 'Release builds require a clean committed checkout.' >&2; exit 2; };; \
			1|true|yes) :;; \
			*) printf '%s\n' 'ALLOW_DIRTY must be 0 or 1' >&2; exit 2;; \
		esac

_node_check: check-build-tools
	$(UV) sync --locked --extra test
	$(UV) run ruff check src tests
	$(NPM) --prefix web ci --no-audit --no-fund
	$(NPM) --prefix web test
	$(NPM) --prefix web run build
	$(PYTEST) -q
	$(NPM) --prefix desktop ci --no-audit --no-fund
	$(NPM) --prefix desktop test

_node_dev-setup: check-build-tools
	$(UV) sync --locked --extra test --extra dev
	@$(UV) run --no-sync reasoning-dev dependencies --npm "$(NPM)"

_node_dev: dev-setup
	@$(UV) run --no-sync reasoning-dev serve --npm "$(NPM)" \
		--frontend-port "$(DEV_FRONTEND_PORT)" --backend-port "$(DEV_BACKEND_PORT)" $(if $(DEV_STATE),--state-dir "$(DEV_STATE)")

_node_check-fast: check-uv
	@$(UV) sync --locked --extra test --extra dev
	@$(UV) run --no-sync reasoning-dev check --npm "$(NPM)" --base "$(CHECK_BASE)" --tests $(TESTS)

_node_build: check-build-tools check-source build-release

_node_build-release: check-build-tools
	$(UV) sync --locked --inexact
	$(UV) run --no-sync reasoning-dev time --label "Frontend clean dependency install" -- $(NPM) --prefix web ci --no-audit --no-fund
	$(UV) run --no-sync reasoning-dev time --label "Frontend build" -- $(NPM) --prefix web run build
	@mkdir -p "$(dir $(RELEASE))"
	@set -eu; \
		temporary="$$(mktemp -d "$(dir $(RELEASE)).reasoning-release.XXXXXX")"; \
		dirty_flag=''; \
		case "$(ALLOW_DIRTY)" in 1|true|yes) dirty_flag='--allow-dirty';; 0|false|no) :;; *) printf '%s\n' 'ALLOW_DIRTY must be 0 or 1' >&2; exit 2;; esac; \
		$(UV) run --locked reasoning-deploy bundle --source-root . \
			--frontend web/dist --output "$$temporary/reasoning-release.tar.gz" $$dirty_flag; \
		mv "$$temporary/reasoning-release.tar.gz" "$(RELEASE)"; \
		rmdir "$$temporary"
	@$(UV) run --locked reasoning-deploy inspect --bundle "$(RELEASE)"
	@printf '\nRelease ready: %s\nFirst server: make deploy\nExisting server: make upgrade\n' "$(RELEASE)"

inspect: check-uv
	@test -f "$(RELEASE)" || { printf '%s\n' 'Release bundle is missing; run make build first.' >&2; exit 2; }
	@$(UV) run --locked reasoning-deploy inspect --bundle "$(RELEASE)"

deploy: check-uv check-deploy-tools
	@test -f "$(RELEASE)" || { printf '%s\n' 'Release bundle is missing; run make build first.' >&2; exit 2; }
	@set -eu; \
		case "$(COMPUTE_BACKEND)" in \
			none) $(UV) run --locked reasoning-deploy setup --bundle "$(RELEASE)" --state-dir "$(DEPLOY_STATE)" --compute-backend none;; \
			auto|modal) $(UV) run --locked --extra modal reasoning-deploy setup --bundle "$(RELEASE)" \
				--state-dir "$(DEPLOY_STATE)" --compute-backend "$(COMPUTE_BACKEND)";; \
			*) printf '%s\n' 'COMPUTE_BACKEND must be auto, none or modal' >&2; exit 2;; \
		esac

_node_upgrade: check-build-tools check-deploy-tools check-source
	@$(UV) run --locked reasoning-deploy operator-session --state-dir "$(DEPLOY_STATE)" -- \
		$(MAKE) --no-print-directory _upgrade RELEASE="$(RELEASE)" ALLOW_DIRTY="$(ALLOW_DIRTY)"

.PHONY: _upgrade _doctor _operator-authorize _operator-kit
_upgrade:
	@$(UV) run --locked reasoning-deploy operator-sync --state-dir "$(DEPLOY_STATE)" --require-no-pending-upgrade
	@$(UV) run --locked reasoning-deploy upgrade-stage --bundle "$(RELEASE)" --state-dir "$(DEPLOY_STATE)"
	@$(MAKE) --no-print-directory build-release RELEASE="$(RELEASE)" ALLOW_DIRTY="$(ALLOW_DIRTY)"
	@$(UV) run --locked reasoning-deploy upgrade --bundle "$(RELEASE)" --state-dir "$(DEPLOY_STATE)"

.PHONY: upgrade-resume
upgrade-resume: check-uv check-deploy-tools
	@$(UV) run --locked reasoning-deploy operator-session --state-dir "$(DEPLOY_STATE)" -- \
		$(UV) run --locked reasoning-deploy operator-resume-upgrade --state-dir "$(DEPLOY_STATE)"

onboard: check-uv check-deploy-tools
	@set -eu; \
		replace_flag=''; \
		case "$(ONBOARD_REPLACE_TOKEN)" in \
			0) :;; 1) replace_flag='--replace-token';; \
			*) printf '%s\n' 'ONBOARD_REPLACE_TOKEN must be 0 or 1' >&2; exit 2;; \
		esac; \
		$(UV) run --locked reasoning-deploy onboard --state-dir "$(DEPLOY_STATE)" --onboard-dir "$(ONBOARD_STATE)" $$replace_flag

operator-authorize: check-uv check-deploy-tools
	@$(UV) run --locked reasoning-deploy operator-session --state-dir "$(DEPLOY_STATE)" -- \
		$(MAKE) --no-print-directory _operator-authorize

# Without OPERATOR_PUBLIC_KEY the command asks for the key file; without
# OPERATOR_HANDOFF it writes DEPLOY_STATE/handoffs/operator-KEY-release-RELEASE.tar.gz.
_operator-authorize:
	@$(UV) run --locked reasoning-deploy operator-sync --state-dir "$(DEPLOY_STATE)"
	@set -eu; \
		set -- --state-dir "$(DEPLOY_STATE)" --bundle "$(RELEASE)"; \
		if [ -n "$(OPERATOR_PUBLIC_KEY)" ]; then set -- "$$@" --public-key "$(OPERATOR_PUBLIC_KEY)"; fi; \
		if [ -n "$(OPERATOR_HANDOFF)" ]; then set -- "$$@" --output "$(OPERATOR_HANDOFF)"; fi; \
		$(UV) run --locked reasoning-deploy operator-authorize "$$@"

operator-join: check-uv check-deploy-tools
	@test -n "$(OPERATOR_HANDOFF)" || { printf '%s\n' 'Set OPERATOR_HANDOFF to the received handoff archive.' >&2; exit 2; }
	@test -n "$(OPERATOR_IDENTITY)" || { printf '%s\n' 'Set OPERATOR_IDENTITY to your authorized SSH private key.' >&2; exit 2; }
	@test -n "$(DO_TOKEN_FILE)" || { printf '%s\n' 'Set DO_TOKEN_FILE to your own DigitalOcean token file.' >&2; exit 2; }
	@test -n "$(OPERATOR_HANDOFF_SHA256)" || { printf '%s\n' 'Set OPERATOR_HANDOFF_SHA256 to the digest confirmed by the existing operator.' >&2; exit 2; }
	@$(UV) run --locked reasoning-deploy operator-join --handoff "$(OPERATOR_HANDOFF)" \
		--state-dir "$(DEPLOY_STATE)" --identity "$(OPERATOR_IDENTITY)" --token-file "$(DO_TOKEN_FILE)" \
		--confirm-handoff-sha256 "$(OPERATOR_HANDOFF_SHA256)"

doctor: check-uv check-deploy-tools
	@$(UV) run --locked reasoning-deploy operator-session --state-dir "$(DEPLOY_STATE)" -- $(MAKE) --no-print-directory _doctor

_doctor:
	@$(UV) run --locked reasoning-deploy operator-sync --state-dir "$(DEPLOY_STATE)"
	@$(UV) run --locked reasoning-deploy operator-doctor --state-dir "$(DEPLOY_STATE)"

operator-sync: check-uv check-deploy-tools
	@$(UV) run --locked reasoning-deploy operator-session --state-dir "$(DEPLOY_STATE)" -- \
		$(UV) run --locked reasoning-deploy operator-sync --state-dir "$(DEPLOY_STATE)"

operator-kit: check-uv check-deploy-tools
	@$(UV) run --locked reasoning-deploy operator-session --state-dir "$(DEPLOY_STATE)" -- $(MAKE) --no-print-directory _operator-kit

_operator-kit:
	@$(UV) run --locked reasoning-deploy operator-sync --state-dir "$(DEPLOY_STATE)"
	@$(UV) run --locked reasoning-deploy operator-kit --state-dir "$(DEPLOY_STATE)" --output "$(RECOVERY_KIT)"

operator-recover: check-uv check-deploy-tools
	@test -n "$(DO_TOKEN_FILE)" || { printf '%s\n' 'Set DO_TOKEN_FILE to a fresh token from the deployment DigitalOcean Team.' >&2; exit 2; }
	@$(UV) run --locked reasoning-deploy operator-recover --kit "$(RECOVERY_KIT)" \
		--state-dir "$(DEPLOY_STATE)" --token-file "$(DO_TOKEN_FILE)"
