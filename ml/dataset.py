from __future__ import annotations

import numpy as np
import pandas as pd

LABEL_MAP = {"adaptation": 0, "fatigue": 1, "overtraining_risk": 2}
LABEL_NAMES = {0: "adaptation", 1: "fatigue", 2: "overtraining_risk"}

def labels_from_subjective(df: pd.DataFrame) -> pd.Series:
    raw = []
    for _, r in df.iterrows():
        so, sf = r.get("soreness"), r.get("sleep_feel")
        rd, fd = r.get("rpe_day"), r.get("feel_day")
        if not (pd.isna(so) and pd.isna(sf)):
            if (not pd.isna(so) and so >= 6) or (not pd.isna(sf) and sf <= 2):
                raw.append("fatigue")
            else:
                raw.append("adaptation")
            continue
        if rd is not None and not pd.isna(rd):
            if rd >= 8 or (fd is not None and not pd.isna(fd) and fd <= 1):
                raw.append("fatigue")
            elif rd <= 5:
                raw.append("adaptation")
            else:
                raw.append(None)
        else:
            raw.append(None)
    raw = pd.Series(raw, index=df.index, dtype=object)
    fatigue_flag = pd.to_numeric(
        raw.map(lambda x: 1 if x == "fatigue" else (0 if x == "adaptation" else np.nan)),
        errors="coerce")
    sustained = fatigue_flag.rolling(5, min_periods=3).sum() >= 3
    ot = sustained & (df["hrv_dev_m7"] < -10) & (df["rhr_dev_m7"] > 6)
    raw = raw.where(~ot.fillna(False), "overtraining_risk")
    return raw.map(LABEL_MAP).astype("Int64")

def make_windows(df: pd.DataFrame, feature_cols: list[str], window: int = 14):
    y = df["label"]
    Xs, ys, ds = [], [], []
    fv = df[feature_cols].to_numpy(dtype=float)
    for i in range(window - 1, len(df)):
        chunk = fv[i - window + 1:i + 1]
        lab = y.iloc[i]
        if pd.isna(lab):
            continue
        Xs.append(chunk)
        ys.append(int(lab))
        ds.append(df.index[i].strftime("%Y-%m-%d"))
    return np.asarray(Xs), np.asarray(ys), ds

def walk_forward_folds(n: int, n_folds: int = 5):
    folds = []
    step = n // (n_folds + 1)
    for k in range(1, n_folds + 1):
        train_end = step * k
        test_end = min(n, step * (k + 1))
        if train_end >= test_end:
            continue
        folds.append((np.arange(0, train_end), np.arange(train_end, test_end)))
    return folds
