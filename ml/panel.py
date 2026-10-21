from __future__ import annotations

import argparse
from datetime import date

from core import db as dbmod
from core.config import load_config
from ml.features import build_panel

def _f(v, fmt="{:.1f}"):
    try:
        if v is None or v != v:
            return "-"
        return fmt.format(v)
    except Exception:
        return "-"

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-09-01")
    p.add_argument("--end", default=None)
    p.add_argument("--limit", type=int, default=30)
    args = p.parse_args()
    conn = dbmod.get_conn(load_config())
    df = build_panel(conn, args.start, args.end or date.today().isoformat())
    cols = ["hrv", "hrv_dev", "load", "tsb", "acwr", "sleep_week_avg"]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    sub = df[cols].tail(args.limit)
    print("date        hrv  hrv_dev%    load      tsb   acwr  sleep_wk")
    for d, r in sub.iterrows():
        print(f"{d:%Y-%m-%d}  {_f(r['hrv'], '{:.0f}'):>4}  "
              f"{_f(r['hrv_dev'], '{:+.1f}'):>8}  {_f(r['load']):>7}  "
              f"{_f(r['tsb']):>7}  {_f(r['acwr'], '{:.2f}'):>5}  "
              f"{_f(r['sleep_week_avg'], '{:.1f}'):>8}")

if __name__ == "__main__":
    main()
