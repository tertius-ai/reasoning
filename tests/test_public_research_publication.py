"""Committed files remain deliverable across outer tool-receipt failures."""

import json
from dataclasses import replace

import pytest
from test_organization_research_workbench import source_run
from test_public_server import installation as installation

from reasoning_ledger.server.openrouter import Quote
from reasoning_ledger.server.research_artifacts import ResearchArtifacts
from reasoning_ledger.server.research_tools import ResearchTools
from reasoning_ledger.server.store import StoreError


class ProcessCrash(BaseException):
    pass


def fail_after_commit(monkeypatch, tools, operation, failure):
    definition = tools.registry.get(operation)

    def stop(**arguments):
        result = definition.handler(**arguments)
        if failure == "crash":
            raise ProcessCrash()
        with tools.store.transaction(write=True) as db:
            db.execute("UPDATE llm_runs SET cancel_requested=1 WHERE id=?", (tools.row["id"],))
        return result

    monkeypatch.setitem(tools.registry._tools, operation, replace(definition, handler=stop))


def assembly_arguments(tools):
    section = tools.invoke("artifact_write", {"path": "saved-section.tex",
                                              "text": "\\documentclass{article}\nRetained α and 研究.\n"})["result"]
    return {"path": "saved.tex", "parts": [{"path": "saved-section.tex", "revision": section["revision"]}]}


@pytest.mark.parametrize("operation", ["artifact_write", "artifact_assemble"])
@pytest.mark.parametrize("failure", ["crash", "cancel"])
def test_saved_file_link_survives_a_crash_or_cancel_before_outer_receipt(installation, monkeypatch, operation, failure):
    state, owner, topics, row, tools = source_run(installation)
    arguments = assembly_arguments(tools) if operation == "artifact_assemble" else {
        "path": "saved.tex", "text": "\\documentclass{article}\nRetained α and 研究.\n",
    }
    fail_after_commit(monkeypatch, tools, operation, failure)
    with pytest.raises(ProcessCrash if failure == "crash" else ValueError):
        tools.invoke(operation, arguments)
    with state.store.transaction() as db:
        receipt = db.execute("SELECT status FROM research_tool_receipts WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                             (row["id"],)).fetchone()
        assert receipt[0] == ("requested" if failure == "crash" else "failed")
        # An assembly's section was saved and published by the same run before the interrupted call.
        published = db.execute("SELECT COUNT(*) FROM research_artifact_publications WHERE run_id=?", (row["id"],)).fetchone()[0]
        assert published == (2 if operation == "artifact_assemble" else 1)
    if failure == "crash":
        state.recover()
    else:
        state.finish(row, "cancelled", no_charge=True)
    messages = state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])
    reply = next(message for message in messages if message.get("inference_run_id") == row["id"])
    assert reply["content"].startswith("Saved project files:") and "Partial response:" in reply["content"]
    assert [(file["path"], file["revision"]) for file in reply["files"]] == [("saved.tex", 1)]
    download = installation[2].get(reply["files"][0]["download_url"])
    assert download.content == "\\documentclass{article}\nRetained α and 研究.\n".encode()


def test_an_assembly_in_a_later_run_binds_its_publication_to_that_run(installation, monkeypatch):
    state, owner, topics, parent, tools = source_run(installation)
    arguments = assembly_arguments(tools)
    state.finish(parent, "succeeded", no_charge=True)
    quote = Quote(**json.loads(parent["quote_json"]))
    state.enqueue(owner["user_id"], owner["organization_id"], topics[1]["id"], "resume-document",
                  state.parameters([parent["model"]], 512, 50, None), [quote])
    resumed = state.claim()
    tools = ResearchTools(state, resumed)
    fail_after_commit(monkeypatch, tools, "artifact_assemble", "crash")
    with pytest.raises(ProcessCrash):
        tools.invoke("artifact_assemble", arguments)
    with state.store.transaction() as db:
        version = db.execute("SELECT v.run_id FROM research_artifact_versions v JOIN research_artifacts a ON a.id=v.artifact_id "
                             "WHERE a.path='saved.tex'").fetchone()
        assert version[0] == resumed["id"]
    state.recover()
    messages = state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])
    reply = next(message for message in messages if message.get("inference_run_id") == resumed["id"])
    assert reply["files"][0]["path"] == "saved.tex"


def test_publication_binding_failure_rolls_back_the_assembled_file(installation, monkeypatch):
    state, owner, topics, _, tools = source_run(installation)
    arguments = assembly_arguments(tools)

    def unavailable(*_args):
        raise StoreError(409, "publication binding unavailable")

    monkeypatch.setattr(ResearchArtifacts, "_record_publication", staticmethod(unavailable))
    with pytest.raises(ValueError, match="publication binding unavailable"):
        tools.invoke("artifact_assemble", arguments)
    files = ResearchArtifacts(state.store)
    # The section that was saved before stays; only the failed assembly is rolled back.
    assert [file["path"] for file in files.list(owner["user_id"], owner["organization_id"], topics[1]["id"])] == ["saved-section.tex"]
