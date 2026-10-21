from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import balanced_accuracy_score, confusion_matrix, f1_score

LABELS = [0, 1, 2]

def multi_metrics(y_true, y_pred, proba: np.ndarray) -> dict:
    y_true = np.asarray(y_true, dtype=int)
    y_pred = np.asarray(y_pred, dtype=int)
    Y = np.eye(3)[y_true]
    return {
        "n": int(len(y_true)),
        "macro_f1": round(float(f1_score(y_true, y_pred, labels=LABELS,
                                         average="macro", zero_division=0)), 4),
        "balanced_acc": round(float(balanced_accuracy_score(y_true, y_pred)), 4),
        "brier": round(float(np.mean(np.sum((proba - Y) ** 2, axis=1))), 4),
        "confusion": confusion_matrix(y_true, y_pred, labels=LABELS).tolist(),
    }

def two_class_metrics(y_true, y_pred_fatigue) -> dict:
    y_true = np.asarray(y_true, dtype=int)
    y_pred = np.asarray(y_pred_fatigue, dtype=int)
    return {
        "n": int(len(y_true)),
        "fatigue_precision": round(_safe_div(int(((y_pred == 1) & (y_true == 1)).sum()),
                                             int((y_pred == 1).sum())), 4),
        "fatigue_recall": round(_safe_div(int(((y_pred == 1) & (y_true == 1)).sum()),
                                          int((y_true == 1).sum())), 4),
        "balanced_acc": round(float(balanced_accuracy_score(y_true, y_pred)), 4),
    }

def pillar_baseline(df: pd.DataFrame) -> dict | None:
    sub = df[["pillar_readiness", "label"]].dropna()
    if sub.empty:
        return None
    y_true = (sub["label"] == 1).astype(int).to_numpy()
    y_pred = (sub["pillar_readiness"] < 40).astype(int).to_numpy()
    return two_class_metrics(y_true, y_pred)

def _safe_div(a, b):
    return a / b if b else 0.0
