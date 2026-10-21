from __future__ import annotations

def _cost(grade: float) -> float:
    i = grade
    return (155.4 * i ** 5 - 30.4 * i ** 4 - 43.3 * i ** 3
            + 46.3 * i ** 2 + 19.5 * i + 3.6)

def grade_ratio(distance_km: float, ascent_m: float) -> float:
    if not distance_km or not ascent_m or ascent_m <= 0:
        return 1.0
    half = distance_km / 2.0
    g = (ascent_m / 1000.0) / half
    up = _cost(g) / _cost(0.0)
    down = _cost(-g) / _cost(0.0)
    return (up + down) / 2.0

def grade_adjusted_pace(distance_km: float, time_s: float, ascent_m: float):
    if not distance_km or not time_s:
        return None, None
    raw = time_s / distance_km
    ratio = grade_ratio(distance_km, ascent_m)
    return raw / ratio, ratio
