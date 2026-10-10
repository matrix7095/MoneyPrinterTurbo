import hmac

from fastapi import Header, HTTPException, Request
from glance_core.agent_roles import ROLE_SPECS, AgentRole
from glance_core.security import verify_role_token

from app.business.runtime import get_runtime


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    return authorization.removeprefix("Bearer ").strip()


def require_admin(request: Request, authorization: str | None = Header(default=None)):
    token = bearer_token(authorization)
    expected = get_runtime(request).settings.admin_token.get_secret_value()
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def require_agent(
    request: Request, authorization: str | None = Header(default=None)
) -> AgentRole | None:
    token = bearer_token(authorization)
    expected = get_runtime(request).settings.bootstrap_agent_token.get_secret_value()
    if hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        return None
    if not token.isascii():
        raise HTTPException(status_code=401, detail="Invalid agent token")
    role = verify_role_token(expected, token, {role.value for role in ROLE_SPECS})
    if role is None:
        raise HTTPException(status_code=401, detail="Invalid agent token")
    return AgentRole(role)
