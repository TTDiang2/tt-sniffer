import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest


@pytest.fixture(autouse=True)
def _auth_off_for_tests():
    """测试环境强制 auth 关闭——用户 config.yaml 启用了 auth 时，HTTP 层
    用例不得被 401 干扰（v8/R1：交付状态校验门禁曾因此变红）。"""
    from app.config import CONFIG
    old = CONFIG.get("auth")
    CONFIG["auth"] = {"enabled": False}
    yield
    CONFIG["auth"] = old
