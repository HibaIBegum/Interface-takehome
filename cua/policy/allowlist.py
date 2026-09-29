"""Origin + route allowlist. The automation may only act on, or navigate to, approved pages."""

from __future__ import annotations

from fnmatch import fnmatchcase
from urllib.parse import urljoin, urlsplit


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def url_allowed(url: str, origins: list[str], routes: list[str], base_url: str = "") -> tuple[bool, str]:
    """(allowed, reason). Relative URLs are resolved against base_url first."""
    full = urljoin(base_url, url)
    parts = urlsplit(full)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False, f"{full!r} is not an http(s) URL"
    origin = origin_of(full)
    if not any(fnmatchcase(origin, pattern) for pattern in origins):
        return False, f"origin {origin} is not allowlisted"
    path = parts.path or "/"
    if not any(fnmatchcase(path, pattern) for pattern in routes):
        return False, f"route {path} is not allowlisted"
    return True, "allowlisted"
