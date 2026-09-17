from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request

import pytest

from runtime_core import video_media, video_network


class _DoHResponse:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.code = status
        self.closed = False
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.payload

    def close(self) -> None:
        self.closed = True


class _PinnedResponse:
    reason = "OK"
    status = 200
    code = 200
    headers = {}

    def read(self, _size: int = -1) -> bytes:
        return b"ok"

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def clear_doh_cache():
    video_network._doh_cache.clear()
    yield
    video_network._doh_cache.clear()


def _dns_results(*addresses: str):
    return lambda *_args, **_kwargs: [
        (2, 1, 6, "", (address, 0)) for address in addresses
    ]


@pytest.mark.parametrize("addresses", [
    ("198.18.0.98",),
    ("198.18.0.98", "::ffff:198.18.0.98"),
    ("198.18.0.98", "::ffff:0:c612:62"),
    ("::ffff:0:c612:62",),
])
def test_xhs_fake_dns_uses_bounded_trusted_doh_and_returns_public_addresses(monkeypatch, addresses) -> None:
    monkeypatch.setattr(video_network.socket, "getaddrinfo", _dns_results(*addresses))
    calls = []

    def fake_urlopen(request, *, timeout):
        calls.append((request, timeout))
        query = parse_qs(urlsplit(request.full_url).query)
        if query.get("type") == ["A"]:
            payload = {"Status": 0, "Answer": [{"type": 1, "TTL": 30, "data": "93.184.216.34"}]}
        else:
            payload = {"Status": 0, "Answer": []}
        return _DoHResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(video_network, "_open_doh", fake_urlopen)

    assert video_media.validate_public_url("https://xhslink.cn/o/7Al9WMJ4hWr") == "https://xhslink.cn/o/7Al9WMJ4hWr"
    addresses = video_network.resolve_pinned_addresses("xhslink.cn", port=443)

    assert addresses == ("93.184.216.34",)
    assert len(calls) == 2
    assert all(timeout == video_network.DOH_TIMEOUT_SECONDS for _request, timeout in calls)
    assert all(
        "https://dns.google/resolve?" in request.full_url and "xsec_token" not in request.full_url
        for request, _timeout in calls
    )


def test_xhs_doh_result_is_cached_for_short_ttl(monkeypatch) -> None:
    monkeypatch.setattr(video_network.socket, "getaddrinfo", _dns_results("198.18.0.99"))
    calls = []

    def fake_urlopen(request, **_kwargs):
        calls.append(request)
        query = parse_qs(urlsplit(request.full_url).query)
        record_type = 1 if query.get("type") == ["A"] else 28
        value = "1.1.1.1" if record_type == 1 else "2606:4700:4700::1111"
        return _DoHResponse(
            json.dumps({"Status": 0, "Answer": [{"type": record_type, "TTL": 30, "data": value}]}).encode()
        )

    monkeypatch.setattr(video_network, "_open_doh", fake_urlopen)

    first = video_network.resolve_pinned_addresses("www.xiaohongshu.com")
    second = video_network.resolve_pinned_addresses("www.xiaohongshu.com")

    assert first == second == ("1.1.1.1", "2606:4700:4700::1111")
    assert len(calls) == 2


def test_arbitrary_fake_dns_domain_is_rejected_without_doh(monkeypatch) -> None:
    monkeypatch.setattr(video_network.socket, "getaddrinfo", _dns_results("198.18.0.10"))

    def unexpected_doh(*_args, **_kwargs):
        raise AssertionError("arbitrary domains must not use the trusted XHS fallback")

    monkeypatch.setattr(video_network, "_open_doh", unexpected_doh)

    with pytest.raises(video_media.PublicURLBlocked):
        video_media.validate_public_url("https://example.com/video.mp4")


@pytest.mark.parametrize("address", ("10.0.0.8", "127.0.0.1", "::1", "::ffff:10.0.0.8", "::ffff:0:a00:8", "::ffff:0:7f00:1"))
def test_supported_xhs_private_dns_is_rejected_without_doh(monkeypatch, address) -> None:
    monkeypatch.setattr(video_network.socket, "getaddrinfo", _dns_results(address))

    def unexpected_doh(*_args, **_kwargs):
        raise AssertionError("private XHS DNS answers must not be upgraded through DoH")

    monkeypatch.setattr(video_network, "_open_doh", unexpected_doh)

    with pytest.raises(video_media.PublicURLBlocked):
        video_media.validate_public_url("https://www.xiaohongshu.com/explore/demo")


@pytest.mark.parametrize("address", ["::ffff:0:a00:8", "::ffff:0:7f00:1", "::ffff:0:c0a8:5"])
def test_embedded_private_ipv4_literals_are_rejected(address):
    assert not video_network._is_public_ip(address)
    with pytest.raises(video_media.PublicURLBlocked):
        video_media.validate_public_url(f"http://[{address}]/video.mp4")


def test_doh_private_answer_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(video_network.socket, "getaddrinfo", _dns_results("198.18.0.98"))
    responses = []

    def fake_urlopen(_request, **_kwargs):
        response = _DoHResponse(
            json.dumps({"Status": 0, "Answer": [{"type": 1, "TTL": 30, "data": "192.168.0.5"}]}).encode()
        )
        responses.append(response)
        return response

    monkeypatch.setattr(video_network, "_open_doh", fake_urlopen)

    with pytest.raises(video_network.PublicEndpointError):
        video_network.resolve_pinned_addresses("xhslink.cn")
    assert responses and all(response.closed for response in responses)


def test_doh_response_body_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(video_network.socket, "getaddrinfo", _dns_results("198.18.0.98"))
    oversized = _DoHResponse(b"{" + b"x" * (video_network.DOH_MAX_RESPONSE_BYTES + 1))
    monkeypatch.setattr(video_network, "_open_doh", lambda *_args, **_kwargs: oversized)

    with pytest.raises(video_network.PublicEndpointError):
        video_network.resolve_pinned_addresses("xhslink.cn")
    assert oversized.closed
    assert oversized.read_sizes == [video_network.DOH_MAX_RESPONSE_BYTES + 1]


def test_doh_opener_uses_local_no_redirect_handler(monkeypatch) -> None:
    captured = []

    class FakeOpener:
        def open(self, request, *, timeout):
            captured.append((request, timeout))
            return _DoHResponse(b"{}")

    def fake_build_opener(*handlers):
        assert any(isinstance(handler, HTTPRedirectHandler) for handler in handlers)
        assert all(not isinstance(handler, type) for handler in handlers)
        return FakeOpener()

    monkeypatch.setattr(video_network, "build_opener", fake_build_opener)
    response = video_network._open_doh(Request("https://dns.google/resolve"), timeout=3)

    assert response is not None
    assert captured[0][1] == 3


def test_xhscdn_subdomains_are_supported_but_suffix_lookalikes_are_not() -> None:
    assert video_network.is_supported_xhs_host("sns-video-bd.xhscdn.com")
    assert video_network.is_supported_xhs_host("xhscdn.com")
    assert not video_network.is_supported_xhs_host("xhscdn.com.evil.example")
    assert not video_network.is_supported_xhs_host("video.xiaohongshu.com.evil.example")


def test_direct_https_connection_is_pinned_and_preserves_host_and_sni(monkeypatch) -> None:
    monkeypatch.setattr(video_network, "resolve_pinned_addresses", lambda *_args, **_kwargs: ("93.184.216.34",))
    calls = {}

    class FakeConnection:
        def __init__(self, host, **kwargs):
            calls["constructor"] = (host, kwargs)

        def request(self, method, target, body, *, headers, encode_chunked):
            calls["request"] = (method, target, body, headers, encode_chunked)

        def getresponse(self):
            return _PinnedResponse()

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(video_network, "_PinnedHTTPSConnection", FakeConnection)
    request = Request(
        "https://www.xiaohongshu.com/explore/demo?xsec_token=secret",
        headers={"Proxy-Authorization": "Basic should-not-reach-origin"},
    )
    request.timeout = 7

    response = video_network.open_pinned_request(request)

    host, kwargs = calls["constructor"]
    method, target, _body, headers, _encode_chunked = calls["request"]
    assert host == "www.xiaohongshu.com"
    assert kwargs["connect_host"] == "93.184.216.34"
    assert kwargs["server_hostname"] == "www.xiaohongshu.com"
    assert kwargs["timeout"] == 7
    assert method == "GET"
    assert target == "/explore/demo?xsec_token=secret"
    assert headers["Host"] == "www.xiaohongshu.com"
    assert all(key.lower() != "proxy-authorization" for key in headers)
    assert response.url == request.full_url


def test_https_proxy_tunnel_is_pinned_to_ip_with_origin_sni(monkeypatch) -> None:
    monkeypatch.setattr(video_network, "resolve_pinned_addresses", lambda *_args, **_kwargs: ("93.184.216.34",))
    calls = {}

    class FakeConnection:
        def __init__(self, host, **kwargs):
            calls["constructor"] = (host, kwargs)

        def set_tunnel(self, host, port, *, headers):
            calls["tunnel"] = (host, port, headers)

        def request(self, method, target, body, *, headers, encode_chunked):
            calls["request"] = (method, target, body, headers, encode_chunked)

        def getresponse(self):
            return _PinnedResponse()

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(video_network, "_PinnedHTTPSConnection", FakeConnection)
    request = Request(
        "https://www.xiaohongshu.com/explore/demo",
        headers={"Proxy-Authorization": "Basic proxy-only"},
    )
    request.set_proxy("proxy.local:8080", "https")
    request.timeout = 9

    video_network.open_pinned_request(request)

    host, kwargs = calls["constructor"]
    tunnel_host, tunnel_port, tunnel_headers = calls["tunnel"]
    _method, target, _body, headers, _encode_chunked = calls["request"]
    assert host == "proxy.local"
    assert kwargs["connect_host"] == "proxy.local"
    assert kwargs["server_hostname"] == "www.xiaohongshu.com"
    assert kwargs["timeout"] == 9
    assert tunnel_host == "93.184.216.34"
    assert tunnel_port == 443
    assert tunnel_headers["Host"] == "93.184.216.34"
    assert target == "/explore/demo"
    assert headers["Host"] == "www.xiaohongshu.com"
    assert all(key.lower() != "proxy-authorization" for key in headers)
    assert tunnel_headers["Proxy-Authorization"] == "Basic proxy-only"


def test_http_proxy_target_uses_pinned_ip_and_origin_host_header(monkeypatch) -> None:
    monkeypatch.setattr(video_network, "resolve_pinned_addresses", lambda *_args, **_kwargs: ("93.184.216.34",))
    calls = {}

    class FakeConnection:
        def __init__(self, host, **kwargs):
            calls["constructor"] = (host, kwargs)

        def request(self, method, target, body, *, headers, encode_chunked):
            calls["request"] = (method, target, body, headers, encode_chunked)

        def getresponse(self):
            return _PinnedResponse()

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(video_network, "_PinnedHTTPConnection", FakeConnection)
    request = Request(
        "http://xhslink.cn/o/demo",
        headers={"Proxy-Authorization": "Basic proxy-only"},
    )
    request.set_proxy("proxy.local:8080", "http")
    request.timeout = 11

    video_network.open_pinned_request(request)

    host, kwargs = calls["constructor"]
    _method, target, _body, headers, _encode_chunked = calls["request"]
    assert host == "proxy.local"
    assert kwargs["connect_host"] == "proxy.local"
    assert kwargs["timeout"] == 11
    assert target == "http://93.184.216.34/o/demo"
    assert headers["Host"] == "xhslink.cn"
    assert next(value for key, value in headers.items() if key.lower() == "proxy-authorization") == "Basic proxy-only"


def test_http_proxy_port_difference_is_detected_even_when_host_matches(monkeypatch) -> None:
    monkeypatch.setattr(video_network, "resolve_pinned_addresses", lambda *_args, **_kwargs: ("93.184.216.34",))
    calls = {}

    class FakeConnection:
        def __init__(self, host, **kwargs):
            calls["constructor"] = (host, kwargs)

        def request(self, _method, _target, _body, *, headers, **_kwargs):
            calls["headers"] = headers

        def getresponse(self):
            return _PinnedResponse()

        def close(self):
            return None

    monkeypatch.setattr(video_network, "_PinnedHTTPConnection", FakeConnection)
    request = Request(
        "http://xhslink.cn/o/demo",
        headers={"Proxy-Authorization": "Basic proxy-only"},
    )
    request.set_proxy("xhslink.cn:8080", "http")

    video_network.open_pinned_request(request)

    host, kwargs = calls["constructor"]
    assert host == "xhslink.cn"
    assert kwargs["connect_host"] == "xhslink.cn"
    assert kwargs["port"] == 8080
    assert next(value for key, value in calls["headers"].items() if key.lower() == "proxy-authorization") == "Basic proxy-only"
