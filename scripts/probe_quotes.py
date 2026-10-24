"""行情接口探针（S3，20261003 夜，一次性留证——纪律：每候选接口只跑一两次，不循环压测）。

对 13 个标的各试 2-3 个 akshare 候选接口，跑通的记录：函数名/参数/字段名/涨跌幅取法。
产出 quotes 配置矩阵（哪些标的有戏、哪些降级）。用法：python scripts/probe_quotes.py
"""
from __future__ import annotations

import json
import sys
import traceback

import akshare as ak


def probe(name: str, fn, extract=None):
    """跑单个候选接口：打印成败与字段样例，异常不中断。"""
    try:
        df = fn()
        cols = list(df.columns) if hasattr(df, "columns") else type(df).__name__
        sample = None
        if hasattr(df, "tail"):
            tail = df.tail(2)
            sample = tail.to_dict("records")
        ok = {"probe": name, "cols": cols, "rows": len(df), "sample": sample}
        if extract:
            try:
                ok["extracted"] = extract(df)
            except Exception as e:  # noqa: BLE001
                ok["extract_error"] = repr(e)
        print(json.dumps(ok, ensure_ascii=False, default=str)[:1500])
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"probe": name, "error": repr(e)[:200]}, ensure_ascii=False))


def main() -> int:
    print("=" * 30, "指数/全球族", "=" * 30)
    # 纳指（美股指数，sina 全球指数）
    probe("index_us_stock_sina .IXIC",
          lambda: ak.index_us_stock_sina(symbol=".IXIC"),
          lambda df: {"last": float(df.iloc[-1]["close"]), "prev": float(df.iloc[-2]["close"]),
                      "date": str(df.iloc[-1]["date"])})
    # 上证/沪深300（东财指数历史日线）
    probe("stock_zh_index_daily sh000001",
          lambda: ak.stock_zh_index_daily(symbol="sh000001"),
          lambda df: {"last": float(df.iloc[-1]["close"]), "prev": float(df.iloc[-2]["close"]),
                      "date": str(df.iloc[-1]["date"])})
    probe("stock_zh_index_daily sh000300",
          lambda: ak.stock_zh_index_daily(symbol="sh000300"))
    # 恒指（东财港股指数现货）
    probe("stock_hk_index_spot_em",
          lambda: ak.stock_hk_index_spot_em())
    # 全球指数批量（东财全球指数现货：KOSPI/日经/恒指可能都在）
    probe("index_global_spot_em",
          lambda: ak.index_global_spot_em())
    # 日经（东财）
    probe("index_nikkei_spot_em?" if hasattr(ak, "index_nikkei_spot_em") else "(no nikkei spot)",
          lambda: ak.index_nikkei_spot_em() if hasattr(ak, "index_nikkei_spot_em") else (_ for _ in ()).throw(RuntimeError("n/a")))

    print("=" * 30, "债券/利率族", "=" * 30)
    # 中美国债收益率（中债10Y + 美债10Y 一次拿全）
    probe("bond_zh_us_rate",
          lambda: ak.bond_zh_us_rate(start_date="20260901"),
          lambda df: {"cols_ok": True, "last": df.iloc[-1].to_dict()})

    print("=" * 30, "SOFR", "=" * 30)
    probe("rate_sofr",
          lambda: ak.rate_sofr(),
          lambda df: {"last": df.iloc[-1].to_dict()})

    print("=" * 30, "外汇族", "=" * 30)
    # USDCNH
    probe("fx_spot_quote" if hasattr(ak, "fx_spot_quote") else "(no fx_spot_quote)",
          lambda: ak.fx_spot_quote() if hasattr(ak, "fx_spot_quote") else (_ for _ in ()).throw(RuntimeError("n/a")))
    probe("currency_boc_safe" if hasattr(ak, "currency_boc_safe") else "(no currency_boc_safe)",
          lambda: ak.currency_boc_safe() if hasattr(ak, "currency_boc_safe") else (_ for _ in ()).throw(RuntimeError("n/a")))

    print("=" * 30, "期货/商品族", "=" * 30)
    # COMEX 黄金 / 布伦特（外盘期货历史）
    probe("futures_foreign_hist GC",
          lambda: ak.futures_foreign_hist(symbol="GC"))
    probe("futures_foreign_hist B",
          lambda: ak.futures_foreign_hist(symbol="B"))
    # 全球期货批量现货（东财）
    probe("futures_global_spot_em" if hasattr(ak, "futures_global_spot_em") else "(no futures_global_spot_em)",
          lambda: ak.futures_global_spot_em() if hasattr(ak, "futures_global_spot_em") else (_ for _ in ()).throw(RuntimeError("n/a")))

    print("=" * 30, "加密货币族", "=" * 30)
    probe("crypto_hist btc",
          lambda: ak.crypto_hist(symbol="btc"),
          lambda df: {"last": df.iloc[-1].to_dict()})

    print("PROBE DONE", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
