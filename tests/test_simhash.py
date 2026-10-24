"""simhash 测试：指纹稳定性、近重复敏感性、入库计算、回填幂等、cluster_size。"""
from datetime import datetime

from app.models import Item
from app.simhash import (CLUSTER_HAMMING, digit_slots, hamming,
                         near_dup, simhash64)
from app.store import Store

A = "市场传闻央行将降准50个基点 释放流动性约万亿元"
A_NEAR = "市场传闻央行将降准50个基点 释放流动性约万亿元（多源快讯）"
B = "美联储宣布维持利率不变 美股三大指数集体收涨"


def test_simhash_stable_and_sensitive():
    assert simhash64(A) == simhash64(A)                  # 稳定
    # 转载改字：落在簇阈值内（阈值标定见 simhash.py 模块注释）
    assert hamming(simhash64(A), simhash64(A_NEAR)) <= CLUSTER_HAMMING
    # 无关文本：与近重复档保持 1.5 倍以上裕度
    assert hamming(simhash64(A), simhash64(B)) > CLUSTER_HAMMING * 1.5


def test_simhash_empty_is_zero():
    assert simhash64("") == 0
    assert simhash64(None) == 0  # type: ignore[arg-type]


def _mk(i, title, text, ts=None):
    return Item(id=f"id{i}", source="cls", ts=ts or datetime.now(),
                title=title, text=text, url="", cls="noise", score=0)


def test_insert_computes_simhash_and_cluster_size(tmp_path):
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(_mk(1, "市场传闻央行降准", A, ts=now))
    s.insert(_mk(2, "市场传闻央行降准", A_NEAR, ts=now))          # 近重复
    s.insert(_mk(3, "美联储决议", B, ts=now))                     # 无关
    rows = {r["id"]: r for r in s.query()["items"]}
    assert rows["id1"]["cluster_size"] >= 2
    assert rows["id2"]["cluster_size"] >= 2
    assert rows["id3"]["cluster_size"] == 1
    assert rows["id1"]["simhash"] != 0


def test_backfill_on_reopen_idempotent(tmp_path):
    """模拟旧库缺指纹：置 0 后重开库，启动回填一次清零且可重复。"""
    db = tmp_path / "t.db"
    s = Store(db)
    s.insert(_mk(1, "t", A))
    with s._lock:
        s._conn.execute("UPDATE items SET simhash=0")
        s._conn.commit()
    assert s.backfill_count() == 1
    s2 = Store(db)                     # 重开触发回填
    assert s2.backfill_count() == 0
    s3 = Store(db)                     # 再开一遍：幂等
    assert s3.backfill_count() == 0
    rows = s3.query()["items"]
    assert rows[0]["simhash"] == simhash64("t" + A)


def test_digit_guard_blocks_same_template_pair(tmp_path):
    """门2 B3：同模板换事实（数字不同）不得入同簇——hamming 落 10-12 的假阳性档。"""
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    s.insert(_mk(1, "杰富瑞上调辉瑞目标价", "将辉瑞目标价上调至35美元，维持增持评级", ts=now))
    s.insert(_mk(2, "美银下调耐克目标价", "将耐克目标价下调至90美元，维持中性评级", ts=now))
    rows = {r["id"]: r for r in s.query()["items"]}
    assert rows["id1"]["cluster_size"] == 1   # 数字槽相异：不算转载
    assert rows["id2"]["cluster_size"] == 1


def test_digit_guard_keeps_true_transfer_with_digits(tmp_path):
    """真转载保数字逐字一致：守卫不得误伤。"""
    s = Store(tmp_path / "t.db")
    now = datetime.now()
    body = "央行开展4500亿元逆回购操作，利率1.40%"
    s.insert(_mk(1, "央行公告", body, ts=now))
    s.insert(_mk(2, "央行公告", body + "（财联社）", ts=now))
    rows = {r["id"]: r for r in s.query()["items"]}
    assert rows["id1"]["cluster_size"] == 2
    assert rows["id2"]["cluster_size"] == 2


# ---------- R4 分支锁死（无数字文本对的拒绝语义，v6/R4） ----------
def _near(a, b):
    from app.simhash import norm_text, bigram_set
    na, nb = norm_text(a), norm_text(b)
    return near_dup(na, nb, simhash64(na), simhash64(nb),
                    ba=bigram_set(na), bb=bigram_set(nb))


def test_r4_both_sides_no_digits_semantic_opposite_not_merged():
    """R4(a)：双侧无数字、同模板语义相反（终止 vs 推进）——containment 恰在
    阈值上（0.80），无数字禁令必须拒绝（V1 hamming 拦不住这对，h=17）。"""
    from app.simhash import containment, norm_text, bigram_set
    a = "某公司公告称终止重大资产重组事项"
    b = "某公司公告称推进重大资产重组事项"
    assert containment(bigram_set(norm_text(a)),
                       bigram_set(norm_text(b))) >= 0.80   # 确认踩在阈值上
    assert digit_slots(norm_text(a)) == [] and digit_slots(norm_text(b)) == []
    assert _near(a, b) is False


def test_r4_asymmetric_no_digit_side_rejected():
    """R4(b)：短侧无数字 ⊂ 长侧有数字、containment≥0.8——短侧无数字即拒
    （不对称拒绝：精度优先）。"""
    from app.simhash import containment, norm_text, bigram_set, digit_slots
    a = "某公司发布业绩预增公告"
    b = "某公司发布业绩预增公告 净利润同比增长45%至12亿元"
    assert containment(bigram_set(norm_text(a)),
                       bigram_set(norm_text(b))) >= 0.80
    assert digit_slots(norm_text(a)) == [] and digit_slots(norm_text(b)) != []
    assert _near(a, b) is False


def test_digit_subset_true_dup_still_merges():
    """对照：短侧数字 ⊆ 长侧数字的真同稿照常并组（非农摘要⊂全文，c=1.0）。"""
    a = "美国9月非农就业人数增加2.9万人"
    b = "美国9月非农就业人数增加2.9万人，预期增加9万人，前值增加16.2万人"
    assert _near(a, b) is True
