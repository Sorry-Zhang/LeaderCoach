from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Optional

from mcp.server.fastmcp import FastMCP

from core import db as dbmod
from core import store
from core import daily as dailymod
from core import learning
from core import cycle as cyclemod
from core.config import load_config, get as cfg_get
from core.engine import compute_fitness, compute_acwr
from . import confirm as confirmmod
from . import notify as notifymod

mcp = FastMCP(
    "garmin-coach",
    instructions="佳明运动手表 AI 教练：数据驱动，不虚构数字，写操作受确认协议约束。",
)

_cfg = load_config()
_store = confirmmod.PendingStore(_cfg)

def _conn():
    return dbmod.get_conn(_cfg)

def _audit(conn, tool, params, status, summary):
    dbmod.audit(conn, tool, "chat", params, status, summary)

def _upgrade_s2(conn) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='red_s_state'").fetchone()
    if row and row["value"] == "active":
        return True
    row = conn.execute(
        "SELECT 1 FROM injury_history WHERE status IN ('active','restricted') LIMIT 1").fetchone()
    return row is not None

def _recent_gates(conn) -> list[str]:
    row = conn.execute(
        "SELECT gates_triggered FROM engine_daily ORDER BY date DESC LIMIT 1").fetchone()
    if not row or not row["gates_triggered"]:
        return []
    try:
        return [g["gate"] for g in json.loads(row["gates_triggered"])]
    except Exception:
        return []

def _ctl_now(conn) -> float:
    row = conn.execute("SELECT ctl FROM engine_daily ORDER BY date DESC LIMIT 1").fetchone()
    if row and row["ctl"] is not None:
        return row["ctl"]
    return 0.0

def _validation_ctx(conn, week_start: str) -> dict:
    d = date.fromisoformat(week_start)
    end = d + timedelta(days=6)
    ctl = _ctl_now(conn)
    prev = 0.0
    rows = conn.execute(
        "SELECT COALESCE(SUM(load_final),0) s FROM activities WHERE start_time >= ? AND start_time < ?",
        ((d - timedelta(days=7)).isoformat(), week_start)).fetchone()
    prev += rows["s"] or 0
    rows = conn.execute(
        "SELECT COALESCE(SUM(srpe_au),0) s FROM strength_sessions WHERE date >= ? AND date < ?",
        ((d - timedelta(days=7)).isoformat(), week_start)).fetchone()
    prev += rows["s"] or 0
    cut = conn.execute("SELECT value FROM meta WHERE key='cut_mode'").fetchone()
    cut_mode = bool(cut and cut["value"] == "1")
    inj = {}
    for r in conn.execute(
            "SELECT constraint_json FROM injury_history WHERE status IN ('active','restricted')").fetchall():
        if r["constraint_json"]:
            try:
                inj.update(json.loads(r["constraint_json"]))
            except Exception:
                pass
    meso = {r["sport_id"]: r["mesocycle_state"] for r in conn.execute(
        "SELECT sport_id, mesocycle_state FROM sport_registry WHERE active=1").fetchall()}
    floor = {r["sport_id"]: r["maintenance_floor_sessions"] for r in conn.execute(
        "SELECT sport_id, maintenance_floor_sessions FROM sport_registry "
        "WHERE active=1 AND maintenance_floor_sessions IS NOT NULL").fetchall()}
    acwr = None
    row = conn.execute("SELECT acwr FROM engine_daily ORDER BY date DESC LIMIT 1").fetchone()
    if row:
        acwr = row["acwr"]
    from core.engine import acwr_band
    band = acwr_band(acwr, _cfg) if acwr is not None else "green"
    return {
        "chronic_load": ctl, "prev_week_load": round(prev, 1), "cut_mode": cut_mode,
        "gates_active": _recent_gates(conn), "acwr_band": band, "acwr": acwr,
        "injury_constraints": inj, "mesocycle_states": meso, "maintenance_floor": floor,
        "template_target_au": None, "week_index_in_block": None, "block_reduced": False,
        "cycle_heavy_flow_dates": set(), "history_heavy_cramps": False,
        "below_floor_prev_week": False, "gate_load_reduced": False,
    }

@mcp.tool()
def get_daily_brief(date: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        d = (date or datetime.now().date().isoformat())[:10]
        brief = dailymod.build_daily_brief(conn, _cfg, d)
        _audit(conn, "get_daily_brief", {"date": d}, "ok", "简报已生成")
        return brief
    finally:
        conn.close()

@mcp.tool()
def get_week_plan(week_start: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        d = date.fromisoformat((week_start or datetime.now().date().isoformat())[:10])
        ws = (d - timedelta(days=d.weekday())).isoformat()
        plan = conn.execute(
            "SELECT * FROM weekly_plans WHERE week_start=? AND status='active'", (ws,)).fetchone()
        if not plan:
            plan = conn.execute(
                "SELECT * FROM weekly_plans WHERE week_start=? AND status='draft'", (ws,)).fetchone()
        if not plan:
            return {"week_start": ws, "plan": None, "template_id": None,
                    "validation_warnings": [], "message": "本周暂无计划"}
        plan_json = json.loads(plan["plan"]) if isinstance(plan["plan"], str) else plan["plan"]
        return {
            "week_start": ws, "template_id": plan["template_id"], "plan": plan_json,
            "validation_warnings": json.loads(plan["validation_warnings"]) if plan["validation_warnings"] else [],
            "status": plan["status"],
        }
    finally:
        conn.close()

@mcp.tool()
def get_plan_vs_actual(week_start: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        d = date.fromisoformat((week_start or datetime.now().date().isoformat())[:10])
        ws = (d - timedelta(days=d.weekday())).isoformat()
        plan = conn.execute(
            "SELECT * FROM weekly_plans WHERE week_start=? AND status='active'", (ws,)).fetchone()
        if not plan:
            return {"week_start": ws, "completion_pct": None, "message": "本周无 active 计划"}
        plan_json = json.loads(plan["plan"]) if isinstance(plan["plan"], str) else plan["plan"]
        days = plan_json.get("days", plan_json)
        rows = []
        planned_total = 0.0
        actual_total = 0.0
        for day in sorted(days.keys()):
            dd = date.fromisoformat(ws) + timedelta(days=list(days.keys()).index(day)) if False else _day_to_date(ws, day)
            planned = sum(s.get("planned_au", 0) for s in days[day].get("sessions", []))
            act = conn.execute(
                "SELECT COALESCE(SUM(load_final),0) s FROM activities WHERE substr(start_time,1,10)=?",
                (dd,)).fetchone()["s"]
            act += conn.execute(
                "SELECT COALESCE(SUM(srpe_au),0) s FROM strength_sessions WHERE date=?", (dd,)).fetchone()["s"]
            planned_total += planned
            actual_total += (act or 0)
            rows.append({"day": day, "date": dd, "planned_au": planned, "actual_au": round(act or 0, 1)})
        return {"week_start": ws, "template_id": plan["template_id"], "slots": rows,
                "planned_au": round(planned_total, 1), "actual_au": round(actual_total, 1),
                "completion_pct": round(actual_total / planned_total, 3) if planned_total else None}
    finally:
        conn.close()

def _day_to_date(week_start: str, day: str) -> str:
    order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    ws = date.fromisoformat(week_start)
    return (ws + timedelta(days=order.index(day))).isoformat()

@mcp.tool()
def get_sync_status() -> dict:
    conn = _conn()
    try:
        last_sync = conn.execute("SELECT value FROM meta WHERE key='last_sync_at'").fetchone()
        brief = dailymod.build_daily_brief(conn, _cfg)
        feeding = brief["data_quality"]["feeding"]
        return {
            "last_sync_at": last_sync["value"] if last_sync else None,
            "feeding": feeding,
            "obs_state": conn.execute("SELECT value FROM meta WHERE key='obs_state'").fetchone(),
            "activity_count": conn.execute("SELECT COUNT(*) c FROM activities").fetchone()["c"],
            "strength_count": conn.execute("SELECT COUNT(*) c FROM strength_sessions").fetchone()["c"],
        }
    finally:
        conn.close()

def _run_sync_blocking(days: int, download_fit: bool):
    """在独立线程中执行同步：MCP 工具运行在事件循环内，不能直接 asyncio.run。"""
    import threading

    from ingest import plugin_bridge

    end = date.today()
    start = end - timedelta(days=max(1, days))
    box: dict = {}

    def _worker():
        try:
            box["result"] = plugin_bridge.run(
                start.isoformat(), end.isoformat(), 100, download_fit, True,
                plugin_bridge.DEFAULTS["node"], plugin_bridge.DEFAULTS["mcp_js"],
                plugin_bridge.DEFAULTS["session"], plugin_bridge.DEFAULTS["username"],
                plugin_bridge.DEFAULTS["region"], plugin_bridge.DEFAULTS["account"],
                plugin_bridge.DEFAULTS["fit_dir"])
        except BaseException as e:  # noqa: BLE001
            box["error"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=300)
    if th.is_alive():
        raise TimeoutError("Garmin 同步超过 300 秒未完成")
    if "error" in box:
        raise RuntimeError(box["error"])
    return box["result"]


@mcp.tool()
def sync_garmin(days: int = 30, download_fit: bool = False) -> dict:
    """从 Garmin 插件拉取最近 N 天的活动与每日生理数据，写入本地数据库。"""
    daily, acts, inserted = _run_sync_blocking(days, download_fit)
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('last_sync_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (datetime.now().isoformat(timespec="seconds"),))
        conn.commit()
        hrv_days = conn.execute(
            "SELECT COUNT(*) c FROM daily_metrics WHERE hrv_last_night IS NOT NULL"
        ).fetchone()["c"]
        sleep_days = conn.execute(
            "SELECT COUNT(*) c FROM daily_metrics WHERE sleep_hours IS NOT NULL"
        ).fetchone()["c"]
    finally:
        conn.close()
    return {"requested_days": days, "daily_days": len(daily),
            "activities_listed": len(acts), "activities_inserted": inserted,
            "hrv_days_total": hrv_days, "sleep_days_total": sleep_days}


@mcp.tool()
def get_activities(date_range: int = 14, sport_id: Optional[str] = None) -> list:
    conn = _conn()
    try:
        start = (datetime.now().date() - timedelta(days=date_range)).isoformat()
        if sport_id:
            rows = conn.execute(
                "SELECT id, substr(start_time,1,10) AS date, sport_id, duration_min, distance_km, "
                "avg_hr, max_hr, avg_pace_min_km, rpe, srpe_au, hr_trimp, load_final, "
                "load_path_used, intensity_domain, mismatch_flag, annotation "
                "FROM activities WHERE start_time>=? AND sport_id=? ORDER BY start_time",
                (start, sport_id)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, substr(start_time,1,10) AS date, sport_id, duration_min, distance_km, "
                "avg_hr, max_hr, avg_pace_min_km, rpe, srpe_au, hr_trimp, load_final, "
                "load_path_used, intensity_domain, mismatch_flag, annotation "
                "FROM activities WHERE start_time>=? ORDER BY start_time", (start,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@mcp.tool()
def get_strength_history(exercise_id: Optional[str] = None, range: int = 30) -> list:
    conn = _conn()
    try:
        start = (datetime.now().date() - timedelta(days=range)).isoformat()
        if exercise_id:
            rows = conn.execute(
                "SELECT * FROM strength_sessions WHERE date>=? AND exercise_id=? ORDER BY date",
                (start, exercise_id)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM strength_sessions WHERE date>=? ORDER BY date", (start,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@mcp.tool()
def get_cycle_status() -> dict:
    conn = _conn()
    try:
        periods = [dict(r) for r in conn.execute(
            "SELECT start_date, end_date FROM cycle_periods ORDER BY start_date").fetchall()]
        pred = cyclemod.predict(datetime.now().date(), periods)
        flags = cyclemod.evaluate_flags(periods, datetime.now().date(), _cfg)
        return {"prediction": pred, "flags": flags}
    finally:
        conn.close()

@mcp.tool()
def get_audit_log(range: int = 30, tool_name: Optional[str] = None) -> list:
    conn = _conn()
    try:
        start = (datetime.now() - timedelta(days=range)).isoformat(timespec="seconds")
        if tool_name:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE ts>=? AND tool_name=? ORDER BY ts DESC LIMIT 500",
                (start, tool_name)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE ts>=? ORDER BY ts DESC LIMIT 500", (start,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

@mcp.tool()
def get_learning_status() -> dict:
    conn = _conn()
    try:
        return {"entries": learning.learning_status(conn),
                "note": "教练会呈现证据并请你裁决；你不代决，引擎不悄悄乱调。"}
    finally:
        conn.close()

@mcp.tool()
def log_subjective(date: str, soreness: int, sleep_feel: int, injury_flag: bool,
                   note: Optional[str] = None, confirm: bool = False,
                   confirm_code: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        params = {"date": date, "soreness": soreness, "sleep_feel": sleep_feel,
                  "injury_flag": injury_flag, "note": note}
        needs_code = bool(injury_flag) or _upgrade_s2(conn)
        if not confirm:
            _, code = _store.open("log_subjective", params, needs_code)
            if needs_code:
                notifymod.notify(_cfg, f"确认码：{code}（录入主观）", kind="code")
            _audit(conn, "log_subjective", params, "E_CONFIRM_REQUIRED", "待确认")
            msg = "确认码已推送到您的设备，请念给我" if needs_code else \
                f"待写：{date} 酸痛 {soreness}/10，睡眠感受 {sleep_feel}/5，伤病旗 {injury_flag}。请用户确认后重调。"
            return {"error": "E_CONFIRM_REQUIRED", "message": msg}
        res = _store.consume("log_subjective", params, confirm_code)
        if not res["ok"]:
            _audit(conn, "log_subjective", params, res["error"], res.get("message", ""))
            return {"error": res["error"], "message": res.get("message", "")}
        store.upsert_subjective(conn, date, soreness, sleep_feel, note, injury_flag)
        dailymod.compute_engine_daily(conn, _cfg, date)
        _audit(conn, "log_subjective", params, "ok", "已保存")
        return {"ok": True, "date": date}
    finally:
        conn.close()

@mcp.tool()
def log_strength(date: str, exercise_id: str, pattern: str, sets: int, reps: int,
                 rpe: int, duration_s: Optional[int] = None, weight_kg: Optional[float] = None,
                 added_kg: Optional[float] = None, note: Optional[str] = None,
                 confirm: bool = False, confirm_code: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        params = {"date": date, "exercise_id": exercise_id, "pattern": pattern, "sets": sets,
                  "reps": reps, "rpe": rpe, "duration_s": duration_s, "weight_kg": weight_kg,
                  "added_kg": added_kg, "note": note}
        needs_code = _upgrade_s2(conn)
        if not confirm:
            _, code = _store.open("log_strength", params, needs_code)
            if needs_code:
                notifymod.notify(_cfg, f"确认码：{code}（力量）", kind="code")
            _audit(conn, "log_strength", params, "E_CONFIRM_REQUIRED", "待确认")
            msg = ("确认码已推送到您的设备，请念给我" if needs_code else
                   f"待写：{date} {exercise_id} {sets}×{reps} @RPE{rpe}。请用户确认后重调。")
            return {"error": "E_CONFIRM_REQUIRED", "message": msg}
        res = _store.consume("log_strength", params, confirm_code)
        if not res["ok"]:
            _audit(conn, "log_strength", params, res["error"], res.get("message", ""))
            return {"error": res["error"], "message": res.get("message", "")}
        out = store.log_strength_row(conn, _cfg, date, exercise_id, pattern, sets, reps,
                                     duration_s, weight_kg, added_kg, rpe, note)
        _audit(conn, "log_strength", params, "ok", "已保存")
        return {"ok": True, **out}
    finally:
        conn.close()

@mcp.tool()
def log_activity_rpe(date: str, sport_id: str, rpe: int, confirm: bool = False,
                     confirm_code: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        params = {"date": date, "sport_id": sport_id, "rpe": rpe}
        needs_code = _upgrade_s2(conn)
        if not confirm:
            _, code = _store.open("log_activity_rpe", params, needs_code)
            if needs_code:
                notifymod.notify(_cfg, f"确认码：{code}（RPE）", kind="code")
            _audit(conn, "log_activity_rpe", params, "E_CONFIRM_REQUIRED", "待确认")
            msg = ("确认码已推送到您的设备，请念给我" if needs_code else
                   f"待写：{date} {sport_id} RPE{rpe}。请用户确认后重调。")
            return {"error": "E_CONFIRM_REQUIRED", "message": msg}
        res = _store.consume("log_activity_rpe", params, confirm_code)
        if not res["ok"]:
            _audit(conn, "log_activity_rpe", params, res["error"], res.get("message", ""))
            return {"error": res["error"], "message": res.get("message", "")}
        try:
            aid = store.log_activity_rpe(conn, _cfg, date, sport_id, rpe)
        except LookupError as e:
            _audit(conn, "log_activity_rpe", params, "E_NOT_FOUND", str(e))
            return {"error": "E_NOT_FOUND", "message": str(e)}
        _audit(conn, "log_activity_rpe", params, "ok", "已保存")
        return {"ok": True, "activity_id": aid}
    finally:
        conn.close()

@mcp.tool()
def log_body_metrics(weight_kg: float, measured_at: Optional[str] = None,
                     body_fat_pct: Optional[float] = None, confirm: bool = False,
                     confirm_code: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        params = {"weight_kg": weight_kg, "measured_at": measured_at, "body_fat_pct": body_fat_pct}
        if not confirm:
            _, code = _store.open("log_body_metrics", params, True)
            notifymod.notify(_cfg, f"确认码：{code}（体重）", kind="code")
            _audit(conn, "log_body_metrics", params, "E_CONFIRM_REQUIRED", "待确认")
            return {"error": "E_CONFIRM_REQUIRED", "message": "确认码已推送到您的设备，请念给我"}
        res = _store.consume("log_body_metrics", params, confirm_code)
        if not res["ok"]:
            _audit(conn, "log_body_metrics", params, res["error"], res.get("message", ""))
            return {"error": res["error"], "message": res.get("message", "")}
        out = store.log_body_metrics_row(conn, weight_kg, measured_at, body_fat_pct)
        _audit(conn, "log_body_metrics", params, "ok", "已保存")
        return {"ok": True, **out}
    finally:
        conn.close()

@mcp.tool()
def log_cycle_period(action: str, date: str, confirm: bool = False,
                     confirm_code: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        params = {"action": action, "date": date}
        if not confirm:
            _, code = _store.open("log_cycle_period", params, True)
            notifymod.notify(_cfg, f"确认码：{code}（周期）", kind="code")
            _audit(conn, "log_cycle_period", params, "E_CONFIRM_REQUIRED", "待确认")
            return {"error": "E_CONFIRM_REQUIRED", "message": "确认码已推送到您的设备，请念给我"}
        res = _store.consume("log_cycle_period", params, confirm_code)
        if not res["ok"]:
            _audit(conn, "log_cycle_period", params, res["error"], res.get("message", ""))
            return {"error": res["error"], "message": res.get("message", "")}
        store.log_cycle_period_row(conn, action, date)
        _audit(conn, "log_cycle_period", params, "ok", "已保存")
        return {"ok": True}
    finally:
        conn.close()

@mcp.tool()
def log_cycle_daily(date: str, flow: Optional[str] = None, symptoms: Optional[list] = None,
                    bbt_c: Optional[float] = None, note: Optional[str] = None,
                    confirm: bool = False, confirm_code: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        params = {"date": date, "flow": flow, "symptoms": symptoms, "bbt_c": bbt_c, "note": note}
        if not confirm:
            _, code = _store.open("log_cycle_daily", params, True)
            notifymod.notify(_cfg, f"确认码：{code}（周期日记）", kind="code")
            _audit(conn, "log_cycle_daily", params, "E_CONFIRM_REQUIRED", "待确认")
            return {"error": "E_CONFIRM_REQUIRED", "message": "确认码已推送到您的设备，请念给我"}
        res = _store.consume("log_cycle_daily", params, confirm_code)
        if not res["ok"]:
            _audit(conn, "log_cycle_daily", params, res["error"], res.get("message", ""))
            return {"error": res["error"], "message": res.get("message", "")}
        store.log_cycle_daily_row(conn, date, flow, symptoms, bbt_c, note)
        _audit(conn, "log_cycle_daily", params, "ok", "已保存")
        return {"ok": True}
    finally:
        conn.close()

@mcp.tool()
def log_injury(body_part: str, onset_date: str, status: str, side: Optional[str] = None,
               constraint_json: Optional[dict] = None, note: Optional[str] = None,
               confirm: bool = False, confirm_code: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        params = {"body_part": body_part, "onset_date": onset_date, "status": status,
                  "side": side, "constraint_json": constraint_json, "note": note}
        if not confirm:
            _, code = _store.open("log_injury", params, True)
            notifymod.notify(_cfg, f"确认码：{code}（伤病）", kind="code")
            _audit(conn, "log_injury", params, "E_CONFIRM_REQUIRED", "待确认")
            return {"error": "E_CONFIRM_REQUIRED", "message": "确认码已推送到您的设备，请念给我"}
        res = _store.consume("log_injury", params, confirm_code)
        if not res["ok"]:
            _audit(conn, "log_injury", params, res["error"], res.get("message", ""))
            return {"error": res["error"], "message": res.get("message", "")}
        rid = store.log_injury_row(conn, body_part, side, onset_date, status, constraint_json, note)
        _audit(conn, "log_injury", params, "ok", "已保存")
        return {"ok": True, "id": rid}
    finally:
        conn.close()

@mcp.tool()
def save_week_plan(week_start: str, plan: dict, confirm: bool = False,
                   confirm_code: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        params = {"week_start": week_start, "plan": plan}
        if not confirm:
            ctx = _validation_ctx(conn, week_start)
            from core.engine import validate_week_plan, summarize_validation
            days = plan.get("days", plan)
            results = validate_week_plan(days, ctx, _cfg)
            overall, warnings = summarize_validation(results)
            if overall == "REJECT":
                _audit(conn, "save_week_plan", params, "REJECT", "校验拒绝")
                return {"error": "E_VALIDATION", "status": "REJECT",
                        "constraint_check": results, "message": "校验器拒绝，详见 constraint_check"}
            _, code = _store.open("save_week_plan", params, True)
            notifymod.notify(_cfg, f"确认码：{code}（存周计划）", kind="code")
            _audit(conn, "save_week_plan", params, "E_CONFIRM_REQUIRED", "待确认")
            return {"error": "E_CONFIRM_REQUIRED", "status": overall,
                    "validation_warnings": warnings, "message": "确认码已推送到您的设备，请念给我"}
        res = _store.consume("save_week_plan", params, confirm_code)
        if not res["ok"]:
            _audit(conn, "save_week_plan", params, res["error"], res.get("message", ""))
            return {"error": res["error"], "message": res.get("message", "")}
        ctx = _validation_ctx(conn, week_start)
        out = store.save_week_plan_draft(conn, _cfg, week_start, plan, ctx)
        _audit(conn, "save_week_plan", params, out["status"], "草稿已存")
        return {"ok": out["status"] != "REJECT", **out}
    finally:
        conn.close()

@mcp.tool()
def activate_week_plan(week_start: str, confirm: bool = False,
                       confirm_code: Optional[str] = None) -> dict:
    conn = _conn()
    try:
        params = {"week_start": week_start}
        row = conn.execute(
            "SELECT 1 FROM weekly_plans WHERE week_start=? AND status='draft'", (week_start,)).fetchone()
        if not row:
            _audit(conn, "activate_week_plan", params, "E_STATE_CONFLICT", "无草稿")
            return {"error": "E_STATE_CONFLICT", "message": "该周无草稿计划"}
        if not confirm:
            _, code = _store.open("activate_week_plan", params, True)
            notifymod.notify(_cfg, f"确认码：{code}（激活周计划）", kind="code")
            _audit(conn, "activate_week_plan", params, "E_CONFIRM_REQUIRED", "待确认")
            return {"error": "E_CONFIRM_REQUIRED", "message": "确认码已推送到您的设备，请念给我"}
        res = _store.consume("activate_week_plan", params, confirm_code)
        if not res["ok"]:
            _audit(conn, "activate_week_plan", params, res["error"], res.get("message", ""))
            return {"error": res["error"], "message": res.get("message", "")}
        store.activate_week_plan_row(conn, week_start)
        _audit(conn, "activate_week_plan", params, "ok", "已激活")
        return {"ok": True, "week_start": week_start}
    finally:
        conn.close()

if __name__ == "__main__":
    mcp.run()
