from __future__ import annotations

from datetime import date, datetime, timedelta

import requests

def fetch_day_weather(lat: float, lon: float, day: str, cfg: dict) -> dict:
    d = date.fromisoformat(day[:10])
    today = date.today()
    endpoint = "archive" if d < today else "forecast"
    url = (
        f"https://api.open-meteo.com/v1/{endpoint}"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={d.isoformat()}&end_date={d.isoformat()}"
        "&daily=temperature_2m_mean,precipitation_sum,wind_speed_10m_max,weathercode"
        "&timezone=auto"
    )
    r = requests.get(url, timeout=cfg["weather"]["timeout_s"])
    r.raise_for_status()
    daily = r.json().get("daily", {})
    code = (daily.get("weathercode") or [None])[0]
    return {
        "temp_start_c": (daily.get("temperature_2m_mean") or [None])[0],
        "humidity_pct": None,
        "wind_kph": (daily.get("wind_speed_10m_max") or [None])[0],
        "weather_condition": _code_text(code),
    }

def _code_text(code) -> str | None:
    if code is None:
        return None
    mapping = {
        0: "clear", 1: "mainly_clear", 2: "partly_cloudy", 3: "overcast",
        45: "fog", 48: "fog", 51: "drizzle", 61: "rain", 63: "rain",
        65: "heavy_rain", 71: "snow", 80: "showers", 95: "thunderstorm",
    }
    return mapping.get(int(code), "unknown")
