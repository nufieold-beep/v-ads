"""
Authentication router – Bearer token API authentication.

Follows the Adtelligent SSP model where API clients authenticate via
POST /v1/token and receive a bearer token for subsequent API calls.

Endpoint:
    POST /api/v1/token   – Authenticate and receive a Bearer access token

The token is a time-limited HMAC-signed payload using the dashboard
credentials configured via LITEADS_DASHBOARD__* environment variables.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from liteads.common.config import get_settings
from liteads.common.logger import get_logger

logger = get_logger(__name__)
router = APIRouter()


# Token validity window (seconds)
_TOKEN_TTL = 86400  # 24 hours


class TokenRequest(BaseModel):
    """Credentials for obtaining a Bearer access token."""

    username: str = Field(..., description="API username")
    password: str = Field(..., description="API password")


class TokenResponse(BaseModel):
    """Bearer access token response (Adtelligent-compatible format)."""

    access_token: str = Field(..., description="Bearer access token")
    token_type: str = Field("Bearer", description="Token type (always 'Bearer')")
    expires_in: int = Field(_TOKEN_TTL, description="Token validity in seconds")


def _make_api_token(username: str, issued_at: int, secret: str) -> str:
    """Create a time-bound HMAC-signed API token.

    Format:  ``<username>:<issued_at>:<hmac_hex>``
    """
    payload = f"{username}:{issued_at}:liteads-api"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{username}:{issued_at}:{sig}"


def verify_api_token(token: str) -> bool:
    """Verify an API bearer token and check it has not expired."""
    settings = get_settings()
    secret = settings.dashboard.secret_key

    parts = token.split(":", 2)
    if len(parts) != 3:
        return False

    username, issued_at_str, _ = parts
    try:
        issued_at = int(issued_at_str)
    except ValueError:
        return False

    # Replay / expiry check
    if (time.time() - issued_at) > _TOKEN_TTL:
        return False

    expected = _make_api_token(username, issued_at, secret)
    return hmac.compare_digest(token, expected)


@router.post(
    "",
    response_model=TokenResponse,
    summary="Obtain Bearer access token",
    description=(
        "Authenticate with username and password to receive a Bearer access token. "
        "Use the returned token in subsequent API requests via the "
        "``Authorization: Bearer <token>`` header. "
        "Tokens are valid for 24 hours. "
        "This endpoint follows the Adtelligent SSP API authentication model."
    ),
    responses={
        200: {"description": "Token issued successfully"},
        401: {"description": "Invalid credentials"},
    },
)
async def obtain_token(body: TokenRequest) -> TokenResponse:
    """
    Authenticate and receive a Bearer API token.

    Credentials are validated against the dashboard configuration
    (``LITEADS_DASHBOARD__USERNAME`` / ``LITEADS_DASHBOARD__PASSWORD``
    environment variables, or the defaults).
    """
    settings = get_settings()

    if not (
        hmac.compare_digest(body.username, settings.dashboard.username)
        and hmac.compare_digest(body.password, settings.dashboard.password)
    ):
        logger.warning("API token request: invalid credentials", user=body.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    issued_at = int(time.time())
    token = _make_api_token(body.username, issued_at, settings.dashboard.secret_key)

    logger.info("API token issued", user=body.username)

    return TokenResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=_TOKEN_TTL,
    )
