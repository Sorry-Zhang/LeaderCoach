from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import date

from core import db as dbmod
from core.config import load_config

API = "https://archive-api.open-meteo.com/v1/archive"
DAILY = ("temperature_2m_max,temperature_2m_min,relative_humidity_2m_mean,"
         "apparent_temperature_max,precipitation_sum")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS weather_daily (date TEXT PRIMARY KEY, t_max REAL, "
    "t_min REAL, humidity REAL, apparent_max REAL, precip REAL, source TEXT)")

def fetch(lat: float, lon: float, start: str, end: str) -> list:
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "daily": DAILY, "timezone": "Asia/Shanghai",
    })
    with urllib.request.urlopen(f"{API}?{q}", timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    d = data.get("daily") or {}
    times = d.get("time") or []
    rows = []
    for i, t in enumerate(times):
        def g(key):
            arr = d.get(key) or []
            return arr[i] if i < len(arr) else None
        rows.append({
            "date": t, "t_max": g("temperature_2m_max"),
            "t_min": g("temperature_2m_min"), "humidity": g("relative_humidity_2m_mean"),
            "apparent_max": g("apparent_temperature_max"),
            "precip": g("precipitation_sum"),
        })
    return rows

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--lat", type=float, default=37.87)
    p.add_argument("--lon", type=float, default=112.55)
    args = p.parse_args(argv)

    rows = fetch(args.lat, args.lon, args.start, args.end)
    conn = dbmod.get_conn(load_config())
    try:
        conn.execute(_SCHEMA)
        for r in rows:
            conn.execute(
                "INSERT INTO weather_daily(date,t_max,t_min,humidity,apparent_max,precip,source) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
                "t_max=excluded.t_max, t_min=excluded.t_min, humidity=excluded.humidity, "
                "apparent_max=excluded.apparent_max, precip=excluded.precip, "
                "source=excluded.source",
                (r["date"], r["t_max"], r["t_min"], r["humidity"],
                 r["apparent_max"], r["precip"], "open-meteo"))
        conn.commit()
    finally:
        conn.close()
    print(json.dumps({"days": len(rows), "first": rows[0]["date"] if rows else None,
                      "last": rows[-1]["date"] if rows else None},
                     ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
