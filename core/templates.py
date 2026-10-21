from __future__ import annotations

import json
from pathlib import Path

def load_templates(cfg: dict) -> dict:
    root = Path(cfg["_root"]) / "templates"
    out = {}
    for p in sorted(root.glob("T*.json")):
        out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    return out

def _pick_modifier(mod) -> float:
    if isinstance(mod, (list, tuple)):
        return (mod[0] + mod[1]) / 2.0
    return float(mod)

def budget_formula(ctl: float, modifier: float) -> float:
    return round(7 * ctl * modifier)

def _round5(x: float) -> int:
    return int(round(x / 5.0) * 5)

def select_template(state: dict, cfg: dict) -> dict:
    if state.get("red_s_active"):
        return {"template_id": "T6_reds", "t4_level": None}
    if state.get("injury_active"):
        return {"template_id": "T6_injury", "t4_level": None}

    days_to_race = state.get("days_to_race")
    if days_to_race is not None and 1 <= days_to_race <= 10:
        return {"template_id": "T5_taper", "t4_level": None}

    tsb = state.get("tsb")
    acwr = state.get("acwr")
    if (tsb is not None and tsb <= -170) or (acwr is not None and acwr > 1.5):
        return {"template_id": "T4_transform", "t4_level": "hard"}
    if (tsb is not None and tsb <= -100) or (acwr is not None and acwr >= 1.3):
        return {"template_id": "T4_transform", "t4_level": "soft"}

    if state.get("block_week") == 4:
        return {"template_id": "T3_deload", "t4_level": None}

    completion = state.get("completion_pct")
    if completion is not None and completion < cfg["templates"]["completion_t1_threshold"]:
        return {"template_id": "T1_base", "t4_level": None}
    if (completion is not None and completion >= cfg["templates"]["completion_t2_threshold"]
            and state.get("acwr_band") == "green"
            and not state.get("gate_history")):
        return {"template_id": "T2_progress", "t4_level": None}
    return {"template_id": "T1_base", "t4_level": None}

def _render(template: dict, budget: float) -> dict:
    days = {}
    for day, d in template["days"].items():
        sessions = []
        for s in d["sessions"]:
            s = dict(s)
            pct = s.get("pct") or 0.0
            s["planned_au"] = _round5(budget * pct / 100.0)
            sessions.append(s)
        days[day] = {"day_type": d["day_type"], "sessions": sessions}
    return days

def _scale_week(days: dict, factor: float) -> dict:
    out = {}
    for day, d in days.items():
        sessions = []
        for s in d["sessions"]:
            s = dict(s)
            if s.get("planned_au"):
                s["planned_au"] = _round5(s["planned_au"] * factor)
            sessions.append(s)
        out[day] = {"day_type": d["day_type"], "sessions": sessions}
    return out

def _limit_quality(days: dict, quality_max: int) -> dict:
    count = 0
    for day, d in days.items():
        for s in d["sessions"]:
            if s.get("slot") in ("primary_quality", "primary_quality_2"):
                if count >= quality_max:
                    s["slot"] = "primary_easy"
                    s["planned_rpe"] = 3
                    s["structure"] = None
                count += 1
    return days

def build_week(template_id: str, budget: float, cfg: dict, prev_week: dict | None = None,
               t4_level: str | None = None, injury_constraints: dict | None = None,
               registry: list[dict] | None = None) -> dict:
    templates = load_templates(cfg)
    t = templates.get(template_id)
    if t is None:
        raise KeyError(f"未知模板 {template_id}")

    if t.get("transform"):
        level = t4_level or "soft"
        spec = t[level]
        mod = _pick_modifier(spec["budget_modifier"])
        if prev_week:
            days = _limit_quality(_scale_week(prev_week, mod), spec.get("quality_max", 1))
        else:
            days = _limit_quality(_render(templates["T1_base"], budget * mod), spec.get("quality_max", 1))
        return {"template_id": template_id, "week_budget_au": round(budget * mod), "days": days}

    if t.get("branch") == "injury":
        mod = _pick_modifier(t["budget_modifier"])
        days = _render(templates["T1_base"], budget * mod)
        days = _apply_injury(days, injury_constraints or {}, registry or [])
        return {"template_id": template_id, "week_budget_au": round(budget * mod), "days": days}

    if t.get("branch") == "reds":
        mod = _pick_modifier(t["budget_modifier"])
        days = _render(templates["T1_base"], budget * mod)
        days = _apply_reds(days, t)
        return {"template_id": template_id, "week_budget_au": round(budget * mod), "days": days}

    mod = _pick_modifier(t.get("budget_modifier", 1.0))
    return {"template_id": template_id, "week_budget_au": round(budget * mod), "days": _render(t, budget * mod)}

def _apply_injury(days: dict, constraints: dict, registry: list[dict]) -> dict:
    forbid = constraints.get("forbid") or []
    limit = constraints.get("limit") or {}
    has_rehab = any(r.get("pattern") == "lower_cardio" and r.get("active") for r in registry)
    for day, d in days.items():
        for s in d["sessions"]:
            if "run" in (s.get("sport_id") or "") and "run" in forbid:
                if has_rehab:
                    s["sport_id"] = next(r["sport_id"] for r in registry if r.get("pattern") == "lower_cardio")
                    s["pattern"] = "lower_cardio"
                    s["slot"] = "rehab_easy"
                else:
                    s["_drop"] = True
            if s.get("sport_id") == "climb" and limit.get("climb") == "volume_only":
                s["forbidden"] = list(set(s.get("forbidden") or []) | {"intensity", "hangboard"})
        d["sessions"] = [s for s in d["sessions"] if not s.get("_drop")]
    return days

def _apply_reds(days: dict, t: dict) -> dict:
    notes_forced = t["rules"]["notes_forced"]
    for day, d in days.items():
        for s in d["sessions"]:
            if s.get("slot") in ("primary_quality", "primary_quality_2"):
                s["slot"] = "primary_easy"
                s["planned_rpe"] = 3
                s["structure"] = None
            if s.get("slot") in ("secondary_intensity",):
                s["forbidden"] = list(set(s.get("forbidden") or []) | {"intensity", "hangboard"})
            s["notes"] = (s.get("notes") or "") + (" " + notes_forced if notes_forced not in (s.get("notes") or "") else "")
    return days
