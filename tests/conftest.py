"""Workbenches exercised through TestClient own an actual application lifespan."""

import hashlib
import os
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from reasoning_ledger.webapp import ChatSession

DEPLOYMENT_TESTS = {
    "test_public_admin_iteration.py",
    "test_public_compute_choice.py",
    "test_public_compute.py",
    "test_deployment_shards.py",
    "test_development.py",
    "test_public_backup.py",
    "test_public_bots.py",
    "test_public_bot_agents.py",
    "test_public_bot_integration.py",
    "test_public_bot_gateway_probe.py",
    "test_public_bot_cloud.py",
    "test_public_bot_terminal.py",
    "test_public_bot_runtime.py",
    "test_public_bot_approvals.py",
    "test_public_bot_conversations.py",
    "test_public_bot_runner.py",
    "test_public_bot_sharing.py",
    "test_public_bot_developer.py",
    "test_public_developer_testing.py",
    "test_public_claim_index_migration.py",
    "test_public_bot_diagnostics.py",
    "test_public_bot_media.py",
    "test_public_bot_media_download.py",
    "test_public_media.py",
    "test_public_media_limits.py",
    "test_public_postgres_media.py",
    "test_public_compute_setup.py",
    "test_public_project_compute.py",
    "test_public_lean_projects.py",
    "test_public_compute_images.py",
    "test_public_compute_publication.py",
    "test_public_compute_publish.py",
    "test_public_compute_provenance.py",
    "test_public_compute_hosted.py",
    "test_public_compute_review.py",
    "test_public_compute_spaces.py",
    "test_public_compute_context.py",
    "test_public_compute_sources.py",
    "test_public_compute_image_acceptance.py",
    "test_public_branding.py",
    "test_public_chat.py",
    "test_public_message_edits.py",
    "test_public_direct_messages.py",
    "test_public_chat_feedback.py",
    "test_public_navigation.py",
    "test_public_references.py",
    "test_public_postgres_navigation.py",
    "test_public_postgres_feedback.py",
    "test_public_ledger_human_checks.py",
    "test_public_ledger_convergence.py",
    "test_public_deployment.py",
    "test_public_dns.py",
    "test_public_feature_surface.py",
    "test_public_inference.py",
    "test_public_model_benchmarks.py",
    "test_public_model_preferences.py",
    "test_public_responsiveness.py",
    "test_public_inference_blog.py",
    "test_public_inference_ledger.py",
    "test_public_knowledge.py",
    "test_public_research_agent.py",
    "test_public_topic_agents.py",
    "test_public_research_export.py",
    "test_public_research_safety.py",
    "test_public_research_network.py",
    "test_organization_research_workbench.py",
    "test_public_usage.py",
    "test_public_input_security.py",
    "test_public_modal.py",
    "test_public_mail_setup.py",
    "test_public_modal_host.py",
    "test_public_modal_setup.py",
    "test_public_modal_ssh.py",
    "test_public_operator_handoff.py",
    "test_public_operator_doctor.py",
    "test_public_operator_sync.py",
    "test_public_operator_session.py",
    "test_public_onboard.py",
    "test_public_operator_recovery.py",
    "test_public_owner_setup.py",
    "test_public_provider_stream.py",
    "test_public_provisioning.py",
    "test_public_registrar_guides.py",
    "test_public_recovery_state.py",
    "test_public_rollback_state.py",
    "test_public_server.py",
    "test_public_server_diagnostics.py",
    "test_public_server_installation.py",
    "test_public_setup.py",
    "test_public_setup_upgrade.py",
    "test_public_setup_auth.py",
    "test_public_webhooks.py",
    "test_public_workflow_runs.py",
    "test_public_workflows.py",
    "test_public_work_contracts.py",
    "test_public_work_jobs.py",
    "test_public_work_context.py",
    "test_public_work_compaction.py",
    "test_public_work_tools.py",
    "test_public_work_compute.py",
    "test_public_work_overview.py",
    "test_public_work_workspaces.py",
    "test_public_work_sources.py",
    "test_public_work_provider.py",
    "test_public_work_worker.py",
    "test_public_work_controllers.py",
    "test_public_postgres.py",
    "test_python_distribution.py",
    "test_source_release.py",
    "test_public_email_verification.py",
    "test_public_source_statements.py",
    "test_public_subtree_stop.py",
}


def pytest_addoption(parser):
    parser.addoption("--science", action="store_true", help="include optional scientific evidence and local CAS campaigns")
    parser.addoption("--deployment", action="store_true", help="run the first organization-server deployment gate")
    parser.addoption("--deployment-shard", help="run deterministic deployment partition INDEX/TOTAL, for example 1/3")
    parser.addoption("--skip-sqlite-params", action="store_true", help="omit SQLite installation parameters in PostgreSQL fixture shards")


def pytest_configure(config):
    if config.getoption("--deployment-shard") and not config.getoption("--deployment"):
        raise pytest.UsageError("--deployment-shard requires --deployment")
    parse_deployment_shard(config.getoption("--deployment-shard"))
    if config.getoption("--science") and config.getoption("--deployment"):
        raise pytest.UsageError("--science and --deployment select different verification scopes; run them separately")
    if config.getoption("--skip-sqlite-params") and not os.environ.get("REASONING_TEST_POSTGRES_DSN"):
        raise pytest.UsageError("--skip-sqlite-params requires a disposable REASONING_TEST_POSTGRES_DSN")


def excluded_test(path, config):
    name = path.name
    if not name.startswith("test_") or path.suffix != ".py":
        return False
    if config.getoption("--deployment"):
        return name not in DEPLOYMENT_TESTS
    return not config.getoption("--science") and (name.startswith("test_science_") or name == "test_local_science_tools.py")


def pytest_ignore_collect(collection_path, config):
    return True if excluded_test(collection_path, config) else None


def pytest_collection_modifyitems(config, items):
    # Pytest can bypass ignore_collect for explicitly supplied file paths.
    # Keep the same opt-in policy there, with visible deselection accounting.
    selected, deselected = [], []
    shard = parse_deployment_shard(config.getoption("--deployment-shard"))
    for item in items:
        parameters = getattr(getattr(item, "callspec", None), "params", {})
        skip_sqlite = config.getoption("--skip-sqlite-params") and parameters.get("installation") == "sqlite"
        outside_shard = shard is not None and deployment_partition(item.nodeid, shard[1]) != shard[0]
        (deselected if excluded_test(item.path, config) or skip_sqlite or outside_shard else selected).append(item)
    if deselected:
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)


def parse_deployment_shard(value):
    if value is None:
        return None
    try:
        index, total = [int(part) for part in value.split("/")]
        if not 1 <= index <= total <= 16:
            raise ValueError
        return index - 1, total
    except (ValueError, AttributeError):
        raise pytest.UsageError("deployment shard must be INDEX/TOTAL with 1 <= INDEX <= TOTAL <= 16") from None


def deployment_partition(nodeid, total):
    return int.from_bytes(hashlib.sha256(nodeid.encode()).digest()[:8], "big") % total


@pytest.fixture(autouse=True)
def workbench_client_lifespans(monkeypatch):
    # Plain TestClient requests do not enter ASGI lifespan automatically. Start
    # workbench fixtures on their first request and close them before undoing
    # their subprocess/provider mocks. Explicit `with TestClient(...)` scopes
    # already have a portal and keep their own ownership. Public server clients
    # are unchanged.
    request = TestClient.request
    initialize = ChatSession.__init__
    standalone = []
    with ExitStack() as clients:

        def owned_session(session, *args, **kwargs):
            initialize(session, *args, **kwargs)
            if kwargs.get("background_workers") is None:
                standalone.append(session)

        def owned_request(client, *args, **kwargs):
            if getattr(client.app, "title", None) == "reasoning-ledger workbench" and client.portal is None:
                clients.enter_context(client)
            return request(client, *args, **kwargs)

        monkeypatch.setattr(TestClient, "request", owned_request)
        monkeypatch.setattr(ChatSession, "__init__", owned_session)
        try:
            yield
        finally:
            # Direct chat-session fixtures also own delayed goal wakes, Codex
            # children, and repair jobs. Signal all of them before joining any;
            # never let a completed test's session reach the next test's mocks.
            for session in standalone:
                session._shutdown_event.set()
            with ExitStack() as closing:
                for session in standalone:
                    closing.callback(session.close)
                    closing.callback(session.interrupt)
                    closing.callback(session._background_workers.close)
