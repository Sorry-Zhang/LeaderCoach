from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import numpy as np

from ml.baseline import build_models, fit_predict
from ml.dataset import labels_from_subjective, make_windows, walk_forward_folds
from ml.evaluate import multi_metrics, pillar_baseline
from ml.features import FEATURES, build_panel

_SCHEMA = """
CREATE TABLE daily_metrics(date TEXT PRIMARY KEY, sleep_hours REAL, sleep_deep_h REAL,
  sleep_light_h REAL, sleep_rem_h REAL, sleep_awake_h REAL, hrv_last_night REAL,
  hrv_baseline REAL, resting_hr REAL, rhr_baseline REAL, stress_daily_avg REAL, data_status TEXT);
CREATE TABLE activities(id INTEGER PRIMARY KEY AUTOINCREMENT, start_time TEXT, sport_id TEXT, load_final REAL);
CREATE TABLE subjective(date TEXT PRIMARY KEY, soreness INTEGER, sleep_feel INTEGER, injury_flag INTEGER);
CREATE TABLE engine_daily(date TEXT PRIMARY KEY, ctl REAL, atl REAL, tsb REAL, acwr REAL,
  ewma7 REAL, ewma28 REAL, readiness_score REAL);
CREATE TABLE strength_sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, srpe_au REAL);
"""

def make_db(days: int = 400, seed: int = 0):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    rng = np.random.default_rng(seed)
    base = date(2024, 3, 1)
    hrv0, rhr0 = 60.0, 50.0
    for i in range(days):
        d = (base + timedelta(days=i)).isoformat()
        load = 0.0 if rng.random() < 0.2 else float(rng.gamma(4, 60))
        hrv = hrv0 + 3 * np.sin(i / 7) + rng.normal(0, 2) - 0.02 * load
        rhr = rhr0 + 0.01 * load + rng.normal(0, 1)
        sleep = 7.5 + rng.normal(0, 0.8) - 0.002 * load
        conn.execute("INSERT INTO activities(start_time, sport_id, load_final) VALUES(?,?,?)",
                     (d, "run", load))
        conn.execute(
            "INSERT INTO daily_metrics(date, sleep_hours, hrv_last_night, resting_hr, "
            "stress_daily_avg) VALUES(?,?,?,?,?)",
            (d, sleep, hrv, rhr, 30 + rng.normal(0, 5)))
        sore = int(np.clip(round(4.0 + 0.006 * load - 0.08 * (hrv - hrv0) + rng.normal(0, 1.0)), 0, 10))
        feel = int(np.clip(round(3.2 - 0.0015 * load + 0.04 * (hrv - hrv0)
                                 + rng.normal(0, 0.6)), 1, 5))
        conn.execute("INSERT INTO subjective(date, soreness, sleep_feel, injury_flag) "
                     "VALUES(?,?,?,0)", (d, sore, feel))
        conn.execute("INSERT INTO engine_daily(date, readiness_score) VALUES(?,?)",
                     (d, float(np.clip(60 + 10 * np.sin(i / 10) + rng.normal(0, 5), 0, 100))))
    conn.commit()
    return conn, (base + timedelta(days=days - 1)).isoformat()

def main() -> None:
    import warnings
    warnings.filterwarnings("ignore", message="Skipping features without any observed values")
    conn, end = make_db()
    df = build_panel(conn, "2024-03-01", end)
    df["label"] = labels_from_subjective(df)
    X, y, _ = make_windows(df, FEATURES, 14)
    if len(X) < 30:
        raise SystemExit(f"合成数据窗口太少：{len(X)}（检查特征是否被 NaN 吞掉）")
    counts = {int(k): int(v) for k, v in zip(*np.unique(y, return_counts=True))}
    print(f"panel_days={len(df)} windows={len(X)} label_counts={counts}")

    for name, model in build_models().items():
        yt, yp, pp = [], [], []
        for tr, te in walk_forward_folds(len(X), 5):
            if len(np.unique(y[tr])) < 2:
                continue
            pred, proba = fit_predict(model, X, y, tr, te)
            yt.extend(y[te].tolist())
            yp.extend(pred.tolist())
            pp.append(proba)
        if not pp:
            print(name, "跳过：所有折都只有单一类别")
            continue
        m = multi_metrics(np.asarray(yt), np.asarray(yp), np.vstack(pp))
        m.pop("confusion", None)
        print(name, m)
    print("pillar_baseline", pillar_baseline(df))
    print("SELFTEST OK")

if __name__ == "__main__":
    main()
