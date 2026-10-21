from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.engine import (
    srpe_au,
    hr_trimp,
    compute_readiness,
    compute_fitness,
    evaluate_gates,
)
from core.config import load_config

_cfg = load_config()

def test_T1_srpe_anchor():
    assert srpe_au(7, 90) == 630.0

def test_T2_trimp_anchor():
    hr_avg = 178
    t = hr_trimp(60, hr_avg, 50, 200)
    assert abs(t - 183) < 3, t

def test_T3_bridge_anchor():
    t = hr_trimp(60, 178, 50, 200)
    assert abs(round(2.6 * t) - 476) < 5

def test_T4_fitness_steady_state():
    series = compute_fitness([400] * 365)
    assert abs(series[-1]["ctl"] - 400) < 1.0
    series42 = compute_fitness([400] * 42)
    assert abs(series42[-1]["ctl"] - 253) < 5

def test_T5_tsb_gate():
    gates = evaluate_gates({"tsb": -100}, _cfg)
    assert any(g["gate"] == "G1" for g in gates)
    gates2 = evaluate_gates({"tsb": -170}, _cfg)
    assert any(g["gate"] == "G2" for g in gates2)

def test_T8_readiness_anchor():
    r = compute_readiness(-6, -126, 7.2, 2, None, None, _cfg["readiness"]["weights"])
    assert abs(r["score"] - 66) <= 1.5, r
    assert r["confidence"] == "medium"
    assert r["pillars"]["hrv"]["score"] == 67
    assert r["pillars"]["tsb"]["score"] == 46
    assert r["pillars"]["sleep"]["score"] == 82
    assert r["pillars"]["rhr"]["score"] == 78

def test_gate_G3_acwr():
    gates = evaluate_gates({"acwr": 1.8}, _cfg)
    assert any(g["gate"] == "G3" for g in gates)

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 项通过")
