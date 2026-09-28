"""Host allowlist: the automation may only act on, or navigate to, approved hosts."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit


def host_allowed(url: str, allowed_hosts: list[str], base_url: str = "") -> bool:
    host = urlsplit(urljoin(base_url, url)).hostname
    return host is not None and host in allowed_hosts
