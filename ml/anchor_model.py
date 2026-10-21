from __future__ import annotations

import argparse
import json
from datetime import date

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.model_selection import LeaveOneOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from core import db as dbmod
from core.config import load_config
from ml.events import build_event_panel, ensure_schema

WINDOWS = (7, 14, 28, 56)
FEATURES = ([f"run_km_{w}d" for w in WINDOWS]
            + [f"load_{w}d" for w in WINDOWS]
            + [f"sleep_{w}d" for w in (14, 28)]
            + [f"hrv_{w}d" for w in (14, 28)])
MIN_EVENTS = 8

def _corr(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < 3 or x.std() == 0 or y.std() == 0:
        return None, int(len(x))
    return round(float(np.corrcoef(x, y)[0, 1]), 3), int(len(x))

def run(conn, start: str, end: str) -> dict:
    ensure_schema(conn)
    ev = build_event_panel(conn, start, end)
    if ev.empty:
        return {"error": "无锚点；用 python -m ml.events --add 录入"}

    ev = ev[ev["gap_pace_s_per_km"].notna() & (ev["date_precision"] == "day")].copy()
    feats = [f for f in FEATURES if f in ev.columns]
    report = {
        "n_events": int(len(ev)),
        "events": ev[["event_date", "event_type", "gap_pace_s_per_km",
                      "effort", "equivalence", "ascent_m"]].to_dict("records"),
        "features": feats,
    }

    secondary = {}
    for col in feats:
        r, n = _corr(ev[col], ev["gap_pace_s_per_km"])
        if r is not None:
            secondary[col] = {"r": r, "n": n,
                              "note": "n<8 时仅方向参考"}
    report["secondary_correlations"] = secondary

    if len(ev) < MIN_EVENTS:
        report["error"] = (f"锚点仅 {len(ev)} 个（含非全力），至少 {MIN_EVENTS} 个才建模；"
                           "当前只给方向参考")
        return report

    X = ev[feats].to_numpy(dtype=float)
    y = ev["gap_pace_s_per_km"].to_numpy(dtype=float)
    model = Pipeline([
        ("imp", SimpleImputer(strategy="median", keep_empty_features=False)),
        ("sc", StandardScaler()),
        ("reg", Ridge(alpha=1.0)),
    ])
    preds = np.zeros(len(y))
    for tr, te in LeaveOneOut().split(X):
        model.fit(X[tr], y[tr])
        preds[te] = model.predict(X[te])
    mae = float(np.mean(np.abs(y - preds)))
    report["model"] = {"mae_s_per_km": round(mae, 2),
                       "naive_mae_s_per_km": round(float(np.mean(np.abs(y - y.mean()))), 2)}
    return report

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=date.today().isoformat())
    args = p.parse_args(argv)
    conn = dbmod.get_conn(load_config())
    try:
        report = run(conn, args.start, args.end)
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    main()
