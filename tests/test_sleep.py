from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import sleep_health as sh
from core.engine import compute_readiness, evaluate_training_forecast
from core.config import load_config

_cfg = load_config()


def test_duration_centered_on_personal_need():
    # 达到个人需要量满分；不足按小时扣分；超 1.5h 后轻微扣分
    assert sh.score_duration(7.5, 7.5) == 100
    assert sh.score_duration(6.5, 7.5) == 60
    assert sh.score_duration(9.5, 7.5) > 80
    assert sh.score_duration(5.0, 7.5) < sh.score_duration(7.0, 7.5)


def test_sleep_debt_penalty():
    assert sh.score_duration(7.5, 7.5, debt_7d=0) == 100
    assert sh.score_duration(7.5, 7.5, debt_7d=6) < 100


def test_regularity_mapping():
    assert sh.score_regularity(sri=95) == 100
    assert sh.score_regularity(sri=50) == 0
    assert 0 < sh.score_regularity(sri=80) < 100
    # 无阶段数据时用睡眠中点离散度兜底
    assert sh.score_regularity(midpoint_sd_h=0) == 100
    assert sh.score_regularity(midpoint_sd_h=2) == 60


def test_efficiency_and_stability():
    assert sh.score_efficiency(100) == 100
    assert sh.score_efficiency(70) == 30
    assert sh.score_stability(waso_min=0, awake_segments=0, frag=1) == 100
    assert sh.score_stability(waso_min=60, awake_segments=4, frag=5) < 50


def test_subjective_scale():
    assert sh.score_subjective(5) == 100
    assert sh.score_subjective(1) == 40
    assert sh.score_subjective(None) is None


def test_readiness_uses_sleep_state_when_given():
    w = _cfg["readiness"]["weights"]
    base = compute_readiness(-6, -126, 7.2, 2, None, None, w)
    low = compute_readiness(-6, -126, 7.2, 2, None, None, w, sleep_score=20)
    assert low["score"] < base["score"]
    assert low["pillars"]["sleep"]["source"] == "sleep_state"
    assert base["pillars"]["sleep"]["source"] == "hours_abs"


def test_g8_observe_mode_does_not_block():
    # 默认 observe：分数再低也不进训练预测（只记录）
    hits = evaluate_training_forecast(
        {"sleep_state_score": 30, "sleep_g8_mode": "observe", "sleep_g8_enforced": False}, _cfg)
    assert not any(h["gate"] == "G8" for h in hits)


def test_g8_enforce_mode_blocks_when_threshold_hit():
    hits = evaluate_training_forecast(
        {"sleep_state_score": 30, "sleep_g8_mode": "enforce", "sleep_g8_enforced": True}, _cfg)
    assert any(h["gate"] == "G8" for h in hits)


def test_g8_enforce_falls_back_to_hours_without_score():
    hits = evaluate_training_forecast(
        {"sleep_hours": 4.5, "sleep_g8_mode": "enforce", "sleep_g8_enforced": False}, _cfg)
    assert any(h["gate"] == "G8" for h in hits)


class _FakeMetricConn:
    def __init__(self, values):
        self.values = values

    def execute(self, sql, args=()):
        rows = [[v] for v in self.values]
        return type("R", (), {"fetchall": lambda self_inner: rows})()


def test_g8_threshold_is_min_of_p10_and_floor():
    cfg = {"sleep": {"g8_mode": "observe", "g8_absolute_floor": 60.0,
                     "g8_use_personal_p10": True, "g8_p10_window_days": 28,
                     "g8_p10_min_days": 7}}
    # p10 ≈ 70，绝对下限 60 → 阈值取 60
    conn = _FakeMetricConn([80, 82, 85, 78, 88, 90, 76, 84, 86, 81])
    st = sh.g8_status(conn, cfg, "2026-10-27", 59.0)
    assert st["threshold"] == 60.0
    assert st["triggered"] is True
    assert st["enforced"] is False  # observe 模式
    # 分数 61 时高于阈值 → 不触发
    assert sh.g8_status(conn, cfg, "2026-10-27", 61.0)["triggered"] is False
    # p10 低于绝对下限时，用 p10（防止长期差睡眠被正常化）
    conn2 = _FakeMetricConn([50, 52, 55, 48, 58, 60, 46, 54, 56, 51])
    st2 = sh.g8_status(conn2, cfg, "2026-10-27", 47.0)
    assert st2["threshold"] < 60
    assert st2["triggered"] is True


def test_need_precedence_from_profile():
    class FakeRow(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)

    class FakeConn:
        def __init__(self, need):
            self.need = need

        def execute(self, sql, *a):
            need = self.need

            class R:
                def fetchone(self_inner):
                    return FakeRow(sleep_need_h=need) if need is not None else None

            return R()

    assert sh.personal_need_h(FakeConn(7.5), _cfg) == (7.5, "athlete_profile")
    assert sh.personal_need_h(FakeConn(None), _cfg)[1] == "config"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 项通过")
