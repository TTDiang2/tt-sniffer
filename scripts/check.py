"""交付前校验关卡：语法编译 -> 包导入 -> 单元测试 -> (可选)联网冒烟。

用法：
  python scripts/check.py          # 全部离线校验（浏览器 E2E 默认跳过）
  python scripts/check.py --e2e    # 显式开启浏览器 E2E（需 8788 + CDP 9334）
  python scripts/check.py --live   # 追加一次真实数据源拉取
"""
from __future__ import annotations

import compileall
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAIL, OK = "\033[31m[FAIL]\033[0m", "\033[32m[OK]\033[0m"


def step(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{OK if ok else FAIL} {name}" + (f" —— {detail}" if detail else ""))
    return ok


def main() -> int:
    results = []

    results.append(step("语法编译 app/", compileall.compile_dir(
        ROOT / "app", quiet=2, force=True)))
    results.append(step("语法编译 tests/", compileall.compile_dir(
        ROOT / "tests", quiet=2, force=True)))

    r = subprocess.run([sys.executable, "-c", "import app.main; import app.server"],
                       cwd=ROOT, capture_output=True, text=True)
    results.append(step("包导入 app.main / app.server", r.returncode == 0,
                        r.stderr.strip()[:200]))

    env = {**os.environ}
    if "--e2e" in sys.argv:
        env.pop("TTS_SKIP_E2E", None)
        print("[INFO] 浏览器 E2E：显式开启（需 8788 + CDP 9334）")
    else:
        env["TTS_SKIP_E2E"] = "1"
        print("[INFO] 浏览器 E2E：默认跳过（--e2e 开启）")
    r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "--no-header"],
                       cwd=ROOT, capture_output=True, text=True, env=env)
    print(r.stdout.strip())
    results.append(step("单元测试", r.returncode == 0))

    if "--live" in sys.argv:
        r = subprocess.run(
            [sys.executable, "-c",
             "import asyncio;from app.collectors.news import fetch_cls;"
             "rows=asyncio.run(fetch_cls());"
             "print(f'财联社电报拉取 {len(rows)} 条')"
             "if rows else (_ for _ in ()).throw(RuntimeError('空数据'))"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        print((r.stdout or r.stderr).strip()[:300])
        results.append(step("联网冒烟 财联社", r.returncode == 0))

    passed = sum(results)
    print(f"\n{'='*40}\n校验结果：{passed}/{len(results)} 通过"
          + ("" if passed == len(results) else "  ←存在失败项，禁止交付"))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
