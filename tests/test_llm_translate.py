"""LLM 翻译单元：字段校验与清洗，不打真实网络。"""
import asyncio

from app import llm


def test_translate_strips_and_returns_payload(monkeypatch):
    async def fake_completion(prompt, label, *args, **kwargs):
        assert "标题：Fed" in prompt
        assert kwargs["max_tokens"] == 500
        return {"title_zh": " 美联储按兵不动 ", "summary_zh": " 政策维持不变 "}

    monkeypatch.setattr(llm, "_json_completion", fake_completion)
    out = asyncio.run(llm.translate("Fed", "Policy unchanged"))
    assert out == {"title_zh": "美联储按兵不动", "summary_zh": "政策维持不变"}


def test_translate_rejects_missing_title(monkeypatch):
    async def fake_completion(prompt, label, *args, **kwargs):
        return {"title_zh": "  ", "summary_zh": "摘要"}

    monkeypatch.setattr(llm, "_json_completion", fake_completion)
    assert asyncio.run(llm.translate("Fed", "Policy unchanged")) is None


def test_translate_retries_transient_failure(monkeypatch):
    """一次网络抖动不应让外网条目永久失去翻译。"""
    from app.config import CONFIG

    monkeypatch.setitem(CONFIG, "llm", {
        "enabled": True, "api_key_env": "TEST_LLM_KEY",
        "base_url": "https://example.test/v1", "model": "test-model",
    })
    monkeypatch.setenv("TEST_LLM_KEY", "test-key")
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {
                "content": '{"title_zh": "标题", "summary_zh": "摘要"}'}}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise llm.httpx.ConnectError("")
            return Resp()

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(llm.httpx, "AsyncClient", Client)
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    out = asyncio.run(llm.translate("Fed", "Policy unchanged"))
    assert len(calls) == 2
    assert out == {"title_zh": "标题", "summary_zh": "摘要"}
