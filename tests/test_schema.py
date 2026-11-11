from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as dbmod

def test_init_21_tables():
    root = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp(prefix="coach_test_"))
    try:
        (tmp / "db").mkdir()
        shutil.copy(root / "db" / "schema.sql", tmp / "db" / "schema.sql")
        shutil.copy(root / "db" / "seed.sql", tmp / "db" / "seed.sql")
        cfg = {"_root": str(tmp), "paths": {"db": "db/coach.db"}}
        res = dbmod.init_db(cfg)
        assert res == "ok"

        conn = dbmod.get_conn(cfg)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        conn.close()
        # v1.7 基础表 21 张 + core/store_ext.py 追加的扩展表
        assert len(names) >= 21, f"预期至少 21 表，实际 {len(names)}"
        for must in ("activities", "activity_splits", "activity_split_detail",
                     "daily_metrics", "plugin_raw", "garmin_metrics"):
            assert must in names, f"缺少表 {must}"

        assert dbmod.check_version(cfg) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def test_audit_append_only():
    root = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp(prefix="coach_test_"))
    try:
        (tmp / "db").mkdir()
        shutil.copy(root / "db" / "schema.sql", tmp / "db" / "schema.sql")
        shutil.copy(root / "db" / "seed.sql", tmp / "db" / "seed.sql")
        cfg = {"_root": str(tmp), "paths": {"db": "db/coach.db"}}
        dbmod.init_db(cfg)
        conn = dbmod.get_conn(cfg)
        dbmod.audit(conn, "test", "cli", {}, "ok", "ok")
        try:
            conn.execute("UPDATE audit_log SET result_status='x'")
            conn.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "audit_log UPDATE 应被 ABORT"
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 项通过")


def test_mcp_tools_all_registered():
    """FastMCP 会扫描装饰器下方紧邻的函数；若把 helper 放在 @mcp.tool() 下面，
    它会被当成工具注册，而真正的工具反而丢失（曾发生：log_subjective 消失、
    多出一个 _normalize_sleep_feel）。这里锁定工具清单与辅助函数边界。"""
    import asyncio
    from mcp_server.tools import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    must = {
        "get_daily_brief", "get_week_plan", "get_plan_vs_actual", "get_sync_status",
        "sync_garmin", "get_sleep_detail", "get_activities", "get_activity_detail",
        "get_grade_analysis", "get_strength_history", "get_cycle_status",
        "get_audit_log", "get_learning_status", "log_subjective", "log_strength",
        "log_activity_rpe", "log_body_metrics", "log_cycle_period", "log_cycle_daily",
        "log_injury", "save_week_plan", "activate_week_plan",
    }
    assert must <= names, f"缺失工具：{sorted(must - names)}"
    leaked = {n for n in names if n.startswith("_")}
    assert not leaked, f"辅助函数被误注册为工具：{sorted(leaked)}"
    assert len(names) == 22, f"工具数应为 22，实际 {len(names)}：{sorted(names)}"

def test_reconcile_stale_sync_jobs():
    """进程中途退出会留下永久 running 的僵尸 job；回收逻辑必须把它收成 interrupted，
    但正在本进程里跑的 job（在内存表中）不能被误杀。"""
    from datetime import datetime, timedelta
    from mcp_server import tools as tmod

    root = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp(prefix="coach_job_"))
    try:
        (tmp / "db").mkdir()
        shutil.copy(root / "db" / "schema.sql", tmp / "db" / "schema.sql")
        shutil.copy(root / "db" / "seed.sql", tmp / "db" / "seed.sql")
        cfg = {"_root": str(tmp), "paths": {"db": "db/coach.db"}}
        dbmod.init_db(cfg)
        conn = dbmod.get_conn(cfg)
        try:
            old = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
            fresh = datetime.now().isoformat(timespec="seconds")
            live = "sync-LIVE"
            with tmod._SYNC_LOCK:
                tmod._SYNC_JOBS[live] = {"job_id": live, "status": "running", "started_at": old}
            try:
                conn.executemany(
                    "INSERT INTO sync_jobs(job_id,status,started_at) VALUES(?,?,?)",
                    [("sync-ZOMBIE", "running", old),
                     (live, "running", old),
                     ("sync-NEW", "running", fresh),
                     ("sync-OLD-DONE", "done", old)])
                conn.commit()
                assert tmod._reconcile_sync_jobs(conn) == 1
                st = {r["job_id"]: r["status"] for r in conn.execute("SELECT job_id,status FROM sync_jobs")}
                assert st["sync-ZOMBIE"] == "interrupted"
                assert st[live] == "running", "内存中仍在运行的 job 被误杀"
                assert st["sync-NEW"] == "running", "刚启动的 job 被误杀"
                assert st["sync-OLD-DONE"] == "done"
                # 幂等：再跑一次不应该改动任何行
                assert tmod._reconcile_sync_jobs(conn) == 0
            finally:
                with tmod._SYNC_LOCK:
                    tmod._SYNC_JOBS.pop(live, None)
            # 进程退出（内存表丢失）后，同一行才应该被回收
            assert tmod._reconcile_sync_jobs(conn) == 1
            st = {r["job_id"]: r["status"] for r in conn.execute("SELECT job_id,status FROM sync_jobs")}
            assert st[live] == "interrupted"
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
