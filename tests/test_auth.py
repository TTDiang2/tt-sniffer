"""Basic Auth 门测试：启用/禁用、凭据对错、本机豁免、隧道请求不免密。"""
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import auth
from app.config import CONFIG


@pytest.fixture(autouse=True)
def _reset_auth_state():
    auth._SUCCESS_CACHE.clear()
    auth._FAILURES.clear()
    yield
    auth._SUCCESS_CACHE.clear()
    auth._FAILURES.clear()


def make_request(client_host: str = "203.0.113.7", auth_header: str | None = None,
                 xff: str | None = None, host: str = "external.example"):
    scope = {
        "type": "http", "method": "GET", "url": b"http://x/api/items",
        "headers": [(b"host", host.encode())],
        "client": (client_host, 12345), "query_string": b"",
    }
    if auth_header:
        scope["headers"].append((b"authorization", auth_header.encode()))
    if xff:
        scope["headers"].append((b"x-forwarded-for", xff.encode()))
    return Request(scope)


def test_disabled_allows_all(monkeypatch):
    monkeypatch.setitem(CONFIG, "auth", {"enabled": False})
    assert auth.check(make_request()) is None


def test_wrong_password_rejected_and_correct_allowed(monkeypatch):
    import hashlib
    monkeypatch.setitem(CONFIG, "auth", {
        "enabled": True, "user": "owner",
        "password_sha256": hashlib.sha256(b"s3cret").hexdigest()})
    # 无凭据 → 401 + WWW-Authenticate
    r = auth.check(make_request())
    assert r.status_code == 401 and "WWW-Authenticate" in r.headers
    # 错密码 → 401
    import base64
    bad = base64.b64encode(b"owner:wrong").decode()
    assert auth.check(make_request(auth_header=f"Basic {bad}")).status_code == 401
    # 对密码 → 放行
    ok = base64.b64encode(b"owner:s3cret").decode()
    assert auth.check(make_request(auth_header=f"Basic {ok}")) is None


def test_pbkdf2_hash_supported(monkeypatch):
    """新配置 password_hash 使用带盐 PBKDF2，同时不能破坏旧 sha256 兼容。"""
    import base64 as b64
    import hashlib
    salt = "test-salt"
    digest = hashlib.pbkdf2_hmac("sha256", b"s3cret", salt.encode(), 100_000).hex()
    monkeypatch.setitem(CONFIG, "auth", {
        "enabled": True, "user": "owner",
        "password_hash": f"pbkdf2_sha256$100000${salt}${digest}"})
    ok = b64.b64encode(b"owner:s3cret").decode()
    bad = b64.b64encode(b"owner:wrong").decode()
    assert auth.check(make_request(auth_header=f"Basic {ok}")) is None
    assert auth.check(make_request(auth_header=f"Basic {bad}")).status_code == 401


def test_failed_logins_rate_limited_per_ip(monkeypatch):
    """连续 10 次错误后 429；成功密码在封禁窗口内也不能绕过。"""
    import base64 as b64
    import hashlib
    monkeypatch.setitem(CONFIG, "auth", {
        "enabled": True, "user": "owner",
        "password_sha256": hashlib.sha256(b"s3cret").hexdigest()})
    bad = b64.b64encode(b"owner:wrong").decode()
    good = b64.b64encode(b"owner:s3cret").decode()
    ip = "198.51.100.23"
    auth._FAILURES.clear()
    try:
        for i in range(10):
            r = auth.check(make_request(client_host=ip,
                                        auth_header=f"Basic {bad}"))
            assert r.status_code == (429 if i == 9 else 401), i
        assert auth.check(make_request(client_host=ip,
                                       auth_header=f"Basic {good}")).status_code == 429
    finally:
        auth._FAILURES.clear()


def test_loopback_exempt_requires_loopback_host(monkeypatch):
    """v8/R3 DNS 重绑定防护：回环 peer + 无 XFF 但 Host 是恶意域名 → 401；
    Host 是回环名 → 豁免。"""
    import hashlib
    monkeypatch.setitem(CONFIG, "auth", {
        "enabled": True, "user": "owner",
        "password_sha256": hashlib.sha256(b"s3cret").hexdigest()})
    # 恶意 Host（重绑定）→ 401
    r = auth.check(make_request(client_host="127.0.0.1",
                                host="evil-rebind.example"))
    assert r is not None and r.status_code == 401
    # 回环 Host → 豁免
    assert auth.check(make_request(client_host="127.0.0.1",
                                   host="127.0.0.1:8787")) is None
    assert auth.check(make_request(client_host="127.0.0.1",
                                   host="localhost")) is None
    # 隧道流量：XFF 存在即视为外部
    assert auth.check(make_request(client_host="127.0.0.1",
                                   host="localhost",
                                   xff="203.0.113.9")).status_code == 401


def test_broken_config_fail_closed(monkeypatch):
    """v8/R2：enabled 但配置残缺 → 一律 401（绝不静默放行）。"""
    monkeypatch.setitem(CONFIG, "auth", {"enabled": True, "user": "owner",
                                         "password_sha256": ""})
    r = auth.check(make_request())
    assert r is not None and r.status_code == 401
    monkeypatch.setitem(CONFIG, "auth", {"enabled": True, "user": "",
                                         "password_sha256": "x" * 64})
    assert auth.check(make_request()).status_code == 401


def test_http_layer_401(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setitem(CONFIG, "auth", {
        "enabled": True, "user": "owner",
        "password_sha256": hashlib.sha256(b"s3cret").hexdigest()})
    from app.pipeline import Pipeline
    from app.server import create_app
    from app.store import Store
    client = TestClient(create_app(Store(tmp_path / "t.db"),
                                   Pipeline(Store(tmp_path / "t.db"))),
                        raise_server_exceptions=False)
    assert client.get("/api/items").status_code == 401
    assert client.get("/").status_code == 401


def test_pwa_assets_public_under_auth(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setitem(CONFIG, "auth", {
        "enabled": True, "user": "owner",
        "password_sha256": hashlib.sha256(b"s3cret").hexdigest()})
    from app.pipeline import Pipeline
    from app.server import create_app
    from app.store import Store
    store = Store(tmp_path / "t.db")
    client = TestClient(create_app(store, Pipeline(store)),
                        raise_server_exceptions=False)
    for path in ("/manifest.webmanifest", "/sw.js", "/icons/icon-192.png"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "www-authenticate" not in r.headers
    assert client.get("/api/items").status_code == 401


def test_http_layer_accepts_cached_credentials(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setitem(CONFIG, "auth", {
        "enabled": True, "user": "owner",
        "password_sha256": hashlib.sha256(b"s3cret").hexdigest()})
    from app.pipeline import Pipeline
    from app.server import create_app
    from app.store import Store
    store = Store(tmp_path / "t.db")
    client = TestClient(create_app(store, Pipeline(store)),
                        raise_server_exceptions=False)
    for _ in range(2):
        assert client.get("/api/items", auth=("owner", "s3cret")).status_code == 200


def test_async_check_caches_success_and_skips_verify(monkeypatch):
    import asyncio
    import base64
    import hashlib

    monkeypatch.setitem(CONFIG, "auth", {
        "enabled": True, "user": "owner",
        "password_sha256": hashlib.sha256(b"s3cret").hexdigest()})
    good = base64.b64encode(b"owner:s3cret").decode()
    calls = []
    real = auth._verify_password

    def counted(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(auth, "_verify_password", counted)
    assert asyncio.run(auth.check_async(
        make_request(auth_header=f"Basic {good}"))) is None
    assert asyncio.run(auth.check_async(
        make_request(auth_header=f"Basic {good}"))) is None
    assert len(calls) == 1


def test_failed_password_is_not_cached(monkeypatch):
    import asyncio
    import base64
    import hashlib

    monkeypatch.setitem(CONFIG, "auth", {
        "enabled": True, "user": "owner",
        "password_sha256": hashlib.sha256(b"s3cret").hexdigest()})
    bad = base64.b64encode(b"owner:wrong").decode()
    calls = []
    real = auth._verify_password

    def counted(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(auth, "_verify_password", counted)
    for _ in range(2):
        assert asyncio.run(auth.check_async(
            make_request(auth_header=f"Basic {bad}"))).status_code == 401
    assert len(calls) == 2
