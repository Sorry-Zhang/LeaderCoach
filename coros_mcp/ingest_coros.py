"""COROS 官方 MCP → 本地库。

只做「拉取 + 映射 + 入库」，不含分析。分析全部复用 core/。

数据源固定为 coros（见 `SOURCE`），与 Garmin 的数据在同一张表里靠
(source, external_id) 隔离：同一天、同一节课两源各存一行，基线各算各的。

单位换算（实测，2026-11-12，见 README 与 FACT）
  - 分圈 distance 是**厘米**（100000 = 1 km）
  - avgSpeedV2 是 km/h × 100
  - avgPace 是 s/km，但整次活动汇总组（type=-1）为 null
  - lapGroups 多口径并存：
        type=2  计时口径每公里（合计 = Workout Time）
        type=10 实际时间口径每公里（合计 = Total Time）
        type=11/12  5 km / 10 km 自动圈
        type=-1 整次汇总
  - 步频只在 type=10/11/12 有值，type=2 的 avgCadence 全为 0
  - 官方 adjustedPace（GAP）实测返回 0，不可用，GAP 由 core/grade.py 自算
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import db as dbmod  # noqa: E402

SOURCE = "coros"
CM_PER_KM = 100000.0

# COROS dubbo 运动类型 → sport_registry（不在表里的落到 other，不猜）
SPORT_MAP = {
    100: "run", 101: "run", 102: "run", 103: "run",   # 跑步 / 越野 / 跑步机 / 田径
    104: "run", 105: "run",
    400: "strength", 401: "strength", 402: "strength",
    403: "strength", 1200: "strength",
    500: "climb", 501: "climb",
    600: "table_tennis",
}


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _clock_to_s(text) -> float | None:
    """'1:12:51' / '72:51' / '555.14' -> 秒。"""
    if text is None:
        return None
    s = str(text).strip()
    if ":" not in s:
        return _num(s)
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return None


def _pace_to_min(text) -> float | None:
    """'4:49 /km' -> 4.8167 (min/km)。"""
    if text is None:
        return None
    sec = _clock_to_s(str(text).split("/")[0])
    return round(sec / 60, 4) if sec else None


def _local(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


# ── 文本解析（getActivityDetail / querySportRecords 返回人读文本，不是 JSON）──

def parse_detail_text(text) -> dict:
    out: dict = {}
    for line in str(text).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key == "Workout Time":
            out["duration_s"] = _clock_to_s(val)
        elif key == "Total Time":
            out["elapsed_s"] = _clock_to_s(val)
        elif key == "Distance":
            out["distance_km"] = _num(val.replace("km", ""))
        elif key == "Average Pace":
            out["avg_pace_min_km"] = _pace_to_min(val)
        elif key == "Best Kilometer":
            out["best_km_pace_min_km"] = _pace_to_min(val)
        elif key == "Average Heart Rate":
            out["avg_hr"] = _num(val.replace("bpm", ""))
        elif key == "Elevation Gain / Loss":
            g, _, l = val.partition("/")
            out["elev_gain_m"] = _num(g.replace("m", ""))
            out["elev_loss_m"] = _num(l.replace("m", ""))
        elif key == "Calories":
            out["calories"] = _num(val.replace("kcal", ""))
        elif key == "Training Load":
            out["official_load"] = _num(val)
        elif key == "Aerobic TE":
            out["te_aerobic"] = _num(val)
        elif key == "Anaerobic TE":
            out["te_anaerobic"] = _num(val)
        elif key == "Training Focus":
            out["training_focus"] = val
        elif key == "Performance":
            out["performance"] = val
    return out


def parse_record_text(text) -> list[dict]:
    """querySportRecords 的文本 → [{labelId, sportType, start_ts, end_ts, ...}]。"""
    out: list[dict] = []
    cur: dict | None = None
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[0].isdigit() and ". " in line[:4]:
            if cur:
                out.append(cur)
            cur = {"title": line}
            continue
        if cur is None:
            continue
        if "LabelId:" in line:
            cur["labelId"] = line.split("LabelId:")[1].split("|")[0].strip()
            if "SportType:" in line:
                cur["sportType"] = int(line.split("SportType:")[1].strip())
        elif "startTimestamp=" in line:
            seg = line.split("startTimestamp=")[1]
            cur["start_ts"] = int(seg.split("|")[0])
            if "endTimestamp=" in line:
                cur["end_ts"] = int(line.split("endTimestamp=")[1].split("|")[0])
    if cur:
        out.append(cur)
    return [r for r in out if r.get("labelId")]


# ── 入库 ──────────────────────────────────────────────────

def _archive(raw_dir: Path, tool: str, payload, target: str) -> str:
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / f"{tool}_{target}.json.gz"
    dest.write_bytes(gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), 9))
    return dest.name


def upsert_activity(conn: sqlite3.Connection, rec: dict, detail: dict, laps: dict,
                    raw_files: list[str]) -> int:
    external_id = str(rec["labelId"])
    sport_type = rec.get("sportType")
    sport_id = SPORT_MAP.get(sport_type, "other")

    groups = {g.get("type"): g for g in (laps.get("lapGroups") or [])}
    timed = (groups.get(2) or {}).get("laps") or []
    whole = ((groups.get(-1) or {}).get("laps") or [{}])[0]

    duration_s = detail.get("duration_s") or whole.get("time")
    distance_km = detail.get("distance_km")
    if distance_km is None and whole.get("distance"):
        distance_km = round(whole["distance"] / CM_PER_KM, 3)

    max_hr = max([l.get("maxHr") or 0 for l in timed] or [0]) or None
    avg_hr = detail.get("avg_hr") or whole.get("avgHr")

    notes = []
    if not max_hr:
        notes.append("max_hr 无来源（COROS 分圈未提供）")
    gain = detail.get("elev_gain_m")
    if not gain:
        notes.append("海拔爬升为 0：可能平路，或设备未记录气压高度；GAP 依赖分段海拔")
    if not any((l.get("avgCadence") or 0) for l in timed):
        notes.append("计时口径分圈 avgCadence 全为 0；步频需读实际时间口径（type=10）")
    if detail.get("elapsed_s") and duration_s and detail["elapsed_s"] > duration_s + 60:
        notes.append(f"含暂停 {round((detail['elapsed_s'] - duration_s) / 60, 1)} 分钟")

    annotation = {
        "coros": {
            "labelId": external_id,
            "sportType": sport_type,
            "training_focus": detail.get("training_focus"),
            "performance": detail.get("performance"),
            "calories": detail.get("calories"),
            "elapsed_s": detail.get("elapsed_s"),
            "best_km_pace_min_km": detail.get("best_km_pace_min_km"),
            "lap_group_types": sorted(k for k in groups if k is not None),
            "raw": raw_files,
        },
        "notes": notes,
    }

    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO activities(source, external_id, start_time, sport_id, duration_min,"
        " distance_km, avg_hr, max_hr, avg_pace_min_km, elevation_gain_m, te_aerobic,"
        " te_anaerobic, official_load, load_final, load_path_used, annotation,"
        " calc_version, param_version, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(source, external_id) DO UPDATE SET"
        " start_time=excluded.start_time, sport_id=excluded.sport_id,"
        " duration_min=excluded.duration_min, distance_km=excluded.distance_km,"
        " avg_hr=excluded.avg_hr, max_hr=excluded.max_hr,"
        " avg_pace_min_km=excluded.avg_pace_min_km,"
        " elevation_gain_m=excluded.elevation_gain_m,"
        " te_aerobic=excluded.te_aerobic, te_anaerobic=excluded.te_anaerobic,"
        " official_load=excluded.official_load, annotation=excluded.annotation,"
        " updated_at=excluded.updated_at",
        (SOURCE, external_id, _local(rec.get("start_ts")), sport_id,
         round((duration_s or 0) / 60, 2), distance_km, avg_hr, max_hr,
         detail.get("avg_pace_min_km"), detail.get("elev_gain_m"),
         detail.get("te_aerobic"), detail.get("te_anaerobic"),
         detail.get("official_load"), detail.get("official_load"), "manual",
         json.dumps(annotation, ensure_ascii=False), "coros-1", "p2026-10", now, now))
    row = conn.execute("SELECT id FROM activities WHERE source=? AND external_id=?",
                       (SOURCE, external_id)).fetchone()
    return row[0]


def write_splits(conn: sqlite3.Connection, activity_id: int, laps: dict,
                 start_ts: int | None) -> dict:
    """写两套分段：km（计时口径，与 Garmin FIT 的 km 一致）+ lap（实际时间口径）。

    起止时刻只能由累计时间推导，且只对实际时间口径成立 —— 计时口径不计暂停，
    按它累加会与真实钟点错位，所以 km 段留空。
    """
    groups = {g.get("type"): g for g in (laps.get("lapGroups") or [])}
    out = {}
    for split_type, gtype in (("km", 2), ("lap", 10)):
        glaps = (groups.get(gtype) or {}).get("laps") or []
        if not glaps:
            continue
        conn.execute("DELETE FROM activity_split_detail WHERE activity_id=? AND split_type=?",
                     (activity_id, split_type))
        for l in glaps:
            dist_m = (l.get("distance") or 0) / 100.0        # 厘米 -> 米
            dur = l.get("time")
            st = et = None
            if split_type == "lap" and start_ts and l.get("totalLength") is not None:
                end_off = l["totalLength"]
                st = _local(start_ts + end_off - (dur or 0))
                et = _local(start_ts + end_off)
            conn.execute(
                "INSERT INTO activity_split_detail(activity_id, split_type, seq, distance_m,"
                " duration_s, pace_min_km, avg_hr, max_hr, elevation_gain_m,"
                " elevation_loss_m, start_time, end_time, source)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (activity_id, split_type, int(l.get("lapIndex") or 0), round(dist_m, 1),
                 dur,
                 round(l["avgPace"] / 60, 4) if l.get("avgPace") else None,
                 l.get("avgHr"), l.get("maxHr"), l.get("elevGain"),
                 l.get("totalDescent"), st, et, "coros_api"))
        out[split_type] = len(glaps)
    return out


def upsert_daily(conn: sqlite3.Connection, day: str, values: dict) -> bool:
    """只写非空字段（与 Garmin 侧 _upsert_daily 同规则），不覆盖已有值。"""
    cols = ("sleep_hours", "sleep_deep_h", "sleep_light_h", "sleep_rem_h",
            "sleep_awake_h", "sleep_start", "sleep_end", "hrv_last_night",
            "hrv_baseline", "resting_hr", "stress_daily_avg")
    row = {k: values.get(k) for k in cols if values.get(k) is not None}
    if not row:
        return False
    existing = conn.execute(
        "SELECT * FROM daily_metrics WHERE source=? AND date=?", (SOURCE, day)).fetchone()
    merged = {k: existing[k] for k in existing.keys()} if existing else {}
    merged.pop("source", None)
    merged.pop("date", None)
    merged.update(row)
    keys = list(merged)
    conn.execute(
        f"INSERT INTO daily_metrics(source,date,{','.join(keys)}) "
        f"VALUES({','.join('?' for _ in range(len(keys) + 2))}) "
        "ON CONFLICT(source,date) DO UPDATE SET "
        + ",".join(f"{k}=excluded.{k}" for k in keys),
        [SOURCE, day] + [merged[k] for k in keys])
    return True
