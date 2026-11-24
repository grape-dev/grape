"""claim 回连基址按「worker 实际访问地址」派生（2026-11-22）

背景：任务行的 server_url 是建单进程视角算的（公网 IP / 回环），远端 worker
可能根本连不上（实测远端 worker → 公网地址返回 000，CLI 全 301）。修法不写死任何
地址：claim 时用 worker 自己访问到的 Host / X-Forwarded-Proto 回填。
"""
import pytest

from src.domains.runtime.routes.runtime import _request_base_url


class _FakeURL:
    def __init__(self, scheme="http"):
        self.scheme = scheme


class _FakeRequest:
    def __init__(self, headers=None, scheme="http"):
        self.headers = headers or {}
        self.url = _FakeURL(scheme)


def test_host_header_wins():
    req = _FakeRequest({"host": "198.51.100.10"})
    assert _request_base_url(req) == "http://198.51.100.10"


def test_forwarded_proto_is_honoured():
    req = _FakeRequest({"host": "grape.example.com", "x-forwarded-proto": "https"})
    assert _request_base_url(req) == "https://grape.example.com"


def test_forwarded_proto_list_takes_first():
    req = _FakeRequest({"host": "h.example.com:8080", "x-forwarded-proto": "https,http"})
    assert _request_base_url(req) == "https://h.example.com:8080"


def test_no_host_returns_empty_for_fallback():
    assert _request_base_url(_FakeRequest({})) == ""
    assert _request_base_url(None) == ""


def test_missing_forwarded_proto_falls_back_to_scheme():
    req = _FakeRequest({"host": "127.0.0.1"}, scheme="https")
    assert _request_base_url(req) == "https://127.0.0.1"