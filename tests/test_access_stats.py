"""观察期访问统计与游客只读测试（20261024）：public_read 豁免、令牌桶限速、
统计计数与 /api/access 本机门。

纪律：CONFIG 注入 + TestClient，不触网；TTSNIFFER_HOME 指 tmp 落盘。
"""
import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app import access_stats
from app.auth import reset_rate_limiter
from app.config import CONFIG
from app.pipeline import Pipeline
from app.server import create_app
from app.store import Store

# 与 fixture 里 password_sha256 配对的测试凭据（owner:pw）
CREDS = [("Authorization",
          "Basic " + base64.b64encode(b"owner:pw").decode())]
_PW_HASH = hashlib.sha256(b"pw").hexdigest()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离环境：数据目录指 tmp、auth 开启+public_read、限速小桶便于触发。
    返回 (public_client, loopback_client)——前者模拟经隧道访客形态。"""
    monkeypatch.setenv("TTSNIFFER_HOME", str(tmp_path))
    import importlib
    importlib.reload(access_stats)          # 预载指向新 tmp
    reset_rate_limiter()
    saved = dict(CONFIG.get("auth") or {})
    CONFIG["auth"] = {"enabled": True, "user": "owner",
                      "password_sha256": _PW_HASH,
                      "public_read": True, "rate_limit_rpm": 5}
    store = Store(tmp_path / "t.db")
    app = create_app(store, Pipeline(Store(tmp_path / "t.db")))
    public = TestClient(app, client=("10.0.0.1", 500))     # 非回环 peer
    # 本机直连形态：peer 回环 + Host 回环（豁免三条件之二三，无代理头是其一）
    loop = TestClient(app, client=("127.0.0.1", 500),
                      base_url="http://127.0.0.1")
    yield public, loop
    CONFIG["auth"] = saved
    reset_rate_limiter()


def test_public_read_allows_guest_reads(env):
    """public_read：游客 GET 数据 200；watchlist 与 POST 仍 401；
    public_ai 未开时 AI 对话也 401。"""
    public, loop = env
    assert public.get("/api/items?limit=1").status_code == 200
    assert public.get("/api/stats").status_code == 200
    assert public.get("/api/watchlist").status_code == 401          # 个人配置
    assert public.put("/api/watchlist", json=[]).status_code == 401  # 改配置
    assert public.post("/api/ai/chat", json={"messages": []}).status_code == 401
    # 带凭据全通
    assert public.get("/api/watchlist", headers=CREDS).status_code == 200


def test_public_ai_guest_chat_and_builtin_limit(env):
    """public_ai：游客 POST /api/ai/chat 过 auth（400=参数错而非 401=没权限）；
    端点内建 10 次/分钟每 IP 限速对游客照砍（第 11 发 429）——防免费代理薅。
    selector 非法让请求在 LLM 调用前 400，测试不触网。"""
    from app.config import CONFIG as C
    public, _ = env
    C["auth"]["public_ai"] = True
    bad = {"selector": {"scope": "bogus-不会存在的"}, "messages": []}
    codes = [public.post("/api/ai/chat", json=bad,
                         headers={"cf-connecting-ip": "203.0.113.55"}).status_code
             for _ in range(12)]
    assert codes[0] == 400                      # auth 过了，死在参数校验
    assert all(c == 400 for c in codes[:10])
    assert all(c == 429 for c in codes[10:])     # 内建 AI 限速接管
    # 关掉开关立即回 401
    C["auth"]["public_ai"] = False
    assert public.post("/api/ai/chat", json=bad).status_code == 401


def test_public_read_off_keeps_legacy_behavior(env, monkeypatch):
    """public_read 缺省关：游客 GET 回到 401（旧语义），带凭据照常。"""
    public, _ = env
    monkeypatch.setitem(CONFIG["auth"], "public_read", False)
    assert public.get("/api/items?limit=1").status_code == 401
    assert public.get("/api/items?limit=1", headers=CREDS).status_code == 200


def test_rate_limit_buckets_and_429(env):
    """令牌桶：rpm=5 的桶连打 8 发，尾部 429；另一 IP 分桶不受影响。"""
    public, _ = env
    got = [public.get("/api/items?limit=1",
                      headers={"cf-connecting-ip": "203.0.113.9"}).status_code
           for _ in range(8)]
    assert got[:5] == [200] * 5 and got[5:] == [429] * 3
    assert public.get("/api/items?limit=1",
                      headers={"cf-connecting-ip": "203.0.113.10"}).status_code == 200


def test_rate_limit_loopback_exempt(env):
    """本机直连（peer 回环+无代理头+Host 回环）不限速也不计数。"""
    _, loop = env
    codes = [loop.get("/api/items?limit=1").status_code for _ in range(8)]
    assert all(c == 200 for c in codes)
    snap = loop.get("/api/access").json()
    assert snap["unique_ips"] == 0          # 主人不算观察对象


def test_access_stats_counts_and_loopback_gate(env):
    """统计：公网请求入账（分类/429），/api/access 本机可看、公网 404。"""
    public, loop = env
    public.get("/api/items?limit=1", headers={"cf-connecting-ip": "198.51.100.7"})
    public.get("/", headers={"cf-connecting-ip": "198.51.100.7"})
    public.get("/healthz", headers={"cf-connecting-ip": "198.51.100.7"})
    for _ in range(7):
        public.get("/api/items?limit=1", headers={"cf-connecting-ip": "198.51.100.7"})
    r = loop.get("/api/access")             # 本机直连可看
    assert r.status_code == 200
    d = r.json()
    assert d["unique_ips"] == 1
    assert d["total"] >= 10
    entry = next(e for e in d["top"] if e["ip"] == "198.51.100.7")
    assert entry["healthz"] == 1 and entry["page"] == 1
    assert entry["api"] >= 8 and entry["limited"] >= 2
    assert d["limited_total"] >= 2
    # 经隧道形态（带 cf-connecting-ip）→ 404 不显存在（用新 IP 免被限速器先拦）
    assert public.get("/api/access",
                      headers={"cf-connecting-ip": "198.51.100.99"}).status_code == 404


def test_access_stats_flush_and_reload(tmp_path, monkeypatch):
    """落盘原子写 + 新实例预载（观察不因重启断档）。"""
    monkeypatch.setenv("TTSNIFFER_HOME", str(tmp_path))
    import importlib
    importlib.reload(access_stats)
    reset_rate_limiter()
    saved = dict(CONFIG.get("auth") or {})
    CONFIG["auth"] = {"enabled": True, "user": "owner",
                      "password_sha256": _PW_HASH,
                      "public_read": True, "rate_limit_rpm": 1000}
    try:
        app = create_app(Store(tmp_path / "t.db"),
                         Pipeline(Store(tmp_path / "t.db")))
        c = TestClient(app, client=("10.0.0.1", 500))
        c.get("/api/items?limit=1", headers={"cf-connecting-ip": "203.0.113.77"})
        access_stats.maybe_flush()                     # 首次必落盘
        f = tmp_path / "access_stats.json"
        assert f.exists()
        assert not (tmp_path / "access_stats.json.tmp").exists()
        payload = json.loads(f.read_text(encoding="utf-8"))
        assert "203.0.113.77" in payload["ips"]
        # 预载：新实例统计仍在
        importlib.reload(access_stats)
        assert "203.0.113.77" in access_stats._ips
    finally:
        CONFIG["auth"] = saved
        reset_rate_limiter()
