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


def test_grade_adjusted_pace_slower_uphill():
    gap = grade.grade_adjusted_pace(5.0, 1.3)
    assert gap > 5.0


def test_grade_adjusted_pace_handles_missing():
    assert grade.grade_adjusted_pace(None, 1.2) is None
    assert grade.grade_adjusted_pace(5.0, None) is None
    assert grade.grade_adjusted_pace(0, 1.2) is None


def test_grade_adjusted_pace_by_grade_matches_ratio():
    g = 0.05
    expect = 5.0 * grade.minetti_cost(g) / grade.CR_FLAT
    assert abs(grade.grade_adjusted_pace_by_grade(5.0, g) - expect) < 1e-3


def test_ols_detects_slope():
    pts = [(0.0, 300.0), (1.0, 310.0), (2.0, 320.0), (3.0, 330.0)]
    fit = grade._ols(pts)
    assert abs(fit["slope"] - 10.0) < 1e-9
    assert abs(fit["r2"] - 1.0) < 1e-9
    assert fit["n"] == 4


def test_ols_needs_enough_points():
    assert grade._ols([(0.0, 1.0), (1.0, 2.0)]) is None
