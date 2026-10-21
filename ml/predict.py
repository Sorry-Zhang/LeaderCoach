from __future__ import annotations

import argparse
import json
import pickle
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from core import db as dbmod
from core.config import load_config
from ml import db as mldb
from ml.features import build_panel

ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "model.pkl"
LABEL_NAMES = {0: "adaptation", 1: "fatigue", 2: "overtraining_risk"}

def _clean(v):
    try:
        f = float(v)
        return None if f != f else round(f, 4)
    except (TypeError, ValueError):
        return None

def predict(conn, day: str, artifact: Path = ARTIFACT) -> dict:
    with open(artifact, "rb") as f:
        art = pickle.load(f)
    model, feats, window, mode = art["model"], art["features"], art["window"], art["mode"]

    start = (pd.Timestamp(day) - pd.Timedelta(days=window + 60)).strftime("%Y-%m-%d")
    df = build_panel(conn, start, day)
    fv = df[feats].to_numpy(dtype=float)
    if len(fv) < window:
        raise RuntimeError(f"特征窗口不足（需要 {window} 天，实际 {len(fv)} 天）")

    x = fv[-1] if mode == "last" else fv[-window:].reshape(1, -1)
    raw = model.predict_proba(np.asarray(x).reshape(1, -1))[0]
    proba = np.zeros(3)
    for j, c in enumerate(model.classes_):
        proba[int(c)] = raw[j]
    state = int(np.argmax(proba))

    row = {
        "date": day,
        "state": LABEL_NAMES[state],
        "p_fatigue": float(proba[1]),
        "p_adaptation": float(proba[0]),
        "p_overtraining": float(proba[2]),
        "model_version": "gbm-" + str(art.get("feature_version", "?")),
        "feature_version": art.get("feature_version"),
        "features": json.dumps(
            dict(zip(feats, [_clean(v) for v in fv[-1]])), ensure_ascii=False),
        "baseline_readiness": float(df["pillar_readiness"].iloc[-1]) if pd.notna(df["pillar_readiness"].iloc[-1]) else None,
        "created_at": pd.Timestamp.now().isoformat(timespec="seconds"),
    }
    mldb.ensure_schema(conn)
    conn.execute(
        "INSERT INTO ml_state(date, state, p_fatigue, p_adaptation, p_overtraining, "
        "model_version, feature_version, features, baseline_readiness, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
        "state=excluded.state, p_fatigue=excluded.p_fatigue, p_adaptation=excluded.p_adaptation, "
        "p_overtraining=excluded.p_overtraining, model_version=excluded.model_version, "
        "feature_version=excluded.feature_version, features=excluded.features, "
        "baseline_readiness=excluded.baseline_readiness, created_at=excluded.created_at",
        (row["date"], row["state"], row["p_fatigue"], row["p_adaptation"],
         row["p_overtraining"], row["model_version"], row["feature_version"],
         row["features"], row["baseline_readiness"], row["created_at"]))
    conn.commit()
    return row

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    args = p.parse_args()
    cfg = load_config()
    conn = dbmod.get_conn(cfg)
    print(json.dumps(predict(conn, args.date), ensure_ascii=False, indent=2))
