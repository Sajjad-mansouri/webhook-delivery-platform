import socket

import pytest
from app.security import ssrf
from app.security.ssrf import UnsafeWebhookURLError, resolve_and_check_public_addresses


def addrinfo(ip, family=None):
    if family is None:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


class TestResolveAndCheckPublicAddresses:
    def test_returns_resolved_public_addresses(self, monkeypatch):
        monkeypatch.setattr(
            ssrf.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [addrinfo("8.8.8.8"), addrinfo("1.1.1.1")],
        )
        assert resolve_and_check_public_addresses("example.com") == {
            "8.8.8.8",
            "1.1.1.1",
        }

    def test_accepts_public_ipv6_address(self, monkeypatch):
        monkeypatch.setattr(
            ssrf.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [addrinfo("2001:4860:4860::8888")],
        )
        assert resolve_and_check_public_addresses("example.com") == {
            "2001:4860:4860::8888"
        }

    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "10.0.0.2", "192.168.1.2", "169.254.1.1", "::1"],
    )
    def test_rejects_any_non_global_resolved_address(self, monkeypatch, address):
        monkeypatch.setattr(
            ssrf.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [addrinfo("8.8.8.8"), addrinfo(address)],
        )
        with pytest.raises(UnsafeWebhookURLError):
            resolve_and_check_public_addresses("example.com")

    def test_rejects_empty_dns_result(self, monkeypatch):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *args, **kwargs: [])
        with pytest.raises(UnsafeWebhookURLError):
            resolve_and_check_public_addresses("example.com")

    def test_wraps_dns_resolution_errors(self, monkeypatch):
        def fail(*args, **kwargs):
            raise OSError("DNS lookup failed")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", fail)
        with pytest.raises(UnsafeWebhookURLError):
            resolve_and_check_public_addresses("example.com")
