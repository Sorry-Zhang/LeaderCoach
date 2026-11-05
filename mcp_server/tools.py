from __future__ import annotations

import json
import threading
import uuid
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

def _recent_forecast(conn) -> list[str]:
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
        "SELECT COALESCE(SUM(load_final),0) s FROM activities "
        "WHERE substr(start_time,1,10) >= ? AND substr(start_time,1,10) < ?",
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
        "forecast_hits": _recent_forecast(conn), "acwr_band": band, "acwr": acwr,
        "injury_constraints": inj, "mesocycle_states": meso, "maintenance_floor": floor,
        "template_target_au": None, "week_index_in_block": None, "block_reduced": False,
        "cycle_heavy_flow_dates": set(), "history_heavy_cramps": False,
        "below_floor_prev_week": False, "forecast_load_reduced": False,
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
        logging = brief["data_quality"]["logging"]
        from core import store_ext
        return {
            "last_sync_at": last_sync["value"] if last_sync else None,
            "logging": logging,
            "obs_state": conn.execute("SELECT value FROM meta WHERE key='obs_state'").fetchone(),
            "activity_count": conn.execute("SELECT COUNT(*) c FROM activities").fetchone()["c"],
            "strength_count": conn.execute("SELECT COUNT(*) c FROM strength_sessions").fetchone()["c"],
            "coverage": store_ext.stats(conn),
            "sync_jobs": _recent_sync_jobs(3),
        }
    finally:
        conn.close()

# ── 异步同步 job：长任务不再阻塞 MCP 调用 ─────────────────────
_SYNC_JOBS: dict = {}
_SYNC_LOCK = threading.Lock()
_SYNC_KEEP = 20


def _recent_sync_jobs(n: int = 3) -> list:
    with _SYNC_LOCK:
        jobs = sorted(_SYNC_JOBS.values(), key=lambda j: j.get("started_at") or "", reverse=True)
        out = []
        for j in jobs[:n]:
            item = {k: j.get(k) for k in
                    ("job_id", "status", "started_at", "finished_at", "days",
                     "download_fit", "full", "error", "elapsed_s")}
            res = j.get("result")
            if res:
                item["result"] = {k: res.get(k) for k in (
                    "mode", "activities", "inserted", "splits", "fit_files",
                    "days", "raw_written", "coverage", "full_error") if k in res}
            out.append(item)
        return out


def _sync_job(job_id: str) -> Optional[dict]:
    with _SYNC_LOCK:
        j = _SYNC_JOBS.get(job_id)
        return dict(j) if j else None


def _finalize_sync(box: dict, days: int) -> dict:
    """同步收尾：写 last_sync_at、取覆盖度、附带回退错误。"""
    result = dict(box.get("result") or {})
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('last_sync_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (datetime.now().isoformat(timespec="seconds"),))
        conn.commit()
        from core import store_ext
        coverage = store_ext.stats(conn)
    finally:
        conn.close()
    if box.get("full_error"):
        result["full_error"] = box["full_error"]
    result["requested_days"] = days
    result["coverage"] = coverage
    return result


def _start_sync_job(days: int, download_fit: bool, full: bool) -> dict:
    jid = f"sync-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    started = datetime.now()
    with _SYNC_LOCK:
        if len(_SYNC_JOBS) > _SYNC_KEEP:
            for k in sorted(_SYNC_JOBS, key=lambda k: _SYNC_JOBS[k].get("started_at") or "")[:-_SYNC_KEEP]:
                _SYNC_JOBS.pop(k, None)
        _SYNC_JOBS[jid] = {"job_id": jid, "status": "running", "days": days,
                           "download_fit": bool(download_fit), "full": bool(full),
                           "started_at": started.isoformat(timespec="seconds")}

    def _worker():
        try:
            box = _run_sync_blocking(days, download_fit, full=full, timeout=7200)
            res = _finalize_sync(box, days)
            with _SYNC_LOCK:
                _SYNC_JOBS[jid].update(
                    status="done", result=res,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    elapsed_s=round((datetime.now() - started).total_seconds(), 1))
        except BaseException as e:  # noqa: BLE001
            with _SYNC_LOCK:
                _SYNC_JOBS[jid].update(
                    status="failed", error=f"{type(e).__name__}: {e}",
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    elapsed_s=round((datetime.now() - started).total_seconds(), 1))

    threading.Thread(target=_worker, daemon=True, name=jid).start()
    return {"job_id": jid, "status": "running", "days": days,
            "started_at": started.isoformat(timespec="seconds"),
            "hint": "同步已在后台启动，不会阻塞对话。用 get_sync_status() 轮询同名 job_id，"
                    "status=done 时看 result（splits 为本次回填的分段条数）。"}


def _run_sync_blocking(days: int, download_fit: bool, full: bool = True, timeout: int = 1800):
    """在独立线程中执行同步：MCP 工具运行在事件循环内，不能直接 asyncio.run。

    full=True 走 ingest.plugin_full（插件全部只读工具 + 原始存档）；
    full=False 或全量失败时回退到 ingest.plugin_bridge（旧的最小采集路径）。
    """
    import threading

    from core.config import get as cfg_get
    from ingest import plugin_full

    full = bool(full) and bool(cfg_get(_cfg, "garmin_plugin.full_sync", True))
    end = date.today()
    start = end - timedelta(days=max(1, days) - 1)
    box: dict = {}

    def _full():
        return plugin_full.run(
            start.isoformat(), end.isoformat(), 1, download_fit,
            plugin_full.DEFAULTS["node"], plugin_full.DEFAULTS["mcp_js"],
            plugin_full.DEFAULTS["session"], plugin_full.DEFAULTS["username"],
            plugin_full.DEFAULTS["region"], plugin_full.DEFAULTS["account"],
            plugin_full.DEFAULTS["fit_dir"])

    def _legacy():
        from ingest import plugin_bridge
        daily, acts, inserted = plugin_bridge.run(
            start.isoformat(), end.isoformat(), 100, download_fit, True,
            plugin_bridge.DEFAULTS["node"], plugin_bridge.DEFAULTS["mcp_js"],
            plugin_bridge.DEFAULTS["session"], plugin_bridge.DEFAULTS["username"],
            plugin_bridge.DEFAULTS["region"], plugin_bridge.DEFAULTS["account"],
            plugin_bridge.DEFAULTS["fit_dir"])
        return {"mode": "legacy", "daily_days": len(daily), "activities_listed": len(acts),
                "activities_inserted": inserted}

    def _worker():
        try:
            if full:
                try:
                    box["result"] = _full()
                    box["result"]["mode"] = "full"
                    return
                except BaseException as e:  # noqa: BLE001
                    box["full_error"] = f"{type(e).__name__}: {e}"
            box["result"] = _legacy()
        except BaseException as e:  # noqa: BLE001
            box["error"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=timeout)
    if th.is_alive():
        raise TimeoutError(f"Garmin 同步超过 {timeout} 秒未完成")
    if "error" in box:
        raise RuntimeError(box["error"])
    return box


@mcp.tool()
def sync_garmin(days: int = 30, download_fit: bool = False, full: bool = True,
                wait: bool = False) -> dict:
    """从 Garmin 插件拉取最近 N 天的数据并写入本地数据库。

    **默认异步**（wait=False）：立即返回 job_id，后台线程执行，不阻塞对话；
    用 get_sync_status() 轮询 job_status（status=done 时看 result）。
    需要一次性结果时传 wait=True（会最长阻塞到超时，慎用）。

    full=True（默认）调用插件的**全部只读工具**：活动（完整详情）、睡眠、心率、步数、体重、
    HRV 摘要与夜间读数、压力/身体电量时序、训练准备度、训练状态、负荷平衡、VO2max、
    比赛预测、训练库、个人资料；原始返回 gzip 存档到 archive/plugin_raw；另用本地 FIT 回填
    运动分段（lap/1km）。睡眠阶段时间线来自 SDK 直读（插件 get_garmin_sleep 不含）。
    full=False 走旧的 plugin_bridge 最小路径。

    download_fit=True 会为当期活动下载缺失的 FIT（分段回填需要它）。
    """
    if not wait:
        return _start_sync_job(days, download_fit, full)
    box = _run_sync_blocking(days, download_fit, full=full)
    return _finalize_sync(box, days)


@mcp.tool()
def get_sleep_detail(date: Optional[str] = None) -> dict:
    """某一天的睡眠明细：入睡/醒来时刻、各阶段时间线、睡眠评分与评分分项、夜间心率/压力/
    身体电量/HRV/呼吸的时间序列（可用时）。"""
    conn = _conn()
    try:
        d = (date or datetime.now().date().isoformat())[:10]
        dm = conn.execute("SELECT * FROM daily_metrics WHERE date=?", (d,)).fetchone()
        stages = [dict(r) for r in conn.execute(
            "SELECT seq, stage, start_local, end_local, seconds FROM sleep_stages "
            "WHERE date=? ORDER BY seq", (d,)).fetchall()]
        metrics = {r["metric"]: r["value"] for r in conn.execute(
            "SELECT metric, value FROM garmin_metrics WHERE date=? AND ("
            "metric LIKE 'sleep%' OR metric LIKE 'avg_sleep%' OR metric LIKE 'avg_overnight%' "
            "OR metric LIKE 'respiration%' OR metric LIKE 'body_battery_change' "
            "OR metric LIKE 'restless%' OR metric LIKE 'awake_count' OR metric LIKE 'hrv_%')",
            (d,)).fetchall()}
        series = {}
        for r in conn.execute(
                "SELECT kind, ts_local, value FROM sleep_series WHERE date=? ORDER BY kind, ts_local",
                (d,)).fetchall():
            series.setdefault(r["kind"], []).append([r["ts_local"], r["value"]])
        return {
            "date": d,
            "sleep": None if not dm else {
                "sleep_hours": dm["sleep_hours"], "deep_h": dm["sleep_deep_h"],
                "light_h": dm["sleep_light_h"], "rem_h": dm["sleep_rem_h"],
                "awake_h": dm["sleep_awake_h"], "start": dm["sleep_start"],
                "end": dm["sleep_end"], "hrv_last_night": dm["hrv_last_night"],
                "resting_hr": dm["resting_hr"],
            },
            "stage_timeline": stages,
            "metrics": metrics,
            "series_points": {k: len(v) for k, v in series.items()},
            "series": series,
        }
    finally:
        conn.close()


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

# ── 分段读取：get_activity_detail ─────────────────────────────

def _fmt_pace(pace_min_km) -> Optional[str]:
    """配速（min/km）→ 'm:ss'；0 或空返回 None。"""
    if not pace_min_km or pace_min_km <= 0:
        return None
    sec = int(round(float(pace_min_km) * 60))
    return f"{sec // 60}:{sec % 60:02d}"


def _resolve_activity(conn, activity_id):
    """activity_id 可为本地整数 id、Garmin activityId，或 'plugin:<id>' 形式。"""
    key = str(activity_id).strip()
    try:
        row = conn.execute("SELECT * FROM activities WHERE id=?", (int(key),)).fetchone()
        if row:
            return row
    except (TypeError, ValueError):
        pass
    clean = key.split("@")[0].replace("plugin:", "").strip()
    for cand in (key, clean, f"plugin:{clean}"):
        row = conn.execute(
            "SELECT * FROM activities WHERE garmin_activity_id=? ORDER BY id DESC LIMIT 1",
            (cand,)).fetchone()
        if row:
            return row
    return None


def _backfill_one(conn, row) -> int:
    """库里没有分段时，就地用本地 FIT 补一份（不联网）。返回写入条数。"""
    from pathlib import Path as _P
    cfg = _cfg
    candidates = []
    if row["fit_path"]:
        candidates.append(_P(row["fit_path"]))
    gid = (row["garmin_activity_id"] or "").replace("plugin:", "").strip()
    if gid.isdigit():
        fit_root = _P(cfg["garmin_plugin"].get("fit_dir") or "data/garmin_fit")
        if not fit_root.is_absolute():
            fit_root = _P(cfg["_root"]) / fit_root
        if fit_root.exists():
            candidates.extend(fit_root.rglob(f"{gid}.fit"))
    files = [f for f in candidates if f and f.exists()]
    if not files:
        return 0
    try:
        from ingest import fit_parser
        return fit_parser.rebuild_splits(cfg, files, conn=conn)
    except Exception:  # noqa: BLE001
        return 0


@mcp.tool()
def get_activity_detail(activity_id: str, granularity: str = "auto",
                        backfill: bool = True) -> dict:
    """单次活动的完整明细，重点是**分段数据**（每段距离/用时/配速/均心率/最高心率/爬升）。

    activity_id：本地活动 id（如 "685"）、Garmin activityId（如 "658766818"），
    或 get_activities 返回的 garmin_activity_id 形式（"plugin:658766818"）。

    granularity：
      - "km"   → 每公里分段（做后程掉速、配速稳定性分析用这个）
      - "lap"  → 设备圈分段（手动/自动圈，含每圈起止时刻）
      - "auto" → 有圈分段就返回圈，否则返回公里分段（默认）

    backfill=True 时，如果库里还没有分段，会尝试用本地 FIT 文件即时补写。
    返回 summary（总距离/总时长/均配速/均心率）+ splits（分段列表）+
    split_summary（前/后半程配速对比、最快最慢公里、掉速率）；无数据时给出说明。
    """
    conn = _conn()
    try:
        row = _resolve_activity(conn, activity_id)
        if not row:
            return {"error": "E_NOT_FOUND",
                    "message": f"找不到活动 {activity_id}，先用 get_activities 查 id"}
        aid = row["id"]
        g = (granularity or "auto").lower()

        def _load(types):
            marks = ",".join("?" * len(types))
            detail = [dict(r) for r in conn.execute(
                "SELECT seq, distance_m, duration_s, pace_min_km, avg_hr, max_hr, "
                "elevation_gain_m, elevation_loss_m, net_elevation_m, avg_grade_pct, "
                "max_grade_pct, gap_pace_min_km, split_type, start_time, end_time, source "
                "FROM activity_split_detail WHERE activity_id=? AND split_type IN (" + marks + ") "
                "ORDER BY seq", (aid, *types)).fetchall()]
            if detail:
                return detail
            # 兼容：只有旧表数据时
            return [dict(r) for r in conn.execute(
                "SELECT km AS seq, distance_m, duration_s, pace_min_km, avg_hr, max_hr, "
                "elevation_gain_m, split_type, start_time, end_time, source "
                "FROM activity_splits WHERE activity_id=? AND split_type IN (" + marks + ") "
                "ORDER BY km", (aid, *types)).fetchall()]

        if g == "lap":
            rows = _load(("lap",))
        elif g == "km":
            rows = _load(("km", "auto"))
        else:
            rows = _load(("lap",)) or _load(("km", "auto"))

        filled = 0
        if not rows and backfill:
            filled = _backfill_one(conn, row)
            if filled:
                rows = (_load(("lap",)) if g == "lap"
                        else _load(("km", "auto")) or _load(("lap",)))

        for r in rows:
            r["pace"] = _fmt_pace(r.get("pace_min_km"))
            if r.get("gap_pace_min_km"):
                r["gap_pace"] = _fmt_pace(r["gap_pace_min_km"])
                if r.get("pace_min_km"):
                    r["gap_vs_actual_sec"] = round(
                        (r["pace_min_km"] - r["gap_pace_min_km"]) * 60, 1)
        km_rows = [r for r in rows if r.get("split_type") in ("km", "auto")]

        summary = {
            "id": aid,
            "garmin_activity_id": row["garmin_activity_id"],
            "start_time": row["start_time"],
            "sport_id": row["sport_id"],
            "distance_km": row["distance_km"],
            "duration_min": row["duration_min"],
            "avg_pace": _fmt_pace(row["avg_pace_min_km"]),
            "avg_hr": row["avg_hr"],
            "max_hr": row["max_hr"],
            "elevation_gain_m": row["elevation_gain_m"],
        }
        if km_rows:
            g_sum = sum(r.get("elevation_gain_m") or 0 for r in km_rows)
            l_sum = sum(r.get("elevation_loss_m") or 0 for r in km_rows)
            d_sum = sum(r.get("distance_m") or 0 for r in km_rows)
            gap_num = sum((r.get("gap_pace_min_km") or 0) * (r.get("distance_m") or 0)
                          for r in km_rows)
            summary["elevation_gain_split_m"] = round(g_sum, 1)
            summary["elevation_loss_split_m"] = round(l_sum, 1)
            if d_sum:
                summary["climb_density_m_per_km"] = round(g_sum / (d_sum / 1000), 1)
            if d_sum and gap_num:
                summary["gap_pace"] = _fmt_pace(gap_num / d_sum)

        out = {"summary": summary, "granularity": g, "split_count": len(rows), "splits": rows}
        if filled:
            out["backfilled_splits"] = filled

        if not rows:
            out["message"] = (
                "该活动还没有分段数据。可用 sync_garmin(download_fit=true) 下载 FIT 后重试，"
                "或确认 data/garmin_fit 下存在 <garminActivityId>.fit。")
            return out

        if km_rows:
            paces = [(r["seq"], r["pace_min_km"]) for r in km_rows if r.get("pace_min_km")]
            if paces:
                half = len(paces) // 2
                first = sum(p for _, p in paces[:half]) / max(1, half)
                second = sum(p for _, p in paces[half:]) / max(1, len(paces) - half)
                fastest = min(paces, key=lambda x: x[1])
                slowest = max(paces, key=lambda x: x[1])
                out["split_summary"] = {
                    "first_half_avg_pace": _fmt_pace(first),
                    "second_half_avg_pace": _fmt_pace(second),
                    "second_half_drift_sec_per_km": round((second - first) * 60, 1),
                    "fastest_km": fastest[0], "fastest_pace": _fmt_pace(fastest[1]),
                    "slowest_km": slowest[0], "slowest_pace": _fmt_pace(slowest[1]),
                    "pace_range_sec_per_km": round((slowest[1] - fastest[1]) * 60, 1),
                }
        _audit(conn, "get_activity_detail",
               {"activity_id": activity_id, "granularity": g}, "ok", f"{len(rows)} 段")
        return out
    finally:
        conn.close()

@mcp.tool()
def get_grade_analysis(activity_id: Optional[str] = None, range_days: int = 90,
                       min_distance_km: float = 3.0) -> dict:
    """坡度-配速分析：分段地形构成、上坡惩罚/下坡收益、坡度调整配速（GAP）。

    **重要方法论**：整次活动的爬升密度不能用来判断分段坡度影响。同样的平均值
    既可以来自缓坡连绵，也可以来自陡上陡下交替——实测你的数据里，活动平均的
    实际-GAP 差只有 0.2–2 秒，但单个分段的修正量可达 47–55 秒（相差近百倍）。
    要看分段级差异，必须用 terrain_breakdown / segment_deviation /
    max_segment_gap_delta_sec。

    用法一：给 activity_id → 单次活动
      - splits：每段带 terrain 标签（climb/descent/mixed/rolling/flat）、
        段内起伏 roughness_m、平均/最大坡度、实际配速、GAP
      - terrain_breakdown：按地形分组统计（段数/距离/爬升/实际配速/GAP）
      - segment_deviation：用 GAP 归一化找「相对最费力 / 最轻松」的段
        （GAP 已扣掉坡度，所以这里的偏离是真实用力或疲劳，不是地形）
      - asymmetry：上坡惩罚 vs 下坡收益（单次样本常不足，会明确标注）
      - summary.max_segment_gap_delta_sec：本次分段级坡度修正的最大值

    用法二：不给 activity_id → 跨活动统计
      - overall：全周期池化回归的上坡惩罚/下坡收益（样本量大才可信）
      - period_compare：近 N 周 vs 之前 N 周的对比（含可信度标注）
      - weeks：按周的分段级地形构成（含每周最大段内 GAP 修正）

    关键概念：
      - GAP = 实际配速 ÷ 坡度代价比（Minetti 2002）。上坡段 GAP 比实际配速**快**，
        下坡段更慢。判断「这段是否掉速」要看 GAP 不看实际配速。
      - uphill_penalty_sec_per_1pct：每 +1% 坡度慢多少秒/km（典型 10–20）。
      - asymmetry_ratio = 下坡收益/上坡惩罚（典型 0.4–0.8，下坡收不回上坡损失）。
      - 合并成一条直线的 r2 天然很低（0.01 级），因为上下坡效应互相抵消——
        这不代表坡度没影响，要看分开的斜率。
    """
    from core import grade as grademod
    conn = _conn()
    try:
        if activity_id is not None:
            row = _resolve_activity(conn, activity_id)
            if not row:
                return {"error": "E_NOT_FOUND",
                        "message": f"找不到活动 {activity_id}，先用 get_activities 查 id"}
            _backfill_one(conn, row)
            rep = grademod.analyze_activity(conn, row["id"])
            if not rep.get("splits"):
                return {"error": "E_NO_SPLITS", "message": rep.get("message", "无分段数据"),
                        "activity": row["garmin_activity_id"]}
            for sp in rep["splits"]:
                sp["pace"] = _fmt_pace(sp.get("pace_min_km"))
                sp["gap_pace"] = _fmt_pace(sp.get("gap_pace_min_km"))
            rep["activity"] = {"id": row["id"], "start_time": row["start_time"],
                               "sport_id": row["sport_id"],
                               "distance_km": row["distance_km"]}
            _audit(conn, "get_grade_analysis", {"activity_id": activity_id}, "ok",
                   f"{len(rep['splits'])} 段")
            return rep

        # ── 跨活动统计 ──
        return {
            "range_days": range_days,
            "overall": grademod.pooled_grade_response(conn, range_days=max(range_days, 365)),
            "period_compare": grademod.period_compare(conn, recent_weeks=8),
            "weeks": grademod.weekly_terrain(conn, range_days=range_days),
            "scope_note": (
                "overall 用全周期（≥365 天）池化，样本量足够，斜率可信；"
                "period_compare 每侧需 ≥40 个上坡段才给结论，否则标 insufficient；"
                "weeks 只给分段级地形构成与爬升量，不给每周斜率——"
                "单周上坡段只有 5–15 个，斜率会在 −8~+51 间乱跳，是噪声不是趋势。"),
        }
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
