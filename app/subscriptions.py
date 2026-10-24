"""声明式订阅（订阅化阶段一，20261024）：subscriptions/*.yaml 丢文件即订阅。

设计依据 docs/reports/20261023_订阅插件化架构调研.md 阶段一："声明式订阅是
一等公民"——新增一个 RSS 源只需放一个 YAML 文件，热加载（mtime 变化下轮
生效），无需改 config、无需重启。

v1 刻意做小（避免过度设计）：
- 只支持 kind: rss（复用现有 rss 采集器与其容错/限速语义）；
- 校验失败的文件跳过并告警，绝不拖死整轮（与采集器"单源失败不毒整轮"同纪律）；
- 不做 per-sub interval（整段一个轮询周期=min(各订阅 interval, 默认 600s)，
  v2 再拆）；不做代码插件、不做市场（调研结论：单人项目做"少而稳"）。

schema（subscriptions/README.md 有完整示例）：
    apiVersion: ttsniffer/v1
    kind: Subscription
    metadata: {id: my-feed, name: 显示名}
    spec: {kind: rss, url: https://…, source: my_feed, tier: street,
           max_age_days: 2, enabled: true, interval: 600}
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from .config import ROOT

log = logging.getLogger("subscriptions")
DEFAULT_DIR = ROOT / "subscriptions"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_TIERS = ("street", "authoritative")
# 模块级缓存：dir/files mtime -> 上次结果；热加载靠 mtime 变化检测
_cache: dict[str, tuple[float, list[tuple[float, str]]]] = {}
_cache_result: dict[str, list[dict]] = {}


def _validate(doc: object, fname: str) -> dict | None:
    """单个订阅文档校验：任何不合法 → None（调用方跳过并告警）。"""
    if not isinstance(doc, dict):
        log.warning("[subs] %s 非 mapping，跳过", fname)
        return None
    if doc.get("apiVersion") != "ttsniffer/v1":
        log.warning("[subs] %s apiVersion 非 ttsniffer/v1，跳过", fname)
        return None
    meta = doc.get("metadata")
    if not isinstance(meta, dict) or not _ID_RE.match(str(meta.get("id") or "")):
        log.warning("[subs] %s metadata.id 缺失或非法（^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$），跳过", fname)
        return None
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        log.warning("[subs] %s spec 缺失，跳过", fname)
        return None
    if spec.get("kind", "rss") != "rss":
        log.warning("[subs] %s spec.kind=%r 阶段一只支持 rss，跳过", fname, spec.get("kind"))
        return None
    url = str(spec.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        log.warning("[subs] %s spec.url 非法（须 http/https），跳过", fname)
        return None
    tier = str(spec.get("tier") or "street")
    if tier not in _TIERS:
        log.warning("[subs] %s spec.tier=%r 非法（street|authoritative），按 street", fname, tier)
        tier = "street"
    sid = str(meta["id"])
    source = str(spec.get("source") or sid)[:32]
    try:
        max_age = max(0, int(spec.get("max_age_days", 2)))
        interval = max(60, int(spec.get("interval", 600)))
    except (TypeError, ValueError):
        log.warning("[subs] %s max_age_days/interval 非数字，按默认值", fname)
        max_age, interval = 2, 600
    return {"id": sid, "name": str(meta.get("name") or sid)[:32], "source": source,
            "url": url, "tier": tier, "max_age_days": max_age,
            "interval": interval, "enabled": bool(spec.get("enabled", True))}


def load_subscriptions(directory: Path | None = None) -> list[dict]:
    """读目录下全部 *.yaml/*.yml → 校验通过的订阅列表（热加载：mtime 未变用缓存）。

    同 id 重复：先到先得并告警（id 是水位/去重的锚，后到的必须让位）。
    """
    directory = Path(directory) if directory else DEFAULT_DIR
    if not directory.is_dir():
        return []
    files = sorted(p for p in directory.glob("*.y*ml") if p.is_file())
    stamp = (directory.stat().st_mtime, [(p.stat().st_mtime, p.name) for p in files])
    cached = _cache.get(str(directory))
    if cached and cached[0] == stamp[0] and cached[1] == stamp[1]:
        return _cache_result[str(directory)]
    out: list[dict] = []
    seen: set[str] = set()
    for p in files:
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001  单文件损坏不毒整轮
            log.warning("[subs] %s 解析失败，跳过: %s", p.name, e)
            continue
        sub = _validate(doc, p.name)
        if sub is None or not sub["enabled"]:
            continue
        if sub["id"] in seen:
            log.warning("[subs] %s 与已有订阅 id=%s 重复，忽略后者", p.name, sub["id"])
            continue
        seen.add(sub["id"])
        out.append(sub)
    _cache[str(directory)] = stamp
    _cache_result[str(directory)] = out
    return out


def as_feed_cfg(subs: list[dict]) -> dict:
    """订阅列表 → rss 采集器的 feeds 配置（source/url/tier/max_age_days 透传）。"""
    return {"feeds": [{"source": s["source"], "name": s["name"], "url": s["url"],
                       "tier": s["tier"], "max_age_days": s["max_age_days"],
                       "enabled": True} for s in subs]}


def cycle_seconds(subs: list[dict], default: int = 600) -> int:
    """轮询周期 = 各订阅 interval 的最小值（v1 全段同轮；防单订阅 60s 拖整段）。"""
    return min([s["interval"] for s in subs] + [default])
