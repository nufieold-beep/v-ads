"""
Tests for Adtelligent SSP model alignment:
- Bearer token auth endpoint (POST /api/v1/token)
- Adtelligent-style route aliases (/api/v1/channels, /api/v1/sources)
- Tag builder uses uip=[UIP] + supports app_store_url
"""

import pytest
from httpx import AsyncClient


# ============================================================================
# Bearer token auth (POST /api/v1/token)
# ============================================================================

@pytest.mark.asyncio
async def test_token_endpoint_valid_credentials(client: AsyncClient) -> None:
    """POST /api/v1/token returns a Bearer token with valid credentials."""
    response = await client.post(
        "/api/v1/token",
        json={"username": "admin", "password": "Dewa@123"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == 86400
    assert len(data["access_token"]) > 10


@pytest.mark.asyncio
async def test_token_endpoint_invalid_credentials(client: AsyncClient) -> None:
    """POST /api/v1/token returns 401 with wrong credentials."""
    response = await client.post(
        "/api/v1/token",
        json={"username": "admin", "password": "wrongpassword"},
    )
    assert response.status_code == 401
    data = response.json()
    assert "detail" in data


@pytest.mark.asyncio
async def test_token_endpoint_wrong_username(client: AsyncClient) -> None:
    """POST /api/v1/token returns 401 with wrong username."""
    response = await client.post(
        "/api/v1/token",
        json={"username": "notauser", "password": "Dewa@123"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_token_endpoint_missing_fields(client: AsyncClient) -> None:
    """POST /api/v1/token returns 422 when body fields are missing."""
    response = await client.post(
        "/api/v1/token",
        json={"username": "admin"},  # missing password
    )
    assert response.status_code == 422


# ============================================================================
# Adtelligent-style route aliases
# ============================================================================

@pytest.mark.asyncio
async def test_channels_alias_routes_registered(client: AsyncClient) -> None:
    """/api/v1/channels/* routes are registered as Adtelligent-style supply aliases."""
    from liteads.ad_server.main import app
    route_paths = [r.path for r in app.routes]
    # Verify the key channel (supply) alias routes exist in the app
    assert "/api/v1/channels/supply-tags" in route_paths
    assert "/api/v1/channels/demand-endpoints" in route_paths
    assert "/api/v1/channels/demand-vast-tags" in route_paths
    assert "/api/v1/channels/mappings" in route_paths


@pytest.mark.asyncio
async def test_sources_alias_routes_registered(client: AsyncClient) -> None:
    """/api/v1/sources/* routes are registered as Adtelligent-style demand aliases."""
    from liteads.ad_server.main import app
    route_paths = [r.path for r in app.routes]
    # Verify the key source (demand) alias routes exist in the app
    assert "/api/v1/sources/supply-tags" in route_paths
    assert "/api/v1/sources/demand-endpoints" in route_paths
    assert "/api/v1/sources/demand-vast-tags" in route_paths
    assert "/api/v1/sources/mappings" in route_paths


@pytest.mark.asyncio
async def test_canonical_routes_still_registered(client: AsyncClient) -> None:
    """Canonical supply-demand routes still exist (aliases are additive, not replacements)."""
    from liteads.ad_server.main import app
    route_paths = [r.path for r in app.routes]
    assert "/api/v1/supply-demand/supply-tags" in route_paths
    assert "/api/v1/supply-demand/demand-endpoints" in route_paths
    assert "/api/v1/supply-demand/demand-vast-tags" in route_paths
    assert "/api/v1/supply-demand/mappings" in route_paths


# ============================================================================
# Tag builder – uip=[UIP] + app_store_url
# ============================================================================

@pytest.mark.asyncio
async def test_tag_builder_uses_uip_macro(client: AsyncClient) -> None:
    """Tag builder generates uip=[UIP] (Adtelligent-standard) not ip=[IP]."""
    response = await client.post(
        "/api/vast/tag-builder",
        json={
            "base_url": "https://ads.example.com",
            "slot_id": "ctv_preroll",
            "environment": "ctv",
            "width": 1920,
            "height": 1080,
            "include_device_macros": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    tag_url = data["vast_tag_url"]
    # Must use Adtelligent-standard uip param, not legacy &ip= param
    assert "uip=" in tag_url
    # There must be no standalone "ip=" parameter (legacy) – only "uip=" is valid.
    # Note: "uip=" contains the substring "ip=" so we check for "&ip=" or "?ip="
    assert "&ip=" not in tag_url
    assert "?ip=" not in tag_url
    # Macro note must reference [UIP] not [IP]
    assert "[UIP]" in data["macro_note"]
    assert "[IP]" not in data["macro_note"]
    # Example cURL should have macro resolved to a sample IP, not [UIP]
    assert "[UIP]" not in data["example_curl"]


@pytest.mark.asyncio
async def test_tag_builder_includes_app_store_url(client: AsyncClient) -> None:
    """Tag builder includes app_store_url in the generated VAST tag URL."""
    response = await client.post(
        "/api/vast/tag-builder",
        json={
            "base_url": "https://ads.example.com",
            "slot_id": "ctv_preroll",
            "environment": "ctv",
            "width": 1920,
            "height": 1080,
            "app_bundle": "com.pluto.tv",
            "app_name": "Pluto TV",
            "app_store_url": "https://channelstore.roku.com/details/8d6f47",
            "include_device_macros": False,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "app_store_url=" in data["vast_tag_url"]


@pytest.mark.asyncio
async def test_tag_builder_no_macros_has_no_uip(client: AsyncClient) -> None:
    """Tag builder without device macros does not include uip or ip param."""
    response = await client.post(
        "/api/vast/tag-builder",
        json={
            "base_url": "https://ads.example.com",
            "slot_id": "test_slot",
            "environment": "inapp",
            "include_device_macros": False,
        },
    )
    assert response.status_code == 200
    tag_url = response.json()["vast_tag_url"]
    assert "uip=" not in tag_url
    assert "ip=" not in tag_url
