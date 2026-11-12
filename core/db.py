from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import resolve

SCHEMA_VERSION = "1.8"
ENGINE_VERSION = "1.3"
PARAM_VERSION = "p2026-10"

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def db_path(cfg: dict) -> Path:
    return resolve(cfg, cfg["paths"]["db"])

def get_conn(cfg: dict) -> sqlite3.Connection:
    p = db_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    # 先迁数据源隔离（不依赖 store_ext 建表：老库的同名表要先重建主键）
    _migrate_source_dimension(conn)
    # v1.8 扩展表（只建新表，不改 v1.7 DDL）
    from . import store_ext
    store_ext.ensure(conn)
    return conn


# ── v1.15：数据源隔离 ──────────────────────────────────────

# 每张表需要带 source 维度；PK 改成 (source, ...) 只能靠重建表。
# sleep_series 不在其中：它是 Garmin SDK 直读的分钟级序列，COROS 官方接口
# 只给睡眠阶段百分比、没有分钟序列，它只可能有一个生产者。
_SOURCE_TABLES = (
    # (表名, 重建 DDL, 需要复制的列)
    ("daily_metrics",
     """CREATE TABLE daily_metrics__m (
     source TEXT NOT NULL DEFAULT 'garmin', date TEXT NOT NULL,
     sleep_hours REAL, sleep_deep_h REAL, sleep_light_h REAL,
     sleep_rem_h REAL, sleep_awake_h REAL,
     sleep_start TEXT, sleep_end TEXT, sleep_mid TEXT,
     hrv_last_night REAL, hrv_baseline REAL, hrv_dev_pct REAL,
     resting_hr INTEGER, rhr_baseline REAL,
     stress_daily_avg REAL,
     data_status TEXT DEFAULT 'ok' CHECK (data_status IN ('ok','partial','missing')),
     PRIMARY KEY (source, date))""",
     ("date", "sleep_hours", "sleep_deep_h", "sleep_light_h", "sleep_rem_h",
      "sleep_awake_h", "sleep_start", "sleep_end", "sleep_mid",
      "hrv_last_night", "hrv_baseline", "hrv_dev_pct", "resting_hr",
      "rhr_baseline", "stress_daily_avg", "data_status")),
    ("engine_daily",
     """CREATE TABLE engine_daily__m (
     source TEXT NOT NULL DEFAULT 'garmin', date TEXT NOT NULL,
     ctl REAL, atl REAL, tsb REAL,
     acwr REAL, ewma7 REAL, ewma28 REAL,
     readiness_score REAL,
     readiness_confidence TEXT CHECK (readiness_confidence IN ('high','medium','low')),
     pillars TEXT, inputs_used TEXT,
     gates_triggered TEXT, flags TEXT,
     param_version TEXT, calc_version TEXT,
     PRIMARY KEY (source, date))""",
     ("date", "ctl", "atl", "tsb", "acwr", "ewma7", "ewma28", "readiness_score",
      "readiness_confidence", "pillars", "inputs_used", "gates_triggered", "flags",
      "param_version", "calc_version")),
    ("sleep_stages",
     """CREATE TABLE sleep_stages__m (
     source TEXT NOT NULL DEFAULT 'garmin', date TEXT NOT NULL, seq INTEGER NOT NULL,
     stage TEXT NOT NULL, start_local TEXT, end_local TEXT, seconds INTEGER NOT NULL,
     PRIMARY KEY (source, date, seq))""",
     ("date", "seq", "stage", "start_local", "end_local", "seconds")),
    ("garmin_metrics",
     """CREATE TABLE garmin_metrics__m (
     source TEXT NOT NULL DEFAULT 'garmin', date TEXT NOT NULL, metric TEXT NOT NULL,
     value REAL, unit TEXT, captured_at TEXT,
     PRIMARY KEY (source, date, metric))""",
     ("date", "metric", "value", "unit", "captured_at")),
)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _source_migration_plan(conn) -> tuple[bool, list[str]]:
    """只读探测：返回 (activities 需加列?, 需重建的表名列表)。

    必须与真正的迁移分开：迁移要拿写锁，而 get_conn 每次都会跑探测。
    早期实现直接在 get_conn 里 BEGIN IMMEDIATE，导致每个连接都抢一次写锁，
    MCP 多线程调用时会撞上 database is locked。
    """
    acts_add = False
    if _table_exists(conn, "activities"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(activities)")}
        acts_add = ("source" not in cols) or ("external_id" not in cols)
    rebuild = []
    for name, _ddl, _cols in _SOURCE_TABLES:
        if not _table_exists(conn, name):
            continue
        if "source" not in {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}:
            rebuild.append(name)
    return acts_add, rebuild


def _stamp_version_if_stale(conn) -> bool:
    """结构已是新版、但版本号还落后时补上版本戳。

    用于修两种情况：迁移中途崩溃导致号没写上；或旧代码迁移完但没改常量。
    只读探测确认结构就绪才动库，不抢写锁。
    """
    if not _table_exists(conn, "meta"):
        return False
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None or row[0] == SCHEMA_VERSION:
        return False
    acts_add, rebuild = _source_migration_plan(conn)
    if acts_add or rebuild:
        return False  # 结构还不完整，交给迁移
    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (SCHEMA_VERSION,))
    conn.commit()
    return True


def _migrate_source_dimension(conn) -> bool:
    """幂等：给数据表加上 source 维度，现有行归为 garmin。

    先只读探测，确实需要迁移时才拿写锁——否则每次 get_conn 都会争锁。
    返回 True 说明本次真的动了结构。单个事务，失败整体回滚。
    """
    acts_add, rebuild = _source_migration_plan(conn)
    if not acts_add and not rebuild:
        _stamp_version_if_stale(conn)
        return False  # 已迁移（或全新库），不碰写锁

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 拿到写锁后重新探测：另一个进程可能刚迁完
        acts_add, rebuild = _source_migration_plan(conn)

        if _table_exists(conn, "activities"):
            cols = {r[1] for r in conn.execute("PRAGMA table_info(activities)")}
            if "source" not in cols:
                conn.execute("ALTER TABLE activities ADD COLUMN source TEXT NOT NULL "
                             "DEFAULT 'garmin'")
            if "external_id" not in cols:
                conn.execute("ALTER TABLE activities ADD COLUMN external_id TEXT")
            if acts_add:
                # Garmin 家的 external_id 就是它自己的 activityId
                conn.execute("UPDATE activities SET external_id = garmin_activity_id "
                             "WHERE external_id IS NULL AND garmin_activity_id IS NOT NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_act_source_ext "
                         "ON activities(source, external_id)")

        for name, ddl, cols in _SOURCE_TABLES:
            if name not in rebuild:
                continue
            # 只复制两表都有的列：从更老的版本升上来时个别列可能还不存在，
            # 直接 SELECT 全列会 OperationalError 把整个迁移带崩。
            have_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
            if "date" not in have_cols:
                raise RuntimeError(
                    f"{name} 缺少 date 列，无法迁移，请先备份后人工处理")
            use_cols = [c for c in cols if c in have_cols]
            col_list = ", ".join(use_cols)
            marks = ", ".join("?" for _ in use_cols)
            rows = [tuple(r) for r in conn.execute(f"SELECT {col_list} FROM {name}")]
            conn.execute(ddl)
            conn.executemany(
                f"INSERT INTO {name}__m(source, {col_list}) VALUES('garmin', {marks})", rows)
            conn.execute(f"DROP TABLE {name}")
            conn.execute(f"ALTER TABLE {name}__m RENAME TO {name}")

        conn.commit()
        # 结构真的变了才把版本号跟上：老库（1.7）升到 1.8 后，
        # check_version / verify 的读数才不会继续显示旧号。
        if (acts_add or rebuild) and _table_exists(conn, "meta"):
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SCHEMA_VERSION,))
            conn.commit()
        return bool(acts_add or rebuild)
    except BaseException:
        conn.rollback()
        raise


def active_source(cfg: dict | None = None) -> str:
    """当前生效的数据源。显式配置，不从数据推断（两台设备同戴会一天一个口径）。"""
    src = ((cfg or {}).get("source") or {}).get("active") or "garmin"
    return str(src).strip().lower()

def _split_statements(script: str) -> list[str]:
    script = script.replace("\r\n", "\n")
    out = []
    for chunk in script.split(";\n"):
        s = chunk.strip()
        if s:
            out.append(s + ";")
    return out

def init_db(cfg: dict) -> str:
    root = Path(cfg["_root"])
    conn = get_conn(cfg)
    try:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if has_meta:
            raise RuntimeError(
                "数据库已初始化。如需重建，请先备份并删除 "
                f"{db_path(cfg)} 后重试（或使用 verify 校验现有库）。"
            )
        conn.execute("BEGIN")
        for stmt in _split_statements((root / "db" / "schema.sql").read_text(encoding="utf-8")):
            conn.execute(stmt)
        for stmt in _split_statements((root / "db" / "seed.sql").read_text(encoding="utf-8")):
            conn.execute(stmt)
        for key, value in [
            ("schema_version", SCHEMA_VERSION),
            ("engine_version", ENGINE_VERSION),
            ("param_version", PARAM_VERSION),
            ("red_s_state", "inactive"),
            ("obs_state", "inactive"),
        ]:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        conn.commit()
        res = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if res != "ok":
            raise RuntimeError(f"integrity_check failed: {res}")
        return res
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def check_version(cfg: dict) -> None:
    conn = get_conn(cfg)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise RuntimeError("数据库未初始化，请先运行：python main.py init")
        if row["value"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"schema_version 不匹配：库为 {row['value']}，程序期望 {SCHEMA_VERSION}。"
            )
    finally:
        conn.close()

def backup(cfg: dict) -> Path:
    src = db_path(cfg)
    dest_dir = resolve(cfg, cfg["paths"]["backup_dir"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"coach_{datetime.now().strftime('%Y%m%d')}.db"
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return dest

def verify(cfg: dict, db_file: str | None = None) -> dict:
    p = Path(db_file) if db_file else db_path(cfg)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        tables = conn.execute(
            "SELECT count(*) AS c FROM sqlite_master WHERE type='table'"
        ).fetchone()["c"]
        return {
            "integrity": integrity,
            "schema_version": row["value"] if row else None,
            "tables": tables,
        }
    finally:
        conn.close()

def audit(
    conn: sqlite3.Connection,
    tool_name: str,
    caller_context: str,
    params,
    result_status: str,
    result_summary: str,
) -> None:
    params_json = json.dumps(params, ensure_ascii=False, default=str) if params is not None else None
    conn.execute(
        "INSERT INTO audit_log(ts,tool_name,caller_context,params,result_status,result_summary) "
        "VALUES(?,?,?,?,?,?)",
        (_now_iso(), tool_name, caller_context, params_json, result_status, result_summary),
    )
    conn.commit()
