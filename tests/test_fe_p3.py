"""P3 统计视图断言（五卡/图例/刻度/矩阵色清债/点按交互/390）（tests/fe_p3_assertions.js 的 pytest 包装）。

需要：隔离实例 8788 在跑 + Chrome CDP 9334（headless）+ 支持 WebSocket 的
Node（≥22，本机 PATH 默认是 18 —— 探测顺序：$TTS_NODE → PATH →
workbuddy Node 22）。缺任一则 skip（CI 无法起浏览器时同样 skip，几何
把关在验收门执行）。"""
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKBUDDY_NODE = next(
    iter(sorted(
        (p for p in (Path.home() / ".workbuddy" / "binaries" / "node" / "versions").glob("*/node.exe")),
        reverse=True,
    )),
    Path(),
)   # 本机多版本 node 安装器惯例路径（取最高版本）；无则空 Path，探测链下一环兜底


def _has_websocket(node_exe: str) -> bool:
    try:
        r = subprocess.run(
            [node_exe, "-e", "process.stdout.write(typeof WebSocket === 'undefined' ? 'no' : 'yes')"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() == "yes"
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_node():
    cands = []
    if os.environ.get("TTS_NODE"):
        cands.append(os.environ["TTS_NODE"])
    on_path = shutil.which("node")
    if on_path:
        cands.append(on_path)
    if WORKBUDDY_NODE.exists():
        cands.append(str(WORKBUDDY_NODE))
    for c in cands:
        if Path(c).exists() and _has_websocket(c):
            return c
    return None


node = find_node()


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


services_up = _port_open(8788) and _port_open(
    int(os.environ.get("TTS_CDP_PORT", "9334")))
pytestmark = pytest.mark.skipif(
    node is None or os.environ.get("TTS_SKIP_E2E") == "1" or not services_up,
    reason="no WebSocket-capable node (>=22), TTS_SKIP_E2E=1, or 8788/CDP not running",
)


def test_p3_assertions():
    r = subprocess.run(
        [node, str(ROOT / "tests" / "fe_p3_assertions.js")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        env={**os.environ, "TTS_BASE": "http://127.0.0.1:8788", "TTS_CDP_PORT": "9334"},
    )
    assert r.returncode == 0, f"P3 assertions failed:\n{r.stdout}\n{r.stderr}"
    assert "ALL P3 CHECKS PASS" in r.stdout


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
