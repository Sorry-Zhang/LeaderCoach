from __future__ import annotations

import argparse
import json
import math
from datetime import date

from core import db as dbmod
from core.config import load_config
from ml.events import ensure_schema

Riegel_REF = 1.06
LABEL = {"pb_400m": "400m", "pb_800m": "800m", "pb_1km": "1km",
         "pb_1500m": "1500m", "pb_1mile": "1mile", "pb_3km": "3km",
         "pb_5km": "5km", "pb_10km": "10km", "pb_hm": "半马",
         "long_run_course": "21km课"}

def _fmt(t):
    m, s = divmod(int(round(t)), 60)
    return f"{m}:{s:02d}"

def run(conn) -> dict:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT event_type, distance_m, time_s, effort, date_precision "
        "FROM events WHERE time_s IS NOT NULL AND distance_m > 0 "
        "ORDER BY distance_m").fetchall()
    if len(rows) < 3:
        return {"error": "锚点不足 3 个"}

    pts = [{"label": LABEL.get(r["event_type"], r["event_type"]),
            "d": r["distance_m"], "t": r["time_s"], "effort": r["effort"],
            "precision": r["date_precision"]} for r in rows]

    pairs = []
    for a, b in zip(pts, pts[1:]):
        exp = math.log(b["t"] / a["t"]) / math.log(b["d"] / a["d"])
        same_effort = a["effort"] == b["effort"] == "max"
        is_course = "21km" in a["label"] or "21km" in b["label"]
        comparable = same_effort and not is_course
        pairs.append({
            "from": a["label"], "to": b["label"],
            "from_time": _fmt(a["t"]), "to_time": _fmt(b["t"]),
            "implied_exponent": round(exp, 3),
            "vs_1.06": round(exp - Riegel_REF, 3),
            "comparable": comparable,
            "interpretation": (
                "不可比（effort/类型不一致）" if not comparable
                else "耐力相对弱" if exp > Riegel_REF + 0.02
                else "速度相对弱" if exp < Riegel_REF - 0.02
                else "均衡"),
        })

    ref = next((p for p in pts if p["label"] == "5km"), pts[len(pts) // 2])
    proj = []
    for p in pts:
        if p is ref:
            continue
        pred = ref["t"] * (p["d"] / ref["d"]) ** Riegel_REF
        proj.append({
            "target": p["label"], "actual": _fmt(p["t"]),
            "riegel_from_5km": _fmt(pred),
            "delta_pct": round((p["t"] - pred) / pred * 100, 1),
        })

    short_labels = {"400m", "800m", "1km", "1500m", "1mile", "3km"}
    long_labels = {"10km", "半马"}
    sd = [p["delta_pct"] for p in proj if p["target"] in short_labels]
    ld = [p["delta_pct"] for p in proj if p["target"] in long_labels]
    sd_m = round(sum(sd) / len(sd), 1) if sd else None
    ld_m = round(sum(ld) / len(ld), 1) if ld else None

    if sd_m is None or ld_m is None:
        focus, rationale, sessions = "unknown", "锚点不足，无法判断", []
    elif ld_m >= 5:
        focus = "endurance"
        rationale = (f"短距离平均快于 Riegel {abs(sd_m):.1f}%，"
                     f"长距离平均慢 {ld_m:.1f}%：耐力是短板")
        sessions = ["threshold", "tempo", "long_run"]
    elif ld_m <= -5:
        focus = "speed"
        rationale = (f"长距离平均快于 Riegel {abs(ld_m):.1f}%：速度是短板")
        sessions = ["interval", "repetition"]
    else:
        focus = "balanced"
        rationale = "长短距离与 Riegel 预测接近，结构均衡"
        sessions = ["mixed"]

    diagnosis = {
        "short_delta_mean_pct": sd_m,
        "long_delta_mean_pct": ld_m,
        "focus": focus,
        "rationale": rationale,
        "suggested_session_types": sessions,
        "caveat": ("基准 5km 为 submaximal，真实 5k 更快 ⇒ 耐力短板只会更明显"
                   if any(p["label"] == "5km" and p["effort"] == "submaximal"
                          for p in pts) else ""),
        "note": "描述性诊断，不是训练安排；实际安排需过引擎训练预测",
    }

    return {"anchors": pts, "adjacent_exponents": pairs,
            "projection_from_5km": proj, "diagnosis": diagnosis}

def main(argv=None):
    p = argparse.ArgumentParser()
    p.parse_args(argv)
    conn = dbmod.get_conn(load_config())
    try:
        report = run(conn)
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    main()
