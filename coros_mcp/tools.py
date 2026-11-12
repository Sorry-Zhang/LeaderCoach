"""coros-coach：独立的 COROS MCP（与 garmin-coach 并列，共用同一个库）。

为什么单独一个 MCP 而不是并进 garmin-coach：
  - 用户可能只用 COROS，不该被迫带 Garmin 依赖（反之亦然）
  - 分析代码不分叉：core/ 完全共用，靠 [source] active 区分口径

为什么共用一个库：
  - 换设备后仍服务同一个人：主观记录、训练计划、伤病历史、目标日期都在同一份
  - 同一天两源各存一行，(source, external_id) 唯一键隔离，基线各算各的
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from core import db as dbmod  # noqa: E402
from core.config import load_config  # noqa: E402

SOURCE = "coros"

# 共用实现：garmin-coach 的那些分析工具在这里原样复用。
# 同一个函数在两个进程里跑，只是 _cfg 指向不同（源不同），分析代码不分叉。
import mcp_server.tools as base  # noqa: E402

mcp = FastMCP(
    "coros-coach",
    instructions=("佳明/COROS 双源运动教练的数据侧：本进程只处理 COROS 数据。"
                  "数据驱动，不虚构数字；写操作受确认协议约束。"),
)


def _coros_cfg() -> dict:
    """把共用配置钉到 coros 源。显式，不从数据推断。"""
    cfg = load_config()
    cfg["source"] = {"active": SOURCE}
    return cfg


_cfg = _coros_cfg()
# 让复用来的分析工具读 coros 源
base._cfg = _cfg
base._store = base.confirmmod.PendingStore(_cfg)


def _conn() -> sqlite3.Connection:
    return dbmod.get_conn(_cfg)


# ── 同步 job（与 garmin 侧同构：异步、落库、启动回收僵尸）──────────

_SYNC_JOBS: dict = {}
_SYNC_LOCK = threading.Lock()
_SYNC_KEEP = 20
_SYNC_RUN_LOCK = threading.Lock()
_STALE_JOB_MINUTES = 30


def _job_persist(conn, j: dict) -> None:
    try:
        res = j.get("result")
        conn.execute(
            "INSERT INTO sync_jobs(job_id, status, started_at, finished_at, elapsed_s, "
            "days, download_fit, full, error, result) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, "
            "finished_at=excluded.finished_at, elapsed_s=excluded.elapsed_s, "
            "error=excluded.error, result=excluded.result",
            (j["job_id"], j.get("status"), j.get("started_at"), j.get("finished_at"),
             j.get("elapsed_s"), j.get("days"), 0, int(bool(j.get("full"))),
             j.get("error"), json.dumps(res, ensure_ascii=False) if res else None))
    except Exception:  # noqa: BLE001
        pass


def _job_write(j: dict) -> None:
    try:
        conn = _conn()
        try:
            _job_persist(conn, j)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def reconcile_sync_jobs(conn=None) -> int:
    """回收僵尸任务：进程中途退出会让 jobs 表里的 running 永不落终态。"""
    owned = set()
    with _SYNC_LOCK:
        owned = {k for k, v in _SYNC_JOBS.items() if v.get("status") == "running"}
    cutoff = (datetime.now() - timedelta(minutes=_STALE_JOB_MINUTES)).isoformat(
        timespec="seconds")
    own_conn = conn is None
    try:
        c = _conn() if own_conn else conn
    except Exception:  # noqa: BLE001
        return 0
    try:
        rows = [r for r in c.execute(
            "SELECT job_id FROM sync_jobs WHERE status='running' "
            "AND job_id LIKE 'coros-%' AND COALESCE(started_at,'') < ?", (cutoff,))
            if r["job_id"] not in owned]
        if not rows:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        for r in rows:
            c.execute("UPDATE sync_jobs SET status='interrupted', finished_at=?, error=? "
                      "WHERE job_id=? AND status='running'",
                      (now, "进程在同步过程中退出，任务中断（启动时自动回收）", r["job_id"]))
        c.commit()
        return len(rows)
    except Exception:  # noqa: BLE001
        try:
            c.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    finally:
        if own_conn:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


try:
    reconcile_sync_jobs()
except Exception:  # noqa: BLE001
    pass


def _job_summary(j: dict) -> dict:
    item = {k: j.get(k) for k in
            ("job_id", "status", "started_at", "finished_at", "days", "error", "elapsed_s")}
    res = j.get("result")
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except (TypeError, ValueError):
            res = None
    if isinstance(res, dict):
        item["result"] = {k: res.get(k) for k in
                          ("records", "inserted", "sleep_days", "hrv_days",
                           "recomputed", "phase") if k in res}
    return item


def _recent_jobs(n: int = 5) -> list:
    merged: dict = {}
    with _SYNC_LOCK:
        for j in _SYNC_JOBS.values():
            merged[j["job_id"]] = dict(j)
    try:
        conn = _conn()
        try:
            for r in conn.execute(
                    "SELECT * FROM sync_jobs WHERE job_id LIKE 'coros-%' "
                    "ORDER BY started_at DESC LIMIT ?", (n * 3,)):
                d = dict(r)
                merged.setdefault(d["job_id"], d)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    ordered = sorted(merged.values(), key=lambda j: j.get("started_at") or "",
                     reverse=True)
    return [_job_summary(j) for j in ordered[:n]]


# ── 工具 ──────────────────────────────────────────────────

@mcp.tool()
def sync_coros(days: int = 7, wait: bool = False) -> dict:
    """从 COROS 官方 MCP 拉取最近 N 天的活动与每日生理，写入本地库。

    默认异步：立即返回 job_id，用 get_sync_status() 轮询。

    拉取内容：
      - 活动：querySportRecords → getActivityDetail → queryActivityLapData
        （两套分段：计时口径每公里 + 实际时间口径设备圈）
      - 每日生理：睡眠、睡眠 HRV、静息心率、压力、平均心率
        （这些只在绑定设备后有数据；无设备时返回明确说明，不写零值）

    原始返回全部 gzip 存档到 data/coros/raw/，服务端改版时可复盘。
    数据写入时带 source='coros'，与 Garmin 的记录按 (source, external_id) 隔离。
    """
    if not wait:
        return _start_job(days)
    return _run_sync(days)


def _start_job(days: int) -> dict:
    jid = f"coros-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    started = datetime.now()
    with _SYNC_LOCK:
        if len(_SYNC_JOBS) > _SYNC_KEEP:
            for k in sorted(_SYNC_JOBS,
                            key=lambda k: _SYNC_JOBS[k].get("started_at") or "")[:-_SYNC_KEEP]:
                _SYNC_JOBS.pop(k, None)
        _SYNC_JOBS[jid] = {"job_id": jid, "status": "running", "days": days,
                           "full": True,
                           "started_at": started.isoformat(timespec="seconds")}
    _job_write(dict(_SYNC_JOBS[jid]))

    def _worker():
        try:
            with _SYNC_RUN_LOCK:   # 串行，避免两个同步抢同一份 token / 同一批表
                res = _run_sync(days)
            with _SYNC_LOCK:
                _SYNC_JOBS[jid].update(
                    status="done", result=res,
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    elapsed_s=round((datetime.now() - started).total_seconds(), 1))
                snap = dict(_SYNC_JOBS[jid])
            _job_write(snap)
        except BaseException as e:  # noqa: BLE001
            with _SYNC_LOCK:
                _SYNC_JOBS[jid].update(
                    status="failed", error=f"{type(e).__name__}: {e}",
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    elapsed_s=round((datetime.now() - started).total_seconds(), 1))
                snap = dict(_SYNC_JOBS[jid])
            _job_write(snap)

    threading.Thread(target=_worker, daemon=True, name=jid).start()
    return {"job_id": jid, "status": "running", "days": days,
            "hint": "后台同步中，用 get_sync_status() 轮询同名 job_id。"}


def _run_sync(days: int) -> dict:
    from . import sync as syncmod
    days = max(1, int(days))
    end = date.today()
    start = end - timedelta(days=days - 1)
    from scripts import coros_client as cc
    notes: list[str] = []
    token = cc.ensure_token()

    conn = _conn()
    try:
        acts = syncmod.sync_activities(conn, token, start, end, notes)
        health = syncmod.sync_health(conn, token, end, notes)
        touched = [r["start_time"][:10] for r in conn.execute(
            "SELECT start_time FROM activities WHERE source=? "
            "AND substr(start_time,1,10) >= ? AND substr(start_time,1,10) <= ?",
            (SOURCE, start.isoformat(), end.isoformat()))]
        eng = syncmod.recompute_engine(conn, _cfg, touched, notes)
        conn.execute("INSERT INTO meta(key,value) VALUES('last_sync_at_coros',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (datetime.now().isoformat(timespec="seconds"),))
        conn.commit()
        from core import store_ext
        coverage = store_ext.stats(conn, SOURCE)
    finally:
        conn.close()

    return {"records": acts["records"], "inserted": acts["inserted"],
            "activities": acts["activities"], "health": health,
            "engine": eng, "notes": notes, "coverage": coverage,
            "range": {"start": start.isoformat(), "end": end.isoformat()}}


@mcp.tool()
def get_sync_status() -> dict:
    """COROS 同步状态：最近同步时间、任务历史、数据覆盖度。

    与 garmin 侧的 get_sync_status 是两套：这里只统计 source='coros' 的数据，
    job 列表也只看 coros- 前缀的任务。
    """
    conn = _conn()
    try:
        last = conn.execute(
            "SELECT value FROM meta WHERE key='last_sync_at_coros'").fetchone()
        reconcile_sync_jobs(conn)
        from core import store_ext
        return {
            "source": SOURCE,
            "last_sync_at": last["value"] if last else None,
            "activity_count": conn.execute(
                "SELECT COUNT(*) c FROM activities WHERE source=?", (SOURCE,)
            ).fetchone()["c"],
            "coverage": store_ext.stats(conn, SOURCE),
            "sync_jobs": _recent_jobs(5),
            "note": ("睡眠/HRV/压力依赖绑定的 COROS 设备；未绑定时这些项恒为 0 天，"
                     "属正常现象。COROS 不支持从外部导入睡眠数据。"),
        }
    finally:
        conn.close()


@mcp.tool()
def get_coros_server_tools() -> dict:
    """列出 COROS 官方 MCP 服务端当前提供的全部工具及其参数。

    远端服务的工具清单可能随版本变化，这里直接问服务端，不写死。
    """
    from scripts import coros_client as cc
    tools = cc.list_tools()
    return {"count": len(tools),
            "tools": [{"name": t.get("name"),
                       "description": (t.get("description") or "").splitlines()[0][:160],
                       "params": list(((t.get("inputSchema") or {}).get("properties") or {}).keys())}
                      for t in tools]}


@mcp.tool()
def get_coros_profile() -> dict:
    """COROS 账号资料与绑定设备（用于确认数据可用性）。

    没有绑定设备时，睡眠/HRV/压力不会有任何数据——先看这里再排查同步。
    """
    from scripts import coros_client as cc
    info = cc.structured(cc.call_tool("queryUserInfo", {}))
    dev = cc.structured(cc.call_tool("queryDevices", {}))
    return {"profile": info, "devices": dev,
            "devices_bound": "no devices" not in str(dev).lower()}


# 复用 garmin-coach 的分析与记录工具（同一套 core/，只是源不同）
_REUSED = (
    "get_daily_brief", "get_week_plan", "get_plan_vs_actual",
    "get_sleep_detail", "get_activities", "get_activity_detail",
    "get_grade_analysis", "get_strength_history", "get_cycle_status",
    "get_audit_log", "get_learning_status", "log_subjective", "log_strength",
    "log_activity_rpe", "log_body_metrics", "log_cycle_period",
    "log_cycle_daily", "log_injury", "save_week_plan", "activate_week_plan",
)
for _name in _REUSED:
    _fn = getattr(base, _name)
    globals()[_name] = _fn      # 同时暴露成模块属性，便于 CLI/测试直接调用
    mcp.tool()(_fn)
del _name, _fn
