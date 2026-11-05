from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import grade  # noqa: E402


def test_minetti_flat_cost():
    # 平地代谢代价是 3.6 J/(kg·m)（Minetti 2002 方程的常数项）
    assert abs(grade.minetti_cost(0.0) - 3.6) < 1e-9


def test_minetti_uphill_costs_more_than_flat():
    assert grade.minetti_cost(0.05) > grade.minetti_cost(0.0)
    assert grade.minetti_cost(0.10) > grade.minetti_cost(0.05)


def test_minetti_downhill_cheaper_than_flat_up_to_steep():
    # 缓下坡比平地省力，但太陡的下降反而更费力（离心制动）
    assert grade.minetti_cost(-0.10) < grade.minetti_cost(0.0)
    assert grade.minetti_cost(-0.40) > grade.minetti_cost(-0.20)


def test_minetti_clamps_extreme_grades():
    assert grade.minetti_cost(5.0) == grade.minetti_cost(grade.GRADE_CLAMP)
    assert grade.minetti_cost(-5.0) == grade.minetti_cost(-grade.GRADE_CLAMP)


def test_segment_stats_flat():
    pts = [(float(d), 100.0) for d in range(0, 1001, 25)]
    st = grade.segment_stats(pts)
    assert st["gain_m"] == 0.0
    assert st["loss_m"] == 0.0
    assert st["avg_grade_pct"] == 0.0
    assert abs(st["cost_ratio"] - 1.0) < 1e-6


def test_segment_stats_steady_climb():
    # 1 km 上升 50 m = 5% 坡度
    pts = [(float(d), 100.0 + d * 0.05) for d in range(0, 1001, 25)]
    st = grade.segment_stats(pts)
    assert abs(st["avg_grade_pct"] - 5.0) < 0.01
    # 阈值累计法丢弃末尾不足 MIN_CLIMB_M 的余量，因此允许最多 3 m 的低估
    assert 47.0 <= st["gain_m"] <= 50.0
    assert st["loss_m"] == 0.0
    assert st["cost_ratio"] > 1.0


def test_segment_stats_ignores_noise_below_threshold():
    # 0.5 m 的抖动不应被计成爬升
    alts = [100.0, 100.5, 100.1, 100.6, 100.2, 100.7]
    pts = [(float(i * 10), a) for i, a in enumerate(alts)]
    st = grade.segment_stats(pts, min_climb=3.0)
    assert st["gain_m"] == 0.0
    assert st["loss_m"] == 0.0


def test_segment_stats_counts_real_climb_above_threshold():
    # 连续上升 10 m 应被计入
    alts = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
    pts = [(float(i * 50), a) for i, a in enumerate(alts)]
    # 阈值累计法会丢弃最后不足阈值的余量，允许小于真实值 3 m
    st = grade.segment_stats(pts, min_climb=3.0)
    assert 7.0 <= st["gain_m"] <= 10.0


def test_grade_adjusted_pace_flat_unchanged():
    assert abs(grade.grade_adjusted_pace(5.0, 1.0) - 5.0) < 1e-9


def test_grade_adjusted_pace_faster_uphill():
    # 上坡代价比 >1 → 等效平路配速更快（同样用力在平路能跑更快）
    gap = grade.grade_adjusted_pace(5.0, 1.3)
    assert gap < 5.0
    assert abs(gap - 5.0 / 1.3) < 1e-3


def test_grade_adjusted_pace_slower_easy_downhill():
    gap = grade.grade_adjusted_pace(5.0, 0.8)
    assert gap > 5.0


def test_grade_adjusted_pace_uphill_matches_energy_balance():
    # 能量速率守恒：Cr(g)·v_g = Cr(0)·v_flat → pace_flat = pace_g · Cr(0)/Cr(g)
    g = 0.05
    pace = 5.0
    expect = pace * grade.CR_FLAT / grade.minetti_cost(g)
    assert abs(grade.grade_adjusted_pace_by_grade(pace, g) - expect) < 1e-3


def test_grade_adjusted_pace_handles_missing():
    assert grade.grade_adjusted_pace(None, 1.2) is None
    assert grade.grade_adjusted_pace(5.0, None) is None
    assert grade.grade_adjusted_pace(0, 1.2) is None


def test_grade_adjusted_pace_by_grade_matches_ratio():
    g = 0.05
    expect = 5.0 * grade.CR_FLAT / grade.minetti_cost(g)
    assert abs(grade.grade_adjusted_pace_by_grade(5.0, g) - expect) < 1e-3


def test_ols_detects_slope():
    pts = [(0.0, 300.0), (1.0, 310.0), (2.0, 320.0), (3.0, 330.0)]
    fit = grade._ols(pts)
    assert abs(fit["slope"] - 10.0) < 1e-9
    assert abs(fit["r2"] - 1.0) < 1e-9
    assert fit["n"] == 4


def test_ols_needs_enough_points():
    assert grade._ols([(0.0, 1.0), (1.0, 2.0)]) is None


# ── 地形分类 ─────────────────────────────────────────────────

def test_classify_terrain_flat():
    assert grade.classify_terrain(0.0, 0.0, 0.0, 1000.0) == "flat"
    assert grade.classify_terrain(2.0, 3.0, -1.0, 1000.0) == "flat"


def test_classify_terrain_climb():
    assert grade.classify_terrain(20.0, 0.0, 20.0, 1000.0) == "climb"


def test_classify_terrain_descent():
    assert grade.classify_terrain(0.0, 30.0, -30.0, 1000.0) == "descent"


def test_classify_terrain_mixed_beats_net_direction():
    # 段内先上 18 m 再下 10 m：净 +8 m，但绝不能当成纯爬坡段
    assert grade.classify_terrain(18.0, 10.0, 8.0, 1000.0) == "mixed"


def test_classify_terrain_rolling_when_net_small():
    # 段内有明显起伏但净变化 <0.5%，标 rolling 而不是 flat
    assert grade.classify_terrain(6.0, 5.0, 1.0, 1000.0) == "rolling"


def test_classify_terrain_needs_distance():
    assert grade.classify_terrain(10, 0, 10, 0) is None
    assert grade.classify_terrain(10, 0, 10, None) is None


# ── 不对称性与离群点处理 ─────────────────────────────────────

def _split(seq, grade_pct, pace_min_km, dist_m=1000.0, gain=None, loss=None,
           gap=None):
    return {"seq": seq, "avg_grade_pct": grade_pct, "pace_min_km": pace_min_km,
            "gap_pace_min_km": gap if gap is not None else pace_min_km,
            "distance_m": dist_m,
            "elevation_gain_m": gain, "elevation_loss_m": loss,
            "net_elevation_m": (gain or 0) - (loss or 0)}


def test_asymmetry_refuses_when_too_few_segments():
    splits = [_split(1, 1.0, 5.0), _split(2, 1.5, 5.2), _split(3, -1.0, 4.8)]
    res = grade.asymmetry(splits)
    assert "uphill_penalty_sec_per_1pct" not in res
    assert "insufficient_samples" in res


def test_asymmetry_excludes_walk_segments():
    # 一个 11:07/km 的走路段在低坡度处，会把斜率彻底带偏
    splits = [_split(i, 1.0 + i * 0.1, 5.0) for i in range(8)]
    splits.append(_split(99, 0.5, 11.0))
    res = grade.asymmetry(splits)
    assert res.get("excluded_walk_segments") == 1


def test_asymmetry_uphill_penalty_positive_when_clean():
    # 坡度越大越慢，且信噪比足够
    splits = [_split(i, 0.5 + i * 0.8, 5.0 + i * 0.15) for i in range(8)]
    # 下坡越陡配速越快（斜率应给出正的收益值）
    splits += [_split(100 + i, -0.5 - i * 0.8, 5.0 - i * 0.10) for i in range(8)]
    res = grade.asymmetry(splits)
    assert res["uphill_penalty_sec_per_1pct"] > 0
    assert res["downhill_benefit_sec_per_1pct"] > 0
    assert 0 < res["asymmetry_ratio"] <= 2


def test_asymmetry_hides_unreliable_fit():
    # 坡度跨度极小、配速随机 → r2 低，不应输出斜率
    splits = [_split(i, 0.5, 5.0 + (0.3 if i % 2 else -0.3)) for i in range(8)]
    splits += [_split(100 + i, -0.5, 5.0 + (0.2 if i % 2 else -0.2)) for i in range(8)]
    res = grade.asymmetry(splits)
    assert "uphill_penalty_sec_per_1pct" not in res


# ── 分段偏离 ─────────────────────────────────────────────────

def test_segment_deviation_excludes_walking():
    splits = [_split(i, 0.0, 5.0) for i in range(1, 7)]
    splits.append(_split(99, 0.0, 11.0))          # 走路段
    res = grade.segment_deviation(splits, top_n=2)
    assert res["excluded_walk_segments"] == 1
    seqs = [d["seq"] for d in res["most_effortful_segments"]]
    assert 99 not in seqs


def test_segment_deviation_needs_enough_segments():
    assert grade.segment_deviation([_split(1, 0, 5.0)]) is None


# ── 地形汇总 ─────────────────────────────────────────────────

def test_terrain_breakdown_groups_by_label():
    splits = [
        {"terrain": "climb", "distance_m": 1000, "elevation_gain_m": 20,
         "elevation_loss_m": 0, "pace_min_km": 5.0, "gap_pace_min_km": 4.8,
         "avg_grade_pct": 2.0},
        {"terrain": "descent", "distance_m": 1000, "elevation_gain_m": 0,
         "elevation_loss_m": 25, "pace_min_km": 4.5, "gap_pace_min_km": 4.9,
         "avg_grade_pct": -2.5},
    ]
    out = grade.terrain_breakdown(splits)
    assert out["climb"]["segments"] == 1
    assert out["descent"]["segments"] == 1
    assert out["climb"]["gap_pace"] == "4:48"
