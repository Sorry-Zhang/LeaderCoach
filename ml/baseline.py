from __future__ import annotations

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

N_CLASSES = 3

def build_models() -> dict:
    return {
        "logreg": Pipeline([
            ("imp", SimpleImputer(strategy="median", keep_empty_features=False)),
            ("sc", StandardScaler()),
            ("clf", LogisticRegression(max_iter=3000, class_weight="balanced")),
        ]),
        "gbm": Pipeline([
            ("imp", SimpleImputer(strategy="median", keep_empty_features=False)),
            ("sc", StandardScaler()),
            ("clf", GradientBoostingClassifier(n_estimators=200, max_depth=3, random_state=0)),
        ]),
    }

def features_2d(X: np.ndarray, mode: str = "last") -> np.ndarray:
    if mode == "flat":
        return X.reshape(len(X), -1)
    return X[:, -1, :]

def fit_predict(model, X: np.ndarray, y: np.ndarray, train_idx, test_idx, mode="last"):
    Xf = features_2d(X, mode)
    model.fit(Xf[train_idx], y[train_idx])
    pred = model.predict(Xf[test_idx])
    raw = model.predict_proba(Xf[test_idx])
    P = np.zeros((len(pred), N_CLASSES))
    for j, c in enumerate(model.classes_):
        P[:, int(c)] = raw[:, j]
    return np.asarray(pred, dtype=int), P
