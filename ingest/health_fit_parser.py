from __future__ import annotations

from datetime import datetime
from pathlib import Path

_SLEEP_LEVEL = {0: "unmeasurable", 1: "awake", 2: "light", 3: "deep", 4: "rem"}

def _date_of(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date().isoformat()
    try:
        return str(ts)[:10]
    except Exception:
        return None

def _iter_messages(path):
    try:
        import fitdecode
    except ImportError as e:
        raise RuntimeError("缺少 fitdecode，请执行：pip install fitdecode") from e
    with fitdecode.FitReader(str(path)) as fit:
        for frame in fit:
            if getattr(frame, "frame_type", None) != fitdecode.FIT_FRAME_DATA:
                continue
            fields = {}
            for f in getattr(frame, "fields", []) or []:
                name = getattr(f, "name", None)
                if name:
                    fields[name] = getattr(f, "value", None)
            yield getattr(frame, "name", ""), fields

def discover(path) -> dict:
    out: dict[str, set] = {}
    for name, fields in _iter_messages(path):
        out.setdefault(name, set()).update(k for k in fields if not k.startswith("unknown"))
    return {k: sorted(v) for k, v in sorted(out.items())}

def _first(fields: dict, *names):
    for n in names:
        v = fields.get(n)
        if v is not None:
            return v
    return None

def parse_health_fit(path) -> dict:
    msgs = list(_iter_messages(path))
    by_date: dict[str, dict] = {}

    def _row(d: str) -> dict:
        return by_date.setdefault(d, {
            "date": d,
            "sleep_deep_h": 0.0, "sleep_light_h": 0.0,
            "sleep_rem_h": 0.0, "sleep_awake_h": 0.0,
            "hrv_last_night": None, "hrv_baseline": None,
            "resting_hr": None, "stress_values": [],
        })

    segs = [
        (f.get("timestamp"), f.get("sleep_level"))
        for n, f in msgs
        if n == "sleep_level" and f.get("timestamp") is not None and f.get("sleep_level") is not None
    ]
    segs.sort(key=lambda x: x[0])
    for i, (t, lv) in enumerate(segs):
        if i + 1 >= len(segs):
            continue
        dur = (segs[i + 1][0] - t).total_seconds()
        if dur <= 0 or dur > 4 * 3600:
            continue
        d = _date_of(t)
        if not d:
            continue
        stage = _SLEEP_LEVEL.get(int(lv))
        row = _row(d)
        if stage == "deep":
            row["sleep_deep_h"] += dur / 3600
        elif stage == "light":
            row["sleep_light_h"] += dur / 3600
        elif stage == "rem":
            row["sleep_rem_h"] += dur / 3600
        elif stage == "awake":
            row["sleep_awake_h"] += dur / 3600

    for n, f in msgs:
        if n == "hrv_status_summary":
            d = _date_of(f.get("timestamp"))
            if d:
                row = _row(d)
                row["hrv_last_night"] = _first(f, "last_night_avg", "last_night", "last_night_5min_high")
                row["hrv_baseline"] = _first(f, "weekly_average", "baseline", "baseline_low_upper")

    for n, f in msgs:
        rhr = _first(f, "resting_heart_rate", "resting_hr")
        if rhr is not None:
            d = _date_of(f.get("timestamp"))
            if d:
                _row(d)["resting_hr"] = int(rhr)

    hr_samples: dict[str, list[int]] = {}
    for n, f in msgs:
        if "monitoring" in n and f.get("heart_rate") is not None:
            d = _date_of(f.get("timestamp"))
            hr = f.get("heart_rate")
            if d and isinstance(hr, (int, float)) and 30 <= hr <= 220:
                hr_samples.setdefault(d, []).append(int(hr))
    for d, hrs in hr_samples.items():
        row = by_date.get(d)
        if row is not None and row.get("resting_hr") is None and len(hrs) >= 20:
            hrs.sort()
            row["resting_hr"] = hrs[max(0, int(len(hrs) * 0.05))]

    for n, f in msgs:
        if n in ("stress_level", "stress_level_summary"):
            d = _date_of(_first(f, "timestamp", "stress_level_time"))
            v = _first(f, "stress_level_value", "stress_value")
            if d and v is not None and int(v) >= 0:
                _row(d)["stress_values"].append(int(v))

    out = {}
    for d, row in by_date.items():
        total_sleep = row["sleep_deep_h"] + row["sleep_light_h"] + row["sleep_rem_h"]
        row["sleep_hours"] = round(total_sleep, 2) if total_sleep > 0 else None
        vals = row.pop("stress_values", [])
        row["stress_daily_avg"] = round(sum(vals) / len(vals), 1) if vals else None
        for k in ("sleep_deep_h", "sleep_light_h", "sleep_rem_h", "sleep_awake_h"):
            row[k] = round(row[k], 2) if row[k] > 0 else None
        out[d] = row
    return out

def import_health_dir(folder, conn=None) -> dict:
    folder = Path(folder)
    files = sorted(folder.rglob("*.fit")) if folder.is_dir() else [folder]
    merged: dict[str, dict] = {}
    for f in files:
        try:
            parsed = parse_health_fit(f)
        except Exception:
            continue
        for d, row in parsed.items():
            cur = merged.setdefault(d, {})
            for k, v in row.items():
                if v is not None:
                    cur[k] = v
    if conn is not None:
        write_health_rows(conn, merged.values())
    return merged

_METRIC_COLS = (
    "sleep_hours", "sleep_deep_h", "sleep_light_h", "sleep_rem_h", "sleep_awake_h",
    "hrv_last_night", "hrv_baseline", "resting_hr", "stress_daily_avg",
)

def write_health_rows(conn, rows) -> int:
    n = 0
    for row in rows:
        d = row.get("date")
        if not d:
            continue
        if all(row.get(k) is None for k in _METRIC_COLS):
            continue
        merged = {}
        existing = conn.execute("SELECT * FROM daily_metrics WHERE date=?", (d,)).fetchone()
        if existing:
            merged = {k: existing[k] for k in existing.keys()}
        for k in _METRIC_COLS:
            if row.get(k) is not None:
                merged[k] = row[k]
            elif k not in merged:
                merged[k] = None
        conn.execute(
            "INSERT INTO daily_metrics(date, sleep_hours, sleep_deep_h, sleep_light_h, "
            "sleep_rem_h, sleep_awake_h, hrv_last_night, hrv_baseline, resting_hr, "
            "stress_daily_avg) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "sleep_hours=excluded.sleep_hours, sleep_deep_h=excluded.sleep_deep_h, "
            "sleep_light_h=excluded.sleep_light_h, sleep_rem_h=excluded.sleep_rem_h, "
            "sleep_awake_h=excluded.sleep_awake_h, hrv_last_night=excluded.hrv_last_night, "
            "hrv_baseline=excluded.hrv_baseline, resting_hr=excluded.resting_hr, "
            "stress_daily_avg=excluded.stress_daily_avg",
            (d, *(merged.get(c) for c in _METRIC_COLS)))
        n += 1
    conn.commit()
    return n

if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m ingest.health_fit_parser <某健康FIT文件>")
        sys.exit(1)
    print(json.dumps(discover(sys.argv[1]), ensure_ascii=False, indent=2))
