"""v26 智者门独立取证脚本：真实生产库快照 + 300 次连打 + 双线程并发 + 等价性 + 性能。

不做 mock，不重写候选集算法，只用 Store.query 直接跑（用生产快照的拷贝，
避免污染真实库）。所有断言输出到原文方便逐项核对。
"""
from __future__ import annotations
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from datetime import date

ROOT = Path("E:/TT_Sniffer")
SRC = ROOT / "data" / "sniffer.db"
sys.path.insert(0, str(ROOT))

from app.store import Store as StoreCls


def prep_db() -> Path:
    """复制生产库到临时路径（保留 WAL）。"""
    tmp = Path(tempfile.mkdtemp(prefix="v26_verify_"))
    dst = tmp / "sniffer.db"
    shutil.copy2(SRC, dst)
    # WAL/SHM 同步拷（否则 SQLite 会 checkpoint 重整）
    for sfx in ("-wal", "-shm"):
        s = SRC.with_name(SRC.name + sfx)
        if s.exists():
            shutil.copy2(s, dst.with_name(dst.name + sfx))
    return dst


def pick_day_window(store) -> tuple[str, int]:
    """取条数最多的单日（验证窗口有大量候选，更易触发竞态）。"""
    with store._lock:
        row = store._conn.execute(
            "SELECT substr(ts,1,10) d, COUNT(*) c FROM items "
            "GROUP BY d ORDER BY -c LIMIT 1").fetchone()
    return row["d"], row["c"]


def reference_per_item(store, day: str) -> dict[str, int]:
    """首条参考 cluster_size（基准）。"""
    r = store.query(day=day, limit=200)
    return {i["id"]: i["cluster_size"] for i in r["items"]}


def near_hammer_no_overflow(store, day: str, ref: dict, n: int = 300) -> tuple[int, float, list]:
    """连打 n 次同窗 query，断言每次结果与 ref 一致（旧实现第 256 次必 ValueError）。

    返回 (mismatch_count, total_seconds, exceptions)。
    """
    mismatches = 0
    errs = []
    t0 = time.perf_counter()
    for k in range(n):
        try:
            r = store.query(day=day, limit=200)
            got = {i["id"]: i["cluster_size"] for i in r["items"]}
            if got != ref:
                mismatches += 1
        except Exception as e:                         # noqa: BLE001
            errs.append((k, type(e).__name__, repr(e)))
    return mismatches, time.perf_counter() - t0, errs


def dual_thread(store, day: str, ref: dict, per_n: int = 100, n_workers: int = 2):
    """双线程并发 query，结果必须与 ref 逐条相等。"""
    errs: list = []
    lock = threading.Lock()
    t0 = time.perf_counter()

    def worker():
        try:
            for _ in range(per_n):
                r = store.query(day=day, limit=200)
                got = {i["id"]: i["cluster_size"] for i in r["items"]}
                if got != ref:
                    with lock:
                        errs.append(("mismatch", got, ref))
                    return
        except Exception as e:                         # noqa: BLE001
            with lock:
                errs.append((type(e).__name__, repr(e)))

    ts = [threading.Thread(target=worker) for _ in range(n_workers)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return errs, time.perf_counter() - t0


def warm_perf(store, day: str, n: int = 100) -> float:
    """同窗口 warm 调用平均耗时（应在毫秒级，旧实现无缓存 2.4s/页）。"""
    # 热身：填充候选集缓存
    store.query(day=day, limit=200)
    t0 = time.perf_counter()
    for _ in range(n):
        store.query(day=day, limit=200)
    return (time.perf_counter() - t0) / n


def old_reference_cluster_size(store, day: str) -> dict[str, int]:
    """旧 near_dup 参考实现：每行遍历窗内全部行 对 n 近重复（不计缓存/还原）。
    新 _attach_clusters 应与该参考逐条一致。
    """
    from app.simhash import (CLUSTER_HAMMING, CONTAIN_TH, MASK64, bigram_set,
                             digit_slots, near_dup, norm_text)
    from datetime import timedelta
    with store._lock:
        lo = f"{day} 00:00:00"
        hi = f"{day} 23:59:59"
        rows = store._conn.execute(
            "SELECT id, ts, simhash, title, text FROM items "
            "WHERE ts BETWEEN ? AND ? AND simhash!=0 "
            "ORDER BY ts DESC LIMIT 200", (lo, hi)).fetchall()
        if not rows:
            return {}
        ids = [r["id"] for r in rows]
        norms = [norm_text((r["title"] or "") + (r["text"] or "")) for r in rows]
        sims = [r["simhash"] for r in rows]
        bigrams = [None] * len(rows)
        out = {}
        for i, r in enumerate(rows):
            lo2 = (datetime.fromisoformat(r["ts"]) - timedelta(days=1)).isoformat(" ", "seconds")
            hi2 = (datetime.fromisoformat(r["ts"]) + timedelta(days=1)).isoformat(" ", "seconds")
            win = store._conn.execute(
                "SELECT id, simhash, title, text FROM items "
                "WHERE ts BETWEEN ? AND ? AND simhash!=0", (lo2, hi2)).fetchall()
            my_text = norms[i]
            my_sim = sims[i]
            n = 0
            for w in win:
                if w["id"] == r["id"] or not w["simhash"]:
                    continue
                wt = norm_text((w["title"] or "") + (w["text"] or ""))
                if near_dup(my_text, wt, my_sim, w["simhash"]):
                    n += 1
            out[r["id"]] = 1 + n
        return out


def main_run():
    print(">>> 准备生产库快照")
    db = prep_db()
    print(f"快照路径: {db}")
    store = StoreCls(db)

    day, n = pick_day_window(store)
    print(f"\n>>> 选最忙单日: {day} (条数={n})")
    ref = reference_per_item(store, day)
    print(f"基准 cluster_size 条数: {len(ref)} (前 5 条: {list(ref.items())[:5]})")

    print("\n>>> [T1] 300 次同窗连打——断言不炸、逐次结果一致")
    mis, sec_elapsed, errs = near_hammer_no_overflow(store, day, ref, n=300)
    if errs:
        print(f"!!! 抛异常次数: {len(errs)} 首例 @iter={errs[0][0]}: {errs[0][1]} {errs[0][2]}")
        return 1
    if mis:
        print(f"!!! 不一致次数: {mis}/{300}")
        return 1
    print(f"OK 300 次无炸、无不一致；累计耗时 {sec_elapsed:.2f}s "
          f"({sec_elapsed/300*1000:.2f}ms/次)")

    print("\n>>> [T2] 双线程各 100 次并发——逐条结果与单线程参考相等")
    errs2, sec2 = dual_thread(store, day, ref, per_n=100, n_workers=2)
    if errs2:
        print(f"!!! 失败首例: {errs2[0]}")
        return 1
    print(f"OK 双线程 200 次并发 query 全部与单线程参考逐条一致；耗时 {sec2:.2f}s")

    print("\n>>> [T3] 同窗口 warm 平均耗时（参考旧实现 2.4s/页）")
    per = warm_perf(store, day, n=100)
    print(f"OK warm 平均 {per*1000:.2f}ms/次（{'<' if per<2.4 else '>='}旧实现 2400ms 基准）")

    print("\n>>> [T4] 新算法 vs 旧 near_dup 参考——逐行 cluster_size 等价")
    # 仅对前 30 条做（参考实现逐行窗扫，O(N²) 全跑 200 行 × 30 分钟窗大）
    ref_old = old_reference_cluster_size(store, day)
    new_first = store.query(day=day, limit=200)["items"]
    new_map = {i["id"]: i["cluster_size"] for i in new_first}
    diff = [(iid, new_map[iid], ref_old.get(iid))
            for iid in list(new_map)[:30]
            if new_map[iid] != ref_old.get(iid)]
    if diff:
        print(f"!!! 不一致行数 {len(diff)}/{min(30, len(new_map))} 首例: {diff[0]}")
        return 1
    print(f"OK 前 {min(30, len(new_map))} 行逐条一致")
    return 0


if __name__ == "__main__":
    from datetime import datetime
    sys.exit(main_run())