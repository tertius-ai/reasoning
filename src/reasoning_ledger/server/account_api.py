"""Account recovery, self-service security, and organization administration."""

from typing import Annotated

from fastapi import Depends, Request, Response
from pydantic import Field, SecretStr

from .request_input import RequestInput as Input
from .store import StoreError


class RecoveryRequest(Input):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    client_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")]


class Link(Input):
    token: Annotated[SecretStr, Field(min_length=16, max_length=512)]


class RecoveryPassword(Link):
    password: Annotated[SecretStr, Field(min_length=12, max_length=1024)]


class ChangePassword(Input):
    current_password: Annotated[SecretStr, Field(min_length=1, max_length=1024)]
    password: Annotated[SecretStr, Field(min_length=12, max_length=1024)]


class RecoveryIdentity(Input):
    client_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")]


def install_routes(app, store, mail, authenticated, credentials, *, session_cookie, csrf_cookie, secure):
    User = Annotated[str, Depends(authenticated)]
    recovery = mail.recovery

    def clear_cookies(response):
        response.delete_cookie(session_cookie, secure=secure, httponly=True, samesite="strict")
        response.delete_cookie(csrf_cookie, secure=secure, httponly=False, samesite="strict")

    @app.get("/api/auth/password-reset")
    def recovery_available():
        return {"enabled": mail.config is not None, "worker_running": app.state.mail_worker.running}

    @app.post("/api/auth/password-reset", status_code=202)
    def request_recovery(body: RecoveryRequest, request: Request):
        peer = request.client.host if request.client else "unknown"
        return recovery.request(body.email, peer, body.client_id)

    @app.post("/api/auth/password-reset/confirm")
    def reset_password(body: RecoveryPassword, response: Response):
        result = recovery.reset(body.token.get_secret_value(), body.password.get_secret_value())
        clear_cookies(response)
        return result

    @app.post("/api/auth/password")
    def change_password(body: ChangePassword, response: Response, user: User):
        result = recovery.change_password(user, body.current_password.get_secret_value(), body.password.get_secret_value())
        clear_cookies(response)
        return result

    @app.post("/api/auth/sessions/revoke-others")
    def sign_out_other_sessions(body: Input, request: Request, user: User):
        credential = credentials(request)
        assert credential is not None
        return recovery.sign_out_others(user, credential[1])

    @app.post("/api/invitations/inspect")
    def inspect_invitation(body: Link, request: Request):
        peer = request.client.host if request.client else "unknown"
        return store.invitation_status(body.token.get_secret_value(), peer=peer)

    @app.get("/api/organizations/{organization_id}/user-administration")
    def users(organization_id: str, user: User):
        result = recovery.administration(user, organization_id)
        result["mail"]["worker_running"] = app.state.mail_worker.running
        return result

    @app.post("/api/organizations/{organization_id}/members/{member_id}/password-reset", status_code=202)
    def member_recovery(organization_id: str, member_id: str, body: RecoveryIdentity, user: User):
        with store.transaction() as db:
            store._require(db, user, organization_id, admin=True)
            member = db.execute("SELECT u.email FROM memberships m JOIN users u ON u.id=m.user_id "
                                "WHERE m.organization_id=? AND m.user_id=? AND u.account_kind='human'",
                                (organization_id, member_id)).fetchone()
            if member is None:
                raise StoreError(404, "organization member unavailable")
        return recovery.request(member["email"], "admin:" + user, body.client_id,
                                actor=user, organization=organization_id, target=member_id)
