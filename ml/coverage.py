from __future__ import annotations

import argparse
import json
from datetime import date

import pandas as pd

from core import db as dbmod
from core.config import load_config
from ml.features import build_panel

def audit(conn, start: str, end: str) -> dict:
    df = build_panel(conn, start, end)
    out: dict = {"start": start, "end": end, "days": int(len(df))}
    for col, key in (("hrv", "hrv"), ("sleep", "sleep"), ("sleep_week_avg", "sleep_week"),
                     ("rhr", "rhr"), ("stress", "stress")):
        mask = df[col].notna()
        out[f"{key}_days"] = int(mask.sum())
        out[f"{key}_first"] = df.index[mask].min().strftime("%Y-%m-%d") if mask.any() else None
        out[f"{key}_last"] = df.index[mask].max().strftime("%Y-%m-%d") if mask.any() else None
        out[f"{key}_max_gap_days"] = _max_gap(mask)
    out["load_days"] = int((df["load"] > 0).sum())
    both = df["hrv"].notna() & df["sleep"].notna()
    out["hrv_and_sleep_days"] = int(both.sum())
    out["longest_consecutive_ready_days"] = _max_run(both)
    both_w = df["hrv"].notna() & df["sleep_week_avg"].notna()
    out["hrv_and_sleep_week_days"] = int(both_w.sum())
    out["longest_consecutive_hrv_sleepweek_days"] = _max_run(both_w)

    for table, key in (("subjective", "subjective_days"),
                       ("activity_labels", "activity_label_days")):
        try:
            out[key] = int(conn.execute(
                "SELECT COUNT(DISTINCT date) FROM " + table).fetchone()[0])
        except Exception:
            out[key] = 0
    out["label_days_total"] = out["subjective_days"] + out["activity_label_days"]
    return out

def _max_gap(mask: pd.Series) -> int:
    best = cur = 0
    for v in mask.to_numpy():
        cur = 0 if v else cur + 1
        best = max(best, cur)
    return best

def _max_run(mask: pd.Series) -> int:
    best = cur = 0
    for v in mask.to_numpy():
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2024-03-01")
    p.add_argument("--end", default=date.today().isoformat())
    args = p.parse_args()
    conn = dbmod.get_conn(load_config())
    print(json.dumps(audit(conn, args.start, args.end), ensure_ascii=False, indent=2, default=str))
