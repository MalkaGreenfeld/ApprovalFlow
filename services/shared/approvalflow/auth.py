"""Self-signed JWT authentication with roles (N1).

Three roles, matching the user stories:

    submitter   create submissions, answer an info request on their own item
    approver    see the escalation queue, approve / reject / send back
    admin       the above, plus the policy configuration (F7) and the
                controller reports (F8, F10)

The signing key comes from the Dapr secret store, falling back to JWT_SECRET, so
no key material sits in the image. Every service verifies the token rather than
trusting the gateway to have done it: a service that accepts unauthenticated
internal calls is one misconfigured network away from being open.

AUTH_ENABLED=false is for the unit tests and a first local run before tokens are
wired up. It logs a warning, because a silently open API is worse than a closed
one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status

from .logging import get_logger
from .secrets import get_secret

logger = get_logger(__name__)

ROLE_SUBMITTER = "submitter"
ROLE_APPROVER = "approver"
ROLE_ADMIN = "admin"
ALL_ROLES = (ROLE_SUBMITTER, ROLE_APPROVER, ROLE_ADMIN)

ISSUER = os.getenv("JWT_ISSUER", "approvalflow")
AUDIENCE = os.getenv("JWT_AUDIENCE", "approvalflow-api")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
EXPIRY_MINUTES = int(os.getenv("JWT_EXPIRATION_MINUTES", "60"))


def auth_enabled() -> bool:
    """Whether tokens are enforced. Read per call so tests can toggle it."""
    return os.getenv("AUTH_ENABLED", "true").strip().lower() not in ("0", "false", "no")


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    subject: str
    roles: tuple[str, ...]
    email: str = ""

    def has_role(self, *roles: str) -> bool:
        return ROLE_ADMIN in self.roles or any(r in self.roles for r in roles)


#: The principal used when auth is switched off, so downstream code can assume
#: ``request.state.principal`` always exists.
ANONYMOUS = Principal(subject="anonymous", roles=ALL_ROLES, email="anonymous@local")


async def signing_key() -> str:
    """Signing key from the Dapr secret store, or ``JWT_SECRET``."""
    key = await get_secret("jwt:secret", env_fallback="JWT_SECRET", required=True)
    assert key is not None  # get_secret raises when required and missing
    return key


async def issue_token(
    subject: str, roles: list[str], *, email: str = "", expires_minutes: int | None = None
) -> tuple[str, int]:
    """Mint a signed token. Returns ``(token, expires_in_seconds)``.

    Raises:
        ValueError: on an unknown role, so a typo cannot mint a useless token.
    """
    unknown = [r for r in roles if r not in ALL_ROLES]
    if unknown:
        raise ValueError(f"unknown role(s): {', '.join(unknown)}")

    minutes = expires_minutes or EXPIRY_MINUTES
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "email": email or subject,
        "roles": roles,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    token = jwt.encode(payload, await signing_key(), algorithm=ALGORITHM)
    return token, minutes * 60


async def decode_token(token: str) -> Principal:
    """Verify a token and return the principal.

    Raises:
        HTTPException: 401 for an expired or otherwise invalid token.
    """
    try:
        claims = jwt.decode(
            token,
            await signing_key(),
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        ) from exc
    except jwt.InvalidTokenError as exc:
        logger.warning("Rejected token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc

    roles = claims.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    return Principal(
        subject=str(claims.get("sub", "")),
        roles=tuple(str(r) for r in roles),
        email=str(claims.get("email", "")),
    )


async def current_principal(request: Request) -> Principal:
    """FastAPI dependency resolving the caller from the ``Authorization`` header."""
    if not auth_enabled():
        request.state.principal = ANONYMOUS
        return ANONYMOUS

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = await decode_token(token.strip())
    request.state.principal = principal
    return principal


def require_roles(*roles: str):
    """Build a dependency that enforces one of ``roles`` (admin always passes).

    Usage::

        @app.post("/api/approvals/{cid}/approve")
        async def approve(principal: Principal = Depends(require_roles("approver"))):
            ...
    """

    async def _guard(principal: Principal = Depends(current_principal)) -> Principal:
        if not auth_enabled():
            return principal
        if not principal.has_role(*roles):
            logger.warning(
                "Authorization denied for %s (has %s, needs one of %s)",
                principal.subject,
                list(principal.roles),
                list(roles),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(roles)}",
            )
        return principal

    return _guard


def log_auth_mode(service: str) -> None:
    """Log the auth posture at startup so an open API is never a surprise."""
    if auth_enabled():
        logger.info("%s: JWT authentication ENABLED (roles enforced)", service)
    else:
        logger.warning(
            "%s: JWT authentication DISABLED via AUTH_ENABLED, every endpoint is "
            "open. Never do this outside local development.",
            service,
        )
