"""情绪评分 vs 真实行情对照验证（用户验证管线：评分体系回测的起点）。

用法：
  python scripts/verify_sentiment.py 2026-10-02

对给定日期输出各资产情绪分，并拉取代表性指数/期货的真实涨跌对照：
- 信号日当天有交易 → 当日涨跌（数据公布在盘前/盘中时即是市场反应）
- 信号日次日已有数据 → 次日涨跌（信号产生在收盘后的情形）
A股/港股休市时明确标注"休市，待下个交易日验证"——预测-验证分离是本脚本
的核心语义：先把每天的评分存下来，行情可验证时再回来对答案。

⚠️ 评分是"当日报道情绪"，对照结果只说明方向一致性，单日样本无统计意义；
   积累数周后可做命中率统计（用户规划：深度学习/AI 模型的训练数据管线）。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import Store  # noqa: E402

# 资产 -> [(名称, akshare拉取函数名, 品种参数)]；接口失效自动跳过
TARGETS = [
    ("a_stock", "上证指数", "index_em", "sh000001"),
    ("a_stock", "沪深300", "index_em", "sh000300"),
    ("us_stock", "标普500", "us_sina", ".INX"),
    ("us_stock", "纳斯达克", "us_sina", ".NDX"),
    ("gold", "COMEX黄金", "foreign", "GC"),
]


def _pct(df, day: str):
    """返回 (当日涨跌%, 次日涨跌%) —— 相对各自前收盘。"""
    import pandas as pd
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    col = "date" if "date" in df.columns else "day"
    df[col] = pd.to_datetime(df[col]).dt.strftime("%Y-%m-%d")
    df = df.sort_values(col).reset_index(drop=True)
    close_col = "close" if "close" in df.columns else "收盘"
    if close_col not in df.columns:
        return None, None
    closes = df[[col, close_col]].rename(columns={col: "d", close_col: "c"})
    closes["prev"] = closes["c"].shift(1)
    closes["chg"] = (closes["c"] / closes["prev"] - 1) * 100
    day_rows = closes.set_index("d")["chg"]
    same = round(day_rows.get(day, float("nan")), 2)
    nxt = None
    later = [d for d in day_rows.index if d > day]
    if later:
        nxt = round(day_rows.get(later[0], float("nan")), 2)
    return (None if same != same else same), (None if nxt is None or nxt != nxt else nxt)


def fetch_quote(kind: str, symbol: str, day: str):
    import akshare as ak
    if kind == "index_em":
        df = ak.stock_zh_index_daily_em(symbol=symbol)
    elif kind == "us_sina":
        df = ak.index_us_stock_sina(symbol=symbol)
    else:
        df = ak.futures_foreign_hist(symbol=symbol)
    return _pct(df, day)


def main(day: str):
    # 不碰活库（用户实例可能在跑）：backup API 出一致性快照，重算在副本上进行
    import sqlite3
    from app.config import data_dir
    snap = data_dir() / "_verify_snapshot.db"
    src = sqlite3.connect(data_dir() / "sniffer.db")
    dst = sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    src.close()
    store = Store(snap)
    rows = store.day_sentiment(day)
    print(f"== {day} 资产情绪分（-100..+100，'-'=无方向论据）==")
    for r in rows:
        sc = f"{r['score']:+d}" if r["score"] is not None else "  -"
        print(f"  {r['icon']} {r['name']:6s} {sc:>5}  (利多 {r['bull_n']} / 利空 {r['bear_n']})")
    if not rows:
        print("  （当日无方向论据）")
        return

    print(f"\n== 行情对照（信号日 {day}）==")
    sentiment_by_a = {r["a"]: (r["score"] or 0) for r in rows}
    for asset, name, kind, symbol in TARGETS:
        try:
            same, nxt = fetch_quote(kind, symbol, day)
        except Exception as e:
            print(f"  {name:10s} 行情拉取失败（跳过）: {str(e)[:60]}")
            continue
        s = sentiment_by_a.get(asset)
        s_txt = f"{s:+d}" if s is not None else "  -"
        parts = []
        if same is not None:
            parts.append(f"当日 {same:+.2f}%")
        else:
            parts.append("当日无交易(休市)")
        if nxt is not None:
            parts.append(f"次日 {nxt:+.2f}%")
        else:
            parts.append("次日待验证")
        # 一致性判定：情绪|s|<15 视为中性不判；否则方向符号一致=✓
        verdict = ""
        ref = nxt if nxt is not None else same
        if s is not None and ref is not None and abs(s) >= 15:
            verdict = " ✓" if (s > 0) == (ref > 0) else " ✗"
        print(f"  {name:10s} 情绪 {s_txt:>4} | {', '.join(parts)}{verdict}")
    print("\n注：单日对照无统计意义；评分存于库中，行情可验证时回看。"
          "\n    A股若休市，待下一交易日数据出现后重跑本脚本即可对答案。"
          "\n    '当日'涨跌含非农公布前的日内走势，存在时间对齐误差——"
          "\n    盘后信号（如 20:30 非农）的准确对照以'次日'一列为准。")
    store._conn.close()
    try:
        snap.unlink()
        for suf in ("-wal", "-shm"):
            p = snap.parent / (snap.name + suf)
            if p.exists():
                p.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else str(date.today() - timedelta(days=1))
    datetime.strptime(day, "%Y-%m-%d")
    main(day)
