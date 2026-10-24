"""Webhook 推送：HTTP 200 里的业务错误码必须判失败，Markdown 特殊字符需转义。"""
import asyncio
from datetime import datetime

from app import push
from app.models import Item


def _item():
    return Item(id="p1", source="cls", ts=datetime.now(),
                title="[传闻] *茅台* 提价", text="正文\n第二行", url="https://x",
                cls="rumor", score=88, keywords=["[传闻]"], targets=[])


def test_markdown_escaped_in_payload():
    md = push.build_payload("wecom", _item())["markdown"]["content"]
    assert "\\[传闻\\]" in md
    assert "\\*茅台\\*" in md
    assert "正文 第二行" in md


def test_http_200_business_error_is_failure(monkeypatch):
    class Resp:
        status_code = 200
        def json(self):
            return {"errcode": 40001, "errmsg": "invalid webhook"}

    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def post(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr(push.httpx, "AsyncClient", lambda **kwargs: Client())
    assert asyncio.run(push.send("http://x", "wecom", _item())) is False


def test_http_200_zero_business_code_is_success(monkeypatch):
    class Resp:
        status_code = 200
        def json(self):
            return {"errcode": 0}

    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def post(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr(push.httpx, "AsyncClient", lambda **kwargs: Client())
    assert asyncio.run(push.send("http://x", "wecom", _item())) is True
