"""Small media proxy helpers for image and thumbnail previews."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Final
from urllib.parse import unquote, urljoin, urlparse

import httpx


MAX_MEDIA_BYTES: Final = 8 * 1024 * 1024
MAX_REDIRECTS: Final = 4
ALLOWED_SCHEMES: Final = {"http", "https"}
BLOCKED_HOSTNAMES: Final = {"localhost", "localhost.localdomain"}
MEDIA_TYPE_BY_EXTENSION: Final = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
MEDIA_REQUEST_HEADERS: Final = {
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36 NekoAISearch/1.0"
    ),
}


@dataclass(frozen=True)
class ProxiedMedia:
    """Fetched media payload returned by the proxy endpoint."""

    content: bytes
    media_type: str


class MediaProxyError(RuntimeError):
    """Raised when a remote media URL cannot be proxied safely."""

    def __init__(self, status_code: int, message: str) -> None:
        """Store the HTTP status code and user-facing error message."""
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def validate_media_url(media_url: str) -> str:
    """Validate and normalize a remote media URL before proxying it."""
    parsed = urlparse(media_url)
    hostname = parsed.hostname or ""

    if parsed.scheme not in ALLOWED_SCHEMES or not hostname:
        raise MediaProxyError(400, "Only absolute http or https media URLs are supported.")

    if _is_blocked_host(hostname):
        raise MediaProxyError(400, "Local or private media URLs cannot be proxied.")

    if _url_extension(media_url) == ".svg":
        raise MediaProxyError(415, "SVG previews are not allowed through the media proxy.")

    return media_url


async def fetch_remote_media(media_url: str) -> ProxiedMedia:
    """Fetch a remote preview image with size, type, and network safeguards."""
    safe_url = validate_media_url(media_url)
    timeout = httpx.Timeout(8.0, connect=3.0)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                _ensure_public_dns_target(safe_url)
                response = await client.send(
                    client.build_request(
                        "GET",
                        safe_url,
                        headers=media_request_headers(safe_url),
                    ),
                    stream=True,
                )

                if response.is_redirect:
                    location = response.headers.get("location")
                    await response.aclose()
                    if not location:
                        raise MediaProxyError(502, "Remote media redirect is missing a target.")
                    safe_url = validate_media_url(urljoin(safe_url, location))
                    continue

                await response.aclose()
                break
            else:
                raise MediaProxyError(502, "Remote media redirected too many times.")

            async with client.stream(
                "GET",
                safe_url,
                headers=media_request_headers(safe_url),
            ) as response:
                if response.status_code >= 400:
                    raise MediaProxyError(502, "Remote media responded with an error.")

                media_type = _response_media_type(response.headers.get("content-type"), safe_url)
                _ensure_allowed_media_type(media_type)
                _ensure_allowed_content_length(response.headers.get("content-length"))

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_MEDIA_BYTES:
                        raise MediaProxyError(413, "Remote media is too large to preview.")

                if not body:
                    raise MediaProxyError(502, "Remote media returned an empty body.")

                return ProxiedMedia(content=bytes(body), media_type=media_type)
    except MediaProxyError:
        raise
    except httpx.RequestError as exc:
        raise MediaProxyError(502, "Remote media could not be fetched.") from exc


def media_request_headers(media_url: str) -> dict[str, str]:
    """Return request headers tuned for known media hosts."""
    headers = dict(MEDIA_REQUEST_HEADERS)
    hostname = (urlparse(media_url).hostname or "").lower()
    if hostname.endswith("hdslb.com") or hostname.endswith("bilibili.com"):
        headers["Referer"] = "https://www.bilibili.com/"
        headers["Origin"] = "https://www.bilibili.com"

    return headers


def _is_blocked_host(hostname: str) -> bool:
    """Return whether a hostname points at a local or private target."""
    normalized = hostname.strip().lower().rstrip(".")
    if normalized in BLOCKED_HOSTNAMES or normalized.endswith(".local"):
        return True

    try:
        address = ip_address(normalized)
    except ValueError:
        return False

    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _ensure_public_dns_target(media_url: str) -> None:
    """Reject URLs whose hostname resolves to private or local network addresses."""
    hostname = urlparse(media_url).hostname or ""
    if _is_blocked_host(hostname):
        raise MediaProxyError(400, "Local or private media URLs cannot be proxied.")

    try:
        resolved = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return

    for item in resolved:
        address = item[4][0]
        if _is_blocked_host(address):
            raise MediaProxyError(400, "Local or private media URLs cannot be proxied.")


def _response_media_type(content_type: str | None, media_url: str) -> str:
    """Resolve a usable media type from response headers or the URL extension."""
    if content_type:
        return content_type.split(";", maxsplit=1)[0].strip().lower()

    extension = _url_extension(media_url)
    return MEDIA_TYPE_BY_EXTENSION.get(extension, "application/octet-stream")


def _ensure_allowed_media_type(media_type: str) -> None:
    """Allow image payloads and reject web pages or unknown binary downloads."""
    if media_type.startswith("image/") and media_type != "image/svg+xml":
        return

    raise MediaProxyError(415, "Remote media is not an image preview.")


def _ensure_allowed_content_length(content_length: str | None) -> None:
    """Reject oversized media before reading the full response body."""
    if not content_length:
        return

    try:
        size = int(content_length)
    except ValueError:
        return

    if size > MAX_MEDIA_BYTES:
        raise MediaProxyError(413, "Remote media is too large to preview.")


def _url_extension(media_url: str) -> str:
    """Read a lower-case image extension from a URL path."""
    path = unquote(urlparse(media_url).path)
    if "." not in path:
        return ""

    return "." + path.rsplit(".", 1)[-1].lower()
