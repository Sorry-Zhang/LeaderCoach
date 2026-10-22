from __future__ import annotations

import math
from datetime import date, datetime, timedelta

BANISTER_FEMALE_K = 0.86
BANISTER_FEMALE_B = 1.67

HRV_POINTS = [5, 0, -5, -10, -15, -20]
HRV_SCORES = [85, 80, 70, 55, 40, 25]
TSB_POINTS = [50, 20, 0, -70, -100, -170]
TSB_SCORES = [90, 80, 75, 60, 50, 40]
SLEEP_POINTS = [8, 7, 6, 5]
SLEEP_SCORES = [90, 80, 60, 40]
RHR_POINTS = [0, 3, 5, 10, 15]
RHR_SCORES = [85, 75, 65, 45, 30]

STRENGTH_PATTERNS = ("squat", "hinge", "push", "pull", "core", "hang", "carry")

def _interp(xs: list[float], ys: list[float], x: float | None) -> float | None:
    if x is None:
        return None
    if x <= xs[0] and xs[0] <= xs[1]:
        return ys[0]
    if x >= xs[0] and xs[0] >= xs[1]:
        return ys[0]
    if x <= xs[-1] and xs[-1] <= xs[-2]:
        return ys[-1]
    if x >= xs[-1] and xs[-1] >= xs[-2]:
        return ys[-1]
    for i in range(len(xs) - 1):
        lo, hi = min(xs[i], xs[i + 1]), max(xs[i], xs[i + 1])
        if lo <= x <= hi:
            y0, y1 = ys[i], ys[i + 1]
            if xs[i] == xs[i + 1]:
                return y0
            return y0 + (y1 - y0) * (x - xs[i]) / (xs[i + 1] - xs[i])
    return None

def srpe_au(rpe: float | None, duration_min: float | None) -> float | None:
    if rpe is None or duration_min is None:
        return None
    return round(rpe * duration_min, 1)

def hr_trimp(duration_min, hr_avg, hr_rest, hr_max) -> float | None:
    if None in (duration_min, hr_avg, hr_rest, hr_max):
        return None
    if (hr_max - hr_rest) <= 0:
        return None
    dhr = (hr_avg - hr_rest) / (hr_max - hr_rest)
    if dhr <= 0:
        return 0.0
    val = duration_min * dhr * BANISTER_FEMALE_K * math.exp(BANISTER_FEMALE_B * dhr)
    return round(val, 2)

def compute_activity_load(
    rpe, duration_min, avg_hr, hr_rest, hr_max,
    k_bridge: float, hr_cross: bool, luteal: bool, cfg: dict,
) -> dict:
    s = srpe_au(rpe, duration_min)
    t = hr_trimp(duration_min, avg_hr, hr_rest, hr_max)

    if s is not None:
        load_final = s
        path = "srpe"
        mismatch = None
        if hr_cross and t is not None and t > 0:
            tol = cfg["load"]["luteal_mismatch_tol"] if luteal else cfg["load"]["mismatch_tol"]
            ref = k_bridge * t
            if ref > 0 and abs(s - ref) / ref > tol:
                mismatch = "srpe_hr_mismatch"
        return {
            "srpe_au": s, "hr_trimp": t, "load_final": load_final,
            "load_path_used": path, "mismatch_flag": mismatch,
        }
    if t is not None:
        return {
            "srpe_au": None, "hr_trimp": t,
            "load_final": round(k_bridge * t, 1),
            "load_path_used": "bridge_trimp", "mismatch_flag": None,
        }
    return {
        "srpe_au": None, "hr_trimp": None, "load_final": None,
        "load_path_used": None, "mismatch_flag": None,
    }

def intensity_domain(rpe=None, hr_zones_pct=None, au_per_h=None) -> str | None:
    if isinstance(hr_zones_pct, dict) and hr_zones_pct:
        high = hr_zones_pct.get("b4", 0) + hr_zones_pct.get("b5", 0)
        if high >= 0.20:
            return "high"
        mid = hr_zones_pct.get("b3", 0)
        if high + mid >= 0.50:
            return "moderate"
        return "low"
    if rpe is not None:
        if rpe >= 7:
            return "high"
        if rpe >= 5:
            return "moderate"
        return "low"
    if au_per_h is not None:
        if au_per_h >= 600:
            return "high"
        if au_per_h >= 300:
            return "moderate"
        return "low"
    return None

def compute_fitness(loads: list[float], tau_ctl: float = 42, tau_atl: float = 7) -> list[dict]:
    ctl = atl = 0.0
    series = []
    for i, load in enumerate(loads):
        load = load or 0.0
        ctl += (load - ctl) / tau_ctl
        atl += (load - atl) / tau_atl
        series.append({
            "day": i,
            "load": load,
            "ctl": round(ctl, 2),
            "atl": round(atl, 2),
            "tsb": round(ctl - atl, 2),
            "warmup": i < 28,
        })
    return series

def compute_acwr(loads: list[float]) -> dict | None:
    lam7, lam28 = 0.25, 2 / 29
    e7 = e28 = 0.0
    for load in loads:
        load = load or 0.0
        e7 = load * lam7 + e7 * (1 - lam7)
        e28 = load * lam28 + e28 * (1 - lam28)
    if e28 <= 0:
        return None
    return {"ewma7": round(e7, 2), "ewma28": round(e28, 2), "acwr": round(e7 / e28, 3)}

def acwr_band(acwr: float, cfg: dict) -> str:
    g = cfg["acwr"]["green"]
    if acwr > cfg["acwr"]["freeze_threshold"]:
        return "red"
    if g[0] <= acwr <= g[1]:
        return "green"
    return "yellow"

def pillar_score(kind: str, value) -> float | None:
    if value is None:
        return None
    if kind == "hrv":
        return _interp(HRV_POINTS, HRV_SCORES, value)
    if kind == "tsb":
        return _interp(TSB_POINTS, TSB_SCORES, value)
    if kind == "sleep":
        return _interp(SLEEP_POINTS, SLEEP_SCORES, value)
    if kind == "rhr":
        return _interp(RHR_POINTS, RHR_SCORES, value)
    return None

def subjective_score(soreness, sleep_feel) -> float | None:
    if soreness is None or sleep_feel is None:
        return None
    score = 60 + (5 - soreness) * 8 + (sleep_feel - 3) * 5
    return max(0.0, min(100.0, score))

def compute_readiness(hrv_dev, tsb, sleep_hours, rhr_dev, soreness, sleep_feel, weights) -> dict:
    pillars = {}
    raw = {
        "hrv": hrv_dev, "tsb": tsb, "sleep": sleep_hours, "rhr": rhr_dev,
    }
    for kind, val in raw.items():
        sc = pillar_score(kind, val)
        if sc is not None:
            pillars[kind] = {"value": val, "weight": weights[kind], "score": round(sc)}
    sub = subjective_score(soreness, sleep_feel)
    if sub is not None:
        pillars["subjective"] = {"value": None, "weight": weights["subjective"], "score": round(sub)}

    if not pillars:
        return {"score": None, "confidence": "low", "pillars": pillars}

    total_w = sum(p["weight"] for p in pillars.values())
    score = sum(p["score"] * p["weight"] for p in pillars.values()) / total_w
    n = len(pillars)
    confidence = "high" if n >= 5 else ("medium" if n >= 3 else "low")
    return {"score": round(score, 1), "confidence": confidence, "pillars": pillars}

def evaluate_gates(ctx: dict, cfg: dict) -> list[dict]:
    f, g = cfg["fitness"], cfg["gates"]
    gates = []

    tsb = ctx.get("tsb")
    if tsb is not None and tsb <= f["tsb_hard_gate"]:
        gates.append({"gate": "G2", "action": "休息日（TSB ≤ 硬闸）"})
    elif tsb is not None and tsb <= f["tsb_soft_gate"]:
        gates.append({"gate": "G1", "action": "当日强制轻松课（TSB ≤ 软闸）"})

    acwr = ctx.get("acwr")
    if acwr is not None and acwr > cfg["acwr"]["freeze_threshold"]:
        gates.append({"gate": "G3", "action": "质量课冻结（当日+次日）"})

    hrv_hist = ctx.get("hrv_dev_history") or []
    persist = g["luteal_gate_persist_days"] if ctx.get("luteal_phase_confound") else g["hrv_gate_persist_days"]
    if len(hrv_hist) >= persist and all(v is not None and v <= g["hrv_dev_gate"] for v in hrv_hist[-persist:]):
        gates.append({"gate": "G4", "action": "强度降级一档（HRV 连续偏离）",
                      "options": ["降级一档", "维持原计划", "改为轻松课"]})

    # 单日骤降：熬夜、疾病、急性压力等非训练因素也会造成大幅下跌，
    # 不能等「连续 2 天」才响应。
    hrv_today = ctx.get("hrv_dev_today")
    if hrv_today is None and hrv_hist:
        hrv_today = hrv_hist[-1]
    options = ["改为轻松课", "维持原计划", "视体感再定"]
    if hrv_today is not None:
        if hrv_today <= g["hrv_single_drop_easy"]:
            gates.append({"gate": "G4c",
                          "action": f"建议当日轻松课（HRV 单日骤降 {hrv_today:.1f}%）",
                          "options": options,
                          "user_choice_required": True})
        elif hrv_today <= g["hrv_single_drop_down"]:
            gates.append({"gate": "G4b",
                          "action": f"建议强度降级一档（HRV 单日骤降 {hrv_today:.1f}%）",
                          "options": options,
                          "user_choice_required": True})

    rhr_hist = ctx.get("rhr_dev_history") or []
    fever = ctx.get("fever", False)
    if fever or (len(rhr_hist) >= g["rhr_gate_persist_days"]
                 and all(v is not None and v >= g["rhr_dev_gate"] for v in rhr_hist[-g["rhr_gate_persist_days"]:])):
        gates.append({"gate": "G5", "action": "疾病协议（休息 + 疾病旗）"})

    if ctx.get("injury_active"):
        gates.append({"gate": "G6", "action": "禁跑分支 + 按伤位限制（constraint_json）"})

    readiness = ctx.get("readiness")
    if readiness is not None and readiness < g["readiness_easy_floor"]:
        gates.append({"gate": "G7", "action": "默认轻松日"})

    sleep_h = ctx.get("sleep_hours")
    if sleep_h is not None and sleep_h < g["sleep_min_for_quality"]:
        gates.append({"gate": "G8", "action": "当日无质量课"})

    if ctx.get("red_s_active"):
        gates.append({"gate": "G9", "action": "负荷 −20~30% + 暂停减脂 + 蛋白质 1.8–2.2 g/kg"})

    cramps = ctx.get("cramps_today")
    if cramps is not None and cramps >= 7:
        gates.append({"gate": "G10", "action": "当日强制轻松课（痛经 ≥7/10）"})

    return gates

def evaluate_reds(ctx: dict, cfg: dict) -> dict:
    r = cfg["reds"]
    triggers = []

    if ctx.get("weight_drop_pct") is not None and ctx["weight_drop_pct"] <= -r["weight_drop_pct"]:
        triggers.append("T1")
    hrv_hist = ctx.get("hrv_dev_history") or []
    if len(hrv_hist) >= r["hrv_dev_persist_days"] and all(
        v is not None and v <= cfg["gates"]["hrv_dev_gate"] for v in hrv_hist[-r["hrv_dev_persist_days"]:]
    ):
        triggers.append("T2")
    rhr_hist = ctx.get("rhr_dev_history") or []
    if len(rhr_hist) >= r["hrv_dev_persist_days"] and all(
        v is not None and v >= r["rhr_dev_persist_pct"] for v in rhr_hist[-r["hrv_dev_persist_days"]:]
    ):
        triggers.append("T3")
    if ctx.get("completion_below_70_weeks", 0) >= 2 and ctx.get("quality_rpe_median", 0) >= 7:
        triggers.append("T4")
    if ctx.get("cycle_irregular") or ctx.get("cycle_missed"):
        triggers.append("T5")

    active = len(triggers) > 0
    actions = []
    if active:
        lo, hi = r["load_reduction_pct"]
        actions.append(f"负荷下调 {lo}–{hi}%")
        actions.append("暂停减脂建议")
        actions.append(f"蛋白质目标 {r['protein_g_per_kg'][0]}–{r['protein_g_per_kg'][1]} g/kg")
        actions.append("写操作自动升级 S2 确认级")
        if "T5" in triggers:
            actions.append("建议就医评估（月经紊乱）")
    return {"active": active, "triggers": triggers, "actions": actions}

_DAY_ORDER = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

def _day_index(s: dict) -> int:
    d = s.get("date")
    if isinstance(d, str):
        if d in _DAY_ORDER:
            return _DAY_ORDER[d]
        try:
            return date.fromisoformat(d[:10]).weekday()
        except Exception:
            return 0
    return 0

def interference_conflicts(sessions: list[dict], cut_mode: bool = False) -> list[dict]:
    conflicts = []
    by_date = {}
    for s in sessions:
        by_date.setdefault(s.get("date"), []).append(s)

    dated = sorted(sessions, key=_day_index)

    for i, a in enumerate(dated):
        for b in dated[i + 1:]:
            gap = _day_index(b) - _day_index(a)
            if gap > 2:
                break
            pa, pb = a.get("pattern"), b.get("pattern")
            if gap == 1:
                if (pa in ("squat", "hinge") and (a.get("rpe") or 0) >= 7
                        and pb == "lower_impact" and b.get("intensity") == "high"):
                    conflicts.append({"pair": (a.get("slot"), b.get("slot")), "rule": "R4",
                                      "message": "下肢大重量(RPE≥7)后 24h 内不应安排跑步质量课"})
                if (pa == "lower_impact" and (a.get("duration_min") or 0) >= 90
                        and pb in ("squat", "hinge")):
                    conflicts.append({"pair": (a.get("slot"), b.get("slot")), "rule": "R4",
                                      "message": "长距离(≥90min)后 24h 内不应安排下肢力量"})
                if pa == "upper_pull_finger" and a.get("intensity") == "high" and pb in ("pull", "push"):
                    conflicts.append({"pair": (a.get("slot"), b.get("slot")), "rule": "R4",
                                      "message": "高强度攀岩后 24–48h 内不应安排上肢拉类力量"})
            if gap < 2 and ((pa == "hang" and pb == "upper_pull_finger")
                            or (pa == "upper_pull_finger" and pb == "hang")):
                conflicts.append({"pair": (a.get("slot"), b.get("slot")), "rule": "R4",
                                  "message": "指力板与攀岩需间隔 ≥48h（双向）"})

    for d, sess in by_date.items():
        if len(sess) < 2:
            continue
        rpe_sum = sum((s.get("rpe") or 0) for s in sess)
        if rpe_sum >= 15:
            conflicts.append({"pair": (d, "次日"), "rule": "R15",
                              "message": f"同日双课 RPE 之和 {rpe_sum} ≥ 15 → 次日不排质量课"})
    return conflicts

def validate_week_plan(week: dict, ctx: dict, cfg: dict) -> list[dict]:
    results = []
    sessions_all = []
    quality_count = 0
    rest_zero_days = 0
    weekly_au = 0.0
    long_au = 0.0
    low_au = 0.0
    total_au = 0.0

    for day, d in week.items():
        sess = d.get("sessions", []) if isinstance(d, dict) else []
        if not sess:
            rest_zero_days += 1
        day_au = 0.0
        for s in sess:
            s = dict(s)
            s["date"] = day
            s["slot"] = s.get("slot", "")
            au = s.get("planned_au") or 0
            rpe = s.get("planned_rpe") or 0
            day_au += au
            total_au += au
            sessions_all.append(s)
            if s.get("slot") in ("primary_quality", "primary_quality_2"):
                quality_count += 1
            if s.get("slot") == "primary_long":
                long_au += au
            if (rpe or 0) <= 4:
                low_au += au
        weekly_au += day_au

        chronic = ctx.get("chronic_load")
        if chronic and day_au > chronic * cfg["fitness"]["r1_daily_cap_ratio"]:
            results.append({"rule": "R1", "level": "REJECT",
                            "evidence": f"{day}: {day_au} AU > {round(chronic * cfg['fitness']['r1_daily_cap_ratio'], 1)} AU",
                            "message": f"{day} 负荷超过单日上限"})

    q_max = 2 if ctx.get("cut_mode") else 3
    if quality_count > q_max:
        results.append({"rule": "R2", "level": "REJECT",
                        "evidence": f"质量课 {quality_count} > {q_max}",
                        "message": "质量课数量超限"})

    rest_min = 2 if ctx.get("cut_mode") else 1
    if rest_zero_days < rest_min:
        results.append({"rule": "R3", "level": "REJECT",
                        "evidence": f"零负荷日 {rest_zero_days} < {rest_min}",
                        "message": "零负荷日不足"})

    for c in interference_conflicts(sessions_all, ctx.get("cut_mode", False)):
        if c["rule"] == "R4":
            results.append({"rule": "R4", "level": "REJECT", "evidence": str(c["pair"]),
                            "message": c["message"]})

    gates_active = ctx.get("gates_active") or []
    if gates_active and quality_count > 0:
        results.append({"rule": "R5", "level": "REJECT",
                        "evidence": f"闸门 {gates_active} 激活期间有 {quality_count} 节质量课",
                        "message": "闸门激活期间禁止质量课"})

    if total_au > 0 and long_au > 0.30 * total_au:
        results.append({"rule": "R6", "level": "WARN",
                        "evidence": f"长距离 {long_au} AU 占 {round(long_au/total_au*100)}%",
                        "message": "长距离占比超 30%"})

    if total_au > 0 and low_au / total_au < 0.75:
        results.append({"rule": "R7", "level": "WARN",
                        "evidence": f"低强度占比 {round(low_au/total_au*100)}% < 75%",
                        "message": "低强度（极化分布）占比不足"})

    prev = ctx.get("prev_week_load")
    band = ctx.get("acwr_band", "green")
    if prev and prev > 0:
        growth = (total_au - prev) / prev
        if (band == "green" and growth > 0.08) or (band in ("yellow", "red") and growth > 0):
            results.append({"rule": "R8", "level": "WARN",
                            "evidence": f"周负荷增幅 {round(growth*100)}%（{band}）",
                            "message": "周负荷增幅超限"})

    heavy_days = ctx.get("cycle_heavy_flow_dates") or set()
    if ctx.get("history_heavy_cramps"):
        for s in sessions_all:
            if s["date"] in heavy_days and (s.get("planned_rpe") or 0) >= 7:
                results.append({"rule": "R9", "level": "WARN",
                                "evidence": f"{s['date']} 强度课落在预测 heavy flow 日",
                                "message": "强度课落在预测经量高峰日（历史痛经）"})
                break

    target = ctx.get("template_target_au")
    if target and target > 0 and abs(total_au - target) / target > 0.15:
        results.append({"rule": "R10", "level": "WARN",
                        "evidence": f"周总量 {round(total_au)} vs 模板 {round(target)}",
                        "message": "周总量偏离模板目标超 15%（LLM 裁量越界）"})

    if ctx.get("week_index_in_block") == 4 and not ctx.get("block_reduced"):
        results.append({"rule": "R11", "level": "WARN",
                        "evidence": "第 4 周未减量 −30~40%",
                        "message": "4 周循环第 4 周应减量"})

    if gates_active and not ctx.get("gate_load_reduced"):
        results.append({"rule": "R12", "level": "WARN",
                        "evidence": "闸门激活期间周总量未按动作包降级",
                        "message": "闸门激活期间应下调周总量"})

    inj = ctx.get("injury_constraints") or {}
    forbid = inj.get("forbid") or []
    limit = inj.get("limit") or {}
    for s in sessions_all:
        if s.get("pattern") in forbid:
            results.append({"rule": "R13", "level": "REJECT",
                            "evidence": f"禁排项 {s.get('pattern')}",
                            "message": f"违反伤病约束（禁止 {s.get('pattern')}）"})
            break
        if limit.get("climb") == "volume_only" and s.get("sport_id") == "climb" and s.get("intensity") == "high":
            results.append({"rule": "R13", "level": "REJECT",
                            "evidence": "攀岩限容量却排强度",
                            "message": "伤病约束：攀岩仅限容量（禁强度/指力）"})
            break

    cal = ctx.get("calendar_grid") or {}
    windows = cal.get("windows", [])
    if windows:
        for s in sessions_all:
            venue = s.get("venue_need")
            if venue and not any(w.get("venue") == venue for w in windows):
                results.append({"rule": "R14", "level": "WARN",
                                "evidence": f"槽位 {s.get('slot')} 需要 {venue} 但无可用窗口",
                                "message": "槽位落在可用时间窗口之外"})
                break

    r15 = [c for c in interference_conflicts(sessions_all, ctx.get("cut_mode", False)) if c["rule"] == "R15"]
    if r15:
        days = sorted(week.keys())
        for c in r15:
            d = c["pair"][0]
            if d in days:
                idx = days.index(d)
                if idx + 1 < len(days):
                    nxt = week[days[idx + 1]].get("sessions", [])
                    if any(s.get("slot") in ("primary_quality", "primary_quality_2") for s in nxt):
                        results.append({"rule": "R15", "level": "WARN",
                                        "evidence": f"{d} 双课 RPE 和 ≥15，次日有质量课",
                                        "message": "同日双课 RPE 和 ≥15 时次日不应有质量课"})
                        break

    meso = ctx.get("mesocycle_states") or {}
    prog_skill = [sid for sid, st in meso.items() if st == "progression" and sid not in ("strength",)]
    if len(prog_skill) > 1:
        results.append({"rule": "R16", "level": "REJECT",
                        "evidence": f"同时峰化：{prog_skill}",
                        "message": "同一周最多 1 个技能型运动处于 progression"})

    if ctx.get("below_floor_prev_week"):
        floor = ctx.get("maintenance_floor") or {}
        for sport_id, min_s in floor.items():
            cnt = sum(1 for s in sessions_all if s.get("sport_id") == sport_id)
            if cnt < min_s:
                results.append({"rule": "R17", "level": "WARN",
                                "evidence": f"{sport_id} 本周 {cnt} < {min_s}",
                                "message": f"连续 2 周低于 {sport_id} 最低频率底线"})

    return results

def summarize_validation(results: list[dict]) -> tuple[str, list[str]]:
    if any(r["level"] == "REJECT" for r in results):
        return "REJECT", [r["message"] for r in results]
    warns = [r["message"] for r in results if r["level"] == "WARN"]
    if warns:
        return "WARN+PASS", warns
    return "PASS", []
