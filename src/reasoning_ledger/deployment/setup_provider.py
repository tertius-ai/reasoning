"""Configure an approved organization policy/key over verified public HTTPS."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import httpx

from .host import InstallError, public_origin
from .private import read_document, read_private, write_document


class APIRejected(InstallError):
    """The server definitively rejected a key creation request."""


def configure_openrouter(config: dict, owner: dict, organization: str, directory: Path, *, transport=None):
    origin = public_origin(config["public_url"])
    if not isinstance(organization, str) or not re.fullmatch(r"[a-f0-9]{32}", organization):
        raise InstallError("organization setup returned an invalid organization identity")
    key = read_private(Path(config["openrouter_file"]), 4096).decode().strip()
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]
    if not 8 <= len(key) <= 4096 or not re.fullmatch(r"[a-f0-9]{32}", owner["setup_id"]):
        raise InstallError("invalid saved provider/owner input")
    label = "reasoning-setup-" + owner["setup_id"]
    desired = {"allowed_models": sorted(set(config["models"])), "monthly_budget_microusd": config["monthly_budget_microusd"]}
    path = directory / "provider-setup.json"
    binding = {"public_url": origin, "organization_id": organization, "label": label, "fingerprint": fingerprint, "policy": desired}
    if path.exists() or path.is_symlink():
        journal = read_document(path)
        if (set(journal) != {"format", "binding", "key_phase", "policy_phase"}
                or type(journal["format"]) is not int or journal["format"] != 1 or journal["binding"] != binding
                or journal["key_phase"] not in {"not_requested", "creating", "ready"}
                or journal["policy_phase"] not in {"not_requested", "setting", "ready"}):
            raise InstallError("provider setup does not match its original organization/key/policy; preserve the journal")
    else:
        journal = {"format": 1, "binding": binding, "key_phase": "not_requested", "policy_phase": "not_requested"}
        write_document(path, journal, new=True)
    with httpx.Client(base_url=origin, timeout=10, trust_env=False, follow_redirects=False, transport=transport) as client:
        def request(method, endpoint, body=None):
            try:
                with client.stream(method, endpoint, json=body) as response:
                    if method == "POST" and endpoint.endswith("provider-keys") and response.status_code in {400, 401, 403, 404, 422, 429}:
                        raise APIRejected("organization server rejected provider-key creation")
                    if response.status_code not in {200, 201, 204}:
                        raise InstallError("organization setup request failed; preserve state and check the server")
                    content = bytearray()
                    for block in response.iter_bytes(chunk_size=8192):
                        content.extend(block)
                        if len(content) > 1024**2:
                            raise InstallError("organization setup reply exceeds its allowance")
                    if response.status_code == 204:
                        if content:
                            raise InstallError("unexpected organization setup reply")
                        return None
                    return json.loads(content)
            except (httpx.HTTPError, ValueError) as exc:
                raise InstallError("organization setup reply is unavailable or invalid; preserve state and reconcile") from exc

        session = request("POST", "/api/auth/login", {"email": owner["email"], "password": owner["password"], "transport": "bearer"})
        if (not isinstance(session, dict) or not isinstance(session.get("access_token"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,2048}", session["access_token"])):
            raise InstallError("organization sign-in returned an invalid session")
        client.headers["Authorization"] = "Bearer " + session["access_token"]
        complete = False
        try:
            endpoint = "/api/organizations/" + organization
            policy = {"allowed_models": desired["allowed_models"]}
            def current_policy():
                value = request("GET", endpoint + "/policy")
                return {"allowed_models": value["allowed_models"]}

            actual = current_policy()
            if actual != policy:
                if actual != {"allowed_models": []} or journal["policy_phase"] != "not_requested":
                    raise InstallError("organization policy has changed; setup will not overwrite administrator edits")
                journal["policy_phase"] = "setting"
                write_document(path, journal)
                if request("PUT", endpoint + "/policy", policy) != policy:
                    raise InstallError("organization LLM policy is unverified")
            if current_policy() != policy:
                raise InstallError("organization LLM policy is unverified")
            journal["policy_phase"] = "ready"
            write_document(path, journal)
            keys = request("GET", endpoint + "/provider-keys")
            if not isinstance(keys, list) or any(not isinstance(row, dict) for row in keys):
                raise InstallError("organization provider-key metadata is invalid")
            matching = [row for row in keys if row.get("label") == label]
            if not matching:
                if journal["key_phase"] != "not_requested":
                    raise InstallError("provider key is absent after a recorded request; reconcile before creating another")
                journal["key_phase"] = "creating"
                write_document(path, journal)
                try:
                    request("POST", endpoint + "/provider-keys", {"provider": "openrouter", "label": label, "key": key})
                except APIRejected:
                    journal["key_phase"] = "not_requested"
                    write_document(path, journal)
                    raise
                keys = request("GET", endpoint + "/provider-keys")
                if not isinstance(keys, list) or any(not isinstance(row, dict) for row in keys):
                    raise InstallError("organization provider-key metadata is invalid")
                matching = [row for row in keys if row.get("label") == label]
            if (len(matching) != 1 or matching[0].get("fingerprint") != fingerprint
                    or matching[0].get("provider") != "openrouter" or matching[0].get("disabled_at") is not None):
                raise InstallError("organization provider key is conflicting, revoked, or unverified")
            complete = True
        finally:
            # Best effort on failure; a successful setup requires revocation too.
            try:
                request("POST", "/api/auth/logout", {})
            except InstallError:
                if complete:
                    raise
        journal["key_phase"] = "ready"
        write_document(path, journal)
