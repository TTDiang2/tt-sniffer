"""统一数据模型：所有来源的原始条目都归一化为 Item。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class Item:
    id: str                 # source + 归一化文本 的 md5，用于去重
    source: str             # cls / em / sina / ths
    ts: datetime            # 发布时间
    title: str
    text: str
    url: str
    cls: str = "noise"      # rumor | debunk | official | noise
    score: int = 0          # 0-100
    keywords: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)   # 命中的标的（代码/名称）
    watch: bool = False     # 关注列表命中
    backfill: bool = False  # 首次回填的旧数据，不触发告警
    impacts: list = field(default_factory=list)  # 资产影响 [{a,d,k,g}]，见 impact.py
    # S4 来源可信度正交维度：authoritative（正规渠道）| street（股吧/TG 小道源）。
    # 与内容定性（cls）正交——小道源里也有辟谣；street 条目打分降档、前端 ⚠️ 标。
    tier: str = "authoritative"

    def to_row(self) -> dict:
        d = asdict(self)
        d["ts"] = self.ts.strftime("%Y-%m-%d %H:%M:%S")
        return d
