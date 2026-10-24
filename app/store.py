"""SQLite 存储（WAL，单写多读）。

V0.2 新增：
- content_hash：跨源内容指纹（同一条快讯多源转载可识别，为后续事件聚类/量化
  因子准备；去重显示策略后续版本再做）
- 按天查询：day 过滤、day_index（近N天日历索引）、day_overview（单日概览）

V0.2.1 新增：
- 滚动清理：30 天前条目删除（热度条只需 14 天，30 天留余量），
  启动时+每小时执行；每日一次 wal_checkpoint(TRUNCATE) 收缩 WAL。
  7x24 快讯写入量下不清库会让库文件与 WAL 无限增长。

S4 新增：
- tier 列（authoritative/street）：来源可信度正交维度，幂等迁移+落库+
  query 可选 tier 筛选（缺省不过滤=既有行为）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import data_dir
from .simhash import (GROUP_ALGO_VER, CLUSTER_HAMMING, CONTAIN_TH, MASK64,
                    bigram_set, digit_slots, near_dup, norm_text, simhash64)

# hamming 向量化（numpy 为 akshare 传递依赖，requirements-freeze 锁 1.25.0）
import numpy as np
_POPCNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

log = logging.getLogger("store")
RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, source TEXT, ts TEXT, title TEXT, text TEXT, url TEXT,
  cls TEXT, score INTEGER, keywords TEXT, targets TEXT, watch INTEGER,
  llm TEXT, translation TEXT, backfill INTEGER, created TEXT);
CREATE INDEX IF NOT EXISTS idx_items_ts ON items(ts);
CREATE INDEX IF NOT EXISTS idx_items_cls ON items(cls);
CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_MIGRATIONS = [
    ("content_hash", "ALTER TABLE items ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"),
    ("simhash", "ALTER TABLE items ADD COLUMN simhash INTEGER NOT NULL DEFAULT 0"),
    # 约定：''=未计算，'[]'=已计算无影响——回填以 WHERE impacts='' 收敛
    ("impacts", "ALTER TABLE items ADD COLUMN impacts TEXT NOT NULL DEFAULT ''"),
    # 影响引擎版本：规则语义升级后 < VER 的行自动重算（backfill 幂等）
    ("impact_ver", "ALTER TABLE items ADD COLUMN impact_ver INTEGER NOT NULL DEFAULT 0"),
    # 同类新闻分组键（智者规格：入库时增量并查集，单例=自身 id）
    ("group_key", "ALTER TABLE items ADD COLUMN group_key TEXT NOT NULL DEFAULT ''"),
    # 分组算法版本（判据语义变更时全库重放）
    ("group_ver", "ALTER TABLE items ADD COLUMN group_ver INTEGER NOT NULL DEFAULT 0"),
    # S4 来源可信度正交维度：authoritative（正规渠道）/ street（股吧/TG 小道源）
    ("tier", "ALTER TABLE items ADD COLUMN tier TEXT NOT NULL DEFAULT 'authoritative'"),
    # 外网新闻中文翻译：{"title_zh","summary_zh"}；原文列不改写
    ("translation", "ALTER TABLE items ADD COLUMN translation TEXT NOT NULL DEFAULT ''"),
]
# 分组索引必须在列存在后建（老库先 ALTER 再建索引）
_GROUP_INDEX = "CREATE INDEX IF NOT EXISTS idx_items_group ON items(group_key)"
# 组链跳距窗口：转载是星形传播（原稿→各拷贝），链式漂移靠窗口天然限界
GROUP_HOP_HOURS = 6
GROUP_BG_THRESHOLD = 2000
# 回填重放的分块大小：块间提交并释放 Store._lock，避免升级实例启动后
# API 查询被整批 64k 行重放长时间挡住（每块最坏几百毫秒级）。
GROUP_REPLAY_CHUNK = 500
def content_hash(title: str, text: str) -> str:
    ws = str.maketrans("", "", " \t\r\n")
    key = (title or "").translate(ws) + "|" + (text or "").translate(ws)
    return hashlib.md5(key.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path | None = None):
        path = path or data_dir() / "sniffer.db"
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        # _attach_clusters 候选集跨请求缓存（窗口整天量化，翻页/改筛选命中；
        # insert_many 触碰数据即失效）。_cc 为平行数组包（ids/hashes/digitsets/
        # norms/bigrams/数字倒排 postings/无数字下标 nod/visited 戳数组）。
        self._cc_key: tuple | None = None
        self._cc: dict | None = None
        self._cc_gen = 0
        self._last_ckpt: str = ""          # 上次 wal_checkpoint 的日期（YYYY-MM-DD）
        self._group_bf_pending: set[str] = set()
        self._group_bf_lock = threading.Lock()
        self._group_bf_running = False
        with self._lock:
            self._conn.executescript(_SCHEMA + "PRAGMA journal_mode=WAL;"
                                     + "PRAGMA synchronous=NORMAL;")
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(items)")}
            for column, sql in _MIGRATIONS:
                if column not in cols:
                    self._conn.execute(sql)
            self._backfill_hashes()
            self._backfill_simhash()
            self._backfill_impacts()
            self._conn.execute(_GROUP_INDEX)
            self._conn.commit()
        self._schedule_group_backfill()
        # JSON1 可用性探测（资产筛选走 json_each；不可用降级 LIKE）
        try:
            self._conn.execute("SELECT json('{}')")
            self._json1 = True
        except sqlite3.OperationalError:
            self._json1 = False
            log.warning("SQLite 无 JSON1 扩展，资产筛选降级 LIKE 兜底")
        self.cleanup()

    def _schedule_group_backfill(self) -> None:
        """小库同步收敛；大库交给独立连接的后台线程，避免启动期长时间占锁。"""
        try:
            with self._lock:
                pending = self._conn.execute(
                    "SELECT COUNT(*) c FROM items WHERE group_key='' OR group_ver<?",
                    (GROUP_ALGO_VER,)).fetchone()["c"]
        except sqlite3.OperationalError:
            return
        if not pending:
            return
        if pending > GROUP_BG_THRESHOLD:
            log.info("同类分组回填：%d 行转后台执行", pending)
            threading.Thread(target=self._group_backfill_bg,
                             name="group-backfill", daemon=True).start()
            return
        self._backfill_groups()

    def schedule_backfill_grouping(self, item_ids) -> None:
        """首轮历史回填入库后，后台把新增行纳入分组，避免同步逐条扫描。"""
        ids = [i for i in item_ids if i]
        if not ids:
            return
        with self._group_bf_lock:
            self._group_bf_pending.update(ids)
            if self._group_bf_running:
                return
            self._group_bf_running = True
        threading.Thread(target=self._group_backfill_worker,
                         name="group-backfill-batch", daemon=True).start()

    def _group_backfill_worker(self) -> None:
        merged = 0
        try:
            while True:
                with self._group_bf_lock:
                    ids = list(self._group_bf_pending)
                    self._group_bf_pending.clear()
                    if not ids:
                        self._group_bf_running = False
                        if merged:
                            log.info("历史回填增量分组完成：合并 %d 行入组", merged)
                        return
                cache: dict[str, tuple] = {}
                for i in range(0, len(ids), GROUP_REPLAY_CHUNK):
                    chunk = ids[i:i + GROUP_REPLAY_CHUNK]
                    with self._lock:
                        merged += self._replay_group_ids_locked(
                            self._conn, chunk, cache)
                        self._conn.commit()
                    # 块间让出 GIL，给等待 Store._lock 的 API 线程抢锁机会。
                    time.sleep(0)
        except Exception:
            log.exception("历史回填增量分组失败")
            with self._group_bf_lock:
                self._group_bf_running = False

    def _group_backfill_bg(self) -> None:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            self._backfill_groups(conn)
            log.info("同类分组后台回填完成")
        except Exception:
            log.exception("同类分组后台回填失败")
        finally:
            conn.close()

    def _backfill_hashes(self):
        """旧库存量的 content_hash 一次性回填（V0.3 聚类依赖）。"""
        try:
            rows = self._conn.execute(
                "SELECT id, title, text FROM items WHERE content_hash=''").fetchall()
        except sqlite3.OperationalError:
            return
        if rows:
            self._conn.executemany(
                "UPDATE items SET content_hash=? WHERE id=?",
                [(content_hash(r["title"], r["text"]), r["id"]) for r in rows])

    def backfill_count(self) -> int:
        """待回填 simhash 的行数（scripts/backfill_simhash.py 用）。"""
        return self._conn.execute(
            "SELECT COUNT(*) c FROM items WHERE simhash=0").fetchone()["c"]

    def _backfill_simhash(self):
        """旧库存量 simhash 幂等回填（用户重启即自动获得，无需手动跑脚本）。"""
        try:
            rows = self._conn.execute(
                "SELECT id, title, text FROM items WHERE simhash=0").fetchall()
        except sqlite3.OperationalError:
            return
        if rows:
            self._conn.executemany(
                "UPDATE items SET simhash=? WHERE id=?",
                [(simhash64((r["title"] or "") + (r["text"] or "")), r["id"])
                 for r in rows])

    def _backfill_impacts(self):
        """impacts 幂等回填：未计算的（''）或旧引擎版本的行 → 现行 VER 重算。

        规则表升级（impact.VER+1）后用户重启即自动获得全量重标注，
        存量历史数据始终与新规则口径一致。收敛后不再重扫。
        """
        from .impact import VER, detect
        try:
            rows = self._conn.execute(
                "SELECT id, title, text FROM items "
                "WHERE impacts='' OR impact_ver<?", (VER,)).fetchall()
        except sqlite3.OperationalError:
            return
        if rows:
            self._conn.executemany(
                "UPDATE items SET impacts=?, impact_ver=? WHERE id=?",
                [(json.dumps(detect((r["title"] or "") + (r["text"] or "")),
                             ensure_ascii=False), VER, r["id"])
                 for r in rows])
            if rows:
                log.info("impact 引擎 v%d 重算 %d 行存量标注", VER, len(rows))

    # ---------- 滚动清理 ----------
    def cleanup(self, retention_days: int = RETENTION_DAYS) -> int:
        """删除 retention_days 天前的条目并按日收缩 WAL。返回本次删除条数。

        cutoff 用 Python 本地时间计算（ts 列由 datetime.now() 写入本地时间串；
        SQLite datetime('now') 是 UTC，直接比较会差时区）。清理只认 ts 列，
        绝不触碰其他表；首次调用及跨日首次都会 wal_checkpoint(TRUNCATE)。
        """
        cutoff = (datetime.now() - timedelta(days=retention_days)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self._conn.execute("DELETE FROM items WHERE ts < ?", (cutoff,))
            self._conn.commit()
            deleted = cur.rowcount
            today = date.today().isoformat()
            if self._last_ckpt != today:
                self._last_ckpt = today
                # 每实例每日一次；当日不再重试（次日或新实例再试），后果仅是
                # WAL 膨胀到次日。busy 时 PRAGMA 返回行而非抛错，两路都留日志
                try:
                    row = self._conn.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if row and row[0]:
                        log.warning("wal_checkpoint busy（%s 页未检查点），WAL 保持到下次清理",
                                    row[1])
                except sqlite3.OperationalError as e:
                    log.warning("wal_checkpoint 失败（当日不再重试）: %s", e)
        return deleted

    def insert(self, item) -> bool:
        """插入单条，已存在返回 False。批量场景用 insert_many。"""
        return self.insert_many([item])[0]

    def update_text_if_longer(self, item_id: str, title: str, text: str) -> bool:
        """update-on-enrich（20261024）：同帖后轮补到全文时升级已入库的标题版。

        触发条件苛刻（新 text ≥ 旧 2 倍且 +40 字）防抖：同 id 已存在（被
        INSERT OR IGNORE 吸收）+ 显著更长才 UPDATE。content_hash/simhash 同步
        重算（后续新条目的近重复匹配立即受益），group_ver 置 0 标记该行随
        下次启动的分组回填重归组；score/ts 不动——重算会动统计口径。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT length(text) n FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                return False
            if not (len(text) >= max(row["n"] * 2, row["n"] + 40)):
                return False
            self._conn.execute(
                "UPDATE items SET text=?, content_hash=?, simhash=?, group_ver=0 "
                "WHERE id=?",
                (text, content_hash(title, text),
                 simhash64(norm_text((title or "") + (text or ""))), item_id))
            self._conn.commit()
            return True

    def insert_many(self, items) -> list[bool]:
        """批量插入（单事务一次 commit），返回每条是否新入库。"""
        from .impact import VER
        from .simhash import GROUP_ALGO_VER
        flags: list[bool] = []
        with self._lock:
            self._cc_gen += 1      # 候选集缓存失效（锁内防并发丢增量）
            for item in items:
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO items
                       (id, source, ts, title, text, url, cls, score, keywords,
                        targets, watch, llm, backfill, created, content_hash,
                        simhash, impacts, impact_ver, tier, group_key,
                        group_ver)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item.id, item.source, item.ts.strftime("%Y-%m-%d %H:%M:%S"),
                     item.title, item.text, item.url, item.cls, item.score,
                     json.dumps(item.keywords, ensure_ascii=False),
                     json.dumps(item.targets, ensure_ascii=False),
                     1 if item.watch else 0, None, 1 if item.backfill else 0,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     content_hash(item.title, item.text),
                     simhash64(norm_text((item.title or "") + (item.text or ""))),
                     json.dumps(getattr(item, "impacts", []) or [],
                                ensure_ascii=False),
                     VER,
                     getattr(item, "tier", "") or "authoritative",
                     item.id, GROUP_ALGO_VER),
                )
                if cur.rowcount > 0 and not item.backfill:
                    self._assign_group(item)
                flags.append(cur.rowcount > 0)
            self._conn.commit()
        return flags

    def set_llm(self, item_id: str, payload: dict):
        with self._lock:
            self._conn.execute("UPDATE items SET llm=? WHERE id=?",
                               (json.dumps(payload, ensure_ascii=False), item_id))
            self._conn.commit()

    def set_translation(self, item_id: str, payload: dict):
        with self._lock:
            self._conn.execute("UPDATE items SET translation=? WHERE id=?",
                               (json.dumps(payload, ensure_ascii=False), item_id))
            self._conn.commit()

    # ---------- 同类新闻分组（智者规格：入库时增量并查集） ----------
    def _assign_group(self, item) -> None:
        """为刚插入的行定组（调用方持有锁，事务内）。

        ts±GROUP_HOP_HOURS 窗内找近重复邻居（hamming≤12 且数字槽不冲突，
        与 _attach_clusters 同判据）；无邻居自成单例组；有邻居并入最早成员
        所在组；邻居分属多组则把较晚组整体重指到较早组（传递闭包，跳距
        窗口天然限界）。批内调用方已按 ts 升序插入，故本批更早成员可见。
        """
        ts = item.ts.strftime("%Y-%m-%d %H:%M:%S")
        lo = (item.ts - timedelta(hours=GROUP_HOP_HOURS)
              ).strftime("%Y-%m-%d %H:%M:%S")
        hi = (item.ts + timedelta(hours=GROUP_HOP_HOURS)
              ).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._conn.execute(
            "SELECT id, ts, simhash, title, text, group_key FROM items "
            "WHERE ts BETWEEN ? AND ? AND simhash!=0 AND id!=?",
            (lo, hi, item.id)).fetchall()
        my_text = norm_text((item.title or "") + (item.text or ""))
        my = simhash64(my_text)
        neighbors = [r for r in rows if r["simhash"] and near_dup(
            my_text, norm_text((r["title"] or "") + (r["text"] or "")),
            my, r["simhash"])]
        if not neighbors:
            self._conn.execute("UPDATE items SET group_key=? WHERE id=?",
                               (item.id, item.id))
            return
        earliest = min(neighbors, key=lambda r: (r["ts"], r["id"]))
        target = earliest["group_key"] or earliest["id"]
        other_keys = {(r["group_key"] or r["id"]) for r in neighbors} - {target}
        for k in other_keys:
            self._conn.execute(
                "UPDATE items SET group_key=? WHERE group_key=?", (target, k))
        self._conn.execute("UPDATE items SET group_key=? WHERE id=?",
                           (target, item.id))

    def _backfill_groups(self, conn=None) -> None:
        if conn is None:
            with self._lock:
                self._backfill_groups_locked(self._conn)
                self._conn.commit()
            return
        self._backfill_groups_locked(conn)
        conn.commit()

    def _backfill_groups_locked(self, db) -> None:
        """group_key 幂等回填：'' 的行先自成单例，再按 ts ASC 重放合并逻辑。

        分组算法升级（GROUP_ALGO_VER+1）时全库重放：归一化指纹全量重算 +
        group_key 重置重跑（一次性秒级）；收敛后只扫 ''，不再重算。
        与 _backfill_simhash 同待遇（用户重启即自动获得）。
        """
        try:
            stale = db.execute(
                "SELECT COUNT(*) c FROM items WHERE group_ver<?",
                (GROUP_ALGO_VER,)).fetchone()["c"]
        except sqlite3.OperationalError:
            return
        if stale:
            log.info("分组算法 v%d：全库 %d 行重算归一化指纹并重放分组",
                     GROUP_ALGO_VER, stale)
            db.executemany(
                "UPDATE items SET simhash=?, group_key='', group_ver=? WHERE id=?",
                [(simhash64(norm_text((r["title"] or "") + (r["text"] or ""))),
                  GROUP_ALGO_VER, r["id"])
                 for r in db.execute(
                     "SELECT id, title, text FROM items").fetchall()])
        try:
            rows = db.execute(
                "SELECT id FROM items WHERE group_key='' ORDER BY ts ASC, id").fetchall()
        except sqlite3.OperationalError:
            return
        if not rows:
            return
        fresh = [r["id"] for r in rows]
        log.info("同类分组回填：%d 行重放合并", len(fresh))
        merged = self._replay_group_ids_locked(db, fresh, {})
        if merged:
            log.info("同类分组回填完成：合并 %d 行入组", merged)

    def _replay_group_ids_locked(self, db, ids, cache: dict) -> int:
        """指定行先置单例再重放合并；调用方持有锁，块内可重复调用。

        只处理 group_key='' 或自指的行，已并入他组的行不再拆组。内部仍按
        GROUP_REPLAY_CHUNK 分块拼 SQL，避免 SQLite 变量数上限。
        """
        ids = [i for i in ids if i]
        if not ids:
            return 0
        db.executemany(
            "UPDATE items SET group_key=? WHERE id=? "
            "AND (group_key='' OR group_key=id)", [(i, i) for i in ids])
        merged = 0
        for i in range(0, len(ids), GROUP_REPLAY_CHUNK):
            chunk = ids[i:i + GROUP_REPLAY_CHUNK]
            marks = ",".join("?" * len(chunk))
            todo = db.execute(
                f"SELECT id, ts, title, text, simhash, group_key FROM items "
                f"WHERE id IN ({marks}) AND group_key=id ORDER BY ts ASC, id",
                chunk).fetchall()
            merged += self._replay_group_rows_locked(db, todo, cache)
        return merged

    def _replay_group_rows_locked(self, db, todo, cache: dict) -> int:
        """按 ts 重放近重合并；调用方持有锁。cache 跨块复用 norm/bigram。"""
        def _keyed(row):
            t = cache.get(row["id"])
            if t is None:
                nt = norm_text((row["title"] or "") + (row["text"] or ""))
                t = (nt, bigram_set(nt))
                cache[row["id"]] = t
            return t
        merged = 0
        for r in todo:
            lo = (datetime.fromisoformat(r["ts"]) - timedelta(hours=GROUP_HOP_HOURS)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            hi = (datetime.fromisoformat(r["ts"]) + timedelta(hours=GROUP_HOP_HOURS)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            win = db.execute(
                "SELECT id, ts, simhash, title, text, group_key FROM items "
                "WHERE ts BETWEEN ? AND ? AND simhash!=0 AND id!=?",
                (lo, hi, r["id"])).fetchall()
            my_text, my_bigrams = _keyed(r)
            neighbors = []
            for w in win:
                if not w["simhash"]:
                    continue
                wt, wb = _keyed(w)
                if near_dup(my_text, wt, r["simhash"], w["simhash"],
                            ba=my_bigrams, bb=wb):
                    neighbors.append(w)
            if not neighbors:
                continue
            self_row = {"id": r["id"], "ts": r["ts"],
                        "group_key": r["group_key"]}
            earliest = min(neighbors + [self_row],
                           key=lambda w: (w["ts"], w["id"]))
            target = earliest["group_key"] or earliest["id"]
            if target == r["id"]:
                continue
            others = {(w["group_key"] or w["id"]) for w in neighbors} - {target}
            for k in others:
                db.execute(
                    "UPDATE items SET group_key=? WHERE group_key=?", (target, k))
            db.execute("UPDATE items SET group_key=? WHERE id=?",
                       (target, r["id"]))
            merged += 1
        return merged

    def group_of(self, item_id: str) -> dict | None:
        """单行的组信息（pipeline SSE 广播用）：组键/组员数/直接近重数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, ts, title, text, simhash, group_key FROM items "
                "WHERE id=?", (item_id,)).fetchone()
            if not row:
                return None
            size = self._conn.execute(
                "SELECT COUNT(*) c FROM items WHERE group_key=?",
                (row["group_key"],)).fetchone()["c"]
            lo = (datetime.fromisoformat(row["ts"]) - timedelta(days=1)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            hi = (datetime.fromisoformat(row["ts"]) + timedelta(days=1)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            win = self._conn.execute(
                "SELECT id, simhash, title, text FROM items "
                "WHERE ts BETWEEN ? AND ? AND simhash!=0", (lo, hi)).fetchall()
            my_text = norm_text((row["title"] or "") + (row["text"] or ""))
            cluster = 1 + sum(
                1 for w in win
                if w["id"] != item_id and w["simhash"]
                and near_dup(my_text, norm_text((w["title"] or "")
                                                + (w["text"] or "")),
                             row["simhash"], w["simhash"]))
            return {"group_key": row["group_key"] or row["id"],
                    "group_size": size, "cluster_size": cluster}

    # ---------- 查询 ----------
    def day_items(self, day: str, *, impacts: bool = False) -> list[sqlite3.Row]:
        """单日条目只读快照；封装时间窗/锁/连接，避免端点直连私有成员。"""
        lo, hi = self._day_range(day)
        cols = "id, ts, source, cls, score, title, text"
        if impacts:
            cols += ", impacts"
        with self._lock:
            return self._conn.execute(
                f"SELECT {cols} FROM items WHERE ts BETWEEN ? AND ?",
                (lo, hi)).fetchall()

    def untranslated_items(self, *, sources=None, limit: int = 100,
                           since_days: int = 30) -> list[sqlite3.Row]:
        """未翻译的外网条目（新→旧），供空闲补译；不扫全库。"""
        cutoff = (datetime.now() - timedelta(days=max(1, since_days))
                  ).strftime("%Y-%m-%d %H:%M:%S")
        where = ["(translation='' OR translation IS NULL)", "ts>=?"]
        args: list = [cutoff]
        if sources:
            marks = ",".join("?" * len(sources))
            where.append(f"source IN ({marks})")
            args.extend(sources)
        args.append(max(1, limit))
        with self._lock:
            return self._conn.execute(
                "SELECT id, source, ts, title, text, url FROM items "
                f"WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?",
                args).fetchall()

    @staticmethod
    def _day_range(day: str) -> tuple[str, str]:
        d = date.fromisoformat(day)
        return (f"{d} 00:00:00", f"{d} 23:59:59")

    @staticmethod
    def _escape_like(q: str) -> str:
        """LIKE 通配符转义（配合 ESCAPE '\'）：%/_/\ 字面匹配。"""
        return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _where(self, *, cls: str | None, q: str | None, watch_only: bool,
               day: str | None, min_score: int | None, max_score: int | None,
               sources: list[str] | None, asset: str | None = None,
               direction: list[str] | None = None,
               day_from: str | None = None, day_to: str | None = None,
               tier: str | None = None) -> tuple[str, list]:
        """组合筛选的公共 WHERE 构造（分页查询与计数共用，口径恒一致）。
        day_from/day_to：闭区间日期范围（AI 近 N 日资料包用），与 day 互斥。"""
        where, args = ["1=1"], []
        if day:
            lo, hi = self._day_range(day)
            where.append("ts BETWEEN ? AND ?")
            args += [lo, hi]
        elif day_from and day_to:
            where.append("ts BETWEEN ? AND ?")
            args += [f"{day_from} 00:00:00", f"{day_to} 23:59:59"]
        if cls and cls != "all":
            where.append("cls=?")
            args.append(cls)
        if tier:
            # S4：tier 筛选（'street'=只看小道源）；缺省 None 不过滤=既有行为
            where.append("tier=?")
            args.append(tier)
        if watch_only:
            where.append("watch=1")
        if q:
            # 多关键词（20261003 S1）：中英文逗号分隔，词间 AND（缩小范围）。
            # AND/OR 裁决：AND 与 sources/cls 等既有筛选的"收窄"语义同向，符合
            # 多条件检索直觉；OR 会放大噪声面，备选未启用（记录于晨报）。
            # 单词路径与旧实现逐字节兼容（同一对 LIKE 条件）。空词过滤（门4 D3 更正）：
            # AND 语义下空词的 LIKE '%%' 子句恒真、行为本就无害——过滤是防御性冗余，
            # 为的是 SQL 干净可断言；"%% 恒真"只在 OR 拼接下才会放大成全表，此处不适用。
            for word in (x.strip() for x in re.split(r"[,，]", q)):
                if not word:
                    continue
                where.append("(title LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')")
                pat = f"%{self._escape_like(word)}%"
                args += [pat, pat]
        if min_score is not None:
            where.append("score>=?")
            args.append(min_score)
        if max_score is not None:
            where.append("score<=?")
            args.append(max_score)
        if sources:
            where.append("source IN (%s)" % ",".join("?" * len(sources)))
            args += list(sources)
        if asset:
            if self._json1:
                sub = ("EXISTS(SELECT 1 FROM json_each(items.impacts) je "
                       "WHERE json_extract(je.value,'$.a')=?")
                args.append(asset)
                if direction:
                    sub += (" AND json_extract(je.value,'$.d') IN (%s)"
                            % ",".join("?" * len(direction)))
                    args += list(direction)
                where.append(sub + ")")
            else:  # 无 JSON1 的 LIKE 兜底（键带闭合引号故精确）
                where.append("impacts LIKE ?")
                args.append(f'%\"a\":\"{asset}\"%')
                if direction:
                    where.append(" AND ".join(["impacts LIKE ?"] * len(direction)))
                    args += [f'%\"d\":\"{x}\"%' for x in direction]
        elif direction:
            # 独立方向筛选（2026-10-16 用户需求：只看带 利多/利空 标记的新闻，
            # 不必先选资产）。语义= impacts 数组里任一影响方向命中。
            if self._json1:
                where.append(
                    "EXISTS(SELECT 1 FROM json_each(items.impacts) je "
                    "WHERE json_extract(je.value,'$.d') IN (%s))"
                    % ",".join("?" * len(direction)))
                args += list(direction)
            else:
                where.append("(" + " OR ".join(["impacts LIKE ?"] * len(direction)) + ")")
                args += [f'%\"d\":\"{x}\"%' for x in direction]
        return " AND ".join(where), args

    # 内部实现列不下发 API（group=0 响应保持历史形状）
    _INTERNAL_COLS = ("group_key", "group_ver")

    @staticmethod
    def _row(r) -> dict:
        d = {k: r[k] for k in r.keys() if k not in Store._INTERNAL_COLS}
        d["keywords"] = json.loads(d["keywords"] or "[]")
        d["targets"] = json.loads(d["targets"] or "[]")
        d["watch"] = bool(d["watch"])
        d["backfill"] = bool(d["backfill"])
        d["llm"] = json.loads(d["llm"]) if d["llm"] else None
        d["translation"] = (json.loads(d["translation"])
                            if "translation" in d and d["translation"] else None)
        d["impacts"] = json.loads(d["impacts"] or "[]") if "impacts" in d.keys() else []
        return d

    def _attach_clusters(self, items: list[dict]) -> None:
        """页内条目附加 cluster_size（含自身的近重复总数）。

        候选取条目 ts ±1 天窗（整天量化）内全部带指纹行；判据 = near_dup
        的两分支内联：hamming≤阈值且数字槽无冲突，或 containment≥0.8 且
        数字守卫。有意不用 4×16bit 鸽笼带预筛：那只对 hamming≤3 有召回
        保证，本库阈值 12 时实测会漏掉大多数真配对。

        2026-10-16 性能修复（生产全站冻结事故根因，旧实现单页 40s+ 且
        async 端点里同步执行=整个事件循环冻结）：
        1) 候选集跨请求缓存（窗口整天量化，翻页/改筛选命中；插入即失效）
           ——比逐页条目 ts 派生的旧窗多出最多 ±23h，换来同日查询确定性；
        2) 数字倒排 + mark-sweep：数字槽冲突（双侧有数字且无共享）的对
           必非同类（两判据守卫皆死）——年份/日期等公共数字使同日新闻
           大量「共享数字」，posting 并集仍大，故用 visited 戳数组遍历
           （零集合分配）；
        3) containment 分支仅在 hamming≤28 时评估：V2 标定的救援对
           hamming 落 14-26（模块注释实证），>28 的 containment 对在
           本库两日 ~1.1 万候选实测零命中（验证脚本 2026-10-16），
           取 26+2 余量拒付无意义全集交。
        """
        sims = [it for it in items if it.get("simhash")]
        if not sims:
            return
        ts_sorted = sorted(it["ts"] for it in sims)
        d0 = (datetime.fromisoformat(ts_sorted[0]) - timedelta(days=1)).date()
        d1 = (datetime.fromisoformat(ts_sorted[-1]) + timedelta(days=1)).date()
        lo = f"{d0} 00:00:00"
        hi = f"{d1} 23:59:59"
        with self._lock:
            key = (lo, hi, self._cc_gen)
            cc = self._cc
            if self._cc_key != key or cc is None:
                rows = self._conn.execute(
                    "SELECT id, simhash, title, text FROM items "
                    "WHERE ts BETWEEN ? AND ? AND simhash!=0", (lo, hi)).fetchall()
                # 平行数组缓存：ids/哈希/数字槽/规范文本/bigram(惰性)
                # + 数字倒排（数字→下标列表）+ 无数字候选下标表
                ids = [r["id"] for r in rows]
                hashes = [r["simhash"] for r in rows]
                ntexts = [norm_text((r["title"] or "") + (r["text"] or ""))
                          for r in rows]
                digitsets = [frozenset(digit_slots(t)) for t in ntexts]
                norms = ntexts
                postings: dict[str, list[int]] = {}
                nod = []
                for i, dg in enumerate(digitsets):
                    if dg:
                        for d in dg:
                            postings.setdefault(d, []).append(i)
                    else:
                        nod.append(i)
                # numpy 位型视图：int64（SQLite 有符号存储）→ view uint64，
                # 位型不变且不触发负数→无符号的弃用转换
                np_hashes = np.array(hashes, dtype=np.int64).view(np.uint64)
                cc = {"ids": ids, "hashes": hashes, "digitsets": digitsets,
                      "norms": norms, "bigrams": [None] * len(ids),
                      "postings": postings, "nod": nod,
                      "np_hashes": np_hashes,
                      "pos": {cid: i for i, cid in enumerate(ids)}}
                self._cc_key, self._cc = key, cc
        ids, digitsets, norms = cc["ids"], cc["digitsets"], cc["norms"]
        bigrams, postings, nod = cc["bigrams"], cc["postings"], cc["nod"]
        # visited 每条目本地新建（10KB 级 bytearray 分配是微秒级）——智者门
        # v26：共享戳数组有 256 溢出炸弹（bytearray 上限）与锁外 read-modify-
        # write 竞态两处确定性缺陷，本地副本一并根治；且每条目须重置（上条
        # 目标记残留会污染守卫），my_digits 空的条目免标记全量
        visited = None
        pos, np_hashes = cc["pos"], cc["np_hashes"]
        for it in items:
            s = it.get("simhash") or 0
            if not s:
                it["cluster_size"] = 1
                continue
            my_norm = norm_text((it.get("title") or "") + (it.get("text") or ""))
            my_digits = frozenset(digit_slots(my_norm))
            my_bigrams = None
            me = pos.get(it["id"], -1)
            # mark：无数字候选 + 共享≥1数字槽的候选（=数字槽无冲突的全部
            # 候选；my_digits 空时守卫恒过→免标记全量）
            if my_digits:
                visited = bytearray(len(ids))
                for i in nod:
                    visited[i] = 1
                for dg in my_digits:
                    for i in postings.get(dg, ()):
                        visited[i] = 1
            # hamming 全体向量化（uint64 异或 + 字节 popcount 查表）
            diff = np.bitwise_xor(np_hashes, np.uint64(s & MASK64))
            popc = _POPCNT[diff.view(np.uint8).reshape(-1, 8)].sum(axis=1)
            near1 = np.nonzero(popc <= CLUSTER_HAMMING)[0].tolist()
            # 分支1：hamming 达标且数字槽无冲突（my_digits 空时守卫恒过）
            n = sum(1 for i in near1
                    if i != me and (not my_digits
                                    or visited[i]))
            # 分支2：containment 救援（12<hamming≤28 的数字无冲突对；
            # my_digits 空或对侧无数字时 R4 守卫必拒，整段跳过）
            if my_digits:
                rescue = np.nonzero((popc > CLUSTER_HAMMING) & (popc <= 28))[0]
                for i in rescue.tolist():
                    if i == me or not visited[i] or not digitsets[i]:
                        continue
                    if bigrams[i] is None:
                        bigrams[i] = bigram_set(norms[i])
                    if my_bigrams is None:
                        my_bigrams = bigram_set(my_norm)
                    A, B = my_bigrams, bigrams[i]
                    inter = len(A & B)
                    if inter and inter / min(len(A), len(B)) >= CONTAIN_TH:
                        cd = digitsets[i]
                        small, big = ((my_digits, cd) if len(my_digits) <= len(cd)
                                      else (cd, my_digits))
                        if small <= big:
                            n += 1
            it["cluster_size"] = 1 + n

    def query(self, cls: str | None = None, q: str | None = None,
              watch_only: bool = False, limit: int = 200,
              day: str | None = None, offset: int = 0, sort: str = "time",
              min_score: int | None = None, max_score: int | None = None,
              sources: list[str] | None = None, asset: str | None = None,
              direction: list[str] | None = None, group: bool = False,
              day_from: str | None = None, day_to: str | None = None,
              tier: str | None = None) -> dict:
        """分页查询：day=None 取全部历史；sort='score' 按分数排序；
        asset/direction 按资产影响筛选（direction 为 bull/bear/rel 列表，
        无 asset 时独立生效=只看带该方向标记的条目）；
        tier='street' 只看小道源（S4），None 不过滤（既有行为不变）。

        group=True 折叠同类：按组分页（一页 limit 组），items 为组代表
        （当日成员 max(score) 胜出，平分取更早 ts）附 group_* 字段；
        WHERE 资格按"任一成员命中"判定。返回 {items, total(组数),
        total_raw(条数=group=False 的 total)}。group=False 路径完全不变。
        """
        where_sql, args = self._where(cls=cls, q=q, watch_only=watch_only,
                                      day=day, min_score=min_score,
                                      max_score=max_score, sources=sources,
                                      asset=asset, direction=direction,
                                      day_from=day_from, day_to=day_to,
                                      tier=tier)
        if not group:
            order = ("score DESC, ts DESC" if sort == "score" else "ts DESC")
            with self._lock:
                total = self._conn.execute(
                    f"SELECT COUNT(*) c FROM items WHERE {where_sql}", args
                ).fetchone()["c"]
                rows = self._conn.execute(
                    f"SELECT * FROM items WHERE {where_sql} "
                    f"ORDER BY {order} LIMIT ? OFFSET ?",
                    args + [limit, offset]).fetchall()
            items = [self._row(r) for r in rows]
            self._attach_clusters(items)
            return {"items": items, "total": total}

        return self._query_grouped(where_sql, args, day=day, sort=sort,
                                   limit=limit, offset=offset)

    def _query_grouped(self, where_sql: str, args: list, day: str | None,
                       sort: str, limit: int, offset: int) -> dict:
        """折叠同类查询：资格组 → 当日成员 → rep 选择。

        组序 SQL 下推（R1 修复：无序 DISTINCT 上切页曾致跨页时序错乱——
        实测 page1 末尾 08:06、page2 开头 08:16）——time=MAX(ts) DESC，
        score=MAX(score) DESC, MAX(ts) DESC；页内排序键用同一 MAX 值，
        多页拼接全局有序。"""
        if sort == "score":
            order_sql = "MAX(score) DESC, MAX(ts) DESC"
        else:
            order_sql = "MAX(ts) DESC"
        with self._lock:
            total_raw = self._conn.execute(
                f"SELECT COUNT(*) c FROM items WHERE {where_sql}", args
            ).fetchone()["c"]
            total = self._conn.execute(
                f"SELECT COUNT(DISTINCT group_key) c FROM items WHERE {where_sql}",
                args).fetchone()["c"]
            key_rows = self._conn.execute(
                f"SELECT group_key, MAX(score) ms, MAX(ts) mts FROM items "
                f"WHERE {where_sql} GROUP BY group_key ORDER BY {order_sql} "
                f"LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
            key_meta = {r["group_key"]: (r["ms"], r["mts"]) for r in key_rows}
            page_keys = [r["group_key"] for r in key_rows]
            if not page_keys:
                return {"items": [], "total": total, "total_raw": total_raw}
            day_sql, day_args = "", []
            if day:
                lo, hi = self._day_range(day)
                day_sql = " AND ts BETWEEN ? AND ?"
                day_args = [lo, hi]
            members: dict[str, list] = {}
            for i in range(0, len(page_keys), 400):
                chunk = page_keys[i:i + 400]
                marks = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT * FROM items WHERE group_key IN ({marks})"
                    f"{day_sql}", chunk + day_args).fetchall()
                for r in rows:
                    members.setdefault(r["group_key"], []).append(r)
        items = []
        for k in page_keys:
            rows = members.get(k, [])
            if not rows:
                continue   # 组内当日无成员（day 过滤后资格组只剩跨天成员的场景）
            # rep：max(score) → 更早 ts → 更长 title+text（确定性）
            rep = max(rows, key=lambda r: (r["score"],
                                           tuple(-ord(c) for c in r["ts"]),
                                           len((r["title"] or "")
                                               + (r["text"] or ""))))
            it = self._row(rep)
            others = [r for r in rows if r["id"] != rep["id"]]
            it["group_key"] = k
            it["group_size"] = len(rows)
            it["group_sources"] = sorted({r["source"] for r in rows})
            it["group_members"] = [
                {"id": r["id"], "ts": r["ts"], "source": r["source"],
                 "score": r["score"], "cls": r["cls"],
                 "disp": (r["title"] or (r["text"] or ""))[:64]}
                for r in sorted(others, key=lambda r: r["ts"])][:50]
            it["group_more"] = max(0, len(others) - 50)
            items.append(it)
        # 组序与 SQL 同口径（MAX 值），保证多页拼接全局有序
        if sort == "score":
            items.sort(key=lambda it: key_meta[it["group_key"]], reverse=True)
        else:
            items.sort(key=lambda it: key_meta[it["group_key"]][1], reverse=True)
        # group_prev：组内早于当日视图的成员数（跨天尾巴）
        if day:
            lo, _ = self._day_range(day)
            with self._lock:
                marks = ",".join("?" * len(page_keys))
                prev_rows = self._conn.execute(
                    f"SELECT group_key, COUNT(*) c FROM items "
                    f"WHERE group_key IN ({marks}) AND ts<? GROUP BY group_key",
                    page_keys + [lo]).fetchall()
            prev = {r["group_key"]: r["c"] for r in prev_rows}
            for it in items:
                it["group_prev"] = prev.get(it["group_key"], 0)
        else:
            for it in items:
                it["group_prev"] = 0
        return {"items": items, "total": total, "total_raw": total_raw}

    def sentiment_history(self, days: int = 14) -> dict:
        """近 N 日逐日资产情绪（复用 sentiment.compute，不写第二套票法）。"""
        from . import sentiment
        from .impact import ASSETS
        start = date.today() - timedelta(days=days - 1)
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(ts,1,10) d, impacts FROM items WHERE ts>=? "
                "ORDER BY ts", (f"{start} 00:00:00",)).fetchall()
        by_day: dict[str, list] = {}
        for r in rows:
            by_day.setdefault(r["d"], []).append(
                {"impacts": json.loads(r["impacts"] or "[]")})
        day_list = [(start + timedelta(days=i)).isoformat()
                    for i in range(days)]
        # 先逐日算出各资产分，再对齐成定长序列（无票日=None）
        scores: dict[str, list] = {a: [None] * days for a in ASSETS}
        ns: dict[str, list] = {a: [None] * days for a in ASSETS}
        meta = {}
        for i, d in enumerate(day_list):
            for r in sentiment.compute(by_day.get(d, [])):
                scores[r["a"]][i] = r["score"]
                ns[r["a"]][i] = [r["bull_n"], r["bear_n"]]
                meta[r["a"]] = {"name": r["name"], "icon": r["icon"]}
        return {"days": day_list,
                "rows": [{"a": a, "name": meta.get(a, {}).get(
                              "name", ASSETS[a]["name"]),
                          "icon": meta.get(a, {}).get(
                              "icon", ASSETS[a]["icon"]),
                          "scores": scores[a], "ns": ns[a]} for a in ASSETS]}

    def day_index(self, days: int = 14) -> list[dict]:
        """近 N 天日历索引（含今日），每天总条数与各类计数。无数据的日期补零。"""
        today = date.today()
        start = today - timedelta(days=days - 1)
        with self._lock:
            rows = self._conn.execute(
                """SELECT substr(ts,1,10) d, COUNT(*) total,
                          SUM(cls='rumor') rumor, SUM(cls='debunk') debunk,
                          SUM(cls='official') official
                   FROM items WHERE ts>=? GROUP BY d""",
                (f"{start} 00:00:00",)).fetchall()
        by_day = {r["d"]: dict(r) for r in rows}
        out = []
        for i in range(days):
            d = start + timedelta(days=i)
            r = by_day.get(str(d), {})
            out.append({"day": str(d), "total": r.get("total", 0) or 0,
                        "rumor": r.get("rumor", 0) or 0,
                        "debunk": r.get("debunk", 0) or 0,
                        "official": r.get("official", 0) or 0})
        return out

    def day_overview(self, day: str, top: int = 15) -> dict:
        """单日概览：分类计数 + 当日热词。"""
        lo, hi = self._day_range(day)
        with self._lock:
            counts = dict(self._conn.execute(
                "SELECT cls, COUNT(*) c FROM items WHERE ts BETWEEN ? AND ? "
                "GROUP BY cls", (lo, hi)).fetchall())
            kw_rows = self._conn.execute(
                "SELECT keywords FROM items WHERE ts BETWEEN ? AND ? AND score>0",
                (lo, hi)).fetchall()
        counter: dict[str, int] = {}
        for r in kw_rows:
            for kw in json.loads(r["keywords"] or "[]"):
                counter[kw] = counter.get(kw, 0) + 1
        return {"day": day,
                "counts": counts,
                "keywords": sorted(counter.items(), key=lambda x: -x[1])[:top]}

    def hot_keywords(self, hours: int = 24, top: int = 20) -> list[tuple[str, int]]:
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            rows = self._conn.execute(
                "SELECT keywords FROM items WHERE ts>? AND score>0", (since,)).fetchall()
        counter: dict[str, int] = {}
        for r in rows:
            for kw in json.loads(r["keywords"] or "[]"):
                counter[kw] = counter.get(kw, 0) + 1
        return sorted(counter.items(), key=lambda x: -x[1])[:top]

    def day_sentiment(self, day: str) -> list[dict]:
        """当日资产情绪（sentiment.compute 聚合，见模块 docstring）。"""
        from . import sentiment
        lo, hi = self._day_range(day)
        with self._lock:
            rows = self._conn.execute(
                "SELECT impacts FROM items WHERE ts BETWEEN ? AND ?",
                (lo, hi)).fetchall()
        items = [{"impacts": json.loads(r["impacts"] or "[]")} for r in rows]
        return sentiment.compute(items)

    def day_summary(self, day: str) -> dict:
        """单日统计聚合：24 小时分时计数、来源占比、分类占比（统计面板用）。"""
        lo, hi = self._day_range(day)
        with self._lock:
            hourly = self._conn.execute(
                """SELECT substr(ts,12,2) h, COUNT(*) total,
                          SUM(cls='rumor') rumor, SUM(cls='debunk') debunk,
                          SUM(cls='official') official
                   FROM items WHERE ts BETWEEN ? AND ? GROUP BY h""",
                (lo, hi)).fetchall()
            by_source = self._conn.execute(
                "SELECT source, COUNT(*) c FROM items WHERE ts BETWEEN ? AND ? "
                "GROUP BY source ORDER BY -c", (lo, hi)).fetchall()
            by_cls = self._conn.execute(
                "SELECT cls, COUNT(*) c FROM items WHERE ts BETWEEN ? AND ? "
                "GROUP BY cls", (lo, hi)).fetchall()
        hours = [{"h": int(r["h"]), "total": r["total"],
                  "rumor": r["rumor"] or 0, "debunk": r["debunk"] or 0,
                  "official": r["official"] or 0} for r in hourly]
        return {"day": day, "hourly": hours,
                "by_source": {r["source"]: r["c"] for r in by_source},
                "by_cls": {r["cls"]: r["c"] for r in by_cls}}

    def day_collisions(self, day: str, top: int = 5) -> list[dict]:
        """辟谣对撞：当日辟谣与其同题材传闻配对（速览块用）。无配对的辟谣不返回。

        配对条件（宁缺勿滥——乱配比漏配害人，实测"养老App辟谣"与"迪拜航空"
        曾因共享来源词"新华社"被配成一组）：
        - targets 交集 ≥1（标的级主题信号，强）；或
        - keywords 交集 ≥2（单一来源词如"新华社"不足以定主题）。
        disp：title 常为空（财联社/新浪正文在 text），前端显示用
        title 优先、text 截断兜底。
        """
        lo, hi = self._day_range(day)
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM items
                   WHERE ts BETWEEN ? AND ? AND cls IN ('rumor','debunk')
                   ORDER BY ts""", (lo, hi)).fetchall()
        rumors, debunks = [], []
        for r in rows:
            d = self._row(r)
            d["_kw"] = set(d["keywords"])
            d["_tg"] = set(d["targets"])
            (rumors if d["cls"] == "rumor" else debunks).append(d)

        def full(x):
            # 点击即开 Reader：返回完整条目（此前只回 id/title/score，正文为空）
            out = {k: v for k, v in x.items() if not k.startswith("_")}
            out["disp"] = (x["title"] or (x["text"] or ""))[:64]
            return out
        pairs = []
        for deb in debunks:
            linked = [ru for ru in rumors if ru is not deb and (
                ru["_tg"] & deb["_tg"]
                or len(ru["_kw"] & deb["_kw"]) >= 2)]
            if linked:
                pairs.append({"debunk": full(deb),
                              "rumors": [full(x) for x in linked[-3:]]})
        return pairs[:top]

    def items_by_ids(self, ids: list[str]) -> list[dict]:
        """按 id 直取（S5 AI 资料包 ids scope）；空列表/未知 id 安全。"""
        if not ids:
            return []
        with self._lock:
            marks = ",".join("?" * len(ids))
            rows = self._conn.execute(
                f"SELECT * FROM items WHERE id IN ({marks})", list(ids)).fetchall()
        return [self._row(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
            by_cls = dict(self._conn.execute(
                "SELECT cls, COUNT(*) c FROM items GROUP BY cls").fetchall())
            last = self._conn.execute(
                "SELECT MAX(ts) m FROM items").fetchone()["m"]
        return {"total": total, "by_cls": by_cls, "last_ts": last}

    def get_kv(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            self._conn.commit()

    # ---------- 关注列表（S2：UI 用户层；config.yaml 保持种子/默认，程序不回写） ----------
    WATCHLIST_KEY = "watchlist_user"

    def get_watchlist_user(self) -> list[dict] | None:
        """用户层关注列表；None=从未在 UI 保存过（生效列表=config 种子）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key=?", (self.WATCHLIST_KEY,)).fetchone()
        if not row:
            return None
        try:
            v = json.loads(row["value"])
            return v if isinstance(v, list) else None
        except (json.JSONDecodeError, TypeError):
            return None

    def set_watchlist_user(self, items: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.WATCHLIST_KEY, json.dumps(items, ensure_ascii=False)))
            self._conn.commit()
