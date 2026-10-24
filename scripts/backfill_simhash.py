"""simhash 显式回填脚本（正常情况 Store.__init__ 已自动回填，本脚本用于核对）。

用法：python scripts/backfill_simhash.py [db路径]
红线：只在自有/测试库上运行，绝不指向生产 data 目录。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.store import Store  # noqa: E402


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    s = Store(path) if path else Store()   # __init__ 内已执行幂等回填
    remaining = s.backfill_count()
    total = s.stats()["total"]
    print(f"库内 {total} 条；simhash 未回填行数：{remaining}")
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
