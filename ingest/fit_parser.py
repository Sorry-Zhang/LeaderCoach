from __future__ import annotations

import json
from pathlib import Path

_SPORT_MAP = {
    "running": "run",
    "cycling": "cycling",
    "climbing": "climb",
    "table_tennis": "table_tennis",
    "strength_training": "strength",
    "trail_running": "trail_run",
    "walking": "other",
    "other": "other",
}

def _frame_fields(frame) -> dict:
    out = {}
    try:
        fields = frame.fields
        for f in fields:
            name = getattr(f, "name", None)
            if name is None:
                continue
            val = getattr(f, "value", None)
            if val is not None:
                out[name] = val
    except Exception:
        pass
    return out

def parse_fit(path) -> dict:
    try:
        import fitdecode
    except ImportError as e:
        raise RuntimeError("缺少 fitdecode 依赖，请执行：pip install fitdecode") from e

    meta: dict = {}
    records: list[dict] = []
    with fitdecode.FitReader(str(path)) as fit:
        for frame in fit:
            if getattr(frame, "frame_type", None) != fitdecode.FIT_FRAME_DATA:
                continue
            name = getattr(frame, "name", "")
            fields = _frame_fields(frame)
            if name == "session":
                meta.update(fields)
            elif name == "record":
                records.append(fields)
            elif name in ("sport", "activity"):
                meta.update(fields)

    def g(*names, default=None):
        for n in names:
            if n in meta and meta[n] is not None:
                return meta[n]
        return default

    sport_raw = g("sport", "sub_sport")
    sport_id = _SPORT_MAP.get(str(sport_raw).lower() if sport_raw else "", "other")

    duration_s = g("total_timer_time", "total_elapsed_time")
    duration_min = round(duration_s / 60.0, 2) if duration_s else None

    dist_m = g("total_distance")
    distance_km = round(dist_m / 1000.0, 3) if dist_m else None

    avg_speed = g("enhanced_avg_speed", "avg_speed")
    pace = None
    if distance_km and duration_min and distance_km > 0:
        pace = round(duration_min / distance_km, 2)

    dynamics = None
    if records:
        def _avg(key):
            vals = [r[key] for r in records if key in r and isinstance(r[key], (int, float))]
            return round(sum(vals) / len(vals), 2) if vals else None
        if any(k in records[0] for k in ("vertical_oscillation", "stance_time", "stride_length")):
            dynamics = {
                "vo_cm": _avg("vertical_oscillation"),
                "gct_ms": _avg("stance_time"),
                "stride_cm": _avg("stride_length"),
            }

    def _scaled(v, lo, hi, div):
        if v is None:
            return None
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return max(lo, min(hi, int(round(x / div))))

    rpe = _scaled(g("workout_rpe"), 1, 10, 10.0)
    feel = _scaled(g("workout_feel"), 0, 5, 20.0)

    st = g("start_time")
    if hasattr(st, "astimezone"):
        from datetime import timedelta, timezone
        st = st.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "garmin_activity_id": g("timestamp", default=None),
        "fit_path": str(path),
        "start_time": st,
        "rpe": rpe,
        "feel": feel,
        "sport_id": sport_id,
        "duration_min": duration_min,
        "distance_km": distance_km,
        "avg_hr": g("avg_heart_rate"),
        "max_hr": g("max_heart_rate"),
        "avg_pace_min_km": pace,
        "elevation_gain_m": g("total_ascent"),
        "power_avg_w": g("avg_power"),
        "power_max_w": g("max_power"),
        "dynamics": dynamics,
        "indoor": None,
        "annotation": None,
    }


# ── 运动分段：lap（手动/自动圈）与 1 km 自动分段 → activity_splits ─────────

def extract_splits(path) -> dict:
    """解析一个活动 FIT 的分段信息。

    返回 {"start_time": "YYYY-MM-DD HH:MM:SS",
          "laps": [{idx, distance_m, timer_s, avg_hr, max_hr, ascent_m, start_time}],
          "km_splits": [{km, pace_min_km, avg_hr, elevation_gain_m}]}
    """
    try:
        import fitdecode
    except ImportError as e:
        raise RuntimeError("缺少 fitdecode，请执行：pip install fitdecode") from e

    laps, records, session = [], [], {}
    with fitdecode.FitReader(str(path)) as fit:
        for frame in fit:
            if getattr(frame, "frame_type", None) != fitdecode.FIT_FRAME_DATA:
                continue
            name = getattr(frame, "name", "")
            fields = _frame_fields(frame)
            if name == "session":
                session.update(fields)
            elif name == "lap":
                laps.append(fields)
            elif name == "record":
                records.append(fields)

    st = session.get("start_time")
    if hasattr(st, "astimezone"):
        from datetime import timedelta, timezone
        st = st.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    lap_rows = []
    for i, l in enumerate(laps):
        ls = l.get("start_time")
        if hasattr(ls, "astimezone"):
            from datetime import timedelta, timezone
            ls = ls.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        lap_rows.append({
            "idx": i,
            "distance_m": l.get("total_distance"),
            "timer_s": l.get("total_timer_time"),
            "avg_hr": l.get("avg_heart_rate"),
            "max_hr": l.get("max_heart_rate"),
            "ascent_m": l.get("total_ascent"),
            "start_time": ls,
        })

    # 1 km 自动分段：按累计距离切分 record
    km_rows = []
    km = 1
    seg_start_t = seg_start_hr_sum = None
    hr_vals, seg_count = [], 0
    seg_start_d = None
    for r in sorted(records, key=lambda x: (x.get("timestamp") is None, x.get("timestamp"))):
        d = r.get("distance")
        t = r.get("timestamp")
        hr = r.get("heart_rate")
        if d is None or t is None:
            continue
        if seg_start_d is None:
            seg_start_d, seg_start_t = d, t
        if hr is not None:
            hr_vals.append(hr)
        if d - seg_start_d >= 1000:
            secs = (t - seg_start_t).total_seconds()
            km_rows.append({
                "km": km,
                "pace_min_km": round(secs / 60, 3) if secs > 0 else None,
                "avg_hr": round(sum(hr_vals) / len(hr_vals)) if hr_vals else None,
                "elevation_gain_m": None,
            })
            km += 1
            seg_start_d, seg_start_t, hr_vals = d, t, []
    return {"start_time": st, "laps": lap_rows, "km_splits": km_rows}


def rebuild_splits(cfg, files, conn=None) -> int:
    """把 FIT 分段写进 activity_splits（activity_splits 在 v1.7 里用 km 作分段键，
    有 laps 时写入圈序号，否则用 1 km 自动分段）。返回写入的分段条数。"""
    from core import db as dbmod
    own = conn is None
    try:
        conn = conn or dbmod.get_conn(cfg)
        n = 0
        for f in files:
            try:
                info = extract_splits(f)
            except Exception:
                continue
            if not info["start_time"]:
                continue
            name = Path(f).name
            act = conn.execute(
                "SELECT id, fit_path FROM activities WHERE fit_path LIKE ? "
                "OR ABS(julianday(substr(start_time,1,19)) - julianday(?)) < 1.0/1440 "
                "ORDER BY (fit_path LIKE ?) DESC LIMIT 1",
                (f"%{name}", info["start_time"], f"%{name}")).fetchone()
            if not act:
                continue
            conn.execute("DELETE FROM activity_splits WHERE activity_id=?", (act["id"],))
            rows = []
            if info["laps"]:
                for l in info["laps"]:
                    secs, dist = l["timer_s"], l["distance_m"]
                    pace = round((secs / 60) / (dist / 1000), 3) if secs and dist else None
                    rows.append((act["id"], l["idx"] + 1, pace, l["avg_hr"], l["ascent_m"]))
            else:
                for k in info["km_splits"]:
                    rows.append((act["id"], k["km"], k["pace_min_km"], k["avg_hr"], None))
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO activity_splits(activity_id, km, pace_min_km, avg_hr, "
                    "elevation_gain_m) VALUES(?,?,?,?,?)", rows)
            # 圈级明细（含每圈起止时刻/计时时长）存进 activities.annotation 的 splits 字段
            if act["fit_path"]:
                detail = json.dumps({"fit": str(f), "laps": info["laps"], "km": info["km_splits"]},
                                    ensure_ascii=False)
                conn.execute("UPDATE activities SET annotation=? WHERE id=?",
                             (detail, act["id"]))
            n += len(rows)
        conn.commit()
        return n
    finally:
        if own and conn is not None:
            conn.close()
