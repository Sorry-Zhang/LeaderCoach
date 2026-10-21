from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

import pandas as pd

from core import db as dbmod
from core.config import load_config
from ml.gap import grade_adjusted_pace

WINDOWS = (7, 14, 28, 56)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL,
    distance_m REAL,
    time_s REAL,
    name TEXT,
    note TEXT,
    created_at TEXT,
    equivalence TEXT DEFAULT 'race',
    elevation_gain_m REAL,
    avg_hr INTEGER,
    effort TEXT DEFAULT 'max',
    date_precision TEXT DEFAULT 'day',
    UNIQUE(event_date, event_type, distance_m)
);
"""

def ensure_schema(conn):
    conn.executescript(_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    for col, ddl in (("equivalence", "TEXT DEFAULT 'race'"),
                     ("elevation_gain_m", "REAL"), ("avg_hr", "INTEGER"),
                     ("effort", "TEXT DEFAULT 'max'"),
                     ("date_precision", "TEXT DEFAULT 'day'")):
        if col not in have:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
    conn.commit()

def add_event(conn, day: str, etype: str, distance_m=None, time_s=None,
              name=None, note=None, equivalence: str = "race",
              elevation_gain_m=None, avg_hr=None, effort: str = "max"):
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO events(event_date,event_type,distance_m,time_s,name,note,created_at,"
        "equivalence,elevation_gain_m,avg_hr,effort) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(event_date,event_type,distance_m) DO UPDATE SET "
        "time_s=excluded.time_s, name=excluded.name, note=excluded.note, "
        "equivalence=excluded.equivalence, elevation_gain_m=excluded.elevation_gain_m, "
        "avg_hr=excluded.avg_hr, effort=excluded.effort",
        (day, etype, distance_m, time_s, name, note,
         pd.Timestamp.now().isoformat(timespec="seconds"), equivalence,
         elevation_gain_m, avg_hr, effort))
    conn.commit()

def _window_days(day: date, n: int):
    return day - timedelta(days=n - 1), day

def build_event_panel(conn, start: str, end: str) -> pd.DataFrame:
    ensure_schema(conn)
    ev = conn.execute(
        "SELECT * FROM events WHERE event_date BETWEEN ? AND ? ORDER BY event_date",
        (start, end)).fetchall()
    if not ev:
        return pd.DataFrame()

    tr = {r["date"]: dict(r) for r in conn.execute(
        "SELECT substr(start_time,1,10) date, SUM(distance_km) km, "
        "SUM(duration_min) dur, COUNT(*) n FROM activities WHERE sport_id='run' "
        "GROUP BY substr(start_time,1,10)")}
    load = {r["date"]: float(r["v"] or 0.0) for r in conn.execute(
        "SELECT substr(start_time,1,10) date, SUM(load_final) v "
        "FROM activities GROUP BY substr(start_time,1,10)")}
    dm = {r["date"]: dict(r) for r in conn.execute(
        "SELECT date, sleep_hours, hrv_last_night, resting_hr FROM daily_metrics")}

    rows = []
    for e in ev:
        day = date.fromisoformat(e["event_date"][:10])
        raw_pace = (e["time_s"] / (e["distance_m"] / 1000.0)
                    if e["time_s"] and e["distance_m"] else None)
        gap, ratio = (grade_adjusted_pace(e["distance_m"] / 1000.0, e["time_s"],
                                          e["elevation_gain_m"])
                      if raw_pace else (None, None))
        row = {"event_date": e["event_date"], "event_type": e["event_type"],
               "distance_m": e["distance_m"],
               "pace_s_per_km": round(raw_pace, 2) if raw_pace else None,
               "gap_pace_s_per_km": round(gap, 2) if gap else None,
               "grade_ratio": round(ratio, 4) if ratio else None,
               "ascent_m": e["elevation_gain_m"],
               "effort": e["effort"], "equivalence": e["equivalence"],
               "date_precision": e["date_precision"]}
        for w in WINDOWS:
            a, b = _window_days(day, w)
            days = [str(a + timedelta(days=i)) for i in range(w)]
            km = sum((tr.get(d) or {}).get("km") or 0.0 for d in days)
            dur = sum((tr.get(d) or {}).get("dur") or 0.0 for d in days)
            run_n = sum(int((tr.get(d) or {}).get("n") or 0) for d in days)
            ld = sum(load.get(d, 0.0) for d in days)
            sl = [dm.get(d, {}).get("sleep_hours") for d in days]
            hr = [dm.get(d, {}).get("hrv_last_night") for d in days]
            rr = [dm.get(d, {}).get("resting_hr") for d in days]
            sl = [x for x in sl if x is not None]
            hr = [x for x in hr if x is not None]
            rr = [x for x in rr if x is not None]
            row[f"run_km_{w}d"] = round(km, 2)
            row[f"run_dur_{w}d"] = round(dur, 1)
            row[f"run_sessions_{w}d"] = run_n
            row[f"load_{w}d"] = round(ld, 1)
            row[f"sleep_{w}d"] = round(sum(sl) / len(sl), 2) if sl else None
            row[f"hrv_{w}d"] = round(sum(hr) / len(hr), 1) if hr else None
            row[f"rhr_{w}d"] = round(sum(rr) / len(rr), 1) if rr else None
            row[f"cover_sleep_{w}d"] = len(sl)
            row[f"cover_hrv_{w}d"] = len(hr)
            row[f"cover_rhr_{w}d"] = len(rr)
        rows.append(row)
    return pd.DataFrame(rows)

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-05-06")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--add", nargs=3, metavar=("DATE", "TYPE", "TIME_S"),
                   help="新增事件，如 --add 2026-06-15 5000 1234.5")
    p.add_argument("--distance", type=float, default=None)
    p.add_argument("--name", default=None)
    args = p.parse_args(argv)

    conn = dbmod.get_conn(load_config())
    try:
        if args.add:
            d, t, ts = args.add
            add_event(conn, d, t, args.distance, float(ts), args.name)
            print(f"已记录事件 {d} {t} time={ts}s distance={args.distance}")
        df = build_event_panel(conn, args.start, args.end)
        if df.empty:
            print("尚无事件。用 --add 录入一场比赛或计时测试。")
        else:
            print(df.to_string(index=False, max_colwidth=20))
    finally:
        conn.close()

if __name__ == "__main__":
    main()
