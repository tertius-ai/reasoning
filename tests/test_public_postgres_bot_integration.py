"""Actual PostgreSQL parity for the bot's shared goals, context and delegates.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import pytest
import test_public_bot_integration as integration
import test_public_bots as bots
import test_public_inference_blog as blog_tests
from postgres_fixture import postgres_configuration as postgres_configuration

from reasoning_ledger.server.store import Store


@pytest.fixture
def fixture(postgres_configuration, tmp_path, monkeypatch):
    # The public bot fixture normally uses a server/ directory; direct it to the
    # disposable state/ directory which actually holds this fixture's PG config.
    monkeypatch.setattr(bots, "Store", lambda _path: Store(tmp_path / "state"))
    value = bots.fixture.__wrapped__(tmp_path)
    assert value[0]._postgres is not None
    return value


test_blog_scope = blog_tests.test_bot_published_edits_search_and_permission_revocation
test_account = integration.test_bot_account_tools_preserve_candidates_and_recheck_live_write_scope
test_followup = integration.test_tmp7_followup_receives_prior_question_and_gateway_tools
test_effort = integration.test_effort_is_retained_and_unsupported_bot_override_is_refused
test_context_revision = integration.test_bot_recent_context_preserves_admitted_message_revision
test_bot_goal = integration.test_bot_goal_needs_no_openrouter_key_and_continues_until_checkpoint
test_delegation = integration.test_delegation_is_narrow_and_integrates_once
