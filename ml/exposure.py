from __future__ import annotations

import argparse
import json
from datetime import date

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import LeaveOneOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from core import db as dbmod
from core.config import load_config
from ml.events import build_event_panel, ensure_schema
from ml.features import build_panel

WINDOWS = (7, 14, 28, 56)

def _corr(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < 3 or x.std() == 0 or y.std() == 0:
        return None, len(x)
    return float(np.corrcoef(x, y)[0, 1]), len(x)

def event_analysis(conn, start: str, end: str) -> dict:
    ensure_schema(conn)
    ev = build_event_panel(conn, start, end)
    if ev.empty:
        return {"error": "无事件；先用 python -m ml.events --add 录入比赛/计时"}

    out = {"n_events": int(len(ev)), "by_window": {}}
    for w in WINDOWS:
        for col in (f"run_km_{w}d", f"load_{w}d", f"sleep_{w}d", f"hrv_{w}d"):
            if col not in ev.columns:
                continue
            r, n = _corr(ev[col], ev["pace_s_per_km"])
            out["by_window"][col] = {"r": round(r, 3) if r is not None else None,
                                     "n": n}
    return out

def daily_analysis(conn, start: str, end: str, target: str = "sleep_28d") -> dict:
    df = build_panel(conn, start, end)
    if df.empty:
        return {"error": "无数据"}

    future = df["sleep"].rolling(7).mean().shift(-7) if "sleep" in df else None
    y = future if target == "sleep_7d_fwd" else df["hrv"].rolling(7).mean().shift(-7)

    feats = [f"load_m{w}" if f"load_m{w}" in df else None for w in (7, 14)]
    feats = [f for f in feats if f]
    feats += ["tsb", "acwr", "sleep_week_avg", "heat_idx"]
    feats = [f for f in feats if f in df.columns]

    X = df[feats].to_numpy(dtype=float)
    yv = y.to_numpy(dtype=float)
    m = np.isfinite(yv)
    X, yv = X[m], yv[m]
    if len(yv) < 20:
        return {"error": f"有效样本仅 {len(yv)}，至少 20", "features": feats}

    def _fit_eval(cols):
        Xm = df[cols].to_numpy(dtype=float)[m]
        mdl = Pipeline([
            ("imp", SimpleImputer(strategy="median", keep_empty_features=False)),
            ("sc", StandardScaler()),
            ("reg", Ridge(alpha=1.0)),
        ])
        pr = np.zeros(len(yv))
        for tr, te in LeaveOneOut().split(Xm):
            mdl.fit(Xm[tr], yv[tr])
            pr[te] = mdl.predict(Xm[te])
        return (round(float(r2_score(yv, pr)), 3),
                round(float(mean_absolute_error(yv, pr)), 3))

    full_r2, full_mae = _fit_eval(feats)

    base_cols = [c for c in ("sleep_week_avg",) if c in df.columns]
    base_r2 = base_mae = None
    if base_cols:
        base_r2, base_mae = _fit_eval(base_cols)

    return {
        "target": target, "n": int(len(yv)), "features": feats,
        "mae": full_mae, "r2": full_r2,
        "baseline_autocorr": {"features": base_cols, "r2": base_r2, "mae": base_mae},
        "delta_r2_vs_autocorr": (round(full_r2 - base_r2, 3)
                                 if base_r2 is not None else None),
        "note": "留一法；delta_r2>0 才说明训练负荷提供了增量信息",
    }

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-05-06")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--target", default="sleep_7d_fwd",
                   choices=["sleep_7d_fwd", "hrv_7d_fwd"])
    args = p.parse_args(argv)
    conn = dbmod.get_conn(load_config())
    try:
        report = {
            "events": event_analysis(conn, args.start, args.end),
            "daily": daily_analysis(conn, args.start, args.end, args.target),
        }
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    main()
