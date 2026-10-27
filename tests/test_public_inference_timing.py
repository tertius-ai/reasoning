"""Retained wait attribution without live providers, prompts, or guessed timings.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import json
from types import SimpleNamespace

import pytest
from test_public_inference import QUOTES, enqueue, prepare
from test_public_server import installation as installation

from reasoning_ledger.server import inference_store, inference_timing, usage_store
from reasoning_ledger.server.openrouter import Usage
from reasoning_ledger.server.usage_store import UsageStore


def test_timing_json_aggregates_keep_numeric_types_on_postgresql():
    from reasoning_ledger.server.postgres import query

    for field in ("recorded_at_ms", "elapsed_ms"):
        expression = f"SUM(CAST(json_extract(event_json,'$.{field}') AS BIGINT))"
        assert query(expression) == f"SUM(CAST(reasoning_json_extract(event_json,'$.{field}') AS BIGINT))"


def test_timing_reads_postgresql_mapping_rows_by_position_not_column_iteration():
    from reasoning_ledger.server.postgres import Row

    class Result:
        def __init__(self, values):
            self.values = values

        def fetchone(self):
            return self.values

        def fetchall(self):
            return self.values

    class Database:
        def execute(self, query, parameters):
            if "inference_call_events WHERE call_id=?" in query:
                assert parameters == ("call",)
                return Result([Row(["kind", "data_json"], ["submission", json.dumps({
                    "status": "running", "preparation_ms": {"credential_check": 2.5, "unknown": 1, "call_ledger": "text"},
                })])])
            assert parameters == ("organization", "run")
            if "'$.type')='timing'" in query:
                return Result([Row(["event_json"], [json.dumps({"type": "timing", "stage": "admission",
                                                                "spans_ms": {"reservation": 1.25}})])])
            if "FROM llm_events" in query:
                # PostgreSQL names both unaliased aggregates "min". Scalar
                # positions retain all three values even with duplicate names.
                return Result(Row(["min", "min", "sum"], [1000, 3000, 123]))
            return Result([Row(["id", "started_at_ms", "data_json"], ["call", 4000, json.dumps({
                "status": "succeeded", "latency_ms": 9000, "time_to_response_headers_ms": 4000,
                "time_to_first_token_ms": 6000,
            })])])

    timing = inference_timing.run_timing(Database(), {"organization_id": "organization", "id": "run", "status": "succeeded"})
    assert timing == {"wait_phase": None, "queue_wait_ms": 2000, "preparation_ms": 1000,
                      "provider_elapsed_ms": 9000, "time_to_response_headers_ms": 4000,
                      "time_to_first_token_ms": 6000, "tool_execution_ms": 123, "provider_calls": 1,
                      "spans_ms": {"admission": {"reservation": 1.25}, "worker": None,
                                   "provider": {"credential_check": 2.5}, "dispatch": None}}


def test_live_timing_separates_queue_preparation_provider_and_completed_tools(installation, monkeypatch):
    _, state, _, owner, topics = prepare(installation)
    clock = [1000.0]
    fake = SimpleNamespace(time=lambda: clock[0])
    for module in (inference_store, inference_timing, usage_store):
        monkeypatch.setattr(module, "time", fake)
    row = enqueue(state, owner, topics[1]["id"], models=["test/one"])[0]
    user, org, identity = owner["user_id"], owner["organization_id"], row["id"]
    assert state.run(user, org, identity)["timing"]["wait_phase"] == "local_queue"
    clock[0] += 2
    claimed = state.claim()
    assert state.run(user, org, identity)["timing"]["wait_phase"] == "preparing_context"
    clock[0] += 1
    ledger = UsageStore(state.store)
    call = ledger.begin_call(claimed, QUOTES[0])
    timing = state.run(user, org, identity)["timing"]
    assert timing["queue_wait_ms"] == 2000 and timing["preparation_ms"] == 1000
    assert timing["wait_phase"] == "provider_wait"
    clock[0] += 4
    ledger.response_headers(call)
    ledger.response_headers(call)
    timing = state.run(user, org, identity)["timing"]
    assert timing["time_to_response_headers_ms"] == timing["provider_elapsed_ms"] == 4000
    assert timing["wait_phase"] == "provider_wait"
    clock[0] += 2
    ledger.first_token(call)
    timing = state.run(user, org, identity)["timing"]
    assert timing["time_to_response_headers_ms"] == 4000
    assert timing["time_to_first_token_ms"] == 6000 and timing["wait_phase"] == "provider_stream"
    clock[0] += 3
    ledger.finish_call(call, status="succeeded", usage=Usage(8, 20, 10))
    with state.store.transaction(write=True) as db:
        state._event(db, org, identity, {"type": "agent", "phase": "tool_requested", "name": "ledger_read"})
    assert state.run(user, org, identity)["timing"]["wait_phase"] == "tool_execution"
    with state.store.transaction(write=True) as db:
        state._event(db, org, identity, {"type": "agent", "phase": "tool_completed", "elapsed_ms": 123,
                                       "result": {"private": "not timing output"}})
    state.finish(claimed, "succeeded", usage=Usage(8, 20, 10))
    timing = state.run(user, org, identity)["timing"]
    assert timing["wait_phase"] is None and timing["tool_execution_ms"] == 123
    assert timing["provider_elapsed_ms"] == 9000 and timing["provider_calls"] == 1
    assert "private" not in json.dumps(timing)
    before = ledger.call(user, org, call)
    ledger.response_headers(call)
    assert ledger.call(user, org, call) == before


def test_historical_missing_timing_is_unknown_not_reconstructed_from_rounded_seconds(installation):
    _, state, _, owner, topics = prepare(installation)
    row = enqueue(state, owner, topics[1]["id"], models=["test/one"])[0]
    claimed = state.claim()
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE llm_events SET event_json=? WHERE run_id=?", (json.dumps({"type": "state"}), row["id"]))
    state.finish(claimed, "failed", no_charge=True, error="provider_network")
    timing = state.run(owner["user_id"], owner["organization_id"], row["id"])["timing"]
    assert timing == {"wait_phase": None, "queue_wait_ms": None, "preparation_ms": None,
                      "provider_elapsed_ms": None, "time_to_response_headers_ms": None,
                      "time_to_first_token_ms": None, "tool_execution_ms": None, "provider_calls": 0,
                      "spans_ms": {"admission": None, "worker": None, "provider": None, "dispatch": None}}


def test_timing_remains_topic_permission_scoped(installation):
    from test_public_server import invited

    _, state, _, owner, topics = prepare(installation)
    row = enqueue(state, owner, topics[1]["id"], models=["test/one"])[0]
    _, _, member = invited(installation)
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE projects SET visibility='private' WHERE id IN "
                   "(SELECT project_id FROM channels WHERE id=?)", (topics[1]["channel_id"],))
    from reasoning_ledger.server.store import StoreError

    with pytest.raises(StoreError):
        state.run(member["user_id"], owner["organization_id"], row["id"])


def test_spans_accept_only_defined_stages_and_names_and_read_back_through_the_vocabulary():
    spans = inference_timing.Spans("provider")
    spans.mark("credential_check")
    spans.mark("credential_check")
    assert set(spans.record()) == {"credential_check"} and spans.record()["credential_check"] >= 0
    with pytest.raises(ValueError):
        spans.mark("prompt")
    with pytest.raises(ValueError):
        inference_timing.Spans("answer")
    with pytest.raises(ValueError):
        inference_timing.Spans("worker").mark("credential_check")
    # Retained values are read back only through the closed vocabulary.
    assert inference_timing.spans("dispatch", {"request_record": 1.5, "secret": 2, "client_setup": -1}) == {"request_record": 1.5}
    assert inference_timing.spans("dispatch", "not spans") is None
    for stage, names in inference_timing.SPANS.items():
        assert names and all(isinstance(name, str) and isinstance(text, str) and text for name, text in names.items()), stage


def test_admission_worker_provider_and_dispatch_spans_are_retained_without_content(installation):
    from fastapi.testclient import TestClient
    from test_public_inference import KEY, MODELS, Fixture, wait

    from reasoning_ledger.server.api import create_app

    config, state, _, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    marker = "Private timing marker 7f1c"
    fixture = Fixture()
    app = create_app(config, provider=fixture.provider)
    headers = {"Authorization": "Bearer " + owner["access_token"]}
    with TestClient(app, base_url=config.public_url, headers=headers) as client:
        root = f"/api/organizations/{org}"
        sequence = client.post(root + f"/topics/{topic}/messages", json={"content": marker, "client_id": "marker"}).json()["sequence"]
        body = {"models": MODELS[:1], "client_id": "timed-request", "through_sequence": sequence}
        accepted = client.post(root + f"/topics/{topic}/inference-runs", json=body)
        assert accepted.status_code == 202
        run = accepted.json()[0]
        # Admission spans commit with the request, so a lost reply's replay returns the same view.
        assert run["timing"]["spans_ms"]["admission"] is not None
        wait(lambda: client.get(root + f"/inference-runs/{run['id']}").json()["status"] == "succeeded")
        timing = client.get(root + f"/inference-runs/{run['id']}").json()["timing"]
        exported = client.get(root + f"/topics/{topic}/export").json()
    spans = timing["spans_ms"]
    assert set(spans["admission"]) == {"admission_check", "catalog_quote", "transaction_acquire", "access_check",
                                       "policy_check", "context_compile", "reservation", "ledger_write"}
    assert {"dispatch", "context_read", "tool_permissions", "tool_preparation", "goal_lookup", "executor_setup",
            "executor_start"} <= set(spans["worker"])
    assert {"context_transform", "purpose_lookup", "credential_check", "media_check", "quote_estimate",
            "reservation_lock", "reservation", "call_ledger_lock", "call_ledger"} <= set(spans["provider"])
    assert set(spans["dispatch"]) == {"request_record", "client_setup"}
    for stage, values in spans.items():
        assert set(values) <= set(inference_timing.SPANS[stage])
        assert all(type(value) in {int, float} and value >= 0 for value in values.values())
    call = UsageStore(state.store).calls(owner["user_id"], org)["calls"][0]
    assert call["preparation_ms"] == spans["provider"] and call["dispatch_ms"] == spans["dispatch"]
    summary = next(item for item in exported["runs"] if item["id"] == run["id"])
    assert summary["timing"]["spans_ms"] == spans
    with state.store.transaction() as db:
        retained = [json.loads(row[0]) for row in db.execute(
            "SELECT event_json FROM llm_events WHERE run_id=? AND json_extract(event_json,'$.type')='timing'", (run["id"],))]
        receipts = [row[0] for row in db.execute(
            "SELECT data_json FROM inference_call_events WHERE call_id=? AND kind IN ('submission','response_headers')",
            (call["id"],))]
    assert {item["stage"] for item in retained} == {"admission", "worker"}
    for text in [json.dumps(timing), json.dumps(retained), *receipts]:
        assert marker not in text and KEY not in text and "α" not in text


def test_first_call_preparation_avoids_redundant_transactions(installation, monkeypatch):
    from test_public_research_agent import ResearchFixture

    from reasoning_ledger.server import store as store_module
    from reasoning_ledger.server.openrouter import Quote
    from reasoning_ledger.server.research_agent import ResearchAgent

    _, state, _, owner, topics = prepare(installation)
    quote = Quote("test/one", "0.000001", "0.000002", "0", 200000, 8192, supports_tools=True)
    state.enqueue(owner["user_id"], owner["organization_id"], topics[1]["id"], "counted",
                  state.parameters(["test/one"], 256, 50, None), [quote])
    row = state.claim()
    fixture = ResearchFixture("plain")
    writes, original = [], store_module.Store.transaction

    def counted(self, *, write=False, **options):
        if write and not fixture.requests:
            writes.append(write)
        return original(self, write=write, **options)

    monkeypatch.setattr(store_module.Store, "transaction", counted)
    ResearchAgent(state, fixture.provider, row, quote).execute()
    # Before the first provider request: capabilities, pending additions, reservation, call header,
    # and one request record holding the round event, session and prefix. An uncompacted snapshot is not rewritten.
    assert len(writes) == 5
    assert state.run(owner["user_id"], owner["organization_id"], row["id"])["status"] == "succeeded"


def test_text_only_requests_skip_the_captured_media_transaction(installation, monkeypatch):
    from reasoning_ledger.server import store as store_module
    from reasoning_ledger.server.media_context import authorize_captured_media

    _, state, _, owner, topics = prepare(installation)
    row = enqueue(state, owner, topics[1]["id"], models=["test/one"])[0]
    opened = []
    original = store_module.Store.transaction

    def counted(self, **options):
        opened.append(options)
        return original(self, **options)

    monkeypatch.setattr(store_module.Store, "transaction", counted)
    authorize_captured_media(state.store, row, [{"role": "user", "content": "No attachments here."}])
    assert opened == []
    authorize_captured_media(state.store, row)
    assert len(opened) == 1


def test_identical_tool_schemas_are_checked_once_and_invalid_schemas_always_fail(monkeypatch):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    from reasoning_ledger.agents import tools

    checked = []
    original = Draft202012Validator.check_schema.__func__

    def counting(cls, schema, *args, **options):
        checked.append(json.dumps(schema, sort_keys=True))
        return original(cls, schema, *args, **options)

    monkeypatch.setattr(Draft202012Validator, "check_schema", classmethod(counting))
    monkeypatch.setattr(tools, "_CHECKED_SCHEMAS", set())
    schema = {"type": "object", "properties": {"query": {"type": "string", "maxLength": 17}}}
    for _ in range(3):
        tools.ToolDefinition("lookup", "Look up.", {**schema}, lambda **_: None)
    assert len(checked) == 1
    for _ in range(2):
        with pytest.raises(SchemaError):
            tools.ToolDefinition("broken", "Broken.", {"type": "no-such-type"}, lambda **_: None)
    assert len(checked) == 3


def test_balance_reads_only_holds_and_this_months_charges_through_indexes(installation):
    from datetime import UTC, datetime

    _, state, _, owner, topics = prepare(installation)
    org = owner["organization_id"]
    row = enqueue(state, owner, topics[1]["id"], models=["test/one"])[0]
    month = int(datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    with state.store.transaction(write=True) as db:
        request = db.execute("SELECT request_id,key_id,quote_json FROM llm_runs WHERE id=?", (row["id"],)).fetchone()
        cases = [("settled", 100, 7, month + 1), ("settled", 100, 11, month - 1), ("settled", 100, None, month + 2),
                 ("reserved", 13, None, None), ("uncertain", 17, None, None), ("released", 19, None, None)]
        for index, (accounting, reserved, actual, settled) in enumerate(cases):
            db.execute("INSERT INTO llm_runs(id,organization_id,request_id,model,key_id,quote_json,status,reserved_microusd,"
                       "accounting_state,actual_microusd,settled_at) VALUES(?,?,?,?,?,?,'succeeded',?,?,?,?)",
                       (f"balance-{index}", org, request[0], f"test/balance-{index}", request[1], request[2], reserved, accounting,
                        actual, settled))
        balance = inference_store.InferenceStore._balance(db, org)
        plans = [" ".join(str(part[-1]) for part in db.execute("EXPLAIN QUERY PLAN " + query, arguments).fetchall())
                 for query, arguments in (
                     ("SELECT data_json FROM inference_call_events WHERE call_id=? AND kind='completion'", ("call",)),
                     ("SELECT COALESCE((SELECT SUM(reserved_microusd) FROM llm_runs WHERE organization_id=? "
                      "AND accounting_state='reserved'),0)", (org,)),
                 )]
    assert balance["spent_microusd"] == 7
    assert balance["reserved_microusd"] == 13 + row["reserved_microusd"]
    assert balance["uncertain_microusd"] == 17
    assert "inference_call_receipt_lookup" in plans[0] and "llm_run_accounting" in plans[1]


def test_provider_clients_share_one_verifying_tls_context():
    import ssl

    from reasoning_ledger.server.openrouter import tls_context

    context = tls_context()
    assert context is tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
