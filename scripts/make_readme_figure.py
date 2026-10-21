from __future__ import annotations

import sqlite3
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "coach.db"
OUT = ROOT / "assets" / "load_vs_hrv.png"

START, END = "2026-03-11", "2026-10-20"

def zscore(s: pd.Series) -> pd.Series:
    s = s.astype(float)
    mu, sd = np.nanmean(s), np.nanstd(s)
    return (s - mu) / sd if sd and not np.isnan(sd) else s * 0

def main() -> None:
    conn = sqlite3.connect(DB)
    try:
        load = pd.read_sql(
            "SELECT substr(start_time,1,10) d, SUM(load_final) v "
            "FROM activities GROUP BY substr(start_time,1,10)", conn)
        hrv = pd.read_sql(
            "SELECT date, hrv_last_night FROM daily_metrics "
            "WHERE hrv_last_night IS NOT NULL ORDER BY date", conn)
    finally:
        conn.close()

    load["d"] = pd.to_datetime(load["d"])
    load = load.set_index("d")["v"]
    hrv["date"] = pd.to_datetime(hrv["date"])
    hrv = hrv.set_index("date")["hrv_last_night"]

    idx = pd.date_range(START, END, freq="D")
    load = load.reindex(idx).fillna(0.0)
    hrv = hrv.reindex(idx)

    base = hrv.rolling(14, min_periods=7).mean()
    dev = (hrv - base) / base * 100.0

    x = np.arange(len(idx))
    fig, ax1 = plt.subplots(figsize=(12, 4.0), dpi=150)
    ax1.bar(x, zscore(load), width=1.0, color="#c9d6e3", label="Relative load")
    ax1.set_ylabel("Load (z)")
    ax1.set_xlabel("Day index (dates hidden)")
    ax1.set_xlim(0, len(idx) - 1)

    ax2 = ax1.twinx()
    ax2.plot(x, zscore(dev), color="#1f4e79", lw=0.9, label="HRV deviation")
    ax2.set_ylabel("HRV deviation (z)")

    handles = [ax1.patches[0], ax2.lines[0]]
    ax1.legend(handles, ["Relative load", "HRV deviation"],
               loc="upper left", frameon=True, facecolor="white",
               framealpha=0.9, edgecolor="none", fontsize=9)
    ax1.set_title("Training load vs HRV deviation (single subject, normalized)",
                  fontsize=11, loc="left")
    for s in ("top",):
        ax1.spines[s].set_visible(False)
        ax2.spines[s].set_visible(False)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT, bbox_inches="tight")
    print("saved", OUT)

if __name__ == "__main__":
    main()
