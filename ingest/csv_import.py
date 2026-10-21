from __future__ import annotations

import csv
from pathlib import Path

_SPORT_MAP = {
    "running": "run",
    "跑步": "run",
    "cycling": "cycling",
    "骑行": "cycling",
    "climbing": "climb",
    "攀岩": "climb",
    "table tennis": "table_tennis",
    "乒乓球": "table_tennis",
    "strength": "strength",
    "strength training": "strength",
    "力量训练": "strength",
    "hiking": "other",
    "walking": "other",
    "other": "other",
}

def _to_minutes(value) -> float | None:
    if not value:
        return None
    s = str(value).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 3:
                h, m, sec = parts
                return round(float(h) * 60 + float(m) + float(sec) / 60, 2)
            if len(parts) == 2:
                m, sec = parts
                return round(float(m) + float(sec) / 60, 2)
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None

def _to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None

def _to_int(value):
    f = _to_float(value)
    return int(round(f)) if f is not None else None

def _pace_to_min_km(value) -> float | None:
    if not value:
        return None
    s = str(value).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            m, sec = parts[0], parts[1]
            return round(float(m) + float(sec) / 60, 2)
        except (ValueError, IndexError):
            return None
    return _to_float(value)

def find_activities_csv(folder) -> Path | None:
    folder = Path(folder)
    if folder.is_file():
        return folder
    if not folder.exists():
        return None
    for p in folder.rglob("*.csv"):
        if p.name.lower() in ("activities.csv", "活动.csv", "activity.csv"):
            return p
    for p in folder.rglob("*.csv"):
        try:
            with p.open("r", encoding="utf-8-sig", errors="ignore") as f:
                head = f.readline()
            if "Activity Type" in head or "活动类型" in head:
                return p
        except Exception:
            continue
    return None

def import_activities_csv(path) -> list[dict]:
    path = Path(path)
    out = []
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            atype = (row.get("Activity Type") or row.get("活动类型") or "").strip().lower()
            sport_id = _SPORT_MAP.get(atype, "other")
            start = (row.get("Date") or row.get("日期") or "").strip()
            if not start:
                continue
            start = start[:10]
            duration = _to_minutes(row.get("Time") or row.get("时间") or row.get("Duration"))
            distance = _to_float(row.get("Distance") or row.get("距离"))
            avg_hr = _to_int(row.get("Avg HR") or row.get("平均心率"))
            max_hr = _to_int(row.get("Max HR") or row.get("最大心率"))
            pace = _pace_to_min_km(row.get("Avg Pace") or row.get("平均配速"))
            elev = _to_float(row.get("Elev Gain") or row.get("Total Ascent") or row.get("累计爬升"))
            power_avg = _to_int(row.get("Avg Power") or row.get("平均功率"))
            power_max = _to_int(row.get("Max Power") or row.get("最大功率"))

            out.append({
                "garmin_activity_id": f"csv:{start}:{atype}:{len(out)}",
                "fit_path": None,
                "start_time": start,
                "sport_id": sport_id,
                "duration_min": duration,
                "distance_km": distance,
                "avg_hr": avg_hr,
                "max_hr": max_hr,
                "avg_pace_min_km": pace,
                "elevation_gain_m": elev,
                "power_avg_w": power_avg,
                "power_max_w": power_max,
                "dynamics": None,
                "indoor": None,
                "annotation": f"csv:{atype}",
            })
    return out
