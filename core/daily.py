from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from .engine import (
    compute_fitness,
    compute_acwr,
    compute_readiness,
    evaluate_training_forecast,
    evaluate_reds,
)
from . import cycle as cyclemod
from . import sleep_health
from . import store_ext

def _iso(d) -> str:
    return d.isoformat()

def _loads_by_day(conn, end_day: date, lookback: int = 120) -> dict:
    start = end_day - timedelta(days=lookback)
    rows = conn.execute(
        "SELECT substr(start_time,1,10) AS d, COALESCE(SUM(load_final),0) AS load "
        "FROM activities WHERE start_time >= ? AND start_time <= ? "
        "GROUP BY substr(start_time,1,10)",
        (_iso(start), _iso(end_day)),
    ).fetchall()
    srows = conn.execute(
        "SELECT date AS d, COALESCE(SUM(srpe_au),0) AS load FROM strength_sessions "
        "WHERE date >= ? AND date <= ? GROUP BY date",
        (_iso(start), _iso(end_day)),
    ).fetchall()
    out = {}
    for r in rows:
        out[r["d"]] = out.get(r["d"], 0.0) + (r["load"] or 0.0)
    for r in srows:
        out[r["d"]] = out.get(r["d"], 0.0) + (r["load"] or 0.0)
    return out

def _load_series(conn, end_day: date, lookback: int = 120) -> list[float]:
    by_day = _loads_by_day(conn, end_day, lookback)
    series = []
    d = end_day - timedelta(days=lookback)
    while d <= end_day:
        series.append(round(by_day.get(_iso(d), 0.0), 1))
        d += timedelta(days=1)
    return series

def _rolling_dev(conn, table_col: str, end_day: date, window: int = 14,
                 ref_col: str | None = None) -> list[tuple[str, float | None, float | None]]:
    """返回 [(date, 主偏离%, 参考偏离%)]。

    主偏离用轮动 window 天均值作基线；参考偏离用 ref_col（如 Garmin balanced）
    作基线，仅作对照，不参与训练预测判定。
    """
    start = end_day - timedelta(days=window * 2)
    cols = f"{table_col} AS v" + (f", {ref_col} AS ref" if ref_col else "")
    rows = conn.execute(
        f"SELECT date, {cols} FROM daily_metrics WHERE date >= ? AND date <= ? ORDER BY date",
        (_iso(start), _iso(end_day)),
    ).fetchall()
    vals = [(r["date"], r["v"], r["ref"] if ref_col else None) for r in rows
            if r["v"] is not None]
    out = []
    for i, (d, v, ref) in enumerate(vals):
        prior = [x[1] for x in vals[max(0, i - window):i] if x[1] is not None]
        dev = None
        if prior:
            base = sum(prior) / len(prior)
            dev = round((v - base) / base * 100, 2) if base else None
        ref_dev = round((v - ref) / ref * 100, 2) if ref else None
        out.append((d, dev, ref_dev))
    return out

def _metric_dev_series(conn, end_day: date) -> tuple[list, list]:
    hrv = _rolling_dev(conn, "hrv_last_night", end_day, ref_col="hrv_baseline")
    rhr = _rolling_dev(conn, "resting_hr", end_day)
    return hrv, rhr

def _cycle_flags(conn, end_day: date, cfg: dict) -> dict:
    periods = [dict(r) for r in conn.execute(
        "SELECT start_date, end_date FROM cycle_periods ORDER BY start_date").fetchall()]
    return cyclemod.evaluate_flags(periods, end_day, cfg)

def compute_engine_daily(conn, cfg: dict, day: str) -> dict:
    end = date.fromisoformat(day[:10])
    series = _load_series(conn, end)
    fit = compute_fitness(series, cfg["fitness"]["tau_ctl"], cfg["fitness"]["tau_atl"])
    last = fit[-1]
    ctl, atl, tsb = last["ctl"], last["atl"], last["tsb"]
    first_load_idx = next((i for i, l in enumerate(series) if l and l > 0), None)
    warmup = (first_load_idx is None) or ((len(series) - 1 - first_load_idx) < 28)

    acwr_info = compute_acwr(series)
    chronic_floor = cfg["fitness"]["chronic_floor_acwr"]
    acwr = None
    ewma7 = ewma28 = None
    if acwr_info and not warmup and acwr_info["ewma28"] >= chronic_floor:
        acwr = acwr_info["acwr"]
        ewma7, ewma28 = acwr_info["ewma7"], acwr_info["ewma28"]

    dm = conn.execute("SELECT * FROM daily_metrics WHERE date=?", (day,)).fetchone()
    sub = conn.execute("SELECT * FROM subjective WHERE date=?", (day,)).fetchone()

    hrv_series, rhr_series = _metric_dev_series(conn, end)
    hrv_dev = next((dev for d, dev, _ in hrv_series if d == day), None)
    hrv_dev_ref = next((ref for d, _, ref in hrv_series if d == day), None)
    rhr_dev = next((dev for d, dev, _ in rhr_series if d == day), None)
    hrv_dev_hist = [dev for _, dev, _ in hrv_series if dev is not None]
    rhr_dev_hist = [dev for _, dev, _ in rhr_series if dev is not None]

    sleep_h = dm["sleep_hours"] if dm else None
    soreness = sub["soreness"] if sub else None
    sleep_feel = sub["sleep_feel"] if sub else None

    # 睡眠状态分：把睡眠折算成「今天恢复到什么程度」，替代单一时长阈值
    try:
        sleep_state = sleep_health.evaluate(conn, cfg, day, sleep_feel)
    except Exception:  # noqa: BLE001
        sleep_state = {"date": day, "score": None, "confidence": "low", "dimensions": {},
                       "personal_need_h": None, "notes": ["睡眠模块计算失败"]}
    sleep_state["percentile_28d"] = sleep_health.percentile(conn, day, sleep_state.get("score"))
    sleep_state["g8"] = sleep_health.g8_status(conn, cfg, day, sleep_state.get("score"))

    weights = cfg["readiness"]["weights"]
    readiness = compute_readiness(hrv_dev, tsb, sleep_h, rhr_dev, soreness, sleep_feel, weights,
                                  sleep_score=sleep_state.get("score"))

    cycle_flags = _cycle_flags(conn, end, cfg)

    injury_rows = conn.execute(
        "SELECT * FROM injury_history WHERE status IN ('active','restricted')").fetchall()
    injury_active = len(injury_rows) > 0
    injury_constraints = {}
    for r in injury_rows:
        if r["constraint_json"]:
            try:
                injury_constraints.update(json.loads(r["constraint_json"]))
            except Exception:
                pass

    red_s_state = conn.execute(
        "SELECT value FROM meta WHERE key='red_s_state'").fetchone()
    red_s_active = red_s_state is not None and red_s_state["value"] == "active"

    cramps = None
    cd = conn.execute("SELECT * FROM cycle_daily WHERE date=?", (day,)).fetchone()
    if cd and cd["symptoms"]:
        try:
            symptoms = json.loads(cd["symptoms"])
            for sym in symptoms:
                if isinstance(sym, dict) and sym.get("type") == "cramps":
                    cramps = sym.get("severity")
        except Exception:
            pass

    forecast_ctx = {
        "tsb": tsb, "acwr": acwr, "readiness": readiness["score"],
        "sleep_hours": sleep_h, "sleep_state_score": sleep_state.get("score"),
        "sleep_g8_mode": sleep_state["g8"]["mode"],
        "sleep_g8_enforced": sleep_state["g8"]["enforced"],
        "hrv_dev_history": hrv_dev_hist,
        "hrv_dev_today": hrv_dev,
        "rhr_dev_history": rhr_dev_hist,
        "luteal_phase_confound": cycle_flags.get("luteal_phase_confound", False),
        "injury_active": injury_active, "red_s_active": red_s_active,
        "cramps_today": cramps,
    }
    forecast = evaluate_training_forecast(forecast_ctx, cfg)

    reds_ctx = {
        "hrv_dev_history": hrv_dev_hist, "rhr_dev_history": rhr_dev_hist,
        "cycle_irregular": cycle_flags.get("cycle_irregular", False),
        "cycle_missed": cycle_flags.get("cycle_missed", False),
    }
    reds = evaluate_reds(reds_ctx, cfg)

    flags = []
    if warmup:
        flags.append("warmup")
    if cycle_flags.get("luteal_phase_confound"):
        flags.append("luteal_phase_confound")

    row = {
        "date": day, "ctl": ctl, "atl": atl, "tsb": tsb, "acwr": acwr,
        "ewma7": ewma7, "ewma28": ewma28,
        "readiness_score": readiness["score"], "readiness_confidence": readiness["confidence"],
        "pillars": json.dumps(readiness["pillars"], ensure_ascii=False),
        "inputs_used": json.dumps(forecast_ctx, ensure_ascii=False, default=str),
        # 列名沿用 v1.7 的 gates_triggered（历史兼容），内容即训练预测项
        "gates_triggered": json.dumps(forecast, ensure_ascii=False),
        "flags": json.dumps(flags, ensure_ascii=False),
        "param_version": cfg["_root"] and "p2026-10",
        "calc_version": "1.3",
    }
    cols = list(row.keys())
    conn.execute(
        f"INSERT INTO engine_daily({','.join(cols)}) VALUES({','.join('?' for _ in cols)}) "
        "ON CONFLICT(date) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in cols if c != "date"),
        [row[c] for c in cols],
    )
    conn.commit()
    # 睡眠状态分入 garmin_metrics，便于算个人百分位与趋势
    if sleep_state.get("score") is not None:
        try:
            metrics = {"sleep_state_score": sleep_state["score"]}
            for k, v in (sleep_state.get("dimensions") or {}).items():
                if v is not None:
                    metrics[f"sleep_dim_{k}"] = v
            for k in ("sri", "midpoint_dev_h", "debt_7d_h", "efficiency_pct", "waso_min"):
                if sleep_state.get(k) is not None:
                    metrics[f"sleep_{k}"] = sleep_state[k]
            g8 = sleep_state.get("g8") or {}
            metrics["sleep_g8_p10"] = g8.get("p10")
            metrics["sleep_g8_threshold"] = g8.get("threshold")
            metrics["sleep_g8_triggered"] = 1.0 if g8.get("triggered") else 0.0
            metrics["sleep_g8_enforced"] = 1.0 if g8.get("enforced") else 0.0
            store_ext.upsert_metrics(conn, day, metrics)
        except Exception:  # noqa: BLE001
            pass
    row["pillars"] = readiness["pillars"]
    row["training_forecast"] = forecast
    row["sleep_health"] = sleep_state
    row["flags"] = flags
    row["hrv_dev_pct"] = hrv_dev
    row["hrv_dev_ref_pct"] = hrv_dev_ref
    row["rhr_dev_pct"] = rhr_dev
    return row

def build_daily_brief(conn, cfg: dict, day: str | None = None) -> dict:
    day = day or date.today().isoformat()
    eng = compute_engine_daily(conn, cfg, day)
    dm = conn.execute("SELECT * FROM daily_metrics WHERE date=?", (day,)).fetchone()
    sub = conn.execute("SELECT * FROM subjective WHERE date=?", (day,)).fetchone()
    body = conn.execute(
        "SELECT weight_7d_avg FROM body_metrics ORDER BY measured_at DESC LIMIT 1").fetchone()
    periods = [dict(r) for r in conn.execute(
        "SELECT start_date, end_date FROM cycle_periods ORDER BY start_date").fetchall()]
    cycle = cyclemod.predict(date.fromisoformat(day), periods)

    sub_rows = conn.execute(
        "SELECT date FROM subjective ORDER BY date DESC LIMIT 1").fetchone()
    last_input = sub_rows["date"] if sub_rows else None
    streak = _streak_days(conn, day)
    escalation = _escalation(conn, cfg, day, streak)

    recent = [dict(r) for r in conn.execute(
        "SELECT sport_id, substr(start_time,1,10) AS date, duration_min, rpe, srpe_au, "
        "hr_trimp, load_final, load_path_used, intensity_domain, mismatch_flag "
        "FROM activities WHERE start_time <= ? ORDER BY start_time DESC LIMIT 10",
        (day,)).fetchall()]
    recent.reverse()

    strength = [dict(r) for r in conn.execute(
        "SELECT exercise_id, pattern, srpe_au, vi, mismatch_flag FROM strength_sessions "
        "WHERE date = ?", (day,)).fetchall()]

    plan_ctx = _plan_context(conn, day)

    return {
        "date": day,
        "recovery": {
            "sleep_hours": dm["sleep_hours"] if dm else None,
            "sleep_stages": {
                "deep": dm["sleep_deep_h"] if dm else None,
                "light": dm["sleep_light_h"] if dm else None,
                "rem": dm["sleep_rem_h"] if dm else None,
                "awake": dm["sleep_awake_h"] if dm else None,
            },
            "sleep_window": {
                "start": dm["sleep_start"] if dm else None,
                "end": dm["sleep_end"] if dm else None,
                "mid": dm["sleep_mid"] if dm else None,
            },
            "hrv_last_night": dm["hrv_last_night"] if dm else None,
            "hrv_dev_pct": eng.get("hrv_dev_pct"),
            "hrv_dev_ref_pct": eng.get("hrv_dev_ref_pct"),
            "hrv_baseline_source": "rolling14",
            "hrv_ref_source": "garmin_balanced",
            "resting_hr": dm["resting_hr"] if dm else None,
            "stress_daily_avg": dm["stress_daily_avg"] if dm else None,
            "data_status": dm["data_status"] if dm else "missing",
            # v1.8：插件全量采集的明细（无数据时为 None，不臆造）
            "sleep_detail": _sleep_detail(conn, day),
            "overnight_series": _overnight_summary(conn, day),
            "garmin_official": _garmin_metrics(conn, day),
            # v1.9：睡眠状态分（个人基线；决定当天训练安排的主要睡眠依据）
            "sleep_health": eng.get("sleep_health"),
        },
        "data_extensions": {"schema": "v1.9-ext", "engine": "1.3"},
        "cycle": {
            "phase": cycle["phase"], "cycle_day": cycle["cycle_day"],
            "days_to_predicted_period": cycle["days_to_predicted_period"],
            "prediction_confidence": cycle["prediction_confidence"],
            "source": "user", "contraception_mode": _contraception(conn),
        },
        "self_computed": {
            "readiness_score": eng["readiness_score"],
            "confidence": eng["readiness_confidence"],
            "pillars": eng["pillars"],
            "ctl": eng["ctl"], "atl": eng["atl"], "tsb": eng["tsb"], "acwr": eng["acwr"],
            "training_forecast": eng["training_forecast"], "flags": eng["flags"],
        },
        "recent_activities": recent,
        "strength_sessions": strength,
        "subjective": {
            "soreness": sub["soreness"] if sub else None,
            "sleep_feel": sub["sleep_feel"] if sub else None,
            "injury_flag": bool(sub["injury_flag"]) if sub else False,
            "streak_days": streak,
        },
        "body": {"weight_7d_avg": body["weight_7d_avg"] if body else None},
        "plan_context": plan_ctx,
        "data_quality": {
            "hr_source": _hr_source(conn),
            "logging": {"subjective_streak_days": streak, "last_input": last_input,
                        "escalation_level": escalation},
            "obs_state_notes": [],
            "notes": [],
        },
        "versions": {"engine": "1.3", "schema": "1.7", "param": "p2026-10"},
    }

def _streak_days(conn, day: str) -> int:
    d = date.fromisoformat(day)
    n = 0
    while True:
        row = conn.execute("SELECT 1 FROM subjective WHERE date=?", (d.isoformat(),)).fetchone()
        if row:
            n += 1
            d -= timedelta(days=1)
        else:
            break
    return n

def _escalation(conn, cfg: dict, day: str, streak: int) -> str:
    f = cfg["logging"]
    if streak >= f["l3_subjective_days"]:
        return "L3"
    if streak >= f["l2_subjective_days"]:
        return "L2"
    if streak >= f["l1_subjective_days"]:
        return "L1"
    return "L0"

def _contraception(conn) -> str:
    row = conn.execute("SELECT contraception FROM athlete_profile WHERE id=1").fetchone()
    return row["contraception"] if row and row["contraception"] else "none"

def _hr_source(conn) -> str:
    row = conn.execute("SELECT hr_rest_source FROM athlete_profile WHERE id=1").fetchone()
    return "wrist" if row and row["hr_rest_source"] else "unknown"

def _plan_context(conn, day: str) -> dict:
    d = date.fromisoformat(day)
    week_start = (d - timedelta(days=d.weekday())).isoformat()
    plan = conn.execute(
        "SELECT * FROM weekly_plans WHERE week_start=? AND status='active'", (week_start,)).fetchone()
    if not plan:
        plan = conn.execute(
            "SELECT * FROM weekly_plans WHERE week_start=? AND status='draft'", (week_start,)).fetchone()
    if not plan:
        return {"week": f"{d.isocalendar().year}-W{d.isocalendar().week:02d}",
                "template_id": None, "planned_au": None, "actual_au": None,
                "completion_pct": None, "mesocycle_states": {}}
    plan_json = json.loads(plan["plan"]) if isinstance(plan["plan"], str) else plan["plan"]
    planned = sum(s.get("planned_au", 0) for dd in plan_json.get("days", {}).values()
                  for s in dd.get("sessions", []))
    actual = _loads_by_day(conn, d, 6).get(day, 0.0)
    return {
        "week": f"{d.isocalendar().year}-W{d.isocalendar().week:02d}",
        "template_id": plan["template_id"],
        "planned_au": planned,
        "actual_au": round(actual, 1),
        "completion_pct": round(actual / planned, 2) if planned else None,
        "mesocycle_states": _meso_states(conn),
    }

def _meso_states(conn) -> dict:
    rows = conn.execute(
        "SELECT sport_id, mesocycle_state FROM sport_registry WHERE active=1").fetchall()
    return {r["sport_id"]: r["mesocycle_state"] for r in rows}


# ── v1.8：插件全量数据的摘要（表不存在或没有数据时一律返回 None） ─────────

def _sleep_detail(conn, day: str) -> dict | None:
    """夜间分段：只给"何时进入深睡/REM、醒了几次"这类教练需要的事件级摘要。"""
    try:
        rows = conn.execute(
            "SELECT stage, start_local, end_local, seconds FROM sleep_stages "
            "WHERE date=? ORDER BY seq", (day,)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    counts, firsts = {}, {}
    for r in rows:
        counts[r["stage"]] = counts.get(r["stage"], 0) + 1
        firsts.setdefault(r["stage"], r["start_local"])
    longest_deep = max((r for r in rows if r["stage"] == "deep"), key=lambda r: r["seconds"], default=None)
    last = rows[-1]
    return {
        "sleep_start": rows[0]["start_local"],
        "sleep_end": last["end_local"],
        "segments": len(rows),
        "segment_count_by_stage": counts,
        "first_deep": firsts.get("deep"),
        "first_rem": firsts.get("rem"),
        "longest_deep_min": round(longest_deep["seconds"] / 60) if longest_deep else None,
        "awake_segments": counts.get("awake", 0),
        "awake_min": round(sum(r["seconds"] for r in rows if r["stage"] == "awake") / 60, 1),
    }


def _overnight_summary(conn, day: str) -> dict | None:
    """夜间分钟级序列的汇总（心率/压力/身体电量/HRV/呼吸）。"""
    try:
        rows = conn.execute(
            "SELECT kind, MIN(value), MAX(value), ROUND(AVG(value), 1), COUNT(*) "
            "FROM sleep_series WHERE date=? GROUP BY kind", (day,)).fetchall()
        hrv = conn.execute(
            "SELECT COUNT(*), MIN(hrv_value), MAX(hrv_value), ROUND(AVG(hrv_value), 1) "
            "FROM hrv_readings WHERE date=?", (day,)).fetchone()
        stress = conn.execute(
            "SELECT COUNT(*) FROM stress_series WHERE date=?", (day,)).fetchone()[0]
        battery = conn.execute(
            "SELECT COUNT(*) FROM body_battery_series WHERE date=?", (day,)).fetchone()[0]
    except Exception:  # noqa: BLE001
        return None
    if not rows and (not hrv or hrv[0] == 0):
        return None
    out = {"series": {r[0]: {"min": r[1], "max": r[2], "avg": r[3], "points": r[4]} for r in rows}}
    if hrv and hrv[0]:
        out["hrv_readings"] = {"points": hrv[0], "min": hrv[1], "max": hrv[2], "avg": hrv[3]}
    out["day_stress_points"] = stress
    out["body_battery_points"] = battery
    return out


def _garmin_metrics(conn, day: str) -> dict | None:
    """Garmin 官方指标（隔离层：只作为参考，不参与自算准备度）。"""
    try:
        rows = conn.execute(
            "SELECT metric, value, unit FROM garmin_metrics WHERE date=?", (day,)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    return {r["metric"]: r["value"] for r in rows}
