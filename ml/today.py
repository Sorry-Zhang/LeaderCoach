from __future__ import annotations

import argparse
import json
from datetime import date

from core import db as dbmod
from core.config import load_config
from ml.anchor_profile import run as profile_run
from ml.plan_audit import build_plan

WD = {"mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
      "fri": "周五", "sat": "周六", "sun": "周日"}

REST_GATES = {"G2", "G5"}
EASY_GATES = {"G1", "G3", "G7", "G8", "G10"}
DOWN_GATES = {"G4", "G9"}

def apply_gates(sessions: list[dict], gates: list[dict]) -> tuple[list[dict], list[str]]:
    ids = {g["gate"] for g in gates}
    actions = []
    out = []
    if ids & REST_GATES:
        actions.append("触发休息闸门 → 今日休息")
        return [], actions

    for s in sessions:
        s = dict(s)
        if ids & EASY_GATES and (s.get("planned_rpe") or 0) >= 6:
            s["planned_rpe"] = 3
            s["structure"] = None
            s["notes"] = (s.get("notes") or "") + " 闸门降级：质量课→轻松"
            actions.append(f"{s.get('sport_id')} 质量课→轻松")
        elif ids & DOWN_GATES and (s.get("planned_rpe") or 0) >= 7:
            s["planned_rpe"] = max(3, (s.get("planned_rpe") or 7) - 1)
            actions.append(f"{s.get('sport_id')} 强度降一档")
        out.append(s)
    return out, actions

def _fmt_session(s: dict) -> dict:
    return {
        "项目": s.get("sport_id"),
        "类型": s.get("slot"),
        "时长_min": s.get("planned_min"),
        "RPE": s.get("planned_rpe"),
        "AU": s.get("planned_au"),
        "结构": s.get("structure"),
        "说明": s.get("notes") or "",
    }

def today(conn, cfg, day: str) -> dict:
    prof = profile_run(conn)
    focus = (prof.get("diagnosis") or {}).get("focus")
    plan = build_plan(conn, cfg, day, focus=focus)
    wd = date.fromisoformat(day).strftime("%a").lower()[:3]
    sessions = list(plan["week"]["days"].get(wd, {}).get("sessions") or [])
    gates = plan["engine"].get("gates_triggered") or []
    adjusted, actions = apply_gates(sessions, gates)

    ed = plan["engine"]
    return {
        "日期": day,
        "星期": WD.get(wd, wd),
        "状态": {
            "CTL": ed.get("ctl"), "ATL": ed.get("atl"), "TSB": ed.get("tsb"),
            "ACWR": ed.get("acwr"), "Readiness": ed.get("readiness_score"),
        },
        "诊断": focus,
        "模板": plan["selection"].get("template_id"),
        "闸门": [g.get("gate") for g in gates],
        "调整": actions,
        "今日安排": [_fmt_session(s) for s in adjusted] or [{"说明": "休息日"}],
        "周预算_AU": plan["week"].get("week_budget_au"),
    }

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    args = p.parse_args(argv)
    cfg = load_config()
    conn = dbmod.get_conn(cfg)
    try:
        report = today(conn, cfg, args.date)
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    main()
