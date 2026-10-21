from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import db as dbmod
from . import learning
from .engine import compute_activity_load, intensity_domain, validate_week_plan, summarize_validation
from . import daily as dailymod
from . import cycle as cyclemod

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

def _profile(conn):
    return conn.execute("SELECT * FROM athlete_profile WHERE id=1").fetchone()

def _luteal_today(conn, day: str) -> bool:
    periods = [dict(r) for r in conn.execute(
        "SELECT start_date, end_date FROM cycle_periods ORDER BY start_date").fetchall()]
    return cyclemod.is_luteal(date.fromisoformat(day[:10]), periods)

def insert_activity(conn, cfg: dict, act: dict) -> int:
    start = (act.get("start_time") or "")[:10]
    prof = _profile(conn)
    hr_rest = prof["hr_rest"] if prof else None
    hr_max = prof["hr_max"] if prof else None
    sport_id = act.get("sport_id") or "other"
    reg = conn.execute("SELECT load_path FROM sport_registry WHERE sport_id=?", (sport_id,)).fetchone()
    load_path = reg["load_path"] if reg else "srpe"
    hr_cross = load_path == "srpe+hr_cross"
    k_bridge, _ = learning.k_bridge_for(conn, sport_id, cfg)

    rpe = act.get("rpe")
    duration = act.get("duration_min")
    load = compute_activity_load(rpe, duration, act.get("avg_hr"), hr_rest, hr_max,
                                 k_bridge, hr_cross, _luteal_today(conn, start), cfg)
    domain = intensity_domain(rpe=rpe)

    gid = act.get("garmin_activity_id") or act.get("fit_path") or f"manual:{start}:{sport_id}"
    params = (
        gid, act.get("fit_path"), start, sport_id, duration,
        act.get("distance_km"), act.get("avg_hr"), act.get("max_hr"),
        json.dumps(act.get("hr_zones_pct")) if act.get("hr_zones_pct") else None,
        act.get("avg_pace_min_km"), act.get("elevation_gain_m"),
        act.get("power_avg_w"), act.get("power_max_w"),
        json.dumps(act.get("dynamics")) if act.get("dynamics") else None,
        act.get("indoor"), act.get("annotation"),
        rpe, load["srpe_au"], load["hr_trimp"], load["load_final"],
        load["load_path_used"], domain, load["mismatch_flag"],
        "1.3", cfg["_root"] and "p2026-10", _now(),
    )
    conn.execute(
        "INSERT INTO activities(garmin_activity_id, fit_path, start_time, sport_id, duration_min, "
        "distance_km, avg_hr, max_hr, hr_zones_pct, avg_pace_min_km, elevation_gain_m, "
        "power_avg_w, power_max_w, dynamics, indoor, annotation, "
        "rpe, srpe_au, hr_trimp, load_final, load_path_used, intensity_domain, mismatch_flag, "
        "calc_version, param_version, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(garmin_activity_id) DO UPDATE SET "
        "start_time=excluded.start_time, sport_id=excluded.sport_id, duration_min=excluded.duration_min, "
        "distance_km=excluded.distance_km, avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, "
        "hr_zones_pct=excluded.hr_zones_pct, avg_pace_min_km=excluded.avg_pace_min_km, "
        "elevation_gain_m=excluded.elevation_gain_m, power_avg_w=excluded.power_avg_w, "
        "power_max_w=excluded.power_max_w, dynamics=excluded.dynamics, indoor=excluded.indoor, "
        "annotation=excluded.annotation, rpe=excluded.rpe, srpe_au=excluded.srpe_au, "
        "hr_trimp=excluded.hr_trimp, load_final=excluded.load_final, "
        "load_path_used=excluded.load_path_used, intensity_domain=excluded.intensity_domain, "
        "mismatch_flag=excluded.mismatch_flag, calc_version=excluded.calc_version, "
        "param_version=excluded.param_version, updated_at=excluded.created_at",
        params,
    )
    conn.commit()
    row = conn.execute("SELECT id FROM activities WHERE garmin_activity_id=?", (gid,)).fetchone()
    try:
        dailymod.compute_engine_daily(conn, cfg, start)
    except Exception:
        pass
    return row["id"]

def log_activity_rpe(conn, cfg: dict, day: str, sport_id: str, rpe: int) -> int:
    row = conn.execute(
        "SELECT id FROM activities WHERE substr(start_time,1,10)=? AND sport_id=? "
        "ORDER BY start_time DESC LIMIT 1", (day, sport_id)).fetchone()
    if not row:
        raise LookupError(f"{day} 没有 {sport_id} 的活动，无法录入 RPE")
    prof = _profile(conn)
    act = conn.execute("SELECT * FROM activities WHERE id=?", (row["id"],)).fetchone()
    k_bridge, _ = learning.k_bridge_for(conn, sport_id, cfg)
    reg = conn.execute("SELECT load_path FROM sport_registry WHERE sport_id=?", (sport_id,)).fetchone()
    hr_cross = (reg["load_path"] if reg else "srpe") == "srpe+hr_cross"
    load = compute_activity_load(rpe, act["duration_min"], act["avg_hr"],
                                 prof["hr_rest"] if prof else None,
                                 prof["hr_max"] if prof else None,
                                 k_bridge, hr_cross, _luteal_today(conn, day), cfg)
    conn.execute(
        "UPDATE activities SET rpe=?, srpe_au=?, load_final=?, load_path_used=?, "
        "intensity_domain=?, mismatch_flag=?, updated_at=? WHERE id=?",
        (rpe, load["srpe_au"], load["load_final"], load["load_path_used"],
         intensity_domain(rpe=rpe), load["mismatch_flag"], _now(), row["id"]))
    conn.commit()
    dailymod.compute_engine_daily(conn, cfg, day)
    return row["id"]

def upsert_subjective(conn, day: str, soreness, sleep_feel, note, injury_flag: bool,
                      alcohol_units=None, caffeine_mg=None, stress=None) -> None:
    conn.execute(
        "INSERT INTO subjective(date, soreness, sleep_feel, note, injury_flag, created_at, "
        "alcohol_units, caffeine_mg, stress) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET soreness=excluded.soreness, "
        "sleep_feel=excluded.sleep_feel, note=excluded.note, "
        "injury_flag=excluded.injury_flag, created_at=excluded.created_at, "
        "alcohol_units=COALESCE(excluded.alcohol_units, subjective.alcohol_units), "
        "caffeine_mg=COALESCE(excluded.caffeine_mg, subjective.caffeine_mg), "
        "stress=COALESCE(excluded.stress, subjective.stress)",
        (day, soreness, sleep_feel, note, int(bool(injury_flag)), _now(),
         alcohol_units, caffeine_mg, stress))
    conn.commit()

def log_strength_row(conn, cfg: dict, day, exercise_id, pattern, sets, reps, duration_s,
                     weight_kg, added_kg, rpe, note) -> dict:
    duration_min = round(duration_s / 60.0, 2) if duration_s else (
        round(sets * reps * 2.5 / 60.0, 2) if sets and reps else None)
    srpe = round(rpe * duration_min, 1) if duration_min else None
    vol_proxy = round((sets or 0) * (reps or 0) * (weight_kg or 0) + (added_kg or 0), 2)
    base = conn.execute("SELECT volume_proxy_mean FROM strength_baseline WHERE exercise_id=?",
                        (exercise_id,)).fetchone()
    vi_ref = base["volume_proxy_mean"] if base else None
    mismatch = None
    if vi_ref:
        if vol_proxy >= 1.2 * vi_ref and rpe <= 4:
            mismatch = "cap_high_feel_low"
        elif vol_proxy <= 0.8 * vi_ref and rpe >= 7:
            mismatch = "cap_low_feel_high"
    conn.execute(
        "INSERT INTO strength_sessions(date, exercise_id, pattern, sets, reps, duration_s, "
        "weight_kg, added_kg, rpe, srpe_au, vi, vi_ref, mismatch_flag, notes, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (day, exercise_id, pattern, sets, reps, duration_s, weight_kg, added_kg, rpe,
         srpe, vol_proxy, vi_ref, mismatch, note, _now()))
    stats = conn.execute(
        "SELECT AVG(vi) AS m, COUNT(*) AS n FROM strength_sessions WHERE exercise_id=?",
        (exercise_id,)).fetchone()
    if stats and stats["m"] is not None:
        conn.execute(
            "INSERT INTO strength_baseline(exercise_id, volume_proxy_mean, n_sessions, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(exercise_id) DO UPDATE SET "
            "volume_proxy_mean=excluded.volume_proxy_mean, n_sessions=excluded.n_sessions, "
            "updated_at=excluded.updated_at",
            (exercise_id, round(stats["m"], 2), stats["n"], _now()))
    conn.commit()
    dailymod.compute_engine_daily(conn, cfg, day)
    return {"srpe_au": srpe, "vi": vol_proxy, "mismatch_flag": mismatch}

def log_body_metrics_row(conn, weight_kg, measured_at, body_fat_pct) -> dict:
    ts = measured_at or _now()
    avg = _weight_7d_avg(conn, ts, weight_kg)
    conn.execute(
        "INSERT INTO body_metrics(measured_at, weight_kg, weight_7d_avg, body_fat_pct) "
        "VALUES(?,?,?,?) ON CONFLICT(measured_at) DO UPDATE SET "
        "weight_kg=excluded.weight_kg, weight_7d_avg=excluded.weight_7d_avg, "
        "body_fat_pct=excluded.body_fat_pct",
        (ts, weight_kg, avg, body_fat_pct))
    conn.commit()
    return {"weight_7d_avg": avg}

def _weight_7d_avg(conn, ts: str, weight_kg: float) -> float:
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    start = (t - timedelta(days=6)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT weight_kg FROM body_metrics WHERE measured_at >= ? AND measured_at <= ?",
        (start, ts)).fetchall()
    vals = [r["weight_kg"] for r in rows if r["weight_kg"] is not None]
    vals.append(weight_kg)
    return round(sum(vals) / len(vals), 2)

def log_injury_row(conn, body_part, side, onset_date, status, constraint_json, note) -> int:
    cur = conn.execute(
        "INSERT INTO injury_history(body_part, side, onset_date, status, constraint_json, note, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (body_part, side, onset_date, status,
         json.dumps(constraint_json, ensure_ascii=False) if constraint_json else None,
         note, _now()))
    conn.commit()
    return cur.lastrowid

def log_cycle_period_row(conn, action: str, day: str) -> None:
    if action == "start":
        conn.execute("UPDATE cycle_periods SET end_date=? WHERE end_date IS NULL", (day,))
        conn.execute(
            "INSERT INTO cycle_periods(start_date, source, created_at) VALUES(?,?,?)",
            (day, "user", _now()))
    elif action == "end":
        conn.execute(
            "UPDATE cycle_periods SET end_date=? WHERE end_date IS NULL", (day,))
    conn.commit()

def log_cycle_daily_row(conn, day: str, flow, symptoms, bbt_c, note) -> None:
    conn.execute(
        "INSERT INTO cycle_daily(date, flow, symptoms, bbt_c, note, created_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
        "flow=excluded.flow, symptoms=excluded.symptoms, bbt_c=excluded.bbt_c, "
        "note=excluded.note, created_at=excluded.created_at",
        (day, flow, json.dumps(symptoms, ensure_ascii=False) if symptoms else None, bbt_c, note, _now()))
    conn.commit()

def save_week_plan_draft(conn, cfg: dict, week_start: str, plan: dict, ctx: dict) -> dict:
    days = plan.get("days", plan)
    results = validate_week_plan(days, ctx, cfg)
    overall, warnings = summarize_validation(results)
    if overall == "REJECT":
        return {"status": "REJECT", "constraint_check": results, "warnings": warnings}
    template_id = plan.get("template_id")
    plan_json = json.dumps(plan, ensure_ascii=False)
    check_json = json.dumps(results, ensure_ascii=False)
    conn.execute(
        "INSERT INTO weekly_plans(week_start, template_id, plan, constraint_check, "
        "validation_warnings, status, created_at) VALUES(?,?,?,?,?,'draft',?)",
        (week_start, template_id, plan_json, check_json, json.dumps(warnings, ensure_ascii=False), _now()))
    conn.commit()
    return {"status": "WARN+PASS" if warnings else "PASS", "constraint_check": results,
            "warnings": warnings}

def activate_week_plan_row(conn, week_start: str) -> None:
    conn.execute("UPDATE weekly_plans SET status='archived' WHERE status='active'")
    conn.execute(
        "UPDATE weekly_plans SET status='active', activated_at=? WHERE week_start=? AND status='draft'",
        (_now(), week_start))
    conn.commit()
