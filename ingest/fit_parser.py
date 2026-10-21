from __future__ import annotations

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
