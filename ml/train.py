from __future__ import annotations

import argparse
import json
import pickle
from datetime import date
from pathlib import Path

import numpy as np

from core import db as dbmod
from core.config import load_config
from ml import FEATURE_VERSION, db as mldb
from ml.baseline import build_models, fit_predict
from ml.dataset import labels_from_subjective, make_windows, walk_forward_folds
from ml.evaluate import multi_metrics, pillar_baseline, two_class_metrics
from ml.features import FEATURES, build_panel

def run(start: str, end: str, window: int = 14, folds: int = 5, mode: str = "last") -> dict:
    cfg = load_config()
    conn = dbmod.get_conn(cfg)
    mldb.ensure_schema(conn)

    df = build_panel(conn, start, end)
    df["label"] = labels_from_subjective(df)
    X, y, dates = make_windows(df, FEATURES, window)
    if len(X) < 30:
        return {"error": f"可用样本仅 {len(X)}，至少需要 30。请先补数据的覆盖度。",
                "coverage": _coverage(df)}

    splits = walk_forward_folds(len(X), folds)
    report = {"window": window, "mode": mode, "n_samples": int(len(X)),
              "n_folds": len(splits), "feature_version": FEATURE_VERSION,
              "models": {}, "pillar_baseline": pillar_baseline(df)}

    for name, model in build_models().items():
        yt, yp, pp = [], [], []
        for tr, te in splits:
            if len(np.unique(y[tr])) < 2:
                continue
            pred, proba = fit_predict(model, X, y, tr, te, mode=mode)
            yt.extend(y[te].tolist())
            yp.extend(pred.tolist())
            pp.append(proba)
        if not pp:
            report["models"][name] = {"error": "所有折训练集均只有单一类别"}
            continue
        pp = np.vstack(pp)
        report["models"][name] = multi_metrics(yt, yp, pp)
        mask = np.isin(np.asarray(yt), [0, 1])
        if mask.any():
            report["models"][name]["two_class"] = two_class_metrics(
                np.asarray(yt)[mask], (np.asarray(yp) == 1).astype(int)[mask])

    best_model = build_models()["gbm"]
    Xf = X[:, -1, :] if mode == "last" else X.reshape(len(X), -1)
    best_model.fit(Xf, y)
    out_dir = Path(__file__).resolve().parent / "artifacts"
    out_dir.mkdir(exist_ok=True)
    with open(out_dir / "model.pkl", "wb") as f:
        pickle.dump({"model": best_model, "features": FEATURES, "window": window,
                     "mode": mode, "feature_version": FEATURE_VERSION}, f)
    report["saved"] = str(out_dir / "model.pkl")
    conn.close()
    return report

def _coverage(df) -> dict:
    return {
        "days": int(len(df)),
        "hrv_days": int(df["hrv"].notna().sum()),
        "sleep_days": int(df["sleep"].notna().sum()),
        "rhr_days": int(df["rhr"].notna().sum()),
        "load_days": int((df["load"] > 0).sum()),
    }

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2024-03-01")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--window", type=int, default=14)
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--mode", default="last", choices=["last", "flat"])
    args = p.parse_args()
    print(json.dumps(run(args.start, args.end, args.window, args.folds, args.mode),
                     ensure_ascii=False, indent=2, default=str))
