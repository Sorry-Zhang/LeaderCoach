from __future__ import annotations

import numpy as np
import pandas as pd

from core.engine import compute_fitness

WARMUP_DAYS = 120

def _loads_by_day(conn, start: str, end: str) -> dict:
    out = {}
    for r in conn.execute(
        "SELECT substr(start_time,1,10) d, COALESCE(SUM(load_final),0) v FROM activities "
        "WHERE substr(start_time,1,10) BETWEEN ? AND ? GROUP BY d", (start, end)):
        out[r["d"]] = float(r["v"] or 0.0)
    for r in conn.execute(
        "SELECT date d, COALESCE(SUM(srpe_au),0) v FROM strength_sessions "
        "WHERE date BETWEEN ? AND ? GROUP BY date", (start, end)):
        out[r["d"]] = out.get(r["d"], 0.0) + float(r["v"] or 0.0)
    return out

def _table_by_day(conn, sql: str, start: str, end: str) -> dict:
    return {r["date"]: dict(r) for r in conn.execute(sql, (start, end))}

def _activity_labels_by_day(conn) -> dict:
    try:
        rows = conn.execute(
            "SELECT date, MAX(rpe) rpe, MIN(feel) feel FROM activity_labels GROUP BY date"
        ).fetchall()
    except Exception:
        return {}
    return {r["date"]: (r["rpe"], r["feel"]) for r in rows}

def _sleep_weekly_by_day(conn, days) -> dict:
    try:
        rows = conn.execute(
            "SELECT date_start, date_end, avg_hours FROM sleep_weekly").fetchall()
    except Exception:
        return {}
    out = {}
    for d in days:
        s = d.strftime("%Y-%m-%d")
        for r in rows:
            if r["date_start"] <= s <= r["date_end"]:
                out[s] = r["avg_hours"]
                break
    return out

def _action_by_day(conn, start: str, end: str) -> dict:
    out = {}
    try:
        rows = conn.execute(
            "SELECT substr(start_time,1,10) d, "
            "SUM(duration_min) dur, "
            "SUM(CASE WHEN sport_id='run' THEN COALESCE(load_final,0) ELSE 0 END) run_load, "
            "COUNT(*) n, "
            "SUM(CASE WHEN avg_hr IS NOT NULL AND max_hr IS NOT NULL AND max_hr>0 "
            "         THEN (avg_hr*1.0/max_hr)*duration_min ELSE 0 END) int_w, "
            "SUM(CASE WHEN avg_hr IS NOT NULL AND max_hr IS NOT NULL AND max_hr>0 "
            "         THEN duration_min ELSE 0 END) int_d "
            "FROM activities WHERE substr(start_time,1,10) BETWEEN ? AND ? GROUP BY d",
            (start, end)).fetchall()
    except Exception:
        return out
    for r in rows:
        out[r["d"]] = {
            "dur_day": float(r["dur"] or 0.0),
            "intensity_day": (r["int_w"] / r["int_d"]) if r["int_d"] else None,
            "run_load": float(r["run_load"] or 0.0),
            "n_sessions": int(r["n"] or 0),
        }
    return out

def build_panel(conn, start: str, end: str) -> pd.DataFrame:
    warm = (pd.Timestamp(start) - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    days = pd.date_range(warm, end, freq="D")
    idx = pd.DatetimeIndex(days)

    loads = _loads_by_day(conn, warm, end)
    series = [loads.get(d.strftime("%Y-%m-%d"), 0.0) for d in idx]
    fit = compute_fitness(series)

    dm = _table_by_day(conn, "SELECT * FROM daily_metrics WHERE date BETWEEN ? AND ?", warm, end)
    sub = _table_by_day(conn, "SELECT * FROM subjective WHERE date BETWEEN ? AND ?", warm, end)
    ed = _table_by_day(
        conn, "SELECT date, readiness_score FROM engine_daily WHERE date BETWEEN ? AND ?", warm, end)

    df = pd.DataFrame(index=idx)
    df["load"] = series
    act = _action_by_day(conn, warm, end)
    df["dur_day"] = [act.get(d.strftime("%Y-%m-%d"), {}).get("dur_day", 0.0) for d in idx]
    df["intensity_day"] = [act.get(d.strftime("%Y-%m-%d"), {}).get("intensity_day") for d in idx]
    df["run_load"] = [act.get(d.strftime("%Y-%m-%d"), {}).get("run_load", 0.0) for d in idx]
    df["n_sessions"] = [act.get(d.strftime("%Y-%m-%d"), {}).get("n_sessions", 0) for d in idx]
    df["ctl"] = [f["ctl"] for f in fit]
    df["atl"] = [f["atl"] for f in fit]
    df["tsb"] = [f["tsb"] for f in fit]
    df["hrv"] = [dm.get(d.strftime("%Y-%m-%d"), {}).get("hrv_last_night") for d in idx]
    df["rhr"] = [dm.get(d.strftime("%Y-%m-%d"), {}).get("resting_hr") for d in idx]
    df["sleep"] = [dm.get(d.strftime("%Y-%m-%d"), {}).get("sleep_hours") for d in idx]
    sw = _sleep_weekly_by_day(conn, idx)
    df["sleep_week_avg"] = [sw.get(d.strftime("%Y-%m-%d")) for d in idx]
    try:
        wx = {r["date"]: dict(r) for r in conn.execute(
            "SELECT * FROM weather_daily WHERE date BETWEEN ? AND ?", (warm, end))}
    except Exception:
        wx = {}
    df["heat_idx"] = [wx.get(d.strftime("%Y-%m-%d"), {}).get("apparent_max") for d in idx]
    df["humidity"] = [wx.get(d.strftime("%Y-%m-%d"), {}).get("humidity") for d in idx]
    labs = _activity_labels_by_day(conn)
    df["rpe_day"] = [labs.get(d.strftime("%Y-%m-%d"), (None, None))[0] for d in idx]
    df["feel_day"] = [labs.get(d.strftime("%Y-%m-%d"), (None, None))[1] for d in idx]
    df["stress"] = [dm.get(d.strftime("%Y-%m-%d"), {}).get("stress_daily_avg") for d in idx]
    df["soreness"] = [sub.get(d.strftime("%Y-%m-%d"), {}).get("soreness") for d in idx]
    df["sleep_feel"] = [sub.get(d.strftime("%Y-%m-%d"), {}).get("sleep_feel") for d in idx]
    df["alcohol_units"] = [sub.get(d.strftime("%Y-%m-%d"), {}).get("alcohol_units") for d in idx]
    df["caffeine_mg"] = [sub.get(d.strftime("%Y-%m-%d"), {}).get("caffeine_mg") for d in idx]
    df["stress_subj"] = [sub.get(d.strftime("%Y-%m-%d"), {}).get("stress") for d in idx]
    df["pillar_readiness"] = [
        ed.get(d.strftime("%Y-%m-%d"), {}).get("readiness_score") for d in idx]

    for c in ("hrv", "rhr", "sleep", "sleep_week_avg", "stress", "soreness",
              "sleep_feel", "rpe_day", "feel_day", "dur_day", "intensity_day",
              "run_load", "n_sessions", "heat_idx", "humidity",
              "alcohol_units", "caffeine_mg", "stress_subj", "pillar_readiness"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["sleep_debt_week"] = df["sleep_week_avg"] - 8.0

    df["hrv_dev"] = _dev(df["hrv"], 14)
    df["rhr_dev"] = _dev(df["rhr"], 14)

    lm7, lm28 = 0.25, 2 / 29
    e7 = e28 = 0.0
    acwr = []
    for L in series:
        e7 = L * lm7 + e7 * (1 - lm7)
        e28 = L * lm28 + e28 * (1 - lm28)
        acwr.append(round(e7 / e28, 3) if e28 > 0 else np.nan)
    df["acwr"] = acwr

    for w in (7, 14):
        df[f"hrv_dev_m{w}"] = df["hrv_dev"].rolling(w, min_periods=3).mean()
        df[f"hrv_dev_s{w}"] = df["hrv_dev"].rolling(w, min_periods=3).std()
        df[f"rhr_dev_m{w}"] = df["rhr_dev"].rolling(w, min_periods=3).mean()
        df[f"sleep_m{w}"] = df["sleep"].rolling(w, min_periods=3).mean()
        df[f"load_m{w}"] = df["load"].rolling(w, min_periods=3).mean()
        df[f"load_s{w}"] = df["load"].rolling(w, min_periods=3).std()

    df["sleep_debt_7"] = df["sleep_m7"] - df["sleep_m14"]
    df["monotony_7"] = df["load_m7"] / df["load_s7"].replace(0, np.nan)
    df["strain_7"] = df["load_m7"] * df["monotony_7"]
    df["hrv_slope_7"] = _slope(df["hrv_dev"], 7)
    df["rhr_slope_7"] = _slope(df["rhr_dev"], 7)

    return df.loc[start:end]

def _dev(s: pd.Series, window: int) -> pd.Series:
    base = s.shift(1).rolling(window, min_periods=3).mean()
    return (s - base) / base * 100

def _slope(s: pd.Series, window: int) -> pd.Series:
    x = np.arange(window, dtype=float)
    return s.rolling(window, min_periods=window).apply(
        lambda y: float(np.polyfit(x, y, 1)[0]) if np.all(np.isfinite(y)) else np.nan,
        raw=True)

FEATURES = [
    "hrv_dev", "rhr_dev", "sleep", "sleep_week_avg", "sleep_debt_week", "stress",
    "hrv_dev_m7", "hrv_dev_s7", "rhr_dev_m7", "sleep_m7",
    "hrv_dev_m14", "rhr_dev_m14", "sleep_m14",
    "sleep_debt_7", "tsb", "acwr", "monotony_7", "strain_7",
    "hrv_slope_7", "rhr_slope_7",
]
