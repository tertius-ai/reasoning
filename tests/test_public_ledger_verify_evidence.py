"""ledger_verify advertises and explains its required evidence.kind; no network or paid inference.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import pytest
from jsonschema import Draft202012Validator
from test_public_bots import fixture as bot_fixture

from reasoning_ledger import service
from reasoning_ledger.server.bot_runtime import BotRuntime
from reasoning_ledger.server.bot_tools import BotTools
from reasoning_ledger.server.store import StoreError
from reasoning_ledger.server.workbench_tools import MathematicalLedger, operation_schema
from reasoning_ledger.store import LedgerStore, LedgerStoreError


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def verifications(store):
    with store.transaction() as db:
        return db.execute("SELECT COUNT(*) FROM mathematical_records WHERE record_type='verification'").fetchone()[0]


def test_exposed_schema_states_the_stored_evidence_requirement():
    schema = operation_schema(service.verify_payload)
    evidence = schema["properties"]["evidence"]
    assert evidence["required"] == ["kind"] and evidence["properties"]["kind"]["minLength"] == 1
    validator = Draft202012Validator(schema)
    base = {"verdict": "flawed", "authority": "review", "proof_id": "prf_" + "0" * 24}
    for invalid in ({}, {"kind": ""}, {"text": "report"}, {"kind": 3}):
        assert not validator.is_valid({**base, "evidence": invalid})
    # Stored records keep further evidence fields; the schema does not reject them.
    assert validator.is_valid({**base, "evidence": {"kind": "gap", "summary": "step 3 assumes the result"}})
    assert validator.is_valid({**base, "evidence": {"kind": "proof-review", "text": "The substantive review report"}})


def test_workbench_refuses_missing_kind_with_actionable_400_and_accepts_valid_evidence(fixture):
    store, owner, topics, _, _, _ = fixture
    ledger = MathematicalLedger(store)

    def call(name, arguments, client_id):
        return ledger.execute(owner["user_id"], owner["organization_id"], name, arguments,
                              client_id=client_id, topic=topics[1], agent=True)

    claim = call("ledger_claim", {"statement": "Every integer n satisfies n = n."}, "claim")
    proof = call("ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."}, "proof")
    for evidence in ({}, {"kind": ""}, {"text": "missing kind"}):
        with pytest.raises(StoreError) as refused:
            call("ledger_verify", {"verdict": "flawed", "authority": "review", "evidence": evidence,
                                   "proof_id": proof["proof_id"]}, "verify-empty")
        assert refused.value.status == 400
        assert "evidence.kind" in refused.value.message and '"kind": "proof-review"' in refused.value.message
    assert verifications(store) == 0
    recorded = call("ledger_verify", {"verdict": "inconclusive", "authority": "review", "proof_id": proof["proof_id"],
                                      "evidence": {"kind": "proof-review", "text": "The substantive review report"}}, "verify")
    assert recorded["verdict"] == "inconclusive" and verifications(store) == 1


def test_local_ledger_service_explains_missing_kind(tmp_path):
    ledger = LedgerStore(tmp_path / "ledger")
    claim = service.claim_payload(ledger, "The integer 2 equals itself.")
    proof = service.prove_payload(ledger, claim["claim_id"], argument="Reflexivity.")
    with pytest.raises(LedgerStoreError, match="evidence.kind must be a nonempty string"):
        service.verify_payload(ledger, verdict="flawed", authority="review", evidence={}, proof_id=proof["proof_id"])


def test_bot_tool_validation_names_the_failing_field_without_echoing_values(fixture):
    store, owner, topics, _, _, bots = fixture
    org = owner["organization_id"]
    bot = bots.create(owner["user_id"], org, "Reviewer", topics[1:],
                      ["messages:read", "messages:write", "knowledge:read", "knowledge:write"], personal=True)
    with store.transaction(write=True) as db:
        db.execute("UPDATE topics SET knowledge_policy_json='[\"ledger\"]' WHERE id=?", (topics[1],))
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    run = runtime.enqueue(owner["user_id"], org, topics[1], f"bot/{bot['id']}/default", "review", prompt="Review")
    lease = (bot["token"], run["id"], runtime.claim(bot["token"])["lease"])
    tools = BotTools(runtime)
    claim = tools.invoke(*lease, "ledger_claim", {"statement": "Every integer n satisfies n = n."}, "claim")
    tools.invoke(*lease, "ledger_read", {"ledger_id": claim["claim_id"]}, "read")
    proof = tools.invoke(*lease, "ledger_prove", {"claim_id": claim["claim_id"], "argument": "Reflexivity."}, "proof")
    secret = "PRIVATE-REPORT-TEXT"
    with pytest.raises(StoreError) as refused:
        tools.invoke(*lease, "ledger_verify", {"verdict": "flawed", "authority": "review", "proof_id": proof["proof_id"],
                                                "evidence": {"text": secret}}, "verify-empty")
    assert refused.value.status == 400
    assert refused.value.message == "invalid mathematical tool arguments for ledger_verify: evidence missing required kind"
    with pytest.raises(StoreError) as refused:
        tools.invoke(*lease, "ledger_verify", {"verdict": secret, "authority": "review", "proof_id": proof["proof_id"],
                                                "evidence": {"kind": "proof-review"}}, "verify-verdict")
    assert "verdict violates enum" in refused.value.message and secret not in refused.value.message
    assert verifications(store) == 0
    recorded = tools.invoke(*lease, "ledger_verify", {"verdict": "flawed", "authority": "review", "proof_id": proof["proof_id"],
                                                      "evidence": {"kind": "proof-review", "text": "Step 2 is unjustified."}},
                            "verify")
    assert recorded["verdict"] == "flawed" and verifications(store) == 1
