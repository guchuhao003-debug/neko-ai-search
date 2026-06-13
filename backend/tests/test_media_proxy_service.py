"""Tests for the media proxy endpoint and URL safeguards."""

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.services.media_proxy_service import (
    MediaProxyError,
    ProxiedMedia,
    fetch_remote_media,
    media_request_headers,
    validate_media_url,
)


def test_validate_media_url_rejects_local_targets() -> None:
    """Local URLs should be blocked before the backend fetches media."""
    with pytest.raises(MediaProxyError):
        validate_media_url("http://127.0.0.1:8000/private.png")

    with pytest.raises(MediaProxyError):
        validate_media_url("https://localhost/private.png")


def test_validate_media_url_accepts_remote_http_images() -> None:
    """Remote HTTP and HTTPS URLs should pass initial proxy validation."""
    assert validate_media_url("https://example.com/image.png") == "https://example.com/image.png"


def test_validate_media_url_rejects_svg_previews() -> None:
    """SVG previews should be blocked because they can carry active content."""
    with pytest.raises(MediaProxyError):
        validate_media_url("https://example.com/vector.svg")


def test_media_request_headers_adds_bilibili_referer() -> None:
    """Bilibili image hosts should receive a Referer to avoid hotlink blocking."""
    headers = media_request_headers("https://i0.hdslb.com/bfs/archive/cover.jpg")

    assert headers["Referer"] == "https://www.bilibili.com/"
    assert headers["Origin"] == "https://www.bilibili.com"


@pytest.mark.anyio
async def test_media_proxy_returns_image_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API endpoint should return the fetched image bytes with cache headers."""

    async def fake_fetch_remote_media(media_url: str) -> ProxiedMedia:
        """Return deterministic image bytes without making a network request."""
        assert media_url == "https://example.com/cover.png"
        return ProxiedMedia(content=b"image-bytes", media_type="image/png")

    monkeypatch.setattr(main, "fetch_remote_media", fake_fetch_remote_media)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/media-proxy",
            params={"url": "https://example.com/cover.png"},
        )

    assert response.status_code == 200
    assert response.content == b"image-bytes"
    assert response.headers["content-type"] == "image/png"
    assert "max-age=86400" in response.headers["cache-control"]


@pytest.mark.anyio
async def test_fetch_remote_media_rejects_redirect_to_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect targets should be validated before the proxy follows them."""

    class FakeRedirectResponse:
        """Minimal httpx-like redirect response for SSRF tests."""

        is_redirect = True
        headers = {"location": "http://127.0.0.1/private.png"}
        status_code = 302

        async def aclose(self) -> None:
            """Match httpx response cleanup API."""

    class FakeClient:
        """Minimal async client used to avoid a real network request."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """Accept httpx AsyncClient constructor arguments."""

        async def __aenter__(self) -> "FakeClient":
            """Enter the fake async client context."""
            return self

        async def __aexit__(self, *args: object) -> None:
            """Exit the fake async client context."""

        def build_request(self, *args: object, **kwargs: object) -> object:
            """Return a placeholder request object."""
            return object()

        async def send(self, *args: object, **kwargs: object) -> FakeRedirectResponse:
            """Return a redirect response pointing at localhost."""
            return FakeRedirectResponse()

    monkeypatch.setattr("app.services.media_proxy_service.httpx.AsyncClient", FakeClient)

    with pytest.raises(MediaProxyError):
        await fetch_remote_media("https://example.com/cover.png")


@pytest.mark.anyio
async def test_media_proxy_maps_proxy_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxy service errors should become matching HTTP errors."""

    async def fake_fetch_remote_media(media_url: str) -> ProxiedMedia:
        """Raise a deterministic error without making a network request."""
        raise MediaProxyError(415, f"Unsupported media: {media_url}")

    monkeypatch.setattr(main, "fetch_remote_media", fake_fetch_remote_media)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/media-proxy",
            params={"url": "https://example.com/page.html"},
        )

    assert response.status_code == 415
    assert response.json()["detail"] == "Unsupported media: https://example.com/page.html"
