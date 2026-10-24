"""告警推送：企业微信 / 钉钉 / 飞书 群机器人（官方 webhook，零封号风险）。

三种格式统一组装：分数、分类、来源、命中词、资产影响方向、原文链接。
"""
from __future__ import annotations

import logging

import httpx

from .impact import ASSETS

log = logging.getLogger("push")

CLS_LABEL = {"rumor": "传闻", "debunk": "辟谣", "official": "官方", "noise": "快讯"}
DIR_LABEL = {"bull": "利多", "bear": "利空", "rel": "相关"}


def _md_escape(value: str) -> str:
    text = value or ""
    for ch in ("\\", "*", "_", "[", "]", "`", "#", ">"):
        text = text.replace(ch, "\\" + ch)
    return text.replace("\r", " ").replace("\n", " ")


def build_payload(kind: str, item) -> dict:
    """按平台组装消息体。item 为 Item 或带属性的 dict。"""
    label = CLS_LABEL.get(item.cls, item.cls)
    kws = "、".join(item.keywords[:8]) if item.keywords else "-"
    url = item.url or ""
    head = f"TT_Sniffer {label} · {item.score}分"
    imps = getattr(item, "impacts", None) or []
    imp_line = ("/".join(f"{DIR_LABEL[x['d']]}·{ASSETS.get(x['a'], {}).get('name', x['a'])}"
                         for x in imps if x.get("d") != "rel")) if imps else ""
    body = _md_escape((item.title or item.text[:80]).strip())
    text_body = _md_escape((item.text or "")[:300])
    kws = _md_escape(kws)

    md = (f"**{head}**" + (f"\n\n**{imp_line}**" if imp_line else "") +
          f"\n> 来源 {item.source} · {item.ts.strftime('%m-%d %H:%M')}\n\n"
          f"{body}\n\n{text_body}\n\n"
          f"命中: {kws}" + (f"\n\n[原文]({url})" if url else ""))
    plain = (f"{head}" + (f"\n{imp_line}" if imp_line else "") +
             f"\n来源 {item.source} {item.ts.strftime('%m-%d %H:%M')}\n"
             f"{body}\n{text_body}\n命中: {kws}" + (f"\n{url}" if url else ""))

    kind = (kind or "wecom").lower()
    if kind == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": head, "text": md}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": plain}}
    return {"msgtype": "markdown", "markdown": {"content": md}}  # wecom


async def send(url: str, kind: str, item) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=build_payload(kind, item))
    except Exception as e:
        log.warning("webhook 推送失败: %s", e)
        return False
    if r.status_code >= 300:
        log.warning("webhook HTTP %s", r.status_code)
        return False
    try:
        data = r.json()
    except ValueError:
        return True
    if not isinstance(data, dict):
        return True
    code = data.get("errcode", data.get("code", 0))
    if code not in (0, "0", None):
        log.warning("webhook 业务错误 code=%r msg=%r", code,
                    data.get("errmsg") or data.get("msg"))
        return False
    return True
