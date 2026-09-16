"""Pinned public networking for the video importer.

The desktop runtime can expose a synthetic ``198.18.0.0/15`` answer for
selected Xiaohongshu names while its proxy still reaches the public site.  A
normal resolver result is never silently accepted in that case.  Supported
Xiaohongshu names are resolved through one fixed HTTPS JSON resolver and every
origin request is then connected to the validated address while preserving the
original Host header and TLS SNI.
"""

from __future__ import annotations

import ipaddress
import json
import math
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, Request, build_opener


FAKE_DNS_NETWORK = ipaddress.ip_network("198.18.0.0/15")
TRUSTED_DOH_ENDPOINT = "https://dns.google/resolve"
DOH_TIMEOUT_SECONDS = 5.0
DOH_MAX_RESPONSE_BYTES = 64 * 1024
DOH_CACHE_TTL_SECONDS = 60.0
DEFAULT_ORIGIN_TIMEOUT_SECONDS = 20.0

SUPPORTED_XHS_HOSTS = frozenset(
    {
        "xhslink.cn",
        "xhslink.com",
        "xiaohongshu.com",
        "www.xhslink.cn",
        "www.xhslink.com",
        "www.xiaohongshu.com",
    }
)


class PublicEndpointError(ValueError):
    """The origin cannot be proven to be a public address."""


@dataclass(frozen=True)
class _CachedAddresses:
    addresses: tuple[str, ...]
    expires_at: float


_cache_lock = threading.RLock()
_doh_cache: dict[str, _CachedAddresses] = {}


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def is_supported_xhs_host(hostname: str) -> bool:
    host = str(hostname or "").rstrip(".").lower()
    return host in SUPPORTED_XHS_HOSTS or host == "xhscdn.com" or host.endswith(".xhscdn.com")


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def _is_fake_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in FAKE_DNS_NETWORK
    except ValueError:
        return False


def _local_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return ()
    addresses: list[str] = []
    seen: set[str] = set()
    for result in results:
        try:
            value = str(result[4][0])
            ipaddress.ip_address(value)
        except (IndexError, TypeError, ValueError):
            continue
        if value not in seen:
            seen.add(value)
            addresses.append(value)
    return tuple(addresses)


def _response_status(response: Any) -> int:
    value = getattr(response, "status", None)
    if value is None:
        value = getattr(response, "code", None)
    try:
        return int(value or 200)
    except (TypeError, ValueError):
        return 200


def _read_doh_body(response: Any) -> bytes:
    try:
        body = response.read(DOH_MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError) as exc:
        raise PublicEndpointError("trusted DNS resolver unavailable") from exc
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise PublicEndpointError("trusted DNS resolver returned invalid data")
    raw = bytes(body)
    if len(raw) > DOH_MAX_RESPONSE_BYTES:
        raise PublicEndpointError("trusted DNS resolver response too large")
    return raw


def _query_doh(hostname: str) -> tuple[str, ...]:
    addresses: list[str] = []
    seen: set[str] = set()
    for record_type, numeric_type in (("A", 1), ("AAAA", 28)):
        query = f"{TRUSTED_DOH_ENDPOINT}?name={quote(hostname, safe='')}&type={record_type}"
        request = Request(
            query,
            headers={
                "Accept": "application/dns-json",
                "User-Agent": "kitchen-video-import/1",
            },
            method="GET",
        )
        try:
            response = _open_doh(request, timeout=DOH_TIMEOUT_SECONDS)
            try:
                if _response_status(response) != 200:
                    raise PublicEndpointError("trusted DNS resolver returned an unexpected status")
                payload = json.loads(_read_doh_body(response).decode("utf-8"))
            finally:
                try:
                    response.close()
                except Exception:
                    pass
        except (HTTPError, URLError, OSError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            # A missing AAAA record is normal.  Continue so an A-only host
            # remains usable; the final empty result still fails closed.
            if record_type == "AAAA":
                continue
            continue
        if not isinstance(payload, dict):
            continue
        answer_rows = payload.get("Answer")
        if not isinstance(answer_rows, list):
            continue
        for row in answer_rows:
            if not isinstance(row, dict):
                continue
            try:
                row_type = int(row.get("type"))
            except (TypeError, ValueError):
                continue
            if row_type != numeric_type:
                continue
            value = str(row.get("data") or "").strip()
            try:
                ipaddress.ip_address(value)
            except ValueError:
                raise PublicEndpointError("trusted DNS resolver returned invalid address")
            if not _is_public_ip(value):
                raise PublicEndpointError("trusted DNS resolver returned non-public address")
            if value not in seen:
                seen.add(value)
                addresses.append(value)
    if not addresses:
        raise PublicEndpointError("trusted DNS resolver returned no public address")
    return tuple(addresses)


def _doh_addresses(hostname: str) -> tuple[str, ...]:
    now = time.monotonic()
    with _cache_lock:
        cached = _doh_cache.get(hostname)
        if cached is not None and cached.expires_at > now:
            return cached.addresses
    addresses = _query_doh(hostname)
    with _cache_lock:
        _doh_cache[hostname] = _CachedAddresses(addresses, now + DOH_CACHE_TTL_SECONDS)
    return addresses


def resolve_pinned_addresses(hostname: str, *, port: int = 443) -> tuple[str, ...]:
    """Return public addresses safe to use for a request to ``hostname``."""

    host = str(hostname or "").rstrip(".").lower()
    if not host:
        raise PublicEndpointError("missing origin host")
    local = _local_addresses(host, int(port))
    if local and all(_is_public_ip(value) for value in local):
        return local
    if local and not all(_is_fake_ip(value) for value in local):
        raise PublicEndpointError("origin resolves to a non-public address")
    if not is_supported_xhs_host(host):
        raise PublicEndpointError("fake DNS is unsupported for this origin")
    return _doh_addresses(host)


def _open_doh(request: Request, *, timeout: float) -> Any:
    """Open the fixed resolver with redirects disabled for this request."""

    return build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


def _authority(hostname: str, port: int, scheme: str) -> str:
    host = str(hostname)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default = 443 if scheme.lower() == "https" else 80
    return host if int(port) == default else f"{host}:{int(port)}"


def _parse_authority(value: str, default_port: int) -> tuple[str, int]:
    parsed = urlsplit(f"//{value}")
    host = parsed.hostname
    if not host:
        raise PublicEndpointError("invalid proxy host")
    try:
        port = parsed.port or default_port
    except ValueError as exc:
        raise PublicEndpointError("invalid proxy port") from exc
    return host, int(port)


class _PinnedHTTPConnection(HTTPConnection):
    def __init__(self, host: str, *, connect_host: str, port: int, **kwargs: Any) -> None:
        super().__init__(host, port=port, **kwargs)
        self._connect_host = connect_host

    def connect(self) -> None:
        self.sock = self._create_connection((self._connect_host, self.port), self.timeout, self.source_address)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if getattr(exc, "errno", None) != getattr(socket, "ENOPROTOOPT", 92):
                raise
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        connect_host: str,
        server_hostname: str,
        port: int,
        context: ssl.SSLContext,
        **kwargs: Any,
    ) -> None:
        super().__init__(host, port=port, context=context, **kwargs)
        self._connect_host = connect_host
        self._server_hostname = server_hostname

    def connect(self) -> None:
        self.sock = self._create_connection((self._connect_host, self.port), self.timeout, self.source_address)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            if getattr(exc, "errno", None) != getattr(socket, "ENOPROTOOPT", 92):
                raise
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_hostname)


def _request_headers(request: Request, origin_authority: str) -> dict[str, str]:
    headers = dict(getattr(request, "unredirected_hdrs", {}))
    headers.update({key: value for key, value in request.headers.items() if key not in headers})
    for key in list(headers):
        if key.lower() == "host":
            del headers[key]
    headers["Host"] = origin_authority
    headers["Connection"] = "close"
    return headers


def _remove_proxy_auth(headers: dict[str, str]) -> str | None:
    value = None
    for key in list(headers):
        if key.lower() == "proxy-authorization":
            value = headers.pop(key)
    return value


def _origin_path(parsed: Any) -> str:
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _absolute_ip_target(scheme: str, address: str, port: int, path: str) -> str:
    return f"{scheme}://{_authority(address, port, scheme)}{path}"


def open_pinned_request(request: Request, *, context: ssl.SSLContext | None = None) -> Any:
    """Open a urllib request through a validated, fixed origin address."""

    parsed = urlsplit(request.full_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise URLError("unsupported origin")
    origin_host = parsed.hostname
    try:
        origin_port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise URLError("invalid origin port") from exc
    addresses = resolve_pinned_addresses(origin_host, port=origin_port)
    address = addresses[0]
    origin_authority = _authority(origin_host, origin_port, scheme)
    headers = _request_headers(request, origin_authority)
    path = _origin_path(parsed)
    proxy_tunnel = bool(getattr(request, "_tunnel_host", None))
    request_host, request_port = _parse_authority(
        str(getattr(request, "host", "") or origin_host),
        443 if scheme == "https" else 80,
    )
    proxy_active = (
        proxy_tunnel
        or request_host.lower().rstrip(".") != origin_host.lower().rstrip(".")
        or request_port != origin_port
    )
    if scheme == "http" and str(getattr(request, "selector", "")).startswith("http://"):
        proxy_active = True
    try:
        request_timeout = float(getattr(request, "timeout", DEFAULT_ORIGIN_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        request_timeout = DEFAULT_ORIGIN_TIMEOUT_SECONDS
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        request_timeout = DEFAULT_ORIGIN_TIMEOUT_SECONDS

    if scheme == "https":
        if proxy_active:
            proxy_auth = _remove_proxy_auth(headers)
            connection = _PinnedHTTPSConnection(
                request_host,
                connect_host=request_host,
                port=request_port,
                server_hostname=origin_host,
                context=context or ssl.create_default_context(),
                timeout=request_timeout,
            )
            tunnel_headers = {"Host": _authority(address, origin_port, scheme)}
            if proxy_auth:
                tunnel_headers["Proxy-Authorization"] = proxy_auth
            connection.set_tunnel(address, origin_port, headers=tunnel_headers)
        else:
            _remove_proxy_auth(headers)
            connection = _PinnedHTTPSConnection(
                origin_host,
                connect_host=address,
                port=origin_port,
                server_hostname=origin_host,
                context=context or ssl.create_default_context(),
                timeout=request_timeout,
            )
        request_target = path
    else:
        if proxy_active:
            if (
                request_host.lower().rstrip(".") == origin_host.lower().rstrip(".")
                and request_port == origin_port
            ):
                # A manually constructed absolute selector can look like a
                # proxy request even when no proxy credentials are present.
                _remove_proxy_auth(headers)
            # HTTP proxies receive an absolute URI.  Replacing only the
            # authority pins the proxy's upstream connection while Host keeps
            # the origin virtual host intact.
            connection = _PinnedHTTPConnection(
                request_host,
                connect_host=request_host,
                port=request_port,
                timeout=request_timeout,
            )
            request_target = _absolute_ip_target(scheme, address, origin_port, path)
        else:
            _remove_proxy_auth(headers)
            connection = _PinnedHTTPConnection(
                origin_host,
                connect_host=address,
                port=origin_port,
                timeout=request_timeout,
            )
            request_target = path

    try:
        connection.request(
            request.get_method(),
            request_target,
            request.data,
            headers=headers,
            encode_chunked=request.has_header("Transfer-encoding"),
        )
        response = connection.getresponse()
    except Exception:
        connection.close()
        raise
    response.url = request.get_full_url()
    response.msg = response.reason
    return response


class PinnedHTTPHandler(HTTPHandler):
    def http_open(self, request: Request) -> Any:
        return open_pinned_request(request)


class PinnedHTTPSHandler(HTTPSHandler):
    def https_open(self, request: Request) -> Any:
        return open_pinned_request(request, context=self._context)


__all__ = [
    "DOH_CACHE_TTL_SECONDS",
    "DOH_MAX_RESPONSE_BYTES",
    "FAKE_DNS_NETWORK",
    "PublicEndpointError",
    "PinnedHTTPHandler",
    "PinnedHTTPSHandler",
    "SUPPORTED_XHS_HOSTS",
    "TRUSTED_DOH_ENDPOINT",
    "is_supported_xhs_host",
    "open_pinned_request",
    "resolve_pinned_addresses",
]
