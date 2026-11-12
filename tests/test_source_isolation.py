"""数据源隔离（v1.15）的回归测试。

背景：Garmin 的 HRV 是夜间平均、COROS 是 RMSSD 均值；静息心率一个是
"睡眠期最低"、一个是"晨起静坐估值"。两源混进同一条 28 天基线会让 G4/G5
误触发，而且不会报错——属于静默错误，必须用测试锁住。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db as dbmod
from core import daily as dailymod
from core.config import DEFAULTS

ROOT = Path(__file__).resolve().parent.parent


def _fresh_cfg(tmp: Path) -> dict:
    (tmp / "db").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "db" / "schema.sql", tmp / "db" / "schema.sql")
    shutil.copy(ROOT / "db" / "seed.sql", tmp / "db" / "seed.sql")
    cfg = deepcopy(DEFAULTS)
    cfg["_root"] = str(tmp)
    cfg["paths"]["db"] = "db/coach.db"
    dbmod.init_db(cfg)
    return cfg


def _seed(conn, source: str, day: str, **kw) -> None:
    cols = ",".join(kw)
    marks = ",".join("?" for _ in kw)
    conn.execute(
        f"INSERT INTO daily_metrics(source,date,{cols}) VALUES(?,?,{marks})",
        (source, day, *kw.values()))


# ── 迁移 ───────────────────────────────────────────────────

def test_schema_has_source_but_sleep_series_does_not():
    """睡眠阶段/日指标/官方指标/引擎日表都要带 source；sleep_series 不带
    （COROS 只给阶段百分比、没有分钟序列，它只可能有一个生产者）。"""
    tmp = Path(tempfile.mkdtemp(prefix="coach_src_"))
    try:
        cfg = _fresh_cfg(tmp)
        conn = dbmod.get_conn(cfg)
        try:
            for t in ("activities", "daily_metrics", "engine_daily",
                      "sleep_stages", "garmin_metrics"):
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
                assert "source" in cols, f"{t} 缺 source 列"
            assert "source" not in {r[1] for r in conn.execute(
                "PRAGMA table_info(sleep_series)")}
            # 主键必须含 source：否则同一天两源会互相覆盖
            for t, pk in (("daily_metrics", "source"), ("engine_daily", "source"),
                          ("sleep_stages", "source"), ("garmin_metrics", "source")):
                info = {r[1]: r[5] for r in conn.execute(f"PRAGMA table_info({t})")}
                assert info["source"] >= 1, f"{t} 主键没包含 source"
            # 活动唯一键改成 (source, external_id)
            idx = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='activities'")]
            assert "idx_act_source_ext" in idx
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_migration_is_idempotent_and_preserves_rows():
    """老库（无 source 列）迁移：行数不变、旧数据归 garmin、重复调用无副作用。"""
    tmp = Path(tempfile.mkdtemp(prefix="coach_mig_"))
    try:
        cfg = _fresh_cfg(tmp)
        conn = dbmod.get_conn(cfg)
        try:
            # 手工退回"老库"形态：删掉 source 维度重建表
            conn.executescript("""
                DROP TABLE IF EXISTS daily_metrics;
                CREATE TABLE daily_metrics (date TEXT PRIMARY KEY,
                  sleep_hours REAL, sleep_deep_h REAL, sleep_light_h REAL,
                  sleep_rem_h REAL, sleep_awake_h REAL, sleep_start TEXT, sleep_end TEXT,
                  sleep_mid TEXT, hrv_last_night REAL, hrv_baseline REAL,
                  hrv_dev_pct REAL, resting_hr INTEGER, rhr_baseline REAL,
                  stress_daily_avg REAL, data_status TEXT DEFAULT 'ok');
                INSERT INTO daily_metrics(date, sleep_hours, hrv_last_night, resting_hr)
                  VALUES ('2026-01-01', 7.0, 50, 48), ('2026-01-02', 8.0, 52, 47);
                DROP INDEX IF EXISTS idx_act_source_ext;
                DROP TABLE IF EXISTS engine_daily;
            """)
            conn.commit()
        finally:
            conn.close()

        conn = dbmod.get_conn(cfg)  # 触发迁移
        try:
            rows = conn.execute(
                "SELECT source, date, sleep_hours, hrv_last_night FROM daily_metrics "
                "ORDER BY date").fetchall()
            assert [(r["source"], r["date"]) for r in rows] == [
                ("garmin", "2026-01-01"), ("garmin", "2026-01-02")]
            assert [r["hrv_last_night"] for r in rows] == [50, 52]
        finally:
            conn.close()

        conn = dbmod.get_conn(cfg)  # 再开一次：必须幂等
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 2
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 隔离 ───────────────────────────────────────────────────

def test_two_sources_same_day_do_not_overwrite():
    """同一天 Garmin 与 COROS 各存一行，互不覆盖。"""
    tmp = Path(tempfile.mkdtemp(prefix="coach_two_"))
    try:
        cfg = _fresh_cfg(tmp)
        conn = dbmod.get_conn(cfg)
        try:
            _seed(conn, "garmin", "2026-03-01", hrv_last_night=50.0, resting_hr=48,
                  sleep_hours=7.5)
            _seed(conn, "coros", "2026-03-01", hrv_last_night=90.0, resting_hr=55,
                  sleep_hours=6.0)
            conn.commit()
            rows = {r["source"]: dict(r) for r in conn.execute(
                "SELECT source, hrv_last_night, resting_hr FROM daily_metrics "
                "WHERE date='2026-03-01'")}
            assert rows["garmin"]["hrv_last_night"] == 50.0
            assert rows["coros"]["hrv_last_night"] == 90.0
            assert rows["garmin"]["resting_hr"] == 48
            assert rows["coros"]["resting_hr"] == 55
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_engine_and_sleep_ignore_other_source():
    """切 active 后引擎与睡眠评分只读当前源；另一源的极端值不得渗入。"""
    tmp = Path(tempfile.mkdtemp(prefix="coach_iso_"))
    try:
        cfg = _fresh_cfg(tmp)
        conn = dbmod.get_conn(cfg)
        try:
            day = "2026-04-10"
            _seed(conn, "garmin", day, hrv_last_night=50.0, resting_hr=48,
                  sleep_hours=7.5, sleep_awake_h=0.2,
                  sleep_start=f"{day}T23:30:00+08:00",
                  sleep_end=f"{day}T07:10:00+08:00", data_status="ok")
            _seed(conn, "coros", day, hrv_last_night=250.0, resting_hr=95,
                  sleep_hours=4.0, sleep_awake_h=1.5,
                  sleep_start=f"{day}T02:00:00+08:00",
                  sleep_end=f"{day}T06:00:00+08:00", data_status="ok")
            conn.commit()

            cfg["source"]["active"] = "garmin"
            g = dailymod.build_daily_brief(conn, cfg, day)["recovery"]
            cfg["source"]["active"] = "coros"
            c = dailymod.build_daily_brief(conn, cfg, day)["recovery"]

            assert g["hrv_last_night"] == 50.0, "Garmin 的 HRV 被 COROS 污染"
            assert g["resting_hr"] == 48
            assert g["sleep_hours"] == 7.5
            assert c["hrv_last_night"] == 250.0
            assert c["resting_hr"] == 95
            assert c["sleep_hours"] == 4.0
            # 睡眠状态分也必须各算各的（4 h 的分数明显低于 7.5 h）
            assert (c["sleep_health"] or {}).get("score") < (g["sleep_health"] or {}).get("score")
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_series_isolated_by_source():
    """CTL/ACWR 的负荷序列只统计当前源的活动，避免同一次训练被算两遍。"""
    tmp = Path(tempfile.mkdtemp(prefix="coach_load_"))
    try:
        cfg = _fresh_cfg(tmp)
        conn = dbmod.get_conn(cfg)
        try:
            for src, d, load in (("garmin", "2026-05-01", 100.0),
                                 ("coros", "2026-05-01", 100.0),
                                 ("garmin", "2026-05-02", 50.0)):
                conn.execute(
                    "INSERT INTO activities(source, external_id, start_time, sport_id,"
                    " duration_min, load_final) VALUES(?,?,?,?,?,?)",
                    (src, f"{src}-{d}", f"{d} 07:00:00", "run", 60, load))
            conn.commit()

            cfg["source"]["active"] = "garmin"
            g = dailymod.compute_engine_daily(conn, cfg, "2026-05-02")
            cfg["source"]["active"] = "coros"
            c = dailymod.compute_engine_daily(conn, cfg, "2026-05-02")
            # Garmin 两天共 150，COROS 只有 100 —— 汇总绝不能是 250
            assert g["ctl"] > c["ctl"], f"garmin ctl={g['ctl']} 应大于 coros ctl={c['ctl']}"
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_coros_and_garmin_activities_coexist_by_external_id():
    """同一运动两源各一条：唯一键是 (source, external_id)，不合并也不冲突。"""
    tmp = Path(tempfile.mkdtemp(prefix="coach_act_"))
    try:
        cfg = _fresh_cfg(tmp)
        conn = dbmod.get_conn(cfg)
        try:
            for src in ("garmin", "coros"):
                conn.execute(
                    "INSERT INTO activities(source, external_id, start_time, sport_id,"
                    " duration_min, distance_km) VALUES(?,?,?,?,?,?)",
                    (src, "same-id", "2026-06-01 08:00:00", "run", 60, 12.0))
            conn.commit()
            assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2
            # 同源重复必须被唯一键拦住
            try:
                conn.execute(
                    "INSERT INTO activities(source, external_id, start_time, sport_id,"
                    " duration_min) VALUES('coros','same-id','2026-06-01 08:00:00','run',60)")
                conn.commit()
                raise AssertionError("同源同 external_id 应该被唯一键拒绝")
            except sqlite3.IntegrityError:
                conn.rollback()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_active_source_defaults_to_garmin():
    """没配 [source] active 时必须落到 garmin，而不是空值或报错。"""
    assert dbmod.active_source({}) == "garmin"
    assert dbmod.active_source(None) == "garmin"
    assert dbmod.active_source({"source": {"active": " COROS "}}) == "coros"


# ── 并发 ───────────────────────────────────────────────────

def test_get_conn_does_not_take_write_lock_every_time():
    """get_conn 每次打开都拿写锁会让 MCP 多线程调用撞 database is locked。
    迁移探测必须是只读的：已迁移的库重复开连接不得抢锁。"""
    import threading
    tmp = Path(tempfile.mkdtemp(prefix="coach_lock_"))
    try:
        cfg = _fresh_cfg(tmp)
        conn = dbmod.get_conn(cfg)
        conn.execute("INSERT INTO activities(source, external_id, start_time, sport_id,"
                     " duration_min) VALUES('garmin','x','2026-07-01 07:00:00','run',60)")
        conn.commit()
        conn.close()

        errs, ok = [], [0]

        def worker():
            try:
                for _ in range(10):
                    c = dbmod.get_conn(cfg)
                    c.execute("SELECT COUNT(*) FROM activities").fetchone()
                    c.close()
                    ok[0] += 1
            except Exception as e:  # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")

        ts = [threading.Thread(target=worker) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errs, f"并发打开连接失败：{errs[:3]}"
        assert ok[0] == 60
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stats_scoped_by_source():
    """覆盖度按源统计：Garmin 的分钟级序列不得出现在 COROS 的 stats 里。"""
    from core import store_ext
    tmp = Path(tempfile.mkdtemp(prefix="coach_stats_"))
    try:
        cfg = _fresh_cfg(tmp)
        conn = dbmod.get_conn(cfg)
        try:
            for src in ("garmin", "coros"):
                conn.execute(
                    "INSERT INTO activities(source, external_id, start_time, sport_id,"
                    " duration_min) VALUES(?,?,?,?,?)",
                    (src, f"{src}-1", "2026-08-01 07:00:00", "run", 60))
            aid = conn.execute("SELECT id FROM activities WHERE source='coros'").fetchone()[0]
            conn.execute("INSERT INTO activity_split_detail(activity_id, split_type, seq,"
                         " distance_m, duration_s) VALUES(?,'km',1,1000,300)", (aid,))
            conn.execute("INSERT INTO daily_metrics(source,date,sleep_hours) "
                         "VALUES('coros','2026-08-01',7.0)")
            conn.execute("INSERT INTO sleep_series(date,kind,ts_local,value) "
                         "VALUES('2026-08-01','hr','2026-08-01T01:00:00',50)")
            conn.commit()

            g = store_ext.stats(conn, "garmin")
            c = store_ext.stats(conn, "coros")
            assert g["activity_split_detail"] == 0
            assert c["activity_split_detail"] == 1
            assert c["daily_metrics_days"] == 1
            # 只有 Garmin 有的分钟级序列：非 garmin 源必须是 null，不是 0 也不是真实值
            assert c["sleep_series_days"] is None
            assert g["sleep_series_days"] == 1
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
