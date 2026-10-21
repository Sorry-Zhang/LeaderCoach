from __future__ import annotations

import statistics
from datetime import date, timedelta

def completed_lengths(periods: list[dict]) -> list[int]:
    lens = []
    for p in periods:
        s = p.get("start_date")
        e = p.get("end_date")
        if s and e:
            try:
                d = (date.fromisoformat(e) - date.fromisoformat(s)).days
                if d > 0:
                    lens.append(d)
            except ValueError:
                continue
    return lens

def predict(today: date, periods: list[dict]) -> dict:
    starts = sorted(p["start_date"] for p in periods if p.get("start_date"))
    lens = completed_lengths(periods)
    n_completed = len(lens)

    result = {
        "prediction_confidence": "none",
        "phase": None,
        "cycle_day": None,
        "days_to_predicted_period": None,
        "next_start": None,
        "ovulation_est": None,
        "median_len": None,
        "window_days": None,
        "enabled": n_completed >= 3,
    }
    if n_completed < 3 or not starts:
        return result

    median_len = statistics.median(lens[-6:]) if len(lens) >= 3 else None
    median_len = round(median_len)
    last_start = date.fromisoformat(starts[-1])
    next_start = last_start + timedelta(days=median_len)
    window = max(2, round(0.15 * median_len))
    ovulation = next_start + timedelta(days=median_len - 14)

    confidence = "low" if n_completed < 6 else "medium"

    menstrual_end = last_start + timedelta(days=4)
    follicular_start = last_start + timedelta(days=5)
    ovul = ovulation
    phase = "luteal"
    cycle_day = (today - last_start).days + 1
    if today <= menstrual_end:
        phase = "menstrual"
    elif today <= ovul - timedelta(days=1):
        phase = "follicular"
    elif abs((today - ovul).days) <= 1:
        phase = "ovulation"
    else:
        phase = "luteal"

    result.update({
        "prediction_confidence": confidence,
        "phase": phase,
        "cycle_day": cycle_day,
        "days_to_predicted_period": max(0, (next_start - today).days),
        "next_start": next_start.isoformat(),
        "ovulation_est": ovulation.isoformat(),
        "median_len": median_len,
        "window_days": window,
    })
    return result

def evaluate_flags(periods: list[dict], today: date, cfg: dict) -> dict:
    lens = completed_lengths(periods)
    starts = sorted(p["start_date"] for p in periods if p.get("start_date"))
    flags = {"cycle_irregular": False, "cycle_missed": False, "luteal_phase_confound": False}

    if len(lens) >= 3 and starts:
        median = statistics.median(lens[-6:])
        recent = lens[-2:]
        if recent and all(abs(r - median) > cfg["cycle"]["cycle_anomaly_days"] for r in recent):
            flags["cycle_irregular"] = True
    if len(lens) >= 3 and starts:
        last_start = date.fromisoformat(starts[-1])
        if (today - last_start).days > cfg["cycle"]["amenorrhea_days"]:
            flags["cycle_missed"] = True

    pred = predict(today, periods)
    if pred.get("phase") == "luteal" and pred.get("prediction_confidence") in ("low", "medium"):
        flags["luteal_phase_confound"] = True

    return flags

def is_luteal(today: date, periods: list[dict]) -> bool:
    return predict(today, periods).get("phase") == "luteal"
