from __future__ import annotations

import argparse
import json
from datetime import date

from core import db as dbmod
from core.adapt import adapt_week
from core.config import load_config
from core.daily import compute_engine_daily
from core.templates import budget_formula, build_week, select_template
from ml.anchor_profile import run as profile_run

def _flags(conn, day: str) -> dict:
    rs = conn.execute("SELECT value FROM meta WHERE key='red_s_state'").fetchone()
    inj = conn.execute(
        "SELECT COUNT(*) c FROM injury_history WHERE status IN ('active','restricted')"
    ).fetchone()
    return {"red_s_active": bool(rs and rs["value"] == "active"),
            "injury_active": bool(inj and inj["c"] > 0)}

def _forecast_items(engine: dict) -> list:
    """取训练预测项。新字段为空列表时不能退回旧字段（旧字段是 JSON 字符串会误解）。"""
    v = engine.get("training_forecast")
    if v is None:
        v = engine.get("gates_triggered") or []
        if isinstance(v, str):
            import json
            try:
                v = json.loads(v)
            except ValueError:
                v = []
    return v if isinstance(v, list) else []


def build_plan(conn, cfg, day: str, focus: str | None = None) -> dict:
    ed = compute_engine_daily(conn, cfg, day)
    fl = _flags(conn, day)
    acwr = ed.get("acwr")
    state = {
        "red_s_active": fl["red_s_active"],
        "injury_active": fl["injury_active"],
        "days_to_race": None,
        "tsb": ed.get("tsb"),
        "acwr": acwr,
        "block_week": 0,
        "completion_pct": None,
        "acwr_band": "green" if (acwr is not None and 0.8 <= acwr <= 1.3) else "other",
        "forecast_history": _forecast_items(ed),
    }
    sport_hist = {r["sport_id"]: r["c"] for r in conn.execute(
        "SELECT sport_id, COUNT(*) c FROM activities GROUP BY sport_id")}
    sel = select_template(state, cfg)
    budget = budget_formula(ed["ctl"], 1.0)
    week = build_week(sel["template_id"], budget, cfg, t4_level=sel.get("t4_level"))
    week = adapt_week(week, sport_hist, focus=focus, week_index=0)
    return {"day": day, "engine": ed, "state": state, "selection": sel,
            "budget": budget, "week": week, "sport_hist": sport_hist}

def audit(plan: dict, diagnosis: dict) -> dict:
    week = plan["week"]
    sessions = [s for d in week["days"].values() for s in d["sessions"]]
    total_au = sum(s.get("planned_au") or 0 for s in sessions)
    high_au = sum(s.get("planned_au") or 0 for s in sessions
                  if (s.get("planned_rpe") or 0) >= 7)
    quality = [s for s in sessions
               if s.get("slot") in ("primary_quality", "primary_quality_2")]
    long_run = [s for s in sessions if s.get("slot") == "primary_long"]
    structures = [s.get("structure") for s in sessions if s.get("structure")]
    has_endurance = any(x in ("threshold", "tempo") for x in structures)

    checks = []
    ctl = plan["engine"]["ctl"]
    checks.append({
        "check": "周预算 vs 7×CTL",
        "value": f"{week['week_budget_au']} / {round(7 * ctl)}",
        "pass": abs(week["week_budget_au"] - 7 * ctl) <= max(30, 0.1 * 7 * ctl),
    })
    checks.append({"check": "质量课数量 ≤2", "value": len(quality),
                   "pass": len(quality) <= 2})
    hi_share = round(high_au / total_au * 100, 1) if total_au else 0
    checks.append({"check": "高强度占比 ≤20%", "value": f"{hi_share}%",
                   "pass": hi_share <= 20})
    checks.append({"check": "长距离 ≤30% 周量",
                   "value": f"{round((long_run[0]['planned_au'] / total_au * 100), 1)}%"
                   if long_run and total_au else "无长距离",
                   "pass": bool(long_run) and total_au and
                           long_run[0]["planned_au"] / total_au <= 0.30})

    focus = diagnosis.get("focus")
    if focus == "endurance":
        checks.append({
            "check": "诊断=耐力短板 → 计划含阈值/节奏课",
            "value": "有" if has_endurance else "无（只有 intervals）",
            "pass": has_endurance,
            "note": "T1 的周二质量课是 intervals，不是 threshold/tempo",
        })
    elif focus == "speed":
        checks.append({"check": "诊断=速度短板 → 计划含间歇课",
                       "value": "有" if "intervals" in structures else "无",
                       "pass": "intervals" in structures})

    plan_sports = {s.get("sport_id") for s in sessions if s.get("sport_id")}
    hist = plan.get("sport_hist") or {}
    cls = week.get("sport_classes") or {}
    never_done = sorted(s for s in plan_sports
                        if s not in hist and cls.get(s) != "occasional")
    not_planned = sorted(set(hist) - plan_sports)
    checks.append({
        "check": "计划项目与实际运动构成一致",
        "value": f"计划={sorted(plan_sports)} 历史={sorted(hist)}",
        "pass": not never_done,
        "note": (f"计划包含从未做过的项目：{never_done}。" if never_done else "")
                + (f"历史有但计划未含：{not_planned}。" if not_planned else ""),
    })

    gates = _forecast_items(plan["engine"])
    checks.append({"check": "预测项未命中", "value": len(gates), "pass": not gates,
                   "gates": gates})

    failed = [c["check"] for c in checks if not c["pass"]]
    return {
        "template": week["template_id"],
        "week_budget_au": week["week_budget_au"],
        "sessions": len(sessions),
        "quality_sessions": len(quality),
        "structures": structures,
        "high_intensity_share_pct": hi_share,
        "checks": checks,
        "failed": failed,
        "verdict": "计划与诊断/训练预测一致" if not failed else "存在不一致：" + "；".join(failed),
    }

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    args = p.parse_args(argv)
    cfg = load_config()
    conn = dbmod.get_conn(cfg)
    try:
        prof = profile_run(conn)
        plan = build_plan(conn, cfg, args.date, focus=prof.get("diagnosis", {}).get("focus"))
    finally:
        conn.close()
    report = {
        "date": args.date,
        "diagnosis": prof.get("diagnosis"),
        "engine_state": {k: plan["engine"].get(k) for k in
                         ("ctl", "atl", "tsb", "acwr", "readiness_score", "flags")},
        "selection": plan["selection"],
        "audit": audit(plan, prof.get("diagnosis") or {}),
        "week": plan["week"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    main()
