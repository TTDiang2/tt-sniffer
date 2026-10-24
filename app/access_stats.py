"""公网访问统计（观察期，20261024）：谁来了、来了多勤、有没有恶意。

中间件逐请求计数（内存），60s 惰性落盘 data/access_stats.json（原子替换，
重启预载——观察不因重启断档）。按 cf-connecting-ip（隧道真实访客 IP）聚合：

    /api/access 端点（server.py，仅本机直连可看）返回：
    - unique_ips / total：独立 IP 数与总请求
    - top：按请求数排序的 IP 明细（api/page/healthz/static 分类计数、
      4xx/5xx/429、首见/最近）
    - uas：User-Agent 聚合 top（python-requests/curl 一眼可见）

纪律：本机直连（loopback 三条件，同 auth._loopback_exempt）的请求不计数
——主人自己不算观察对象；IP 明细只落本机 data/，不对外、不入库。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("access")
_IP_MAX = 4096
_UA_MAX = 256
_PRUNE_AGE = 48 * 3600.0     # 48h 无活动出清（落盘时）
_FLUSH_INTERVAL = 60.0

_since: float = time.time()
_ips: dict[str, dict] = {}
_uas: dict[str, int] = {}
_limited_total = 0
_last_flush = 0.0


def _file() -> Path:
    from .config import data_dir
    return data_dir() / "access_stats.json"


def _load() -> None:
    global _since, _ips, _uas, _limited_total
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as e:  # noqa: BLE001  损坏不致命：从零开始观察
        log.warning("access_stats 载入失败，按空表开始: %r", e)
        return
    if not isinstance(data, dict):
        return
    _since = float(data.get("since") or time.time())
    _ips = data.get("ips") if isinstance(data.get("ips"), dict) else {}
    _uas = data.get("uas") if isinstance(data.get("uas"), dict) else {}
    _limited_total = int(data.get("limited_total") or 0)
    log.info("access_stats 预载 %d 个 IP（自 %s）", len(_ips),
             time.strftime("%m-%d %H:%M", time.localtime(_since)))


_load()


def reset_for_tests() -> None:
    global _since, _ips, _uas, _limited_total, _last_flush
    _since, _ips, _uas, _limited_total, _last_flush = time.time(), {}, {}, 0, 0.0


def _classify(path: str) -> str:
    if path == "/healthz":
        return "healthz"
    if path == "/" or path.startswith("/assets/") or path.startswith("/icons/") \
            or path in ("/manifest.webmanifest", "/sw.js"):
        return "page"
    if path.startswith("/api/"):
        return "api"
    return "other"


def observe_request(request) -> None:
    """中间件入口调用：按访客 IP 记一笔（本机直连不计）。"""
    from .auth import _client_key, _loopback_exempt
    if _loopback_exempt(request):
        return
    key = _client_key(request)
    cls = _classify(request.url.path)
    now = time.time()
    entry = _ips.get(key)
    if entry is None:
        if len(_ips) >= _IP_MAX:
            _prune(now)
        entry = _ips.setdefault(key, {
            "n": 0, "api": 0, "page": 0, "healthz": 0, "static": 0, "other": 0,
            "err4": 0, "err5": 0, "limited": 0, "first": now, "last": now,
        })
    entry["n"] += 1
    entry[cls] = entry.get(cls, 0) + 1
    entry["last"] = now
    ua = (request.headers.get("user-agent") or "").strip()[:80]
    if ua:
        if len(_uas) >= _UA_MAX and ua not in _uas:
            _prune(now)
        _uas[ua] = _uas.get(ua, 0) + 1


def observe_status(request, status: int) -> None:
    """中间件出口调用：补状态码分类（401/429/5xx 是滥用信号）。"""
    from .auth import _client_key, _loopback_exempt
    if _loopback_exempt(request):
        return
    global _limited_total
    key = _client_key(request)
    entry = _ips.get(key)
    if entry is None:
        return
    if status == 429:
        entry["limited"] += 1
        _limited_total += 1
    elif 400 <= status < 500:
        entry["err4"] += 1
    elif status >= 500:
        entry["err5"] += 1


def _prune(now: float) -> None:
    for k, e in list(_ips.items()):
        if now - e.get("last", 0) > _PRUNE_AGE:
            _ips.pop(k, None)
    total_ua = sum(_uas.values())
    if len(_uas) > _UA_MAX and total_ua > 0:
        # UA 表按计数保 top（低频杂音出清）
        keep = sorted(_uas.items(), key=lambda kv: -kv[1])[:_UA_MAX]
        _uas.clear()
        _uas.update(keep)


def _flush(now: float) -> None:
    global _last_flush
    try:
        _prune(now)
        tmp = _file().with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "since": _since, "limited_total": _limited_total,
            "ips": _ips, "uas": _uas,
        }, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_file())
        _last_flush = now
    except Exception as e:  # noqa: BLE001  统计写盘失败绝不影响主链路
        log.warning("access_stats 落盘失败: %r", e)


def maybe_flush() -> None:
    """60s 惰性落盘（中间件里顺手调，不 spawn 任务）。"""
    now = time.time()
    if now - _last_flush >= _FLUSH_INTERVAL:
        _flush(now)


def snapshot() -> dict:
    """/api/access 返回体（仅本机直连可看）。"""
    top = sorted(_ips.items(), key=lambda kv: -kv[1]["n"])[:50]
    uas = sorted(_uas.items(), key=lambda kv: -kv[1])[:30]
    return {
        "since": _since,
        "days": round((time.time() - _since) / 86400, 1),
        "total": sum(e["n"] for e in _ips.values()),
        "unique_ips": len(_ips),
        "limited_total": _limited_total,
        "top": [{"ip": k, **e} for k, e in top],
        "uas": [{"ua": k, "n": v} for k, v in uas],
    }
