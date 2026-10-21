from __future__ import annotations

import argparse
import json
from datetime import date

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from core import db as dbmod
from core.config import load_config
from ml.dataset import walk_forward_folds
from ml.features import build_panel

STATE = ["hrv_dev", "rhr_dev", "sleep_week_avg", "tsb", "acwr",
         "hrv_dev_m7", "hrv_dev_s7"]
CONFOUNDER = ["heat_idx", "humidity", "alcohol_units", "caffeine_mg", "stress_subj"]
ACTION = ["load", "dur_day", "intensity_day", "run_load", "n_sessions",
          "load_lag1", "load_lag2", "load_lag3", "intensity_lag1"]
TARGET = "hrv_dev"

def build_models() -> dict:
    return {
        "ridge": Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("sc", StandardScaler()),
            ("reg", Ridge(alpha=1.0)),
        ]),
        "gbm": Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("reg", GradientBoostingRegressor(n_estimators=200, max_depth=3,
                                              random_state=0)),
        ]),
    }

def _counterfactual(df, model, cols, loads=(0, 200, 400, 600)) -> list:
    row = df[cols].iloc[-1].to_numpy(dtype=float).copy()
    i_load = cols.index("load")
    out = []
    for L in loads:
        r = row.copy()
        r[i_load] = float(L)
        pred = float(model.predict(r.reshape(1, -1))[0])
        out.append({"load_au": L, "pred_next_hrv_dev_pct": round(pred, 2)})
    return out

def run(start: str, end: str, folds: int = 5) -> dict:
    import warnings
    warnings.filterwarnings("ignore", message="Skipping features without any observed values")
    conn = dbmod.get_conn(load_config())
    try:
        df = build_panel(conn, start, end)
    finally:
        conn.close()

    for k in (1, 2, 3):
        df[f"load_lag{k}"] = df["load"].shift(k).fillna(0.0)
    df["intensity_lag1"] = df["intensity_day"].shift(1)

    cols = STATE + CONFOUNDER + ACTION
    X = df[cols].to_numpy(dtype=float)
    y = df[TARGET].shift(-1).to_numpy(dtype=float)
    valid = np.isfinite(y)
    X, y = X[valid], y[valid]
    if len(X) < 30:
        return {"error": f"可用转移样本仅 {len(X)}，至少 30", "n": int(len(X))}

    report = {"n_samples": int(len(X)), "target": "next-day " + TARGET,
              "state": STATE, "confounder": CONFOUNDER, "action": ACTION,
              "models": {}}
    for name, model in build_models().items():
        yt, yp = [], []
        for tr, te in walk_forward_folds(len(X), folds):
            model.fit(X[tr], y[tr])
            yp.extend(model.predict(X[te]).tolist())
            yt.extend(y[te].tolist())
        yt = np.asarray(yt, dtype=float)
        yp = np.asarray(yp, dtype=float)
        corr = 0.0
        if len(yt) > 2 and yt.std() > 0 and yp.std() > 0:
            corr = float(np.corrcoef(yt, yp)[0, 1])
        report["models"][name] = {
            "mae": round(float(mean_absolute_error(yt, yp)), 3),
            "r2": round(float(r2_score(yt, yp)), 3),
            "corr": round(corr, 3),
        }

    demo = build_models()["ridge"]
    demo.fit(X, y)
    report["counterfactual_today"] = _counterfactual(df, demo, cols)
    return report

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-03-11")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--folds", type=int, default=5)
    args = p.parse_args()
    print(json.dumps(run(args.start, args.end, args.folds),
                     ensure_ascii=False, indent=2, default=str))
