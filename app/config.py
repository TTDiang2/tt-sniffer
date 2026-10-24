"""配置加载：config.yaml 优先，缺失时回退 config.example.yaml 内置默认。"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
log = logging.getLogger("config")


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        user = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return deep_merge(DEFAULTS, user)
    return copy.deepcopy(DEFAULTS)


LLM_CHANNELS = ("translate", "comment", "chat")


def resolve_channel(llm_cfg: dict, name: str) -> dict:
    """LLM 三通道解析（单一事实源；读时归一化，config.yaml 与测试注入同构）。

    兼容契约（多 key 方案钉死）：
    - 不写 channels = 三通道全部继承旧 base_url/model/api_key_env（零行为变化）；
    - 通道内缺省字段继承旧字段；
    - 显式 keys_env = 完全取代旧 api_key_env（不自动合并、不去重）；
      密钥永不落 config 文件——keys_env 存的是环境变量名。
    畸形输入（channels 非字典 / 通道值非 mapping / keys_env 非列表）一律
    warning 并按未配置降级，不抛错——防 YAML 手滑把 /api/ai/status 打成 500。
    """
    llm_cfg = llm_cfg or {}
    ch: dict = {}
    raw = llm_cfg.get("channels")
    if isinstance(raw, dict):
        v = raw.get(name)
        if isinstance(v, dict):
            ch = dict(v)
        elif v is not None:
            log.warning("llm.channels.%s 非 mapping，按未配置处理: %r", name, v)
    elif raw is not None:
        log.warning("llm.channels 非 mapping，按未配置处理: %r", raw)
    ch.setdefault("base_url", llm_cfg.get("base_url"))
    ch.setdefault("model", llm_cfg.get("model"))
    keys = ch.get("keys_env")
    legacy = [llm_cfg["api_key_env"]] if llm_cfg.get("api_key_env") else []
    if keys is None or keys == []:
        ch["keys_env"] = legacy
    elif isinstance(keys, str):
        log.info("llm.channels.%s.keys_env 为字符串，按单个环境变量名处理: %r", name, keys)
        ch["keys_env"] = [keys] if keys else []
    elif isinstance(keys, (list, tuple)):
        good = [k for k in keys if isinstance(k, str) and k]
        if len(good) != len(keys):
            log.warning("llm.channels.%s.keys_env 含非字符串项已忽略: %r", name, keys)
        ch["keys_env"] = good
    else:
        log.warning("llm.channels.%s.keys_env 类型非法(%s)，按未配置处理",
                    name, type(keys).__name__)
        ch["keys_env"] = legacy
    ch.setdefault("system_prompt", "")
    return ch


CONFIG = load_config()


def data_dir() -> Path:
    d = Path(os.environ.get("TTSNIFFER_HOME", ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d
