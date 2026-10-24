"""多 key / 多模型（三通道）单元：配置归一化 + KeyPool 轮换/冷却/禁用 +
通道路由 + 尝试预算钉死 + system_prompt 语义。全程 mock 网络，不打真实接口。"""
import asyncio

import httpx
import pytest

from app import llm
from app.config import resolve_channel


@pytest.fixture(autouse=True)
def _fresh_pools():
    llm._POOLS.clear()
    yield
    llm._POOLS.clear()


LEGACY = {"enabled": True, "api_key_env": "TTS_LLM_KEY", "base_url": "https://legacy.test/v1",
          "model": "legacy-model"}


# ---------- 配置归一化 ----------

def test_resolve_channel_legacy_inherits():
    """不写 channels：三通道全部继承旧 base_url/model/api_key_env（零行为变化）。"""
    for name in ("translate", "comment", "chat"):
        ch = resolve_channel(LEGACY, name)
        assert ch["base_url"] == "https://legacy.test/v1"
        assert ch["model"] == "legacy-model"
        assert ch["keys_env"] == ["TTS_LLM_KEY"]
        assert ch["system_prompt"] == ""


def test_resolve_channel_explicit_keys_replace():
    """显式 keys_env = 完全取代旧 api_key_env（不自动合并、不去重）。"""
    cfg = dict(LEGACY, channels={"translate": {"keys_env": ["K_A", "K_B"]}})
    assert resolve_channel(cfg, "translate")["keys_env"] == ["K_A", "K_B"]
    # 其余通道不受影响
    assert resolve_channel(cfg, "comment")["keys_env"] == ["TTS_LLM_KEY"]


def test_resolve_channel_partial_inherit_and_empty():
    cfg = dict(LEGACY, channels={"chat": {"model": "strong-model"}})
    ch = resolve_channel(cfg, "chat")
    assert ch["model"] == "strong-model"
    assert ch["base_url"] == "https://legacy.test/v1"      # 缺省字段继承
    assert ch["keys_env"] == ["TTS_LLM_KEY"]
    # 旧 api_key_env 缺失且通道未写 keys_env → 空池
    assert resolve_channel({"enabled": True}, "translate")["keys_env"] == []


def test_resolve_channel_malformed_degrades(caplog):
    """畸形 config 警告降级不抛错（防 YAML 手滑打挂 /api/ai/status）。"""
    # channels 为 list → 全部按未配置（继承旧字段）
    for ch in (resolve_channel(dict(LEGACY, channels=["oops"]), "translate"),
               resolve_channel(dict(LEGACY, channels={"translate": "oops"}), "translate")):
        assert ch["keys_env"] == ["TTS_LLM_KEY"]
        assert ch["base_url"] == "https://legacy.test/v1"
    # keys_env: "K_A"（手滑少写 -）→ 整串当单个环境变量名，不拆字符
    assert resolve_channel(dict(LEGACY, channels={
        "translate": {"keys_env": "K_A"}}), "translate")["keys_env"] == ["K_A"]
    # keys_env 非列表非字符串 → 按未配置（继承旧 key）
    assert resolve_channel(dict(LEGACY, channels={
        "translate": {"keys_env": 3}}), "translate")["keys_env"] == ["TTS_LLM_KEY"]
    # 列表内非字符串项被过滤
    assert resolve_channel(dict(LEGACY, channels={
        "translate": {"keys_env": ["K_A", 7, None]}}), "translate")["keys_env"] == ["K_A"]
    assert any("按未配置" in r.message or "单个环境变量名" in r.message
               for r in caplog.records)


# ---------- KeyPool ----------

def test_keypool_rotation_skips_cooldown(monkeypatch):
    monkeypatch.setenv("K_A", "a")
    monkeypatch.setenv("K_B", "b")
    pool = llm.KeyPool(["K_A", "K_B"], cooldown_s=60)
    first = pool.pick()
    second = pool.pick()
    assert first[0] != second[0]                       # round-robin 轮换
    pool.mark_retryable(second[0])                     # second 进 60s 冷却
    third = pool.pick()
    assert third[0] == first[0]                        # 冷却中的被跳过
    pool.mark_ok(second[0])
    assert pool.pick()[0] == second[0]                 # mark_ok 解除冷却


def test_keypool_all_cooled_picks_earliest(monkeypatch):
    monkeypatch.setenv("K_A", "a")
    monkeypatch.setenv("K_B", "b")
    pool = llm.KeyPool(["K_A", "K_B"], cooldown_s=60)
    pool.mark_retryable("K_A")                        # A：now+60
    import time as _t
    pool._cooldown["K_B"] = _t.monotonic() + 120      # B：now+120，比 A 更晚结束
    assert pool.pick()[0] == "K_A"                    # 全冷却取最早结束者


def test_keypool_auth_fail_disables_after_two(monkeypatch):
    monkeypatch.setenv("K_A", "a")
    monkeypatch.setenv("K_B", "b")
    pool = llm.KeyPool(["K_A", "K_B"])
    assert pool.mark_auth_fail("K_A") is False         # 第一次：计数未达
    assert pool.mark_auth_fail("K_A") is True          # 第二次：禁用
    for _ in range(4):
        assert pool.pick()[0] == "K_B"                 # 禁用后永不再选中
    pool.mark_ok("K_A")                                # 成功一次即复位
    assert pool.pick()[0] == "K_A"


def test_keypool_skips_empty_env(monkeypatch):
    monkeypatch.setenv("K_B", "b")
    monkeypatch.delenv("K_A", raising=False)
    pool = llm.KeyPool(["K_A", "K_B"])
    for _ in range(3):
        assert pool.pick() == ("K_B", "b")
    monkeypatch.delenv("K_B", raising=False)
    assert pool.pick() is None                         # 全空 → None


# ---------- 通道路由 + 预算钉死 ----------

def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return httpx.HTTPStatusError(f"{code}", request=req,
                                 response=httpx.Response(code, request=req))


class FakeClient:
    """记录每次调用的 Authorization 与 body；按脚本抛错/回包。script: list[dict]"""
    calls: list[dict] = []

    def __init__(self, script, *args, **kwargs):
        self._script = script

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        FakeClient.calls.append({"url": url, "auth": headers["Authorization"], "json": json})
        step = self._script[min(len(FakeClient.calls) - 1, len(self._script) - 1)]
        if step.get("raise"):
            raise step["raise"]
        return _FakeResp(step.get("body") or
                         {"choices": [{"message": {"content": '{"title_zh": "t", "summary_zh": "s"}'}}]})


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


async def _no_sleep(_s):
    pass


def test_translate_rotates_key_on_429(monkeypatch):
    """translate 预算 3 次：keyA 429 → 冷却并换 keyB 成功；密钥只经环境变量。"""
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", dict(LEGACY, channels={
        "translate": {"base_url": "https://pool.test/v1", "model": "tr-model",
                      "keys_env": ["K_A", "K_B"]}}))
    monkeypatch.setenv("K_A", "key-a")
    monkeypatch.setenv("K_B", "key-b")
    FakeClient.calls = []
    script = [{"raise": _status_error(429)},
              {"body": {"choices": [{"message": {"content": '{"title_zh": "标题", "summary_zh": "摘要"}'}}]}}]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: FakeClient(script, *a, **k))
    monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)
    out = asyncio.run(llm.translate("Fed", "Policy"))
    assert out == {"title_zh": "标题", "summary_zh": "摘要"}
    assert len(FakeClient.calls) == 2
    assert FakeClient.calls[0]["auth"] == "Bearer key-a"
    assert FakeClient.calls[1]["auth"] == "Bearer key-b"        # 换 key 重试
    assert FakeClient.calls[0]["url"] == "https://pool.test/v1/chat/completions"
    assert FakeClient.calls[0]["json"]["model"] == "tr-model"   # 走 translate 通道


def test_translate_budget_three_attempts(monkeypatch):
    """3 次预算用尽即失败（key 池不放大请求量）。"""
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", dict(LEGACY, channels={
        "translate": {"keys_env": ["K_A", "K_B"]}}))
    monkeypatch.setenv("K_A", "a")
    monkeypatch.setenv("K_B", "b")
    FakeClient.calls = []
    script = [{"raise": _status_error(429)}]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: FakeClient(script, *a, **k))
    monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)
    assert asyncio.run(llm.translate("Fed", "Policy")) is None
    assert len(FakeClient.calls) == 3                           # retries=2 → 共 3 次


def test_comment_single_attempt_budget(monkeypatch):
    """comment 通道预算=单次：429 也不换 key 加试（方案钉死）。"""
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", dict(LEGACY, channels={
        "comment": {"keys_env": ["K_A", "K_B"]}}))
    monkeypatch.setenv("K_A", "a")
    monkeypatch.setenv("K_B", "b")
    FakeClient.calls = []
    script = [{"raise": _status_error(429)}]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: FakeClient(script, *a, **k))
    assert asyncio.run(llm.enrich("标题", "正文")) is None
    assert len(FakeClient.calls) == 1


def test_auth_error_fails_immediately(monkeypatch):
    """401：立即失败不重试；两次后该 key 禁用（后续请求走别的 key）。"""
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", dict(LEGACY, channels={
        "translate": {"keys_env": ["K_A", "K_B"]}}))
    monkeypatch.setenv("K_A", "a")
    monkeypatch.setenv("K_B", "b")
    FakeClient.calls = []
    script = [{"raise": _status_error(401)}]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: FakeClient(script, *a, **k))
    assert asyncio.run(llm.translate("Fed", "Policy")) is None
    assert len(FakeClient.calls) == 1                           # 401 不重试
    # 第二次 401（同 key 再轮到一次）→ 禁用；之后 pick 只剩 K_B
    pool = llm._pool("translate")
    pool.mark_auth_fail("K_A")
    for _ in range(3):
        assert pool.pick()[0] == "K_B"


# ---------- system_prompt 语义 ----------

def test_system_prompt_appended_not_spliced(monkeypatch):
    """comment 配了 system_prompt：以 role=system 附加，内置模板（含 JSON 契约）原样保留。"""
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", dict(LEGACY, channels={
        "comment": {"keys_env": ["K_A"], "system_prompt": "one_line 要更毒舌"}}))
    monkeypatch.setenv("K_A", "a")
    FakeClient.calls = []
    script = [{"body": {"choices": [{"message": {"content": '{"is_rumor": false}'}}]}}]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: FakeClient(script, *a, **k))
    asyncio.run(llm.enrich("标题", "正文"))
    msgs = FakeClient.calls[0]["json"]["messages"]
    assert msgs[0] == {"role": "system", "content": "one_line 要更毒舌"}
    user = msgs[1]["content"]
    assert '"is_rumor"' in user and "只输出 JSON" in user       # 模板未被改动


def test_no_system_message_legacy_parity(monkeypatch):
    """未配 system_prompt：messages 只有一条 user（与旧版逐字节同构）。"""
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", LEGACY)
    monkeypatch.setenv("TTS_LLM_KEY", "k")
    FakeClient.calls = []
    script = [{"body": {"choices": [{"message": {"content": '{"is_rumor": false}'}}]}}]
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: FakeClient(script, *a, **k))
    asyncio.run(llm.enrich("标题", "正文"))
    msgs = FakeClient.calls[0]["json"]["messages"]
    assert len(msgs) == 1 and msgs[0]["role"] == "user"


def test_chat_system_prompt_replaces_default(monkeypatch):
    from app.config import CONFIG
    assert llm.system_prompt_for("chat", "DEFAULT") == "DEFAULT"   # 未配置 → 默认模板
    monkeypatch.setitem(CONFIG, "llm", dict(LEGACY, channels={
        "chat": {"system_prompt": "自定义分析师"}}))
    assert llm.system_prompt_for("chat", "DEFAULT") == "自定义分析师"


# ---------- chat 通道 ----------

class _FakeStreamResp:
    def __init__(self, status, lines):
        self.status_code = status
        self._lines = lines

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _status_error(self.status_code)

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeStreamClient:
    calls: list[dict] = []

    def __init__(self, resp, *a, **k):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None, json=None):
        FakeStreamClient.calls.append({"url": url, "auth": headers["Authorization"], "json": json})
        return _FakeCtx(self._resp)


class _FakeCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def test_chat_stream_uses_channel_and_cooldown(monkeypatch):
    """chat 走 chat 通道配置；429 标记冷却；单次预算不重试。"""
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", dict(LEGACY, channels={
        "chat": {"base_url": "https://chat.test/v1", "model": "strong-model",
                 "keys_env": ["K_A"]}}))
    monkeypatch.setenv("K_A", "chat-key")
    FakeStreamClient.calls = []
    resp = _FakeStreamResp(429, [])
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: FakeStreamClient(resp, *a, **k))
    with pytest.raises(httpx.HTTPStatusError):
        async def run():
            async for _ in llm.chat_stream([{"role": "user", "content": "hi"}]):
                pass
        asyncio.run(run())
    assert FakeStreamClient.calls[0]["json"]["model"] == "strong-model"
    assert FakeStreamClient.calls[0]["auth"] == "Bearer chat-key"
    pool = llm._pool("chat")
    assert pool._cooldown.get("K_A", 0) > 0                     # 429 已冷却


def test_chat_stream_yields_deltas(monkeypatch):
    from app.config import CONFIG
    monkeypatch.setitem(CONFIG, "llm", dict(LEGACY, channels={
        "chat": {"base_url": "https://chat.test/v1", "keys_env": ["K_A"]}}))
    monkeypatch.setenv("K_A", "chat-key")
    lines = ['data: {"choices": [{"delta": {"content": "你好"}}]}',
             "data: [DONE]"]
    resp = _FakeStreamResp(200, lines)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **k: FakeStreamClient(resp, *a, **k))

    async def run():
        out = []
        async for d in llm.chat_stream([{"role": "user", "content": "hi"}]):
            out.append(d)
        return out

    assert asyncio.run(run()) == ["你好"]
