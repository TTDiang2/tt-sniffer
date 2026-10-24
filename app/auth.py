"""应用层 Basic Auth：Cloudflare 上线时的第二道密码（第一道是 CF Access）。

用法：config.yaml 设
  auth:
    enabled: true
    user: <用户名>
    password_hash: pbkdf2_sha256$600000$<salt>$<hash>
  或兼容旧配置 password_sha256: <sha256(password) 的十六进制>
密码生成：
  python -c "import hashlib,secrets; s=secrets.token_hex(16); print('pbkdf2_sha256$600000$%s$%s' % (s, hashlib.pbkdf2_hmac('sha256', b'你的密码', s.encode(), 600000).hex()))"
（强随机 ≥16 字符；PBKDF2 有盐，旧 sha256 仅为兼容已有部署保留。）
连续失败按来源 IP 计数：5 分钟内 10 次后返回 429，成功登录清零。
未启用（enabled: false 或缺省）时所有请求直通——本机使用照旧零摩擦。
**enabled 但配置残缺 = fail-closed**（一律 401 + 日志告警），防线绝不无声蒸发。

⚠️ 必须与 HTTPS（Cloudflare 自动提供）配合：明文 HTTP 下 Basic Auth 凭据可被
中间人读取。仅经 Cloudflare 隧道访问时传输层已加密。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import threading
import time

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .config import CONFIG

log = logging.getLogger("auth")
_REALM = "TT_Sniffer"
_FAIL_MAX = 10
_FAIL_WINDOW = 300.0
_FAILURES: dict[str, list[float]] = {}
_FAIL_LOCK = threading.Lock()
_SUCCESS_CACHE: dict[str, float] = {}
_SUCCESS_TTL = 60.0
_SUCCESS_LOCK = threading.Lock()

# ---- 观察期游客只读（20261024）：public_read 开启时 GET/HEAD 免登录 ----
# 语义：公开数据随便看（试验"没有登录是否有人恶意爬"）；但 /api/watchlist
# （个人配置）与一切非 GET（AI 对话/翻译=烧用户 LLM 配额，watchlist PUT=改
# 配置）仍要密码。auth.enabled=false 是全开（含 AI），与 public_read 不同。
_PROTECTED_PREFIXES = ("/api/watchlist",)
_RPM_DEFAULT = 300          # 每 IP 每分钟 /api/* 读请求数上限；0=关闭限速
_RL_LOCK = threading.Lock()
_RL_BUCKETS: dict[str, tuple[float, float]] = {}   # key -> (tokens, last_monotonic)


def public_read_enabled() -> bool:
    return bool((CONFIG.get("auth") or {}).get("public_read"))


def _public_read_exempt(request: Request) -> bool:
    """观察期游客豁免（auth.check 内调用，须 state=="ok"）：

    - public_read: GET/HEAD 公开数据免登录（/api/watchlist 除外）；
    - public_ai: 追加放行 POST /api/ai/chat（对话烧的 LLM 对用户免费，
      且端点自带 10 次/分钟每 IP 限速——真人够用、白嫖代理无利可图）。
    其余非 GET（watchlist PUT 等）与未开开关时维持 401 旧语义。
    """
    cfg = CONFIG.get("auth") or {}
    if not cfg.get("public_read"):
        return False
    if request.method in ("GET", "HEAD"):
        return not request.url.path.startswith(_PROTECTED_PREFIXES)
    return bool(cfg.get("public_ai")) and request.method == "POST" \
        and request.url.path == "/api/ai/chat"


def reset_rate_limiter() -> None:
    """测试辅助：清空令牌桶。"""
    with _RL_LOCK:
        _RL_BUCKETS.clear()


def rate_limit(request: Request) -> Response | None:
    """公网 /api/* 读请求的每 IP 令牌桶（观察期防爬崩；None=放行）。

    桶容量=每分钟配额（突发等额），按恒定速率回流；loopback 直连（无代理头）
    豁免——本机脚本/校验零摩擦。cf-connecting-ip 优先（隧道真实访客 IP）。
    """
    if request.method not in ("GET", "HEAD"):
        return None
    path = request.url.path
    if not path.startswith("/api/") or path == "/api/stream":
        return None
    if _loopback_exempt(request):
        return None
    rpm = int((CONFIG.get("auth") or {}).get("rate_limit_rpm") or _RPM_DEFAULT)
    if rpm <= 0:
        return None
    key = _client_key(request)
    now = time.monotonic()
    with _RL_LOCK:
        tokens, last = _RL_BUCKETS.get(key, (float(rpm), now))
        tokens = min(float(rpm), tokens + (now - last) * rpm / 60.0)
        if tokens < 1.0:
            _RL_BUCKETS[key] = (tokens, now)
            _prune_buckets(now)
            return JSONResponse(
                {"detail": "rate limited; slow down"},
                status_code=429, headers={"Retry-After": "30"})
        _RL_BUCKETS[key] = (tokens - 1.0, now)
        if len(_RL_BUCKETS) > 4096:
            _prune_buckets(now)
    return None


def _prune_buckets(now: float) -> None:
    """桶满 4096 时按最后活跃清旧（半天无流量即清）。"""
    if len(_RL_BUCKETS) <= 4096:
        return
    cutoff = now - 43200.0
    for k, (_t, last) in list(_RL_BUCKETS.items()):
        if last < cutoff:
            _RL_BUCKETS.pop(k, None)

# 回环豁免仅接受这些 Host（DNS 重绑定防护：攻击页经重绑定直连 127.0.0.1 时
# peer 是回环且无代理头，但 Host 是恶意域名——必须校验 Host，v8/R3）
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def _auth_state() -> tuple[str, str, str, str]:
    """返回 ("off","","","") / ("ok",user,kind,credential) / ("broken","","","")。

    broken = enabled 但 user/密码哈希残缺——fail-closed（v8/R2：
    原实现静默放行，防线无声蒸发）。
    """
    auth = CONFIG.get("auth") or {}
    if not auth.get("enabled"):
        return "off", "", "", ""
    user = str(auth.get("user") or "")
    if not user:
        return "broken", "", "", ""
    new_hash = str(auth.get("password_hash") or "")
    if new_hash:
        parts = new_hash.split("$")
        if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
            try:
                iterations = int(parts[1])
            except ValueError:
                iterations = 0
            if 100_000 <= iterations <= 2_000_000 and parts[2] \
                    and _valid_sha256(parts[3]):
                return "ok", user, "pbkdf2", new_hash
        return "broken", "", "", ""
    legacy = str(auth.get("password_sha256") or "")
    if _valid_sha256(legacy):
        return "ok", user, "sha256", legacy
    return "broken", "", "", ""


def _loopback_exempt(request: Request) -> bool:
    """本机回环豁免（run_checks/监控脚本零摩擦）。三条件缺一不可：
    peer 是回环 + 无代理头（XFF/CF-IP）+ Host 是回环名。"""
    client = request.client.host if request.client else ""
    if client != "127.0.0.1":
        return False
    if request.headers.get("x-forwarded-for") is not None \
            or request.headers.get("cf-connecting-ip") is not None:
        return False
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return host in _LOOPBACK_HOSTS


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") \
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _recent_failures(key: str, now: float) -> list[float]:
    hits = _FAILURES.get(key, [])
    hits = [t for t in hits if now - t < _FAIL_WINDOW]
    if hits:
        _FAILURES[key] = hits
    else:
        _FAILURES.pop(key, None)
    return hits


def _blocked(key: str) -> bool:
    now = time.monotonic()
    with _FAIL_LOCK:
        return len(_recent_failures(key, now)) >= _FAIL_MAX


def _record_failure(key: str) -> bool:
    now = time.monotonic()
    with _FAIL_LOCK:
        hits = _recent_failures(key, now)
        hits.append(now)
        _FAILURES[key] = hits
        return len(hits) >= _FAIL_MAX


def _clear_failures(key: str) -> None:
    with _FAIL_LOCK:
        _FAILURES.pop(key, None)


def _cache_key(kind: str, user: str, credential: str,
               req_user: str, req_pwd: str) -> str:
    material = "\0".join((kind, user, credential, req_user, req_pwd))
    return hashlib.sha256(material.encode()).hexdigest()


def _cache_hit(key: str) -> bool:
    now = time.monotonic()
    with _SUCCESS_LOCK:
        stamped = _SUCCESS_CACHE.get(key)
        if stamped is None:
            return False
        if now - stamped >= _SUCCESS_TTL:
            _SUCCESS_CACHE.pop(key, None)
            return False
        return True


def _cache_put(key: str) -> None:
    now = time.monotonic()
    with _SUCCESS_LOCK:
        if len(_SUCCESS_CACHE) >= 1024:
            cutoff = now - _SUCCESS_TTL
            for old_key, stamped in list(_SUCCESS_CACHE.items()):
                if stamped < cutoff:
                    _SUCCESS_CACHE.pop(old_key, None)
            if len(_SUCCESS_CACHE) >= 1024:
                oldest = min(_SUCCESS_CACHE, key=_SUCCESS_CACHE.get)
                _SUCCESS_CACHE.pop(oldest, None)
        _SUCCESS_CACHE[key] = now


def _verify_password(kind: str, password: str, credential: str) -> bool:
    if kind == "pbkdf2":
        _, raw_iterations, salt, expected = credential.split("$")
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(raw_iterations)).hex()
    else:
        actual = hashlib.sha256(password.encode()).hexdigest()
        expected = credential
    return hmac.compare_digest(actual.encode(), expected.encode())


def check(request: Request) -> Response | None:
    """Basic Auth 门（返回 None=放行，Response=拒绝）。"""
    state, user, kind, credential = _auth_state()
    if state == "off":
        return None
    if _loopback_exempt(request):
        return None
    if state == "ok" and _public_read_exempt(request):
        return None            # 观察期游客只读：GET/HEAD 公开数据免登录
    if state == "broken":
        # fail-closed：配置残缺绝不放行（v8/R2）
        log.error("auth.enabled 但 user/密码哈希残缺——全部请求拒绝")
        return _challenge()
    key = _client_key(request)
    if _blocked(key):
        return _challenge(429, _FAIL_WINDOW)
    header = request.headers.get("authorization") or ""
    if not header.startswith("Basic "):
        return _challenge()
    try:
        import base64
        raw = base64.b64decode(header[6:], validate=True).decode("utf-8")
        req_user, _, req_pwd = raw.partition(":")
    except Exception:
        return _failed(key)
    cache_key = _cache_key(kind, user, credential, req_user, req_pwd)
    if _cache_hit(cache_key):
        _clear_failures(key)
        return None
    ok_user = hmac.compare_digest(req_user.encode(), user.encode())
    ok_pwd = _verify_password(kind, req_pwd, credential)
    if ok_user and ok_pwd:
        _cache_put(cache_key)
        _clear_failures(key)
        return None
    return _failed(key)


async def check_async(request: Request) -> Response | None:
    return await asyncio.to_thread(check, request)


def _failed(key: str) -> Response:
    if _record_failure(key):
        log.warning("Basic Auth 连续失败过多，临时封禁 ip=%s", key)
        return _challenge(429, _FAIL_WINDOW)
    return _challenge()


def _challenge(status: int = 401, retry_after: float | None = None) -> Response:
    headers = {"WWW-Authenticate": f'Basic realm="{_REALM}", charset="UTF-8"',
               "Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))
    return Response(
        status_code=status,
        headers=headers,
        content=JSONResponse({"detail": "需要登录"}).body,
        media_type="application/json",
    )
