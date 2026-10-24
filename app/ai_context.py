"""AI 上下文打包器（S5 门8）：给分析师的资料包，不是全库导出。

selector：{scope: today|day|days|cls|search|ids|asset, day?, days?, cls?, q?, ids?, asset?}
- today/day/cls/search/asset 复用 store.query（q 的多关键词 AND 语义自动继承 S1）；
- days=N 近 N 日闭区间（含今天，AI 抽屉「近7天」快捷）；
- ids 直取指定条目（store.items_by_ids）。
序列化行：`[id] MM-DD HH:MM 来源/档位 分类 分数 | 标题 | 正文截500字`。
预算纪律：默认上限 100 条 / 60k 字符，超出按分数从低到高截并在包尾注明截断数。
"""
from __future__ import annotations

from datetime import date, timedelta
from .store import Store

MAX_ITEMS = 100
MAX_CHARS = 60_000
TRUNCATE_TEXT = 500

_TIER_TAG = {"street": "小道"}


def _fmt_row(it: dict) -> str:
    ts = (it.get("ts") or "")[5:16]                       # MM-DD HH:MM
    tier = _TIER_TAG.get(it.get("tier") or "", "官方流")
    title = (it.get("title") or "").replace("\n", " ")
    text = (it.get("text") or "").replace("\n", " ")
    if len(text) > TRUNCATE_TEXT:
        text = text[:TRUNCATE_TEXT] + "…"
    src = it.get("source") or ""
    return (f"[{it['id']}] {ts} {src}/{tier} {it.get('cls')} {it.get('score')}分"
            f" | {title} | {text}")


def _fetch(store: Store, selector: dict) -> list[dict]:
    scope = selector.get("scope", "today")
    if scope == "ids":
        return store.items_by_ids([str(x) for x in selector.get("ids", [])][:MAX_ITEMS])
    kw: dict = {"limit": MAX_ITEMS, "sort": "score"}
    if scope == "today":
        kw["day"] = str(date.today())
    elif scope == "day":
        kw["day"] = str(selector.get("day") or date.today())
    elif scope == "days":
        try:
            n = int(selector.get("days") or 7)
        except (TypeError, ValueError):
            n = 7
        n = min(max(n, 1), 30)
        today = date.today()
        kw["day_from"] = str(today - timedelta(days=n - 1))
        kw["day_to"] = str(today)
    elif scope == "cls":
        kw["cls"] = str(selector.get("cls") or "all")
    elif scope == "search":
        kw["q"] = str(selector.get("q") or "")
        if selector.get("day"):
            kw["day"] = str(selector["day"])
    elif scope == "asset":
        a = str(selector.get("asset") or "").strip()
        if a and a != "all":
            kw["asset"] = a
    else:
        raise ValueError(f"unknown scope: {scope}")
    return store.query(**kw)["items"]


def build_context(store: Store, selector: dict) -> str:
    """资料包正文。返回 (文本)；截断发生时包尾注明（模型需知道它看到的是子集）。"""
    items = _fetch(store, selector or {})
    rows = [_fmt_row(it) for it in items]
    total = len(rows)
    kept = len("\n".join(rows).encode("utf-8"))
    # 字符预算：从分数最低的行开始丢（sort=score 时列表尾部即低分）
    while rows and kept > MAX_CHARS:
        dropped = rows.pop()
        kept -= len(dropped.encode("utf-8")) + 1
    body = "\n".join(rows)
    if total > len(rows):
        body += f"\n（预算截断：共 {total} 条，已按分数截取前 {len(rows)} 条）"
    if not rows:
        body = "（该选择范围暂无条目）"
    return body
