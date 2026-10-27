"""v1.8 扩展表：原始载荷存档 + 睡眠分段 + 每日指标明细 + 时间序列。

schema v1.7 的 DDL 冻结在 db/schema.sql，只许追加；本模块只建新表，不改旧表。
所有函数幂等（CREATE TABLE IF NOT EXISTS），可在连接后安全重复调用。
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DDL = (
    # 插件原始返回的 L0 存档（与 archive/fit 的"只写不改"同规）
    "CREATE TABLE IF NOT EXISTS plugin_raw ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " fetched_at TEXT NOT NULL, target_date TEXT, tool TEXT NOT NULL,"
    " scope TEXT, payload_bytes INTEGER, path TEXT NOT NULL,"
    " UNIQUE(tool, target_date, scope))",
    # 夜间 HRV 每 5 分钟读数
    "CREATE TABLE IF NOT EXISTS hrv_readings ("
    " date TEXT NOT NULL, reading_time_local TEXT NOT NULL, reading_time_gmt TEXT,"
    " hrv_value INTEGER, PRIMARY KEY (date, reading_time_local))",
    # 全天压力每 3 分钟时间序列
    "CREATE TABLE IF NOT EXISTS stress_series ("
    " date TEXT NOT NULL, ts_local TEXT NOT NULL, stress_level INTEGER,"
    " PRIMARY KEY (date, ts_local))",
    # Body Battery 时间序列 [ts, status, level, version]
    "CREATE TABLE IF NOT EXISTS body_battery_series ("
    " date TEXT NOT NULL, ts_local TEXT NOT NULL, level INTEGER, status TEXT,"
    " PRIMARY KEY (date, ts_local))",
    # 睡眠分段（阶段时间线）：起 = sleepStartTimestampLocal + 前序阶段秒数
    "CREATE TABLE IF NOT EXISTS sleep_stages ("
    " date TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL,"
    " start_local TEXT, end_local TEXT, seconds INTEGER NOT NULL,"
    " PRIMARY KEY (date, seq))",
    # 官方快照明细（准备度 / 训练状态 / 负荷平衡 / VO2max / 比赛预测 / 心率 / 体重）
    "CREATE TABLE IF NOT EXISTS garmin_metrics ("
    " date TEXT NOT NULL, metric TEXT NOT NULL, value REAL, unit TEXT,"
    " captured_at TEXT, PRIMARY KEY (date, metric))",
    # 夜间分钟级序列（来自 SDK 直读的每日睡眠原始 DTO）
    "CREATE TABLE IF NOT EXISTS sleep_series ("
    " date TEXT NOT NULL, kind TEXT NOT NULL, ts_local TEXT NOT NULL, value REAL,"
    " PRIMARY KEY (date, kind, ts_local))",
    # garmin_activity_id -> activity 名称
    "CREATE TABLE IF NOT EXISTS garmin_activities ("
    " activity_id TEXT PRIMARY KEY, activity_name TEXT, sport_key TEXT,"
    " start_local TEXT, updated_at TEXT)",
)


def ensure(conn) -> None:
    for stmt in DDL:
        conn.execute(stmt)
    conn.commit()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ── 原始载荷存档 ─────────────────────────────────────────────

def raw_root(cfg: dict) -> Path:
    p = cfg["garmin_plugin"].get("raw_dir") or "archive/plugin_raw"
    path = Path(p)
    return path if path.is_absolute() else Path(cfg["_root"]) / path


def save_raw(cfg: dict, conn, tool: str, target_date: str | None, payload,
             range_end: str | None = None, ingest_old=None) -> dict:
    """gzip 存档一份原始返回，并登记到 plugin_raw。

    同 tool+日期+范围只保留一份。若同名存档已超过 1 小时（属于上一轮采集），
    先调用 ingest_old(旧载荷) 把它当历史数据解析，再覆盖成本轮副本——
    这样同一解析器既处理新数据，也重放了被覆盖的上一批数据。
    """
    root = raw_root(cfg)
    day = target_date or "latest"
    scope = f"{day}_{range_end}" if range_end else ""
    dest = root / tool / (f"{scope}.json.gz" if scope else f"{day}.json.gz")
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    written = False
    if dest.exists() and time.time() - dest.stat().st_mtime > 3600:
        if ingest_old is not None:
            try:
                ingest_old(load_raw(dest))
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 重放上一批存档失败 {dest}: {type(e).__name__}: {e}", file=sys.stderr)
        dest.write_bytes(gzip.compress(body, compresslevel=9))
        written = True
    elif not dest.exists():
        dest.write_bytes(gzip.compress(body, compresslevel=9))
        written = True
    conn.execute(
        "INSERT INTO plugin_raw(fetched_at, target_date, tool, scope, payload_bytes, path) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(tool, target_date, scope) DO UPDATE SET "
        "fetched_at=excluded.fetched_at, payload_bytes=excluded.payload_bytes, path=excluded.path",
        (_now(), target_date, tool, scope, len(body), str(dest)))
    return {"tool": tool, "date": target_date, "scope": scope, "bytes": len(body),
            "written": written}


def load_raw(path) -> object:
    return json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))


def _local_iso(ms) -> str | None:
    """Garmin 的毫秒时间戳是 UTC；记录统一转成本地时间（+08:00）。"""
    try:
        ts = datetime.fromtimestamp(int(ms) / 1000).astimezone()
    except (TypeError, ValueError, OSError):
        return None
    return ts.isoformat(timespec="seconds")


def replace_sleep_stages(conn, day: str, levels, start_local) -> int:
    """写入睡眠阶段时间线。

    Garmin 的 sleepLevels 是活动水平码（activityLevel: 0=深睡, 1=浅睡, 2=REM, 3=清醒），
    不是"睡眠等级"。若上游给的是 {startGMT, endGMT} 则按时间切段，否则按秒数累积推算。
    """
    conn.execute("DELETE FROM sleep_stages WHERE date=?", (day,))
    if not isinstance(levels, list) or not levels:
        conn.commit()
        return 0
    try:
        cursor = _parse_local(start_local) if start_local is not None else None
    except (TypeError, ValueError):
        cursor = None
    n = 0
    for item in levels:
        if not isinstance(item, dict):
            continue
        start = item.get("startGMT") or item.get("startLocal")
        end = item.get("endGMT") or item.get("endLocal")
        if start and end:
            # Garmin 的 sleepLevels 用 startGMT/endGMT，按真实时刻换算成本地时间
            s_dt, e_dt = _parse_utc(start), _parse_utc(end)
            secs = int((e_dt - s_dt).total_seconds())
            start_local_iso = s_dt.astimezone().isoformat(timespec="seconds")
            end_local_iso = e_dt.astimezone().isoformat(timespec="seconds")
        elif cursor is not None and item.get("endTimeGMTSeconds") is not None:
            secs = int(item["endTimeGMTSeconds"])
            start_local_iso = cursor.isoformat(timespec="seconds")
            cursor = datetime.fromtimestamp(cursor.timestamp() + secs).astimezone()
            end_local_iso = cursor.isoformat(timespec="seconds")
        else:
            continue
        if secs <= 0:
            continue
        stage = normalize_stage(item.get("activityLevel"), item.get("activityLevelName"))
        conn.execute(
            "INSERT INTO sleep_stages(date, seq, stage, start_local, end_local, seconds) "
            "VALUES(?,?,?,?,?,?)", (day, n, stage, start_local_iso, end_local_iso, secs))
        n += 1
    conn.commit()
    return n


# Garmin 睡眠活动水平码（sleepLevels.activityLevel）
_ACTIVITY_LEVEL = {0: "deep", 1: "light", 2: "rem", 3: "awake"}


def normalize_stage(level, name=None) -> str:
    if name:
        key = str(name).strip().upper()
        mapping = {"DEEP_SLEEP": "deep", "DEEP": "deep", "LIGHT_SLEEP": "light", "LIGHT": "light",
                   "REM": "rem", "AWAKE": "awake"}
        if key in mapping:
            return mapping[key]
    try:
        code = int(level)
    except (TypeError, ValueError):
        return "unknown"
    return _ACTIVITY_LEVEL.get(code, "unknown")


def _parse_local(value) -> datetime:
    """把 '2026-10-25T14:17:13.0'（Garmin 的本地/带时区 ISO）或毫秒时间戳解析成本地 datetime。"""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000).astimezone()
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.astimezone()


def _parse_utc(value) -> datetime:
    """Garmin 的 *GMT 字段没有时区后缀，其实是 UTC。"""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    dt = datetime.fromisoformat(str(value).replace("Z", ""))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


_NIGHT_SERIES = {
    "sleepHeartRate": "sleep_hr",
    "sleepStress": "sleep_stress",
    "sleepBodyBattery": "sleep_body_battery",
    "hrvData": "sleep_hrv",
    "wellnessEpochRespirationDataDTOList": "sleep_resp",
    "sleepRestlessMoments": "restless",
}


def write_night_series(conn, day: str, raw: dict) -> dict:
    """把 SDK 睡眠原始载荷里的分钟级序列拆进 sleep_series（先删后写）。"""
    if not isinstance(raw, dict):
        return {}
    conn.execute("DELETE FROM sleep_series WHERE date=?", (day,))
    counts = {}
    for key, kind in _NIGHT_SERIES.items():
        rows = raw.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts = row.get("startGMT", row.get("startTimeGMT", row.get("timestamp")))
            if ts is None:
                continue
            try:
                iso = _local_iso(ts) if isinstance(ts, (int, float)) else \
                    _parse_local(ts).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                continue
            val = row.get("value", row.get("respirationValue"))
            conn.execute("INSERT OR IGNORE INTO sleep_series(date, kind, ts_local, value) "
                         "VALUES(?,?,?,?)", (day, kind, iso, _f(val)))
            counts[kind] = counts.get(kind, 0) + 1
    conn.commit()
    return counts


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def write_series(conn, day: str, hrv_readings=None, stress_values=None, battery_values=None) -> dict:
    """把当日序列写库（先删后写，保证同日重跑幂等）。"""
    out = {"hrv": 0, "stress": 0, "battery": 0}
    if hrv_readings is not None:
        conn.execute("DELETE FROM hrv_readings WHERE date=?", (day,))
        for r in hrv_readings:
            if not isinstance(r, dict):
                continue
            local = r.get("readingTimeLocal")
            if not local:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO hrv_readings(date, reading_time_local, reading_time_gmt, hrv_value) "
                "VALUES(?,?,?,?)",
                (day, local, r.get("readingTimeGMT"), r.get("hrvValue")))
            out["hrv"] += 1
    if stress_values is not None:
        conn.execute("DELETE FROM stress_series WHERE date=?", (day,))
        for row in stress_values:
            if not isinstance(row, list) or len(row) < 2 or row[1] is None:
                continue
            ts = _local_iso(row[0])
            if ts is None:
                continue
            conn.execute("INSERT OR IGNORE INTO stress_series(date, ts_local, stress_level) "
                         "VALUES(?,?,?)", (day, ts, int(row[1])))
            out["stress"] += 1
    if battery_values is not None:
        conn.execute("DELETE FROM body_battery_series WHERE date=?", (day,))
        for row in battery_values:
            if not isinstance(row, list) or len(row) < 3 or row[2] is None:
                continue
            ts = _local_iso(row[0])
            if ts is None:
                continue
            conn.execute("INSERT OR IGNORE INTO body_battery_series(date, ts_local, level, status) "
                         "VALUES(?,?,?,?)", (day, ts, int(row[2]), str(row[1]) if len(row) > 1 else None))
            out["battery"] += 1
    conn.commit()
    return out


def upsert_metrics(conn, day: str, metrics: dict) -> int:
    """metrics: {metric_name: (value, unit) | value}。"""
    n = 0
    for name, v in (metrics or {}).items():
        if v is None:
            continue
        if isinstance(v, (tuple, list)):
            value, unit = v[0], (v[1] if len(v) > 1 else None)
        else:
            value, unit = v, None
        if not isinstance(value, (int, float)) or value != value:  # None / NaN
            continue
        conn.execute(
            "INSERT INTO garmin_metrics(date, metric, value, unit, captured_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(date, metric) DO UPDATE SET value=excluded.value, unit=excluded.unit, "
            "captured_at=excluded.captured_at",
            (day, name, float(value), unit, _now()))
        n += 1
    conn.commit()
    return n


def upsert_garmin_activity(conn, aid, name, sport_key, start_local) -> None:
    conn.execute(
        "INSERT INTO garmin_activities(activity_id, activity_name, sport_key, start_local, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(activity_id) DO UPDATE SET "
        "activity_name=excluded.activity_name, sport_key=excluded.sport_key, "
        "start_local=excluded.start_local, updated_at=excluded.updated_at",
        (str(aid), name, sport_key, start_local, _now()))
    conn.commit()


def stats(conn) -> dict:
    """给 get_sync_status / get_daily_brief 用的数据覆盖度摘要。"""
    def one(sql):
        return conn.execute(sql).fetchone()[0]
    return {
        "daily_metrics_days": one("SELECT COUNT(*) FROM daily_metrics"),
        "sleep_stage_days": one("SELECT COUNT(DISTINCT date) FROM sleep_stages"),
        "sleep_series_days": one("SELECT COUNT(DISTINCT date) FROM sleep_series"),
        "hrv_reading_days": one("SELECT COUNT(DISTINCT date) FROM hrv_readings"),
        "stress_series_days": one("SELECT COUNT(DISTINCT date) FROM stress_series"),
        "battery_series_days": one("SELECT COUNT(DISTINCT date) FROM body_battery_series"),
        "garmin_metric_days": one("SELECT COUNT(DISTINCT date) FROM garmin_metrics"),
        "garmin_metric_names": one("SELECT COUNT(DISTINCT metric) FROM garmin_metrics"),
        "activity_splits": one("SELECT COUNT(*) FROM activity_splits"),
        "raw_payloads": one("SELECT COUNT(*) FROM plugin_raw"),
    }
