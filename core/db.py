from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import resolve

SCHEMA_VERSION = "1.7"
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
    # v1.8 扩展表（只建新表，不改 v1.7 DDL）
    from . import store_ext
    store_ext.ensure(conn)
    return conn

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
