"""Lean environment keys, and the request and administrator approval of an organization's environments."""

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_public_compute import compute as compute
from test_public_compute import worker
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server import lean_environments as environments
from reasoning_ledger.server.compute_catalog import canonical
from reasoning_ledger.server.compute_environments import MAX_REQUESTED, MAX_REQUESTED_PER_MEMBER

TOOLCHAIN = "leanprover/lean4:v4.34.0-rc1\n"
MATHLIB = "1fe0a51a" + "0" * 32
PROJECT = "89d7dc3e" + "0" * 32


def entry(name, url, rev, **changes):
    return {"url": url, "type": "git", "subDir": None, "scope": "", "rev": rev, "name": name,
            "manifestFile": "lake-manifest.json", "inputRev": "main", "inherited": False, "configFile": "lakefile.lean",
            **changes}


def manifest(*entries, version="1.1.0", **changes):
    packages = list(entries) or [
        entry("mathlib", "https://github.com/leanprover-community/mathlib4", MATHLIB, inputRev="master", inherited=True),
        entry("Upstream", "https://github.com/example-group/upstream.git", PROJECT),
    ]
    return {"version": version, "packagesDir": ".lake/packages", "packages": packages, "name": "Project", "lakeDir": ".lake",
            **changes}


def text(value):
    return json.dumps(value, indent=1)


def refused(lean_toolchain=TOOLCHAIN, lake_manifest=None):
    with pytest.raises(environments.SpecificationError) as raised:
        environments.specification(lean_toolchain, text(manifest()) if lake_manifest is None else lake_manifest)
    return str(raised.value)


def test_specification_keeps_only_what_decides_the_environment():
    specification = environments.specification(TOOLCHAIN, text(manifest()))
    assert specification == {
        "format": 1,
        "toolchain": "leanprover/lean4:v4.34.0-rc1",
        "packages": [
            {"name": "Upstream", "url": "https://github.com/example-group/upstream.git", "rev": PROJECT, "sub_dir": None},
            {"name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4", "rev": MATHLIB, "sub_dir": None},
        ],
    }
    key = environments.key(specification)
    assert key == hashlib.sha256(canonical(specification).encode()).hexdigest()
    assert environments.validate(json.loads(canonical(specification))) == specification

    # Lake's bookkeeping, the manifest's layout and the order of its packages do not change the key.
    reordered = manifest(*reversed(manifest()["packages"]), name="Other", lakeDir="build")
    for value in reordered["packages"]:
        value.update(inputRev="v1", inherited=not value["inherited"], scope="x", configFile="lakefile.toml")
    assert environments.key(environments.specification("  leanprover/lean4:v4.34.0-rc1  ", json.dumps(reordered))) == key
    for version in (7, "1.0.0"):
        assert environments.key(environments.specification(TOOLCHAIN, text(manifest(version=version)))) == key


@pytest.mark.parametrize("change", [
    lambda value: value["packages"][0].update(rev="2" * 40),
    lambda value: value["packages"][0].update(url="https://github.com/leanprover-community/mathlib4.git"),
    lambda value: value["packages"][1].update(name="Upstream2"),
    lambda value: value["packages"][1].update(subDir="Sub/Dir"),
    lambda value: value["packages"].pop(),
])
def test_every_pinned_source_changes_the_key(change):
    key = environments.key(environments.specification(TOOLCHAIN, text(manifest())))
    changed = manifest()
    change(changed)
    assert environments.key(environments.specification(TOOLCHAIN, text(changed))) != key
    assert environments.key(environments.specification("leanprover/lean4:v4.34.0", text(manifest()))) != key


@pytest.mark.parametrize("name", ["leanprover/lean4:v4.24.0", "leanprover/lean4:v4.0.0", "leanprover/lean4:v4.34.0-rc12"])
def test_official_releases_and_release_candidates_are_accepted(name):
    assert environments.toolchain(name + "\n") == name


@pytest.mark.parametrize("name", [
    "leanprover/lean4:nightly-2026-11-01", "leanprover/lean4:stable", "leanprover/lean4:v5.0.0", "leanprover/lean4:4.24.0",
    "leanprover/lean4:v4.024.0", "leanprover/lean4:v4.24.0-rc0", "leanprover/lean4:v4.24", "v4.24.0",
    "someone/lean4:v4.24.0", "leanprover/lean4:v4.24.0\nleanprover/lean4:v4.25.0", "", "leanprover/lean4:v4.24.0 " + "x" * 128,
    None, 4,
])
def test_other_toolchains_are_refused(name):
    message = refused(name)
    assert "official Lean release" in message or "at most 128 bytes" in message


def test_urls_and_names_are_kept_as_written():
    keys = set()
    for url, name in (("https://github.com/o/r", "p"), ("https://github.com/o/r.git", "p"), ("https://github.com/O/R", "p"),
                      ("https://github.com/o/r", "P")):
        keys.add(environments.key(environments.specification(TOOLCHAIN, text(manifest(entry(name, url, "a" * 40))))))
    assert len(keys) == 4


@pytest.mark.parametrize(("change", "message"), [
    ({"type": "path", "dir": "../local"}, "not a git package"),
    ({"url": "https://gitlab.com/owner/repository"}, "https://github.com/"),
    ({"url": "http://github.com/owner/repository"}, "https://github.com/"),
    ({"url": "https://user@github.com/owner/repository"}, "https://github.com/"),
    ({"url": "https://github.com:443/owner/repository"}, "https://github.com/"),
    ({"url": "https://github.com/owner/repository/tree/main"}, "https://github.com/"),
    ({"url": "https://github.com/owner/repository?ref=main"}, "https://github.com/"),
    ({"url": "https://github.com/owner/.."}, "https://github.com/"),
    ({"url": "https://github.com/-owner/repository"}, "https://github.com/"),
    ({"url": None}, "https://github.com/"),
    ({"rev": "main"}, "40-character"),
    ({"rev": MATHLIB.upper()}, "40-character"),
    ({"rev": MATHLIB[:12]}, "40-character"),
    ({"rev": None}, "40-character"),
    ({"name": "../mathlib"}, "plain name"),
    ({"name": "«mathlib»"}, "plain name"),
    ({"name": ""}, "plain name"),
    ({"subDir": "../outside"}, "unsafe subDir"),
    ({"subDir": "/absolute"}, "unsafe subDir"),
    ({"subDir": "a//b"}, "unsafe subDir"),
    ({"subDir": "."}, "unsafe subDir"),
    ({"subDir": ["a"]}, "unsafe subDir"),
])
def test_unfetchable_or_unpinned_packages_are_refused(change, message):
    value = manifest()
    value["packages"][0].update(change)
    assert message in refused(lake_manifest=text(value))


@pytest.mark.parametrize(("lake_manifest", "message"), [
    (text(manifest(version=8)), "version 7, 1.0.0 or 1.1.0"),
    (text(manifest(version="7")), "version 7, 1.0.0 or 1.1.0"),
    (text(manifest(version="1.2.0")), "version 7, 1.0.0 or 1.1.0"),
    (text({"packages": []}), "version 7, 1.0.0 or 1.1.0"),
    (text([manifest()]), "version 7, 1.0.0 or 1.1.0"),
    (text(manifest(packages={"mathlib": {}})), "at most 64 entries"),
    (text(manifest(*[entry(f"p{index}", f"https://github.com/o/r{index}", "a" * 40) for index in range(65)])),
     "at most 64 entries"),
    (text(manifest(entry("p", "https://github.com/o/r", "a" * 40), entry("p", "https://github.com/o/s", "b" * 40))),
     "more than once"),
    (text(manifest(entry("p", "https://github.com/o/r", "a" * 40), "p")), "must be an object"),
    ('{"version": "1.1.0", "packages": [], "packages": []}', "repeats a field"),
    ('{"version": "1.1.0", "packages": [{"name": "p", "type": "git", "url": "https://github.com/o/r", '
     '"rev": "' + "a" * 40 + '", "rev": "' + "b" * 40 + '"}]}', "repeats a field"),
    ('{"version": "1.1.0", "packages": [], "x": NaN}', "not valid JSON"),
    ("not json", "not valid JSON"),
    (text(manifest(padding="x" * environments.MANIFEST_BYTES)), "at most 32 KiB"),
    (None, "at most 32 KiB"),
])
def test_malformed_manifests_are_refused(lake_manifest, message):
    if lake_manifest is None:
        with pytest.raises(environments.SpecificationError, match=message):
            environments.packages(None)
        return
    assert message in refused(lake_manifest=lake_manifest)


def test_a_manifest_without_packages_pins_only_the_toolchain():
    specification = environments.specification(TOOLCHAIN, text(manifest(packages=[])))
    assert specification["packages"] == []
    assert environments.sources(specification) == [{
        "kind": "toolchain", "name": "Lean", "url": "https://github.com/leanprover/lean4/releases/tag/v4.34.0-rc1",
        "revision": "v4.34.0-rc1", "sub_dir": None,
    }]


@pytest.mark.parametrize("change", [
    lambda value: value.update(extra=1),
    lambda value: value.update(format=True),
    lambda value: value.update(format=2),
    lambda value: value.update(toolchain="leanprover/lean4:nightly"),
    lambda value: value.update(toolchain=None),
    lambda value: value["packages"].reverse(),
    lambda value: value["packages"][0].update(type="git"),
    lambda value: value["packages"][0].pop("sub_dir"),
    lambda value: value["packages"][0].update(rev="main"),
    lambda value: value.update(packages=None),
])
def test_only_canonical_specifications_validate(change):
    value = environments.specification(TOOLCHAIN, text(manifest()))
    change(value)
    with pytest.raises(environments.SpecificationError):
        environments.validate(value)
    with pytest.raises(environments.SpecificationError):
        environments.key(value)


def test_sources_list_the_toolchain_release_then_every_package():
    specification = environments.specification(TOOLCHAIN, text(manifest(
        entry("Sub", "https://github.com/o/monorepo", "c" * 40, subDir="lean/Sub"), *manifest()["packages"])))
    assert environments.sources(specification) == [
        {"kind": "toolchain", "name": "Lean", "url": "https://github.com/leanprover/lean4/releases/tag/v4.34.0-rc1",
         "revision": "v4.34.0-rc1", "sub_dir": None},
        {"kind": "package", "name": "Sub", "url": "https://github.com/o/monorepo", "revision": "c" * 40, "sub_dir": "lean/Sub"},
        {"kind": "package", "name": "Upstream", "url": "https://github.com/example-group/upstream.git", "revision": PROJECT,
         "sub_dir": None},
        {"kind": "package", "name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4", "revision": MATHLIB,
         "sub_dir": None},
    ]


def root(owner):
    return f"/api/organizations/{owner['organization_id']}/compute-environments"


def ask(client, owner, lake_manifest=None, lean_toolchain=TOOLCHAIN):
    return client.post(root(owner), json={
        "lean_toolchain": lean_toolchain, "lake_manifest": text(manifest()) if lake_manifest is None else lake_manifest})


def decide(client, owner, record, decision="approved", key=None):
    return client.put(f"{root(owner)}/{record['id']}/decision", json={"decision": decision, "key": key or record["key"]})


def audit(store, owner):
    with store.transaction() as db:
        return [tuple(row) for row in db.execute(
            "SELECT action,target_id FROM audit_events WHERE organization_id=? AND action LIKE 'compute.environment.%' "
            "ORDER BY rowid", (owner["organization_id"],))]


def test_a_member_asks_once_and_an_administrator_approves_exactly_the_listed_sources(installation):
    _, store, client, owner = installation
    _, member_client, member = invited(installation)
    response = ask(member_client, owner)
    assert response.status_code == 200, response.text
    record = response.json()
    specification = environments.specification(TOOLCHAIN, text(manifest()))
    assert record["status"] == "requested"
    assert record["key"] == environments.key(specification)
    assert record["specification"] == specification
    assert record["sources"] == environments.sources(specification)
    assert (record["requested_by"], record["decided_by"], record["decided_at"]) == (member["user_id"], None, None)
    assert record["organization_id"] == owner["organization_id"]

    # The same dependencies, however the manifest is laid out, name the same record.
    reordered = manifest(*reversed(manifest()["packages"]))
    again = ask(client, owner, json.dumps(reordered))
    assert again.status_code == 200 and again.json() == record
    assert member_client.get(root(owner)).json() == [record]
    assert member_client.get(f"{root(owner)}/{record['id']}").json() == record

    # Members cannot decide, and a decision must name the record's key.
    assert decide(member_client, owner, record).status_code == 403
    wrong = decide(client, owner, record, key="f" * 64)
    assert wrong.status_code == 409 and "different Lean environment" in wrong.json()["detail"]
    approved = decide(client, owner, record)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["decided_by"] == owner["user_id"] and approved.json()["decided_at"] > 0
    assert {key: value for key, value in approved.json().items() if key not in {"status", "decided_by", "decided_at"}} == {
        key: value for key, value in record.items() if key not in {"status", "decided_by", "decided_at"}}

    # Approval is final, repeating it changes nothing, and asking again reuses the environment.
    assert decide(client, owner, record).json() == approved.json()
    final = decide(client, owner, record, "declined")
    assert final.status_code == 409 and "already approved" in final.json()["detail"]
    assert ask(member_client, owner).json() == approved.json()
    assert audit(store, owner) == [("compute.environment.request", record["id"]), ("compute.environment.approve", record["id"])]


def test_a_declined_environment_stays_declined_until_an_administrator_approves_it(installation):
    _, store, client, owner = installation
    record = ask(client, owner).json()
    declined = decide(client, owner, record, "declined")
    assert declined.status_code == 200 and declined.json()["status"] == "declined"
    _, member_client, _ = invited(installation)
    assert ask(member_client, owner).json() == declined.json()
    assert decide(client, owner, record, "declined").json() == declined.json()
    approved = decide(client, owner, record)
    assert approved.json()["status"] == "approved"
    assert [action for action, _ in audit(store, owner)] == [
        "compute.environment.request", "compute.environment.decline", "compute.environment.approve"]


def test_viewers_read_but_cannot_ask_and_other_organizations_see_nothing(installation):
    _, _, client, owner = installation
    record = ask(client, owner).json()
    _, viewer, _ = invited(installation, role="viewer", email="viewer@example.org")
    assert viewer.get(root(owner)).json() == [record]
    assert ask(viewer, owner).status_code == 403
    assert decide(viewer, owner, record).status_code == 403

    _, admin_client, _ = invited(installation, role="admin", email="admin@example.org")
    assert decide(admin_client, owner, record).json()["status"] == "approved"

    outsider_org = client.post("/api/organizations", json={"name": "Elsewhere", "credential_mode": "none"})
    assert outsider_org.status_code == 201, outsider_org.text
    other = {"organization_id": outsider_org.json()["id"]}
    assert client.get(root(other)).json() == []
    assert client.get(f"{root(other)}/{record['id']}").status_code == 404
    assert decide(client, other, record).status_code == 404


def test_invalid_requests_and_decisions_are_refused(installation):
    _, _, client, owner = installation
    bad = manifest()
    bad["packages"][0]["url"] = "https://example.org/mathlib4"
    response = ask(client, owner, text(bad))
    assert response.status_code == 400 and "https://github.com/" in response.json()["detail"]
    assert ask(client, owner, lean_toolchain="leanprover/lean4:nightly-2026-11-01").status_code == 400
    assert client.post(root(owner), json={"lean_toolchain": TOOLCHAIN}).status_code == 422
    assert client.post(root(owner), json={"lean_toolchain": TOOLCHAIN, "lake_manifest": "{}", "extra": 1}).status_code == 422
    assert client.get(root(owner)).json() == []
    record = ask(client, owner).json()
    for body in ({"decision": "maybe", "key": record["key"]}, {"decision": "approved", "key": "F" * 64},
                 {"decision": "approved"}, {"decision": "approved", "key": record["key"], "extra": 1}):
        assert client.put(f"{root(owner)}/{record['id']}/decision", json=body).status_code == 422
    assert decide(client, owner, {"id": "0" * 32, "key": record["key"]}).status_code == 404


def test_waiting_requests_are_bounded_per_member_and_per_organization(installation):
    _, _, client, owner = installation
    pinned = iter(range(10_000))

    def new(asker):
        return ask(asker, owner, text(manifest(entry("p", "https://github.com/o/r", f"{next(pinned):040x}"))))

    askers = [client] + [invited(installation, email=f"m{index}@example.org")[1]
                         for index in range(MAX_REQUESTED // MAX_REQUESTED_PER_MEMBER)]
    for _ in range(MAX_REQUESTED_PER_MEMBER):
        assert new(client).status_code == 200
    mine = new(client)
    assert mine.status_code == 429 and "you have 4" in mine.json()["detail"]
    for asker in askers[1:-1]:
        for _ in range(MAX_REQUESTED_PER_MEMBER):
            assert new(asker).status_code == 200
    full = new(askers[-1])
    assert full.status_code == 429 and "this organization has 16" in full.json()["detail"]
    # Asking for an environment the organization already has is never refused.
    existing = client.get(root(owner)).json()[-1]
    revision = existing["specification"]["packages"][0]["rev"]
    assert ask(askers[-1], owner, text(manifest(entry("p", "https://github.com/o/r", revision)))).json() == existing
    assert decide(client, owner, existing, "declined").status_code == 200
    assert new(askers[-1]).status_code == 200


def test_requests_list_waiting_first_then_newest(installation):
    _, _, client, owner = installation
    records = []
    for index in range(3):
        client.app.state.compute_environments.clock = lambda index=index: 1_800_000_000 + index
        records.append(ask(client, owner, text(manifest(entry("p", "https://github.com/o/r", f"{index:040x}")))).json())
    assert [record["requested_at"] for record in records] == [1_800_000_000, 1_800_000_001, 1_800_000_002]
    decide(client, owner, records[2])
    assert [record["id"] for record in client.get(root(owner)).json()] == [records[1]["id"], records[0]["id"], records[2]["id"]]


def test_the_database_keeps_sources_and_approvals_fixed(installation):
    _, store, client, owner = installation
    record = ask(client, owner).json()
    changes = ("environment_key='" + "f" * 64 + "'", "specification_json='{}'", "requested_by='" + owner["user_id"] + "x'",
               "requested_at=1", "organization_id='other'")
    with store.transaction(write=True) as db:
        for change in changes:
            with pytest.raises(sqlite3.DatabaseError, match="names its sources for good"):
                db.execute(f"UPDATE compute_environments SET {change} WHERE id=?", (record["id"],))
        for change in ("status='approved'", "status='declined',decided_by=NULL,decided_at=NULL"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(f"UPDATE compute_environments SET {change} WHERE id=?", (record["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO compute_environments(id,organization_id,environment_key,specification_json,status,"
                       "requested_by,requested_at) VALUES('x',?,?,'{}','requested',?,1)",
                       (owner["organization_id"], record["key"], owner["user_id"]))
    decide(client, owner, record)
    with store.transaction(write=True) as db, pytest.raises(sqlite3.DatabaseError, match="stays approved"):
        db.execute("UPDATE compute_environments SET status='declined' WHERE id=?", (record["id"],))


def test_a_relay_reads_only_approved_environments_of_its_own_organization(compute):
    config, _, _, client, owner, _ = compute
    record = ask(client, owner).json()
    enrolled, relay = worker(compute)
    path = f"/api/worker/compute-environments/{record['id']}"
    assert relay.get(path).status_code == 404
    decide(client, owner, record)
    response = relay.get(path)
    assert response.status_code == 200, response.text
    assert response.json() == {"id": record["id"], "organization_id": owner["organization_id"], "key": record["key"],
                               "status": "approved", "specification": record["specification"]}
    assert relay.get("/api/worker/compute-environments/" + "f" * 32).status_code == 404
    assert TestClient(client.app, base_url=config.public_url).get(path).status_code == 401

    elsewhere = client.post("/api/organizations", json={"name": "Elsewhere", "credential_mode": "none"})
    assert elsewhere.status_code == 201, elsewhere.text
    other = {"organization_id": elsewhere.json()["id"]}
    theirs = ask(client, other, lean_toolchain="leanprover/lean4:v4.24.0\n").json()
    assert decide(client, other, theirs).json()["status"] == "approved"
    assert relay.get(f"/api/worker/compute-environments/{theirs['id']}").status_code == 404

    revoked = client.delete(f"/api/organizations/{owner['organization_id']}/compute-workers/{enrolled['id']}")
    assert revoked.status_code == 204
    assert relay.get(path).status_code == 401
