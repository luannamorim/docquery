"""Azure Entra ID bearer token validation.

The API is a resource server: it validates tokens the caller already obtained
from Entra ID and never requests any itself, so there is no client secret and no
login endpoint here.

Only the `roles` claim (app roles) is read for authorization. Delegated scopes
(`scp`) are deliberately ignored — app roles are assigned to both users and
service principals, so one mapping covers interactive and client-credentials
callers alike.

Every function takes Settings as a parameter instead of calling get_settings():
that is what lets tests swap configuration through app.dependency_overrides,
which the middleware layer cannot do.
"""

import logging
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from docquery.config import Settings, get_settings
from docquery.folders import normalize_segment

logger = logging.getLogger(__name__)

# Starlette's HTTPBearer answers a missing header with 403 and no
# WWW-Authenticate. auto_error=False hands us the None so we can raise the 401
# the OAuth 2.0 bearer spec asks for.
_bearer = HTTPBearer(auto_error=False)

_MISSING_TOKEN_HEADERS = {"WWW-Authenticate": "Bearer"}
_INVALID_TOKEN_HEADERS = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


def issuer_for(settings: Settings) -> str:
    return f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0"


def jwks_uri_for(settings: Settings) -> str:
    return (
        f"https://login.microsoftonline.com/{settings.azure_tenant_id}"
        "/discovery/v2.0/keys"
    )


@lru_cache
def _get_jwks_client(jwks_uri: str) -> jwt.PyJWKClient:
    """Cached JWKS client.

    PyJWKClient keeps the key set for `lifespan` seconds and refetches whenever a
    token carries an unknown `kid`, so Microsoft's key rotation needs no handling
    here. The timeout is well below the request timeout so an unreachable tenant
    fails fast instead of hanging the worker.
    """
    return jwt.PyJWKClient(jwks_uri, timeout=10)


def _get_signing_key(token: str, settings: Settings):
    """Resolve a token's signing key from the tenant's JWKS.

    Kept as its own function because it is the network boundary, and therefore
    the seam the test suite monkeypatches.
    """
    client = _get_jwks_client(jwks_uri_for(settings))
    return client.get_signing_key_from_jwt(token).key


def validate_token(token: str, settings: Settings) -> dict:
    """Verify a bearer token and return its claims.

    Rejections carry a single generic message: the specific reason (expired,
    wrong audience, bad signature) is logged server-side but never returned, so
    the response cannot be used to probe the expected issuer or audience.
    """
    try:
        key = _get_signing_key(token, settings)
        return jwt.decode(
            token,
            key,
            # Pinned: the key always comes from the JWKS, so a token asking for
            # HS256 (or none) must not be honoured.
            algorithms=["RS256"],
            # `aud` arrives as the bare client id or as the App ID URI depending
            # on how the caller requested the scope. Both name this same API.
            audience=[settings.azure_client_id, f"api://{settings.azure_client_id}"],
            issuer=issuer_for(settings),
            leeway=settings.auth_leeway_seconds,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWKClientConnectionError as exc:
        # Checked before PyJWTError: this is a subclass of it, and an unreachable
        # JWKS endpoint is our outage, not a bad token. Its sibling
        # PyJWKClientError ("no signing key matches this kid") is deliberately
        # left to the 401 branch — an unknown kid is a bad token.
        logger.error("JWKS fetch failed: %s", exc)
        raise HTTPException(
            status_code=503, detail="Authentication service unavailable"
        ) from exc
    except jwt.PyJWTError as exc:
        logger.warning("Token rejected: %s", type(exc).__name__)
        raise HTTPException(
            status_code=401, detail="Invalid token", headers=_INVALID_TOKEN_HEADERS
        ) from exc


SECTOR_ROLE_PREFIX = "sector."


def roles_to_sectors(roles: list[str], settings: Settings) -> list[str]:
    """Sectors a token may read, as the union of what its app roles grant.

    A role named `sector.<folder>` grants that folder by convention, so the
    common case needs no configuration at all. The prefix is what makes this
    safe: without it every app role would be a grant, and an unrelated one
    (Reader.All, User.Read) would silently open a folder that happened to share
    its name.

    `auth_role_sector_map` remains for the names the convention cannot carry —
    an Entra role value takes no spaces or accents, so a folder called
    "recursos humanos" needs an explicit entry. A mapped role uses only its
    mapped value: the map translates, it never adds to what the prefix derives.

    An empty result means the caller reads nothing. A token with no granting
    role is not an error; it simply reaches no compartment. Names are
    normalized the same way ingest normalizes folder names.
    """
    mapped_roles = {role for role, _ in settings.auth_role_sector_map}
    sectors = {
        sector
        for role, raw in settings.auth_role_sector_map
        if role in roles and (sector := normalize_segment(raw))
    }
    sectors |= {
        sector
        for role in roles
        if role not in mapped_roles
        and role.startswith(SECTOR_ROLE_PREFIX)
        and (sector := normalize_segment(role[len(SECTOR_ROLE_PREFIX) :]))
    }
    return sorted(sectors)


def require_auth(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer)
    ] = None,
) -> dict | None:
    """Validate the bearer token and return its claims, or None when auth is off.

    Single entry point for authentication. FastAPI caches a dependency's result
    per request, so a route that also reads the sectors validates the token
    once.
    """
    if not settings.auth_enabled:
        return None
    if credentials is None:
        raise HTTPException(
            status_code=401, detail="Not authenticated", headers=_MISSING_TOKEN_HEADERS
        )
    return validate_token(credentials.credentials, settings)


def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    claims: Annotated[dict | None, Depends(require_auth)] = None,
) -> None:
    """Allow only callers holding the ingestion role.

    Ingestion is the one operation that rewrites what everyone else reads: it
    deletes a source's chunks before writing the new ones, so triggering it
    repeatedly empties the corpus for every reader in turn. Requiring a token
    was never enough — that only proved the caller worked here.

    403 rather than the 404 the conversation routes answer with. A conversation
    id is a secret worth not confirming; /ingest is in the OpenAPI document, and
    pretending it is absent would only mislead the operator who does hold the
    role.

    With auth off there is no identity to check and the quickstart ingests
    without one, so the check does not apply — the same rule get_user_sectors
    follows.
    """
    if not settings.auth_enabled:
        return
    if settings.auth_admin_role not in (claims or {}).get("roles", []):
        logger.warning(
            "Ingestion refused: token lacks the %s role", settings.auth_admin_role
        )
        raise HTTPException(
            status_code=403, detail="This action requires the ingestion role"
        )
